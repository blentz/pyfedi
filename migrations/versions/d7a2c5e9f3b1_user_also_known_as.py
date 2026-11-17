"""A user's account aliases (ActivityPub alsoKnownAs), for moving an account here from another server

Revision ID: d7a2c5e9f3b1
Revises: c6f1b4e8a2d5
Create Date: 2026-11-18 02:30:00.000000

Mastodon moves an account only to one whose actor lists the old account in `alsoKnownAs`. Nullable, no default:
an account without aliases has none.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'd7a2c5e9f3b1'
down_revision = 'c6f1b4e8a2d5'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('user', sa.Column('also_known_as', postgresql.ARRAY(sa.String(length=512)), nullable=True))


def downgrade():
    op.drop_column('user', 'also_known_as')
