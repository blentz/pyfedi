"""What deleting a Post removes from this instance's object storage.

`Post.delete_dependencies` in `app/models.py` has two S3 branches: the mirrored
video named by `Post.url`, and the archived reply tree named by `Post.archived`.

D1343. The video branch passed `self.url` -- the whole `https://...` -- to
`delete_from_s3`, which uses its argument as an object KEY. No object has a key
that begins `https://`, so `delete_objects` was asked for something that cannot
exist and every mirrored video stayed in the bucket for good.

Fixing the key alone would have been worse than the leak. `Post.url` is written
by a peer for anything federated in, AND it is shared by design: cross-posts are
found by url equality (`Post.cross_posts`), so up to ten Post rows name one
video. Measured: two posts in the same community with the same url are two rows,
so deleting one of them would have taken the file the other still plays.
"""
import os

import pytest
from flask import current_app, g

from app import db
from app.constants import POST_TYPE_VIDEO
from app.models import File, Post, Site


@pytest.fixture
def env(app, api_baseline, tmp_path, monkeypatch):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    monkeypatch.setitem(current_app.config, 'S3_PUBLIC_URL', 'cdn.example')
    monkeypatch.setattr('app.models._store_files_in_s3', lambda: True)
    return SimpleNamespace(app=app, tmp_path=tmp_path, baseline=api_baseline)


@pytest.fixture
def deleted_keys(monkeypatch):
    """Every key handed to `delete_from_s3`, by either arm of the DEBUG split."""
    seen = []
    monkeypatch.setattr(
        'app.shared.tasks.maintenance.delete_from_s3',
        type('_Task', (), {'delay': staticmethod(lambda keys: seen.extend(keys)),
                           '__call__': staticmethod(lambda keys: seen.extend(keys))})())
    return seen


def a_video_post(baseline, url, **columns):
    post = Post(user_id=baseline.user2.id, community_id=baseline.community1.id,
                title='a clip', type=POST_TYPE_VIDEO, url=url,
                posted_at=baseline.post1.posted_at,
                last_active=baseline.post1.last_active, **columns)
    db.session.add(post)
    db.session.commit()
    return post


class TestTheMirroredVideo:
    def test_the_key_and_not_the_url_reaches_the_task(self, env, deleted_keys):
        """The whole of the leak: `['https://cdn.example/posts/aa/bb/clip.mp4']`
        was measured going to `delete_objects` as a key."""
        post = a_video_post(env.baseline, 'https://cdn.example/posts/aa/bb/clip.mp4')

        post.delete_dependencies()

        assert deleted_keys == ['posts/aa/bb/clip.mp4']

    def test_a_cross_post_still_playing_it_keeps_it(self, env, deleted_keys):
        url = 'https://cdn.example/posts/aa/bb/clip.mp4'
        first = a_video_post(env.baseline, url)
        a_video_post(env.baseline, url)

        first.delete_dependencies()

        assert deleted_keys == []

    def test_the_last_row_naming_it_does_delete_it(self, env, deleted_keys):
        """The other side of the cross-post rule: once nothing else points at the
        object, the file goes. Otherwise the fix for one leak would be another."""
        url = 'https://cdn.example/posts/aa/bb/clip.mp4'
        first = a_video_post(env.baseline, url)
        second = a_video_post(env.baseline, url)
        db.session.delete(first)
        db.session.commit()

        second.delete_dependencies()

        assert deleted_keys == ['posts/aa/bb/clip.mp4']

    def test_a_file_row_still_pointing_at_it_keeps_it(self, env, deleted_keys):
        url = 'https://cdn.example/posts/aa/bb/clip.mp4'
        db.session.add(File(source_url=url))
        db.session.commit()
        post = a_video_post(env.baseline, url)

        post.delete_dependencies()

        assert deleted_keys == []

    @pytest.mark.parametrize('url', [
        'https://cdn.example/../../secret.mp4',
        'https://cdn.example/%2e%2e/secret.mp4',
        'https://cdn.example/',
        'https://cdn.example.evil.test/clip.mp4',
        'https://peer.test/posts/aa/bb/clip.mp4',
        None,
    ])
    def test_a_url_that_names_no_object_of_ours(self, env, deleted_keys, url):
        post = a_video_post(env.baseline, url)

        post.delete_dependencies()

        assert deleted_keys == []

    def test_a_post_that_is_not_a_video(self, env, deleted_keys):
        """The branch is `type == POST_TYPE_VIDEO`: an image post whose url
        happens to be in the bucket is not the video mirror, and its File row is
        what owns anything there."""
        post = a_video_post(env.baseline, 'https://cdn.example/posts/aa/bb/clip.mp4')
        post.type = POST_TYPE_VIDEO + 1
        db.session.commit()

        post.delete_dependencies()

        assert deleted_keys == []

    def test_an_instance_not_using_s3(self, env, deleted_keys, monkeypatch):
        monkeypatch.setattr('app.models._store_files_in_s3', lambda: False)
        post = a_video_post(env.baseline, 'https://cdn.example/posts/aa/bb/clip.mp4')

        post.delete_dependencies()

        assert deleted_keys == []


class TestTheArchivedReplyTree:
    """`archived` is written by `archive_post` in `app/utils.py` and by nothing
    else, so unlike `url` it is never a peer's string. It goes through the same
    helper anyway: one reading of what an S3 URL of ours is, rather than six.
    """

    def test_an_archive_in_s3(self, env, deleted_keys):
        post = a_video_post(env.baseline, None,
                            archived='https://cdn.example/archive/aa/bb/x.json.gz')

        post.delete_dependencies()

        assert deleted_keys == ['archive/aa/bb/x.json.gz']

    def test_an_archive_on_disk_is_unlinked(self, env, deleted_keys):
        path = str(env.tmp_path / 'archive.json.gz')
        with open(path, 'wb') as handle:
            handle.write(b'gzipped replies')
        post = a_video_post(env.baseline, None, archived=path)

        post.delete_dependencies()

        assert deleted_keys == []
        assert not os.path.exists(path)

    def test_an_archive_path_that_has_already_gone(self, env, deleted_keys):
        post = a_video_post(env.baseline, None,
                            archived=str(env.tmp_path / 'never-written.gz'))

        post.delete_dependencies()

        assert deleted_keys == []

    def test_an_archive_url_climbing_out_of_the_bucket(self, env, deleted_keys):
        post = a_video_post(env.baseline, None,
                            archived='https://cdn.example/../../secret.gz')

        post.delete_dependencies()

        assert deleted_keys == []

    def test_no_archive_at_all(self, env, deleted_keys):
        post = a_video_post(env.baseline, None)

        post.delete_dependencies()

        assert deleted_keys == []
