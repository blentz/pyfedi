"""discovery_sync holds the communities the instance actor keeps synced

Revision ID: e8a4c1f9d2b6
Revises: d7f3a9c2e4b1
Create Date: 2026-10-05 12:00:00.000000

Interop D24 proactive sync. One row per synced community; built by `flask sync_discovery`.
"""
from alembic import op
import sqlalchemy as sa

revision = 'e8a4c1f9d2b6'
down_revision = 'd7f3a9c2e4b1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'discovery_sync',
        sa.Column('community_id', sa.Integer(), nullable=False),
        sa.Column('entry_id', sa.Integer(), nullable=True),
        sa.Column('follow_target', sa.String(length=1024), nullable=False),
        sa.Column('follow_uuid', sa.String(length=36), nullable=True),
        sa.Column('follow_state', sa.String(length=10), server_default='none', nullable=False),
        sa.Column('followed_at', sa.DateTime(), nullable=True),
        sa.Column('last_polled_at', sa.DateTime(), nullable=True),
        sa.Column('last_error', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['community_id'], ['community.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['entry_id'], ['discovery_entry.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('community_id'),
    )
    with op.batch_alter_table('discovery_sync', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_discovery_sync_follow_uuid'), ['follow_uuid'], unique=False)


def downgrade():
    with op.batch_alter_table('discovery_sync', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_discovery_sync_follow_uuid'))
    op.drop_table('discovery_sync')
