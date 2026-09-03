"""tests/test_ap_moderation.py

DEVIATION from the task brief: `delete_post_or_comment` takes
`redis_client.lock(...)` as a real `with` block (not merely acquiring it),
and this environment's fakeredis (requirements-test.txt, unpinned; observed
as 2.37.1, no `lupa` installed) implements no Lua scripting at all -- not
EVAL, not EVALSHA. `Lock.acquire()` needs none and succeeds against
`redis_double`'s fakeredis instance, but `Lock.release()` calls a Lua script
via EVALSHA and raises `redis.exceptions.ResponseError: unknown command
'evalsha'` on `__exit__`, every time. This is documented as a known caveat
on `redis_double` itself (tests/conftest.py, sub-project 5a) and already
worked around elsewhere in this codebase (`_RedisLockOnlyDouble` /
`redis_lock_only_double` in tests/test_inbox_dispatch_votes.py). Confirmed
empirically here too: all three tests below failed with exactly that
ResponseError on first run when driven through `redis_double`.

So this file defines its own narrow `redis_lock_only_double` fixture
(same pattern as test_inbox_dispatch_votes.py's) and uses it in place of
`redis_double` for every test that reaches `delete_post_or_comment`'s
success path. Of the six moderation functions this sub-project covers, only
delete_post_or_comment calls `redis_client.lock(...)`
(`restore_post_or_comment`, `site_ban_remove_data`,
`community_ban_remove_data`, `ban_user` and `unban_user` do not -- verified
by grep over app/activitypub/util.py:2268-2560), so this workaround is
scoped to this file's Post-deletion tests and is not expected to recur for
Tasks 2-9.
"""
import contextlib

import pytest

from app import db
from app.activitypub import util as ap_util
from app.constants import NOTIF_REPORT, NOTIF_USER, POST_STATUS_PUBLISHED
from app.models import File, InstanceRole, ModLog, Notification, Post, PostReply, User
from tests.factories import (make_community, make_community_ban, make_community_member,
                             make_instance, make_post, make_post_reply, make_site, make_user,
                             seed_community_owner)


class _RedisLockOnlyDouble:
    """A minimal `app.redis_client` double covering ONLY `.lock(...)` used as
    a context manager. See the module docstring's DEVIATION paragraph for
    why plain `redis_double` cannot be used for delete_post_or_comment's
    success path: this environment's fakeredis has no Lua scripting, which
    redis-py's real `Lock.release()` requires.
    """

    def lock(self, *args, **kwargs):
        return contextlib.nullcontext()


@pytest.fixture
def redis_lock_only_double(monkeypatch):
    monkeypatch.setattr('app.redis_client', _RedisLockOnlyDouble())


def seed_moderation_scene(community_name='books'):
    """A community with an owner, a content author and a separate moderator.

    `seed_community_owner` creates BOTH the Instance (id 1) and a local User
    (id 1), which `make_community` hardcodes against as real foreign keys.
    The moderator is a distinct user from the author so the
    `to_delete.author.id != deletor.id` modlog branch is reachable -- when
    the deletor IS the author, no modlog entry is written at all.
    """
    site = make_site()
    instance = seed_community_owner('peer.example')
    community = make_community(name=community_name, host='test.piefed.local')
    author = make_user(instance, 'contentauthor', local=True)
    moderator = make_user(instance, 'themoderator', local=True)
    make_community_member(moderator, community, is_moderator=True)
    db.session.commit()
    return site, instance, community, author, moderator


def _double_file_deletion(monkeypatch):
    """Stop File.delete_from_disk from touching the filesystem or the CDN.

    Both ban-removal functions call it in a loop over every File attached to
    the banned user's posts. It is patched on the MODEL CLASS rather than on
    a module, because both call sites reach it as a bound method on a row
    they just queried, not through an imported name.

    The recorded `purge_cdn` value is the observable for a registered
    asymmetry: `site_ban_remove_data` passes `purge_cdn=True` explicitly and
    `community_ban_remove_data` takes the default. Recording the flag is what
    lets a test state that difference rather than assert it from the source.
    """
    calls = []

    def fake_delete(self, purge_cdn=True):
        calls.append((self.id, purge_cdn))

    monkeypatch.setattr(File, 'delete_from_disk', fake_delete)
    return calls


