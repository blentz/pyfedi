"""Round 259: a microblog's derived title, and the files a deleted post takes with it.

Two clusters in `app/models.py`.

`Post.new`'s microblog branch has no `name` to use as a title, so it DERIVES one from the body --
and then reads that derived title for the `[NSFL]` / `[NSFW]` markers a Mastodon poster writes by
hand. Those two lines are content labels set from a peer's text.

`Post.delete_dependencies` removes the files a post owns. The video arm and the archive arm both
build an S3 key from a URL (D1343's repair), and both are shared values: cross-posts are found by
url equality, so up to ten Post rows can name one video. The rows here pin which key is asked for
and when nothing is asked for at all.
"""
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.activitypub.util import create_post
from app.constants import POST_TYPE_VIDEO
from app.models import ArchivedPostReply, File, Post, Site
from tests.factories import (make_community, make_community_member, make_instance,
                             make_post, make_post_reply, make_site, make_user)

PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('videoland')
    author = make_user(api_baseline.instance_local, 'videoauthor', local=True)
    db.session.commit()
    make_community_member(author, community)
    return SimpleNamespace(app=app, community=community, author=author,
                           baseline=api_baseline)


@pytest.fixture
def ingest(db_session, http_mock):
    """A remote author and a community `Post.new` can attach a microblog to.

    `http_mock` is present because the ingest path fetches any link it finds in the body;
    `assert_all_called` is relaxed since most rows here have no link.
    """
    http_mock._assert_all_called = False
    author = make_user(make_instance('m.example'), 'alice')
    community = make_community()
    make_site()
    return SimpleNamespace(author=author, community=community, http_mock=http_mock)


def _delete_recording_s3(post):
    """`post.delete_dependencies()` with the S3 task recorded and the filesystem left alone.

    The keys reach the task through `.delay(...)` when the app is not in debug, which is how the
    suite runs -- so a plain function double records nothing.
    """
    asked = []

    class Recorder:
        def __call__(self, keys):
            asked.append(keys)

        def delay(self, keys):
            asked.append(keys)

    with patch('app.shared.tasks.maintenance.delete_from_s3', Recorder()), \
            patch.object(File, 'delete_from_disk',
                         lambda self, *args, **kwargs: None):
        post.delete_dependencies()
        db.session.commit()
    return asked


def a_note(content, fields=None):
    """A Create wrapping a titleless Note, which is what makes `Post.new` derive a title."""
    obj = {
        'id': 'https://m.example/users/alice/statuses/1',
        'type': 'Note',
        'content': content,
        'attributedTo': 'https://m.example/users/alice',
        'to': [PUBLIC],
    }
    if fields:
        obj.update(fields)
    return {'id': 'https://m.example/users/alice/statuses/1/activity',
            'type': 'Create', 'to': [PUBLIC], 'object': obj}


# --------------------------------------------------------------------------
# A microblog's derived title
# --------------------------------------------------------------------------


