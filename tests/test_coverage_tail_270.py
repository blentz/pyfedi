"""Round 270: the last one-line arms across three modules.

A sweep rather than one function, grouped by what the line decides.

    the debug dispatch arms       `if current_app.debug: <task>(...) else: <task>.delay(...)`, in
                                  `flush_cdn_cache` and twice in `Post.delete_dependencies`. Under
                                  debug the work happens inline; in production it is queued, and a
                                  queued S3 delete that never ran is a file that stays in the bucket
                                  for ever.
    the reading-language list     `languages_for_form`'s arm for an account that HAS chosen reading
                                  languages, which decides which languages a post may be written in.
    the blocked-image sweep       `posts_with_blocked_images`, the admin's list of posts matching a
                                  PDQ-hashed blocklist.
    the tag filter                `get_deduped_post_ids`, where a hashtag narrows a listing.
    the federation warnings       `show_reason_why_no_federation`, which tells somebody their post
                                  will not reach the community they are looking at.
    `login_required`'s first arm  a CORS preflight and a `LOGIN_DISABLED` deployment, neither of
                                  which may be bounced to the login page.
    two small readers             `guess_mime_type`'s extension fallback, `Feed.creator`, and
                                  `Feed.parent_feed_name`.
"""
from unittest.mock import patch

import pytest
from flask import current_app, g

from app import db
from app.models import (Community, File, Instance, Language, Post, Site, Tag, User,
                        utcnow)
from app.models import flush_cdn_cache
from app.utils import (blocked_instances, get_deduped_post_ids, guess_mime_type,
                       languages_for_form, posts_with_blocked_images,
                       show_reason_why_no_federation)
from tests.factories import (make_community, make_community_member, make_feed,
                             make_instance, make_post, make_user)


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('sweepland')
    author = make_user(api_baseline.instance_local, 'sweepauthor', local=True)
    db.session.commit()
    make_community_member(author, community)
    db.session.commit()
    return SimpleNamespace(app=app, community=community, author=author,
                           baseline=api_baseline)


# --------------------------------------------------------------------------
# Inline under debug, queued in production
# --------------------------------------------------------------------------


class TestWhenTheCdnIsPurged:
    """`flush_cdn_cache` is called after a file is deleted, so what it purges is the difference
    between a removed image still being served from the edge and not. Both arms matter, and the
    `zone_id and token` guard above them is what keeps an instance with no CDN from queueing work
    nobody will do.
    """

    @pytest.fixture
    def configured(self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'CLOUDFLARE_ZONE_ID', 'a zone')
        monkeypatch.setitem(current_app.config, 'CLOUDFLARE_API_TOKEN', 'a token')
        return env

    def test_a_purge_runs_inline_under_debug(self, configured, monkeypatch):
        monkeypatch.setattr(configured.app, 'debug', True, raising=False)
        called = []

        with patch('app.models.flush_cdn_cache_task',
                   side_effect=lambda url: called.append(url)):
            flush_cdn_cache('https://cdn.example/x.webp')

        assert called == ['https://cdn.example/x.webp']

    def test_a_purge_is_queued_otherwise(self, configured, monkeypatch):
        monkeypatch.setattr(configured.app, 'debug', False, raising=False)
        queued = []

        class Task:
            @staticmethod
            def delay(url):
                queued.append(url)

        with patch('app.models.flush_cdn_cache_task', Task):
            flush_cdn_cache('https://cdn.example/x.webp')

        assert queued == ['https://cdn.example/x.webp']

    def test_an_instance_with_no_cdn_purges_nothing(self, env, monkeypatch):
        """`if zone_id and token`. Without the guard every file delete on an instance with no CDN
        configured queues a task that can only fail."""
        monkeypatch.setitem(current_app.config, 'CLOUDFLARE_ZONE_ID', '')
        monkeypatch.setitem(current_app.config, 'CLOUDFLARE_API_TOKEN', '')
        called = []

        class Task:
            @staticmethod
            def delay(url):
                called.append(url)

        with patch('app.models.flush_cdn_cache_task', Task):
            flush_cdn_cache('https://cdn.example/x.webp')

        assert called == []


