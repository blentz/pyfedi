"""The community API's listing and lifecycle half: the community list, one
community, joining, leaving, blocking, creating, editing, subscribing and
deleting.

Sub-project 84, slice K -- the rest of `app/api/alpha/utils/community.py`,
which this file closes. Seven defects, all measured first:

* an account BANNED from a community rejoined it by asking. The web path
  refuses this twice (`do_subscribe`, D991's shape); the API did not refuse
  it at all, and the membership row it wrote put the community back into the
  banned account's subscribed feed (D1209);
* `hide_nsfw`, `hide_nsfl` and `hide_gen_ai` are not flags -- 0 is Show, 1 is
  Hide completely, 2 is Blur, 3 is Semi-transparent -- and the listing read
  all three for truthiness, so an account that had asked only for a BLURRED
  thumbnail had every NSFW community removed from its listing (D1211);
* `show_nsfl = show_nsfw` tied two separate account settings together: asking
  for NSFW handed back the NSFL the reader had chosen to hide (D1212);
* asked for 'Subscribed' or the moderating listings WITHOUT an account, the
  endpoint fell through and answered with every community on the instance
  (D1208);
* four endpoints passed an unchecked community id to a shared function, which
  answered `NoResultFound`, `AttributeError`, or -- for a block -- a
  `ForeignKeyViolation` that poisoned the session and put the SQL in the
  response (D1210);
* leaving a community never joined answered "No row was found when one was
  required" (D1213);
* a community could be created with an EMPTY name, taking `/c/` for itself
  (D1214, in `app/shared/community.py`).
"""
from unittest.mock import patch

import pytest
from flask import current_app, g

from app import db
from app.api.alpha.utils.community import (get_community, get_community_list,
                                           post_community,
                                           post_community_block,
                                           post_community_delete,
                                           post_community_follow,
                                           post_community_leave_all,
                                           put_community,
                                           put_community_subscribe)
from app.constants import NOTIF_COMMUNITY
from app.models import (Community, CommunityBlock, CommunityMember, Language,
                        NotificationSubscription, Site, User)
from tests.factories import (a_keypair, make_community, make_community_ban,
                             make_community_block, make_community_member,
                             make_instance_block, make_user)


def token(user):
    return f'Bearer {user.encode_jwt_token()}'


def names(res):
    return sorted(entry['community']['name'] for entry in res['communities'])


@pytest.fixture
def env(app, api_baseline):
    """`probeland` is local and `member` belongs to it; `stranger` belongs to
    nothing."""
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('probeland')
    member = api_baseline.user2
    make_community_member(member, community)
    return SimpleNamespace(community=community, member=member,
                           stranger=api_baseline.user3,
                           admin=api_baseline.user1,
                           baseline=api_baseline)


@pytest.fixture
def maker(app, api_baseline):
    """An account that may create a community: verified, with keys, on an
    instance whose languages are seeded."""
    user = api_baseline.user1
    user.private_key, user.public_key = a_keypair()
    for code, name in [('en', 'English'), ('und', 'Undetermined')]:
        db.session.add(Language(code=code, name=name))
    db.session.commit()
    return user


# ------------------------------------------------------------- the listing

