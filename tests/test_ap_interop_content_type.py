"""Interop C1 item 5 (docs/superpowers/specs/2026-10-11-ap-interop-c1-design.md): the ActivityPub content type.

Served: `application/ld+json; profile="https://www.w3.org/ns/activitystreams"` when the Accept header prefers
JSON-LD, else `application/activity+json`, through one helper, ap_media_type(). Asked for: every outgoing
ActivityPub GET sends both forms (AP_FETCH_ACCEPT), signed GETs included, and the signature covers it.
is_activitypub_request() ignores a media type whose q-value is 0.
"""
import pathlib
import re

import pytest
from flask import Response, g

from app import constants, db
from app.activitypub import entitlement
from app.activitypub.actor import fetch_remote_actor_data
from app.activitypub.entitlement import secure_mode_cache_headers
from app.activitypub.routes import ap_media_type
from app.activitypub.signature import signed_get_request
from app.constants import AP_FETCH_ACCEPT, AS2_LD_MEDIA_TYPE
from app.main.routes import ap_application_cache_key
from app.utils import accepted_media_ranges, is_activitypub_request
from tests.factories import a_keypair, make_community, make_instance, make_site, make_user, signed_get_headers
from tests.test_actor_profiles import _double_the_renderers, profile_get, seed_actors
from tests.test_ap_security_entitlement import _requester_for
from tests.test_main_front_page import env  # noqa: F401 -- the fixture

AP = 'application/activity+json'
LD_PLAIN = 'application/ld+json'
APP = pathlib.Path(__file__).resolve().parent.parent / 'app'


# ---- parsing -----------------------------------------------------------------------------------------------------

def test_accepted_media_ranges_reads_types_parameters_and_q_values():
    assert accepted_media_ranges('Application/LD+JSON; Profile="x y"; q=0.4, text/html,,a/b;q=junk;q=2') == [
        ('application/ld+json', {'profile': 'x y'}, 0.4),
        ('text/html', {}, 1.0),
        ('a/b', {}, 1.0),   # an unreadable q counts as 1; one above 1 is clamped to 1
    ]


def test_accepted_media_ranges_reads_a_q_of_nan_as_unreadable():
    """float() reads 'nan', and clamping NaN with min/max leaves it NaN: q came out outside 0..1. Like any
    other unreadable q it counts as 1. Found by the accept_header fuzz target."""
    assert accepted_media_ranges('a/b;q=nan, c/d;q=-NaN') == [('a/b', {}, 1.0), ('c/d', {}, 1.0)]


def test_accepted_media_ranges_keeps_a_quoted_comma_or_semicolon_inside_its_parameter():
    assert accepted_media_ranges('application/ld+json; profile="a,b;c"; q=0.4, text/html;q=0') == [
        ('application/ld+json', {'profile': 'a,b;c'}, 0.4),
        ('text/html', {}, 0.0),
    ]


def test_accepted_media_ranges_reads_an_escaped_quote_as_part_of_the_value():
    assert accepted_media_ranges(r'a/b; x="p\"q,r", c/d') == [('a/b', {'x': r'p\"q,r'}, 1.0), ('c/d', {}, 1.0)]


def test_secure_mode_and_the_vary_hook_recognise_the_same_answer_types():
    assert entitlement.AP_MEDIA_TYPES is constants.AP_MEDIA_TYPES


@pytest.mark.parametrize('accept, expected', [
    (AP, AP),
    (AS2_LD_MEDIA_TYPE, AS2_LD_MEDIA_TYPE),
    (LD_PLAIN, AS2_LD_MEDIA_TYPE),
    (f'{AP};q=0.5, {LD_PLAIN};q=0.9', AS2_LD_MEDIA_TYPE),
    (f'{AP};q=0.9, {AS2_LD_MEDIA_TYPE};q=0.5', AP),
    (AP_FETCH_ACCEPT, AP),                                      # equal preference: activity+json
    (f'{LD_PLAIN};q=0', AP),
    (f'{AP};q=0, {LD_PLAIN}', AS2_LD_MEDIA_TYPE),
    ('application/ld+json; profile="https://example.com/other"', AP),   # JSON-LD, but not ActivityStreams
    ('text/html, */*;q=0.8', AP),
    ('', AP),
])
def test_the_served_content_type_follows_the_accept_header(app, accept, expected):
    with app.test_request_context('/', headers={'Accept': accept}):
        assert ap_media_type() == expected


