"""Round 250: what a local account is told when a peer bans it, and two small helpers.

Three clusters in `app/activitypub/util.py`.

`ban_user`'s community branch has rows for the CommunityBan row, the membership flag and the
modlog entry (`tests/test_ap_moderation.py`). What it did not have rows for is everything the
BANNED PERSON experiences: the notification, its message, the join request that is withdrawn,
and the notification subscription that is removed. Those only happen for a LOCAL account --
a remote one is its own instance's problem -- and only when they have actually posted in the
community, which is a deliberate narrowing worth pinning.

`populate_child_feed_worker` resolves a feed on another instance and attaches it as a child of
one here, which is an outbound fetch driven by an id.

`log_incoming_ap` writes the inbox's audit trail. It takes an optional session, because half
its callers run inside a celery task with their own -- and the two arms commit to different
places.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.activitypub import util as ap_util
from app.constants import NOTIF_BAN
from app.models import (ActivityPubLog, CommunityBan, CommunityJoinRequest,
                        CommunityMember, Notification, NotificationSubscription, Site)
from tests.factories import (make_community, make_community_member, make_instance,
                             make_post, make_site, make_user, seed_community_owner)


@pytest.fixture
def scene(app, db_session):
    make_site()
    instance = seed_community_owner('peer.example')
    community = make_community(name='books', host='test.piefed.local')
    author = make_user(instance, 'contentauthor', local=True)
    moderator = make_user(instance, 'themoderator', local=True)
    make_community_member(moderator, community, is_moderator=True)
    db.session.commit()
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    return SimpleNamespace(app=app, instance=instance, community=community,
                           author=author, moderator=moderator)


def a_block(summary='spam'):
    activity = {'id': 'https://peer.example/activities/block/1'}
    if summary is not None:
        activity['summary'] = summary
    return activity


# --------------------------------------------------------------------------
# What the banned person is told
# --------------------------------------------------------------------------


class TestWhatABannedLocalAccountIsTold:

    def _ban(self, scene, summary='spam'):
        ap_util.ban_user(scene.moderator, scene.author, scene.community,
                         a_block(summary))

    def _notifications(self):
        return Notification.query.filter_by(notif_type=NOTIF_BAN).all()

    def test_somebody_who_has_posted_there_is_notified(self, scene):
        """`if community.has_poster(blocked)`. The notification names the community and links
        to the moderator chat, which is how the banned person can ask about it."""
        make_post(scene.community, scene.author, ap_id='https://test.piefed.local/p/1')
        db.session.commit()

        self._ban(scene)

        notification = self._notifications()[0]
        assert notification.user_id == scene.author.id
        assert notification.author_id == scene.moderator.id
        assert 'books' in notification.title
        assert notification.url == \
            f'/chat/ban_from_mod/{scene.author.id}/{scene.community.id}'
        assert notification.targets['community_id'] == scene.community.id

    def test_somebody_who_never_posted_there_is_not_notified(self, scene):
        """The narrowing, and the reason for it: a ban from a community somebody has never
        posted in is not news they asked for. The ban itself still happens."""
        self._ban(scene)

        assert self._notifications() == []
        assert CommunityBan.query.filter_by(user_id=scene.author.id,
                                            community_id=scene.community.id).count() == 1

    def test_the_unread_badge_goes_up_outside_debug(self, scene, monkeypatch):
        """`if not current_app.debug`. The comment says why -- incrementing it hangs the app
        when an activity is re-submitted from the admin UI in debug -- so the counter and the
        notification row are separate decisions and the row asserts both."""
        make_post(scene.community, scene.author, ap_id='https://test.piefed.local/p/1')
        db.session.commit()
        monkeypatch.setattr(scene.app, 'debug', False)
        before = scene.author.unread_notifications or 0

        self._ban(scene)

        db.session.refresh(scene.author)
        assert scene.author.unread_notifications == before + 1

    def test_the_badge_is_left_alone_in_debug(self, scene, monkeypatch):
        make_post(scene.community, scene.author, ap_id='https://test.piefed.local/p/1')
        db.session.commit()
        monkeypatch.setattr(scene.app, 'debug', True)
        before = scene.author.unread_notifications or 0

        self._ban(scene)

        db.session.refresh(scene.author)
        assert scene.author.unread_notifications == before
        assert self._notifications() != []

    def test_a_pending_join_request_is_withdrawn(self, scene):
        """A banned account must not still be queued for membership of the community that
        banned them -- otherwise an approval later would re-admit them."""
        db.session.add(CommunityJoinRequest(user_id=scene.author.id,
                                            community_id=scene.community.id))
        db.session.commit()

        self._ban(scene)

        assert CommunityJoinRequest.query.filter_by(
            user_id=scene.author.id, community_id=scene.community.id).count() == 0

    def test_their_notification_subscription_is_removed(self, scene):
        """Subscribed to the community's new posts, then banned from it: without this they
        keep being told about content they cannot reply to."""
        from app.constants import NOTIF_COMMUNITY

        db.session.add(NotificationSubscription(name='books', user_id=scene.author.id,
                                                entity_id=scene.community.id,
                                                type=NOTIF_COMMUNITY))
        db.session.commit()

        self._ban(scene)

        assert NotificationSubscription.query.filter_by(
            user_id=scene.author.id, entity_id=scene.community.id).count() == 0

    def test_a_remote_account_gets_none_of_it(self, scene):
        """`if blocked.is_local()`. A remote account's notifications, join requests and
        subscriptions live on its own instance; writing them here would be this instance
        keeping state about somebody else's user."""
        remote = make_user(scene.instance, 'faraway')
        remote.ap_id = 'faraway@peer.example'
        remote.ap_profile_id = 'https://peer.example/u/faraway'
        db.session.commit()
        make_post(scene.community, remote, ap_id='https://peer.example/p/1')
        db.session.add(CommunityJoinRequest(user_id=remote.id,
                                            community_id=scene.community.id))
        db.session.commit()

        ap_util.ban_user(scene.moderator, remote, scene.community, a_block())

        assert Notification.query.filter_by(notif_type=NOTIF_BAN).count() == 0
        assert CommunityJoinRequest.query.filter_by(user_id=remote.id).count() == 1
        # The ban itself is still recorded -- that part is ours to keep.
        assert CommunityBan.query.filter_by(user_id=remote.id).count() == 1

    def test_a_block_with_no_summary_bans_with_no_reason(self, scene):
        """`else: reason = ''`. The `summary` is optional in the activity, and `None` in a
        `String(255)` column read by the modlog would render as the word None."""
        self._ban(scene, summary=None)

        ban = CommunityBan.query.filter_by(user_id=scene.author.id).one()
        assert ban.reason == ''


