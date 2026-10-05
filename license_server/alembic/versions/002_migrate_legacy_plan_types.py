"""migrate_legacy_plan_types

The `plantype` Postgres enum (and the `licenses.plan_type` column data) still
carry the pre-restructure labels PILOT/BASIC1/BASIC2/BASIC3/ENTERPRISE.

SQLAlchemy's ``Enum(PlanType)`` persists the Python enum member's *name*
(not its ``.value``) unless ``values_callable`` is given, and this project
doesn't set it — so the native type's labels are the upper-cased member
names, not "pilot"/"basic1"/etc.

The "final of new license plan" change (see app/models.py) renamed the
PlanType members to PILOT/PLAN_100/PLAN_250/PLAN_500/UNLIMITED but never
migrated the already-created Postgres enum type or existing rows. Any
license row created before that change (e.g. plan_type='ENTERPRISE') now
fails to load with:

    LookupError: 'ENTERPRISE' is not among the defined enum values.

because 'ENTERPRISE' (and 'BASIC1'/'BASIC2'/'BASIC3') are no longer valid
PlanType members.

This migration remaps existing rows to their new equivalent plan (per the
1:1 mapping documented in LICENSE_PLANS_SUMMARY.md) and rebuilds the
`plantype` enum type to only contain the currently valid labels.

Revision ID: 002
Revises: 001
Create Date: 2026-08-11
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '002'
down_revision = '001'
branch_labels = None
depends_on = None

OLD_LABELS = ('PILOT', 'BASIC1', 'BASIC2', 'BASIC3', 'ENTERPRISE')
NEW_LABELS = ('PILOT', 'PLAN_100', 'PLAN_250', 'PLAN_500', 'UNLIMITED')

# Legacy plan_type value -> its replacement under the new catalog.
# PILOT is unchanged. BASIC1/2/3 map to their same-position audit/harden
# quota plan, ENTERPRISE (unlimited) maps to UNLIMITED.
LEGACY_TO_NEW = {
    'BASIC1': 'PLAN_100',
    'BASIC2': 'PLAN_250',
    'BASIC3': 'PLAN_500',
    'ENTERPRISE': 'UNLIMITED',
}


def _remap_enum(old_labels, new_labels, value_map):
    """Swap the `plantype` native enum's labels, remapping existing column
    data via the given value_map (identity for anything not in the map)."""
    bind = op.get_bind()

    new_type = sa.dialects.postgresql.ENUM(
        *new_labels, name='plantype_new', create_type=False,
    )
    new_type.create(bind, checkfirst=True)

    case_whens = " ".join(
        f"WHEN '{old}' THEN '{new}'" for old, new in value_map.items()
    )
    op.execute(
        f"""
        ALTER TABLE licenses
        ALTER COLUMN plan_type TYPE plantype_new
        USING (
            CASE plan_type::text
                {case_whens}
                ELSE plan_type::text
            END
        )::plantype_new
        """
    )

    op.execute("DROP TYPE plantype")
    op.execute("ALTER TYPE plantype_new RENAME TO plantype")


def upgrade() -> None:
    _remap_enum(OLD_LABELS, NEW_LABELS, LEGACY_TO_NEW)


def downgrade() -> None:
    reverse_map = {new: old for old, new in LEGACY_TO_NEW.items()}
    _remap_enum(NEW_LABELS, OLD_LABELS, reverse_map)
