"""Step 3, Task 6: purging a media post removes its file, locally or in S3 (A13.8); a soft delete keeps it."""
import os

from types import SimpleNamespace

import pytest

from app import db
from app.constants import POST_TYPE_LINK, POST_TYPE_VIDEO
from app.models import Post
from app.shared.media import media_local_path
from app.shared import post as post_module
from app.shared.post import delete_post
from tests.factories import make_instance, make_post, make_user
from tests.test_media_probe import env, files_on_disk, run, s3, stored_post  # noqa: F401  (fixtures and helpers)

CDN = 'https://cdn.probeland.test/'


def purge(post):
    post.delete_dependencies()
    db.session.delete(post)
    db.session.commit()


@pytest.mark.parametrize('fixture, kind', [('video_h264.mp4', 'video'), ('audio_cover.mp3', 'audio')])
def test_purging_a_local_media_post_removes_the_file_and_the_thumbnail(env, fixture, kind):
    post = run(stored_post(env, fixture, kind))
    assert os.path.isfile(media_local_path(post)) and len(files_on_disk()) >= 2
    purge(post)
    assert files_on_disk() == []


def test_purging_a_local_audio_post_without_art_removes_the_file(env):
    post = run(stored_post(env, 'audio.mp3', 'audio'))
    assert len(files_on_disk()) == 1
    purge(post)
    assert files_on_disk() == []


def test_purging_a_media_post_whose_file_is_already_gone_is_quiet(env):
    post = run(stored_post(env))
    os.unlink(media_local_path(post))
    purge(post)
    assert files_on_disk() == []


@pytest.mark.parametrize('fixture, kind', [('video_h264.mp4', 'video'), ('audio.mp3', 'audio')])
def test_purging_an_s3_media_post_removes_the_object(env, s3, fixture, kind):
    post = run(stored_post(env, fixture, kind))
    key = post.url.removeprefix(CDN)
    s3.head_object(Bucket='pyfedi-test', Key=key)
    purge(post)
    keys = [o['Key'] for o in s3.list_objects_v2(Bucket='pyfedi-test').get('Contents', [])]
    assert key not in keys


def test_a_soft_delete_keeps_the_files(env, monkeypatch):
    monkeypatch.setattr(post_module, 'current_user', SimpleNamespace(id=env.mod.id))
    post = run(stored_post(env))
    before = files_on_disk()
    delete_post(post.id, False, None, None)
    assert files_on_disk() == before


def test_a_link_post_naming_a_traversal_deletes_nothing_outside(env, app):
    post = run(stored_post(env))
    victim = os.path.join('app', 'static', 'x')
    with open(victim, 'w') as f:
        f.write('keep')
    other = stored_post(env, title='Other')
    other_file = media_local_path(other)
    post.type = POST_TYPE_LINK
    post.url = f"{app.config['SERVER_URL']}/static/media/posts/../../x"
    db.session.commit()
    purge(post)
    assert os.path.isfile(victim) and os.path.isfile(other_file)


def test_a_link_post_naming_another_posts_media_leaves_it(env, app):
    post = stored_post(env, title='Real')
    run(post)
    real_file = media_local_path(Post.query.filter_by(title='Real').one())
    link = Post.query.filter(Post.title != 'Real').first() or stored_post(env, title='Link')
    link.extensions = None
    link.url = Post.query.filter_by(title='Real').one().url
    db.session.commit()
    purge(link)
    assert os.path.isfile(real_file)



@pytest.mark.parametrize('origin', ['local', 'remote'])
def test_the_owning_posts_purge_removes_its_local_media_a_link_names(env, origin):
    """Security ruling (content removal): a post that only links to the media is not a reference. Anyone can make one,
    a remote post included, so letting it keep the file would let anyone stop a takedown. The owner's purge removes
    the file, and the link post becomes a dead link."""
    post = run(stored_post(env))
    media_file = media_local_path(post)
    if origin == 'remote':
        peer = make_instance('peer.example')
        author = make_user(peer, 'linker')
        linker = make_post(env.cast, author, ap_id='https://peer.example/post/1', title='Link')
        linker.instance_id = peer.id
    else:
        linker = make_post(env.cast, env.mod, ap_id='https://test.piefed.local/post/cross', title='Link')
    linker.type, linker.url = POST_TYPE_LINK, post.url
    db.session.commit()

    purge(post)

    assert not os.path.exists(media_file)


def test_the_owning_posts_purge_removes_its_s3_media_a_link_names(env, s3):
    post = run(stored_post(env))
    key = post.url.removeprefix(CDN)
    linker = make_post(env.cast, env.mod, ap_id='https://test.piefed.local/post/cross', title='Link')
    linker.type, linker.url = POST_TYPE_LINK, post.url
    db.session.commit()

    purge(post)

    assert key not in [o['Key'] for o in s3.list_objects_v2(Bucket='pyfedi-test').get('Contents', [])]


def test_a_legacy_video_post_keeps_an_s3_object_another_post_names(env, s3):
    """A video post with no media record (a federated video mirrored into the bucket) owns nothing: up to ten
    cross-posts name one object, and purging one of them leaves it (D1343)."""
    key = 'posts/le/ga/legacyvideo0001.mp4'
    s3.put_object(Bucket='pyfedi-test', Key=key, Body=b'video')
    first = make_post(env.chan, env.mod, ap_id='https://test.piefed.local/post/v1', title='v1')
    second = make_post(env.chan, env.mod, ap_id='https://test.piefed.local/post/v2', title='v2')
    for video in (first, second):
        video.type, video.url = POST_TYPE_VIDEO, CDN + key
    db.session.commit()

    purge(first)

    s3.head_object(Bucket='pyfedi-test', Key=key)
