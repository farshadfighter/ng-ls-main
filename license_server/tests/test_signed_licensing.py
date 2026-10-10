"""Signed licensing answers, machine binding and the legacy rebind.

Run from the license_server/ directory with a reachable PostgreSQL:
    DATABASE_URL=postgresql://user:pass@localhost/ls_test python -m unittest tests.test_signed_licensing -v

What is protected here:
* every licensing answer carries a token signed with LICENSE_SIGNING_PRIVATE_KEY,
  that verifies with the public key and says the same as the plain fields,
  including the client's nonce (no replay), fingerprint and asset limit;
* an offline period never changes the license (the pilot fallback is the
  client's, and reversible); the heartbeat records the reported asset count;
* without a signing key the endpoints refuse to answer (503);
* a license is bound to the fingerprint the CLIENT computed, one machine only;
* a binding made by a pre-signing NGCorion (no nonce; it sent the license
  server's own fingerprint) may move once to a client-computed fingerprint,
  and an admin can release a binding.

The app under test carries only the licenses router (no rate limiter, no Redis);
license rows are created and removed by each test.
"""
import base64
import json
from datetime import datetime, timedelta, timezone
import os
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import crud, models, schemas  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.routers import licenses  # noqa: E402
from app.utils import license_token  # noqa: E402
from app.utils.signing import generate_signature  # noqa: E402

Base.metadata.create_all(bind=engine)

KEY = Ed25519PrivateKey.generate()
SEED = base64.b64encode(KEY.private_bytes(
    serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
)).decode()
PUBLIC = KEY.public_key()


def decode(token: str) -> dict:
    """Verify a token with the test public key and return its claims (raises when invalid)."""
    body, sig = token.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    payload = base64.urlsafe_b64decode(pad(body))
    PUBLIC.verify(base64.urlsafe_b64decode(pad(sig)), payload)
    return json.loads(payload)


def signed_headers(data: dict, org_token: str) -> dict:
    """The client's HMAC headers for /validate."""
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {"X-Signature": generate_signature(data, org_token, ts), "X-Timestamp": ts}


