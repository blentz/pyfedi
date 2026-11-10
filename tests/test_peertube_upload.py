"""Uploading a video from PeerTube's app (plan W2.3; spec 2026-11-10-peertube-upload-design, rulings U1-U3): the
resumable upload in parts, the post made when the details come, its privacy (public, unlisted made public, internal a
local-only post; private and password refused), the web's rule for who may upload where and how much, resuming,
cancelling, editing and deleting."""
import io
import os
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app import db
from app.constants import COMMUNITY_KIND_DISCUSSION, COMMUNITY_KIND_VIDEO_CHANNEL, POST_STATUS_PROCESSING, \
    POST_STATUS_PUBLISHED, POST_TYPE_VIDEO
from app.models import Language, Post, Site, VideoUpload, utcnow
from app.oauth_server import tokens
from app.peertube.entities import video_uuid
from app.shared import video_uploads
from app.local_only import outbound_refusal
from app.shared.media import with_media
from app.utils import set_setting
from tests.factories import make_community, make_community_member, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site', 'private_static_tree')
FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures', 'media')
UPLOAD = '/api/v1/videos/upload-resumable'


def _clip(name='video_h264.mp4') -> bytes:
    with open(os.path.join(FIXTURES, name), 'rb') as clip:
        return clip.read()


@pytest.fixture
def env(app, db_session, monkeypatch):
    db.session.get(Site, 1).private_instance = False
    local = make_instance('test.piefed.local', software='piefed')
    make_user(local, 'founder', local=True)
    mod, stranger = make_user(local, 'ptuploader', local=True), make_user(local, 'ptstranger', local=True)
    for user in (mod, stranger):
        user.verified, user.private_key = True, 'a private key'
    db.session.add_all([Language(code='und', name='Undetermined'), Language(code='en', name='English')])
    chan, talk = make_community('ptchan'), make_community('pttalk')
    chan.kind, talk.kind = COMMUNITY_KIND_VIDEO_CHANNEL, COMMUNITY_KIND_DISCUSSION
    db.session.commit()
    for community in (chan, talk):
        make_community_member(mod, community, is_moderator=True)
        make_community_member(stranger, community)
    set_setting('allow_video_file_uploads', 'users')
    db.session.commit()
    monkeypatch.setattr('app.shared.post.queue_probe', lambda post_id: None)   # the media pipeline is its own tests'
    local_client, _secret = tokens.local_client()

    def headers(user):
        access, _refresh = tokens.issue_token(local_client, user, tokens.LOCAL_CLIENT_SCOPE)
        return {'Authorization': f'Bearer {access}'}
    return SimpleNamespace(app=app, client=app.test_client(), mod=mod, stranger=stranger, chan=chan, talk=talk,
                           headers=headers(mod), stranger_headers=headers(stranger))


def _start(env, data=b'', headers=None, **fields):
    body = {'filename': 'clip.mp4', 'name': 'My upload', 'channelId': env.chan.id, **fields}
    return env.client.post(UPLOAD, json=body, headers={**(headers or env.headers),
                                                       'X-Upload-Content-Length': str(len(data) or 1000),
                                                       'X-Upload-Content-Type': 'video/mp4'})


def _upload_id(response) -> str:
    assert response.status_code == 201, response.get_data(as_text=True)
    return response.headers['Location'].split('upload_id=')[1]


def _part(env, upload_id, data, first, total, headers=None):
    last = first + len(data) - 1
    return env.client.put(f'{UPLOAD}?upload_id={upload_id}', data=data,
                          headers={**(headers or env.headers), 'Content-Range': f'bytes {first}-{last}/{total}',
                                   'Content-Type': 'application/octet-stream'})


def _uploaded(env, data, **fields) -> dict:
    upload_id = _upload_id(_start(env, data, **fields))
    middle = len(data) // 2
    held = _part(env, upload_id, data[:middle], 0, len(data))
    assert held.status_code == 308 and held.headers['Range'] == f'bytes=0-{middle - 1}'
    done = _part(env, upload_id, data[middle:], middle, len(data))
    assert done.status_code == 200, done.get_data(as_text=True)
    return {'upload_id': upload_id, **done.get_json()['video']}


def _details(env, uuid, headers=None, **fields):
    return env.client.put(f'/api/v1/videos/{uuid}', json=fields, headers=headers or env.headers)


def _post_of(upload_id) -> Post:
    upload = VideoUpload.query.filter_by(upload_id=upload_id).one()
    return db.session.get(Post, upload.post_id) if upload.post_id is not None else None


