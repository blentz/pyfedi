"""A CMS page may be for members only

Revision ID: c3e9a5d1f7b4
Revises: f2b6d8a4c9e1
Create Date: 2026-11-13 12:00:00.000000

Every page that is already there stays public: the column defaults to false.
"""
from alembic import op
import sqlalchemy as sa

revision = 'c3e9a5d1f7b4'
down_revision = 'f2b6d8a4c9e1'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('cms_page', schema=None) as batch_op:
        batch_op.add_column(sa.Column('members_only', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade():
    with op.batch_alter_table('cms_page', schema=None) as batch_op:
        batch_op.drop_column('members_only')