def test_a_moderator_deleting_a_post_marks_it_deleted_and_decrements_counters(
        app, db_session, monkeypatch, redis_lock_only_double):
    """`delete_post_or_comment`'s Post branch, driven directly rather than
    through the inbox dispatcher.

    A patched `app.redis_client` is REQUIRED, not optional: this function
    takes `redis_client.lock(...)` on three keys in the Post branch and four
    more in the PostReply branch, and it does `from app
    import redis_client` inside its own body, so a patch of `app.redis_client`
    reaches it. Without one the test talks to the real, shared,
    never-truncated Redis in the compose stack. `redis_lock_only_double` is
    used here rather than `redis_double` -- see the module docstring's
    DEVIATION paragraph for why.

    Both counters are seeded to 5 before the call. They default to 0, and a
    decrement from 0 to -1 is indistinguishable from a decrement that did not
    happen if the assertion only checks "not 5" -- so the baseline is
    explicit and the assertion is an exact equality.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    community.post_count = 5
    author.post_count = 5
    db.session.commit()

    ap_util.delete_post_or_comment(moderator, post, False,
                                   {'id': 'https://peer.example/activities/delete/1'},
                                   'spam')

    assert post.deleted is True
    assert post.deleted_by == moderator.id
    assert community.post_count == 4
    assert author.post_count == 4


def test_deleting_another_users_post_writes_a_modlog_entry(
        app, db_session, monkeypatch, redis_lock_only_double):
    """`if to_delete.author.id != deletor.id:` -- the modlog entry is written
    only when someone deletes SOMEONE ELSE'S content. A self-delete is not
    moderation and is not logged, which its twin below asserts.

    `add_to_modlog` runs for real here. It raises on an unknown action
    (app/utils.py), so the action string `delete_post` is itself under test:
    a typo would raise rather than silently write nothing.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    db.session.commit()

    ap_util.delete_post_or_comment(moderator, post, False,
                                   {'id': 'https://peer.example/activities/delete/1'},
                                   'spam')

    entries = db.session.query(ModLog).filter_by(target_user_id=author.id).all()
    assert len(entries) == 1
    assert entries[0].action == 'delete_post'
    assert entries[0].reason == 'spam'


def test_an_author_deleting_their_own_post_writes_no_modlog_entry(
        app, db_session, monkeypatch, redis_lock_only_double):
    """The false side of `if to_delete.author.id != deletor.id:`. The post is
    still deleted -- both assertions are made, so this cannot pass by the
    deletion having been refused.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    db.session.commit()

    ap_util.delete_post_or_comment(author, post, False,
                                   {'id': 'https://peer.example/activities/delete/1'},
                                   'changed my mind')

    assert post.deleted is True
    assert db.session.query(ModLog).count() == 0


def test_an_unrelated_user_cannot_delete_a_post(app, db_session, monkeypatch, redis_lock_only_double):
    """All four disjuncts false. This is the test every mutation below must
    leave passing -- if dropping a disjunct made an UNAUTHORISED delete
    succeed, this test is what catches it.

    The observable is that the post is NOT deleted. The failure branch calls
    `log_incoming_ap(..., APLOG_FAILURE, ..., 'Deletor did not have
    permisson')`, but that writes nothing unless LOG_ACTIVITYPUB_TO_DB is on,
    so the real effect is asserted instead.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    stranger = make_user(instance, 'stranger', local=True)
    post = make_post(community, author, None, title='a post')
    community.post_count = 5
    db.session.commit()

    ap_util.delete_post_or_comment(stranger, post, False,
                                   {'id': 'https://peer.example/activities/delete/1'},
                                   'spam')

    assert post.deleted is False
    assert community.post_count == 5


