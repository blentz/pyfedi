"""Group B of `app/shared/tasks/maintenance.py` -- the content-lifecycle tasks.

Sub-project 29 closed Group A, the ten tasks that need no transport, in
`tests/test_shared_tasks_maintenance_cleanup.py`. Group B is the eight that
move or destroy content:

  `process_expired_bans:76`            `remove_old_community_content:134`
  `remove_old_bot_content:161`         `delete_old_soft_deleted_content:216`
  `archive_old_posts:886`              `archive_old_users:934`
  `archive_user:958`                   `pwn_bots:1163`

TWENTY-THREE BRANCH POINTS AGAINST GROUP A'S SEVEN. Group A was straight-line
SQL in a repeated try/except skeleton. Group B branches on config values, on
whether a row still exists, on whether an image is shared between posts, and on
whether object storage is configured.

TWO TASKS DELETE THROUGH `app/shared/post.py`. `remove_old_community_content:150`
and `remove_old_bot_content:185` both call `delete_post`
(`app/shared/post.py:755`), which is a floored module at 40% with its own
federation behaviour. THIS FILE DOES NOT TEST `delete_post`. It tests which post
ids these two tasks hand over, by replacing `delete_post` in this module's
namespace with a recorder. Asserting on federation here would be testing another
module through a keyhole.

ONE TASK BUILDS A boto3 CLIENT. `archive_old_posts:912-919` constructs one when
`:911`'s `store_files_in_s3()` is true and passes it to `archive_post`. The
oracle is whether `archive_post` receives a client or `None` -- no S3 endpoint is
contacted, and `archive_post` is replaced by a recorder for the same reason
`delete_post` is.

THE TASK RUNS ON ITS OWN CONNECTION. `get_task_session()` returns
`Session(bind=db.engine)` (app/utils.py:3673-3675), so rows a test seeds must be
COMMITTED before the task runs, and an ORM attribute read afterwards is stale
unless the test calls `db.session.expire_all()` first (fact 153).
"""

from datetime import timedelta

import pytest
from sqlalchemy import event

from app import db
from app.constants import NOTIF_UNBAN
from app.models import (
    BotChallenge, CommunityBan, CommunityMember, File, InstanceBan,
    Notification, Post, PostBookmark, PostReply, User, utcnow,
)
from app.shared.tasks.maintenance import (
    archive_old_posts, archive_old_users, archive_user,
    delete_old_soft_deleted_content, process_expired_bans, pwn_bots,
    remove_old_bot_content, remove_old_community_content,
)
from tests.factories import (
    make_community, make_community_ban, make_community_member, make_file,
    make_instance, make_instance_ban, make_post, make_post_reply, make_user,
)


def _boom(*args, **kwargs):
    """Raise when patched in for a symbol used inside a task's `try`.

    Reaching the task's `except` arm this way is the caller's responsibility:
    pick a symbol the task actually calls from inside its `try`. `pwn_bots` is
    the case where the obvious choice -- `utcnow` -- does not work, because it
    computes its cutoff one line above the `try`.
    """
    raise RuntimeError('the task itself failed')


def _seed():
    """instance, a local user, a community, and one post -- all committed.

    Every factory here commits, so the task's own connection can see these
    rows. The first `make_instance` in a test receives id 1, because the
    `db_session` fixture resets sequences; tests that need a remote user build
    a second instance.
    """
    instance = make_instance('peer.example')
    user = make_user(instance, 'reader', local=True)
    community = make_community()
    post = make_post(community, user, 'https://peer.example/p/1')
    return instance, user, community, post


class _Recorder:
    """Replace a module-level callable and remember how it was called.

    Used for `delete_post` and `archive_post`, both of which belong to other
    modules this round does not test. `calls` holds one tuple of positional
    arguments per invocation.
    """

    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        return self.result


class TestPwnBots:
    """`pwn_bots:1163` -- everyone who ignored a bot challenge for 24h is a bot.

    `:1169` iterates challenges older than the cutoff whose `is_a_bot` is still
    NULL, `:1170` marks the user, and `:1173` marks the challenge. One branch
    point, the loop.

    `:1169` used to READ THROUGH `BotChallenge.query`, WHICH IS `db.session`,
    while `:1170` and `:1173` write through the task's own session, with the
    body NOT wrapped in `patch_db_session` -- this round's PC2. Task 8 spent a
    bounded effort trying to observe that split from a test and, unlike PC2 in
    sub-project 29, found one:
    `test_the_read_and_the_writes_share_one_connection_once_wrapped` below
    instruments `db.engine`'s `before_cursor_execute` and asserts the SELECT
    and the two UPDATEs run on the same connection checkout. It FAILS against the
    unpatched body (the SELECT runs on a connection distinct from the task's
    own) and PASSES once `:1168` gains `with patch_db_session(session):`,
    which is why `pwn_bots` now has that wrapper.
    """

    def test_a_challenge_ignored_past_the_cutoff_marks_the_user_a_bot(self, db_session):
        instance, user, _, _ = _seed()
        challenge = BotChallenge(uuid='c1', user_id=user.id,
                                 sent_at=utcnow() - timedelta(days=2))
        db.session.add(challenge)
        db.session.commit()

        pwn_bots()

        db.session.expire_all()
        refreshed = db.session.get(User, user.id)
        assert (refreshed.bot, refreshed.bot_override) == (True, True)
        assert db.session.get(BotChallenge, challenge.id).is_a_bot is True

    def test_a_challenge_inside_the_cutoff_is_left_alone(self, db_session):
        """The BOUNDARY. A deletion test alone passes against no cutoff at all."""
        instance, user, _, _ = _seed()
        challenge = BotChallenge(uuid='c2', user_id=user.id,
                                 sent_at=utcnow() - timedelta(hours=12))
        db.session.add(challenge)
        db.session.commit()

        pwn_bots()

        db.session.expire_all()
        assert db.session.get(User, user.id).bot is not True
        assert db.session.get(BotChallenge, challenge.id).is_a_bot is None

    def test_an_answered_challenge_is_left_alone(self, db_session):
        """`:1169`'s `is_a_bot == None` conjunct.

        A challenge someone answered has a non-NULL `is_a_bot`, and must not be
        reprocessed however old it is.
        """
        instance, user, _, _ = _seed()
        challenge = BotChallenge(uuid='c3', user_id=user.id,
                                 sent_at=utcnow() - timedelta(days=30),
                                 is_a_bot=False)
        db.session.add(challenge)
        db.session.commit()

        pwn_bots()

        db.session.expire_all()
        assert db.session.get(User, user.id).bot is not True
        assert db.session.get(BotChallenge, challenge.id).is_a_bot is False

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch):
        """`:1177-1179`'s handler, reached through `:1170`.

        NOT through `utcnow`. `:1166` computes the cutoff one line ABOVE
        `:1167`'s `try:`, so patching the clock raises before the handler
        exists and would pass even if the whole `except` clause were deleted.
        `pwn_bots` is the only task in this group that orders those two
        statements that way.

        A challenge must be seeded, because `:1170`'s `text(...)` is inside the
        loop and an empty result set never reaches it.
        """
        instance, user, _, _ = _seed()
        challenge = BotChallenge(uuid='c4', user_id=user.id,
                                 sent_at=utcnow() - timedelta(days=2))
        db.session.add(challenge)
        db.session.commit()
        monkeypatch.setattr('app.shared.tasks.maintenance.text', _boom)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            pwn_bots()

    def test_the_read_and_the_writes_share_one_connection_once_wrapped(self, db_session):
        """PC2's discriminator: which connection checkout issues each statement.

        `BotChallenge.query` at `:1169` and `session.execute` at `:1170`/
        `:1173` are two different session objects when the body is not
        wrapped in `patch_db_session`. Of Task 8's three candidate probes,
        only this one produced a result: a conflicting uncommitted write held
        during the task (candidate 1) deadlocked Postgres and was abandoned
        before it returned a verdict; checking whether wrapping changed any
        existing test's behaviour (candidate 3) was never run, because this
        probe already answered the question; and whether the two sessions see
        different data was never probed at all -- that is the brief's
        premise, not something established here. Which connection checkout
        each statement runs on is what this probe measures: a
        `before_cursor_execute` listener on `db.engine` records
        `id(conn.connection)` per statement, and the SELECT lands on a
        different checkout than the two UPDATEs whenever the two sessions are
        actually different objects.
        """
        instance, user, _, _ = _seed()
        challenge = BotChallenge(uuid='c5', user_id=user.id,
                                 sent_at=utcnow() - timedelta(days=2))
        db.session.add(challenge)
        db.session.commit()

        seen = []

        def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
            kind = statement.strip().split(None, 1)[0].upper()
            seen.append((id(conn.connection), kind))

        event.listen(db.engine, 'before_cursor_execute', before_cursor_execute)
        try:
            pwn_bots()
        finally:
            event.remove(db.engine, 'before_cursor_execute', before_cursor_execute)

        select_conns = {c for c, k in seen if k == 'SELECT'}
        update_conns = {c for c, k in seen if k == 'UPDATE'}
        assert select_conns and update_conns
        assert select_conns == update_conns