@pytest.mark.parametrize('accept, expected', [
    (AP, True),
    (LD_PLAIN, True),
    (AS2_LD_MEDIA_TYPE, True),
    (f'text/html, {AP};q=0.1', True),
    (f'{AP};q=0', False),
    (f'text/html, {LD_PLAIN};q=0, {AP};q=0.0', False),
    ('*/*', False),
    ('text/html', False),
    ('', False),
])
def test_is_activitypub_request_ignores_a_q_value_of_zero(app, accept, expected):
    with app.test_request_context('/', headers={'Accept': accept}):
        assert is_activitypub_request() is expected


# ---- served ------------------------------------------------------------------------------------------------------

HARD_CODED_CONTENT_TYPE = re.compile(r"""(content_type\s*=|\[\s*['"]Content-Type['"]\s*\]\s*=)\s*['"]application/activity\+json['"]""")


@pytest.mark.parametrize('line', ["content_type = 'application/activity+json'", 'content_type = "application/activity+json"',
                                  "headers['Content-Type'] = 'application/activity+json'",
                                  'headers["Content-Type"] = "application/activity+json"'])
def test_the_hard_coded_content_type_pattern_catches_either_quote(line):
    assert HARD_CODED_CONTENT_TYPE.search(line)


@pytest.mark.parametrize('module', ['activitypub/routes.py', 'main/routes.py'])
def test_no_activitypub_route_hard_codes_its_content_type(module):
    assert not HARD_CODED_CONTENT_TYPE.search((APP / module).read_text())


def test_a_user_document_asked_for_as_json_ld_is_served_as_json_ld(app, db_session, monkeypatch):
    site, instance = seed_actors()
    make_user(instance, 'alice', local=True)
    _double_the_renderers(monkeypatch)

    ld = profile_get(app, '/u/alice', accept=LD_PLAIN)
    activity = profile_get(app, '/u/alice', accept=AP)

    assert ld.status_code == 200 and ld.content_type == AS2_LD_MEDIA_TYPE
    assert activity.status_code == 200 and activity.content_type == AP
    assert 'Accept' in [v.strip() for v in ld.headers['Vary'].split(',')]


def test_a_head_request_asked_for_as_json_ld_is_answered_as_json_ld(app, db_session, monkeypatch):
    site, instance = seed_actors()
    make_user(instance, 'alice', local=True)
    _double_the_renderers(monkeypatch)

    with app.test_client() as client:
        response = client.head('/u/alice', headers={'Accept': LD_PLAIN})

    assert response.status_code == 200 and response.content_type == AS2_LD_MEDIA_TYPE


def test_a_community_document_asked_for_with_the_profile_is_served_with_it(app, db_session, monkeypatch):
    seed_actors()
    make_community(name='books', host='test.piefed.local')
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/c/books', accept=AS2_LD_MEDIA_TYPE)

    assert response.status_code == 200 and response.content_type == AS2_LD_MEDIA_TYPE


def test_the_instance_actor_and_its_outbox_follow_the_accept_header(app, db_session):
    make_site()
    with app.test_client() as client:
        for path in ('/actor', '/actor/outbox'):
            assert client.get(path, headers={'Accept': LD_PLAIN}).content_type == AS2_LD_MEDIA_TYPE
            assert client.get(path, headers={'Accept': AP}).content_type == AP


def test_the_instance_application_follows_the_accept_header(env):
    assert env.client.get('/', headers={'Accept': LD_PLAIN}).content_type == AS2_LD_MEDIA_TYPE
    assert env.client.get('/', headers={'Accept': AP}).content_type == AP


def test_the_instance_application_is_cached_once_per_content_type(app):
    """@cache.cached keyed on the path alone would hand the first requester's content type to everyone for 6 s."""
    with app.test_request_context('/', headers={'Accept': LD_PLAIN}):
        ld_key = ap_application_cache_key()
    with app.test_request_context('/', headers={'Accept': AP}):
        ap_key = ap_application_cache_key()
    assert ld_key != ap_key
    assert ld_key.endswith(AS2_LD_MEDIA_TYPE) and ap_key.endswith(AP)


