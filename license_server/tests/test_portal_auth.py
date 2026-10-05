"""Tests for app.core.portal_auth — the IP whitelist and portal JWT checks
guarding /internal/portal/*.

Run from the license_server/ directory (so app/ is importable):
    python -m unittest tests.test_portal_auth -v

Same env-pinning trick as tests/test_security.py: required settings are set
*before* app.core.config is imported, since pydantic-settings env vars take
precedence over the local .env file, so the test never picks up real secrets.

Neither Postgres nor Redis is needed. The app under test is a bare FastAPI app
carrying only this router's two dependencies, so nothing touches the database,
and the Redis client is replaced with a fake for each test. That keeps the
auth layer testable without the compose stack up.
"""
import base64
import json
import os
import sys
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("TURNSTILE_SECRET_KEY", "test-turnstile-secret")
os.environ.setdefault("ADMIN_USERNAME", "test-admin")
os.environ.setdefault("ADMIN_PASSWORD", "correct-horse-battery-staple")

PORTAL_SECRET = "portal-shared-secret-not-the-admin-one"
ALLOWED_IP = "172.16.120.80"

os.environ["PORTAL_JWT_SECRET"] = PORTAL_SECRET
os.environ["PORTAL_IP_WHITELIST"] = ALLOWED_IP

import redis  # noqa: E402
from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from jose import jwt  # noqa: E402

from app.core import portal_auth  # noqa: E402
from app.core.config import settings  # noqa: E402


class FakeRedis:
    """Enough of redis.Redis for the jti replay check: SET NX semantics."""

    def __init__(self, fail=False):
        self.store = {}
        self.fail = fail
        self.calls = []

    def set(self, key, value, nx=False, ex=None):
        if self.fail:
            raise redis.RedisError("connection refused")
        self.calls.append((key, ex))
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True


def make_app():
    """A minimal app with exactly this router's dependency stack."""
    app = FastAPI()

    @app.get(
        "/internal/portal/ping",
        dependencies=[
            Depends(portal_auth.require_portal_ip),
            Depends(portal_auth.verify_portal_jwt),
        ],
    )
    def ping():
        return {"ok": True}

    return app


def make_token(
    secret=PORTAL_SECRET,
    issuer=None,
    audience=None,
    lifetime=300,
    iat_offset=0,
    jti="sentinel",
    omit=(),
):
    """A portal-style token, with knobs for each thing that should be rejected."""
    now = int(time.time())
    claims = {
        "iss": settings.PORTAL_JWT_ISSUER if issuer is None else issuer,
        "aud": settings.PORTAL_JWT_AUDIENCE if audience is None else audience,
        "iat": now + iat_offset,
        "exp": now + iat_offset + lifetime,
        "jti": jti,
    }
    for claim in omit:
        claims.pop(claim, None)
    return jwt.encode(claims, secret, algorithm="HS256")


class PortalAuthTestCase(unittest.TestCase):
    def setUp(self):
        self.fake_redis = FakeRedis()
        patcher = patch.object(portal_auth, "redis_client", self.fake_redis)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(make_app())

    def call(self, token=None, ip=ALLOWED_IP, headers=None):
        hdrs = dict(headers or {})
        if ip is not None:
            # What nginx sets from $remote_addr for location /internal/.
            hdrs["X-Real-IP"] = ip
        if token is not None:
            hdrs["Authorization"] = f"Bearer {token}"
        return self.client.get("/internal/portal/ping", headers=hdrs)


