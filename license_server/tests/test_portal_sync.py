"""Tests for app.core.portal_client and app.core.portal_sync — pulling the
portal's users and orders into the local cache tables.

Run from the license_server/ directory (so app/ is importable):
    python -m unittest tests.test_portal_sync -v

No portal and no database: requests.get is patched, and the upsert methods are
patched where the test is about mapping or pagination rather than SQL. The
tolerant-parsing tests are the important ones here — the portal's real response
shape was never specified, so these pin down exactly which shapes are handled
and which fail loudly.
"""
import os
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("TURNSTILE_SECRET_KEY", "test-turnstile-secret")
os.environ.setdefault("ADMIN_USERNAME", "test-admin")
os.environ.setdefault("ADMIN_PASSWORD", "correct-horse-battery-staple")

PORTAL_SECRET = "portal-shared-secret-not-the-admin-one"
os.environ["PORTAL_JWT_SECRET"] = PORTAL_SECRET
os.environ["PORTAL_BASE_URL"] = "http://172.16.120.80"

import requests  # noqa: E402
from jose import jwt  # noqa: E402

from app.core import portal_client, portal_sync  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.portal_client import PortalClientError, extract_records  # noqa: E402
from app.core.portal_sync import (  # noqa: E402
    PortalSyncer,
    SyncAlreadyRunning,
    map_order,
    map_user,
)

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
UID = "018f3a2b-7c2e-7000-8000-0123456789ab"


def json_response(body, status=200):
    res = MagicMock(spec=requests.Response)
    res.ok = 200 <= status < 300
    res.status_code = status
    res.json.return_value = body
    res.text = str(body)[:500]
    return res


class EnvelopeTest(unittest.TestCase):
    """The portal's response shape was never specified, so several common
    conventions are accepted. These pin down which."""

    def test_bare_list(self):
        self.assertEqual(extract_records([{"id": 1}], "/p"), [{"id": 1}])

    def test_each_supported_wrapper_key(self):
        for key in ("items", "data", "results", "records", "users", "orders", "rows"):
            with self.subTest(key=key):
                self.assertEqual(
                    extract_records({key: [{"id": 1}], "total": 1}, "/p"), [{"id": 1}]
                )

    def test_empty_page(self):
        self.assertEqual(extract_records({"items": []}, "/p"), [])
        self.assertEqual(extract_records([], "/p"), [])

    def test_non_dict_entries_are_dropped(self):
        """A list of ids rather than objects must not crash the mapper later."""
        self.assertEqual(
            extract_records({"items": [{"id": 1}, "nope", None, 7]}, "/p"), [{"id": 1}]
        )

    def test_unrecognised_envelope_fails_loudly_and_says_what_it_saw(self):
        """Silently caching nothing would be much worse than an error — the
        message has to name the keys so the fix is obvious."""
        with self.assertRaises(PortalClientError) as ctx:
            extract_records({"payload": [{"id": 1}], "status": "ok"}, "/internal/ls/users")
        message = str(ctx.exception)
        self.assertIn("payload", message)
        self.assertIn("_RECORD_KEYS", message)

    def test_non_json_container_is_an_error(self):
        for body in ("a string", 42, None):
            with self.subTest(body=body):
                with self.assertRaises(PortalClientError):
                    extract_records(body, "/p")


