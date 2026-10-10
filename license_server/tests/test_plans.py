"""The asset-based plan catalog (app/plans.py) and how licenses use it.

Run from the license_server/ directory with a reachable PostgreSQL:
    DATABASE_URL=postgresql://user:pass@localhost/ls_test python -m unittest tests.test_plans -v

What is protected here:
* the SKUs and asset limits the business sells, and the add-on arithmetic
  (limit = base + 50 per pack; none for the pilot or Unlimited);
* old plan names keep meaning the plan with the same number;
* creating a license (admin and portal) stores the plan's limit, and an
  unknown plan or impossible add-ons is a 422, never a guess;
* an admin can change the plan or the add-ons later without touching the term.
"""
import os
import sys
import unittest
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef-0123456789")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from app import plans  # noqa: E402  (no settings: safe to import during discovery)

crud = models = schemas = admin = get_current_admin = AdminPrincipal = SessionLocal = None


def setUpModule():
    # Imported here, not at the top: these load app.core.config.settings, and
    # unittest discovery imports every test module before running any. The
    # settings must first be loaded by the modules that set the environment
    # they need (test_portal_*, test_security).
    global crud, models, schemas, admin, get_current_admin, AdminPrincipal, SessionLocal
    from app import crud, models, schemas
    from app.core.security import AdminPrincipal, get_current_admin
    from app.database import Base, SessionLocal, engine
    from app.routers import admin
    Base.metadata.create_all(bind=engine)


class CatalogTest(unittest.TestCase):

    def test_the_skus_and_their_asset_limits(self):
        expected = {
            "NGC-PILOT-5-1M": ("NGCorion Pilot", 5, 30),
            "NGC-ESS-25-1Y": ("NGCorion Essential", 25, 365),
            "NGC-PRO-100-1Y": ("NGCorion Professional", 100, 365),
            "NGC-BUS-250-1Y": ("NGCorion Business", 250, 365),
            "NGC-ENT-500-1Y": ("NGCorion Enterprise", 500, 365),
            "NGC-ENT-1000-1Y": ("NGCorion Enterprise", 1000, 365),
            "NGC-ENT-2500-1Y": ("NGCorion Enterprise", 2500, 365),
            "NGC-ENT-ULT-1Y": ("NGCorion Enterprise Unlimited", None, 365),
        }
        self.assertEqual({c: (p.name, p.base_assets, p.duration_days) for c, p in plans.PLANS.items()}, expected)

    def test_600_assets_is_the_500_plan_with_two_addons(self):
        self.assertEqual(plans.resolve("NGC-ENT-500-1Y").max_assets(2), 600)
        self.assertEqual(plans.ADDON_ASSETS, 50)

    def test_addons_only_on_finite_paid_plans(self):
        self.assertEqual(plans.check_addons(plans.resolve("NGC-ESS-25-1Y"), 3), 3)
        for code in ("NGC-ENT-ULT-1Y", plans.PILOT_CODE):
            with self.assertRaises(plans.InvalidAddons):
                plans.check_addons(plans.resolve(code), 1)
            self.assertEqual(plans.check_addons(plans.resolve(code), 0), 0)
        with self.assertRaises(plans.InvalidAddons):
            plans.check_addons(plans.resolve("NGC-PRO-100-1Y"), plans.MAX_ADDON_PACKS + 1)
        self.assertIsNone(plans.resolve("NGC-ENT-ULT-1Y").max_assets(0))

    def test_codes_resolve_in_any_case_and_old_names_map_by_number(self):
        self.assertEqual(plans.resolve(" ngc-bus-250-1y ").code, "NGC-BUS-250-1Y")
        self.assertEqual(
            {old: plans.resolve(old).code for old in ("pilot", "plan_100", "plan_250", "PLAN_500", "unlimited")},
            {"pilot": plans.PILOT_CODE, "plan_100": "NGC-PRO-100-1Y", "plan_250": "NGC-BUS-250-1Y",
             "PLAN_500": "NGC-ENT-500-1Y", "unlimited": "NGC-ENT-ULT-1Y"},
        )
        for bad in ("", "gold", "NGC-ENT-750-1Y", plans.ADDON_CODE):
            with self.assertRaises(plans.UnknownPlan):
                plans.resolve(bad)

    def test_schemas_reject_what_the_catalog_rejects(self):
        base = dict(customer_name="A", customer_email="a@example.com", organization_name="O")
        self.assertEqual(schemas.LicenseCreate(**base, plan_code="plan_250").plan_code, "NGC-BUS-250-1Y")
        for bad in (dict(plan_code="gold"), dict(plan_code="NGC-ENT-ULT-1Y", addon_packs=1),
                    dict(plan_code="NGC-PRO-100-1Y", addon_packs=-1)):
            with self.assertRaises(ValidationError):
                schemas.LicenseCreate(**base, **bad)

    def test_portal_body_takes_one_plan_field(self):
        base = dict(user_uuid7=uuid.uuid4(), order_id=1, order_number="O-1", customer_name="A",
                    customer_email="a@example.com", organization_name="O")
        self.assertEqual(schemas.PortalLicenseCreate(**base, plan_code="NGC-ENT-1000-1Y").plan_code, "NGC-ENT-1000-1Y")
        legacy = schemas.PortalLicenseCreate(**base, plan_type="plan_500", addon_packs=2)
        self.assertEqual((legacy.plan_code, legacy.addon_packs), ("NGC-ENT-500-1Y", 2))
        for bad in (dict(), dict(plan_code="NGC-PRO-100-1Y", plan_type="plan_100"), dict(plan_type="gold"),
                    dict(plan_code=plans.PILOT_CODE, addon_packs=1)):
            with self.assertRaises(ValidationError):
                schemas.PortalLicenseCreate(**base, **bad)


