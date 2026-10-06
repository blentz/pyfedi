"""Interop D24: an opt-in search of PeerTube videos across the network, through SepiaSearch."""
import pytest
from cachelib import SimpleCache

from app import cache
from app.discovery import external_search, sources
from app.discovery.external_search import search_videos, video_from
from tests.discovery_fixtures import fresh_cache  # noqa: F401
from tests.factories import make_banned_instance, make_community, make_instance, make_post, make_user

pytestmark = pytest.mark.usefixtures('site', 'fresh_cache')


@pytest.fixture(autouse=True)
def real_cache(app, monkeypatch):
    """The test config's NullCache would make the cache-hit test fail."""
    monkeypatch.setitem(app.extensions['cache'], cache, SimpleCache())


@pytest.fixture(autouse=True)
def founder(db_session):
    make_user(make_instance('test.piefed.local', software='piefed'), 'founder', local=True)


def raw(n=1, host='tube.example', nsfw=False, **over):
    video = {'url': f'https://{host}/videos/watch/{n}', 'name': f'Video {n}', 'nsfw': nsfw,
             'channel': {'displayName': 'Chan', 'host': host}}
    video.update(over)
    return video


class Answers(list):
    """The calls made to the fake fetch_json; `payload` is what it answers with."""
    payload = None


@pytest.fixture
def answers(monkeypatch):
    calls = Answers()

    def fetch(url, params=None, headers=None, max_bytes=None, max_seconds=15):
        calls.append(dict(url=url, params=params, max_seconds=max_seconds))
        return calls.payload
    calls.payload = {'data': [raw(1), raw(2)]}
    monkeypatch.setattr(external_search.sources, 'fetch_json', fetch)
    return calls


def test_a_search_asks_sepiasearch_with_a_3_second_cap(db_session, answers):
    videos = search_videos('  Cats ', allow_nsfw=False)

    assert [v['title'] for v in videos] == ['Video 1', 'Video 2']
    assert answers[0]['url'] == 'https://sepiasearch.org/api/v1/search/videos'
    assert answers[0]['params'] == {'search': 'Cats', 'count': 10} and answers[0]['max_seconds'] == 3


def test_the_same_query_differently_spaced_or_cased_is_served_from_cache(db_session, answers):
    search_videos('cats', allow_nsfw=False)
    search_videos(' CATS ', allow_nsfw=False)
    assert len(answers) == 1


def test_a_failure_is_not_cached_and_gives_nothing(db_session, answers):
    answers.payload = None
    assert search_videos('cats', allow_nsfw=False) == []
    answers.payload = {'data': [raw(1)]}
    assert len(search_videos('cats', allow_nsfw=False)) == 1


@pytest.mark.parametrize('payload', [None, [], {'data': 'x'}, {'nodata': []}])
def test_unreadable_payloads_give_nothing(db_session, answers, payload):
    answers.payload = payload
    assert search_videos('cats', allow_nsfw=False) == []


def test_an_empty_query_asks_nothing(db_session, answers):
    assert search_videos('   ', allow_nsfw=False) == [] and answers == []


def test_nsfw_needs_permission(db_session, answers):
    answers.payload = {'data': [raw(1, nsfw=True), raw(2)]}
    assert [v['title'] for v in search_videos('x', allow_nsfw=False)] == ['Video 2']
    assert [v['title'] for v in search_videos('x', allow_nsfw=True)] == ['Video 1', 'Video 2']


def test_banned_hosts_are_dropped(db_session, answers):
    answers.payload = {'data': [raw(1, host='bad.example'), raw(2)]}
    make_banned_instance('bad.example')
    assert [v['title'] for v in search_videos('x', allow_nsfw=False)] == ['Video 2']


def test_videos_already_stored_here_are_dropped(db_session, answers):
    instance = make_instance('tube.example', software='peertube')
    community = make_community('chan', host='tube.example')
    make_post(community, make_user(instance, 'author'), 'https://tube.example/videos/watch/1')
    assert [v['title'] for v in search_videos('x', allow_nsfw=False)] == ['Video 2']


def test_at_most_ten(db_session, answers):
    answers.payload = {'data': [raw(i) for i in range(15)]}
    assert len(search_videos('x', allow_nsfw=False)) == 10


@pytest.mark.parametrize('bad', [
    'not a dict',
    raw(url='http://tube.example/videos/watch/1'),              # not https
    raw(url='https://elsewhere.example/videos/watch/1'),        # Review Focus 5: url host != channel host
    raw(channel={'host': 'Not A Host'}),
    raw(channel='x'),
    raw(url=5),
    raw(name=''),
    raw(name=None),
    raw(url='https://[::1/x'),
    raw(url='https://evil.example\\@tube.example/x'),
    raw(url='https://user@tube.example/x'),
    raw(url='https://tube.example:8443/x'),
    raw(url='https://tube.example:evil/x'),
    raw(url=' https://tube.example/x'),
    raw(url='\x00https://tube.example/x'),
    raw(url='https://tube.example/x\n'),
    raw(url='https://evil.example\t@tube.example/x')])
def test_malformed_rows_are_dropped(bad):
    assert video_from(bad) is None


def test_a_good_row(db_session):
    assert video_from(raw(3, nsfw='yes')) == {'url': 'https://tube.example/videos/watch/3', 'title': 'Video 3',
                                             'channel': 'Chan', 'host': 'tube.example', 'nsfw': False}


def test_a_row_with_no_channel_name_uses_the_host(db_session):
    assert video_from(raw(channel={'host': 'tube.example'}))['channel'] == 'tube.example'


def test_fetch_json_passes_max_seconds_to_the_capped_get(monkeypatch, app):
    seen = {}
    monkeypatch.setattr(sources, 'get_request_capped',
                        lambda url, max_bytes, headers=None, max_seconds=15: seen.update(s=max_seconds) or (200, b'{}'))
    with app.app_context():
        assert sources.fetch_json('https://x.example/', max_seconds=3) == {}
    assert seen['s'] == 3


def test_a_payload_with_no_data_list_is_logged_without_the_query(db_session, answers, caplog):
    answers.payload = {'nodata': []}
    with caplog.at_level('INFO'):
        assert search_videos('secretquery', allow_nsfw=False) == []
    assert 'sepiasearch video search answered with no data list' in caplog.text
    assert 'secretquery' not in caplog.text
