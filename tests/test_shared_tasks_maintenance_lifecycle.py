"""Group B of `app/shared/tasks/maintenance.py` -- the content-lifecycle tasks.

Sub-project 29 closed Group A, the ten tasks that need no transport, in
`tests/test_shared_tasks_maintenance_cleanup.py`. Group B is the eight that
move or destroy content:

  `process_expired_bans:76`            `remove_old_community_content:134`
  `remove_old_bot_content:160`         `delete_old_soft_deleted_content:215`
  `archive_old_posts:885`              `archive_old_users:933`
  `archive_user:957`                   `pwn_bots:1161`

TWENTY-THREE BRANCH POINTS AGAINST GROUP A'S SEVEN. Group A was straight-line
SQL in a repeated try/except skeleton. Group B branches on config values, on
whether a row still exists, on whether an image is shared between posts, and on
whether object storage is configured.

TWO TASKS DELETE THROUGH `app/shared/post.py`. `remove_old_community_content:150`
and `remove_old_bot_content:184` both call `delete_post`
(`app/shared/post.py:755`), which is a floored module at 40% with its own
federation behaviour. THIS FILE DOES NOT TEST `delete_post`. It tests which post
ids these two tasks hand over, by replacing `delete_post` in this module's
namespace with a recorder. Asserting on federation here would be testing another
module through a keyhole.

ONE TASK BUILDS A boto3 CLIENT. `archive_old_posts:911-918` constructs one when
`:910`'s `store_files_in_s3()` is true and passes it to `archive_post`. The
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
    """`pwn_bots:1161` -- everyone who ignored a bot challenge for 24h is a bot.

    `:1166` iterates challenges older than the cutoff whose `is_a_bot` is still
    NULL, `:1167` marks the user, and `:1170` marks the challenge. One branch
    point, the loop.

    `:1166` READS THROUGH `BotChallenge.query`, WHICH IS `db.session`, while
    `:1167` and `:1170` write through the task's own session, and the body is
    not wrapped in `patch_db_session`. That split is this round's PC2 and is
    NOT fixed by these tests -- they pass either way, which is itself the point
    Task 8 has to establish.
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
        """`:1166`'s `is_a_bot == None` conjunct.

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
        """`:1174-1176`'s handler, reached through `:1167`.

        NOT through `utcnow`. `:1164` computes the cutoff one line ABOVE
        `:1165`'s `try:`, so patching the clock raises before the handler
        exists and would pass even if the whole `except` clause were deleted.
        `pwn_bots` is the only task in this group that orders those two
        statements that way.

        A challenge must be seeded, because `:1167`'s `text(...)` is inside the
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
        (app/models.py:1251-1252) is false once `ap_id` is set and does not
        start with SERVER_URL. The ban is still deleted -- only the
        notification is skipped.
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