class PaginationTest(unittest.TestCase):
    def setUp(self):
        p = patch.object(settings, "PORTAL_SYNC_PAGE_SIZE", 2)
        p.start()
        self.addCleanup(p.stop)

    def test_walks_until_a_short_page(self):
        pages = [
            json_response({"items": [{"id": 1}, {"id": 2}]}),
            json_response({"items": [{"id": 3}, {"id": 4}]}),
            json_response({"items": [{"id": 5}]}),
        ]
        with patch.object(requests, "get", side_effect=pages) as get:
            records = portal_client.fetch_paginated("/internal/ls/users")
        self.assertEqual([r["id"] for r in records], [1, 2, 3, 4, 5])
        self.assertEqual(get.call_count, 3)
        self.assertEqual([c.kwargs["params"]["page"] for c in get.call_args_list], [1, 2, 3])

    def test_stops_on_an_empty_page(self):
        pages = [json_response({"items": [{"id": 1}, {"id": 2}]}), json_response({"items": []})]
        with patch.object(requests, "get", side_effect=pages) as get:
            records = portal_client.fetch_paginated("/internal/ls/users")
        self.assertEqual(len(records), 2)
        self.assertEqual(get.call_count, 2)

    def test_single_short_page_is_one_request(self):
        with patch.object(requests, "get", return_value=json_response({"items": [{"id": 1}]})) as get:
            portal_client.fetch_paginated("/internal/ls/users")
        self.assertEqual(get.call_count, 1)

    def test_max_pages_caps_a_portal_that_ignores_page(self):
        """Without the cap, a portal returning a full page forever would spin
        until the process died."""
        full_page = json_response({"items": [{"id": 1}, {"id": 2}]})
        with patch.object(settings, "PORTAL_SYNC_MAX_PAGES", 5):
            with patch.object(requests, "get", return_value=full_page) as get:
                records = portal_client.fetch_paginated("/internal/ls/users")
        self.assertEqual(get.call_count, 5)
        self.assertEqual(len(records), 10)

    def test_per_page_and_q_are_sent(self):
        with patch.object(requests, "get", return_value=json_response({"items": []})) as get:
            portal_client.fetch_users(q="acme")
        params = get.call_args.kwargs["params"]
        self.assertEqual(params["per_page"], 2)
        self.assertEqual(params["q"], "acme")

    def test_q_is_omitted_when_not_given(self):
        with patch.object(requests, "get", return_value=json_response({"items": []})) as get:
            portal_client.fetch_users()
        self.assertNotIn("q", get.call_args.kwargs["params"])

    def test_urls(self):
        for fetch, expected in [
            (portal_client.fetch_users, "http://172.16.120.80/internal/ls/users"),
            (portal_client.fetch_orders, "http://172.16.120.80/internal/ls/orders"),
        ]:
            with self.subTest(expected=expected):
                with patch.object(requests, "get", return_value=json_response({"items": []})) as get:
                    fetch()
                self.assertEqual(get.call_args.args[0], expected)

    def test_request_has_a_timeout(self):
        with patch.object(requests, "get", return_value=json_response({"items": []})) as get:
            portal_client.fetch_users()
        self.assertEqual(
            get.call_args.kwargs["timeout"], settings.PORTAL_SYNC_TIMEOUT_SECONDS
        )


class ClientAuthTest(unittest.TestCase):
    def test_each_request_carries_a_fresh_reversed_pair_token(self):
        pages = [
            json_response({"items": [{"id": 1}] * settings.PORTAL_SYNC_PAGE_SIZE}),
            json_response({"items": []}),
        ]
        with patch.object(requests, "get", side_effect=pages) as get:
            portal_client.fetch_paginated("/internal/ls/users")

        tokens = [
            c.kwargs["headers"]["Authorization"].removeprefix("Bearer ")
            for c in get.call_args_list
        ]
        self.assertEqual(len(set(tokens)), 2, "token reused across pages")
        for token in tokens:
            claims = jwt.decode(
                token, PORTAL_SECRET, algorithms=["HS256"],
                issuer="license-server", audience="portal",
            )
            self.assertTrue(claims["jti"])

    def test_event_notifier_and_sync_mint_the_same_claims(self):
        """create_event_token delegates here, so the iss/aud reversal cannot
        drift between the two outbound callers."""
        from app.core.portal_events import create_event_token

        for token in (create_event_token(), portal_client.create_portal_token()):
            claims = jwt.get_unverified_claims(token)
            self.assertEqual(claims["iss"], "license-server")
            self.assertEqual(claims["aud"], "portal")

    def test_http_error_is_a_portal_client_error(self):
        with patch.object(requests, "get", return_value=json_response({}, status=500)):
            with self.assertRaises(PortalClientError):
                portal_client.fetch_users()

    def test_connection_error_is_a_portal_client_error(self):
        with patch.object(requests, "get", side_effect=requests.ConnectionError("refused")):
            with self.assertRaises(PortalClientError):
                portal_client.fetch_users()

    def test_non_json_body_is_a_portal_client_error(self):
        res = MagicMock(spec=requests.Response)
        res.ok = True
        res.status_code = 200
        res.json.side_effect = ValueError("not json")
        res.text = "<html>oops</html>"
        with patch.object(requests, "get", return_value=res):
            with self.assertRaises(PortalClientError):
                portal_client.fetch_users()


