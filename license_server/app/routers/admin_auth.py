"""
Admin console sign-in: login, logout, the signed-in user, and changing one's own password.
Accounts, lockout and sessions: app/accounts.py; checking a session: app/core/security.py.
The bot check on login (CAPTCHA_MODE): the server's own picture (app/captcha.py), Cloudflare
Turnstile, or none.
"""
import base64
import logging
from datetime import datetime
from typing import Optional

import requests
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from .. import accounts, audit, captcha, models
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
    # CAPTCHA_MODE=builtin: the picture's id (POST /api/admin/captcha) and the characters typed
    captcha_id: str = Field(default="", max_length=64)
    captcha_answer: str = Field(default="", max_length=16)


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
    malformed reply all refuse the login. Asked only with CAPTCHA_MODE=turnstile.
    """
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


def verify_captcha(challenge_id: str, answer: str) -> None:
    """Reject the request unless `answer` is what the server's picture showed (app/captcha.py).

    The picture is spent by this attempt, right or wrong; the form then asks for a new one.
    Fails closed: without Redis the login is refused (503).
    """
    if not challenge_id or not answer.strip():
        raise HTTPException(status_code=400, detail="Type the characters shown in the picture")
    try:
        ok = captcha.check(challenge_id, answer)
    except captcha.CaptchaUnavailable as exc:
        logger.error("Login picture check unavailable (Redis): %s", exc)
        raise HTTPException(status_code=503, detail="Could not check the picture, try again")
    if not ok:
        raise HTTPException(status_code=400,
                            detail="The characters do not match the picture, or it expired. Try the new picture.")


def verify_challenge(credentials: LoginRequest, remote_ip: Optional[str]) -> None:
    """The bot check CAPTCHA_MODE asks for: the server's picture, Turnstile, or none (off)."""
    if settings.CAPTCHA_MODE == "builtin":
        verify_captcha(credentials.captcha_id, credentials.captcha_answer)
    elif settings.CAPTCHA_MODE == "turnstile":
        verify_turnstile(credentials.cf_turnstile_response, remote_ip)


@router.get("/login-options")
def login_options():
    """Which bot check the login form must show (CAPTCHA_MODE). Public: the form asks before anyone
    signs in, so the choice lives only in the server's environment. `turnstile_enabled` is kept for
    a login page loaded before the upgrade."""
    return {"captcha": settings.CAPTCHA_MODE, "turnstile_enabled": settings.CAPTCHA_MODE == "turnstile"}


@router.post("/captcha")
def new_captcha(response: Response):
    """A new login picture (CAPTCHA_MODE=builtin): its id, the PNG as a data URL, and how many seconds
    it stays valid. Public, under the per-IP rate limit; POST and no-store so nothing caches it."""
    if settings.CAPTCHA_MODE != "builtin":
        raise HTTPException(status_code=404, detail="This server does not use the login picture")
    try:
        challenge_id, png = captcha.new_challenge()
    except captcha.CaptchaUnavailable as exc:
        logger.error("Login picture unavailable (Redis): %s", exc)
        raise HTTPException(status_code=503, detail="Could not make the login picture, try again")
    response.headers["Cache-Control"] = "no-store"
    return {"id": challenge_id, "image": "data:image/png;base64," + base64.b64encode(png).decode(),
            "expires_in": captcha.TTL_SECONDS}


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
    verify_challenge(credentials, ip)
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
