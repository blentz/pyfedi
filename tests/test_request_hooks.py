"""Coverage for app/request_hooks.py.

Before this wiring moved into create_app(), no test could render a page: the
context processor, jinja globals and before_request lived in pyfedi.py, outside
the factory, so a test app had none of them and any render raised
AttributeError: site.

Every test here asserts on something observable: a response header, a `g`
value, a session value read back through the test client's cookie jar, a
rendered string, or a return value -- never just "the line executed".

A note on inspecting `g` after a test-client request: `tests/conftest.py` holds
one session-scoped app context for the whole test run, and Flask's
RequestContext.push() only pushes a fresh app context (with a fresh `g`) when
the current one belongs to a different app. Every test-client request in this
suite reuses that one app context, so `g` is not torn down when the request
context pops -- whatever before_request/after_request left on `g` (g.nonce,
g.site, g.admin_ids, g._login_user, ...) is still readable from the test after
`client.get(...)` returns. `db_session` clears `g.__dict__` before every test,
so this never leaks between tests.
"""

from datetime import datetime

import pytest
from flask import g, render_template_string
from sqlalchemy import text

from app import db
from app.models import Settings
from tests.factories import make_community, make_instance, make_post, make_user

pytestmark = pytest.mark.usefixtures('site')


def login(client, user):
    """Log a user in the way flask-login reads it back, without a real POST."""
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


# ---------------------------------------------------------------------------
# Step 1: the deliverable itself
# ---------------------------------------------------------------------------

def test_a_page_renders_under_test(app, db_session):
    """The point of the whole change: a route renders in the test app."""
    make_instance('test.piefed.local', software='piefed')

    with app.test_client() as client:
        response = client.get('/')

    assert response.status_code in (200, 302)


# ---------------------------------------------------------------------------
# before_request
# ---------------------------------------------------------------------------

def test_options_request_short_circuits_before_the_route(app, db_session):
    """OPTIONS gets the CORS preflight response, and never reaches index()."""
    with app.test_client() as client:
        response = client.options('/')

    assert response.status_code == 200
    assert response.headers['Access-Control-Allow-Origin'] == app.config.get('CORS_ALLOW_ORIGIN', '*')
    assert response.headers['Access-Control-Allow-Methods'] == 'GET, POST, PUT, DELETE, OPTIONS'
    # An empty body proves before_request's own make_response('', 200) was returned
    # and index() -- which renders a full HTML page -- never ran.
    assert response.data == b''


def test_normal_request_sets_nonce_locale_and_low_bandwidth(app, db_session):
    make_instance('test.piefed.local', software='piefed')

    with app.test_client() as client:
        client.get('/')

    assert isinstance(g.nonce, str) and len(g.nonce) > 0
    assert isinstance(g.locale, str) and len(g.locale) > 0
    assert g.low_bandwidth is False


def test_low_bandwidth_cookie_sets_g_low_bandwidth(app, db_session):
    make_instance('test.piefed.local', software='piefed')

    with app.test_client() as client:
        client.set_cookie('low_bandwidth', '1', domain=app.config['SERVER_NAME'])
        client.get('/')

    assert g.low_bandwidth is True


def test_inbox_path_does_not_get_g_site(app, db_session):
    """Deliberate: shared_inbox() sets g.site itself, to increase the chance of
    duplicate-Activity detection working across concurrently-processed requests."""
    with app.test_client() as client:
        # request_json is None here, so shared_inbox() returns 400 before it ever
        # sets g.site itself -- what we're checking is that before_request didn't
        # set it either.
        response = client.post('/inbox', data='null', content_type='application/json')

    assert response.status_code == 400
    assert not hasattr(g, 'site')


def test_static_path_does_not_get_g_site(app, db_session):
    with app.test_client() as client:
        response = client.get('/static/browserconfig.xml')

    assert response.status_code == 200
    assert not hasattr(g, 'site')


def test_admin_ids_computed_and_persisted_when_setting_absent(app, db_session):
    make_instance('test.piefed.local', software='piefed')
    assert Settings.query.filter_by(name='admin_ids').first() is None

    with app.test_client() as client:
        client.get('/auth/please_wait')

    assert g.admin_ids == []
    # get_setting() is cached in redis; before_request calls set_setting() to
    # persist what it computed, so a Settings row now exists.
    assert Settings.query.filter_by(name='admin_ids').first() is not None


def test_admin_ids_read_from_setting_when_present(app, db_session):
    from app.utils import set_setting
    make_instance('test.piefed.local', software='piefed')
    set_setting('admin_ids', [42])
    db.session.commit()

    with app.test_client() as client:
        client.get('/auth/please_wait')

    # [42] could only have come from the pre-existing setting: no user with id 42
    # exists, so a fresh computation would not produce it.
    assert g.admin_ids == [42]


