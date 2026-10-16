"""community podcast_category (spec 2026-10-14 §3.3, §10 A17.5)

Revision ID: e5b8d2f0a4c7
Revises: d4a7c1e9b3f6
"""
from alembic import op
import sqlalchemy as sa

revision = 'e5b8d2f0a4c7'
down_revision = 'd4a7c1e9b3f6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('community', schema=None) as batch_op:
        batch_op.add_column(sa.Column('podcast_category', sa.String(length=64), nullable=True))


def downgrade():
    with op.batch_alter_table('community', schema=None) as batch_op:
        batch_op.drop_column('podcast_category')
