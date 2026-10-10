"""
Admin console authentication and authorisation.

A login (app/accounts.py) opens a server-side session (models.AdminSession) and hands the browser
a short JWT that names it: claims sub (user name), sid (session id), iat, exp, iss, aud; HS256
with SECRET_KEY. Every request checks both: the JWT's signature, issuer, audience and expiry,
and then the session - not revoked (logout, password change, account disabled), not past its
absolute end (JWT_EXPIRE_HOURS after login) and used within ADMIN_IDLE_TIMEOUT_MINUTES. So a
stolen token stops working at logout, and an unattended console locks itself.

Roles (models.AdminRole): auditor < operator < admin.
  require_reader    any role: read licenses and plans
  require_operator  operator or admin: change licenses
  require_admin     admin: accounts, permanent deletion
  require_auditor   auditor or admin: the audit trail (an operator does not review their own
                    actions)
An account that must change its password (first login, reset by an admin) gets nothing but
/me, /me/password and /logout until it has (get_current_admin_any).
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from .config import settings

TOKEN_ISSUER = "ngcorion-license-server"
TOKEN_AUDIENCE = "license-admin"
# last_seen_at is written at most this often per session, not on every request.
_SEEN_RESOLUTION = timedelta(seconds=60)

ROLE_RANK = {models.AdminRole.AUDITOR.value: 1, models.AdminRole.OPERATOR.value: 2,
             models.AdminRole.ADMIN.value: 3}

security = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AdminPrincipal:
    """The admin behind a request."""
    user_id: int
    username: str
    role: str
    session_id: str
    must_change_password: bool = False

    def __str__(self) -> str:  # log lines and issued_by columns show the user name
        return self.username


def _unauthorized(detail: str = "Not authenticated") -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail,
                         headers={"WWW-Authenticate": "Bearer"})


def create_access_token(username: str, session_id: str, expires_at: datetime) -> str:
    """The JWT for a session."""
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"sub": username, "sid": session_id, "iat": int(now.timestamp()),
         "exp": int(expires_at.timestamp()), "iss": TOKEN_ISSUER, "aud": TOKEN_AUDIENCE},
        settings.SECRET_KEY, algorithm="HS256",
    )


def _decode(token: str) -> dict:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"], issuer=TOKEN_ISSUER,
                          audience=TOKEN_AUDIENCE, options={"require": ["exp", "iat", "sub", "sid"]})
    except jwt.PyJWTError:
        raise _unauthorized("Could not validate credentials")


def get_current_admin_any(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
) -> AdminPrincipal:
    """The admin of a valid session, whether or not they still have to change their password."""
    if credentials is None or not credentials.credentials:
        raise _unauthorized()
    claims = _decode(credentials.credentials)
    session = db.get(models.AdminSession, claims["sid"])
    now = datetime.now(timezone.utc)
    if session is None or session.revoked_at is not None or session.expires_at <= now:
        raise _unauthorized("Your session has ended. Please log in again.")
    if now - session.last_seen_at > timedelta(minutes=settings.ADMIN_IDLE_TIMEOUT_MINUTES):
        session.revoked_at = now
        db.commit()
        raise _unauthorized("Your session ended after a period of inactivity. Please log in again.")
    user = db.get(models.AdminUser, session.user_id)
    if user is None or not user.is_active or user.username != claims["sub"]:
        raise _unauthorized("Your session has ended. Please log in again.")
    if now - session.last_seen_at >= _SEEN_RESOLUTION:
        session.last_seen_at = now
        db.commit()
    return AdminPrincipal(user_id=user.id, username=user.username, role=user.role,
                          session_id=session.id, must_change_password=bool(user.must_change_password))


def get_current_admin(principal: AdminPrincipal = Depends(get_current_admin_any)) -> AdminPrincipal:
    """The admin of a valid session that may work: refused until a required password change."""
    if principal.must_change_password:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="You must change your password before continuing.")
    return principal


def _at_least(role: str):
    def dependency(principal: AdminPrincipal = Depends(get_current_admin)) -> AdminPrincipal:
        if ROLE_RANK.get(principal.role, 0) < ROLE_RANK[role]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="Your role does not allow this action.")
        return principal
    dependency.__name__ = f"require_{role}"
    return dependency


require_reader = _at_least(models.AdminRole.AUDITOR.value)
require_operator = _at_least(models.AdminRole.OPERATOR.value)
require_admin = _at_least(models.AdminRole.ADMIN.value)


def require_auditor(principal: AdminPrincipal = Depends(get_current_admin)) -> AdminPrincipal:
    """Auditor or admin: reading the audit trail."""
    if principal.role not in (models.AdminRole.AUDITOR.value, models.AdminRole.ADMIN.value):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Your role does not allow this action.")
    return principal


def request_ip(request: Request) -> Optional[str]:
    """The caller's address as nginx saw it (X-Real-IP, which nginx overwrites), else the socket
    peer. Same reasoning as app/core/portal_auth.client_ip."""
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.split(",")[0].strip()[:64]
    return request.client.host if request.client else None
