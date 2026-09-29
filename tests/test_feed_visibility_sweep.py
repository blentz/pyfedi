"""D1394: a feed id from the request, read without asking who is asking.

`show_feed` (app/feed/routes.py) refuses a private feed to anybody but its owner
and its members, `/f/<name>/following` and `/f/<name>/outbox` answer 403 for one,
and the API's `get_feed` raises `access_denied` -- so a private feed's membership
is a thing this codebase protects, and D1173 has already repaired one endpoint
that took a feed id and joined without asking.

Ten other readers of a caller-supplied feed id asked nothing at all, and
`show_feed` itself asked too loosely. Each of the ten reports the feed's
membership in some form; the plainest is the community browser, which reports it
exactly:

    PROBE (anonymous, feed 'secretfeed' public=False holding 'hiddencomm')
      GET /f/secretfeed               302 -> /feeds   'not public'
      GET /communities?feed_id=1      200   hiddencomm present: True
                                            publiccomm present: False

The response is a readout of the private feed, filtered to it and nothing else,
to a visitor with no account. The other nine:

  * /health2?feed_id=              the same body, discarding its rows
  * /tag/<t>?category=feed&category_id=   the feed's posts, no login
  * /tags/cloud/feed/<id>          the feed's tag cloud, no login
  * /tags/posts/<id>?feed_id=      the feed's posts again, no login
  * /f/<name>.rss                  show_feed's RSS twin, which had no gate
  * /f/<name>/submit               the feed's communities in a dropdown
  * /feed/<id>/copy                a copy of the feed, in the caller's account
  * /api/alpha/post/list?feed_id=  D1173's shape, one file over
  * /api/alpha/post/list2?feed_id= the same, duplicated

All eleven -- the ten and `show_feed` -- now ask
`feed_readable_by(feed, user_id)` (app/utils.py), which is where the rule lives
rather than in any one of them. Two more sites, `/f/<name>/moderators` and
`/f/<name>/followers`, take the bare `if not feed.public: abort(403)` their three
ActivityPub siblings already had, because a peer's request carries no session.

THE RULE TIGHTENED WHILE BEING MOVED. `show_feed`'s member arm tested
`feed.subscribed(current_user.id)` for truth, and that call answers
SUBSCRIPTION_PENDING (-1) for an unapproved join request and SUBSCRIPTION_BANNED
(-2) for a member the owner threw out. Both are truthy. So asking to join a
private feed was enough to read it -- which is the whole of the approval gate --
and being banned from one did not stop you. The helper compares
`>= SUBSCRIPTION_MEMBER`, as app/feed/routes.py:134, :781 and
app/community/routes.py:229 already did.

WHAT THE REFUSALS LOOK LIKE, and why they differ. An unreadable feed is answered
the way each route already answers a feed id that names nothing -- 404 where
there is an `or abort(404)`, an empty list where the id merely filters, a
redirect on `show_feed`. That keeps 'private' and 'absent' indistinguishable,
which is what `show_feed`'s own wording ('Could not find that feed or it is not
public') has always claimed.

A HARNESS NOTE. `make_local_feed`'s `public` defaults to False, matching the
column. Eight tests in tests/test_tag_cloud.py, tests/test_tag_lists.py and
tests/test_tag_page.py took that default and then made an anonymous request, so
they were asserting on the leak; they now pass `public=True`, because tree
traversal is what they are about.

AN INCIDENTAL, in the same lines. Ten copies of
`FeedItem.query.join(Feed, FeedItem.feed_id == <id>)` are now `filter_by`. The
join names no predicate between the two tables, so it crossed every matching
FeedItem with every row of `feed`; measured on four feeds holding two items, the
database returned eight rows and `.all()` returned two, because `Query.all()`
over a single full entity uniquifies by primary key. Every answer was therefore
right and none of the work needed doing -- but `db.session.execute()` does not
uniquify, so a rewrite of any of them would have inherited the duplicates.
app/api/alpha/utils/post.py had already been repaired alone; this is the sweep.
"""
import pytest
from unittest.mock import patch

