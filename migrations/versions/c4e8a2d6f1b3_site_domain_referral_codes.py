"""site domain: whether a referral code opens a closed domain's registration

Revision ID: c4e8a2d6f1b3
Revises: f1c7a3e9d2b4
Create Date: 2026-11-19 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'c4e8a2d6f1b3'
down_revision = 'f1c7a3e9d2b4'
branch_labels = None
depends_on = None


def upgrade():
    # true keeps what every existing domain did before (referral codes spec RC11)
    op.add_column('site_domain', sa.Column('referral_codes', sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade():
    op.drop_column('site_domain', 'referral_codes')
