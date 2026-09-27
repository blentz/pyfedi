"""What a banned user's files leave behind, and what the CDN keeps serving.

`User.delete_dependencies` and `User.purge_content` in `app/models.py`, and
`File.delete_from_disk`'s cache list.

Deleting a file from disk is only half of a takedown on an instance behind a CDN:
until the edge is purged, the URL still answers. Both defects here are about the
purge not happening.

D1348. The `user_file` uploads -- every image the user attached through the
uploader -- were deleted with `purge_cdn=False`, and their File rows left behind.
`purge_content` had its own `user_file` block that would have purged them with
`purge_cdn=flush` and deleted the rows, but it could never run: `purge_content`
calls `delete_dependencies` first, and both callers
(`app/shared/user.py:ban_user`, `app/user/utils.py`) call it before that as well,
so every association the block looked for was already gone. Measured, with the
CDN configured and a user holding an avatar, a cover, one upload and one post
image:

    after delete_dependencies:  cover, avatar
    after purge_content:        cover, avatar, post image

-- the upload is missing from both, and its File row survived with no association.

D1349. `delete_from_disk` appended to `purge_from_cache` INSIDE
`if os.path.isfile(...)`, so a file already gone from disk was never purged.
Measured: the same four files with nothing on disk purged nothing at all. Whether
the local copy is still there says nothing about whether an edge still holds it.
"""
import os

import pytest
from flask import current_app, g
from sqlalchemy import text

from app import db
from app.models import File, Post, Site, User, user_file


@pytest.fixture
def env(app, api_baseline, monkeypatch, tmp_path):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    monkeypatch.setitem(current_app.config, 'CLOUDFLARE_ZONE_ID', 'zone')
    monkeypatch.setitem(current_app.config, 'CLOUDFLARE_API_TOKEN', 'token')
    monkeypatch.setitem(current_app.config, 'DEBUG', True)
    purged = []
    monkeypatch.setattr('app.models.flush_cdn_cache_task',
                        lambda urls: purged.extend(urls))
    return SimpleNamespace(baseline=api_baseline, purged=purged,
                           server=current_app.config['SERVER_URL'])


def a_file(path, on_disk=True):
    file = File(file_path=path)
    db.session.add(file)
    db.session.commit()
    if on_disk:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as handle:
            handle.write(b'x')
    return file


def cleanup(*paths):
    for path in paths:
        if os.path.isfile(path):
            os.unlink(path)


def url_for(env, path):
    return path.replace('app/', f'{env.server}/')


UPLOAD = 'app/static/media/users/zz/zz/upload.webp'
AVATAR = 'app/static/media/users/zz/zz/avatar.webp'
POSTIMG = 'app/static/media/posts/zz/zz/postimg.webp'


class TestABannedUsersUploads:
    """The `user_file` rows, which is where D1348 lived."""

    def an_upload(self, user, path=UPLOAD, on_disk=True):
        file = a_file(path, on_disk=on_disk)
        db.session.execute(user_file.insert().values(user_id=user.id,
                                                     file_id=file.id))
        db.session.commit()
        return file

    def test_the_cdn_is_purged(self, env):
        user = env.baseline.user2
        self.an_upload(user)
        try:
            user.delete_dependencies()

            assert url_for(env, UPLOAD) in env.purged
        finally:
            cleanup(UPLOAD)

    def test_the_file_is_removed_from_disk(self, env):
        user = env.baseline.user2
        self.an_upload(user)
        try:
            user.delete_dependencies()

            assert not os.path.isfile(UPLOAD)
        finally:
            cleanup(UPLOAD)

    def test_the_row_is_not_orphaned(self, env):
        """It used to be deleted from disk with its File row left behind and no
        association pointing at it -- a row nothing could ever find again."""
        user = env.baseline.user2
        file = self.an_upload(user)
        file_id = file.id
        try:
            user.delete_dependencies()
            db.session.commit()

            assert db.session.get(File, file_id) is None
        finally:
            cleanup(UPLOAD)

    def test_the_association_goes(self, env):
        user = env.baseline.user2
        self.an_upload(user)
        try:
            user.delete_dependencies()
            db.session.commit()

            assert db.session.execute(
                text('SELECT count(*) FROM user_file WHERE user_id = :u'),
                {'u': user.id}).scalar() == 0
        finally:
            cleanup(UPLOAD)

    def test_purge_cdn_false_removes_the_file_without_purging(self, env):
        """`purge_content(flush=False)` has to reach this, which is the whole
        point of the new argument: before, the flag could not affect these files
        at all, because they were gone by the time it was read."""
        user = env.baseline.user2
        self.an_upload(user)
        try:
            user.delete_dependencies(purge_cdn=False)

            assert env.purged == []
            assert not os.path.isfile(UPLOAD)
        finally:
            cleanup(UPLOAD)

    def test_a_file_another_user_also_uploaded_is_kept(self, env):
        """`DELETE FROM user_file WHERE file_id = :file_id` took out EVERY user's
        association with that file, not only this user's. Same policy as round
        152's S3 delete: losing this association is not the same as owning the
        file."""
        user, other = env.baseline.user2, env.baseline.user3
        file = self.an_upload(user)
        db.session.execute(user_file.insert().values(user_id=other.id,
                                                     file_id=file.id))
        db.session.commit()
        file_id = file.id
        try:
            user.delete_dependencies()
            db.session.commit()

            assert db.session.get(File, file_id) is not None
            assert os.path.isfile(UPLOAD)
            assert env.purged == []
            rows = db.session.execute(
                text('SELECT user_id FROM user_file WHERE file_id = :f'),
                {'f': file_id}).scalars().all()
            assert rows == [other.id]
        finally:
            cleanup(UPLOAD)


