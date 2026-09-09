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


class TestCleanupOldNotifications:
    """`cleanup_old_notifications:25` -- TWO tables, TWO cutoffs.

    `:29-30` deletes Notification rows older than 90 days; `:33-34` deletes
    RevokedToken rows older than 365 days. The docstring at `:26` names only
    the first, which is registered as a finding rather than fixed.
    """

    def test_a_notification_older_than_ninety_days_is_removed(self, db_session):
        _, user, _, post = _seed()
        old = make_notification(user, post)
        old.created_at = utcnow() - timedelta(days=91)
        db.session.commit()

        cleanup_old_notifications()

        assert db.session.query(Notification).count() == 0

    def test_a_notification_inside_ninety_days_survives(self, db_session):
        _, user, _, post = _seed()
        fresh = make_notification(user, post)
        fresh.created_at = utcnow() - timedelta(days=89)
        db.session.commit()

        cleanup_old_notifications()

        assert db.session.query(Notification).count() == 1

    def test_the_two_cutoffs_are_different(self, db_session):
        """A RevokedToken 100 days old outlives a Notification 100 days old.

        This is the assertion that fails if `:33`'s 365 is changed to 90, or
        if the RevokedToken delete is dropped onto the notification cutoff.
        Neither of the two single-table tests above can see that.
        """
        _, user, _, post = _seed()
        notification = make_notification(user, post)
        notification.created_at = utcnow() - timedelta(days=100)
        token = RevokedToken(jti='a-hundred-days-old',
                             revoked_at=utcnow() - timedelta(days=100))
        db.session.add(token)
        db.session.commit()

        cleanup_old_notifications()

        assert db.session.query(Notification).count() == 0
        assert db.session.query(RevokedToken).count() == 1

    def test_a_revoked_token_older_than_a_year_is_removed(self, db_session):
        token = RevokedToken(jti='old', revoked_at=utcnow() - timedelta(days=366))
        db.session.add(token)
        db.session.commit()

        cleanup_old_notifications()

        assert db.session.query(RevokedToken).count() == 0

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch):
        monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            cleanup_old_notifications()


class TestCleanupOldActivityPubLogs:
    """`cleanup_old_activitypub_logs:872` -- logs older than three days."""

    def test_a_log_older_than_three_days_is_removed(self, db_session):
        log = make_activitypub_log('https://peer.example/activities/create/1')
        log.created_at = utcnow() - timedelta(days=4)
        db.session.commit()

        cleanup_old_activitypub_logs()

        assert db.session.query(ActivityPubLog).count() == 0

    def test_a_log_inside_three_days_survives(self, db_session):
        log = make_activitypub_log('https://peer.example/activities/create/2')
        log.created_at = utcnow() - timedelta(days=2)
        db.session.commit()

        cleanup_old_activitypub_logs()

        assert db.session.query(ActivityPubLog).count() == 1

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch):
        monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            cleanup_old_activitypub_logs()


class TestCleanupOldReadPosts:
    """`cleanup_old_read_posts:45` -- a raw DELETE against an association table.

    `:50` reads the cutoff from `get_setting('read_posts_cutoff', 180)`, which
    goes through `db.session`; `:49`'s `patch_db_session(session)` is what
    makes that read land on the task's own session. The harness leaves
    `patch_db_session` live because `tests/conftest.py:112` pushes only an app
    context (facts 156 and 157).
    """

    def test_a_read_post_older_than_the_cutoff_is_removed(self, db_session):
        _, user, _, post = _seed()
        db.session.execute(db.text(
            'INSERT INTO read_posts (user_id, read_post_id, interacted_at) '
            'VALUES (:u, :p, :t)'),
            {'u': user.id, 'p': post.id, 't': utcnow() - timedelta(days=181)})
        db.session.commit()

        cleanup_old_read_posts()

        assert db.session.execute(
            db.text('SELECT COUNT(*) FROM read_posts')).scalar() == 0

    def test_a_read_post_inside_the_cutoff_survives(self, db_session):
        _, user, _, post = _seed()
        db.session.execute(db.text(
            'INSERT INTO read_posts (user_id, read_post_id, interacted_at) '
            'VALUES (:u, :p, :t)'),
            {'u': user.id, 'p': post.id, 't': utcnow() - timedelta(days=179)})
        db.session.commit()

        cleanup_old_read_posts()

        assert db.session.execute(
            db.text('SELECT COUNT(*) FROM read_posts')).scalar() == 1

    def test_the_cutoff_comes_from_the_setting_not_the_default(self, db_session):
        """A row 100 days old survives at the default and dies at a 90-day setting.

        This is the test that proves `:50`'s `get_setting` call is load-bearing
        rather than decorative -- and therefore that `:49`'s `patch_db_session`
        is doing something, since `get_setting` reads `db.session`.
        """
        from app.utils import set_setting
        _, user, _, post = _seed()
        db.session.execute(db.text(
            'INSERT INTO read_posts (user_id, read_post_id, interacted_at) '
            'VALUES (:u, :p, :t)'),
            {'u': user.id, 'p': post.id, 't': utcnow() - timedelta(days=100)})
        set_setting('read_posts_cutoff', 90)
        db.session.commit()

        cleanup_old_read_posts()

        assert db.session.execute(
            db.text('SELECT COUNT(*) FROM read_posts')).scalar() == 0

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch):
        monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            cleanup_old_read_posts()


