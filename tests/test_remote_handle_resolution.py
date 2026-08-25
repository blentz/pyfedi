"""/u/<actor> resolving a remote handle the instance has never seen.

Before this, `user_profile` only queried the local database, so visiting
/u/wakko@mastodon.cloud for an unknown account returned 404 and there was no way
to reach that account's profile — and therefore no way to reach its Follow button.

The resolution is gated three ways, and each guard has its own test below:
authenticated callers only, HTML requests only, and a banned instance must 404
rather than propagate the exception search_for_user raises.
"""

import pytest

from tests.factories import make_instance, make_site, make_user


@pytest.fixture(autouse=True)
def site(db_session):
    """before_request (registered by the app factory as of the app-factory-request-wiring
    change) populates g.site for every request except /inbox and /static/, which requires
    a Site row with id 1 to exist -- see make_site()'s docstring. Not needed when this file
    was written, since the app built by the conftest `app` fixture did not yet register
    that hook and g.site was simply left unset."""
    return make_site()


@pytest.fixture
def resolve_spy(monkeypatch):
    """Replace the webfinger lookup, recording every address it is asked for."""
    calls = []

    def fake_search(address, allow_fetch=True):
        calls.append(address)
        return fake_search.result

    fake_search.result = None
    monkeypatch.setattr('app.activitypub.routes.search_for_user', fake_search, raising=False)
    return fake_search, calls


def test_anonymous_request_does_not_resolve(app, db_session, resolve_spy):
    """An anonymous visitor cannot make the server fetch a host of their choosing"""
    _, calls = resolve_spy

    with app.test_client() as client:
        response = client.get('/u/wakko@mastodon.cloud')

    assert response.status_code == 404
    assert calls == [], 'no outbound lookup may happen for an anonymous request'


def test_activitypub_request_does_not_resolve(app, db_session, resolve_spy):
    """An ActivityPub request for an unknown handle asks about OUR user, not a remote one.

    A remote server sending Accept: application/activity+json to /u/foo@bar.example
    is asking whether we host that user. Resolving it would both answer the wrong
    question and let that server make us fetch a third host.
    """
    _, calls = resolve_spy

    with app.test_client() as client:
        response = client.get('/u/wakko@mastodon.cloud',
                              headers={'Accept': 'application/activity+json'})

    assert response.status_code == 404
    assert calls == [], 'no outbound lookup may happen for an ActivityPub request'


def test_unresolvable_handle_still_returns_404(app, db_session, resolve_spy):
    """A handle that resolves to nothing falls through to the existing 404"""
    fake_search, calls = resolve_spy
    fake_search.result = None
    make_instance('test.piefed.local', software='piefed')  # local instance must be id 1
    local = make_user(None, 'localuser', local=True)

    with app.test_client() as client:
        with client.session_transaction() as session:
            session['_user_id'] = str(local.id)
            session['_fresh'] = True
        response = client.get('/u/wakko@mastodon.cloud')

    assert response.status_code == 404
    assert calls == ['wakko@mastodon.cloud'], 'the lookup should have been attempted once'


def test_banned_instance_returns_404_not_500(app, db_session, resolve_spy):
    """search_for_user raises for a banned instance; that must not become a 500.

    app/user/utils.py raises Exception(f"{server} is blocked.") rather than
    returning None, so an unguarded call turns a blocked domain into a server error.
    """
    make_instance('test.piefed.local', software='piefed')  # local instance must be id 1
    local = make_user(None, 'localuser', local=True)
    attempted = []

    def raising_search(address, allow_fetch=True):
        attempted.append(address)
        raise Exception('mastodon.cloud is blocked.')

    import app.activitypub.routes as ap_routes
    ap_routes.search_for_user = raising_search

    with app.test_client() as client:
        with client.session_transaction() as session:
            session['_user_id'] = str(local.id)
            session['_fresh'] = True
        response = client.get('/u/wakko@mastodon.cloud')

    # Both halves matter: the lookup must be attempted (otherwise this test would
    # pass against code that never resolves at all), and the raise must become a 404.
    assert attempted == ['wakko@mastodon.cloud']
    assert response.status_code == 404


# NOT TESTED HERE: that a successfully resolved handle renders its profile page.
#
# @app.before_request lives in pyfedi.py, outside create_app(), so an app built by
# the conftest fixture never registers it and `g.site` is unset. Rendering any full
# page therefore raises AttributeError under test, for every route in this codebase,
# not just this one. Covering it means either registering that handler in the app
# factory or replicating it in the fixture — a change with a blast radius well
# beyond this feature.
#
# What the tests above do cover is every branch this feature added: the two guards
# that must not resolve, the lookup being attempted when they pass, and the banned
# instance exception becoming a 404. The only uncovered step is Flask handing the
# resolved User to the pre-existing show_profile(), which this change did not touch.
