"""The community API's moderation half: bans, unbans, the ban listing, the
NSFW switch, the moderator list and community flair.

Sub-project 84, slice J -- the moderating half of
`app/api/alpha/utils/community.py`. Four defects, all measured first:

* five endpoints looked a row up by id and then read an attribute off the
  answer without checking it, so naming a community, account or post that
  does not exist was `NoResultFound: No row was found when one was required`
  or `'NoneType' object has no attribute 'is_owner'` -- logged with a
  traceback and reported to Sentry, for a caller who simply typed a wrong
  id (D1202);
* a ban's expiry was compared against `datetime.now()`, the host's LOCAL
  clock, while `ban_until` holds UTC -- so on any instance whose clock is
  not set to UTC the whole ban window was displaced by the UTC offset, in
  the unsafe direction east of Greenwich: a banned account was reported
  unbanned, and a genuinely future expiry was refused as being in the past
  (D1203);
* `expires_at` was parsed with one hardcoded format string, so the ordinary
  whole-second (`2030-01-01T00:00:00Z`) and numeric-offset
  (`2030-01-01T00:00:00+00:00`) spellings of the same instant came back as
  `time data ... does not match format '%Y-%m-%dT%H:%M:%S.%fZ'` (D1204);
* flair creation checked for a duplicate using the UNstripped title and the
  colours, but stored the title stripped -- so ' news ', and 'news' in a
  different colour, both created a second flair with the same name (D1205).
"""
import os
import time
from datetime import datetime, timedelta

import pytest
from dateutil.relativedelta import relativedelta
from flask import current_app, g

from app import db
from app.api.alpha.utils.community import (a_ban_expiry,
                                           get_community_moderate_bans,
                                           post_community_flair_create,
                                           post_community_flair_delete,
                                           post_community_mod,
                                           post_community_moderate_ban,
                                           post_community_moderate_post_nsfw,
                                           put_community_flair_edit,
                                           put_community_moderate_unban)
from app.constants import NOTIF_BAN, NOTIF_COMMUNITY, NOTIF_UNBAN
from app.models import (CommunityBan, CommunityFlair, CommunityJoinRequest,
                        CommunityMember, Notification, NotificationSubscription,
                        Post, Site, User, utcnow)
from tests.factories import (make_community, make_community_ban,
                             make_community_flair,
                             make_community_join_request, make_community_member,
                             make_post, make_post_flair, make_user)


def token(user):
    return f'Bearer {user.encode_jwt_token()}'


class east_of_utc:
    """The process clock moved nine hours east, without tzdata.

    `JST-9` is a POSIX TZ string -- a name and the offset that has to be ADDED
    to local time to get UTC -- so it needs no zoneinfo files to be installed
    in the image. Every test using this asserts the shift actually took, so it
    can never pass vacuously on a host where the environment variable is
    ignored.
    """

    def __enter__(self):
        self.previous = os.environ.get('TZ')
        os.environ['TZ'] = 'JST-9'
        time.tzset()
        shift = (datetime.now() - utcnow()).total_seconds()
        assert 8.9 * 3600 < shift < 9.1 * 3600, shift
        return self

    def __exit__(self, *exc):
        if self.previous is None:
            del os.environ['TZ']
        else:
            os.environ['TZ'] = self.previous
        time.tzset()
        return False


@pytest.fixture
def env(app, api_baseline):
    """A LOCAL community: every moderation endpoint below refuses to act on a
    community this instance does not host.

    `api_baseline.user1` is id 1, which `User.is_admin` answers True for
    unconditionally -- so it is this file's admin, never its "some account
    with no standing here".
    """
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('probeland')
    moderator = api_baseline.user2
    stranger = api_baseline.user3
    target = api_baseline.user4
    make_community_member(moderator, community, is_moderator=True)
    make_community_member(target, community)
    return SimpleNamespace(community=community, moderator=moderator,
                           stranger=stranger, target=target,
                           admin=api_baseline.user1,
                           remote_community=api_baseline.community1)


# ---------------------------------------------------------------- the listing

