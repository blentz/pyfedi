"""Round 234: the ActivityPub actor endpoints, and the inbox's two unparseable bodies.

`app/activitypub/routes.py` is the federation surface -- every line in it answers a request
from another server. Six clusters were uncovered:

    /testredis                  a liveness probe with both its arms unexercised
    webfinger's `g.site` fill   the arm taken when `before_request` has not run
    resolve_remote_handle    two of its three refusals: an anonymous caller and an
                                ActivityPub one. This is the guard that stops another
                                server making THIS server fetch a third host.
    /u/<actor>/outbox           answered nothing in any test, though it is a public
                                endpoint every peer polls
    /c/<actor> not found        the redirect to a remote lookup for a signed-in viewer
    the shared inbox            `BlockingIOError` -- a peer that disconnects mid-body

The `resolve_remote_handle` rows are the ones that carry weight. It is called while
answering `/u/<actor>`, so a caller who can get past its refusals chooses a hostname this
server then connects to. Two of the three refusals had no row.
"""
from types import SimpleNamespace
from unittest.mock import patch

from werkzeug.exceptions import NotFound

import pytest
from flask import g

from app import db
from app.models import Site
from tests.factories import make_community, make_user


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    return SimpleNamespace(app=app, site=site, baseline=api_baseline,
                           client=app.test_client())


def signed_in_client(app, user):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return client


AP_HEADERS = {'Accept': 'application/activity+json'}
BROWSER_HEADERS = {'Accept': 'text/html,application/xhtml+xml'}


# --------------------------------------------------------------------------
# resolve_remote_handle
# --------------------------------------------------------------------------


class TestWhoMayMakeThisServerFetchAnother:
    """`/u/<actor>` for an actor this instance does not hold calls
    `resolve_remote_handle`, which will go and FETCH the named account from whatever
    host the string names. Its three refusals are what stop a stranger choosing that host:
    the name must look remote, the caller must be signed in, and the request must not
    itself be an ActivityPub request -- otherwise a peer asking about
    `victim@third.example` makes this server connect there.
    """

    def _search(self, actor):
        from app.activitypub.routes import resolve_remote_handle

        return resolve_remote_handle(actor)

    def test_an_anonymous_caller_is_refused(self, env):
        """`:486`. Nothing is fetched for somebody with no account, which is the case an
        unauthenticated scraper is in."""
        # An HTML Accept header, so the refusal under test is the anonymous one and not
        # the ActivityPub one below it -- a bare context has no Accept at all, and the
        # request then looks like a peer's.
        with env.app.test_request_context('/u/someone@remote.example',
                                          headers=BROWSER_HEADERS):
            with patch('app.activitypub.routes.search_for_user') as search:
                assert self._search('someone@remote.example') is None

        assert search.call_count == 0

    def test_an_activitypub_request_is_refused(self, env):
        """`:490`. A peer's own request carries `Accept: application/activity+json`, and
        answering it by fetching a third host is how one server turns another into a
        proxy."""
        reader = make_user(env.baseline.instance_local, 'apcaller', local=True)
        db.session.commit()

        with env.app.test_request_context('/u/someone@remote.example',
                                          headers=AP_HEADERS):
            with patch('app.activitypub.routes.current_user') as user, \
                    patch('app.activitypub.routes.search_for_user') as search:
                user.is_authenticated = True
                assert self._search('someone@remote.example') is None

        assert search.call_count == 0

    def test_a_local_name_is_refused_before_anything_else(self, env):
        """`if '@' not in actor`. A bare name is a local account, and this function exists
        only to resolve remote ones.

        The caller is signed in here on purpose: with an anonymous one the guard below
        would answer for it, and a mutant deleting THIS guard would survive.
        """
        with env.app.test_request_context('/u/someone', headers=BROWSER_HEADERS):
            with patch('app.activitypub.routes.current_user') as user, \
                    patch('app.activitypub.routes.search_for_user') as search:
                user.is_authenticated = True
                assert self._search('someone') is None

        assert search.call_count == 0

    def test_a_signed_in_browser_request_does_fetch(self, env):
        """The arm the three refusals guard, so that the rows above cannot be satisfied by
        a function that never fetches at all."""
        found = object()

        with env.app.test_request_context('/u/someone@remote.example',
                                          headers=BROWSER_HEADERS):
            with patch('app.activitypub.routes.current_user') as user, \
                    patch('app.activitypub.routes.search_for_user',
                          return_value=found) as search:
                user.is_authenticated = True
                assert self._search('someone@remote.example') is found

        assert search.call_count == 1

    def test_a_refusal_from_the_search_is_swallowed(self, env):
        """`except Exception: return None`. `search_for_user` RAISES for a banned instance
        rather than returning None, and the caller's own 404 is the right answer -- a
        traceback here would be a 500 on a URL anybody can request."""
        with env.app.test_request_context('/u/someone@banned.example',
                                          headers=BROWSER_HEADERS):
            with patch('app.activitypub.routes.current_user') as user, \
                    patch('app.activitypub.routes.search_for_user',
                          side_effect=Exception('instance is banned')):
                user.is_authenticated = True
                assert self._search('someone@banned.example') is None


