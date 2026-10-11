"""Phase S6 of the security track: secure mode. With Site.require_signed_fetch on, an ActivityPub GET or HEAD that
no stored actor validly signed gets 401. The instance actor, WebFinger and NodeInfo stay open; HTML is never
affected. Spec: "Secure mode". Review Focus 4.
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from flask import g
from sqlalchemy import inspect, text

from app import db
from app.activitypub.entitlement import MINIMAL_ACTOR_ENDPOINTS, MINIMAL_ACTOR_FIELDS, SIGNED_FETCH_AP_ONLY_PREFIXES, \
    SIGNED_FETCH_EXEMPT_PREFIXES, actor_document, signed_fetch_exempt, signed_fetch_refusal
from app.activitypub.util import AS_PUBLIC
from app.admin.forms import FederationForm
from app.models import Site
from tests.factories import a_keypair, cavage_signed_headers, make_activitypub_log, make_instance, make_local_feed, \
    make_site, make_user, signed_get_headers
from tests.test_ap_content_objects import AP_ACCEPT, _double_the_delegates, ap_get, browser_get, seed_local_post, \
    signed_ap_get

MIGRATION = Path(__file__).resolve().parent.parent / 'migrations' / 'versions' / 'd9f3b7a15c2e_site_require_signed_fetch.py'
UA = 'Test (+https://peer2.example)'


# --- the exemption and the refusal, unit level ---

@pytest.mark.parametrize('path, exempt', [
    ('/actor', True), ('/actor/outbox', True), ('/actor/inbox', True),
    ('/.well-known/webfinger', True), ('/.well-known/nodeinfo', True),
    ('/nodeinfo/2.0', True), ('/nodeinfo/2.1.json', True),
    ('/actors', False), ('/actorx/outbox', False), ('/nodeinfo', False), ('/post/1', False), ('/u/alice', False),
])
def test_the_exempt_routes(path, exempt):
    assert signed_fetch_exempt(path) is exempt


def test_the_exempt_and_ap_only_lists_are_pinned():
    """Widening either list changes what secure mode protects: it must be a deliberate edit to this test too."""
    assert SIGNED_FETCH_EXEMPT_PREFIXES == ('/actor', '/.well-known/', '/nodeinfo/')
    assert SIGNED_FETCH_AP_ONLY_PREFIXES == ('/activities/', '/quote_boost_auth')
    # R-SECUREACTORS: answered unsigned, but only in minimal form
    assert MINIMAL_ACTOR_ENDPOINTS == ('activitypub.user_profile', 'activitypub.community_profile',
                                       'activitypub.feed_profile')
    assert MINIMAL_ACTOR_FIELDS == ('@context', 'id', 'type', 'preferredUsername', 'inbox', 'publicKey')


def _refusal(app, path='/post/1', method='GET', accept=AP_ACCEPT, secure=True):
    """g lives on the app context, which the session-wide app fixture keeps pushed: drop g.site after, or it leaks."""
    with app.test_request_context(path, method=method, headers={'Accept': accept}):
        g.site = SimpleNamespace(require_signed_fetch=secure)
        try:
            return signed_fetch_refusal()
        finally:
            g.pop('site', None)


@pytest.mark.parametrize('method', ['GET', 'HEAD'])
def test_an_unsigned_activitypub_fetch_is_refused_with_a_no_store_401(app, method):
    response = _refusal(app, method=method)

    assert response.status_code == 401
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.headers['WWW-Authenticate'] == 'Signature'
    assert response.get_data() == b''


@pytest.mark.parametrize('accept', ['', '*/*', 'application/json', 'text/html'])
@pytest.mark.parametrize('path', ['/activities/create/x', '/quote_boost_auth'])
def test_an_ap_only_route_is_refused_whatever_the_accept_header(app, path, accept):
    """These answer ActivityPub JSON for any Accept, so the header cannot be what decides."""
    assert _refusal(app, path=path, accept=accept).status_code == 401


@pytest.mark.parametrize('kwargs', [{'method': 'POST'}, {'accept': 'text/html'}, {'secure': False},
                                    {'path': '/actor/outbox'}, {'path': '/.well-known/nodeinfo'}])
def test_nothing_else_is_refused(app, kwargs):
    assert _refusal(app, **kwargs) is None


def test_a_request_with_no_site_loaded_is_not_refused(app):
    """/inbox and /static/ never load g.site."""
    with app.test_request_context('/post/1', headers={'Accept': AP_ACCEPT}):
        g.pop('site', None)    # whatever an earlier request on the shared app context left behind
        assert signed_fetch_refusal() is None


# --- through the real request hook ---

@pytest.fixture
def secure_post(app, db_session, monkeypatch):
    calls = _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    db.session.get(Site, 1).require_signed_fetch = True
    db.session.commit()
    return SimpleNamespace(post=post, calls=calls)


def test_secure_mode_refuses_an_unsigned_fetch_of_a_post(app, secure_post):
    response = ap_get(app, f'/post/{secure_post.post.id}')

    assert response.status_code == 401
    assert response.headers['Cache-Control'] == 'no-store'


def test_secure_mode_refuses_an_unsigned_head_like_a_get(app, secure_post):
    with app.test_client() as client:
        assert client.head(f'/post/{secure_post.post.id}', headers={'Accept': AP_ACCEPT}).status_code == 401


def test_secure_mode_serves_a_signer_stored_here(app, secure_post):
    signer = make_user(make_instance('peer2.example'), 'bob', with_keys=True)

    assert signed_ap_get(app, f'/post/{secure_post.post.id}', signer, UA).status_code == 200


def test_secure_mode_refuses_a_signer_never_seen_here(app, secure_post):
    """No fetch on the request path: an unknown signer is as good as no signature."""
    private_key, _public = a_keypair()
    ghost = SimpleNamespace(private_key=private_key, ap_profile_id='https://unknown.example/users/ghost')

    assert signed_ap_get(app, f'/post/{secure_post.post.id}', ghost, UA).status_code == 401


def test_secure_mode_leaves_a_browser_alone(app, secure_post):
    response = browser_get(app, f'/post/{secure_post.post.id}')

    assert response.status_code == 200
    assert len(secure_post.calls['show_post']) == 1


def test_secure_mode_leaves_nodeinfo_open(app, secure_post):
    assert ap_get(app, '/.well-known/nodeinfo').status_code == 200


@pytest.mark.parametrize('path', ['/actor', '/actor/outbox', '/.well-known/webfinger?resource=acct:{server}@{server}',
                                  '/.well-known/nodeinfo', '/nodeinfo/2.0'])
def test_secure_mode_leaves_every_exempt_route_open_end_to_end(app, secure_post, path):
    """P13: a peer must reach the instance actor, WebFinger and NodeInfo unsigned, or it can never learn a key."""
    server = app.config['SERVER_NAME']

    response = ap_get(app, path.format(server=server))

    assert response.status_code == 200
    if 'webfinger' in path:
        assert response.json['subject'] == f'acct:{server}@{server}'


def test_with_secure_mode_off_an_unsigned_fetch_is_served(app, db_session, monkeypatch):
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()

    assert ap_get(app, f'/post/{post.id}').status_code == 200


# --- the migration ---

def _load_migration():
    spec = importlib.util.spec_from_file_location('require_signed_fetch_migration', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _column(connection):
    return next((c for c in inspect(connection).get_columns('site') if c['name'] == 'require_signed_fetch'), None)


def test_require_signed_fetch_is_not_null_and_defaults_to_false(app):
    with db.engine.connect() as connection:
        column = _column(connection)
    assert column is not None
    assert column['nullable'] is False
    assert 'false' in column['default'].lower()


def test_downgrade_then_upgrade_restores_the_column_off(app, db_session):
    make_site()
    db.session.commit()
    db.session.close()    # nothing of the session's may still hold a lock on site
    migration = _load_migration()
    assert migration.down_revision == 'e5c1d8a2b7f9'

    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
                assert _column(connection) is None
                migration.upgrade()
            assert _column(connection)['nullable'] is False
            assert connection.execute(text('SELECT require_signed_fetch FROM site WHERE id = 1')).scalar() is False
        finally:
            transaction.rollback()


# --- R-SECURECACHE: a signed answer must not reach an unsigned requester through a shared cache ---

def _signed_get(app, path, signer):
    """Production's signer adds and signs 'Accept: application/ld+json', so the header is sent as signed."""
    headers = signed_get_headers(path, signer)
    headers['User-Agent'] = UA
    with app.test_client() as client:
        return client.get(path, headers=headers)