class TestBanListing:
    def test_a_community_nobody_holds_is_refused_by_name(self, env):
        """D1202. `Community.query.filter_by(id=...).one()` answered
        NoResultFound, which the API's shared error handler logs with a
        traceback and reports to Sentry before handing the caller "No row was
        found when one was required"."""
        with pytest.raises(Exception, match='community not found'):
            get_community_moderate_bans(token(env.admin),
                                        {'community_id': 999999})

    def test_an_account_with_no_standing_cannot_read_the_bans(self, env):
        with pytest.raises(Exception, match='incorrect_login'):
            get_community_moderate_bans(token(env.stranger),
                                        {'community_id': env.community.id})

    def test_a_community_hosted_elsewhere_is_refused(self, env):
        make_community_member(env.moderator, env.remote_community,
                              is_moderator=True)
        with pytest.raises(Exception, match='not local'):
            get_community_moderate_bans(
                token(env.moderator),
                {'community_id': env.remote_community.id})

    def test_an_admin_who_moderates_nothing_may_still_read_the_bans(self, env):
        make_community_ban(env.target, env.community, banned_by=env.moderator,
                           reason='spam')
        res = get_community_moderate_bans(token(env.admin),
                                          {'community_id': env.community.id})
        assert [item['reason'] for item in res['items']] == ['spam']

    def test_a_permanent_ban_has_no_expiry_at_all(self, env):
        make_community_ban(env.target, env.community, banned_by=env.moderator,
                           reason='forever', ban_until=None)
        item = get_community_moderate_bans(
            token(env.moderator), {'community_id': env.community.id})['items'][0]
        assert item['expired'] is False
        assert item['expires_at'] is None
        assert item['expired_at'] is None

    def test_a_ban_whose_time_has_passed_reports_when_it_ran_out(self, env):
        make_community_ban(env.target, env.community, banned_by=env.moderator,
                           reason='old', ban_until=datetime(2000, 1, 1))
        item = get_community_moderate_bans(
            token(env.moderator), {'community_id': env.community.id})['items'][0]
        assert item['expired'] is True
        assert item['expired_at'] == '2000-01-01T00:00:00.000000Z'

    def test_a_running_ban_reports_when_it_will_run_out(self, env):
        until = utcnow() + timedelta(days=2)
        make_community_ban(env.target, env.community, banned_by=env.moderator,
                           reason='two days', ban_until=until)
        item = get_community_moderate_bans(
            token(env.moderator), {'community_id': env.community.id})['items'][0]
        assert item['expired'] is False
        assert item['expires_at'] == until.isoformat(timespec='microseconds') + 'Z'

    def test_a_running_ban_is_not_reported_expired_east_of_greenwich(self, env):
        """D1203. `cb.ban_until < datetime.now()` read the HOST's clock while
        `ban_until` holds UTC. Nine hours east, a ban with four hours left on
        it was reported to its moderators as already expired."""
        make_community_ban(env.target, env.community, banned_by=env.moderator,
                           reason='four hours',
                           ban_until=utcnow() + timedelta(hours=4))
        with east_of_utc():
            item = get_community_moderate_bans(
                token(env.moderator),
                {'community_id': env.community.id})['items'][0]
        assert item['expired'] is False

    def test_the_listing_pages(self, env):
        for name in ('one', 'two', 'three'):
            make_community_ban(make_user(None, name, local=True), env.community,
                               banned_by=env.moderator, reason=name)
        first = get_community_moderate_bans(
            token(env.moderator), {'community_id': env.community.id, 'limit': 2})
        assert len(first['items']) == 2
        assert first['next_page'] == '2'
        second = get_community_moderate_bans(
            token(env.moderator),
            {'community_id': env.community.id, 'limit': 2, 'page': 2})
        assert len(second['items']) == 1
        assert second['next_page'] is None

    def test_a_limit_beyond_the_configured_page_length_is_clamped(self, env,
                                                                  monkeypatch):
        # monkeypatch, not assignment: the `app` fixture is session-scoped, so
        # a config key written directly here stays written for every test that
        # runs after this one.
        monkeypatch.setitem(current_app.config, 'PAGE_LENGTH', 2)
        for name in ('one', 'two', 'three'):
            make_community_ban(make_user(None, name, local=True), env.community,
                               banned_by=env.moderator, reason=name)
        res = get_community_moderate_bans(
            token(env.moderator),
            {'community_id': env.community.id, 'limit': 500})
        assert len(res['items']) == 2


