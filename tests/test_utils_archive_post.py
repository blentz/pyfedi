"""Archiving an old post, and what it takes out of the database.

Sub-project 121 -- `archive_post` in `app/utils.py`. It moves a post's body and
all of its replies into a gzipped JSON file, nulls the columns they came from,
records an `ArchivedPostReply` row per reply, deletes the reply rows, and purges
the post's generated image files. `archive_old_posts` calls it for every post
past the configured age.

No test had ever executed it: the caller's tests replace it with a recorder,
which is right for them and left this function's own behaviour unasserted.

D1335 is what that hid. Archiving is destructive by design -- the body lives in
the file afterwards, not in the row -- so a SECOND run starts from a post with
nothing left and writes `body: null, replies: []` over the archive the first run
made, at the same path. The text is then gone from both places. Only
`archive_old_posts`'s `WHERE p.archived IS NULL` stood between that and a caller.
"""
import glob
import gzip
import os

import orjson
import pytest
from flask import current_app, g

from app import db
from app.models import (ArchivedPostReply, File, Post, PostReply,
                        PostReplyBookmark, Site)
from app.utils import archive_post
from tests.factories import (make_community, make_community_member, make_post,
                             make_post_reply, make_user)

ARCHIVED_DIR = 'app/static/media/archived'


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('probeland')
    author = api_baseline.user2
    db.session.commit()
    make_community_member(author, community)
    written = set(glob.glob(f'{ARCHIVED_DIR}/*'))
    yield SimpleNamespace(app=app, community=community, author=author,
                          baseline=api_baseline)
    # Whatever this test wrote into the shared media tree goes with it.
    for path in set(glob.glob(f'{ARCHIVED_DIR}/*')) - written:
        if os.path.isfile(path):
            os.unlink(path)


def a_post(env, body=None, replies=0, suffix='1'):
    post = make_post(env.community, env.author,
                     ap_id=f'https://test.piefed.local/p/{suffix}')
    post.body = body
    post.body_html = f'<p>{body}</p>' if body else None
    db.session.commit()
    for index in range(replies):
        make_post_reply(post, env.author, body=f'reply {index}')
    post.reply_count = replies
    db.session.commit()
    return post


def archive_of(post):
    with gzip.open(post.archived, 'rb') as handle:
        return orjson.loads(handle.read())


class TestWhatIsWrittenAndWhatIsRemoved:
    def test_a_long_post_is_archived(self, env):
        post = a_post(env, body='x' * 300, suffix='long')
        archive_post(post.id, None)
        db.session.expire_all()
        post = db.session.get(Post, post.id)
        assert post.archived == f'{ARCHIVED_DIR}/post_{post.id}.json.gz'
        assert os.path.isfile(post.archived)

    def test_the_body_moves_out_of_the_row_and_into_the_file(self, env):
        post = a_post(env, body='x' * 300, suffix='body')
        archive_post(post.id, None)
        db.session.expire_all()
        post = db.session.get(Post, post.id)
        assert post.body is None
        assert post.body_html is None
        assert archive_of(post)['body'] == 'x' * 300
        assert archive_of(post)['body_html'] == '<p>' + 'x' * 300 + '</p>'

    def test_the_archive_says_which_version_it_is(self, env):
        post = a_post(env, body='x' * 300, suffix='version')
        archive_post(post.id, None)
        db.session.expire_all()
        payload = archive_of(db.session.get(Post, post.id))
        assert payload['version'] == 1
        assert payload['id'] == post.id

    def test_the_replies_move_with_it(self, env):
        post = a_post(env, body='x' * 300, replies=3, suffix='replies')
        archive_post(post.id, None)
        db.session.expire_all()
        post = db.session.get(Post, post.id)
        assert PostReply.query.filter_by(post_id=post.id).count() == 0
        bodies = [reply['body'] for reply in archive_of(post)['replies']]
        assert sorted(bodies) == ['reply 0', 'reply 1', 'reply 2']

    def test_each_reply_leaves_a_row_behind(self, env):
        """`ArchivedPostReply` is what still ties a user to a comment that is no
        longer in `post_reply`."""
        post = a_post(env, body='x' * 300, replies=2, suffix='rows')
        reply_ids = sorted(reply.id for reply
                           in PostReply.query.filter_by(post_id=post.id))
        archive_post(post.id, None)
        db.session.expire_all()
        rows = ArchivedPostReply.query.filter_by(post_id=post.id).all()
        assert sorted(row.post_reply_id for row in rows) == reply_ids
        assert {row.user_id for row in rows} == {env.author.id}

    def test_a_reply_somebody_bookmarked_is_kept(self, env):
        """A bookmark points at a `post_reply` row, so deleting the row would
        break it. The reply is archived AND kept."""
        post = a_post(env, body='x' * 300, replies=1, suffix='bookmarked')
        reply = PostReply.query.filter_by(post_id=post.id).one()
        db.session.add(PostReplyBookmark(user_id=env.author.id,
                                         post_reply_id=reply.id))
        db.session.commit()
        archive_post(post.id, None)
        db.session.expire_all()
        assert PostReply.query.filter_by(post_id=post.id).count() == 1
        assert ArchivedPostReply.query.filter_by(post_id=post.id).count() == 1

    def test_a_short_post_with_no_replies_is_left_alone(self, env):
        """The archive's own url would be longer than the body it saves."""
        post = a_post(env, body='short', suffix='short')
        archive_post(post.id, None)
        db.session.expire_all()
        post = db.session.get(Post, post.id)
        assert post.archived is None
        assert post.body == 'short'

    def test_a_post_with_no_body_and_no_replies_is_left_alone(self, env):
        post = a_post(env, body=None, suffix='nobody')
        archive_post(post.id, None)
        db.session.expire_all()
        assert db.session.get(Post, post.id).archived is None

    def test_a_short_post_with_replies_is_archived(self, env):
        """The guard is `reply_count == 0 AND the body is short`, so one reply is
        enough to make it worth archiving."""
        post = a_post(env, body='short', replies=1, suffix='shortreplies')
        archive_post(post.id, None)
        db.session.expire_all()
        assert db.session.get(Post, post.id).archived is not None

    def test_a_post_that_does_not_exist(self, env):
        archive_post(999999, None)

    def test_nothing_is_written_for_a_post_that_does_not_exist(self, env):
        before = set(glob.glob(f'{ARCHIVED_DIR}/*'))
        archive_post(999999, None)
        assert set(glob.glob(f'{ARCHIVED_DIR}/*')) == before


