"""Tests for app.core.portal_events — outbound license-event notification.

Run from the license_server/ directory (so app/ is importable):
    python -m unittest tests.test_portal_events -v

No Postgres, no Redis and no portal are needed. requests.post is patched, and
the notifier's own DB write (portal_synced_at) is patched out, so these are
tests of the delivery machinery: payload shape, token claims, retry and backoff
policy, queue bounds, and the guarantee that nothing ever raises to the caller.

The retry tests drive _deliver() directly on the calling thread rather than
going through the worker threads, so there is nothing to wait for and no
sleeping: PORTAL_EVENT_BACKOFF_BASE_SECONDS is patched to ~0 and the backoff
call itself is asserted on separately.
"""
import os
import sys
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef-0123456789")
os.environ.setdefault("TURNSTILE_SECRET_KEY", "test-turnstile-secret")
os.environ.setdefault("ADMIN_USERNAME", "test-admin")
os.environ.setdefault("ADMIN_PASSWORD", "correct-horse-battery-staple")

PORTAL_SECRET = "portal-shared-secret-not-the-admin-one"
os.environ["PORTAL_JWT_SECRET"] = PORTAL_SECRET
os.environ["PORTAL_BASE_URL"] = "http://172.16.120.80"

import requests  # noqa: E402
import jwt  # noqa: E402

from app.core import portal_events  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.portal_events import (  # noqa: E402
    LicenseEvent,
    PortalNotifier,
    build_event_payload,
)


class FakeLicense:
    """A stand-in for a models.License row, with only what the payload reads.

    A plain object rather than a real model instance, so these tests need no
    database and no session; build_event_payload only does attribute access.
    """

    def __init__(self, **kw):
        created = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.id = kw.get("id", 42)
        self.license_key = kw.get("license_key", "A1B2-C3D4-E5F6-G7H8")
        self.user_uuid7 = kw.get("user_uuid7", uuid.UUID("018f3a2b-7c2e-7000-8000-0123456789ab"))
        self.order_id = kw.get("order_id", 990001)
        self.order_number = kw.get("order_number", "ORD-990001")
        self.plan_code = kw.get("plan_code", "NGC-BUS-250-1Y")
        self.max_assets = kw.get("max_assets", 350)
        self.addon_packs = kw.get("addon_packs", 2)
        self.is_active = kw.get("is_active", True)
        self.is_pilot_mode = kw.get("is_pilot_mode", False)
        self.created_at = kw.get("created_at", created)
        self.expires_at = kw.get("expires_at", created + timedelta(days=365))
        self.activated_at = kw.get("activated_at", None)
        self.portal_meta = kw.get("portal_meta", {"invoice": "INV-7"})


def ok_response(status=200, text="{}"):
    res = MagicMock(spec=requests.Response)
    res.ok = 200 <= status < 300
    res.status_code = status
    res.text = text
    return res


class NotifierTestCase(unittest.TestCase):
    """A fresh notifier per test, with the DB write stubbed out."""

    def setUp(self):
        self.notifier = PortalNotifier()
        patcher = patch.object(self.notifier, "_mark_synced")
        self.mark_synced = patcher.start()
        self.addCleanup(patcher.stop)
        # Near-zero backoff: the policy is asserted via _backoff() directly.
        p2 = patch.object(settings, "PORTAL_EVENT_BACKOFF_BASE_SECONDS", 0.001)
        p2.start()
        self.addCleanup(p2.stop)


