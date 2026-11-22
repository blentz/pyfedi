"""user: what the microblog feed carries (microblog posts, community posts too, or all activity)

Revision ID: 9c1e42dfcd53
Revises: f5b1d8e3a7c9
Create Date: 2026-11-21 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '9c1e42dfcd53'
down_revision = 'f5b1d8e3a7c9'
branch_labels = None
depends_on = None


def upgrade():
    # 'microblog': only microblog posts are the default (spec 2026-11-21-microblog-feed-setting)
    op.add_column('user', sa.Column('microblog_feed', sa.String(length=16), nullable=False,
                                    server_default='microblog'))


def downgrade():
    op.drop_column('user', 'microblog_feed')
