"""`app.utils.back` and the ten routes that had its body copied into them.

`back(default)` sends a user to the page they came from, falling back to
`default` when there is no Referer, when the Referer is the URL being requested
(which would be a redirect loop), or when the Referer does not point at this
site (`is_safe_redirect_target` -- see tests/test_safe_redirect_target.py).

Two feed routes carried a verbatim copy of that logic -- comments included. The
TestFeedDeleteRedirect / TestFeedAddCommunityRedirect tests are the pins from
that refactor: they assert the redirect those routes produced BEFORE the copies
were replaced.

The eight BackSiteContract subclasses at the bottom are the remaining sites,
unified afterwards. Those are NOT no-behaviour-change pins: every one of them
gained the self-URL guard and the empty-Referer fallback, six of them gained an
origin check they never had, and two had a bypassable substring check replaced
with a real one. The per-class docstrings say which.
"""

import pytest
from flask import request, session
from flask_wtf.csrf import generate_csrf

from app import db
from app.models import Feed, Site
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

    def test_an_off_site_referrer_falls_back(self, app):
        """back() used to follow any Referer at all, which made every caller an
        open redirect. It now runs the same origin check referrer() does."""
        with app.test_request_context('/here', headers={'Referer': 'https://evil.example/x'}):
            response = back('/fallback')
        assert response.headers['Location'] == '/fallback'

    def test_a_substring_lookalike_referrer_falls_back(self, app):
        with app.test_request_context(
                '/here', headers={'Referer': 'https://evil.example/?x=test.piefed.local'}):
            response = back('/fallback')
        assert response.headers['Location'] == '/fallback'

    def test_a_protocol_relative_referrer_falls_back(self, app):
        with app.test_request_context('/here', headers={'Referer': '//evil.example/x'}):
            response = back('/fallback')
        assert response.headers['Location'] == '/fallback'

    def test_an_on_site_absolute_referrer_is_followed(self, app):
        with app.test_request_context(
                '/here', headers={'Referer': 'https://test.piefed.local/there'}):
            response = back('/fallback')
        assert response.headers['Location'] == 'https://test.piefed.local/there'


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
        assert db.session.get(Feed, feed.id) is None

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
        token = csrf(client.application, client)
        return client.post('/feed/add_community',
                           data={'csrf_token': token, 'new_feed_id': feed.id,
                                 'current_feed_id': 0, 'community_id': community.id},
                           headers=headers or {})

    def test_a_get_is_refused(self, app, db_session):
        """D664, fixed. The route was a state-changing GET with no CSRF token,
        so any page could make a signed-in user add a community to their own
        feed. It is now POST-only."""
        user, feed, community = self._setup()
        with app.test_client() as client:
            login(client, user)
            response = client.get(f'/feed/add_community?new_feed_id={feed.id}'
                                  f'&current_feed_id=0&community_id={community.id}')
        assert response.status_code == 405
        assert db.session.get(Feed, feed.id).num_communities == 0

    def test_a_post_without_a_csrf_token_is_refused(self, app, db_session):
        """D664, fixed. The POST has to carry the token."""
        user, feed, community = self._setup()
        with app.test_client() as client:
            login(client, user)
            response = client.post('/feed/add_community',
                                   data={'new_feed_id': feed.id, 'current_feed_id': 0,
                                         'community_id': community.id})
        assert response.status_code == 400
        assert db.session.get(Feed, feed.id).num_communities == 0

    def test_the_user_goes_back_to_the_page_they_came_from(self, app, db_session):
        user, feed, community = self._setup()
        with app.test_client() as client:
            login(client, user)
            response = self._add(client, user, feed, community,
                                 headers={'Referer': 'https://test.piefed.local/f/somewhere'})
        assert response.status_code == 302
        assert response.headers['Location'] == 'https://test.piefed.local/f/somewhere'
        assert db.session.get(Feed, feed.id).num_communities == 1

    def test_without_a_referrer_the_index_is_used(self, app, db_session):
        user, feed, community = self._setup()
        with app.test_client() as client:
            login(client, user)
            response = self._add(client, user, feed, community)
        assert response.headers['Location'] == '/home'

    def test_a_referrer_equal_to_the_request_url_falls_back_to_the_index(self, app, db_session):
        user, feed, community = self._setup()
        same = url_of(app, '/feed/add_community')
        with app.test_client() as client:
            login(client, user)
            response = self._add(client, user, feed, community, headers={'Referer': same})
        assert response.headers['Location'] == '/home'


