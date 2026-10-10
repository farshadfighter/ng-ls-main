"""
Admin accounts: the first account, logins (with lockout), sessions, and account management.

Logins
------
authenticate() checks a user name and password and, on success, opens a session
(models.AdminSession). After ADMIN_MAX_FAILED_LOGINS failures in a row an account is locked for
ADMIN_LOCKOUT_MINUTES; while locked, the password is not even checked. An unknown user name, a
disabled account, a locked one and a wrong password all get the same answer and take the same
time (passwords.dummy_verify), so a login attempt reveals nothing about which accounts exist.
Every attempt is audited (app/audit.py) with its real reason.

The first account
-----------------
bootstrap_first_admin() makes it at start-up when admin_users is empty, from ADMIN_USERNAME /
ADMIN_PASSWORD, as an admin who must change the password at first login. Further accounts are
made by an admin in the console, or with `python -m app.cli` on the server.

Account rules
-------------
* A new or reset password is chosen by an admin and must be changed by its owner at first use.
* Changing a password ends every other session of that account; disabling an account or
  resetting its password ends all of them.
* The last active admin cannot be disabled or demoted, so the console cannot be locked out of
  account management.
* Accounts are never deleted (the audit trail refers to them); they are disabled.
"""
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import audit, models
from .core import passwords
from .core.config import settings
from .core.security import create_access_token

logger = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
LOGIN_REFUSED = "Invalid user name or password."


class AccountError(ValueError):
    """An account change that is not allowed; the message says why (a 400/409)."""


class LoginRefused(Exception):
    """A login that did not succeed. The message is what the caller may see (always the same);
    `reason` is what the audit trail records."""

    def __init__(self, reason: str):
        super().__init__(LOGIN_REFUSED)
        self.reason = reason


def normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def check_username(username: str) -> str:
    name = normalize_username(username)
    if not USERNAME_RE.match(name):
        raise AccountError("A user name is 2-64 characters: letters, digits, '.', '_' or '-', "
                           "starting with a letter or digit.")
    return name


def get_by_username(db: Session, username: str) -> Optional[models.AdminUser]:
    return db.query(models.AdminUser).filter(models.AdminUser.username == normalize_username(username)).first()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- first account

def _has_accounts(db: Session) -> bool:
    return db.query(models.AdminUser.id).first() is not None


def bootstrap_first_admin(db: Session) -> Optional[models.AdminUser]:
    """Make the first admin from ADMIN_USERNAME / ADMIN_PASSWORD if there is no account at all.
    Returns it, or None when there are accounts already or the settings cannot be used."""
    if _has_accounts(db):
        return None
    password = settings.ADMIN_PASSWORD or ""
    try:
        username = check_username(settings.ADMIN_USERNAME)
    except AccountError as exc:
        logger.error("[accounts] ADMIN_USERNAME is not a valid user name: %s", exc)
        return None
    if len(password) < 8 or password.lower() in {"changeme", "password", "admin", username}:
        logger.error(
            "[accounts] No admin account exists and ADMIN_PASSWORD is missing or trivial, so none "
            "was created. Set ADMIN_PASSWORD (it must be changed at first login) or run "
            "`python -m app.cli create-user <name> --role admin` in the container."
        )
        return None
    user = models.AdminUser(username=username, role=models.AdminRole.ADMIN.value,
                            password_hash=passwords.hash_password(password), must_change_password=True,
                            full_name="Initial administrator", created_by="system")
    db.add(user)
    try:
        db.commit()
    except IntegrityError:  # another worker made it first
        db.rollback()
        return None
    db.refresh(user)
    audit.record("user.create", audit.SUCCESS, actor="system", target=username,
                 detail={"role": user.role, "via": "bootstrap"})
    logger.info("[accounts] made the first admin account %r; it must change its password at first login",
                username)
    return user


# ---------------------------------------------------------------- logins and sessions

def authenticate(db: Session, username: str, password: str, ip: Optional[str],
                 user_agent: Optional[str] = None) -> tuple[models.AdminUser, str]:
    """Check a login; on success open a session and return (user, access token).
    LoginRefused otherwise. Audited either way."""
    name = normalize_username(username)[:64]
    now = _now()
    user = get_by_username(db, name) if name else None

    def refuse(reason: str):
        audit.record("auth.login", audit.FAILURE, actor=name or "-", ip=ip, target=name or None,
                     detail={"reason": reason})
        raise LoginRefused(reason)

    if user is None:
        passwords.dummy_verify()
        refuse("unknown user")
    if user.locked_until is not None and user.locked_until > now:
        passwords.dummy_verify()
        refuse("account locked")
    if not user.is_active:
        passwords.dummy_verify()
        refuse("account disabled")
    if not passwords.verify_password(password or "", user.password_hash):
        user.failed_logins = (user.failed_logins or 0) + 1
        locked = user.failed_logins >= settings.ADMIN_MAX_FAILED_LOGINS
        if locked:
            user.locked_until = now + timedelta(minutes=settings.ADMIN_LOCKOUT_MINUTES)
            user.failed_logins = 0
        db.commit()
        if locked:
            audit.record("auth.lockout", audit.SUCCESS, actor="system", ip=ip, target=user.username,
                         detail={"minutes": settings.ADMIN_LOCKOUT_MINUTES,
                                 "after_failures": settings.ADMIN_MAX_FAILED_LOGINS})
        refuse("wrong password")

    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    user.last_login_ip = ip
    expires = now + timedelta(hours=settings.JWT_EXPIRE_HOURS)
    session = models.AdminSession(id=secrets.token_hex(32), user_id=user.id, created_at=now,
                                  last_seen_at=now, expires_at=expires, ip=ip,
                                  user_agent=(user_agent or "")[:255] or None)
    db.add(session)
    db.commit()
    audit.record("auth.login", audit.SUCCESS, actor=user.username, role=user.role, ip=ip,
                 target=user.username, detail={"must_change_password": bool(user.must_change_password)})
    return user, create_access_token(user.username, session.id, expires)


