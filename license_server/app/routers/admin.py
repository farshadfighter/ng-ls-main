import logging

import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from typing import List
from .. import crud, schemas
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