from flask import current_app, g, session
from flask_wtf.csrf import generate_csrf

from app import db
from app.constants import SUBSCRIPTION_BANNED, SUBSCRIPTION_MEMBER, \
    SUBSCRIPTION_NONMEMBER, SUBSCRIPTION_PENDING
from app.models import FeedMember, Language, Site, Tag, post_tag
from app.utils import feed_readable_by
from tests.factories import (a_keypair, make_community, make_community_member,
                             make_feed_item, make_feed_join_request,
                             make_feed_member, make_instance, make_local_feed,
                             make_post, make_user)

pytestmark = pytest.mark.usefixtures('site')
HOST = 'test.piefed.local'

# The two communities every test below distinguishes. Neither name is a
# substring of the other (fact 777): a page that leaks one and a page that
# leaks both must not read the same.
INSIDE = 'feedonlycomm'
OUTSIDE = 'unrelatedcomm'


def login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def csrf(app, client):
    with app.test_request_context():
        token = generate_csrf()
        raw = session['csrf_token']
    with client.session_transaction() as sess:
        sess['csrf_token'] = raw
    return token


@pytest.fixture
def env(app, db_session):
    """A private feed holding one community, its owner, and a stranger.

    `private_instance` off, because half of these routes are anonymous and
    `login_required_if_private_instance` would answer the login page for all of
    them -- a 302 that looks like a refusal and is not this one.
    """
    from types import SimpleNamespace
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    instance = make_instance(HOST, software='piefed')
    founder = make_user(instance, 'founder', local=True)
    assert founder.id == 1
    owner = make_user(instance, 'feedowner', local=True)
    stranger = make_user(instance, 'stranger', local=True)
    for user in (owner, stranger):
        user.verified = True
        user.private_key = 'a-key'
        # `make_user` leaves `ap_profile_id` None for a local account, and
        # /f/<name>/moderators answers with exactly that column -- so a test
        # asserting the owner is not named needs a value to look for.
        user.ap_profile_id = f'https://{HOST}/u/{user.user_name}'
    secret = make_local_feed('secretfeed', public=False)
    secret.user_id = owner.id
    open_feed = make_local_feed('openfeed', public=True)
    open_feed.user_id = owner.id
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.commit()
    inside = make_community(INSIDE)
    outside = make_community(OUTSIDE)
    make_feed_item(secret, inside)
    make_feed_item(open_feed, inside)
    make_community_member(stranger, inside)
    g.admin_ids = []
    return SimpleNamespace(instance=instance, owner=owner, stranger=stranger,
                           secret=secret, open_feed=open_feed,
                           inside=inside, outside=outside)


def _tagged(tag, community, author, title):
    post = make_post(community, author,
                     ap_id=f'https://{HOST}/post/{title}', title=title)
    db.session.execute(post_tag.insert().values(post_id=post.id, tag_id=tag.id))
    db.session.commit()
    return post


@pytest.fixture
def tagged(env):
    """One tag on a post inside the feed and on a post outside it."""
    tag = Tag(name='solarstorm', display_as='solarstorm', banned=False,
              post_count=0)
    db.session.add(tag)
    db.session.commit()
    _tagged(tag, env.inside, env.owner, 'insidepost')
    _tagged(tag, env.outside, env.owner, 'outsidepost')
    return tag


# --------------------------------------------------------------------------
# The rule itself
# --------------------------------------------------------------------------