class TestWhenAPostsVideoIsDeletedFromTheBucket:
    """Two more copies of the same dispatch, in `Post.delete_dependencies`: one for a mirrored
    video and one for an archived post's JSON. Each `delay` is a file in object storage that
    nothing will ever name again if the task is not queued -- D1343 is the finding that the key
    itself used to be wrong here.
    """

    @pytest.fixture
    def in_s3(self, env, monkeypatch):
        for key, value in (('S3_ACCESS_KEY', 'a key'),
                           ('S3_ACCESS_SECRET', 'a secret'),
                           ('S3_ENDPOINT', 'https://s3.example'),
                           ('S3_BUCKET', 'a-bucket'),
                           ('S3_PUBLIC_URL', 'cdn.example')):
            monkeypatch.setitem(current_app.config, key, value)
        return env

    def _video_post(self, env, suffix):
        from app.constants import POST_TYPE_VIDEO

        post = make_post(env.community, env.author,
                         ap_id=f'https://test.piefed.local/v/{suffix}')
        post.type = POST_TYPE_VIDEO
        post.url = 'https://cdn.example/videos/ab/cd/clip.mp4'
        db.session.commit()
        return post

    def test_the_delete_runs_inline_under_debug(self, in_s3, monkeypatch):
        monkeypatch.setattr(in_s3.app, 'debug', True, raising=False)
        post = self._video_post(in_s3, 'debug')
        deleted = []

        with patch('app.shared.tasks.maintenance.delete_from_s3',
                   side_effect=lambda keys: deleted.append(keys)):
            post.delete_dependencies()

        assert deleted == [['videos/ab/cd/clip.mp4']]

    def test_the_delete_is_queued_otherwise(self, in_s3, monkeypatch):
        monkeypatch.setattr(in_s3.app, 'debug', False, raising=False)
        post = self._video_post(in_s3, 'queued')
        queued = []

        class Task:
            def __call__(self, keys):
                raise AssertionError('called directly rather than queued')

            @staticmethod
            def delay(keys):
                queued.append(keys)

        with patch('app.shared.tasks.maintenance.delete_from_s3', Task()):
            post.delete_dependencies()

        assert queued == [['videos/ab/cd/clip.mp4']]

    def test_an_archived_posts_json_is_deleted_inline_under_debug(self, in_s3, monkeypatch):
        """The second copy, for `Post.archived`. Same shape, separate lines -- and an archive left
        in the bucket is the post's own text, kept after the post was deleted."""
        monkeypatch.setattr(in_s3.app, 'debug', True, raising=False)
        post = make_post(in_s3.community, in_s3.author,
                         ap_id='https://test.piefed.local/v/archived')
        post.archived = 'https://cdn.example/archived/post_9.json.gz'
        db.session.commit()
        deleted = []

        with patch('app.shared.tasks.maintenance.delete_from_s3',
                   side_effect=lambda keys: deleted.append(keys)):
            post.delete_dependencies()

        assert deleted == [['archived/post_9.json.gz']]


# --------------------------------------------------------------------------
# Which languages a form offers
# --------------------------------------------------------------------------


class TestWhichLanguagesAFormOffers:

    def _languages(self, env):
        english = Language(code='en', name='English')
        german = Language(code='de', name='German')
        french = Language(code='fr', name='French')
        db.session.add_all([english, german, french])
        db.session.commit()
        return english, german, french

    def test_an_accounts_chosen_reading_languages_are_offered(self, env):
        """`for language in Language.query.filter(Language.id.in_(current_user.read_language_ids))`.

        This is the list a post may be WRITTEN in, so it decides who can read what somebody
        publishes -- and the arm is reached only for an account that has chosen reading languages,
        which is the one whose choice it is honouring. Ordered by name, because it is rendered as a
        select.
        """
        english, german, french = self._languages(env)
        env.author.read_language_ids = [german.id, english.id]
        db.session.commit()

        with env.app.test_request_context('/'):
            with patch('app.utils.current_user', env.author):
                offered = languages_for_form()

        names = [name for _id, name in offered if name]
        assert 'English' in names and 'German' in names
        assert 'French' not in names
        assert names == sorted(names)

    def test_an_account_with_no_choice_is_offered_the_site_language(self, env):
        """The `if not used_languages:` fallback below it. An account that has chosen nothing must
        still be offered something, or the form cannot be submitted at all."""
        english, german, french = self._languages(env)
        env.author.read_language_ids = []
        db.session.commit()

        with env.app.test_request_context('/'):
            with patch('app.utils.current_user', env.author):
                offered = languages_for_form()

        assert offered != []


