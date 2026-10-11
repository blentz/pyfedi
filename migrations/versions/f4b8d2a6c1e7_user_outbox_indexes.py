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
    op.create_index('ix_post_user_posted', 'post', ['user_id', sa.literal_column('posted_at DESC')], unique=False,
                    postgresql_where=sa.text('deleted = false'))
    op.create_index('ix_post_reply_user_posted', 'post_reply', ['user_id', sa.literal_column('posted_at DESC')],
                    unique=False, postgresql_where=sa.text('deleted = false'))


def downgrade():
    op.drop_index('ix_post_reply_user_posted', table_name='post_reply')
    op.drop_index('ix_post_user_posted', table_name='post')