class PayloadTest(unittest.TestCase):
    def test_payload_has_exactly_the_specified_fields(self):
        """Extra fields are a liability if the portal validates strictly, so
        the payload is pinned to the integration spec's field set."""
        payload = build_event_payload(LicenseEvent.CREATED, FakeLicense())
        self.assertEqual(
            set(payload),
            {
                "event", "license_id", "license_key", "user_uuid7", "order_id",
                "plan_code", "max_assets", "addon_packs", "is_active", "is_pilot_mode", "starts_at",
                "expires_at", "meta",
            },
        )

    def test_organization_token_is_never_sent(self):
        """It is the licensed app's shared secret; the portal has no use for it."""
        payload = build_event_payload(LicenseEvent.CREATED, FakeLicense())
        self.assertNotIn("organization_token", payload)

    def test_event_names(self):
        for event, expected in [
            (LicenseEvent.CREATED, "license.created"),
            (LicenseEvent.UPDATED, "license.updated"),
            (LicenseEvent.REVOKED, "license.revoked"),
            (LicenseEvent.EXPIRED, "license.expired"),
        ]:
            payload = build_event_payload(event, FakeLicense())
            self.assertEqual(payload["event"], expected)

    def test_uuid_and_plan_are_json_safe(self):
        """A UUID object is not JSON-serialisable; it must be flattened at
        build time or requests.post(json=...) raises in a worker."""
        import json

        payload = build_event_payload(LicenseEvent.CREATED, FakeLicense())
        self.assertIsInstance(payload["user_uuid7"], str)
        self.assertEqual(payload["plan_code"], "NGC-BUS-250-1Y")
        json.dumps(payload)  # must not raise

    def test_plan_carries_the_asset_entitlement(self):
        """The SKU the inbound endpoint accepts, and what it entitles."""
        payload = build_event_payload(LicenseEvent.CREATED, FakeLicense())
        self.assertEqual(
            (payload["plan_code"], payload["max_assets"], payload["addon_packs"]),
            ("NGC-BUS-250-1Y", 350, 2),
        )
        unlimited = build_event_payload(
            LicenseEvent.CREATED, FakeLicense(plan_code="NGC-ENT-ULT-1Y", max_assets=None, addon_packs=0)
        )
        self.assertIsNone(unlimited["max_assets"])

    def test_starts_at_is_created_at_so_the_term_is_consistent(self):
        lic = FakeLicense()
        payload = build_event_payload(LicenseEvent.CREATED, lic)
        self.assertEqual(payload["starts_at"], lic.created_at.isoformat())
        # starts_at..expires_at must describe the term that was actually sold.
        span = datetime.fromisoformat(payload["expires_at"]) - datetime.fromisoformat(
            payload["starts_at"]
        )
        self.assertEqual(span, timedelta(days=365))

    def test_null_user_and_order_for_a_manual_license(self):
        payload = build_event_payload(
            LicenseEvent.CREATED, FakeLicense(user_uuid7=None, order_id=None)
        )
        self.assertIsNone(payload["user_uuid7"])
        self.assertIsNone(payload["order_id"])

    def test_missing_meta_becomes_an_empty_object(self):
        payload = build_event_payload(LicenseEvent.CREATED, FakeLicense(portal_meta=None))
        self.assertEqual(payload["meta"], {})

    def test_naive_timestamps_are_read_as_utc(self):
        naive = datetime(2026, 1, 1, 12, 0)
        payload = build_event_payload(LicenseEvent.CREATED, FakeLicense(created_at=naive))
        self.assertTrue(payload["starts_at"].endswith("+00:00"))


