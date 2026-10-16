"""Step 3, Task 6: purging a media post removes its file, locally or in S3 (A13.8); a soft delete keeps it."""
import os

from types import SimpleNamespace

import pytest

from app import db
from app.constants import POST_TYPE_LINK
from app.models import Post
from app.shared.media import media_local_path
from app.shared import post as post_module
from app.shared.post import delete_post
from tests.factories import make_post
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


def test_an_s3_object_another_post_names_stays(env, s3):
    post = run(stored_post(env))
    key = post.url.removeprefix(CDN)
    other = stored_post(env, title='Other')
    other.url = post.url
    db.session.commit()
    purge(post)
    s3.head_object(Bucket='pyfedi-test', Key=key)


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


def test_purging_a_media_post_keeps_the_local_file_a_crosspost_names(env, app):
    """Final review M6: cross-posts are found by url equality, so a link post can name this post's local media. Purging
    the media post leaves the file the cross-post still plays, as the S3 branch already does."""
    post = run(stored_post(env))
    media_file = media_local_path(post)
    crosspost = make_post(env.cast, env.mod, ap_id='https://test.piefed.local/post/cross', title='Cross')
    crosspost.type, crosspost.url = POST_TYPE_LINK, post.url
    db.session.commit()

    purge(post)

    assert os.path.isfile(media_file)
    os.unlink(media_file)
