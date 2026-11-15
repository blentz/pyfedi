"""Bitcoin's price fixed daily at midnight UTC, and each BTCPay invoice made with what it asked

Revision ID: c8e2f6a0d4b5
Revises: b7d1e5f9c3a2
Create Date: 2026-11-15 23:30:00.000000

Plan 2026-11-15-btc-daily-price.md, B1, B3.
"""
from alembic import op
import sqlalchemy as sa

revision = 'c8e2f6a0d4b5'
down_revision = 'b7d1e5f9c3a2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('btc_daily_rate',
                    sa.Column('day', sa.Date(), primary_key=True),
                    sa.Column('currency', sa.String(3), primary_key=True),
                    sa.Column('rate', sa.Numeric(24, 8), nullable=False),
                    sa.Column('fixed_at', sa.DateTime(), nullable=False))
    op.create_table('btcpay_invoice',
                    sa.Column('invoice_id', sa.String(255), primary_key=True),
                    sa.Column('offer_id', sa.Integer(), sa.ForeignKey('offer.id', ondelete='SET NULL')),
                    sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id', ondelete='SET NULL')),
                    sa.Column('expected_minor', sa.BigInteger(), nullable=False),
                    sa.Column('currency', sa.String(3), nullable=False),
                    sa.Column('sats', sa.BigInteger(), nullable=False),
                    sa.Column('rate_day', sa.Date(), nullable=False),
                    sa.Column('created_at', sa.DateTime(), nullable=False))
    op.create_index('ix_btcpay_invoice_offer_id', 'btcpay_invoice', ['offer_id'])
    op.create_index('ix_btcpay_invoice_user_id', 'btcpay_invoice', ['user_id'])


def downgrade():
    op.drop_table('btcpay_invoice')
    op.drop_table('btc_daily_rate')
