"""user: which parts of their profile other people see

Revision ID: f2b7c9d4e1a6
Revises: e7c3a9d5b214
Create Date: 2026-11-26 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'f2b7c9d4e1a6'
down_revision = 'e7c3a9d5b214'
branch_labels = None
depends_on = None


def upgrade():
    # spec 2026-11-26-profile-privacy: microblog posts only by default, the followers list as before
    op.add_column('user', sa.Column('profile_community_posts', sa.Boolean(), nullable=False, server_default='false'))
    op.add_column('user', sa.Column('profile_comments', sa.Boolean(), nullable=False, server_default='false'))
    op.add_column('user', sa.Column('profile_followers', sa.String(length=8), nullable=False, server_default='list'))


def downgrade():
    op.drop_column('user', 'profile_followers')
    op.drop_column('user', 'profile_comments')
    op.drop_column('user', 'profile_community_posts')
