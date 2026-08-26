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
