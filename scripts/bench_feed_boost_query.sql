-- Throwaway benchmark for Task 7 (surface boosted posts in the feed).
--
-- Seeds a synthetic dataset into the TEST database (never the dev stack) and
-- runs EXPLAIN ANALYZE for the "subscribed feed" query from
-- get_deduped_post_ids() in app/utils.py, both WITHOUT and WITH the second
-- (post_boost) EXISTS clause added by this task, so the two plans/timings can
-- be compared directly in one run.
--
-- Re-run with:
--   podman-compose -f compose.test.yaml exec -T test-db \
--     psql -U pyfedi -d pyfedi_test -f - < scripts/bench_feed_boost_query.sql
--
-- What it seeds (deterministic, no randomness -- re-running reproduces the
-- same numbers):
--   * 400 remote instances (id 1..400)
--   * 300 communities (id 1..300), 20% (every 5th, 60 communities) with
--     show_all = true, the rest show_all = false, all unbanned
--   * 5001 users: id 1 is the "measured" local user; id 2..5001 are remote,
--     spread evenly across the 400 instances
--   * 50,000 posts (id 1..50000), authors and communities assigned by
--     deterministic arithmetic (not randomness) so the run is exactly
--     reproducible, posted_at spaced 1 minute apart counting back from now()
--   * user id 1 follows 300 remote users (id 2..301) -- "a few hundred
--     follows for the measured user"
--   * post_boost: every 20th post (2,500 posts total = 5% of all posts) is
--     boosted by exactly one user; of those, ~30% (750) are boosted by one
--     of the 300 followed users (id 2..301) and ~70% (1,750) by some other,
--     unfollowed, remote user -- so the new clause has real (but not
--     universal) work to do
--
-- The truncate/reseed is idempotent: run this file as many times as you
-- like. Nothing here survives the test suite's db_session fixture teardown
-- (which TRUNCATEs every table after each test), so run this AFTER
-- ./run_tests.sh, not interleaved with it -- or just re-run this file again
-- afterwards.

\timing on

BEGIN;

TRUNCATE TABLE post_boost, user_follower, post, community, "user", instance RESTART IDENTITY CASCADE;

-- 400 remote instances
INSERT INTO instance (id, domain, software, created_at, updated_at, last_seen,
                       failures, dormant, gone_forever, trusted, silenced, popular)
SELECT gs, 'remote' || gs || '.example', 'mastodon', now(), now(), now(),
       0, false, false, false, false, true
FROM generate_series(1, 400) AS gs;

-- 1 local instance for the measured user (id 1, reused as instance for community ownership too)
-- (community.instance_id below reuses the 400 remote instances; that's fine, communities
-- are remote-looking in this synthetic set, which is representative of federated feeds)

-- 300 communities: every 5th has show_all = true (60 of 300, ~20%)
INSERT INTO community (id, name, title, instance_id, subscriptions_count, total_subscriptions_count,
                        post_count, nsfw, nsfl, banned, local_only, private, show_all, show_popular,
                        searchable, encrypted, restricted_to_mods, new_mods_wanted, private_mods,
                        un_moderated, ap_discoverable, ignore_remote_language, ignore_remote_gen_ai,
                        created_at, last_active, first_federated_at)
SELECT gs, 'community' || gs, 'Community ' || gs, ((gs - 1) % 400) + 1, 0, 0, 0,
       false, false, false, false, false,
       (gs % 5 = 0),           -- show_all: true for every 5th community
       true, true, false, false, false, false, false, false, false, false,
       now(), now(), now()
FROM generate_series(1, 300) AS gs;

-- Measured local user, id 1
INSERT INTO "user" (id, user_name, email, instance_id, verified, banned,
                     ap_id, ap_profile_id, ap_public_url, ap_inbox_url,
                     created, last_seen, num_following, num_followers)
VALUES (1, 'measureduser', 'measureduser@example.com', 1, true, false,
        NULL, NULL, NULL, NULL, now(), now(), 300, 0);

-- 5000 remote users, id 2..5001, spread across the 400 instances
INSERT INTO "user" (id, user_name, email, instance_id, verified, banned,
                     ap_id, ap_profile_id, ap_public_url, ap_inbox_url,
                     created, last_seen, num_following, num_followers)
