"""asset_based_plans

Licensing v3: a license is sold per number of managed assets, by SKU
(app/plans.py), with 50-asset add-on packs. Audits and hardening runs are no
longer counted.

On ``licenses``:

* added ``plan_code`` (the SKU), ``max_assets`` (NULL = unlimited),
  ``addon_packs``, and ``reported_assets`` / ``reported_assets_at`` (what the
  licensed machine last reported in its heartbeat);
* existing licenses move to the plan with the same number, now counted in
  assets: pilot -> NGC-PILOT-5-1M (5), plan_100 -> NGC-PRO-100-1Y,
  plan_250 -> NGC-BUS-250-1Y, plan_500 -> NGC-ENT-500-1Y,
  unlimited -> NGC-ENT-ULT-1Y;
* dropped ``plan_type`` (and its native enum type ``plantype``),
  ``max_audits``, ``max_hardens``, ``used_audits`` and ``used_hardens``.

Licenses that the old server downgraded to the pilot plan for being offline
48 hours were overwritten for good; their original plan is not recorded
anywhere. They show as a pilot license with a one-year term, and this
migration logs their ids: set the right plan for each in the admin UI
(License -> Change plan).

Kept in sync with app/models.py by hand (see the note in 003).

Revision ID: 006
Revises: 005
Create Date: 2026-10-10

"""
import logging

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '006'
down_revision = '005'
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

# plantype label (the upper-case member name the old column stored) ->
# (plan code, max assets). Copied, not imported: a migration must keep
# meaning the same thing when app/plans.py changes later.
LEGACY = {
    'PILOT': ('NGC-PILOT-5-1M', 5),
    'PLAN_100': ('NGC-PRO-100-1Y', 100),
    'PLAN_250': ('NGC-BUS-250-1Y', 250),
    'PLAN_500': ('NGC-ENT-500-1Y', 500),
    'UNLIMITED': ('NGC-ENT-ULT-1Y', None),
}

# For downgrade: plan code -> (plantype label, max audits/hardens).
BACK = {
    'NGC-PILOT-5-1M': ('PILOT', 2),
    'NGC-ESS-25-1Y': ('PLAN_100', 100),
    'NGC-PRO-100-1Y': ('PLAN_100', 100),
    'NGC-BUS-250-1Y': ('PLAN_250', 250),
    'NGC-ENT-500-1Y': ('PLAN_500', 500),
    'NGC-ENT-1000-1Y': ('PLAN_500', 500),
    'NGC-ENT-2500-1Y': ('PLAN_500', 500),
    'NGC-ENT-ULT-1Y': ('UNLIMITED', None),
}


def _sql(value):
    return 'NULL' if value is None else (f"'{value}'" if isinstance(value, str) else str(value))


def upgrade() -> None:
    op.add_column('licenses', sa.Column('plan_code', sa.String(length=32), nullable=True))
    op.add_column('licenses', sa.Column('max_assets', sa.Integer(), nullable=True))
    op.add_column('licenses', sa.Column('addon_packs', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('licenses', sa.Column('reported_assets', sa.Integer(), nullable=True))
    op.add_column('licenses', sa.Column('reported_assets_at', sa.DateTime(timezone=True), nullable=True))

    code = " ".join(f"WHEN '{old}' THEN {_sql(new)}" for old, (new, _) in LEGACY.items())
    limit = " ".join(f"WHEN '{old}' THEN {_sql(n)}" for old, (_, n) in LEGACY.items())
    # Built only from the LEGACY constant above; no outside input.
    op.execute(f"""
        UPDATE licenses SET
            plan_code = CASE plan_type::text {code} ELSE 'NGC-PILOT-5-1M' END,
            max_assets = CASE plan_type::text {limit} ELSE 5 END
    """)  # noqa: S608

    # Pilot rows with a paid plan's term: most likely downgraded by the old
    # server for being offline. Their real plan is lost; an admin has to set it.
    suspects = op.get_bind().execute(sa.text("""
        SELECT id, license_key FROM licenses
        WHERE plan_type::text = 'PILOT' AND expires_at - created_at > INTERVAL '60 days'
        ORDER BY id
    """)).fetchall()
    for row in suspects:
        logger.warning(
            "license id=%s (%s) is a pilot license with a paid term: probably downgraded "
            "for being offline; set its plan in the admin UI", row.id, row.license_key,
        )

    op.alter_column('licenses', 'plan_code', nullable=False)
    for column in ('plan_type', 'max_audits', 'max_hardens', 'used_audits', 'used_hardens'):
        op.drop_column('licenses', column)
    op.execute("DROP TYPE IF EXISTS plantype")


def downgrade() -> None:
    plantype = sa.dialects.postgresql.ENUM(
        'PILOT', 'PLAN_100', 'PLAN_250', 'PLAN_500', 'UNLIMITED', name='plantype',
    )
    plantype.create(op.get_bind(), checkfirst=True)
    op.add_column('licenses', sa.Column('plan_type', plantype, nullable=True))
    for column in ('max_audits', 'max_hardens'):
        op.add_column('licenses', sa.Column(column, sa.Integer(), nullable=True))
    for column in ('used_audits', 'used_hardens'):
        op.add_column('licenses', sa.Column(column, sa.Integer(), nullable=True, server_default='0'))

    label = " ".join(f"WHEN '{c}' THEN '{old}'" for c, (old, _) in BACK.items())
    quota = " ".join(f"WHEN '{c}' THEN {_sql(n)}" for c, (_, n) in BACK.items())
    # Built only from the BACK constant above; no outside input.
    op.execute(f"""
        UPDATE licenses SET
            plan_type = (CASE plan_code {label} ELSE 'PILOT' END)::plantype,
            max_audits = CASE plan_code {quota} ELSE 2 END,
            max_hardens = CASE plan_code {quota} ELSE 2 END
    """)  # noqa: S608
    op.alter_column('licenses', 'plan_type', nullable=False)

    for column in ('reported_assets_at', 'reported_assets', 'addon_packs', 'max_assets', 'plan_code'):
        op.drop_column('licenses', column)
