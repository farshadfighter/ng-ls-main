"""add_performance_indexes

Revision ID: 001
Revises: 
Create Date: 2025-04-23

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create performance indexes
    op.create_index('idx_license_key_active', 'licenses', ['license_key', 'is_active'])
    op.create_index('idx_org_token_active', 'licenses', ['organization_token', 'is_active'])
    op.create_index('idx_expires_at', 'licenses', ['expires_at'])
    op.create_index('idx_last_heartbeat', 'licenses', ['last_heartbeat_at'])


def downgrade() -> None:
    # Drop indexes
    op.drop_index('idx_last_heartbeat', table_name='licenses')
    op.drop_index('idx_expires_at', table_name='licenses')
    op.drop_index('idx_org_token_active', table_name='licenses')
    op.drop_index('idx_license_key_active', table_name='licenses')
