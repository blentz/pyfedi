"""post_vote and post_reply_vote federated_like (C5 liked counts only votes whose Like was sent)

Revision ID: c9f3a5b7d2e4
Revises: b8e2d4f6a1c3
"""
import sqlalchemy as sa
from alembic import op

revision = 'c9f3a5b7d2e4'
down_revision = 'b8e2d4f6a1c3'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('post_vote', sa.Column('federated_like', sa.Boolean(), nullable=True))
    op.add_column('post_reply_vote', sa.Column('federated_like', sa.Boolean(), nullable=True))


def downgrade():
    op.drop_column('post_reply_vote', 'federated_like')
    op.drop_column('post_vote', 'federated_like')
