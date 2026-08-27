"""Proof tests for the feed-related factories this task adds, and for the
Redis-write hazard in get_deduped_post_ids.

Every factory here is proven against the PRODUCTION reader that consumes it:
blocked_users / blocked_domains / blocked_or_banned_instances / blocked_communities
directly (they are standalone functions), and the raw read_posts / hidden_posts /
post_flair queries embedded in get_deduped_post_ids and get_instance_stickies by
driving those functions end to end. A factory that wrote rows the reader does not
read would let every later test built on it pass without exercising the thing it
names.

Redis policy: get_deduped_post_ids's cache-write
(`redis_client.set(result_id, ..., ex=86400)`, app/utils.py:3942) targets whatever
`app.redis_client` currently is. redis_double (tests/conftest.py) now patches that
attribute, in addition to the four get_redis_connection bindings it already
covered -- see its docstring for why a single monkeypatch is enough here (every
`from app import redis_client` site does that import inside a function body, so it
is re-resolved on every call). Every test below that drives get_deduped_post_ids
requests redis_double for exactly that reason: without it, each authenticated call
would write a real, never-expiring-in-practice key to the shared test Redis that
only `./run_tests.sh --down` clears -- forbidden by this campaign because it
replays ~269 migrations.
"""
import json
import uuid

import redis
from flask_login import login_user

from app import db
from app.utils import (blocked_communities, blocked_domains, blocked_or_banned_instances,
                       blocked_users, get_deduped_post_ids, get_instance_stickies)
from tests.factories import (hide_post, make_community, make_community_block, make_community_member,
                             make_domain, make_domain_block, make_flair_block, make_instance,
                             make_instance_block, make_post, make_post_flair, make_user,
                             make_user_block, mark_post_read)


def logged_in_ids(app, viewer, community_ids, **kwargs):
    """The post ids get_deduped_post_ids returns for `viewer`.

    A fresh uuid result_id on every call, so nothing is ever served from a
    previously cached key -- each call genuinely re-runs the query.
    """
    with app.test_request_context('/'):
        login_user(viewer)
        return get_deduped_post_ids(uuid.uuid4().hex, community_ids, 'new', **kwargs)


def logged_in_stickies(app, viewer, community_ids, sort='new'):
    """The Post objects get_instance_stickies returns for `viewer`."""
    with app.test_request_context('/'):
        login_user(viewer)
        return get_instance_stickies(community_ids, sort)


# --- Step 1: the Redis-write policy, proved before anything else depends on it --

def test_redis_double_covers_app_redis_client(app, db_session, redis_double):
    """get_deduped_post_ids really does write into whatever `app.redis_client`
    points at. Would fail (dbsize staying 0) if redis_double stopped patching
    that attribute -- the write would go to the object redis_double built,
    untouched, while the real client kept receiving it unnoticed.
    """
    make_instance('redispolicy.example')
    user = make_user(None, 'redispolicyuser', local=True)
    community = make_community('rediscomm')
    make_community_member(user, community)
    make_post(community, user, 'https://redispolicy.example/posts/1')

    assert redis_double.dbsize() == 0
    logged_in_ids(app, user, [community.id])
    assert redis_double.dbsize() == 1, \
        'get_deduped_post_ids should have written its cached result through app.redis_client'


def test_authenticated_feed_calls_do_not_grow_the_real_test_redis(app, db_session, redis_double):
    """The hazard this step exists to close, measured directly against the real
    server rather than inferred: connect to the actual test Redis (the same
    CACHE_REDIS_URL production uses), bypassing every patched name, and show its
    key count does not move while five authenticated get_deduped_post_ids calls
    run under redis_double.

    Would fail if redis_double stopped covering app.redis_client, or if a future
    change to get_deduped_post_ids started writing through some other, unpatched
    binding -- either way this test would see the real server's dbsize grow.
    """
    real_redis = redis.Redis.from_url(app.config['CACHE_REDIS_URL'], decode_responses=True)
    before = real_redis.dbsize()

    make_instance('growthcheck.example')
    user = make_user(None, 'growthcheckuser', local=True)
    community = make_community('growthcomm')
    make_community_member(user, community)
    make_post(community, user, 'https://growthcheck.example/posts/1')

    for _ in range(5):
        logged_in_ids(app, user, [community.id])

    after = real_redis.dbsize()
    assert after == before, \
        f'real test Redis grew from {before} to {after} keys across 5 authenticated calls'


# --- Step 3/4: the new factories, each proved against its production reader ---

def test_domain_block_is_visible_to_blocked_domains(app, db_session):
    """Fails if the factory writes rows blocked_domains() does not read."""
    make_instance('domainblock.example')
    user = make_user(None, 'domainblocker', local=True)
    domain = make_domain('blocked.example')
    make_domain_block(user, domain)

    assert domain.id in blocked_domains(user.id)


