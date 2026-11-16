"""When an account's IP address was recorded, so that it can be cleared a year on

Revision ID: f3c7d9e1a5b2
Revises: e2b6c8d4f0a1
Create Date: 2026-11-16 20:00:00.000000

Privacy policy, 2026-11-16: the IP address an account last used is kept for 12 months. An address stored before this
is dated by when the account was last seen, the latest it can have been recorded, so none is cleared too soon.
"""
from alembic import op
import sqlalchemy as sa

revision = 'f3c7d9e1a5b2'
down_revision = 'e2b6c8d4f0a1'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('user', sa.Column('ip_address_at', sa.DateTime()))
    op.execute("UPDATE \"user\" SET ip_address_at = COALESCE(last_seen, created) "
               "WHERE ip_address IS NOT NULL AND ip_address <> ''")


def downgrade():
    op.drop_column('user', 'ip_address_at')
