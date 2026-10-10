"""Admin accounts, sessions, roles and the audit trail (app/accounts.py, app/core/security.py,
app/core/passwords.py, app/audit.py and the admin_auth / admin_users / admin_audit routers).

Run from the license_server/ directory with a reachable PostgreSQL:
    DATABASE_URL=postgresql://user:pass@localhost/ls_test python -m unittest tests.test_admin_accounts -v
"""
import os
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef-0123456789")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import passwords  # noqa: E402  (no settings: safe during discovery)

GOOD = "Correct-Horse-42!"
ALSO_GOOD = "Battery-Staple-99?"

accounts = audit = models = security = settings = SessionLocal = None
routers = {}


def setUpModule():
    # Imported here, not at the top: they load the settings (see test_plans.py).
    global accounts, audit, models, security, settings, SessionLocal
    from app import accounts, audit, models
    from app.core import security
    from app.core.config import settings
    from app.database import Base, SessionLocal, engine
    from app.routers import admin, admin_audit, admin_auth, admin_users
    routers.update(admin=admin, admin_audit=admin_audit, admin_auth=admin_auth, admin_users=admin_users)
    Base.metadata.create_all(bind=engine)


class PasswordTest(unittest.TestCase):

    def test_hash_round_trip_and_salt(self):
        h1, h2 = passwords.hash_password(GOOD), passwords.hash_password(GOOD)
        self.assertNotEqual(h1, h2)  # salted
        self.assertTrue(h1.startswith("scrypt$15$8$1$"))
        self.assertTrue(passwords.verify_password(GOOD, h1))
        self.assertFalse(passwords.verify_password(GOOD + "x", h1))
        for broken in (None, "", "md5$abc", "scrypt$x$y", h1.replace("scrypt", "bcrypt")):
            self.assertFalse(passwords.verify_password(GOOD, broken))

    def test_policy(self):
        for bad in ("short1!A", "alllowercaseletters", "NoDigitsOrSymbolsHere", "x" * 129):
            with self.assertRaises(passwords.PasswordPolicyError):
                passwords.check_policy(bad)
        with self.assertRaises(passwords.PasswordPolicyError):
            passwords.check_policy("Alice-Is-Great-1", "alice")
        current = passwords.hash_password(GOOD)
        with self.assertRaises(passwords.PasswordPolicyError):
            passwords.check_policy(GOOD, "bob", current)
        passwords.check_policy(ALSO_GOOD, "bob", current)


