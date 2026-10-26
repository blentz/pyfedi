"""daily error counts, one row per day, unit, kind and level (onion unlinkability, project 5)

Revision ID: f2a6c8d4b1e7
Revises: e7b3d9a4c2f8
Create Date: 2026-10-26 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f2a6c8d4b1e7'
down_revision = 'e7b3d9a4c2f8'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('error_count',
                    sa.Column('day', sa.Date(), nullable=False),
                    sa.Column('unit', sa.String(length=32), nullable=False),
                    sa.Column('kind', sa.String(length=256), nullable=False),
                    sa.Column('level', sa.String(length=8), nullable=False),
                    sa.Column('count', sa.Integer(), nullable=False),
                    sa.PrimaryKeyConstraint('day', 'unit', 'kind', 'level'))


def downgrade():
    op.drop_table('error_count')