class TestListing:
    def test_anonymous_sees_every_unbanned_community(self, env):
        assert names(get_community_list(None, {})) == [
            'community1', 'community2', 'community3', 'probeland']

    def test_the_local_listing_is_only_this_instance(self, env):
        assert names(get_community_list(None, {'type_': 'Local'})) == \
            ['probeland']

    @pytest.mark.parametrize('type_', ['Subscribed', 'Moderating',
                                       'ModeratorView'])
    def test_a_listing_of_your_own_needs_an_account(self, env, type_):
        """D1208. These fell through to the `else` arm and answered with EVERY
        community on the instance."""
        with pytest.raises(Exception, match='incorrect login'):
            get_community_list(None, {'type_': type_})

    def test_the_subscribed_listing_is_what_you_joined(self, env):
        # api_baseline already puts `member` in community2.
        assert names(get_community_list(token(env.member),
                                        {'type_': 'Subscribed'})) == \
            ['community2', 'probeland']

    def test_the_subscribed_listing_of_someone_who_joined_nothing(self, env):
        assert get_community_list(token(env.stranger),
                                  {'type_': 'Subscribed'})['communities'] == []

    @pytest.mark.parametrize('type_', ['Moderating', 'ModeratorView'])
    def test_the_moderating_listing_is_what_you_moderate(self, env, type_):
        make_community_member(env.stranger, env.community, is_moderator=True)
        assert names(get_community_list(token(env.stranger),
                                        {'type_': type_})) == ['probeland']

    def test_a_private_community_is_hidden_from_anonymous_readers(self, env):
        private = make_community('secretland')
        private.private = True
        db.session.commit()
        assert 'secretland' not in names(get_community_list(None, {}))

    def test_a_private_community_is_hidden_from_a_stranger(self, env):
        private = make_community('secretland')
        private.private = True
        db.session.commit()
        assert 'secretland' not in names(
            get_community_list(token(env.stranger), {}))

    def test_a_private_community_is_shown_to_its_members(self, env):
        private = make_community('secretland')
        private.private = True
        db.session.commit()
        make_community_member(env.stranger, private)
        assert 'secretland' in names(
            get_community_list(token(env.stranger), {}))

    def test_a_private_community_is_hidden_from_a_banned_member(self, env):
        private = make_community('secretland')
        private.private = True
        db.session.commit()
        membership = make_community_member(env.stranger, private)
        membership.is_banned = True
        db.session.commit()
        assert 'secretland' not in names(
            get_community_list(token(env.stranger), {}))

    def test_a_community_you_are_banned_from_is_left_out(self, env):
        """`api_baseline` bans user1 from community3."""
        assert 'community3' not in names(
            get_community_list(token(env.admin), {}))

    def test_a_community_you_blocked_is_left_out(self, env):
        make_community_block(env.stranger, env.community)
        assert 'probeland' not in names(
            get_community_list(token(env.stranger), {}))

    def test_a_community_on_an_instance_you_blocked_is_left_out(self, env):
        make_instance_block(env.stranger, env.baseline.instance_remote)
        listed = names(get_community_list(token(env.stranger), {}))
        assert 'community1' not in listed
        assert 'probeland' in listed


class TestVisibilitySettings:
    @pytest.fixture
    def rough(self, env):
        nsfw = make_community('adultsonly')
        nsfw.nsfw = True
        nsfl = make_community('gorehouse')
        nsfl.nsfl = True
        machine = make_community('slopfarm')
        machine.ai_generated = True
        db.session.commit()
        return env

    def set(self, user, **columns):
        for column, value in columns.items():
            setattr(user, column, value)
        db.session.commit()

    def test_hide_completely_takes_them_out(self, rough):
        self.set(rough.stranger, hide_nsfw=1, hide_nsfl=1, hide_gen_ai=1)
        listed = names(get_community_list(token(rough.stranger), {}))
        assert 'adultsonly' not in listed
        assert 'gorehouse' not in listed
        assert 'slopfarm' not in listed

    @pytest.mark.parametrize('setting', [0, 2, 3])
    def test_show_blur_and_transparent_all_leave_them_in(self, rough, setting):
        """D1211. 0 is Show, 1 is Hide completely, 2 is Blur, 3 is
        Semi-transparent. Read for truthiness, 2 and 3 -- both of which are
        ways of SHOWING something -- took the community out of the listing
        altogether."""
        self.set(rough.stranger, hide_nsfw=setting, hide_nsfl=setting,
                 hide_gen_ai=setting)
        listed = names(get_community_list(token(rough.stranger), {}))
        assert 'adultsonly' in listed
        assert 'gorehouse' in listed
        assert 'slopfarm' in listed

    def test_asking_for_nsfw_does_not_hand_back_nsfl(self, rough):
        """D1212. `show_nsfl = show_nsfw` tied two separate settings together,
        so a reader who had hidden NSFL was shown it for asking about NSFW."""
        self.set(rough.stranger, hide_nsfw=1, hide_nsfl=1)
        listed = names(get_community_list(token(rough.stranger),
                                          {'show_nsfw': True}))
        assert 'adultsonly' in listed
        assert 'gorehouse' not in listed

    def test_an_anonymous_reader_is_shown_neither_by_default(self, rough):
        listed = names(get_community_list(None, {}))
        assert 'adultsonly' not in listed
        assert 'gorehouse' not in listed

    def test_an_anonymous_reader_can_be_handed_nsfl_on_its_own(self, rough):
        listed = names(get_community_list(None, {'show_nsfl': True}))
        assert 'gorehouse' in listed
        assert 'adultsonly' not in listed

    def test_an_anonymous_reader_asking_for_nsfw_gets_only_nsfw(self, rough):
        """D1212, anonymously."""
        listed = names(get_community_list(None, {'show_nsfw': True}))
        assert 'adultsonly' in listed
        assert 'gorehouse' not in listed