class IpWhitelistTest(PortalAuthTestCase):
    def test_whitelisted_ip_with_valid_token_passes(self):
        res = self.call(make_token(jti=str(uuid.uuid4())))
        self.assertEqual(res.status_code, 200, res.text)

    def test_non_whitelisted_ip_is_forbidden(self):
        res = self.call(make_token(jti=str(uuid.uuid4())), ip="10.0.0.5")
        self.assertEqual(res.status_code, 403)

    def test_ip_is_checked_before_the_token(self):
        """A caller from the wrong network gets 403, not 401 — the token is
        never parsed, so a bad address cannot be probed for token feedback."""
        res = self.call("garbage-not-a-jwt", ip="10.0.0.5")
        self.assertEqual(res.status_code, 403)

    def test_spoofed_x_forwarded_for_does_not_grant_access(self):
        """The core regression guard.

        uvicorn runs with --forwarded-allow-ips "*", which makes
        request.client.host the left-most X-Forwarded-For entry — i.e.
        caller-controlled. The whitelist must read X-Real-IP (which nginx
        overwrites) instead, so this must stay a 403.
        """
        res = self.call(
            make_token(jti=str(uuid.uuid4())),
            ip="10.0.0.5",
            headers={"X-Forwarded-For": f"{ALLOWED_IP}, 10.0.0.5"},
        )
        self.assertEqual(res.status_code, 403)

    def test_unparseable_source_address_is_forbidden(self):
        res = self.call(make_token(jti=str(uuid.uuid4())), ip="not-an-ip")
        self.assertEqual(res.status_code, 403)

    def test_empty_whitelist_allows_nobody(self):
        with patch.object(settings, "PORTAL_IP_WHITELIST", ""):
            res = self.call(make_token(jti=str(uuid.uuid4())))
        self.assertEqual(res.status_code, 403)

    def test_cidr_entry_is_honoured(self):
        with patch.object(settings, "PORTAL_IP_WHITELIST", "172.16.120.0/24"):
            res = self.call(make_token(jti=str(uuid.uuid4())), ip="172.16.120.99")
        self.assertEqual(res.status_code, 200, res.text)

    def test_malformed_whitelist_fails_closed(self):
        with patch.object(settings, "PORTAL_IP_WHITELIST", "not-a-network"):
            res = self.call(make_token(jti=str(uuid.uuid4())))
        self.assertEqual(res.status_code, 503)


class PortalTokenTest(PortalAuthTestCase):
    def test_missing_token_is_unauthorized(self):
        self.assertEqual(self.call(None).status_code, 401)

    def test_garbage_token_is_unauthorized(self):
        self.assertEqual(self.call("not-a-jwt").status_code, 401)

    def test_wrong_secret_is_rejected(self):
        token = make_token(secret="some-other-secret", jti=str(uuid.uuid4()))
        self.assertEqual(self.call(token).status_code, 401)

    def test_admin_secret_cannot_sign_a_portal_token(self):
        """The two secrets are separate on purpose: an admin session token, or
        anything signed with SECRET_KEY, must not open the portal endpoints."""
        token = make_token(secret=settings.SECRET_KEY, jti=str(uuid.uuid4()))
        self.assertEqual(self.call(token).status_code, 401)

    def test_expired_token_is_rejected(self):
        # Far enough in the past to clear PORTAL_JWT_LEEWAY_SECONDS.
        token = make_token(lifetime=60, iat_offset=-600, jti=str(uuid.uuid4()))
        self.assertEqual(self.call(token).status_code, 401)

    def test_wrong_audience_is_rejected(self):
        """python-jose skips aud verification unless audience= is passed to
        decode; this fails if that argument is ever dropped."""
        token = make_token(audience="some-other-service", jti=str(uuid.uuid4()))
        self.assertEqual(self.call(token).status_code, 401)

    def test_wrong_issuer_is_rejected(self):
        token = make_token(issuer="not-the-portal", jti=str(uuid.uuid4()))
        self.assertEqual(self.call(token).status_code, 401)

    def test_token_without_exp_is_rejected(self):
        """require_exp: without it, jose has nothing to verify and a token with
        no expiry would be valid forever."""
        token = make_token(omit=("exp",), jti=str(uuid.uuid4()))
        self.assertEqual(self.call(token).status_code, 401)

    def test_token_without_iat_is_rejected(self):
        token = make_token(omit=("iat",), jti=str(uuid.uuid4()))
        self.assertEqual(self.call(token).status_code, 401)

    def test_overlong_lifetime_is_rejected(self):
        """Unexpired but minted to live far past the integration's 5 minutes."""
        lifetime = settings.PORTAL_JWT_MAX_LIFETIME_SECONDS + 3600
        token = make_token(lifetime=lifetime, jti=str(uuid.uuid4()))
        self.assertEqual(self.call(token).status_code, 401)

    def test_token_without_jti_is_rejected(self):
        token = make_token(omit=("jti",), jti=None)
        self.assertEqual(self.call(token).status_code, 401)

    def test_alg_none_token_is_rejected(self):
        """The classic JWT downgrade: a token whose header claims no signature.

        Hand-assembled rather than built with jwt.encode, because python-jose
        refuses to *encode* alg=none at all — only decoding it has ever been
        the risk. Passing algorithms=["HS256"] to decode is what refuses it;
        this fails if that is ever widened or read from the token header.
        """
        now = int(time.time())
        header = {"alg": "none", "typ": "JWT"}
        claims = {
            "iss": settings.PORTAL_JWT_ISSUER,
            "aud": settings.PORTAL_JWT_AUDIENCE,
            "iat": now,
            "exp": now + 300,
            "jti": str(uuid.uuid4()),
        }
        segment = lambda d: (
            base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
        )
        unsigned = f"{segment(header)}.{segment(claims)}."
        self.assertEqual(self.call(unsigned).status_code, 401)

    def test_hs256_is_the_only_accepted_algorithm(self):
        """A correctly-signed token using a different HMAC variant must also
        fail — decode pins the algorithm, it does not merely check a family."""
        token = jwt.encode(
            {
                "iss": settings.PORTAL_JWT_ISSUER,
                "aud": settings.PORTAL_JWT_AUDIENCE,
                "iat": int(time.time()),
                "exp": int(time.time()) + 300,
                "jti": str(uuid.uuid4()),
            },
            PORTAL_SECRET,
            algorithm="HS512",
        )
        self.assertEqual(self.call(token).status_code, 401)