class TestUnbanExpiredUsers:
    """`unban_expired_users:392` -- one UPDATE with three conditions."""

    def test_a_user_whose_ban_has_expired_is_unbanned(self, db_session):
        instance, user, _, _ = _seed()
        user.banned = True
        user.banned_until = utcnow() - timedelta(days=1)
        db.session.commit()

        unban_expired_users()

        db.session.expire_all()
        assert db.session.get(User, user.id).banned is False

    def test_a_user_whose_ban_has_not_expired_stays_banned(self, db_session):
        instance, user, _, _ = _seed()
        user.banned = True
        user.banned_until = utcnow() + timedelta(days=1)
        db.session.commit()

        unban_expired_users()

        db.session.expire_all()
        assert db.session.get(User, user.id).banned is True

    def test_a_permanent_ban_has_no_banned_until_and_survives(self, db_session):
        """`:397`'s third condition, `banned_until is not null`.

        Without it the NULL comparison would already exclude the row, so this
        test cannot fail by deleting that conjunct alone -- it pins the
        BEHAVIOUR (a permanent ban is not lifted) rather than the conjunct, and
        the mutation transcript records that distinction.
        """
        instance, user, _, _ = _seed()
        user.banned = True
        user.banned_until = None
        db.session.commit()

        unban_expired_users()

        db.session.expire_all()
        assert db.session.get(User, user.id).banned is True

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch):
        monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            unban_expired_users()


class TestUpdateHashtagCounts:
    """`update_hashtag_counts:194` -- one correlated UPDATE, no clock at all.

    This is the one task in Group A that never calls `utcnow()`, so its
    error-path test patches `app.shared.tasks.maintenance.text` instead.
    """

    def test_a_tag_count_is_recomputed_from_post_tag(self, db_session):
        _, user, community, post = _seed()
        second = make_post(community, user, 'https://peer.example/p/2')
        tag = Tag(name='solarstorm', display_as='SolarStorm', post_count=0)
        db.session.add(tag)
        db.session.commit()
        db.session.execute(db.text(
            'INSERT INTO post_tag (post_id, tag_id) VALUES (:p, :t)'),
            {'p': post.id, 't': tag.id})
        db.session.execute(db.text(
            'INSERT INTO post_tag (post_id, tag_id) VALUES (:p, :t)'),
            {'p': second.id, 't': tag.id})
        db.session.commit()

        update_hashtag_counts()

        db.session.expire_all()
        assert db.session.get(Tag, tag.id).post_count == 2

    def test_a_tag_with_no_posts_is_set_to_zero(self, db_session):
        """The UPDATE has no WHERE, so a stale count on an unused tag is reset.

        This is the arm that distinguishes "recompute every tag" from
        "recompute the tags that have posts", and nothing else in the file
        covers it.
        """
        tag = Tag(name='stale', display_as='Stale', post_count=7)
        db.session.add(tag)
        db.session.commit()

        update_hashtag_counts()

        db.session.expire_all()
        assert db.session.get(Tag, tag.id).post_count == 0

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch):
        monkeypatch.setattr('app.shared.tasks.maintenance.text', _boom)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            update_hashtag_counts()