class UserMappingTest(unittest.TestCase):
    def test_canonical_record(self):
        row = map_user(
            {
                "user_uuid7": UID,
                "email": "ops@acme.example",
                "full_name": "Acme Ops",
                "organization_name": "Acme",
                "is_active": True,
                "created_at": "2026-01-01T12:00:00Z",
            },
            NOW,
        )
        self.assertEqual(row["user_uuid7"], uuid.UUID(UID))
        self.assertEqual(row["email"], "ops@acme.example")
        self.assertEqual(row["full_name"], "Acme Ops")
        self.assertEqual(row["is_active"], True)
        self.assertEqual(row["portal_created_at"].year, 2026)
        self.assertEqual(row["synced_at"], NOW)

    def test_alternate_key_names(self):
        """The portal's field names were never specified, so each column tries
        several candidates."""
        row = map_user(
            {"id": UID, "mail": "a@b.co", "name": "Nom", "company": "Corp", "active": 1},
            NOW,
        )
        self.assertEqual(row["user_uuid7"], uuid.UUID(UID))
        self.assertEqual(row["email"], "a@b.co")
        self.assertEqual(row["full_name"], "Nom")
        self.assertEqual(row["organization_name"], "Corp")
        self.assertIs(row["is_active"], True)

    def test_raw_keeps_the_whole_record(self):
        """So a wrong mapping guess loses nothing and can be corrected without
        a re-sync."""
        record = {"id": UID, "surprise_field": {"nested": [1, 2]}}
        self.assertEqual(map_user(record, NOW)["raw"], record)

    def test_record_without_a_usable_id_is_skipped(self):
        self.assertIsNone(map_user({"email": "a@b.co"}, NOW))
        self.assertIsNone(map_user({"id": "not-a-uuid"}, NOW))
        self.assertIsNone(map_user({}, NOW))

    def test_empty_strings_count_as_absent(self):
        row = map_user({"id": UID, "email": "", "name": "Real"}, NOW)
        self.assertIsNone(row["email"])
        self.assertEqual(row["full_name"], "Real")

    def test_overlong_values_are_truncated_not_fatal(self):
        """A name longer than the column is cosmetic; a failed sync is not."""
        row = map_user({"id": UID, "name": "x" * 400}, NOW)
        self.assertEqual(len(row["full_name"]), 255)

    def test_boolean_encodings(self):
        for raw, expected in [
            (True, True), (False, False), ("true", True), ("FALSE", False),
            ("yes", True), ("no", False), (1, True), (0, False),
            ("active", True), ("disabled", False), ("wat", None), (None, None),
        ]:
            with self.subTest(raw=raw):
                row = map_user({"id": UID, "is_active": raw}, NOW)
                self.assertIs(row["is_active"], expected)

    def test_timestamp_formats(self):
        for raw in ["2026-01-01T12:00:00Z", "2026-01-01T12:00:00+00:00", "2026-01-01 12:00:00"]:
            with self.subTest(raw=raw):
                self.assertIsNotNone(map_user({"id": UID, "created_at": raw}, NOW)["portal_created_at"])

    def test_epoch_timestamp(self):
        row = map_user({"id": UID, "created_at": 1767225600}, NOW)
        self.assertEqual(row["portal_created_at"].year, 2026)

    def test_unparseable_timestamp_is_dropped_not_fatal(self):
        row = map_user({"id": UID, "created_at": "last tuesday"}, NOW)
        self.assertIsNone(row["portal_created_at"])


