"""Step 3, Task 5: probe_media validates an upload in the sandbox, thumbnails it, moves it to S3 when S3 is on, and
then publishes its post, or fails it and tells the author (spec §5.2, A13.4, A13.5, A13.7, A13.8, A13.9).

The tests run the real ffprobe and ffmpeg on the tiny fixtures in tests/fixtures/media. Each runs from a private
static tree, so the files it provokes are its own; it still removes them. Federation and the subscriber notifications
are recorded, not run: what is asserted is that they are asked for, once, and only for a ready post.
"""
import io
import math
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

import boto3
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from werkzeug.datastructures import FileStorage

from app import cli, db
from app.constants import (COMMUNITY_KIND_DISCUSSION, COMMUNITY_KIND_PODCAST, COMMUNITY_KIND_VIDEO_CHANNEL, NOTIF_DEFAULT,
                           POST_STATUS_PROCESSING, POST_STATUS_PUBLISHED, POST_STATUS_SCHEDULED, POST_TYPE_LINK,
                           POST_TYPE_VIDEO)
from app.models import CommunityBan, CommunityMember, File, ModLog, Notification, Post, Settings, User, utcnow
from app.shared.ffmpeg import MediaToolError, run_ffprobe
from app.shared.media import media_local_path, media_of, store_uploaded_media, with_media
from app.shared import post as post_module
from app.shared.tasks import task_selector
from app.shared.tasks import media as media_tasks
from app.shared.tasks.media import probe_media, publish_ready_post, requeue_stuck_media, validate_probe
from app.utils import set_setting, upload_file_to_s3
from tests.factories import make_community, make_community_member, make_instance, make_post, make_user

FIX = os.path.join(os.path.dirname(__file__), 'fixtures', 'media')


def probe(*streams, duration='12.5'):
    return {'streams': list(streams), 'format': {'duration': duration}}


V = {'codec_type': 'video', 'codec_name': 'h264', 'width': 1280, 'height': 720, 'avg_frame_rate': '30000/1001'}
A = {'codec_type': 'audio', 'codec_name': 'aac'}
COVER = {'codec_type': 'video', 'codec_name': 'mjpeg', 'disposition': {'attached_pic': 1}}


# ---------------------------------------------------------------- validate_probe: no database, no ffmpeg


def test_video_metadata():
    assert validate_probe(probe(V, A), 'video') == {
        'duration_s': 13, 'width': 1280, 'height': 720, 'fps': 30, 'codec': 'h264'}


def test_audio_metadata_has_no_picture_fields():
    assert validate_probe(probe({'codec_type': 'audio', 'codec_name': 'opus'}), 'audio') == {
        'duration_s': 13, 'codec': 'opus'}


def test_audio_with_cover_art_reads_the_audio_stream():
    assert validate_probe(probe(COVER, {'codec_type': 'audio', 'codec_name': 'mp3'}), 'audio')['codec'] == 'mp3'


def test_cover_art_is_not_the_video_stream_of_a_video():
    with pytest.raises(MediaToolError, match='no video stream'):
        validate_probe(probe(COVER, A), 'video')


@pytest.mark.parametrize('rate', ['0/0', 'N/A', None, '25'])
def test_an_unreadable_frame_rate_is_left_out(rate):
    meta = validate_probe(probe({**V, 'avg_frame_rate': rate}, A), 'video')
    assert 'fps' not in meta and meta['width'] == 1280


@pytest.mark.parametrize('bad, reason', [
    (probe({**V, 'codec_name': 'mpeg4'}, A), 'codec'),
    (probe(V, A, duration='0'), 'duration'), (probe(V, A, duration='N/A'), 'duration'),
    (probe({**V, 'width': 0}, A), 'dimensions'), (probe({**V, 'height': None}, A), 'dimensions'),
    (probe(A), 'no video stream'),
    ({'streams': 'x'}, 'unreadable'), ({'streams': []}, 'duration'),
    ({'streams': [V], 'format': 'x'}, 'duration'),
])
def test_bad_video_is_refused_with_a_reason(bad, reason):
    with pytest.raises(MediaToolError, match=reason):
        validate_probe(bad, 'video')


def test_bad_audio_codec_is_refused():
    with pytest.raises(MediaToolError, match='codec'):
        validate_probe(probe({'codec_type': 'audio', 'codec_name': 'flac'}), 'audio')


def test_cover_art_alone_is_not_audio():
    with pytest.raises(MediaToolError, match='no audio stream'):
        validate_probe(probe(COVER), 'audio')


# ---------------------------------------------------------------- probe_media against the database


@pytest.fixture
def env(app, db_session, site, private_static_tree, monkeypatch):
    local = make_instance('test.piefed.local', software='piefed')
    make_user(local, 'founder', local=True)   # user 1 is the admin
    mod = make_user(local, 'mod', local=True)
    mod.verified = True
    mod.private_key = 'a private key'   # can_create_post wants both of a local user
    chan, cast = make_community('chan'), make_community('cast')
    chan.kind, cast.kind = COMMUNITY_KIND_VIDEO_CHANNEL, COMMUNITY_KIND_PODCAST
    db.session.commit()
    for community in (chan, cast):
        make_community_member(mod, community, is_moderator=True)
    set_setting('allow_video_file_uploads', 'users')   # the author may upload to both, until a test revokes it
    set_setting('allow_audio_file_uploads', 'users')

    tasks, notified, sized = [], [], []
    monkeypatch.setattr(media_tasks, 'task_selector', lambda key, **kw: tasks.append((key, kw)))
    monkeypatch.setattr(media_tasks, 'notify_about_post', lambda post: notified.append(post.id))
    monkeypatch.setattr(media_tasks, 'make_image_sizes', lambda *args: sized.append(args))
    yield SimpleNamespace(app=app, mod=mod, chan=chan, cast=cast, tasks=tasks, notified=notified, sized=sized)
    db.session.rollback()
    for directory, _, files in os.walk('app/static'):
        for name in files:
            os.unlink(os.path.join(directory, name))


