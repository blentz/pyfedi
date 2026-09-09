"""Group A of `app/shared/tasks/maintenance.py` -- the ten pure-database tasks.

`maintenance.py` is 1181 lines and 636 statements, three times the largest
module this campaign has closed in one round, so it is split by TESTING
SURFACE rather than by subject. Group A is the ten tasks that need no
transport at all:

  `cleanup_old_notifications:25`   `cleanup_old_read_posts:45`
  `cleanup_send_queue:61`          `update_hashtag_counts:194`
  `update_community_stats:283`     `cleanup_old_voting_data:327`
  `unban_expired_users:392`        `recalculate_user_attitudes:712`
  `calculate_community_activity_stats:748`
  `cleanup_old_activitypub_logs:870`

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
    """`cleanup_old_activitypub_logs:870` -- logs older than three days."""

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
    goes through `db.session`; `:49` wraps the body in
    `patch_db_session(session)`. `tests/conftest.py:112` pushes only an app
    context, so `has_request_context()` is false and the patch does apply
    under this harness (facts 156 and 157) -- but see
    `test_the_cutoff_comes_from_the_setting_not_the_default` for why no test
    here can show that the patch matters.
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

        This proves `:50`'s `get_setting` return value is load-bearing -- the
        cutoff comes from the setting, not a hardcoded 180 -- because the row
        would have survived at the default and only dies once the setting is
        lowered to 90.

        It does NOT prove `:49`'s `patch_db_session` is necessary, and under
        this harness no test can. `get_task_session()` opens a second
        connection to the same database (`app/utils.py:3673-3675`); the
        setting row this test writes is committed on `db.session` before the
        task runs, so it is visible from either connection under READ
        COMMITTED whether or not `get_setting` reads through the patched
        session. Deleting `:49`'s `with patch_db_session(session):` would not
        make this test fail. `patch_db_session` exists for a live Celery
        worker, where `db.session` is not the session the task is writing
        through -- a distinction this in-process harness cannot reproduce,
        since it is the same app-context `db.session` that makes the
        unpatched path work too.
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


class TestCleanupOldVotingData:
    """`cleanup_old_voting_data:327` -- four DELETEs behind two config guards.

    `:334`'s `if local_months != -1` and `:359`'s `if remote_months != -1` read
    `KEEP_LOCAL_VOTE_DATA_TIME` and `KEEP_REMOTE_VOTE_DATA_TIME`
    (config.py:177-178, both defaulting to 6). The cutoff is 28 days per month,
    so at the default a vote older than 168 days goes and one younger stays.

    LOCAL AND REMOTE ARE `instance_id = 1` AND `instance_id != 1`. The
    `db_session` fixture resets sequences, so the first `make_instance` in a
    test receives id 1; a test that wants both kinds of voter builds two
    instances and takes the first as local.
    """

    def _two_voters_with_votes(self, age_days):
        """A local voter and a remote voter, each with a post vote and a reply
        vote of the given age. Returns (local_user, remote_user)."""
        local_instance = make_instance('local.example')
        remote_instance = make_instance('remote.example')
        assert local_instance.id == 1
        local_user = make_user(local_instance, 'homebody', local=True)
        remote_user = make_user(remote_instance, 'visitor')
        community = make_community()
        post = make_post(community, local_user, 'https://local.example/p/1')
        reply = make_post_reply(post, local_user)
        stamp = utcnow() - timedelta(days=age_days)
        for voter in (local_user, remote_user):
            post_vote = make_post_vote(voter, post, 1.0)
            reply_vote = make_post_reply_vote(voter, reply, 1.0)
            post_vote.created_at = stamp
            reply_vote.created_at = stamp
        db.session.commit()
        return local_user, remote_user

    def _vote_counts(self):
        return (
            db.session.execute(db.text('SELECT COUNT(*) FROM post_vote')).scalar(),
            db.session.execute(db.text('SELECT COUNT(*) FROM post_reply_vote')).scalar(),
        )

    def test_old_votes_from_both_kinds_of_voter_are_removed(self, db_session):
        self._two_voters_with_votes(age_days=28 * 6 + 1)

        cleanup_old_voting_data()

        assert self._vote_counts() == (0, 0)

    def test_recent_votes_survive(self, db_session):
        self._two_voters_with_votes(age_days=28 * 6 - 1)

        cleanup_old_voting_data()

        assert self._vote_counts() == (2, 2)

    def test_minus_one_for_local_keeps_local_votes_and_drops_remote(self, db_session, app):
        """`:334`'s false arm. -1 means "keep local vote data forever".

        The remote deletes still run, so this test also proves the two guards
        are independent rather than one guard read twice. The survivor is
        named by `user_id` rather than just counted, so the test fails if the
        task drops the local vote and keeps the remote one instead.
        """
        local_user, remote_user = self._two_voters_with_votes(age_days=28 * 6 + 1)
        original = app.config['KEEP_LOCAL_VOTE_DATA_TIME']
        app.config['KEEP_LOCAL_VOTE_DATA_TIME'] = -1

        try:
            cleanup_old_voting_data()
        finally:
            app.config['KEEP_LOCAL_VOTE_DATA_TIME'] = original

        surviving_post_voters = set(db.session.execute(db.text(
            'SELECT user_id FROM post_vote')).scalars().all())
        surviving_reply_voters = set(db.session.execute(db.text(
            'SELECT user_id FROM post_reply_vote')).scalars().all())
        assert surviving_post_voters == {local_user.id}
        assert surviving_reply_voters == {local_user.id}

    def test_minus_one_for_remote_keeps_remote_votes_and_drops_local(self, db_session, app):
        """`:359`'s false arm, the mirror of the test above.

        The survivor is named by `user_id` rather than just counted, so the
        test fails if the task drops the remote vote and keeps the local one
        instead.
        """
        local_user, remote_user = self._two_voters_with_votes(age_days=28 * 6 + 1)
        original = app.config['KEEP_REMOTE_VOTE_DATA_TIME']
        app.config['KEEP_REMOTE_VOTE_DATA_TIME'] = -1

        try:
            cleanup_old_voting_data()
        finally:
            app.config['KEEP_REMOTE_VOTE_DATA_TIME'] = original

        surviving_post_voters = set(db.session.execute(db.text(
            'SELECT user_id FROM post_vote')).scalars().all())
        surviving_reply_voters = set(db.session.execute(db.text(
            'SELECT user_id FROM post_reply_vote')).scalars().all())
        assert surviving_post_voters == {remote_user.id}
        assert surviving_reply_voters == {remote_user.id}

    def test_minus_one_for_both_deletes_nothing(self, db_session, app):
        """Both false arms at once -- the task becomes a no-op."""
        self._two_voters_with_votes(age_days=28 * 6 + 1)
        original_local = app.config['KEEP_LOCAL_VOTE_DATA_TIME']
        original_remote = app.config['KEEP_REMOTE_VOTE_DATA_TIME']
        app.config['KEEP_LOCAL_VOTE_DATA_TIME'] = -1
        app.config['KEEP_REMOTE_VOTE_DATA_TIME'] = -1

        try:
            cleanup_old_voting_data()
        finally:
            app.config['KEEP_LOCAL_VOTE_DATA_TIME'] = original_local
            app.config['KEEP_REMOTE_VOTE_DATA_TIME'] = original_remote

        assert self._vote_counts() == (2, 2)

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch):
        monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            cleanup_old_voting_data()


class TestUpdateCommunityStats:
    """`update_community_stats:283` -- recount subscribers, posts and replies.

    `:288-290` selects communities that are not banned and were active in the
    last three days. `:303` writes `subscriptions_count` from a join that
    excludes banned members and bots; `:309` and `:313` write `post_count` and
    `post_reply_count` from raw counts that exclude deleted rows.

    `:305-306` IS THREE CONDITIONS IN ONE ARC PAIR. Branch coverage reads 100%
    with two of them untested (fact 142), so each gets its own test:
    `is_local()`, `total_subscriptions_count is None`, and
    `total_subscriptions_count < subscriptions_count`.

    `:317`'s `session.commit()` used to sit inside the `for` loop opened at
    `:292`, committing once per community rather than once after the loop.
    Every test below only ever seeds one eligible community, so none of them
    can distinguish a commit-per-iteration from a single commit after the
    loop -- `TestUpdateCommunityStatsIsAtomic`, below, is what tests that
    distinction, and `:317` is now dedented to commit once after the loop.
    """

    def test_subscriptions_count_excludes_bots_and_banned_members(self, db_session):
        instance, user, community, _ = _seed()
        bot = make_user(instance, 'botty', local=True)
        bot.bot = True
        banned = make_user(instance, 'outcast', local=True)
        db.session.commit()
        make_community_member(user, community)
        make_community_member(bot, community)
        banned_membership = make_community_member(banned, community)
        banned_membership.is_banned = True
        db.session.commit()

        update_community_stats()

        db.session.expire_all()
        assert db.session.get(Community, community.id).subscriptions_count == 1

    def test_post_and_reply_counts_exclude_deleted_rows(self, db_session):
        instance, user, community, post = _seed()
        deleted_post = make_post(community, user, 'https://peer.example/p/2')
        deleted_post.deleted = True
        reply = make_post_reply(post, user)
        deleted_reply = make_post_reply(post, user)
        deleted_reply.deleted = True
        db.session.commit()

        update_community_stats()

        db.session.expire_all()
        refreshed = db.session.get(Community, community.id)
        assert (refreshed.post_count, refreshed.post_reply_count) == (1, 1)

    def test_a_banned_community_is_skipped(self, db_session):
        _, user, community, _ = _seed()
        community.banned = True
        community.post_count = 99
        db.session.commit()

        update_community_stats()

        db.session.expire_all()
        assert db.session.get(Community, community.id).post_count == 99

    def test_a_community_idle_for_more_than_three_days_is_skipped(self, db_session):
        _, user, community, _ = _seed()
        community.last_active = utcnow() - timedelta(days=4)
        community.post_count = 99
        db.session.commit()

        update_community_stats()

        db.session.expire_all()
        assert db.session.get(Community, community.id).post_count == 99

    def test_a_local_community_with_no_total_gets_one(self, db_session):
        """`:305-306`'s second condition: total_subscriptions_count is None."""
        instance, user, community, _ = _seed()
        make_community_member(user, community)
        community.total_subscriptions_count = None
        db.session.commit()

        update_community_stats()

        db.session.expire_all()
        assert db.session.get(Community, community.id).total_subscriptions_count == 1

    def test_a_local_community_whose_total_lags_is_raised(self, db_session):
        """`:305-306`'s third condition: total < subscriptions."""
        instance, user, community, _ = _seed()
        make_community_member(user, community)
        community.total_subscriptions_count = 0
        db.session.commit()

        update_community_stats()

        db.session.expire_all()
        assert db.session.get(Community, community.id).total_subscriptions_count == 1

    def test_a_local_community_whose_total_already_leads_is_left_alone(self, db_session):
        """The false arm of `:305-306`'s third condition.

        A local community's total counts remote subscribers too, so a total
        ABOVE the local count is the normal state and must not be pulled down.
        """
        instance, user, community, _ = _seed()
        make_community_member(user, community)
        community.total_subscriptions_count = 50
        db.session.commit()

        update_community_stats()

        db.session.expire_all()
        assert db.session.get(Community, community.id).total_subscriptions_count == 50

    def test_a_remote_community_keeps_its_total_untouched(self, db_session):
        """`:305`'s first condition, `is_local()`, taking its false arm.

        `make_community` leaves `ap_id` None, which makes `is_local()` true at
        app/models.py:796's first disjunct. Setting both `ap_id` and
        `ap_profile_id` to a remote host makes it false, and `profile_id()`
        (app/models.py:787) then returns a URI that does not start with
        SERVER_URL.
        """
        instance, user, community, _ = _seed()
        make_community_member(user, community)
        community.ap_id = 'microblogs@peer.example'
        community.ap_profile_id = 'https://peer.example/c/microblogs'
        community.total_subscriptions_count = 0
        db.session.commit()

        update_community_stats()

        db.session.expire_all()
        refreshed = db.session.get(Community, community.id)
        assert refreshed.subscriptions_count == 1
        assert refreshed.total_subscriptions_count == 0

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch):
        monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            update_community_stats()


