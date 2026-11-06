"""a prepaid period's reminder is sent once

Revision ID: e5b9c3a7d2f4
Revises: d4a8b2f6c1e9
Create Date: 2026-11-06 18:00:00.000000

Plan docs/superpowers/plans/2026-11-06-monetization-phase-4.md, T4.
"""
from alembic import op
import sqlalchemy as sa

revision = 'e5b9c3a7d2f4'
down_revision = 'd4a8b2f6c1e9'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('member_subscription', sa.Column('reminded_at', sa.DateTime()))


def downgrade():
    op.drop_column('member_subscription', 'reminded_at')
