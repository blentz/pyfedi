"""mark the local microblogs community's kind (spec 2026-10-14 §3.1, §10 A15.3)

Revision ID: d4a7c1e9b3f6
Revises: b7e3c9a1f5d2
Create Date: 2026-10-16

"""
from alembic import op


revision = 'd4a7c1e9b3f6'
down_revision = 'b7e3c9a1f5d2'
branch_labels = None
depends_on = None

# find_microblogging_community()'s row
ROW = "instance_id = 1 AND user_id = 1 AND name = 'microblogs'"


def upgrade():
    op.execute(f"UPDATE community SET kind = 'microblogs' WHERE {ROW}")


def downgrade():
    op.execute(f"UPDATE community SET kind = 'discussion' WHERE {ROW}")
