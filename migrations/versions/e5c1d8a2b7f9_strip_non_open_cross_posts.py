"""followers-only posts are taken out of every cross-post list

Revision ID: e5c1d8a2b7f9
Revises: c4a8e2f61d3b
Create Date: 2026-10-10 12:00:00.000000

Amendment B (S7.4), ruling R-XPOSTCLEAN. Post.calculate_cross_posts used to link a
followers-only post with every other post of the same link, either way, so a public
post's page offered it as "Comments from other communities" and its cross-post
list. The code no longer does that; this repairs rows already stored:

1. every post's cross_posts loses the ids of non-open posts (visibility other than
   public/unlisted);
2. a non-open post's own cross_posts is cleared, and its reply_count_cross_posted
   falls back to its own reply_count;
3. each post whose list lost an id has reply_count_cross_posted recomputed the way
   PostReply.new maintains it: the post's reply_count plus that of every post still
   in its list (just its reply_count when the list is now empty).
"""
from alembic import op
from sqlalchemy import text

revision = 'e5c1d8a2b7f9'
down_revision = 'c4a8e2f61d3b'
branch_labels = None
depends_on = None

STRIP_SQL = """
UPDATE post
SET cross_posts = ARRAY(SELECT x FROM unnest(post.cross_posts) AS x
                        WHERE x NOT IN (SELECT id FROM post WHERE visibility NOT IN ('public', 'unlisted')))
WHERE visibility IN ('public', 'unlisted')
  AND cross_posts && ARRAY(SELECT id FROM post WHERE visibility NOT IN ('public', 'unlisted'))
RETURNING id
"""

CLEAR_SQL = """
UPDATE post
SET cross_posts = NULL, reply_count_cross_posted = reply_count
WHERE visibility NOT IN ('public', 'unlisted') AND cross_posts IS NOT NULL
"""

RECOUNT_SQL = """
UPDATE post
SET reply_count_cross_posted = post.reply_count + coalesce(
    (SELECT sum(other.reply_count) FROM post AS other WHERE other.id = ANY(post.cross_posts)), 0)
WHERE post.id = ANY(:ids)
"""


def strip_non_open_cross_posts(connection):
    affected = [row[0] for row in connection.execute(text(STRIP_SQL))]
    connection.execute(text(CLEAR_SQL))
    if affected:
        connection.execute(text(RECOUNT_SQL), {'ids': affected})


def upgrade():
    strip_non_open_cross_posts(op.get_bind())


def downgrade():
    # Nothing to undo. The rows removed were links the code must never have made (a followers-only post offered on
    # public pages); restoring them would re-open that leak, and the counts are derived data recomputed from them.
    pass
