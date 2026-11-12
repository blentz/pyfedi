"""the order of the sidebars' boxes and which are hidden, for the site and each domain

Revision ID: e5a8c2f7b4d1
Revises: c3f8a1e6d2b9
Create Date: 2026-11-12 12:00:00.000000

`home_sidebar` is the home page sidebar's boxes, `community_sidebar` the community pages', each a comma-separated list
of box names in order, a hidden box's name after a '-' (app/sidebar.py). '' or NULL is the default order, every box
shown; on a domain's row it is the site's.
"""
from alembic import op
import sqlalchemy as sa


revision = 'e5a8c2f7b4d1'
down_revision = 'c3f8a1e6d2b9'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('site') as batch_op:
        batch_op.add_column(sa.Column('home_sidebar', sa.Text(), nullable=True, server_default=''))
        batch_op.add_column(sa.Column('community_sidebar', sa.Text(), nullable=True, server_default=''))
    with op.batch_alter_table('domain_branding') as batch_op:
        batch_op.add_column(sa.Column('home_sidebar', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('community_sidebar', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('domain_branding') as batch_op:
        batch_op.drop_column('community_sidebar')
        batch_op.drop_column('home_sidebar')
    with op.batch_alter_table('site') as batch_op:
        batch_op.drop_column('community_sidebar')
        batch_op.drop_column('home_sidebar')
