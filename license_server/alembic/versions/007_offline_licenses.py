"""offline_licenses

Offline (air-gapped) licenses (app/offline.py): an admin marks a license
offline and issues signed one-year license files for it from the admin UI.

* licenses.offline_allowed (NOT NULL, default false), offline_issued_at and
  offline_valid_until (the most recent file);
* offline_license_issues: every file issued (when, by whom, for which
  machine, valid until when, reported asset count).

Kept in sync with app/models.py by hand (see the note in 003).

Revision ID: 007
Revises: 006
Create Date: 2026-10-10

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '007'
down_revision = '006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('licenses', sa.Column('offline_allowed', sa.Boolean(), nullable=False,
                                        server_default=sa.text('false')))
    op.add_column('licenses', sa.Column('offline_issued_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('licenses', sa.Column('offline_valid_until', sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        'offline_license_issues',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('license_id', sa.Integer(), sa.ForeignKey('licenses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('issued_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('valid_until', sa.DateTime(timezone=True), nullable=False),
        sa.Column('vm_fingerprint', sa.String(), nullable=False),
        sa.Column('reported_assets', sa.Integer(), nullable=True),
        sa.Column('issued_by', sa.String(length=255), nullable=True),
    )
    op.create_index('idx_offline_issue_license', 'offline_license_issues', ['license_id'])


def downgrade() -> None:
    op.drop_index('idx_offline_issue_license', table_name='offline_license_issues')
    op.drop_table('offline_license_issues')
    for column in ('offline_valid_until', 'offline_issued_at', 'offline_allowed'):
        op.drop_column('licenses', column)
