"""Admin messaging: built-in email overrides and admin messages

Revision ID: b4d8e2f6a913
Revises: a95a82e17b01
Create Date: 2026-11-24 12:00:00.000000

Spec docs/superpowers/specs/2026-11-24-admin-messaging-design.md D3, D10-D17.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'b4d8e2f6a913'
down_revision = 'a95a82e17b01'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('email_template',
                    sa.Column('key', sa.String(length=50), primary_key=True),
                    sa.Column('subject', sa.String(length=255), nullable=False),
                    sa.Column('body', sa.Text(), nullable=False),
                    sa.Column('updated_at', sa.DateTime(), nullable=False),
                    sa.Column('updated_by', sa.Integer(), sa.ForeignKey('user.id', ondelete='SET NULL')))
    op.create_table('admin_message',
                    sa.Column('id', sa.Integer(), primary_key=True),
                    sa.Column('subject', sa.String(length=255), nullable=False),
                    sa.Column('body', sa.Text(), nullable=False),
                    sa.Column('audience', sa.String(length=10), nullable=False),
                    sa.Column('domain_ids', postgresql.ARRAY(sa.Integer())),
                    sa.Column('role_ids', postgresql.ARRAY(sa.Integer())),
                    sa.Column('subscription', sa.String(length=10)),
                    sa.Column('offer_ids', postgresql.ARRAY(sa.Integer())),
                    sa.Column('service_notice', sa.Boolean(), nullable=False),
                    sa.Column('status', sa.String(length=10), nullable=False),
                    sa.Column('scheduled_for', sa.DateTime()),
                    sa.Column('created_by', sa.Integer(), sa.ForeignKey('user.id', ondelete='SET NULL')),
                    sa.Column('created_at', sa.DateTime(), nullable=False),
                    sa.Column('updated_at', sa.DateTime(), nullable=False),
                    sa.Column('heartbeat_at', sa.DateTime()),
                    sa.Column('sent_at', sa.DateTime()),
                    sa.Column('sent_count', sa.Integer(), nullable=False),
                    sa.Column('failed_count', sa.Integer(), nullable=False))
    op.create_index('ix_admin_message_scheduled_for', 'admin_message', ['scheduled_for'])
    op.create_table('admin_message_pick',
                    sa.Column('message_id', sa.Integer(), sa.ForeignKey('admin_message.id', ondelete='CASCADE'),
                              primary_key=True),
                    sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id', ondelete='CASCADE'), primary_key=True))
    op.create_table('admin_message_delivery',
                    sa.Column('message_id', sa.Integer(), sa.ForeignKey('admin_message.id', ondelete='CASCADE'),
                              primary_key=True),
                    sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id', ondelete='CASCADE'), primary_key=True),
                    sa.Column('claimed_at', sa.DateTime(), nullable=False),
                    sa.Column('sent_at', sa.DateTime()),
                    sa.Column('error', sa.String(length=255)))


def downgrade():
    op.drop_table('admin_message_delivery')
    op.drop_table('admin_message_pick')
    op.drop_index('ix_admin_message_scheduled_for', table_name='admin_message')
    op.drop_table('admin_message')
    op.drop_table('email_template')