class TestUpdateCommunityStatsIsAtomic:
    """PC2: `:317`'s commit used to sit inside the loop opened at `:292`.

    Before this task's fix, a failure at community N left communities 1..N-1
    committed, because `:319-321`'s handler rolls back only the current unit
    of work -- it did not protect the task's whole effect, despite reading as
    though it did. `:317` now runs once after the loop, so the two tests below
    fail if the commit is ever moved back inside it.
    """

    def test_a_failure_partway_through_leaves_no_partial_writes(self, db_session, monkeypatch):
        import app.shared.tasks.maintenance as maintenance

        _, user, first, _ = _seed()
        second = make_community(name='second')
        make_community_member(user, first)
        make_community_member(user, second)
        first.subscriptions_count = 0
        second.subscriptions_count = 0
        db.session.commit()

        # Each community costs two text() calls, at `:309` and `:313`. Letting
        # two through and failing on the third puts the failure inside the
        # SECOND community, after the first has been fully processed.
        real_text = maintenance.text
        calls = {'n': 0}

        def _text_that_fails_on_the_second_community(sql):
            calls['n'] += 1
            if calls['n'] > 2:
                raise RuntimeError('the task itself failed')
            return real_text(sql)

        monkeypatch.setattr(maintenance, 'text', _text_that_fails_on_the_second_community)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            update_community_stats()

        db.session.expire_all()
        counts = {c.name: c.subscriptions_count
                  for c in db.session.query(Community).all()}
        assert counts == {'microblogs': 0, 'second': 0}

    def test_the_number_of_community_selects_does_not_scale_with_the_loop(self, db_session):
        """PC2's second consequence: `expire_on_commit` forced a re-SELECT.

        `expire_on_commit` defaults to True (fact 58), so before this task's
        fix each commit inside the loop expired every loaded Community and the
        next iteration's first attribute read reloaded its row. Counting
        statements on `db.engine` catches the task's own connection, because
        `get_task_session()` binds to that same engine (app/utils.py:3673-3675).

        With 4 eligible communities the unmodified code produced exactly 4
        matching statements: the one eligibility query at `:287-290`, plus one
        re-SELECT by primary key at the top of each of iterations 2, 3 and 4,
        on the Community object the previous iteration's commit had just
        expired -- for N communities the mechanism predicts N. After the fix
        it predicts exactly 1: the eligibility query alone, since with no
        in-loop commit nothing is ever expired and autoflush emits UPDATEs,
        not SELECTs, for the pending attribute changes.
        """
        import re

        from sqlalchemy import event

        _, user, first, _ = _seed()
        for name in ('second', 'third', 'fourth'):
            make_community(name=name)
        db.session.commit()

        statements = []

        def _record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(db.engine, 'before_cursor_execute', _record)
        try:
            update_community_stats()
        finally:
            event.remove(db.engine, 'before_cursor_execute', _record)

        # A WORD BOUNDARY, not a substring. `:294-302`'s
        # `select(func.count()).select_from(CommunityMember)` emits
        # `FROM community_member`, which `' FROM community' in s.lower()` would
        # match -- an oracle that counts a statement it did not name.
        selects = [s for s in statements
                   if s.lstrip().upper().startswith('SELECT')
                   and re.search(r'\bfrom\s+community\b', s, re.IGNORECASE)]
        assert len(selects) == 1


