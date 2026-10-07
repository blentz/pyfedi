"""backfilled posts are re-ranked by when they were published

Revision ID: a3c9e5f1b7d4
Revises: f1b7d3a8c5e2
Create Date: 2026-10-07 12:00:00.000000

Interop D24. Post.new ranks a post by its arrival time; the outbox backfill then
re-dated backfilled posts from their 'published' date without re-ranking them, so
the hot sort (ORDER BY ranking) listed the media tab in per-channel ingestion
blocks. The code is fixed; this repairs rows already stored. A post whose
posted_at is more than an hour before created_at was backfilled. Its ranking is
recomputed with Post.post_ranking's formula (sign(score) * log10(max(|score|, 1))
+ (epoch seconds - 1685766018) / 45000, rounded to 7 places, naive UTC epoch), and
ranking_scaled moves by the same delta, since it is ranking plus a per-community
constant.
"""
from alembic import op

revision = 'a3c9e5f1b7d4'
down_revision = 'f1b7d3a8c5e2'
branch_labels = None
depends_on = None

RERANK_SQL = """
WITH fresh AS (
    SELECT id, ranking AS old_ranking,
           round((sign(score) * log(10, greatest(abs(score), 1)::numeric)
                  + (extract(epoch FROM posted_at) - 1685766018) / 45000.0)::numeric, 7)::double precision
               AS new_ranking
    FROM post
    WHERE posted_at < created_at - interval '1 hour'
)
UPDATE post
SET ranking = fresh.new_ranking,
    ranking_scaled = post.ranking_scaled + (fresh.new_ranking - fresh.old_ranking)
FROM fresh
WHERE post.id = fresh.id
"""


def upgrade():
    op.execute(RERANK_SQL)


def downgrade():
    # Ranking is derived data: the old values were wrong and are not restored.
    pass
