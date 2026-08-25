"""`app.utils.back` and the routes that had its body copied into them.

`back(default)` sends a user to the page they came from, falling back to
`default` when there is no Referer or when the Referer is the URL being
requested (which would be a redirect loop). Two feed routes carried a verbatim
copy of that logic -- comments included -- instead of calling it.

The route tests here are pins: they assert the redirect each route produced
BEFORE the copies were replaced, so they hold the refactor to "no behaviour
change" rather than merely exercising the new call.
"""

import pytest
from flask import request, session
from flask_wtf.csrf import generate_csrf

from app import db
from app.models import Feed
from app.utils import back
from tests.factories import make_community, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')


def login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def csrf(app, client):
    """A CSRF token/session pair the app will accept.

    app.utils.login_required calls flask_wtf's validate_csrf directly on every
    POST, which ignores WTF_CSRF_ENABLED -- so a POST route test needs a real
    token even though the test config disables CSRF for forms.
    """
    with app.test_request_context():
        token = generate_csrf()
        raw = session['csrf_token']
    with client.session_transaction() as sess:
        sess['csrf_token'] = raw
    return token


def url_of(app, path):
    """The absolute URL the test client will report as request.url for `path`."""
    with app.test_request_context(path):
        return request.url


def make_feed(user, name='a-feed'):
    feed = Feed(user_id=user.id, title=name, name=name, machine_name=name,
                num_communities=0, public=False, is_instance_feed=False,
                instance_id=1, ap_profile_id=f'https://test.piefed.local/f/{name}')
    db.session.add(feed)
    db.session.commit()
    return feed


class TestBack:
    def test_the_referrer_is_where_the_user_goes(self, app):
        with app.test_request_context('/here'):
            response = back('/fallback')
        assert response.status_code == 302
        assert response.headers['Location'] == '/fallback'

    def test_an_off_page_referrer_is_followed(self, app):
        with app.test_request_context('/here', headers={'Referer': '/there'}):
            response = back('/fallback')
        assert response.headers['Location'] == '/there'

    def test_a_referrer_equal_to_the_current_url_falls_back(self, app):
        """Without this guard the redirect points at the URL being requested,
        which is a loop."""
        with app.test_request_context('/here') as ctx:
            with app.test_request_context('/here', headers={'Referer': ctx.request.url}):
                response = back('/fallback')
        assert response.headers['Location'] == '/fallback'

    def test_an_empty_referer_header_falls_back(self, app):
        with app.test_request_context('/here', headers={'Referer': ''}):
            response = back('/fallback')
        assert response.headers['Location'] == '/fallback'


class TestFeedDeleteRedirect:
    """app/feed/routes.py feed_delete. A non-public feed with no communities
    federates nothing, so this exercises the redirect and nothing else."""

    def _delete(self, app, client, feed, headers=None):
        token = csrf(app, client)
        return client.post(f'/feed/{feed.id}/delete',
                           data={'csrf_token': token},
                           headers=headers or {})

    def test_the_user_goes_back_to_the_page_they_came_from(self, app, db_session):
        instance = make_instance('test.piefed.local', software='piefed')
        user = make_user(instance, 'feed-owner', local=True)
        feed = make_feed(user)
        with app.test_client() as client:
            login(client, user)
            response = self._delete(app, client, feed,
                                    headers={'Referer': 'https://test.piefed.local/f/somewhere'})
        assert response.status_code == 302
        assert response.headers['Location'] == 'https://test.piefed.local/f/somewhere'
        assert db.session.query(Feed).get(feed.id) is None

    def test_without_a_referrer_the_index_is_used(self, app, db_session):
        instance = make_instance('test.piefed.local', software='piefed')
        user = make_user(instance, 'feed-owner', local=True)
        feed = make_feed(user)
        with app.test_client() as client:
            login(client, user)
            response = self._delete(app, client, feed)
        assert response.headers['Location'] == '/home'

    def test_a_referrer_equal_to_the_request_url_falls_back_to_the_index(self, app, db_session):
        instance = make_instance('test.piefed.local', software='piefed')
        user = make_user(instance, 'feed-owner', local=True)
        feed = make_feed(user)
        same = url_of(app, f'/feed/{feed.id}/delete')
        with app.test_client() as client:
            login(client, user)
            response = self._delete(app, client, feed, headers={'Referer': same})
        assert response.headers['Location'] == '/home'


class TestFeedAddCommunityRedirect:
    """app/feed/routes.py feed_add_community."""

    def _setup(self):
        instance = make_instance('test.piefed.local', software='piefed')
        user = make_user(instance, 'feed-owner', local=True)
        user.feed_auto_follow = False  # keep the route off the subscribe path
        db.session.commit()
        feed = make_feed(user)
        community = make_community('added-community')
        return user, feed, community

    def _add(self, client, user, feed, community, headers=None):
        return client.get(f'/feed/add_community?user_id={user.id}&new_feed_id={feed.id}'
                          f'&current_feed_id=0&community_id={community.id}',
                          headers=headers or {})

    def test_the_user_goes_back_to_the_page_they_came_from(self, app, db_session):
        user, feed, community = self._setup()
        with app.test_client() as client:
            login(client, user)
            response = self._add(client, user, feed, community,
                                 headers={'Referer': 'https://test.piefed.local/f/somewhere'})
        assert response.status_code == 302
        assert response.headers['Location'] == 'https://test.piefed.local/f/somewhere'
        assert db.session.query(Feed).get(feed.id).num_communities == 1

    def test_without_a_referrer_the_index_is_used(self, app, db_session):
        user, feed, community = self._setup()
        with app.test_client() as client:
            login(client, user)
            response = self._add(client, user, feed, community)
        assert response.headers['Location'] == '/home'

    def test_a_referrer_equal_to_the_request_url_falls_back_to_the_index(self, app, db_session):
        user, feed, community = self._setup()
        path = (f'/feed/add_community?user_id={user.id}&new_feed_id={feed.id}'
                f'&current_feed_id=0&community_id={community.id}')
        same = url_of(app, path)
        with app.test_client() as client:
            login(client, user)
            response = self._add(client, user, feed, community, headers={'Referer': same})
        assert response.headers['Location'] == '/home'
