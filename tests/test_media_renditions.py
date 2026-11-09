"""W2.1b (spec 2026-11-09-peertube-read-api-design §5, ruling V3): a video upload, once ready, gains H.264/AAC copies at
480p and 360p, each only below the source's short side. They are recorded in the media record, listed by PeerTube's
API and the federated Video, and removed with their post. A failed copy is dropped; the post never waits for them.

The tests run the real ffmpeg on the 1 s fixtures, from a private static tree (tests/test_media_probe.py's `env`)."""
import os
import pytest
from sqlalchemy import text

from app import db
from app.activitypub.serializers.peertube import peertube_video
from app.constants import POST_STATUS_PUBLISHED
from app.models import Post, rendition_name, renditions_of
from app.peertube import entities
from app.shared import ffmpeg
from app.shared.ffmpeg import MediaToolError, run_ffprobe
from app.shared.media import media_local_path, media_of
from app.shared.tasks import media as media_tasks
from app.shared.tasks import task_selector
from app.shared.tasks.media import make_renditions, rendition_sides
from tests import test_media_probe as probe_tests
from tests.test_media_probe import files_on_disk, run, stored_post

env, s3 = probe_tests.env, probe_tests.s3   # its fixtures: a private static tree, and a moto bucket


def ready(env, fixture='video_720p.mp4', **columns):
    """A probed, published upload of `fixture`, with the tasks the probe asked for cleared."""
    post = run(stored_post(env, fixture, **columns))
    assert media_of(post)['status'] == 'ready' and post.status == POST_STATUS_PUBLISHED
    env.tasks.clear()
    return post


def render(post):
    post_id = post.id
    make_renditions(False, post_id)
    db.session.expire_all()
    return db.session.get(Post, post_id)


def streams(path):
    return {stream['codec_type']: stream for stream in run_ffprobe(path)['streams']}


# which sizes

@pytest.mark.parametrize('width, height, sides', [
    (1280, 720, [480, 360]), (854, 480, [360]), (480, 854, [360]), (640, 360, []), (64, 64, []), (3840, 2160, [480, 360]),
])
def test_only_sizes_below_the_short_side_are_made(width, height, sides):
    assert rendition_sides({'width': width, 'height': height}) == sides


def test_a_ready_video_with_room_for_a_smaller_size_is_queued_for_renditions(env):
    post = run(stored_post(env, 'video_720p.mp4'))
    assert ('make_renditions', {'post_id': post.id}) in env.tasks


def test_a_video_too_small_for_any_rendition_is_not_queued(env):
    post = run(stored_post(env))   # 64x64
    assert [key for key, _ in env.tasks] == ['make_post']
    assert 'renditions' not in media_of(post)


def test_make_renditions_is_registered_with_task_selector(env, monkeypatch):
    seen = []
    monkeypatch.setattr(media_tasks.make_renditions, 'delay', lambda **kw: seen.append(kw))
    task_selector('make_renditions', post_id=5)
    assert seen == [{'send_async': True, 'post_id': 5}]


# the copies

def test_a_720p_upload_gains_480p_and_360p_mp4s_beside_it(env):
    post = ready(env)
    source = media_local_path(post)
    post = render(post)

    assert renditions_of(post) == [
        {'side': 480, 'width': 854, 'height': 480, 'bytes': os.path.getsize(rendition_name(source, 480))},
        {'side': 360, 'width': 640, 'height': 360, 'bytes': os.path.getsize(rendition_name(source, 360))}]
    for side in (480, 360):
        found = streams(rendition_name(source, side))
        assert found['video']['codec_name'] == 'h264' and found['audio']['codec_name'] == 'aac'
        assert found['video']['height'] == side
    assert rendition_name(source, 480) == source[:-len('.mp4')] + '_480p.mp4'
    assert os.listdir('app/static/tmp') == []
    assert env.tasks == [('edit_post', {'post_id': post.id})]   # the published Video is updated with its new files


def test_a_portrait_video_is_scaled_by_its_width_and_needs_no_audio(env):
    post = render(ready(env, 'video_portrait.mp4', title='Portrait'))
    assert renditions_of(post) == [{'side': 360, 'width': 360, 'height': 640,
                                    'bytes': renditions_of(post)[0]['bytes']}]
    assert 'audio' not in streams(rendition_name(media_local_path(post), 360))


def test_a_second_run_leaves_the_recorded_renditions_alone(env, monkeypatch):
    post = render(ready(env))
    recorded = renditions_of(post)
    env.tasks.clear()
    monkeypatch.setattr(media_tasks, 'transcode', lambda *a, **k: pytest.fail('transcoded again'))
    post = render(post)
    assert renditions_of(post) == recorded and env.tasks == []


