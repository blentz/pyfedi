"""Who PieFed says it is to Pixelfed's own app (plan W1.4; spec 2026-11-10-pixelfed-app-design, ruling X1). The app
reads /api/nodeinfo/2.0.json, a path only Pixelfed's clients ask, and refuses any server that is not Pixelfed 0.12.3 or
later; that path alone says Pixelfed 0.15.2, and the standard nodeinfo stays PieFed's."""
import pytest

from app import cache


@pytest.fixture
def client(app, api_baseline):
    cache.clear()
    return app.test_client()


def test_pixelfeds_own_path_says_pixelfed(client):
    response = client.get('/api/nodeinfo/2.0.json')
    assert response.status_code == 200
    body = response.get_json()
    assert (body['software']['name'], body['software']['version']) == ('pixelfed', '0.15.2')
    assert body['version'] == '2.0' and 'usage' in body and body['protocols'] == ['activitypub']


@pytest.mark.parametrize('path', ['/nodeinfo/2.0', '/nodeinfo/2.0.json', '/nodeinfo/2.1.json'])
def test_the_standard_nodeinfo_stays_piefeds_whichever_is_asked_first(client, path):
    client.get('/api/nodeinfo/2.0.json')
    assert client.get(path).get_json()['software']['name'] == 'piefed'


def test_jerboa_on_pixelfeds_path_is_told_pixelfed(client):
    """The path decides: it is Pixelfed's, whoever asks it."""
    body = client.get('/api/nodeinfo/2.0.json', headers={'User-Agent': 'Jerboa'}).get_json()
    assert body['software']['name'] == 'pixelfed'