def _privately_cached(response):
    directives = [d.strip() for d in response.headers['Cache-Control'].split(',')]
    vary = [v.strip() for v in response.headers['Vary'].split(',')]
    return 'private' in directives and 'public' not in directives and 'Signature' in vary


@pytest.mark.parametrize('suffix', ['', '/replies'])
def test_under_secure_mode_a_signed_answer_is_private_and_varies_on_the_signature(app, secure_post, suffix):
    signer = make_user(make_instance('peer2.example'), 'bob', with_keys=True)

    response = _signed_get(app, f'/post/{secure_post.post.id}{suffix}', signer)

    assert response.status_code == 200
    assert _privately_cached(response)
    assert 'max-age=' in response.headers['Cache-Control']    # the signer's own cache may still keep it


@pytest.mark.parametrize('suffix', ['', '/replies'])
def test_with_secure_mode_off_public_caching_is_unchanged(app, db_session, monkeypatch, suffix):
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()

    response = ap_get(app, f'/post/{post.id}{suffix}')

    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'public, max-age=120'
    assert 'Accept' in response.headers['Vary'] and 'Signature' not in response.headers['Vary']


def test_secure_mode_leaves_the_exempt_routes_publicly_cacheable(app, secure_post):
    response = ap_get(app, '/actor/outbox')

    assert response.headers['Cache-Control'] == 'public, max-age=60'
    assert 'Signature' not in response.headers.get('Vary', '')