# --------------------------------------------------------------------------
# The blocked-image sweep
# --------------------------------------------------------------------------


class TestThePostsMatchingABlockedImage:
    """`posts_with_blocked_images` is the admin's list of posts whose image matches the PDQ-hash
    blocklist -- the mechanism for removing an image an instance must not host. The join is a
    HAMMING DISTANCE (`length(replace((a # b)::text, '0', '')) < 15`), so it matches near-duplicates
    rather than exact bytes, which is the whole point.
    """

    def _blocked(self, hash_bits):
        db.session.execute(
            db.text("INSERT INTO blocked_image (hash, note) VALUES (:hash, :note)"),
            {'hash': hash_bits, 'note': 'a probe'})
        db.session.commit()

    def _post_with_hash(self, env, suffix, hash_bits):
        image = File(source_url=f'https://peer.example/{suffix}.png', hash=hash_bits)
        db.session.add(image)
        db.session.commit()
        post = make_post(env.community, env.author,
                         ap_id=f'https://test.piefed.local/b/{suffix}')
        post.image_id = image.id
        db.session.commit()
        return post

    def test_a_post_whose_image_matches_is_listed(self, env):
        bits = '0' * 256
        self._blocked(bits)
        post = self._post_with_hash(env, 'match', bits)

        assert post.id in posts_with_blocked_images()

    def test_a_post_whose_image_does_not_match_is_not(self, env):
        self._blocked('0' * 256)
        post = self._post_with_hash(env, 'nomatch', '1' * 256)

        assert post.id not in posts_with_blocked_images()

    def test_a_deleted_post_is_not_listed(self, env):
        """`WHERE post.deleted = false`. A deleted post is already gone from every page, and
        listing it would have an admin reviewing content nobody can see."""
        bits = '0' * 256
        self._blocked(bits)
        post = self._post_with_hash(env, 'deleted', bits)
        post.deleted = True
        db.session.commit()

        assert post.id not in posts_with_blocked_images()

    def test_a_post_with_no_hashed_image_is_not_listed(self, env):
        """`AND file.hash is not null`. Without it the `#` operator is applied to NULL, which is
        NULL, whose text length is not less than 15 -- but the row is still read for every post on
        the instance."""
        image = File(source_url='https://peer.example/plain.png')
        db.session.add(image)
        db.session.commit()
        post = make_post(env.community, env.author,
                         ap_id='https://test.piefed.local/b/nohash')
        post.image_id = image.id
        db.session.commit()
        self._blocked('0' * 256)

        assert post.id not in posts_with_blocked_images()


# --------------------------------------------------------------------------
# Why a post will not federate
# --------------------------------------------------------------------------


class TestWhyAPostWillNotFederate:
    """`show_reason_why_no_federation` flashes a warning on a community's page. It is the only
    place somebody is told that their own block or their instance's ban means what they write here
    will never leave -- so an unflashed warning is a post that silently goes nowhere.
    """

    def test_an_instance_the_reader_blocked_is_explained(self, env):
        from app import cache
        from app.models import InstanceBlock

        peer = make_instance('blocked.example')
        db.session.commit()
        db.session.add(InstanceBlock(user_id=env.author.id, instance_id=peer.id))
        db.session.commit()
        cache.delete_memoized(blocked_instances, env.author.id)
        flashed = []

        with env.app.test_request_context('/'):
            with patch('app.utils.current_user', env.author), \
                    patch('app.utils.flash',
                          side_effect=lambda message, category=None: flashed.append(message)):
                show_reason_why_no_federation(peer.id)

        assert len(flashed) == 1
        assert 'blocked.example' in str(flashed[0])

    def test_an_instance_with_no_block_or_ban_says_nothing(self, env):
        peer = make_instance('fine.example')
        db.session.commit()
        flashed = []

        with env.app.test_request_context('/'):
            with patch('app.utils.current_user', env.author), \
                    patch('app.utils.flash',
                          side_effect=lambda message, category=None: flashed.append(message)):
                show_reason_why_no_federation(peer.id)

        assert flashed == []