class JtiReplayTest(PortalAuthTestCase):
    def test_token_is_single_use(self):
        token = make_token(jti="replay-me")
        self.assertEqual(self.call(token).status_code, 200)
        # Same token, still unexpired, second presentation.
        self.assertEqual(self.call(token).status_code, 401)

    def test_distinct_jtis_both_pass(self):
        self.assertEqual(self.call(make_token(jti="one")).status_code, 200)
        self.assertEqual(self.call(make_token(jti="two")).status_code, 200)

    def test_jti_key_ttl_is_bounded_by_the_max_lifetime(self):
        self.call(make_token(jti="ttl-check"))
        (key, ttl), = self.fake_redis.calls
        self.assertEqual(key, f"{portal_auth.JTI_KEY_PREFIX}ttl-check")
        self.assertGreater(ttl, 0)
        self.assertLessEqual(ttl, settings.PORTAL_JWT_MAX_LIFETIME_SECONDS)

    def test_redis_outage_fails_closed(self):
        """Unlike the rate limiter (which fails open), an unavailable Redis
        must stop portal calls: the endpoint behind this creates licenses, and
        an unverified-for-replay write is worse than a 503."""
        with patch.object(portal_auth, "redis_client", FakeRedis(fail=True)):
            res = self.call(make_token(jti=str(uuid.uuid4())))
        self.assertEqual(res.status_code, 503)


class ConfigurationTest(PortalAuthTestCase):
    def test_unset_portal_secret_refuses_every_call(self):
        token = make_token(jti=str(uuid.uuid4()))
        with patch.object(settings, "PORTAL_JWT_SECRET", None):
            res = self.call(token)
        self.assertEqual(res.status_code, 503)

    def test_rejection_detail_does_not_say_why(self):
        """A caller past the IP gate learns only that the token was rejected —
        signature, issuer, audience, expiry and jti are indistinguishable."""
        details = {
            self.call(make_token(secret="wrong", jti=str(uuid.uuid4()))).json()["detail"],
            self.call(make_token(issuer="wrong", jti=str(uuid.uuid4()))).json()["detail"],
            self.call(make_token(audience="wrong", jti=str(uuid.uuid4()))).json()["detail"],
            self.call(make_token(lifetime=60, iat_offset=-600, jti=str(uuid.uuid4()))).json()["detail"],
        }
        self.assertEqual(details, {portal_auth._INVALID_TOKEN})


if __name__ == "__main__":
    unittest.main()