def test_instance_block_is_visible_to_blocked_or_banned_instances(app, db_session):
    """Fails if the factory writes rows blocked_or_banned_instances() does not
    read. (The InstanceBan half of this union is covered by make_instance_ban's
    own docstring; this proves the InstanceBlock half.)
    """
    make_instance('instanceblockerhome.example')
    user = make_user(None, 'instanceblocker', local=True)
    instance = make_instance('blocked-instance.example')
    make_instance_block(user, instance)

    assert instance.id in blocked_or_banned_instances(user.id)


def test_community_block_is_visible_to_blocked_communities(app, db_session):
    """Fails if the factory writes rows blocked_communities() does not read."""
    make_instance('communityblock.example')
    user = make_user(None, 'communityblocker', local=True)
    community = make_community('blockedcomm')
    make_community_block(user, community)

    assert community.id in blocked_communities(user.id)


def test_marked_read_post_is_excluded_from_the_feed_when_hide_read_posts_is_set(app, db_session, redis_double):
    """Fails if mark_post_read writes rows the raw `read_posts` query inside
    get_deduped_post_ids (app/utils.py:3861-3862) does not read.

    The control assertion (post visible before marking read) rules out the post
    being excluded for some unrelated reason.
    """
    make_instance('readcheck.example')
    viewer = make_user(None, 'readhider', local=True)
    viewer.hide_read_posts = True
    db.session.commit()
    community = make_community('readcomm')
    make_community_member(viewer, community)
    post = make_post(community, viewer, 'https://readcheck.example/posts/1')

    assert post.id in logged_in_ids(app, viewer, [community.id])
    mark_post_read(viewer, post)
    assert post.id not in logged_in_ids(app, viewer, [community.id])


def test_hidden_post_is_excluded_from_instance_stickies(app, db_session):
    """Fails if hide_post writes rows the raw `hidden_posts` query inside
    get_instance_stickies (app/utils.py:4012-4014, 4037) does not read. Proves the
    hidden_posts reader in get_instance_stickies specifically -- a sibling test
    (test_marked_read_post_is_excluded_from_the_feed...) proves the read_posts /
    hidden_posts readers inside get_deduped_post_ids instead, so between them both
    named readers of these factories are exercised.
    """
    make_instance('stickycheck.example')
    viewer = make_user(None, 'stickyhider', local=True)
    community = make_community('stickycomm')
    make_community_member(viewer, community)
    post = make_post(community, viewer, 'https://stickycheck.example/posts/1')
    post.instance_sticky = True
    db.session.commit()

    assert post.id in [p.id for p in logged_in_stickies(app, viewer, [community.id])]
    hide_post(viewer, post)
    assert post.id not in [p.id for p in logged_in_stickies(app, viewer, [community.id])]


def test_flair_block_is_excluded_from_the_feed(app, db_session, redis_double):
    """Fails if make_post_flair / make_flair_block write rows the raw
    `post_flair` / CommunityFlairBlock query inside get_deduped_post_ids
    (app/utils.py:3893-3899) does not read.
    """
    make_instance('flaircheck.example')
    viewer = make_user(None, 'flairblocker', local=True)
    community = make_community('flaircomm')
    make_community_member(viewer, community)
    post = make_post(community, viewer, 'https://flaircheck.example/posts/1')
    flair = make_post_flair(post, name='spoiler')

    assert post.id in logged_in_ids(app, viewer, [community.id])
    make_flair_block(viewer, flair)
    assert post.id not in logged_in_ids(app, viewer, [community.id])


# --- Step 5: NullCache must still hold, or seven later tasks build on sand ----

def test_blocked_users_is_not_cached_between_calls(app, db_session):
    """A stale filter answer is indistinguishable from a flake, so this is
    re-verified here rather than assumed from sub-project 0 / 1b-i.

    A test that called blocked_users() twice and got the right answer both times
    would prove nothing -- the first call could have poisoned the second with a
    cached []. Changing the underlying data (creating the UserBlock row) BETWEEN
    the two calls is what makes this discriminate: it would fail if
    blocked_users() were @cache.memoize'd against a real cache backend, because
    the second call would still return the first call's cached [].

    Would fail (second assertion) if TestConfig's CACHE_TYPE stopped being
    NullCache, or if blocked_users() somehow gained its own non-memoize caching.
    """
    make_instance('cachecheck.example')
    blocker = make_user(None, 'cachecheck', local=True)
    blocked = make_user(None, 'target', local=True)

    assert blocked_users(blocker.id) == []
    make_user_block(blocker, blocked)
    assert blocked.id in blocked_users(blocker.id)


# --- Fix round 1: the two undocumented gaps in get_deduped_post_ids's own body,
# found during Task 8's fresh re-measurement (neither is the hashtag filter, the
# anonymous private-community branch, or the unrecognized-sort fallthrough --
# those three stay documented-uncovered; these two were simply never noticed) --