def test_an_activitypub_answer_without_its_own_vary_gets_vary_accept(app, db_session):
    """A public Tombstone sets no Vary of its own; its content type now depends on Accept."""
    seed_actors('test.piefed.local')
    user = make_user(None, 'gone', local=True)
    user.deleted = True
    user.banned = True
    db.session.commit()

    with app.test_client() as client:
        response = client.get('/u/gone', headers={'Accept': LD_PLAIN})

    assert response.status_code == 410
    assert response.content_type == AS2_LD_MEDIA_TYPE
    assert 'Accept' in [v.strip() for v in response.headers['Vary'].split(',')]


def test_a_json_ld_answer_does_not_set_a_session_cookie(app, db_session):
    """As tests/test_request_hooks.py pins for activity+json: an external Referer would mark the session modified."""
    user = make_user(make_instance('test.piefed.local', software='piefed'), 'apuser', local=True)

    with app.test_client() as client:
        response = client.get(f'/u/{user.user_name}', headers={'Accept': LD_PLAIN,
                                                               'Referer': 'http://elsewhere.example/page'})

    assert response.content_type == AS2_LD_MEDIA_TYPE
    assert not any('session=' in h for h in response.headers.getlist('Set-Cookie'))


def test_secure_mode_still_recognises_a_json_ld_answer(app, db_session):
    """secure_mode_cache_headers compares response.mimetype, which drops the profile parameter."""
    site = make_site()
    site.require_signed_fetch = True
    db.session.commit()
    with app.test_request_context('/post/1', method='GET', headers={'Accept': 'text/html'}):
        g.site = site
        response = Response('{}', content_type=AS2_LD_MEDIA_TYPE)
        response.cache_control.public = True
        secure_mode_cache_headers(response)

    assert 'private' in response.headers['Cache-Control']
    assert 'Signature' in response.vary


@pytest.mark.parametrize('path', ['/c/books@peer.example', '/f/news@peer.example'])
def test_a_zero_q_activitypub_accept_is_not_refused_as_an_activitypub_request(app, db_session, monkeypatch, path):
    """These two remote-handle refusals tested the Accept header inline, by substring, so q=0 still got 400."""
    seed_actors()
    _double_the_renderers(monkeypatch)

    with app.test_client() as client:
        response = client.get(path, headers={'Accept': f'{AP};q=0, text/html'})

    assert response.status_code == 404


def test_a_zero_q_activitypub_accept_gets_the_front_page(env):
    response = env.client.get('/', headers={'Accept': f'{AP};q=0, text/html'})
    assert response.status_code == 200
    assert response.content_type.startswith('text/html')


# ---- asked for ---------------------------------------------------------------------------------------------------

def test_an_outgoing_actor_fetch_asks_for_both_forms(app, db_session, http_mock):
    route = http_mock.get('https://peer.example/u/alice').respond(200, json={'id': 'https://peer.example/u/alice'})

    assert fetch_remote_actor_data('https://peer.example/u/alice') == {'id': 'https://peer.example/u/alice'}
    assert route.calls.last.request.headers['Accept'] == AP_FETCH_ACCEPT


def test_a_signed_get_asks_for_both_forms_and_signs_the_accept_header(app, db_session, http_mock):
    route = http_mock.get('https://peer.example/u/bob').respond(200, json={'id': 'https://peer.example/u/bob'})

    signed_get_request('https://peer.example/u/bob', a_keypair()[0], 'https://test.piefed.local/actor#main-key')

    sent = route.calls[0].request.headers
    assert sent['accept'] == AP_FETCH_ACCEPT
    assert 'accept' in re.search(r'headers="([^"]*)"', sent['signature']).group(1).split()


def test_a_signed_get_with_the_new_accept_still_verifies_at_the_peer(app, db_session):
    """Production's verifier, as a peer runs it. Accept is a signed header, so the new value is what is signed:
    changing it after signing breaks the signature."""
    make_site()
    signer = make_user(make_instance('peer.example'), 'alice', with_keys=True)
    headers = signed_get_headers('/post/1', signer)

    assert headers['Accept'] == AP_FETCH_ACCEPT
    assert _requester_for(app, '/post/1', headers) == signer
    headers['Accept'] = AP
    assert _requester_for(app, '/post/1', headers) is None
