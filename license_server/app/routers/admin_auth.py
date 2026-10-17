"""
Admin console sign-in: login, logout, the signed-in user, and changing one's own password.
Accounts, lockout and sessions: app/accounts.py; checking a session: app/core/security.py.
"""
import logging
from datetime import datetime
from typing import Optional

import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from .. import accounts, audit, models
from ..core.config import settings
from ..core.security import AdminPrincipal, get_current_admin_any, request_ip
from ..database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin-auth"])

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


class LoginRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    username: str = Field(max_length=64)
    password: str = Field(max_length=128)
    cf_turnstile_response: str = Field(default="", alias="cf-turnstile-response", max_length=4096)


class AdminMe(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    username: str
    full_name: Optional[str] = None
    email: Optional[str] = None
    role: str
    must_change_password: bool
    last_login_at: Optional[datetime] = None
    last_login_ip: Optional[str] = None
    password_changed_at: Optional[datetime] = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: AdminMe
    idle_timeout_minutes: int


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=128)
    new_password: str = Field(max_length=128)


def verify_turnstile(token: str, remote_ip: Optional[str]) -> None:
    """Reject the request unless Cloudflare confirms the Turnstile token.

    Fails closed: a missing secret, an unreachable siteverify endpoint or a
    malformed reply all refuse the login. Skipped only when the administrator
    turned the check off (TURNSTILE_ENABLED=false, a host with no route to
    Cloudflare).
    """
    if not settings.TURNSTILE_ENABLED:
        return
    if not settings.TURNSTILE_SECRET_KEY:
        logger.error("TURNSTILE_SECRET_KEY is not set; refusing admin login")
        raise HTTPException(status_code=503, detail="Login verification is not configured")
    if not token:
        raise HTTPException(status_code=400, detail="Please complete the human verification")

    payload = {"secret": settings.TURNSTILE_SECRET_KEY, "response": token}
    if remote_ip:
        payload["remoteip"] = remote_ip
    try:
        resp = requests.post(TURNSTILE_VERIFY_URL, data=payload, timeout=10)
        result = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.error("Turnstile siteverify failed: %s", exc)
        raise HTTPException(status_code=503, detail="Could not verify the login challenge, try again")

    if not result.get("success"):
        logger.warning("Turnstile rejected admin login: %s", result.get("error-codes"))
        raise HTTPException(status_code=400, detail="Human verification failed, please try again")


@router.get("/login-options")
def login_options():
    """Whether the login form must show the Turnstile challenge (TURNSTILE_ENABLED). Public: the
    form asks before anyone signs in, so the flag lives only in the server's environment."""
    return {"turnstile_enabled": settings.TURNSTILE_ENABLED}


@router.get("/login-banner")
def login_banner():
    """The notice the login page shows before anyone signs in (ADMIN_LOGIN_BANNER). Public."""
    return {"banner": settings.ADMIN_LOGIN_BANNER}


@router.post("/login", response_model=TokenResponse)
def login(credentials: LoginRequest, request: Request, db: Session = Depends(get_db)):
    """Sign in. The same 401 for every refusal (unknown user, wrong password, locked, disabled);
    the audit trail has the real reason."""
    ip = request_ip(request)
    # Checked before the credentials so failed challenges never reach them.
    verify_turnstile(credentials.cf_turnstile_response, ip)
    try:
        user, token = accounts.authenticate(db, credentials.username, credentials.password, ip,
                                            request.headers.get("user-agent"))
    except accounts.LoginRefused as exc:
        raise HTTPException(
            status_code=401,
            detail=f"{exc} After {settings.ADMIN_MAX_FAILED_LOGINS} failed attempts an account is "
                   f"locked for {settings.ADMIN_LOCKOUT_MINUTES} minutes.",
        )
    return TokenResponse(access_token=token, user=AdminMe.model_validate(user),
                         idle_timeout_minutes=settings.ADMIN_IDLE_TIMEOUT_MINUTES)


@router.post("/logout")
def logout(request: Request, principal: AdminPrincipal = Depends(get_current_admin_any),
           db: Session = Depends(get_db)):
    """End this session at the server: its token stops working at once."""
    accounts.end_session(db, principal.session_id)
    audit.record("auth.logout", audit.SUCCESS, actor=principal.username, role=principal.role,
                 ip=request_ip(request), target=principal.username)
    return {"message": "Signed out"}


@router.get("/me", response_model=AdminMe)
def me(principal: AdminPrincipal = Depends(get_current_admin_any), db: Session = Depends(get_db)):
    return db.get(models.AdminUser, principal.user_id)


@router.post("/me/password")
def change_password(body: PasswordChange, request: Request,
                    principal: AdminPrincipal = Depends(get_current_admin_any),
                    db: Session = Depends(get_db)):
    """Change one's own password (also the required change at first login). Ends every other
    session of the account."""
    user = db.get(models.AdminUser, principal.user_id)
    ip = request_ip(request)
    try:
        accounts.change_own_password(db, user, body.current_password, body.new_password,
                                     principal.session_id)
    except accounts.AccountError as exc:
        audit.record("auth.password_change", audit.FAILURE, actor=principal.username,
                     role=principal.role, ip=ip, target=principal.username, detail={"reason": str(exc)})
        raise HTTPException(status_code=400, detail=str(exc))
    audit.record("auth.password_change", audit.SUCCESS, actor=principal.username, role=principal.role,
                 ip=ip, target=principal.username)
    return {"message": "Password changed"}
