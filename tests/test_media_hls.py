"""Videos as HLS streams too (owner rulings H4, HL1-HL3, 2026-11-12): while the admin offers HLS (off by default), a
ready video upload, once its renditions are made, gains HLS copies: each MP4 it has (the upload itself when its codecs
play in HLS, then each rendition) repackaged, not encoded again, into one fragmented MP4 and its playlist, under
`<name>_hls/` beside it with a master playlist, on disk or in the bucket. PeerTube's API lists the master as the
video's streaming playlist while HLS is offered. Turning HLS on affects new uploads only; the admin's backfill makes
the rest. Turning it off stops listing them and keeps the files; the admin's delete removes them all.

The tests run the real ffmpeg on the 1 s fixtures, from a private static tree (tests/test_media_probe.py's `env`)."""
import os

import pytest

from app import db
from app.models import HLS_SETTING, Post, hls_directory, hls_master, hls_names, hls_of
from app.peertube import entities
from app.shared import ffmpeg
from app.shared.ffmpeg import MediaToolError
from app.shared.media import media_local_path, media_of, with_media
from app.shared.tasks import media as media_tasks
from app.shared.tasks import task_selector
from app.shared.tasks.media import backfill_hls, delete_all_hls, make_hls, wants_hls
from app.utils import set_setting
from tests import test_media_probe as probe_tests
from tests.test_media_probe import run, stored_post
from tests.test_media_renditions import ready, render

env, s3 = probe_tests.env, probe_tests.s3   # its fixtures: a private static tree, and a moto bucket

FIX = probe_tests.FIX


@pytest.fixture
def hls_on(env):
    set_setting(HLS_SETTING, True)
    return env


def packaged(post):
    post_id = post.id
    make_hls(False, post_id)
    db.session.expire_all()
    return db.session.get(Post, post_id)


def rendered(env, fixture='video_720p.mp4', **columns):
    post = render(ready(env, fixture, **columns))
    env.tasks.clear()
    return post


# names and records

def test_the_names_are_beside_the_upload_and_derived_from_it():
    assert hls_directory('posts/ab/cd/abcdABCD1234567.mp4') == 'posts/ab/cd/abcdABCD1234567_hls'
    assert hls_master('https://x/a/b.mp4') == 'https://x/a/b_hls/master.m3u8'
    assert hls_names('a/b.mp4') == ['a/b_hls/master.m3u8', 'a/b_hls/source.m3u8', 'a/b_hls/source.mp4',
                                    'a/b_hls/480p.m3u8', 'a/b_hls/480p.mp4', 'a/b_hls/360p.m3u8', 'a/b_hls/360p.mp4']


@pytest.mark.parametrize('entry', [
    'not a dict', {'name': 'bogus', 'width': 1, 'height': 1, 'bandwidth': 1, 'bytes': 1},
    {'name': '480p', 'width': 0, 'height': 1, 'bandwidth': 1, 'bytes': 1},
    {'name': '480p', 'width': True, 'height': 1, 'bandwidth': 1, 'bytes': 1},
    {'name': '480p', 'width': 1, 'height': 1, 'bandwidth': 1},
], ids=['not-a-dict', 'unknown-name', 'zero', 'bool', 'missing-key'])
def test_only_well_formed_variants_are_read(env, entry):
    post = rendered(env)
    good = {'name': '360p', 'width': 640, 'height': 360, 'bandwidth': 8, 'bytes': 1, 'extra': 'dropped'}
    with_media(post, hls=[entry, good, dict(good, bytes=2)])
    assert hls_of(post) == [{'name': '360p', 'width': 640, 'height': 360, 'bandwidth': 8, 'bytes': 2}]
    with_media(post, hls='not a list')
    assert hls_of(post) == []


# when

def test_hls_is_off_by_default(env):
    post = rendered(env)
    assert not wants_hls(post)
    assert 'make_hls' not in [key for key, _ in env.tasks]


def test_with_hls_on_a_video_is_queued_once_its_renditions_are_recorded(hls_on):
    post = ready(hls_on)
    assert not wants_hls(post)   # its renditions come first
    render(post)
    assert ('make_hls', {'post_id': post.id}) in hls_on.tasks