def stored_post(env, fixture='video_h264.mp4', kind='video', title='Processing', **columns):
    community = env.chan if kind == 'video' else env.cast
    post = make_post(community, env.mod, ap_id=f'https://test.piefed.local/post/{title}', title=title)
    post.type = POST_TYPE_VIDEO if kind == 'video' else POST_TYPE_LINK
    for name, value in columns.items():
        setattr(post, name, value)
    with open(os.path.join(FIX, fixture), 'rb') as f:
        upload = FileStorage(stream=io.BytesIO(f.read()), filename='upload.bin')
    assert store_uploaded_media(post, upload, kind) is None
    db.session.commit()
    return post


def run(post):
    """probe_media as the worker runs it, then the post as the database now holds it."""
    post_id = post.id
    probe_media(False, post_id)
    db.session.expire_all()
    return db.session.get(Post, post_id)


def files_on_disk():
    return sorted(os.path.join(d, f) for d, _, files in os.walk('app/static') for f in files)


def ffprobe_duration(fixture):
    return math.ceil(float(run_ffprobe(os.path.join(FIX, fixture))['format']['duration']))


def assert_nothing_published(env, post):
    assert post.status == POST_STATUS_PROCESSING
    assert env.tasks == [] and env.notified == []


def test_a_good_video_is_ready_thumbnailed_and_published(env):
    post = stored_post(env)
    before = dict(media_of(post))
    post = run(post)

    media = media_of(post)
    assert media['status'] == 'ready'
    assert media['duration_s'] == ffprobe_duration('video_h264.mp4')
    assert (media['width'], media['height'], media['codec'], media['fps']) == (64, 64, 'h264', 10)
    assert media['uuid'] == before['uuid'] and media['bytes'] == before['bytes'] and media['mime'] == 'video/mp4'
    assert 'error' not in media
    assert post.status == POST_STATUS_PUBLISHED and post.posted_at is not None

    assert isinstance(post.image, File) and media['thumbnail_file_id'] == post.image_id
    thumbnail = post.image.local_path_for_url(post.image.source_url)
    assert thumbnail.endswith('.jpg') and os.path.getsize(thumbnail) > 0
    assert env.sized == [(post.image_id, 512, 1200, 'posts', post.community.low_quality)]
    assert env.tasks == [('make_post', {'post_id': post.id})]
    assert env.notified == [post.id]
    assert os.path.isfile(media_local_path(post))   # S3 is off: the media stays where it was stored
    assert os.listdir('app/static/tmp') == []


def test_an_audio_file_with_cover_art_gets_that_art_as_its_thumbnail(env):
    post = run(stored_post(env, 'audio_cover.mp3', 'audio'))
    media = media_of(post)
    assert media['status'] == 'ready' and media['codec'] == 'mp3'
    assert media['duration_s'] == ffprobe_duration('audio_cover.mp3')
    assert 'width' not in media and 'height' not in media and 'fps' not in media
    assert post.image_id is not None and media['thumbnail_file_id'] == post.image_id
    assert os.path.isfile(post.image.local_path_for_url(post.image.source_url))
    assert post.status == POST_STATUS_PUBLISHED


def test_an_audio_file_without_art_has_no_thumbnail(env):
    post = run(stored_post(env, 'audio.mp3', 'audio'))
    media = media_of(post)
    assert media['status'] == 'ready' and 'thumbnail_file_id' not in media
    assert post.image_id is None and env.sized == []
    assert post.status == POST_STATUS_PUBLISHED
    assert File.query.count() == 0


def test_a_refused_codec_fails_the_post_and_tells_the_author(env):
    post = stored_post(env, 'video_mpeg4.mp4')
    local = media_local_path(post)
    unread = env.mod.unread_notifications
    post = run(post)

    media = media_of(post)
    assert media['status'] == 'failed' and 'mpeg4' in media['error'] and 'codec' in media['error']
    assert_nothing_published(env, post)
    assert not os.path.exists(local) and files_on_disk() == []   # ruling: a failed upload's file is removed
    notification = Notification.query.one()
    assert notification.user_id == env.mod.id and notification.author_id == env.mod.id
    assert (notification.notif_type, notification.subtype) == (NOTIF_DEFAULT, 'media_failed')
    assert notification.targets == {'post_id': post.id}
    assert notification.url == f'/post/{post.id}'
    assert notification.title.startswith('Your upload could not be processed: ') and 'mpeg4' in notification.title
    assert db.session.get(User, env.mod.id).unread_notifications == unread + 1
    assert File.query.count() == 0


def test_the_failure_notification_lists_with_its_title(env, app):
    run(stored_post(env, 'video_mpeg4.mp4'))
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(env.mod.id)
        session['_fresh'] = True
    response = client.get('/notifications')
    assert response.status_code == 200
    assert 'Your upload could not be processed' in response.get_data(as_text=True)


def test_a_sandbox_error_fails_the_post_with_its_reason(env, monkeypatch):
    def timed_out(path):
        raise MediaToolError('ffprobe timed out after 60 s')
    monkeypatch.setattr(media_tasks, 'run_ffprobe', timed_out)
    post = run(stored_post(env))
    assert media_of(post)['status'] == 'failed'
    assert media_of(post)['error'] == 'ffprobe timed out after 60 s'
    assert_nothing_published(env, post)


def test_a_thumbnail_that_cannot_be_written_fails_the_post_and_leaves_nothing(env, monkeypatch):
    post = stored_post(env)
    media_file = media_local_path(post)

    def half_written(img, fp, **kwargs):
        with open(fp, 'wb') as f:
            f.write(b'partial')
        raise OSError(28, 'No space left on device', fp)
    monkeypatch.setattr(media_tasks, 'save_image_without_metadata', half_written)
    post = run(post)

    assert media_of(post)['status'] == 'failed'
    assert media_of(post)['error'] == 'the thumbnail could not be made'   # no local path reaches the author
    assert_nothing_published(env, post)
    assert post.image_id is None and File.query.count() == 0
    assert not os.path.exists(media_file) and files_on_disk() == []


