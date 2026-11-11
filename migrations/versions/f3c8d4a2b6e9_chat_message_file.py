"""A message's pictures, in order: chat_message_file

Revision ID: f3c8d4a2b6e9
Revises: e2b7c3f9a1d5
Create Date: 2026-11-11 06:00:00.000000

Plan W1.4b2, spec docs/superpowers/specs/2026-11-10-pixelfed-app-design.md §6 (ruling 2026-11-11): media in messages.
"""
from alembic import op
import sqlalchemy as sa

revision = 'f3c8d4a2b6e9'
down_revision = 'e2b7c3f9a1d5'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'chat_message_file',
        sa.Column('chat_message_id', sa.Integer(), sa.ForeignKey('chat_message.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('file_id', sa.Integer(), sa.ForeignKey('file.id', ondelete='CASCADE'), nullable=False),
        sa.Column('weight', sa.Integer(), nullable=False, server_default='0'),
        sa.PrimaryKeyConstraint('chat_message_id', 'file_id'),
    )
    op.create_index('ix_chat_message_file_file_id', 'chat_message_file', ['file_id'])


def downgrade():
    op.drop_index('ix_chat_message_file_file_id', table_name='chat_message_file')
    op.drop_table('chat_message_file')