class TestListingShape:
    def test_a_query_matches_the_title_and_the_handle(self, env):
        assert names(get_community_list(None, {'q': 'probe'})) == ['probeland']
        assert names(get_community_list(None, {'q': 'nothing here'})) == []

    def test_a_handle_that_looks_remote_is_searched_for(self, env):
        """The `!name@host` form asks the resolver to fetch the community
        first, then searches for what it found -- and the search is run on the
        handle WITHOUT its leading '!', which is not part of any ap_id."""
        env.community.ap_id = 'probeland@test.piefed.local'
        db.session.commit()
        with patch('app.api.alpha.utils.community.search_for_community') as go:
            res = get_community_list(token(env.member),
                                     {'q': '!probeland@test.piefed.local'})
        go.assert_called_once_with('!probeland@test.piefed.local')
        assert names(res) == ['probeland']

    def test_an_anonymous_reader_does_not_reach_the_resolver(self, env):
        with patch('app.api.alpha.utils.community.search_for_community') as go:
            get_community_list(None, {'q': '!probeland@test.piefed.local'})
        go.assert_not_called()

    @pytest.mark.parametrize('sort, first', [
        ('New', 'probeland'),
        ('Old', 'community1'),
        ('TopAll', 'community1'),
        ('TopSubscribers', 'community1'),
        ('NewFederated', 'probeland'),
        ('OldFederated', 'community1'),
        ('Hot', 'community1'),
        ('nonsense', 'community1'),
    ])
    def test_the_sorts(self, env, sort, first):
        from datetime import timedelta

        from app.models import utcnow
        now = utcnow()
        # `last_active` runs the OTHER way from `created_at`, so the default
        # sort cannot agree with 'New' or 'NewFederated' by accident.
        ordered = Community.query.order_by(Community.id).all()
        for offset, community in enumerate(ordered):
            community.created_at = now - timedelta(days=10 - offset)
            community.first_federated_at = now - timedelta(days=10 - offset)
            community.last_active = now - timedelta(days=offset + 1)
            community.total_subscriptions_count = 10 - offset
        db.session.commit()
        res = get_community_list(None, {'sort': sort})
        assert res['communities'][0]['community']['name'] == first

    def test_the_listing_pages(self, env):
        first = get_community_list(None, {'limit': 2})
        assert len(first['communities']) == 2
        assert first['next_page'] == '2'
        second = get_community_list(None, {'limit': 2, 'page': 2})
        assert len(second['communities']) == 2
        assert second['next_page'] is None

    def test_a_limit_beyond_the_configured_page_length_is_clamped(
            self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'PAGE_LENGTH', 2)
        assert len(get_community_list(None, {'limit': 500})['communities']) == 2


