"""Looking a person up, and what their profile shows about them.

Sub-project 89 -- `app/user/utils.py`. Two halves: `search_for_user`, which
turns `@name@server` into a User row and fetches the remote one if this
instance has never seen it, and the `_get_user_*` helpers behind
`/u/<actor>`, which decide what a visitor is shown about somebody else's
account.

Four defects, measured first:

* `name, server = address.lower().split('@')` unpacked whatever it was
  given, so `@a@b@c` was `ValueError: too many values to unpack` -- a 500
  from a crafted URL, where "no such person" was the answer. The community
  side of this was D1258; this is the same line on the user side (D1268);
* `webfinger_json['links']`, `links['href']` and `object['type']` were all
  read out of another instance's JSON without a membership test, each one a
  500 (D1269);
* the retry loop around the actor fetch had no `break`, so a request that
  SUCCEEDED was made a second time. Every user lookup on this instance cost
  the remote instance two fetches, and every 401 cost it two more (D1270);
* the profile's overview tab -- posts and replies interleaved -- filtered
  neither `post.private` nor `post_reply.private`, though the posts tab and
  the replies tab beside it both do. The community-membership filter it
  relies on instead only covers private communities the account is STILL a
  member of, so anything it posted in a private community it has since left,
  or been banned from, was shown to anonymous visitors (D1271).

Also hardened: `_get_user_same_ip` lists the other accounts sharing an IP
address and checked only that the viewer was logged in. The template gates
the block on `is_admin_or_staff`, so nothing leaked through the profile page,
but the gate now lives in the function as well.
"""
from unittest.mock import patch

import httpx
import pytest
from flask import current_app, g
from flask_login import login_user, logout_user

from app import db
from app.models import BannedInstances, Post, PostReply, Site, User, UserNote
from app.user.utils import (SimplePagination, _get_user_archived_replies,
                            _get_user_moderates, _get_user_post_replies,
                            _get_user_posts, _get_user_posts_and_replies,
                            _get_user_same_ip, _get_user_subscribed_communities,
                            _get_user_upvoted_posts, insert_or_update_user_note,
                            search_for_user, unsubscribe_from_community)
from tests.factories import (make_community, make_community_member, make_post,
                             make_post_reply, make_post_vote, make_user)

WEBFINGER = 'https://remote.test/.well-known/webfinger'
ACTOR = 'https://remote.test/u/someone'


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    monkeypatch.setattr('app.user.utils.time.sleep', lambda seconds: None)
    return SimpleNamespace(app=app, baseline=api_baseline,
                           server=current_app.config['SERVER_NAME'])


def self_link(**extra):
    link = {'rel': 'self', 'type': 'application/activity+json', 'href': ACTOR}
    link.update(extra)
    return {'links': [link]}


PERSON = {'type': 'Person', 'id': ACTOR, 'preferredUsername': 'someone',
          'inbox': f'{ACTOR}/inbox', 'publicKey': {'id': f'{ACTOR}#main-key',
                                                   'owner': ACTOR,
                                                   'publicKeyPem': 'x'}}


# --------------------------------------------------------------------------
# search_for_user
# --------------------------------------------------------------------------

class TestAnAddressThatIsNotOne:
    """D1268. Each of these can come out of a URL segment or a search box."""

    @pytest.mark.parametrize('address', [
        '@a@b@c',            # two servers
        '@@example.test',    # no name
        '@',                 # nothing at all
        '@someone@',         # no server, with the marker
        'a@b@c',             # the same, without the leading marker
    ])
    def test_it_is_answered_with_nothing(self, env, address):
        assert search_for_user(address, allow_fetch=False) is None

    def test_a_bare_name_nobody_here_has(self, env):
        assert search_for_user('nobodyatall', allow_fetch=False) is None


class TestSomebodyThisInstanceAlreadyKnows:
    def test_a_local_account_is_found_by_name(self, env):
        assert search_for_user(env.baseline.user2.user_name,
                               allow_fetch=False) == env.baseline.user2

    def test_the_leading_marker_is_optional(self, env):
        assert search_for_user('@' + env.baseline.user2.user_name,
                               allow_fetch=False) == env.baseline.user2

    def test_a_remote_account_is_found_by_its_handle(self, env):
        remote = make_user(env.baseline.instance_remote, 'someone')
        remote.ap_id = 'someone@remote.test'
        db.session.commit()
        assert search_for_user('@someone@remote.test',
                               allow_fetch=False) == remote

    def test_the_handle_is_matched_in_lower_case(self, env):
        """D1272. `name` and `server` were lowercased and then the lookup
        used the ORIGINAL address, so a handle with a capital letter in it
        found nobody -- and with fetching allowed, asked the remote instance
        for somebody this one already had."""
        remote = make_user(env.baseline.instance_remote, 'someone')
        remote.ap_id = 'someone@remote.test'
        db.session.commit()
        assert search_for_user('@SomeOne@Remote.Test',
                               allow_fetch=False) == remote

    def test_a_remote_handle_is_not_mistaken_for_a_local_name(self, env):
        """The local branch requires `ap_id is None`."""
        assert search_for_user('@someone@remote.test',
                               allow_fetch=False) is None


