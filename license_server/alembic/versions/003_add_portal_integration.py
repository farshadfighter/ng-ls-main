"""add_portal_integration

Phase 1 of the internal-portal integration (http://172.16.120.80): schema only.

Two independent pieces of schema:

1. Five columns on ``licenses`` that record where a license came from, plus a
   ``portal_meta`` JSONB for the portal's opaque ``meta`` object. Every one is
   nullable — an admin-created license leaves them all empty — except
   ``source``, which gets a NOT NULL default of 'manual' so existing rows are
   correctly labelled as hand-created without a separate backfill.

   The partial unique index on ``order_id`` is the load-bearing one: it is what
   makes a retried ``POST /internal/portal/licenses`` idempotent rather than
   minting a second license key for one order. It is partial (portal rows only)
   because every manual row has ``order_id`` NULL.

2. ``portal_users_cache`` and ``portal_orders_cache``, which are created empty
   and stay unused until phase 4 (the users/orders sync that backs the admin
   UI's license form). They are here rather than in a later revision so phase 4
   is code-only.

Neither cache table takes a foreign key to ``licenses`` or to the other: they
are caches of records the portal owns, they can be truncated and re-synced at
will, and an order can legitimately sync before the user it belongs to.

Kept in sync with app/models.py by hand. That is not optional here:
db_bootstrap.py stamps ``head`` immediately after
``Base.metadata.create_all()`` on an empty database, so the models file IS the
head schema for a fresh install while this file is the head schema for an
existing one. If the two drift, a fresh database and a migrated database end up
with different schemas and nothing reports it.

Revision ID: 003
Revises: 002
Create Date: 2026-09-24

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = '003'
down_revision = '002'
branch_labels = None
depends_on = None

# models.LicenseSource.MANUAL — spelled out rather than imported so the
# migration keeps describing this exact historical step even if the enum in
# app/models.py gains or renames members later.
DEFAULT_SOURCE = 'manual'
PORTAL_SOURCE = 'portal_auto'


def upgrade() -> None:
    # --- licenses: portal provenance columns --------------------------------
    op.add_column('licenses', sa.Column('user_uuid7', sa.Uuid(), nullable=True))
    op.add_column('licenses', sa.Column('order_id', sa.BigInteger(), nullable=True))
    op.add_column('licenses', sa.Column('order_number', sa.String(length=64), nullable=True))
    # NOT NULL with a server_default, so the ALTER backfills every existing row
    # to 'manual' in one pass — which is accurate: every license that exists
    # before this migration was created by an admin.
    op.add_column(
        'licenses',
        sa.Column(
            'source',
            sa.String(length=32),
            nullable=False,
            server_default=DEFAULT_SOURCE,
        ),
    )
    op.add_column(
        'licenses',
        sa.Column('portal_synced_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column('licenses', sa.Column('portal_meta', postgresql.JSONB(), nullable=True))

    op.create_index('idx_license_user_uuid7', 'licenses', ['user_uuid7'])
    # The idempotency guarantee for POST /internal/portal/licenses.
    op.create_index(
        'uq_licenses_portal_order_id',
        'licenses',
        ['order_id'],
        unique=True,
        postgresql_where=sa.text(f"source = '{PORTAL_SOURCE}'"),
    )

    # --- portal caches (unused until phase 4) -------------------------------
    op.create_table(
        'portal_users_cache',
        # The portal's own UUIDv7 is the primary key: these rows have no
        # identity of their own, and keying on it makes the sync a plain upsert.
        sa.Column('user_uuid7', sa.Uuid(), primary_key=True),
        sa.Column('email', sa.String(length=255), nullable=True),
        sa.Column('full_name', sa.String(length=255), nullable=True),
        sa.Column('organization_name', sa.String(length=255), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        # The portal's timestamp, kept distinct from synced_at (ours) so a
        # stale cache is diagnosable.
        sa.Column('portal_created_at', sa.DateTime(timezone=True), nullable=True),
        # Full portal payload, so a field the portal adds later is already
        # stored and can be promoted to a column without re-syncing.
        sa.Column('raw', postgresql.JSONB(), nullable=True),
        sa.Column('synced_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('idx_portal_user_email', 'portal_users_cache', ['email'])
    op.create_index('idx_portal_user_full_name', 'portal_users_cache', ['full_name'])

    op.create_table(
        'portal_orders_cache',
        # autoincrement=False: the portal assigns order ids, we never mint one.
        sa.Column('order_id', sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column('order_number', sa.String(length=64), nullable=True),
        sa.Column('user_uuid7', sa.Uuid(), nullable=True),
        # String, not the plantype enum: the portal's plan vocabulary is its
        # own, and an order naming a plan this server does not sell must still
        # cache and be visible to an admin instead of failing the sync.
        sa.Column('plan_type', sa.String(length=32), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=True),
        sa.Column('duration_days', sa.Integer(), nullable=True),
        sa.Column('portal_created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('raw', postgresql.JSONB(), nullable=True),
        sa.Column('synced_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('idx_portal_order_number', 'portal_orders_cache', ['order_number'])
    op.create_index('idx_portal_order_user', 'portal_orders_cache', ['user_uuid7'])
    op.create_index('idx_portal_order_status', 'portal_orders_cache', ['status'])


def downgrade() -> None:
    # Dropping the caches loses only cached copies of portal-owned records;
    # re-running the phase-4 sync rebuilds them.
    op.drop_index('idx_portal_order_status', table_name='portal_orders_cache')
    op.drop_index('idx_portal_order_user', table_name='portal_orders_cache')
    op.drop_index('idx_portal_order_number', table_name='portal_orders_cache')
    op.drop_table('portal_orders_cache')

    op.drop_index('idx_portal_user_full_name', table_name='portal_users_cache')
    op.drop_index('idx_portal_user_email', table_name='portal_users_cache')
    op.drop_table('portal_users_cache')

    # Dropping these columns DOES lose data: which portal order and user a
    # license belongs to is recorded nowhere else. Downgrading past 003 with
    # portal-created licenses in the table orphans them from the portal.
    op.drop_index('uq_licenses_portal_order_id', table_name='licenses')
    op.drop_index('idx_license_user_uuid7', table_name='licenses')
    op.drop_column('licenses', 'portal_meta')
    op.drop_column('licenses', 'portal_synced_at')
    op.drop_column('licenses', 'source')
    op.drop_column('licenses', 'order_number')
    op.drop_column('licenses', 'order_id')
    op.drop_column('licenses', 'user_uuid7')