# --------------------------------------------------------------------------
# Two small readers
# --------------------------------------------------------------------------


class TestGuessingAMimeType:
    """D1435. An `if content_type is None:` arm stood above the live one, with the same fallback
    written out twice, and it was UNREACHABLE: `mimetypes.guess_type` answers a `(type, encoding)`
    tuple and never None, so `content_type[0] is None` was always the test that mattered. The
    duplicate is deleted; these rows are what says the surviving arm does both jobs.
    """

    def test_a_known_extension_is_guessed(self, env):
        assert guess_mime_type('posts/ab/cd/x.webp') == 'image/webp'

    def test_a_known_non_image_type_is_not_forced_to_an_image(self, env):
        """The fallback answers `image/<ext>`, which happens to be RIGHT for `.webp` and wrong for
        everything else -- so the row that distinguishes the two arms has to use a non-image type.
        `archive_post` uploads a `.json.gz` through this function, and the value becomes the
        object's `ContentType`, which is what a browser obeys."""
        assert guess_mime_type('archived/post_1.json') == 'application/json'
        assert guess_mime_type('archived/post_1.json.gz') != 'image/gz'

    def test_an_unknown_extension_becomes_an_image_of_that_type(self, env):
        """The fallback arm. `mimetypes.guess_type` answers `(None, None)` for an extension it does
        not know, and this value becomes the `ContentType` of an S3 object -- which is what a
        browser obeys. Guessing `image/<ext>` is how a format newer than the Python install still
        gets served as an image."""
        assert guess_mime_type('posts/ab/cd/x.jxl') == 'image/jxl'

    def test_no_extension_at_all_becomes_octet_stream(self, env):
        """The other side: no extension is no guess, and `application/octet-stream` is what a
        browser DOWNLOADS rather than renders -- the safe answer for bytes nobody can name."""
        assert guess_mime_type('posts/ab/cd/x') == 'application/octet-stream'


class TestWhatAFeedSaysAboutItsPeople:

    def test_the_creator_is_named_by_ap_id_when_there_is_one(self, env):
        """`owner.ap_id if owner.ap_id else owner.user_name`. A remote account's `ap_id` is
        `name@host`, which is what identifies them off this instance; a local account has none, and
        its bare username is right because the feed's own url already says the host."""
        remote = make_user(env.baseline.instance_remote, 'remotefeedowner')
        db.session.commit()
        feed = make_feed(env.baseline.instance_remote, 'remotefeed')
        feed.user_id = remote.id
        db.session.commit()

        assert feed.creator() == remote.ap_id

    def test_a_local_creator_is_named_by_username(self, env):
        feed = make_feed(env.baseline.instance_local, 'localfeed')
        feed.user_id = env.author.id
        db.session.commit()
        assert env.author.ap_id is None

        assert feed.creator() == env.author.user_name

    def test_a_child_feed_names_its_parent(self, env):
        parent = make_feed(env.baseline.instance_local, 'parentfeed')
        parent.title = 'The Parent'
        child = make_feed(env.baseline.instance_local, 'childfeed')
        db.session.commit()
        child.parent_feed_id = parent.id
        db.session.commit()

        assert child.parent_feed_name() == 'The Parent'

    def test_a_top_level_feed_names_no_parent(self, env, recwarn):
        """D1434. The column is NULL for a top-level feed -- which is most of them -- and
        `db.session.get(Feed, None)` answers

            SAWarning: fully NULL primary key identity cannot load any object. This
            condition may raise an error in a future release.

        once per render. The `if parent_feed` below already treated it as no parent, so the fix is
        not to ask: the guard is the same one D1422 added to `RssFeedItem.delete_dependencies`.
        `recwarn` is what makes the repair assertable rather than only visible in the suite's
        warning count, which is this campaign's ratchet.
        """
        feed = make_feed(env.baseline.instance_local, 'toplevelfeed')
        feed.parent_feed_id = None
        db.session.commit()

        assert feed.parent_feed_name() == ''
        assert [str(warning.message) for warning in recwarn
                if 'NULL primary key' in str(warning.message)] == []