def test_a_failure_after_the_thumbnail_removes_its_file_row_and_image(env, monkeypatch):
    post = stored_post(env)
    media_file = media_local_path(post)

    def refuse(*args):
        raise OSError('the disk went away')
    monkeypatch.setattr(media_tasks, 'store_files_in_s3', lambda: True)
    monkeypatch.setattr(media_tasks, 'upload_file_to_s3', refuse)
    post = run(post)

    assert media_of(post)['status'] == 'failed'
    assert media_of(post)['error'] == 'the file could not be stored'
    assert_nothing_published(env, post)
    assert post.image_id is None and File.query.count() == 0
    assert 'thumbnail_file_id' not in media_of(post)
    assert not os.path.exists(media_file) and files_on_disk() == []


def test_a_scheduled_upload_waits_for_its_schedule(env):
    post = run(stored_post(env, scheduled_for=utcnow() + timedelta(days=1), timezone='UTC'))
    assert media_of(post)['status'] == 'ready'
    assert post.status == POST_STATUS_SCHEDULED
    assert env.tasks == [] and env.notified == []


@pytest.fixture
def s3(env, s3_bucket, monkeypatch):
    for key, value in (('S3_ACCESS_KEY', 'key'), ('S3_ACCESS_SECRET', 'secret'),
                       ('S3_ENDPOINT', 'https://s3.us-east-1.amazonaws.com'), ('S3_REGION', 'us-east-1'),
                       ('S3_BUCKET', s3_bucket), ('S3_PUBLIC_URL', 'cdn.probeland.test'),
                       ('S3_STORAGE_CLASS', ''), ('S3_PUBLIC_ACL', '')):
        monkeypatch.setitem(env.app.config, key, value)
    return boto3.client('s3', region_name='us-east-1')


def test_with_s3_on_the_media_and_its_thumbnail_move_to_the_bucket(env, s3):
    post = stored_post(env)
    local = media_local_path(post)
    name = os.path.basename(local)
    post = run(post)

    key = f'posts/{name[0:2]}/{name[2:4]}/{name}'
    head = s3.head_object(Bucket='pyfedi-test', Key=key)
    assert head['ContentType'] == 'video/mp4'
    assert head['ContentLength'] == media_of(post)['bytes']
    assert post.url == f'https://cdn.probeland.test/{key}'
    assert media_local_path(post) is None
    assert not os.path.exists(local)

    assert post.image.source_url.startswith('https://cdn.probeland.test/posts/')
    thumbnail_key = post.image.source_url.removeprefix('https://cdn.probeland.test/')
    assert s3.head_object(Bucket='pyfedi-test', Key=thumbnail_key)['ContentType'] == 'image/jpeg'
    assert files_on_disk() == []
    assert media_of(post)['status'] == 'ready' and post.status == POST_STATUS_PUBLISHED


def test_a_media_upload_refused_by_s3_removes_the_local_file_and_the_thumbnail_leaves_the_bucket(env, s3, monkeypatch):
    post = stored_post(env)
    local = media_local_path(post)
    real_upload = media_tasks.upload_file_to_s3

    def thumbnail_only(path, key, content_type):
        if path == local:
            raise boto3.exceptions.S3UploadFailedError('refused')
        return real_upload(path, key, content_type)
    monkeypatch.setattr(media_tasks, 'upload_file_to_s3', thumbnail_only)
    post = run(post)

    assert media_of(post)['status'] == 'failed' and media_of(post)['error'] == 'the file could not be stored'
    assert_nothing_published(env, post)
    assert not os.path.exists(local) and files_on_disk() == []   # ruling: a failed upload's file is removed
    assert s3.list_objects_v2(Bucket='pyfedi-test').get('KeyCount') == 0
    assert File.query.count() == 0


def test_the_post_row_is_not_locked_while_its_files_go_to_the_bucket(env, s3, monkeypatch):
    """An S3 upload of up to 500 MB can take minutes. Until the run holds its claim, nothing of the post is written,
    so the author editing it, a mod removing it or a ban's bulk update does not wait on the upload."""
    post = stored_post(env)
    post_id = post.id
    real_upload = media_tasks.upload_file_to_s3
    edited = []

    def upload(path, key, content_type):
        with Session(bind=db.engine) as other:
            other.execute(text("SET LOCAL lock_timeout = '1s'"))
            other.execute(text('UPDATE post SET title = :title WHERE id = :id'),
                          {'title': f'edited {len(edited)}', 'id': post_id})
            other.commit()
        edited.append(path)
        return real_upload(path, key, content_type)
    monkeypatch.setattr(media_tasks, 'upload_file_to_s3', upload)
    post = run(post)

    assert len(edited) == 2   # the thumbnail, then the media
    assert post.title == 'edited 1'
    assert media_of(post)['status'] == 'ready' and post.status == POST_STATUS_PUBLISHED
    assert post.url.startswith('https://cdn.probeland.test/posts/')
    assert media_of(post)['thumbnail_file_id'] == post.image_id
    assert post.image.source_url.startswith('https://cdn.probeland.test/posts/')


def test_upload_file_to_s3_passes_the_storage_class_and_acl(env, s3, monkeypatch):
    monkeypatch.setitem(env.app.config, 'S3_STORAGE_CLASS', 'STANDARD_IA')
    monkeypatch.setitem(env.app.config, 'S3_PUBLIC_ACL', True)
    with open('app/static/tmp/x.ogg', 'wb') as f:
        f.write(b'OggS')
    assert upload_file_to_s3('app/static/tmp/x.ogg', 'posts/ab/cd/x.ogg', 'audio/ogg') == \
        'https://cdn.probeland.test/posts/ab/cd/x.ogg'
    head = s3.head_object(Bucket='pyfedi-test', Key='posts/ab/cd/x.ogg')
    assert head['ContentType'] == 'audio/ogg' and head['StorageClass'] == 'STANDARD_IA'
    grants = s3.get_object_acl(Bucket='pyfedi-test', Key='posts/ab/cd/x.ogg')['Grants']
    assert any(g['Grantee'].get('URI', '').endswith('/AllUsers') and g['Permission'] == 'READ' for g in grants)
    assert os.path.exists('app/static/tmp/x.ogg')   # the caller removes the local copy


