"""Step 6, Task 2: a podcast episode's number, season and explicit flag (spec §3.4, §10 A17.4).

They live in `Post.extensions['podcast']`, beside the Castopod `credits`, and are set on the audio upload form of a
local podcast community and on the edit form of such an episode. Nobody else's form has them."""
import re

import pytest
from wtforms import Form

from app import db
from app.community.forms import EpisodeFieldsMixin
from app.constants import POST_TYPE_LINK
from app.models import Post
from app.shared.media import episode_form_wanted, episode_of, with_episode
from tests.factories import make_post
from tests.podcast_fixtures import episode_post  # noqa: F401
from tests.test_media_upload import env, fixture_bytes, stored_post, submit, submit_url  # noqa: F401

pytestmark = pytest.mark.usefixtures('site', 'private_static_tree')


def test_with_episode_keeps_credits(episode_post):
    episode_post.extensions = {**episode_post.extensions, 'podcast': {'credits': [{'name': 'Host'}]}}
    with_episode(episode_post, 3, 1, True)
    assert episode_post.extensions['podcast'] == {'credits': [{'name': 'Host'}], 'episode_number': 3, 'season': 1,
                                                  'explicit': True}
    assert episode_post.extensions['media']['status'] == 'ready'   # every other key kept


def test_with_episode_clears_unset_numbers(episode_post):
    with_episode(episode_post, 3, 1, True)
    with_episode(episode_post, None, None, False)
    assert episode_of(episode_post) == {'explicit': False}


def test_with_episode_on_a_post_without_extensions(episode_post):
    episode_post.extensions = None
    with_episode(episode_post, 1, None, False)
    assert episode_post.extensions == {'podcast': {'episode_number': 1, 'explicit': False}}


def test_episode_of_without_podcast_key(episode_post):
    assert episode_of(episode_post) == {}
    episode_post.extensions = {**episode_post.extensions, 'podcast': 'garbage'}
    assert episode_of(episode_post) == {}
    episode_post.extensions = None
    assert episode_of(episode_post) == {}


def test_a_deleted_mixin_field_is_gone_from_the_form_instance(app):
    class F(EpisodeFieldsMixin, Form):
        pass
    form = F()
    assert 'episode_number' in form and form.episode_number is not None
    del form.episode_number, form.season, form.explicit
    # WTForms leaves the attribute behind as None, so `hasattr` cannot tell: the code asks `in form`
    assert 'episode_number' not in form and 'explicit' not in form and form.episode_number is None
    assert 'episode_number' in F()


def test_episode_form_wanted(env):
    assert episode_form_wanted(env.cast) is True
    assert episode_form_wanted(env.talk) is False
    episode = stored_post(env, env.cast, 'audio.mp3', 'audio', POST_TYPE_LINK)
    assert episode_form_wanted(env.cast, episode) is True
    video = make_post(env.chan, env.mod, ap_id='https://test.piefed.local/post/v', title='V')
    assert episode_form_wanted(env.cast, video) is False   # no media: not an audio episode


def test_upload_form_of_a_podcast_shows_episode_fields(env):
    client, _ = env.client_for(env.mod)
    page = client.get(submit_url(env.app, env.cast, 'audio')).data
    assert b'name="episode_number"' in page and b'name="season"' in page and b'name="explicit"' in page


def test_upload_form_without_media_upload_has_no_episode_fields(env):
    client, _ = env.client_for(env.stranger)
    page = client.get(submit_url(env.app, env.cast, 'audio')).data
    assert b'name="episode_number"' not in page


def test_link_form_of_a_podcast_has_no_episode_fields(env):
    client, _ = env.client_for(env.mod)
    page = client.get(submit_url(env.app, env.cast, 'link')).data
    assert b'name="episode_number"' not in page


def test_upload_stores_episode_fields(env):
    response = submit(env, env.mod, env.cast, 'audio', 'episode.mp3', fixture_bytes('audio.mp3'),
                      episode_number='4', season='2', explicit='y')
    assert response.status_code == 302
    post = Post.query.one()
    assert episode_of(post) == {'episode_number': 4, 'season': 2, 'explicit': True}


