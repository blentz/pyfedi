"""Discount codes: a use reserved at checkout with the price expected, and a code held by a pending account

Revision ID: a5c9e3d7b1f4
Revises: f4b8d2c6a9e3
Create Date: 2026-11-15 15:00:00.000000

Spec 2026-11-15-paid-membership-design.md D3, plan phase 3 D1, D4.
"""
from alembic import op
import sqlalchemy as sa

revision = 'a5c9e3d7b1f4'
down_revision = 'f4b8d2c6a9e3'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('promo_code_use', sa.Column('offer_id', sa.Integer()))
    op.add_column('promo_code_use', sa.Column('expected_minor', sa.BigInteger()))
    op.add_column('promo_code_use', sa.Column('payment_id', sa.Integer()))
    op.create_foreign_key('fk_promo_code_use_offer', 'promo_code_use', 'offer', ['offer_id'], ['id'], ondelete='SET NULL')
    op.create_foreign_key('fk_promo_code_use_payment', 'promo_code_use', 'payment', ['payment_id'], ['id'],
                          ondelete='SET NULL')
    op.add_column('user', sa.Column('pending_promo_code_id', sa.Integer()))
    op.create_foreign_key('fk_user_pending_promo_code', 'user', 'promo_code', ['pending_promo_code_id'], ['id'],
                          ondelete='SET NULL')


def downgrade():
    op.drop_constraint('fk_user_pending_promo_code', 'user', type_='foreignkey')
    op.drop_column('user', 'pending_promo_code_id')
    op.drop_constraint('fk_promo_code_use_payment', 'promo_code_use', type_='foreignkey')
    op.drop_constraint('fk_promo_code_use_offer', 'promo_code_use', type_='foreignkey')
    op.drop_column('promo_code_use', 'payment_id')
    op.drop_column('promo_code_use', 'expected_minor')
    op.drop_column('promo_code_use', 'offer_id')
