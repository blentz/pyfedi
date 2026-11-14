"""Connected accounts: a member's accounts on other services, and each post's copies there

Revision ID: d4f8a2c6e1b9
Revises: c3e9a5d1f7b4
Create Date: 2026-11-13 21:00:00.000000

Spec docs/superpowers/specs/2026-11-13-connected-accounts-design.md §4. Two new tables; nothing existing changes.
"""
from alembic import op
import sqlalchemy as sa

revision = 'd4f8a2c6e1b9'
down_revision = 'c3e9a5d1f7b4'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'social_connection',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('service', sa.String(length=20), nullable=False),
        sa.Column('variant', sa.String(length=20), nullable=True),
        sa.Column('remote_id', sa.String(length=255), nullable=False),
        sa.Column('remote_name', sa.String(length=255), nullable=False),
        sa.Column('sealed_tokens', sa.Text(), nullable=False),
        sa.Column('default_on', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('state', sa.String(length=10), server_default='ok', nullable=False),
        sa.Column('last_error', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'service', 'remote_id', name='uq_social_connection_account'),
    )
    op.create_index('ix_social_connection_user_id', 'social_connection', ['user_id'])
    op.create_table(
        'social_crosspost',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('post_id', sa.Integer(), nullable=False),
        sa.Column('connection_id', sa.Integer(), nullable=True),
        sa.Column('service', sa.String(length=20), nullable=False),
        sa.Column('variant', sa.String(length=20), nullable=True),
        sa.Column('state', sa.String(length=10), nullable=False),
        sa.Column('remote_id', sa.String(length=512), nullable=True),
        sa.Column('remote_url', sa.String(length=1024), nullable=True),
        sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
        sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('last_error', sa.String(length=255), nullable=True),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
        sa.Column('edited_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['post_id'], ['post.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['connection_id'], ['social_connection.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('post_id', 'connection_id', name='uq_social_crosspost_post_connection'),
    )
    op.create_index('ix_social_crosspost_post_id', 'social_crosspost', ['post_id'])
    op.create_index('ix_social_crosspost_connection_id', 'social_crosspost', ['connection_id'])
    op.create_index('ix_social_crosspost_state', 'social_crosspost', ['state'])
    op.create_index('ix_social_crosspost_next_attempt_at', 'social_crosspost', ['next_attempt_at'])


def downgrade():
    op.drop_table('social_crosspost')
    op.drop_table('social_connection')