class TestProcessExpiredBans:
    """`process_expired_bans:76` -- lift community bans whose term has run out.

    `:80` selects `CommunityBan` rows past `ban_until`; `:89` clears the
    membership's `is_banned`; `:94`'s guard decides whether a notification is
    written; `:116` deletes the ban row. A second loop at `:119-123` clears
    expired `InstanceBan` rows.

    `CommunityBan` has a COMPOSITE primary key (app/models.py:3576-3577) and no
    `id` column, so these tests query by both keys rather than by an id the
    factory never returned.

    `:111-114` and `:121-122` call `cache.delete_memoized` six times between
    them. These tests deliberately do not assert on those calls -- the cache is
    a real Flask-Caching instance shared across the suite, and asserting on
    invalidation would couple this file to another subsystem's internals.
    """

    def _expired_ban(self, user, community):
        return make_community_ban(user, community,
                                  ban_until=utcnow() - timedelta(days=1))

    def _ban_row(self, user, community):
        return db.session.query(CommunityBan).filter_by(
            user_id=user.id, community_id=community.id).first()

    def test_an_expired_ban_is_deleted(self, db_session):
        instance, user, community, _ = _seed()
        self._expired_ban(user, community)

        process_expired_bans()

        db.session.expire_all()
        assert self._ban_row(user, community) is None

    def test_a_ban_still_running_survives(self, db_session):
        """The BOUNDARY -- `:80`'s `ban_until < utcnow()`."""
        instance, user, community, _ = _seed()
        make_community_ban(user, community,
                           ban_until=utcnow() + timedelta(days=1))

        process_expired_bans()

        db.session.expire_all()
        assert self._ban_row(user, community) is not None

    def test_a_permanent_ban_has_no_ban_until_and_survives(self, db_session):
        """`ban_until` is NULL for a permanent ban, and `NULL < timestamp` is
        UNKNOWN, which `WHERE` treats as false. This test pins the BEHAVIOUR --
        a permanent ban is not lifted -- rather than any one conjunct, and it
        would still pass if the comparison were written another way that also
        excluded NULLs.
        """
        instance, user, community, _ = _seed()
        make_community_ban(user, community, ban_until=None)

        process_expired_bans()

        db.session.expire_all()
        assert self._ban_row(user, community) is not None

    def test_the_membership_is_unbanned(self, db_session):
        """`:88`'s true arm -- a membership row exists, so `:89` clears it."""
        instance, user, community, _ = _seed()
        membership = make_community_member(user, community)
        membership.is_banned = True
        db.session.commit()
        self._expired_ban(user, community)

        process_expired_bans()

        db.session.expire_all()
        assert db.session.query(CommunityMember).filter_by(
            user_id=user.id, community_id=community.id).first().is_banned is False

    def test_a_ban_with_no_membership_row_still_clears(self, db_session):
        """`:88`'s FALSE arm. A user banned without ever having joined has no
        `CommunityMember` row, and the task must still delete the ban.
        """
        instance, user, community, _ = _seed()
        self._expired_ban(user, community)

        process_expired_bans()

        db.session.expire_all()
        assert self._ban_row(user, community) is None

    def test_a_local_user_is_notified(self, db_session):
        """`:94`'s true arm -- `:97-106` writes a NOTIF_UNBAN notification and
        `:107` increments the unread counter.
        """
        instance, user, community, _ = _seed()
        self._expired_ban(user, community)

        process_expired_bans()

        db.session.expire_all()
        notifications = db.session.query(Notification).filter_by(
            user_id=user.id, notif_type=NOTIF_UNBAN).all()
        assert len(notifications) == 1
        assert db.session.get(User, user.id).unread_notifications == 1

    def test_a_remote_user_is_not_notified(self, db_session):
        """`:94`'s false arm, via `is_local()`.

        `make_user` with `local=False` sets `ap_id`, and `User.is_local()`
        (app/models.py:1251-1252) is false once `ap_id` is set and
        `ap_profile_id` does not start with SERVER_URL. The ban is still
        deleted -- only the notification is skipped.
        """
        local_instance = make_instance('local.example')
        remote_instance = make_instance('remote.example')
        remote_user = make_user(remote_instance, 'visitor')
        community = make_community()
        make_community_ban(remote_user, community,
                           ban_until=utcnow() - timedelta(days=1))

        process_expired_bans()

        db.session.expire_all()
        assert db.session.query(Notification).filter_by(
            user_id=remote_user.id).count() == 0
        assert db.session.query(CommunityBan).filter_by(
            user_id=remote_user.id, community_id=community.id).first() is None

    def test_an_expired_instance_ban_is_deleted(self, db_session):
        """`:119-123`'s second loop, which no community-ban test reaches."""
        instance, user, _, _ = _seed()
        ban = make_instance_ban(user, instance)
        ban.banned_until = utcnow() - timedelta(days=1)
        db.session.commit()

        process_expired_bans()

        db.session.expire_all()
        assert db.session.query(InstanceBan).filter_by(
            user_id=user.id, instance_id=instance.id).first() is None

    def test_a_permanent_instance_ban_survives(self, db_session):
        """`:119`'s `banned_until != None` conjunct, which -- unlike the
        community-ban case -- is written explicitly rather than left to SQL's
        three-valued logic. This test pins the behaviour; it cannot by itself
        prove the explicit conjunct is load-bearing, since the comparison beside
        it would exclude NULLs anyway.
        """
        instance, user, _, _ = _seed()
        make_instance_ban(user, instance)

        process_expired_bans()

        db.session.expire_all()
        assert db.session.query(InstanceBan).filter_by(
            user_id=user.id, instance_id=instance.id).first() is not None

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch):
        monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            process_expired_bans()


