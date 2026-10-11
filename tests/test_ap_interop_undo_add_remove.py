"""Interop C1, item 3 (Add/Remove): an Undo of an Add runs the Remove, and an Undo of a
Remove runs the Add -- for a sticky post, a moderator, and a community in a feed.

Before this change the Undo dispatch had no Add or Remove arm: both fell through to
'Unmatched activity' and the change stayed. A feed's Announce of such an Undo did not even
get that far: the D115 gate refused it because it named no user or community.

The authority is exactly the original handlers': a moderator of the community or an admin
of its instance (`may_moderate`), and for a feed, the feed's own signed Announce. An Undo
whose target is already in the state it asks for is a logged no-op.

The undone activity is the dict inside the Undo. An Undo whose object is a bare URI was
already refused by the Undo arm (D112) and still is: nothing is fetched to find out what
it named.
"""
import pytest

from app import db
from app.activitypub.routes import may_moderate
from app.models import ActivityPubLog, CommunityMember, FeedItem, Post, utcnow
from tests.factories import inbox_activity, make_community, make_feed, make_feed_item, make_instance, make_post, \
    make_user, make_community_member
from tests.test_inbox_dispatch_add_remove import _seed_community_with_mod_and_admin
from tests.test_inbox_dispatch_lock_delete import record_moderation
from tests.test_inbox_dispatch_preamble import dispatch


def undo_from_community(community, actor, inner_type, object_value, target):
    """An Announce by `community` of `actor`'s Undo of an Add or Remove -- the shape Lemmy
    and PieFed use for every moderation activity in a community."""
    inner = {'id': f'{community.ap_profile_id}/activities/{inner_type.lower()}-1', 'type': inner_type,
             'actor': actor.ap_profile_id, 'object': object_value, 'target': target}
    undo = {'id': f'{community.ap_profile_id}/activities/undo-1', 'type': 'Undo',
            'actor': actor.ap_profile_id, 'object': inner}
    return inbox_activity(community, activity_type='Announce', object=undo)


def undo_from_feed(feed, inner_type, community):
    """A feed's Announce of its own Undo of an Add or Remove of `community`, the shape the feed
    Add/Remove tests in tests/test_inbox_dispatch_add_remove.py use for the plain activity."""
    inner = {'id': f'{feed.ap_profile_id}/activities/{inner_type.lower()}-1', 'type': inner_type,
             'actor': feed.ap_profile_id, 'object': {'id': community.ap_profile_id},
             'target': feed.ap_profile_id}
    undo = {'id': f'{feed.ap_profile_id}/activities/undo-1', 'type': 'Undo',
            'actor': feed.ap_profile_id, 'object': inner}
    return inbox_activity(feed, activity_type='Announce', object=undo)


def only_log():
    log = ActivityPubLog.query.one()
    return log.activity_type, log.result, log.exception_message


