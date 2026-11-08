"""OAuth 2 apps, authorization codes and tokens, for client apps (Mastodon, Pixelfed and others)

Revision ID: f6c2a8e4b1d7
Revises: e5b9c3a7d2f4
Create Date: 2026-11-08 12:00:00.000000

Plan docs/superpowers/plans/2026-11-07-api-completeness.md, W0.2.
"""
from alembic import op
import sqlalchemy as sa

revision = 'f6c2a8e4b1d7'
down_revision = 'e5b9c3a7d2f4'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'oauth_app',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('client_id', sa.String(64), nullable=False, unique=True),
        sa.Column('client_secret_hash', sa.String(64), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('website', sa.String(1024)),
        sa.Column('redirect_uris', sa.Text(), nullable=False),
        sa.Column('scope', sa.String(1024), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_table(
        'oauth_code',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('code_hash', sa.String(64), nullable=False, unique=True),
        sa.Column('app_id', sa.Integer(), sa.ForeignKey('oauth_app.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False),
        sa.Column('redirect_uri', sa.Text(), nullable=False),
        sa.Column('scope', sa.String(1024), nullable=False),
        sa.Column('code_challenge', sa.String(128)),
        sa.Column('code_challenge_method', sa.String(10)),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_oauth_code_app_id', 'oauth_code', ['app_id'])
    op.create_index('ix_oauth_code_user_id', 'oauth_code', ['user_id'])
    op.create_table(
        'oauth_token',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('app_id', sa.Integer(), sa.ForeignKey('oauth_app.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id', ondelete='CASCADE')),
        sa.Column('access_token_hash', sa.String(64), nullable=False, unique=True),
        sa.Column('refresh_token_hash', sa.String(64), unique=True),
        sa.Column('scope', sa.String(1024), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('last_used_at', sa.DateTime()),
        sa.Column('revoked_at', sa.DateTime()),
    )
    op.create_index('ix_oauth_token_app_id', 'oauth_token', ['app_id'])
    op.create_index('ix_oauth_token_user_id', 'oauth_token', ['user_id'])


def downgrade():
    op.drop_table('oauth_token')
    op.drop_table('oauth_code')
    op.drop_table('oauth_app')
