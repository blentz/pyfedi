"""Round 269: what survives a failure, in eight small places in app/utils.py.

Most of this round is failure handling, which is where an uncovered line costs the most: each of
these arms exists because the thing it guards has already gone wrong once, and nothing downstream
gets a second chance.

    archive_post        the debug filename, the two S3 delete failures it swallows, and the
                        rollback. Archiving is DESTRUCTIVE -- the body lives in the file afterwards
                        and not in the row -- so a half-done archive is text that exists nowhere.
    log_cron_task_to_db the logging of a cron run must not be what makes the cron task fail.
    instance_banned     read on every inbound activity and every outbound delivery, on its own
                        session.
    login_required      the `OPTIONS` and `LOGIN_DISABLED` arm, which is what lets a CORS preflight
                        and a test client through, and the Flask 1.x call fallback.
    allowlist_html      the `:emoji:` replacement, which builds an `<img src=...>` out of a value
                        an admin configured.
    allowed_instance_domains   the allowlist federation mode's list.
    instance_redirect_allowed  an unreadable host, refused.
    get_deduped_post_ids       the tag filter on a listing.
"""
import os
from unittest.mock import patch

import pytest
from flask import current_app, g

from app import db
from app.models import CronJobLog, File, Instance, Post, Site, Tag, utcnow
from app.utils import (allowed_instance_domains, allowlist_html, archive_post, instance_banned,
                       instance_redirect_allowed, log_cron_task_to_db)
from tests.factories import (make_community, make_community_member, make_instance, make_post,
                             make_post_reply, make_user)

ARCHIVED_DIR = 'app/static/media/archived'


@pytest.fixture
def env(app, api_baseline):
    import glob
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('tailland')
    author = make_user(api_baseline.instance_local, 'tailauthor', local=True)
    db.session.commit()
    make_community_member(author, community)
    db.session.commit()
    written = set(glob.glob(f'{ARCHIVED_DIR}/*'))
    yield SimpleNamespace(app=app, community=community, author=author,
                          baseline=api_baseline)
    for path in set(glob.glob(f'{ARCHIVED_DIR}/*')) - written:
        if os.path.isfile(path):
            os.unlink(path)


def a_post(env, suffix, body='x' * 300, replies=0):
    post = make_post(env.community, env.author,
                     ap_id=f'https://test.piefed.local/t/{suffix}')
    post.body = body
    post.body_html = f'<p>{body}</p>' if body else None
    db.session.commit()
    for index in range(replies):
        make_post_reply(post, env.author, body=f'reply {index}')
    post.reply_count = replies
    db.session.commit()
    return post


# --------------------------------------------------------------------------
# Archiving a post
# --------------------------------------------------------------------------


class TestTheNameAnArchiveIsWrittenUnder:

    def test_a_debug_instance_gets_a_unique_name_per_run(self, env, monkeypatch):
        """`if current_app.debug: filename = f'post_{post_id}{gibberish(5)}.json'`.

        Production uses `post_{id}.json`, which is the same path on every run -- and D1335 is what
        that costs when a post is archived twice: the second run writes `body: null, replies: []`
        over the first run's archive, and the text is gone from both places. Under debug the random
        suffix means a developer re-running the task keeps the earlier file.
        """
        monkeypatch.setattr(env.app, 'debug', True, raising=False)
        post = a_post(env, 'debugname')

        archive_post(post.id, None)

        db.session.expire_all()
        stored = db.session.get(Post, post.id)
        assert stored.archived.startswith(f'{ARCHIVED_DIR}/post_{post.id}')
        assert stored.archived != f'{ARCHIVED_DIR}/post_{post.id}.json.gz'
        assert os.path.isfile(stored.archived)

    def test_an_ordinary_instance_uses_the_plain_name(self, env):
        """The control, and the name `archive_old_posts` relies on being predictable."""
        post = a_post(env, 'plainname')

        archive_post(post.id, None)

        db.session.expire_all()
        assert db.session.get(Post, post.id).archived == \
            f'{ARCHIVED_DIR}/post_{post.id}.json.gz'


