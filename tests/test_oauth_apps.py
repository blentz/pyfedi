"""POST /api/v1/apps: a client app registers itself, as on Mastodon (plan W0.2 step 2; spec
docs/superpowers/specs/2026-11-07-mastodon-client-api-design.md §3.1)."""
import pytest

from app.models import OAuthApp
from app.oauth_scopes import SCOPES, covers
from app.oauth_server import tokens
from tests.test_login_gate import private  # noqa: F401  the private-instance fixture

TUSKY = 'oauth2redirect://com.keylesspalace.tusky/'
OOB = 'urn:ietf:wg:oauth:2.0:oob'


@pytest.fixture
def client(app, api_baseline):
    return app.test_client()


def test_an_app_registers_with_a_form_as_tusky_does(client):
    response = client.post('/api/v1/apps', data={'client_name': 'Tusky', 'redirect_uris': TUSKY,
                                                 'scopes': 'read write follow push', 'website': 'https://tusky.app'})
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    registered = OAuthApp.query.one()
    assert body['client_id'] == registered.client_id
    assert registered.check_client_secret(body['client_secret'])
    assert body['id'] == str(registered.id)
    assert body['name'] == 'Tusky' and body['website'] == 'https://tusky.app'
    assert body['scopes'] == ['read', 'write', 'follow', 'push']
    assert body['redirect_uri'] == TUSKY and body['redirect_uris'] == [TUSKY]
    assert body['client_secret_expires_at'] == 0


def test_an_app_registers_with_json_and_a_list_of_redirects(client):
    response = client.post('/api/v1/apps', json={'client_name': 'Phanpy', 'scopes': 'read write',
                                                 'redirect_uris': ['https://phanpy.social/', OOB]})
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    assert body['redirect_uris'] == ['https://phanpy.social/', OOB]
    assert body['redirect_uri'] == f'https://phanpy.social/\n{OOB}'
    assert body['website'] is None


def test_redirects_in_one_string_are_split_on_whitespace(client):
    response = client.post('/api/v1/apps', data={'client_name': 'A', 'redirect_uris': f'https://a.example/cb\n{OOB}'})
    assert response.get_json()['redirect_uris'] == ['https://a.example/cb', OOB]


def test_no_scopes_means_read(client):
    response = client.post('/api/v1/apps', data={'client_name': 'A', 'redirect_uris': OOB})
    assert response.get_json()['scopes'] == ['read']
    assert OAuthApp.query.one().scope == 'read'


@pytest.mark.parametrize('fields', [
    {'redirect_uris': OOB},
    {'client_name': '   ', 'redirect_uris': OOB},
    {'client_name': 'A'},
    {'client_name': 'A', 'redirect_uris': ''},
    {'client_name': 'A', 'redirect_uris': 'not-a-uri'},
    {'client_name': 'A', 'redirect_uris': 'javascript:alert(1)'},
    {'client_name': 'A', 'redirect_uris': 'data:text/html,hi'},
    {'client_name': 'A', 'redirect_uris': 'https://a.example/cb#fragment'},
    {'client_name': 'A', 'redirect_uris': 'https:///no-host'},
    {'client_name': 'A', 'redirect_uris': OOB, 'scopes': 'admin:everything'},
    {'client_name': 'A' * 256, 'redirect_uris': OOB},
    {'client_name': 'A', 'redirect_uris': OOB, 'website': 'https://' + 'a' * 1100 + '.example'},
], ids=['no-name', 'blank-name', 'no-redirect', 'empty-redirect', 'relative', 'javascript', 'data', 'fragment',
        'no-host', 'admin-scope', 'long-name', 'long-website'])
def test_a_bad_registration_is_refused_as_mastodon_does(client, fields):
    response = client.post('/api/v1/apps', data=fields)
    assert response.status_code == 422
    assert response.get_json()['error'].startswith('Validation failed')
    assert OAuthApp.query.count() == 0


def test_a_json_list_of_redirects_must_be_strings(client):
    response = client.post('/api/v1/apps', json={'client_name': 'A', 'redirect_uris': [OOB, 5]})
    assert response.status_code == 422