@pytest.mark.parametrize('case', [None, 'no media record', 'already ready', 'deleted', 'being probed'])
def test_a_post_with_nothing_to_probe_is_left_alone(env, case):
    if case is None:
        probe_media(False, 999999)
        assert env.tasks == []
        return
    if case == 'no media record':
        post = make_post(env.chan, env.mod, ap_id='https://test.piefed.local/post/plain', title='plain')
        before = (post.status, post.extensions)
    else:
        post = stored_post(env)
        if case == 'already ready':
            with_media(post, status='ready')
        elif case == 'being probed':
            with_media(post, status='probing')
        else:
            post.deleted = True
        db.session.commit()
        before = (post.status, dict(post.extensions))
    post = run(post)
    assert (post.status, post.extensions) == before
    assert env.tasks == [] and env.notified == [] and Notification.query.count() == 0


def test_a_file_gone_from_disk_fails_the_post(env):
    post = stored_post(env)
    os.unlink(media_local_path(post))
    post = run(post)
    assert media_of(post)['status'] == 'failed' and media_of(post)['error'] == 'media file is missing'
    assert_nothing_published(env, post)


def test_a_media_url_that_is_not_a_stored_name_is_missing_media(env):
    post = stored_post(env)
    post.url = 'https://elsewhere.example/clip.mp4'
    db.session.commit()
    post = run(post)
    assert media_of(post)['error'] == 'media file is missing'


def test_an_error_the_task_does_not_expect_is_rolled_back_and_raised(env, monkeypatch):
    post = stored_post(env)

    def broken(path):
        raise RuntimeError('a bug')
    monkeypatch.setattr(media_tasks, 'run_ffprobe', broken)
    with pytest.raises(RuntimeError):
        probe_media(False, post.id)
    db.session.expire_all()
    assert media_of(db.session.get(Post, post.id))['status'] == 'processing'   # released, not left 'probing'


def test_publish_ready_post_follows_the_scheduled_publisher(env):
    post = make_post(env.chan, env.mod, ap_id='https://test.piefed.local/post/p', title='p')
    post.status = POST_STATUS_PROCESSING
    db.session.commit()
    publish_ready_post(post)
    assert post.status == POST_STATUS_PUBLISHED and post.posted_at is not None
    assert env.tasks == [('make_post', {'post_id': post.id})] and env.notified == [post.id]


def test_publish_ready_post_leaves_a_scheduled_post_to_its_schedule(env):
    post = make_post(env.chan, env.mod, ap_id='https://test.piefed.local/post/s', title='s')
    post.status, post.scheduled_for = POST_STATUS_PROCESSING, utcnow() + timedelta(days=1)
    db.session.commit()
    publish_ready_post(post)
    assert post.status == POST_STATUS_SCHEDULED
    assert env.tasks == [] and env.notified == []


def test_probe_media_is_registered_with_task_selector(env, monkeypatch):
    seen = []
    monkeypatch.setattr(media_tasks.probe_media, 'delay', lambda **kw: seen.append(kw))
    task_selector('probe_media', post_id=5)
    assert seen == [{'send_async': True, 'post_id': 5}]


def test_a_frame_ffmpeg_could_not_grab_fails_the_post_with_its_reason(env, monkeypatch):
    def nothing_written(path, at_s, out_path):
        raise MediaToolError('ffmpeg wrote no image')
    monkeypatch.setattr(media_tasks, 'extract_frame', nothing_written)
    post = stored_post(env)
    media_file = media_local_path(post)
    post = run(post)
    assert media_of(post)['error'] == 'ffmpeg wrote no image'
    assert_nothing_published(env, post)
    assert not os.path.exists(media_file) and files_on_disk() == []


def test_the_thumbnail_is_written_in_the_configured_image_format(env, monkeypatch):
    monkeypatch.setitem(env.app.config, 'MEDIA_IMAGE_FORMAT', 'webp')
    post = run(stored_post(env))
    thumbnail = post.image.local_path_for_url(post.image.source_url)
    assert thumbnail.endswith('.webp')
    with open(thumbnail, 'rb') as f:
        head = f.read(12)
    assert head[:4] == b'RIFF' and head[8:12] == b'WEBP'


def test_with_s3_on_audio_without_art_moves_only_the_media(env, s3):
    post = stored_post(env, 'audio.mp3', 'audio')
    name = os.path.basename(media_local_path(post))
    post = run(post)
    key = f'posts/{name[0:2]}/{name[2:4]}/{name}'
    assert s3.head_object(Bucket='pyfedi-test', Key=key)['ContentType'] == 'audio/mpeg'
    assert s3.list_objects_v2(Bucket='pyfedi-test')['KeyCount'] == 1
    assert post.url == f'https://cdn.probeland.test/{key}' and post.image_id is None
    assert files_on_disk() == [] and post.status == POST_STATUS_PUBLISHED


# ---------------------------------------------------------------- the author's rights, re-checked at publish time


def while_probing(monkeypatch, change):
    """Run `change(session, post)` in another transaction, committed while probe_media is busy probing: the author or
    the community changes between the upload and the publish."""
    real = media_tasks.run_ffprobe

    def probing(path):
        with Session(bind=db.engine) as other:
            change(other)
            other.commit()
        return real(path)
    monkeypatch.setattr(media_tasks, 'run_ffprobe', probing)


def setting_off(name):
    def change(other):
        other.query(Settings).filter_by(name=name).one().value = '"no"'
    return change