class TestTheRule:
    def test_a_public_feed_is_readable_by_anybody(self, app, env):
        assert feed_readable_by(env.open_feed, None) is True

    def test_a_private_feed_is_not_readable_anonymously(self, app, env):
        assert feed_readable_by(env.secret, None) is False

    def test_a_private_feed_is_not_readable_by_a_stranger(self, app, env):
        assert feed_readable_by(env.secret, env.stranger.id) is False

    def test_a_private_feed_is_readable_by_its_owner(self, app, env):
        assert feed_readable_by(env.secret, env.owner.id) is True

    def test_a_private_feed_is_readable_by_a_member(self, app, env):
        make_feed_member(env.stranger, env.secret)
        assert env.secret.subscribed(env.stranger.id) == SUBSCRIPTION_MEMBER
        assert feed_readable_by(env.secret, env.stranger.id) is True

    def test_an_unapproved_join_request_is_not_membership(self, app, env):
        """SUBSCRIPTION_PENDING is -1, which is truthy. `show_feed` tested truth,
        so asking to join a private feed was the same as being let in."""
        make_feed_join_request(env.stranger, env.secret)
        assert env.secret.subscribed(env.stranger.id) == SUBSCRIPTION_PENDING
        assert feed_readable_by(env.secret, env.stranger.id) is False

    def test_a_banned_member_is_not_a_member(self, app, env):
        """SUBSCRIPTION_BANNED is -2, also truthy."""
        member = make_feed_member(env.stranger, env.secret)
        member.is_banned = True
        db.session.commit()
        assert env.secret.subscribed(env.stranger.id) == SUBSCRIPTION_BANNED
        assert feed_readable_by(env.secret, env.stranger.id) is False

    def test_a_non_member_answers_zero_and_is_refused(self, app, env):
        """The control for the two rows above: the ordinary refusal comes back
        through a falsy value, so a fix that only special-cased -1 and -2 would
        still be wrong for 0."""
        assert env.secret.subscribed(env.stranger.id) == SUBSCRIPTION_NONMEMBER
        assert feed_readable_by(env.secret, env.stranger.id) is False

    def test_no_feed_at_all(self, app, env):
        """Every caller looks the id up first, and an id that names nothing is
        not readable rather than an AttributeError."""
        assert feed_readable_by(None, env.owner.id) is False

    def test_a_public_feed_stays_readable_by_a_banned_member(self, app, env):
        """`public` is answered before membership is looked at, as `show_feed`
        answers it: a feed ban is not a site ban."""
        member = make_feed_member(env.stranger, env.open_feed)
        member.is_banned = True
        db.session.commit()
        assert feed_readable_by(env.open_feed, env.stranger.id) is True


# --------------------------------------------------------------------------
# /communities -- the plainest readout
# --------------------------------------------------------------------------


class TestTheCommunityBrowser:
    def _browse(self, app, env, feed, user=None):
        client = app.test_client()
        if user is not None:
            login(client, user)
        response = client.get(f'/communities?feed_id={feed.id}')
        assert response.status_code == 200
        return response.get_data(as_text=True)

    def test_an_anonymous_visitor_is_told_nothing(self, app, env):
        body = self._browse(app, env, env.secret)
        assert INSIDE not in body

    def test_a_stranger_with_an_account_is_told_nothing(self, app, env):
        body = self._browse(app, env, env.secret, env.stranger)
        assert INSIDE not in body

    def test_the_owner_still_sees_their_own_feed(self, app, env):
        """The control. A fix that emptied the filter for everybody would pass
        every row above and break the feature."""
        body = self._browse(app, env, env.secret, env.owner)
        assert INSIDE in body
        assert OUTSIDE not in body

    def test_a_member_still_sees_the_feed_they_joined(self, app, env):
        make_feed_member(env.stranger, env.secret)
        body = self._browse(app, env, env.secret, env.stranger)
        assert INSIDE in body
        assert OUTSIDE not in body

    def test_a_public_feed_filters_for_anybody(self, app, env):
        body = self._browse(app, env, env.open_feed)
        assert INSIDE in body
        assert OUTSIDE not in body

    def test_without_a_feed_id_every_community_is_listed(self, app, env):
        """feed_id=0 is the default and must not reach the filter at all."""
        response = app.test_client().get('/communities')
        body = response.get_data(as_text=True)
        assert INSIDE in body and OUTSIDE in body

    def test_an_id_naming_no_feed_lists_nothing(self, app, env):
        """The shape the refusal borrows: an unknown id already produced an
        empty list rather than a 404, so 'private' and 'absent' read alike."""
        response = app.test_client().get('/communities?feed_id=999999')
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert INSIDE not in body and OUTSIDE not in body

    def test_the_health_probe_takes_the_same_parameter_and_still_answers(
            self, app, env):
        """/health2 runs list_communities' body and discards the rows, so there
        is nothing to read out of it -- but it takes the id from the same place
        and must not 500 on the added lookup."""
        response = app.test_client().get(f'/health2?feed_id={env.secret.id}')
        assert response.status_code == 200
        assert response.get_data(as_text=True) == ''

    def test_the_health_probe_asks_the_question_even_though_it_answers_nothing(
            self, app, env):
        """Asserted on the call rather than on the response, and deliberately.

        /health2 returns '' by design, so dropping its guard changes nothing an
        observer can see -- the one mutant of this round that no behavioural test
        can kill. What the route owes is that it asks, because it is meant to be
        the same body as list_communities and a later edit that reads its rows
        would otherwise inherit the leak. That claim is about the call, so the
        call is what is asserted.
        """
        with patch('app.main.routes.feed_readable_by',
                   return_value=False) as asked:
            response = app.test_client().get(
                f'/health2?feed_id={env.secret.id}')

        assert response.status_code == 200
        assert asked.call_count == 1
        feed, user_id = asked.call_args.args
        assert feed.id == env.secret.id and user_id is None


