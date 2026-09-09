"""Group A of `app/shared/tasks/maintenance.py` -- the ten pure-database tasks.

`maintenance.py` is 1181 lines and 636 statements, three times the largest
module this campaign has closed in one round, so it is split by TESTING
SURFACE rather than by subject. Group A is the ten tasks that need no
transport at all:

  `cleanup_old_notifications:25`   `cleanup_old_read_posts:45`
  `cleanup_send_queue:61`          `update_hashtag_counts:194`
  `update_community_stats:283`     `cleanup_old_voting_data:327`
  `unban_expired_users:392`        `recalculate_user_attitudes:712`
  `calculate_community_activity_stats:750`
  `cleanup_old_activitypub_logs:872`

NONE OF THE FEDERATION ORACLES APPLY HERE. These tasks are invoked from cron
entry points in `app/cli.py:808-947`, never from a route and never through
`task_selector`. There is no `flash()`, no `SRC_`-forked return value, no
delivered-inbox set, no `post_request`, and no `ActivityPubLog` row written by
any of them. Fact 148's warning about respx swallowing unmatched requests is
irrelevant in this file. The oracle is a row count or a column value.

THE TASK RUNS ON ITS OWN CONNECTION. `get_task_session()` returns
`Session(bind=db.engine)` (app/utils.py:3673-3675), so rows a test seeds must
be COMMITTED before the task runs or the task cannot see them, and an ORM
attribute read after the task runs is stale unless the test calls
`db.session.expire_all()` first (fact 153). Reading through a fresh query is
immune and is what most tests here do.

THE ERROR PATHS ARE THE SAME TEST TEN TIMES. Every task in this group wraps
its body in `try / except Exception: session.rollback(); raise / finally:
session.close()`, which is about thirty of the group's 151 statements. Nine of
the ten call `utcnow()` inside the `try`, and `utcnow` is bound into this
module's namespace at `app/shared/tasks/maintenance.py:16`, so monkeypatching
`app.shared.tasks.maintenance.utcnow` raises inside the `try` without touching
`tests/factories.py`, which reaches `utcnow` through `app.models`.
`update_hashtag_counts` calls no clock and is the one exception -- its
error-path test patches `app.shared.tasks.maintenance.text`.
"""

from datetime import timedelta

import pytest

from app import db
from app.models import (
    ActivityPubLog, Community, Notification, RevokedToken, SendQueue, Tag,
    User, utcnow,
)
from app.shared.tasks.maintenance import (
    calculate_community_activity_stats, cleanup_old_activitypub_logs,
    cleanup_old_notifications, cleanup_old_read_posts, cleanup_old_voting_data,
    cleanup_send_queue, recalculate_user_attitudes, unban_expired_users,
    update_community_stats, update_hashtag_counts,
)
from tests.factories import (
    make_activitypub_log, make_community, make_community_member, make_instance,
    make_notification, make_post, make_post_reply, make_post_reply_vote,
    make_post_vote, make_user,
)


def _boom(*args, **kwargs):
    """Raise from inside a task's `try`, to reach its `except` arm."""
    raise RuntimeError('the task itself failed')


def _seed():
    """instance, a local user, a community, and one post -- all committed.

    Every factory here commits, so the task's own connection can see these
    rows. `make_community` leaves `ap_id` None, which makes `is_local()` true
    (app/models.py:796) -- a test that needs the false arm sets `ap_id` and
    `ap_profile_id` to a remote host and commits.
    """
    instance = make_instance('peer.example')
    user = make_user(instance, 'reader', local=True)
    community = make_community()
    post = make_post(community, user, 'https://peer.example/p/1')
    return instance, user, community, post


class TestCleanupSendQueue:
    """`cleanup_send_queue:61` -- SendQueue rows older than seven days."""

    def test_a_send_queue_row_older_than_seven_days_is_removed(self, db_session):
        old = SendQueue(destination_domain='peer.example',
                        created=utcnow() - timedelta(days=8))
        db.session.add(old)
        db.session.commit()

        cleanup_send_queue()

        assert db.session.query(SendQueue).count() == 0

    def test_a_send_queue_row_inside_seven_days_survives(self, db_session):
        """The BOUNDARY, which is the half a deletion test cannot prove.

        A test that only checks the old row is gone passes just as well against
        a task with no cutoff at all. This is the test that fails if the
        comparison at `:66` is widened.
        """
        fresh = SendQueue(destination_domain='peer.example',
                          created=utcnow() - timedelta(days=6))
        db.session.add(fresh)
        db.session.commit()

        cleanup_send_queue()

        assert db.session.query(SendQueue).count() == 1

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch):
        monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            cleanup_send_queue()