GONE, NO_RIGHT = 'the community is gone', 'you may no longer post this media in this community'
# Each revocation, committed while probing, and the reason the author is then given
REVOCATIONS = {
    'the post was deleted': (
        lambda env, post: lambda o: setattr(o.get(Post, post.id), 'deleted', True), 'the post was deleted'),
    'the author was deleted': (
        lambda env, post: lambda o: setattr(o.get(User, env.mod.id), 'deleted', True), NO_RIGHT),
    'the author was banned from the site': (
        lambda env, post: lambda o: setattr(o.get(User, env.mod.id), 'banned', True), NO_RIGHT),
    'the author may no longer post': (
        lambda env, post: lambda o: setattr(o.get(User, env.mod.id), 'ban_posts', True), NO_RIGHT),
    'the author was banned from the community': (
        lambda env, post: lambda o: o.add(CommunityBan(user_id=env.mod.id, community_id=post.community_id,
                                                       banned_by=1)), NO_RIGHT),
    'the author is no longer a moderator': (
        lambda env, post: lambda o: setattr(
            o.query(CommunityMember).filter_by(user_id=env.mod.id, community_id=post.community_id).one(),
            'is_moderator', False), NO_RIGHT),
    'the upload switch was turned off': (
        lambda env, post: setting_off('allow_video_file_uploads'), NO_RIGHT),
    'the community is no longer a video channel': (
        lambda env, post: lambda o: setattr(o.get(type(env.chan), post.community_id), 'kind',
                                            COMMUNITY_KIND_DISCUSSION), NO_RIGHT),
    'the community was deleted': (
        lambda env, post: lambda o: setattr(o.get(type(env.chan), post.community_id), 'banned', True), GONE),
}


@pytest.mark.parametrize('case', list(REVOCATIONS))
def test_a_right_lost_while_probing_fails_the_post_instead_of_publishing_it(env, monkeypatch, case):
    post = stored_post(env)
    revoke, reason = REVOCATIONS[case]
    while_probing(monkeypatch, revoke(env, post))
    post = run(post)
    media = media_of(post)
    assert media['status'] == 'failed'
    assert media['error'] == reason
    assert_nothing_published(env, post)
    notification = Notification.query.one()
    assert (notification.user_id, notification.subtype) == (env.mod.id, 'media_failed')
    assert post.image_id is None and File.query.count() == 0   # the thumbnail made for it is removed


def test_a_right_lost_before_a_scheduled_upload_is_ready_fails_it_too(env, monkeypatch):
    post = stored_post(env, scheduled_for=utcnow() + timedelta(days=1), timezone='UTC')
    while_probing(monkeypatch, REVOCATIONS['the author is no longer a moderator'][0](env, post))
    post = run(post)
    assert media_of(post)['status'] == 'failed' and post.status == POST_STATUS_PROCESSING


def test_rights_unchanged_while_probing_publish_as_before(env, monkeypatch):
    while_probing(monkeypatch, lambda other: None)   # positive control: the same hook, with nothing revoked
    post = run(stored_post(env))
    assert media_of(post)['status'] == 'ready' and post.status == POST_STATUS_PUBLISHED
    assert env.tasks == [('make_post', {'post_id': post.id})] and env.notified == [post.id]


# ---------------------------------------------------------------- one run per upload


def test_a_second_run_while_the_first_is_probing_does_nothing(env, monkeypatch):
    """Two deliveries of the same task: the second starts while the first is inside ffprobe. It finds the upload
    already claimed and leaves it alone, so the post is probed, published and announced once."""
    post = stored_post(env)
    real, probes, inner = media_tasks.run_ffprobe, [], []

    def probing(path):
        probes.append(path)
        if not inner:
            inner.append(probe_media(False, post.id))   # the second run, in its own session
        return real(path)
    monkeypatch.setattr(media_tasks, 'run_ffprobe', probing)
    post = run(post)

    assert len(probes) == 1 and inner == [None]
    assert media_of(post)['status'] == 'ready' and post.status == POST_STATUS_PUBLISHED
    assert env.tasks == [('make_post', {'post_id': post.id})] and env.notified == [post.id]


def test_a_failure_does_not_leave_the_upload_claimed(env):
    post = run(stored_post(env, 'video_mpeg4.mp4'))
    assert media_of(post)['status'] == 'failed'


def test_a_local_copy_that_cannot_be_removed_after_its_upload_does_not_fail_the_post(env, s3, monkeypatch):
    """The media and its thumbnail are safe in the bucket once uploaded, so a failed unlink of the local copies is
    logged and the post is published; post.url already names the bucket, so the object is not orphaned."""
    post = stored_post(env)
    local = media_local_path(post)
    name = os.path.basename(local)
    real_unlink = os.unlink

    def stuck(path, *args, **kwargs):
        if str(path).startswith('app/static/media/posts/'):
            raise PermissionError(13, 'Permission denied', path)
        return real_unlink(path, *args, **kwargs)
    with monkeypatch.context() as patched:
        patched.setattr(os, 'unlink', stuck)
        post = run(post)

    key = f'posts/{name[0:2]}/{name[2:4]}/{name}'
    assert media_of(post)['status'] == 'ready' and post.status == POST_STATUS_PUBLISHED
    assert post.url == f'https://cdn.probeland.test/{key}'
    assert s3.head_object(Bucket='pyfedi-test', Key=key)['ContentLength'] == media_of(post)['bytes']
    assert post.image.source_url.startswith('https://cdn.probeland.test/posts/')
    assert os.path.exists(local)   # left behind, and logged


def test_the_local_copy_goes_even_when_federating_the_published_post_fails(env, s3, monkeypatch):
    """Once the commit names the bucket the local copy is only a leftover: an error federating or announcing the
    post after that commit must not keep it on disk forever."""
    post = stored_post(env)
    local = media_local_path(post)

    def broker_down(key, **kwargs):
        raise ConnectionError('the broker went away')
    monkeypatch.setattr(media_tasks, 'task_selector', broker_down)
    with pytest.raises(ConnectionError):
        probe_media(False, post.id)
    db.session.expire_all()
    post = db.session.get(Post, post.id)

    assert media_of(post)['status'] == 'ready' and post.status == POST_STATUS_PUBLISHED
    assert post.url.startswith('https://cdn.probeland.test/posts/')
    assert not os.path.exists(local)


def test_an_owner_without_the_moderator_flag_still_publishes(env, monkeypatch):
    """The project's moderator is is_owner OR is_moderator (Community.moderators). An AP Remove can leave an owner
    with is_moderator False; that owner may upload, so the publish-time check must let the post through."""
    post = stored_post(env)

    def owner_only(other):
        member = other.query(CommunityMember).filter_by(user_id=env.mod.id, community_id=post.community_id).one()
        member.is_moderator, member.is_owner = False, True
    while_probing(monkeypatch, owner_only)
    post = run(post)
    assert media_of(post)['status'] == 'ready' and post.status == POST_STATUS_PUBLISHED
    assert env.tasks == [('make_post', {'post_id': post.id})]


