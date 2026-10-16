"""Step 3, Task 4: the upload path, the processing state and its gating (spec A13).

A video or audio file is taken only from a moderator of a `video_channel` or `podcast`
community whom the upload switch admits (A13.6). It is sniffed, stored under
`app/static/media/posts/`, served from `Post.url` with no `File` row (A13.4), and the
post waits in `POST_STATUS_PROCESSING` (A13.1) until `probe_media` (Task 5) finishes.
Every unpublished-post gate then hides it.

`queue_probe` is patched in every test that stores media but one, the end-to-end test that
runs the real probe_media eagerly. Every test that stores media removes the files (the static leak gate).
"""
import io
import os
import re
import uuid
from types import SimpleNamespace

import pytest
from flask import g, request, session as flask_session, url_for
from flask.testing import EnvironBuilder
from flask_wtf.csrf import generate_csrf
from werkzeug.datastructures import FileStorage

from app import db
from app.constants import (COMMUNITY_KIND_DISCUSSION, COMMUNITY_KIND_PODCAST, COMMUNITY_KIND_VIDEO_CHANNEL,
                           POST_STATUS_PROCESSING, POST_STATUS_PUBLISHED, POST_TYPE_IMAGE, POST_TYPE_LINK,
                           POST_TYPE_VIDEO)
from app.models import Language, Post, Site
from app.shared import media as media_module
from app.shared.media import media_local_path, media_of, queue_probe, store_uploaded_media, with_media
from app.shared.post import media_upload_kind
from app.shared.tasks.pages import send_post
from app.utils import set_setting
from tests.factories import make_community, make_community_member, make_instance, make_post, make_user

pytestmark = pytest.mark.usefixtures('site', 'private_static_tree')

FIX = os.path.join(os.path.dirname(__file__), 'fixtures', 'media')
BIG = 1024 * 1024 + 64 * 1024   # past the 1 MiB of form slack the request cap allows


def fixture_bytes(name):
    with open(os.path.join(FIX, name), 'rb') as f:
        return f.read()


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def csrf(app, client):
    with app.test_request_context():
        g.pop('csrf_token', None)   # the app context, and with it generate_csrf's cache, outlives one client
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as session:
        session['csrf_token'] = raw
    return token


def stored_files():
    """Every file under the media tree; private_static_tree makes it this test's own, so the walk is cheap."""
    return [os.path.join(d, f) for d, _, files in os.walk('app/static/media/posts') for f in files]


def remove_stored_media():
    for post in Post.query.all():
        path = media_local_path(post)
        if path and os.path.exists(path):
            os.unlink(path)