@pytest.mark.parametrize('body', [
    [{'client_name': 'A'}],
    {'client_name': 5, 'redirect_uris': OOB},
    {'client_name': 'A', 'redirect_uris': OOB, 'website': 5},
    {'client_name': 'A', 'redirect_uris': {'uri': OOB}},
    {'client_name': 'A', 'redirect_uris': 'https://a.example/' + 'a' * 2000},
    {'client_name': 'A', 'redirect_uris': 'http://[::1/cb'},
    {'client_name': 'A', 'redirect_uris': 'https://a.example/cb#'},
], ids=['not-an-object', 'number-name', 'number-website', 'redirects-object', 'long-redirect', 'unparseable',
        'empty-fragment'])
def test_a_bad_json_registration_is_refused(client, body):
    response = client.post('/api/v1/apps', json=body)
    assert response.status_code == 422
    assert OAuthApp.query.count() == 0


def test_a_custom_scheme_redirect_is_accepted(client):
    response = client.post('/api/v1/apps', json={'client_name': 'A', 'redirect_uris': 'org.joinmastodon.android://cb'})
    assert response.status_code == 200


@pytest.mark.parametrize('uri', ['https://a.example/cb\njavascript:alert(1)', 'https://a.example/c b',
                                 'https://a.example/cb\t', 'https://a.example/cb\x00', 'https://a.example/cb\x7f'],
                         ids=['newline', 'space', 'tab', 'nul', 'del'])
def test_a_redirect_with_whitespace_or_a_control_character_is_refused(client, uri):
    """Stored one per line: a newline inside one would make a second, unchecked URI."""
    response = client.post('/api/v1/apps', json={'client_name': 'A', 'redirect_uris': [uri]})
    assert response.status_code == 422
    assert OAuthApp.query.count() == 0


@pytest.mark.parametrize('website', ['javascript:alert(1)', 'data:text/html,hi', 'tusky.app', 'https:///no-host',
                                     'ftp://files.example/', 'http://[::1/'],
                         ids=['javascript', 'data', 'no-scheme', 'no-host', 'ftp', 'unparseable'])
def test_a_website_must_be_an_http_url(client, website):
    """The consent page links to it."""
    response = client.post('/api/v1/apps', data={'client_name': 'A', 'redirect_uris': OOB, 'website': website})
    assert response.status_code == 422
    assert OAuthApp.query.count() == 0


def test_an_app_registers_on_a_private_instance_before_anybody_signs_in(app, private):
    """An app registers before it can send anyone to log in, as with Mastodon's own private servers."""
    response = app.test_client().post('/api/v1/apps', data={'client_name': 'Tusky', 'redirect_uris': TUSKY})
    assert response.status_code == 200, response.get_data(as_text=True)


def test_authorizing_on_a_private_instance_still_starts_at_the_login_page(app, private):
    registered, _secret = tokens.register_app('Tusky', [TUSKY], 'read', None)
    response = app.test_client().get('/oauth/authorize', query_string={
        'response_type': 'code', 'client_id': registered.client_id, 'redirect_uri': TUSKY})
    assert response.status_code == 302 and '/auth/login' in response.headers['Location']


def test_an_unknown_scope_is_named_in_the_refusal(client):
    """An app's developer is told which scope PieFed does not know, not just that one is wrong."""
    response = client.post('/api/v1/apps', data={'client_name': 'A', 'redirect_uris': OOB,
                                                 'scopes': 'read write mystery:scope'})
    assert response.status_code == 422
    assert response.get_json()['error'] == 'Validation failed: Scopes are invalid (unknown scope: mystery:scope)'


def test_pixelfeds_security_scopes_are_taken_and_grant_nothing(client):
    """Pixelfed's app (2.0.1) asks security:read and security:write, for screens PieFed serves no route of: they are
    known, so it registers, and cover no scope a route asks (W1.4a)."""
    scopes = 'read write follow push admin:read admin:write security:read security:write'
    response = client.post('/api/v1/apps', data={'client_name': 'Pixelfed for Android', 'redirect_uris': 'pixelfed://login',
                                                 'scopes': scopes})
    assert response.status_code == 200, response.get_json()
    assert 'security:write' in response.get_json()['scopes']
    assert not any(covers('security:read security:write', scope) for scope in SCOPES - {'security:read',
                                                                                         'security:write'})
