"""Promo codes: gifts of a free period now, discounts later, and each use

Revision ID: f4b8d2c6a9e3
Revises: e9a4c7b3f2d8
Create Date: 2026-11-15 12:00:00.000000

Spec 2026-11-15-paid-membership-design.md D3, plan phase 2 G1.
"""
from alembic import op
import sqlalchemy as sa

revision = 'f4b8d2c6a9e3'
down_revision = 'e9a4c7b3f2d8'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'promo_code',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('code', sa.String(64), nullable=False, unique=True),
        sa.Column('kind', sa.String(10), nullable=False),
        sa.Column('offer_id', sa.Integer(), sa.ForeignKey('offer.id', ondelete='SET NULL'), index=True),
        sa.Column('days', sa.Integer()),
        sa.Column('percent', sa.Integer()),
        sa.Column('amount_minor', sa.BigInteger()),
        sa.Column('max_uses', sa.Integer()),
        sa.Column('expires_at', sa.DateTime()),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('owner_id', sa.Integer(), sa.ForeignKey('user.id', ondelete='CASCADE'), unique=True),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('user.id', ondelete='SET NULL')),
        sa.Column('created_at', sa.DateTime()),
    )
    op.create_table(
        'promo_code_use',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('promo_code_id', sa.Integer(), sa.ForeignKey('promo_code.id', ondelete='CASCADE'), nullable=False,
                  index=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('subscription_id', sa.Integer(), sa.ForeignKey('member_subscription.id', ondelete='SET NULL')),
        sa.Column('used_at', sa.DateTime()),
        sa.UniqueConstraint('promo_code_id', 'user_id', name='uq_promo_code_use_once'),
    )


def downgrade():
    op.drop_table('promo_code_use')
    op.drop_table('promo_code')