class TestWhatPurgeContentPurges:
    """`purge_content(flush=...)` end to end. Its own `user_file` block is gone --
    it could never run -- so `flush` has to reach those files through
    `delete_dependencies`."""

    def a_user_with_everything(self, env):
        user = env.baseline.user2
        avatar = a_file(AVATAR)
        upload = a_file(UPLOAD)
        post_image = a_file(POSTIMG)
        user.avatar_id = avatar.id
        db.session.execute(user_file.insert().values(user_id=user.id,
                                                     file_id=upload.id))
        post = Post.query.filter_by(user_id=user.id).first()
        post.image_id = post_image.id
        db.session.commit()
        return user

    def test_every_file_reaches_the_cdn_purge(self, env):
        user = self.a_user_with_everything(env)
        try:
            user.purge_content(flush=True)

            for path in (AVATAR, UPLOAD, POSTIMG):
                assert url_for(env, path) in env.purged, path
        finally:
            cleanup(AVATAR, UPLOAD, POSTIMG)

    def test_flush_false_purges_the_uploads_nothing(self, env):
        """The upload is the file `flush` could not reach before."""
        user = self.a_user_with_everything(env)
        try:
            user.purge_content(flush=False)

            assert url_for(env, UPLOAD) not in env.purged
        finally:
            cleanup(AVATAR, UPLOAD, POSTIMG)

    def test_the_posts_are_soft_deleted_by_default(self, env):
        """Unrelated to the CDN and asserted because `soft` and `flush` were
        confused once before: a positional `flush` landed in `soft` and
        hard-deleted everything."""
        user = self.a_user_with_everything(env)
        post_ids = [post.id for post in Post.query.filter_by(user_id=user.id)]
        try:
            user.purge_content(flush=True)

            for post_id in post_ids:
                post = db.session.get(Post, post_id)
                assert post is not None and post.deleted is True
        finally:
            cleanup(AVATAR, UPLOAD, POSTIMG)

    def test_soft_false_really_deletes_them(self, env):
        user = self.a_user_with_everything(env)
        post_ids = [post.id for post in Post.query.filter_by(user_id=user.id)]
        try:
            user.purge_content(soft=False, flush=True)

            for post_id in post_ids:
                assert db.session.get(Post, post_id) is None
        finally:
            cleanup(AVATAR, UPLOAD, POSTIMG)


class TestAFileAlreadyGoneFromDisk:
    """D1349. The purge list was built inside `if os.path.isfile(...)`, so the one
    case where purging matters most -- the bytes are gone locally but an edge may
    still hold them -- was the case that purged nothing.
    """

    def test_a_missing_file_is_still_purged(self, env):
        file = a_file(UPLOAD, on_disk=False)

        file.delete_from_disk()

        assert env.purged == [url_for(env, UPLOAD)]

    def test_a_missing_thumbnail_is_still_purged(self, env):
        file = File(thumbnail_path=UPLOAD)
        db.session.add(file)
        db.session.commit()

        file.delete_from_disk()

        assert env.purged == [url_for(env, UPLOAD)]

    def test_a_file_that_is_there_is_purged_and_unlinked(self, env):
        file = a_file(UPLOAD)
        try:
            file.delete_from_disk()

            assert env.purged == [url_for(env, UPLOAD)]
            assert not os.path.isfile(UPLOAD)
        finally:
            cleanup(UPLOAD)

    def test_a_file_that_vanishes_between_the_test_and_the_unlink(self, env,
                                                                 monkeypatch):
        """`os.path.isfile` then `os.unlink` is a race, which is why the unlink
        has its own FileNotFoundError arm. The purge still has to happen."""
        file = a_file(UPLOAD)

        def vanishing_unlink(path):
            raise FileNotFoundError(path)

        # `app.models.os` IS the os module, so this patch is global -- including
        # for this test's own cleanup, which is why it is undone first.
        monkeypatch.setattr('app.models.os.unlink', vanishing_unlink)
        try:
            file.delete_from_disk()

            assert env.purged == [url_for(env, UPLOAD)]
        finally:
            monkeypatch.undo()
            cleanup(UPLOAD)

    def test_purge_cdn_false_still_means_no_purge(self, env):
        file = a_file(UPLOAD, on_disk=False)

        file.delete_from_disk(purge_cdn=False)

        assert env.purged == []

    def test_a_row_with_no_paths_purges_nothing(self, env):
        file = File()
        db.session.add(file)
        db.session.commit()

        file.delete_from_disk()

        assert env.purged == []

    def test_an_s3_url_is_purged_by_its_own_branch(self, env, monkeypatch):
        """The S3 arm never consulted the local disk, so it was unaffected; it is
        asserted here so the repair of the local arm is known not to have changed
        it."""
        monkeypatch.setitem(current_app.config, 'S3_PUBLIC_URL', 'cdn.example')
        monkeypatch.setattr('app.models._store_files_in_s3', lambda: True)
        monkeypatch.setattr('app.shared.tasks.maintenance.delete_from_s3',
                            type('_Task', (), {
                                'delay': staticmethod(lambda keys: None),
                                '__call__': staticmethod(lambda keys: None)})())
        file = File(file_path='https://cdn.example/posts/zz/zz/a.webp')
        db.session.add(file)
        db.session.commit()

        file.delete_from_disk()

        assert env.purged == ['https://cdn.example/posts/zz/zz/a.webp']
