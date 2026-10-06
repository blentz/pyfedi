"""discovery_exclusion holds actor URLs the proactive sync must never re-add

Revision ID: f1b7d3a8c5e2
Revises: e8a4c1f9d2b6
Create Date: 2026-10-06 12:00:00.000000

Interop D24. An admin who deletes a synced community must not see it re-created and re-followed at the next reconcile.
"""
from alembic import op
import sqlalchemy as sa

revision = 'f1b7d3a8c5e2'
down_revision = 'e8a4c1f9d2b6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'discovery_exclusion',
        sa.Column('actor_url', sa.String(length=1024), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('actor_url'),
    )


def downgrade():
    op.drop_table('discovery_exclusion')