def test_empty_community_ids_returns_an_empty_list_without_querying(app, db_session):
    """Covers the early-return guard at app/utils.py:3792-3793 --
    `if not community_sql and (community_ids is None or len(community_ids) == 0):
    return []` -- never exercised by any earlier test in this sub-project, since
    every other test calls with at least one real community id.

    Mutation that would fail this: delete the guard (or just its `return []`).
    With community_sql left at its default (None) and community_ids == [], the
    very next branch taken indexes `community_ids[0]`
    (`elif community_ids[0] == -1:`, app/utils.py:3801), which raises IndexError
    on an empty list -- so removing the guard does not quietly change the return
    value, it crashes the call, and this test would fail with that error instead
    of a plain assertion failure.
    """
    make_instance('emptycommunities.example')
    viewer = make_user(None, 'emptycommunitiesviewer', local=True)

    with app.test_request_context('/'):
        login_user(viewer)
        assert get_deduped_post_ids(uuid.uuid4().hex, [], 'new') == []


def test_a_cached_result_id_is_served_without_reaching_the_database(app, db_session, redis_double):
    """Covers the Redis cache-HIT read path at app/utils.py:3794-3798 -- `if
    result_id: if redis_client.exists(result_id): return
    json.loads(redis_client.get(result_id))` -- which no earlier test in this
    sub-project exercised. Task 1's tests (above) proved only the cache-WRITE
    side of this same mechanism.

    Primes app.redis_client (via redis_double, so this never touches the real
    test Redis) at a result_id with a cached value that is NOT the id of a real,
    currently-visible post. A real post is also seeded in the same
    community. If get_deduped_post_ids fell through to the live query instead of
    taking the cache-hit branch, the live query would return [post.id] and the
    assertion below would fail -- so a test that merely checked "some list came
    back" would pass either way, and this one does not.

    Mutation that would fail this: delete the `if redis_client.exists(result_id):
    return json.loads(...)` block. The call would then fall through to the live
    query and return [post.id] instead of the primed stale value, failing the
    equality assertion.
    """
    make_instance('cachehit.example')
    viewer = make_user(None, 'cachehitviewer', local=True)
    community = make_community('cachehitcomm')
    make_community_member(viewer, community)
    post = make_post(community, viewer, 'https://cachehit.example/posts/1')

    result_id = uuid.uuid4().hex
    stale_cached_ids = [-999999]  # deliberately not post.id, and not a real post
    redis_double.set(result_id, json.dumps(stale_cached_ids), ex=86400)

    with app.test_request_context('/'):
        login_user(viewer)
        returned = get_deduped_post_ids(result_id, [community.id], 'new')

    assert returned == stale_cached_ids, (
        f'expected the primed cache value {stale_cached_ids} to be served as-is; '
        f'got {returned!r} instead -- the cache-hit branch (app/utils.py:3795-3796) '
        f'was not taken. A live query here would have returned [{post.id}]'
    )
    assert post.id not in returned


def test_an_authenticated_call_with_an_empty_result_id_still_writes_a_wasted_cache_entry(
        app, db_session, redis_double):
    """Confirms the design doc's suspected defect is LIVE, not merely
    theoretical: the cache READ is guarded by `if result_id:` (app/utils.py:3794)
    but the cache WRITE at the end of the function is guarded only by `if
    current_user.is_authenticated:` (app/utils.py:3942) -- an authenticated call
    with an empty result_id (the caller's own way of saying "do not cache") still
    performs the write, to a key literally named ''.

    Two real call sites pass a literal '' result_id: app/feed/routes.py:719
    (show_feed_rss) and app/topic/routes.py:230 (show_topic_rss). Neither route
    carries @login_required, so most requests are anonymous -- but neither
    excludes an authenticated session either (confirmed by reading both routes'
    full decorator stacks), so a logged-in browser opening either RSS URL
    reaches exactly this path. This test proves the write happens for an
    authenticated caller; it reports the defect, it does not fix it, per this
    campaign's report-don't-fix rule.

    Also closes the last uncovered branch in get_deduped_post_ids's own
    result_id handling, app/utils.py:3794->3798 -- the False arm of `if
    result_id:`, skipping the cache-hit check entirely for a falsy result_id --
    which the cache-hit test above does not reach (it uses a truthy result_id).
    """
    make_instance('emptyresultid.example')
    viewer = make_user(None, 'emptyresultidviewer', local=True)
    community = make_community('emptyresultidcomm')
    make_community_member(viewer, community)
    post = make_post(community, viewer, 'https://emptyresultid.example/posts/1')

    assert not redis_double.exists('')
    with app.test_request_context('/'):
        login_user(viewer)
        returned = get_deduped_post_ids('', [community.id], 'new')

    assert returned == [post.id]
    assert redis_double.exists(''), (
        "expected the wasted-write defect: an authenticated call with an empty "
        "result_id should still write a key literally named ''"
    )