class TestInstancesThisOneWillNotTalkTo:
    def test_a_banned_instance_is_refused_loudly(self, env):
        db.session.add(BannedInstances(domain='nasty.test',
                                       reason='spam'))
        db.session.commit()
        with pytest.raises(Exception, match='nasty.test is blocked. Reason: spam'):
            search_for_user('@someone@nasty.test')

    def test_a_banned_instance_with_no_reason_given(self, env):
        db.session.add(BannedInstances(domain='nasty.test'))
        db.session.commit()
        with pytest.raises(Exception, match='nasty.test is blocked.'):
            search_for_user('@someone@nasty.test')

    def test_the_ban_is_checked_before_anything_is_fetched(self, env,
                                                           http_mock):
        db.session.add(BannedInstances(domain='nasty.test'))
        db.session.commit()
        with pytest.raises(Exception):
            search_for_user('@someone@nasty.test')
        assert len(http_mock.calls) == 0


class TestWhatWebfingerSays:
    def test_a_webfinger_that_answers_404(self, env, http_mock):
        http_mock.get(WEBFINGER).mock(return_value=httpx.Response(404))
        assert search_for_user('@someone@remote.test') is None

    def test_a_webfinger_that_is_not_json(self, env, http_mock):
        http_mock.get(WEBFINGER).mock(
            return_value=httpx.Response(200, text='<html>nope</html>'))
        assert search_for_user('@someone@remote.test') is None

    def test_a_webfinger_with_no_links_key(self, env, http_mock):
        """D1269. This was `KeyError: 'links'`."""
        http_mock.get(WEBFINGER).mock(
            return_value=httpx.Response(200, json={'subject': 'acct:someone'}))
        assert search_for_user('@someone@remote.test') is None

    def test_a_webfinger_whose_links_are_not_a_list(self, env, http_mock):
        http_mock.get(WEBFINGER).mock(
            return_value=httpx.Response(200, json={'links': 'nonsense'}))
        assert search_for_user('@someone@remote.test') is None

    def test_links_that_are_not_iterable_at_all(self, env, http_mock):
        """`for links in 5` is `TypeError: 'int' object is not iterable`,
        which is why the list check is there as well as the per-link one."""
        http_mock.get(WEBFINGER).mock(
            return_value=httpx.Response(200, json={'links': 5}))
        assert search_for_user('@someone@remote.test') is None

    def test_links_that_are_an_object_rather_than_a_list(self, env,
                                                          http_mock):
        """Iterating a dict yields its KEYS, so `'rel' in links` was true of
        the string `'rel'` and `links['rel']` was `TypeError: string indices
        must be integers`."""
        http_mock.get(WEBFINGER).mock(return_value=httpx.Response(
            200, json={'links': {'rel': 'self', 'href': ACTOR}}))
        assert search_for_user('@someone@remote.test') is None

    def test_a_link_that_is_not_an_object(self, env, http_mock):
        http_mock.get(WEBFINGER).mock(
            return_value=httpx.Response(200, json={'links': ['nonsense']}))
        assert search_for_user('@someone@remote.test') is None

    def test_a_link_that_is_a_list(self, env, http_mock):
        """`'rel' in ['rel', 'self']` is true, and `links['rel']` on a list
        is `TypeError: list indices must be integers`."""
        http_mock.get(WEBFINGER).mock(return_value=httpx.Response(
            200, json={'links': [['rel', 'self']]}))
        assert search_for_user('@someone@remote.test') is None

    def test_a_self_link_with_no_href(self, env, http_mock):
        """D1269. This was `KeyError: 'href'`."""
        http_mock.get(WEBFINGER).mock(return_value=httpx.Response(
            200, json={'links': [{'rel': 'self',
                                  'type': 'application/activity+json'}]}))
        assert search_for_user('@someone@remote.test') is None

    def test_a_webfinger_with_no_self_link(self, env, http_mock):
        http_mock.get(WEBFINGER).mock(return_value=httpx.Response(
            200, json={'links': [{'rel': 'http://webfinger.net/rel/profile-page',
                                  'href': ACTOR}]}))
        assert search_for_user('@someone@remote.test') is None

    def test_a_link_with_no_rel_at_all(self, env, http_mock):
        http_mock.get(WEBFINGER).mock(return_value=httpx.Response(
            200, json={'links': [{'href': ACTOR}]}))
        assert search_for_user('@someone@remote.test') is None

    def test_a_self_link_with_no_type_is_asked_for_as_activity_json(
            self, env, http_mock):
        http_mock.get(WEBFINGER).mock(return_value=httpx.Response(
            200, json={'links': [{'rel': 'self', 'href': ACTOR}]}))
        actor = http_mock.get(ACTOR).mock(
            return_value=httpx.Response(200, json=PERSON))
        search_for_user('@someone@remote.test')
        assert actor.calls[0].request.headers['Accept'] == \
            'application/activity+json'

    def test_a_webfinger_that_never_answers_raises(self, env, http_mock):
        """Nothing here catches it -- the source says so: "todo: try, except
        block around every get_request". Pinned as it stands so that changing
        it is a decision rather than an accident."""
        http_mock.get(WEBFINGER).mock(side_effect=httpx.ConnectError('no route'))
        with pytest.raises(httpx.HTTPError):
            search_for_user('@someone@remote.test')


