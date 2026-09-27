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


class TestAPostsImageAtTheEdge:
    """D1350. `Post.delete_dependencies` and `PostReply.delete_dependencies`
    deleted their images with `purge_cdn=False`, which is how every caller avoided
    one Cloudflare request per file -- and the cost was that no post image was
    ever purged at all. A moderator deleting a post left it readable at the edge.

    `cache_urls` collects instead, so the callers that delete many posts make one
    request and there is no reason left to turn the purge off.
    """

    def a_post_with_an_image(self, env, path=POSTIMG):
        post = Post.query.filter_by(user_id=env.baseline.user2.id).first()
        post.image_id = a_file(path).id
        db.session.commit()
        return post

    def test_deleting_a_post_purges_its_image(self, env):
        post = self.a_post_with_an_image(env)
        try:
            post.delete_dependencies()

            assert env.purged == [url_for(env, POSTIMG)]
        finally:
            cleanup(POSTIMG)

    def test_a_caller_that_collects_gets_the_url_instead(self, env):
        """The batching arm: nothing is flushed here, so a caller deleting a
        hundred posts still makes one request."""
        post = self.a_post_with_an_image(env)
        collected = []
        try:
            post.delete_dependencies(cache_urls=collected)

            assert collected == [url_for(env, POSTIMG)]
            assert env.purged == []
        finally:
            cleanup(POSTIMG)

    def test_the_replies_images_go_in_the_same_list(self, env):
        from app.models import PostReply

        post = self.a_post_with_an_image(env)
        reply = PostReply.query.filter_by(post_id=post.id).first()
        reply_image = 'app/static/media/posts/zz/zz/replyimg.webp'
        reply.image_id = a_file(reply_image).id
        db.session.commit()
        try:
            post.delete_dependencies()

            assert sorted(env.purged) == sorted([url_for(env, POSTIMG),
                                                 url_for(env, reply_image)])
        finally:
            cleanup(POSTIMG, reply_image)

    def test_deleting_a_reply_on_its_own_purges_its_image(self, env):
        from app.models import PostReply

        reply = PostReply.query.filter_by(user_id=env.baseline.user2.id).first()
        reply_image = 'app/static/media/posts/zz/zz/replyimg.webp'
        reply.image_id = a_file(reply_image).id
        db.session.commit()
        try:
            reply.delete_dependencies()

            assert env.purged == [url_for(env, reply_image)]
        finally:
            cleanup(reply_image)

    def test_a_post_with_no_image_flushes_nothing(self, env):
        post = Post.query.filter_by(user_id=env.baseline.user2.id).first()

        post.delete_dependencies()

        assert env.purged == []

    SECOND_IMAGE = 'app/static/media/posts/zz/zz/second.webp'

    def two_posts_with_images(self, env):
        """Two, deliberately. A caller that batches and a caller that lets each
        post flush for itself both end up purging the same URLs, so only the
        NUMBER of requests tells them apart -- and one post cannot show it."""
        posts = Post.query.filter_by(user_id=env.baseline.user2.id).limit(2).all()
        assert len(posts) == 2
        posts[0].image_id = a_file(POSTIMG).id
        posts[1].image_id = a_file(self.SECOND_IMAGE).id
        db.session.commit()
        return posts

    @pytest.fixture
    def flushes(self, monkeypatch):
        """Every call to `flush_cdn_cache`, with its list, patched where the
        methods under test look it up."""
        import app.models as models

        recorded = []
        original = models.flush_cdn_cache
        monkeypatch.setattr(models, 'flush_cdn_cache',
                            lambda urls: (recorded.append(urls), original(urls))[1])
        return recorded

    def test_a_community_flushes_once_for_all_its_posts(self, env, flushes):
        """`Community.delete_dependencies` walks every post, so one request per
        file is what `purge_cdn=False` was standing in for. It collects."""
        from app.models import Community

        posts = self.two_posts_with_images(env)
        community = db.session.get(Community, posts[0].community_id)
        try:
            community.delete_dependencies()

            assert len(flushes) == 1
            assert sorted(flushes[0]) == sorted([url_for(env, POSTIMG),
                                                 url_for(env, self.SECOND_IMAGE)])
        finally:
            cleanup(POSTIMG, self.SECOND_IMAGE)

    def test_a_banned_domain_flushes_once_for_all_its_posts(self, env, flushes):
        from app.models import Domain

        posts = self.two_posts_with_images(env)
        domain = Domain(name='spam.example', post_count=2)
        db.session.add(domain)
        db.session.commit()
        for post in posts:
            post.domain_id = domain.id
        db.session.commit()
        try:
            domain.purge_content()

            assert len(flushes) == 1
            assert sorted(flushes[0]) == sorted([url_for(env, POSTIMG),
                                                 url_for(env, self.SECOND_IMAGE)])
        finally:
            cleanup(POSTIMG, self.SECOND_IMAGE)

    def test_a_banned_domain_deletes_the_posts_too(self, env):
        from app.models import Domain

        posts = self.two_posts_with_images(env)
        domain = Domain(name='spam.example', post_count=2)
        db.session.add(domain)
        db.session.commit()
        post_ids = []
        for post in posts:
            post.domain_id = domain.id
            post_ids.append(post.id)
        db.session.commit()
        try:
            domain.purge_content()

            for post_id in post_ids:
                assert db.session.get(Post, post_id) is None
        finally:
            cleanup(POSTIMG, self.SECOND_IMAGE)

    def test_a_banned_domain_removes_the_images_from_disk(self, env):
        from app.models import Domain

        posts = self.two_posts_with_images(env)
        domain = Domain(name='spam.example', post_count=2)
        db.session.add(domain)
        db.session.commit()
        for post in posts:
            post.domain_id = domain.id
        db.session.commit()
        try:
            domain.purge_content()

            assert not os.path.isfile(POSTIMG)
            assert not os.path.isfile(self.SECOND_IMAGE)
        finally:
            cleanup(POSTIMG, self.SECOND_IMAGE)


