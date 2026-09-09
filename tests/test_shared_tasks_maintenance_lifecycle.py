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