def test_upload_with_only_explicit_stores_no_numbers(env):
    submit(env, env.mod, env.cast, 'audio', 'episode.mp3', fixture_bytes('audio.mp3'), explicit='y')
    assert episode_of(Post.query.one()) == {'explicit': True}


def test_upload_without_episode_fields_stores_explicit_false(env):
    submit(env, env.mod, env.cast, 'audio', 'episode.mp3', fixture_bytes('audio.mp3'))
    assert episode_of(Post.query.one()) == {'explicit': False}


def test_episode_number_below_one_is_refused(env):
    response = submit(env, env.mod, env.cast, 'audio', 'episode.mp3', fixture_bytes('audio.mp3'),
                      episode_number='0')
    assert response.status_code == 200
    assert Post.query.count() == 0
    assert b'Number must be at least 1' in response.data


def test_edit_form_is_prefilled_and_saves(env, http_mock):
    http_mock.head(re.compile(r'.*\.mp3')).respond(200, headers={'Content-Type': 'audio/mpeg'})
    post = stored_post(env, env.cast, 'audio.mp3', 'audio', POST_TYPE_LINK)
    with_episode(post, 4, 2, True)
    db.session.commit()
    client, token = env.client_for(env.mod)
    page = client.get(f'/post/{post.id}/edit').data
    assert b'name="episode_number"' in page and b'value="4"' in page and b'value="2"' in page

    response = client.post(f'/post/{post.id}/edit', content_type='multipart/form-data', data={
        'title': 'Renamed', 'body': 'about it', 'link_url': post.url, 'language_id': str(env.english.id),
        'timezone': 'Europe/London', 'csrf_token': token, 'episode_number': '5', 'season': '2'})
    assert response.status_code == 302
    db.session.expire_all()
    assert episode_of(db.session.get(Post, post.id)) == {'episode_number': 5, 'season': 2, 'explicit': False}


def test_edit_of_a_non_episode_link_has_no_episode_fields(env, http_mock):
    http_mock.head('https://example.com/page').respond(200, headers={'Content-Type': 'text/html'})
    post = make_post(env.talk, env.mod, ap_id='https://test.piefed.local/post/plain', title='Plain')
    post.type = POST_TYPE_LINK
    post.url = 'https://example.com/page'
    db.session.commit()
    client, token = env.client_for(env.mod)
    assert b'name="episode_number"' not in client.get(f'/post/{post.id}/edit').data

    response = client.post(f'/post/{post.id}/edit', content_type='multipart/form-data', data={
        'title': 'Renamed', 'body': 'about it', 'link_url': 'https://example.com/page',
        'language_id': str(env.english.id), 'timezone': 'Europe/London', 'csrf_token': token,
        'episode_number': '5', 'explicit': 'y'})
    assert response.status_code == 302
    db.session.expire_all()
    assert 'podcast' not in (db.session.get(Post, post.id).extensions or {})


def test_api_edit_keeps_episode_fields(env, monkeypatch, http_mock):
    http_mock.head(re.compile(r'.*\.mp3')).respond(200, headers={'Content-Type': 'audio/mpeg'})
    post = stored_post(env, env.cast, 'audio.mp3', 'audio', POST_TYPE_LINK)
    with_episode(post, 4, 2, True)
    db.session.commit()
    monkeypatch.setitem(env.app.config, 'ENABLE_ALPHA_API', 'true')
    client, _ = env.client_for(env.mod)
    response = client.put('/api/alpha/post', headers={'Authorization': f'Bearer {env.mod.encode_jwt_token()}'},
                          json={'post_id': post.id, 'title': 'Renamed by api'})
    assert response.status_code == 200
    db.session.expire_all()
    post = db.session.get(Post, post.id)
    assert post.title == 'Renamed by api'
    assert episode_of(post) == {'episode_number': 4, 'season': 2, 'explicit': True}
