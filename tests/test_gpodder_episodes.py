"""gpodder sync's episode actions (plan W4c; spec docs/superpowers/specs/2026-11-12-gpodder-sync-design.md §5, ruling
G3): kept per person, the newest of each action on each episode, answered since a time; a play of an episode that is a
post here marks it read for the person, if they may read it."""
import base64
from datetime import timedelta

import pytest
from sqlalchemy import text

import app.gpodder.episodes as episodes_module
from app import db
from app.models import GpodderEpisodeAction, utcnow
from app.oauth_server import tokens
from tests.factories import make_community, make_post, make_user

FEED = 'https://podcasts.example/feed.xml'
MEDIA = 'https://test.piefed.local/static/media/episode-one.mp3'


@pytest.fixture
def world(app, api_baseline):
    alice = make_user(None, 'epalice', local=True)
    author = make_user(None, 'epauthor', local=True)
    shows = make_community('epshows')
    post = make_post(shows, author, 'https://test.piefed.local/post/ep-one', title='Episode one')
    post.url = MEDIA
    secret = make_community('epsecret')
    secret.private = True
    hidden = make_post(secret, author, 'https://test.piefed.local/post/ep-secret', title='Secret episode')
    db.session.commit()
    password = tokens.issue_app_password(alice, 'Phone')
    return {'alice': alice, 'post': post, 'hidden': hidden, 'client': app.test_client(),
            'auth': {'Authorization': 'Basic ' + base64.b64encode(f'epalice:{password}'.encode()).decode()}}


def _action(**fields):
    return {'podcast': FEED, 'episode': 'https://podcasts.example/one.mp3', 'action': 'play',
            'timestamp': '2026-11-12T08:00:00', 'started': 0, 'position': 120, 'total': 1800, **fields}


def _upload(world, actions):
    return world['client'].post('/api/2/episodes/epalice.json', headers=world['auth'], json=actions)


def _pull(world, query='since=0'):
    response = world['client'].get(f'/api/2/episodes/epalice.json?{query}', headers=world['auth'])
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def _read(world, post) -> bool:
    return db.session.execute(text('SELECT 1 FROM read_posts WHERE user_id = :user AND read_post_id = :post'),
                              {'user': world['alice'].id, 'post': post.id}).first() is not None