def test_authenticated_request_updates_last_seen_and_clears_email_unread_sent(app, db_session):
    instance = make_instance('test.piefed.local', software='piefed')
    user = make_user(instance, 'lastseen', local=True)
    old_last_seen = datetime(2020, 1, 1)
    user.last_seen = old_last_seen
    user.email_unread_sent = True
    db.session.commit()

    with app.test_client() as client:
        login(client, user)
        client.get('/auth/please_wait')

    assert user.last_seen > old_last_seen
    assert user.email_unread_sent is False


def test_anonymous_windows_user_agent_sets_font_inter(app, db_session):
    make_instance('test.piefed.local', software='piefed')

    with app.test_client() as client:
        client.get('/', headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})

    assert g._login_user.font == 'inter'


def test_anonymous_non_windows_user_agent_clears_font(app, db_session):
    make_instance('test.piefed.local', software='piefed')

    with app.test_client() as client:
        client.get('/', headers={'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64)'})

    assert g._login_user.font == ''


def test_anonymous_external_referer_is_stored_in_session(app, db_session):
    """Session changes for an anonymous HTML response only survive on paths
    handled specially in after_request (e.g. /auth/); an ordinary anonymous page
    forces session.modified back to False so no session cookie is set at all --
    see test_anonymous_html_response_is_not_cached below. /auth/please_wait is one of
    the paths that does not do that, so it is used here to observe the write."""
    with app.test_client() as client:
        client.get('/auth/please_wait', headers={'Referer': 'http://elsewhere.example/page'})
        with client.session_transaction() as session:
            assert session['Referer'] == 'http://elsewhere.example/page'


def test_anonymous_same_server_referer_is_not_stored(app, db_session):
    with app.test_client() as client:
        client.get('/auth/please_wait', headers={'Referer': f"https://{app.config['SERVER_NAME']}/somewhere"})
        with client.session_transaction() as session:
            assert 'Referer' not in session


# ---------------------------------------------------------------------------
# after_request
# ---------------------------------------------------------------------------

def test_cors_headers_present_on_a_normal_response(app, db_session):
    with app.test_client() as client:
        response = client.get('/auth/please_wait')

    assert response.headers['Access-Control-Allow-Origin'] == app.config.get('CORS_ALLOW_ORIGIN', '*')
    assert response.headers['Access-Control-Allow-Methods'] == 'GET, POST, PUT, DELETE, OPTIONS'
    assert response.headers['Access-Control-Allow-Headers'] == 'Content-Type, Authorization, Accept, User-Agent'


def test_static_path_gets_long_lived_cache_control(app, db_session):
    with app.test_client() as client:
        response = client.get('/static/browserconfig.xml')

    assert response.headers['Cache-Control'] == 'public, max-age=31536000'


def test_activity_json_response_does_not_set_a_session_cookie(app, db_session):
    instance = make_instance('test.piefed.local', software='piefed')
    user = make_user(instance, 'apuser', local=True)

    with app.test_client() as client:
        # An external Referer would normally mark the session modified (see
        # test_anonymous_external_referer_is_stored_in_session); the
        # activity+json branch must override that and suppress the cookie.
        response = client.get(f'/u/{user.user_name}',
                              headers={'Accept': 'application/activity+json',
                                       'Referer': 'http://elsewhere.example/page'})

    assert response.content_type == 'application/activity+json'
    set_cookie_headers = response.headers.getlist('Set-Cookie')
    assert not any('session=' in h for h in set_cookie_headers)


def test_html_response_gets_nosniff_and_deny(app, db_session):
    with app.test_client() as client:
        response = client.get('/auth/please_wait')

    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    assert response.headers['X-Frame-Options'] == 'DENY'


def test_embed_path_does_not_get_x_frame_options(app, db_session):
    instance = make_instance('test.piefed.local', software='piefed')
    user = make_user(instance, 'embedauthor', local=True)
    community = make_community('embedcomm')
    post = make_post(community, user, 'https://test.piefed.local/p/1', title='embed me')

    with app.test_client() as client:
        response = client.get(f'/post/{post.id}/embed')

    assert response.status_code == 200
    assert 'X-Frame-Options' not in response.headers
    # X-Content-Type-Options is not gated by the /embed exclusion, so it still
    # shows up here -- confirms this is the /embed branch and not some other
    # reason the header is missing.
    assert response.headers['X-Content-Type-Options'] == 'nosniff'