class LicensePlanTest(unittest.TestCase):

    def setUp(self):
        notify = patch.object(crud.notifier, "notify", lambda *a, **k: None)
        notify.start()
        self.addCleanup(notify.stop)
        self.db = SessionLocal()
        self.email = f"plans-{uuid.uuid4().hex[:10]}@example.com"
        app = FastAPI()
        app.include_router(admin.router)
        app.dependency_overrides[get_current_admin] = lambda: AdminPrincipal(
            user_id=0, username="test-admin", role="admin", session_id="test")
        self.client = TestClient(app)

    def tearDown(self):
        self.db.query(models.License).filter(models.License.customer_email == self.email).delete()
        self.db.commit()
        self.db.close()

    def create(self, **kw):
        body = dict(customer_name="Probe", customer_email=self.email, organization_name="Probe Org", **kw)
        return self.client.post("/api/admin/licenses", json=body)

    def test_admin_creates_a_license_with_addons(self):
        r = self.create(plan_code="NGC-ENT-500-1Y", addon_packs=2)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual((body["plan_code"], body["plan_name"], body["max_assets"], body["addon_packs"]),
                         ("NGC-ENT-500-1Y", "NGCorion Enterprise", 600, 2))
        self.assertFalse(body["is_pilot_mode"])
        self.assertIsNone(body["reported_assets"])

    def test_pilot_license_is_five_assets_for_a_month(self):
        body = self.create(plan_code="NGC-PILOT-5-1M").json()
        self.assertEqual((body["max_assets"], body["is_pilot_mode"]), (5, True))
        lic = crud.get_license_by_key(self.db, body["license_key"])
        self.assertAlmostEqual((lic.expires_at - lic.created_at).total_seconds(), timedelta(days=30).total_seconds(),
                               delta=60)

    def test_unknown_plan_and_impossible_addons_are_422(self):
        self.assertEqual(self.create(plan_code="NGC-GOLD").status_code, 422)
        self.assertEqual(self.create(plan_code="NGC-ENT-ULT-1Y", addon_packs=1).status_code, 422)

    def test_the_catalog_endpoint(self):
        body = self.client.get("/api/admin/plans").json()
        self.assertEqual([p["code"] for p in body["plans"]], list(plans.PLANS))
        self.assertEqual(body["addon"], {"code": "NGC-ADD-50-1Y", "assets": 50, "max_packs": plans.MAX_ADDON_PACKS})

    def test_changing_plan_and_addons_keeps_the_term(self):
        lic = self.create(plan_code="NGC-PRO-100-1Y").json()
        key, expires = lic["license_key"], lic["expires_at"]

        r = self.client.post(f"/api/admin/licenses/{key}/plan", json={"addon_packs": 3})
        self.assertEqual((r.json()["max_assets"], r.json()["addon_packs"]), (250, 3))

        r = self.client.post(f"/api/admin/licenses/{key}/plan", json={"plan_code": "NGC-ENT-1000-1Y"})
        self.assertEqual((r.json()["plan_code"], r.json()["max_assets"], r.json()["addon_packs"]),
                         ("NGC-ENT-1000-1Y", 1150, 3))

        # Unlimited takes no add-ons: the packs are dropped, not refused.
        r = self.client.post(f"/api/admin/licenses/{key}/plan", json={"plan_code": "NGC-ENT-ULT-1Y"})
        self.assertEqual((r.json()["max_assets"], r.json()["addon_packs"]), (None, 0))
        self.assertEqual(r.json()["expires_at"], expires)

        self.assertEqual(self.client.post(f"/api/admin/licenses/{key}/plan", json={"addon_packs": 1}).status_code, 422)
        self.assertEqual(self.client.post(f"/api/admin/licenses/{key}/plan", json={}).status_code, 422)
        self.assertEqual(self.client.post(f"/api/admin/licenses/{key}/plan",
                                          json={"plan_code": "NGC-GOLD"}).status_code, 422)
        self.assertEqual(self.client.post("/api/admin/licenses/NO-SUCH/plan",
                                          json={"addon_packs": 1}).status_code, 404)

    def test_a_downgraded_pilot_can_be_given_its_plan_back(self):
        # What migration 006 leaves for a license the old server downgraded.
        key = self.create(plan_code="NGC-PILOT-5-1M").json()["license_key"]
        r = self.client.post(f"/api/admin/licenses/{key}/plan", json={"plan_code": "NGC-BUS-250-1Y"})
        self.assertEqual((r.json()["max_assets"], r.json()["is_pilot_mode"]), (250, False))

    def test_portal_create_stores_the_catalog_limit(self):
        data = schemas.PortalLicenseCreate(
            user_uuid7=uuid.uuid4(), order_id=900000 + uuid.uuid4().int % 99999, order_number="ORD-P",
            plan_type="plan_500", addon_packs=2, customer_name="Probe", customer_email=self.email,
            organization_name="Probe Org", duration_days=400,
        )
        lic, created = crud.create_license_from_portal(self.db, data)
        self.assertTrue(created)
        self.assertEqual((lic.plan_code, lic.max_assets, lic.addon_packs, lic.is_pilot_mode),
                         ("NGC-ENT-500-1Y", 600, 2, False))
        self.assertAlmostEqual((lic.expires_at - lic.portal_synced_at).days, 400, delta=1)


if __name__ == "__main__":
    unittest.main()
