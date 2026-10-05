"""Outbound license-event notification to the internal portal (phase 3).

After a license is created, changed, revoked or found expired, the portal is
told about it with:

    POST {PORTAL_BASE_URL}/internal/ls/license-events

signed with PORTAL_JWT_SECRET but with the iss/aud pair REVERSED relative to
the tokens the portal sends us (iss="license-server", aud="portal"). That
reversal is what stops a notification token being replayed back against our own
/internal/portal/* endpoints, which require iss="portal"/aud="license-server"
(app/core/portal_auth.py).

Delivery model
--------------
Fire-and-forget, and meant literally: nothing on the request path ever waits
for the portal, and nothing the portal does can fail a license operation.

    caller thread            queue              worker threads
    ─────────────            ─────              ──────────────
    commit the license  ->   enqueue    ->      POST, retry, give up
    return to client         (bounded)          update portal_synced_at

The payload is built synchronously, at enqueue time, while the ORM object is
still attached to the caller's live session — a detached License handed to
another thread would raise on first attribute access. What crosses the queue is
a plain dict.

Retries are deliberately NOT done with FastAPI's BackgroundTasks. Those run
inside the request's own task, so up to ~7s of backoff would occupy a
threadpool worker per event and, under a burst of license creations, starve
the pool that every other sync endpoint in this app runs on.

What this does NOT guarantee
----------------------------
Delivery. The queue is in memory and dies with the process, so an event can be
lost to a crash, a `docker compose restart`, or a queue that filled up because
the portal was down for a long time. That is the accepted cost of never
blocking a license operation on the portal's availability. The portal must
therefore treat these as best-effort hints and be able to reconcile from
GET /internal/portal/licenses/{license_id} (phase 1) rather than relying on
having seen every event.

Consumers must also be idempotent: a delivery that times out after the portal
has already processed it is retried, so the same event can legitimately arrive
twice.

Disabled unless configured
--------------------------
PORTAL_BASE_URL and PORTAL_JWT_SECRET must both be set. Otherwise the notifier
is inert — enqueue becomes a no-op, no thread is started, and the reason is
logged once at startup. That keeps a license server with no portal (and the
test suite) working unchanged.
"""
import enum
import logging
import queue
import random
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import requests
from jose import jwt
from sqlalchemy import update

from .config import settings
from .portal_client import create_portal_token

logger = logging.getLogger(__name__)

# Path on the portal that receives these events.
EVENT_PATH = "/internal/ls/license-events"

# Sentinel pushed onto the queue to wake workers for shutdown.
_SHUTDOWN = object()


class LicenseEvent(str, enum.Enum):
    """The event names the portal expects in the payload's `event` field."""

    CREATED = "license.created"
    UPDATED = "license.updated"
    REVOKED = "license.revoked"
    EXPIRED = "license.expired"


