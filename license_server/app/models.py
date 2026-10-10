from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
from .database import Base
from . import plans
import enum

class LicenseSource(str, enum.Enum):
    """Where a license record came from.

    Deliberately NOT a SQLAlchemy ``Enum`` on the column — ``licenses.source``
    is a plain VARCHAR(32). The old ``plan_type`` column was a native Postgres
    enum, and adding a label to one needs a full type rebuild (see
    alembic/versions/002_migrate_legacy_plan_types.py for what that cost).
    A new source (say a reseller API) should be one line here, not a migration.
    """

    MANUAL = "manual"          # created by an admin through /api/admin/licenses
    PORTAL_AUTO = "portal_auto"  # created by the portal via /internal/portal/licenses


class License(Base):
    __tablename__ = "licenses"
    
    __table_args__ = (
        Index('idx_license_key_active', 'license_key', 'is_active'),
        Index('idx_org_token_active', 'organization_token', 'is_active'),
        Index('idx_expires_at', 'expires_at'),
        Index('idx_last_heartbeat', 'last_heartbeat_at'),
        Index('idx_license_user_uuid7', 'user_uuid7'),
        # One license per portal order. A partial index (portal rows only) so
        # the many source='manual' rows, which all have order_id NULL, are not
        # dragged into a uniqueness check they can never satisfy meaningfully.
        # This is what makes a retried POST /internal/portal/licenses
        # idempotent instead of minting a second key for the same order —
        # crud.create_license_from_portal relies on it to close the race
        # between two concurrent calls carrying the same order_id.
        #
        # Kept byte-for-byte in sync with
        # alembic/versions/003_add_portal_integration.py: db_bootstrap.py
        # stamps `head` straight after Base.metadata.create_all() on an empty
        # database, so whatever create_all builds from this file IS the head
        # schema as far as Alembic is concerned. A definition that drifts from
        # the migration leaves fresh and migrated databases silently different.
        Index(
            'uq_licenses_portal_order_id',
            'order_id',
            unique=True,
            postgresql_where=text("source = 'portal_auto'"),
        ),
    )
    
    id = Column(Integer, primary_key=True, index=True)
    license_key = Column(String, unique=True, index=True, nullable=False)
    organization_token = Column(String, unique=True, index=True, nullable=False)
    
    customer_name = Column(String, nullable=False)
    customer_email = Column(String, nullable=False)
    organization_name = Column(String, nullable=False)
    
    # The plan's SKU (app/plans.py), e.g. "NGC-BUS-250-1Y". A plain string,
    # not a native enum, so a new plan is a catalog entry and not a migration.
    plan_code = Column(String(32), nullable=False)

    # The license's entitlement: how many assets NGCorion may manage. Stored,
    # not derived from the catalog at read time, so a license keeps what it
    # was sold with: plan base + 50 per add-on pack (app/plans.py).
    # NULL = unlimited.
    max_assets = Column(Integer, nullable=True)
    addon_packs = Column(Integer, nullable=False, default=0, server_default="0")

    # What the licensed machine last reported in its heartbeat: how many
    # assets it manages, and when. Information for the admin (a customer
    # outgrowing the plan); the limit itself is enforced by NGCorion from the
    # signed max_assets.
    reported_assets = Column(Integer, nullable=True)
    reported_assets_at = Column(DateTime(timezone=True), nullable=True)

    # VM fingerprint برای قفل شدن روی یک ماشین
    vm_fingerprint = Column(String, nullable=True)
    # How the binding was made: "client" = computed by NGCorion on the
    # licensed machine (signed-licensing releases); NULL = a legacy binding to
    # the license server's own fingerprint, which may move once. Migration 005.
    fingerprint_source = Column(String(16), nullable=True)
    
    is_active = Column(Boolean, default=True)
    # A trial license. Set for the pilot plan, or by the portal for a trial
    # sold on another plan. Not set because a machine was offline: NGCorion
    # falls back to the pilot entitlement on its own, and only until it
    # reaches this server again.
    is_pilot_mode = Column(Boolean, default=False)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    activated_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    last_validated_at = Column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at = Column(DateTime(timezone=True), nullable=True)

    # --- Portal integration (see app/routers/internal_portal.py) ------------
    # All nullable: every column here is empty on a license an admin created
    # by hand, which is still the normal path.
    user_uuid7 = Column(Uuid, nullable=True)      # portal's user id (UUIDv7)
    order_id = Column(BigInteger, nullable=True)  # portal's numeric order id
    order_number = Column(String(64), nullable=True)  # human-facing order ref

    # LicenseSource value. server_default as well as default so rows written
    # by anything other than the ORM (psql, a migration) also get 'manual'.
    source = Column(
        String(32),
        nullable=False,
        default=LicenseSource.MANUAL.value,
        server_default=LicenseSource.MANUAL.value,
    )

    # Last time this row and the portal agreed about it. Set at creation for a
    # portal-created license; phase 3 (event notification) updates it on every
    # successful POST to the portal's /internal/ls/license-events.
    portal_synced_at = Column(DateTime(timezone=True), nullable=True)

    # Set when license.expired has been sent to the portal for this license,
    # by the sweep in app/core/portal_events.py. It is what makes that sweep
    # emit each expiry exactly once: the query skips rows that already carry
    # it, and being a column rather than an in-memory or Redis marker, that
    # holds across restarts and Redis eviction.
    #
    # Distinct from portal_synced_at, which moves on every delivered event.
    # This one is a one-way latch for a single terminal event.
    expiry_notified_at = Column(DateTime(timezone=True), nullable=True)

    # The portal's opaque `meta` object, stored verbatim. The integration spec
    # passes it but does not define its shape, so it is kept whole rather than
    # flattened into columns that would need a migration each time the portal
    # adds a key.
    portal_meta = Column(JSONB, nullable=True)

    @property
    def plan_name(self) -> str:
        """The plan's product name ("NGCorion Business"); the code itself for a plan the
        catalog no longer has."""
        plan = plans.PLANS.get(self.plan_code or "")
        return plan.name if plan else (self.plan_code or "")