def test_with_hls_on_a_video_too_small_for_renditions_is_queued_when_it_is_ready(hls_on):
    post = run(stored_post(hls_on))   # 64x64: no rendition is due
    assert ('make_hls', {'post_id': post.id}) in hls_on.tasks


def test_a_remote_video_or_one_already_packaged_is_not_wanted(hls_on):
    post = rendered(hls_on)
    with_media(post, hls=[])
    assert not wants_hls(post)
    assert not wants_hls(None)


def test_make_hls_and_the_admin_jobs_are_registered_with_task_selector(monkeypatch):
    seen = []
    for name in ('make_hls', 'backfill_hls', 'delete_all_hls'):
        monkeypatch.setattr(getattr(media_tasks, name), 'delay', lambda name=name, **kw: seen.append((name, kw)))
    task_selector('make_hls', post_id=5)
    task_selector('backfill_hls')
    task_selector('delete_all_hls')
    assert seen == [('make_hls', {'send_async': True, 'post_id': 5}), ('backfill_hls', {'send_async': True}),
                    ('delete_all_hls', {'send_async': True})]


# the copies

def test_a_720p_upload_gains_a_master_and_a_variant_for_itself_and_each_rendition(hls_on):
    post = rendered(hls_on)
    source = media_local_path(post)
    post = packaged(post)

    assert [variant['name'] for variant in hls_of(post)] == ['source', '480p', '360p']
    directory = hls_directory(source)
    assert sorted(os.listdir(directory)) == ['360p.m3u8', '360p.mp4', '480p.m3u8', '480p.mp4', 'master.m3u8',
                                             'source.m3u8', 'source.mp4']
    master = open(f'{directory}/master.m3u8').read()
    assert master.startswith('#EXTM3U') and master.index('source.m3u8') < master.index('480p.m3u8') < \
        master.index('360p.m3u8')
    assert 'RESOLUTION=1280x720' in master and 'BANDWIDTH=' in master
    playlist = open(f'{directory}/480p.m3u8').read()
    assert '#EXT-X-PLAYLIST-TYPE:VOD' in playlist and '#EXT-X-BYTERANGE' in playlist and '480p.mp4' in playlist
    for variant in hls_of(post):
        assert variant['bytes'] == os.path.getsize(f"{directory}/{variant['name']}.mp4")


def test_a_variant_plays_the_same_streams_as_its_mp4(hls_on):
    post = packaged(rendered(hls_on))
    found = ffmpeg.run_ffprobe(f"{hls_directory(media_local_path(post))}/source.mp4")
    assert {stream['codec_name'] for stream in found['streams']} >= {'h264'}


def test_an_upload_whose_codecs_hls_cannot_carry_is_offered_by_its_renditions_alone(hls_on, monkeypatch):
    monkeypatch.setattr(media_tasks, '_plays_in_hls', lambda path: False)
    post = packaged(rendered(hls_on))
    assert [variant['name'] for variant in hls_of(post)] == ['480p', '360p']


@pytest.mark.parametrize('fixture, plays', [('video_h264.mp4', True), ('audio_opus.ogg', False)],
                         ids=['h264', 'no-video'])
def test_what_plays_in_hls(fixture, plays):
    assert media_tasks._plays_in_hls(os.path.join(FIX, fixture)) is plays


def test_a_variant_that_fails_is_dropped_and_the_rest_kept(hls_on, monkeypatch):
    real = ffmpeg.hls_package

    def failing(path, playlist, media, timeout_s):
        if playlist.endswith('480p.m3u8'):
            open(playlist, 'w').close()
            raise MediaToolError('no 480p')
        real(path, playlist, media, timeout_s)
    monkeypatch.setattr(media_tasks, 'hls_package', failing)
    post = packaged(rendered(hls_on))
    assert [variant['name'] for variant in hls_of(post)] == ['source', '360p']
    assert not os.path.exists(f'{hls_directory(media_local_path(post))}/480p.m3u8')


def test_when_every_variant_fails_nothing_is_offered_and_it_is_not_tried_again(hls_on, monkeypatch):
    monkeypatch.setattr(media_tasks, 'hls_package', lambda *args: (_ for _ in ()).throw(MediaToolError('no')))
    post = packaged(rendered(hls_on))
    assert media_of(post)['hls'] == [] and not wants_hls(post)
    assert not os.path.exists(hls_master(media_local_path(post)))