class EventTokenTest(unittest.TestCase):
    def test_claims_are_the_reverse_of_the_inbound_pair(self):
        """The reversal is what stops one of our notification tokens being
        replayed against our own /internal/portal/* endpoints."""
        claims = jwt.decode(
            portal_events.create_event_token(),
            PORTAL_SECRET,
            algorithms=["HS256"],
            audience=settings.PORTAL_EVENT_JWT_AUDIENCE,
            issuer=settings.PORTAL_EVENT_JWT_ISSUER,
        )
        self.assertEqual(claims["iss"], "license-server")
        self.assertEqual(claims["aud"], "portal")
        # And that is the opposite of what we accept inbound.
        self.assertEqual(settings.PORTAL_JWT_ISSUER, "portal")
        self.assertEqual(settings.PORTAL_JWT_AUDIENCE, "license-server")

    def test_our_own_event_token_is_rejected_by_our_inbound_verifier(self):
        """The concrete consequence of the reversal, asserted end to end."""
        from jwt import PyJWTError as JWTError

        with self.assertRaises(JWTError):
            jwt.decode(
                portal_events.create_event_token(),
                PORTAL_SECRET,
                algorithms=["HS256"],
                audience=settings.PORTAL_JWT_AUDIENCE,
                issuer=settings.PORTAL_JWT_ISSUER,
            )

    def test_token_is_short_lived_and_has_a_jti(self):
        claims = jwt.decode(
            portal_events.create_event_token(),
            PORTAL_SECRET,
            algorithms=["HS256"],
            audience=settings.PORTAL_EVENT_JWT_AUDIENCE,
            issuer=settings.PORTAL_EVENT_JWT_ISSUER,
        )
        self.assertEqual(
            claims["exp"] - claims["iat"], settings.PORTAL_EVENT_JWT_LIFETIME_SECONDS
        )
        self.assertLessEqual(claims["exp"] - claims["iat"], 600)
        self.assertTrue(claims["jti"])

    def test_each_token_is_unique(self):
        jtis = {
            jwt.decode(portal_events.create_event_token(), options={"verify_signature": False})["jti"]
            for _ in range(5)
        }
        self.assertEqual(len(jtis), 5)

    def test_signed_with_the_portal_secret_not_the_admin_secret(self):
        from jwt import PyJWTError as JWTError

        with self.assertRaises(JWTError):
            jwt.decode(
                portal_events.create_event_token(),
                settings.SECRET_KEY,
                algorithms=["HS256"],
                audience=settings.PORTAL_EVENT_JWT_AUDIENCE,
                issuer=settings.PORTAL_EVENT_JWT_ISSUER,
            )


class DeliveryTest(NotifierTestCase):
    def test_successful_post_goes_to_the_right_url_with_a_bearer_token(self):
        with patch.object(requests, "post", return_value=ok_response()) as post:
            self.notifier._deliver(
                build_event_payload(LicenseEvent.CREATED, FakeLicense()), 1
            )
        post.assert_called_once()
        self.assertEqual(
            post.call_args.args[0],
            "http://172.16.120.80/internal/ls/license-events",
        )
        auth = post.call_args.kwargs["headers"]["Authorization"]
        self.assertTrue(auth.startswith("Bearer "))
        self.assertEqual(post.call_args.kwargs["json"]["event"], "license.created")
        self.assertEqual(self.notifier.delivered, 1)

    def test_request_has_a_timeout(self):
        """Without one, a hung portal would hold a worker thread forever."""
        with patch.object(requests, "post", return_value=ok_response()) as post:
            self.notifier._deliver(
                build_event_payload(LicenseEvent.CREATED, FakeLicense()), 1
            )
        self.assertEqual(
            post.call_args.kwargs["timeout"], settings.PORTAL_EVENT_TIMEOUT_SECONDS
        )

    def test_portal_synced_at_updated_on_success_only(self):
        with patch.object(requests, "post", return_value=ok_response()):
            self.notifier._deliver(
                build_event_payload(LicenseEvent.CREATED, FakeLicense()), 1
            )
        self.mark_synced.assert_called_once_with(42)

        self.mark_synced.reset_mock()
        with patch.object(requests, "post", return_value=ok_response(400)):
            self.notifier._deliver(
                build_event_payload(LicenseEvent.CREATED, FakeLicense()), 1
            )
        self.mark_synced.assert_not_called()

    def test_2xx_variants_all_count_as_delivered(self):
        for status in (200, 201, 202, 204):
            with self.subTest(status=status):
                n = PortalNotifier()
                with patch.object(n, "_mark_synced"), patch.object(
                    requests, "post", return_value=ok_response(status)
                ):
                    n._deliver(build_event_payload(LicenseEvent.CREATED, FakeLicense()), 1)
                self.assertEqual(n.delivered, 1)