class TestArchivingTwice:
    """D1335. The second run had nothing left to save and saved that."""

    def test_the_archive_is_not_replaced_with_an_empty_one(self, env):
        post = a_post(env, body='x' * 300, replies=2, suffix='twice')
        archive_post(post.id, None)
        db.session.expire_all()
        post = db.session.get(Post, post.id)
        first = archive_of(post)

        archive_post(post.id, None)

        db.session.expire_all()
        post = db.session.get(Post, post.id)
        again = archive_of(post)
        assert again['body'] == first['body'] == 'x' * 300
        assert len(again['replies']) == len(first['replies']) == 2

    def test_the_path_does_not_change(self, env):
        post = a_post(env, body='x' * 300, suffix='twicepath')
        archive_post(post.id, None)
        db.session.expire_all()
        first = db.session.get(Post, post.id).archived
        archive_post(post.id, None)
        db.session.expire_all()
        assert db.session.get(Post, post.id).archived == first

    def test_a_reply_added_after_archiving_is_not_swept_up(self, env):
        """The second call returns before the reply-deleting loop, so a comment
        written after the archive survives -- which is what the loop would
        otherwise remove without adding it to the file."""
        post = a_post(env, body='x' * 300, suffix='twicereply')
        archive_post(post.id, None)
        db.session.expire_all()
        post = db.session.get(Post, post.id)
        make_post_reply(post, env.author, body='after the archive')
        db.session.commit()

        archive_post(post.id, None)

        db.session.expire_all()
        assert PostReply.query.filter_by(post_id=post.id).count() == 1

    def test_the_caller_would_not_ask_twice(self, env):
        """Where the protection used to live, asserted so the guard above is
        known to be a second line of defence rather than the only one."""
        from pathlib import Path

        source = Path('app/shared/tasks/maintenance.py').read_text(encoding='utf8')
        assert 'p.archived IS NULL' in source