class OrderMappingTest(unittest.TestCase):
    def test_canonical_record(self):
        row = map_order(
            {
                "order_id": 990001, "order_number": "ORD-1", "user_uuid7": UID,
                "plan_type": "plan_250", "status": "paid", "duration_days": 365,
                "created_at": "2026-01-01T12:00:00Z",
            },
            NOW,
        )
        self.assertEqual(row["order_id"], 990001)
        self.assertEqual(row["order_number"], "ORD-1")
        self.assertEqual(row["user_uuid7"], uuid.UUID(UID))
        self.assertEqual(row["duration_days"], 365)

    def test_alternate_key_names(self):
        row = map_order(
            {"id": 7, "number": "N-7", "user_id": UID, "plan": "gold", "state": "new"},
            NOW,
        )
        self.assertEqual(row["order_id"], 7)
        self.assertEqual(row["order_number"], "N-7")
        self.assertEqual(row["plan_type"], "gold")
        self.assertEqual(row["status"], "new")

    def test_string_order_id_is_coerced(self):
        self.assertEqual(map_order({"id": "42"}, NOW)["order_id"], 42)

    def test_record_without_a_usable_id_is_skipped(self):
        self.assertIsNone(map_order({"order_number": "N-1"}, NOW))
        self.assertIsNone(map_order({"id": "abc"}, NOW))

    def test_non_uuid_user_id_caches_with_a_null_user(self):
        """A portal keying users by serial id still gives a useful order row."""
        row = map_order({"id": 1, "user_id": 12345}, NOW)
        self.assertEqual(row["order_id"], 1)
        self.assertIsNone(row["user_uuid7"])

    def test_unknown_plan_is_kept_verbatim(self):
        """plan_type is the portal's vocabulary, not this server's PlanType —
        an order for a plan we do not sell must still be visible to an admin."""
        self.assertEqual(map_order({"id": 1, "plan": "enterprise_xl"}, NOW)["plan_type"], "enterprise_xl")