class SignedLicensingTest(unittest.TestCase):

    def setUp(self):
        # No outbound portal events from tests, without touching the global
        # settings the portal tests read.
        notify = patch.object(crud.notifier, "notify", lambda *a, **k: None)
        notify.start()
        self.addCleanup(notify.stop)
        self._saved_key = settings.LICENSE_SIGNING_PRIVATE_KEY
        self.addCleanup(setattr, settings, "LICENSE_SIGNING_PRIVATE_KEY", self._saved_key)
        settings.LICENSE_SIGNING_PRIVATE_KEY = SEED
        license_token._signer_for.cache_clear()
        app = FastAPI()
        app.include_router(licenses.router)
        self.client = TestClient(app)
        self.db = SessionLocal()
        self.email = f"signed-{uuid.uuid4().hex[:10]}@example.com"
        self.lic = crud.create_license(self.db, schemas.LicenseCreate(
            customer_name="Probe", customer_email=self.email, organization_name="Probe Org",
            plan_code="NGC-BUS-250-1Y", addon_packs=2,
        ))

    def tearDown(self):
        self.db.query(models.License).filter(models.License.customer_email == self.email).delete()
        self.db.commit()
        self.db.close()

    def activate(self, fp, nonce="n-activate"):
        return self.client.post("/api/licenses/activate",
                                json={"license_key": self.lic.license_key, "vm_fingerprint": fp, "nonce": nonce})

    def test_activation_answer_is_signed_and_matches_the_plain_fields(self):
        r = self.activate("fp-machine-a", nonce="abc123")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        claims = decode(body["signed_token"])
        self.assertEqual(claims["typ"], "activate")
        self.assertEqual(claims["lic"], self.lic.license_key)
        self.assertEqual(claims["fp"], "fp-machine-a")
        self.assertEqual(claims["nonce"], "abc123")
        self.assertTrue(claims["valid"])
        self.assertEqual(claims["plan"], "NGC-BUS-250-1Y")
        self.assertEqual(claims["plan_name"], "NGCorion Business")
        self.assertEqual(claims["limits"], {"max_assets": 350, "addon_packs": 2})
        self.assertEqual(claims["limits"], body["limits"])
        self.assertNotIn("usage", claims)
        self.assertEqual(claims["kid"], license_token.key_id(PUBLIC.public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)))
        self.assertGreater(claims["exp"], claims["iat"])

    def test_a_changed_token_no_longer_verifies(self):
        token = self.activate("fp-machine-a").json()["signed_token"]
        body, sig = token.split(".")
        claims = decode(token)
        claims["limits"] = {"max_assets": None, "addon_packs": 0}
        forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
        with self.assertRaises(Exception):
            decode(f"{forged}.{sig}")

    def test_without_a_signing_key_the_server_refuses_to_answer(self):
        settings.LICENSE_SIGNING_PRIVATE_KEY = None
        r = self.activate("fp-machine-a")
        self.assertEqual(r.status_code, 503)

    def test_one_machine_per_license(self):
        self.assertEqual(self.activate("fp-machine-a").status_code, 200)
        self.assertEqual(self.activate("fp-machine-a").status_code, 200)       # same machine again
        r = self.activate("fp-machine-b")
        self.assertEqual(r.status_code, 400)
        self.assertIn("another VM", r.json()["detail"])

    def test_validate_says_no_for_another_machine_and_signs_the_no(self):
        org = self.activate("fp-machine-a").json()["organization_token"]
        data = {"license_key": self.lic.license_key, "organization_token": org, "vm_fingerprint": "fp-machine-b"}
        r = self.client.post("/api/licenses/validate", json=dict(data, nonce="v1"), headers=signed_headers(data, org))
        body = r.json()
        self.assertFalse(body["valid"])
        claims = decode(body["signed_token"])
        self.assertFalse(claims["valid"])
        self.assertEqual(claims["nonce"], "v1")
        self.assertIsNone(claims["limits"])

    def test_there_is_no_consume_endpoint(self):
        # Licensing v3 counts assets, not operations.
        r = self.client.post("/api/licenses/consume", json={})
        self.assertEqual(r.status_code, 404)

    def test_heartbeat_token_reports_validity_and_records_the_asset_count(self):
        org = self.activate("fp-machine-a").json()["organization_token"]
        data = {"license_key": self.lic.license_key, "organization_token": org, "vm_fingerprint": "fp-machine-a",
                "nonce": "h1", "asset_count": 312}
        r = self.client.post("/api/licenses/heartbeat", json=data)
        self.assertEqual(r.status_code, 200, r.text)
        claims = decode(r.json()["signed_token"])
        self.assertEqual((claims["typ"], claims["valid"], claims["nonce"]), ("heartbeat", True, "h1"))
        self.assertEqual(claims["limits"]["max_assets"], 350)
        self.assertNotIn("down", claims)
        self.db.expire_all()
        self.assertEqual(self.lic.reported_assets, 312)
        self.assertIsNotNone(self.lic.reported_assets_at)

        # An older client without asset_count leaves the last report alone.
        data.pop("asset_count")
        self.assertEqual(self.client.post("/api/licenses/heartbeat", json=data).status_code, 200)
        self.db.expire_all()
        self.assertEqual(self.lic.reported_assets, 312)

        self.assertEqual(self.client.post("/api/licenses/heartbeat", json=dict(data, asset_count=-1)).status_code, 422)

        crud.deactivate_license(self.db, self.lic.license_key)
        claims = decode(self.client.post("/api/licenses/heartbeat", json=data).json()["signed_token"])
        self.assertFalse(claims["valid"])

    def test_a_long_offline_period_does_not_change_the_license(self):
        # Up to v2 the server overwrote the plan with the pilot plan after 48h
        # without a heartbeat. The offline fallback is NGCorion's own now, and
        # reversible; the license keeps what was sold.
        org = self.activate("fp-machine-a").json()["organization_token"]
        self.db.expire_all()
        self.lic.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(days=10)
        self.db.commit()
        vdata = {"license_key": self.lic.license_key, "organization_token": org, "vm_fingerprint": "fp-machine-a"}
        r = self.client.post("/api/licenses/validate", json=dict(vdata, nonce="v3"), headers=signed_headers(vdata, org))
        claims = decode(r.json()["signed_token"])
        self.assertTrue(claims["valid"])
        self.assertEqual((claims["plan"], claims["pilot"]), ("NGC-BUS-250-1Y", False))
        r = self.client.post("/api/licenses/heartbeat", json=dict(vdata, nonce="h2"))
        self.assertEqual(decode(r.json()["signed_token"])["limits"]["max_assets"], 350)
        self.db.expire_all()
        self.assertEqual((self.lic.plan_code, self.lic.max_assets, self.lic.is_pilot_mode),
                         ("NGC-BUS-250-1Y", 350, False))

    def test_legacy_binding_moves_once_to_a_client_computed_fingerprint(self):
        # An older NGCorion activates (no nonce): bound to whatever it sent,
        # which was the license server's own fingerprint.
        r = self.client.post("/api/licenses/activate",
                             json={"license_key": self.lic.license_key, "vm_fingerprint": "server-own-fp"})
        self.assertEqual(r.status_code, 200)
        self.db.expire_all()
        self.assertIsNone(self.lic.fingerprint_source)

        # The upgraded NGCorion computes its own fingerprint: one move allowed.
        self.assertEqual(self.activate("fp-real-machine").status_code, 200)
        self.db.expire_all()
        self.assertEqual((self.lic.vm_fingerprint, self.lic.fingerprint_source), ("fp-real-machine", "client"))
        # ...and only one.
        self.assertEqual(self.activate("fp-other-machine").status_code, 400)

    def test_an_old_client_cannot_steal_a_client_binding(self):
        self.assertEqual(self.activate("fp-real-machine").status_code, 200)
        r = self.client.post("/api/licenses/activate",
                             json={"license_key": self.lic.license_key, "vm_fingerprint": "server-own-fp"})
        self.assertEqual(r.status_code, 400)

    def test_admin_can_release_the_binding(self):
        self.assertEqual(self.activate("fp-machine-a").status_code, 200)
        self.db.expire_all()            # the activation ran in the request's own session
        crud.reset_binding(self.db, self.lic.license_key)
        self.assertEqual(self.activate("fp-machine-b").status_code, 200)
        self.assertIsNone(crud.reset_binding(self.db, "NO-SUCH-KEY"))


class KeyHandlingTest(unittest.TestCase):

    def test_seed_round_trip_and_bad_keys(self):
        signer = license_token.TokenSigner(license_token.load_private_key(SEED))
        self.assertEqual(signer.public_key_b64, base64.b64encode(PUBLIC.public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode())
        for bad in ("", "not base64!!", base64.b64encode(b"short").decode()):
            with self.assertRaises(license_token.SigningKeyMissing):
                license_token.load_private_key(bad)

    def test_public_key_verifies_what_the_signer_signs(self):
        signer = license_token.TokenSigner(KEY)
        token = signer.sign({"typ": "x", "msg": "لایسنس معتبر است"})
        claims = decode(token)
        self.assertEqual(claims["msg"], "لایسنس معتبر است")
        self.assertEqual(claims["v"], license_token.TOKEN_VERSION)
        self.assertIsInstance(Ed25519PublicKey.from_public_bytes(signer.public_raw), Ed25519PublicKey)


if __name__ == "__main__":
    unittest.main()
