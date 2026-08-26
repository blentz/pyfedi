"""A microblog reaches an aggregate feed through a FOLLOW, never through a subscription.

These tests drive `app.utils.get_deduped_post_ids` itself -- the function the
subscribed feed, the topic feeds, the user feeds and the home RSS route all call --
rather than a copy of its SQL, so a restructure of the WHERE clause cannot pass them
by. The observable behaviour is the list of post ids the function returns.

Background: `Post.private` is the microblog marker, set by `Post.new()`
(app/models.py ~1796) for any object with no `name`. It is NOT a followers-only
flag; `create_post()` (app/activitypub/util.py ~2498) refuses `followers` and
`direct` visibility before `Post.new()` is ever reached, so no non-public object
becomes a Post at all. `tests/test_post_private_is_only_the_microblog_marker.py`
pins that.
"""

import uuid

import fakeredis
import pytest
from flask_login import login_user

import app as app_package
from app.activitypub.util import record_boost
from app.utils import get_deduped_post_ids
from tests.factories import (make_community, make_community_member, make_follow, make_instance,
                             make_post, make_user)


@pytest.fixture
def isolated_result_cache(monkeypatch):
    """get_deduped_post_ids memoises its result list into `app.redis_client`.

    That module-level client points at the shared test Redis, which survives every
    test and every run (see tests/README.md on `disable_rate_limiter`). Two tests
    that happened to pass the same result_id would otherwise read each other's
    answer instead of running the query. `get_deduped_post_ids` does
    `from app import redis_client` inside the function body, so rebinding the
    attribute on the package is enough -- it is re-read on every call.
    """
    monkeypatch.setattr(app_package, 'redis_client', fakeredis.FakeStrictRedis())


def subscribed_feed_ids(app, viewer, community):
    """The post ids the subscribed feed shows `viewer` for `community`.

    Mirrors app/main/routes.py:141 for view_filter == 'subscribed': the viewer's
    community memberships as community_ids, and include_following=True because the
    viewer is authenticated. A fresh result_id per call so nothing is served from
    the cache.
    """
    with app.test_request_context('/'):
        login_user(viewer)
        return get_deduped_post_ids(uuid.uuid4().hex, [community.id], 'new',
                                    include_following=True)


@pytest.fixture
def subscribed(db_session, isolated_result_cache):
    """A local user subscribed to a community that carries both kinds of post.

    The community is remote-flavoured on purpose: this reproduces
    microblogs@piefed.social, an ordinary federated community whose posts are
    ingested microblogs. find_microblogging_community() cannot reach it (it filters
    instance_id == 1), so the 'local' view's exclusion of the microblog community
    does not cover it.
    """
    remote = make_instance('m.example')
    author = make_user(remote, 'mastodonauthor')
    viewer = make_user(None, 'subscriber', local=True)
    community = make_community('aggregator')
    make_community_member(viewer, community)
    microblog = make_post(community, author, 'https://m.example/notes/1', microblog=True)
    ordinary = make_post(community, author, 'https://m.example/notes/2', title='an ordinary post')
    return viewer, community, author, microblog, ordinary


def test_ordinary_post_in_a_subscribed_community_is_in_the_feed(app, subscribed):
    """The control. Without this, "not in the feed" could mean the query returned nothing."""
    viewer, community, _, _, ordinary = subscribed

    assert ordinary.id in subscribed_feed_ids(app, viewer, community)


def test_microblog_is_not_in_the_feed_of_a_subscriber_who_follows_nobody(app, subscribed):
    """The reported bug: Mastodon posts in the subscribed feed of a user with no follows.

    The viewer is a member of the community that carries the post, and follows
    nobody. Subscription alone must not surface a microblog.
    """
    viewer, community, _, microblog, _ = subscribed

    assert microblog.id not in subscribed_feed_ids(app, viewer, community)


def test_microblog_is_in_the_feed_when_the_viewer_follows_its_author(app, subscribed):
    """The other direction. Following the author is what entitles you to see it."""
    viewer, community, author, microblog, _ = subscribed
    make_follow(viewer, author)

    assert microblog.id in subscribed_feed_ids(app, viewer, community)


def test_microblog_is_in_the_feed_when_the_viewer_follows_its_booster(app, subscribed):
    """Deliberate earlier behaviour: a boosted microblog appears via the booster.

    The boost disjunct carries no p.private gate, and must not acquire one -- gating
    it excluded every ingested Mastodon post from the feed, which is the regression
    tests/test_feed_boost_visibility.py exists to prevent.
    """
    viewer, community, _, microblog, _ = subscribed
    booster = make_user(make_instance('booster.example'), 'booster')
    make_follow(viewer, booster)
    record_boost(microblog, booster)

    assert microblog.id in subscribed_feed_ids(app, viewer, community)


def test_a_follow_does_not_open_microblogs_by_other_authors(app, subscribed):
    """The follow entitlement is per-author, not a blanket switch.

    Without this, moving the gate onto the community disjunct could be faked by
    dropping it whenever the viewer follows anyone at all -- which is the shape of
    the bug being fixed, just with a follow count of one instead of zero.
    """
    viewer, community, _, microblog, _ = subscribed
    someone_else = make_user(make_instance('elsewhere.example'), 'someoneelse')
    make_follow(viewer, someone_else)
    other_microblog = make_post(community, someone_else, 'https://elsewhere.example/notes/9',
                                microblog=True)

    ids = subscribed_feed_ids(app, viewer, community)

    assert other_microblog.id in ids       # its author is followed
    assert microblog.id not in ids         # this one's author is not


def test_microblog_stays_out_of_a_feed_that_does_not_include_following(app, subscribed):
    """Topic feeds and /feed pass include_following=False; they must be unaffected."""
    viewer, community, author, microblog, ordinary = subscribed
    make_follow(viewer, author)

    with app.test_request_context('/'):
        login_user(viewer)
        ids = get_deduped_post_ids(uuid.uuid4().hex, [community.id], 'new')

    assert ordinary.id in ids
    assert microblog.id not in ids


def test_subscribed_feed_runs_for_a_viewer_with_no_follows_at_all(app, subscribed):
    """Parameter binding: :local_user_id is only bound when the follow disjuncts are added.

    A restructured WHERE that referenced :local_user_id outside them would raise
    here rather than return a list. `num_following` is 0 for this viewer, so
    get_deduped_post_ids appends neither the follow nor the boost disjunct.
    """
    viewer, community, _, _, ordinary = subscribed
    assert viewer.num_following == 0

    assert subscribed_feed_ids(app, viewer, community) == [ordinary.id]
