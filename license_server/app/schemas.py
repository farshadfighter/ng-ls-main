from pydantic import BaseModel, EmailStr, Field
from datetime import datetime
from typing import Any, Optional
from uuid import UUID
from .models import PlanType

class LicenseCreate(BaseModel):
    customer_name: str
    customer_email: EmailStr
    organization_name: str
    plan_type: PlanType

    # Optional link to the portal user and order this license was sold under,
    # for a license an admin creates by hand. The admin UI finds these through
    # GET /api/admin/portal/users and /orders (the phase-4 cache) and passes
    # them back here; without them the browse endpoints would let an admin
    # look a customer up but not record which one the license belongs to.
    #
    # The license still gets source='manual' — it was created by a person, not
    # by the portal's own POST /internal/portal/licenses — so it is not
    # covered by the partial unique index on order_id (which is scoped to
    # source='portal_auto'), and crud.get_license_by_order_id will not mistake
    # it for a portal-created one. Two manual licenses may therefore share an
    # order_id, which is intended: re-issuing a key for one order is a normal
    # admin action.
    user_uuid7: Optional[UUID] = None
    order_id: Optional[int] = None
    order_number: Optional[str] = Field(default=None, max_length=64)

class LicenseActivate(BaseModel):
    license_key: str
    vm_fingerprint: str

class LicenseValidate(BaseModel):
    license_key: str
    organization_token: str
    vm_fingerprint: str

class OperationConsume(BaseModel):
    license_key: str
    organization_token: str
    vm_fingerprint: str
    operation_type: str  # "audit" or "harden" — Asset Management is not license-gated
    count: int = 1

class LicenseResponse(BaseModel):
    id: int
    license_key: str
    organization_token: str
    customer_name: str
    customer_email: str
    organization_name: str
    plan_type: PlanType

    max_audits: Optional[int]
    max_hardens: Optional[int]

    used_audits: int
    used_hardens: int

    vm_fingerprint: Optional[str]
    is_active: bool
    is_pilot_mode: bool
    
    created_at: datetime
    activated_at: Optional[datetime]
    expires_at: datetime
    last_validated_at: Optional[datetime]
    last_heartbeat_at: Optional[datetime]
    
    class Config:
        from_attributes = True

class ValidationResponse(BaseModel):
    valid: bool
    message: str
    plan_type: Optional[PlanType] = None
    is_pilot_mode: bool = False
    limits: Optional[dict] = None
    usage: Optional[dict] = None
    organization_token: Optional[str] = None

class HeartbeatResponse(BaseModel):
    success: bool
    message: str
    should_downgrade: bool = False

class FingerprintResponse(BaseModel):
    """Response model for VM fingerprint endpoint"""
    fingerprint: str


# --- Internal portal integration (app/routers/internal_portal.py) -----------

class PortalLicenseCreate(BaseModel):
    """Body of POST /internal/portal/licenses.

    Note on plan_type: PlanType is a str-Enum whose *values* are lowercase
    ("pilot", "plan_100", "plan_250", "plan_500", "unlimited"), and pydantic
    coerces by value — so the portal must send "plan_100", not "PLAN_100".
    (The Postgres column stores the upper-case member NAME, because
    SQLAlchemy's Enum persists names unless values_callable is given and this
    project does not set it; see alembic/versions/002_migrate_legacy_plan_types.py.
    That is an internal detail and not what crosses the wire.) An unknown plan
    is a 422 rather than a license created on a guessed plan.
    """

    user_uuid7: UUID
    order_id: int
    order_number: str = Field(max_length=64)
    plan_type: PlanType

    customer_name: str = Field(min_length=1, max_length=255)
    customer_email: EmailStr
    organization_name: str = Field(min_length=1, max_length=255)

    # Overrides the plan catalog's duration (crud.get_plan_limits) when given,
    # because the portal's order — not this server's price list — decides how
    # long the customer paid for. Omitted means "use the plan's own duration".
    # Bounded at 10 years: a typo of 36500 instead of 365 should be a 422, not
    # a license that outlives the company.
    duration_days: Optional[int] = Field(default=None, gt=0, le=3650)

    # Likewise explicit rather than derived from plan_type == PILOT, so the
    # portal can sell a trial on a non-pilot plan. None means "derive it".
    is_pilot_mode: Optional[bool] = None

    # Opaque portal payload, stored verbatim in licenses.portal_meta.
    meta: Optional[dict[str, Any]] = None


