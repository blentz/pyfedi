"""`feed_create_post`: "post to a community in this feed".

D1389, the third instance of a shape the codebase documents twice.
`int(request.form.get('community_id'))` sat behind `!= ''`, which only rules out
absent and empty -- and a form field is whatever the caller sends. Measured:

    community_id='abc'      ValueError: invalid literal for int() with base 10
    community_id='1.5'      ValueError
    community_id='null'     ValueError
    community_id='999999'   404   (the `or abort(404)` beside it already worked)
    community_id='0'        404
    community_id=''         200   (renders the picker)
    absent                  200

Both sibling sites already guard the same parse, each with a comment naming the
same failure: `app/post/routes.py:750` (a poll vote, D1093's shape) and `:1062`
(D1093 itself, a reply's language). An id that does not parse names no community
either, so it gets the same 404 rather than a traceback.

A HARNESS NOTE. The route is `methods=['GET', 'POST']` and POSTing without a CSRF
token answers 400 before any of this runs -- the first probe reported 400 for
every value, including `'abc'`, which looked like the route rejecting bad input.
Fact 749, and every test below carries a token.
"""
import pytest
from flask import g

from app import db
from app.models import Community, FeedItem, InstanceBan, Language, Site, User
from tests.factories import (make_community, make_community_member,
                             make_instance, make_local_feed, make_user)

pytestmark = pytest.mark.usefixtures('site')
HOST = 'test.piefed.local'


@pytest.fixture
def env(app, db_session):
    """Alice may post: verified, approved (she has a private_key, which is what
    `approval_required` reads), and a member of a community in the feed."""
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    local = make_instance(HOST)
    make_user(local, 'founder', local=True)
    alice = make_user(local, 'alice', local=True)
    alice.verified = True
    alice.private_key = 'a-key'
    in_feed = make_community('general')
    outside = make_community('elsewhere')
    feed = make_local_feed('newsfeed', public=True)
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.commit()
    make_community_member(alice, in_feed)
    db.session.add(FeedItem(feed_id=feed.id, community_id=in_feed.id))
    db.session.commit()
    g.admin_ids = []
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(alice.id)
        sess['_fresh'] = True
    return client, in_feed, outside


def submit(app, client, community_id=None):
    """POST with a real CSRF token -- without one the route answers 400 before
    reading the form at all (fact 749)."""
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf

    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as sess:
        sess['csrf_token'] = raw
    data = {'csrf_token': token}
    if community_id is not None:
        data['community_id'] = community_id
    return client.post('/f/newsfeed/submit', data=data)


# --------------------------------------------------------------------------
# D1389
# --------------------------------------------------------------------------


class TestACommunityIdThatIsNotANumber:
    @pytest.mark.parametrize('community_id', ['abc', '1.5', 'null', 'NaN',
                                              '0x2', '1;2', ' ', '-1', '1e3'])
    def test_it_is_a_404_rather_than_a_500(self, app, env, community_id):
        client, in_feed, outside = env

        assert submit(app, client, community_id).status_code == 404

    @pytest.mark.parametrize('community_id', ['999999', '0'])
    def test_an_id_naming_no_community_is_still_a_404(self, app, env,
                                                     community_id):
        """The `or abort(404)` that was already there -- it must keep working,
        and the new guard must not be what produces this answer."""
        client, in_feed, outside = env

        assert submit(app, client, community_id).status_code == 404

    def test_a_padded_id_is_accepted(self, app, env):
        """`.strip().isdigit()`, matching what `int()` accepts (fact 838)."""
        client, in_feed, outside = env

        response = submit(app, client, f'  {in_feed.id}  ')

        assert response.status_code == 307  # re-posts the token to join_then_add (D994)
        assert response.headers['Location'] == '/community/general/join_then_add'


class TestACommunityIdThatResolves:
    def test_it_redirects_to_that_communitys_compose_form(self, app, env):
        """Asserted first: every row above would also pass if the route simply
        404'd everything."""
        client, in_feed, outside = env

        response = submit(app, client, str(in_feed.id))

        assert response.status_code == 307  # re-posts the token to join_then_add (D994)
        assert response.headers['Location'] == '/community/general/join_then_add'

    def test_a_community_outside_the_feed_is_still_accepted(self, app, env):
        """Pinned as current behaviour, not endorsed: the id is not checked
        against the feed's own communities, so the picker's contents are a
        suggestion rather than a restriction. `join_then_add` is what decides
        whether she may post there."""
        client, in_feed, outside = env

        response = submit(app, client, str(outside.id))

        assert response.status_code == 307  # re-posts the token to join_then_add (D994)
        assert response.headers['Location'] == '/community/elsewhere/join_then_add'


class TestNoCommunityId:
    @pytest.mark.parametrize('community_id', ['', None])
    def test_the_picker_is_rendered(self, app, env, community_id):
        """`!= ''` covers both an empty field and an absent one, and both mean
        "I have not chosen yet"."""
        client, in_feed, outside = env

        response = submit(app, client, community_id)

        assert response.status_code == 200
        assert b'general' in response.data

    def test_a_get_renders_the_picker_too(self, app, env):
        client, in_feed, outside = env

        response = client.get('/f/newsfeed/submit')

        assert response.status_code == 200
        assert b'general' in response.data


    def test_the_picker_leaves_out_a_community_on_an_instance_the_user_is_banned_from(
            self, app, env):
        """D995, owner ruling: the picker asked `Community.user_is_banned`,
        which read only CommunityBan rows, so it offered a community the user
        is banned from through an instance ban."""
        client, in_feed, outside = env
        alice = User.query.filter_by(user_name='alice').one()
        db.session.add(InstanceBan(user_id=alice.id, instance_id=in_feed.instance_id))
        db.session.commit()

        response = client.get('/f/newsfeed/submit')

        assert response.status_code == 200
        assert f'/community/{in_feed.link()}/submit'.encode() not in response.data


class TestTheFeedItself:
    def test_an_unknown_feed_is_a_404(self, app, env):
        client, in_feed, outside = env

        assert client.get('/f/nosuchfeed/submit').status_code == 404

    def test_the_feed_name_is_matched_case_insensitively(self, app, env):
        """`Feed.machine_name == feed_name.strip().lower()` -- a link with the
        feed's display casing still resolves."""
        client, in_feed, outside = env

        assert client.get('/f/NewsFeed/submit').status_code == 200

    def test_an_anonymous_visitor_is_sent_to_the_login(self, app, env):
        response = app.test_client().get('/f/newsfeed/submit')

        assert response.status_code in (302, 401)