class TestWhatTheActorSays:
    def test_a_person_is_created(self, env, http_mock):
        http_mock.get(WEBFINGER).mock(
            return_value=httpx.Response(200, json=self_link()))
        http_mock.get(ACTOR).mock(return_value=httpx.Response(200, json=PERSON))
        user = search_for_user('@someone@remote.test')
        assert user is not None
        assert user.ap_id == 'someone@remote.test'

    def test_a_service_is_created_too(self, env, http_mock):
        http_mock.get(WEBFINGER).mock(
            return_value=httpx.Response(200, json=self_link()))
        http_mock.get(ACTOR).mock(return_value=httpx.Response(
            200, json=dict(PERSON, type='Service')))
        assert search_for_user('@someone@remote.test') is not None

    def test_an_actor_with_no_type(self, env, http_mock):
        """D1269. This was `KeyError: 'type'`."""
        http_mock.get(WEBFINGER).mock(
            return_value=httpx.Response(200, json=self_link()))
        http_mock.get(ACTOR).mock(return_value=httpx.Response(
            200, json={'id': ACTOR, 'preferredUsername': 'someone'}))
        assert search_for_user('@someone@remote.test') is None

    def test_an_actor_that_is_a_community(self, env, http_mock):
        http_mock.get(WEBFINGER).mock(
            return_value=httpx.Response(200, json=self_link()))
        http_mock.get(ACTOR).mock(return_value=httpx.Response(
            200, json=dict(PERSON, type='Group')))
        assert search_for_user('@someone@remote.test') is None

    def test_an_actor_that_answers_404(self, env, http_mock):
        http_mock.get(WEBFINGER).mock(
            return_value=httpx.Response(200, json=self_link()))
        http_mock.get(ACTOR).mock(return_value=httpx.Response(404))
        assert search_for_user('@someone@remote.test') is None

    def test_an_actor_that_is_not_json(self, env, http_mock):
        http_mock.get(WEBFINGER).mock(
            return_value=httpx.Response(200, json=self_link()))
        http_mock.get(ACTOR).mock(
            return_value=httpx.Response(200, text='<html>nope</html>'))
        assert search_for_user('@someone@remote.test') is None


class TestHowOftenTheRemoteIsAsked:
    """D1270. The retry loop had no `break`, so a request that succeeded was
    made again -- two fetches per lookup, four for an actor behind
    authorized-fetch."""

    def test_an_actor_that_answers_is_fetched_once(self, env, http_mock):
        http_mock.get(WEBFINGER).mock(
            return_value=httpx.Response(200, json=self_link()))
        actor = http_mock.get(ACTOR).mock(
            return_value=httpx.Response(200, json=PERSON))
        search_for_user('@someone@remote.test')
        assert actor.call_count == 1

    def test_a_connection_error_is_retried(self, env, http_mock):
        """`get_request`'s own retry is what recovers here, so the loop in
        `search_for_user` never reaches its second pass."""
        http_mock.get(WEBFINGER).mock(
            return_value=httpx.Response(200, json=self_link()))
        actor = http_mock.get(ACTOR).mock(side_effect=[
            httpx.ConnectError('no route'),
            httpx.Response(200, json=PERSON)])
        assert search_for_user('@someone@remote.test') is not None
        assert actor.call_count == 2

    def test_an_actor_that_never_answers_is_given_up_on(self, env, http_mock):
        """Four requests, not two: `get_request` retries once by itself, and
        the loop here wraps another retry around it. The loop is nearly
        redundant, which is recorded rather than changed -- what matters is
        that a remote that never answers is eventually given up on."""
        http_mock.get(WEBFINGER).mock(
            return_value=httpx.Response(200, json=self_link()))
        actor = http_mock.get(ACTOR).mock(
            side_effect=httpx.ConnectError('no route'))
        assert search_for_user('@someone@remote.test') is None
        assert actor.call_count == 4


