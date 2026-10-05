"""Admin endpoints over the portal user/order cache (phase 4).

    GET  /api/admin/portal/users?q=      browse cached portal users
    GET  /api/admin/portal/orders?q=     browse cached portal orders
    POST /api/admin/portal/sync          run a sync now
    GET  /api/admin/portal/sync/status   is one running, how did the last go

These back the admin UI's manual license form: an admin creating a license by
hand looks a customer up here and links the license to the portal user and
order (licenses.user_uuid7 / order_id, added in migration 003).

Reads come from the LOCAL cache, never from the portal. That keeps the form
responsive and keeps it working when the portal is down — at the cost of
showing data up to PORTAL_SYNC_INTERVAL_SECONDS old, which is why every row
carries synced_at and POST /sync exists.

A separate router from app/routers/admin.py, mounted under the same
/api/admin prefix, purely to keep license administration and portal-cache
browsing in separate files. Same auth: every route depends on
get_current_admin, so these need an admin UI session token, NOT a portal
token. They are ordinary admin endpoints, reachable through nginx's public
/api/ block, and have nothing to do with /internal/portal/*.
"""
import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .. import models, schemas
from ..core import portal_client
from ..core.config import settings
from ..core.portal_client import PortalClientError
from ..core.portal_sync import SyncAlreadyRunning, syncer
from ..core.security import get_current_admin
from ..database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/portal",
    tags=["admin-portal"],
    dependencies=[Depends(get_current_admin)],
)


def _like(term: str) -> str:
    """A LIKE pattern for a substring search, with the wildcards the user
    typed neutralised so a stray % does not match everything."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@router.get("/users", response_model=List[schemas.PortalUserCacheResponse])
def list_portal_users(
    q: Optional[str] = Query(
        default=None,
        description="Substring match on email, full name or organization name.",
    ),
    refresh: bool = Query(
        default=False,
        description=(
            "Also query the portal live for `q` and cache the result before "
            "answering. For finding a customer created since the last sync."
        ),
    ),
    skip: int = 0,
    limit: int = Query(default=50, le=500),
    db: Session = Depends(get_db),
):
    """Browse cached portal users.

    `refresh=true` exists for the one case the cache cannot cover: an order was
    just placed by a brand-new customer, and the admin is creating their
    license before the next sync has run. It forwards `q` to the portal's
    documented `?q=` parameter, upserts whatever comes back, and then answers
    from the cache as usual.

    A failed refresh is deliberately NOT an error: the cached results are still
    the useful answer, so the failure is logged and the local search proceeds.
    The alternative — a 502 that hides rows we already hold — would make the
    licence form unusable whenever the portal blinks.
    """
    if refresh and q:
        _refresh_users_from_portal(q)

    query = db.query(models.PortalUserCache)
    if q:
        pattern = _like(q)
        query = query.filter(
            or_(
                models.PortalUserCache.email.ilike(pattern),
                models.PortalUserCache.full_name.ilike(pattern),
                models.PortalUserCache.organization_name.ilike(pattern),
            )
        )
    # Most recently seen first, so a just-synced customer is at the top.
    return (
        query.order_by(models.PortalUserCache.synced_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


def _refresh_users_from_portal(q: str) -> None:
    """Best-effort live lookup of `q`, cached for later. Never raises."""
    if not syncer.enabled:
        return
    try:
        records = portal_client.fetch_users(q=q)
        written = syncer.cache_users(records)
    except PortalClientError as exc:
        logger.warning("[admin_portal] live user lookup for %r failed: %s", q, exc)
        return
    except Exception:
        logger.exception("[admin_portal] live user lookup for %r failed", q)
        return
    if written:
        logger.info("[admin_portal] live lookup cached %d user(s) for %r", written, q)


@router.get("/orders", response_model=List[schemas.PortalOrderCacheResponse])
def list_portal_orders(
    q: Optional[str] = Query(
        default=None,
        description="Substring match on order number, plan type or status.",
    ),
    user_uuid7: Optional[str] = Query(
        default=None, description="Only this portal user's orders."
    ),
    skip: int = 0,
    limit: int = Query(default=50, le=500),
    db: Session = Depends(get_db),
):
    """Browse cached portal orders.

    No `refresh` here: the integration spec documents no `?q=` on the portal's
    orders endpoint, so a live lookup would mean pulling every page just to
    filter locally. POST /api/admin/portal/sync is the way to freshen orders.

    `user_uuid7` is the parameter the license form actually uses — pick the
    customer in the users list, then list their orders.
    """
    query = db.query(models.PortalOrderCache)

    if user_uuid7:
        try:
            parsed = uuid.UUID(user_uuid7)
        except ValueError:
            raise HTTPException(
                status_code=422, detail="user_uuid7 is not a valid UUID"
            )
        query = query.filter(models.PortalOrderCache.user_uuid7 == parsed)

    if q:
        pattern = _like(q)
        query = query.filter(
            or_(
                models.PortalOrderCache.order_number.ilike(pattern),
                models.PortalOrderCache.plan_type.ilike(pattern),
                models.PortalOrderCache.status.ilike(pattern),
            )
        )

    # Newest order first: the one an admin is licensing is almost always the
    # most recent. NULLs last so records the portal sent without a timestamp
    # do not crowd out real ones.
    return (
        query.order_by(models.PortalOrderCache.portal_created_at.desc().nullslast())
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.post("/sync", response_model=schemas.PortalSyncResultResponse)
def trigger_sync():
    """Sync users and orders from the portal now, and report the outcome.

    Synchronous, unlike the license-event notifier: an admin who presses
    "sync" is waiting for the answer, and a result they cannot see would be
    useless. It is bounded by PORTAL_SYNC_TIMEOUT_SECONDS per page.

    409 if a sync is already running — refused rather than queued, so pressing
    the button twice cannot stack up runs that each re-fetch every page.

    200 with ok=false when the portal or the database failed mid-run. The two
    entities sync independently, so this is how a partial success is reported;
    the per-entity `error` fields say what broke. It is not a 5xx because the
    request itself succeeded and the body is the real answer.
    """
    try:
        result = syncer.sync(trigger="manual")
    except SyncAlreadyRunning:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A portal sync is already running; try again shortly.",
        )
    except PortalClientError as exc:
        # Only raised for a missing configuration, which is a server problem.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        )
    return result.as_dict()


@router.get("/sync/status", response_model=schemas.PortalSyncStatusResponse)
def sync_status():
    """Whether the sync is configured, whether one is running, and the last
    run's outcome — so the admin UI can show cache freshness and disable its
    sync button while one is in flight."""
    last = syncer.last_result
    return schemas.PortalSyncStatusResponse(
        enabled=syncer.enabled,
        running=syncer.is_running,
        interval_seconds=settings.PORTAL_SYNC_INTERVAL_SECONDS,
        last_result=last.as_dict() if last else None,
    )