class TestRecalculateUserAttitudes:
    """`recalculate_user_attitudes:712` -- recompute attitude and post stats.

    `:720-722` selects users seen in the last day, `:727` batches them 100 at a
    time (`:715`), and `:734-735` call `recalculate_attitude` and
    `recalculate_post_stats` on each. Both model methods read `db.session`
    (app/models.py:1351, :1421), which is what `:718`'s `patch_db_session`
    redirects onto the task's own session.
    """

    def test_a_recently_seen_user_has_post_stats_recomputed(self, db_session):
        instance, user, community, post = _seed()
        make_post(community, user, 'https://peer.example/p/2')
        user.last_seen = utcnow()
        user.post_count = 99
        db.session.commit()

        recalculate_user_attitudes()

        db.session.expire_all()
        assert db.session.get(User, user.id).post_count == 2

    def test_a_user_not_seen_for_a_day_is_skipped(self, db_session):
        """`:727`'s zero-iteration arm: no eligible users, no batches."""
        instance, user, community, post = _seed()
        user.last_seen = utcnow() - timedelta(days=2)
        user.post_count = 99
        db.session.commit()

        recalculate_user_attitudes()

        db.session.expire_all()
        assert db.session.get(User, user.id).post_count == 99

    def test_deleted_posts_do_not_count(self, db_session):
        instance, user, community, post = _seed()
        gone = make_post(community, user, 'https://peer.example/p/3')
        gone.deleted = True
        user.last_seen = utcnow()
        db.session.commit()

        recalculate_user_attitudes()

        db.session.expire_all()
        assert db.session.get(User, user.id).post_count == 1

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch):
        monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            recalculate_user_attitudes()