class TestAnActorBehindAuthorizedFetch:
    def test_a_401_is_asked_again_with_a_signature(self, env, http_mock):
        http_mock.get(WEBFINGER).mock(
            return_value=httpx.Response(200, json=self_link()))
        http_mock.get(ACTOR).mock(return_value=httpx.Response(401))
        with patch('app.user.utils.signed_get_request',
                   return_value=httpx.Response(200, json=PERSON)) as signed:
            user = search_for_user('@someone@remote.test')
        assert signed.call_count == 1
        assert user is not None

    def test_the_signed_request_is_retried_once(self, env, http_mock):
        http_mock.get(WEBFINGER).mock(
            return_value=httpx.Response(200, json=self_link()))
        http_mock.get(ACTOR).mock(return_value=httpx.Response(401))
        with patch('app.user.utils.signed_get_request',
                   side_effect=[httpx.ConnectError('no route'),
                                httpx.Response(200, json=PERSON)]) as signed:
            user = search_for_user('@someone@remote.test')
        assert signed.call_count == 2
        assert user is not None

    def test_a_signed_request_that_never_answers(self, env, http_mock):
        http_mock.get(WEBFINGER).mock(
            return_value=httpx.Response(200, json=self_link()))
        http_mock.get(ACTOR).mock(return_value=httpx.Response(401))
        with patch('app.user.utils.signed_get_request',
                   side_effect=httpx.ConnectError('no route')):
            assert search_for_user('@someone@remote.test') is None

    def test_a_signed_request_that_is_also_refused(self, env, http_mock):
        http_mock.get(WEBFINGER).mock(
            return_value=httpx.Response(200, json=self_link()))
        http_mock.get(ACTOR).mock(return_value=httpx.Response(401))
        with patch('app.user.utils.signed_get_request',
                   return_value=httpx.Response(403)):
            assert search_for_user('@someone@remote.test') is None


class TestWhenTheRemoteMayNotBeAsked:
    def test_an_unknown_handle_with_fetching_turned_off(self, env, http_mock):
        assert search_for_user('@someone@remote.test',
                               allow_fetch=False) is None
        assert len(http_mock.calls) == 0

    def test_an_unknown_local_name_is_never_fetched(self, env, http_mock):
        assert search_for_user('nobodyatall') is None
        assert len(http_mock.calls) == 0


# --------------------------------------------------------------------------
# unsubscribing, on behalf of an account being deleted
# --------------------------------------------------------------------------

class TestUnsubscribingFromACommunity:
    @pytest.fixture
    def remote_community(self, env):
        community = make_community('faraway', host='remote.test')
        community.ap_id = 'faraway@remote.test'
        community.ap_inbox_url = 'https://remote.test/c/faraway/inbox'
        db.session.commit()
        return community

    def test_an_undo_follow_is_sent(self, env, remote_community):
        user = env.baseline.user2
        with patch('app.user.utils.send_post_request') as send:
            unsubscribe_from_community(remote_community, user)
        assert send.call_count == 1
        inbox, activity = send.call_args.args[0], send.call_args.args[1]
        assert inbox == 'https://remote.test/c/faraway/inbox'
        assert activity['type'] == 'Undo'
        assert activity['object']['type'] == 'Follow'
        assert activity['object']['object'] == remote_community.public_url()
        assert activity['actor'] == user.public_url()

    def test_an_instance_that_is_gone_is_not_written_to(self, env,
                                                        remote_community):
        remote_community.instance.gone_forever = True
        db.session.commit()
        with patch('app.user.utils.send_post_request') as send:
            unsubscribe_from_community(remote_community, env.baseline.user2)
        assert send.call_count == 0

    def test_a_dormant_instance_is_not_written_to(self, env, remote_community):
        remote_community.instance.dormant = True
        db.session.commit()
        with patch('app.user.utils.send_post_request') as send:
            unsubscribe_from_community(remote_community, env.baseline.user2)
        assert send.call_count == 0


# --------------------------------------------------------------------------
# the profile page
# --------------------------------------------------------------------------

@pytest.fixture
def scene(env):
    """An account with one public post, one deleted post, and one post and
    reply in a private community it has since been banned from."""
    from types import SimpleNamespace

    owner = env.baseline.user2
    public = make_community('townsquare', host=env.server)
    open_post = make_post(public, owner, 'https://x.test/p/open',
                          title='in the open')
    open_reply = make_post_reply(open_post, owner, body='said openly')
    gone = make_post(public, owner, 'https://x.test/p/gone', title='taken down')
    gone.deleted = True
    gone.deleted_by = owner.id

    private = make_community('secrets', host=env.server)
    private.private = True
    db.session.commit()
    membership = make_community_member(owner, private)
    secret_post = make_post(private, owner, 'https://x.test/p/secret',
                            title='in private')
    secret_post.private = True
    secret_reply = make_post_reply(secret_post, owner, body='said privately')
    secret_reply.private = True
    db.session.commit()
    return SimpleNamespace(owner=owner, public=public, private=private,
                           open_post=open_post, open_reply=open_reply,
                           gone=gone, secret_post=secret_post,
                           secret_reply=secret_reply, membership=membership,
                           admin=env.baseline.user1,
                           stranger=env.baseline.user3)


def as_viewer(app, viewer=None):
    """A request context with `viewer` signed in, or nobody."""
    context = app.test_request_context('/')
    context.push()
    g.admin_ids = []
    if viewer is not None:
        login_user(viewer)
    return context


def release(context):
    logout_user()
    context.pop()


def titles(items):
    return {getattr(item, 'title', None) or getattr(item, 'body', None)
            for item in items}