# --------------------------------------------------------------------------
# The three tag routes
# --------------------------------------------------------------------------


class TestTheTagRoutes:
    def _paths(self, feed, tag):
        return [f'/tag/{tag.name}?category=feed&category_id={feed.id}',
                f'/tags/cloud/feed/{feed.id}',
                f'/tags/posts/{tag.id}?feed_id={feed.id}']

    @pytest.mark.parametrize('index', [0, 1, 2])
    def test_a_private_feed_is_a_404_anonymously(self, app, env, tagged, index):
        path = self._paths(env.secret, tagged)[index]
        with patch('app.tag.routes.flask') as fl:
            fl.render_template.return_value = 'rendered'
            response = app.test_client().get(path)
        assert response.status_code == 404

    @pytest.mark.parametrize('index', [0, 1, 2])
    def test_a_private_feed_is_a_404_for_a_stranger(self, app, env, tagged,
                                                   index):
        path = self._paths(env.secret, tagged)[index]
        client = app.test_client()
        login(client, env.stranger)
        with patch('app.tag.routes.flask') as fl:
            fl.render_template.return_value = 'rendered'
            response = client.get(path)
        assert response.status_code == 404

    @pytest.mark.parametrize('index', [0, 1, 2])
    def test_the_owner_is_served(self, app, env, tagged, index):
        """The control for all six rows above."""
        path = self._paths(env.secret, tagged)[index]
        client = app.test_client()
        login(client, env.owner)
        with patch('app.tag.routes.flask') as fl:
            fl.render_template.return_value = 'rendered'
            response = client.get(path)
        assert response.status_code == 200

    @pytest.mark.parametrize('index', [0, 1, 2])
    def test_a_public_feed_is_served_anonymously(self, app, env, tagged, index):
        path = self._paths(env.open_feed, tagged)[index]
        with patch('app.tag.routes.flask') as fl:
            fl.render_template.return_value = 'rendered'
            response = app.test_client().get(path)
        assert response.status_code == 200

    def test_the_posts_a_readable_feed_selects_are_still_its_own(self, app, env,
                                                                tagged):
        """Not only the status code: the filter has to keep working, and it is
        the filter that would be the leak if the guard were removed."""
        client = app.test_client()
        login(client, env.owner)
        with patch('app.tag.routes.flask') as fl:
            fl.render_template.return_value = 'rendered'
            client.get(f'/tags/posts/{tagged.id}?feed_id={env.secret.id}')
        titles = [post.title for post in fl.render_template.call_args.kwargs['posts']]
        assert titles == ['insidepost']


# --------------------------------------------------------------------------
# The pages a feed is reached by name
# --------------------------------------------------------------------------