# -------------------------------------------------------------- one community

class TestOneCommunity:
    def test_naming_neither_an_id_nor_a_name_is_refused(self, env):
        with pytest.raises(Exception, match='id or name required'):
            get_community(None, {})

    def test_an_id_that_is_not_a_number_is_refused_by_name(self, env):
        """D1210. `int('abc')` reached the caller as "invalid literal for
        int() with base 10: 'abc'"."""
        with pytest.raises(Exception, match='id must be a number'):
            get_community(None, {'id': 'abc'})

    def test_a_community_by_id(self, env):
        res = get_community(None, {'id': env.community.id})
        assert res['community_view']['community']['name'] == 'probeland'

    def test_a_bare_name_is_taken_as_local(self, env):
        res = get_community(token(env.member), {'name': 'probeland'})
        assert res['community_view']['community']['name'] == 'probeland'

    def test_a_name_nobody_holds_is_refused(self, env):
        with pytest.raises(Exception, match='unknown community'):
            get_community(None, {'name': 'nowhere'})

    def test_an_id_nobody_holds_is_refused(self, env):
        with pytest.raises(Exception, match='unknown community'):
            get_community(None, {'id': 999999})

    def test_a_handle_this_instance_does_not_know_is_searched_for(self, env):
        with patch('app.api.alpha.utils.community.search_for_community') as go:
            with pytest.raises(Exception, match='unknown community'):
                get_community(token(env.member), {'name': 'far@away.test'})
        go.assert_called_once_with('!far@away.test')

    def test_a_handle_already_written_with_its_bang(self, env):
        """The resolver wants exactly one leading '!', and a caller who wrote
        one must not be handed '!!far@away.test'."""
        with patch('app.api.alpha.utils.community.search_for_community') as go:
            with pytest.raises(Exception, match='unknown community'):
                get_community(token(env.member), {'name': '!far@away.test'})
        go.assert_called_once_with('!far@away.test')

    def test_an_anonymous_reader_does_not_reach_the_resolver(self, env):
        with patch('app.api.alpha.utils.community.search_for_community') as go:
            with pytest.raises(Exception, match='unknown community'):
                get_community(None, {'name': 'far@away.test'})
        go.assert_not_called()


# ------------------------------------------------------- joining and leaving

class TestFollow:
    def test_a_community_nobody_holds_is_refused_by_name(self, env):
        """D1210."""
        with pytest.raises(Exception, match='community not found'):
            post_community_follow(token(env.stranger),
                                  {'community_id': 999999, 'follow': True})

    def test_joining_writes_the_membership(self, env):
        res = post_community_follow(token(env.stranger),
                                    {'community_id': env.community.id,
                                     'follow': True})
        assert res['community_view']['community']['name'] == 'probeland'
        assert CommunityMember.query.filter_by(
            user_id=env.stranger.id, community_id=env.community.id).count() == 1

    def test_an_account_banned_from_the_community_cannot_rejoin(self, env):
        """D1209. The web path refuses this twice and the API did not refuse
        it at all: the membership row went in, and the community came back
        into the banned account's subscribed listing."""
        make_community_ban(env.stranger, env.community, banned_by=env.admin,
                           reason='spam')
        with pytest.raises(Exception, match='banned from this community'):
            post_community_follow(token(env.stranger),
                                  {'community_id': env.community.id,
                                   'follow': True})
        assert CommunityMember.query.filter_by(
            user_id=env.stranger.id, community_id=env.community.id).count() == 0

    def test_leaving_removes_the_membership(self, env):
        post_community_follow(token(env.member),
                              {'community_id': env.community.id,
                               'follow': False})
        assert CommunityMember.query.filter_by(
            user_id=env.member.id, community_id=env.community.id).count() == 0

    def test_leaving_a_community_never_joined_is_refused_by_name(self, env):
        """D1213. `leave_community` reads the membership with `.one()`."""
        with pytest.raises(Exception, match='not a member'):
            post_community_follow(token(env.stranger),
                                  {'community_id': env.community.id,
                                   'follow': False})

    def test_a_moderator_must_step_down_before_leaving(self, env):
        make_community_member(env.stranger, env.community, is_moderator=True)
        with pytest.raises(Exception, match='Step down as a moderator'):
            post_community_follow(token(env.stranger),
                                  {'community_id': env.community.id,
                                   'follow': False})


