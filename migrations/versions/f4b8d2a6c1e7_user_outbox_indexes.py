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


def drop_invalid_leftover(name, table):
    """An interrupted CREATE INDEX CONCURRENTLY (a deploy timeout, a cancelled lock wait) leaves an INVALID index of
    its name behind, which the planner never uses and which made the rerun fail with 'already exists'. Drop it so the
    rerun builds the index again. Offline (--sql) there is no database to ask."""
    if op.get_context().as_sql:
        return
    invalid = op.get_bind().execute(sa.text('SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid '
                                            'WHERE c.relname = :name AND NOT i.indisvalid'), {'name': name}).scalar()
    if invalid:
        op.drop_index(name, table_name=table, postgresql_concurrently=True)


def upgrade():
    # `post` and `post_reply` are the biggest tables: a plain CREATE INDEX would block every write to them for the
    # whole build, so both are built concurrently, which cannot run inside a transaction. NULLS LAST is the order the
    # outbox pages in; a DESC index is NULLS FIRST unless told otherwise, and could not give it.
    # A rerun after an interruption keeps an index already built (if_not_exists) and rebuilds an INVALID one.
    with op.get_context().autocommit_block():
        drop_invalid_leftover('ix_post_user_posted', 'post')
        op.create_index('ix_post_user_posted', 'post', ['user_id', sa.text('posted_at DESC NULLS LAST')], unique=False,
                        postgresql_where=sa.text('deleted = false'), postgresql_concurrently=True, if_not_exists=True)
        drop_invalid_leftover('ix_post_reply_user_posted', 'post_reply')
        op.create_index('ix_post_reply_user_posted', 'post_reply', ['user_id', sa.text('posted_at DESC NULLS LAST')],
                        unique=False, postgresql_where=sa.text('deleted = false'), postgresql_concurrently=True,
                        if_not_exists=True)


def downgrade():
    with op.get_context().autocommit_block():
        op.drop_index('ix_post_reply_user_posted', table_name='post_reply', postgresql_concurrently=True)
        op.drop_index('ix_post_user_posted', table_name='post', postgresql_concurrently=True)
