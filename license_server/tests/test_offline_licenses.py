"""Offline (air-gapped) licenses: the request file, the signed one-year license file, and the
rules for issuing one (app/offline.py, crud.issue_offline_license, the admin endpoints).

Run from the license_server/ directory with a reachable PostgreSQL:
    DATABASE_URL=postgresql://user:pass@localhost/ls_test python -m unittest tests.test_offline_licenses -v
"""
import base64
import json
import os
import sys
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import offline  # noqa: E402  (no settings: safe during discovery)

KEY = Ed25519PrivateKey.generate()
SEED = base64.b64encode(KEY.private_bytes(
    serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
)).decode()
FP = "a" * 64

crud = models = admin = get_current_admin = SessionLocal = settings = license_token = None


def setUpModule():
    # Imported here, not at the top: they load the settings, which other test modules must load
    # first with the environment they need (see test_plans.py).
    global crud, models, admin, get_current_admin, SessionLocal, settings, license_token
    from app import crud, models
    from app.core.config import settings
    from app.core.security import get_current_admin
    from app.database import Base, SessionLocal, engine
    from app.routers import admin
    from app.utils import license_token
    Base.metadata.create_all(bind=engine)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def request_text(license_key, fp=FP, assets=17, nonce="nonce-0123456789"):
    """A request file as NGCorion writes it."""
    body = {"lic": license_key, "fp": fp, "assets": assets, "nonce": nonce, "created": int(time.time())}
    return f"{offline.REQUEST_HEADER}\n{b64url(json.dumps(body).encode())}\n"


def decode(token):
    body, sig = token.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    payload = base64.urlsafe_b64decode(pad(body))
    KEY.public_key().verify(base64.urlsafe_b64decode(pad(sig)), payload)
    return json.loads(payload)