class TestLeaveAll:
    def test_an_anonymous_caller_is_refused(self, env):
        with pytest.raises(Exception, match='incorrect login'):
            post_community_leave_all(None)

    def test_every_membership_goes(self, env):
        second = make_community('otherland')
        make_community_member(env.member, second)
        post_community_leave_all(token(env.member))
        assert CommunityMember.query.filter_by(
            user_id=env.member.id).count() == 0

    def test_a_community_you_moderate_is_kept(self, env):
        moderated = make_community('mineland')
        make_community_member(env.member, moderated, is_moderator=True)
        post_community_leave_all(token(env.member))
        remaining = [m.community_id for m in CommunityMember.query.filter_by(
            user_id=env.member.id).all()]
        assert remaining == [moderated.id]

    def test_leaving_a_remote_community_that_was_joined_properly(self, env):
        """D1215's other arm: when the join DID leave a CommunityJoinRequest
        behind, the Undo Follow names it and the row goes."""
        from app.models import CommunityJoinRequest
        from tests.factories import make_community_join_request
        remote = env.baseline.community2   # ap_id is set, so not local
        make_community_join_request(env.member, remote)
        post_community_leave_all(token(env.member))
        assert CommunityJoinRequest.query.filter_by(
            user_id=env.member.id, community_id=remote.id).count() == 0

    def test_an_account_that_joined_nothing_is_answered(self, env):
        res = post_community_leave_all(token(env.stranger))
        assert res['local_user_view']['person']['id'] == env.stranger.id

    def test_the_feeds_are_left_too(self, env):
        from tests.factories import make_feed_item, make_feed_member, make_local_feed
        feed = make_local_feed('newsfeed')
        make_feed_member(env.member, feed)
        make_feed_item(feed, env.community)
        post_community_leave_all(token(env.member))
        from app.models import FeedMember
        assert FeedMember.query.filter_by(user_id=env.member.id).count() == 0

    def test_a_feed_you_own_is_kept(self, env):
        from tests.factories import make_feed_member, make_local_feed
        from app.models import FeedMember
        feed = make_local_feed('myfeed')
        make_feed_member(env.member, feed, is_owner=True)
        post_community_leave_all(token(env.member))
        assert FeedMember.query.filter_by(user_id=env.member.id).count() == 1


class TestBlock:
    def test_a_community_nobody_holds_is_refused_by_name(self, env):
        """D1210. The block used to reach the database as an insert against a
        community id that does not exist: a ForeignKeyViolation that poisoned
        the session and put the SQL in the response."""
        with pytest.raises(Exception, match='community not found'):
            post_community_block(token(env.stranger),
                                 {'community_id': 999999, 'block': True})

    def test_blocking_and_unblocking(self, env):
        post_community_block(token(env.stranger),
                             {'community_id': env.community.id, 'block': True})
        assert CommunityBlock.query.filter_by(
            user_id=env.stranger.id, community_id=env.community.id).count() == 1
        post_community_block(token(env.stranger),
                             {'community_id': env.community.id, 'block': False})
        assert CommunityBlock.query.filter_by(
            user_id=env.stranger.id, community_id=env.community.id).count() == 0