def test_auth_register_path_skips_csp_hsts_and_frame_headers(app, db_session, monkeypatch):
    """The whole CSP/HSTS/X-Content-Type-Options/X-Frame-Options block is skipped
    for any path containing 'auth/register' -- registration pages redirect through
    third-party payment/CAPTCHA iframes that this would otherwise interfere with.

    Uses a path that merely contains that substring and matches no route (404),
    rather than the real /auth/register view: that view renders RegistrationForm
    through a template that calls form.csrf_token() the same way login.html does,
    which raises under this suite's WTF_CSRF_ENABLED=False (see the other /auth/
    tests' use of /auth/please_wait instead). The condition being tested here is
    purely a substring check on request.path, so a 404 on a matching path exercises
    the exact same branch without going anywhere near that template."""
    monkeypatch.setitem(app.config, 'HTTP_PROTOCOL', 'https')

    with app.test_client() as client:
        response = client.get('/auth/register-nonexistent-path-for-branch-coverage')

    assert response.status_code == 404
    assert 'Content-Security-Policy' not in response.headers
    assert 'Strict-Transport-Security' not in response.headers
    assert 'X-Content-Type-Options' not in response.headers
    assert 'X-Frame-Options' not in response.headers


def test_csp_header_present_for_authenticated_non_htmx_request(app, db_session):
    instance = make_instance('test.piefed.local', software='piefed')
    user = make_user(instance, 'cspuser', local=True)

    with app.test_client() as client:
        login(client, user)
        response = client.get('/auth/please_wait')

    assert response.headers['Content-Security-Policy'] == \
        f"script-src 'self' 'nonce-{g.nonce}' 'strict-dynamic'; object-src 'none'; base-uri 'none';"


def test_no_csp_header_for_htmx_request(app, db_session):
    instance = make_instance('test.piefed.local', software='piefed')
    user = make_user(instance, 'htmxuser', local=True)

    with app.test_client() as client:
        login(client, user)
        response = client.get('/auth/please_wait', headers={'HX-Request': 'true'})

    assert 'Content-Security-Policy' not in response.headers


def test_hsts_header_present_when_protocol_is_https(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'HTTP_PROTOCOL', 'https')

    with app.test_client() as client:
        response = client.get('/auth/please_wait')

    assert response.headers['Strict-Transport-Security'] == 'max-age=63072000; includeSubDomains; preload'


def test_hsts_header_absent_when_protocol_is_not_https(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'HTTP_PROTOCOL', 'http')

    with app.test_client() as client:
        response = client.get('/auth/please_wait')

    assert 'Strict-Transport-Security' not in response.headers


def test_rsl_license_link_present_when_ai_crawlers_not_allowed(app, db_session, monkeypatch):
    """app/utils.py's render_template() wrapper also sets its own 'Link' header
    (Early-Hints preload directives) on every rendered page, before after_request
    runs. after_request appends to that with response.headers.add(), so the
    response ends up with two separate Link header lines -- getlist() is needed
    to see both; response.headers['Link'] would only return the first one."""
    monkeypatch.setitem(app.config, 'ALLOW_AI_CRAWLERS', False)

    with app.test_client() as client:
        response = client.get('/auth/please_wait')

    links = response.headers.getlist('Link')
    assert any('rel="license"' in link and f"https://{app.config['SERVER_NAME']}/rsl.xml" in link
              for link in links)


def test_rsl_license_link_absent_when_ai_crawlers_allowed(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'ALLOW_AI_CRAWLERS', True)

    with app.test_client() as client:
        response = client.get('/auth/please_wait')

    links = response.headers.getlist('Link')
    assert not any('rel="license"' in link for link in links)


def test_api_paths_get_no_store_cache_control(app, db_session):
    with app.test_client() as client:
        response = client.get('/api/this-path-does-not-exist')

    assert response.status_code == 404
    assert response.headers['Cache-Control'] == 'no-store'


def _make_embeddable_post():
    instance = make_instance('test.piefed.local', software='piefed')
    author = make_user(instance, 'cacheauthor', local=True)
    community = make_community('cachecomm')
    post = make_post(community, author, 'https://test.piefed.local/p/cache', title='cache me')
    return post, instance