def test_a_video_uploaded_in_parts_becomes_a_post_when_its_details_come(env):
    video = _uploaded(env, _clip())
    upload = VideoUpload.query.filter_by(upload_id=video['upload_id']).one()
    assert upload.post_id is None and video['uuid'] == video_uploads.pending_uuid(upload)
    response = _details(env, video['uuid'], name='Renamed', description='About it', privacy=1, nsfw=False,
                        tags=['one', 'two'], category=5, licence=1)
    assert response.status_code == 204, response.get_data(as_text=True)
    post = _post_of(video['upload_id'])
    assert (post.title, post.body, post.type, post.status) == ('Renamed', 'About it', POST_TYPE_VIDEO,
                                                               POST_STATUS_PROCESSING)
    assert not post.local_only and sorted(tag.name for tag in post.tags) == ['one', 'two']
    assert not os.path.exists(upload.path)   # the parts' file went into the media store
    again = _details(env, video['uuid'], name='Named again')   # the upload's uuid names its post from then on
    assert again.status_code == 204 and db.session.get(Post, post.id).title == 'Named again'


@pytest.mark.parametrize('privacy,local_only', [(1, False), (2, False), (4, True)])
def test_public_and_unlisted_are_public_and_internal_is_local_only(env, privacy, local_only):
    video = _uploaded(env, _clip())
    assert _details(env, video['uuid'], privacy=privacy).status_code == 204
    post = _post_of(video['upload_id'])
    assert post.local_only is local_only and post.visibility == 'public'


@pytest.mark.parametrize('privacy', [3, 5, 9, '1', True])
def test_private_password_and_unknown_privacies_are_refused(env, privacy):
    video = _uploaded(env, _clip())
    response = _details(env, video['uuid'], privacy=privacy)
    assert response.status_code == 400 and response.get_json()['code'] == 'invalid_request'
    assert _post_of(video['upload_id']) is None
    assert _start(env, privacy=privacy).status_code == 400


def test_an_upload_whose_start_carried_its_privacy_is_posted_at_its_last_byte(env):
    video = _uploaded(env, _clip(), privacy=4, description='Said at the start')
    post = _post_of(video['upload_id'])
    assert video['uuid'] == video_uuid(post) and video['id'] == post.id
    assert post.local_only and post.body == 'Said at the start'


def test_a_part_from_elsewhere_and_a_question_are_answered_with_what_is_held(env):
    data = _clip()
    upload_id = _upload_id(_start(env, data))
    asked = env.client.put(f'{UPLOAD}?upload_id={upload_id}', headers={**env.headers,
                                                                       'Content-Range': f'bytes */{len(data)}'})
    assert asked.status_code == 308 and 'Range' not in asked.headers
    _part(env, upload_id, data[:100], 0, len(data))
    ahead = _part(env, upload_id, data[200:300], 200, len(data))
    assert ahead.status_code == 308 and ahead.headers['Range'] == 'bytes=0-99'
    assert VideoUpload.query.filter_by(upload_id=upload_id).one().received == 100


@pytest.mark.parametrize('content_range', ['bytes 0-9/7', 'bytes 5-2/1000', 'nonsense', 'bytes 0-1999/1000',
                                           'bytes */999'])
def test_a_range_that_is_not_the_uploads_is_refused(env, content_range):
    upload_id = _upload_id(_start(env, b'x' * 1000))
    response = env.client.put(f'{UPLOAD}?upload_id={upload_id}', data=b'x' * 10,
                              headers={**env.headers, 'Content-Range': content_range})
    assert response.status_code == 400


def test_a_part_whose_body_is_not_its_range_is_refused(env):
    upload_id = _upload_id(_start(env, b'x' * 1000))
    response = env.client.put(f'{UPLOAD}?upload_id={upload_id}', data=b'x' * 5,
                              headers={**env.headers, 'Content-Range': 'bytes 0-9/1000'})
    assert response.status_code == 400


def test_a_short_part_is_taken_back(env):
    upload = VideoUpload.query.filter_by(upload_id=_upload_id(_start(env, b'x' * 1000))).one()
    with pytest.raises(video_uploads.Refused):
        video_uploads.receive(upload, 0, io.BytesIO(b'x' * 5), 10)
    assert os.path.getsize(upload.path) == 0 and upload.received == 0


def test_only_where_the_web_takes_an_upload(env):
    assert _start(env, headers=env.stranger_headers).status_code == 403   # no moderator
    assert _start(env, channelId=env.talk.id).status_code == 403   # no video community
    assert _start(env, channelId=999999).status_code == 403
    assert _start(env, channelId='x').status_code == 403
    set_setting('allow_video_file_uploads', 'no')
    assert _start(env).status_code == 403


@pytest.mark.parametrize('fields,size', [({'name': ''}, '10'), ({'filename': ''}, '10'), ({}, '0'), ({}, 'big'),
                                         ({'name': 'x' * 300}, '10')])
def test_a_start_without_a_name_or_a_size_is_refused(env, fields, size):
    body = {'filename': 'clip.mp4', 'name': 'A name', 'channelId': env.chan.id, **fields}
    response = env.client.post(UPLOAD, json=body, headers={**env.headers, 'X-Upload-Content-Length': size})
    assert response.status_code == 400