class TestSubscribe:
    def test_a_community_nobody_holds_is_refused_by_name(self, env):
        """D1210."""
        with pytest.raises(Exception, match='community not found'):
            put_community_subscribe(token(env.stranger),
                                    {'community_id': 999999,
                                     'subscribe': True})

    def test_subscribing_and_unsubscribing(self, env):
        put_community_subscribe(token(env.member),
                                {'community_id': env.community.id,
                                 'subscribe': True})
        assert NotificationSubscription.query.filter_by(
            user_id=env.member.id, entity_id=env.community.id,
            type=NOTIF_COMMUNITY).count() == 1
        put_community_subscribe(token(env.member),
                                {'community_id': env.community.id,
                                 'subscribe': False})
        assert NotificationSubscription.query.filter_by(
            user_id=env.member.id, entity_id=env.community.id,
            type=NOTIF_COMMUNITY).count() == 0

    def test_unsubscribing_from_something_you_never_followed(self, env):
        with pytest.raises(Exception, match='did not exist'):
            put_community_subscribe(token(env.member),
                                    {'community_id': env.community.id,
                                     'subscribe': False})


class TestDelete:
    def test_a_community_nobody_holds_is_refused_by_name(self, env):
        """D1210. `db.session.get(Community, ...)` answers None, and
        `delete_community` read `is_owner` off it."""
        with pytest.raises(Exception, match='community not found'):
            post_community_delete(token(env.admin),
                                  {'community_id': 999999, 'deleted': True})

    def test_an_account_with_no_standing_cannot_delete(self, env):
        with pytest.raises(Exception, match='incorrect_login'):
            post_community_delete(token(env.stranger),
                                  {'community_id': env.community.id,
                                   'deleted': True})

    def test_deleting_and_restoring(self, env):
        post_community_delete(token(env.admin),
                              {'community_id': env.community.id,
                               'deleted': True})
        assert db.session.get(Community, env.community.id).banned is True
        post_community_delete(token(env.admin),
                              {'community_id': env.community.id,
                               'deleted': False})
        assert db.session.get(Community, env.community.id).banned is False


# ------------------------------------------------------ creating and editing

class TestCreate:
    def test_an_account_with_no_keys_cannot_create(self, env):
        with pytest.raises(Exception, match='until your account is verified'):
            post_community(token(env.stranger),
                           {'name': 'newland', 'title': 'new land'})

    def test_a_community_with_nothing_but_a_name_and_a_title(self, maker, env):
        res = post_community(token(maker),
                             {'name': 'newland', 'title': 'new land'})
        community = db.session.get(
            Community, res['community_view']['community']['id'])
        assert community.name == 'newland'
        assert community.title == 'new land'
        assert community.nsfw is False
        assert community.restricted_to_mods is False
        assert community.local_only is False
        assert community.question_answer is False

    def test_every_field_at_once(self, maker, env):
        res = post_community(token(maker),
                             {'name': 'newland', 'title': 'new land',
                              'description': 'a place', 'rules': 'be kind',
                              'nsfw': True, 'restricted_to_mods': True,
                              'local_only': True, 'question_answer': True,
                              'discussion_languages': []})
        community = db.session.get(
            Community, res['community_view']['community']['id'])
        assert community.description == 'a place'
        assert community.rules == 'be kind'
        assert community.nsfw is True
        assert community.restricted_to_mods is True
        assert community.local_only is True
        assert community.question_answer is True

    def test_the_name_is_slugified(self, maker, env):
        res = post_community(token(maker), {'name': 'New Land!', 'title': 'x'})
        assert res['community_view']['community']['name'] == 'new_land'

    def test_a_name_that_slugifies_to_nothing_is_refused(self, maker, env):
        """D1214. slugify('---') is '', and the empty name was accepted: a
        community addressed as /c/, which nothing can link to."""
        with pytest.raises(Exception, match='needs a name'):
            post_community(token(maker), {'name': '---', 'title': 'x'})

    def test_a_name_already_taken_is_refused(self, maker, env):
        post_community(token(maker), {'name': 'newland', 'title': 'x'})
        with pytest.raises(Exception, match='already exists'):
            post_community(token(maker), {'name': 'newland', 'title': 'x'})

    def test_the_creator_owns_it(self, maker, env):
        res = post_community(token(maker), {'name': 'newland', 'title': 'x'})
        membership = CommunityMember.query.filter_by(
            user_id=maker.id,
            community_id=res['community_view']['community']['id']).one()
        assert membership.is_owner is True
        assert membership.is_moderator is True


