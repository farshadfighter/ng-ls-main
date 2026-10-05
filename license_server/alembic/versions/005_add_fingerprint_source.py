"""add_fingerprint_source

Signed licensing: records how a license's machine binding was made.

``licenses.fingerprint_source``:

* ``client`` - the fingerprint was computed by NGCorion on the licensed
  machine (releases with signed licensing; they send a nonce). One machine per
  license is enforced against it.
* NULL - bound before this column existed, or by an NGCorion release before
  signed licensing. Those releases asked GET /api/fingerprint, which returns
  the LICENSE SERVER's own fingerprint (and a different one whenever its
  container is recreated, because the hostname is part of it), so the value
  identifies no customer machine. crud.activate_license lets such a binding
  move once to a client-computed fingerprint.

No backfill: every existing binding is of the second kind.

Kept in sync with app/models.py by hand (see the note in 003).

Revision ID: 005
Revises: 004
Create Date: 2026-10-05

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '005'
down_revision = '004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('licenses', sa.Column('fingerprint_source', sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column('licenses', 'fingerprint_source')