class TestRemoveOldCommunityContent:
    """`remove_old_community_content:134` -- honour per-community retention.

    `:138` selects communities with `content_retention > 0` (the column
    defaults to -1, app/models.py:581), `:143-147` picks their posts older than
    the cutoff that are neither deleted nor sticky, and `:150` hands each id to
    `delete_post`.

    `delete_post` IS REPLACED BY A RECORDER IN THESE TESTS. It belongs to
    `app/shared/post.py`, a floored module at 40% with its own federation
    behaviour, and this round tests which ids reach it rather than what it then
    does.
    """

    def test_a_post_older_than_the_retention_window_is_handed_over(self, db_session, monkeypatch):
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.delete_post', recorder)
        instance, user, community, post = _seed()
        community.content_retention = 7
        post.posted_at = utcnow() - timedelta(days=8)
        db.session.commit()

        remove_old_community_content()

        assert [c[0] for c in recorder.calls] == [post.id]

    def test_a_post_inside_the_window_is_left_alone(self, db_session, monkeypatch):
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.delete_post', recorder)
        instance, user, community, post = _seed()
        community.content_retention = 7
        post.posted_at = utcnow() - timedelta(days=6)
        db.session.commit()

        remove_old_community_content()

        assert recorder.calls == []

    def test_a_community_with_no_retention_policy_is_skipped(self, db_session, monkeypatch):
        """`:138`'s filter. The column's default of -1 means "keep forever"."""
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.delete_post', recorder)
        instance, user, community, post = _seed()
        post.posted_at = utcnow() - timedelta(days=400)
        db.session.commit()

        remove_old_community_content()

        assert recorder.calls == []

    def test_a_community_with_a_zero_retention_is_skipped(self, db_session, monkeypatch):
        """`:138`'s `> 0` boundary, not just its FALSE arm at -1.

        A `content_retention` of exactly 0 must ALSO fail the filter -- a
        mutant widening it to `>= 0` opens the gate at zero and, via `:142`'s
        `cut_off = utcnow() - timedelta(days=0)`, would hand every post in
        the community to `delete_post` regardless of age.
        """
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.delete_post', recorder)
        instance, user, community, post = _seed()
        community.content_retention = 0
        post.posted_at = utcnow() - timedelta(days=400)
        db.session.commit()

        remove_old_community_content()

        assert recorder.calls == []

    def test_a_sticky_or_already_deleted_post_is_skipped(self, db_session, monkeypatch):
        """`:143-147`'s `deleted=False, sticky=False` filter.

        Both are asserted in one test because each alone would leave the other
        conjunct unexercised, and the task's behaviour for both is the same.
        """
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.delete_post', recorder)
        instance, user, community, post = _seed()
        community.content_retention = 7
        post.posted_at = utcnow() - timedelta(days=8)
        post.sticky = True
        gone = make_post(community, user, 'https://peer.example/p/2')
        gone.posted_at = utcnow() - timedelta(days=8)
        gone.deleted = True
        db.session.commit()

        remove_old_community_content()

        assert recorder.calls == []

    def test_the_deletion_is_not_federated(self, db_session, monkeypatch):
        """`:150` passes False for `delete_post`'s SECOND parameter, which is
        `federate_deletion` (app/shared/post.py:755) and not a locality flag.

        A retention-policy deletion is deliberately not federated, so remote
        instances keep the post. `remove_old_bot_content:185` passes a different
        value for the same parameter; the two are separate policies, not an
        inconsistency.
        """
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.delete_post', recorder)
        instance, user, community, post = _seed()
        community.content_retention = 7
        post.posted_at = utcnow() - timedelta(days=8)
        db.session.commit()

        remove_old_community_content()

        assert recorder.calls[0][1] is False

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch):
        """`:142`'s cutoff, not `:138`'s query, is what must raise.

        `utcnow` is read once per community inside the `for` loop (`:142`), so
        a community must be seeded WITH a retention policy or the loop body --
        and therefore `_boom` -- is never reached, and the task returns
        normally instead of propagating.
        """
        instance, user, community, post = _seed()
        community.content_retention = 7
        db.session.commit()
        monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            remove_old_community_content()


