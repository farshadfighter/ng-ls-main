"""Endpoints the internal portal (http://172.16.120.80) calls on this server.

Phase 1 of the portal integration: inbound only. The outbound halves — license
event notification to POST {PORTAL_BASE_URL}/internal/ls/license-events, and
the users/orders sync into portal_users_cache / portal_orders_cache — are not
implemented here yet.

Not under /api
--------------
This router is mounted at /internal/portal, outside the /api prefix every other
router uses, so the two audiences are separable at the proxy: nginx serves
/api/ to the world (well, to the admin UI and the licensed app) and restricts
/internal/ to PORTAL_IP_WHITELIST with `allow`/`deny`. A path under /api would
have to be carved out of an already-public location block.

That nginx block is required, not optional. Without it these paths fall through
to the SPA's `try_files ... /index.html` fallback and answer **200 with
index.html**, which the portal would read as success.

Auth
----
Applied to every route in this router as router-level dependencies, in order:
source IP, then token (signature + iss/aud/exp + single-use jti). See
app/core/portal_auth.py. Nothing here is reachable unauthenticated, and there
is deliberately no per-route override.

These routes are also exempt from the per-IP rate limiter
(app/middleware/rate_limit.py) — the portal is a single source IP placing
orders in batches, and the IP whitelist plus single-use tokens are the real
gate.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from .. import audit, crud, schemas
from ..core.portal_auth import client_ip, require_portal_ip, verify_portal_jwt
from ..database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/internal/portal",
    tags=["internal-portal"],
    # Order is load-bearing: a caller from the wrong network is refused before
    # any token parsing or Redis round trip. Both run for every route below.
    dependencies=[Depends(require_portal_ip), Depends(verify_portal_jwt)],
    responses={
        401: {"description": "Missing, invalid, expired or replayed portal token"},
        403: {"description": "Source IP is not in PORTAL_IP_WHITELIST"},
        503: {"description": "Portal integration unconfigured, or Redis unavailable"},
    },
)


@router.post("/licenses", response_model=schemas.PortalLicenseResponse)
def create_license_from_portal(
    payload: schemas.PortalLicenseCreate,
    response: Response,
    request: Request,
    db: Session = Depends(get_db),
):
    """Create a license for a portal order.

    Idempotent on ``order_id``: retrying a call whose response the portal never
    received returns the license already created for that order rather than
    minting a second key. The two cases are distinguishable by status code —
    201 for a new license, 200 for a replay — while the body is identical, so a
    portal that ignores the status code still behaves correctly.

    ``plan_code`` is a SKU of the catalog (app/plans.py, e.g. "NGC-ENT-500-1Y"),
    with ``addon_packs`` 50-asset add-ons; ``plan_type`` with an old plan name
    ("plan_250") is accepted instead. Anything else is a 422.

    The asset limit comes from this server's catalog (plan + add-ons).
    ``duration_days`` and ``is_pilot_mode``, when sent, override the catalog —
    the order decides the term, not the price list.
    """
    license, created = crud.create_license_from_portal(db, payload)

    response.status_code = (
        status.HTTP_201_CREATED if created else status.HTTP_200_OK
    )
    if created:
        audit.record("license.create", audit.SUCCESS, actor="portal", ip=client_ip(request),
                     target=license.license_key,
                     detail={"plan": license.plan_code, "addon_packs": license.addon_packs,
                             "order_id": license.order_id, "order_number": license.order_number})

    return schemas.PortalLicenseResponse(
        license_id=license.id,
        license_key=license.license_key,
        expires_at=license.expires_at,
        is_active=license.is_active,
    )


@router.get("/licenses/{license_id}", response_model=schemas.PortalLicenseStatus)
def get_license_status(
    license_id: int,
    db: Session = Depends(get_db),
):
    """Current state of a license, by the ``license_id`` the create call returned.

    Returns any license, not only portal-created ones, so that a license an
    admin made by hand and then linked to an order is still inspectable from
    the portal. ``source`` in the response says which it is.
    """
    license = crud.get_license_by_id(db, license_id)
    if not license:
        raise HTTPException(status_code=404, detail="License not found")

    return schemas.PortalLicenseStatus(
        license_id=license.id,
        license_key=license.license_key,
        plan_code=license.plan_code,
        plan_name=license.plan_name,
        is_active=license.is_active,
        is_pilot_mode=license.is_pilot_mode,
        customer_name=license.customer_name,
        customer_email=license.customer_email,
        organization_name=license.organization_name,
        user_uuid7=license.user_uuid7,
        order_id=license.order_id,
        order_number=license.order_number,
        source=license.source,
        created_at=license.created_at,
        activated_at=license.activated_at,
        expires_at=license.expires_at,
        last_validated_at=license.last_validated_at,
        last_heartbeat_at=license.last_heartbeat_at,
        portal_synced_at=license.portal_synced_at,
        max_assets=license.max_assets,
        addon_packs=license.addon_packs or 0,
        reported_assets=license.reported_assets,
        reported_assets_at=license.reported_assets_at,
        # Reports that the license is bound to a machine without leaking the
        # fingerprint hash itself.
        is_activated=license.vm_fingerprint is not None,
    )
