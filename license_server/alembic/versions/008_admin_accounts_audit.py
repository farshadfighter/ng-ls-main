"""admin_accounts_audit

Named admin accounts, server-side admin sessions and the audit trail
(app/models.py AdminUser, AdminSession, AuditEvent). The first account is made
at start-up from ADMIN_USERNAME / ADMIN_PASSWORD (app/accounts.py) and must
change its password at first login.

audit_events is append-only: a trigger refuses UPDATE and DELETE.

Kept in sync with app/models.py by hand (see the note in 003).

Revision ID: 008
Revises: 007
Create Date: 2026-10-11

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '008'
down_revision = '007'
branch_labels = None
depends_on = None

AUDIT_GUARD_SQL = """
CREATE OR REPLACE FUNCTION audit_events_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS audit_events_no_change ON audit_events;
CREATE TRIGGER audit_events_no_change BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_events_append_only();
"""


def upgrade() -> None:
    op.create_table(
        'admin_users',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('username', sa.String(length=64), nullable=False, unique=True),
        sa.Column('full_name', sa.String(length=255), nullable=True),
        sa.Column('email', sa.String(length=255), nullable=True),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('password_hash', sa.String(length=255), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('must_change_password', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('failed_logins', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_login_ip', sa.String(length=64), nullable=True),
        sa.Column('password_changed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('created_by', sa.String(length=64), nullable=True),
    )
    op.create_table(
        'admin_sessions',
        sa.Column('id', sa.String(length=64), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('admin_users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ip', sa.String(length=64), nullable=True),
        sa.Column('user_agent', sa.String(length=255), nullable=True),
    )
    op.create_index('idx_admin_session_user', 'admin_sessions', ['user_id'])
    op.create_table(
        'audit_events',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('actor', sa.String(length=64), nullable=False),
        sa.Column('actor_role', sa.String(length=16), nullable=True),
        sa.Column('ip', sa.String(length=64), nullable=True),
        sa.Column('action', sa.String(length=64), nullable=False),
        sa.Column('target', sa.String(length=255), nullable=True),
        sa.Column('outcome', sa.String(length=16), nullable=False),
        sa.Column('detail', postgresql.JSONB(), nullable=True),
    )
    op.create_index('idx_audit_at', 'audit_events', ['at'])
    op.create_index('idx_audit_actor', 'audit_events', ['actor'])
    op.create_index('idx_audit_action', 'audit_events', ['action'])
    op.execute(AUDIT_GUARD_SQL)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_events_no_change ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS audit_events_append_only()")
    op.drop_index('idx_audit_action', table_name='audit_events')
    op.drop_index('idx_audit_actor', table_name='audit_events')
    op.drop_index('idx_audit_at', table_name='audit_events')
    op.drop_table('audit_events')
    op.drop_index('idx_admin_session_user', table_name='admin_sessions')
    op.drop_table('admin_sessions')
    op.drop_table('admin_users')
