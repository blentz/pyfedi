"""file.thumbnail_written_by_us: the thumbnail file at thumbnail_path was written by this server (follow-up F3)

Existing rows get false: a path shaped like our thumbnailer's is no proof that we wrote it, so they are not reclaimed.

Revision ID: a9c4e6b2d8f1
Revises: f2a6c8d4b1e7
Create Date: 2026-10-26 18:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a9c4e6b2d8f1'
down_revision = 'f2a6c8d4b1e7'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('file', schema=None) as batch_op:
        batch_op.add_column(sa.Column('thumbnail_written_by_us', sa.Boolean(), server_default='false',
                                      nullable=False))


def downgrade():
    with op.batch_alter_table('file', schema=None) as batch_op:
        batch_op.drop_column('thumbnail_written_by_us')
