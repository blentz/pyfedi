"""Roles carried by subscription tiers

Revision ID: b5e9a3d7c1f4
Revises: a4d8f2c6e0b3
Create Date: 2026-11-16 23:00:00.000000

Plan docs/superpowers/plans/2026-11-16-tier-roles.md: offer_role says which roles a tier carries; role_grant.via_tier
marks a grant a tier made, which only the tier ends.
"""
from alembic import op
import sqlalchemy as sa

revision = 'b5e9a3d7c1f4'
down_revision = 'a4d8f2c6e0b3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('offer_role',
                    sa.Column('offer_id', sa.Integer(), sa.ForeignKey('offer.id', ondelete='CASCADE'), primary_key=True),
                    sa.Column('role_id', sa.Integer(), sa.ForeignKey('role.id', ondelete='CASCADE'), primary_key=True))
    op.create_index('ix_offer_role_role_id', 'offer_role', ['role_id'])
    op.add_column('role_grant', sa.Column('via_tier', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column('role_grant', 'via_tier')
    op.drop_index('ix_offer_role_role_id', table_name='offer_role')
    op.drop_table('offer_role')
