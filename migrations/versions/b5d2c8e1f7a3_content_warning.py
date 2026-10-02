"""Post and PostReply keep a peer's content warning; Post counts its gallery

Revision ID: b5d2c8e1f7a3
Revises: a7e3f1c2b9d4
Create Date: 2026-10-02 18:00:00.000000

Interop spec D10: Mastodon and Pixelfed send the warning as `summary`. Nullable,
no backfill: a row ingested before this has no warning to recover.
post.gallery_count is the number of post_file rows, so a listing's "N images"
badge does not count them per post; nothing wrote post_file before this.
"""
from alembic import op
import sqlalchemy as sa

revision = 'b5d2c8e1f7a3'
down_revision = 'a7e3f1c2b9d4'
branch_labels = None
depends_on = None


def upgrade():
    for table in ('post', 'post_reply'):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column('content_warning', sa.Text(), nullable=True))
    with op.batch_alter_table('post', schema=None) as batch_op:
        batch_op.add_column(sa.Column('gallery_count', sa.Integer(), server_default='0', nullable=False))


def downgrade():
    with op.batch_alter_table('post', schema=None) as batch_op:
        batch_op.drop_column('gallery_count')
    for table in ('post', 'post_reply'):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_column('content_warning')