class TestRemoveOldBotContent:
    """`remove_old_bot_content:161` -- delete replyless bot posts.

    `:168`'s `if bot_retention > 0:` gates the whole body on
    `BOT_CONTENT_RETENTION` (config.py:190, default 6, with -1 documented as
    "forever, no deletion"), and `:169`'s cutoff is 28 days per unit.
    `:171-176` selects non-deleted, non-sticky, replyless posts by bots, and
    `:180`'s loop batches them 100 at a time.

    THIS FUNCTION NEVER COMMITS -- a registered finding of this round, not
    fixed by these tests. It gained a `@celery.task` decorator this round
    (PC3), making it dispatchable with `.delay()`; nothing in the tree calls
    it that way, and `app/cli.py`'s synchronous `daily_maintenance` command,
    its only caller, is unchanged.
    """

    def _bot_post(self, community, user, ap_id, age_days):
        post = make_post(community, user, ap_id)
        post.from_bot = True
        post.reply_count = 0
        post.posted_at = utcnow() - timedelta(days=age_days)
        return post

    def test_an_old_replyless_bot_post_is_handed_over(self, db_session, monkeypatch):
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.delete_post', recorder)
        instance, user, community, post = _seed()
        bot_post = self._bot_post(community, user, 'https://peer.example/p/2',
                                  28 * 6 + 1)
        db.session.commit()

        remove_old_bot_content()

        assert [c[0] for c in recorder.calls] == [bot_post.id]

    def test_a_recent_bot_post_is_left_alone(self, db_session, monkeypatch):
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.delete_post', recorder)
        instance, user, community, post = _seed()
        self._bot_post(community, user, 'https://peer.example/p/2', 28 * 6 - 1)
        db.session.commit()

        remove_old_bot_content()

        assert recorder.calls == []

    def test_a_bot_post_with_replies_is_left_alone(self, db_session, monkeypatch):
        """`:175`'s `reply_count=0`."""
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.delete_post', recorder)
        instance, user, community, post = _seed()
        bot_post = self._bot_post(community, user, 'https://peer.example/p/2',
                                  28 * 6 + 1)
        bot_post.reply_count = 3
        db.session.commit()

        remove_old_bot_content()

        assert recorder.calls == []

    def test_a_human_post_is_left_alone(self, db_session, monkeypatch):
        """`:174`'s `from_bot=True`."""
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.delete_post', recorder)
        instance, user, community, post = _seed()
        post.posted_at = utcnow() - timedelta(days=28 * 6 + 1)
        post.reply_count = 0
        db.session.commit()

        remove_old_bot_content()

        assert recorder.calls == []

    def test_a_retention_of_minus_one_disables_the_task(self, db_session, monkeypatch, app):
        """`:168`'s FALSE arm. -1 is documented as "forever, no deletion"."""
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.delete_post', recorder)
        instance, user, community, post = _seed()
        self._bot_post(community, user, 'https://peer.example/p/2', 28 * 6 + 1)
        db.session.commit()
        original = app.config['BOT_CONTENT_RETENTION']
        app.config['BOT_CONTENT_RETENTION'] = -1

        try:
            remove_old_bot_content()
        finally:
            app.config['BOT_CONTENT_RETENTION'] = original

        assert recorder.calls == []

    def test_a_retention_of_zero_also_disables_the_task(self, db_session, monkeypatch, app):
        """`:168`'s `> 0` boundary, not just its FALSE arm at -1.

        A `BOT_CONTENT_RETENTION` of exactly 0 must ALSO fail the gate -- a
        mutant widening it to `>= 0` opens the gate at zero and, via `:169`'s
        `cut_off = utcnow() - timedelta(days=28 * 0)` (i.e. now), would hand
        every replyless bot post to `delete_post` regardless of age.
        """
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.delete_post', recorder)
        instance, user, community, post = _seed()
        self._bot_post(community, user, 'https://peer.example/p/2', 28 * 6 + 1)
        db.session.commit()
        original = app.config['BOT_CONTENT_RETENTION']
        app.config['BOT_CONTENT_RETENTION'] = 0

        try:
            remove_old_bot_content()
        finally:
            app.config['BOT_CONTENT_RETENTION'] = original

        assert recorder.calls == []

    def test_the_deletion_federates_for_a_local_author(self, db_session, monkeypatch):
        """`:185` passes `post.author.is_local()` for `federate_deletion`,
        where `remove_old_community_content:150` passes a constant False.
        """
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.delete_post', recorder)
        instance, user, community, post = _seed()
        self._bot_post(community, user, 'https://peer.example/p/2', 28 * 6 + 1)
        db.session.commit()

        remove_old_bot_content()

        assert recorder.calls[0][1] is True

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch):
        monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            remove_old_bot_content()


class TestDeleteOldSoftDeletedContent:
    """`delete_old_soft_deleted_content:216` -- hard-delete after seven days.

    `:226-237` selects soft-deleted posts past the cutoff that are unbookmarked
    and either mod-deleted, retention-deleted (`deleted_by = 1`) or replyless.
    `:243-251` collects image ids shared by more than one post. `:255`'s
    compound then skips any post whose image is shared, because the cascade
    would fail -- the source comment at `:239-242` says so and defers the real
    fix.

    `:255` IS THREE CONDITIONS IN ONE ARC PAIR. coverage.py does not decompose
    conjunctions (fact 142), so branch coverage reads 100% with two of them
    untested. Each has its own test below.

    Three hard-delete assertions below capture the row's id into a local
    (`gone_id`/`reply_id`) BEFORE the task runs, and then query
    `db.session.query(...).filter_by(id=...).first() is None` rather than
    `db.session.get(<obj>.id) is None`. The row a factory builds is already
    in `db.session`'s identity map; once the task deletes it through its own
    `get_task_session()` connection, EITHER touching the ORM object's
    already-expired `.id` attribute OR calling `db.session.get()` against
    that identity-mapped instance triggers SQLAlchemy's refresh-on-expired
    path, which finds no row and raises `ObjectDeletedError` instead of
    quietly returning `None`. Reading the id first and querying by that
    plain value afterward avoids touching the stale instance at all.
    """

    def _soft_deleted_post(self, community, user, ap_id, age_days=8):
        post = make_post(community, user, ap_id)
        post.deleted = True
        post.deleted_by = 1
        post.reply_count = 0
        post.posted_at = utcnow() - timedelta(days=age_days)
        return post

    def test_an_old_soft_deleted_post_is_hard_deleted(self, db_session):
        instance, user, community, post = _seed()
        gone = self._soft_deleted_post(community, user, 'https://peer.example/p/2')
        db.session.commit()
        gone_id = gone.id

        delete_old_soft_deleted_content()

        db.session.expire_all()
        assert db.session.query(Post).filter_by(id=gone_id).first() is None

    def test_a_recently_deleted_post_survives(self, db_session):
        """The BOUNDARY -- `:229`'s `p.posted_at < :cutoff`, seven days."""
        instance, user, community, post = _seed()
        recent = self._soft_deleted_post(community, user,
                                         'https://peer.example/p/2', age_days=6)
        db.session.commit()

        delete_old_soft_deleted_content()

        db.session.expire_all()
        assert db.session.get(Post, recent.id) is not None

    def test_a_post_whose_image_is_shared_survives(self, db_session):
        """`:255`'s third condition, and the reason `:243-251` exists.

        Two posts referencing one File make the delete cascade fail, so the
        task deliberately skips them. The source comment at `:239-242` records
        that this is a workaround rather than a fix.
        """
        instance, user, community, post = _seed()
        shared = make_file(file_path='/static/shared.png')
        first = self._soft_deleted_post(community, user, 'https://peer.example/p/2')
        first.image_id = shared.id
        second = make_post(community, user, 'https://peer.example/p/3')
        second.image_id = shared.id
        db.session.commit()

        delete_old_soft_deleted_content()

        db.session.expire_all()
        assert db.session.get(Post, first.id) is not None

    def test_a_post_with_its_own_image_is_deleted(self, db_session):
        """`:255`'s second condition taking its false arm while the third takes
        its true arm -- the image exists but is not shared.
        """
        instance, user, community, post = _seed()
        own = make_file(file_path='/static/own.png')
        gone = self._soft_deleted_post(community, user, 'https://peer.example/p/2')
        gone.image_id = own.id
        db.session.commit()
        gone_id = gone.id

        delete_old_soft_deleted_content()

        db.session.expire_all()
        assert db.session.query(Post).filter_by(id=gone_id).first() is None

    def test_a_bookmarked_post_survives(self, db_session):
        """`:230-234`'s NOT EXISTS against post_bookmark."""
        instance, user, community, post = _seed()
        kept = self._soft_deleted_post(community, user, 'https://peer.example/p/2')
        db.session.commit()
        db.session.add(PostBookmark(post_id=kept.id, user_id=user.id))
        db.session.commit()

        delete_old_soft_deleted_content()

        db.session.expire_all()
        assert db.session.get(Post, kept.id) is not None

    def test_an_old_soft_deleted_reply_is_hard_deleted(self, db_session):
        """`:261-274`'s second pass, which no post test reaches."""
        instance, user, community, post = _seed()
        reply = make_post_reply(post, user)
        reply.deleted = True
        reply.posted_at = utcnow() - timedelta(days=8)
        db.session.commit()
        reply_id = reply.id

        delete_old_soft_deleted_content()

        db.session.expire_all()
        assert db.session.query(PostReply).filter_by(id=reply_id).first() is None

    def test_a_reply_with_children_survives(self, db_session):
        """`:272`'s `if not post_reply.has_replies(include_deleted=True):`.

        A deleted reply that still has children is kept, because removing it
        would orphan them.
        """
        instance, user, community, post = _seed()
        parent = make_post_reply(post, user)
        parent.deleted = True
        parent.posted_at = utcnow() - timedelta(days=8)
        child = make_post_reply(post, user)
        child.parent_id = parent.id
        db.session.commit()

        delete_old_soft_deleted_content()

        db.session.expire_all()
        assert db.session.get(PostReply, parent.id) is not None

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch):
        monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            delete_old_soft_deleted_content()


