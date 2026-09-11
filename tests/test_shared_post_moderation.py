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


def make_site_admin(user):
    """Give `user` a role named exactly 'Admin'.

    `User.is_admin()` (app/models.py:1259-1265) checks role NAMES, not
    permissions, so `grant_permission` cannot produce a site admin however it
    is called. The name must be the literal string 'Admin'.
    """
    from app.models import Role, user_role
    role = Role(name='Admin', weight=0)
    db.session.add(role)
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
    db.session.commit()
    return role


def make_instance_admin(user, instance):
    """An InstanceRole making `user` an admin of `instance`.

    `Community.is_instance_admin(user)` (app/models.py:769-776) looks up
    InstanceRole by the COMMUNITY's instance_id, not the user's, so the
    instance passed here must be the one `seed_post_context`'s community
    resolves to -- which is the first instance seeded, id 1.
    """
    from app.models import InstanceRole
    role = InstanceRole(instance_id=instance.id, user_id=user.id, role='admin')
    db.session.add(role)
    db.session.commit()
    return role


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


def test_a_moderator_locks_a_post_through_the_api(db_session):
    """`:941`'s true arm via `is_moderator`, `:942`'s assignment, `:951`'s
    federation, and `:958`'s return.

    Catches a regression inverting `:941`, which would leave comments_enabled
    untouched. Asserts the field rather than the return, because `:958` returns
    the same shape on the refused path.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    s.post.comments_enabled = True
    db.session.commit()

    user_id, post = lock_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    assert user_id == s.voter.id
    db.session.refresh(s.post)
    assert s.post.comments_enabled is False


def test_unlocking_sets_comments_enabled_back_to_true(db_session):
    """`:934`'s false arm, `:938`'s assignment and `:939`'s modlog_type.

    Catches a regression collapsing `:934`, which would lock on an unlock
    request.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    s.post.comments_enabled = False
    db.session.commit()

    lock_post(s.post.id, False, SRC_API, auth=bearer(s.voter))

    db.session.refresh(s.post)
    assert s.post.comments_enabled is True


def test_locking_writes_a_modlog_entry_naming_the_action(db_session):
    """`:944-946`'s add_to_modlog with `:936`'s modlog_type.

    `:934` sets modlog_type to 'lock_post' or 'unlock_post' and `:944` passes
    it. Catches a regression hardcoding either string, which would record an
    unlock as a lock.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)

    lock_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    actions = {row.action for row in db.session.query(ModLog).all()}
    assert actions == {'lock_post'}


def test_unlocking_writes_the_unlock_action(db_session):
    """`:939`'s modlog_type on the false arm of `:934`.

    The counterpart of the test above. Together they prove `:944`'s argument is
    driven by `:934` rather than fixed.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)

    lock_post(s.post.id, False, SRC_API, auth=bearer(s.voter))

    actions = {row.action for row in db.session.query(ModLog).all()}
    assert actions == {'unlock_post'}


def test_a_site_admin_who_is_not_a_moderator_may_lock(db_session):
    """`:941`'s SECOND disjunct alone -- `community.is_admin_or_staff(user)`.

    `:941` is `is_moderator or community.is_admin_or_staff(user)`, one arc pair
    to coverage.py. This takes the second disjunct with the first false, which
    the moderator tests cannot do. Catches a regression dropping the admin
    disjunct.
    """
    s = seed_post_context(community_name='moderation')
    make_site_admin(s.voter)

    user_id, post = lock_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    db.session.refresh(s.post)
    assert s.post.comments_enabled is False


def test_an_unprivileged_user_changes_nothing(db_session):
    """`:941`'s false arm.

    Neither disjunct holds, so the body is skipped entirely. Asserts the field
    AND the empty ModLog, because `:958` returns `user.id, post` on this path
    exactly as it does on success -- the return proves nothing.
    """
    s = seed_post_context(community_name='moderation')
    s.post.comments_enabled = True
    db.session.commit()

    user_id, post = lock_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    db.session.refresh(s.post)
    assert s.post.comments_enabled is True
    assert db.session.query(ModLog).count() == 0


def test_a_banned_moderator_is_not_a_moderator(db_session):
    """`Community.moderators()`'s `is_banned == False` filter
    (app/models.py:716-722).

    A CommunityMember row with is_moderator=True and is_banned=True does NOT
    satisfy `:941`. Catches a regression dropping that filter, which would let
    a banned moderator keep moderating.
    """
    s = seed_post_context(community_name='moderation')
    member = seed_moderator(s)
    member.is_banned = True
    db.session.commit()
    s.post.comments_enabled = True
    db.session.commit()

    lock_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    db.session.refresh(s.post)
    assert s.post.comments_enabled is True