# ------------------------------------------------------------------ banning

class TestBan:
    def base(self, env, **extra):
        data = {'user_id': env.target.id, 'community_id': env.community.id,
                'reason': 'spam'}
        data.update(extra)
        return data

    def test_an_account_nobody_holds_is_refused_by_name(self, env):
        """D1202. `db.session.get(User, ...)` answers None, and the next line
        read `blocked.id` off it."""
        with pytest.raises(Exception, match='user not found'):
            post_community_moderate_ban(token(env.moderator),
                                        self.base(env, user_id=999999))

    def test_a_community_nobody_holds_is_refused_by_name(self, env):
        with pytest.raises(Exception, match='community not found'):
            post_community_moderate_ban(token(env.moderator),
                                        self.base(env, community_id=999999))

    def test_an_account_with_no_standing_cannot_ban(self, env):
        with pytest.raises(Exception, match='incorrect_login'):
            post_community_moderate_ban(token(env.stranger), self.base(env))

    def test_a_community_hosted_elsewhere_is_refused(self, env):
        make_community_member(env.moderator, env.remote_community,
                              is_moderator=True)
        with pytest.raises(Exception, match='not local'):
            post_community_moderate_ban(
                token(env.moderator),
                self.base(env, community_id=env.remote_community.id))

    def test_a_ban_with_no_expiry_given_runs_for_a_year(self, env):
        before = utcnow()
        res = post_community_moderate_ban(token(env.moderator), self.base(env))
        ban = CommunityBan.query.filter_by(user_id=env.target.id,
                                           community_id=env.community.id).one()
        assert timedelta(days=364) < ban.ban_until - before < timedelta(days=367)
        assert res['expired'] is False
        assert res['reason'] == 'spam'
        assert res['banned_user']['id'] == env.target.id
        assert res['banned_by']['id'] == env.moderator.id

    def test_the_default_year_is_counted_in_utc(self, env):
        """D1203. `datetime.now() + relativedelta(years=1)` wrote the host's
        LOCAL wall clock into a column holding UTC, so nine hours east every
        default ban ran nine hours and a year."""
        with east_of_utc():
            before = utcnow()
            post_community_moderate_ban(token(env.moderator), self.base(env))
        ban = CommunityBan.query.filter_by(user_id=env.target.id,
                                           community_id=env.community.id).one()
        expected = before + relativedelta(years=1)
        assert abs((ban.ban_until - expected).total_seconds()) < 5

    def test_a_permanent_ban_has_no_expiry(self, env):
        res = post_community_moderate_ban(token(env.moderator),
                                          self.base(env, permanent=True))
        assert res['expires_at'] is None
        ban = CommunityBan.query.filter_by(user_id=env.target.id,
                                           community_id=env.community.id).one()
        assert ban.ban_until is None

    def test_an_expiry_in_microseconds_is_taken(self, env):
        res = post_community_moderate_ban(
            token(env.moderator),
            self.base(env, expires_at='2030-01-01T00:00:00.000000Z'))
        assert res['expires_at'] == '2030-01-01T00:00:00.000000Z'

    def test_an_expiry_in_whole_seconds_is_taken(self, env):
        """D1204. The only accepted spelling was the one this module emits."""
        res = post_community_moderate_ban(
            token(env.moderator),
            self.base(env, expires_at='2030-01-01T00:00:00Z'))
        assert res['expires_at'] == '2030-01-01T00:00:00.000000Z'

    def test_an_expiry_carrying_an_offset_is_converted_to_utc(self, env):
        """D1204."""
        res = post_community_moderate_ban(
            token(env.moderator),
            self.base(env, expires_at='2030-01-01T09:00:00+09:00'))
        assert res['expires_at'] == '2030-01-01T00:00:00.000000Z'

    def test_an_expiry_in_the_past_is_refused(self, env):
        with pytest.raises(Exception, match='must be a time in the future'):
            post_community_moderate_ban(
                token(env.moderator),
                self.base(env, expires_at='2000-01-01T00:00:00.000000Z'))

    def test_an_expiry_hours_away_is_not_refused_east_of_greenwich(self, env):
        """D1203. `ban_until < datetime.now()` read the HOST's clock. Nine
        hours east, every expiry less than nine hours out was refused as
        being in the past -- including the one this endpoint had just
        offered as the current time, which it printed with `utcnow()`."""
        expires_at = (utcnow() + timedelta(hours=4)).isoformat(
            timespec='microseconds') + 'Z'
        with east_of_utc():
            res = post_community_moderate_ban(
                token(env.moderator), self.base(env, expires_at=expires_at))
        assert res['expires_at'] == expires_at

    def test_an_expiry_that_is_not_a_timestamp_is_refused_by_name(self, env):
        """D1204."""
        with pytest.raises(Exception, match='not an ISO 8601 timestamp'):
            post_community_moderate_ban(token(env.moderator),
                                        self.base(env, expires_at='next week'))

    def test_banning_again_moves_the_expiry(self, env):
        make_community_ban(env.target, env.community, banned_by=env.moderator,
                           reason='first', ban_until=datetime(2029, 1, 1))
        post_community_moderate_ban(
            token(env.moderator),
            self.base(env, expires_at='2030-01-01T00:00:00.000000Z'))
        ban = CommunityBan.query.filter_by(user_id=env.target.id,
                                           community_id=env.community.id).one()
        assert ban.ban_until == datetime(2030, 1, 1)
        assert ban.reason == 'first'   # the existing row keeps its reason

    def test_banning_again_with_the_same_expiry_leaves_the_row_alone(self, env):
        make_community_ban(env.target, env.community, banned_by=env.moderator,
                           reason='first', ban_until=datetime(2030, 1, 1))
        post_community_moderate_ban(
            token(env.moderator),
            self.base(env, expires_at='2030-01-01T00:00:00.000000Z'))
        assert CommunityBan.query.filter_by(
            user_id=env.target.id, community_id=env.community.id).count() == 1

    def test_a_ban_marks_the_membership_banned(self, env):
        post_community_moderate_ban(token(env.moderator), self.base(env))
        member = CommunityMember.query.filter_by(
            user_id=env.target.id, community_id=env.community.id).one()
        assert member.is_banned is True

    def test_a_ban_withdraws_a_pending_join_request(self, env):
        make_community_join_request(env.target, env.community)
        post_community_moderate_ban(token(env.moderator), self.base(env))
        assert CommunityJoinRequest.query.filter_by(
            user_id=env.target.id, community_id=env.community.id).count() == 0

    def test_a_local_account_is_told_it_was_banned(self, env):
        before = env.target.unread_notifications
        post_community_moderate_ban(token(env.moderator), self.base(env))
        notification = Notification.query.filter_by(
            user_id=env.target.id, notif_type=NOTIF_BAN).one()
        assert notification.author_id == env.moderator.id
        assert db.session.get(User, env.target.id).unread_notifications == before + 1

    def test_a_ban_ends_the_banned_account_subscription(self, env):
        db.session.add(NotificationSubscription(
            name='probeland', user_id=env.target.id,
            entity_id=env.community.id, type=NOTIF_COMMUNITY))
        db.session.commit()
        post_community_moderate_ban(token(env.moderator), self.base(env))
        assert NotificationSubscription.query.filter_by(
            user_id=env.target.id, entity_id=env.community.id,
            type=NOTIF_COMMUNITY).count() == 0

    def test_an_account_that_never_joined_can_still_be_banned(self, env,
                                                              api_baseline):
        stranger = env.stranger
        post_community_moderate_ban(token(env.moderator),
                                    self.base(env, user_id=stranger.id))
        assert CommunityBan.query.filter_by(
            user_id=stranger.id, community_id=env.community.id).count() == 1
        assert CommunityMember.query.filter_by(
            user_id=stranger.id, community_id=env.community.id).count() == 0

    def test_a_debugging_instance_does_not_touch_the_unread_count(self, env,
                                                                  monkeypatch):
        """The increment hangs the app when the account being banned is the
        one that pressed 'Re-submit this activity', so DEBUG turns it off --
        the notification itself is still written."""
        monkeypatch.setitem(current_app.config, 'DEBUG', True)
        before = env.target.unread_notifications
        post_community_moderate_ban(token(env.moderator), self.base(env))
        assert Notification.query.filter_by(user_id=env.target.id,
                                            notif_type=NOTIF_BAN).count() == 1
        assert db.session.get(User, env.target.id).unread_notifications == before

    def test_an_account_hosted_elsewhere_is_not_notified(self, env,
                                                         api_baseline):
        remote = make_user(api_baseline.instance_remote, 'faraway')
        make_community_member(remote, env.community)
        post_community_moderate_ban(token(env.moderator),
                                    self.base(env, user_id=remote.id))
        assert Notification.query.filter_by(user_id=remote.id).count() == 0
        assert CommunityBan.query.filter_by(
            user_id=remote.id, community_id=env.community.id).count() == 1