class _GhostReplySession:
    """Wraps a real task session so a lookup of one chosen PostReply id returns
    `None`, standing in for that row having been deleted by ANOTHER session
    between the raw `SELECT` at `delete_old_soft_deleted_content:261-266` and
    the ORM lookup at `:269`. Every other call -- `.query(Post)`, a lookup of
    any other id, `.execute`, `.delete`, `.commit`, `.rollback`, `.close` --
    forwards to the real session untouched.

    BOTH lookup shapes are intercepted. Production moved from
    `session.query(PostReply).get(id)` to `session.get(PostReply, id)` in
    sub-project 71, and a double that knew only the old shape stopped standing
    in for anything: the ghost row was found, deleted, and the test failed on
    an assertion about the real row. `.query()` is kept so the double still
    describes the call it replaced.
    """

    def __init__(self, real_session, ghost_id):
        self._real = real_session
        self._ghost_id = ghost_id

    def __getattr__(self, name):
        return getattr(self._real, name)

    def get(self, entity, ident, *args, **kwargs):
        if entity is PostReply and ident == self._ghost_id:
            return None
        return self._real.get(entity, ident, *args, **kwargs)

    def query(self, *entities, **kwargs):
        real_query = self._real.query(*entities, **kwargs)
        if entities and entities[0] is PostReply:
            return _GhostQuery(real_query, self._ghost_id)
        return real_query


class _GhostQuery:
    """Forwards everything to the wrapped `Query` except `.get(ghost_id)`."""

    def __init__(self, real_query, ghost_id):
        self._real = real_query
        self._ghost_id = ghost_id

    def __getattr__(self, name):
        return getattr(self._real, name)

    def get(self, ident):
        if ident == self._ghost_id:
            return None
        return self._real.get(ident)


class TestDeleteOldSoftDeletedContentReplyRace:
    """`delete_old_soft_deleted_content:270`'s missing branch -- `if
    post_reply:` taking its FALSE arm.

    `:261-266` selects `post_reply` ids matching the retention criteria with
    one raw SQL `SELECT`, a snapshot at that instant. `:269` then re-fetches
    each id through the ORM, one at a time, in a separate round trip. Between
    those two steps another session can delete the row -- a concurrent
    request, another worker -- and `:270`'s `if post_reply:` guard exists for
    exactly that case, per its own comment ("Check if still exists"). A
    single-process test cannot make a genuine second connection race the
    first, so `_GhostReplySession` stands in for the race by wrapping the
    task's own session and making `.query(PostReply).get(ghost_id)` return
    `None` for the one id under test. `get_task_session` is imported into
    `app.shared.tasks.maintenance`'s namespace (`maintenance.py:19`), the
    same seam `TestRefreshInstanceChooser` in the external file patches for
    `get_request`.

    Deleting or inverting `:270`'s guard makes `post_reply.delete_dependencies()`
    run on `None`, raising `AttributeError` that escapes this task's own
    `try` to `:278`'s `raise` -- turning a row already gone into a crash for
    the whole task instead of a silent skip. The oracle is therefore BOTH
    that the task returns normally (a bare call, no `pytest.raises`) AND
    that the row -- untouched by this pass, since the ghost lookup never
    reaches `delete_dependencies` or `session.delete` -- still exists
    afterward; the guard's absence would fail on the first, a mutant that
    kept the guard but deleted anyway would fail on the second.
    """

    def test_a_reply_gone_before_the_get_is_left_alone(self, db_session, monkeypatch):
        instance, user, community, post = _seed()
        reply = make_post_reply(post, user)
        reply.deleted = True
        reply.posted_at = utcnow() - timedelta(days=8)
        db.session.commit()
        reply_id = reply.id

        from app.utils import get_task_session as _real_get_task_session

        def _ghost_get_task_session():
            return _GhostReplySession(_real_get_task_session(), reply_id)

        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_task_session', _ghost_get_task_session)

        delete_old_soft_deleted_content()

        db.session.expire_all()
        assert db.session.query(PostReply).filter_by(id=reply_id).first() is not None