class PortalUserCache(Base):
    """Local copy of a portal user, for the admin UI's license form.

    Populated by the phase-4 sync from GET {PORTAL_BASE_URL}/internal/ls/users
    and unused until then. It exists in migration 003 so phase 4 needs no
    second migration.

    A cache, never a source of truth: the portal owns these records, nothing
    here writes back, and the table can be truncated and re-synced at any time.
    Hence no foreign key from licenses.user_uuid7 to this table — a license
    must be creatable for a user the cache has not caught up with yet.

    Only the fields the admin UI searches on get real columns; the whole portal
    payload is kept in ``raw`` so a field the portal adds later is already
    stored and can be promoted to a column without a re-sync.
    """

    __tablename__ = "portal_users_cache"

    __table_args__ = (
        Index('idx_portal_user_email', 'email'),
        Index('idx_portal_user_full_name', 'full_name'),
    )

    user_uuid7 = Column(Uuid, primary_key=True)  # the portal's own id, not ours
    email = Column(String(255), nullable=True)
    full_name = Column(String(255), nullable=True)
    organization_name = Column(String(255), nullable=True)
    is_active = Column(Boolean, nullable=True)

    # The portal's created_at, not ours — named apart from synced_at so the two
    # timelines are never confused when debugging a stale cache.
    portal_created_at = Column(DateTime(timezone=True), nullable=True)
    raw = Column(JSONB, nullable=True)
    synced_at = Column(DateTime(timezone=True), server_default=func.now())


class PortalOrderCache(Base):
    """Local copy of a portal order. Same contract as PortalUserCache.

    Fed by GET {PORTAL_BASE_URL}/internal/ls/orders in phase 4.

    ``plan_type`` is the portal's own plan string, not checked against the
    catalog (app/plans.py): an order naming a plan this server does not offer
    must still be cacheable and visible to an admin rather than failing the sync.
    """

    __tablename__ = "portal_orders_cache"

    __table_args__ = (
        Index('idx_portal_order_number', 'order_number'),
        Index('idx_portal_order_user', 'user_uuid7'),
        Index('idx_portal_order_status', 'status'),
    )

    order_id = Column(BigInteger, primary_key=True, autoincrement=False)
    order_number = Column(String(64), nullable=True)
    user_uuid7 = Column(Uuid, nullable=True)  # no FK: orders may sync first
    plan_type = Column(String(32), nullable=True)
    status = Column(String(32), nullable=True)
    duration_days = Column(Integer, nullable=True)

    portal_created_at = Column(DateTime(timezone=True), nullable=True)
    raw = Column(JSONB, nullable=True)
    synced_at = Column(DateTime(timezone=True), server_default=func.now())