def test_the_author_may_delete_their_own_post(app, db_session, monkeypatch, redis_lock_only_double):
    """Disjunct 1: `to_delete.user_id == deletor.id`. The author is given NO
    moderator row and is not an instance admin, so the other three disjuncts
    are false and dropping this one fails exactly this test.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    db.session.commit()

    ap_util.delete_post_or_comment(author, post, False,
                                   {'id': 'https://peer.example/activities/delete/1'}, '')

    assert post.deleted is True


def test_a_same_instance_admin_may_delete_a_post(app, db_session, monkeypatch, redis_lock_only_double):
    """Disjunct 2, which is itself a CONJUNCTION:
    `deletor.instance_id == to_delete.author.instance_id and
    deletor.is_instance_admin()`. Both halves must hold, and the two halves
    are mutation-tested separately in Step 4.

    DEVIATION from the brief: the brief's snippet grants admin via
    `admin.roles.append(Role.query.filter_by(name='Admin').first())`. Two
    problems with that, found by reading `app/models.py`: (1)
    `User.is_instance_admin()` (line 1277-1284) -- the method this guard
    actually calls -- never reads `roles`/`Role` at all; it queries
    `InstanceRole` for a row whose `instance_id` matches the user's OWN
    `instance_id`. `roles`/`Role` is what `is_admin()` (line 1259) reads,
    and `is_admin()` is not in this guard. (2) No 'Admin' `Role` row exists
    in the test database -- one is only created by `app/cli.py`'s init
    routine, which test setup does not run -- so
    `Role.query.filter_by(name='Admin').first()` returns `None` and
    `admin.roles.append(None)` raises. This test instead grants admin the
    way the rest of the suite already does for `is_instance_admin()`
    (e.g. tests/test_inbox_dispatch_new_content.py:330): an `InstanceRole`
    row keyed to the admin's own instance.

    The admin is NOT a community moderator, so disjunct 3 is false.

    A SECOND deviation, found only by running the disjunct-2-removal
    mutation below: `make_community` hardcodes `instance_id=1`, and
    `seed_community_owner`'s Instance (the one `admin` and `author` are
    both made on) is also id 1 -- the first row inserted after the
    per-test truncation. Left alone, `community` and `admin` sit on the
    SAME instance, so the very `InstanceRole` row that makes
    `admin.is_instance_admin()` true (disjunct 2's second half) also makes
    `community.is_instance_admin(admin)` true (disjunct 4) -- the two
    disjuncts were not actually isolated, and removing disjunct 2 entirely
    left this test passing on disjunct 4 alone. The community is moved to
    a separate, freshly-created home instance so only disjunct 2 is true.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    admin = make_user(instance, 'theadmin', local=True)
    db.session.add(InstanceRole(instance_id=admin.instance_id, user_id=admin.id, role='admin'))
    community_host = make_instance('communityhost.example')
    community.instance_id = community_host.id
    post = make_post(community, author, None, title='a post')
    db.session.commit()

    ap_util.delete_post_or_comment(admin, post, False,
                                   {'id': 'https://peer.example/activities/delete/1'}, '')

    assert post.deleted is True


def test_a_community_moderator_may_delete_a_post(app, db_session, monkeypatch, redis_lock_only_double):
    """Disjunct 3: `community.is_moderator(deletor)`. `seed_moderation_scene`
    gives the moderator a CommunityMember row with `is_moderator=True` and
    nothing else -- not the author, not an admin -- so this disjunct is the
    only true one.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    db.session.commit()

    ap_util.delete_post_or_comment(moderator, post, False,
                                   {'id': 'https://peer.example/activities/delete/1'}, '')

    assert post.deleted is True


def test_a_communitys_home_instance_admin_may_delete_a_post(
        app, db_session, monkeypatch, redis_lock_only_double):
    """Disjunct 4: `community.is_instance_admin(deletor)`. Reachability
    analysis (see the report for the full writeup): `Community.
    is_instance_admin` (app/models.py:769-776) checks for an `InstanceRole`
    row matching `community.instance_id`, whereas disjunct 2's
    `deletor.is_instance_admin()` (app/models.py:1277-1284) checks a row
    matching `deletor.instance_id`. The two only coincide when
    `deletor.instance_id == community.instance_id` -- nothing in the guard
    or in `InstanceRole`'s schema forces that. This deletor lives on a
    freshly-created THIRD instance (so `deletor.instance_id !=
    to_delete.author.instance_id`, making disjunct 2's conjunction false
    regardless of its `is_instance_admin()` half), and holds an
    `InstanceRole` row keyed to the COMMUNITY's home instance rather than
    their own, which is exactly what `Community.is_instance_admin` reads.
    Not the author, not a moderator, so disjuncts 1 and 3 are false too.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    other_instance = make_instance('otherinstance.example')
    deletor = make_user(other_instance, 'foreignadmin', local=False)
    db.session.add(InstanceRole(instance_id=community.instance_id, user_id=deletor.id, role='admin'))
    post = make_post(community, author, None, title='a post')
    db.session.commit()

    ap_util.delete_post_or_comment(deletor, post, False,
                                   {'id': 'https://peer.example/activities/delete/1'}, '')

    assert post.deleted is True


def test_a_different_instances_admin_cannot_delete_an_unrelated_communitys_post(
        app, db_session, monkeypatch, redis_lock_only_double):
    """Closes the instance-match half of disjunct 2's conjunction
    (`deletor.instance_id == to_delete.author.instance_id`) against the
    weakening mutation that drops it (Step 4's mutation table). This
    deletor genuinely IS an instance admin -- `deletor.is_instance_admin()`
    is True, backed by an `InstanceRole` row on their OWN home instance --
    but that instance is neither the author's nor the community's, so
    every disjunct is false as the guard is written: disjunct 2's
    instance-match half fails, and disjunct 4 fails too since the
    `InstanceRole` row is keyed to `other_instance`, not
    `community.instance_id`.

    If the instance-match half were dropped, disjunct 2 would collapse to
    `deletor.is_instance_admin()` alone, which is True for this deletor,
    and the delete would wrongly succeed -- this is the test that would
    catch that.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    other_instance = make_instance('otherinstance.example')
    deletor = make_user(other_instance, 'foreignadmin', local=False)
    db.session.add(InstanceRole(instance_id=other_instance.id, user_id=deletor.id, role='admin'))
    post = make_post(community, author, None, title='a post')
    community.post_count = 5
    db.session.commit()

    ap_util.delete_post_or_comment(deletor, post, False,
                                   {'id': 'https://peer.example/activities/delete/1'}, '')

    assert post.deleted is False
    assert community.post_count == 5


def test_deleting_a_reply_decrements_every_counter_it_maintains(
        app, db_session, monkeypatch, redis_lock_only_double):
    """`delete_post_or_comment`'s PostReply branch. It decrements FIVE
    counters where the Post branch decrements two:
    `post.reply_count` (only when the author is not a bot),
    `post.reply_count_cross_posted` (only when already non-zero),
    `author.post_reply_count`, and `community.post_reply_count`.

    All four are seeded to 5. They default to 0, and `reply_count_cross_posted`
    is additionally guarded by `if to_delete.post.reply_count_cross_posted:`
    -- at its default of 0 that guard is FALSE, so a test resting on the
    default would never reach the decrement and would pass against its
    removal.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    reply = make_post_reply(post, author)
    post.reply_count = 5
    post.reply_count_cross_posted = 5
    author.post_reply_count = 5
    community.post_reply_count = 5
    author.bot = False
    db.session.commit()

    ap_util.delete_post_or_comment(moderator, reply, False,
                                   {'id': 'https://peer.example/activities/delete/1'}, '')

    assert reply.deleted is True
    assert reply.deleted_by == moderator.id
    assert post.reply_count == 4
    assert post.reply_count_cross_posted == 4
    assert author.post_reply_count == 4
    assert community.post_reply_count == 4


def test_deleting_a_bots_reply_leaves_the_posts_reply_count_alone(
        app, db_session, monkeypatch, redis_lock_only_double):
    """`if not to_delete.author.bot:` guards BOTH `post.reply_count` and
    `post.reply_count_cross_posted`. A bot's reply is deleted and its author
    and community counters still fall, but the post's do not.

    `bot` is set to True explicitly; `make_user` leaves it at the column
    default, so a test resting on that default would be asserting the wrong
    side of the branch.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    reply = make_post_reply(post, author)
    post.reply_count = 5
    author.post_reply_count = 5
    community.post_reply_count = 5
    author.bot = True
    db.session.commit()

    ap_util.delete_post_or_comment(moderator, reply, False,
                                   {'id': 'https://peer.example/activities/delete/1'}, '')

    assert reply.deleted is True
    assert post.reply_count == 5
    assert author.post_reply_count == 4
    assert community.post_reply_count == 4


def test_deleting_a_nested_reply_decrements_its_ancestors_child_count(
        app, db_session, monkeypatch, redis_lock_only_double):
    """`if to_delete.path:` runs raw SQL decrementing `child_count` for every
    id in `path[:-1]` -- the reply's ancestors, excluding itself.

    `make_post_reply` leaves `path` as None, so this branch is unreachable
    unless a test sets it. `PostReply.path` is `ARRAY(db.Integer)`
    (app/models.py:2898), so a plain list of ids is the right shape. Both
    replies get an explicit `path`, and the PARENT's child_count is seeded
    to 5 so the decrement is an exact assertion rather than a "less than
    before".

    The raw SQL (`update post_reply set child_count = child_count - 1 where
    id in :parents`) bypasses the ORM's identity map, so the row changes
    without the mapped `parent` object being notified. The explicit
    `db.session.refresh(parent)` below is nevertheless NOT currently
    required to make the assertion pass -- verified by removing it and
    running this test, not reasoned about: `app/__init__.py:81` leaves
    `expire_on_commit` at its default of `True` (it overrides only
    `autoflush`), and `delete_post_or_comment` calls `db.session.commit()`
    immediately after the raw SQL, which expires `parent` along with every
    other object in the session -- the next read of `parent.child_count`
    re-fetches it from the database on its own. The refresh is kept anyway
    so this assertion stays correct if `session_options` ever changes to
    stop expiring on commit, not because the test fails without it today.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    parent = make_post_reply(post, author, body='parent')
    child = make_post_reply(post, author, body='child')
    parent.path = [parent.id]
    child.path = [parent.id, child.id]
    parent.child_count = 5
    db.session.commit()

    ap_util.delete_post_or_comment(moderator, child, False,
                                   {'id': 'https://peer.example/activities/delete/1'}, '')

    db.session.refresh(parent)
    assert child.deleted is True
    assert parent.child_count == 4


def test_deleting_a_post_removes_its_notifications_but_keeps_report_notifs(
        app, db_session, monkeypatch, redis_lock_only_double):
    """The Post branch deletes Notifications whose `targets->>'post_id'`
    matches, EXCEPT those of type NOTIF_REPORT or NOTIF_REPORT_ESCALATION --
    the `continue` inside the loop. This notification-cleanup code lives
    only in the `isinstance(to_delete, Post)` half of `delete_post_or_comment`
    (app/activitypub/util.py:2229-2236) -- the PostReply branch has no
    equivalent -- so this test, unlike the other three in this task, deletes
    a Post rather than a PostReply.

    Two notifications are seeded against the same post so the test
    discriminates: an ordinary one that must go and a report one that must
    stay. A single-notification fixture could not tell a working exemption
    from a loop that deleted nothing.

    DEVIATION from the brief: the brief's `ordinary` notification uses
    `notif_type=0`. Read literally that is not a magic number: 0 is
    `NOTIF_USER` (app/constants.py:52), a real, non-report constant, so the
    test's intent -- something the exemption must NOT protect -- still
    holds. Named explicitly here rather than left as a bare 0.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    ordinary = Notification(title='reply to your post', user_id=author.id,
                            author_id=moderator.id, notif_type=NOTIF_USER,
                            targets={'post_id': post.id})
    report = Notification(title='post reported', user_id=moderator.id,
                          author_id=author.id, notif_type=NOTIF_REPORT,
                          targets={'post_id': post.id})
    db.session.add_all([ordinary, report])
    db.session.commit()

    ap_util.delete_post_or_comment(moderator, post, False,
                                   {'id': 'https://peer.example/activities/delete/1'}, '')

    surviving = db.session.query(Notification).all()
    assert len(surviving) == 1
    assert surviving[0].notif_type == NOTIF_REPORT


def test_restoring_a_post_clears_deleted_and_restores_counters(
        app, db_session, monkeypatch, redis_lock_only_double):
    """`restore_post_or_comment`'s Post branch. Note it takes NO redis locks
    where `delete_post_or_comment` wraps every one of these same counter
    mutations in one -- a registered asymmetry, not something this test
    fixes. `redis_lock_only_double` is still requested so the test is safe if that
    changes.

    Counters are seeded to 4 and asserted at 5, the mirror of Task 1's
    deletion test.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    post.deleted = True
    post.deleted_by = moderator.id
    community.post_count = 4
    author.post_count = 4
    db.session.commit()

    ap_util.restore_post_or_comment(moderator, post, False,
                                    {'id': 'https://peer.example/activities/undo/1'}, 'appeal')

    assert post.deleted is False
    assert post.deleted_by is None
    assert community.post_count == 5
    assert author.post_count == 5


def test_restoring_a_reply_restores_only_the_counters_it_knows_about(
        app, db_session, monkeypatch, redis_lock_only_double):
    """PINS A DEFECT. `restore_post_or_comment`'s PostReply branch increments
    `post.reply_count` (when not a bot) and `author.post_reply_count` -- and
    NOTHING ELSE. `delete_post_or_comment` decrements four counters for the
    same row.

    So `community.post_reply_count` and `post.reply_count_cross_posted` are
    asserted UNCHANGED here, which is the defect: a restore does not undo
    what the delete did. Task 5 proves this end to end with a round trip;
    this test states it for the restore call in isolation.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    reply = make_post_reply(post, author)
    reply.deleted = True
    post.reply_count = 4
    post.reply_count_cross_posted = 4
    author.post_reply_count = 4
    community.post_reply_count = 4
    author.bot = False
    db.session.commit()

    ap_util.restore_post_or_comment(moderator, reply, False,
                                    {'id': 'https://peer.example/activities/undo/1'}, '')

    assert reply.deleted is False
    assert post.reply_count == 5
    assert author.post_reply_count == 5
    assert community.post_reply_count == 4      # NOT restored -- the defect
    assert post.reply_count_cross_posted == 4   # NOT restored -- the defect


def test_an_unrelated_user_cannot_restore_a_post(app, db_session, monkeypatch, redis_lock_only_double):
    """The same four-disjunct guard `delete_post_or_comment` carries, copied
    rather than shared -- verified textually identical, which is why this
    task tests the refusal and one success instead of repeating Task 2's
    four-way isolation.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    stranger = make_user(instance, 'stranger', local=True)
    post = make_post(community, author, None, title='a post')
    post.deleted = True
    db.session.commit()

    ap_util.restore_post_or_comment(stranger, post, False,
                                    {'id': 'https://peer.example/activities/undo/1'}, '')

    assert post.deleted is True


def test_restoring_another_users_post_writes_a_restore_modlog_entry(
        app, db_session, monkeypatch, redis_lock_only_double):
    """Action string `restore_post`, distinct from deletion's `delete_post`.
    `add_to_modlog` raises on an unknown action, so the string is under test.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    post.deleted = True
    db.session.commit()

    ap_util.restore_post_or_comment(moderator, post, False,
                                    {'id': 'https://peer.example/activities/undo/1'}, 'appeal')

    entries = db.session.query(ModLog).all()
    assert len(entries) == 1
    assert entries[0].action == 'restore_post'


def test_a_delete_then_restore_cycle_permanently_loses_two_counters(
        app, db_session, monkeypatch, redis_lock_only_double):
    """PINS A DEFECT, and it is this slice's most consequential.

    `delete_post_or_comment` decrements four counters for a reply;
    `restore_post_or_comment` increments two. So a delete followed by a
    restore -- the exact sequence a moderator produces by removing a comment
    and then reversing it on appeal -- leaves `community.post_reply_count`
    and `post.reply_count_cross_posted` permanently one lower. Every
    subsequent cycle loses one more, and nothing later notices or repairs it.

    A single-direction test cannot show this. Asserting that restore leaves a
    counter alone is only a defect if delete moved it, so the two calls have
    to happen in one test with the starting values recorded.

    DO NOT FIX -- registered, because correcting a counter changes numbers
    users already see and the historical drift is unknown.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    reply = make_post_reply(post, author)
    post.reply_count = 5
    post.reply_count_cross_posted = 5
    author.post_reply_count = 5
    community.post_reply_count = 5
    author.bot = False
    db.session.commit()

    ap_util.delete_post_or_comment(moderator, reply, False,
                                   {'id': 'https://peer.example/activities/delete/1'}, '')
    ap_util.restore_post_or_comment(moderator, reply, False,
                                    {'id': 'https://peer.example/activities/undo/1'}, '')

    assert reply.deleted is False
    # The two counters both halves maintain come back:
    assert post.reply_count == 5
    assert author.post_reply_count == 5
    # The two only the delete side maintains do NOT:
    assert community.post_reply_count == 4
    assert post.reply_count_cross_posted == 4


def test_a_post_delete_then_restore_cycle_is_lossless(
        app, db_session, monkeypatch, redis_lock_only_double):
    """The contrast that makes the reply finding sharp: the POST branches of
    both functions maintain the same two counters, so a post round trip
    returns to exactly where it started.

    Without this test the reply result reads as "these functions are sloppy
    about counters". With it, the finding is specific: the Post branches
    agree and the PostReply branches do not.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    community.post_count = 5
    author.post_count = 5
    db.session.commit()

    ap_util.delete_post_or_comment(moderator, post, False,
                                   {'id': 'https://peer.example/activities/delete/1'}, '')
    ap_util.restore_post_or_comment(moderator, post, False,
                                    {'id': 'https://peer.example/activities/undo/1'}, '')

    assert post.deleted is False
    assert community.post_count == 5
    assert author.post_count == 5


def test_a_site_ban_deletes_every_post_and_reply_the_user_made(
        app, db_session, monkeypatch):
    """`site_ban_remove_data` filters on `user_id` alone -- no community
    filter, unlike `community_ban_remove_data`. So content in EVERY community
    goes, which is what a site ban means.

    Two communities are seeded precisely to prove the absence of that filter:
    a one-community fixture cannot tell "all the user's content" from "this
    community's content".
    """
    _double_file_deletion(monkeypatch)
    site, instance, community, author, moderator = seed_moderation_scene()
    other = make_community(name='elsewhere', host='test.piefed.local')
    here = make_post(community, author, None, title='here')
    there = make_post(other, author, None, title='there')
    reply = make_post_reply(here, author)
    db.session.commit()

    ap_util.site_ban_remove_data(moderator.id, author)

    assert here.deleted is True
    assert there.deleted is True
    assert reply.deleted is True
    assert here.deleted_by == moderator.id


def test_a_site_ban_does_not_zero_the_users_reply_count(app, db_session, monkeypatch):
    """PINS A DEFECT. DO NOT FIX -- a later task does.

    `site_ban_remove_data` contains `blocked.reply_count = 0`. `User` has no
    `reply_count` column: it declares `post_count` and `post_reply_count`
    (app/models.py), and `reply_count` belongs to `Post`. SQLAlchemy accepts
    the assignment as an ordinary Python attribute on the instance, so it
    never reaches the database and never raises.

    The effect is that a site-banned user's REAL reply counter keeps its
    pre-ban value forever, while `blocked.post_count = 0` on the very next
    line works because that column does exist. `community_ban_remove_data`
    decrements the real `post_reply_count` correctly.

    Both counters are seeded to 5 so the contrast is exact: post_count is
    asserted at 0 (the working line) and post_reply_count at 5 (the broken
    one). Asserting only the second would leave a reader unable to tell a
    bug from a deliberate choice not to zero anything.
    """
    _double_file_deletion(monkeypatch)
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    make_post_reply(post, author)
    author.post_count = 5
    author.post_reply_count = 5
    db.session.commit()

    ap_util.site_ban_remove_data(moderator.id, author)

    assert author.post_count == 0        # the line that works
    assert author.post_reply_count == 5  # the line that does not -- the defect


def test_a_site_ban_deletes_attached_files_and_purges_the_cdn(
        app, db_session, monkeypatch):
    """`site_ban_remove_data` calls `delete_from_disk(purge_cdn=True)`
    EXPLICITLY, where `community_ban_remove_data` takes the default. The
    recorded flag is asserted so the asymmetry is observable in the suite
    rather than read off the source.

    `source_url` is set to '' afterwards by the function; it is asserted too,
    because a File whose bytes are gone but whose source_url still points at
    them is a broken row rather than a deleted one.
    """
    calls = _double_file_deletion(monkeypatch)
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    attached = File(source_url='https://peer.example/img.png')
    db.session.add(attached)
    db.session.commit()
    post.image_id = attached.id
    db.session.commit()

    ap_util.site_ban_remove_data(moderator.id, author)

    assert (attached.id, True) in calls
    assert attached.source_url == ''


def test_a_site_ban_deletes_the_users_avatar_and_cover(app, db_session, monkeypatch):
    """`if blocked.avatar_id:` and `if blocked.cover_id:` -- both guarded, and
    both are None on a factory-built user, so a test resting on the default
    would never reach either branch.

    `community_ban_remove_data` has NO equivalent: a community ban leaves the
    avatar alone. That asymmetry is defensible and the source says so; it is
    registered, not fixed.
    """
    calls = _double_file_deletion(monkeypatch)
    site, instance, community, author, moderator = seed_moderation_scene()
    avatar = File(source_url='https://peer.example/avatar.png')
    cover = File(source_url='https://peer.example/cover.png')
    db.session.add_all([avatar, cover])
    db.session.commit()
    author.avatar_id = avatar.id
    author.cover_id = cover.id
    db.session.commit()

    ap_util.site_ban_remove_data(moderator.id, author)

    assert avatar.id in [c[0] for c in calls]
    assert cover.id in [c[0] for c in calls]
    assert avatar.source_url == ''
    assert cover.source_url == ''


def test_a_site_ban_skips_content_already_deleted(app, db_session, monkeypatch):
    """Both queries filter `deleted=False`. An already-deleted post must not
    have its counters decremented a second time, which is what that filter
    prevents.

    The community's counter is seeded and asserted to prove the row was
    skipped rather than merely left flagged: a post already deleted is
    already flagged, so `deleted is True` alone cannot discriminate.
    """
    _double_file_deletion(monkeypatch)
    site, instance, community, author, moderator = seed_moderation_scene()
    already = make_post(community, author, None, title='already gone')
    already.deleted = True
    community.post_count = 5
    db.session.commit()

    ap_util.site_ban_remove_data(moderator.id, author)

    assert community.post_count == 5


def test_a_site_ban_skips_replies_already_deleted(app, db_session, monkeypatch):
    """The reply query's `deleted=False` filter, which NO other test can kill.

    `test_a_site_ban_skips_content_already_deleted` covers the POST query's copy
    of the same clause. The two are separate call sites in
    `site_ban_remove_data`, and dropping either one is a distinct regression --
    so each needs its own fixture, and until this test existed the reply copy
    was enforced by nothing.

    The reply is the ONLY content seeded, and it is already deleted, so a
    working filter leaves every counter alone. Without the filter the function
    would decrement `community.post_reply_count` for a row it had already
    accounted for -- the double-decrement that makes this worth pinning rather
    than registering.
    """
    _double_file_deletion(monkeypatch)
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    already = make_post_reply(post, author)
    already.deleted = True
    post.reply_count = 5
    community.post_reply_count = 5
    author.bot = False
    db.session.commit()

    ap_util.site_ban_remove_data(moderator.id, author)

    assert community.post_reply_count == 5
    assert post.reply_count == 5


def test_a_community_ban_deletes_only_that_communitys_content(
        app, db_session, monkeypatch):
    """`community_ban_remove_data` filters on `user_id` AND `community_id`,
    where `site_ban_remove_data` filters on `user_id` alone. Content the same
    user made elsewhere must survive.

    Two communities are required. With one, the community filter is
    unkillable -- dropping it changes nothing observable, exactly the
    combinatorial gap sub-project 11 hit on `post_ap_context`'s post_id.
    """
    _double_file_deletion(monkeypatch)
    site, instance, community, author, moderator = seed_moderation_scene()
    other = make_community(name='elsewhere', host='test.piefed.local')
    here = make_post(community, author, None, title='here')
    there = make_post(other, author, None, title='there')
    here_reply = make_post_reply(here, author)
    there_reply = make_post_reply(there, author)
    db.session.commit()

    ap_util.community_ban_remove_data(moderator.id, community.id, author)

    assert here.deleted is True
    assert here_reply.deleted is True
    assert there.deleted is False
    assert there_reply.deleted is False


def test_a_community_ban_decrements_the_users_real_reply_counter(
        app, db_session, monkeypatch):
    """The contrast with `site_ban_remove_data`'s broken line: this function
    does `blocked.post_reply_count -= 1` per reply, against the real column,
    and it works. Both are asserted so the pair reads as one finding.

    Counters seeded to 5; one post and one reply are removed, so both fall
    to 4 -- a relative decrement, unlike the site path's absolute zeroing.
    """
    _double_file_deletion(monkeypatch)
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    make_post_reply(post, author)
    author.post_count = 5
    author.post_reply_count = 5
    author.bot = False
    db.session.commit()

    ap_util.community_ban_remove_data(moderator.id, community.id, author)

    assert author.post_count == 4
    assert author.post_reply_count == 4


def test_a_community_ban_deletes_files_without_purging_the_cdn(
        app, db_session, monkeypatch):
    """PINS A NON-ASYMMETRY, which is why the assertion is `is True`.

    `community_ban_remove_data` calls `delete_from_disk()` with no argument
    and `site_ban_remove_data` passes `purge_cdn=True` explicitly -- but the
    parameter DEFAULTS to True (`app/models.py`), so both paths purge and the
    difference is in the spelling alone.

    Asserting the flag's real value is what makes that legible: reading the
    two call sites side by side invites the conclusion that a community ban
    leaves files on the CDN, and this test says otherwise in the suite rather
    than leaving the next reader to check the signature.
    """
    calls = _double_file_deletion(monkeypatch)
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    attached = File(source_url='https://peer.example/img.png')
    db.session.add(attached)
    db.session.commit()
    post.image_id = attached.id
    db.session.commit()

    ap_util.community_ban_remove_data(moderator.id, community.id, author)

    assert len(calls) == 1
    assert calls[0][0] == attached.id
    assert calls[0][1] is True   # the default -- same as the site path passes
    assert attached.source_url == ''
