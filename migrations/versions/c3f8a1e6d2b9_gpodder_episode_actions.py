"""gpodder sync's episode actions: gpodder_episode_action

Revision ID: c3f8a1e6d2b9
Revises: b7e2c9d4f1a3
Create Date: 2026-11-12 11:00:00.000000

Plan W4c, spec docs/superpowers/specs/2026-11-12-gpodder-sync-design.md §5 (ruling G3 of 2026-11-12).
"""
from alembic import op
import sqlalchemy as sa

revision = 'c3f8a1e6d2b9'
down_revision = 'b7e2c9d4f1a3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'gpodder_episode_action',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False),
        sa.Column('podcast', sa.String(2000), nullable=False),
        sa.Column('episode', sa.String(2000), nullable=False),
        sa.Column('episode_digest', sa.String(64), nullable=False),
        sa.Column('guid', sa.String(2000)),
        sa.Column('action', sa.String(8), nullable=False),
        sa.Column('device', sa.String(64)),
        sa.Column('timestamp', sa.DateTime(), nullable=False),
        sa.Column('started', sa.Integer()),
        sa.Column('position', sa.Integer()),
        sa.Column('total', sa.Integer()),
        sa.Column('received_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('user_id', 'episode_digest', 'action', name='uq_gpodder_episode_action'),
    )
    op.create_index('ix_gpodder_episode_action_user_received', 'gpodder_episode_action', ['user_id', 'received_at'])


def downgrade():
    op.drop_index('ix_gpodder_episode_action_user_received', table_name='gpodder_episode_action')
    op.drop_table('gpodder_episode_action')
