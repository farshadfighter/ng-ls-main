"""Sync portal users and orders into the local cache tables (phase 4).

    GET {PORTAL_BASE_URL}/internal/ls/users   -> portal_users_cache
    GET {PORTAL_BASE_URL}/internal/ls/orders  -> portal_orders_cache

Runs every PORTAL_SYNC_INTERVAL_SECONDS (900 = 15 minutes) on a background
thread, and on demand from POST /api/admin/portal/sync. The cached rows back
the admin UI's license form, so an admin creating a license by hand can pick
the portal user and order to link it to.

A cache, not a replica
----------------------
Nothing here is authoritative and nothing is written back to the portal. The
tables can be truncated and re-synced at any time, which is why the upsert
below never deletes and why no foreign key points at them (see the model
docstrings in app/models.py).

One consequence worth knowing: a user or order DELETED on the portal is not
removed here — the sync only ever inserts and updates. It would go stale
rather than disappear, and an admin could still pick it. Pruning rows absent
from a full sync is possible (``synced_at`` is effectively a last-seen
timestamp) but is deliberately not done: one portal response that is empty for
the wrong reason would empty the cache, and a stale extra row is a much
cheaper failure than a cache that silently wipes itself.

Field mapping
-------------
The portal's field names were not specified and this could not be tested
against the real portal, so each column lists several plausible source keys and
takes the first one present (see USER_FIELDS / ORDER_FIELDS). The full record
also goes into the ``raw`` JSONB column, so a wrong guess loses nothing and can
be corrected from cached data without re-syncing.

A record with no usable primary key is skipped and counted, never fatal: one
malformed row must not abandon the rest of the page.
"""
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy.dialects.postgresql import insert as pg_insert

from . import portal_client
from .config import settings
from .portal_client import PortalClientError

logger = logging.getLogger(__name__)


# --- field mapping ---------------------------------------------------------
# column -> candidate keys in the portal's record, tried in order.
# Add to these (not to the parsing code) when the portal's real shape is known.

USER_FIELDS: dict[str, tuple[str, ...]] = {
    "user_uuid7": ("user_uuid7", "uuid7", "uuid", "user_uuid", "id"),
    "email": ("email", "email_address", "mail"),
    "full_name": ("full_name", "name", "fullname", "display_name"),
    "organization_name": (
        "organization_name", "organization", "org_name", "company",
        "company_name",
    ),
    "is_active": ("is_active", "active", "enabled"),
    "portal_created_at": ("created_at", "created", "date_joined", "createdAt"),
}

ORDER_FIELDS: dict[str, tuple[str, ...]] = {
    "order_id": ("order_id", "id"),
    "order_number": ("order_number", "number", "code", "reference"),
    "user_uuid7": ("user_uuid7", "user_uuid", "uuid7", "user_id", "user"),
    "plan_type": ("plan_type", "plan", "plan_code", "product"),
    "status": ("status", "state", "order_status"),
    "duration_days": ("duration_days", "duration", "days", "period_days"),
    "portal_created_at": ("created_at", "created", "placed_at", "createdAt"),
}


def _first(record: dict, keys: tuple[str, ...]) -> Any:
    """First non-None value among `keys`. Empty strings count as absent, since
    a portal sending "" for an unset field means the same as omitting it."""
    for key in keys:
        value = record.get(key)
        if value is not None and value != "":
            return value
    return None