# ---------------------------------------------------------------- the stuck-probe sweep (A13.9)


def test_a_claim_stamps_the_time_and_counts_the_attempt(env, monkeypatch):
    post = stored_post(env)

    def broken(path):
        raise RuntimeError('a bug')
    monkeypatch.setattr(media_tasks, 'run_ffprobe', broken)
    for attempt in (1, 2):
        before = utcnow()
        with pytest.raises(RuntimeError):
            probe_media(False, post.id)
        db.session.expire_all()
        media = media_of(db.session.get(Post, post.id))
        assert media['status'] == 'processing' and media['attempts'] == attempt
        assert before <= datetime.fromisoformat(media['probing_since']) <= utcnow()


@pytest.fixture
def sweep(env, monkeypatch):
    queued = []
    monkeypatch.setattr(media_tasks, 'queue_probe', queued.append)
    env.queued = queued
    return env


def stuck(env, status, minutes_ago=None, attempts=1, title='Processing', **media):
    """An upload past its budget unless `minutes_ago` says otherwise: 30 minutes for a claimed probe, 2 hours for one
    still waiting for its claim."""
    if minutes_ago is None:
        minutes_ago = 31 if status == 'probing' else 121
    post = stored_post(env, title=title)
    stamp = (utcnow() - timedelta(minutes=minutes_ago)).isoformat()
    if status == 'probing':
        media.setdefault('probing_since', stamp)
    else:
        post.created_at = utcnow() - timedelta(minutes=minutes_ago)
    with_media(post, status=status, attempts=attempts, **media)
    db.session.commit()
    return post


def swept(post):
    requeue_stuck_media()
    db.session.expire_all()
    return db.session.get(Post, post.id)


def test_a_stale_probe_is_put_back_and_queued_again(sweep):
    post = swept(stuck(sweep, 'probing'))
    media = media_of(post)
    assert media['status'] == 'processing' and media['attempts'] == 1 and media['requeues'] == 1
    assert utcnow() - datetime.fromisoformat(media['requeued_at']) < timedelta(minutes=1)
    assert sweep.queued == [post.id] and post.status == POST_STATUS_PROCESSING


def test_a_probe_still_within_its_time_is_left_alone(sweep):
    post = stuck(sweep, 'probing', minutes_ago=5)
    before = dict(media_of(post))
    post = swept(post)
    assert media_of(post) == before and sweep.queued == []


def test_a_probe_with_an_unreadable_start_time_counts_as_stale(sweep):
    post = swept(stuck(sweep, 'probing', probing_since='not a time'))
    assert media_of(post)['status'] == 'processing' and sweep.queued == [post.id]


def test_an_upload_never_probed_is_queued_again(sweep):
    post = swept(stuck(sweep, 'processing'))
    assert media_of(post)['status'] == 'processing' and sweep.queued == [post.id]
    assert 'requeued_at' in media_of(post) and media_of(post)['requeues'] == 1


def test_an_upload_whose_task_is_lost_before_it_claims_times_out_too(sweep):
    """Only a claim counts an attempt, so a task lost before it claims would be queued again every 2 hours
    forever. The sweep's requeues are counted as well, and the same cap of 3 applies."""
    post = stuck(sweep, 'processing', attempts=0)
    for requeue in (1, 2, 3):
        post = swept(post)
        assert media_of(post)['requeues'] == requeue and media_of(post)['status'] == 'processing'
        with_media(post, requeued_at=(utcnow() - timedelta(minutes=121)).isoformat())   # its task lost again
        db.session.commit()
    post = swept(post)
    assert media_of(post)['status'] == 'failed' and media_of(post)['error'] == 'processing timed out'
    assert sweep.queued == [post.id] * 3 and Notification.query.count() == 1


def test_one_post_the_sweep_cannot_handle_does_not_stop_the_others(sweep, monkeypatch, caplog):
    """The sweep runs inside send-queue's minute: an error on one post is logged and the sweep goes on, so the
    other posts, and the rest of the minute's cron work, still run."""
    first, second = stuck(sweep, 'processing', title='first'), stuck(sweep, 'processing', title='second')

    def queue(post_id):
        if post_id == first.id:
            raise ConnectionError('the broker went away')
        sweep.queued.append(post_id)
    monkeypatch.setattr(media_tasks, 'queue_probe', queue)
    requeue_stuck_media()
    assert sweep.queued == [second.id]
    assert 'the broker went away' in caplog.text


def test_a_post_the_sweep_cannot_reload_after_its_error_is_still_logged(sweep, monkeypatch, caplog):
    """The rollback after an error expires the post, so reading post.id in the handler would go back to the database:
    with the row gone (or the connection dropped) that raises out of the handler and stops the sweep."""
    first, second = stuck(sweep, 'processing', title='first'), stuck(sweep, 'processing', title='second')
    first_id = first.id
    real_sweep = media_tasks._sweep

    def row_gone(post, now):
        if post.id == first_id:
            with Session(bind=db.engine) as other:
                other.execute(text('DELETE FROM post WHERE id = :id'), {'id': first_id})
                other.commit()
            raise ConnectionError('the database went away')
        real_sweep(post, now)
    monkeypatch.setattr(media_tasks, '_sweep', row_gone)
    requeue_stuck_media()
    assert sweep.queued == [second.id]
    assert f'requeue_stuck_media: post {first_id}: ' in caplog.text and 'the database went away' in caplog.text


@pytest.mark.parametrize('case', ['uploaded just now', 'queued again just now'])
def test_an_upload_waiting_its_turn_is_not_queued_twice(sweep, case):
    if case == 'uploaded just now':
        post = stuck(sweep, 'processing', minutes_ago=2)
    else:   # the sweep queued it a minute ago; the queue may simply be long
        post = stuck(sweep, 'processing', requeued_at=(utcnow() - timedelta(minutes=1)).isoformat())
    swept(post)
    assert sweep.queued == []