@pytest.fixture
def modcomm(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, moderator, admin = _seed_community_with_mod_and_admin(name='undocomm')
    community.ap_moderators_url = community.ap_profile_id + '/moderators'
    db.session.commit()
    author = make_user(instance, 'postauthor')
    post = make_post(community, author, ap_id='https://peer.example/post/7')
    return instance, community, moderator, admin, post


@pytest.fixture
def feedcomm(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    make_user(instance, 'community_owner')  # occupies user id=1 for make_community's hardcoded owner
    community = make_community(name='feedcomm', host='peer.example')
    community.ap_fetched_at = utcnow()
    db.session.commit()
    feed = make_feed(instance)
    feed.num_communities = 3
    db.session.commit()
    return community, feed


def test_may_moderate_is_a_moderator_or_an_instance_admin(modcomm):
    instance, community, moderator, admin, post = modcomm
    outsider = make_user(instance, 'outsider')
    assert may_moderate(community, moderator) is True
    assert may_moderate(community, admin) is True
    assert may_moderate(community, outsider) is False


# --- sticky ---------------------------------------------------------------------------

def test_an_undo_of_an_add_to_featured_unsticks_the_post(modcomm):
    instance, community, moderator, admin, post = modcomm
    post.sticky = True
    db.session.commit()

    dispatch(undo_from_community(community, moderator, 'Add', post.ap_id, community.ap_profile_id + '/featured'))

    db.session.expire_all()
    assert db.session.get(Post, post.id).sticky is False
    assert only_log() == ('Remove', 'success', None)


def test_an_undo_of_a_remove_from_featured_sticks_the_post_again(modcomm):
    instance, community, moderator, admin, post = modcomm

    dispatch(undo_from_community(community, admin, 'Remove', post.ap_id, community.ap_profile_id + '/featured'))

    db.session.expire_all()
    assert db.session.get(Post, post.id).sticky is True
    assert only_log() == ('Add', 'success', None)


def test_an_undo_of_an_add_to_featured_of_a_post_that_is_not_sticky_is_a_logged_no_op(modcomm):
    instance, community, moderator, admin, post = modcomm

    dispatch(undo_from_community(community, moderator, 'Add', post.ap_id, community.ap_profile_id + '/featured'))

    db.session.expire_all()
    assert db.session.get(Post, post.id).sticky is False
    assert only_log() == ('Remove', 'ignored', 'Undo: post is not sticky')


def test_an_undo_of_a_remove_from_featured_of_a_sticky_post_is_a_logged_no_op(modcomm):
    instance, community, moderator, admin, post = modcomm
    post.sticky = True
    db.session.commit()

    dispatch(undo_from_community(community, moderator, 'Remove', post.ap_id, community.ap_profile_id + '/featured'))

    db.session.expire_all()
    assert db.session.get(Post, post.id).sticky is True
    assert only_log() == ('Add', 'ignored', 'Undo: post is already sticky')


def test_an_undo_by_someone_without_authority_changes_nothing(modcomm):
    """The original handlers' check, unchanged: neither a moderator nor an instance admin."""
    instance, community, moderator, admin, post = modcomm
    post.sticky = True
    db.session.commit()
    outsider = make_user(instance, 'outsider')
    outsider.ap_fetched_at = utcnow()
    db.session.commit()

    dispatch(undo_from_community(community, outsider, 'Add', post.ap_id, community.ap_profile_id + '/featured'))

    db.session.expire_all()
    assert db.session.get(Post, post.id).sticky is True
    assert only_log() == ('Remove', 'failure', 'Does not have permission')


# --- moderators -----------------------------------------------------------------------

def test_an_undo_of_an_add_mod_demotes_and_writes_the_modlog(modcomm, monkeypatch):
    instance, community, moderator, admin, post = modcomm
    calls = record_moderation(monkeypatch, 'add_to_modlog')
    promoted = make_user(instance, 'promoted')
    promoted.ap_fetched_at = utcnow()
    make_community_member(promoted, community, is_moderator=True)

    dispatch(undo_from_community(community, admin, 'Add', promoted.ap_profile_id, community.ap_moderators_url))

    membership = CommunityMember.query.filter_by(community_id=community.id, user_id=promoted.id).one()
    assert membership.is_moderator is False
    assert [args[0] for args, kwargs in calls['add_to_modlog']] == ['remove_mod']
    assert only_log() == ('Remove', 'success', None)


def test_an_undo_of_a_remove_mod_promotes_again_and_writes_the_modlog(modcomm, monkeypatch):
    instance, community, moderator, admin, post = modcomm
    calls = record_moderation(monkeypatch, 'add_to_modlog')
    demoted = make_user(instance, 'demoted')
    demoted.ap_fetched_at = utcnow()
    make_community_member(demoted, community, is_moderator=False)

    dispatch(undo_from_community(community, moderator, 'Remove', demoted.ap_profile_id, community.ap_moderators_url))

    membership = CommunityMember.query.filter_by(community_id=community.id, user_id=demoted.id).one()
    assert membership.is_moderator is True
    assert [args[0] for args, kwargs in calls['add_to_modlog']] == ['add_mod']
    assert only_log() == ('Add', 'success', None)


@pytest.mark.parametrize('membership', ['none', 'member'])
def test_an_undo_of_an_add_mod_for_someone_not_a_moderator_is_a_logged_no_op(modcomm, monkeypatch, membership):
    instance, community, moderator, admin, post = modcomm
    calls = record_moderation(monkeypatch, 'add_to_modlog')
    someone = make_user(instance, 'someone')
    someone.ap_fetched_at = utcnow()
    db.session.commit()
    if membership == 'member':
        make_community_member(someone, community, is_moderator=False)

    dispatch(undo_from_community(community, moderator, 'Add', someone.ap_profile_id, community.ap_moderators_url))

    assert calls['add_to_modlog'] == []
    assert only_log() == ('Remove', 'ignored', 'Undo: not a moderator')


def test_an_undo_of_a_remove_mod_for_a_moderator_is_a_logged_no_op(modcomm, monkeypatch):
    instance, community, moderator, admin, post = modcomm
    calls = record_moderation(monkeypatch, 'add_to_modlog')
    still_mod = make_user(instance, 'stillmod')
    still_mod.ap_fetched_at = utcnow()
    make_community_member(still_mod, community, is_moderator=True)

    dispatch(undo_from_community(community, admin, 'Remove', still_mod.ap_profile_id, community.ap_moderators_url))

    assert calls['add_to_modlog'] == []
    assert only_log() == ('Add', 'ignored', 'Undo: already a moderator')


# --- feeds ----------------------------------------------------------------------------

def test_a_feeds_undo_of_an_add_takes_the_community_out_of_the_feed(feedcomm):
    community, feed = feedcomm
    make_feed_item(feed, community)

    dispatch(undo_from_feed(feed, 'Add', community))

    db.session.expire_all()
    assert FeedItem.query.filter_by(feed_id=feed.id, community_id=community.id).count() == 0
    assert feed.num_communities == 2
    assert only_log() == ('Remove', 'success', None)


def test_a_feeds_undo_of_a_remove_puts_the_community_back(feedcomm):
    community, feed = feedcomm

    dispatch(undo_from_feed(feed, 'Remove', community))

    db.session.expire_all()
    assert FeedItem.query.filter_by(feed_id=feed.id, community_id=community.id).count() == 1
    assert feed.num_communities == 4
    assert only_log() == ('Add', 'success', None)


def test_a_feeds_undo_of_a_remove_of_a_community_still_in_the_feed_is_a_logged_no_op(feedcomm):
    """A plain Add of a community already in the feed inserts a second FeedItem; an Undo must
    not."""
    community, feed = feedcomm
    make_feed_item(feed, community)

    dispatch(undo_from_feed(feed, 'Remove', community))

    db.session.expire_all()
    assert FeedItem.query.filter_by(feed_id=feed.id, community_id=community.id).count() == 1
    assert feed.num_communities == 3
    assert only_log() == ('Add', 'ignored', 'Undo: community is already in feed')


def test_a_feeds_undo_of_an_add_of_a_community_not_in_the_feed_is_a_logged_no_op(feedcomm):
    """Ignored and returned: the auto-unsubscribe loop that follows a real Remove must not run
    for a removal that did not happen."""
    community, feed = feedcomm

    dispatch(undo_from_feed(feed, 'Add', community))

    db.session.expire_all()
    assert feed.num_communities == 3
    assert only_log() == ('Remove', 'ignored', 'Community is not in feed')


def test_a_feeds_announce_of_an_undo_of_something_else_is_still_refused(feedcomm):
    """The D115 gate is widened for an Undo of an Add or Remove only."""
    community, feed = feedcomm
    undo = {'id': f'{feed.ap_profile_id}/activities/undo-2', 'type': 'Undo', 'actor': feed.ap_profile_id,
            'object': {'id': f'{feed.ap_profile_id}/activities/like-1', 'type': 'Like',
                       'actor': feed.ap_profile_id, 'object': 'https://peer.example/post/1'}}

    dispatch(inbox_activity(feed, activity_type='Announce', object=undo))

    assert only_log() == ('Announce', 'failure', 'Undo announced by a feed has no user or community')