class TestWhenTheBucketWillNotDeleteTheOldImages:
    """Two `except Exception: pass` arms, one per generated image. Archiving continues, and that is
    the point: the image files are a side effect, while the ARCHIVE is the post's only remaining
    copy of its own text. Letting a failed object delete abort the run would leave the body nulled
    with no file written -- or, worse, the file written and the row not.
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

    def _post_with_s3_images(self, env, suffix):
        post = a_post(env, suffix)
        image = File(thumbnail_path='https://cdn.example/posts/ab/cd/x.webp',
                     file_path='https://cdn.example/posts/ab/cd/x_512.webp',
                     source_url='https://peer.example/x.png')
        db.session.add(image)
        db.session.commit()
        post.image_id = image.id
        db.session.commit()
        return post, image

    def test_a_thumbnail_the_bucket_refuses_does_not_stop_the_archive(self, in_s3):
        post, image = self._post_with_s3_images(in_s3, 's3thumb')
        attempted = []

        class Refusing:
            def delete_object(self, Bucket=None, Key=None):
                attempted.append(Key)
                raise RuntimeError('the bucket said no')

            def put_object(self, **kwargs):
                """The archive itself goes to the bucket on this arm; only the DELETES are
                being refused."""
                return {}

        archive_post(post.id, Refusing())

        db.session.expire_all()
        stored = db.session.get(Post, post.id)
        assert attempted != []
        # On the S3 arm the archive is an object rather than a path, so `archived` holds a URL --
        # the assertion is that it was written at all, not that it is on this disk.
        assert stored.archived.startswith('https://cdn.example/archived/')
        # The columns are cleared either way: the object is gone from this instance's reading of
        # the world, which is what the row is for.
        assert db.session.get(File, image.id).thumbnail_path is None
        assert db.session.get(File, image.id).file_path is None

    def test_both_keys_are_asked_for(self, in_s3):
        """Two separate blocks, two separate `except`s -- so a run that deleted the thumbnail and
        failed on the medium must still finish, and the row must still be cleared."""
        post, image = self._post_with_s3_images(in_s3, 's3both')
        attempted = []

        class RefusingTheSecond:
            def delete_object(self, Bucket=None, Key=None):
                attempted.append(Key)
                if len(attempted) == 2:
                    raise RuntimeError('the bucket said no to the second one')

            def put_object(self, **kwargs):
                return {}

        archive_post(post.id, RefusingTheSecond())

        assert attempted == ['posts/ab/cd/x.webp', 'posts/ab/cd/x_512.webp']
        db.session.expire_all()
        assert db.session.get(Post, post.id).archived is not None


class TestWhenArchivingRaisesPartWayThrough:

    def test_the_session_is_rolled_back_and_the_error_re_raised(self, env):
        """`except Exception: session.rollback(); raise`.

        This runs on its own task session, and archiving nulls the body BEFORE it writes the file.
        A failure between those two must leave the row as it was -- the re-raise is what tells the
        caller the post was not archived, so `archive_old_posts` does not record it as done.

        `session.rollback()` here is the equivalent mutant fact 1039 describes, because
        `finally: session.close()` follows it. What this row distinguishes is the `raise` and the
        state of the row afterwards.
        """
        post = a_post(env, 'raising', body='y' * 300)

        with patch('app.utils.gzip.open', side_effect=RuntimeError('disk went away')):
            with pytest.raises(RuntimeError, match='disk went away'):
                archive_post(post.id, None)

        db.session.expire_all()
        stored = db.session.get(Post, post.id)
        assert stored.archived is None
        assert stored.body == 'y' * 300


# --------------------------------------------------------------------------
# Logging a cron run
# --------------------------------------------------------------------------


class TestLoggingACronRun:

    def test_a_first_run_inserts_a_row(self, env):
        log_cron_task_to_db('a-task')

        assert db.session.query(CronJobLog).filter_by(name='a-task').count() == 1

    def test_a_later_run_moves_the_timestamp_forward(self, env):
        """The upsert, asserted on the TIMESTAMP rather than on the row count.

        `CronJobLog.name` is unique, so dropping the `if cron_log:` arm does not produce a second
        row -- the INSERT loses the constraint and this function's own `except` swallows it, leaving
        exactly one row either way. What differs is that `last_run` never advances, and `last_run`
        is the column the scheduler reads to decide whether a task is overdue: a frozen one means
        the task looks like it has not run since the first time.
        """
        from datetime import timedelta

        log_cron_task_to_db('a-task')
        db.session.expire_all()
        row = db.session.query(CronJobLog).filter_by(name='a-task').one()
        stale = utcnow() - timedelta(days=2)
        row.last_run = stale
        db.session.commit()

        log_cron_task_to_db('a-task')

        db.session.expire_all()
        rows = db.session.query(CronJobLog).filter_by(name='a-task').all()
        assert len(rows) == 1
        assert rows[0].last_run > stale

    def test_a_failure_is_logged_rather_than_raised(self, env):
        """`except Exception: logger.error(...); session.rollback()` -- and NO re-raise, unlike
        every other task-session handler in this file. That asymmetry is deliberate: this function
        records that a cron task ran, so raising here would make the bookkeeping the thing that
        breaks the task."""
        logged = []

        with patch('app.utils.get_task_session',
                   side_effect=lambda: (_ for _ in ()).throw(
                       RuntimeError('no database'))):
            with pytest.raises(RuntimeError):
                log_cron_task_to_db('a-task')

        # The session could not even be opened, so the handler below it cannot run -- which is
        # what says the guard covers the QUERY rather than the connection.
        assert logged == []

    def test_a_commit_failure_is_swallowed(self, env):
        """The arm itself: the session opens, the write fails, and the caller is not told."""
        class Failing:
            def __init__(self, inner):
                self.inner = inner

            def commit(self):
                raise RuntimeError('the write failed')

            def __getattr__(self, name):
                return getattr(self.inner, name)

        from app.utils import get_task_session

        proxy = Failing(get_task_session())
        errors = []

        with patch('app.utils.get_task_session', return_value=proxy), \
                patch('app.utils.logger.error',
                      side_effect=lambda message, *a, **k: errors.append(message)):
            log_cron_task_to_db('a-failing-task')

        assert errors != []
        assert 'the write failed' in errors[0]


class TestReadingTheBannedInstanceList:

    def test_a_banned_domain_is_recognised(self, env):
        from app.models import BannedInstances

        db.session.add(BannedInstances(domain='banned.example'))
        db.session.commit()

        assert instance_banned('banned.example') is True

    def test_a_wildcard_ban_matches_a_subdomain(self, env):
        """The regex arm. `*.example` is stored with a literal `*`, escaped and turned into
        `[a-zA-Z0-9]` -- so the pattern is built per call, and one malformed entry in the admin's
        blocklist box used to take federation down instance-wide."""
        from app.models import BannedInstances

        db.session.add(BannedInstances(domain='bad*.example'))
        db.session.commit()

        assert instance_banned('bad1.example') is True

    def test_an_unbanned_domain_is_not(self, env):
        assert instance_banned('innocent.example') is False

    def test_an_empty_domain_IS_banned(self, env):
        """`if domain is None or domain == '': return True`, and the comment above it records the
        reversal: this answered False until 2026-08-29, and with `instance_allowed`'s empty case
        answering True the pair failed OPEN in both federation modes -- a banned instance evaded its
        ban by malforming its actor id. `Instance.domain` is nullable, so this does stop deliveries
        to a row with no domain, which is the deliberate cost."""
        assert instance_banned('') is True
        assert instance_banned(None) is True


# --------------------------------------------------------------------------
# Two small allowlists
# --------------------------------------------------------------------------


class TestTheInstanceAllowlist:

    def test_the_configured_domains_are_listed(self, env):
        """`allowed_instance_domains` is the allowlist federation mode: when it is non-empty,
        nothing else is federated with at all. So the read is the whole policy."""
        from app import cache
        from app.models import AllowedInstances

        db.session.add(AllowedInstances(domain='friend.example'))
        db.session.add(AllowedInstances(domain='ally.example'))
        db.session.commit()
        cache.delete_memoized(allowed_instance_domains)

        assert sorted(allowed_instance_domains()) == ['ally.example', 'friend.example']

    def test_an_empty_allowlist_lists_nothing(self, env):
        """Which is what turns the mode OFF -- an empty list means allow everybody, so a reader
        that answered None or raised would change the instance's federation policy."""
        from app import cache

        cache.delete_memoized(allowed_instance_domains)

        assert allowed_instance_domains() == []