class RetryPolicyTest(NotifierTestCase):
    def _deliver_with(self, side_effect):
        with patch.object(requests, "post", side_effect=side_effect) as post:
            self.notifier._deliver(
                build_event_payload(LicenseEvent.CREATED, FakeLicense()), 1
            )
        return post

    def test_connection_error_is_retried_to_the_attempt_limit(self):
        post = self._deliver_with(requests.ConnectionError("refused"))
        self.assertEqual(post.call_count, settings.PORTAL_EVENT_MAX_ATTEMPTS)
        self.assertEqual(self.notifier.failed, 1)
        self.assertEqual(self.notifier.delivered, 0)

    def test_timeout_is_retried(self):
        post = self._deliver_with(requests.Timeout("too slow"))
        self.assertEqual(post.call_count, settings.PORTAL_EVENT_MAX_ATTEMPTS)

    def test_5xx_is_retried(self):
        post = self._deliver_with([ok_response(503)] * 5)
        self.assertEqual(post.call_count, settings.PORTAL_EVENT_MAX_ATTEMPTS)

    def test_429_and_408_are_retried(self):
        for status in (429, 408):
            with self.subTest(status=status):
                n = PortalNotifier()
                with patch.object(n, "_mark_synced"), patch.object(
                    requests, "post", return_value=ok_response(status)
                ) as post:
                    n._deliver(build_event_payload(LicenseEvent.CREATED, FakeLicense()), 1)
                self.assertEqual(post.call_count, settings.PORTAL_EVENT_MAX_ATTEMPTS)

    def test_4xx_is_not_retried(self):
        """A 400/401/404/422 will answer the same way every time; repeating it
        only burns attempts and delays the rest of the queue."""
        for status in (400, 401, 403, 404, 422):
            with self.subTest(status=status):
                n = PortalNotifier()
                with patch.object(n, "_mark_synced"), patch.object(
                    requests, "post", return_value=ok_response(status)
                ) as post:
                    n._deliver(build_event_payload(LicenseEvent.CREATED, FakeLicense()), 1)
                self.assertEqual(post.call_count, 1)
                self.assertEqual(n.failed, 1)

    def test_recovers_on_a_later_attempt(self):
        post = self._deliver_with([ok_response(503), ok_response(200)])
        self.assertEqual(post.call_count, 2)
        self.assertEqual(self.notifier.delivered, 1)
        self.assertEqual(self.notifier.failed, 0)
        self.mark_synced.assert_called_once_with(42)

    def test_a_fresh_token_per_attempt(self):
        """If the portal does single-use jti checking (as we do inbound),
        reusing a token across a retry would be refused as a replay and the
        retry could never succeed."""
        post = self._deliver_with([ok_response(503), ok_response(503), ok_response(200)])
        tokens = [
            c.kwargs["headers"]["Authorization"].removeprefix("Bearer ")
            for c in post.call_args_list
        ]
        self.assertEqual(len(tokens), 3)
        self.assertEqual(len(set(tokens)), 3, "token was reused across attempts")
        jtis = {jwt.decode(t, options={"verify_signature": False})["jti"] for t in tokens}
        self.assertEqual(len(jtis), 3)

    def test_backoff_is_exponential_and_jittered(self):
        with patch.object(settings, "PORTAL_EVENT_BACKOFF_BASE_SECONDS", 1.0):
            d1, d2, d3 = (self.notifier._backoff(i) for i in (1, 2, 3))
        # ~1s, ~2s, ~4s, each with up to 25% jitter added.
        self.assertTrue(1.0 <= d1 < 1.25, d1)
        self.assertTrue(2.0 <= d2 < 2.5, d2)
        self.assertTrue(4.0 <= d3 < 5.0, d3)
        # Jitter must actually vary, or a burst retries in lockstep.
        with patch.object(settings, "PORTAL_EVENT_BACKOFF_BASE_SECONDS", 1.0):
            self.assertGreater(len({self.notifier._backoff(1) for _ in range(20)}), 1)

    def test_attempt_limit_is_configurable(self):
        with patch.object(settings, "PORTAL_EVENT_MAX_ATTEMPTS", 5):
            post = self._deliver_with(requests.ConnectionError("refused"))
        self.assertEqual(post.call_count, 5)

    def test_shutdown_abandons_the_backoff_instead_of_sleeping_it_out(self):
        self.notifier._stopping.set()
        post = self._deliver_with(requests.ConnectionError("refused"))
        self.assertEqual(post.call_count, 1)