class TestTheRssTwin:
    def test_a_private_feed_is_a_404(self, app, env):
        """`show_feed` redirected and this, the same feed at the same name with
        '.rss' after it, served the title, the description and one entry per
        post with the community it is in."""
        response = app.test_client().get('/f/secretfeed.rss')
        assert response.status_code == 404

    def test_a_stranger_gets_the_same_404(self, app, env):
        client = app.test_client()
        login(client, env.stranger)
        assert client.get('/f/secretfeed.rss').status_code == 404

    def test_the_owner_is_served(self, app, env):
        client = app.test_client()
        login(client, env.owner)
        response = client.get('/f/secretfeed.rss')
        assert response.status_code == 200
        assert response.headers['Content-Type'].startswith('application/rss+xml')

    def test_a_public_feed_is_served_anonymously(self, app, env):
        response = app.test_client().get('/f/openfeed.rss')
        assert response.status_code == 200


class TestTheSubmitPage:
    def test_a_private_feed_is_a_404_for_a_stranger(self, app, env):
        """The page lists the feed's communities in its picker, so a logged-in
        account that may not open the feed was shown what is in it."""
        client = app.test_client()
        login(client, env.stranger)
        with patch('app.feed.routes.render_template', return_value='rendered'):
            response = client.get('/f/secretfeed/submit')
        assert response.status_code == 404

    def test_the_owner_is_served_and_the_picker_holds_the_feed(self, app, env):
        client = app.test_client()
        login(client, env.owner)
        captured = {}

        def fake_render(template, **kwargs):
            captured.update(kwargs)
            return 'rendered'

        with patch('app.feed.routes.render_template', side_effect=fake_render):
            response = client.get('/f/secretfeed/submit')
        assert response.status_code == 200
        assert [c.name for c in captured['communities']] == [INSIDE]

    def test_a_public_feed_is_served_to_a_stranger(self, app, env):
        client = app.test_client()
        login(client, env.stranger)
        with patch('app.feed.routes.render_template', return_value='rendered'):
            response = client.get('/f/openfeed/submit')
        assert response.status_code == 200


class TestCopyingAFeed:
    def test_a_stranger_may_not_copy_a_private_feed(self, app, env):
        """Copying is a read: the new feed is built from this one's title, its
        description and every FeedItem in it, and the copy belongs to the
        caller."""
        client = app.test_client()
        login(client, env.stranger)
        with patch('app.feed.routes.render_template', return_value='rendered'):
            response = client.get(f'/feed/{env.secret.id}/copy')
        assert response.status_code == 404

    def test_the_owner_may_copy_their_own(self, app, env):
        client = app.test_client()
        login(client, env.owner)
        with patch('app.feed.routes.render_template', return_value='rendered'):
            response = client.get(f'/feed/{env.secret.id}/copy')
        assert response.status_code == 200

    def test_anybody_may_copy_a_public_feed(self, app, env):
        client = app.test_client()
        login(client, env.stranger)
        with patch('app.feed.routes.render_template', return_value='rendered'):
            response = client.get(f'/feed/{env.open_feed.id}/copy')
        assert response.status_code == 200


# --------------------------------------------------------------------------
# show_feed, where the rule came from
# --------------------------------------------------------------------------


class TestShowFeedItself:
    def _get(self, app, user=None):
        client = app.test_client()
        if user is not None:
            login(client, user)
        with patch('app.feed.routes.render_template', return_value='rendered'):
            return client.get('/f/secretfeed')

    def test_a_stranger_is_still_redirected(self, app, env):
        response = self._get(app, env.stranger)
        assert response.status_code == 302
        assert '/feeds' in response.headers['Location']

    def test_the_owner_is_still_served(self, app, env):
        assert self._get(app, env.owner).status_code == 200

    def test_a_member_is_still_served(self, app, env):
        make_feed_member(env.stranger, env.secret)
        assert self._get(app, env.stranger).status_code == 200

    def test_an_unapproved_join_request_no_longer_admits(self, app, env):
        """The tightening. `feed.subscribed()` answers -1 here, `if
        feed.subscribed(...)` accepted it, and the owner had not approved
        anything."""
        make_feed_join_request(env.stranger, env.secret)
        response = self._get(app, env.stranger)
        assert response.status_code == 302

    def test_a_banned_member_no_longer_admits(self, app, env):
        """-2, likewise truthy. The owner threw this account out of the feed."""
        member = make_feed_member(env.stranger, env.secret)
        member.is_banned = True
        db.session.commit()
        response = self._get(app, env.stranger)
        assert response.status_code == 302

    def test_the_owner_is_served_even_when_banned_from_their_own_feed(
            self, app, env):
        """Ownership is asked before membership, so a stray FeedMember row does
        not lock an owner out of what they made."""
        member = FeedMember(feed_id=env.secret.id, user_id=env.owner.id,
                            is_banned=True)
        db.session.add(member)
        db.session.commit()
        assert self._get(app, env.owner).status_code == 200


