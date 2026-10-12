"""post and post_reply quote_ap_id (C2 quote posts on input)

Revision ID: a7c3e9f1b5d2
Revises: f4b8d2a6c1e7
"""
import sqlalchemy as sa
from alembic import op

revision = 'a7c3e9f1b5d2'
down_revision = 'f4b8d2a6c1e7'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('post', sa.Column('quote_ap_id', sa.String(length=255), nullable=True))
    op.add_column('post_reply', sa.Column('quote_ap_id', sa.String(length=255), nullable=True))


def downgrade():
    op.drop_column('post_reply', 'quote_ap_id')
    op.drop_column('post', 'quote_ap_id')