class TestTheSizeOfACloudflarePurge:
    """D1351. The endpoint takes at most 30 files per request and answers 400 for
    a longer list. `flush_cdn_cache_task` sent whatever it was given in one
    request and never read the response, so a purge of 31 files failed silently --
    and the callers that batch are exactly the ones that exceed 30.
    """

    @pytest.fixture
    def cloudflare(self, app, monkeypatch, http_mock):
        monkeypatch.setitem(current_app.config, 'CLOUDFLARE_ZONE_ID', 'zone')
        monkeypatch.setitem(current_app.config, 'CLOUDFLARE_API_TOKEN', 'token')
        route = http_mock.post(
            'https://api.cloudflare.com/client/v4/zones/zone/purge_cache'
        ).respond(200, json={'success': True})
        return route

    def sent(self, route):
        import orjson

        return [orjson.loads(call.request.content) for call in route.calls]

    def test_thirty_files_go_in_one_request(self, cloudflare):
        from app.models import flush_cdn_cache_task

        urls = [f'https://x.test/{index}.webp' for index in range(30)]
        flush_cdn_cache_task(urls)

        assert self.sent(cloudflare) == [{'files': urls}]

    def test_thirty_one_files_are_split(self, cloudflare):
        from app.models import flush_cdn_cache_task

        urls = [f'https://x.test/{index}.webp' for index in range(31)]
        flush_cdn_cache_task(urls)

        assert self.sent(cloudflare) == [{'files': urls[:30]}, {'files': urls[30:]}]

    def test_a_hundred_files_are_split_into_four(self, cloudflare):
        from app.models import flush_cdn_cache_task

        urls = [f'https://x.test/{index}.webp' for index in range(100)]
        flush_cdn_cache_task(urls)

        batches = self.sent(cloudflare)
        assert [len(batch['files']) for batch in batches] == [30, 30, 30, 10]
        assert [url for batch in batches for url in batch['files']] == urls

    def test_a_single_string_is_still_one_file(self, cloudflare):
        from app.models import flush_cdn_cache_task

        flush_cdn_cache_task('https://x.test/one.webp')

        assert self.sent(cloudflare) == [{'files': ['https://x.test/one.webp']}]

    def test_purge_everything_is_not_split(self, cloudflare):
        from app.models import flush_cdn_cache_task

        flush_cdn_cache_task('all')

        assert self.sent(cloudflare) == [{'purge_everything': True}]

    def test_a_refusal_is_logged_rather_than_swallowed(self, app, monkeypatch,
                                                      http_mock, caplog):
        """The response was never read at all, so a rejected purge looked exactly
        like a successful one."""
        import logging

        monkeypatch.setitem(current_app.config, 'CLOUDFLARE_ZONE_ID', 'zone')
        monkeypatch.setitem(current_app.config, 'CLOUDFLARE_API_TOKEN', 'token')
        http_mock.post(
            'https://api.cloudflare.com/client/v4/zones/zone/purge_cache'
        ).respond(400, json={'errors': [{'message': 'too many files'}]})

        from app.models import flush_cdn_cache_task

        with caplog.at_level(logging.WARNING):
            flush_cdn_cache_task(['https://x.test/one.webp'])

        assert 'CDN purge refused' in caplog.text
        assert '400' in caplog.text

    def test_no_zone_configured_sends_nothing(self, app, monkeypatch, http_mock):
        monkeypatch.setitem(current_app.config, 'CLOUDFLARE_ZONE_ID', '')
        monkeypatch.setitem(current_app.config, 'CLOUDFLARE_API_TOKEN', '')

        from app.models import flush_cdn_cache_task

        # No route is registered, so any request at all raises out of respx --
        # which is the assertion. Registering one and checking call_count == 0
        # fails the fixture's own assert_all_called instead.
        flush_cdn_cache_task(['https://x.test/one.webp'])

        assert list(http_mock.calls) == []