def test_the_web_arm_flashes_when_locking(db_session, app):
    """`:949`'s true arm and `:950`'s flash.

    `:948`'s `if locked:` picks the message and `:949` gates it on SRC_WEB.
    Asserts the flashed CONTENT, not merely that nothing raised -- a mutant
    deleting `:950` would otherwise survive. `get_flashed_messages` must be
    called INSIDE the request context and consumes the queue, so call it once.
    """
    from flask import get_flashed_messages

    s = seed_post_context(community_name='moderation')
    seed_moderator(s)

    with web_ctx(app, s.voter):
        result = lock_post(s.post.id, True, SRC_WEB)
        flashed = get_flashed_messages()

    assert result is None
    assert len(flashed) == 1
    assert 'locked' in flashed[0]


def test_the_web_arm_flashes_a_different_message_when_unlocking(db_session, app):
    """`:953`'s true arm and `:954`'s flash, distinct from `:950`'s.

    Catches a regression collapsing `:948`, which would flash 'locked' on an
    unlock. Compares the two messages rather than asserting one exists.
    """
    from flask import get_flashed_messages

    s = seed_post_context(community_name='moderation')
    seed_moderator(s)

    with web_ctx(app, s.voter):
        lock_post(s.post.id, False, SRC_WEB)
        flashed = get_flashed_messages()

    assert len(flashed) == 1
    assert 'unlocked' in flashed[0]


def test_the_api_arm_does_not_flash_when_locking(db_session):
    """`:949`'s false arm, on the lock path.

    Called with NO request context at all -- that absence is the oracle.
    `flash()` writes to `session`, and Flask's `session` proxy raises
    RuntimeError outside a request context. So if `:949`'s `if src ==
    SRC_WEB:` guard were deleted, this contextless `locked=True` SRC_API call
    would reach `:948`'s true branch, hit the now-unguarded `:950` flash, and
    raise instead of returning normally. Verified live: dropping the `:949`
    guard makes this exact call raise `RuntimeError: Working outside of
    request context.` This test says nothing about `:953` -- `locked=True`
    never reaches the `else` at `:952-955` that contains it.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)

    user_id, post = lock_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    assert user_id == s.voter.id


def test_the_api_arm_does_not_flash_when_unlocking(db_session):
    """`:953`'s false arm, on the unlock path.

    The counterpart of the test above, needed because `locked=False` is the
    only way to reach `:952-955` and exercise `:953`'s guard directly rather
    than relying on some other test to catch it incidentally. Same
    contextless oracle: verified live that dropping `:953`'s guard makes this
    exact call raise `RuntimeError: Working outside of request context.`
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)

    user_id, post = lock_post(s.post.id, False, SRC_API, auth=bearer(s.voter))

    assert user_id == s.voter.id


def test_a_moderator_moves_a_post_to_another_community(db_session):
    """`:969`'s first disjunct, `:973`'s move_to and `:974`'s commit.

    Asserts the post's community_id after a refresh, because `move_to` does not
    commit and an unrefreshed read would pass even if `:974` were deleted.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    target = make_community('target')

    user_id, post = move_post(s.post.id, target.id, SRC_API, auth=bearer(s.voter))

    assert user_id == s.voter.id
    db.session.refresh(s.post)
    assert s.post.community_id == target.id


def test_moving_records_the_target_community_in_the_modlog(db_session):
    """`:976-978`'s add_to_modlog, which passes `community=target_community`
    rather than the post's original community.

    Catches a regression passing `post.community`, which would file the entry
    against the community the post left.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    target = make_community('target')

    move_post(s.post.id, target.id, SRC_API, auth=bearer(s.voter))

    entries = db.session.query(ModLog).all()
    assert len(entries) == 1
    assert entries[0].action == 'move_post'
    assert entries[0].community_id == target.id


def test_an_instance_admin_may_move_a_post(db_session):
    """`:969`'s SECOND disjunct alone -- `community.is_instance_admin(user)`.

    This is the disjunct that distinguishes P2 from P1, and the only reason
    move_post and sticky_post admit an actor lock_post refuses. Catches a
    regression dropping it, which would silently narrow move_post to P1.
    """
    s = seed_post_context(community_name='moderation')
    target = make_community('target')
    make_instance_admin(s.voter, s.instance)

    move_post(s.post.id, target.id, SRC_API, auth=bearer(s.voter))

    db.session.refresh(s.post)
    assert s.post.community_id == target.id


def test_a_site_admin_may_move_a_post(db_session):
    """`:969`'s THIRD disjunct alone -- `user.is_admin_or_staff()`.

    Note this is the User method, where lock_post:941 reaches the same check
    through `Community.is_admin_or_staff(user)` (app/models.py:778-779), a pure
    delegating wrapper. Same effect, different spelling.
    """
    s = seed_post_context(community_name='moderation')
    target = make_community('target')
    make_site_admin(s.voter)

    move_post(s.post.id, target.id, SRC_API, auth=bearer(s.voter))

    db.session.refresh(s.post)
    assert s.post.community_id == target.id