class RequestParsingTest(unittest.TestCase):

    def test_a_request_round_trips(self):
        req = offline.parse_request(request_text("AAAA-BBBB-CCCC-DDDD"))
        self.assertEqual((req.license_key, req.vm_fingerprint, req.asset_count), ("AAAA-BBBB-CCCC-DDDD", FP, 17))

    def test_anything_else_is_refused(self):
        for bad in ("", "hello", f"{offline.REQUEST_HEADER}\n!!!notbase64", f"{offline.LICENSE_HEADER}\nabc",
                    request_text("K", fp="short"), request_text("K", nonce="x"), request_text("K", assets=-1),
                    "x" * 9000):
            with self.assertRaises(offline.InvalidOfflineRequest):
                offline.parse_request(bad)

    def test_a_file_is_valid_one_year_but_never_past_the_license(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(offline.valid_until(now, now + timedelta(days=500)), now + timedelta(days=365))
        self.assertEqual(offline.valid_until(now, now + timedelta(days=200)), now + timedelta(days=200))


class OfflineIssueTest(unittest.TestCase):

    def setUp(self):
        notify = patch.object(crud.notifier, "notify", lambda *a, **k: None)
        notify.start()
        self.addCleanup(notify.stop)
        self.addCleanup(setattr, settings, "LICENSE_SIGNING_PRIVATE_KEY", settings.LICENSE_SIGNING_PRIVATE_KEY)
        settings.LICENSE_SIGNING_PRIVATE_KEY = SEED
        license_token._signer_for.cache_clear()
        self.db = SessionLocal()
        self.email = f"offline-{uuid.uuid4().hex[:10]}@example.com"
        app = FastAPI()
        app.include_router(admin.router)
        app.dependency_overrides[get_current_admin] = lambda: "test-admin"
        self.client = TestClient(app)

    def tearDown(self):
        self.db.query(models.License).filter(models.License.customer_email == self.email).delete()
        self.db.commit()
        self.db.close()

    def create(self, **kw):
        body = dict(customer_name="Probe", customer_email=self.email, organization_name="Air Gapped Org",
                    plan_code="NGC-ENT-500-1Y", addon_packs=2, **kw)
        r = self.client.post("/api/admin/licenses", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def issue(self, key, text):
        return self.client.post(f"/api/admin/licenses/{key}/offline-file", json={"request": text})

    def test_issuing_a_signed_one_year_file_bound_to_the_machine(self):
        lic = self.create(offline_allowed=True)
        self.assertTrue(lic["offline_allowed"])
        r = self.issue(lic["license_key"], request_text(lic["license_key"], nonce="n-issue-000001"))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["filename"].endswith(".ngclic"))
        header, token = body["content"].strip().split("\n")
        self.assertEqual(header, offline.LICENSE_HEADER)

        claims = decode(token)
        self.assertEqual((claims["typ"], claims["lic"], claims["fp"], claims["valid"], claims["nonce"]),
                         ("offline", lic["license_key"], FP, True, "n-issue-000001"))
        self.assertEqual(claims["limits"], {"max_assets": 600, "addon_packs": 2})
        self.assertEqual(claims["plan"], "NGC-ENT-500-1Y")
        # one year, but the license (also one year, created a moment earlier) ends first
        self.assertLessEqual(claims["offline_until"], claims["exp"])
        self.assertGreater(claims["offline_until"], time.time() + 364 * 86400)

        self.db.expire_all()
        row = crud.get_license_by_key(self.db, lic["license_key"])
        self.assertEqual((row.vm_fingerprint, row.fingerprint_source, row.reported_assets), (FP, "client", 17))
        self.assertIsNotNone(row.activated_at)
        self.assertEqual(body["license"]["offline_valid_until"][:10], row.offline_valid_until.isoformat()[:10])

        issues = self.client.get(f"/api/admin/licenses/{lic['license_key']}/offline-issues").json()
        self.assertEqual([(i["vm_fingerprint"], i["reported_assets"], i["issued_by"]) for i in issues],
                         [(FP, 17, "test-admin")])

    def test_renewal_from_the_same_machine_works(self):
        lic = self.create(offline_allowed=True)
        key = lic["license_key"]
        self.assertEqual(self.issue(key, request_text(key)).status_code, 200)
        self.assertEqual(self.issue(key, request_text(key, assets=40)).status_code, 200)
        self.assertEqual(len(self.client.get(f"/api/admin/licenses/{key}/offline-issues").json()), 2)

    def test_only_offline_licenses_get_files(self):
        lic = self.create()
        key = lic["license_key"]
        r = self.issue(key, request_text(key))
        self.assertEqual(r.status_code, 409)
        self.assertIn("not an offline license", r.json()["detail"])

        self.assertTrue(self.client.post(f"/api/admin/licenses/{key}/offline", json={"allowed": True}).json()["offline_allowed"])
        self.assertEqual(self.issue(key, request_text(key)).status_code, 200)

    def test_refusals(self):
        lic = self.create(offline_allowed=True)
        key = lic["license_key"]
        # garbage
        self.assertEqual(self.issue(key, "not a request").status_code, 422)
        # a request for another license
        r = self.issue(key, request_text("ZZZZ-ZZZZ-ZZZZ-ZZZZ"))
        self.assertEqual(r.status_code, 409)
        self.assertIn("is for license", r.json()["detail"])
        # bound to another machine
        self.assertEqual(self.issue(key, request_text(key)).status_code, 200)
        r = self.issue(key, request_text(key, fp="b" * 64))
        self.assertEqual(r.status_code, 409)
        self.assertIn("another machine", r.json()["detail"])
        # ...until the binding is released
        crud.reset_binding(self.db, key)
        self.assertEqual(self.issue(key, request_text(key, fp="b" * 64)).status_code, 200)
        # revoked
        crud.deactivate_license(self.db, key)
        self.assertEqual(self.issue(key, request_text(key, fp="b" * 64)).status_code, 409)
        # unknown key
        self.assertEqual(self.issue("NO-SUCH", request_text("NO-SUCH")).status_code, 404)

    def test_an_expired_license_gets_no_file(self):
        lic = self.create(offline_allowed=True)
        row = crud.get_license_by_key(self.db, lic["license_key"])
        row.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        self.db.commit()
        r = self.issue(lic["license_key"], request_text(lic["license_key"]))
        self.assertEqual(r.status_code, 409)
        self.assertIn("expired", r.json()["detail"])


    # --- the Test page's file check (POST /api/admin/offline/inspect) -----------------------

    def inspect(self, text):
        return self.client.post("/api/admin/offline/inspect", json={"content": text})

    def texts(self, body):
        return [c["text"] for c in body["checks"] if not c["ok"]]

    def test_inspecting_a_request_says_what_issuing_would_do_and_changes_nothing(self):
        lic = self.create(offline_allowed=True)
        key = lic["license_key"]
        r = self.inspect(request_text(key, assets=12))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual((body["kind"], body["ok"], body["license_key"], body["vm_fingerprint"], body["asset_count"]),
                         ("request", True, key, FP, 12))
        self.assertEqual((body["plan_code"], body["max_assets"]), ("NGC-ENT-500-1Y", 600))
        self.assertTrue(any("can be issued" in c["text"] for c in body["checks"]))
        # read-only: no binding, no record
        self.db.expire_all()
        row = crud.get_license_by_key(self.db, key)
        self.assertIsNone(row.vm_fingerprint)
        self.assertEqual(self.client.get(f"/api/admin/licenses/{key}/offline-issues").json(), [])

    def test_inspecting_a_request_reports_why_no_file_can_be_issued(self):
        lic = self.create()
        body = self.inspect(request_text(lic["license_key"])).json()
        self.assertFalse(body["ok"])
        self.assertIn("not an offline license", " ".join(self.texts(body)))

        body = self.inspect(request_text("ZZZZ-YYYY-XXXX-WWWW")).json()
        self.assertFalse(body["ok"])
        self.assertIn("no license ZZZZ-YYYY-XXXX-WWWW", " ".join(self.texts(body)))

    def test_an_issued_license_file_checks_out(self):
        lic = self.create(offline_allowed=True)
        key = lic["license_key"]
        content = self.issue(key, request_text(key)).json()["content"]
        body = self.inspect(content).json()
        self.assertEqual((body["kind"], body["ok"], body["signature_valid"]), ("license", True, True), body)
        self.assertEqual((body["license_key"], body["vm_fingerprint"], body["max_assets"], body["addon_packs"]),
                         (key, FP, 600, 2))
        self.assertEqual(body["claims"]["typ"], "offline")
        self.assertTrue(any("In the issue record" in c["text"] and "test-admin" in c["text"] for c in body["checks"]))

    def test_an_altered_or_foreign_license_file_is_caught(self):
        lic = self.create(offline_allowed=True)
        key = lic["license_key"]
        header, token = self.issue(key, request_text(key)).json()["content"].strip().split("\n")

        # altered: a bigger asset limit, the original signature
        body_part, sig = token.split(".")
        pad = lambda v: v + "=" * (-len(v) % 4)  # noqa: E731
        claims = json.loads(base64.urlsafe_b64decode(pad(body_part)))
        claims["limits"]["max_assets"] = 100000
        forged = b64url(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
        body = self.inspect(f"{header}\n{forged}.{sig}\n").json()
        self.assertEqual((body["ok"], body["signature_valid"], body["max_assets"]), (False, False, 100000))
        self.assertIn("altered", " ".join(self.texts(body)))
        # an unverified file is not matched against the issue record
        self.assertFalse(any("issue record" in c["text"] for c in body["checks"]))

        # signed with some other key
        other = license_token.TokenSigner(Ed25519PrivateKey.generate())
        token = other.sign(dict(claims, typ="offline"))
        body = self.inspect(f"{header}\n{token}\n").json()
        self.assertFalse(body["signature_valid"])
        self.assertIn(f"Signed with key {other.kid}", " ".join(self.texts(body)))

    def test_a_revoked_license_is_flagged_on_its_file(self):
        lic = self.create(offline_allowed=True)
        key = lic["license_key"]
        content = self.issue(key, request_text(key)).json()["content"]
        crud.deactivate_license(self.db, key)
        body = self.inspect(content).json()
        self.assertTrue(body["signature_valid"])
        self.assertFalse(body["ok"])
        self.assertIn("revoked since", " ".join(self.texts(body)))

    def test_anything_else_is_refused(self):
        for bad in ("hello", f"{offline.LICENSE_HEADER}\nnot-a-token", f"{offline.REQUEST_HEADER}\n!!!"):
            self.assertEqual(self.inspect(bad).status_code, 422, bad)


if __name__ == "__main__":
    unittest.main()
