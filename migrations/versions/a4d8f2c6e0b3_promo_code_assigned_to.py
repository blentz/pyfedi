"""A gift or discount code made for one member to hand out

Revision ID: a4d8f2c6e0b3
Revises: f3c7d9e1a5b2
Create Date: 2026-11-16 22:00:00.000000

Owner 2026-11-16: Admin -> Payments -> Codes can make a code for a member, listed on their referral page. Not a
referral: the code stays an ordinary gift or discount code.
"""
from alembic import op
import sqlalchemy as sa

revision = 'a4d8f2c6e0b3'
down_revision = 'f3c7d9e1a5b2'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('promo_code', sa.Column('assigned_to', sa.Integer()))
    op.create_foreign_key('fk_promo_code_assigned_to', 'promo_code', 'user', ['assigned_to'], ['id'],
                          ondelete='SET NULL')
    op.create_index('ix_promo_code_assigned_to', 'promo_code', ['assigned_to'])


def downgrade():
    op.drop_index('ix_promo_code_assigned_to', table_name='promo_code')
    op.drop_constraint('fk_promo_code_assigned_to', 'promo_code', type_='foreignkey')
    op.drop_column('promo_code', 'assigned_to')
