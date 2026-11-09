"""A person mutes another: user_mute

Revision ID: b8e4f0c3d5a2
Revises: a7d3e9b2c4f1
Create Date: 2026-11-09 02:00:00.000000

Owner ruling 2026-11-08: build mutes (plan W1.3, Mastodon's mute).
"""
from alembic import op
import sqlalchemy as sa

revision = 'b8e4f0c3d5a2'
down_revision = 'a7d3e9b2c4f1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'user_mute',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('muter_id', sa.Integer(), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False),
        sa.Column('muted_id', sa.Integer(), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False),
        sa.Column('notifications', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('expires_at', sa.DateTime()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('muter_id', 'muted_id', name='uq_user_mute_pair'),
    )
    op.create_index('ix_user_mute_muter_id', 'user_mute', ['muter_id'])
    op.create_index('ix_user_mute_muted_id', 'user_mute', ['muted_id'])


def downgrade():
    op.drop_table('user_mute')