# ---------------------------------------------------------------------------
# The eight sites that had `Referer`-handling copied into them and are now
# routed through back(). Each subclass supplies the request; the contract below
# is the same for all of them, because that is the point of unifying them.
#
# Two of these sites (community subscribe/unsubscribe) previously carried
# `current_app.config['SERVER_NAME'] in referrer` -- a substring test. The other
# six carried NO origin check at all and would follow any Referer anywhere, so
# for those the "off-site referrer" and "lookalike" cases below are the tests of
# a closed open-redirect, not of preserved behaviour. All eight previously
# lacked the self-URL guard and accepted an empty `Referer:` header.
# ---------------------------------------------------------------------------

SELF = object()


class BackSiteContract:
    """The behaviour every route routed through back() now has."""

    #: set by prepare()
    expected_default = None

    def prepare(self):
        """Create the DB rows. Return (path, user_or_None) and set
        self.expected_default."""
        raise NotImplementedError

    def _run(self, app, referer):
        path, user = self.prepare()
        headers = {}
        if referer is SELF:
            headers['Referer'] = url_of(app, path)
        elif referer is not None:
            headers['Referer'] = referer
        with app.test_client() as client:
            if user is not None:
                login(client, user)
            return client.get(path, headers=headers)

    def test_a_same_site_referrer_is_followed(self, app, db_session, site):
        response = self._run(app, 'https://test.piefed.local/came-from')
        assert response.status_code == 302
        assert response.headers['Location'] == 'https://test.piefed.local/came-from'

    def test_a_relative_referrer_is_followed(self, app, db_session, site):
        response = self._run(app, '/came-from')
        assert response.headers['Location'] == '/came-from'

    def test_an_off_site_referrer_goes_to_the_default(self, app, db_session, site):
        response = self._run(app, 'https://evil.example/x')
        assert response.headers['Location'] == self.expected_default

    def test_a_substring_lookalike_referrer_goes_to_the_default(self, app, db_session, site):
        response = self._run(app, 'https://evil.example/?x=test.piefed.local')
        assert response.headers['Location'] == self.expected_default

    def test_a_protocol_relative_referrer_goes_to_the_default(self, app, db_session, site):
        response = self._run(app, '//evil.example/x')
        assert response.headers['Location'] == self.expected_default

    def test_no_referrer_goes_to_the_default(self, app, db_session, site):
        response = self._run(app, None)
        assert response.headers['Location'] == self.expected_default

    def test_an_empty_referer_header_goes_to_the_default(self, app, db_session, site):
        """Previously `referrer is not None` was true for '', so these routes
        emitted `redirect('')`."""
        response = self._run(app, '')
        assert response.headers['Location'] == self.expected_default

    def test_a_referrer_equal_to_the_request_url_goes_to_the_default(self, app, db_session, site):
        """Previously these routes redirected to the page being requested."""
        response = self._run(app, SELF)
        assert response.headers['Location'] == self.expected_default


def open_registration():
    """approval_required redirects a key-less local user while the site is
    'Closed' (the Site default), which would short-circuit the routes below."""
    db.session.get(Site, 1).registration_mode = 'Open'
    db.session.commit()


class TestCommunitySubscribe(BackSiteContract):
    """app/community/routes.py subscribe -- had the SERVER_NAME substring guard."""

    def prepare(self):
        instance = make_instance('test.piefed.local', software='piefed')
        user = make_user(instance, 'joiner', local=True)
        open_registration()
        community = make_community('joinable')
        self.expected_default = '/c/joinable'
        return f'/community/{community.name}/subscribe', user