class TestTheTitleAMicroblogGets:
    """A Note has no `name`, so the title is derived from the body -- and then read back for
    the markers a poster types into the text themselves.
    """

    def test_the_title_comes_from_the_body(self, ingest):
        post = create_post(False, ingest.community,
                           a_note('<p>a sentence long enough to become a title</p>'),
                           ingest.author)

        assert post is not None
        assert 'a sentence long enough to become a title' in post.title

    def test_an_nsfl_marker_in_the_derived_title_sets_the_flag(self, ingest):
        """`[NSFL]`, `(NSFL)` or `[COMBAT]` in the derived title. A Mastodon poster has no NSFL
        field to set, so the convention is to type it -- and this instance's readers filter on
        the column, not on the text."""
        post = create_post(False, ingest.community,
                           a_note('<p>[NSFL] something upsetting at some length</p>'),
                           ingest.author)

        assert post.nsfl is True

    @pytest.mark.parametrize('marker', ['[NSFL]', '(NSFL)', '[COMBAT]'])
    def test_every_nsfl_spelling_is_recognised(self, ingest, marker):
        post = create_post(False, ingest.community,
                           a_note(f'<p>{marker} something upsetting at some length</p>'),
                           ingest.author)

        assert post.nsfl is True

    def test_the_marker_is_matched_without_regard_to_case(self, ingest):
        """`title.upper()`. Somebody typing `[nsfl]` means the same thing."""
        post = create_post(False, ingest.community,
                           a_note('<p>[nsfl] something upsetting at some length</p>'),
                           ingest.author)

        assert post.nsfl is True

    @pytest.mark.parametrize('marker', ['[NSFW]', '(NSFW)'])
    def test_an_nsfw_marker_sets_the_other_flag(self, ingest, marker):
        """A separate `if`, not an `elif` -- a post can be both, and they are different
        filters."""
        post = create_post(False, ingest.community,
                           a_note(f'<p>{marker} something explicit at some length</p>'),
                           ingest.author)

        assert post.nsfw is True
        assert post.nsfl is False

    def test_a_post_marked_both_ways_carries_both_flags(self, ingest):
        post = create_post(
            False, ingest.community,
            a_note('<p>[NSFL] [NSFW] something at some considerable length</p>'),
            ingest.author)

        assert post.nsfl is True
        assert post.nsfw is True

    def test_an_ordinary_microblog_carries_neither(self, ingest):
        """The control. Both markers are absent from almost every post, and setting either
        wrongly hides content from everybody who filters on it."""
        post = create_post(False, ingest.community,
                           a_note('<p>an ordinary sentence of some length</p>'),
                           ingest.author)

        assert post.nsfl is False
        assert post.nsfw is False

    def test_a_heading_that_is_a_link_becomes_the_posts_url(self, ingest):
        """`if link != '': post.url = link`. `microblog_content_to_title` returns a link only
        for an `<h1>` whose text is wrapped in an anchor -- the shape a WordPress or Ghost
        actor sends -- and THAT link is preferred over anything found in the body below."""
        ingest.http_mock.head('https://m.example/articles/1').respond(
            200, headers={'Content-Type': 'text/html'})
        ingest.http_mock.get('https://m.example/articles/1').respond(
            200, headers={'Content-Type': 'text/html'}, text='<html></html>')

        # The heading's link is on the AUTHOR'S OWN host and the body's is not. That matters:
        # the body fallback below excludes the author's host, so only the heading arm can
        # produce this url -- without it the body link wins and the assertion fails.
        post = create_post(
            False, ingest.community,
            a_note('<h1><a href="https://m.example/articles/1">A Published Article</a>'
                   '</h1><p>see also <a href="https://other.example/elsewhere">this</a>'
                   ' at some length</p>'),
            ingest.author)

        assert post.url == 'https://m.example/articles/1'
        assert 'A Published Article' in post.title

    def test_a_link_in_the_body_becomes_the_posts_url(self, ingest):
        """`microblog_content_to_link`. A Note that is mostly a link is a link post here, which
        is what makes it show a thumbnail and join a cross-post group."""
        ingest.http_mock.head('https://news.example/story').respond(
            200, headers={'Content-Type': 'text/html'})
        ingest.http_mock.get('https://news.example/story').respond(
            200, headers={'Content-Type': 'text/html'}, text='<html></html>')

        post = create_post(
            False, ingest.community,
            a_note('<p>look at this <a href="https://news.example/story">story</a> '
                   'which is quite interesting</p>'),
            ingest.author)

        assert post.url == 'https://news.example/story'


# --------------------------------------------------------------------------
# The files a deleted post takes with it
# --------------------------------------------------------------------------