@pytest.fixture
def env(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # the templates render form.csrf_token()
    site = db.session.get(Site, 1)
    site.private_instance = False
    local = make_instance('test.piefed.local', software='piefed')
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1   # user 1 is the admin
    mod = make_user(local, 'mod', local=True)
    stranger = make_user(local, 'stranger', local=True)
    for user in (mod, stranger):
        user.verified = True
        user.private_key = 'a private key'   # validation_required / approval_required
    english = Language(code='en', name='English')
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.add(english)
    chan, cast, talk = make_community('chan'), make_community('cast'), make_community('talk')
    chan.kind, cast.kind, talk.kind = COMMUNITY_KIND_VIDEO_CHANNEL, COMMUNITY_KIND_PODCAST, COMMUNITY_KIND_DISCUSSION
    db.session.commit()
    for community in (chan, cast, talk):
        make_community_member(mod, community, is_moderator=True)
        make_community_member(stranger, community)
    set_setting('allow_video_file_uploads', 'users')
    set_setting('allow_audio_file_uploads', 'users')
    db.session.commit()

    probes = []
    monkeypatch.setattr('app.shared.media.queue_probe', probes.append)
    monkeypatch.setattr('app.shared.post.queue_probe', probes.append)

    def client_for(user):
        # The test's app context outlives a request, and with it flask_login's cached `g._login_user`: drop it, or
        # every request is answered for whoever asked first
        g.pop('_login_user', None)
        client = app.test_client()
        login(client, user)
        return client, csrf(app, client)

    yield SimpleNamespace(app=app, local=local, founder=founder, mod=mod, stranger=stranger, english=english,
                          chan=chan, cast=cast, talk=talk, probes=probes, client_for=client_for)
    db.session.rollback()
    remove_stored_media()


def submit_url(app, community, kind):
    with app.test_request_context():
        return url_for('community.add_post', actor=community.name, type=kind)


def form_data(env, token, community, filename=None, payload=b'', **fields):
    data = {'title': 'A clip', 'body': 'about it', 'communities': str(community.id),
            'language_id': str(env.english.id), 'notify_author': 'y', 'timezone': 'Europe/London',
            'submit': 'Publish', 'csrf_token': token}
    if filename is not None:
        data['image_file'] = (io.BytesIO(payload), filename)
    data.update(fields)
    return data


def submit(env, user, community, kind, filename=None, payload=b'', **fields):
    client, token = env.client_for(user)
    return client.post(submit_url(env.app, community, kind),
                       data=form_data(env, token, community, filename, payload, **fields),
                       content_type='multipart/form-data')


def stored_post(env, community, fixture='video_h264.mp4', kind='video', post_type=POST_TYPE_VIDEO, title='Processing'):
    post = make_post(community, env.mod, ap_id=f'https://test.piefed.local/post/{title}', title=title)
    post.type = post_type
    upload = FileStorage(stream=io.BytesIO(fixture_bytes(fixture)), filename='upload.bin')
    assert store_uploaded_media(post, upload, kind) is None
    db.session.commit()
    return post


# ---------------------------------------------------------------- 1, 2: a stored upload


def test_a_video_upload_is_stored_and_waits_in_processing(env):
    data = fixture_bytes('video_h264.mp4')
    response = submit(env, env.mod, env.chan, 'video', 'clip.mp4', data)

    post = Post.query.one()
    assert response.status_code == 302
    assert response.headers['Location'].endswith(post.slug)
    assert post.status == POST_STATUS_PROCESSING
    assert post.type == POST_TYPE_VIDEO
    media = media_of(post)
    assert media['status'] == 'processing'
    assert uuid.UUID(media['uuid']).version == 4
    assert media['mime'] == 'video/mp4' and media['bytes'] == len(data)
    assert post.url.startswith(env.app.config['SERVER_URL'] + '/static/media/posts/')
    assert post.url.endswith('.mp4') and post.image_id is None
    with open(media_local_path(post), 'rb') as f:
        assert f.read() == data
    assert env.probes == [post.id]


def test_an_audio_upload_is_a_link_post_with_the_audio_media_type(env):
    data = fixture_bytes('audio.mp3')
    response = submit(env, env.mod, env.cast, 'audio', 'episode.mp3', data)

    post = Post.query.one()
    assert response.status_code == 302
    assert post.type == POST_TYPE_LINK and post.status == POST_STATUS_PROCESSING
    assert post.extensions['media_type'] == 'audio'
    assert media_of(post)['mime'] == 'audio/mpeg'
    assert post.url.endswith('.mp3')
    with open(media_local_path(post), 'rb') as f:
        assert f.read() == data
    assert env.probes == [post.id]


def test_the_upload_field_is_offered_to_a_permitted_moderator(env):
    client, _ = env.client_for(env.mod)
    assert b'name="image_file"' in client.get(submit_url(env.app, env.chan, 'video')).data
    page = client.get(submit_url(env.app, env.cast, 'audio')).data
    assert b'name="image_file"' in page and b'name="audio_url"' in page


# ---------------------------------------------------------------- 3, 4: the bytes decide


def test_a_file_that_is_not_media_is_refused_with_a_form_error(env):
    response = submit(env, env.mod, env.chan, 'video', 'clip.mp4', fixture_bytes('not_media.mp4'))

    assert response.status_code == 200
    assert b'not a supported video file' in response.data
    assert Post.query.count() == 0
    assert stored_files() == []
    assert env.probes == []


def test_an_mp3_sent_to_the_video_form_is_refused(env):
    response = submit(env, env.mod, env.chan, 'video', 'clip.mp4', fixture_bytes('audio.mp3'))

    assert response.status_code == 200
    assert b'not a supported video file' in response.data
    assert Post.query.count() == 0
    assert stored_files() == []


# ---------------------------------------------------------------- 5, 6, 7: the size limit


def test_a_file_over_the_limit_is_413(env):
    set_setting('max_video_upload_bytes', 1000)
    response = submit(env, env.mod, env.chan, 'video', 'clip.mp4', fixture_bytes('video_h264.mp4'))

    assert response.status_code == 413
    assert Post.query.count() == 0
    assert stored_files() == []


def oversized_video():
    return fixture_bytes('video_h264.mp4')[:64] + b'\0' * BIG


def test_a_body_past_the_cap_is_413_before_the_view_reads_it(env, monkeypatch):
    """The honest Content-Length is over the cap: Werkzeug refuses it before reading. The route's own exact
    check is disabled, so the 413 can only come from the request cap."""
    set_setting('max_video_upload_bytes', 1000)
    monkeypatch.setattr('app.community.routes.upload_byte_limit', lambda kind: 10 ** 12)
    response = submit(env, env.mod, env.chan, 'video', 'clip.mp4', oversized_video())

    assert response.status_code == 413
    assert Post.query.count() == 0
    assert stored_files() == []


class ChunkedBuilder(EnvironBuilder):
    """A request with no Content-Length whose server terminates the stream, as for chunked encoding."""

    def get_environ(self):
        environ = super().get_environ()
        del environ['CONTENT_LENGTH']
        environ['wsgi.input_terminated'] = True
        return environ


def test_a_chunked_body_past_the_cap_is_413(env, monkeypatch):
    """No Content-Length (chunked, or a lying client): the cap stops the stream itself."""
    set_setting('max_video_upload_bytes', 1000)
    monkeypatch.setattr('app.community.routes.upload_byte_limit', lambda kind: 10 ** 12)
    client, token = env.client_for(env.mod)
    builder = ChunkedBuilder(env.app, path=submit_url(env.app, env.chan, 'video'), method='POST',
                             data=form_data(env, token, env.chan, 'clip.mp4', oversized_video()))

    response = client.open(builder)

    assert response.status_code == 413
    assert Post.query.count() == 0
    assert stored_files() == []


def test_a_file_exactly_at_the_limit_is_accepted_and_one_byte_more_is_413(env):
    data = fixture_bytes('video_h264.mp4')
    set_setting('max_video_upload_bytes', len(data) - 1)
    assert submit(env, env.mod, env.chan, 'video', 'clip.mp4', data).status_code == 413
    assert Post.query.count() == 0
    assert stored_files() == []

    set_setting('max_video_upload_bytes', len(data))
    assert submit(env, env.mod, env.chan, 'video', 'clip.mp4', data).status_code == 302
    assert media_of(Post.query.one())['bytes'] == len(data)


def test_the_cap_applies_only_to_media_submissions(env):
    """The hook leaves every other request on the global limit."""
    with env.app.test_request_context(submit_url(env.app, env.chan, 'image'), method='POST'):
        env.app.preprocess_request()
        assert request.max_content_length == env.app.config['MAX_CONTENT_LENGTH']
    with env.app.test_request_context(submit_url(env.app, env.chan, 'video'), method='GET'):
        env.app.preprocess_request()
        assert request.max_content_length == env.app.config['MAX_CONTENT_LENGTH']
    with env.app.test_request_context('/auth/login', method='POST'):
        env.app.preprocess_request()
        assert request.max_content_length == env.app.config['MAX_CONTENT_LENGTH']
    with env.app.test_request_context('/community/nosuch/submit/video', method='POST'):
        env.app.preprocess_request()   # an unknown community keeps the global cap
        assert request.max_content_length == env.app.config['MAX_CONTENT_LENGTH']
    g.pop('_login_user', None)
    with env.app.test_request_context(submit_url(env.app, env.cast, 'audio'), method='POST'):
        env.app.preprocess_request()   # anonymous: never above the global cap
        assert request.max_content_length == min(env.app.config['MAX_CONTENT_LENGTH'], 209715200 + 1024 * 1024)


SMALL_GLOBAL = 64 * 1024   # the global cap, made small so a body over it stays cheap


def padded_video(size=SMALL_GLOBAL + 32 * 1024):
    return fixture_bytes('video_h264.mp4')[:64] + b'\0' * size


def test_the_cap_is_raised_only_for_a_permitted_moderator(env, monkeypatch):
    """Finding 1 (security): the media cap is above the global one, so it is given only to a user who may upload
    media to the community the URL names. Everyone else keeps the global cap."""
    monkeypatch.setitem(env.app.config, 'MAX_CONTENT_LENGTH', SMALL_GLOBAL)

    response = submit(env, env.stranger, env.chan, 'video', 'clip.mp4', padded_video())
    assert response.status_code == 413   # a member, not a moderator

    response = submit(env, env.mod, env.talk, 'audio', 'episode.mp3', padded_video())
    assert response.status_code == 413   # a moderator, of a discussion community

    set_setting('allow_video_file_uploads', 'no')
    response = submit(env, env.mod, env.chan, 'video', 'clip.mp4', padded_video())
    assert response.status_code == 413   # a moderator the switch does not admit
    set_setting('allow_video_file_uploads', 'users')
    assert Post.query.count() == 0 and stored_files() == []

    response = submit(env, env.mod, env.chan, 'video', 'clip.mp4', padded_video())
    assert response.status_code == 302   # the permitted moderator still gets the raised cap
    assert media_of(Post.query.one())['bytes'] == SMALL_GLOBAL + 32 * 1024 + 64


def test_an_anonymous_upload_is_stopped_as_before(env, monkeypatch):
    monkeypatch.setitem(env.app.config, 'MAX_CONTENT_LENGTH', SMALL_GLOBAL)
    g.pop('_login_user', None)
    client = env.app.test_client()
    response = client.post(submit_url(env.app, env.chan, 'video'), content_type='multipart/form-data',
                           data={'title': 'x', 'image_file': (io.BytesIO(padded_video()), 'clip.mp4')})
    assert response.status_code in (302, 413)   # login_required, or the global cap: never a stored upload
    assert Post.query.count() == 0 and stored_files() == []


# ---------------------------------------------------------------- 8, 9, 10: who may upload


@pytest.fixture
def linked_video(http_mock):
    link = 'https://example.com/clip.mp4'
    http_mock.head(link).respond(200, headers={'Content-Type': 'video/mp4'})
    http_mock.get(link).respond(200, html='')
    return link


def assert_ignored_upload(env, response, link):
    post = Post.query.one()
    assert response.status_code == 302
    assert media_of(post) is None and post.status == POST_STATUS_PUBLISHED
    assert post.url == link
    assert env.probes == []
    assert stored_files() == []


def test_a_member_who_is_not_a_moderator_cannot_upload(env, linked_video):
    client, _ = env.client_for(env.stranger)
    assert b'name="image_file"' not in client.get(submit_url(env.app, env.chan, 'video')).data

    response = submit(env, env.stranger, env.chan, 'video', 'clip.mp4', fixture_bytes('video_h264.mp4'),
                      video_url=linked_video)
    assert_ignored_upload(env, response, linked_video)


def test_a_moderator_the_switch_does_not_admit_cannot_upload(env, linked_video):
    set_setting('allow_video_file_uploads', 'no')
    client, _ = env.client_for(env.mod)
    assert b'name="image_file"' not in client.get(submit_url(env.app, env.chan, 'video')).data

    response = submit(env, env.mod, env.chan, 'video', 'clip.mp4', fixture_bytes('video_h264.mp4'),
                      video_url=linked_video)
    assert_ignored_upload(env, response, linked_video)


def test_a_discussion_community_takes_no_upload(env, linked_video):
    client, _ = env.client_for(env.mod)
    assert b'name="image_file"' not in client.get(submit_url(env.app, env.talk, 'video')).data

    response = submit(env, env.mod, env.talk, 'video', 'clip.mp4', fixture_bytes('video_h264.mp4'),
                      video_url=linked_video)
    assert_ignored_upload(env, response, linked_video)


def test_an_ignored_upload_without_a_link_is_the_forms_ordinary_error(env):
    response = submit(env, env.stranger, env.chan, 'video', 'clip.mp4', fixture_bytes('video_h264.mp4'))
    assert response.status_code == 200
    assert b'Give a URL or upload a file.' in response.data
    assert Post.query.count() == 0
    assert stored_files() == []

    response = submit(env, env.mod, env.cast, 'audio')
    assert response.status_code == 200
    assert b'Give a URL or upload a file.' in response.data
    assert Post.query.count() == 0
    assert stored_files() == []


def test_an_audio_link_is_an_ordinary_link_post(env, http_mock):
    link = 'https://example.com/episode.mp3'
    http_mock.head(link).respond(200, headers={'Content-Type': 'audio/mpeg'})
    http_mock.get(link).respond(200, html='')
    response = submit(env, env.stranger, env.cast, 'audio', audio_url=link)

    post = Post.query.one()
    assert response.status_code == 302
    assert post.type == POST_TYPE_LINK and post.url == link and media_of(post) is None


# ---------------------------------------------------------------- 11: no replacement on edit


def ready_post(env):
    post = stored_post(env, env.chan)
    post.status = POST_STATUS_PUBLISHED
    with_media(post, status='ready')
    db.session.commit()
    return post


def edit(env, post, filename=None, payload=b'', **fields):
    client, token = env.client_for(env.mod)
    data = {'title': 'Renamed', 'body': 'about it', 'video_url': post.url, 'language_id': str(env.english.id),
            'notify_author': 'y', 'timezone': 'Europe/London', 'submit': 'Publish', 'csrf_token': token}
    if filename is not None:
        data['image_file'] = (io.BytesIO(payload), filename)
    data.update(fields)
    return client.post(f'/post/{post.id}/edit', data=data, content_type='multipart/form-data')


def test_editing_a_media_post_cannot_replace_its_file(env):
    post = ready_post(env)
    url, record = post.url, dict(media_of(post))
    with open(media_local_path(post), 'rb') as f:
        before = f.read()

    client, _ = env.client_for(env.mod)
    assert b'name="image_file"' not in client.get(f'/post/{post.id}/edit').data
    response = edit(env, post, 'other.mp4', fixture_bytes('video_vp9.webm'))

    db.session.expire_all()
    post = db.session.get(Post, post.id)
    assert response.status_code == 200
    assert b'cannot be replaced' in response.data
    assert post.url == url and media_of(post) == record and post.title == 'Processing'
    with open(media_local_path(post), 'rb') as f:
        assert f.read() == before


def test_a_url_edit_of_a_media_post_is_refused_out_loud(env):
    """Finding 3: a changed URL used to be dropped silently under a success flash. The field is read-only on the
    form, and a changed URL is refused with a message; nothing is saved."""
    post = ready_post(env)
    url, record = post.url, dict(media_of(post))

    client, _ = env.client_for(env.mod)
    page = client.get(f'/post/{post.id}/edit').data
    assert re.search(rb'<input[^>]*name="video_url"[^>]*readonly', page)

    response = edit(env, post, video_url='https://example.com/other.mp4')

    db.session.expire_all()
    post = db.session.get(Post, post.id)
    assert response.status_code == 200
    assert b'The media of this post cannot be changed.' in response.data
    assert post.title == 'Processing' and post.url == url and media_of(post) == record


def test_an_unchanged_url_edit_of_a_media_post_is_saved(env):
    post = ready_post(env)
    url, record = post.url, dict(media_of(post))

    response = edit(env, post)

    db.session.expire_all()
    post = db.session.get(Post, post.id)
    assert response.status_code == 302
    assert post.title == 'Renamed' and post.url == url and media_of(post) == record


def test_a_failure_after_the_store_leaves_no_file(env, monkeypatch):
    """Finding 4: when anything raises after store_uploaded_media wrote the file, make_post removes the post and
    the file with it."""
    def fail(post_id):
        raise RuntimeError('probe queue down')
    monkeypatch.setattr('app.shared.post.queue_probe', fail)

    response = submit(env, env.mod, env.chan, 'video', 'clip.mp4', fixture_bytes('video_h264.mp4'))

    assert response.status_code == 302 and '/submit' not in response.headers['Location']
    assert Post.query.count() == 0
    assert stored_files() == []


def test_an_audio_media_post_edit_cannot_replace_its_file_either(env):
    post = stored_post(env, env.cast, 'audio.mp3', 'audio', POST_TYPE_LINK)
    url = post.url
    client, token = env.client_for(env.mod)
    response = client.post(f'/post/{post.id}/edit', content_type='multipart/form-data', data={
        'title': 'Renamed', 'body': 'about it', 'link_url': url, 'language_id': str(env.english.id),
        'timezone': 'Europe/London', 'csrf_token': token, 'image_file': (io.BytesIO(b'x'), 'a.mp3')})

    db.session.expire_all()
    assert response.status_code == 200 and b'cannot be replaced' in response.data
    assert db.session.get(Post, post.id).url == url


# ---------------------------------------------------------------- 12: a processing post is hidden


def page_for(env, user, path):
    if user is None:
        g.pop('_login_user', None)   # see client_for
        return env.app.test_client().get(path)
    return env.client_for(user)[0].get(path)


def test_a_processing_post_is_seen_only_by_its_author_moderators_and_admins(env):
    post = stored_post(env, env.chan)
    other_mod = make_user(env.local, 'othermod', local=True)
    make_community_member(other_mod, env.chan, is_moderator=True)
    db.session.commit()
    path = f'/post/{post.id}'

    assert page_for(env, None, path).status_code == 404
    assert page_for(env, env.stranger, path).status_code == 404
    page = page_for(env, env.mod, path)   # the author, who is also the community's moderator
    assert page.status_code == 200 and b'is still being processed' in page.data
    assert page_for(env, env.founder, path).status_code == 200
    assert page_for(env, other_mod, path).status_code == 200


def test_a_post_being_probed_shows_the_processing_notice(env):
    post = stored_post(env, env.chan)
    with_media(post, status='probing')   # probe_media has claimed it and is working on it
    db.session.commit()

    author, _ = env.client_for(env.mod)
    assert b'is still being processed' in author.get(f'/post/{post.id}').data


def test_a_failed_post_shows_its_reason(env):
    post = stored_post(env, env.chan)
    with_media(post, status='failed', error='The video codec is not supported.')
    db.session.commit()

    author, _ = env.client_for(env.mod)
    assert b'The video codec is not supported.' in author.get(f'/post/{post.id}').data


def test_a_processing_post_is_not_listed_anywhere(env, monkeypatch):
    post = stored_post(env, env.chan, title='HIDDENCLIP')
    shown = make_post(env.chan, env.mod, ap_id='https://test.piefed.local/post/shown', title='SHOWNCLIP')
    db.session.commit()
    monkeypatch.setitem(env.app.config, 'ENABLE_ALPHA_API', 'true')
    author, _ = env.client_for(env.mod)

    community_page = author.get('/c/chan').data
    assert b'SHOWNCLIP' in community_page and b'HIDDENCLIP' not in community_page

    titles = [p['post']['title'] for p in author.get('/api/alpha/post/list',
                                                     query_string={'community_id': env.chan.id}).get_json()['posts']]
    assert 'SHOWNCLIP' in titles and 'HIDDENCLIP' not in titles

    home = author.get('/', query_string={'sort': 'new'}).data
    assert b'SHOWNCLIP' in home and b'HIDDENCLIP' not in home
    assert post.status == POST_STATUS_PROCESSING


def test_the_activitypub_representation_of_a_processing_post_is_403(env):
    post = stored_post(env, env.chan)
    response = env.app.test_client().get(f'/post/{post.id}', headers={'Accept': 'application/activity+json'})
    assert response.status_code == 403


def test_send_post_delivers_nothing_until_the_media_is_ready(env, monkeypatch):
    post = stored_post(env, env.chan)
    env.chan.ap_profile_id = env.chan.ap_public_url = 'https://peer.example/c/chan'
    env.chan.ap_inbox_url = 'https://peer.example/inbox'
    env.chan.ap_id = 'chan@peer.example'
    db.session.commit()
    sent = []
    monkeypatch.setattr('app.shared.tasks.pages.send_post_request', lambda inbox, *a, **k: sent.append(inbox))

    send_post(post.id, session=db.session)
    assert sent == []

    post.status = POST_STATUS_PUBLISHED
    with_media(post, status='ready')
    db.session.commit()
    send_post(post.id, session=db.session)
    assert 'https://peer.example/inbox' in sent


# ---------------------------------------------------------------- the helpers


def test_store_uploaded_media_refuses_what_it_cannot_accept(env):
    post = make_post(env.chan, env.mod, ap_id='https://test.piefed.local/post/x')
    bad = FileStorage(stream=io.BytesIO(fixture_bytes('not_media.mp4')), filename='x.mp4')
    assert store_uploaded_media(post, bad, 'video') == 'This is not a supported video file.'

    set_setting('max_audio_upload_bytes', 10)
    big = FileStorage(stream=io.BytesIO(fixture_bytes('audio.mp3')), filename='x.mp3')
    assert store_uploaded_media(post, big, 'audio') == 'This audio file is too large.'
    assert post.url is None and media_of(post) is None


def recorded_post(env, url, ap_id='https://test.piefed.local/post/crafted'):
    """A post carrying a media record and `url`, as no code path but store_uploaded_media makes one."""
    post = make_post(env.chan, env.mod, ap_id=ap_id)
    post.url = url
    with_media(post, status='ready')
    db.session.commit()
    return post


def media_url(env, rest):
    return env.app.config['SERVER_URL'] + '/static/media/posts/' + rest


def test_media_local_path_names_a_file_this_server_stored(env):
    post = stored_post(env, env.chan)
    path = media_local_path(post)
    assert re.fullmatch(r'app/static/media/posts/(\w\w)/(\w\w)/\1\2\w{11}\.mp4', path)
    assert os.path.exists(path) and post.url == media_url(env, path[len('app/static/media/posts/'):])


@pytest.mark.parametrize('rest', [
    '../../../config.py',                       # traversal
    'ab/cd/../../../../config.py',
    '%2e%2e/%2e%2e/abcdEFGHIJKLMNO.mp4',        # percent-escaped traversal
    'ab/cd/abceEFGHIJKLMNO.mp4',               # directories that are not the name's first four characters
    'ab/cd/x/abcdEFGHIJKLMNO.mp4',             # an extra segment
    'ab/cd/abcdEFGHIJKLMN.mp4',                # a name one character short
    'ab/cd/abcdEFGHIJKLMNO.py',                # an extension we never write
    'ab/cd/abcdEFGHIJKLMNO.mp4\n',
])
def test_media_local_path_refuses_a_url_it_did_not_write(env, rest):
    assert media_local_path(recorded_post(env, media_url(env, rest))) is None


def test_media_local_path_needs_a_media_record_and_a_local_post(env):
    stored = stored_post(env, env.chan)
    link = make_post(env.chan, env.stranger, ap_id='https://test.piefed.local/post/link')
    link.url = stored.url                       # another post's media, on a link post with no record
    assert media_local_path(link) is None
    remote = recorded_post(env, stored.url, ap_id='https://peer.example/post/1')
    assert media_local_path(remote) is None     # a remote post, whatever record it carries
    assert media_local_path(recorded_post(env, None, ap_id='https://test.piefed.local/post/n')) is None
    assert media_local_path(recorded_post(env, 'https://example.com/static/media/posts/ab/cd/abcdEFGHIJKLMNO.mp4',
                                          ap_id='https://test.piefed.local/post/o')) is None


def test_media_local_path_refuses_a_path_that_resolves_outside_the_media_tree(env, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    os.makedirs('app/static/media/posts/ab', exist_ok=True)
    os.symlink(outside, 'app/static/media/posts/ab/cd')
    assert media_local_path(recorded_post(env, media_url(env, 'ab/cd/abcdEFGHIJKLMNO.mp4'))) is None


def test_a_failed_link_post_never_deletes_another_posts_media(env, monkeypatch):
    """A link post naming another post's media URL, failing inside edit_post, must not unlink that file."""
    stored = stored_post(env, env.chan)
    path = media_local_path(stored)

    def fail(*args, **kwargs):
        raise RuntimeError('a later step fails')
    monkeypatch.setattr('app.shared.post.is_image_url', lambda url: False)
    monkeypatch.setattr('app.shared.post.opengraph_parse', lambda url: None)
    monkeypatch.setattr(Post, 'calculate_cross_posts', fail)   # after post.url is set
    response = submit(env, env.mod, env.talk, 'link', link_url=stored.url)

    assert response.status_code == 302 and Post.query.count() == 1
    assert os.path.exists(path)


def test_queue_probe_queues_the_probe_task(monkeypatch):
    calls = []
    monkeypatch.setattr(media_module, 'task_selector', lambda key, **kw: calls.append((key, kw)))
    queue_probe(42)
    assert calls == [('probe_media', {'post_id': 42})]


def test_media_upload_kind_needs_the_kind_the_type_and_the_permission(env):
    upload = FileStorage(stream=io.BytesIO(b'x'), filename='x.mp4')
    assert media_upload_kind(env.mod, env.chan, POST_TYPE_VIDEO, upload) == 'video'
    assert media_upload_kind(env.mod, env.cast, POST_TYPE_LINK, upload) == 'audio'
    assert media_upload_kind(env.stranger, env.chan, POST_TYPE_VIDEO, upload) is None
    assert media_upload_kind(env.mod, env.talk, POST_TYPE_VIDEO, upload) is None
    assert media_upload_kind(env.mod, env.chan, POST_TYPE_IMAGE, upload) is None
    assert media_upload_kind(env.mod, env.chan, POST_TYPE_VIDEO, None) is None


def test_a_video_sent_to_the_image_form_of_a_video_channel_takes_the_image_path(env):
    """The image form keeps its own extension list: a video there is refused, not stored as media."""
    response = submit(env, env.mod, env.chan, 'image', 'clip.mp4', fixture_bytes('video_h264.mp4'))

    assert response.status_code == 302 and '/submit' not in response.headers['Location']
    assert Post.query.count() == 0
    assert stored_files() == []


def test_an_edit_does_not_schedule_a_processing_post(env):
    post = stored_post(env, env.chan)
    response = edit(env, post, scheduled_for='2099-01-01T10:00')

    db.session.expire_all()
    assert response.status_code == 302
    assert db.session.get(Post, post.id).status == POST_STATUS_PROCESSING


def test_the_audio_form_prefills_its_own_link_field(env):
    client, _ = env.client_for(env.mod)
    page = client.get(submit_url(env.app, env.cast, 'audio'), query_string={'link': 'https://example.com/e.mp3'})
    assert b'value="https://example.com/e.mp3"' in page.data

    source = make_post(env.cast, env.mod, ap_id='https://test.piefed.local/post/src', title='Source')
    source.url = 'https://example.com/source.mp3'
    db.session.commit()
    page = client.get(submit_url(env.app, env.cast, 'audio'), query_string={'source': source.id})
    assert b'value="https://example.com/source.mp3"' in page.data


def test_a_quicktime_upload_is_stored_as_mov(env):
    post = make_post(env.chan, env.mod, ap_id='https://test.piefed.local/post/qt')
    upload = FileStorage(stream=io.BytesIO(b'\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00qt  '), filename='clip.bin')
    assert store_uploaded_media(post, upload, 'video') is None
    db.session.commit()
    assert post.url.endswith('.mov') and media_of(post)['mime'] == 'video/quicktime'
    assert os.path.exists(media_local_path(post))


def test_an_audio_link_that_is_not_http_is_the_forms_ordinary_error(env):
    response = submit(env, env.mod, env.cast, 'audio', audio_url='ftp://example.com/e.mp3')
    assert response.status_code == 200
    assert b'Submitted links need to start with' in response.data
    assert Post.query.count() == 0


# ---------------------------------------------------------------- Task 5: the whole chain, eager


def test_an_upload_is_probed_and_published_in_one_eager_request(env, monkeypatch):
    """The real queue_probe and probe_media, run eagerly inside the upload request. The create queues the probe
    only after deciding not to federate or notify, so the published post is sent and announced once, by
    probe_media, and not a second time by the request that saw it come back published."""
    monkeypatch.setattr('app.shared.post.queue_probe', queue_probe)
    sent, notified = [], []
    for module in ('app.shared.post', 'app.shared.tasks.media'):
        monkeypatch.setattr(f'{module}.task_selector', lambda key, **kw: sent.append((key, kw)))
        monkeypatch.setattr(f'{module}.notify_about_post', lambda post: notified.append(post.id))
    monkeypatch.setattr('app.shared.tasks.media.make_image_sizes', lambda *args: None)

    response = submit(env, env.mod, env.chan, 'video', 'clip.mp4', fixture_bytes('video_h264.mp4'))

    assert response.status_code == 302
    db.session.expire_all()
    post = Post.query.one()
    assert post.status == POST_STATUS_PUBLISHED
    assert media_of(post)['status'] == 'ready' and media_of(post)['codec'] == 'h264'
    assert sent == [('make_post', {'post_id': post.id})]
    assert notified == [post.id]
    image = post.image
    os.unlink(image.local_path_for_url(image.source_url))
