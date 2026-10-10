"""The per-IP rate limiter (app/middleware/rate_limit.py), against a real Redis.

    REDIS_HOST=localhost DATABASE_URL=... python -m unittest tests.test_rate_limit -v
"""
import os
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef-0123456789")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

settings = RateLimitMiddleware = None


def setUpModule():
    global settings, RateLimitMiddleware
    from app.core.config import settings
    from app.middleware.rate_limit import RateLimitMiddleware


class RateLimitTest(unittest.TestCase):

    def setUp(self):
        app = FastAPI()
        app.add_middleware(RateLimitMiddleware)
        app.get("/api/ping")(lambda: {"ok": True})
        self.client = TestClient(app)
        limit = patch.object(settings, "RATE_LIMIT_PER_MINUTE", 3)
        limit.start()
        self.addCleanup(limit.stop)
        self.ip = f"198.51.100.{uuid.uuid4().int % 250 + 1}-{uuid.uuid4().hex[:6]}"

    def test_limit_answers_429_with_retry_after(self):
        headers = {"X-Real-IP": self.ip}
        codes = [self.client.get("/api/ping", headers=headers).status_code for _ in range(4)]
        self.assertEqual(codes, [200, 200, 200, 429])
        r = self.client.get("/api/ping", headers=headers)
        self.assertEqual((r.status_code, r.headers.get("retry-after")), (429, "60"))

    def test_a_forged_forwarded_for_does_not_open_a_new_bucket(self):
        codes = [self.client.get("/api/ping", headers={"X-Real-IP": self.ip,
                                                       "X-Forwarded-For": f"10.0.0.{i}"}).status_code
                 for i in range(5)]
        self.assertEqual(codes[3:], [429, 429])

    def test_other_clients_have_their_own_bucket(self):
        for _ in range(4):
            self.client.get("/api/ping", headers={"X-Real-IP": self.ip})
        other = self.ip + "-other"
        self.assertEqual(self.client.get("/api/ping", headers={"X-Real-IP": other}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