class TestCalculateCommunityActivityStats:
    """`calculate_community_activity_stats:748` -- four activity sources.

    `:763-769` builds a temporary table, `:772-814` fills it from posts, post
    replies, post votes and post reply votes, `:817-819` indexes it, `:825-837`
    aggregates over four windows, and `:843-856` writes the four columns back.

    EACH INSERT HAS ITS OWN BOT EXCLUSION: `from_bot = False` for posts and
    replies, `u.bot = False` for the two vote sources. A test that seeds only
    posts executes every statement while pinning one quarter of the behaviour,
    so these tests seed all four and then remove them one kind at a time.

    The two window tests below bound only the day threshold: both seeded ages
    (-1h and -2d) fall on the same side of the week, month and half-year
    thresholds, so they would not catch a mutation that merged the week
    threshold into the month. Closing that residual needs a third seeded age
    between one week and six months, which is outside this class's approved
    scope.
    """

    def _community_with_one_activity_of_each_kind(self, when):
        instance, author, community, post = _seed()
        voter = make_user(instance, 'voter', local=True)
        reply_voter = make_user(instance, 'reply_voter', local=True)
        replier = make_user(instance, 'replier', local=True)
        reply = make_post_reply(post, replier)
        post_vote = make_post_vote(voter, post, 1.0)
        reply_vote = make_post_reply_vote(reply_voter, reply, 1.0)
        post.posted_at = when
        reply.posted_at = when
        post_vote.created_at = when
        reply_vote.created_at = when
        community.last_active = utcnow()
        db.session.commit()
        return community, {author.id, replier.id, voter.id, reply_voter.id}

    def test_activity_inside_a_day_counts_in_every_window(self, db_session):
        community, actors = self._community_with_one_activity_of_each_kind(
            utcnow() - timedelta(hours=1))

        calculate_community_activity_stats()

        db.session.expire_all()
        refreshed = db.session.get(Community, community.id)
        assert (refreshed.active_daily, refreshed.active_weekly,
                refreshed.active_monthly, refreshed.active_6monthly) == (
            len(actors), len(actors), len(actors), len(actors))

    def test_activity_older_than_a_day_counts_only_in_the_wider_windows(self, db_session):
        community, actors = self._community_with_one_activity_of_each_kind(
            utcnow() - timedelta(days=2))

        calculate_community_activity_stats()

        db.session.expire_all()
        refreshed = db.session.get(Community, community.id)
        assert refreshed.active_daily == 0
        assert (refreshed.active_weekly, refreshed.active_monthly,
                refreshed.active_6monthly) == (len(actors), len(actors), len(actors))

    def test_a_bot_author_is_excluded(self, db_session):
        """`:777`'s `p.from_bot = False`, on the post INSERT."""
        instance, author, community, post = _seed()
        post.from_bot = True
        post.posted_at = utcnow() - timedelta(hours=1)
        community.last_active = utcnow()
        db.session.commit()

        calculate_community_activity_stats()

        db.session.expire_all()
        assert db.session.get(Community, community.id).active_daily == 0

    def test_a_bot_voter_is_excluded(self, db_session):
        """`:799`'s `u.bot = False`, on the post-vote INSERT.

        The post itself is seeded outside the window so the only candidate
        activity is the vote, which makes the assertion about the vote rather
        than about whatever else happens to be in range.
        """
        instance, author, community, post = _seed()
        bot = make_user(instance, 'botty', local=True)
        bot.bot = True
        vote = make_post_vote(bot, post, 1.0)
        vote.created_at = utcnow() - timedelta(hours=1)
        post.posted_at = utcnow() - timedelta(weeks=40)
        community.last_active = utcnow()
        db.session.commit()

        calculate_community_activity_stats()

        db.session.expire_all()
        assert db.session.get(Community, community.id).active_daily == 0

    def test_a_bot_replier_is_excluded(self, db_session):
        """`:787`'s `pr.from_bot = False`, on the post-replies INSERT.

        The post itself is seeded outside the window so the only candidate
        activity is the reply, which makes the assertion about the reply
        rather than about whatever else happens to be in range.
        """
        instance, author, community, post = _seed()
        bot = make_user(instance, 'botty_replier', local=True)
        reply = make_post_reply(post, bot)
        reply.from_bot = True
        reply.posted_at = utcnow() - timedelta(hours=1)
        post.posted_at = utcnow() - timedelta(weeks=40)
        community.last_active = utcnow()
        db.session.commit()

        calculate_community_activity_stats()

        db.session.expire_all()
        assert db.session.get(Community, community.id).active_daily == 0

    def test_a_bot_reply_voter_is_excluded(self, db_session):
        """`:812`'s `u.bot = False`, on the post-reply-votes INSERT.

        The post is seeded outside the window so the only candidate activity
        is the reply vote, which makes the assertion about the vote rather
        than about whatever else happens to be in range.
        """
        instance, author, community, post = _seed()
        replier = make_user(instance, 'replier', local=True)
        reply = make_post_reply(post, replier)
        bot = make_user(instance, 'botty_reply_voter', local=True)
        bot.bot = True
        vote = make_post_reply_vote(bot, reply, 1.0)
        vote.created_at = utcnow() - timedelta(hours=1)
        reply.posted_at = utcnow() - timedelta(weeks=40)
        post.posted_at = utcnow() - timedelta(weeks=40)
        community.last_active = utcnow()
        db.session.commit()

        calculate_community_activity_stats()

        db.session.expire_all()
        assert db.session.get(Community, community.id).active_daily == 0

    def test_a_banned_community_is_not_updated(self, db_session):
        """`:834`'s `c.banned = FALSE`."""
        community, _ = self._community_with_one_activity_of_each_kind(
            utcnow() - timedelta(hours=1))
        community.banned = True
        community.active_daily = 77
        db.session.commit()

        calculate_community_activity_stats()

        db.session.expire_all()
        assert db.session.get(Community, community.id).active_daily == 77

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch):
        monkeypatch.setattr('app.shared.tasks.maintenance.utcnow', _boom)

        with pytest.raises(RuntimeError, match='the task itself failed'):
            calculate_community_activity_stats()
