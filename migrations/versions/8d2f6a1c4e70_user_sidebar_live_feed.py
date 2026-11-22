"""user: what the sidebar's live feed box shows (all posts, or followed people and communities)

Revision ID: 8d2f6a1c4e70
Revises: 5b7e0c3d9a41
Create Date: 2026-11-22 18:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '8d2f6a1c4e70'
down_revision = '5b7e0c3d9a41'
branch_labels = None
depends_on = None


def upgrade():
    # every post, as the home page's Live, All tab, stays the default (spec 2026-11-22-sidebar-live-feed)
    op.add_column('user', sa.Column('sidebar_live_feed', sa.String(length=16), nullable=False, server_default='all'))


def downgrade():
    op.drop_column('user', 'sidebar_live_feed')
