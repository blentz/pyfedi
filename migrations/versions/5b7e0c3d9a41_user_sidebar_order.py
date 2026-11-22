"""user: the sidebars' boxes as the user arranged them (NULL: the domain's order)

Revision ID: 5b7e0c3d9a41
Revises: 2daa520cdaa7
Create Date: 2026-11-22 15:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '5b7e0c3d9a41'
down_revision = '2daa520cdaa7'
branch_labels = None
depends_on = None


def upgrade():
    # spec 2026-11-22-user-sidebar-order; app/sidebar.py reads the values
    op.add_column('user', sa.Column('home_sidebar', sa.Text(), nullable=True))
    op.add_column('user', sa.Column('community_sidebar', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('user', 'community_sidebar')
    op.drop_column('user', 'home_sidebar')
