"""Tests for app.core.security.verify_admin_credentials.

Run from the license_server/ directory (so app/ is importable):
    python -m unittest tests.test_security -v

Required settings (DATABASE_URL, SECRET_KEY, ...) are pinned to fixed test
values *before* app.core.config is imported anywhere in the process, since
pydantic-settings env vars take precedence over the local .env file — this
keeps the test isolated from whatever real secrets happen to be on disk.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("TURNSTILE_SECRET_KEY", "test-turnstile-secret")
os.environ["ADMIN_USERNAME"] = "test-admin"
os.environ["ADMIN_PASSWORD"] = "correct-horse-battery-staple"

from app.core import security  # noqa: E402


class VerifyAdminCredentialsTest(unittest.TestCase):
    def test_correct_username_and_password_pass(self):
        self.assertTrue(
            security.verify_admin_credentials("test-admin", "correct-horse-battery-staple")
        )

    def test_wrong_username_fails(self):
        self.assertFalse(
            security.verify_admin_credentials("not-admin", "correct-horse-battery-staple")
        )

    def test_wrong_password_fails(self):
        self.assertFalse(
            security.verify_admin_credentials("test-admin", "wrong-password")
        )

    def test_both_wrong_fails(self):
        self.assertFalse(security.verify_admin_credentials("nope", "nope"))

    def test_empty_credentials_fail(self):
        self.assertFalse(security.verify_admin_credentials("", ""))

    def test_case_sensitive(self):
        self.assertFalse(
            security.verify_admin_credentials("Test-Admin", "correct-horse-battery-staple")
        )

    def test_uses_constant_time_comparison(self):
        """Guards against a regression back to `!=`/`==`: both fields must go
        through hmac.compare_digest."""
        with patch.object(
            security.hmac, "compare_digest", wraps=security.hmac.compare_digest
        ) as spy:
            security.verify_admin_credentials("test-admin", "correct-horse-battery-staple")
            self.assertEqual(spy.call_count, 2)

    def test_does_not_short_circuit_on_bad_username(self):
        """A wrong username must not skip the password check. Short-circuiting
        (e.g. `compare_digest(u, U) and compare_digest(p, P)` evaluated lazily,
        or a plain `or` between `!=` checks) would let a request with a wrong
        username return faster than one with a right username, leaking via
        timing whether the username alone was correct."""
        with patch.object(
            security.hmac, "compare_digest", wraps=security.hmac.compare_digest
        ) as spy:
            security.verify_admin_credentials("wrong-user", "correct-horse-battery-staple")
            self.assertEqual(spy.call_count, 2)

    def test_does_not_short_circuit_on_bad_password(self):
        with patch.object(
            security.hmac, "compare_digest", wraps=security.hmac.compare_digest
        ) as spy:
            security.verify_admin_credentials("test-admin", "wrong-password")
            self.assertEqual(spy.call_count, 2)


if __name__ == "__main__":
    unittest.main()
