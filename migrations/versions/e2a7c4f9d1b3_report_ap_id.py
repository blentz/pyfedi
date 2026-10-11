"""report ap_id (interop C1 item 3: an Undo of a Flag finds its report)

Revision ID: e2a7c4f9d1b3
Revises: d9f3b7a15c2e
"""
import sqlalchemy as sa
from alembic import op

revision = 'e2a7c4f9d1b3'
down_revision = 'd9f3b7a15c2e'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('report', sa.Column('ap_id', sa.String(length=255), nullable=True))
    op.create_index('ix_report_ap_id', 'report', ['ap_id'], unique=False)


def downgrade():
    op.drop_index('ix_report_ap_id', table_name='report')
    op.drop_column('report', 'ap_id')
