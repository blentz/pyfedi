"""gpodder sync's devices and subscriptions: gpodder_device, gpodder_subscription

Revision ID: b7e2c9d4f1a3
Revises: a4d9e1c7b3f2
Create Date: 2026-11-12 09:00:00.000000

Plan W4b, spec docs/superpowers/specs/2026-11-12-gpodder-sync-design.md §4 (ruling G2 of 2026-11-12).
"""
from alembic import op
import sqlalchemy as sa

revision = 'b7e2c9d4f1a3'
down_revision = 'a4d9e1c7b3f2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'gpodder_device',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False),
        sa.Column('device_id', sa.String(64), nullable=False),
        sa.Column('caption', sa.String(100), nullable=False),
        sa.Column('type', sa.String(16), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('user_id', 'device_id', name='uq_gpodder_device_user_device'),
    )
    op.create_table(
        'gpodder_subscription',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False),
        sa.Column('url', sa.String(2000), nullable=False),
        sa.Column('subscribed', sa.Boolean(), nullable=False),
        sa.Column('changed_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('user_id', 'url', name='uq_gpodder_subscription_user_url'),
    )
    op.create_index('ix_gpodder_subscription_user_changed', 'gpodder_subscription', ['user_id', 'changed_at'])


def downgrade():
    op.drop_index('ix_gpodder_subscription_user_changed', table_name='gpodder_subscription')
    op.drop_table('gpodder_subscription')
    op.drop_table('gpodder_device')