class TestArchiveUser:
    """`archive_user:958` -- drop a user's avatar and cover.

    `:960` and `:965` guard the two images INDEPENDENTLY, so this helper
    handles a user with only one. `archive_old_users:943`'s query used to
    require BOTH -- that mismatch was this round's PC1, fixed by widening
    `:943` to an `OR`; `TestArchiveOldUsersReachesOneImageUsers` covers the
    widened query, not this class, which exercises the helper directly.

    Step 1 probed `File.delete_from_disk(purge_cdn=False)` (called at `:963`
    and `:968`) against a `make_file(file_path='/static/avatar.png')` row,
    whose path points nowhere. It passed unpatched: `delete_from_disk`
    (app/models.py:421) only reaches `os.unlink` behind
    `os.path.isfile(self.file_path)` (app/models.py:429), which is False for
    a nonexistent path, so the branch is skipped rather than raising. No
    monkeypatch of `File.delete_from_disk` is needed in this class.
    """

    def test_both_images_are_removed(self, db_session):
        instance, user, _, _ = _seed()
        avatar = make_file(file_path='/static/avatar.png')
        cover = make_file(file_path='/static/cover.png')
        user.avatar_id = avatar.id
        user.cover_id = cover.id
        db.session.commit()

        archive_user(user.id, db.session)

        db.session.expire_all()
        refreshed = db.session.get(User, user.id)
        assert (refreshed.avatar_id, refreshed.cover_id) == (None, None)
        assert db.session.get(File, avatar.id) is None
        assert db.session.get(File, cover.id) is None

    def test_a_user_with_only_an_avatar_is_handled(self, db_session):
        """`:960` true, `:965` false. The helper copes; before this round's
        PC1 fix, `archive_old_users`'s query never sent it such a user --
        `TestArchiveOldUsersReachesOneImageUsers` covers that the widened
        query now does.
        """
        instance, user, _, _ = _seed()
        avatar = make_file(file_path='/static/avatar.png')
        user.avatar_id = avatar.id
        db.session.commit()

        archive_user(user.id, db.session)

        db.session.expire_all()
        assert db.session.get(User, user.id).avatar_id is None

    def test_a_user_with_neither_image_flushes_nothing(self, db_session,
                                                      monkeypatch):
        """The empty arm of the CDN flush D1350 added here. A user with no avatar
        and no cover collects no URLs, and an empty list must not become a
        Cloudflare request for nothing."""
        from flask import current_app

        instance, user, _, _ = _seed()
        monkeypatch.setitem(current_app.config, 'CLOUDFLARE_ZONE_ID', 'zone')
        monkeypatch.setitem(current_app.config, 'CLOUDFLARE_API_TOKEN', 'token')
        flushed = []
        monkeypatch.setattr('app.shared.tasks.maintenance.flush_cdn_cache',
                            lambda urls: flushed.append(urls))

        archive_user(user.id, db.session)

        assert flushed == []

    def test_the_images_are_purged_from_the_cdn(self, db_session, monkeypatch):
        """D1350's `archive_user` half: both files were deleted with
        `purge_cdn=False`, so an archived user's avatar and cover went from disk
        and stayed at the edge. One flush for the two of them."""
        from flask import current_app

        instance, user, _, _ = _seed()
        avatar = make_file(file_path='app/static/media/users/aa/bb/avatar.png')
        cover = make_file(file_path='app/static/media/users/aa/bb/cover.png')
        user.avatar_id = avatar.id
        user.cover_id = cover.id
        db.session.commit()
        monkeypatch.setitem(current_app.config, 'CLOUDFLARE_ZONE_ID', 'zone')
        monkeypatch.setitem(current_app.config, 'CLOUDFLARE_API_TOKEN', 'token')
        flushed = []
        monkeypatch.setattr('app.shared.tasks.maintenance.flush_cdn_cache',
                            lambda urls: flushed.append(urls))
        server = current_app.config['SERVER_URL']

        archive_user(user.id, db.session)

        assert len(flushed) == 1
        assert sorted(flushed[0]) == sorted([
            f'{server}/static/media/users/aa/bb/avatar.png',
            f'{server}/static/media/users/aa/bb/cover.png'])

    def test_a_user_with_only_a_cover_is_handled(self, db_session):
        """`:960` false, `:965` true."""
        instance, user, _, _ = _seed()
        cover = make_file(file_path='/static/cover.png')
        user.cover_id = cover.id
        db.session.commit()

        archive_user(user.id, db.session)

        db.session.expire_all()
        assert db.session.get(User, user.id).cover_id is None


class TestArchiveOldUsers:
    """`archive_old_users:934` -- strip images from idle remote users.

    `:936` gates the whole body on `ARCHIVE_POSTS` (config.py:184, default 0),
    and `:939`'s cutoff is that value times 28 days. `:940-946` selects remote
    users idle past the cutoff, and `:948` hands each to `archive_user`.
    """

    def _idle_remote_user(self, name, *, avatar=True, cover=True, age_days=None):
        remote_instance = make_instance(f'{name}.example')
        user = make_user(remote_instance, name)
        if avatar:
            user.avatar_id = make_file(file_path=f'/static/{name}-a.png').id
        if cover:
            user.cover_id = make_file(file_path=f'/static/{name}-c.png').id
        user.last_seen = utcnow() - timedelta(days=age_days if age_days else 6 * 28 + 1)
        db.session.commit()
        return user

    def test_an_idle_remote_user_with_both_images_is_archived(self, db_session, app):
        make_instance('local.example')
        user = self._idle_remote_user('visitor')
        original = app.config['ARCHIVE_POSTS']
        app.config['ARCHIVE_POSTS'] = 6

        try:
            archive_old_users()
        finally:
            app.config['ARCHIVE_POSTS'] = original

        db.session.expire_all()
        refreshed = db.session.get(User, user.id)
        assert (refreshed.avatar_id, refreshed.cover_id) == (None, None)

    def test_two_idle_remote_users_are_both_archived(self, db_session, app):
        """The multi-row path, D354.

        `:947`'s `user_ids = session.execute(text(sql), {'cutoff':
        cutoff}).scalars()` is not materialized, and `:948`'s loop calls
        `archive_user`, which commits once per user at `:971`. Every other
        test in this class seeds exactly one archivable user, so no test
        actually runs the loop for more than one iteration. D354 argues this
        is safe because psycopg2's default client-side cursor has already
        transferred the whole result set before the loop's first commit, but
        that argument had never been executed by a test. This seeds two idle
        remote users and asserts both are archived, running the path D354
        only argues about.
        """
        make_instance('local.example')
        first = self._idle_remote_user('firstuser')
        second = self._idle_remote_user('seconduser')
        original = app.config['ARCHIVE_POSTS']
        app.config['ARCHIVE_POSTS'] = 6

        try:
            archive_old_users()
        finally:
            app.config['ARCHIVE_POSTS'] = original

        db.session.expire_all()
        refreshed_first = db.session.get(User, first.id)
        refreshed_second = db.session.get(User, second.id)
        assert (refreshed_first.avatar_id, refreshed_first.cover_id) == (None, None)
        assert (refreshed_second.avatar_id, refreshed_second.cover_id) == (None, None)

    def test_a_recently_seen_user_is_skipped(self, db_session, app):
        """The BOUNDARY -- `:944`'s `u.last_seen < :cutoff`."""
        make_instance('local.example')
        user = self._idle_remote_user('recent', age_days=6 * 28 - 1)
        original = app.config['ARCHIVE_POSTS']
        app.config['ARCHIVE_POSTS'] = 6

        try:
            archive_old_users()
        finally:
            app.config['ARCHIVE_POSTS'] = original

        db.session.expire_all()
        assert db.session.get(User, user.id).avatar_id is not None

    def test_archiving_off_by_default_makes_the_task_a_no_op(self, db_session):
        """`:936`'s false arm. ARCHIVE_POSTS defaults to 0 (config.py:184), so
        this test sets no config at all -- the default IS the false arm.
        """
        make_instance('local.example')
        user = self._idle_remote_user('untouched')

        archive_old_users()

        db.session.expire_all()
        assert db.session.get(User, user.id).avatar_id is not None

    def test_a_local_user_is_skipped(self, db_session, app):
        """`:943`'s `u.ap_id IS NOT NULL`. A local user has no ap_id."""
        instance, user, _, _ = _seed()
        user.avatar_id = make_file(file_path='/static/local-a.png').id
        user.cover_id = make_file(file_path='/static/local-c.png').id
        user.last_seen = utcnow() - timedelta(days=6 * 28 + 1)
        db.session.commit()
        original = app.config['ARCHIVE_POSTS']
        app.config['ARCHIVE_POSTS'] = 6

        try:
            archive_old_users()
        finally:
            app.config['ARCHIVE_POSTS'] = original

        db.session.expire_all()
        assert db.session.get(User, user.id).avatar_id is not None

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch, app):
        monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)
        original = app.config['ARCHIVE_POSTS']
        app.config['ARCHIVE_POSTS'] = 6

        try:
            with pytest.raises(RuntimeError, match='the task itself failed'):
                archive_old_users()
        finally:
            app.config['ARCHIVE_POSTS'] = original


