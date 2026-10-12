"""post and post_reply language_variants (C3 multi-language content)

Revision ID: b8e2d4f6a1c3
Revises: a7c3e9f1b5d2
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = 'b8e2d4f6a1c3'
down_revision = 'a7c3e9f1b5d2'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('post', sa.Column('language_variants', postgresql.JSONB(), nullable=True))
    op.add_column('post_reply', sa.Column('language_variants', postgresql.JSONB(), nullable=True))


def downgrade():
    op.drop_column('post_reply', 'language_variants')
    op.drop_column('post', 'language_variants')
