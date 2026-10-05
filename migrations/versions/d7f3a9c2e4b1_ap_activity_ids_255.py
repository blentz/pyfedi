"""post and post_reply keep Create and Announce ids up to 255 characters

Revision ID: d7f3a9c2e4b1
Revises: c6e1d9a4b7f2
Create Date: 2026-10-05 02:30:00.000000

PeerTube's announce ids (https://<host>/videos/watch/<uuid>/announces/<n>) pass 100 characters, so String(100)
refused those videos at insert; ap_id is already String(255). Widening a varchar is a catalog-only change in
Postgres: no table rewrite.
"""
from alembic import op
import sqlalchemy as sa

revision = 'd7f3a9c2e4b1'
down_revision = 'c6e1d9a4b7f2'
branch_labels = None
depends_on = None

COLUMNS = [(table, column) for table in ('post', 'post_reply') for column in ('ap_create_id', 'ap_announce_id')]


def upgrade():
    for table, column in COLUMNS:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.alter_column(column, existing_type=sa.String(length=100), type_=sa.String(length=255),
                                  existing_nullable=True)


def downgrade():
    for table, column in COLUMNS:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.alter_column(column, existing_type=sa.String(length=255), type_=sa.String(length=100),
                                  existing_nullable=True)
