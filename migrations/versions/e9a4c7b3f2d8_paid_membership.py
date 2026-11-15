"""Paid membership: when a domain began charging, and the tier a pending account chose

Revision ID: e9a4c7b3f2d8
Revises: d8f3b6a2e1c7
Create Date: 2026-11-15 06:00:00.000000

Spec 2026-11-15-paid-membership-design.md, plan phase 1 (M2, M3). Every existing domain starts with no start date,
which keeps today's rule: nobody is grandfathered by date until a domain is switched to paid from now on.
"""
from alembic import op
import sqlalchemy as sa

revision = 'e9a4c7b3f2d8'
down_revision = 'd8f3b6a2e1c7'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('site_domain', sa.Column('paid_since', sa.DateTime()))
    op.add_column('user', sa.Column('pending_offer_id', sa.Integer()))
    op.create_foreign_key('fk_user_pending_offer', 'user', 'offer', ['pending_offer_id'], ['id'], ondelete='SET NULL')


def downgrade():
    op.drop_constraint('fk_user_pending_offer', 'user', type_='foreignkey')
    op.drop_column('user', 'pending_offer_id')
    op.drop_column('site_domain', 'paid_since')