# --------------------------------------------------------------------------
# The public actor endpoints
# --------------------------------------------------------------------------


class TestTheUserOutbox:
    """Every peer polls `/u/<actor>/outbox`. It is a fixed empty collection -- PieFed does
    not publish a user outbox -- and nothing had ever asked for it.
    """

    def test_it_answers_an_empty_ordered_collection(self, env):
        author = make_user(env.baseline.instance_local, 'outboxowner', local=True)
        db.session.commit()

        response = env.client.get(f'/u/{author.user_name}/outbox', headers={'Accept': 'application/activity+json'})

        assert response.status_code == 200
        body = response.get_json()
        assert body['type'] == 'OrderedCollection'
        assert body['totalItems'] == 0
        assert body['orderedItems'] == []

    def test_it_is_served_as_activitypub_and_varies_on_accept(self, env):
        """The three headers. `Vary: Accept` is what stops a cache handing an HTML answer
        to a peer asking for JSON, and this endpoint is cacheable for the collection
        max-age (D180, fixed by owner ruling: ten seconds before)."""
        response = env.client.get('/u/anybody/outbox', headers={'Accept': 'application/activity+json'})

        assert response.content_type == 'application/activity+json'
        # `Accept` as a whole entry: Flask appends `Accept-Encoding` to Vary on its own, so
        # a substring test passes even with the header this route sets removed.
        assert 'Accept' in [part.strip()
                            for part in response.headers['Vary'].split(',')]
        assert response.headers['Cache-Control'] == 'public, max-age=60'

    def test_it_does_not_require_the_actor_to_exist(self, env):
        """Asserted rather than assumed: the route never looks the name up, so it answers
        the same empty collection for an account nobody holds. That is a disclosure
        decision -- it means the endpoint cannot be used to test whether a username
        exists."""
        response = env.client.get('/u/nobody-at-all/outbox', headers={'Accept': 'application/activity+json'})

        assert response.status_code == 200
        assert response.get_json()['totalItems'] == 0


class TestACommunityThisInstanceDoesNotHave:

    def test_a_signed_in_viewer_is_sent_to_the_remote_lookup(self, env):
        """`:610-612`. A browser following `/c/name@host` for a community this instance has
        never seen is offered the federated lookup, with the handle split back into its two
        halves."""
        reader = make_user(env.baseline.instance_local, 'seeker', local=True)
        reader.verified = True
        db.session.commit()

        response = signed_in_client(env.app, reader).get('/c/elsewhere@peer.example')

        assert response.status_code == 302
        # The two halves go to two different parameters, so the assertion names both --
        # passing the whole handle as each is a redirect that still mentions both strings.
        assert response.headers['Location'].endswith(
            '/community/lookup/elsewhere/peer.example')

    def test_a_signed_in_viewer_asking_for_a_bare_name_is_offered_the_add_form(self, env):
        """The `elif` below it: no `@`, so there is no host to look the community up on."""
        reader = make_user(env.baseline.instance_local, 'seeker', local=True)
        reader.verified = True
        db.session.commit()

        response = signed_in_client(env.app, reader).get('/c/nosuchcommunity')

        assert response.status_code == 302

    def test_an_anonymous_viewer_gets_a_404(self, env):
        """The `else`. A stranger is told nothing about what this instance does or does not
        hold, and is not offered a lookup that would make the server fetch a host they
        named."""
        response = env.client.get('/c/elsewhere@peer.example')

        assert response.status_code == 404

    def test_a_peer_asking_for_json_is_refused(self, env):
        """`if is_activitypub_request(): abort(404)` sits ahead of every branch above, so
        no peer can reach the redirect. A handle containing `@` is refused earlier still,
        with a 400 from the route's own validation -- so the row asserts the refusal rather
        than a particular code, and pins the bare-name case at 404."""
        with_host = env.client.get('/c/elsewhere@peer.example', headers=AP_HEADERS)
        bare = env.client.get('/c/nosuchcommunity', headers=AP_HEADERS)

        assert with_host.status_code >= 400
        assert bare.status_code == 404

    def test_the_subscribe_intent_redirects_to_the_community(self, env):
        """`:623`, the FEP-3b86 activity intent: a bookmarkable URL on the reader's OWN
        instance that starts a subscribe."""
        community = make_community('intentland')
        db.session.commit()

        response = env.client.get(f'/c/{community.name}/subscribe')

        assert response.status_code == 302
        # The community page, not the subscribe route: subscribe is POST-only since
        # D994's sibling fix, so the intent lands where the reader confirms with Join.
        assert response.headers['Location'] == f'/c/{community.name}'


# --------------------------------------------------------------------------
# The shared inbox's unparseable bodies
# --------------------------------------------------------------------------