class TestTheOverviewTab:
    """D1271. Posts and replies interleaved, and the only tab that did not
    filter the private flag."""

    def test_a_stranger_is_not_shown_a_private_community_post(self, env,
                                                              scene):
        scene.membership.is_banned = True
        db.session.commit()
        context = as_viewer(env.app)
        try:
            items, _ = _get_user_posts_and_replies(scene.owner, 1)
        finally:
            release(context)
        assert 'in private' not in titles(items)
        assert 'said privately' not in titles(items)
        assert 'in the open' in titles(items)

    def test_nor_after_the_account_has_simply_left(self, env, scene):
        db.session.delete(scene.membership)
        db.session.commit()
        context = as_viewer(env.app)
        try:
            items, _ = _get_user_posts_and_replies(scene.owner, 1)
        finally:
            release(context)
        assert 'in private' not in titles(items)
        assert 'said privately' not in titles(items)

    def test_nor_while_the_account_is_still_a_member(self, env, scene):
        """The membership filter covers this one; the private flag covers it
        too, and either alone would be enough here."""
        context = as_viewer(env.app)
        try:
            items, _ = _get_user_posts_and_replies(scene.owner, 1)
        finally:
            release(context)
        assert 'in private' not in titles(items)

    def test_a_stranger_is_not_shown_deleted_posts(self, env, scene):
        context = as_viewer(env.app, scene.stranger)
        try:
            items, _ = _get_user_posts_and_replies(scene.owner, 1)
        finally:
            release(context)
        assert 'taken down' not in titles(items)

    def test_the_account_itself_sees_what_it_deleted(self, env, scene):
        context = as_viewer(env.app, scene.owner)
        try:
            items, _ = _get_user_posts_and_replies(scene.owner, 1)
        finally:
            release(context)
        assert 'taken down' in titles(items)

    def test_an_admin_sees_everything(self, env, scene):
        context = as_viewer(env.app, scene.admin)
        g.admin_ids = [scene.admin.id]
        try:
            items, _ = _get_user_posts_and_replies(scene.owner, 1)
        finally:
            release(context)
        assert 'taken down' in titles(items)

    def test_a_second_page_is_offered_when_there_is_one(self, env, scene):
        for index in range(21):
            make_post(scene.public, scene.owner,
                      f'https://x.test/p/bulk{index}', title=f'bulk {index}')
        db.session.commit()
        context = as_viewer(env.app)
        try:
            items, has_next = _get_user_posts_and_replies(scene.owner, 1)
            page_two, _ = _get_user_posts_and_replies(scene.owner, 2)
        finally:
            release(context)
        assert has_next is True
        assert len(items) == 20
        assert page_two


class TestThePostsTab:
    def test_a_stranger_sees_neither_deleted_nor_private(self, env, scene):
        scene.membership.is_banned = True
        db.session.commit()
        context = as_viewer(env.app)
        try:
            page = _get_user_posts(scene.owner, 1)
        finally:
            release(context)
        assert 'in the open' in titles(page.items)
        assert 'in private' not in titles(page.items)
        assert 'taken down' not in titles(page.items)

    def test_the_account_itself_sees_what_it_deleted(self, env, scene):
        context = as_viewer(env.app, scene.owner)
        try:
            page = _get_user_posts(scene.owner, 1)
        finally:
            release(context)
        assert 'taken down' in titles(page.items)

    def test_an_admin_sees_everything(self, env, scene):
        context = as_viewer(env.app, scene.admin)
        g.admin_ids = [scene.admin.id]
        try:
            page = _get_user_posts(scene.owner, 1)
        finally:
            release(context)
        assert 'taken down' in titles(page.items)

    def test_pagination_reports_what_is_there(self, env, scene):
        for index in range(25):
            make_post(scene.public, scene.owner,
                      f'https://x.test/p/bulk{index}', title=f'bulk {index}')
        db.session.commit()
        context = as_viewer(env.app)
        try:
            first = _get_user_posts(scene.owner, 1)
            second = _get_user_posts(scene.owner, 2)
        finally:
            release(context)
        assert first.has_next is True
        assert first.has_prev is False
        assert second.has_prev is True
        assert second.page == 2


class TestTheRepliesTab:
    def test_a_stranger_sees_neither_deleted_nor_private(self, env, scene):
        scene.membership.is_banned = True
        scene.open_reply.deleted = False
        db.session.commit()
        context = as_viewer(env.app)
        try:
            page = _get_user_post_replies(scene.owner, 1)
        finally:
            release(context)
        assert 'said openly' in titles(page.items)
        assert 'said privately' not in titles(page.items)

    def test_the_account_itself_sees_what_it_deleted(self, env, scene):
        scene.open_reply.deleted = True
        scene.open_reply.deleted_by = scene.owner.id
        db.session.commit()
        context = as_viewer(env.app, scene.owner)
        try:
            page = _get_user_post_replies(scene.owner, 1)
        finally:
            release(context)
        assert 'said openly' in titles(page.items)

    def test_a_stranger_does_not(self, env, scene):
        scene.open_reply.deleted = True
        scene.open_reply.deleted_by = scene.owner.id
        db.session.commit()
        context = as_viewer(env.app, scene.stranger)
        try:
            page = _get_user_post_replies(scene.owner, 1)
        finally:
            release(context)
        assert 'said openly' not in titles(page.items)

    def test_an_admin_sees_everything(self, env, scene):
        scene.open_reply.deleted = True
        db.session.commit()
        context = as_viewer(env.app, scene.admin)
        g.admin_ids = [scene.admin.id]
        try:
            page = _get_user_post_replies(scene.owner, 1)
        finally:
            release(context)
        assert 'said openly' in titles(page.items)


