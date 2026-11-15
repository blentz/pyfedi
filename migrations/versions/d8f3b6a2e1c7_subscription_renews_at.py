"""A subscription that renews by itself keeps its next billing date

Revision ID: d8f3b6a2e1c7
Revises: c3e9a5d1f7b4
Create Date: 2026-11-15 04:00:00.000000

Stripe's and PayPal's paid-through date, for the admin's users list. Prepaid subscriptions keep theirs in
current_period_end. Existing rows start empty and fill in at their next payment.
"""
from alembic import op
import sqlalchemy as sa

revision = 'd8f3b6a2e1c7'
down_revision = 'c3e9a5d1f7b4'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('member_subscription', sa.Column('renews_at', sa.DateTime()))


def downgrade():
    op.drop_column('member_subscription', 'renews_at')