class TestCommunityUnsubscribe(BackSiteContract):
    """app/community/routes.py unsubscribe -- had the SERVER_NAME substring guard."""

    def prepare(self):
        instance = make_instance('test.piefed.local', software='piefed')
        user = make_user(instance, 'leaver', local=True)
        community = make_community('leavable')
        self.expected_default = '/c/leavable'
        return f'/community/{community.name}/unsubscribe', user


class TestCommunityLookupRemote(BackSiteContract):
    """app/community/routes.py lookup, the anonymous branch -- no origin check
    at all before this change."""

    def prepare(self):
        self.expected_default = '/'
        return '/community/lookup/somewhere/remote.example', None


class TestFeedSubscribe(BackSiteContract):
    """app/feed/routes.py subscribe -- no origin check at all before this change."""

    def prepare(self):
        instance = make_instance('test.piefed.local', software='piefed')
        user = make_user(instance, 'feed-joiner', local=True)
        user.feed_auto_follow = False
        db.session.commit()
        open_registration()
        feed = make_feed(user, 'joinable-feed')
        self.expected_default = '/f/joinable-feed'
        return f'/feed/{feed.name}/subscribe', user


class TestFeedUnsubscribe(BackSiteContract):
    """app/feed/routes.py feed_unsubscribe -- no origin check at all before."""

    def prepare(self):
        instance = make_instance('test.piefed.local', software='piefed')
        user = make_user(instance, 'feed-leaver', local=True)
        feed = make_feed(user, 'leavable-feed')
        self.expected_default = '/f/leavable-feed'
        return f'/feed/{feed.name}/unsubscribe', user


class TestFeedLookupRemote(BackSiteContract):
    """app/feed/routes.py lookup, the anonymous branch -- no origin check before."""

    def prepare(self):
        self.expected_default = '/'
        return '/feed/lookup/somewhere/remote.example', None


class TestUserLookupNotRetrieved(BackSiteContract):
    """app/user/routes.py lookup, the authenticated "could not be retrieved"
    branch -- no origin check before. search_for_user fails because outbound
    HTTP is blocked in the harness, which is exactly the branch under test."""

    def prepare(self):
        instance = make_instance('test.piefed.local', software='piefed')
        user = make_user(instance, 'searcher', local=True)
        self.expected_default = '/'
        return '/user/lookup/nobody/remote.example', user


class TestUserLookupAnonymous(BackSiteContract):
    """app/user/routes.py lookup, the anonymous branch -- no origin check before."""

    def prepare(self):
        self.expected_default = '/'
        return '/user/lookup/nobody/remote.example', None


class TestReferrerDefaultIsNotAnEscapeHatch:
    """Two routes passed the POSTed `referrer` field to referrer() as its
    DEFAULT as well: `redirect(referrer(form.referrer.data))`.

    The field is a HiddenField named `referrer`, so it is already referrer()'s
    source #2 and is now checked. Passing it a second time as the default
    re-injected the same user-controlled string on the unchecked path, undoing
    the guard. The sibling route two functions above (post_reminder) already
    called `referrer()` with no argument, so dropping it is the consistent form
    as well as the safe one.

    Sites: app/instance/routes.py instance_add_people,
           app/post/routes.py post_reply_reminder.
    """

    def _post(self, app, client, referrer_field):
        token = csrf(app, client)
        return client.post('/instance/add_people',
                           data={'csrf_token': token, 'people': '',
                                 'referrer': referrer_field, 'submit': 'Add people'})

    def _user(self):
        instance = make_instance('test.piefed.local', software='piefed')
        user = make_user(instance, 'adder', local=True)
        open_registration()
        return user

    def test_a_cross_origin_referrer_field_does_not_come_back_as_the_default(self, app, db_session, site):
        user = self._user()
        with app.test_client() as client:
            login(client, user)
            response = self._post(app, client, 'https://evil.example/phish')
        assert response.status_code == 302
        assert response.headers['Location'] == '/home'

    def test_a_same_origin_referrer_field_is_still_honoured(self, app, db_session, site):
        user = self._user()
        with app.test_client() as client:
            login(client, user)
            response = self._post(app, client, 'https://test.piefed.local/instance/x')
        assert response.headers['Location'] == 'https://test.piefed.local/instance/x'