SELECT gs,
       'user' || gs,
       'user' || gs || '@example.com',
       ((gs - 1) % 400) + 1,
       true, false,
       'user' || gs || '@remote' || (((gs - 1) % 400) + 1) || '.example',
       'https://remote' || (((gs - 1) % 400) + 1) || '.example/users/user' || gs,
       'https://remote' || (((gs - 1) % 400) + 1) || '.example/users/user' || gs,
       'https://remote' || (((gs - 1) % 400) + 1) || '.example/users/user' || gs || '/inbox',
       now(), now(), 0, 0
FROM generate_series(2, 5001) AS gs;

SELECT setval('user_id_seq', 5001, true);

-- 50,000 posts. author_id and community_id assigned by deterministic
-- arithmetic; posted_at counts back from now() one minute per post so
-- ORDER BY posted_at DESC LIMIT 50 has a real, indexed ordering to walk.
INSERT INTO post (id, user_id, community_id, instance_id, status, title, type, microblog,
                   comments_enabled, deleted, reply_count, score, nsfw, nsfl, sticky,
                   instance_sticky, from_bot, private, indexable,
                   created_at, posted_at, last_active, up_votes, down_votes)
SELECT t.gs,
       t.author_id,
       1 + ((t.gs * 13) % 300),
       ((t.author_id - 1) % 400) + 1,
       1, 'Post ' || t.gs, 2, true,
       true, false, 0, 0, false, false, false,
       false, false, false, true,
       now() - (t.gs || ' minutes')::interval,
       now() - (t.gs || ' minutes')::interval,
       now() - (t.gs || ' minutes')::interval,
       0, 0
FROM (SELECT gs, 2 + ((gs * 7) % 5000) AS author_id FROM generate_series(1, 50000) AS gs) t;

SELECT setval('post_id_seq', 50000, true);

-- Measured user (id 1) follows 300 remote users, id 2..301.
INSERT INTO user_follower (local_user_id, remote_user_id, is_accepted, is_inward, created_at)
SELECT 1, gs, true, false, now()
FROM generate_series(2, 301) AS gs;

-- Boosts: every 20th post (2,500 of 50,000 = 5%) gets exactly one boost.
-- ~30% of those boosts (750) come from a followed user (id 2..301); the
-- remaining ~70% (1,750) come from some other remote user (id 302..5001).
INSERT INTO post_boost (user_id, post_id, created_at)
SELECT
    CASE WHEN (p.id / 20) % 10 < 3
         THEN 2 + ((p.id / 20) % 300)
         ELSE 302 + ((p.id * 31) % 4700)
    END,
    p.id,
    now()
FROM (SELECT id FROM post WHERE id % 20 = 0) p;

COMMIT;

-- Sanity counts
SELECT 'instances' AS what, count(*) FROM instance
UNION ALL SELECT 'communities', count(*) FROM community
UNION ALL SELECT 'communities_show_all', count(*) FROM community WHERE show_all is true
UNION ALL SELECT 'users', count(*) FROM "user"
UNION ALL SELECT 'posts', count(*) FROM post
UNION ALL SELECT 'follows_of_measured_user', count(*) FROM user_follower WHERE local_user_id = 1
UNION ALL SELECT 'post_boosts', count(*) FROM post_boost
UNION ALL SELECT 'post_boosts_by_followed', count(*) FROM post_boost pb
    WHERE EXISTS (SELECT 1 FROM user_follower uf WHERE uf.local_user_id = 1
                  AND uf.remote_user_id = pb.user_id AND uf.is_inward is false);

-- =====================================================================
-- BEFORE: the query as it exists prior to this task (single EXISTS, on
-- the post's author only).
-- =====================================================================
\echo '=== BEFORE (author-only EXISTS) ==='
EXPLAIN (ANALYZE, BUFFERS, TIMING)
SELECT p.id FROM "post" as p
INNER JOIN "community" as c on p.community_id = c.id
WHERE (c.show_all is true OR EXISTS (SELECT 1 FROM user_follower uf
      WHERE uf.local_user_id = 1 AND uf.remote_user_id = p.user_id AND is_inward is false))
AND c.banned is false ORDER BY p.posted_at DESC LIMIT 50;

-- =====================================================================
-- AFTER: with the second EXISTS (post_boost joined to user_follower)
-- added by this task, OR'ed into the same parenthesized clause.
-- =====================================================================
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
