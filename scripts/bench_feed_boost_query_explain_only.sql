-- Re-runs just the two EXPLAIN ANALYZE blocks from bench_feed_boost_query.sql
-- against whatever data is currently seeded (no re-seed, no truncate) -- used
-- to check timing stability on a warm buffer cache after the first run.
\timing on

\echo '=== BEFORE (author-only EXISTS) ==='
EXPLAIN (ANALYZE, BUFFERS, TIMING)
SELECT p.id FROM "post" as p
INNER JOIN "community" as c on p.community_id = c.id
WHERE (c.show_all is true OR EXISTS (SELECT 1 FROM user_follower uf
      WHERE uf.local_user_id = 1 AND uf.remote_user_id = p.user_id AND is_inward is false))
AND c.banned is false ORDER BY p.posted_at DESC LIMIT 50;

\echo '=== AFTER (author EXISTS OR boost-by-followed EXISTS) ==='
EXPLAIN (ANALYZE, BUFFERS, TIMING)
SELECT p.id FROM "post" as p
INNER JOIN "community" as c on p.community_id = c.id
WHERE (c.show_all is true OR EXISTS (SELECT 1 FROM user_follower uf
      WHERE uf.local_user_id = 1 AND uf.remote_user_id = p.user_id AND is_inward is false)
      OR EXISTS (SELECT 1 FROM post_boost pb
                 INNER JOIN user_follower uf2 ON uf2.remote_user_id = pb.user_id
                 WHERE pb.post_id = p.id
                 AND uf2.local_user_id = 1
                 AND uf2.is_inward is false))
AND c.banned is false ORDER BY p.posted_at DESC LIMIT 50;
