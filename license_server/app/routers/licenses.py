"""
Endpoints NGCorion talks to: activate, validate, heartbeat.

The entitlement is the asset limit (plan + add-on packs, app/plans.py), which
NGCorion enforces itself from the signed answer. There is no per-operation
quota any more, so no /consume: audits and hardening runs are not counted.

Every answer carries `signed_token`, the same answer signed with the server's
Ed25519 key (app/utils/license_token.py). NGCorion only acts on a token it can
verify, so these endpoints refuse to answer at all (503) while no signing key
is configured - an unsigned "valid" would be ignored by the client anyway, and
a clear 503 is easier to diagnose than a product that silently stays locked.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from .. import crud, schemas
from ..database import get_db
from ..utils.license_token import SigningKeyMissing, TokenSigner, build_claims, get_signer, limits_of
from ..utils.signing import verify_signature

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/licenses", tags=["licenses"])


def require_signer() -> TokenSigner:
    """The configured token signer; 503 when the server has no signing key."""
    try:
        return get_signer()
    except SigningKeyMissing as exc:
        logger.error("[License] %s", exc)
        raise HTTPException(status_code=503, detail="License signing is not configured on the license server")


def verify_request_signature(
    x_timestamp: Optional[str] = Header(None),
    x_signature: Optional[str] = Header(None),
    data: schemas.LicenseValidate = None
):
    """Verify the client's HMAC over the request (organization token as the shared secret)."""
    if not x_timestamp or not x_signature:
        raise HTTPException(status_code=401, detail="Missing signature headers")

    request_data = {
        "license_key": data.license_key,
        "organization_token": data.organization_token,
        "vm_fingerprint": data.vm_fingerprint
    }

    # Use organization_token as the shared secret
    if not verify_signature(request_data, x_signature, x_timestamp, data.organization_token):
        raise HTTPException(status_code=401, detail="Invalid signature")

    return True


def _signed_response(signer: TokenSigner, typ: str, data, valid: bool, message: str, license=None,
                     organization_token: Optional[str] = None, extra=None) -> schemas.ValidationResponse:
    """A ValidationResponse whose plain fields and signed token say the same thing."""
    token = signer.sign(build_claims(
        typ, license_key=data.license_key, vm_fingerprint=data.vm_fingerprint, valid=valid,
        message=message, nonce=data.nonce, license=license, extra=extra,
    ))
    if license is None or not valid:
        return schemas.ValidationResponse(valid=valid, message=message, signed_token=token)
    return schemas.ValidationResponse(
        valid=True,
        message=message,
        plan_code=license.plan_code,
        plan_name=license.plan_name,
        is_pilot_mode=license.is_pilot_mode,
        organization_token=organization_token,
        expires_at=license.expires_at,
        limits=limits_of(license),
        signed_token=token,
    )


@router.post("/activate", response_model=schemas.ValidationResponse)
def activate_license(data: schemas.LicenseActivate, db: Session = Depends(get_db),
                     signer: TokenSigner = Depends(require_signer)):
    """Bind a license to the machine fingerprint the client computed, and return the organization
    token. Public: the license key itself is the credential. A rejection is HTTP 400.
    """
    # Only releases with signed licensing send a nonce, and only they compute
    # the fingerprint on the licensed machine.
    success, message, license = crud.activate_license(
        db, data.license_key, data.vm_fingerprint, client_computed=bool(data.nonce)
    )

    if not success:
        raise HTTPException(status_code=400, detail=message)

    return _signed_response(signer, "activate", data, True, message, license,
                            organization_token=license.organization_token)


@router.post("/validate", response_model=schemas.ValidationResponse)
def validate_license(
    data: schemas.LicenseValidate,
    db: Session = Depends(get_db),
    x_timestamp: Optional[str] = Header(None),
    x_signature: Optional[str] = Header(None),
    signer: TokenSigner = Depends(require_signer),
):
    """Validate a license for a machine. The verdict comes back as HTTP 200 with `valid` true or
    false - signed either way, so the client can tell a real "no" from a broken answer.

    The client's HMAC headers are optional (older callers and the admin test panel send none);
    when present they must be right.
    """
    if x_timestamp and x_signature:
        try:
            verify_request_signature(x_timestamp, x_signature, data)
        except HTTPException:
            return _signed_response(signer, "validate", data, False, "Invalid signature")

    success, message, license = crud.validate_license(
        db, data.license_key, data.organization_token, data.vm_fingerprint
    )

    if not success:
        return _signed_response(signer, "validate", data, False, message)

    return _signed_response(signer, "validate", data, True, message, license)


@router.post("/heartbeat", response_model=schemas.HeartbeatResponse)
def heartbeat(data: schemas.LicenseHeartbeat, db: Session = Depends(get_db),
              signer: TokenSigner = Depends(require_signer)):
    """Liveness ping from the client (hourly), with the number of assets it manages. A rejection
    is HTTP 400; success carries a signed token with the license's current state.

    Each signed answer is also what keeps NGCorion on its plan: after 48 hours without one it
    falls back to the pilot entitlement (5 assets) until it gets one again.
    """
    success, message = crud.heartbeat(
        db, data.license_key, data.organization_token, data.vm_fingerprint, data.asset_count
    )

    if not success:
        raise HTTPException(status_code=400, detail=message)

    license = crud.get_license_by_key(db, data.license_key)
    # crud.heartbeat only authenticates the caller; whether the license is still
    # usable (active, not expired) is decided here for the signed answer.
    usable = bool(license and license.is_active and license.expires_at > datetime.now(timezone.utc))
    token = signer.sign(build_claims(
        "heartbeat", license_key=data.license_key, vm_fingerprint=data.vm_fingerprint,
        valid=usable, message=message, nonce=data.nonce, license=license,
    ))
    return schemas.HeartbeatResponse(success=True, message=message, signed_token=token)
