import logging

import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from typing import List
from .. import crud, offline, plans, schemas
from ..utils.license_token import SigningKeyMissing, build_claims, get_signer
from ..core.security import get_current_admin, create_access_token, verify_admin_credentials
from ..core.config import settings
from ..database import get_db
from pydantic import BaseModel, ConfigDict, Field
from datetime import timedelta

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

class LoginRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    username: str
    password: str
    cf_turnstile_response: str = Field(default="", alias="cf-turnstile-response")

class TokenResponse(BaseModel):
    access_token: str
    token_type: str

def verify_turnstile(token: str, remote_ip: str | None) -> None:
    """Reject the request unless Cloudflare confirms the Turnstile token.

    Fails closed: a missing secret, an unreachable siteverify endpoint or a
    malformed reply all refuse the login.
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

@router.post("/login", response_model=TokenResponse)
def login(credentials: LoginRequest, request: Request):
    """Admin login endpoint - returns JWT token"""
    # Checked before the credentials so failed challenges never reach them.
    verify_turnstile(
        credentials.cf_turnstile_response,
        request.client.host if request.client else None,
    )
    if not verify_admin_credentials(credentials.username, credentials.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    access_token = create_access_token(
        data={"sub": credentials.username},
        expires_delta=timedelta(hours=settings.JWT_EXPIRE_HOURS)
    )
    return TokenResponse(access_token=access_token, token_type="bearer")

@router.get("/plans")
def list_plans(admin: str = Depends(get_current_admin)):
    """The plan catalog (app/plans.py): SKUs, names, base asset limits, terms, and the add-on."""
    return plans.catalog()

@router.post("/licenses", response_model=schemas.LicenseResponse)
def create_license(
    license_data: schemas.LicenseCreate,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin)
):
    return crud.create_license(db, license_data)

@router.get("/licenses", response_model=List[schemas.LicenseResponse])
def list_licenses(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin)
):
    return crud.get_all_licenses(db, skip, limit)

@router.get("/licenses/{license_key}", response_model=schemas.LicenseResponse)
def get_license(
    license_key: str,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin)
):
    license = crud.get_license_by_key(db, license_key)
    if not license:
        raise HTTPException(status_code=404, detail="License not found")
    return license

@router.delete("/licenses/{license_key}")
def deactivate_license(
    license_key: str,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin)
):
    success = crud.deactivate_license(db, license_key)
    if not success:
        raise HTTPException(status_code=404, detail="License not found")
    return {"message": "License deactivated successfully"}

@router.delete("/licenses/{license_key}/permanent")
def delete_license(
    license_key: str,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin)
):
    """Permanently delete a license record.

    This is irreversible and removes the row entirely (used for pruning
    legacy licenses). Use the plain DELETE endpoint to only deactivate.
    """
    success = crud.delete_license(db, license_key)
    if not success:
        raise HTTPException(status_code=404, detail="License not found")
    return {"message": "License deleted permanently"}


@router.post("/licenses/{license_key}/reset-binding", response_model=schemas.LicenseResponse)
def reset_license_binding(
    license_key: str,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin)
):
    """Release a license from the machine it is bound to, so the customer can activate it on a
    new or reinstalled server. The old machine is refused from its next validation on.
    """
    license = crud.reset_binding(db, license_key)
    if license is None:
        raise HTTPException(status_code=404, detail="License not found")
    logger.info("[Admin] %s released license %s from its machine", admin, license_key)
    return license


@router.post("/licenses/{license_key}/plan", response_model=schemas.LicenseResponse)
def change_license_plan(
    license_key: str,
    change: schemas.PlanChange,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin)
):
    """Change a license's plan and/or its add-on packs (an upgrade, add-ons bought later, or
    correcting a license the old server downgraded for being offline). The term stays. NGCorion
    picks the new limit up with its next validation or heartbeat.
    """
    if change.plan_code is None and change.addon_packs is None:
        raise HTTPException(status_code=422, detail="Give a plan_code, addon_packs, or both")
    try:
        license = crud.change_plan(db, license_key, change.plan_code, change.addon_packs)
    except ValueError as exc:   # plans.UnknownPlan / InvalidAddons
        raise HTTPException(status_code=422, detail=str(exc))
    if license is None:
        raise HTTPException(status_code=404, detail="License not found")
    logger.info("[Admin] %s set license %s to %s with %s add-on pack(s)",
                admin, license_key, license.plan_code, license.addon_packs)
    return license


# ---------- Offline (air-gapped) licenses: app/offline.py ----------

@router.post("/licenses/{license_key}/offline", response_model=schemas.LicenseResponse)
def set_license_offline(
    license_key: str,
    body: schemas.OfflineToggle,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin)
):
    """Mark a license offline (its NGCorion never reaches this server) or not. Turning it off only
    stops new files; one already issued works until its valid_until."""
    license = crud.set_offline_allowed(db, license_key, body.allowed)
    if license is None:
        raise HTTPException(status_code=404, detail="License not found")
    logger.info("[Admin] %s set offline=%s on license %s", admin, body.allowed, license_key)
    return license


@router.post("/licenses/{license_key}/offline-file", response_model=schemas.OfflineFileResponse)
def issue_offline_license_file(
    license_key: str,
    body: schemas.OfflineFileRequest,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin)
):
    """Sign an offline license file for the request file the customer's NGCorion made: bound to
    that machine, with the license's plan and asset limit, valid for one year (or until the
    license expires, if sooner). 422 for a file that is not a request, 409 when the license
    cannot have one (not offline, revoked, expired, bound to another machine)."""
    try:
        request = offline.parse_request(body.request)
    except offline.InvalidOfflineRequest as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    try:
        signer = get_signer()
    except SigningKeyMissing as exc:
        logger.error("[License] %s", exc)
        raise HTTPException(status_code=503, detail="License signing is not configured on the license server")
    try:
        license, until = crud.issue_offline_license(db, license_key, request, issued_by=admin)
    except crud.OfflineIssueRefused as exc:
        status = 404 if str(exc) == "License not found" else 409
        raise HTTPException(status_code=status, detail=str(exc))

    token = signer.sign(build_claims(
        "offline", license_key=license.license_key, vm_fingerprint=request.vm_fingerprint,
        valid=True, message="offline license", nonce=request.nonce, license=license,
        extra={"offline_until": int(until.timestamp())},
    ))
    logger.info("[Admin] %s issued an offline license file for %s, valid until %s",
                admin, license_key, until.isoformat())
    return schemas.OfflineFileResponse(
        filename=offline.file_name(license.organization_name, license.license_key),
        content=offline.license_file(token),
        valid_until=until,
        license=license,
    )


@router.post("/offline/inspect", response_model=schemas.OfflineInspectResponse)
def inspect_offline_file(
    body: schemas.OfflineInspectRequest,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin)
):
    """Check an offline file without changing anything (the Test page). A request file: what it
    asks for and whether a license file could be issued for it. A license file: what it grants,
    whether this server's key signed it, whether it is still valid, and whether it is in the issue
    record. 422 for text that is neither."""
    try:
        signer = get_signer()
        public_raw, kid = signer.public_raw, signer.kid
    except SigningKeyMissing:
        public_raw = kid = None
    try:
        return crud.inspect_offline_file(db, body.content, public_raw, kid)
    except (offline.InvalidOfflineRequest, offline.InvalidOfflineLicense) as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/licenses/{license_key}/offline-issues", response_model=List[schemas.OfflineIssueResponse])
def list_offline_issues(
    license_key: str,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin)
):
    """The offline license files issued for a license, newest first."""
    license = crud.get_license_by_key(db, license_key)
    if license is None:
        raise HTTPException(status_code=404, detail="License not found")
    return crud.offline_issues(db, license.id)