class TestABodyTheInboxCannotRead:
    """Three ways a POST to `/inbox` carries nothing usable. Each answers 400 and logs,
    rather than raising -- an inbox that 500s on a malformed body gives every peer a way to
    fill the error log.
    """

    def test_a_body_that_is_not_json_is_refused(self, env):
        response = env.client.post('/inbox', data='not json at all',
                                   content_type='application/activity+json')

        assert response.status_code == 400

    def test_an_empty_body_is_refused(self, env):
        """`request_json is None` -- distinct from the parse failure above, because
        `get_json(force=True)` returns None for a body that parses to JSON null."""
        response = env.client.post('/inbox', data='null',
                                   content_type='application/activity+json')

        assert response.status_code == 400

    def test_a_peer_that_disconnects_mid_body_is_refused(self, env):
        """`:634-636`. `BlockingIOError` is what werkzeug raises when the client stops
        sending partway through, and it is NOT a `BadRequest`, so it needs its own arm --
        without it this is an unhandled exception on a route every peer can reach."""
        with patch('flask.Request.get_json',
                   side_effect=BlockingIOError('client went away')):
            response = env.client.post('/inbox', data='{}',
                                       content_type='application/activity+json')

        assert response.status_code == 400
        assert response.get_data(as_text=True) == ''


# --------------------------------------------------------------------------
# /testredis
# --------------------------------------------------------------------------


class TestTheRedisProbe:
    """A liveness probe an operator curls. Both arms exist so that a failure is reported as
    text rather than as a traceback, which means both need a row -- the failing one cannot
    be reached by breaking redis, so it is driven through the connection helper.
    """

    def test_it_reports_ok_when_redis_answers(self, env):
        response = env.client.get('/testredis')

        assert response.status_code == 200
        assert response.get_data(as_text=True) == 'Redis: OK'

    def test_it_reports_failure_when_the_key_does_not_come_back(self, env):
        """The `else`. A redis that accepts the write and returns nothing on the read is
        exactly the half-broken state this probe exists to name."""
        class Forgetful:
            def set(self, *args, **kwargs):
                return True

            def get(self, *args, **kwargs):
                return None

        with patch('app.activitypub.routes.get_redis_connection',
                   return_value=Forgetful()):
            response = env.client.get('/testredis')

        assert response.get_data(as_text=True) == 'Redis: FAIL'


class TestWebfingerIdentifiesItsCaller:
    """`requestor_domain()` reads the host out of a peer's User-Agent -- Lemmy and friends
    send `Lemmy/1.0; +https://peer.example` -- and webfinger uses it to apply the allowlist
    or the blocklist BEFORE answering anything about a local account.
    """

    PEER_UA = 'Lemmy/0.19.3; +https://peer.example'

    def test_a_named_peer_is_still_answered_when_it_is_not_banned(self, env):
        author = make_user(env.baseline.instance_local, 'fingered', local=True)
        db.session.commit()

        response = env.client.get(
            f'/.well-known/webfinger?resource=acct:{author.user_name}@'
            f"{env.app.config['SERVER_NAME']}",
            headers={'User-Agent': self.PEER_UA})

        assert response.status_code == 200

    def test_the_site_row_is_fetched_when_the_request_context_has_none(self, env):
        """`:60`. `g.site` is normally filled by `before_request`; webfinger re-fetches it
        rather than trusting that, because the allowlist decision one line below is an
        access check and `AttributeError` is not a refusal.

        Reaching it means calling the view with a `g` that has no `site`, which a test
        client cannot produce -- `before_request` always runs. `g` belongs to the app
        context, and the suite pushes one for the whole test, so the attribute is deleted
        rather than expected to be absent.
        """
        from app.activitypub.routes import webfinger

        with env.app.test_request_context(
                '/.well-known/webfinger?resource=acct:nobody@example.com',
                headers={'User-Agent': self.PEER_UA}):
            del g.site
            assert not hasattr(g, 'site')

            # `acct:nobody@example.com` is not an account here, so the view's own lookup
            # answers 404 -- which is raised, not returned. The assertion is that the
            # allowlist check above it ran at all, and it could only run with `g.site`
            # filled in by this line.
            with pytest.raises(NotFound):
                webfinger()

            assert g.site is not None
        g.site = env.site

    def test_a_request_with_no_peer_in_its_user_agent_skips_the_check(self, env):
        """The `if` itself. A browser sends no `+https://...`, so `requesting_domain` is
        empty and neither the allowlist nor the blocklist is consulted -- webfinger is a
        public discovery endpoint for everyone else."""
        author = make_user(env.baseline.instance_local, 'fingered', local=True)
        db.session.commit()

        response = env.client.get(
            f'/.well-known/webfinger?resource=acct:{author.user_name}@'
            f"{env.app.config['SERVER_NAME']}",
            headers={'User-Agent': 'Mozilla/5.0'})

        assert response.status_code == 200

    def test_a_request_with_no_resource_is_a_404(self, env):
        response = env.client.get('/.well-known/webfinger')

        assert response.status_code == 404