class TestTheOtherProfileBlocks:
    def test_archived_replies_are_listed_newest_first(self, env, scene):
        from datetime import timedelta

        from app.models import ArchivedPostReply
        from app.utils import utcnow
        older = ArchivedPostReply(user_id=scene.owner.id, post_reply_id=1,
                                  created_at=utcnow() - timedelta(days=2))
        newer = ArchivedPostReply(user_id=scene.owner.id, post_reply_id=2,
                                  created_at=utcnow())
        db.session.add_all([older, newer])
        db.session.commit()
        assert [r.post_reply_id
                for r in _get_user_archived_replies(scene.owner)] == [2, 1]

    def test_only_this_account_s_archived_replies(self, env, scene):
        from app.models import ArchivedPostReply
        from app.utils import utcnow
        db.session.add(ArchivedPostReply(user_id=scene.stranger.id,
                                         post_reply_id=3,
                                         created_at=utcnow()))
        db.session.commit()
        assert _get_user_archived_replies(scene.owner) == []

    def test_the_communities_an_account_moderates(self, env, scene):
        make_community_member(scene.owner, scene.public, is_moderator=True)
        db.session.commit()
        context = as_viewer(env.app, scene.stranger)
        try:
            assert scene.public in _get_user_moderates(scene.owner)
        finally:
            release(context)

    def test_a_private_mod_list_is_hidden_from_strangers(self, env, scene):
        scene.public.private_mods = True
        make_community_member(scene.owner, scene.public, is_moderator=True)
        db.session.commit()
        context = as_viewer(env.app, scene.stranger)
        try:
            assert _get_user_moderates(scene.owner) == []
        finally:
            release(context)

    def test_and_from_anonymous_visitors(self, env, scene):
        scene.public.private_mods = True
        make_community_member(scene.owner, scene.public, is_moderator=True)
        db.session.commit()
        context = as_viewer(env.app)
        try:
            assert _get_user_moderates(scene.owner) == []
        finally:
            release(context)

    def test_but_not_from_the_account_itself(self, env, scene):
        scene.public.private_mods = True
        make_community_member(scene.owner, scene.public, is_moderator=True)
        db.session.commit()
        context = as_viewer(env.app, scene.owner)
        try:
            assert scene.public in _get_user_moderates(scene.owner)
        finally:
            release(context)

    def test_a_banned_community_is_never_listed(self, env, scene):
        scene.public.banned = True
        make_community_member(scene.owner, scene.public, is_moderator=True)
        db.session.commit()
        context = as_viewer(env.app, scene.owner)
        try:
            assert _get_user_moderates(scene.owner) == []
        finally:
            release(context)


class TestOtherAccountsOnTheSameAddress:
    """The list of alt accounts. The template gates the block on
    `is_admin_or_staff`; so does the function now."""

    @pytest.fixture
    def alt(self, env, scene):
        scene.owner.ip_address = '10.0.0.9'
        other = make_user(env.baseline.instance_local, 'altaccount',
                          local=True)
        other.ip_address = '10.0.0.9'
        db.session.commit()
        return other

    def test_an_admin_is_shown_them(self, env, scene, alt):
        context = as_viewer(env.app, scene.admin)
        g.admin_ids = [scene.admin.id]
        try:
            assert alt in _get_user_same_ip(scene.owner)
        finally:
            release(context)

    def test_an_ordinary_member_is_not(self, env, scene, alt):
        context = as_viewer(env.app, scene.stranger)
        try:
            assert _get_user_same_ip(scene.owner) == []
        finally:
            release(context)

    def test_nor_is_a_stranger(self, env, scene, alt):
        context = as_viewer(env.app)
        try:
            assert _get_user_same_ip(scene.owner) == []
        finally:
            release(context)

    def test_an_account_with_no_address_on_file(self, env, scene, alt):
        scene.owner.ip_address = None
        db.session.commit()
        context = as_viewer(env.app, scene.admin)
        g.admin_ids = [scene.admin.id]
        try:
            assert _get_user_same_ip(scene.owner) == []
        finally:
            release(context)

    def test_an_account_whose_address_is_empty(self, env, scene, alt):
        scene.owner.ip_address = ''
        db.session.commit()
        context = as_viewer(env.app, scene.admin)
        g.admin_ids = [scene.admin.id]
        try:
            assert _get_user_same_ip(scene.owner) == []
        finally:
            release(context)

    def test_the_account_itself_is_not_its_own_alt(self, env, scene, alt):
        context = as_viewer(env.app, scene.admin)
        g.admin_ids = [scene.admin.id]
        try:
            assert scene.owner not in _get_user_same_ip(scene.owner)
        finally:
            release(context)


