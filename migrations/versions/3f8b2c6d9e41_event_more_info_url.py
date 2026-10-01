"""Give an event a 'More info' link

Revision ID: 3f8b2c6d9e41
Revises: 9c3e5a1d7b20
Create Date: 2026-10-01 12:00:00.000000

CreateEventForm has carried a `more_info_url` field, validated, with nowhere
to put it (R223). The column it is stored in, nullable like the event's
other links.
"""
from alembic import op
import sqlalchemy as sa

revision = '3f8b2c6d9e41'
down_revision = '9c3e5a1d7b20'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('event', schema=None) as batch_op:
        batch_op.add_column(sa.Column('more_info_url', sa.String(length=1024), nullable=True))


def downgrade():
    with op.batch_alter_table('event', schema=None) as batch_op:
        batch_op.drop_column('more_info_url')
