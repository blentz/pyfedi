-- Throwaway benchmark for Task 7 (surface boosted posts in the feed), v2.
--
-- Lives here, NOT in scripts/, because it TRUNCATEs six tables on entry -- it must
-- never ship in a directory a deploy or a curious operator might run something out
-- of. Seeds a synthetic dataset into the TEST database (never the dev stack) and
-- runs EXPLAIN ANALYZE for the actual "hot" feed query shape from
-- get_deduped_post_ids() in app/utils.py: ORDER BY p.ranking DESC, p.posted_at DESC
-- LIMIT 1000 (the real default sort/limit, not the LIMIT 50 example in the task
-- brief), both WITHOUT and WITH the new post_boost EXISTS clause -- including its
-- `p.private is false` gate -- so the two plans/timings can be compared directly.
--
-- Fixes applied after review of the first version of this script:
--   - ANALYZE now runs after the seed COMMIT, before either EXPLAIN. The first
--     version measured a stats-less planner; that made the "AFTER" plan's cost
--     estimate look artificially worse than "BEFORE" even though AFTER ran faster,
--     which was meaningless -- both plans were available to the planner throughout,
--     BEFORE was just mis-costed. That comparison has been dropped entirely.
--   - Boosted posts are now selected via `hashtext()`-based mixing instead of
--     `id % 20 = 0`. The old modulo selection aliased against the community-id
--     formula (`1 + ((id*13) % 300)`, gcd(20,300)=20) and the author-id formula
--     (`2 + ((id*7) % 5000)`, gcd(20,5000)=20): every boosted post landed in one of
--     only 15 communities, and none of them had show_all=true, and boosts spanned
--     only 250 distinct authors -- the OPPOSITE of the "partial, non-trivial
--     overlap" the report claimed. The sanity-count block below now explicitly
--     reports how many boosted posts land in show_all communities and how many
--     distinct authors they span, so this can't silently regress again.
--   - `post.ranking` is now seeded with a spread of values (also via `hashtext()`,
--     deterministic) instead of being left at the column default of 0.0 for every
--     row. The production query's default sort is `ORDER BY p.ranking DESC,
--     p.posted_at DESC`, not `ORDER BY p.posted_at DESC` alone; with every row
--     ranked identically at 0.0, that first sort key would have done nothing and
--     the benchmark would silently have been testing plain posted_at ordering.
--   - LIMIT and ORDER BY now match get_deduped_post_ids' default ("hot") sort:
--     `ORDER BY p.ranking DESC, p.posted_at DESC LIMIT 1000`, not the brief's
--     example `ORDER BY p.posted_at DESC LIMIT 50`. At LIMIT 50 the query stopped
--     after ~78 candidate rows and the new EXISTS subplan ran a few dozen times;
--     at LIMIT 1000 it has to keep going far longer, which is the dimension that
--     actually drives the new clause's cost.
--   - The AFTER query now includes the `p.private is false` gate that ships in
--     app/utils.py (this task's clause reads `(p.private is false AND EXISTS(...))`,
--     not a bare EXISTS) -- benchmarking the ungated form would not measure what
--     actually ships.
--
-- boost_count controls how many of the 50,000 posts get boosted -- pass it with
-- `-v boost_count=N` (default 2500 = 5% of posts, if not passed). Re-run at
-- boost_count=5000 (~10% of posts) for the second, larger-table-size gate check:
--
--   podman-compose -f compose.test.yaml exec -T test-db \
--     psql -U pyfedi -d pyfedi_test -v boost_count=2500 -f - \
--     < .superpowers/sdd/2026-08-23-microblog-boost-ingestion/bench_feed_boost_query.sql
--
--   podman-compose -f compose.test.yaml exec -T test-db \
--     psql -U pyfedi -d pyfedi_test -v boost_count=5000 -f - \
--     < .superpowers/sdd/2026-08-23-microblog-boost-ingestion/bench_feed_boost_query.sql
--
-- Deterministic and idempotent otherwise: everything but boost_count is fixed, so
-- two runs at the same boost_count reproduce the same numbers. Nothing here
-- survives the test suite's db_session fixture teardown (which TRUNCATEs every
-- table after each test) or this script's own next run, so run this AFTER
-- ./run_tests.sh, not interleaved with it, and truncate again before running the
-- suite afterwards -- see the TRUNCATE command in task-7-report.md's "Cleanup"
-- section.
--
-- What it seeds (all deterministic -- no true randomness, hashtext() is a fixed
-- function of its input):
--   * 400 remote instances (id 1..400)
--   * 300 communities (id 1..300), 20% (every 5th, 60 communities) with
--     show_all = true, the rest show_all = false, all unbanned
--   * 5001 users: id 1 is the "measured" local user; id 2..5001 are remote,
--     spread evenly across the 400 instances
--   * 50,000 posts (id 1..50000), community assigned by `1 + ((id*13) % 300)`,
--     author by `2 + ((id*7) % 5000)` (both formulas are bijective mod their
--     modulus, since gcd(13,300)=1 and gcd(7,5000)=1 -- uniform coverage over the
--     full post set), `ranking` spread via hashtext, `private = false` for every
--     post, `posted_at` spaced 1 minute apart counting back from now()
--   * user id 1 follows 300 remote users (id 2..301) -- "a few hundred follows
--     for the measured user"
--   * post_boost: :boost_count posts (selected via hashtext mixing, NOT an
--     arithmetic progression) are each boosted by exactly one user; of those,
--     ~30% are boosted by one of the 300 followed users (id 2..301) and ~70% by
--     some other, unfollowed, remote user (id 302..5001) -- also via hashtext, so
--     this selection doesn't alias either

\if :{?boost_count}
\else
\set boost_count 2500
\endif

\timing on

BEGIN;

TRUNCATE TABLE post_boost, user_follower, post, community, "user", instance RESTART IDENTITY CASCADE;

-- 400 remote instances
INSERT INTO instance (id, domain, software, created_at, updated_at, last_seen,
                       failures, dormant, gone_forever, trusted, silenced, popular)
SELECT gs, 'remote' || gs || '.example', 'mastodon', now(), now(), now(),
       0, false, false, false, false, true
FROM generate_series(1, 400) AS gs;

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

-- 50,000 posts. author_id and community_id assigned by formulas that are bijective
-- mod their modulus (gcd(13,300)=1, gcd(7,5000)=1), so coverage is uniform.
-- ranking is spread via hashtext() so ORDER BY p.ranking DESC is not degenerate
-- (the column default is 0.0 for every row otherwise). private = false for every
-- post: this dataset models only public posts so the new clause's own
-- `p.private is false` gate is exercised as "always true", isolating the cost of
-- the EXISTS itself -- see the private-post visibility test in
-- tests/test_feed_boost_visibility.py for the gate's correctness, which this
-- performance benchmark does not re-check.
INSERT INTO post (id, user_id, community_id, instance_id, status, title, type, microblog,
                   comments_enabled, deleted, reply_count, score, nsfw, nsfl, sticky,
                   instance_sticky, from_bot, private, indexable,
                   created_at, posted_at, last_active, up_votes, down_votes, ranking)
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
       0, 0,
       (abs(hashtext('rank:' || t.gs)) % 1000000)::float / 10000.0   -- 0.0000..99.9999, deterministic
FROM (SELECT gs, 2 + ((gs * 7) % 5000) AS author_id FROM generate_series(1, 50000) AS gs) t;

SELECT setval('post_id_seq', 50000, true);

-- Measured user (id 1) follows 300 remote users, id 2..301.
INSERT INTO user_follower (local_user_id, remote_user_id, is_accepted, is_inward, created_at)
SELECT 1, gs, true, false, now()
FROM generate_series(2, 301) AS gs;

-- Boosts: :boost_count posts, selected via hashtext() mixing (not an arithmetic
-- progression, so it does not alias against the community_id/author_id formulas
-- above), each get exactly one boost. ~30% of boosts (also chosen via hashtext)
-- come from a followed user (id 2..301); the rest come from some other remote user
-- (id 302..5001).
INSERT INTO post_boost (user_id, post_id, created_at)
SELECT
    CASE WHEN abs(hashtext('booster-pick:' || p.id)) % 10 < 3
         THEN 2 + (abs(hashtext('booster-followed:' || p.id)) % 300)
         ELSE 302 + (abs(hashtext('booster-other:' || p.id)) % 4700)
    END,
    p.id,
    now()
FROM (
    SELECT id FROM post
    ORDER BY abs(hashtext('boost-pick:' || id))
    LIMIT :boost_count
) p;

COMMIT;

-- Give the planner real statistics before measuring -- the first version of this
-- script skipped this, so its "cold" EXPLAIN ANALYZE measured stats-less planner
-- behaviour rather than anything representative.
ANALYZE instance, community, "user", post, user_follower, post_boost;

-- Sanity counts, including the two ITEM 4 checks: how many boosted posts land in a
-- show_all community (should be a healthy chunk, not zero), and how many distinct
-- authors the boosted posts span (should be a large fraction of boost_count, not a
-- small aliased subset).
SELECT 'boost_count (requested)' AS what, :'boost_count' AS value
UNION ALL SELECT 'instances', count(*)::text FROM instance
UNION ALL SELECT 'communities', count(*)::text FROM community
UNION ALL SELECT 'communities_show_all', count(*)::text FROM community WHERE show_all is true
UNION ALL SELECT 'users', count(*)::text FROM "user"
UNION ALL SELECT 'posts', count(*)::text FROM post
UNION ALL SELECT 'follows_of_measured_user', count(*)::text FROM user_follower WHERE local_user_id = 1
UNION ALL SELECT 'post_boosts', count(*)::text FROM post_boost
UNION ALL SELECT 'post_boosts_by_followed', count(*)::text FROM post_boost pb
    WHERE EXISTS (SELECT 1 FROM user_follower uf WHERE uf.local_user_id = 1
                  AND uf.remote_user_id = pb.user_id AND uf.is_inward is false)
UNION ALL SELECT 'post_boosts_in_show_all_community (ITEM 4)', count(*)::text
    FROM post_boost pb JOIN post p ON p.id = pb.post_id JOIN community c ON c.id = p.community_id
    WHERE c.show_all is true
UNION ALL SELECT 'post_boosts_distinct_authors (ITEM 4)', count(DISTINCT p.user_id)::text
    FROM post_boost pb JOIN post p ON p.id = pb.post_id;

-- =====================================================================
-- BEFORE: the query as it exists prior to this task (single EXISTS, on the
-- post's author only), with the real default sort/limit.
-- =====================================================================
\echo '=== BEFORE (author-only EXISTS), boost_count=' :boost_count ' ==='
EXPLAIN (ANALYZE, BUFFERS, TIMING)
SELECT p.id FROM "post" as p
INNER JOIN "community" as c on p.community_id = c.id
WHERE (c.show_all is true OR EXISTS (SELECT 1 FROM user_follower uf
      WHERE uf.local_user_id = 1 AND uf.remote_user_id = p.user_id AND is_inward is false))
AND c.banned is false ORDER BY p.ranking DESC, p.posted_at DESC LIMIT 1000;

-- =====================================================================
-- AFTER: with the second, privacy-gated EXISTS added by this task, OR'ed into
-- the same parenthesized clause -- exactly as shipped in app/utils.py.
-- =====================================================================
\echo '=== AFTER (author EXISTS OR private-gated boost-by-followed EXISTS), boost_count=' :boost_count ' ==='
EXPLAIN (ANALYZE, BUFFERS, TIMING)
SELECT p.id FROM "post" as p
INNER JOIN "community" as c on p.community_id = c.id
WHERE (c.show_all is true OR EXISTS (SELECT 1 FROM user_follower uf
      WHERE uf.local_user_id = 1 AND uf.remote_user_id = p.user_id AND is_inward is false)
      OR (p.private is false AND EXISTS (SELECT 1 FROM post_boost pb
                 INNER JOIN user_follower uf2 ON uf2.remote_user_id = pb.user_id
                 WHERE pb.post_id = p.id
                 AND uf2.local_user_id = 1
                 AND uf2.is_inward is false)))
AND c.banned is false ORDER BY p.ranking DESC, p.posted_at DESC LIMIT 1000;
