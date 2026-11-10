"""Who PieFed says it is to Jerboa (plan D3; spec 2026-11-10-lemmy-v3-design §3, ruling L1). Jerboa refuses a server
whose nodeinfo does not say `lemmy`, and picks its models from the version. It names itself on every request, so
nodeinfo says Lemmy 0.19.11 to it and PieFed to everyone else, and one answer is never served from the cache to the
other."""
import pytest

from app import cache

PATHS = ['/nodeinfo/2.0', '/nodeinfo/2.0.json', '/nodeinfo/2.1', '/nodeinfo/2.1.json']


@pytest.fixture
def client(app, api_baseline):
    cache.clear()
    return app.test_client()


def _software(client, path, agent):
    response = client.get(path, headers={'User-Agent': agent} if agent else {})
    assert response.status_code == 200
    assert 'User-Agent' in response.headers.get('Vary', '')
    return response.get_json()['software']


@pytest.mark.parametrize('path', PATHS)
def test_jerboa_is_told_lemmy_and_everyone_else_piefed_whichever_asks_first(client, path):
    software = _software(client, path, 'Jerboa')
    assert (software['name'], software['version']) == ('lemmy', '0.19.11')
    assert _software(client, path, 'okhttp/4.12.0')['name'] == 'piefed'
    assert _software(client, path, None)['name'] == 'piefed'
    assert _software(client, path, 'Jerboa')['name'] == 'lemmy'   # from the cache, still its own


@pytest.mark.parametrize('agent', ['Jerboa/0.0.88', 'Jerboa (Android)'])
def test_a_jerboa_agent_with_more_after_its_name_is_jerboa(client, agent):
    assert _software(client, '/nodeinfo/2.0.json', agent)['name'] == 'lemmy'


@pytest.mark.parametrize('agent', ['NotJerboa', 'Jerboard/1.0', 'Mozilla/5.0 Jerboa'])
def test_only_an_agent_that_begins_with_the_name_is_jerboa(client, agent):
    assert _software(client, '/nodeinfo/2.0.json', agent)['name'] == 'piefed'


def test_the_rest_of_the_document_is_the_same(client):
    jerboa = client.get('/nodeinfo/2.1.json', headers={'User-Agent': 'Jerboa'}).get_json()
    everyone = client.get('/nodeinfo/2.1.json').get_json()
    assert {**jerboa, 'software': None} == {**everyone, 'software': None}