def test_authenticated_html_response_gets_no_store_cache_control(app, db_session):
    """Uses /post/<id>/embed rather than an /auth/ path: /auth/please_wait gets
    no-store purely because of its path prefix (see after_request), which would
    not isolate the is_authenticated condition this test is about.

    render_template() (app/utils.py) sets its own Cache-Control for anonymous
    responses before after_request runs, but only for anonymous ones -- for an
    authenticated viewer it sets nothing, so after_request's
    setdefault('Cache-Control', ...) is free to apply."""
    post, instance = _make_embeddable_post()
    viewer = make_user(instance, 'cacheviewer', local=True)

    with app.test_client() as client:
        login(client, viewer)
        response = client.get(f'/post/{post.id}/embed')

    assert response.headers['Cache-Control'] == 'no-store, no-cache, must-revalidate, private'


def test_anonymous_html_response_is_not_marked_no_store(app, db_session):
    post, _instance = _make_embeddable_post()

    with app.test_client() as client:
        response = client.get(f'/post/{post.id}/embed')

    # render_template() sets its own 'no-cache, must-revalidate' for anonymous
    # viewers; the point here is that after_request's no-store string is not it.
    assert response.headers.get('Cache-Control') != 'no-store, no-cache, must-revalidate, private'


def test_html_response_vary_includes_accept_language_and_cookie(app, db_session):
    """Needs a logged-in client: app/__init__.py's StripCookieVaryForAnonymous WSGI
    middleware strips 'Cookie' back out of Vary for requests with no session cookie
    (so anonymous responses stay cacheable), which would mask the header this hook
    sets if the request were anonymous."""
    instance = make_instance('test.piefed.local', software='piefed')
    user = make_user(instance, 'varyuser', local=True)

    with app.test_client() as client:
        login(client, user)  # session_transaction() writes the cookie into the jar immediately
        response = client.get('/auth/please_wait')

    assert 'Accept-Language' in response.headers['Vary']
    assert 'Cookie' in response.headers['Vary']


# ---------------------------------------------------------------------------
# context processor
# ---------------------------------------------------------------------------

def test_context_processor_supplies_nonce_from_g(app, db_session):
    with app.test_request_context('/'):
        g.nonce = 'test-nonce-value'
        rendered = render_template_string('{{ nonce }}')

    assert rendered == 'test-nonce-value'


def test_context_processor_nonce_falls_back_to_none_without_g(app, db_session):
    with app.test_request_context('/'):
        rendered = render_template_string('{{ nonce }}')

    assert rendered == 'None'


def test_context_processor_site_falls_back_to_none_without_g(app, db_session):
    with app.test_request_context('/'):
        rendered = render_template_string('{{ site }}')

    assert rendered == 'None'


def test_context_processor_supplies_site_from_g(app, db_session):
    make_instance('test.piefed.local', software='piefed')

    with app.test_request_context('/'):
        from app.utils import get_site_as_dict
        from app.models import Site
        g.site = Site(**get_site_as_dict())
        rendered = render_template_string('{{ site.name }}')

    assert rendered == 'Test Site'


# ---------------------------------------------------------------------------
# teardown_appcontext
# ---------------------------------------------------------------------------

def test_teardown_removes_the_session_for_the_popped_context(app, db_session):
    from flask.globals import app_ctx as app_ctx_proxy

    with app.app_context():
        key = id(app_ctx_proxy._get_current_object())
        db.session.execute(text('SELECT 1'))
        assert key in db.session.registry.registry

    assert key not in db.session.registry.registry


def test_teardown_rolls_back_then_removes_on_exception(app, db_session, monkeypatch):
    """Flask-SQLAlchemy 3.x registers its own teardown_appcontext (db.init_app(),
    called before register_request_hooks() in create_app()) that unconditionally
    calls db.session.remove(). Teardown callbacks run in reverse registration
    order, so ours (rollback-then-remove, only on exception) runs first, then
    Flask-SQLAlchemy's runs and removes again -- hence 'remove' appears twice
    below. That second removal is harmless (scoped_session.remove() is a no-op if
    nothing is registered for the current scope) but it does mean the removal
    half of this hook is redundant; the rollback-before-removing is the only part
    Flask-SQLAlchemy's own teardown does not already provide."""
    calls = []
    monkeypatch.setattr(db.session, 'rollback', lambda: calls.append('rollback'))
    monkeypatch.setattr(db.session, 'remove', lambda: calls.append('remove'))

    with pytest.raises(RuntimeError):
        with app.app_context():
            raise RuntimeError('boom')

    assert calls == ['rollback', 'remove', 'remove']


# ---------------------------------------------------------------------------
# shell_context_processor
# ---------------------------------------------------------------------------

def test_shell_context_processor_returns_db_and_app(app, db_session):
    make_shell_context = next(
        f for f in app.shell_context_processors if f.__name__ == 'make_shell_context'
    )

    assert make_shell_context() == {'db': db, 'app': app}