class TestArchiveOldUsersReachesOneImageUsers:
    """PC1: `:943` used to require BOTH images; `archive_user` requires either.

    Before this round's fix, `:943` filtered on
    `u.avatar_id IS NOT NULL AND u.cover_id IS NOT NULL`, while
    `archive_user:960` and `:965` guard the two images independently. A
    remote user with an avatar and no cover was never selected, however long
    they had been idle, though the helper that processes them handles that
    case. `:943` now filters on `(u.avatar_id IS NOT NULL OR u.cover_id IS
    NOT NULL)`, and these two tests cover that a one-image user is reached
    from either side of that `OR`.
    """

    def test_an_idle_remote_user_with_only_an_avatar_is_archived(self, db_session, app):
        make_instance('local.example')
        remote_instance = make_instance('remote.example')
        user = make_user(remote_instance, 'oneimage')
        avatar = make_file(file_path='/static/one-a.png')
        user.avatar_id = avatar.id
        user.last_seen = utcnow() - timedelta(days=6 * 28 + 1)
        db.session.commit()
        original = app.config['ARCHIVE_POSTS']
        app.config['ARCHIVE_POSTS'] = 6

        try:
            archive_old_users()
        finally:
            app.config['ARCHIVE_POSTS'] = original

        db.session.expire_all()
        assert db.session.get(User, user.id).avatar_id is None

    def test_an_idle_remote_user_with_only_a_cover_is_archived(self, db_session, app):
        """Pins the other half of `:943`'s `OR`.

        `TestArchiveOldUsers._idle_remote_user` takes an `avatar=False`
        keyword written for exactly this case, but it is an instance method
        on `TestArchiveOldUsers`, not on this class, and this class does not
        inherit from it (inheriting would also inherit that class's own
        tests, double-counting them here) -- so it is not reachable from
        `self` in this class. Rather than duplicate the helper as its own
        method, this inlines the same seeding the avatar-only test above
        does, swapped to cover-only.

        Every other test that drives `archive_old_users`' selection query
        gives the seeded user an avatar, so a mutant dropping the cover
        disjunct -- narrowing `:943` back to `u.avatar_id IS NOT NULL) AND
        ...` -- survived all 56 tests before this one existed. Without this
        test, a remote user with only a cover would never be selected for
        archiving, however long they had been idle.
        """
        make_instance('local.example')
        remote_instance = make_instance('remote.example')
        user = make_user(remote_instance, 'coveronly')
        cover = make_file(file_path='/static/one-c.png')
        user.cover_id = cover.id
        user.last_seen = utcnow() - timedelta(days=6 * 28 + 1)
        db.session.commit()
        original = app.config['ARCHIVE_POSTS']
        app.config['ARCHIVE_POSTS'] = 6

        try:
            archive_old_users()
        finally:
            app.config['ARCHIVE_POSTS'] = original

        db.session.expire_all()
        assert db.session.get(User, user.id).cover_id is None


