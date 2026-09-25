"""Make the columns the post sorts page by NOT NULL

Revision ID: c4f1a9d7e2b8
Revises: 9e99070afe06
Create Date: 2026-09-25 18:40:00.000000

/api/alpha/post/list2 pages with sqlakeyset, which warns -- correctly -- that
"Ordering by nullable column post.X can cause rows to be incorrectly omitted
from the results". A keyset page is `WHERE (sort_columns) < (values from the
last row)`, and a NULL in any of those comparisons makes the predicate NULL
rather than true, so the row is never returned by any page. Not an error
anywhere: the row is simply missing from every page of that sort, and the total
still counts it.

Seven columns feed those sorts: sticky, instance_sticky, score, ranking,
ranking_scaled, posted_at and last_active. Six have a Python-side default and so
are only ever NULL on rows written before it existed; `last_active` has no
default of any kind, and a post with replies but no last_active was exactly the
omitted row the API's Active arm had to filter out by hand.

WHAT THIS COSTS TO RUN. `SET NOT NULL` normally scans the whole table under an
ACCESS EXCLUSIVE lock, which on a large `post` table is a write outage for the
length of the scan. PostgreSQL 12 and later will accept a validated CHECK
constraint as proof instead, so each column here is done as:

    ADD CONSTRAINT ... CHECK (col IS NOT NULL) NOT VALID   -- instant
    VALIDATE CONSTRAINT ...                                -- scans, but takes
                                                              only SHARE UPDATE
                                                              EXCLUSIVE, so
                                                              reads and writes
                                                              continue
    ALTER COLUMN ... SET NOT NULL                          -- no scan, brief lock
    DROP CONSTRAINT ...                                    -- instant

The UPDATEs that fill the NULLs come first and are the only part that touches
row data; they are limited to rows that are actually NULL.
"""
from alembic import op

revision = 'c4f1a9d7e2b8'
down_revision = '9e99070afe06'
branch_labels = None
depends_on = None

# Each column with the value to give the rows that have none. The six constants
# are the same defaults the model has applied to every row written since they
# were added, so this backfill makes old rows match new ones.
BACKFILLS = (
    ('sticky', 'false'),
    ('instance_sticky', 'false'),
    ('score', '0'),
    ('ranking', '0'),
    ('ranking_scaled', '0'),
    # posted_at is when the originating server created the post; created_at is
    # when it arrived here, which is the closest thing available for a row that
    # never recorded one.
    ('posted_at', 'COALESCE(created_at, NOW())'),
    # last_active is the newest thing that happened to the post. The newest
    # reply is the truthful answer where there are replies -- and those are the
    # rows the Active sort was dropping -- and the post's own time where there
    # are none.
    ('last_active',
     'COALESCE((SELECT MAX(pr.posted_at) FROM post_reply pr WHERE pr.post_id = post.id),'
     ' posted_at, created_at, NOW())'),
)


# A NOT NULL column with no SERVER default makes every INSERT that omits it
# fail, and not every insert goes through the ORM's Python-side defaults -- a
# raw `INSERT INTO post` in a migration, a test or a psql session does not. The
# schema carries the same defaults the model does, so the two agree whoever is
# writing.
SERVER_DEFAULTS = (
    ('sticky', 'false'),
    ('instance_sticky', 'false'),
    ('score', '0'),
    ('ranking', '0'),
    ('ranking_scaled', '0'),
    ('posted_at', 'now()'),
    ('last_active', 'now()'),
)


def upgrade():
    for column, value in BACKFILLS:
        op.execute(f'UPDATE post SET {column} = {value} WHERE {column} IS NULL')

    for column, value in SERVER_DEFAULTS:
        op.execute(f'ALTER TABLE post ALTER COLUMN {column} SET DEFAULT {value}')

    for column, _value in BACKFILLS:
        constraint = f'post_{column}_not_null'
        op.execute(f'ALTER TABLE post ADD CONSTRAINT {constraint} '
                   f'CHECK ({column} IS NOT NULL) NOT VALID')
        op.execute(f'ALTER TABLE post VALIDATE CONSTRAINT {constraint}')
        op.execute(f'ALTER TABLE post ALTER COLUMN {column} SET NOT NULL')
        op.execute(f'ALTER TABLE post DROP CONSTRAINT {constraint}')


def downgrade():
    # The backfilled values are left in place: they are what the model would
    # have written, and there is no record of which rows were NULL before.
    for column, _value in BACKFILLS:
        op.execute(f'ALTER TABLE post ALTER COLUMN {column} DROP NOT NULL')
    for column, _value in SERVER_DEFAULTS:
        op.execute(f'ALTER TABLE post ALTER COLUMN {column} DROP DEFAULT')