class TestEdit:
    def test_a_community_nobody_holds_is_refused_by_name(self, env):
        with pytest.raises(Exception, match='community not found'):
            put_community(token(env.admin),
                          {'community_id': 999999, 'title': 'x'})

    def test_an_account_with_no_standing_cannot_edit(self, env):
        with pytest.raises(Exception, match='incorrect_login'):
            put_community(token(env.stranger),
                          {'community_id': env.community.id, 'title': 'mine'})

    def test_naming_no_field_keeps_what_is_there(self, maker, env):
        env.community.description = 'a place'
        env.community.rules = 'be kind'
        db.session.commit()
        put_community(token(env.admin), {'community_id': env.community.id})
        community = db.session.get(Community, env.community.id)
        assert community.title == 'probeland'
        assert community.rules == 'be kind'

    def test_every_field_at_once(self, maker, env):
        put_community(token(env.admin),
                      {'community_id': env.community.id, 'title': 'elsewhere',
                       'description': 'a new place', 'rules': 'be kinder',
                       'nsfw': True, 'restricted_to_mods': True,
                       'local_only': True, 'question_answer': True,
                       'discussion_languages': []})
        community = db.session.get(Community, env.community.id)
        assert community.title == 'elsewhere'
        assert community.description == 'a new place'
        assert community.rules == 'be kinder'
        assert community.nsfw is True
        assert community.restricted_to_mods is True
        assert community.local_only is True
        assert community.question_answer is True

    def test_the_pictures_can_be_replaced(self, maker, env):
        # is_image_url sends a HEAD request when the extension does not settle
        # it, and make_image_sizes fetches and resizes the picture.
        with patch('app.shared.community.is_image_url', return_value=True), \
                patch('app.shared.community.make_image_sizes') as sizes:
            put_community(token(env.admin),
                          {'community_id': env.community.id,
                           'icon_url': 'https://cdn.test/icon.png',
                           'banner_url': 'https://cdn.test/banner.png'})
        community = db.session.get(Community, env.community.id)
        assert community.icon.source_url == 'https://cdn.test/icon.png'
        assert community.image.source_url == 'https://cdn.test/banner.png'
        assert sizes.call_count == 2

    def test_the_pictures_already_set_are_kept(self, maker, env):
        from tests.factories import make_file
        # Both a `file_path` AND a `source_url`: `edit_community` compares the
        # url it is handed against BOTH, and a File carrying only a file_path
        # makes `icon_url != community.icon.source_url` true for the value the
        # endpoint sends and for None alike, which hides the difference.
        env.community.icon = make_file(file_path='/static/icon.png',
                                       source_url='https://cdn.test/icon.png')
        env.community.image = make_file(file_path='/static/banner.png',
                                        source_url='https://cdn.test/banner.png')
        db.session.commit()
        put_community(token(env.admin), {'community_id': env.community.id})
        community = db.session.get(Community, env.community.id)
        assert community.icon.file_path == '/static/icon.png'
        assert community.image.file_path == '/static/banner.png'

    def test_the_languages_already_set_are_kept(self, maker, env):
        language = Language.query.filter_by(code='en').one()
        env.community.languages.append(language)
        db.session.commit()
        put_community(token(env.admin), {'community_id': env.community.id})
        assert language in db.session.get(Community, env.community.id).languages
