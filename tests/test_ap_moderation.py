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
from app.constants import NOTIF_REPORT, POST_STATUS_PUBLISHED
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