def test_a_failed_size_is_dropped_and_the_others_kept(env, monkeypatch, caplog):
    real = media_tasks.transcode

    def flaky(path, out_path, side, portrait, timeout_s):
        if side == 480:
            with open(out_path, 'wb') as half:
                half.write(b'half')
            raise MediaToolError('ffmpeg failed: boom')
        real(path, out_path, side, portrait, timeout_s)
    monkeypatch.setattr(media_tasks, 'transcode', flaky)
    post = ready(env)
    source = media_local_path(post)
    post = render(post)
    assert [entry['side'] for entry in renditions_of(post)] == [360]
    assert not os.path.exists(rendition_name(source, 480))
    assert 'boom' in caplog.text


def test_when_every_size_fails_the_post_records_none_and_is_not_updated(env, monkeypatch):
    def broken(path, out_path, side, portrait, timeout_s):
        raise MediaToolError('ffmpeg failed')
    monkeypatch.setattr(media_tasks, 'transcode', broken)
    post = render(ready(env))
    assert media_of(post)['renditions'] == [] and env.tasks == []
    assert media_of(post)['status'] == 'ready' and post.status == POST_STATUS_PUBLISHED


def test_the_transcode_time_limit_grows_with_the_video_and_is_capped():
    assert ffmpeg.transcode_timeout(0) == ffmpeg.TRANSCODE_BASE_S
    assert ffmpeg.transcode_timeout(100) == ffmpeg.TRANSCODE_BASE_S + 100 * ffmpeg.TRANSCODE_PER_SECOND
    assert ffmpeg.transcode_timeout(10 ** 9) == ffmpeg.TRANSCODE_MAX_S


@pytest.mark.parametrize('case', ['missing', 'not ready', 'audio', 'remote', 'small', 'source gone'])
def test_a_post_with_nothing_to_render_is_left_alone(env, case):
    if case == 'missing':
        make_renditions(False, 987654321)
        return
    post = ready(env, 'video_h264.mp4' if case == 'small' else 'video_720p.mp4')
    if case == 'not ready':
        media_tasks.with_media(post, status='failed')
    elif case == 'audio':
        media_tasks.with_media(post, mime='audio/mpeg')
    elif case == 'remote':
        post.ap_id = 'https://elsewhere.example/post/1'   # is_local reads the ap_id
    elif case == 'source gone':
        os.unlink(media_local_path(post))
    db.session.commit()
    before = files_on_disk()
    post = render(post)
    assert renditions_of(post) == [] and env.tasks == []
    if case != 'source gone':
        assert files_on_disk() == before


def test_a_post_whose_media_changed_while_transcoding_keeps_nothing(env, monkeypatch):
    real = media_tasks.transcode
    post = ready(env)
    source, post_id = media_local_path(post), post.id

    def then_fail_it(path, out_path, side, portrait, timeout_s):
        real(path, out_path, side, portrait, timeout_s)
        db.session.execute(text("UPDATE post SET extensions = jsonb_set(extensions::jsonb, '{media,status}', "
                                "'\"failed\"')::json WHERE id = :id"), {'id': post_id})
    monkeypatch.setattr(media_tasks, 'transcode', then_fail_it)
    post = render(post)
    assert 'renditions' not in media_of(post)
    assert not any(os.path.exists(rendition_name(source, side)) for side in (480, 360))


# where they appear

def test_peertubes_api_lists_each_rendition_as_a_file(env):
    post = render(ready(env))
    with env.app.test_request_context():
        listed = entities.files(post)
    assert [(entry['resolution']['label'], entry['size']) for entry in listed] == [
        ('720p', media_of(post)['bytes']), ('480p', renditions_of(post)[0]['bytes']),
        ('360p', renditions_of(post)[1]['bytes'])]
    assert listed[1]['fileUrl'].endswith('_480p.mp4') and listed[1]['torrentUrl'] == ''
    assert listed[1]['fps'] == media_of(post)['fps']


def test_the_federated_video_links_each_rendition(env):
    post = render(ready(env))
    links = [link for link in peertube_video(post)['url'] if link['mediaType'] == 'video/mp4']
    assert [(link['height'], link['width']) for link in links] == [(720, 1280), (480, 854), (360, 640)]
    assert links[2]['href'] == rendition_name(post.url, 360) and links[2]['size'] == renditions_of(post)[1]['bytes']


def test_a_rendition_record_a_peer_could_not_write_is_still_checked(env):
    post = ready(env)
    media_tasks.with_media(post, renditions=[{'side': 720, 'width': 1, 'height': 1, 'bytes': 1}, 'x',
                                             {'side': 360, 'width': '640', 'height': 360, 'bytes': 1},
                                             {'side': 360, 'width': 640, 'height': 360, 'bytes': 9}])
    assert renditions_of(post) == [{'side': 360, 'width': 640, 'height': 360, 'bytes': 9}]


# removal

def test_purging_the_post_removes_its_renditions(env):
    post = render(ready(env))
    source = media_local_path(post)
    post.delete_dependencies()
    db.session.commit()
    assert not any(os.path.exists(rendition_name(source, side)) for side in (480, 360))
    assert not os.path.exists(source)