def end_session(db: Session, session_id: str) -> None:
    session = db.get(models.AdminSession, session_id)
    if session is not None and session.revoked_at is None:
        session.revoked_at = _now()
        db.commit()


def _end_sessions(db: Session, user_id: int, keep: Optional[str] = None) -> int:
    query = db.query(models.AdminSession).filter(models.AdminSession.user_id == user_id,
                                                 models.AdminSession.revoked_at.is_(None))
    if keep:
        query = query.filter(models.AdminSession.id != keep)
    return query.update({models.AdminSession.revoked_at: _now()}, synchronize_session=False)


# ---------------------------------------------------------------- own password

def change_own_password(db: Session, user: models.AdminUser, current: str, new: str,
                        session_id: str) -> None:
    """AccountError for a wrong current password or a new one that breaks the policy."""
    if not passwords.verify_password(current or "", user.password_hash):
        raise AccountError("The current password is not correct.")
    try:
        passwords.check_policy(new, user.username, user.password_hash)
    except passwords.PasswordPolicyError as exc:
        raise AccountError(str(exc))
    user.password_hash = passwords.hash_password(new)
    user.must_change_password = False
    user.password_changed_at = _now()
    _end_sessions(db, user.id, keep=session_id)
    db.commit()


# ---------------------------------------------------------------- account management

def _active_admins(db: Session) -> int:
    return db.query(models.AdminUser).filter(models.AdminUser.role == models.AdminRole.ADMIN.value,
                                             models.AdminUser.is_active.is_(True)).count()


def _check_role(role: str) -> str:
    if role not in {r.value for r in models.AdminRole}:
        raise AccountError("The role must be admin, operator or auditor.")
    return role


def create_user(db: Session, *, username: str, role: str, password: str, full_name: Optional[str],
                email: Optional[str], created_by: str) -> models.AdminUser:
    name = check_username(username)
    _check_role(role)
    try:
        passwords.check_policy(password, name)
    except passwords.PasswordPolicyError as exc:
        raise AccountError(str(exc))
    if get_by_username(db, name) is not None:
        raise AccountError(f"There is already an account named {name}.")
    user = models.AdminUser(username=name, role=role, password_hash=passwords.hash_password(password),
                            must_change_password=True, full_name=full_name, email=email,
                            created_by=created_by)
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise AccountError(f"There is already an account named {name}.")
    db.refresh(user)
    return user


def update_user(db: Session, user: models.AdminUser, *, actor_id: int, role: Optional[str] = None,
                is_active: Optional[bool] = None, full_name: Optional[str] = None,
                email: Optional[str] = None) -> dict:
    """Change an account; returns what changed (for the audit trail)."""
    changes = {}
    losing_admin = (
        user.role == models.AdminRole.ADMIN.value and user.is_active
        and ((role is not None and role != models.AdminRole.ADMIN.value) or is_active is False)
    )
    if losing_admin and _active_admins(db) <= 1:
        raise AccountError("This is the last active admin account; it cannot be disabled or demoted.")
    if user.id == actor_id and is_active is False:
        raise AccountError("You cannot disable your own account.")
    if role is not None and role != user.role:
        changes["role"] = [user.role, _check_role(role)]
        user.role = role
    if is_active is not None and bool(is_active) != bool(user.is_active):
        changes["is_active"] = [bool(user.is_active), bool(is_active)]
        user.is_active = bool(is_active)
        if not is_active:
            _end_sessions(db, user.id)
    if full_name is not None and full_name != user.full_name:
        changes["full_name"] = True
        user.full_name = full_name
    if email is not None and email != user.email:
        changes["email"] = True
        user.email = email
    db.commit()
    db.refresh(user)
    return changes


def reset_password(db: Session, user: models.AdminUser, password: str) -> None:
    """Set a temporary password chosen by an admin; its owner must change it at next login."""
    try:
        passwords.check_policy(password, user.username)
    except passwords.PasswordPolicyError as exc:
        raise AccountError(str(exc))
    user.password_hash = passwords.hash_password(password)
    user.must_change_password = True
    user.password_changed_at = _now()
    user.failed_logins = 0
    user.locked_until = None
    _end_sessions(db, user.id)
    db.commit()


def unlock(db: Session, user: models.AdminUser) -> None:
    user.failed_logins = 0
    user.locked_until = None
    db.commit()