def _iso(value: Optional[datetime]) -> Optional[str]:
    """A timestamp as ISO 8601, or None.

    Columns are TIMESTAMPTZ, but a value that came back from SQLite or was
    built in Python can still be naive; those are read as UTC, which is what
    every datetime this app writes actually is (crud.py uses
    datetime.now(timezone.utc) throughout).
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def build_event_payload(event: LicenseEvent, license) -> dict[str, Any]:
    """Snapshot a License row as the portal's event payload.

    Must be called while ``license`` is still attached to an open session.

    Exactly the fields the integration spec defines, and no more: a strict
    validator on the portal's side should not have to tolerate extras. Notably
    absent, and available if the portal asks for them later:
      * organization_token — the licensed app's shared secret, never sent;
      * used_audits/used_hardens — usage, not lifecycle;
      * an event_id/occurred_at pair, which is what the portal would want if it
        prefers to dedupe on an idempotency key rather than on (event,
        license_id) state.

    `starts_at` maps to created_at, not activated_at: expires_at is computed as
    created_at + the plan's duration (crud.create_license), so created_at is
    the start of the term the customer paid for. activated_at is when the
    software was first bound to a machine, which can be much later or never,
    and pairing it with expires_at would describe a term that does not exist.
    """
    return {
        "event": event.value,
        "license_id": license.id,
        "license_key": license.license_key,
        # UUID is not JSON-serialisable; str() gives the canonical form.
        "user_uuid7": str(license.user_uuid7) if license.user_uuid7 else None,
        "order_id": license.order_id,
        # .value, so the portal sees the same lowercase vocabulary the inbound
        # endpoint accepts ("plan_250"). The Postgres column stores the
        # upper-case member name; that stays an internal detail.
        "plan_type": license.plan_type.value,
        "is_active": license.is_active,
        "is_pilot_mode": license.is_pilot_mode,
        "starts_at": _iso(license.created_at),
        "expires_at": _iso(license.expires_at),
        "meta": license.portal_meta or {},
    }


def create_event_token() -> str:
    """Mint a short-lived service token for one notification attempt.

    A FRESH token per attempt, not per event. We do single-use `jti` replay
    rejection on our own inbound endpoints (app/core/portal_auth.py), so the
    portal plausibly does too — reusing one token across a retry would then be
    refused as a replay and the retry could never succeed.

    The claims are built in app/core/portal_client.create_portal_token, which
    phase 4's sync also uses, so the iss/aud reversal that keeps our outbound
    tokens from being replayed against our own inbound endpoints is defined
    once and cannot drift between the two callers.
    """
    return create_portal_token()


def _is_retryable(status_code: int) -> bool:
    """Whether a failed POST is worth trying again.

    5xx, 429 and 408 are transient — the portal is overloaded, restarting or
    slow. Every other 4xx is the portal telling us this request is wrong
    (unknown route, bad token, schema mismatch); repeating it byte-for-byte
    cannot change the answer, so it is logged as permanent and dropped rather
    than burning three attempts and a queue slot.
    """
    return status_code >= 500 or status_code in (408, 429)


class PortalNotifier:
    """Owns the event queue and the threads that drain it."""

    def __init__(self):
        self._queue: queue.Queue = queue.Queue(
            maxsize=settings.PORTAL_EVENT_QUEUE_MAXSIZE
        )
        self._workers: list[threading.Thread] = []
        self._sweeper: Optional[threading.Thread] = None
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        # Counters, for the log line on shutdown and for tests.
        self.delivered = 0
        self.dropped = 0
        self.failed = 0

    # --- configuration ----------------------------------------------------

    @property
    def enabled(self) -> bool:
        """Both halves of the config are needed: somewhere to POST to, and a
        secret to sign with. Either missing means the integration cannot work,
        so it is off rather than half-on."""
        return bool(settings.PORTAL_BASE_URL and settings.PORTAL_JWT_SECRET)

    @property
    def event_url(self) -> str:
        return f"{settings.PORTAL_BASE_URL.rstrip('/')}{EVENT_PATH}"

    # --- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Start the worker threads (and the expiry sweeper). Idempotent."""
        with self._lock:
            if self._workers:
                return
            if not self.enabled:
                missing = []
                if not settings.PORTAL_BASE_URL:
                    missing.append("PORTAL_BASE_URL")
                if not settings.PORTAL_JWT_SECRET:
                    missing.append("PORTAL_JWT_SECRET")
                logger.warning(
                    "[portal_events] disabled: %s not set; license events will "
                    "not be sent to the portal",
                    " and ".join(missing),
                )
                return

            self._stopping.clear()
            for i in range(settings.PORTAL_EVENT_WORKERS):
                # daemon=True so a failure to shut down cleanly cannot wedge
                # the process; stop() below is the orderly path.
                thread = threading.Thread(
                    target=self._worker_loop, name=f"portal-events-{i}", daemon=True
                )
                thread.start()
                self._workers.append(thread)

            logger.info(
                "[portal_events] enabled: %d worker(s) posting to %s",
                len(self._workers),
                self.event_url,
            )

            if settings.PORTAL_EXPIRY_SWEEP_INTERVAL_SECONDS > 0:
                self._sweeper = threading.Thread(
                    target=self._sweep_loop, name="portal-expiry-sweep", daemon=True
                )
                self._sweeper.start()
                logger.info(
                    "[portal_events] expiry sweep every %ds",
                    settings.PORTAL_EXPIRY_SWEEP_INTERVAL_SECONDS,
                )

    def stop(self, timeout: Optional[float] = None) -> None:
        """Ask the workers to finish the queue, then stop.

        Called from the app's lifespan shutdown so a planned restart drains
        what it can instead of dropping everything already enqueued. Anything
        still queued when the timeout expires is lost, and is logged as such.
        """
        with self._lock:
            workers, self._workers = self._workers, []
            sweeper, self._sweeper = self._sweeper, None

        if not workers and sweeper is None:
            return

        if timeout is None:
            timeout = settings.PORTAL_EVENT_SHUTDOWN_TIMEOUT_SECONDS

        self._stopping.set()
        for _ in workers:
            # One sentinel each, so every worker wakes even if the queue is empty.
            try:
                self._queue.put_nowait(_SHUTDOWN)
            except queue.Full:
                pass

        deadline = time.monotonic() + timeout
        for thread in workers:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if sweeper is not None:
            sweeper.join(timeout=1.0)

        remaining = self._queue.qsize()
        logger.info(
            "[portal_events] stopped: %d delivered, %d failed, %d dropped, "
            "%d still queued and now lost",
            self.delivered,
            self.failed,
            self.dropped,
            remaining,
        )

    # --- producing ---------------------------------------------------------

    def notify(self, event: LicenseEvent, license) -> None:
        """Enqueue an event for a license. Never raises, never blocks.

        Call this AFTER the transaction has committed. An event for a change
        that then rolls back would tell the portal about a state that does not
        exist, and unlike a lost event that is not self-correcting.

        Any exception here is swallowed: notifying the portal must never be the
        reason a license operation fails.
        """
        if not self.enabled:
            return
        try:
            payload = build_event_payload(event, license)
        except Exception:
            logger.exception("[portal_events] could not build %s payload", event.value)
            return

        self._enqueue(payload)

    def notify_snapshot(self, payload: dict[str, Any]) -> None:
        """Enqueue a payload that was built earlier, by build_event_payload.

        For the one case where the row cannot be read at enqueue time: a hard
        delete (crud.delete_license) has to snapshot the license before the
        DELETE, but must not enqueue until the transaction has committed.
        """
        if not self.enabled:
            return
        self._enqueue(payload)

    def _enqueue(self, payload: dict[str, Any]) -> None:
        try:
            # put_nowait, never put(): blocking here would push the portal's
            # backlog onto the request path, which is the one thing this
            # design exists to prevent. A full queue drops the event loudly.
            self._queue.put_nowait((payload, 1))
        except queue.Full:
            self.dropped += 1
            logger.error(
                "[portal_events] queue full (%d): dropping %s for license_id=%s",
                settings.PORTAL_EVENT_QUEUE_MAXSIZE,
                payload.get("event"),
                payload.get("license_id"),
            )

    # --- consuming ---------------------------------------------------------

    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _SHUTDOWN:
                    return
                payload, attempt = item
                self._deliver(payload, attempt)
            except Exception:
                # A worker thread that dies stops draining the queue for good,
                # so nothing is allowed to escape this loop.
                logger.exception("[portal_events] worker error; continuing")
            finally:
                self._queue.task_done()

    def _deliver(self, payload: dict[str, Any], attempt: int) -> None:
        """One POST, with retries, until it succeeds or attempts run out."""
        event = payload.get("event")
        license_id = payload.get("license_id")
        max_attempts = settings.PORTAL_EVENT_MAX_ATTEMPTS

        while attempt <= max_attempts:
            retryable, detail = self._post(payload)
            if retryable is None:
                # Delivered.
                self.delivered += 1
                logger.info(
                    "[portal_events] delivered %s for license_id=%s (attempt %d)",
                    event,
                    license_id,
                    attempt,
                )
                self._mark_synced(license_id)
                return

            if not retryable:
                self.failed += 1
                logger.error(
                    "[portal_events] permanent failure for %s license_id=%s, "
                    "not retrying: %s",
                    event,
                    license_id,
                    detail,
                )
                return

            if attempt >= max_attempts:
                self.failed += 1
                logger.error(
                    "[portal_events] giving up on %s for license_id=%s after "
                    "%d attempts: %s",
                    event,
                    license_id,
                    attempt,
                    detail,
                )
                return

            delay = self._backoff(attempt)
            logger.warning(
                "[portal_events] attempt %d/%d failed for %s license_id=%s "
                "(%s); retrying in %.1fs",
                attempt,
                max_attempts,
                event,
                license_id,
                detail,
                delay,
            )
            # Interruptible: a shutdown does not have to wait out the backoff.
            if self._stopping.wait(timeout=delay):
                logger.warning(
                    "[portal_events] shutting down; abandoning %s for license_id=%s",
                    event,
                    license_id,
                )
                self.failed += 1
                return
            attempt += 1

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with jitter: ~1s, ~2s, ~4s from a 1s base.

        Jittered because every license created in the same burst would
        otherwise retry in lockstep and hit a struggling portal as one spike.
        """
        base = settings.PORTAL_EVENT_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
        return base * (1 + random.random() * 0.25)

    def _post(self, payload: dict[str, Any]) -> tuple[Optional[bool], str]:
        """POST once.

        Returns ``(retryable, detail)``: ``(None, "")`` on success, ``(True,
        ...)`` if worth retrying, ``(False, ...)`` if not.
        """
        try:
            response = requests.post(
                self.event_url,
                json=payload,
                headers={"Authorization": f"Bearer {create_event_token()}"},
                timeout=settings.PORTAL_EVENT_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            # Connection refused, DNS failure, timeout — all transient by nature.
            return True, f"{type(exc).__name__}: {exc}"

        if response.ok:
            return None, ""

        # Body is truncated: it is the portal's error text, and an HTML error
        # page would otherwise fill the log.
        detail = f"HTTP {response.status_code}: {response.text[:200]}"
        return _is_retryable(response.status_code), detail

    def _mark_synced(self, license_id: Optional[int]) -> None:
        """Record that the portal has been told about this license.

        Its own short session and transaction: the worker runs on a different
        thread from the request that produced the event, and that request's
        session is long gone.

        A bare UPDATE rather than a load-then-set, so that a row deleted since
        the event was queued (DELETE /api/admin/licenses/{key}/permanent, which
        emits a revoked event first) updates zero rows instead of raising.
        Failures here are logged and swallowed — portal_synced_at is
        bookkeeping, and the event has already been delivered.
        """
        if license_id is None:
            return
        # Imported here, not at module scope: app.models imports app.database,
        # and app.crud imports this module — a top-level import would close
        # that loop.
        from ..database import SessionLocal
        from ..models import License

        session = SessionLocal()
        try:
            session.execute(
                update(License)
                .where(License.id == license_id)
                .values(portal_synced_at=datetime.now(timezone.utc))
            )
            session.commit()
        except Exception:
            session.rollback()
            logger.exception(
                "[portal_events] could not update portal_synced_at for license_id=%s",
                license_id,
            )
        finally:
            session.close()

    # --- expiry sweep ------------------------------------------------------

    def _sweep_loop(self) -> None:
        """Periodically emit license.expired for licenses whose term has ended.

        Why a sweep is needed at all: nothing in this codebase acts when a
        license expires. expires_at is only ever compared against now() inside
        crud.validate_license/activate_license, i.e. lazily, when the licensed
        app happens to call in. Without this loop, `license.expired` would only
        fire for a license someone tried to use after it expired — and a
        customer who simply stops using the software (the normal case) would
        expire with the portal never being told.

        Each license is swept once: expiry_notified_at is stamped when the
        event is enqueued, and the query skips rows that already have it. That
        survives restarts, unlike any in-memory or Redis-based marker.
        """
        interval = settings.PORTAL_EXPIRY_SWEEP_INTERVAL_SECONDS
        # A short initial delay so startup is not competing with the first
        # sweep, and so a crash-loop does not sweep on every boot.
        if self._stopping.wait(timeout=min(30.0, interval)):
            return
        while not self._stopping.is_set():
            try:
                self.sweep_expired()
            except Exception:
                logger.exception("[portal_events] expiry sweep failed; continuing")
            if self._stopping.wait(timeout=interval):
                return

    def sweep_expired(self) -> int:
        """Emit license.expired for newly-expired licenses. Returns how many.

        Separated from the loop so it can be called directly by tests and, if
        it is ever wanted, from an admin endpoint or a one-off script.
        """
        from ..database import SessionLocal
        from ..models import License

        session = SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            expired = (
                session.query(License)
                .filter(
                    License.expires_at < now,
                    License.expiry_notified_at.is_(None),
                )
                .limit(settings.PORTAL_EXPIRY_SWEEP_BATCH_SIZE)
                .all()
            )
            if not expired:
                return 0

            payloads = []
            for license in expired:
                # Built while the row is still attached, as everywhere else.
                payloads.append(build_event_payload(LicenseEvent.EXPIRED, license))
                license.expiry_notified_at = now

            # Stamp first, then enqueue. The other order would re-send on every
            # sweep if the commit failed, and a duplicate expired event is
            # worse than a missed one here: the sweep is the only thing that
            # ever emits it, so a failed commit means an endless repeat.
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        for payload in payloads:
            self._enqueue(payload)

        logger.info("[portal_events] expiry sweep: enqueued %d event(s)", len(payloads))
        return len(payloads)


# The process-wide notifier. app/main.py starts and stops it; app/crud.py calls
# .notify() on it.
notifier = PortalNotifier()