class TestWhatADeletedVideoPostRemoves:
    """D1343's repair: the S3 key is derived from the url with `s3_key_from_url`, not the url
    itself. The rows assert WHICH key is asked for, because asking for the url deletes nothing
    and leaves every mirrored video in the bucket.
    """

    @pytest.fixture
    def s3_on(self, env, monkeypatch):
        for key, value in (('S3_ACCESS_KEY', 'a key'),
                           ('S3_ACCESS_SECRET', 'a secret'),
                           ('S3_ENDPOINT', 'https://s3.example'),
                           ('S3_BUCKET', 'a-bucket'),
                           ('S3_PUBLIC_URL', 'cdn.example')):
            monkeypatch.setitem(env.app.config, key, value)
        return env

    def _delete(self, env, post):
        """Run the deletion with the S3 task intercepted and the disk untouched.

        The task is dispatched as `delete_from_s3.delay(...)` outside debug, so the recorder has
        to answer on the `.delay` attribute as well as on the call itself.
        """
        return _delete_recording_s3(post)

    def test_the_key_is_derived_from_the_url(self, s3_on):
        post = make_post(s3_on.community, s3_on.author,
                         ap_id='https://test.piefed.local/v/1')
        post.type = POST_TYPE_VIDEO
        post.url = 'https://cdn.example/videos/ab/cd/clip.mp4'
        db.session.commit()

        asked = self._delete(s3_on, post)

        assert asked == [['videos/ab/cd/clip.mp4']]

    def test_a_video_post_with_no_url_asks_for_nothing(self, s3_on):
        """`and self.url`. A video post whose url never arrived has no object to delete, and
        `s3_key_from_url(None)` would be the error."""
        post = make_post(s3_on.community, s3_on.author,
                         ap_id='https://test.piefed.local/v/2')
        post.type = POST_TYPE_VIDEO
        post.url = None
        db.session.commit()

        assert self._delete(s3_on, post) == []

    def test_a_post_that_is_not_a_video_asks_for_nothing(self, s3_on):
        """`self.type == POST_TYPE_VIDEO`. A link post's url is somebody else's page, and
        deriving an S3 key from it would ask the bucket for an object named after a third
        party's path."""
        post = make_post(s3_on.community, s3_on.author,
                         ap_id='https://test.piefed.local/v/3')
        post.url = 'https://news.example/story'
        db.session.commit()

        assert self._delete(s3_on, post) == []

    def test_an_image_post_mirrored_to_the_bucket_is_not_deleted_by_this_arm(self, s3_on):
        """The type test matters even for a url that IS in our bucket. An image post's file is
        owned by its `File` row and deleted through `image.delete_from_disk`, so asking for the
        object here as well would delete it twice -- and `url` is shared between cross-posts,
        so the second ask could be for a file another post still shows."""
        from app.constants import POST_TYPE_IMAGE

        post = make_post(s3_on.community, s3_on.author,
                         ap_id='https://test.piefed.local/v/5')
        post.type = POST_TYPE_IMAGE
        post.url = 'https://cdn.example/posts/ab/cd/picture.webp'
        db.session.commit()

        assert self._delete(s3_on, post) == []

    def test_nothing_is_asked_for_when_s3_is_not_configured(self, env):
        """`_store_files_in_s3()`. Most instances store locally, and the whole arm is behind
        that test."""
        post = make_post(env.community, env.author,
                         ap_id='https://test.piefed.local/v/4')
        post.type = POST_TYPE_VIDEO
        post.url = 'https://cdn.example/videos/ab/cd/clip.mp4'
        db.session.commit()

        assert self._delete(env, post) == []


class TestWhatADeletedArchivedPostRemoves:
    """An archived post's replies live in a file rather than in the database, so deleting the
    post has to delete that file -- from S3 when the instance stores there, from disk when it
    does not.
    """

    @pytest.fixture
    def archived(self, env):
        post = make_post(env.community, env.author,
                         ap_id='https://test.piefed.local/a/1')
        reply = make_post_reply(post, env.author, body='an archived reply')
        db.session.commit()
        db.session.add(ArchivedPostReply(post_id=post.id, user_id=env.author.id,
                                         post_reply_id=reply.id))
        db.session.commit()
        env.post = post
        env.reply = reply
        return env

    def _delete(self, env, post):
        return _delete_recording_s3(post)

    def test_the_archive_object_is_deleted_from_s3(self, archived, monkeypatch):
        for key, value in (('S3_ACCESS_KEY', 'a key'),
                           ('S3_ACCESS_SECRET', 'a secret'),
                           ('S3_ENDPOINT', 'https://s3.example'),
                           ('S3_BUCKET', 'a-bucket'),
                           ('S3_PUBLIC_URL', 'cdn.example')):
            monkeypatch.setitem(archived.app.config, key, value)
        archived.post.archived = 'https://cdn.example/archives/1.json.gz'
        db.session.commit()

        asked = self._delete(archived, archived.post)

        assert asked == [['archives/1.json.gz']]

    def test_the_archived_replies_go_with_the_post(self, archived):
        """`ArchivedPostReply` rows are the database half of the archive, and they are deleted
        whether or not the file is."""
        archived.post.archived = 'app/static/media/archives/nothing-here.json.gz'
        post_id = archived.post.id
        db.session.commit()

        self._delete(archived, archived.post)

        assert ArchivedPostReply.query.filter_by(post_id=post_id).count() == 0

    def test_a_local_archive_file_is_unlinked(self, archived, tmp_path):
        """The `elif os.path.isfile(...)` arm, for an instance storing locally. The path is
        written for real and its disappearance is the assertion."""
        path = tmp_path / 'archive.json.gz'
        path.write_bytes(b'archived replies')
        archived.post.archived = str(path)
        db.session.commit()

        self._delete(archived, archived.post)

        assert not os.path.isfile(path)

    def test_a_post_that_was_never_archived_asks_for_nothing(self, archived):
        """`if self.archived:` -- the column is NULL for every live post, which is almost all
        of them."""
        archived.post.archived = None
        db.session.commit()

        assert self._delete(archived, archived.post) == []
