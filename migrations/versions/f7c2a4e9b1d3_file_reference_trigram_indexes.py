"""a file's other records are found by index: trigram indexes on the columns that name a file (plan C3)

Revision ID: f7c2a4e9b1d3
Revises: e5b9c3d7a1f4
Create Date: 2026-10-24 16:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


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


def drop_invalid_leftover(name, table):
    """An interrupted CREATE INDEX CONCURRENTLY (a deploy timeout, a cancelled lock wait) leaves an INVALID index of
    its name behind, which the planner never uses and which `IF NOT EXISTS` takes for the index: the rerun would
    stamp this revision with the sequential scans back. Drop it so the rerun builds the index again. Offline (--sql)
    there is no database to ask. (As f4b8d2a6c1e7's: a migration cannot import another.)"""
    if op.get_context().as_sql:
        return
    invalid = op.get_bind().execute(sa.text('SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid '
                                            'WHERE c.relname = :name AND NOT i.indisvalid'), {'name': name}).scalar()
    if invalid:
        op.drop_index(name, table_name=table, postgresql_concurrently=True)


def upgrade():
    with op.get_context().autocommit_block():
        op.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')
        for name, table, column in INDEXES:
            drop_invalid_leftover(name, table)
            op.execute(f'CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON "{table}" USING gin ({column} gin_trgm_ops)')


def downgrade():
    with op.get_context().autocommit_block():
        for name, _table, _column in INDEXES:
            op.execute(f'DROP INDEX CONCURRENTLY IF EXISTS {name}')
