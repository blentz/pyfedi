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


PIXELDROID = {'User-Agent': 'PixelDroid'}


@pytest.mark.parametrize('path', ['/nodeinfo/2.0', '/nodeinfo/2.0.json'])
def test_pixeldroid_is_told_pixelfed_with_its_mobile_apis_on(client, path):
    """Ruling S6 (2026-11-11): PixelDroid names itself on every request and warns at login of a server whose nodeinfo
    (the standard one, schema 2.0) does not say pixelfed, and stops at one whose config does not have mobile_apis.
    Its config is Pixelfed's, as /api/v2/config answers it; the repository stays PieFed's, so PixelDroid does not take
    Pixelfed's own avatar upload for ours."""
    body = client.get(path, headers=PIXELDROID).get_json()
    assert (body['software']['name'], body['software']['version']) == ('pixelfed', '0.15.2')
    metadata = body['metadata']
    assert metadata['config']['features']['mobile_apis'] is True
    assert metadata['config']['site']['url'].startswith('http') and metadata['config']['site']['name']
    assert metadata['config']['uploader']['max_caption_length'] == client.get('/api/v2/config').get_json()[
        'uploader']['max_caption_length']
    assert metadata['nodeName'] and 'pixelfed' not in metadata['software']['repo']


def test_pixeldroid_is_told_pixelfed_on_nodeinfo_21_too(client):
    assert client.get('/nodeinfo/2.1', headers=PIXELDROID).get_json()['software']['name'] == 'pixelfed'


@pytest.mark.parametrize('agent', ['PixelDroidX', 'Mozilla/5.0 PixelDroid', 'Tusky/30'])
def test_only_pixeldroid_by_name_is_told_pixelfed(client, agent):
    client.get('/nodeinfo/2.0', headers=PIXELDROID)
    body = client.get('/nodeinfo/2.0', headers={'User-Agent': agent}).get_json()
    assert body['software']['name'] == 'piefed' and 'metadata' not in body


def test_pixeldroids_answer_is_kept_apart_from_everyone_elses(client):
    client.get('/nodeinfo/2.0')
    assert client.get('/nodeinfo/2.0', headers=PIXELDROID).get_json()['software']['name'] == 'pixelfed'
    assert client.get('/nodeinfo/2.0').get_json()['software']['name'] == 'piefed'
    assert 'User-Agent' in client.get('/nodeinfo/2.0', headers=PIXELDROID).headers['Vary']