class TestUpvotedPosts:
    @pytest.fixture
    def upvoted(self, env, scene):
        post = make_post(scene.public, scene.stranger, 'https://x.test/p/liked',
                         title='liked')
        make_post_vote(scene.owner, post, 1)
        db.session.commit()
        return post

    def test_the_account_itself_sees_them(self, env, scene, upvoted):
        context = as_viewer(env.app, scene.owner)
        try:
            assert upvoted in _get_user_upvoted_posts(scene.owner)
        finally:
            release(context)

    def test_an_admin_sees_them(self, env, scene, upvoted):
        context = as_viewer(env.app, scene.admin)
        g.admin_ids = [scene.admin.id]
        try:
            assert upvoted in _get_user_upvoted_posts(scene.owner)
        finally:
            release(context)

    def test_another_member_does_not(self, env, scene, upvoted):
        context = as_viewer(env.app, scene.stranger)
        try:
            assert _get_user_upvoted_posts(scene.owner) == []
        finally:
            release(context)

    def test_a_stranger_does_not(self, env, scene, upvoted):
        context = as_viewer(env.app)
        try:
            assert _get_user_upvoted_posts(scene.owner) == []
        finally:
            release(context)

    def test_a_downvote_is_not_an_upvote(self, env, scene):
        post = make_post(scene.public, scene.stranger,
                         'https://x.test/p/disliked', title='disliked')
        make_post_vote(scene.owner, post, -1)
        db.session.commit()
        context = as_viewer(env.app, scene.owner)
        try:
            assert _get_user_upvoted_posts(scene.owner) == []
        finally:
            release(context)


class TestSubscribedCommunities:
    def test_the_account_itself_sees_them(self, env, scene):
        make_community_member(scene.owner, scene.public)
        scene.owner.show_subscribed_communities = False
        db.session.commit()
        context = as_viewer(env.app, scene.owner)
        try:
            assert scene.public in _get_user_subscribed_communities(scene.owner)
        finally:
            release(context)

    def test_an_admin_sees_them(self, env, scene):
        make_community_member(scene.owner, scene.public)
        scene.owner.show_subscribed_communities = False
        db.session.commit()
        context = as_viewer(env.app, scene.admin)
        g.admin_ids = [scene.admin.id]
        try:
            assert scene.public in _get_user_subscribed_communities(scene.owner)
        finally:
            release(context)

    def test_another_member_sees_them_only_if_the_account_allows_it(
            self, env, scene):
        make_community_member(scene.owner, scene.public)
        scene.owner.show_subscribed_communities = True
        db.session.commit()
        context = as_viewer(env.app, scene.stranger)
        try:
            assert scene.public in _get_user_subscribed_communities(scene.owner)
        finally:
            release(context)

    def test_and_not_when_it_does_not(self, env, scene):
        make_community_member(scene.owner, scene.public)
        scene.owner.show_subscribed_communities = False
        db.session.commit()
        context = as_viewer(env.app, scene.stranger)
        try:
            assert _get_user_subscribed_communities(scene.owner) == []
        finally:
            release(context)

    def test_a_stranger_never_does(self, env, scene):
        make_community_member(scene.owner, scene.public)
        scene.owner.show_subscribed_communities = True
        db.session.commit()
        context = as_viewer(env.app)
        try:
            assert _get_user_subscribed_communities(scene.owner) == []
        finally:
            release(context)

    def test_a_banned_community_is_never_listed(self, env, scene):
        make_community_member(scene.owner, scene.public)
        scene.public.banned = True
        db.session.commit()
        context = as_viewer(env.app, scene.owner)
        try:
            assert scene.public not in \
                _get_user_subscribed_communities(scene.owner)
        finally:
            release(context)


class TestNotesOnAnAccount:
    def test_a_note_is_written(self, env, scene):
        context = as_viewer(env.app, scene.stranger)
        try:
            insert_or_update_user_note('one to watch', scene.owner)
        finally:
            release(context)
        note = UserNote.query.filter_by(target_id=scene.owner.id,
                                        user_id=scene.stranger.id).one()
        assert note.body == 'one to watch'

    def test_writing_again_replaces_it_rather_than_adding_one(self, env,
                                                              scene):
        context = as_viewer(env.app, scene.stranger)
        try:
            insert_or_update_user_note('first thought', scene.owner)
            insert_or_update_user_note('second thought', scene.owner)
        finally:
            release(context)
        notes = UserNote.query.filter_by(target_id=scene.owner.id,
                                         user_id=scene.stranger.id).all()
        assert len(notes) == 1
        assert notes[0].body == 'second thought'

    def test_two_people_keep_their_own_notes(self, env, scene):
        context = as_viewer(env.app, scene.stranger)
        try:
            insert_or_update_user_note('what one thinks', scene.owner)
        finally:
            release(context)
        context = as_viewer(env.app, scene.admin)
        try:
            insert_or_update_user_note('what another thinks', scene.owner)
        finally:
            release(context)
        assert UserNote.query.filter_by(target_id=scene.owner.id).count() == 2


