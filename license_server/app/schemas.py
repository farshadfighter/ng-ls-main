from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from datetime import datetime
from typing import Any, Optional
from uuid import UUID
from . import plans


def _plan_code(value: str) -> str:
    """A SKU (or legacy plan name) as its canonical SKU; ValueError (a 422) when unknown."""
    return plans.resolve(value).code


class _PlanChoice(BaseModel):
    """plan_code + addon_packs, checked against the catalog (app/plans.py)."""

    # A SKU such as "NGC-BUS-250-1Y" (any case). The plan names of the old
    # catalog ("plan_250") are accepted too and mean the plan with that number.
    plan_code: str
    # 50-asset add-on packs (NGC-ADD-50-1Y). Not for the pilot or Unlimited.
    addon_packs: int = Field(default=0, ge=0, le=plans.MAX_ADDON_PACKS)

    @field_validator("plan_code")
    @classmethod
    def _known_plan(cls, value: str) -> str:
        return _plan_code(value)

    @model_validator(mode="after")
    def _addons_fit_the_plan(self):
        plans.check_addons(plans.resolve(self.plan_code), self.addon_packs)
        return self


class LicenseCreate(_PlanChoice):
    customer_name: str
    customer_email: EmailStr
    organization_name: str
    # An offline (air-gapped) license: runs on signed license files issued
    # from the admin UI instead of activation and heartbeats (app/offline.py).
    offline_allowed: bool = False

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
    # Random value the client picks per request; echoed inside the signed
    # token so a recorded answer cannot be replayed to the client later.
    nonce: Optional[str] = Field(default=None, max_length=128)

class LicenseValidate(BaseModel):
    license_key: str
    organization_token: str
    vm_fingerprint: str
    # Random value the client picks per request; echoed inside the signed
    # token so a recorded answer cannot be replayed to the client later.
    nonce: Optional[str] = Field(default=None, max_length=128)

class LicenseHeartbeat(LicenseValidate):
    # How many assets the licensed machine manages now. Recorded for the
    # admin (licenses.reported_assets); older clients do not send it.
    asset_count: Optional[int] = Field(default=None, ge=0, le=10_000_000)


class OfflineToggle(BaseModel):
    """Body of POST /api/admin/licenses/{key}/offline."""
    allowed: bool


class OfflineFileRequest(BaseModel):
    """Body of POST /api/admin/licenses/{key}/offline-file: the request file NGCorion made."""
    request: str = Field(min_length=1, max_length=8192)


class OfflineFileResponse(BaseModel):
    """A signed offline license file, to hand to the customer."""
    filename: str
    content: str
    valid_until: datetime
    license: "LicenseResponse"


class OfflineIssueResponse(BaseModel):
    issued_at: datetime
    valid_until: datetime
    vm_fingerprint: str
    reported_assets: Optional[int]
    issued_by: Optional[str]

    class Config:
        from_attributes = True


