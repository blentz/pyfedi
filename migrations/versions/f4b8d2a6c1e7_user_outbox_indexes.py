"""user outbox indexes (interop C1 item 7): a user's posts and comments, newest first

Revision ID: f4b8d2a6c1e7
Revises: e2a7c4f9d1b3
"""
import sqlalchemy as sa
from alembic import op

revision = 'f4b8d2a6c1e7'
down_revision = 'e2a7c4f9d1b3'
branch_labels = None
depends_on = None


def upgrade():
    # `post` and `post_reply` are the biggest tables: a plain CREATE INDEX would block every write to them for the
    # whole build, so both are built concurrently, which cannot run inside a transaction. NULLS LAST is the order the
    # outbox pages in; a DESC index is NULLS FIRST unless told otherwise, and could not give it.
    with op.get_context().autocommit_block():
        op.create_index('ix_post_user_posted', 'post', ['user_id', sa.text('posted_at DESC NULLS LAST')], unique=False,
                        postgresql_where=sa.text('deleted = false'), postgresql_concurrently=True)
        op.create_index('ix_post_reply_user_posted', 'post_reply', ['user_id', sa.text('posted_at DESC NULLS LAST')],
                        unique=False, postgresql_where=sa.text('deleted = false'), postgresql_concurrently=True)


def downgrade():
    with op.get_context().autocommit_block():
        op.drop_index('ix_post_reply_user_posted', table_name='post_reply', postgresql_concurrently=True)
        op.drop_index('ix_post_user_posted', table_name='post', postgresql_concurrently=True)