class TestKept:
    def test_an_action_is_pulled_as_it_was_sent(self, world):
        response = _upload(world, [_action(device='phone', guid='ep-1')])
        assert response.status_code == 200, response.get_data(as_text=True)
        assert isinstance(response.get_json()['timestamp'], int) and response.get_json()['update_urls'] == []
        pulled = _pull(world)
        assert pulled['actions'] == [{'podcast': FEED, 'episode': 'https://podcasts.example/one.mp3', 'guid': 'ep-1',
                                      'action': 'play', 'device': 'phone', 'timestamp': '2026-11-12T08:00:00',
                                      'started': 0, 'position': 120, 'total': 1800}]
        assert isinstance(pulled['timestamp'], int)

    def test_only_the_newest_of_an_action_on_an_episode_is_kept(self, world):
        _upload(world, [_action(position=120)])
        _upload(world, [_action(position=600, timestamp='2026-11-12T09:00:00')])
        _upload(world, [_action(position=300, timestamp='2026-11-12T08:30:00')])   # an older one, sent late
        assert [action['position'] for action in _pull(world)['actions']] == [600]
        assert GpodderEpisodeAction.query.count() == 1

    def test_each_action_on_an_episode_is_kept_apart(self, world):
        _upload(world, [_action(), {'podcast': FEED, 'episode': 'https://podcasts.example/one.mp3',
                                    'action': 'download'}])
        assert sorted(action['action'] for action in _pull(world)['actions']) == ['download', 'play']

    def test_a_pull_answers_what_came_since(self, world):
        _upload(world, [_action()])
        GpodderEpisodeAction.query.update({'received_at': utcnow() - timedelta(hours=1)})
        db.session.commit()
        body = _upload(world, [_action(episode='https://podcasts.example/two.mp3')]).get_json()
        assert [action['episode'] for action in _pull(world, f"since={body['timestamp']}")['actions']] == \
            ['https://podcasts.example/two.mp3']

    def test_a_pull_narrows_to_a_podcast_or_a_device(self, world):
        _upload(world, [_action(device='phone'), _action(podcast='https://other.example/rss', device='laptop',
                                                         episode='https://other.example/a.mp3')])
        assert [a['device'] for a in _pull(world, 'since=0&podcast=https://other.example/rss')['actions']] == ['laptop']
        assert [a['podcast'] for a in _pull(world, 'since=0&device=phone')['actions']] == [FEED]

    def test_aggregated_answers_the_newest_action_of_each_episode(self, world):
        _upload(world, [{'podcast': FEED, 'episode': 'https://podcasts.example/one.mp3', 'action': 'download',
                         'timestamp': '2026-11-12T07:00:00'}, _action()])
        actions = _pull(world, 'since=0&aggregated=true')['actions']
        assert [action['action'] for action in actions] == ['play']

    def test_flattr_is_dropped_and_urls_trimmed(self, world):
        body = _upload(world, [_action(action='flattr'), _action(podcast=f'  {FEED} ')]).get_json()
        assert body['update_urls'] == [[f'  {FEED} ', FEED]]
        assert [action['action'] for action in _pull(world)['actions']] == ['play']

    def test_a_time_with_an_offset_is_kept_in_utc(self, world):
        _upload(world, [_action(timestamp='2026-11-12T10:00:00+02:00'), _action(action='download',
                                                                                 timestamp='2026-11-12T07:00:00Z')])
        times = {action['action']: action['timestamp'] for action in _pull(world)['actions']}
        assert times == {'play': '2026-11-12T08:00:00', 'download': '2026-11-12T07:00:00'}

    def test_an_action_with_no_time_is_stamped_on_arrival(self, world):
        action = _action()
        del action['timestamp']
        _upload(world, [action])
        assert _pull(world)['actions'][0]['timestamp'].startswith(str(utcnow().year))

    @pytest.mark.parametrize('actions', [
        {'not': 'a list'}, ['not an object'], [_action(action='skip')], [_action(podcast='ftp://x.example/f')],
        [_action(episode=5)], [_action(timestamp='yesterday')], [_action(timestamp=5)], [_action(position=-1)], [_action(position='1')],
        [_action(device='bad id')], [_action(guid='g' * 2001)], [_action(episode='https://x.example/' + 'e' * 2000)],
    ], ids=['not-a-list', 'not-an-object', 'unknown-action', 'not-http', 'episode-not-text', 'bad-time', 'time-not-text',
            'negative-position', 'position-not-a-number', 'bad-device', 'long-guid', 'long-url'])
    def test_an_upload_gpodder_would_refuse_is_refused_whole(self, world, actions):
        response = _upload(world, actions)
        assert response.status_code == 400 and GpodderEpisodeAction.query.count() == 0

    def test_an_upload_has_at_most_so_many_actions(self, world, monkeypatch):
        monkeypatch.setattr(episodes_module, 'MAX_ACTIONS', 2)
        assert _upload(world, [_action(), _action(), _action()]).status_code == 400

    def test_a_person_keeps_at_most_so_many_the_oldest_going_first(self, world, monkeypatch):
        monkeypatch.setattr(episodes_module, 'MAX_KEPT', 2)
        for n in range(3):
            _upload(world, [_action(episode=f'https://podcasts.example/{n}.mp3')])
            GpodderEpisodeAction.query.update({'received_at': GpodderEpisodeAction.received_at - timedelta(minutes=1)})
            db.session.commit()
        assert sorted(row.episode for row in GpodderEpisodeAction.query) == ['https://podcasts.example/1.mp3',
                                                                           'https://podcasts.example/2.mp3']

    def test_since_must_be_a_number(self, world):
        assert world['client'].get('/api/2/episodes/epalice.json?since=x', headers=world['auth']).status_code == 400


class TestMarkingRead:
    def test_a_play_of_a_posts_media_marks_it_read(self, world):
        _upload(world, [_action(episode=MEDIA)])
        assert _read(world, world['post'])

    def test_a_play_of_a_posts_guid_marks_it_read(self, world):
        _upload(world, [_action(guid=world['post'].profile_id())])
        assert _read(world, world['post'])

    def test_a_play_of_a_local_posts_address_marks_it_read(self, world):
        _upload(world, [_action(guid=f"https://test.piefed.local/post/{world['post'].id}")])
        assert _read(world, world['post'])

    def test_only_a_play_marks_read(self, world):
        _upload(world, [_action(episode=MEDIA, action='download')])
        assert not _read(world, world['post'])

    def test_a_post_the_person_may_not_read_is_not_marked(self, world):
        _upload(world, [_action(guid=world['hidden'].profile_id())])
        assert not _read(world, world['hidden'])

    def test_an_episode_that_is_no_post_marks_nothing(self, world):
        _upload(world, [_action(guid='https://test.piefed.local/post/999999999')])
        assert db.session.execute(text('SELECT count(*) FROM read_posts')).scalar() == 0
