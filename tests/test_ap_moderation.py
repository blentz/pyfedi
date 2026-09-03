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
from app.models import File, ModLog, Notification, Post, PostReply, User
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
    takes `redis_client.lock(...)` on four keys, and it does `from app
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