class NeverRaisesTest(NotifierTestCase):
    """The contract crud.py depends on: notification cannot fail a license
    operation, whatever goes wrong."""

    def test_notify_swallows_a_broken_payload(self):
        class Exploding:
            def __getattr__(self, name):
                raise RuntimeError("boom")

        self.notifier.notify(LicenseEvent.CREATED, Exploding())  # must not raise
        self.assertEqual(self.notifier._queue.qsize(), 0)

    def test_worker_survives_a_delivery_that_raises(self):
        """A worker thread that dies stops draining the queue permanently."""
        with patch.object(self.notifier, "_deliver", side_effect=RuntimeError("boom")):
            self.notifier._queue.put_nowait(({"event": "x", "license_id": 1}, 1))
            self.notifier._queue.put_nowait(portal_events._SHUTDOWN)
            self.notifier._worker_loop()  # must return normally

    def test_full_queue_drops_instead_of_blocking(self):
        with patch.object(settings, "PORTAL_EVENT_QUEUE_MAXSIZE", 2):
            n = PortalNotifier()
            started = time.monotonic()
            for _ in range(10):
                n.notify(LicenseEvent.CREATED, FakeLicense())
            elapsed = time.monotonic() - started
        self.assertEqual(n._queue.qsize(), 2)
        self.assertEqual(n.dropped, 8)
        self.assertLess(elapsed, 1.0, "enqueue blocked on a full queue")

    def test_mark_synced_failure_does_not_lose_the_delivery(self):
        n = PortalNotifier()
        with patch.object(n, "_mark_synced", side_effect=RuntimeError("db down")):
            with patch.object(requests, "post", return_value=ok_response()):
                n._queue.put_nowait(
                    (build_event_payload(LicenseEvent.CREATED, FakeLicense()), 1)
                )
                n._queue.put_nowait(portal_events._SHUTDOWN)
                n._worker_loop()
        self.assertEqual(n.delivered, 1)


class DisabledTest(unittest.TestCase):
    def test_disabled_without_a_base_url(self):
        with patch.object(settings, "PORTAL_BASE_URL", ""):
            n = PortalNotifier()
            self.assertFalse(n.enabled)
            with patch.object(requests, "post") as post:
                n.notify(LicenseEvent.CREATED, FakeLicense())
                n.start()
            post.assert_not_called()
            self.assertEqual(n._queue.qsize(), 0)
            self.assertEqual(n._workers, [])

    def test_disabled_without_a_secret(self):
        with patch.object(settings, "PORTAL_JWT_SECRET", None):
            n = PortalNotifier()
            self.assertFalse(n.enabled)
            n.notify(LicenseEvent.CREATED, FakeLicense())
            self.assertEqual(n._queue.qsize(), 0)

    def test_stop_is_safe_when_never_started(self):
        PortalNotifier().stop()  # must not raise or hang

    def test_event_url_handles_a_trailing_slash(self):
        with patch.object(settings, "PORTAL_BASE_URL", "http://172.16.120.80/"):
            self.assertEqual(
                PortalNotifier().event_url,
                "http://172.16.120.80/internal/ls/license-events",
            )


class WorkerLifecycleTest(unittest.TestCase):
    def test_queued_events_are_delivered_by_the_worker_threads(self):
        """The one test that exercises the real threads end to end."""
        n = PortalNotifier()
        with patch.object(n, "_mark_synced"), patch.object(
            requests, "post", return_value=ok_response()
        ) as post:
            n.start()
            self.addCleanup(n.stop)
            for _ in range(5):
                n.notify(LicenseEvent.CREATED, FakeLicense())
            n._queue.join()
        self.assertEqual(post.call_count, 5)
        self.assertEqual(n.delivered, 5)

    def test_start_is_idempotent(self):
        n = PortalNotifier()
        with patch.object(requests, "post", return_value=ok_response()):
            n.start()
            self.addCleanup(n.stop)
            n.start()
        self.assertEqual(len(n._workers), settings.PORTAL_EVENT_WORKERS)

    def test_stop_drains_what_is_queued(self):
        n = PortalNotifier()
        with patch.object(n, "_mark_synced"), patch.object(
            requests, "post", return_value=ok_response()
        ) as post:
            n.start()
            for _ in range(3):
                n.notify(LicenseEvent.CREATED, FakeLicense())
            n.stop()
        self.assertEqual(post.call_count, 3)
        self.assertEqual(n._queue.qsize(), 0)


if __name__ == "__main__":
    unittest.main()