class TestSimplePagination:
    def test_it_reports_what_it_was_given(self):
        page = SimplePagination(['a', 'b'], 2, 20, has_next=True,
                                has_prev=True)
        assert (page.page, page.per_page, page.total) == (2, 20, 2)
        assert (page.next_num, page.prev_num) == (3, 1)
        assert list(page) == ['a', 'b']

    def test_the_first_page_has_nothing_before_it(self):
        page = SimplePagination([], 1, 20, has_next=False, has_prev=False)
        assert page.next_num is None
        assert page.prev_num is None
        assert page.total == 0


# --------------------------------------------------------------------------
# deleting an account and everything it wrote
# --------------------------------------------------------------------------

class TestPurgingAnAccount:
    """`purge_user_then_delete` is what an admin's "ban, delete and remove
    all their content" runs. It is queued, so its failures are a traceback in
    a worker log rather than anything the admin sees."""

    @pytest.fixture
    def doomed(self, env, scene):
        """The account from `scene`, plus a membership to unsubscribe from."""
        remote = make_community('faraway', host='remote.test')
        remote.ap_id = 'faraway@remote.test'
        remote.ap_inbox_url = 'https://remote.test/c/faraway/inbox'
        db.session.commit()
        make_community_member(scene.owner, remote)
        db.session.commit()
        return scene.owner

    def run_task(self, user_id, flush=True):
        from app.user.utils import purge_user_then_delete_task
        with patch('app.user.utils.task_selector') as selector, \
                patch('app.user.utils.unsubscribe_from_community') as unsub, \
                patch.object(User, 'delete_dependencies'), \
                patch.object(User, 'purge_content') as purge:
            purge_user_then_delete_task(user_id, flush)
        return selector, unsub, purge

    def test_the_account_ends_up_deleted(self, env, doomed):
        user_id = doomed.id
        self.run_task(user_id)
        db.session.expire_all()
        assert db.session.get(User, user_id).deleted is True

    def test_every_post_and_reply_is_queued_for_deletion(self, env, doomed):
        selector, _, _ = self.run_task(doomed.id)
        actions = [call.args[0] for call in selector.call_args_list]
        assert 'delete_post' in actions
        assert 'delete_reply' in actions

    def test_every_community_is_unsubscribed_from(self, env, doomed):
        _, unsub, _ = self.run_task(doomed.id)
        assert unsub.call_count >= 1

    def test_the_flush_flag_is_the_cdn_one(self, env, doomed):
        """D1273. `purge_content(flush)` put the CDN flag into `soft`, so a
        deletion with the CDN purge turned OFF hard-deleted every post and
        reply from the database -- and purged the CDN anyway, since `flush`
        then fell back to its default."""
        _, _, purge = self.run_task(doomed.id, flush=False)
        assert purge.call_args.kwargs == {'flush': False}
        assert purge.call_args.args == ()

    def test_an_account_that_is_already_gone(self, env):
        """The task is queued with an id; by the time it runs the row may not
        be there."""
        self.run_task(999999)

    def test_a_failure_is_rolled_back_and_raised(self, env, doomed):
        from app.user.utils import purge_user_then_delete_task
        with patch('app.user.utils.task_selector',
                   side_effect=Exception('the queue is down')):
            with pytest.raises(Exception, match='the queue is down'):
                purge_user_then_delete_task(doomed.id, True)
        db.session.expire_all()
        assert db.session.get(User, doomed.id).deleted is not True


class TestHowThePurgeIsDispatched:
    def test_in_debug_it_runs_here_and_now(self, env, monkeypatch):
        monkeypatch.setattr(current_app, 'debug', True)
        with patch('app.user.utils.purge_user_then_delete_task') as task:
            from app.user.utils import purge_user_then_delete
            purge_user_then_delete(7, flush=False)
        task.assert_called_once_with(7, False)

    def test_otherwise_it_is_queued(self, env, monkeypatch):
        monkeypatch.setattr(current_app, 'debug', False)
        with patch('app.user.utils.purge_user_then_delete_task') as task:
            from app.user.utils import purge_user_then_delete
            purge_user_then_delete(7, flush=True)
        task.delay.assert_called_once_with(7, True)


class TestAnAccountInNoPrivateCommunities:
    """The overview query builds its exclusion list first and only appends a
    `NOT IN` when there is something to exclude."""

    def test_an_admin_looking_at_one(self, env, scene):
        context = as_viewer(env.app, scene.admin)
        g.admin_ids = [scene.admin.id]
        try:
            items, _ = _get_user_posts_and_replies(scene.stranger, 1)
        finally:
            release(context)
        assert isinstance(items, list)

    def test_the_account_itself(self, env, scene):
        context = as_viewer(env.app, scene.stranger)
        try:
            items, _ = _get_user_posts_and_replies(scene.stranger, 1)
        finally:
            release(context)
        assert isinstance(items, list)

    def test_a_stranger_looking_at_one(self, env, scene):
        context = as_viewer(env.app)
        try:
            items, _ = _get_user_posts_and_replies(scene.stranger, 1)
        finally:
            release(context)
        assert isinstance(items, list)