@pytest.fixture
def secure_activity(app, db_session, redis_double):
    make_site()
    activity_id = f"https://{app.config['SERVER_NAME']}/activities/create/pub"
    make_activitypub_log(activity_id, direction='out', activity_type='Create',
                         activity_json=json.dumps({'id': activity_id, 'type': 'Create', 'to': [AS_PUBLIC[0]]}))
    db.session.get(Site, 1).require_signed_fetch = True
    db.session.commit()
    return '/activities/create/pub'



@pytest.mark.parametrize('accept', [None, '*/*', 'application/json'])
def test_secure_mode_refuses_an_unsigned_activity_whatever_the_accept(app, secure_activity, accept):
    headers = {'Accept': accept} if accept else {}
    with app.test_client() as client:
        response = client.get(secure_activity, headers=headers)

    assert response.status_code == 401


def test_secure_mode_serves_a_signed_activity_privately(app, secure_activity):
    signer = make_user(make_instance('peer2.example'), 'bob', with_keys=True)

    response = _signed_get(app, secure_activity, signer)

    assert response.status_code == 200
    assert _privately_cached(response)



# --- R-SECUREACTORS: an unsigned fetch of a user, community or feed actor gets its keys, not a 401 ---

MINIMAL_ACTOR_KEYS = {'@context', 'id', 'type', 'preferredUsername', 'inbox', 'endpoints', 'publicKey'}


@pytest.fixture
def secure_actors(app, db_session, monkeypatch):
    """A local user, community and public feed, with secure mode on."""
    _double_the_delegates(monkeypatch)
    community, author, _post = seed_local_post()
    feed = make_local_feed('localfeed', public=True)
    db.session.get(Site, 1).require_signed_fetch = True
    db.session.commit()
    return {'user': f'/u/{author.user_name}', 'community': f'/c/{community.name}', 'feed': f'/f/{feed.name}'}


def _public_actor_cache(response):
    directives = [d.strip() for d in response.headers['Cache-Control'].split(',')]
    vary = [v.strip() for v in response.headers['Vary'].split(',')]
    return 'public' in directives and 'private' not in directives and 'Accept' in vary and 'Signature' in vary


@pytest.mark.parametrize('kind', ['user', 'community', 'feed'])
def test_an_unsigned_fetch_of_an_actor_gets_a_minimal_document(app, secure_actors, kind):
    """A peer signing its key fetches with an instance actor this instance has never seen must still learn keys."""
    response = ap_get(app, secure_actors[kind])

    assert response.status_code == 200
    document = response.json
    assert set(document) == MINIMAL_ACTOR_KEYS
    assert document['endpoints'] == {'sharedInbox': f"{app.config['SERVER_URL']}/inbox"}
    assert document['publicKey']['owner'] == document['id']
    assert 'publicKeyPem' in document['publicKey']
    assert _public_actor_cache(response)   # the same for every unsigned requester; a signer never shares its entry


@pytest.mark.parametrize('kind', ['user', 'community', 'feed'])
def test_a_signer_never_seen_here_gets_the_minimal_document(app, secure_actors, kind):
    private_key, _public = a_keypair()
    ghost = SimpleNamespace(private_key=private_key, ap_profile_id='https://unknown.example/users/ghost')

    response = signed_ap_get(app, secure_actors[kind], ghost, UA)

    assert response.status_code == 200
    assert set(response.json) == MINIMAL_ACTOR_KEYS


