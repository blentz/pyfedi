"""server_view_member (joining a server view, spec 2026-10-13)

Revision ID: d4a8e2c6f1b9
Revises: c9f3a5b7d2e4
"""
import sqlalchemy as sa
from alembic import op

revision = 'd4a8e2c6f1b9'
down_revision = 'c9f3a5b7d2e4'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('server_view_member',
                    sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False),
                    sa.Column('instance_id', sa.Integer(), sa.ForeignKey('instance.id', ondelete='CASCADE'),
                              nullable=False),
                    sa.Column('created_at', sa.DateTime(), nullable=True),
                    sa.PrimaryKeyConstraint('user_id', 'instance_id'))
    op.create_index('ix_server_view_member_instance_id', 'server_view_member', ['instance_id'])


def downgrade():
    op.drop_index('ix_server_view_member_instance_id', table_name='server_view_member')
    op.drop_table('server_view_member')