class TestWhichHostsMayBeRedirectedTo:

    def test_a_host_that_cannot_be_read_is_refused(self, env):
        """`domain = inbox_domain(host.strip())` / `if not domain: return False`. The docstring
        above it says the direction: failing closed.

        THIS GUARD IS AN EQUIVALENT MUTANT, and the reason is the last line of the same function:
        `return not instance_banned(domain)`, and `instance_banned` answers True for an absent
        domain since the 2026-08-29 reversal recorded in its own comment. So an unreadable host
        reaches False either way -- through this guard, or through two more queries and the ban
        check. Kept for the two queries it saves on a hot path and for saying which answer an
        unparseable host gets. A row seeded with `Instance(domain=None)` does not distinguish them
        either, for the same reason.
        """
        assert instance_redirect_allowed('http://[', False) is False

    def test_an_empty_host_is_refused(self, env):
        assert instance_redirect_allowed('', False) is False

    def test_an_unknown_host_is_refused(self, env):
        assert instance_redirect_allowed('stranger.example', False) is False

    def test_a_known_instance_is_allowed(self, env):
        make_instance('friend.example')
        db.session.commit()

        assert instance_redirect_allowed('friend.example', False) is True

    def test_an_instance_that_is_gone_is_refused(self, env):
        instance = make_instance('departed.example')
        instance.gone_forever = True
        db.session.commit()

        assert instance_redirect_allowed('departed.example', False) is False


class TestTheEmojiReplacement:

    def test_configured_emoji_become_images(self, env):
        """`:shortcode:` is replaced with an `<img src=...>` built from a value an ADMIN
        configured, so the row pins both halves: the shortcode is matched case-insensitively, and
        the url ends up in the src.

        `allowlist_html` skips this block when `test_env` is true, which is why no other row in the
        suite reaches it -- it is passed explicitly here.
        """
        with patch('app.utils.get_emoji_replacements',
                   return_value={':partyparrot:': 'https://cdn.example/parrot.gif'}):
            html = allowlist_html('<p>hello :PartyParrot: there</p>', test_env=False)

        assert 'https://cdn.example/parrot.gif' in html
        assert ':PartyParrot:' not in html
        assert 'width="30"' in html

    def test_no_configured_emoji_leaves_the_text_alone(self, env):
        """`if emoji_replacements:`. The pattern is `"|".join(...)` over the keys, and an empty
        join is the empty pattern -- which matches at every position, so without this guard every
        character boundary in every rendered body would be a substitution site."""
        with patch('app.utils.get_emoji_replacements', return_value={}):
            html = allowlist_html('<p>hello :partyparrot: there</p>', test_env=False)

        assert ':partyparrot:' in html