class PlanChange(BaseModel):
    """Body of POST /api/admin/licenses/{key}/plan. Either field may be left out."""

    plan_code: Optional[str] = None
    addon_packs: Optional[int] = Field(default=None, ge=0, le=plans.MAX_ADDON_PACKS)

    @field_validator("plan_code")
    @classmethod
    def _known_plan(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else _plan_code(value)

class LicenseResponse(BaseModel):
    id: int
    license_key: str
    organization_token: str
    customer_name: str
    customer_email: str
    organization_name: str

    plan_code: str
    plan_name: str
    # None = unlimited. plan base + 50 per add-on pack.
    max_assets: Optional[int]
    addon_packs: int
    # What the licensed machine last reported (heartbeat); None until it has.
    reported_assets: Optional[int]
    reported_assets_at: Optional[datetime]

    vm_fingerprint: Optional[str]
    is_active: bool
    is_pilot_mode: bool

    # Offline (air-gapped) license and its most recent license file.
    offline_allowed: bool = False
    offline_issued_at: Optional[datetime] = None
    offline_valid_until: Optional[datetime] = None

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
    plan_code: Optional[str] = None
    plan_name: Optional[str] = None
    is_pilot_mode: bool = False
    # {"max_assets": N or None, "addon_packs": k}
    limits: Optional[dict] = None
    organization_token: Optional[str] = None
    expires_at: Optional[datetime] = None
    # The same answer signed with LICENSE_SIGNING_PRIVATE_KEY (see
    # app/utils/license_token.py). NGCorion acts only on this token; the plain
    # fields above stay for older clients and for humans reading the response.
    signed_token: Optional[str] = None

class HeartbeatResponse(BaseModel):
    success: bool
    message: str
    # The same answer signed with LICENSE_SIGNING_PRIVATE_KEY (see
    # app/utils/license_token.py). NGCorion acts only on this token; the plain
    # fields above stay for older clients and for humans reading the response.
    signed_token: Optional[str] = None

class FingerprintResponse(BaseModel):
    """Response model for VM fingerprint endpoint"""
    fingerprint: str


# --- Internal portal integration (app/routers/internal_portal.py) -----------

class PortalLicenseCreate(BaseModel):
    """Body of POST /internal/portal/licenses.

    The plan is ``plan_code``, a SKU of the catalog (app/plans.py, e.g.
    "NGC-ENT-500-1Y"), plus ``addon_packs`` 50-asset add-ons. A portal still
    on the old vocabulary may send ``plan_type`` ("plan_250") instead, which
    means the plan with that number. An unknown plan, or add-ons the plan
    cannot take, is a 422 rather than a license created on a guess.
    """

    user_uuid7: UUID
    order_id: int
    order_number: str = Field(max_length=64)
    plan_code: Optional[str] = None
    plan_type: Optional[str] = None   # legacy name of plan_code
    addon_packs: int = Field(default=0, ge=0, le=plans.MAX_ADDON_PACKS)

    customer_name: str = Field(min_length=1, max_length=255)
    customer_email: EmailStr
    organization_name: str = Field(min_length=1, max_length=255)

    # Overrides the plan catalog's duration (app/plans.py) when given,
    # because the portal's order — not this server's price list — decides how
    # long the customer paid for. Omitted means "use the plan's own duration".
    # Bounded at 10 years: a typo of 36500 instead of 365 should be a 422, not
    # a license that outlives the company.
    duration_days: Optional[int] = Field(default=None, gt=0, le=3650)

    # Likewise explicit rather than derived from the plan (pilot or not), so the
    # portal can sell a trial on a non-pilot plan. None means "derive it".
    is_pilot_mode: Optional[bool] = None

    # Opaque portal payload, stored verbatim in licenses.portal_meta.
    meta: Optional[dict[str, Any]] = None

    @model_validator(mode="after")
    def _one_known_plan(self):
        if bool(self.plan_code) == bool(self.plan_type):
            raise ValueError("send exactly one of plan_code and plan_type")
        self.plan_code = _plan_code(self.plan_code or self.plan_type)
        self.plan_type = None
        plans.check_addons(plans.resolve(self.plan_code), self.addon_packs)
        return self


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
    of a license's current state, so it carries activation, asset limit and
    heartbeat freshness, which is what a portal-side status page needs.

    organization_token is NOT included. It is the shared secret the licensed
    app authenticates with, and the portal has no use for it.
    """

    license_id: int
    license_key: str
    plan_code: str
    plan_name: str
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

    # None means unlimited. reported_* is what the licensed machine last said
    # it manages.
    max_assets: Optional[int]
    addon_packs: int
    reported_assets: Optional[int]
    reported_assets_at: Optional[datetime]

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

    ``plan_type`` is the PORTAL's plan string, not a checked plan code — an
    order may name a plan this server does not sell, and the admin still needs
    to see it. Mapping it to a plan is the admin's decision when they fill in
    the license form.
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


# OfflineFileResponse names LicenseResponse before it is defined.
OfflineFileResponse.model_rebuild()