class SyncOrchestrationTest(unittest.TestCase):
    def setUp(self):
        self.syncer = PortalSyncer()
        self.users = patch.object(self.syncer, "_upsert_users", side_effect=lambda rows: len(rows))
        self.orders = patch.object(self.syncer, "_upsert_orders", side_effect=lambda rows: len(rows))
        self.upsert_users = self.users.start()
        self.upsert_orders = self.orders.start()
        self.addCleanup(self.users.stop)
        self.addCleanup(self.orders.stop)

    def test_successful_run_counts_both_entities(self):
        with patch.object(portal_client, "fetch_users", return_value=[{"id": UID}]), \
             patch.object(portal_client, "fetch_orders", return_value=[{"id": 1}, {"id": 2}]):
            result = self.syncer.sync(trigger="manual")
        self.assertTrue(result.ok)
        self.assertEqual(result.trigger, "manual")
        self.assertEqual((result.users.fetched, result.users.upserted), (1, 1))
        self.assertEqual((result.orders.fetched, result.orders.upserted), (2, 2))
        self.assertIsNotNone(result.finished_at)

    def test_unusable_records_are_counted_as_skipped_not_fatal(self):
        with patch.object(portal_client, "fetch_users", return_value=[{"id": UID}, {"email": "no-id@x.co"}]), \
             patch.object(portal_client, "fetch_orders", return_value=[]):
            result = self.syncer.sync()
        self.assertEqual(result.users.fetched, 2)
        self.assertEqual(result.users.upserted, 1)
        self.assertEqual(result.users.skipped, 1)
        self.assertTrue(result.ok)

    def test_a_broken_users_endpoint_still_lets_orders_sync(self):
        """The two are independent so the admin UI degrades to one list, not none."""
        with patch.object(portal_client, "fetch_users", side_effect=PortalClientError("users down")), \
             patch.object(portal_client, "fetch_orders", return_value=[{"id": 1}]):
            result = self.syncer.sync()
        self.assertFalse(result.ok)
        self.assertIn("users down", result.users.error)
        self.assertIsNone(result.orders.error)
        self.assertEqual(result.orders.upserted, 1)

    def test_a_database_failure_is_captured_not_raised(self):
        self.upsert_users.side_effect = RuntimeError("db down")
        with patch.object(portal_client, "fetch_users", return_value=[{"id": UID}]), \
             patch.object(portal_client, "fetch_orders", return_value=[]):
            result = self.syncer.sync()
        self.assertFalse(result.ok)
        self.assertIn("db down", result.users.error)

    def test_an_unexpected_fetch_error_is_captured(self):
        with patch.object(portal_client, "fetch_users", side_effect=ValueError("weird")), \
             patch.object(portal_client, "fetch_orders", return_value=[]):
            result = self.syncer.sync()
        self.assertFalse(result.ok)
        self.assertIn("weird", result.users.error)

    def test_concurrent_sync_is_refused_not_queued(self):
        """Pressing the admin sync button twice must not stack up runs that
        each re-fetch every page."""
        def slow_fetch():
            self.assertTrue(self.syncer.is_running)
            with self.assertRaises(SyncAlreadyRunning):
                self.syncer.sync()
            return []

        with patch.object(portal_client, "fetch_users", side_effect=slow_fetch), \
             patch.object(portal_client, "fetch_orders", return_value=[]):
            self.syncer.sync()

    def test_lock_is_released_after_a_failure(self):
        with patch.object(portal_client, "fetch_users", side_effect=PortalClientError("x")), \
             patch.object(portal_client, "fetch_orders", side_effect=PortalClientError("y")):
            self.syncer.sync()
        self.assertFalse(self.syncer.is_running)
        with patch.object(portal_client, "fetch_users", return_value=[]), \
             patch.object(portal_client, "fetch_orders", return_value=[]):
            self.syncer.sync()  # must not raise SyncAlreadyRunning

    def test_last_result_is_recorded_for_the_status_endpoint(self):
        self.assertIsNone(self.syncer.last_result)
        with patch.object(portal_client, "fetch_users", return_value=[]), \
             patch.object(portal_client, "fetch_orders", return_value=[]):
            self.syncer.sync(trigger="periodic")
        self.assertEqual(self.syncer.last_result.trigger, "periodic")
        self.assertIn("users", self.syncer.last_result.as_dict())

    def test_cache_users_maps_and_writes(self):
        written = self.syncer.cache_users([{"id": UID}, {"no": "id"}])
        self.assertEqual(written, 1)

    def test_cache_users_with_nothing_usable_writes_nothing(self):
        self.assertEqual(self.syncer.cache_users([{"no": "id"}]), 0)
        self.upsert_users.assert_not_called()


class DisabledTest(unittest.TestCase):
    def test_disabled_without_a_base_url(self):
        with patch.object(settings, "PORTAL_BASE_URL", ""):
            s = PortalSyncer()
            self.assertFalse(s.enabled)
            with self.assertRaises(PortalClientError):
                s.sync()
            s.start()
            self.assertIsNone(s._thread)

    def test_disabled_without_a_secret(self):
        with patch.object(settings, "PORTAL_JWT_SECRET", None):
            self.assertFalse(PortalSyncer().enabled)

    def test_zero_interval_starts_no_thread_but_allows_manual_sync(self):
        with patch.object(settings, "PORTAL_SYNC_INTERVAL_SECONDS", 0):
            s = PortalSyncer()
            s.start()
            self.assertIsNone(s._thread)
            self.assertTrue(s.enabled)

    def test_stop_is_safe_when_never_started(self):
        PortalSyncer().stop()

    def test_start_is_idempotent(self):
        s = PortalSyncer()
        with patch.object(settings, "PORTAL_SYNC_INTERVAL_SECONDS", 3600):
            s.start()
            self.addCleanup(s.stop)
            first = s._thread
            s.start()
        self.assertIs(s._thread, first)


if __name__ == "__main__":
    unittest.main()