# --------------------------------------------------------------------------
# Attaching a feed from another instance
# --------------------------------------------------------------------------


class TestAttachingARemoteFeedAsAChild:
    """`populate_child_feed_worker` takes a handle like `~name@host`, resolves the feed on
    that instance, and records it as a child of a feed here.
    """

    def test_the_resolved_feed_is_attached_to_the_parent(self, scene):
        from app.models import Feed

        parent = Feed(name='parent', title='Parent', instance_id=1,
                      ap_profile_id='https://test.piefed.local/f/parent',
                      ap_public_url='https://test.piefed.local/f/parent')
        child = Feed(name='child', title='Child', instance_id=1,
                     ap_profile_id='https://peer.example/f/child',
                     ap_public_url='https://peer.example/f/child')
        db.session.add_all([parent, child])
        db.session.commit()

        parent_id, child_id = parent.id, child.id

        with patch('app.feed.util.search_for_feed', return_value=child):
            ap_util.populate_child_feed_worker(parent_id, '~child@peer.example')

        # The worker ends with `db.session.remove()`, which detaches every object this test
        # was holding -- so the row is read back by id rather than refreshed.
        assert db.session.get(Feed, child_id).parent_feed_id == parent_id

    def test_a_feed_that_cannot_be_resolved_raises(self, scene):
        """`except Exception: rollback; raise`. `search_for_feed` answers None for a handle
        it cannot resolve, and `None.parent_feed_id` is an AttributeError -- so the task
        fails loudly rather than silently attaching nothing, which is what a celery retry
        needs."""
        with patch('app.feed.util.search_for_feed', return_value=None):
            with pytest.raises(AttributeError):
                ap_util.populate_child_feed_worker(1, '~nothing@peer.example')

    def test_the_dispatcher_runs_inline_in_debug(self, scene, monkeypatch):
        monkeypatch.setattr(scene.app, 'debug', True)
        ran = []

        with patch.object(ap_util, 'populate_child_feed_worker') as worker:
            worker.side_effect = lambda *args: ran.append(args)
            ap_util.populate_child_feed(7, '~child@peer.example')

            assert worker.delay.call_count == 0

        assert ran == [(7, '~child@peer.example')]

    def test_the_dispatcher_queues_in_production(self, scene, monkeypatch):
        monkeypatch.setattr(scene.app, 'debug', False)
        queued = []

        with patch.object(ap_util, 'populate_child_feed_worker') as worker:
            worker.delay.side_effect = lambda *args: queued.append(args)
            ap_util.populate_child_feed(7, '~child@peer.example')

        assert queued == [(7, '~child@peer.example')]


# --------------------------------------------------------------------------
# The inbox's audit trail
# --------------------------------------------------------------------------