# --------------------------------------------------------------------------
# The alpha API -- D1173's shape, one file over
# --------------------------------------------------------------------------


def auth(user):
    return {'Authorization': f'Bearer {user.encode_jwt_token()}'}


@pytest.fixture
def api(app, env, monkeypatch):
    monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
    g.site = db.session.get(Site, 1)
    site = db.session.get(Site, 1)
    site.language_id = Language.query.filter_by(code='und').one().id
    for user in (env.owner, env.stranger):
        user.private_key, user.public_key = a_keypair()
    db.session.commit()
    make_post(env.inside, env.owner, ap_id=f'https://{HOST}/post/apilisted',
              title='apilisted')
    db.session.commit()
    return app.test_client()


@pytest.mark.parametrize('endpoint', ['list', 'list2'])
class TestTheAlphaPostListing:
    def test_a_private_feed_is_refused_anonymously(self, app, env, api,
                                                   endpoint):
        response = api.get(f'/api/alpha/post/{endpoint}?feed_id={env.secret.id}')
        assert response.status_code == 400
        assert response.get_json()['message'] == 'feed not found'

    def test_a_private_feed_is_refused_to_a_stranger(self, app, env, api,
                                                    endpoint):
        response = api.get(f'/api/alpha/post/{endpoint}?feed_id={env.secret.id}',
                           headers=auth(env.stranger))
        assert response.status_code == 400
        assert response.get_json()['message'] == 'feed not found'

    def test_the_owner_is_answered(self, app, env, api, endpoint):
        """The control: the same request from the account that owns the feed
        returns the listing, so the refusal is about who asked."""
        response = api.get(f'/api/alpha/post/{endpoint}?feed_id={env.secret.id}',
                           headers=auth(env.owner))
        assert response.status_code == 200
        titles = [post['post']['title'] for post in response.get_json()['posts']]
        assert titles == ['apilisted']

    def test_a_public_feed_is_answered_anonymously(self, app, env, api,
                                                  endpoint):
        response = api.get(
            f'/api/alpha/post/{endpoint}?feed_id={env.open_feed.id}')
        assert response.status_code == 200

    def test_an_id_naming_no_feed_says_so(self, app, env, api, endpoint):
        """The message the refusal borrows, so the two cannot be told apart."""
        response = api.get(f'/api/alpha/post/{endpoint}?feed_id=999999')
        assert response.status_code == 400
        assert response.get_json()['message'] == 'feed not found'


# --------------------------------------------------------------------------
# The five ActivityPub feed endpoints, two of which did not ask
# --------------------------------------------------------------------------


AP = {'Accept': 'application/activity+json'}


