"""`app/shared/post.py`'s moderator verbs -- Group B of five.

SCOPE. Six functions, 110 uncovered statements and 56 uncovered branch arcs
when this file was started:

  mod_remove_post :1039-1081 (26/12), lock_post :927-960 (22/14),
  mod_restore_post :1082-1114 (20/8), sticky_post :990-1019 (18/10),
  move_post :961-989 (15/8), hide_post :1020-1038 (9/4).

THE HARNESS IS INHERITED FROM GROUP A and lives in tests/factories.py:
`seed_post_context`, `web_ctx` and `bearer`. The facts behind it are recorded
in tests/README.md 206-212. The three that bind hardest here:

  - NO TEST MAY REQUEST `redis_double`. mod_remove_post:1045 and
    mod_restore_post:1088 both do `from app import redis_client` INSIDE the
    function body and then lock on it. Group A proved the fixture reaches that
    shape and then breaks it: `redis_client.lock(...)`'s `__exit__` releases
    through EVALSHA, which fakeredis does not implement, giving
    `redis.exceptions.ResponseError: unknown command 'evalsha'`. Without the
    fixture the call binds to the compose stack's real Redis and works.

  - SRC_API ARMS NEED NO REQUEST CONTEXT. `get_ip_address` (app/__init__.py)
    wraps its `request` read in `except RuntimeError` and returns ''. Only the
    SRC_WEB arms need `web_ctx`, and here they need it for `flash` alone.

  - NONE OF THESE SIX FUNCTIONS RENDERS A TEMPLATE. Group A's rule about
    asserting on `result.status_code` applies to functions that return a
    rendered template -- `vote_for_post` and `subscribe_post`. Every function
    in THIS file returns `user.id, post`, or None on the SRC_WEB arms of
    lock_post, move_post, mod_remove_post and mod_restore_post.

THE GATES ARE THE POINT OF THIS FILE, and they do not agree. Five of the six
functions gate on permission, in two distinct predicates:

  P1  is_moderator or user.is_admin_or_staff()
      -- lock_post:941, mod_remove_post:1049, mod_restore_post:1091
  P2  is_moderator or community.is_instance_admin(user) or user.is_admin_or_staff()
      -- move_post:969, sticky_post:999

`Community.is_admin_or_staff(user)` (app/models.py:778-779) is
`return user.is_admin_or_staff()`, a pure delegating wrapper, so lock_post's
spelling differs from the others cosmetically but not semantically. The real
divergence is `is_instance_admin`, which P1 omits. It is REGISTERED, NOT FIXED
-- see the round's spec. `hide_post` has no gate, correctly: it writes
per-user state.

TWO FAILURE MODES, TOO. mod_remove_post:1050 and mod_restore_post:1092 RAISE
`Exception('Does not have permission')`. lock_post, move_post and sticky_post
fall through and return as though they had succeeded -- which Task 10 fixes
for the SRC_API arm only, because the web callers have no error handling.

A GATE TEST MUST ASSERT THE SIDE EFFECT DID NOT HAPPEN. Every one of these
functions returns the same shape on the permitted and the refused path, so
the return value alone distinguishes nothing. Assert the post's field, the
ModLog row count, and where federation is in question, whether task_selector
fired.

`add_to_modlog` (app/utils.py:3564) COMMITS at :3582, so a test asserting
that nothing was written must query the database rather than the session. It
also raises at :3569 for an action outside ModLog.action_map; all seven
strings this group passes were verified present.
"""

import pytest
from types import SimpleNamespace

from app import db
from app.constants import SRC_API, SRC_WEB
from app.models import CommunityMember, ModLog, Post, hidden_posts
from app.shared.post import (
    hide_post,
    lock_post,
    mod_remove_post,
    mod_restore_post,
    move_post,
    sticky_post,
)
from tests.factories import bearer, make_community, make_community_member, \
    make_post, make_user, seed_post_context, web_ctx


def seed_moderator(s, user=None):
    """Make `user` (default `s.voter`) a moderator of `s.community`.

    `Community.moderators()` (app/models.py:716-722) filters
    `is_banned == False`, so a banned CommunityMember is NOT a moderator --
    which is an arm worth pinning separately rather than assuming.
    """
    return make_community_member(user or s.voter, s.community, is_moderator=True)


def test_hiding_a_post_through_the_api_inserts_the_row(db_session):
    """`:1029`'s `mark_post_as_hidden`, reached through `:1028`'s true arm.

    Catches a regression inverting `:1028`, which would send a hide request
    down the DELETE branch and leave the table empty.
    """
    s = seed_post_context(community_name='moderation')

    user_id, post = hide_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    assert user_id == s.voter.id
    rows = db.session.execute(
        hidden_posts.select().where(hidden_posts.c.user_id == s.voter.id)).fetchall()
    assert {row.hidden_post_id for row in rows} == {s.post.id}


def test_unhiding_a_post_deletes_the_row(db_session):
    """`:1031`'s raw DELETE, reached through `:1028`'s false arm.

    Catches a regression inverting `:1028`, which would re-insert on an unhide
    instead of removing.
    """
    s = seed_post_context(community_name='moderation')
    hide_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    user_id, post = hide_post(s.post.id, False, SRC_API, auth=bearer(s.voter))

    assert user_id == s.voter.id
    rows = db.session.execute(
        hidden_posts.select().where(hidden_posts.c.user_id == s.voter.id)).fetchall()
    assert rows == []


def test_hiding_twice_does_not_duplicate_the_row(db_session):
    """`mark_post_as_hidden`'s own `has_hidden_post` guard.

    `hide_post` has no duplicate check of its own -- the model method does.
    Catches a regression dropping that guard, which would append a second row
    for the same pair.
    """
    s = seed_post_context(community_name='moderation')
    hide_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    hide_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    rows = db.session.execute(
        hidden_posts.select().where(hidden_posts.c.user_id == s.voter.id)).fetchall()
    assert len(rows) == 1


def test_unhiding_a_post_that_was_never_hidden_is_a_no_op(db_session):
    """`:1031`'s DELETE against zero matching rows.

    The raw SQL deletes nothing and does not raise. Catches a regression that
    made the unhide path assume a row exists.
    """
    s = seed_post_context(community_name='moderation')

    user_id, post = hide_post(s.post.id, False, SRC_API, auth=bearer(s.voter))

    assert user_id == s.voter.id
    rows = db.session.execute(
        hidden_posts.select().where(hidden_posts.c.user_id == s.voter.id)).fetchall()
    assert rows == []


def test_the_web_arm_reads_current_user(db_session, app):
    """`:1021`'s false arm and `:1024`'s `current_user`.

    Catches a regression making `:1021` read the bearer token unconditionally,
    which would raise with auth=None. `hide_post` has no gate, so this is the
    only thing `:1021`'s false arm needs.
    """
    s = seed_post_context(community_name='moderation')

    with web_ctx(app, s.voter):
        user_id, post = hide_post(s.post.id, True, SRC_WEB)

    assert user_id == s.voter.id
    rows = db.session.execute(
        hidden_posts.select().where(hidden_posts.c.user_id == s.voter.id)).fetchall()
    assert {row.hidden_post_id for row in rows} == {s.post.id}
