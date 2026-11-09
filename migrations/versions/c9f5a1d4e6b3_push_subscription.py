"""Web Push subscriptions: push_subscription

Revision ID: c9f5a1d4e6b3
Revises: b8e4f0c3d5a2
Create Date: 2026-11-09 04:00:00.000000

Owner ruling 2026-11-09: push notifications, clearnet only (app.mastodon.push).
"""
from alembic import op
import sqlalchemy as sa

revision = 'c9f5a1d4e6b3'
down_revision = 'b8e4f0c3d5a2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'push_subscription',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('token_id', sa.Integer(), sa.ForeignKey('oauth_token.id', ondelete='CASCADE'), nullable=False,
                  unique=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False),
        sa.Column('endpoint', sa.String(2048), nullable=False),
        sa.Column('p256dh', sa.String(128), nullable=False),
        sa.Column('auth', sa.String(64), nullable=False),
        sa.Column('alerts', sa.JSON(), nullable=False),
        sa.Column('policy', sa.String(10), nullable=False, server_default='all'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_push_subscription_user_id', 'push_subscription', ['user_id'])


def downgrade():
    op.drop_table('push_subscription')