def test_an_unprivileged_user_cannot_move_a_post(db_session):
    """`:969`'s false arm, all three disjuncts failing.

    Asserts the post did not move AND that no ModLog row exists, because
    `:987` returns `user.id, post` on this path exactly as on success.
    """
    s = seed_post_context(community_name='moderation')
    target = make_community('target')
    original = s.post.community_id

    user_id, post = move_post(s.post.id, target.id, SRC_API, auth=bearer(s.voter))

    db.session.refresh(s.post)
    assert s.post.community_id == original
    assert db.session.query(ModLog).count() == 0


def test_the_web_arm_flashes_that_the_post_moved(db_session, app):
    """`:980`'s true arm and `:981`'s flash.

    Asserts the flashed content, so a mutant deleting `:981` does not survive.
    """
    from flask import get_flashed_messages

    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    target = make_community('target')

    with web_ctx(app, s.voter):
        result = move_post(s.post.id, target.id, SRC_WEB)
        flashed = get_flashed_messages()

    assert result is None
    assert len(flashed) == 1
    assert 'moved' in flashed[0]


def test_a_moderator_stickies_a_post(db_session):
    """`:999`'s first disjunct alone (`is_moderator`) and `:1000`'s assignment.

    `s.voter` gets no other privilege here, so the true arm is reached
    through the moderator disjunct only. Catches a regression dropping the
    assignment at `:1000`, which would leave `post.sticky` unset.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)

    user_id, post = sticky_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    assert user_id == s.voter.id
    db.session.refresh(s.post)
    assert s.post.sticky is True


def test_unstickying_clears_the_flag_and_records_the_action(db_session):
    """`:1001`'s false arm, `:1004`'s modlog_type.

    Catches a regression collapsing `:1001`, which would file an unsticky as a
    feature.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    s.post.sticky = True
    db.session.commit()

    sticky_post(s.post.id, False, SRC_API, auth=bearer(s.voter))

    db.session.refresh(s.post)
    assert s.post.sticky is False
    actions = {row.action for row in db.session.query(ModLog).all()}
    assert actions == {'unfeatured_post'}


def test_stickying_backfills_the_communitys_featured_url(db_session):
    """`:1005`'s true arm and `:1006`'s assignment.

    Catches a regression dropping the backfill, which would leave a community
    with no ap_featured_url after its first sticky.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    s.community.ap_featured_url = None
    db.session.commit()

    sticky_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    db.session.refresh(s.community)
    assert s.community.ap_featured_url == s.community.ap_profile_id + '/featured'


def test_an_existing_featured_url_is_left_alone(db_session):
    """`:1005`'s false arm.

    Catches a regression making `:1006` unconditional, which would overwrite a
    community's real featured collection URL.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    s.community.ap_featured_url = 'https://elsewhere.example/c/x/featured'
    db.session.commit()

    sticky_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    db.session.refresh(s.community)
    assert s.community.ap_featured_url == 'https://elsewhere.example/c/x/featured'


def test_an_unprivileged_user_cannot_sticky_a_post(db_session):
    """`:999`'s false arm, all three disjuncts failing.

    Asserts the flag and the empty ModLog. `:1017` returns `user.id, post`
    unconditionally, so the return proves nothing.
    """
    s = seed_post_context(community_name='moderation')

    user_id, post = sticky_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    db.session.refresh(s.post)
    assert s.post.sticky is not True
    assert db.session.query(ModLog).count() == 0


def test_an_instance_admin_may_sticky_a_post(db_session):
    """`:999`'s second disjunct alone -- `community.is_instance_admin(user)`.

    This is the disjunct that distinguishes P2 from P1, and the only reason
    move_post and sticky_post admit an actor lock_post refuses.
    """
    s = seed_post_context(community_name='moderation')
    make_instance_admin(s.voter, s.instance)

    sticky_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    db.session.refresh(s.post)
    assert s.post.sticky is True


def test_a_site_admin_may_sticky_a_post(db_session):
    """`:999`'s THIRD disjunct alone -- `user.is_admin_or_staff()`.

    Task 5 witnessed this same disjunct for `move_post:969`'s copy of P2, but
    that is evidence about a different `if` statement. This test witnesses it
    independently for `sticky_post:999`, so Task 11's mutation pass has a
    witness that dies when `:999`'s third disjunct specifically is weakened.
    """
    s = seed_post_context(community_name='moderation')
    make_site_admin(s.voter)

    sticky_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    db.session.refresh(s.post)
    assert s.post.sticky is True