@pytest.mark.parametrize('status', ['probing', 'processing'])
def test_a_third_lost_attempt_fails_the_upload_and_tells_the_author(sweep, status):
    post = swept(stuck(sweep, status, attempts=3))
    media = media_of(post)
    assert media['status'] == 'failed' and media['error'] == 'processing timed out'
    assert sweep.queued == [] and sweep.tasks == [] and post.status == POST_STATUS_PROCESSING
    notification = Notification.query.one()
    assert (notification.user_id, notification.subtype) == (sweep.mod.id, 'media_failed')


@pytest.mark.parametrize('case', ['deleted', 'failed', 'ready', 'published', 'no media record'])
def test_the_sweep_skips_what_is_not_stuck(sweep, case):
    if case == 'no media record':
        post = make_post(sweep.chan, sweep.mod, ap_id='https://test.piefed.local/post/plain', title='plain')
        post.status, post.created_at = POST_STATUS_PROCESSING, utcnow() - timedelta(hours=1)
    else:
        post = stuck(sweep, 'probing')
        if case == 'deleted':
            post.deleted = True
        elif case == 'published':
            post.status = POST_STATUS_PUBLISHED
        else:
            with_media(post, status=case)
    db.session.commit()
    before = (post.status, dict(post.extensions or {}))
    post = swept(post)
    assert (post.status, dict(post.extensions or {})) == before
    assert sweep.queued == [] and Notification.query.count() == 0


def test_the_per_minute_cron_runs_the_sweep(sweep, monkeypatch):
    calls = []
    monkeypatch.setattr('app.cli.requeue_stuck_media', lambda: calls.append(True))
    monkeypatch.setattr('app.cli.sleep', lambda seconds: None)   # send-queue's random start delay
    cli.register(sweep.app)   # pyfedi.py registers the commands; the test app has none
    result = sweep.app.test_cli_runner().invoke(args=['send-queue'])
    assert result.exception is None, result.exception
    assert calls == [True]


@pytest.mark.parametrize('attempts', [1, 3])
def test_the_sweep_leaves_an_upload_a_live_probe_moved_first(sweep, monkeypatch, attempts):
    """The sweep read the upload as stuck, but a probe claimed or finished it before the sweep's own move: the move
    changes no row, and the sweep neither queues it nor fails it."""
    post = stuck(sweep, 'probing', attempts=attempts)
    monkeypatch.setattr(media_tasks, '_move_media_status', lambda *args, **kwargs: False)
    swept(post)
    assert sweep.queued == [] and Notification.query.count() == 0
    assert media_of(db.session.get(Post, post.id))['status'] == 'probing'


def test_a_failed_release_does_not_hide_the_error_that_caused_it(env, monkeypatch, caplog):
    """The run fails unexpectedly, and then its release of the claim fails too (the database went away, say). The
    release's error is logged; the error raised is still the one that broke the run."""
    post = stored_post(env)
    real_move = media_tasks._move_media_status

    def broken(path):
        raise RuntimeError('a bug')

    def release_fails(post_id, from_status, to_status, **kwargs):
        if from_status == 'probing':
            raise ConnectionError('the database went away')
        return real_move(post_id, from_status, to_status, **kwargs)
    monkeypatch.setattr(media_tasks, 'run_ffprobe', broken)
    monkeypatch.setattr(media_tasks, '_move_media_status', release_fails)
    with pytest.raises(RuntimeError, match='a bug'):
        probe_media(False, post.id)
    assert 'the database went away' in caplog.text


# ---------------------------------------------------------------- the claim token: a stale run writes nothing


def reclaimed_while_probing(monkeypatch, post, then_raise=None):
    """While this run is inside ffprobe, the sweep takes the upload back and a second run claims it: committed from
    another session, the media record now carries the second run's token."""
    real = media_tasks.run_ffprobe

    def probing(path):
        with Session(bind=db.engine) as other:
            row = other.get(Post, post.id)
            row.extensions = {**row.extensions, 'media': {**row.extensions['media'], 'status': 'probing',
                                                          'probing_since': 'the second run'}}
            other.commit()
        if then_raise is not None:
            raise then_raise
        return real(path)
    monkeypatch.setattr(media_tasks, 'run_ffprobe', probing)


def assert_left_to_the_second_run(env, post, media_file):
    media = media_of(post)
    assert (media['status'], media['probing_since']) == ('probing', 'the second run')
    assert post.status == POST_STATUS_PROCESSING and post.image_id is None
    assert env.tasks == [] and env.notified == [] and Notification.query.count() == 0
    assert File.query.count() == 0
    assert files_on_disk() == [media_file]   # its thumbnail is gone; the media is the second run's to probe


def test_a_run_that_lost_its_claim_does_not_publish(env, monkeypatch):
    post = stored_post(env)
    media_file = media_local_path(post)
    reclaimed_while_probing(monkeypatch, post)
    post = run(post)
    assert_left_to_the_second_run(env, post, media_file)
    assert post.url.startswith(env.app.config['SERVER_URL'])


def test_a_run_that_lost_its_claim_does_not_fail_the_post(env, monkeypatch):
    post = stored_post(env, 'video_mpeg4.mp4')
    media_file = media_local_path(post)
    reclaimed_while_probing(monkeypatch, post)
    post = run(post)
    assert_left_to_the_second_run(env, post, media_file)
    assert 'error' not in media_of(post)


def test_a_run_that_lost_its_claim_after_moving_to_s3_removes_only_its_thumbnail(env, s3, monkeypatch):
    """Both runs upload the media under the same key, so the loser leaves that object, and the local file the
    winner may still be reading; it removes the thumbnail object it uploaded under its own name."""
    post = stored_post(env)
    media_file = media_local_path(post)
    name = os.path.basename(media_file)
    reclaimed_while_probing(monkeypatch, post)
    post = run(post)
    assert_left_to_the_second_run(env, post, media_file)
    keys = [o['Key'] for o in s3.list_objects_v2(Bucket='pyfedi-test').get('Contents', [])]
    assert keys == [f'posts/{name[0:2]}/{name[2:4]}/{name}']
    assert post.url.startswith(env.app.config['SERVER_URL'])