def _as_uuid(value: Any) -> Optional[uuid.UUID]:
    """A UUID, or None if the value is not one.

    Returns None rather than raising: an order whose user_id is an integer (a
    portal keying users by serial id, not UUID) still caches usefully with a
    null user_uuid7, and `raw` keeps the original either way.
    """
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _as_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _as_bool(value: Any) -> Optional[bool]:
    """Tolerates the several ways a portal might encode a boolean."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    lowered = str(value).strip().lower()
    if lowered in ("true", "t", "yes", "y", "1", "active", "enabled"):
        return True
    if lowered in ("false", "f", "no", "n", "0", "inactive", "disabled"):
        return False
    return None


def _as_datetime(value: Any) -> Optional[datetime]:
    """Parse a timestamp, or None.

    datetime.fromisoformat handles what a JSON API normally emits. Python 3.11+
    accepts a trailing "Z", but it is normalised anyway so the same code works
    if this ever runs on an older interpreter. A value that will not parse is
    dropped to None rather than failing the record — it is display metadata,
    and the original survives in `raw`.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    # Epoch seconds, which some portals send for created_at.
    as_number = _as_int(value)
    if as_number is not None and as_number > 0:
        try:
            return datetime.fromtimestamp(as_number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _truncate(value: Any, limit: int) -> Optional[str]:
    """Fit a value into a VARCHAR column instead of letting the INSERT fail.

    A portal name longer than our column is a cosmetic problem; an aborted
    sync is not. The untruncated value stays in `raw`.
    """
    if value is None:
        return None
    text = str(value)
    if len(text) > limit:
        logger.debug("[portal_sync] truncating a %d-char value to %d", len(text), limit)
        return text[:limit]
    return text


def map_user(record: dict, now: datetime) -> Optional[dict]:
    """A portal user record as a portal_users_cache row, or None if unusable."""
    user_uuid7 = _as_uuid(_first(record, USER_FIELDS["user_uuid7"]))
    if user_uuid7 is None:
        return None
    return {
        "user_uuid7": user_uuid7,
        "email": _truncate(_first(record, USER_FIELDS["email"]), 255),
        "full_name": _truncate(_first(record, USER_FIELDS["full_name"]), 255),
        "organization_name": _truncate(
            _first(record, USER_FIELDS["organization_name"]), 255
        ),
        "is_active": _as_bool(_first(record, USER_FIELDS["is_active"])),
        "portal_created_at": _as_datetime(
            _first(record, USER_FIELDS["portal_created_at"])
        ),
        "raw": record,
        "synced_at": now,
    }


def map_order(record: dict, now: datetime) -> Optional[dict]:
    """A portal order record as a portal_orders_cache row, or None if unusable."""
    order_id = _as_int(_first(record, ORDER_FIELDS["order_id"]))
    if order_id is None:
        return None
    return {
        "order_id": order_id,
        "order_number": _truncate(_first(record, ORDER_FIELDS["order_number"]), 64),
        "user_uuid7": _as_uuid(_first(record, ORDER_FIELDS["user_uuid7"])),
        "plan_type": _truncate(_first(record, ORDER_FIELDS["plan_type"]), 32),
        "status": _truncate(_first(record, ORDER_FIELDS["status"]), 32),
        "duration_days": _as_int(_first(record, ORDER_FIELDS["duration_days"])),
        "portal_created_at": _as_datetime(
            _first(record, ORDER_FIELDS["portal_created_at"])
        ),
        "raw": record,
        "synced_at": now,
    }


@dataclass
class EntityResult:
    """Outcome for one entity (users or orders) in a sync run."""

    fetched: int = 0
    upserted: int = 0
    skipped: int = 0
    error: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "fetched": self.fetched,
            "upserted": self.upserted,
            "skipped": self.skipped,
            "error": self.error,
        }


@dataclass
class SyncResult:
    """Outcome of a whole sync run, returned by the admin trigger endpoint."""

    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: Optional[datetime] = None
    duration_seconds: float = 0.0
    trigger: str = "periodic"
    users: EntityResult = field(default_factory=EntityResult)
    orders: EntityResult = field(default_factory=EntityResult)

    @property
    def ok(self) -> bool:
        return self.users.error is None and self.orders.error is None

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "trigger": self.trigger,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_seconds": round(self.duration_seconds, 3),
            "users": self.users.as_dict(),
            "orders": self.orders.as_dict(),
        }


class SyncAlreadyRunning(RuntimeError):
    """A sync is in progress; the caller asked for another one."""