def test_the_limits_are_the_webs_per_file_and_unfinished_ones(env):
    set_setting('max_video_upload_bytes', 1000)
    too_big = _start(env, b'x' * 1001)
    assert too_big.status_code == 413 and too_big.get_json()['code'] == 'max_file_size_reached'
    for _ in range(3):
        _upload_id(_start(env, b'x' * 600))
    assert _start(env, b'x' * 10).status_code == 429   # three unfinished already (U2)
    VideoUpload.query.filter_by(user_id=env.mod.id).delete()
    _upload_id(_start(env, b'x' * 1000))
    _upload_id(_start(env, b'x' * 1000))   # 2000 together: twice the limit, allowed
    together = _start(env, b'x' * 10)
    assert together.status_code == 413 and together.get_json()['code'] == 'quota_reached'   # past twice the limit


def test_a_file_that_is_no_video_is_refused_at_its_details(env):
    video = _uploaded(env, _clip('not_media.mp4'))
    response = _details(env, video['uuid'], privacy=1)
    assert response.status_code == 400 and _post_of(video['upload_id']) is None


def test_details_before_the_last_byte_are_refused(env):
    upload_id = _upload_id(_start(env, b'x' * 1000))
    upload = VideoUpload.query.filter_by(upload_id=upload_id).one()
    assert _details(env, video_uploads.pending_uuid(upload), privacy=1).status_code == 400


def test_an_upload_is_its_uploaders_alone(env):
    upload_id = _upload_id(_start(env, b'x' * 1000))
    assert _part(env, upload_id, b'x' * 10, 0, 1000, headers=env.stranger_headers).status_code == 404
    assert env.client.delete(f'{UPLOAD}?upload_id={upload_id}', headers=env.stranger_headers).status_code == 404
    upload = VideoUpload.query.filter_by(upload_id=upload_id).one()
    assert _details(env, video_uploads.pending_uuid(upload), headers=env.stranger_headers,
                    privacy=1).status_code == 404


def test_cancelling_removes_the_parts(env):
    upload_id = _upload_id(_start(env, b'x' * 1000))
    path = VideoUpload.query.filter_by(upload_id=upload_id).one().path
    assert env.client.delete(f'{UPLOAD}?upload_id={upload_id}', headers=env.headers).status_code == 204
    assert not os.path.exists(path) and VideoUpload.query.filter_by(upload_id=upload_id).first() is None


def test_a_finished_upload_asked_again_answers_its_video(env):
    video = _uploaded(env, _clip())
    again = _part(env, video['upload_id'], b'x', 0, 1)
    assert again.status_code == 200 and again.get_json()['video']['uuid'] == video['uuid']


def test_a_videos_privacy_may_change_while_it_processes_and_is_fixed_once_published(env):
    """The app sends the details twice: on its own as the upload ends (the defaults), then the person's choice. The
    post is not published nor federated while its media processes, so until then its privacy may change."""
    video = _uploaded(env, _clip(), privacy=1)
    post = _post_of(video['upload_id'])
    assert _details(env, video['uuid'], privacy=4).status_code == 204
    assert db.session.get(Post, post.id).local_only is True
    assert _details(env, video['uuid'], privacy=1).status_code == 204
    assert db.session.get(Post, post.id).local_only is False
    post = db.session.get(Post, post.id)
    post.status = POST_STATUS_PUBLISHED
    db.session.commit()
    refused = _details(env, video['uuid'], privacy=4)
    assert refused.status_code == 400 and 'published' in refused.get_json()['detail']


def test_a_videos_details_are_edited_and_its_privacy_is_fixed(env):
    video = _uploaded(env, _clip(), privacy=1)
    post = _post_of(video['upload_id'])
    post.status = POST_STATUS_PUBLISHED
    db.session.commit()
    assert _details(env, video_uuid(post), name='Edited', nsfw=True, tags=['three']).status_code == 204
    post = db.session.get(Post, post.id)
    assert (post.title, post.nsfw, [tag.name for tag in post.tags]) == ('Edited', True, ['three'])
    assert _details(env, video_uuid(post), privacy=4).status_code == 400
    assert _details(env, video_uuid(post), privacy=1, description='Kept public').status_code == 204
    assert _details(env, video_uuid(post), headers=env.stranger_headers, name='Not yours').status_code == 404


def test_a_thumbnail_is_taken_and_not_kept(env):
    video = _uploaded(env, _clip(), privacy=1)
    response = env.client.put(f"/api/v1/videos/{video['uuid']}", headers=env.headers,
                              data={'thumbnailfile': (io.BytesIO(b'png'), 'image.png')},
                              content_type='multipart/form-data')
    assert response.status_code == 204
    other = env.client.put('/api/v1/videos/999999', headers=env.headers,
                           data={'thumbnailfile': (io.BytesIO(b'png'), 'image.png')}, content_type='multipart/form-data')
    assert other.status_code == 404