class TestTheActivityPubEndpoints:
    """`feed_profile`, `/outbox` and `/following` answered 403 for a private
    feed. `/moderators` named its owner and `/followers` counted its members,
    both with 200. These are peer-facing and unauthenticated, so the test is
    `public` alone, as the three siblings already had it."""

    @pytest.mark.parametrize('suffix', ['', '/outbox', '/following',
                                        '/moderators', '/followers'])
    def test_a_private_feed_is_forbidden_at_all_five(self, app, env, suffix):
        response = app.test_client().get(f'/f/secretfeed{suffix}', headers=AP)
        assert response.status_code == 403

    @pytest.mark.parametrize('suffix', ['', '/outbox', '/following',
                                        '/moderators', '/followers'])
    def test_a_public_feed_is_served_at_all_five(self, app, env, suffix):
        """The control: the same five must still answer a peer about a public
        feed, or the fix has taken federation with it."""
        response = app.test_client().get(f'/f/openfeed{suffix}', headers=AP)
        assert response.status_code == 200
        assert response.content_type.startswith('application/activity+json')

    @pytest.mark.parametrize('suffix', ['/outbox', '/following', '/moderators',
                                        '/followers'])
    def test_a_name_naming_no_feed_is_still_a_404(self, app, env, suffix):
        """404, not 403: the refusal must not turn an absent feed into one that
        exists and is private."""
        response = app.test_client().get(f'/f/nosuchfeed{suffix}', headers=AP)
        assert response.status_code == 404

    def test_the_owners_profile_is_not_named_for_a_private_feed(self, app, env):
        """What `/moderators` answered: the `ap_profile_id` of whoever owns the
        feed."""
        response = app.test_client().get('/f/secretfeed/moderators', headers=AP)
        assert response.status_code == 403
        assert env.owner.ap_profile_id not in response.get_data(as_text=True)

    def test_a_private_feeds_membership_is_not_counted(self, app, env):
        """And what `/followers` answered: `totalItems`, the number of accounts
        subscribed to it."""
        make_feed_member(env.stranger, env.secret)
        response = app.test_client().get('/f/secretfeed/followers', headers=AP)
        assert response.status_code == 403

    def test_a_public_feeds_membership_is_still_counted(self, app, env):
        make_feed_member(env.stranger, env.open_feed)
        response = app.test_client().get('/f/openfeed/followers', headers=AP)
        assert response.get_json()['totalItems'] == 1

    def test_a_public_feeds_communities_are_listed_once_each(self, app, env):
        """The idiom again, where duplicates would have been peer-visible:
        `/outbox` and `/following` build a plain list with no `IN` to collapse
        it, so `totalItems` is whatever the query returned."""
        make_local_feed('decoy1', public=True)
        make_local_feed('decoy2', public=True)
        for suffix in ('/outbox', '/following'):
            response = app.test_client().get(f'/f/openfeed{suffix}',
                                             headers=AP)
            assert response.get_json()['totalItems'] == 1, suffix


# --------------------------------------------------------------------------
# The idiom, measured
# --------------------------------------------------------------------------


def test_a_feeds_items_are_counted_once_per_item(app, env):
    """The incidental. `FeedItem.query.join(Feed, FeedItem.feed_id == <id>)`
    named no predicate between the two tables, so the database returned one row
    per (matching item, feed) pair -- eight here, for two items and four feeds.

    `Query.all()` uniquified them back down to two, which is why no caller was
    wrong; `db.session.execute()` does not, which is why none of them should
    have been written that way. This pins the multiplicity so a future rewrite
    of any of the ten sites cannot reintroduce it unnoticed.
    """
    from app.models import Feed, FeedItem
    make_feed_item(env.secret, env.outside)
    make_local_feed('decoy1', public=True)
    make_local_feed('decoy2', public=True)
    assert Feed.query.count() == 4

    crossed = FeedItem.query.join(Feed, FeedItem.feed_id == env.secret.id)
    assert len(db.session.execute(crossed.statement).all()) == 8
    assert len(crossed.all()) == 2
    assert len(FeedItem.query.filter_by(feed_id=env.secret.id).all()) == 2


def test_the_communities_a_feed_holds_are_listed_once_each(app, env):
    """The same point at a route: /communities must not list a community twice
    because the instance has more than one feed.

    Asserted on the paginated rows rather than on the rendered page: the page
    links a community more than once by design (the row and its icon), so
    counting '/c/<name>' in the HTML proves nothing either way.
    """
    make_local_feed('decoy1', public=True)
    make_local_feed('decoy2', public=True)
    captured = {}

    def fake_render(template, **kwargs):
        captured.update(kwargs)
        return 'rendered'

    with patch('app.main.routes.render_template', side_effect=fake_render):
        response = app.test_client().get(
            f'/communities?feed_id={env.open_feed.id}')

    assert response.status_code == 200
    assert [c.name for c in captured['communities'].items] == [INSIDE]