class TestArchiveOldPosts:
    """`archive_old_posts:886` -- archive old posts out of the main tables.

    `:888` gates the body on ARCHIVE_POSTS (config.py:184, default 0).
    `:892-908`'s query excludes stickied posts, private and unarchivable
    communities, and each community's hundred most recent posts. `:911` decides
    whether an S3 client is built, `:921` hands each id to `archive_post`, and
    `:924` closes the client.

    `archive_post` IS REPLACED BY A RECORDER. The real one opens its own task
    session (app/utils.py:4946) and moves files; this round tests which ids
    reach it and whether it got a client, not what it does with either.
    """

    def _past_the_recency_window(self, community, user):
        """Fill the community's hundred-most-recent window, then add one old post.

        `:901-907` excludes each community's hundred most recent posts by
        `created_at`. A community with a hundred posts or fewer therefore has
        NOTHING archivable, and every "this post is skipped" assertion below
        would pass whatever the task did. These tests seed a hundred recent
        posts to fill that window and one post old enough to fall outside it
        AND past `:897`'s cutoff.

        The hundred fillers are staggered a day apart so the ordering has no
        ties, and they are added in one `add_all` rather than a hundred
        committing factory calls.

        Returns the one post that is genuinely archivable.
        """
        now = utcnow()
        fillers = [
            Post(community_id=community.id, user_id=user.id,
                 instance_id=user.instance_id, title='filler',
                 ap_id=f'https://peer.example/fill/{i}',
                 created_at=now - timedelta(days=i),
                 posted_at=now - timedelta(days=i),
                 last_active=now - timedelta(days=i))
            for i in range(100)
        ]
        old = Post(community_id=community.id, user_id=user.id,
                   instance_id=user.instance_id, title='old',
                   ap_id='https://peer.example/old',
                   created_at=now - timedelta(days=6 * 28 + 1),
                   posted_at=now - timedelta(days=6 * 28 + 1),
                   last_active=now - timedelta(days=6 * 28 + 1))
        db.session.add_all(fillers + [old])
        db.session.commit()
        return old

    def test_a_post_beyond_the_recency_window_is_handed_over(self, db_session, monkeypatch, app):
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.archive_post', recorder)
        instance, user, community, post = _seed()
        old = self._past_the_recency_window(community, user)
        original = app.config['ARCHIVE_POSTS']
        app.config['ARCHIVE_POSTS'] = 6

        try:
            archive_old_posts()
        finally:
            app.config['ARCHIVE_POSTS'] = original

        assert [c[0] for c in recorder.calls] == [old.id]

    def test_a_community_inside_the_recency_window_archives_nothing(self, db_session, monkeypatch, app):
        """`:901-907`'s exclusion, and the test that stops the three filter
        tests below from passing vacuously.

        The same old post, in a community with only a handful of others, is not
        archived -- because it is still among that community's hundred most
        recent. Without this test a reader could not tell whether the filter
        tests below prove anything.
        """
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.archive_post', recorder)
        instance, user, community, post = _seed()
        old = make_post(community, user, 'https://peer.example/old')
        old.created_at = utcnow() - timedelta(days=6 * 28 + 1)
        db.session.commit()
        original = app.config['ARCHIVE_POSTS']
        app.config['ARCHIVE_POSTS'] = 6

        try:
            archive_old_posts()
        finally:
            app.config['ARCHIVE_POSTS'] = original

        assert recorder.calls == []

    def test_archiving_off_by_default_makes_the_task_a_no_op(self, db_session, monkeypatch):
        """`:888`'s false arm, which is the default configuration."""
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.archive_post', recorder)
        instance, user, community, post = _seed()
        self._past_the_recency_window(community, user)

        archive_old_posts()

        assert recorder.calls == []

    def test_a_sticky_post_is_skipped(self, db_session, monkeypatch, app):
        """`:898`'s `p.sticky = false`."""
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.archive_post', recorder)
        instance, user, community, post = _seed()
        old = self._past_the_recency_window(community, user)
        old.sticky = True
        db.session.commit()
        original = app.config['ARCHIVE_POSTS']
        app.config['ARCHIVE_POSTS'] = 6

        try:
            archive_old_posts()
        finally:
            app.config['ARCHIVE_POSTS'] = original

        assert recorder.calls == []

    def test_a_post_in_an_unarchivable_community_is_skipped(self, db_session, monkeypatch, app):
        """`:899`'s `c.can_be_archived = true` (app/models.py:588)."""
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.archive_post', recorder)
        instance, user, community, post = _seed()
        self._past_the_recency_window(community, user)
        community.can_be_archived = False
        db.session.commit()
        original = app.config['ARCHIVE_POSTS']
        app.config['ARCHIVE_POSTS'] = 6

        try:
            archive_old_posts()
        finally:
            app.config['ARCHIVE_POSTS'] = original

        assert recorder.calls == []

    def test_a_post_in_a_private_community_is_skipped(self, db_session, monkeypatch, app):
        """`:900`'s `c.private = false` (app/models.py:611)."""
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.archive_post', recorder)
        instance, user, community, post = _seed()
        self._past_the_recency_window(community, user)
        community.private = True
        db.session.commit()
        original = app.config['ARCHIVE_POSTS']
        app.config['ARCHIVE_POSTS'] = 6

        try:
            archive_old_posts()
        finally:
            app.config['ARCHIVE_POSTS'] = original

        assert recorder.calls == []

    def test_no_s3_client_is_built_when_object_storage_is_off(self, db_session, monkeypatch, app):
        """`:911`'s FALSE arm, which is the default configuration
        (config.py:103-107 default all three S3 settings to '').

        `archive_post` receives None, and `:923`'s `if s3:` is false so `:924`
        never runs.
        """
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.archive_post', recorder)
        instance, user, community, post = _seed()
        self._past_the_recency_window(community, user)
        original = app.config['ARCHIVE_POSTS']
        app.config['ARCHIVE_POSTS'] = 6

        try:
            archive_old_posts()
        finally:
            app.config['ARCHIVE_POSTS'] = original

        assert recorder.calls
        assert all(c[1] is None for c in recorder.calls)

    def test_an_s3_client_is_built_and_passed_when_configured(self, db_session, monkeypatch, app):
        """`:911`'s TRUE arm, and `:923`'s.

        Constructing a boto3 client makes no network call, so setting the three
        config values is enough and no endpoint is contacted. The oracle is
        that `archive_post` received something other than None.
        """
        recorder = _Recorder()
        monkeypatch.setattr('app.shared.tasks.maintenance.archive_post', recorder)
        instance, user, community, post = _seed()
        self._past_the_recency_window(community, user)
        originals = {k: app.config[k] for k in
                     ('ARCHIVE_POSTS', 'S3_ACCESS_KEY', 'S3_ACCESS_SECRET',
                      'S3_ENDPOINT', 'S3_REGION')}
        app.config['ARCHIVE_POSTS'] = 6
        app.config['S3_ACCESS_KEY'] = 'key'
        app.config['S3_ACCESS_SECRET'] = 'secret'
        app.config['S3_ENDPOINT'] = 'https://s3.example'
        app.config['S3_REGION'] = 'us-east-1'

        try:
            archive_old_posts()
        finally:
            for k, v in originals.items():
                app.config[k] = v

        assert recorder.calls
        assert all(c[1] is not None for c in recorder.calls)

    def test_the_s3_client_is_closed_when_archiving_fails(self, db_session, monkeypatch, app):
        """D363, fixed: `s3.close()` sat at the end of the `try`, so a raise
        from `archive_post` leaked the client's connection pool. It is now
        closed in the `finally`."""
        closed = []

        class _Client:
            def close(self):
                closed.append(True)

        class _Session:
            def client(self, **kwargs):
                return _Client()

        monkeypatch.setattr('app.shared.tasks.maintenance.boto3.session.Session', _Session)
        monkeypatch.setattr('app.shared.tasks.maintenance.store_files_in_s3', lambda: True)
        monkeypatch.setattr('app.shared.tasks.maintenance.archive_post', _boom)
        instance, user, community, post = _seed()
        self._past_the_recency_window(community, user)
        monkeypatch.setitem(app.config, 'ARCHIVE_POSTS', 6)

        with pytest.raises(RuntimeError):
            archive_old_posts()

        assert closed == [True]

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch, app):
        monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)
        original = app.config['ARCHIVE_POSTS']
        app.config['ARCHIVE_POSTS'] = 6

        try:
            with pytest.raises(RuntimeError, match='the task itself failed'):
                archive_old_posts()
        finally:
            app.config['ARCHIVE_POSTS'] = original
