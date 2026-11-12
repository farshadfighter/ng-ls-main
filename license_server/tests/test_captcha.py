"""The admin login's own picture check (app/captcha.py, CAPTCHA_MODE=builtin in admin_auth).

Run from the license_server/ directory with a reachable PostgreSQL (and Redis for the last test):
    DATABASE_URL=postgresql://user:pass@localhost/ls_test python -m unittest tests.test_captcha -v
"""
import hashlib
import io
import os
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef-0123456789")

import redis  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

captcha = admin_auth = None


def setUpModule():
    # Imported here, not at the top: they load the settings (see test_plans.py).
    global captcha, admin_auth
    from app import captcha
    from app.database import Base, engine
    from app.routers import admin_auth
    Base.metadata.create_all(bind=engine)


class FakeRedis:
    """Enough of redis.Redis for the picture check: SET with EX, GETDEL."""

    def __init__(self, fail=False):
        self.fail = fail
        self.data, self.ttl = {}, {}

    def set(self, key, value, ex=None):
        if self.fail:
            raise redis.RedisError("connection refused")
        self.data[key], self.ttl[key] = value, ex
        return True

    def getdel(self, key):
        if self.fail:
            raise redis.RedisError("connection refused")
        return self.data.pop(key, None)


class CaptchaTest(unittest.TestCase):

    def setUp(self):
        self.redis = FakeRedis()
        p = patch.object(captcha, "redis_client", self.redis)
        p.start()
        self.addCleanup(p.stop)

    def new(self):
        """A new picture and the characters it shows."""
        seen = []
        with patch.object(captcha, "render", side_effect=lambda text: seen.append(text) or b"png"):
            challenge_id, _ = captcha.new_challenge()
        return challenge_id, seen[0]

    def test_characters(self):
        self.assertFalse(set("0O1IL7Z2S5B8G6U4") & set(captcha.ALPHABET))
        _, answer = self.new()
        self.assertEqual(len(answer), 5)
        self.assertTrue(set(answer) <= set(captcha.ALPHABET))

    def test_kept_hashed_for_two_minutes(self):
        challenge_id, answer = self.new()
        self.assertRegex(challenge_id, r"^[A-Za-z0-9_-]{32}$")
        key = "captcha:" + challenge_id
        self.assertEqual(self.redis.data[key], hashlib.sha256(answer.encode()).hexdigest())
        self.assertNotIn(answer, self.redis.data[key])
        self.assertEqual(self.redis.ttl[key], 120)

    def test_right_answer_once_any_case_and_spacing(self):
        challenge_id, answer = self.new()
        self.assertTrue(captcha.check(challenge_id, f" {answer[:2].lower()} {answer[2:]} "))
        self.assertFalse(captcha.check(challenge_id, answer))      # spent

    def test_wrong_answer_spends_the_picture(self):
        challenge_id, answer = self.new()
        self.assertFalse(captcha.check(challenge_id, "WRONG"))
        self.assertFalse(captcha.check(challenge_id, answer))

    def test_unknown_or_malformed_id(self):
        self.assertFalse(captcha.check("", "ACDEF"))
        self.assertFalse(captcha.check("../../etc", "ACDEF"))
        self.assertFalse(captcha.check("a" * 32, "ACDEF"))
        self.redis.fail = True                                     # a malformed id never reaches Redis
        self.assertFalse(captcha.check("bad id", "ACDEF"))

    def test_redis_down_fails_closed(self):
        self.redis.fail = True
        with self.assertRaises(captcha.CaptchaUnavailable):
            captcha.new_challenge()
        with self.assertRaises(captcha.CaptchaUnavailable):
            captcha.check("a" * 32, "ACDEF")

    def test_picture(self):
        png = captcha.render("ACDEF")
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(Image.open(io.BytesIO(png)).size, (captcha.WIDTH, captcha.HEIGHT))
        self.assertNotEqual(png, captcha.render("ACDEF"))           # drawn anew each time
        self.assertLess(len(png), 20_000)


class LoginWithPictureTest(unittest.TestCase):
    """POST /api/admin/captcha and the login it guards (CAPTCHA_MODE=builtin, the default)."""

    def setUp(self):
        self.redis = FakeRedis()
        p = patch.object(captcha, "redis_client", self.redis)
        p.start()
        self.addCleanup(p.stop)
        app = FastAPI()
        app.include_router(admin_auth.router)
        self.client = TestClient(app)

    def picture(self):
        seen = []
        real = captcha.render
        with patch.object(captcha, "render", side_effect=lambda text: seen.append(text) or real(text)):
            r = self.client.post("/api/admin/captcha")
        self.assertEqual(r.status_code, 200, r.text)
        return r, seen[0]

    def login(self, challenge_id, answer):
        return self.client.post("/api/admin/login", json={
            "username": "nobody-" + uuid.uuid4().hex[:6], "password": "Wrong-Password-1",
            "captcha_id": challenge_id, "captcha_answer": answer})

    def test_picture_endpoint(self):
        r, _ = self.picture()
        self.assertEqual(r.headers["cache-control"], "no-store")
        body = r.json()
        self.assertEqual(body["expires_in"], 120)
        self.assertTrue(body["image"].startswith("data:image/png;base64,"))
        self.assertRegex(body["id"], r"^[A-Za-z0-9_-]{32}$")

    def test_login_needs_the_right_characters_once(self):
        r, answer = self.picture()
        challenge_id = r.json()["id"]
        self.assertEqual(self.login(challenge_id, "").status_code, 400)       # nothing typed: kept
        # right characters: the check passes and the (wrong) password is what refuses
        self.assertEqual(self.login(challenge_id, answer.lower()).status_code, 401)
        again = self.login(challenge_id, answer)                               # spent
        self.assertEqual(again.status_code, 400)
        self.assertIn("picture", again.json()["detail"])

        r, answer = self.picture()
        self.assertEqual(self.login(r.json()["id"], "XXXXX").status_code, 400)
        self.assertEqual(self.login(r.json()["id"], answer).status_code, 400)  # one guess per picture

    def test_redis_down_refuses_login(self):
        self.redis.fail = True
        self.assertEqual(self.client.post("/api/admin/captcha").status_code, 503)
        self.assertEqual(self.login("a" * 32, "ACDEF").status_code, 503)

    def test_answer_length_is_bounded(self):
        self.assertEqual(self.login("a" * 32, "A" * 17).status_code, 422)


class RealRedisTest(unittest.TestCase):
    """The same round trip against the Redis CI runs (GETDEL needs Redis 6.2 or later)."""

    def test_round_trip(self):
        try:
            captcha.redis_client.ping()
        except redis.RedisError:
            self.skipTest("no Redis reachable")
        seen = []
        with patch.object(captcha, "render", side_effect=lambda text: seen.append(text) or b"png"):
            challenge_id, _ = captcha.new_challenge()
        self.assertGreater(captcha.redis_client.ttl("captcha:" + challenge_id), 100)
        self.assertTrue(captcha.check(challenge_id, seen[0]))
        self.assertFalse(captcha.check(challenge_id, seen[0]))
        self.assertIsNone(captcha.redis_client.get("captcha:" + challenge_id))


if __name__ == "__main__":
    unittest.main()
