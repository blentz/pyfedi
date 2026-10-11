"""site require_signed_fetch (security track S6: secure mode)

Revision ID: d9f3b7a15c2e
Revises: e5c1d8a2b7f9
"""
import sqlalchemy as sa
from alembic import op

revision = 'd9f3b7a15c2e'
down_revision = 'e5c1d8a2b7f9'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('site', sa.Column('require_signed_fetch', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column('site', 'require_signed_fetch')