class TestThePostsGeneratedImages:
    def an_image(self, post, local=True):
        os.makedirs('app/static/media/posts/ab/cd', exist_ok=True)
        if local:
            image = File(file_path='app/static/media/posts/ab/cd/probe.png',
                         thumbnail_path='app/static/media/posts/ab/cd/probe_t.png',
                         source_url='https://peer.test/probe.png')
        else:
            image = File(
                file_path=f'https://{current_app.config["S3_PUBLIC_URL"]}/posts/a.png',
                thumbnail_path=f'https://{current_app.config["S3_PUBLIC_URL"]}/posts/t.png',
                source_url='https://peer.test/probe.png')
        db.session.add(image)
        db.session.commit()
        if local:
            for path in (image.file_path, image.thumbnail_path):
                with open(path, 'wb') as handle:
                    handle.write(b'x')
        post.image_id = image.id
        db.session.commit()
        return image

    def test_the_local_files_are_deleted(self, env):
        post = a_post(env, body='x' * 300, suffix='image')
        image = self.an_image(post)
        paths = (image.file_path, image.thumbnail_path)
        archive_post(post.id, None)
        assert [os.path.exists(path) for path in paths] == [False, False]

    def test_the_columns_are_cleared(self, env):
        post = a_post(env, body='x' * 300, suffix='imagecols')
        image = self.an_image(post)
        archive_post(post.id, None)
        db.session.expire_all()
        fresh = db.session.get(File, image.id)
        assert fresh.file_path is None
        assert fresh.thumbnail_path is None

    def test_the_source_url_is_kept(self, env):
        """It names the peer's original, which archiving does not own."""
        post = a_post(env, body='x' * 300, suffix='imagesource')
        image = self.an_image(post)
        archive_post(post.id, None)
        db.session.expire_all()
        assert db.session.get(File, image.id).source_url == \
            'https://peer.test/probe.png'

    def test_an_image_row_that_is_gone(self, env, monkeypatch):
        """`if image_file:` guards a row a foreign key says must exist --
        `post_image_id_fkey` refuses `image_id = 999999` outright -- so the
        absence is simulated at the session, the way round 145 does for
        `Post.new`'s author lookup (fact 653)."""
        post = a_post(env, body='x' * 300, suffix='imagemissing')
        image = self.an_image(post)
        post_id, image_id = post.id, image.id

        import app.utils as utils

        real_get_task_session = utils.get_task_session

        def session_without_the_file():
            session = real_get_task_session()
            original_get = session.get

            def get(model, identity, *args, **keywords):
                if model is File and identity == image_id:
                    return None
                return original_get(model, identity, *args, **keywords)

            session.get = get
            return session

        monkeypatch.setattr(utils, 'get_task_session', session_without_the_file)
        archive_post(post_id, None)
        monkeypatch.undo()

        db.session.expire_all()
        assert db.session.get(Post, post_id).archived is not None
        # The files are still there, because the row that names them was hidden.
        assert os.path.exists('app/static/media/posts/ab/cd/probe.png')
        os.unlink('app/static/media/posts/ab/cd/probe.png')
        os.unlink('app/static/media/posts/ab/cd/probe_t.png')

    def test_a_file_that_is_already_off_disk(self, env):
        post = a_post(env, body='x' * 300, suffix='imagegone')
        image = self.an_image(post)
        os.unlink(image.file_path)
        os.unlink(image.thumbnail_path)
        archive_post(post.id, None)
        db.session.expire_all()
        assert db.session.get(File, image.id).file_path is None

    def test_a_short_post_still_loses_its_generated_images(self, env):
        """The early return for a short post happens AFTER the image purge, and
        deliberately: the generated sizes can be rebuilt from `source_url`."""
        post = a_post(env, body='short', suffix='imageshort')
        image = self.an_image(post)
        paths = (image.file_path, image.thumbnail_path)
        archive_post(post.id, None)
        db.session.expire_all()
        assert [os.path.exists(path) for path in paths] == [False, False]
        assert db.session.get(Post, post.id).archived is None


class TestWhatTheArchiveSaysAboutEachReply:
    def test_the_fields_a_reader_needs(self, env):
        post = a_post(env, body='x' * 300, replies=1, suffix='fields')
        archive_post(post.id, None)
        db.session.expire_all()
        entry = archive_of(db.session.get(Post, post.id))['replies'][0]
        for key in ('id', 'body', 'body_html', 'posted_at', 'score', 'ranking',
                    'parent_id', 'user_id', 'depth', 'path', 'author_name',
                    'author_id', 'author_user_name', 'author_ap_id'):
            assert key in entry, key

    def test_the_author_is_named(self, env):
        post = a_post(env, body='x' * 300, replies=1, suffix='author')
        archive_post(post.id, None)
        db.session.expire_all()
        entry = archive_of(db.session.get(Post, post.id))['replies'][0]
        assert entry['author_id'] == env.author.id
        assert entry['author_user_name'] == env.author.user_name

    def test_a_nested_reply_keeps_its_parent(self, env):
        post = a_post(env, body='x' * 300, suffix='nested')
        parent = make_post_reply(post, env.author, body='parent')
        child = make_post_reply(post, env.author, body='child')
        # `make_post_reply` builds top-level replies; `post_replies` walks the
        # tree by `parent_id`/`depth`/`path`, so a child has to be made one.
        child.parent_id = parent.id
        child.depth = 1
        child.path = [post.id, parent.id, child.id]
        parent.path = [post.id, parent.id]
        parent.child_count = 1
        post.reply_count = 2
        db.session.commit()
        parent_id = parent.id
        archive_post(post.id, None)
        db.session.expire_all()
        entries = archive_of(db.session.get(Post, post.id))['replies']
        # The archive keeps the tree rather than flattening it: each entry
        # carries its own `replies`, which is what `serialize_tree` recurses
        # into. So a child is inside its parent, not beside it.
        assert len(entries) == 1
        assert entries[0]['body'] == 'parent'
        assert len(entries[0]['replies']) == 1
        assert entries[0]['replies'][0]['body'] == 'child'
        assert entries[0]['replies'][0]['parent_id'] == parent_id
        assert entries[0]['replies'][0]['depth'] == 1
