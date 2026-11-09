"""Protocol probes (interop/apps/probes.py) against canned answers, and against this tree's own routes."""
import httpx
import pytest

from interop.apps import probes


def _client(routes):
    """An httpx client whose answers come from `routes`: {(method, path): (status, json)}; anything else is a 404 page."""
    def handle(request):
        status, body = routes.get((request.method, request.url.path), (404, None))
        if body is None:
            return httpx.Response(status, text='<html>not found</html>')
        return httpx.Response(status, json=body)
    return httpx.Client(transport=httpx.MockTransport(handle), base_url='https://piefed.test')


def test_mastodon_without_the_api_is_unsupported_then_blocked():
    result = probes.run('mastodon', _client({}))
    assert result == {
        'login': ('unsupported', 'POST /api/v1/apps answered 404: no app registration, so no OAuth login'),
        'view': ('unsupported', 'GET /api/v1/timelines/public answered 404: no Mastodon timelines'),
        'post': ('blocked', 'POST /api/v1/statuses needs a login'),
    }


def test_mastodon_with_the_api_passes():
    result = probes.run('mastodon', _client({('POST', '/api/v1/apps'): (200, {'client_id': 'c'}),
                                             ('GET', '/api/v1/timelines/public'): (200, [])}))
    assert [outcome for outcome, _ in result.values()] == ['passed', 'passed', 'passed']
    assert result['post'][1] == 'login works: drive the app to check POST /api/v1/statuses'


def test_mastodon_registration_without_a_client_id_is_unsupported():
    result = probes.run('mastodon', _client({('POST', '/api/v1/apps'): (200, {'error': 'x'})}))
    assert result['login'][0] == 'unsupported'


def test_peertube_without_the_api():
    result = probes.run('peertube', _client({}))
    assert result == {
        'login': ('unsupported', 'GET /api/v1/oauth-clients/local answered 404: no OAuth client, so no password-grant login'),
        'view': ('unsupported', 'GET /api/v1/videos answered 404: no PeerTube video API'),
        'post': ('blocked', 'POST /api/v1/videos/upload needs a login'),
    }


def test_peertube_with_the_api_passes():
    result = probes.run('peertube', _client({('GET', '/api/v1/oauth-clients/local'): (200, {'client_id': 'c'}),
                                             ('GET', '/api/v1/videos'): (200, {'data': []})}))
    assert [outcome for outcome, _ in result.values()] == ['passed', 'passed', 'passed']


def test_peertube_video_list_must_have_data():
    result = probes.run('peertube', _client({('GET', '/api/v1/videos'): (200, {'total': 0})}))
    assert result['view'][0] == 'unsupported'


def test_lemmy_v3_without_the_api():
    result = probes.run('lemmy-v3', _client({}), 'alice', 'pw')
    assert result == {
        'login': ('unsupported', 'POST /api/v3/user/login answered 404: no Lemmy v3 API'),
        'view': ('unsupported', 'GET /api/v3/post/list answered 404: no Lemmy v3 API'),
        'post': ('blocked', 'POST /api/v3/post needs a login'),
    }


def test_lemmy_v3_with_the_api_passes():
    result = probes.run('lemmy-v3', _client({('POST', '/api/v3/user/login'): (200, {'jwt': 'j'}),
                                             ('GET', '/api/v3/post/list'): (200, {'posts': []})}), 'alice', 'pw')
    assert [outcome for outcome, _ in result.values()] == ['passed', 'passed', 'passed']


def test_lemmy_v3_list_must_hold_posts():
    result = probes.run('lemmy-v3', _client({('GET', '/api/v3/post/list'): (200, {'error': 'x'})}), 'a', 'p')
    assert result['view'][0] == 'unsupported'


@pytest.fixture
def client(app, site):
    return app.test_client()


@pytest.mark.parametrize('family, expected', [
    ('mastodon', {'login': 'passed', 'view': 'passed', 'post': 'passed'}),   # registration (W1.1), timelines (W1.2)
    ('peertube', {'login': 'unsupported', 'view': 'unsupported', 'post': 'blocked'}),
    ('lemmy-v3', {'login': 'unsupported', 'view': 'unsupported', 'post': 'blocked'}),
], ids=['mastodon', 'peertube', 'lemmy-v3'])
def test_this_tree_serves_what_the_probes_expect(client, family, expected):
    """What the live harness records, checked against the app itself: a probe of this tree's routes. A failure here
    means PieFed gained one of these APIs; update interop/apps/matrix.json with it."""
    class Wsgi(httpx.BaseTransport):
        def handle_request(self, request):
            response = client.open(request.url.raw_path.decode(), method=request.method, data=request.content,
                                   headers=dict(request.headers))
            return httpx.Response(response.status_code, headers=dict(response.headers), content=response.data)
    # This tree's own host: any other is answered 421 (Misdirected Request), which a probe reads as no API at all,
    # so with localhost here this test passed whatever the tree served
    http = httpx.Client(transport=Wsgi(), base_url=f"http://{client.application.config['SERVER_NAME']}")
    result = probes.run(family, http, 'nobody', 'nothing')
    assert all(' answered 421' not in note for _, note in result.values()), result
    assert {step: outcome for step, (outcome, _) in result.items()} == expected


def test_mastodon_on_a_private_instance_is_served_behind_login():
    """The harness's PieFed is private: an anonymous timeline is refused with Mastodon's 401, which shows it is served."""
    result = probes.run('mastodon', _client({('POST', '/api/v1/apps'): (200, {'client_id': 'c'}),
                                             ('GET', '/api/v1/timelines/public'):
                                                 (401, {'error': 'The access token is invalid'})}))
    assert result['view'][0] == 'passed' and 'login refusal' in result['view'][1]


def test_mastodon_a_401_that_is_not_mastodons_is_still_unsupported():
    result = probes.run('mastodon', _client({('POST', '/api/v1/apps'): (200, {'client_id': 'c'}),
                                             ('GET', '/api/v1/timelines/public'): (401, ['not', 'an', 'error'])}))
    assert result['view'][0] == 'unsupported'
