"""Sub-project 5c, Task 6 -- FIX 4: Add's auto-subscribe loop runs without a
community.

The Add arm's feed branch (routes.py, current numbering ~1400-1423) is
reached when the activity's actor resolves to a Feed rather than a Community
or User (the Announce/Accept/Reject actor-resolution preamble at
routes.py:861-870), and the inner activity's `object` carries an `id`.

FeedItem creation is correctly guarded:

    if community_to_add and isinstance(community_to_add, Community):
        ...create the FeedItem, bump feed.num_communities, commit...

but the auto-subscribe loop directly below it was NOT inside that guard, and
unconditionally read `community_to_add.ap_id` to build the actor string
`do_subscribe` needs. When `community_to_add` is `None` (the community named
by the Add could not be resolved) or is some non-Community actor, that read
raises `AttributeError: 'NoneType' object has no attribute 'ap_id'` for any
feed that has at least one local member with `feed_auto_follow` set -- which
is the column's own default (`app/models.py:1033`), so an ordinary feed
member trips it.

The fix moves the loop (its comment, the `feed_members` query, and the
`for` body) one level in, under the same guard, since the loop's entire
purpose -- subscribing a feed's members to the community just added -- has
nothing to do when no community was added.

`do_subscribe` is imported inline inside the loop
(`from app.community.routes import do_subscribe`), so it does not exist as
an attribute of `app.activitypub.routes` -- `record_moderation` (which
patches names on that module) cannot double it. It is patched directly on
`app.community.routes`, where the inline import actually resolves it.
"""

import pytest

from app import db
from app.activitypub import routes as activitypub_routes
from app.models import ActivityPubLog, FeedItem, utcnow
from tests.factories import inbox_activity, make_feed, make_instance, make_user
from tests.test_inbox_dispatch_preamble import dispatch

import app.community.routes as community_routes


def _seed_feed_with_local_auto_follow_member(host='peer.example'):
    """A Feed reachable through the preamble's actor resolution (it is the
    request's `actor`), plus one local FeedMember whose `feed_auto_follow`
    is True (the column's own default) -- the minimum needed to reach the
    unguarded `community_to_add.ap_id` read.
    """
    instance = make_instance(host)
    feed = make_feed(instance)
    member = make_user(None, 'localmember', local=True)
    from tests.factories import make_feed_member
    make_feed_member(member, feed)
    return instance, feed, member


def test_an_add_whose_community_cannot_be_resolved_does_not_touch_feed_members(
        app, db_session, monkeypatch):
    """routes.py's Add/feed branch (current numbering ~1405-1423). The
    FeedItem creation is guarded by
    `if community_to_add and isinstance(community_to_add, Community)`, but
    (pre-fix) the feed_members loop below it is NOT -- and it reads
    `community_to_add.ap_id`, so an unresolvable community raises
    AttributeError once the feed has at least one local, auto-follow member.

    Asserts the corrected behaviour: no FeedItem is created, do_subscribe is
    never called, and no exception escapes. LOG_ACTIVITYPUB_TO_DB is turned
    on so the ActivityPubLog assertion is load-bearing (it defaults False,
    under which log_incoming_ap writes nothing regardless of the fix,
    making the same assertion vacuous) -- this branch of the Add arm calls
    no log_incoming_ap on any path, a registered finding this pins.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, feed, member = _seed_feed_with_local_auto_follow_member()
    assert member.is_local() and member.feed_auto_follow

    # Wrap, don't replace: only the Add arm's own re-lookup of the community
    # named in the Add (community_only=True, create_if_not_found defaulting
    # True) needs interception -- that is the one call that would otherwise
    # risk a real outbound fetch for an id this test deliberately makes
    # unresolvable. Both of the preamble's own probes (routes.py:862's
    # community_only=True/create_if_not_found=False miss, and :864's
    # feed_only=True/create_if_not_found=False hit) fall through to the real
    # find_actor_or_create_cached, so the preamble's feed lookup genuinely
    # resolves the seeded Feed row from the database rather than being
    # handed it by the double.
    real_find_actor_or_create_cached = activitypub_routes.find_actor_or_create_cached

    def _find(actor, create_if_not_found=True, community_only=False, feed_only=False):
        if create_if_not_found:  # the Add arm's re-lookup of an unresolvable id
            return None
        return real_find_actor_or_create_cached(
            actor, create_if_not_found=create_if_not_found,
            community_only=community_only, feed_only=feed_only)

    monkeypatch.setattr(activitypub_routes, 'find_actor_or_create_cached', _find)

    do_subscribe_calls = []
    monkeypatch.setattr(
        community_routes, 'do_subscribe',
        lambda *args, **kwargs: do_subscribe_calls.append((args, kwargs)))

    inner_add = {
        'id': f'{feed.ap_profile_id}/activities/add1',
        'type': 'Add',
        'actor': feed.ap_profile_id,
        'object': {'id': 'https://unresolvable.example/c/ghost'},
        'target': feed.ap_profile_id,
    }
    activity = inbox_activity(feed, activity_type='Announce', object=inner_add)

    dispatch(activity)

    assert FeedItem.query.count() == 0
    assert do_subscribe_calls == []
    assert ActivityPubLog.query.count() == 0