# ------------------------------------------------------------------ unbanning

class TestUnban:
    def base(self, env, **extra):
        data = {'user_id': env.target.id, 'community_id': env.community.id}
        data.update(extra)
        return data

    def test_an_account_nobody_holds_is_refused_by_name(self, env):
        """D1202."""
        with pytest.raises(Exception, match='user not found'):
            put_community_moderate_unban(token(env.moderator),
                                         self.base(env, user_id=999999))

    def test_a_community_nobody_holds_is_refused_by_name(self, env):
        """D1202."""
        with pytest.raises(Exception, match='community not found'):
            put_community_moderate_unban(token(env.moderator),
                                         self.base(env, community_id=999999))

    def test_an_account_with_no_standing_cannot_unban(self, env):
        make_community_ban(env.target, env.community, banned_by=env.moderator)
        with pytest.raises(Exception, match='incorrect_login'):
            put_community_moderate_unban(token(env.stranger), self.base(env))

    def test_a_community_hosted_elsewhere_is_refused(self, env):
        make_community_member(env.moderator, env.remote_community,
                              is_moderator=True)
        with pytest.raises(Exception, match='not local'):
            put_community_moderate_unban(
                token(env.moderator),
                self.base(env, community_id=env.remote_community.id))

    def test_an_account_that_was_never_banned_is_refused(self, env):
        with pytest.raises(Exception, match='ban does not exist'):
            put_community_moderate_unban(token(env.moderator), self.base(env))

    def test_an_unban_removes_the_ban_and_clears_the_membership(self, env):
        make_community_ban(env.target, env.community, banned_by=env.moderator,
                           reason='spam')
        member = CommunityMember.query.filter_by(
            user_id=env.target.id, community_id=env.community.id).one()
        member.is_banned = True
        db.session.commit()
        res = put_community_moderate_unban(token(env.moderator), self.base(env))
        assert CommunityBan.query.filter_by(
            user_id=env.target.id, community_id=env.community.id).count() == 0
        assert CommunityMember.query.filter_by(
            user_id=env.target.id, community_id=env.community.id).one().is_banned is False
        assert res['expired'] is True
        assert res['reason'] == 'spam'
        assert res['banned_user']['id'] == env.target.id

    def test_a_local_account_is_told_it_was_unbanned(self, env):
        make_community_ban(env.target, env.community, banned_by=env.moderator)
        before = env.target.unread_notifications
        put_community_moderate_unban(token(env.moderator), self.base(env))
        notification = Notification.query.filter_by(
            user_id=env.target.id, notif_type=NOTIF_UNBAN).one()
        assert notification.author_id == env.moderator.id
        assert db.session.get(User, env.target.id).unread_notifications == before + 1

    def test_a_debugging_instance_does_not_touch_the_unread_count(self, env,
                                                                  monkeypatch):
        make_community_ban(env.target, env.community, banned_by=env.moderator)
        monkeypatch.setitem(current_app.config, 'DEBUG', True)
        before = env.target.unread_notifications
        put_community_moderate_unban(token(env.moderator), self.base(env))
        assert Notification.query.filter_by(user_id=env.target.id,
                                            notif_type=NOTIF_UNBAN).count() == 1
        assert db.session.get(User, env.target.id).unread_notifications == before

    def test_an_account_hosted_elsewhere_is_not_notified(self, env,
                                                         api_baseline):
        remote = make_user(api_baseline.instance_remote, 'faraway')
        make_community_ban(remote, env.community, banned_by=env.moderator)
        put_community_moderate_unban(token(env.moderator),
                                     self.base(env, user_id=remote.id))
        assert Notification.query.filter_by(user_id=remote.id).count() == 0