def test_deleting_a_video_or_an_upload_with_no_post(env):
    pending = _uploaded(env, _clip())
    assert env.client.delete(f"/api/v1/videos/{pending['uuid']}", headers=env.headers).status_code == 204
    assert VideoUpload.query.filter_by(upload_id=pending['upload_id']).first() is None
    posted = _uploaded(env, _clip(), privacy=1)
    post = _post_of(posted['upload_id'])
    assert env.client.delete(f"/api/v1/videos/{posted['uuid']}", headers=env.stranger_headers).status_code == 404
    assert env.client.delete(f"/api/v1/videos/{posted['uuid']}", headers=env.headers).status_code == 204
    assert db.session.get(Post, post.id).deleted
    assert env.client.delete(f"/api/v1/videos/{posted['uuid']}", headers=env.headers).status_code == 404


def test_the_hourly_sweep_removes_what_no_post_came_of(env):
    stale_id, kept_id = _upload_id(_start(env, b'x' * 1000)), _upload_id(_start(env, b'x' * 1000))
    posted = _uploaded(env, _clip(), privacy=1)
    stale = VideoUpload.query.filter_by(upload_id=stale_id).one()
    stale.updated_at = utcnow() - timedelta(hours=25)
    _post_of(posted['upload_id'])
    VideoUpload.query.filter_by(upload_id=posted['upload_id']).one().updated_at = utcnow() - timedelta(days=3)
    db.session.commit()
    assert video_uploads.sweep() == 1
    assert VideoUpload.query.filter_by(upload_id=stale_id).first() is None
    assert {row.upload_id for row in VideoUpload.query.all()} == {kept_id, posted['upload_id']}


def test_chapters_are_taken_and_not_kept(env):
    """The app sends them with the details; PieFed keeps no chapters, and a refusal stopped its details screen."""
    video = _uploaded(env, _clip(), privacy=1)
    sent = env.client.put(f"/api/v1/videos/{video['uuid']}/chapters", json={'chapters': []}, headers=env.headers)
    assert sent.status_code == 204
    assert env.client.put('/api/v1/videos/999999/chapters', json={'chapters': []},
                          headers=env.headers).status_code == 404


def test_the_upload_forms_lists(env):
    assert env.client.get('/api/v1/videos/privacies', headers=env.headers).get_json() == {'1': 'Public',
                                                                                          '4': 'Internal'}
    for name in ('categories', 'licences', 'languages'):
        assert env.client.get(f'/api/v1/videos/{name}', headers=env.headers).get_json() == {}


def test_the_channels_offered_are_where_an_upload_goes(env):
    """A moderated video community is offered; a moderated discussion is not (the web takes no upload there)."""
    me = env.client.get('/api/v1/users/me', headers=env.headers).get_json()
    assert [channel['name'] for channel in me['videoChannels']] == ['ptchan']
    assert env.client.get('/api/v1/users/me', headers=env.stranger_headers).get_json()['videoChannels'] == []


def test_an_internal_video_reads_back_as_internal(env):
    post = Post(user_id=env.mod.id, community_id=env.chan.id, instance_id=env.mod.instance_id, title='Internal one',
                type=POST_TYPE_VIDEO, ap_id='https://test.piefed.local/post/internal', local_only=True,
                url='https://test.piefed.local/static/media/posts/ab/cd/abcdefghijklmno.mp4', posted_at=utcnow())
    db.session.add(post)
    db.session.commit()
    with_media(post, status='ready', uuid='1', mime='video/mp4', bytes=1, duration_s=3, width=640, height=360, fps=30,
               codec='h264')
    db.session.commit()
    body = env.client.get(f'/api/v1/videos/{post.id}', headers=env.headers).get_json()
    assert body['privacy'] == {'id': 4, 'label': 'Internal'}
    assert env.client.get(f'/api/v1/videos/{post.id}').status_code == 404   # signed out: not there at all


def test_an_internal_upload_never_leaves_this_server(env):
    video = _uploaded(env, _clip(), privacy=4)
    post = _post_of(video['upload_id'])
    activity = {'type': 'Create', 'object': {'id': post.profile_id(), 'type': 'Page'}}
    assert outbound_refusal(activity) == 'it names a local-only post'


def test_finished_uploads_no_post_came_of_count_against_the_limits(env):
    """Their file is held until the sweep as an unfinished one's is: finishing upload after upload with no details
    must not get round U2's limits (security review)."""
    for _ in range(3):
        _uploaded(env, _clip())   # finished, no details: no post
    refused = _start(env, b'x' * 10)
    assert refused.status_code == 429
