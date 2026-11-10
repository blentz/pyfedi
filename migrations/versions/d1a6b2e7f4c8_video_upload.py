"""A video uploaded in parts through PeerTube's resumable upload: video_upload

Revision ID: d1a6b2e7f4c8
Revises: c9f5a1d4e6b3
Create Date: 2026-11-10 20:00:00.000000

Plan W2.3, spec docs/superpowers/specs/2026-11-10-peertube-upload-design.md.
"""
from alembic import op
import sqlalchemy as sa

revision = 'd1a6b2e7f4c8'
down_revision = 'c9f5a1d4e6b3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'video_upload',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('upload_id', sa.String(64), nullable=False, unique=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False),
        sa.Column('community_id', sa.Integer(), sa.ForeignKey('community.id', ondelete='CASCADE'), nullable=False),
        sa.Column('filename', sa.String(256), nullable=False),
        sa.Column('name', sa.String(256), nullable=False),
        sa.Column('content_type', sa.String(128), nullable=False),
        sa.Column('size', sa.BigInteger(), nullable=False),
        sa.Column('received', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('path', sa.String(512), nullable=False),
        sa.Column('details', sa.JSON()),
        sa.Column('finished_at', sa.DateTime()),
        sa.Column('post_id', sa.Integer(), sa.ForeignKey('post.id', ondelete='SET NULL')),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_video_upload_user_id', 'video_upload', ['user_id'])
    op.create_index('ix_video_upload_post_id', 'video_upload', ['post_id'])


def downgrade():
    op.drop_table('video_upload')