class AccountsApiTest(unittest.TestCase):

    def setUp(self):
        turnstile = patch.object(routers["admin_auth"], "verify_turnstile", lambda *a, **k: None)
        turnstile.start()
        self.addCleanup(turnstile.stop)
        self.db = SessionLocal()
        self.addCleanup(self.db.close)
        app = FastAPI()
        for name in ("admin_auth", "admin_users", "admin_audit", "admin"):
            app.include_router(routers[name].router)
        self.client = TestClient(app)
        self.tag = uuid.uuid4().hex[:8]

    # -- helpers -----------------------------------------------------------------------

    def make(self, role="admin", password=GOOD, must_change=False, name=None):
        name = name or f"{role}-{self.tag}-{uuid.uuid4().hex[:4]}"
        user = models.AdminUser(username=name, role=role, password_hash=passwords.hash_password(password),
                                must_change_password=must_change, created_by="test")
        self.db.add(user)
        self.db.commit()
        return name

    def login(self, name, password=GOOD):
        return self.client.post("/api/admin/login", json={"username": name, "password": password,
                                                          "cf-turnstile-response": "x"})

    def token(self, name, password=GOOD):
        r = self.login(name, password)
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    def events(self, **kw):
        self.db.expire_all()
        rows, _ = audit.search(self.db, **kw)
        return rows

    # -- login, lockout, sessions ------------------------------------------------------

    def test_login_and_me(self):
        name = self.make("operator")
        r = self.login(name)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual((body["user"]["username"], body["user"]["role"]), (name, "operator"))
        me = self.client.get("/api/admin/me", headers={"Authorization": f"Bearer {body['access_token']}"})
        self.assertEqual(me.json()["username"], name)
        self.assertTrue(self.events(actor=name, action="auth.login", outcome="success"))

    def test_every_refusal_looks_the_same(self):
        name = self.make()
        answers = {self.login(name, "Wrong-Password-1").json()["detail"],
                   self.login(f"nobody-{self.tag}", GOOD).json()["detail"]}
        self.assertEqual(len(answers), 1)
        reasons = {e.detail["reason"] for e in self.events(action="auth.login", outcome="failure")
                   if e.target in (name, f"nobody-{self.tag}")}
        self.assertEqual(reasons, {"wrong password", "unknown user"})

    def test_lockout_after_repeated_failures_and_unlock(self):
        name = self.make()
        for _ in range(settings.ADMIN_MAX_FAILED_LOGINS):
            self.assertEqual(self.login(name, "Wrong-Password-1").status_code, 401)
        # Locked: even the right password is refused.
        self.assertEqual(self.login(name).status_code, 401)
        self.assertTrue(self.events(action="auth.lockout", target=name))
        self.assertIn("account locked", {e.detail["reason"] for e in self.events(target=name, action="auth.login")})

        admin = self.token(self.make("admin"))
        uid = accounts.get_by_username(self.db, name).id
        self.assertEqual(self.client.post(f"/api/admin/users/{uid}/unlock", headers=admin).status_code, 200)
        self.assertEqual(self.login(name).status_code, 200)

    def test_lockout_expires(self):
        name = self.make()
        user = accounts.get_by_username(self.db, name)
        user.locked_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.db.commit()
        self.assertEqual(self.login(name).status_code, 200)

    def test_disabled_account_cannot_log_in(self):
        name = self.make()
        user = accounts.get_by_username(self.db, name)
        user.is_active = False
        self.db.commit()
        self.assertEqual(self.login(name).status_code, 401)

    def test_logout_ends_the_session(self):
        headers = self.token(self.make())
        self.assertEqual(self.client.get("/api/admin/me", headers=headers).status_code, 200)
        self.assertEqual(self.client.post("/api/admin/logout", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/api/admin/me", headers=headers).status_code, 401)

    def test_idle_session_ends(self):
        name = self.make()
        headers = self.token(name)
        session = (self.db.query(models.AdminSession).join(models.AdminUser)
                   .filter(models.AdminUser.username == name).one())
        session.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=settings.ADMIN_IDLE_TIMEOUT_MINUTES + 1)
        self.db.commit()
        r = self.client.get("/api/admin/me", headers=headers)
        self.assertEqual(r.status_code, 401)
        self.assertIn("inactivity", r.json()["detail"])

    def test_forged_or_foreign_tokens_are_refused(self):
        import jwt
        name = self.make()
        self.token(name)
        session = (self.db.query(models.AdminSession).join(models.AdminUser)
                   .filter(models.AdminUser.username == name).first())
        now = int(datetime.now(timezone.utc).timestamp())
        good = {"sub": name, "sid": session.id, "iat": now, "exp": now + 600,
                "iss": security.TOKEN_ISSUER, "aud": security.TOKEN_AUDIENCE}
        for claims, key in (
            (good, "some-other-secret"),                                  # wrong key
            ({**good, "aud": "portal"}, settings.SECRET_KEY),             # wrong audience
            ({**good, "sid": "no-such-session"}, settings.SECRET_KEY),    # no session
            ({**good, "sub": "someone-else"}, settings.SECRET_KEY),       # session of another user
            ({**good, "exp": now - 10}, settings.SECRET_KEY),             # expired
        ):
            token = jwt.encode(claims, key, algorithm="HS256")
            self.assertEqual(self.client.get("/api/admin/me", headers={"Authorization": f"Bearer {token}"})
                             .status_code, 401, claims)
        self.assertEqual(self.client.get("/api/admin/me").status_code, 401)

    # -- password change ----------------------------------------------------------------

    def test_must_change_password_first(self):
        name = self.make("operator", must_change=True)
        headers = self.token(name)
        self.assertEqual(self.client.get("/api/admin/licenses", headers=headers).status_code, 403)
        self.assertTrue(self.client.get("/api/admin/me", headers=headers).json()["must_change_password"])
        r = self.client.post("/api/admin/me/password", headers=headers,
                             json={"current_password": GOOD, "new_password": "weak"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/admin/me/password", headers=headers,
                             json={"current_password": GOOD, "new_password": ALSO_GOOD})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.client.get("/api/admin/licenses", headers=headers).status_code, 200)
        self.assertEqual(self.login(name).status_code, 401)
        self.assertEqual(self.login(name, ALSO_GOOD).status_code, 200)

    def test_password_change_ends_other_sessions(self):
        name = self.make()
        first, second = self.token(name), self.token(name)
        r = self.client.post("/api/admin/me/password", headers=second,
                             json={"current_password": GOOD, "new_password": ALSO_GOOD})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/admin/me", headers=first).status_code, 401)
        self.assertEqual(self.client.get("/api/admin/me", headers=second).status_code, 200)

    # -- roles ---------------------------------------------------------------------------

    def license_body(self):
        return {"customer_name": "Role Probe", "customer_email": f"role-{self.tag}@example.com",
                "organization_name": "Role Org", "plan_code": "NGC-ESS-25-1Y"}

    def tearDown(self):
        self.db.query(models.License).filter(models.License.customer_email == f"role-{self.tag}@example.com").delete()
        self.db.commit()

    def test_auditor_reads_but_does_not_change(self):
        h = self.token(self.make("auditor"))
        self.assertEqual(self.client.get("/api/admin/licenses", headers=h).status_code, 200)
        self.assertEqual(self.client.post("/api/admin/licenses", headers=h, json=self.license_body()).status_code, 403)
        self.assertEqual(self.client.get("/api/admin/audit", headers=h).status_code, 200)
        self.assertEqual(self.client.get("/api/admin/users", headers=h).status_code, 403)

    def test_operator_changes_licenses_only(self):
        h = self.token(self.make("operator"))
        r = self.client.post("/api/admin/licenses", headers=h, json=self.license_body())
        self.assertEqual(r.status_code, 200, r.text)
        key = r.json()["license_key"]
        self.assertEqual(self.client.delete(f"/api/admin/licenses/{key}/permanent", headers=h).status_code, 403)
        self.assertEqual(self.client.get("/api/admin/users", headers=h).status_code, 403)
        self.assertEqual(self.client.get("/api/admin/audit", headers=h).status_code, 403)
        self.assertTrue(self.events(action="license.create", target=key, outcome="success"))

    def test_admin_manages_accounts(self):
        admin_name = self.make("admin")
        h = self.token(admin_name)
        new = f"newbie-{self.tag}"
        r = self.client.post("/api/admin/users", headers=h,
                             json={"username": new.upper(), "role": "auditor", "password": GOOD})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual((r.json()["username"], r.json()["must_change_password"]), (new, True))
        uid = r.json()["id"]
        self.assertEqual(self.client.post("/api/admin/users", headers=h,
                                          json={"username": new, "role": "auditor", "password": GOOD}).status_code, 400)
        self.assertEqual(self.client.post("/api/admin/users", headers=h,
                                          json={"username": f"x-{self.tag}", "role": "root", "password": GOOD}).status_code, 400)
        self.assertEqual(self.client.post("/api/admin/users", headers=h,
                                          json={"username": f"y-{self.tag}", "role": "auditor", "password": "weak"}).status_code, 400)

        newbie = self.token(new)
        r = self.client.patch(f"/api/admin/users/{uid}", headers=h, json={"is_active": False})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/admin/me", headers=newbie).status_code, 401)  # session ended
        self.assertTrue(self.events(action="user.update", target=new))

        r = self.client.post(f"/api/admin/users/{uid}/reset-password", headers=h, json={"password": ALSO_GOOD})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["must_change_password"])

    def test_the_last_admin_stays(self):
        # Make this account the only active admin for the duration of the test.
        self.db.query(models.AdminUser).filter(models.AdminUser.role == "admin").update({"is_active": False})
        self.db.commit()
        name = self.make("admin")
        h = self.token(name)
        uid = accounts.get_by_username(self.db, name).id
        r = self.client.patch(f"/api/admin/users/{uid}", headers=h, json={"role": "operator"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("last active admin", r.json()["detail"])
        other = self.make("admin")
        self.assertEqual(self.client.patch(f"/api/admin/users/{uid}", headers=h, json={"is_active": False})
                         .status_code, 409)  # not yourself
        self.assertEqual(self.client.patch(f"/api/admin/users/{uid}", headers=h, json={"role": "operator"})
                         .status_code, 200)  # another admin exists now
        self.assertIsNotNone(other)

    # -- the audit trail -------------------------------------------------------------------

    def test_audit_trail_is_append_only(self):
        from sqlalchemy import text
        audit.record("test.event", audit.SUCCESS, actor="test", target=self.tag)
        with self.assertRaises(Exception):
            self.db.execute(text("UPDATE audit_events SET outcome='x' WHERE target=:t"), {"t": self.tag})
            self.db.commit()
        self.db.rollback()
        with self.assertRaises(Exception):
            self.db.execute(text("DELETE FROM audit_events WHERE target=:t"), {"t": self.tag})
            self.db.commit()
        self.db.rollback()
        self.assertEqual(len(self.events(target=self.tag)), 1)

    def test_audit_search_and_csv_export(self):
        h = self.token(self.make("auditor"))
        audit.record("test.event", audit.FAILURE, actor="test", target=self.tag, detail={"x": "=HYPERLINK(1)"})
        page = self.client.get("/api/admin/audit", headers=h, params={"target": self.tag}).json()
        self.assertEqual((page["total"], page["items"][0]["action"]), (1, "test.event"))
        r = self.client.get("/api/admin/audit/export.csv", headers=h, params={"target": self.tag,
                                                                              "action": "test"})
        self.assertEqual(r.status_code, 200)
        line = r.text.strip().splitlines()[1]
        self.assertIn("test.event", line)
        self.assertTrue(self.events(action="audit.export"))

    def test_csv_cells_cannot_become_formulas(self):
        cell = routers["admin_audit"]._csv_cell
        self.assertEqual(cell("=1+1"), "'=1+1")
        self.assertEqual(cell("@SUM(A1)"), "'@SUM(A1)")
        self.assertEqual(cell("plain"), "plain")


class BootstrapTest(unittest.TestCase):

    def setUp(self):
        self.db = SessionLocal()
        self.addCleanup(self.db.close)
        self.name = f"boot-{uuid.uuid4().hex[:6]}"

    def test_nothing_while_accounts_exist(self):
        with patch.object(accounts, "_has_accounts", return_value=True), \
                patch.object(settings, "ADMIN_USERNAME", self.name), \
                patch.object(settings, "ADMIN_PASSWORD", "initial-pass-123"):
            self.assertIsNone(accounts.bootstrap_first_admin(self.db))
        self.assertIsNone(accounts.get_by_username(self.db, self.name))

    def test_first_admin_from_the_settings(self):
        with patch.object(accounts, "_has_accounts", return_value=False), \
                patch.object(settings, "ADMIN_USERNAME", self.name.upper()), \
                patch.object(settings, "ADMIN_PASSWORD", "initial-pass-123"):
            user = accounts.bootstrap_first_admin(self.db)
        self.assertEqual((user.username, user.role, user.must_change_password), (self.name, "admin", True))
        self.assertTrue(passwords.verify_password("initial-pass-123", user.password_hash))

    def test_no_first_admin_with_a_trivial_password(self):
        for weak in (None, "", "changeme", "short"):
            with patch.object(accounts, "_has_accounts", return_value=False), \
                    patch.object(settings, "ADMIN_USERNAME", self.name), \
                    patch.object(settings, "ADMIN_PASSWORD", weak):
                self.assertIsNone(accounts.bootstrap_first_admin(self.db))
        self.assertIsNone(accounts.get_by_username(self.db, self.name))


if __name__ == "__main__":
    unittest.main()