# ------------------------------------------------------------------- the rest

class TestNsfw:
    @pytest.fixture
    def post(self, env, api_baseline):
        return make_post(env.community, env.target,
                         ap_id='https://test.piefed.local/p/1')

    def test_a_post_nobody_holds_is_refused_by_name(self, env):
        """D1202. `db.session.get(Post, ...)` answers None, and the next line
        read `post.community_id` off it."""
        with pytest.raises(Exception, match='post not found'):
            post_community_moderate_post_nsfw(
                token(env.moderator), {'post_id': 999999, 'nsfw_status': True})

    def test_an_account_with_no_standing_cannot_mark_a_post(self, env, post):
        with pytest.raises(Exception, match='incorrect_login'):
            post_community_moderate_post_nsfw(
                token(env.stranger), {'post_id': post.id, 'nsfw_status': True})

    def test_a_community_hosted_elsewhere_is_refused(self, env, api_baseline):
        make_community_member(env.moderator, env.remote_community,
                              is_moderator=True)
        elsewhere = make_post(env.remote_community, env.target,
                              ap_id='https://remote.test/p/1')
        with pytest.raises(Exception, match='not local'):
            post_community_moderate_post_nsfw(
                token(env.moderator),
                {'post_id': elsewhere.id, 'nsfw_status': True})

    def test_a_moderator_marks_and_unmarks_a_post(self, env, post):
        post_community_moderate_post_nsfw(
            token(env.moderator), {'post_id': post.id, 'nsfw_status': True})
        assert db.session.get(Post, post.id).nsfw is True
        res = post_community_moderate_post_nsfw(
            token(env.moderator), {'post_id': post.id, 'nsfw_status': False})
        assert db.session.get(Post, post.id).nsfw is False
        assert res['post']['id'] == post.id


