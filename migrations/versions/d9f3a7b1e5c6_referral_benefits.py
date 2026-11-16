"""A member's referral code gives one benefit: the default, a gift, a discount code or nothing

Revision ID: d9f3a7b1e5c6
Revises: c8e2f6a0d4b5
Create Date: 2026-11-16 06:00:00.000000

Plan 2026-11-16-referral-benefits.md, R1, R3. A referral code that had a gift of its own keeps it as its own.
"""
from alembic import op
import sqlalchemy as sa

revision = 'd9f3a7b1e5c6'
down_revision = 'c8e2f6a0d4b5'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('promo_code', sa.Column('benefit', sa.String(10)))
    op.add_column('promo_code', sa.Column('discount_code_id', sa.Integer()))
    op.create_foreign_key('fk_promo_code_discount_code', 'promo_code', 'promo_code', ['discount_code_id'], ['id'],
                          ondelete='SET NULL')
    op.execute("UPDATE promo_code SET benefit = 'gift' WHERE owner_id IS NOT NULL AND offer_id IS NOT NULL")


def downgrade():
    op.drop_constraint('fk_promo_code_discount_code', 'promo_code', type_='foreignkey')
    op.drop_column('promo_code', 'discount_code_id')
    op.drop_column('promo_code', 'benefit')