@pytest.mark.parametrize('kind', ['user', 'community', 'feed'])
def test_a_signer_stored_here_gets_the_full_document_privately(app, secure_actors, kind):
    signer = make_user(make_instance('peer2.example'), 'bob', with_keys=True)

    response = signed_ap_get(app, secure_actors[kind], signer, UA)

    assert response.status_code == 200
    assert 'outbox' in response.json and set(response.json) > MINIMAL_ACTOR_KEYS
    assert _privately_cached(response)


def test_an_unsigned_head_of_a_user_actor_is_answered(app, secure_actors):
    with app.test_client() as client:
        assert client.head(secure_actors['user'], headers={'Accept': AP_ACCEPT}).status_code == 200


@pytest.mark.parametrize('suffix', ['/outbox', '/followers'])
def test_an_actors_collections_stay_refused_unsigned(app, secure_actors, suffix):
    assert ap_get(app, secure_actors['community'] + suffix).status_code == 401


def test_with_secure_mode_off_an_unsigned_actor_fetch_gets_the_full_document(app, secure_actors):
    db.session.get(Site, 1).require_signed_fetch = False
    db.session.commit()

    response = ap_get(app, secure_actors['user'])

    assert 'outbox' in response.json
    assert response.headers['Cache-Control'] == 'public, max-age=300'
    assert 'Signature' not in response.headers['Vary']


def test_a_minimal_document_omits_endpoints_the_actor_does_not_have(app):
    with app.test_request_context('/u/alice') as context:
        context.request.environ['pyfedi.minimal_actor_document'] = True
        assert actor_document({'id': 'x', 'type': 'Person', 'outbox': 'o'}) == {'id': 'x', 'type': 'Person'}


# --- R-SECUREACTORS (b): a cavage (request-target) covers the query string ---

PAGED = '/c/books/outbox?page=1'


def _cavage_get(app, path, signer, target):
    """GET `path` with a cavage signature whose (request-target) is `get <target>`."""
    headers = cavage_signed_headers(signer.private_key, f'{signer.ap_profile_id}#main-key', method='get', path=target,
                                    signed=('(request-target)', 'host', 'date'))
    headers.update({'Accept': AP_ACCEPT, 'User-Agent': UA})
    with app.test_client() as client:
        return client.get(path, headers=headers)


def test_a_paged_collection_signed_in_the_spec_form_is_served(app, secure_post):
    """Mastodon (4.2+) and GoToSocial sign '<method> <path>?<query>'."""
    signer = make_user(make_instance('peer2.example'), 'bob', with_keys=True)

    assert _cavage_get(app, PAGED, signer, target=PAGED).status_code == 200


def test_a_paged_collection_signed_without_its_query_is_still_served(app, secure_post):
    """Older senders (earlier PieFed, older Mastodon) left the query out; Mastodon still accepts that form too."""
    signer = make_user(make_instance('peer2.example'), 'bob', with_keys=True)

    assert _cavage_get(app, PAGED, signer, target='/c/books/outbox').status_code == 200


def test_a_signature_over_another_query_is_refused(app, secure_post):
    signer = make_user(make_instance('peer2.example'), 'bob', with_keys=True)

    assert _cavage_get(app, PAGED, signer, target='/c/books/outbox?page=2').status_code == 401


def test_this_instance_signs_the_query_of_a_get(app, secure_post):
    """Production's signer covers the query: its signature for ?page=1 does not verify for ?page=2."""
    signer = make_user(make_instance('peer2.example'), 'bob', with_keys=True)
    headers = signed_get_headers(PAGED, signer)
    headers['User-Agent'] = UA
    with app.test_client() as client:
        assert client.get(PAGED, headers=headers).status_code == 200
        assert client.get('/c/books/outbox?page=2', headers=headers).status_code == 401


# --- R-SECUREACTORS (d): the admin help text says what secure mode asks of peers and what stays open ---

def test_the_admin_help_text_describes_secure_mode(app):
    with app.test_request_context('/admin/misc'):
        description = str(FederationForm(meta={'csrf': False}).require_signed_fetch.description)

    assert 'sign their fetches with an actor key this instance already knows' in description
    assert 'actor documents stay readable in a minimal form' in description
    assert 'web pages, through the API and in RSS feeds' in description