def test_a_missing_source_or_rendition_is_skipped(hls_on):
    post = rendered(hls_on)
    os.unlink(media_local_path(post).replace('.mp4', '_480p.mp4'))
    post = packaged(post)
    assert [variant['name'] for variant in hls_of(post)] == ['source', '360p']


def test_a_rendition_without_dimensions_is_not_packaged(hls_on, monkeypatch):
    post = rendered(hls_on)
    monkeypatch.setattr(media_tasks, 'renditions_of', lambda post: [{'side': 480, 'width': None, 'height': 480}])
    post = packaged(post)
    assert [variant['name'] for variant in hls_of(post)] == ['source']


def test_a_video_with_no_stored_file_is_left_alone(hls_on, monkeypatch):
    post = rendered(hls_on)
    monkeypatch.setattr(media_tasks, 'media_local_path', lambda post: None)
    post = packaged(post)
    assert 'hls' not in media_of(post)


def test_a_second_run_changes_nothing(hls_on):
    post = packaged(rendered(hls_on))
    before = media_of(post)['hls']
    post = packaged(post)
    assert media_of(post)['hls'] == before


def test_a_run_that_finds_one_recorded_meanwhile_keeps_that_one(hls_on, monkeypatch):
    post = rendered(hls_on)
    real = media_tasks._write_master

    def meanwhile(path, made):
        real(path, made)
        theirs = db.session.get(Post, post.id)
        with_media(theirs, hls=[{'name': '360p', 'width': 640, 'height': 360, 'bandwidth': 1, 'bytes': 1}])
        db.session.commit()
    monkeypatch.setattr(media_tasks, '_write_master', meanwhile)
    post = packaged(post)
    assert [variant['name'] for variant in hls_of(post)] == ['360p']


def test_a_post_gone_meanwhile_keeps_no_copies(hls_on, monkeypatch):
    post = rendered(hls_on)
    source = media_local_path(post)
    real = media_tasks._write_master

    def gone(path, made):
        real(path, made)
        theirs = db.session.get(Post, post.id)
        with_media(theirs, status='failed')
        db.session.commit()
    monkeypatch.setattr(media_tasks, '_write_master', gone)
    post = packaged(post)
    assert 'hls' not in media_of(post) and not os.path.exists(hls_directory(source))


def test_ffmpeg_writing_nothing_is_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(ffmpeg, '_run', lambda *args: b'')
    with pytest.raises(MediaToolError, match='no HLS variant'):
        ffmpeg.hls_package(os.path.join(FIX, 'video_h264.mp4'), str(tmp_path / 'a.m3u8'), str(tmp_path / 'a.mp4'), 5)


# in the bucket

def bucket_keys(s3):
    return {item['Key'] for item in s3.list_objects_v2(Bucket='pyfedi-test').get('Contents', [])}


def test_with_s3_the_copies_go_into_the_bucket_beside_the_upload(hls_on, s3):
    post = rendered(hls_on)
    post = packaged(post)
    key = post.url.split('cdn.probeland.test/', 1)[1]
    prefix = hls_directory(key)
    assert {name for name in bucket_keys(s3) if name.startswith(prefix)} == {
        f'{prefix}/{name}' for name in ('master.m3u8', 'source.m3u8', 'source.mp4', '480p.m3u8', '480p.mp4',
                                        '360p.m3u8', '360p.mp4')}
    assert s3.head_object(Bucket='pyfedi-test', Key=f'{prefix}/master.m3u8')['ContentType'] == \
        'application/vnd.apple.mpegurl'
    assert not [name for name in os.listdir('app/static/tmp') if name.endswith('_hls')]
    assert [variant['name'] for variant in hls_of(post)] == ['source', '480p', '360p']


def test_with_s3_a_failed_store_keeps_nothing(hls_on, s3, monkeypatch):
    post = rendered(hls_on)
    calls = []

    def failing(path, key, mime):
        calls.append(key)
        if len(calls) == 2:
            raise OSError('the bucket went away')
        return real(path, key, mime)
    real = media_tasks.upload_file_to_s3
    monkeypatch.setattr(media_tasks, 'upload_file_to_s3', failing)
    post = packaged(post)
    assert media_of(post)['hls'] == []
    key = post.url.split('cdn.probeland.test/', 1)[1]
    assert not [name for name in bucket_keys(s3) if name.startswith(hls_directory(key))]