class TestWritingTheInboxAuditTrail:
    """`log_incoming_ap` is the record of what a peer sent and what this instance did with
    it. It is the thing an operator reads when a peer complains that something did not
    arrive, so the two things that matter are that the row is written at all and that it goes
    into the caller's session when there is one.
    """

    def test_a_row_is_written_with_the_type_and_the_result(self, scene, monkeypatch):
        from app.constants import APLOG_CREATE, APLOG_SUCCESS

        monkeypatch.setitem(scene.app.config, 'LOG_ACTIVITYPUB_TO_DB', True)

        ap_util.log_incoming_ap('https://peer.example/activities/1', APLOG_CREATE,
                                APLOG_SUCCESS, None)

        row = ActivityPubLog.query.one()
        assert row.activity_id == 'https://peer.example/activities/1'
        assert row.result == 'success'
        assert row.direction == 'in'
        # `if saved_json:` -- nothing was passed, so nothing is stored. Without this a
        # mutant that always stored would write the string 'null' into the column.
        assert row.activity_json is None

    def test_a_failure_message_is_recorded(self, scene, monkeypatch):
        from app.constants import APLOG_CREATE, APLOG_FAILURE

        monkeypatch.setitem(scene.app.config, 'LOG_ACTIVITYPUB_TO_DB', True)

        ap_util.log_incoming_ap('https://peer.example/activities/1', APLOG_CREATE,
                                APLOG_FAILURE, None, 'the community is local only')

        assert ActivityPubLog.query.one().exception_message == \
            'the community is local only'

    def test_the_activity_body_is_stored_when_the_caller_passes_it(self, scene,
                                                                  monkeypatch):
        """`if saved_json:` -- the body is kept only when the caller asked for it, because
        storing every activity is a lot of rows and some of them are private messages."""
        from app.constants import APLOG_CREATE, APLOG_SUCCESS

        monkeypatch.setitem(scene.app.config, 'LOG_ACTIVITYPUB_TO_DB', True)

        ap_util.log_incoming_ap('https://peer.example/activities/1', APLOG_CREATE,
                                APLOG_SUCCESS, {'type': 'Create'})

        assert '"type": "Create"' in ActivityPubLog.query.one().activity_json

    def test_a_callers_own_session_is_used_when_given(self, scene, monkeypatch):
        """The `if session:` arm. Half the callers run inside a celery task with their own
        session, and committing to `db.session` from there writes into a transaction nobody
        is going to finish."""
        from app.constants import APLOG_CREATE, APLOG_SUCCESS

        monkeypatch.setitem(scene.app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
        added = []

        class Recording:
            def add(self, row):
                added.append(row)

            def commit(self):
                added.append('commit')

        ap_util.log_incoming_ap('https://peer.example/activities/1', APLOG_CREATE,
                                APLOG_SUCCESS, None, session=Recording())

        assert added[-1] == 'commit'
        assert isinstance(added[0], ActivityPubLog)
        # Nothing reached the request-scoped session.
        assert ActivityPubLog.query.count() == 0

    def test_nothing_is_written_when_db_logging_is_off(self, scene, monkeypatch):
        """The default on most instances: the log is a diagnostic, and an instance that does
        not want the rows gets none."""
        from app.constants import APLOG_CREATE, APLOG_SUCCESS

        monkeypatch.setitem(scene.app.config, 'LOG_ACTIVITYPUB_TO_DB', False)

        ap_util.log_incoming_ap('https://peer.example/activities/1', APLOG_CREATE,
                                APLOG_SUCCESS, None)

        assert ActivityPubLog.query.count() == 0

    def test_file_logging_is_a_separate_switch(self, scene, monkeypatch):
        """`LOG_ACTIVITYPUB_TO_FILE` is read after the database block and independently of
        it, so an instance can log to the file without keeping any rows."""
        from app.constants import APLOG_CREATE, APLOG_SUCCESS

        monkeypatch.setitem(scene.app.config, 'LOG_ACTIVITYPUB_TO_DB', False)
        monkeypatch.setitem(scene.app.config, 'LOG_ACTIVITYPUB_TO_FILE', True)
        logged = []
        monkeypatch.setattr(scene.app.logger, 'info',
                            lambda message, *args, **kwargs: logged.append(message))

        ap_util.log_incoming_ap('https://peer.example/activities/1', APLOG_CREATE,
                                APLOG_SUCCESS, None)

        assert any('https://peer.example/activities/1' in line for line in logged)
        assert ActivityPubLog.query.count() == 0


class TestUnbanningWithNoStatedReason:

    def test_an_undo_with_no_summary_unbans_with_an_empty_reason(self, scene):
        """`unban_user`'s `else: reason = ''`. The reason reaches `add_to_modlog`, so `None`
        there would render as the word None in the public modlog -- and an Undo/Block
        carrying no `summary` is the ordinary shape, since the reason was given when the ban
        was made."""
        from app.models import ModLog

        make_community_member(scene.author, scene.community)
        db.session.commit()
        ap_util.ban_user(scene.moderator, scene.author, scene.community, a_block())

        ap_util.unban_user(scene.moderator, scene.author, scene.community,
                           {'id': 'https://peer.example/activities/undo/1',
                            'object': {'id': 'https://peer.example/activities/block/1'}})

        assert CommunityBan.query.filter_by(user_id=scene.author.id,
                                            community_id=scene.community.id).count() == 0
        entry = ModLog.query.filter_by(action='unban_user').one()
        assert entry.reason == ''
