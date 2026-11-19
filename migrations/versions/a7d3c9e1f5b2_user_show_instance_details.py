"""user: whether the web view shows FediVerse instance details

Revision ID: a7d3c9e1f5b2
Revises: c4e8a2d6f1b3
Create Date: 2026-11-19 18:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'a7d3c9e1f5b2'
down_revision = 'c4e8a2d6f1b3'
branch_labels = None
depends_on = None


def upgrade():
    # false: plain community names are the default (spec 2026-11-19-plain-community-names)
    op.add_column('user', sa.Column('show_instance_details', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column('user', 'show_instance_details')