def test_with_s3_a_post_gone_meanwhile_leaves_nothing_in_the_bucket(hls_on, s3, monkeypatch):
    post = rendered(hls_on)
    key = post.url.split('cdn.probeland.test/', 1)[1]
    real = media_tasks._upload_hls

    def gone(*args):
        made = real(*args)
        theirs = db.session.get(Post, post.id)
        with_media(theirs, status='failed')
        db.session.commit()
        return made
    monkeypatch.setattr(media_tasks, '_upload_hls', gone)
    packaged(post)
    assert not [name for name in bucket_keys(s3) if name.startswith(hls_directory(key))]


# deleting

def test_a_deleted_post_takes_its_hls_copies(hls_on):
    post = packaged(rendered(hls_on))
    source = media_local_path(post)
    post.delete_dependencies()
    assert not os.path.exists(hls_directory(source))


def test_with_s3_a_deleted_post_takes_its_hls_copies_from_the_bucket(hls_on, s3):
    post = packaged(rendered(hls_on))
    key = post.url.split('cdn.probeland.test/', 1)[1]
    hls_on.app.debug, debug = True, hls_on.app.debug   # delete_from_s3 inline
    try:
        post.delete_dependencies()
    finally:
        hls_on.app.debug = debug
    assert not [name for name in bucket_keys(s3) if name.startswith(hls_directory(key))]


# PeerTube's API

def test_the_master_is_the_streaming_playlist_while_hls_is_offered(hls_on):
    post = packaged(rendered(hls_on))
    with hls_on.app.test_request_context():
        found = entities.video(post, whole=True)['streamingPlaylists']
        assert len(found) == 1 and found[0]['type'] == 1
        assert found[0]['playlistUrl'].endswith(hls_master(post.url).split('/static', 1)[1])
        set_setting(HLS_SETTING, False)   # off: not offered, the files kept
        assert entities.video(post, whole=True)['streamingPlaylists'] == []
    assert os.path.exists(hls_master(media_local_path(post)))


def test_a_video_without_copies_has_no_streaming_playlist(hls_on):
    post = rendered(hls_on)
    with hls_on.app.test_request_context():
        assert entities.video(post, whole=True)['streamingPlaylists'] == []


# the admin's jobs

def test_the_backfill_queues_every_video_that_wants_hls(hls_on):
    first, second = rendered(hls_on), packaged(rendered(hls_on, title='Second'))
    hls_on.tasks.clear()
    backfill_hls(False)
    assert hls_on.tasks == [('make_hls', {'post_id': first.id})]
    assert second.id not in [kw['post_id'] for _, kw in hls_on.tasks]


def test_delete_all_removes_every_copy_and_its_record(hls_on):
    post = packaged(rendered(hls_on))
    source = media_local_path(post)
    delete_all_hls(False)
    db.session.expire_all()
    post = db.session.get(Post, post.id)
    assert 'hls' not in media_of(post) and not os.path.exists(hls_directory(source))
    assert wants_hls(post)   # a backfill makes them again


def test_delete_all_with_s3_empties_the_bucket_of_copies(hls_on, s3):
    post = packaged(rendered(hls_on))
    key = post.url.split('cdn.probeland.test/', 1)[1]
    delete_all_hls(False)
    assert not [name for name in bucket_keys(s3) if name.startswith(hls_directory(key))]


@pytest.mark.parametrize('job', [backfill_hls, delete_all_hls], ids=['backfill', 'delete'])
def test_a_failing_job_rolls_back(hls_on, monkeypatch, job):
    monkeypatch.setattr(media_tasks, '_video_ids', lambda: (_ for _ in ()).throw(RuntimeError('boom')))
    with pytest.raises(RuntimeError):
        job(False)


def test_a_failing_make_hls_rolls_back(hls_on, monkeypatch):
    monkeypatch.setattr(media_tasks, '_make_hls', lambda post_id: (_ for _ in ()).throw(RuntimeError('boom')))
    with pytest.raises(RuntimeError):
        make_hls(False, 1)
