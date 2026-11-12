"""where this server's source code, bug tracker and get-involved page are

Revision ID: f2b6d8a4c9e1
Revises: e5a8c2f7b4d1
Create Date: 2026-11-12 14:00:00.000000

The footer's links, nodeinfo's repository and the Mastodon API's source_url (app/project_links.py). '' or NULL is
upstream PieFed's.
"""
from alembic import op
import sqlalchemy as sa


revision = 'f2b6d8a4c9e1'
down_revision = 'e5a8c2f7b4d1'
branch_labels = None
depends_on = None

COLUMNS = ('source_url', 'bug_report_url', 'get_involved_url')


def upgrade():
    with op.batch_alter_table('site') as batch_op:
        for column in COLUMNS:
            batch_op.add_column(sa.Column(column, sa.String(length=256), nullable=True, server_default=''))


def downgrade():
    with op.batch_alter_table('site') as batch_op:
        for column in reversed(COLUMNS):
            batch_op.drop_column(column)
