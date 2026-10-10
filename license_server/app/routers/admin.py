"""
The admin console's license API. Sign-in is in admin_auth.py, accounts in admin_users.py, the
audit trail in admin_audit.py.

Roles (app/core/security.py): reading needs any role, changing a license needs operator or admin,
deleting one for good needs admin. Every change is written to the audit trail (app/audit.py).
"""
import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from .. import audit, crud, offline, plans, schemas
from ..core.security import AdminPrincipal, request_ip, require_admin, require_operator, require_reader
from ..database import get_db
from ..utils.license_token import SigningKeyMissing, build_claims, get_signer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _audit(request: Request, admin: AdminPrincipal, action: str, target: str, outcome: str = audit.SUCCESS,
           detail=None) -> None:
    audit.record(action, outcome, actor=admin.username, role=admin.role, ip=request_ip(request),
                 target=target, detail=detail)


@router.get("/plans")
def list_plans(admin: AdminPrincipal = Depends(require_reader)):
    """The plan catalog (app/plans.py): SKUs, names, base asset limits, terms, and the add-on."""
    return plans.catalog()

@router.post("/licenses", response_model=schemas.LicenseResponse)
def create_license(
    license_data: schemas.LicenseCreate,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminPrincipal = Depends(require_operator)
):
    license = crud.create_license(db, license_data)
    _audit(request, admin, "license.create", license.license_key,
           detail={"plan": license.plan_code, "addon_packs": license.addon_packs,
                   "organization": license.organization_name, "offline": bool(license.offline_allowed)})
    return license

@router.get("/licenses", response_model=List[schemas.LicenseResponse])
def list_licenses(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
    admin: AdminPrincipal = Depends(require_reader)
):
    return crud.get_all_licenses(db, skip, limit)

@router.get("/licenses/{license_key}", response_model=schemas.LicenseResponse)
def get_license(
    license_key: str,
    db: Session = Depends(get_db),
    admin: AdminPrincipal = Depends(require_reader)
):
    license = crud.get_license_by_key(db, license_key)
    if not license:
        raise HTTPException(status_code=404, detail="License not found")
    return license

@router.delete("/licenses/{license_key}")
def deactivate_license(
    license_key: str,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminPrincipal = Depends(require_operator)
):
    success = crud.deactivate_license(db, license_key)
    if not success:
        raise HTTPException(status_code=404, detail="License not found")
    _audit(request, admin, "license.revoke", license_key)
    return {"message": "License deactivated successfully"}

@router.delete("/licenses/{license_key}/permanent")
def delete_license(
    license_key: str,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminPrincipal = Depends(require_admin)
):
    """Permanently delete a license record.

    This is irreversible and removes the row entirely (used for pruning
    legacy licenses). Use the plain DELETE endpoint to only deactivate.
    """
    success = crud.delete_license(db, license_key)
    if not success:
        raise HTTPException(status_code=404, detail="License not found")
    _audit(request, admin, "license.delete", license_key)
    return {"message": "License deleted permanently"}


@router.post("/licenses/{license_key}/reset-binding", response_model=schemas.LicenseResponse)
def reset_license_binding(
    license_key: str,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminPrincipal = Depends(require_operator)
):
    """Release a license from the machine it is bound to, so the customer can activate it on a
    new or reinstalled server. The old machine is refused from its next validation on.
    """
    license = crud.reset_binding(db, license_key)
    if license is None:
        raise HTTPException(status_code=404, detail="License not found")
    _audit(request, admin, "license.release_machine", license_key)
    return license


@router.post("/licenses/{license_key}/plan", response_model=schemas.LicenseResponse)
def change_license_plan(
    license_key: str,
    change: schemas.PlanChange,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminPrincipal = Depends(require_operator)
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
    _audit(request, admin, "license.change_plan", license_key,
           detail={"plan": license.plan_code, "addon_packs": license.addon_packs})
    return license


# ---------- Offline (air-gapped) licenses: app/offline.py ----------

@router.post("/licenses/{license_key}/offline", response_model=schemas.LicenseResponse)
def set_license_offline(
    license_key: str,
    body: schemas.OfflineToggle,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminPrincipal = Depends(require_operator)
):
    """Mark a license offline (its NGCorion never reaches this server) or not. Turning it off only
    stops new files; one already issued works until its valid_until."""
    license = crud.set_offline_allowed(db, license_key, body.allowed)
    if license is None:
        raise HTTPException(status_code=404, detail="License not found")
    _audit(request, admin, "license.offline_option", license_key, detail={"allowed": body.allowed})
    return license


@router.post("/licenses/{license_key}/offline-file", response_model=schemas.OfflineFileResponse)
def issue_offline_license_file(
    license_key: str,
    body: schemas.OfflineFileRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminPrincipal = Depends(require_operator)
):
    """Sign an offline license file for the request file the customer's NGCorion made: bound to
    that machine, with the license's plan and asset limit, valid for one year (or until the
    license expires, if sooner). 422 for a file that is not a request, 409 when the license
    cannot have one (not offline, revoked, expired, bound to another machine)."""
    try:
        req = offline.parse_request(body.request)
    except offline.InvalidOfflineRequest as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    try:
        signer = get_signer()
    except SigningKeyMissing as exc:
        logger.error("[License] %s", exc)
        raise HTTPException(status_code=503, detail="License signing is not configured on the license server")
    try:
        license, until = crud.issue_offline_license(db, license_key, req, issued_by=admin.username)
    except crud.OfflineIssueRefused as exc:
        status = 404 if str(exc) == "License not found" else 409
        if status == 409:
            _audit(request, admin, "license.offline_file", license_key, audit.FAILURE,
                   detail={"reason": str(exc), "machine": req.vm_fingerprint[:16]})
        raise HTTPException(status_code=status, detail=str(exc))

    token = signer.sign(build_claims(
        "offline", license_key=license.license_key, vm_fingerprint=req.vm_fingerprint,
        valid=True, message="offline license", nonce=req.nonce, license=license,
        extra={"offline_until": int(until.timestamp())},
    ))
    _audit(request, admin, "license.offline_file", license_key,
           detail={"valid_until": until.isoformat(), "machine": req.vm_fingerprint[:16],
                   "reported_assets": req.asset_count})
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
    admin: AdminPrincipal = Depends(require_reader)
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
    admin: AdminPrincipal = Depends(require_reader)
):
    """The offline license files issued for a license, newest first."""
    license = crud.get_license_by_key(db, license_key)
    if license is None:
        raise HTTPException(status_code=404, detail="License not found")
    return crud.offline_issues(db, license.id)
