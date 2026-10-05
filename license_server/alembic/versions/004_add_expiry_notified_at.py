"""add_expiry_notified_at

Phase 3 of the portal integration (outbound license events).

One column, ``licenses.expiry_notified_at``, which records that
``license.expired`` has been sent to the portal for that license. It is what
makes the expiry sweep in app/core/portal_events.py emit each expiry exactly
once — the sweep's query skips rows that already carry it.

It has to be a column rather than a Redis key or an in-memory set: the sweep
runs every few minutes forever, so a marker that does not survive a restart or
a Redis eviction would re-send `license.expired` for every already-expired
license on the next pass, and keep doing it.

Existing rows are backfilled rather than left NULL. A license that expired
before this code was deployed was never going to produce an event — nothing
observed expiry back then — and leaving it NULL would make the first sweep
after deploy emit a `license.expired` for every historically expired license at
once. The backfill stamps those with now(), which reads as "not notified, and
not going to be": the portal learns nothing new, which is correct, because it
was never told about them and their state is already visible through
GET /internal/portal/licenses/{license_id}.

Licenses that expire AFTER this migration runs are the ones the sweep is for,
and they are untouched by the backfill.

Kept in sync with app/models.py by hand — see the note in 003 on why that
matters here (db_bootstrap.py stamps `head` straight after
Base.metadata.create_all(), so the models file is the head schema for a fresh
install while this file is the head schema for an existing one).

Revision ID: 004
Revises: 003
Create Date: 2026-09-24

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '004'
down_revision = '003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'licenses',
        sa.Column('expiry_notified_at', sa.DateTime(timezone=True), nullable=True),
    )

    # Suppress a burst of events for licenses that expired before this feature
    # existed. Only already-expired rows: anything still live must stay NULL so
    # the sweep picks it up when its time comes.
    op.execute(
        """
        UPDATE licenses
        SET expiry_notified_at = now()
        WHERE expires_at < now()
        """
    )


def downgrade() -> None:
    # Losing this column means the next sweep after a re-upgrade would treat
    # every expired license as un-notified. The backfill in upgrade() is what
    # makes that safe, so the round trip is not harmful, merely quiet.
    op.drop_column('licenses', 'expiry_notified_at')