class PortalSyncer:
    """Owns the periodic sync thread and the manual-trigger entry point."""

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()
        # Non-blocking: a manual trigger arriving mid-sync is refused (409)
        # rather than queued, so an impatient admin cannot stack up runs that
        # each re-fetch every page.
        self._running = threading.Lock()
        self._lifecycle = threading.Lock()
        self.last_result: Optional[SyncResult] = None

    @property
    def enabled(self) -> bool:
        """Both a target and a signing secret are needed; either missing means
        the sync cannot work, so it is off rather than half-on."""
        return bool(settings.PORTAL_BASE_URL and settings.PORTAL_JWT_SECRET)

    @property
    def is_running(self) -> bool:
        return self._running.locked()

    # --- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Start the periodic thread. Idempotent; a no-op when disabled."""
        with self._lifecycle:
            if self._thread is not None:
                return
            if not self.enabled:
                missing = []
                if not settings.PORTAL_BASE_URL:
                    missing.append("PORTAL_BASE_URL")
                if not settings.PORTAL_JWT_SECRET:
                    missing.append("PORTAL_JWT_SECRET")
                logger.warning(
                    "[portal_sync] disabled: %s not set; the portal user/order "
                    "cache will stay empty",
                    " and ".join(missing),
                )
                return
            if settings.PORTAL_SYNC_INTERVAL_SECONDS <= 0:
                logger.info(
                    "[portal_sync] periodic sync disabled "
                    "(PORTAL_SYNC_INTERVAL_SECONDS=0); manual sync still works"
                )
                return

            self._stopping.clear()
            self._thread = threading.Thread(
                target=self._loop, name="portal-sync", daemon=True
            )
            self._thread.start()
            logger.info(
                "[portal_sync] enabled: syncing users and orders from %s every %ds",
                settings.PORTAL_BASE_URL,
                settings.PORTAL_SYNC_INTERVAL_SECONDS,
            )

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the thread and wait briefly. Safe if never started."""
        with self._lifecycle:
            thread, self._thread = self._thread, None
        self._stopping.set()
        if thread is not None:
            thread.join(timeout=timeout)
            logger.info("[portal_sync] stopped")

    def _loop(self) -> None:
        interval = settings.PORTAL_SYNC_INTERVAL_SECONDS
        # A short delay before the first run so startup is not competing with a
        # full sync, and a crash-loop does not hammer the portal on every boot.
        if self._stopping.wait(timeout=min(15.0, interval)):
            return
        while not self._stopping.is_set():
            try:
                self.sync(trigger="periodic")
            except SyncAlreadyRunning:
                logger.info("[portal_sync] previous run still going; skipping this tick")
            except Exception:
                # Nothing may escape: a dead thread means no sync until restart.
                logger.exception("[portal_sync] sync failed; will retry next interval")
            if self._stopping.wait(timeout=interval):
                return

    # --- syncing ----------------------------------------------------------

    def sync(self, trigger: str = "manual") -> SyncResult:
        """Run one full sync of users and orders.

        Raises SyncAlreadyRunning if one is already in progress, and
        PortalClientError if the integration is not configured. It does NOT
        raise for a portal or database failure during the run: those are
        captured per entity in the result, so a broken /users endpoint still
        lets /orders finish syncing.
        """
        if not self.enabled:
            raise PortalClientError(
                "portal sync is not configured (PORTAL_BASE_URL and "
                "PORTAL_JWT_SECRET must both be set)"
            )
        if not self._running.acquire(blocking=False):
            raise SyncAlreadyRunning("a portal sync is already running")

        result = SyncResult(trigger=trigger)
        started = time.monotonic()
        try:
            # Independent, and deliberately so: orders are still worth caching
            # when the users endpoint is broken, and vice versa. The admin UI
            # degrades to one list instead of none.
            self._sync_entity(
                "users",
                result.users,
                portal_client.fetch_users,
                map_user,
                self._upsert_users,
            )
            self._sync_entity(
                "orders",
                result.orders,
                portal_client.fetch_orders,
                map_order,
                self._upsert_orders,
            )
        finally:
            result.finished_at = datetime.now(timezone.utc)
            result.duration_seconds = time.monotonic() - started
            self.last_result = result
            self._running.release()

        logger.info(
            "[portal_sync] %s sync finished in %.1fs: users %d/%d upserted "
            "(%d skipped), orders %d/%d upserted (%d skipped)%s",
            trigger,
            result.duration_seconds,
            result.users.upserted,
            result.users.fetched,
            result.users.skipped,
            result.orders.upserted,
            result.orders.fetched,
            result.orders.skipped,
            "" if result.ok else " WITH ERRORS",
        )
        return result

    def _sync_entity(
        self,
        name: str,
        outcome: EntityResult,
        fetch: Callable[[], list[dict]],
        mapper: Callable[[dict, datetime], Optional[dict]],
        upsert: Callable[[list[dict]], int],
    ) -> None:
        try:
            records = fetch()
        except PortalClientError as exc:
            outcome.error = str(exc)
            logger.error("[portal_sync] could not fetch %s: %s", name, exc)
            return
        except Exception as exc:
            outcome.error = f"{type(exc).__name__}: {exc}"
            logger.exception("[portal_sync] unexpected error fetching %s", name)
            return

        outcome.fetched = len(records)
        now = datetime.now(timezone.utc)
        rows = []
        for record in records:
            mapped = mapper(record, now)
            if mapped is None:
                outcome.skipped += 1
                continue
            rows.append(mapped)

        if outcome.skipped:
            logger.warning(
                "[portal_sync] skipped %d %s record(s) with no usable id; "
                "sample keys seen: %s",
                outcome.skipped,
                name,
                sorted(records[0])[:12] if records else [],
            )

        if not rows:
            return

        try:
            outcome.upserted = upsert(rows)
        except Exception as exc:
            outcome.error = f"{type(exc).__name__}: {exc}"
            logger.exception("[portal_sync] could not write %s to the cache", name)

    # --- writing ----------------------------------------------------------

    def cache_users(self, records: list[dict]) -> int:
        """Map and cache portal user records fetched outside a full sync.

        Used by the admin browse endpoint's `refresh=true` live lookup, so it
        writes through the same mapping and upsert as the periodic sync rather
        than reaching into private methods.

        Returns the number of rows written; records with no usable id are
        skipped, as in a normal sync.
        """
        now = datetime.now(timezone.utc)
        rows = [row for row in (map_user(r, now) for r in records) if row is not None]
        if not rows:
            return 0
        return self._upsert_users(rows)

    def _upsert_users(self, rows: list[dict]) -> int:
        from ..models import PortalUserCache

        return self._upsert(PortalUserCache, rows, ["user_uuid7"])

    def _upsert_orders(self, rows: list[dict]) -> int:
        from ..models import PortalOrderCache

        return self._upsert(PortalOrderCache, rows, ["order_id"])

    def _upsert(self, model, rows: list[dict], pk: list[str]) -> int:
        """INSERT ... ON CONFLICT DO UPDATE, in chunks.

        A real upsert rather than read-then-write: the portal is the source of
        truth, so every fetched row is applied whether or not we already had
        it, in one statement per chunk instead of one SELECT per record.

        Chunked because a single INSERT carrying every row of a large portal
        would build an enormous statement and can exceed the driver's bind
        parameter limit.

        Deduplicated within each chunk: Postgres refuses an ON CONFLICT
        statement that touches the same key twice ("cannot affect row a second
        time"), and a portal that returns the same record on two pages — easy
        to do when rows are being written while we paginate — would otherwise
        fail the whole sync. Last occurrence wins.

        Imported inside the method: app.models imports app.database, and this
        module is imported from the routers, so a module-level import would
        widen the import graph for no benefit.
        """
        from ..database import SessionLocal

        session = SessionLocal()
        written = 0
        try:
            chunk_size = settings.PORTAL_SYNC_UPSERT_CHUNK_SIZE
            for start in range(0, len(rows), chunk_size):
                chunk = rows[start : start + chunk_size]

                deduped: dict[tuple, dict] = {}
                for row in chunk:
                    deduped[tuple(row[k] for k in pk)] = row
                chunk = list(deduped.values())

                statement = pg_insert(model).values(chunk)
                # Every column except the primary key is refreshed from the
                # portal, synced_at included, so synced_at doubles as a
                # last-seen timestamp per row.
                updatable = {
                    c.name: statement.excluded[c.name]
                    for c in model.__table__.columns
                    if c.name not in pk
                }
                session.execute(
                    statement.on_conflict_do_update(index_elements=pk, set_=updatable)
                )
                written += len(chunk)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        return written


# Process-wide syncer. app/main.py starts and stops it; the admin router
# triggers it and reads last_result.
syncer = PortalSyncer()