class TestModerators:
    @pytest.fixture
    def owner(self, env):
        member = CommunityMember.query.filter_by(
            user_id=env.moderator.id, community_id=env.community.id).one()
        member.is_owner = True
        db.session.commit()
        return env.moderator

    def test_an_owner_adds_a_moderator(self, env, owner):
        res = post_community_mod(token(owner),
                                 {'community_id': env.community.id,
                                  'person_id': env.stranger.id, 'added': True})
        assert env.stranger.id in [mod['moderator']['id']
                                   for mod in res['moderators']]
        member = CommunityMember.query.filter_by(
            user_id=env.stranger.id, community_id=env.community.id).one()
        assert member.is_moderator is True

    def test_an_owner_removes_a_moderator(self, env, owner):
        make_community_member(env.stranger, env.community, is_moderator=True)
        res = post_community_mod(token(owner),
                                 {'community_id': env.community.id,
                                  'person_id': env.stranger.id, 'added': False})
        assert env.stranger.id not in [mod['moderator']['id']
                                       for mod in res['moderators']]


class TestFlairCreate:
    def test_a_community_nobody_holds_is_refused_by_name(self, env):
        """D1202. `db.session.get(Community, ...)` answers None, and the next
        line called `community.is_owner(...)` on it."""
        with pytest.raises(Exception, match='community not found'):
            post_community_flair_create(token(env.moderator),
                                        {'community_id': 999999,
                                         'flair_title': 'news'})

    def test_an_account_with_no_standing_cannot_create_flair(self, env):
        with pytest.raises(Exception, match='insufficient permissions'):
            post_community_flair_create(token(env.stranger),
                                        {'community_id': env.community.id,
                                         'flair_title': 'news'})

    def test_a_flair_created_with_nothing_but_a_title(self, env):
        res = post_community_flair_create(token(env.moderator),
                                          {'community_id': env.community.id,
                                           'flair_title': 'news'})
        assert res['flair_title'] == 'news'
        assert res['text_color'] == '#000000'
        assert res['background_color'] == '#DEDDDA'
        assert res['blur_images'] is False
        # Read back after a rollback: `flair_view` calls `get_ap_id()`, which
        # ASSIGNS the identity it computes, so the response says nothing about
        # whether the endpoint stored one. Only a read that discards what this
        # session has not committed can tell the two apart.
        db.session.rollback()
        stored = db.session.get(CommunityFlair, res['id'])
        assert stored.ap_id == f"{res['ap_id']}"
        assert stored.ap_id.endswith(f"/tag/{res['id']}")

    def test_short_hex_colours_are_written_out_in_full(self, env):
        res = post_community_flair_create(token(env.moderator),
                                          {'community_id': env.community.id,
                                           'flair_title': 'news',
                                           'text_color': '#fff',
                                           'background_color': '#123',
                                           'blur_images': True})
        assert res['text_color'] == '#ffffff'
        assert res['background_color'] == '#112233'
        assert res['blur_images'] is True

    def test_long_hex_colours_are_kept_as_given(self, env):
        res = post_community_flair_create(token(env.moderator),
                                          {'community_id': env.community.id,
                                           'flair_title': 'news',
                                           'text_color': '#abcdef',
                                           'background_color': '#fedcba'})
        assert res['text_color'] == '#abcdef'
        assert res['background_color'] == '#fedcba'

    def test_the_same_title_twice_is_refused(self, env):
        post_community_flair_create(token(env.moderator),
                                    {'community_id': env.community.id,
                                     'flair_title': 'news'})
        with pytest.raises(Exception, match='Flair already exists'):
            post_community_flair_create(token(env.moderator),
                                        {'community_id': env.community.id,
                                         'flair_title': 'news'})

    def test_the_same_title_padded_is_refused(self, env):
        """D1205. The duplicate check read `data['flair_title']` while the row
        stored `data['flair_title'].strip()`, so ' news ' matched nothing and
        was written as a second 'news'."""
        post_community_flair_create(token(env.moderator),
                                    {'community_id': env.community.id,
                                     'flair_title': 'news'})
        with pytest.raises(Exception, match='Flair already exists'):
            post_community_flair_create(token(env.moderator),
                                        {'community_id': env.community.id,
                                         'flair_title': ' news '})

    def test_the_same_title_in_another_colour_is_refused(self, env):
        """D1205. The colours were part of the duplicate check, so one
        community could carry any number of identically named flairs."""
        post_community_flair_create(token(env.moderator),
                                    {'community_id': env.community.id,
                                     'flair_title': 'news'})
        with pytest.raises(Exception, match='Flair already exists'):
            post_community_flair_create(token(env.moderator),
                                        {'community_id': env.community.id,
                                         'flair_title': 'news',
                                         'text_color': '#ffffff'})

    def test_the_same_title_in_another_community_is_allowed(self, env):
        other = make_community('elsewhere')
        make_community_member(env.moderator, other, is_moderator=True)
        post_community_flair_create(token(env.moderator),
                                    {'community_id': env.community.id,
                                     'flair_title': 'news'})
        res = post_community_flair_create(token(env.moderator),
                                          {'community_id': other.id,
                                           'flair_title': 'news'})
        assert res['community_id'] == other.id


