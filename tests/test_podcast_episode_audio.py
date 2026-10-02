"""C1: a Castopod episode announcement Note gets its audio from the PodcastEpisode object it links to."""
import httpx

from tests.factories import make_community, make_instance, make_site, make_user, peer_actor_json, peer_instance

PEER = 'peer.example'
PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'
EPISODE = f'https://{PEER}/@mypodcast/episodes/ep-1'
AUDIO = f'https://{PEER}/media/ep1.mp3'
COVER = f'https://{PEER}/media/ep1.jpg'


def _episode(**changes):
    document = {'id': EPISODE, 'type': 'PodcastEpisode', 'attributedTo': f'https://{PEER}/@mypodcast',
                'image': {'type': 'Image', 'mediaType': 'image/jpeg', 'url': COVER},
                'audio': {'id': AUDIO, 'type': 'Audio', 'name': 'Episode 1', 'size': 1, 'duration': 3,
                          'url': {'href': AUDIO, 'type': 'Link', 'mediaType': 'audio/mpeg'}}}
    document.update(changes)
    return document


def _ingest(content, note_id=f'https://{PEER}/@mypodcast/posts/1'):
    from app.activitypub.util import actor_json_to_model, create_post
    peer_instance(PEER)
    author = actor_json_to_model(peer_actor_json('Person', name='mypodcast', server=PEER,
                                                 fields={'type': 'Podcast'}), 'mypodcast', PEER)
    community = make_community()
    make_site()
    activity = {'id': f'{note_id}/activity', 'type': 'Create',
                'object': {'id': note_id, 'type': 'Note', 'content': content,
                           'attributedTo': author.ap_profile_id, 'to': [PUBLIC], 'cc': []}}
    return create_post(False, community, activity, author)


ANNOUNCEMENT = f'<a href="{EPISODE}">Episode 1: Hello</a><br/><p>New episode is out!</p>'


def test_an_episode_announcement_gets_its_audio_and_image(db_session, http_mock):
    from app.constants import POST_TYPE_LINK
    route = http_mock.get(EPISODE).respond(200, json=_episode())
    http_mock.get(COVER).respond(404)

    post = _ingest(ANNOUNCEMENT)

    assert route.calls.last.request.headers['Accept'] == 'application/activity+json'
    assert post.type == POST_TYPE_LINK
    assert post.url == AUDIO
    assert post.image.source_url == COVER
    assert 'New episode is out!' in post.body


def _post_page_html(app, post, monkeypatch):
    """The post page's NewReplyForm renders form.csrf_token, which the test config's WTF_CSRF_ENABLED=False removes"""
    from tests.test_visibility_single_object import client_as
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)
    viewer = make_user(make_instance('viewer.example'), 'viewer', local=True)
    return client_as(app, viewer).get(f'/post/{post.id}').get_data(as_text=True)


def test_the_post_page_renders_a_native_audio_player(app, db_session, http_mock, monkeypatch):
    route = http_mock.get(EPISODE).respond(200, json=_episode())
    http_mock.get(COVER).respond(404)
    post = _ingest(ANNOUNCEMENT)

    html = _post_page_html(app, post, monkeypatch)

    assert '<audio controls' in html
    assert f'src="{AUDIO}"' in html
    assert route.called


def test_a_non_mp3_audio_url_still_gets_the_player(app, db_session, http_mock, monkeypatch):
    audio = f'https://{PEER}/media/ep1.m4a'
    episode = _episode()
    episode['audio']['url'] = {'href': audio, 'type': 'Link', 'mediaType': 'audio/mp4'}
    http_mock.get(EPISODE).respond(200, json=episode)
    http_mock.get(COVER).respond(404)
    post = _ingest(ANNOUNCEMENT)

    assert f'src="{audio}"' in _post_page_html(app, post, monkeypatch)


def test_a_failed_fetch_leaves_the_post_as_it_is(db_session, http_mock):
    http_mock.get(EPISODE).respond(404)

    post = _ingest(ANNOUNCEMENT)

    assert post is not None
    assert post.url != AUDIO
    assert post.image is None


def test_an_episode_without_audio_changes_nothing(db_session, http_mock):
    http_mock.get(EPISODE).respond(200, json=_episode(audio=None))

    post = _ingest(ANNOUNCEMENT)

    assert post.url != AUDIO
    assert post.image is None


def test_a_non_audio_media_type_is_refused(db_session, http_mock):
    episode = _episode()
    episode['audio']['url']['mediaType'] = 'text/html'
    http_mock.get(EPISODE).respond(200, json=episode)

    assert _ingest(ANNOUNCEMENT).url != AUDIO


def test_audio_hosted_elsewhere_is_refused(db_session, http_mock):
    episode = _episode()
    episode['audio']['url']['href'] = 'https://elsewhere.example/ep1.mp3'
    http_mock.get(EPISODE).respond(200, json=episode)

    assert _ingest(ANNOUNCEMENT).url != 'https://elsewhere.example/ep1.mp3'


def test_a_transport_error_leaves_the_post_as_it_is(db_session, http_mock, monkeypatch):
    monkeypatch.setattr('app.activitypub.util.time.sleep', lambda seconds: None)
    http_mock.get(EPISODE).mock(side_effect=httpx.ConnectError('down'))

    post = _ingest(ANNOUNCEMENT)

    assert post is not None
    assert post.url != AUDIO


def test_a_link_to_another_host_is_not_an_episode():
    from app.activitypub.util import castopod_episode_url
    note = {'type': 'Note', 'content': '<a href="https://evil.example/@mypodcast/episodes/ep-1">Episode</a>'}

    assert castopod_episode_url(note, f'https://{PEER}/@mypodcast') is None
    assert castopod_episode_url({**note, 'content': ANNOUNCEMENT}, f'https://{PEER}/@mypodcast') == EPISODE
    assert castopod_episode_url({**note, 'content': ANNOUNCEMENT, 'inReplyTo': 'x'}, f'https://{PEER}/@mypodcast') is None


def test_an_ordinary_note_is_never_fetched(db_session, http_mock):
    post = _ingest(f'<p>see <a href="https://{PEER}/about">about</a></p>')

    assert post is not None


def test_an_unauthorised_episode_fetch_is_signed(db_session, http_mock, monkeypatch):
    import app.activitypub.util as util
    http_mock.get(EPISODE).respond(401)
    signed = []

    class Signed:
        status_code = 200

        def json(self):
            return _episode()

        def close(self):
            pass

    monkeypatch.setattr(util, 'signed_get_request', lambda uri, *a, **k: signed.append(uri) or Signed())
    http_mock.get(COVER).respond(404)

    post = _ingest(ANNOUNCEMENT)

    assert signed == [EPISODE]
    assert post.url == AUDIO
