"""a file's other records are found by index: trigram indexes on the columns that name a file (plan C3)

Revision ID: f7c2a4e9b1d3
Revises: e5b9c3d7a1f4
Create Date: 2026-10-24 16:00:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = 'f7c2a4e9b1d3'
down_revision = 'e5b9c3d7a1f4'
branch_labels = None
depends_on = None

# File.delete_from_disk asks which other records name a file (models._records_naming) by
# `<column> LIKE '%<name>%'`. A leading wildcard uses no btree index: on prod (402k posts, 285k files) the two
# selects were parallel sequential scans, about 400 ms per delete. A pg_trgm GIN index serves LIKE with any wildcard.
# Built CONCURRENTLY, outside a transaction, so the tables stay writable while they build. The downgrade drops the
# indexes and leaves the extension, which something else may use by then.
INDEXES = (
    ('ix_file_file_path_trgm', 'file', 'file_path'),
    ('ix_file_thumbnail_path_trgm', 'file', 'thumbnail_path'),
    ('ix_file_source_url_trgm', 'file', 'source_url'),
    ('ix_post_url_trgm', 'post', 'url'),
    ('ix_post_archived_trgm', 'post', 'archived'),
)


def upgrade():
    op.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')
    with op.get_context().autocommit_block():
        for name, table, column in INDEXES:
            op.execute(f'CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON "{table}" USING gin ({column} gin_trgm_ops)')


def downgrade():
    with op.get_context().autocommit_block():
        for name, _table, _column in INDEXES:
            op.execute(f'DROP INDEX CONCURRENTLY IF EXISTS {name}')
