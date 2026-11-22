"""user: whether a new microblog post is private (stays on this server) by default

Revision ID: 2daa520cdaa7
Revises: 9c1e42dfcd53
Create Date: 2026-11-22 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '2daa520cdaa7'
down_revision = '9c1e42dfcd53'
branch_labels = None
depends_on = None


def upgrade():
    # public and federated stays the default (spec 2026-11-22-private-microblogs)
    op.add_column('user', sa.Column('microblogs_private', sa.Boolean(), nullable=False, server_default='false'))


def downgrade():
    op.drop_column('user', 'microblogs_private')
