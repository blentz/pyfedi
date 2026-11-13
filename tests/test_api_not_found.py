"""A path under an API's prefix that names no route (fuzz-routes finding, 2026-11-13: GET /api/v3/, a non-number in
/api/v1.1/direct/conversations/<int>/leave, DELETE /api/v1/users/me/subscriptions/) is that API's own 404, not the
site's page (ruling FR1): Lemmy's, alpha's, PeerTube's problem+json (whose `error` serves Mastodon's clients on the
/api/v1 they share), Mastodon's for Pixelfed's paths, and OAuth's."""
import pytest

pytestmark = pytest.mark.usefixtures('site')


def get(app, path, method='GET'):
    response = app.test_client().open(path, method=method)
    assert response.status_code == 404, (path, response.status_code)
    return response


def test_lemmys_prefix(app):
    assert get(app, '/api/v3/').get_json() == {'error': 'not_found'}


def test_alphas_prefix(app):
    body = get(app, '/api/alpha/no/such/route').get_json()
    assert body == {'code': 404, 'message': 'not found', 'status': 'Not Found', 'error': 'not_found'}


@pytest.mark.parametrize('path, method', [('/api/v1/users/me/subscriptions/', 'DELETE'), ('/api/v1/no-such', 'GET'),
                                          ('/api/v2/no-such', 'GET')], ids=['peertube-empty-handle', 'v1', 'v2'])
def test_the_prefix_mastodon_and_peertube_share(app, path, method):
    response = get(app, path, method)
    body = response.get_json()
    assert response.mimetype == 'application/problem+json'
    assert body['status'] == 404 and body['code'] == 'not_found' and body['error'] == 'Not found'


@pytest.mark.parametrize('path', ['/api/v1.1/direct/conversations/x/leave', '/api/pixelfed/v1/no-such'],
                         ids=['v1.1', 'pixelfed'])
def test_pixelfeds_prefixes_answer_as_mastodon(app, path):
    assert get(app, path, 'POST' if path.endswith('leave') else 'GET').get_json() == {'error': 'Record not found'}


def test_oauths_prefix(app):
    assert get(app, '/oauth/no-such').get_json() == {'error': 'not_found'}


def test_a_page_is_still_the_sites_page(app):
    assert b'Oops' in get(app, '/no-such-page-here').get_data()