# S3

def test_with_s3_the_renditions_go_to_the_bucket_beside_the_source(env, s3):
    post = ready(env)
    assert media_local_path(post) is None   # the probe moved the source into the bucket
    post = render(post)
    key = post.url.removeprefix('https://cdn.probeland.test/')
    for entry in renditions_of(post):
        head = s3.head_object(Bucket='pyfedi-test', Key=rendition_name(key, entry['side']))
        assert head['ContentType'] == 'video/mp4' and head['ContentLength'] == entry['bytes']
    assert [entry['side'] for entry in renditions_of(post)] == [480, 360]
    assert files_on_disk() == []   # the downloaded source and the copies are gone from disk


def test_with_s3_purging_the_post_deletes_its_renditions_from_the_bucket(env, s3):
    post = render(ready(env))
    key = post.url.removeprefix('https://cdn.probeland.test/')
    post.delete_dependencies()
    db.session.commit()
    for name in (key, rendition_name(key, 480), rendition_name(key, 360)):
        with pytest.raises(Exception):
            s3.head_object(Bucket='pyfedi-test', Key=name)


def test_with_s3_a_rendition_the_bucket_refuses_is_dropped(env, s3, monkeypatch):
    real = media_tasks.upload_file_to_s3

    def refuse_480(local, key, mime):
        if key.endswith('_480p.mp4'):
            raise OSError('refused')
        return real(local, key, mime)
    post = ready(env)
    monkeypatch.setattr(media_tasks, 'upload_file_to_s3', refuse_480)
    post = render(post)
    assert [entry['side'] for entry in renditions_of(post)] == [360]
    assert files_on_disk() == []


# what goes wrong

def test_an_error_the_task_does_not_expect_is_rolled_back_and_raised(env, monkeypatch):
    def broken(*args):
        raise RuntimeError('unexpected')
    monkeypatch.setattr(media_tasks, 'transcode', broken)
    post = ready(env)
    with pytest.raises(RuntimeError):
        make_renditions(False, post.id)
    db.session.expire_all()
    assert 'renditions' not in media_of(db.session.get(Post, post.id))


def test_a_rendition_ffprobe_finds_no_picture_in_is_dropped(env, monkeypatch):
    post = ready(env)
    monkeypatch.setattr(media_tasks, 'run_ffprobe', lambda path: {'streams': [{'codec_type': 'audio'}, 'x']})
    source = media_local_path(post)
    post = render(post)
    assert media_of(post)['renditions'] == []
    assert not any(os.path.exists(rendition_name(source, side)) for side in (480, 360))


def test_with_s3_a_source_the_bucket_will_not_hand_back_makes_nothing(env, s3, monkeypatch):
    def refuse(key, local_path):
        with open(local_path, 'wb') as half:
            half.write(b'half')
        raise OSError('refused')
    post = ready(env)
    monkeypatch.setattr(media_tasks, 'download_from_s3', refuse)
    post = render(post)
    assert 'renditions' not in media_of(post) and files_on_disk() == []


def test_renditions_of_a_post_gone_meanwhile_are_removed(env):
    made = []
    for side in (480, 360):
        path = f'app/static/media/posts/zz/zz/zzzz{side:011d}_{side}p.mp4'
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as copy:
            copy.write(b'copy')
        made.append((side, 1, 1, 4, path))
    media_tasks._record_renditions(987654321, made, in_s3=False)
    assert not any(os.path.exists(where) for *_, where in made)


def test_with_s3_renditions_of_a_post_gone_meanwhile_leave_the_bucket(env, s3):
    s3.put_object(Bucket='pyfedi-test', Key='posts/zz/zz/gone_480p.mp4', Body=b'copy')
    media_tasks._record_renditions(987654321, [(480, 1, 1, 4, 'posts/zz/zz/gone_480p.mp4')], in_s3=True)
    with pytest.raises(Exception):
        s3.head_object(Bucket='pyfedi-test', Key='posts/zz/zz/gone_480p.mp4')
    media_tasks._record_renditions(987654321, [], in_s3=True)   # nothing made: nothing to delete


def test_a_record_another_run_wrote_first_is_kept_with_its_files(env):
    post = render(ready(env))
    recorded, source = renditions_of(post), media_local_path(post)
    made = [(480, 1, 1, 4, rendition_name(source, 480))]
    media_tasks._record_renditions(post.id, made, in_s3=False)
    db.session.expire_all()
    assert renditions_of(db.session.get(Post, post.id)) == recorded
    assert os.path.exists(rendition_name(source, 480))


def test_a_rendition_link_of_a_video_with_no_frame_rate_has_none(env):
    post = render(ready(env))
    media_tasks.with_media(post, fps=None)
    links = [link for link in peertube_video(post)['url'] if link['mediaType'] == 'video/mp4']
    assert all('fps' not in link for link in links[1:])
