"""instance signature format

Revision ID: c4a8e2f61d3b
Revises: b7e1c2d9f4a6
"""
import sqlalchemy as sa
from alembic import op

revision = 'c4a8e2f61d3b'
down_revision = 'b7e1c2d9f4a6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('instance', sa.Column('signature_format', sa.String(length=16), nullable=False,
                                        server_default='cavage'))


def downgrade():
    op.drop_column('instance', 'signature_format')
