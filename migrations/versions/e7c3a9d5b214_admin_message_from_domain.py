"""Admin messages: the domain a message is sent from

Revision ID: e7c3a9d5b214
Revises: b4d8e2f6a913
Create Date: 2026-11-26 12:00:00.000000

Spec docs/superpowers/specs/2026-11-24-admin-messaging-design.md MS6, D24.
"""
from alembic import op
import sqlalchemy as sa

revision = 'e7c3a9d5b214'
down_revision = 'b4d8e2f6a913'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('admin_message', sa.Column('from_domain_id', sa.Integer(),
                                             sa.ForeignKey('site_domain.id', ondelete='SET NULL'), nullable=True))


def downgrade():
    op.drop_column('admin_message', 'from_domain_id')