class TestFlairEdit:
    def test_a_flair_nobody_holds_is_refused_by_name(self, env):
        with pytest.raises(Exception, match='No matching flair with id=999999'):
            put_community_flair_edit(token(env.moderator), {'flair_id': 999999})

    def test_an_account_with_no_standing_cannot_edit_flair(self, env):
        flair = make_community_flair(env.community, 'news')
        with pytest.raises(Exception, match='insufficient permissions'):
            put_community_flair_edit(token(env.stranger),
                                     {'flair_id': flair.id,
                                      'flair_title': 'olds'})

    def test_every_field_can_be_changed_at_once(self, env):
        flair = make_community_flair(env.community, 'news', ap_id='https://x/1')
        res = put_community_flair_edit(token(env.moderator),
                                       {'flair_id': flair.id,
                                        'flair_title': 'olds',
                                        'text_color': '#abc',
                                        'background_color': '#abcdef',
                                        'blur_images': True})
        assert res['flair_title'] == 'olds'
        assert res['text_color'] == '#aabbcc'
        assert res['background_color'] == '#abcdef'
        assert res['blur_images'] is True
        assert res['ap_id'] == 'https://x/1'

    def test_a_flair_with_no_identity_is_given_one(self, env):
        flair = make_community_flair(env.community, 'news')
        flair.ap_id = None
        db.session.commit()
        flair_id = flair.id
        res = put_community_flair_edit(token(env.moderator),
                                       {'flair_id': flair_id})
        assert res['ap_id'].endswith(f'/tag/{flair_id}')
        # See test_a_flair_created_with_nothing_but_a_title: the response
        # cannot distinguish a stored identity from one `flair_view` made up
        # on the way out.
        db.session.rollback()
        assert db.session.get(CommunityFlair, flair_id).ap_id == res['ap_id']

    def test_a_long_text_colour_and_a_short_background(self, env):
        flair = make_community_flair(env.community, 'news', ap_id='https://x/1')
        res = put_community_flair_edit(token(env.moderator),
                                       {'flair_id': flair.id,
                                        'text_color': '#abcdef',
                                        'background_color': '#abc'})
        assert res['text_color'] == '#abcdef'
        assert res['background_color'] == '#aabbcc'

    def test_naming_no_field_changes_nothing(self, env):
        flair = make_community_flair(env.community, 'news', ap_id='https://x/1')
        res = put_community_flair_edit(token(env.moderator),
                                       {'flair_id': flair.id})
        assert res['flair_title'] == 'news'