def test_a_stale_release_leaves_the_live_claim_alone(env, monkeypatch):
    post = stored_post(env)
    media_file = media_local_path(post)
    reclaimed_while_probing(monkeypatch, post, then_raise=RuntimeError('a bug'))
    with pytest.raises(RuntimeError, match='a bug'):
        probe_media(False, post.id)
    db.session.expire_all()
    assert_left_to_the_second_run(env, db.session.get(Post, post.id), media_file)


# ---------------------------------------------------------------- ruling (final review #3): a failure removes the file


@pytest.mark.parametrize('debug', [True, False])   # debug deletes in line; otherwise the delete is queued
def test_a_failure_after_the_move_to_s3_removes_the_object(env, s3, monkeypatch, debug):
    """The right is lost while probing, after the media is in the bucket: post.url names the object, and the failure
    removes it with the local copy."""
    monkeypatch.setattr(env.app, 'debug', debug)
    post = stored_post(env)
    local = media_local_path(post)
    while_probing(monkeypatch, REVOCATIONS['the author is no longer a moderator'][0](env, post))
    post = run(post)

    assert media_of(post)['status'] == 'failed' and post.url.startswith('https://cdn.probeland.test/posts/')
    assert s3.list_objects_v2(Bucket='pyfedi-test').get('KeyCount') == 0
    assert not os.path.exists(local) and files_on_disk() == []


def test_a_failed_post_removes_its_bucket_object_even_when_a_link_names_it(env, s3, monkeypatch):
    """Security ruling: a post that only links to the URL is not a reference, as for purge (A13.8)."""
    post = stored_post(env)
    other_id = make_post(env.chan, env.mod, ap_id='https://test.piefed.local/post/other', title='Other').id
    db.session.commit()

    def refused_after_a_crosspost(post):
        # the bucket URL, named by a second post before this one fails; db.session is the task's session here
        db.session.get(Post, other_id).url = post.url
        db.session.commit()
        return 'the post was deleted'
    monkeypatch.setattr(media_tasks, 'publish_refusal', refused_after_a_crosspost)
    post = run(post)

    assert media_of(post)['status'] == 'failed' and db.session.get(Post, other_id).url == post.url
    assert s3.list_objects_v2(Bucket='pyfedi-test').get('KeyCount') == 0


def test_a_timed_out_upload_loses_its_file(sweep):
    post = stuck(sweep, 'processing', attempts=3)
    local = media_local_path(post)
    post = swept(post)
    assert media_of(post)['status'] == 'failed' and not os.path.exists(local)


def test_a_failure_whose_file_is_already_gone_is_quiet(env, monkeypatch):
    post = stored_post(env, 'video_mpeg4.mp4')
    real = media_tasks.run_ffprobe

    def gone_meanwhile(path):
        result = real(path)
        os.unlink(path)
        return result
    monkeypatch.setattr(media_tasks, 'run_ffprobe', gone_meanwhile)
    post = run(post)
    assert media_of(post)['status'] == 'failed' and files_on_disk() == []


def test_the_sweep_does_not_load_a_failed_post(sweep, monkeypatch):
    """A failed post stays POST_STATUS_PROCESSING for good; the sweep's query leaves it out in SQL, not after loading."""
    failed = stuck(sweep, 'probing', title='failed')
    with_media(failed, status='failed')
    waiting = stuck(sweep, 'processing', title='waiting')
    db.session.commit()
    loaded = []
    monkeypatch.setattr(media_tasks, '_sweep', lambda post, cutoff: loaded.append(post.id))
    requeue_stuck_media()
    assert loaded == [waiting.id]


# ---------------------------------------------------------------- final review M2: a sticky upload is featured when ready


def test_a_sticky_upload_is_featured_once_it_is_published(env, monkeypatch):
    monkeypatch.setattr(post_module, 'task_selector', lambda key, **kw: env.tasks.append((key, kw)))
    post = run(stored_post(env, sticky=True))

    assert post.status == POST_STATUS_PUBLISHED
    assert env.tasks == [('make_post', {'post_id': post.id}), ('sticky_post', {'user_id': env.mod.id, 'post_id': post.id})]
    assert ModLog.query.filter_by(action='featured_post', post_id=post.id).count() == 1


def test_a_sticky_upload_that_fails_is_never_featured(env, monkeypatch):
    monkeypatch.setattr(post_module, 'task_selector', lambda key, **kw: env.tasks.append((key, kw)))
    post = run(stored_post(env, 'video_mpeg4.mp4', sticky=True))
    assert media_of(post)['status'] == 'failed' and env.tasks == [] and ModLog.query.count() == 0


def test_a_post_not_marked_sticky_is_not_featured(env, monkeypatch):
    monkeypatch.setattr(post_module, 'task_selector', lambda key, **kw: env.tasks.append((key, kw)))
    post = run(stored_post(env))
    assert env.tasks == [('make_post', {'post_id': post.id})] and ModLog.query.count() == 0


# ---------------------------------------------------------------- ruling (final review M3): a backlog is not stuck


@pytest.mark.parametrize('minutes_ago', [31, 119])
def test_an_upload_waiting_for_its_claim_has_two_hours(sweep, minutes_ago):
    """A long background queue is not a lost task: an upload never claimed since its upload, or since its last
    requeue, is queued again only after 2 hours."""
    post = swept(stuck(sweep, 'processing', minutes_ago=minutes_ago, attempts=0))
    assert sweep.queued == [] and 'requeues' not in media_of(post)


def test_a_requeued_upload_waiting_for_its_claim_has_two_hours_again(sweep):
    post = stuck(sweep, 'processing', requeued_at=(utcnow() - timedelta(minutes=31)).isoformat())
    swept(post)
    assert sweep.queued == []


def test_a_claimed_probe_gone_stale_keeps_thirty_minutes(sweep):
    post = swept(stuck(sweep, 'probing', minutes_ago=31))
    assert media_of(post)['status'] == 'processing' and sweep.queued == [post.id]