class PortalLicenseResponse(BaseModel):
    """Response of POST /internal/portal/licenses.

    Exactly the four fields the integration spec defines — the portal stores
    these against its order. A replayed create (same order_id) returns the
    already-existing license's values here, not a new key.
    """

    license_id: int
    license_key: str
    expires_at: datetime
    is_active: bool


class PortalLicenseStatus(BaseModel):
    """Response of GET /internal/portal/licenses/{license_id}.

    Deliberately wider than PortalLicenseResponse: this is the portal's view
    of a license's current state, so it carries activation, quota usage and
    heartbeat freshness, which is what a portal-side status page needs.

    organization_token is NOT included. It is the shared secret the licensed
    app authenticates with, and the portal has no use for it.
    """

    license_id: int
    license_key: str
    plan_type: PlanType
    is_active: bool
    is_pilot_mode: bool

    customer_name: str
    customer_email: str
    organization_name: str

    user_uuid7: Optional[UUID]
    order_id: Optional[int]
    order_number: Optional[str]
    source: str

    created_at: datetime
    activated_at: Optional[datetime]
    expires_at: datetime
    last_validated_at: Optional[datetime]
    last_heartbeat_at: Optional[datetime]
    portal_synced_at: Optional[datetime]

    # None on either field means unlimited for that operation.
    max_audits: Optional[int]
    max_hardens: Optional[int]
    used_audits: int
    used_hardens: int

    # Whether the license has been bound to a machine yet, without exposing
    # the fingerprint hash itself.
    is_activated: bool

    class Config:
        from_attributes = True


# --- Portal user/order cache (app/routers/admin_portal.py) ------------------

class PortalUserCacheResponse(BaseModel):
    """A cached portal user, as the admin UI's license form sees it.

    ``raw`` (the untouched portal record) is intentionally NOT exposed. It is
    kept in the table so a wrong field mapping can be corrected without a
    re-sync, but it is internal: returning it would make the admin UI depend on
    the portal's private field names, which is exactly what the mapping exists
    to insulate it from.
    """

    user_uuid7: UUID
    email: Optional[str]
    full_name: Optional[str]
    organization_name: Optional[str]
    is_active: Optional[bool]
    portal_created_at: Optional[datetime]
    # When this row was last refreshed from the portal — the admin UI can warn
    # that the list is stale if a sync has not run recently.
    synced_at: Optional[datetime]

    class Config:
        from_attributes = True


class PortalOrderCacheResponse(BaseModel):
    """A cached portal order. ``raw`` is withheld, as for users.

    ``plan_type`` is the PORTAL's plan string, not this server's PlanType — an
    order may name a plan this server does not sell, and the admin still needs
    to see it. Mapping it to a PlanType is the admin's decision when they fill
    in the license form.
    """

    order_id: int
    order_number: Optional[str]
    user_uuid7: Optional[UUID]
    plan_type: Optional[str]
    status: Optional[str]
    duration_days: Optional[int]
    portal_created_at: Optional[datetime]
    synced_at: Optional[datetime]

    class Config:
        from_attributes = True


class PortalSyncEntityResult(BaseModel):
    """Per-entity outcome of a sync run."""

    fetched: int
    upserted: int
    # Records the portal returned that carried no usable primary key. A
    # non-zero count here usually means the field mapping in
    # app/core/portal_sync.py needs the portal's real key name added.
    skipped: int
    error: Optional[str] = None


class PortalSyncResultResponse(BaseModel):
    """Outcome of one sync run.

    ``ok`` is false when either entity failed. The two are synced
    independently, so a run can legitimately be a partial success — users
    cached, orders errored — and both halves are reported.
    """

    ok: bool
    trigger: str
    started_at: datetime
    finished_at: Optional[datetime]
    duration_seconds: float
    users: PortalSyncEntityResult
    orders: PortalSyncEntityResult


class PortalSyncStatusResponse(BaseModel):
    """Whether a sync is in flight, and how the last one went."""

    enabled: bool
    running: bool
    interval_seconds: int
    last_result: Optional[PortalSyncResultResponse] = None