class TestFlairDelete:
    def test_a_flair_nobody_holds_is_refused_by_name(self, env):
        with pytest.raises(Exception, match='No matching flair with id=999999'):
            post_community_flair_delete(token(env.moderator),
                                        {'flair_id': 999999})

    def test_an_account_with_no_standing_cannot_delete_flair(self, env):
        flair = make_community_flair(env.community, 'news')
        with pytest.raises(Exception, match='insufficient permissions'):
            post_community_flair_delete(token(env.stranger),
                                        {'flair_id': flair.id})

    def test_deleting_a_flair_takes_it_off_the_posts_wearing_it(self, env):
        post = make_post(env.community, env.target,
                         ap_id='https://test.piefed.local/p/1')
        flair = make_post_flair(post, 'news')
        res = post_community_flair_delete(token(env.moderator),
                                          {'flair_id': flair.id})
        assert db.session.get(CommunityFlair, flair.id) is None
        assert db.session.execute(
            db.text('SELECT count(*) FROM post_flair WHERE flair_id = :id'),
            {'id': flair.id}).scalar() == 0
        assert res['community_view']['community']['id'] == env.community.id


class TestBanExpiryParsing:
    """`a_ban_expiry` (D1204) -- the timestamp spellings a client can send."""

    @pytest.mark.parametrize('given, expected', [
        ('2030-01-01T00:00:00.000000Z', datetime(2030, 1, 1)),
        ('2030-01-01T00:00:00Z', datetime(2030, 1, 1)),
        ('2030-01-01T00:00:00z', datetime(2030, 1, 1)),
        ('  2030-01-01T00:00:00Z  ', datetime(2030, 1, 1)),
        ('2030-01-01T00:00:00+00:00', datetime(2030, 1, 1)),
        ('2030-01-01T09:00:00+09:00', datetime(2030, 1, 1)),
        ('2029-12-31T19:00:00-05:00', datetime(2030, 1, 1)),
        ('2030-01-01T00:00:00', datetime(2030, 1, 1)),
    ])
    def test_a_timestamp_is_read_as_utc(self, app, given, expected):
        assert a_ban_expiry(given) == expected

    @pytest.mark.parametrize('given', ['next week', '', '2030-13-01T00:00:00Z'])
    def test_anything_else_is_refused_by_name(self, app, given):
        with pytest.raises(Exception, match='not an ISO 8601 timestamp'):
            a_ban_expiry(given)
