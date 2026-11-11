"""Each member's own state in a conversation: conversation_member.state, hidden, muted

Revision ID: e2b7c3f9a1d5
Revises: d1a6b2e7f4c8
Create Date: 2026-11-11 04:00:00.000000

Plan W1.4b, spec docs/superpowers/specs/2026-11-10-pixelfed-app-design.md §6 (rulings 2026-11-11): message requests,
hidden and muted conversations. Every existing membership is active, shown and unmuted, as it behaved before.
"""
from alembic import op
import sqlalchemy as sa

revision = 'e2b7c3f9a1d5'
down_revision = 'd1a6b2e7f4c8'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('conversation_member', sa.Column('state', sa.String(10), nullable=False, server_default='active'))
    op.add_column('conversation_member', sa.Column('hidden', sa.Boolean(), nullable=False, server_default='false'))
    op.add_column('conversation_member', sa.Column('muted', sa.Boolean(), nullable=False, server_default='false'))


def downgrade():
    op.drop_column('conversation_member', 'muted')
    op.drop_column('conversation_member', 'hidden')
    op.drop_column('conversation_member', 'state')
