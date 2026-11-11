"""Stories seen and stories expired early: story_view, story_expiry

Revision ID: a4d9e1c7b3f2
Revises: f3c8d4a2b6e9
Create Date: 2026-11-11 15:00:00.000000

Plan W1.4c, spec docs/superpowers/specs/2026-11-10-pixelfed-app-design.md §7 (rulings S1, S2 of 2026-11-11).
"""
from alembic import op
import sqlalchemy as sa

revision = 'a4d9e1c7b3f2'
down_revision = 'f3c8d4a2b6e9'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'story_view',
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False),
        sa.Column('post_id', sa.Integer(), sa.ForeignKey('post.id', ondelete='CASCADE'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('user_id', 'post_id'),
    )
    op.create_index('ix_story_view_post_id', 'story_view', ['post_id'])
    op.create_table(
        'story_expiry',
        sa.Column('post_id', sa.Integer(), sa.ForeignKey('post.id', ondelete='CASCADE'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('post_id'),
    )
    op.create_index('ix_story_expiry_created_at', 'story_expiry', ['created_at'])


def downgrade():
    op.drop_index('ix_story_expiry_created_at', table_name='story_expiry')
    op.drop_table('story_expiry')
    op.drop_index('ix_story_view_post_id', table_name='story_view')
    op.drop_table('story_view')
