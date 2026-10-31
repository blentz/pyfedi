"""referral codes: the user's current code and the referral table

Revision ID: a7c2e5f9d3b1
Revises: f3c7a1e9b5d2
Create Date: 2026-10-31 12:00:00.000000

Referral-code registration (spec 2026-10-31): `user.referral_nonce`/`referral_code` hold the user's current code,
NULL until one is generated; `referral` records who referred whom.
"""
from alembic import op
import sqlalchemy as sa

revision = 'a7c2e5f9d3b1'
down_revision = 'f3c7a1e9b5d2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('referral_nonce', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('referral_code', sa.String(length=36), nullable=True))
        batch_op.create_index(batch_op.f('ix_user_referral_code'), ['referral_code'], unique=True)
    op.create_table(
        'referral',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('referrer_id', sa.Integer(), nullable=True),
        sa.Column('referred_id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['referrer_id'], ['user.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['referred_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('referred_id'),
    )
    with op.batch_alter_table('referral', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_referral_referrer_id'), ['referrer_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_referral_code'), ['code'], unique=False)


def downgrade():
    with op.batch_alter_table('referral', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_referral_code'))
        batch_op.drop_index(batch_op.f('ix_referral_referrer_id'))
    op.drop_table('referral')
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_user_referral_code'))
        batch_op.drop_column('referral_code')
        batch_op.drop_column('referral_nonce')
