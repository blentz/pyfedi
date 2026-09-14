"""backfill reply_count_cross_posted

Revision ID: 9e99070afe06
Revises: 7b8bf43fa079
Create Date: 2026-09-14 16:45:19.746146

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9e99070afe06'
down_revision = '7b8bf43fa079'
branch_labels = None
depends_on = None


def upgrade():
    # post.reply_count_cross_posted was added nullable with no server default
    # and no backfill (6814385881d3), so every row predating that revision
    # holds NULL. app/models.py:1723's default=0 is Python-side and applies
    # only to rows the ORM inserts. Five of the six sites that decrement or
    # increment this column are unguarded, and None - 1 raises TypeError.
    #
    # reply_count is the right value rather than 0: app/models.py:3108 defines
    # the column as the post's own reply_count when it has no cross-posts, and
    # :3100-3104 as the sum across the cross-post set when it does. The WHERE
    # clause keeps correctly-maintained counts from being flattened.
    op.execute("UPDATE post SET reply_count_cross_posted = reply_count "
               "WHERE reply_count_cross_posted IS NULL")
    op.execute("ALTER TABLE post ALTER COLUMN reply_count_cross_posted "
               "SET DEFAULT 0")


def downgrade():
    # Drop only the default. The backfilled values are not restored to NULL:
    # a downgrade that destroys rows is worse than the defect it reverses.
    op.execute("ALTER TABLE post ALTER COLUMN reply_count_cross_posted "
               "DROP DEFAULT")
