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
from app.constants import NOTIF_POST, NOTIF_REPORT, NOTIF_REPORT_ESCALATION, SRC_API, SRC_WEB
from app.models import CommunityMember, ModLog, Notification, Post, hidden_posts
from app.shared.post import (
    hide_post,
    lock_post,
    mod_remove_post,
    mod_restore_post,
    move_post,
    sticky_post,
)
from tests.factories import bearer, make_community, make_community_member, \
    make_notification, make_post, make_user, seed_post_context, web_ctx


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


def test_an_unprivileged_user_does_not_federate_a_sticky(db_session):
    """PC1: `:1012`'s federation block now sits INSIDE `:999`'s permission gate.

    An unprivileged actor must not trigger task_selector at all -- if the
    gate is refused, no sticky ever happened locally, so nothing should be
    federated. Before PC1's fix, `:999` and `:1012` were at the same
    indentation, so task_selector fired whether or not the gate passed,
    letting an unauthorized user federate a sticky that never happened
    locally. app/community/routes.py:1109 reaches this with an ordinary post
    author on the create path, so this was not only a crafted-API-call risk.

    Counts task_selector calls by monkeypatching the module-level name, and
    restores in a finally: it is imported by other tests in the same session
    and a leaked patch corrupts every test that follows.
    """
    calls = []
    s = seed_post_context(community_name='moderation')

    import app.shared.post as post_module
    original = post_module.task_selector

    def counting_task_selector(task_key, **kwargs):
        calls.append(task_key)
        return original(task_key, **kwargs)

    post_module.task_selector = counting_task_selector
    try:
        sticky_post(s.post.id, True, SRC_API, auth=bearer(s.voter))
    finally:
        post_module.task_selector = original

    db.session.refresh(s.post)
    assert s.post.sticky is not True
    assert calls == [], 'a refused sticky was federated anyway'


def test_a_permitted_sticky_still_federates(db_session):
    """`:1013`'s task_selector on the permitted path, after PC1.

    The counterpart of the test above. Without this one, PC1 could be
    'fixed' by deleting the federation entirely and both tests would pass.
    """
    calls = []
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)

    import app.shared.post as post_module
    original = post_module.task_selector

    def counting_task_selector(task_key, **kwargs):
        calls.append(task_key)
        return original(task_key, **kwargs)

    post_module.task_selector = counting_task_selector
    try:
        sticky_post(s.post.id, True, SRC_API, auth=bearer(s.voter))
    finally:
        post_module.task_selector = original

    assert calls == ['sticky_post']


def test_a_permitted_unsticky_federates_the_undo(db_session):
    """`:1015`'s task_selector, the else arm of `:1012`, after PC1.

    Catches a regression hardcoding `:1013`'s task key, which would federate a
    sticky when the moderator unstickied.
    """
    calls = []
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    s.post.sticky = True
    db.session.commit()

    import app.shared.post as post_module
    original = post_module.task_selector

    def counting_task_selector(task_key, **kwargs):
        calls.append(task_key)
        return original(task_key, **kwargs)

    post_module.task_selector = counting_task_selector
    try:
        sticky_post(s.post.id, False, SRC_API, auth=bearer(s.voter))
    finally:
        post_module.task_selector = original

    assert calls == ['unsticky_post']


def test_a_moderator_removes_a_post(db_session):
    """`:1049`'s false arm (permission granted), `:1055`'s deleted flag,
    `:1056`'s deleted_by and `:1077`'s return.

    Note the gate is spelled negatively: `:1049` raises when the user is NOT
    permitted, so the permitted path is its FALSE arm.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)

    user_id, post = mod_remove_post(s.post.id, 'spam', SRC_API, bearer(s.voter))

    assert user_id == s.voter.id
    db.session.refresh(s.post)
    assert s.post.deleted is True
    assert s.post.deleted_by == s.voter.id


def test_removal_decrements_both_counters(db_session):
    """`:1057`'s author.post_count and `:1058`'s community.post_count.

    Catches a regression dropping either decrement, which the deleted flag
    alone would not reveal.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    s.post.author.post_count = 5
    s.community.post_count = 7
    db.session.commit()

    mod_remove_post(s.post.id, 'spam', SRC_API, bearer(s.voter))

    db.session.refresh(s.post.author)
    db.session.refresh(s.community)
    assert s.post.author.post_count == 4
    assert s.community.post_count == 6


def test_a_site_admin_who_is_not_a_moderator_may_remove_a_post(db_session):
    """`:1049`'s SECOND conjunct alone -- `not user.is_admin_or_staff()`.

    `:1049` is `not is_moderator and not is_admin_or_staff()`, one arc pair to
    coverage.py: the permitted path is the whole expression's FALSE arm.
    `test_a_moderator_removes_a_post` reaches that false arm through the
    FIRST conjunct alone (is_moderator true, short-circuiting before the
    second is even evaluated). This test reaches it through the SECOND
    conjunct with the first true -- a site admin who is NOT a moderator of
    the community. Catches a regression that drops the admin/staff disjunct
    and gates removal on moderator status alone, which the moderator test
    above cannot detect because it never exercises a non-moderator actor.

    Uses `make_site_admin(s.voter)`, not `s.author`: `s.author` is User id 1,
    and `User.is_admin()` returns True unconditionally for id 1, so a test
    built on it would pass through an accident of seeding order rather than
    through the role it claims to exercise.
    """
    s = seed_post_context(community_name='moderation')
    make_site_admin(s.voter)

    user_id, post = mod_remove_post(s.post.id, 'spam', SRC_API, bearer(s.voter))

    assert user_id == s.voter.id
    db.session.refresh(s.post)
    assert s.post.deleted is True


def test_an_unprivileged_user_is_refused_with_an_exception(db_session):
    """`:1049`'s true arm and `:1050`'s raise.

    Unlike lock_post, move_post and sticky_post, this function RAISES rather
    than returning as though it succeeded. Asserts the message AND that the
    post survived, because a bare pytest.raises(Exception) is satisfied by any
    exception including an unrelated crash.
    """
    s = seed_post_context(community_name='moderation')

    with pytest.raises(Exception, match='Does not have permission'):
        mod_remove_post(s.post.id, 'spam', SRC_API, bearer(s.voter))

    db.session.refresh(s.post)
    assert s.post.deleted is not True


def test_removal_writes_the_reason_to_the_modlog(db_session):
    """`:1061-1063`'s add_to_modlog with `reason=reason`.

    lock_post and move_post pass reason='' unconditionally; this function
    forwards the caller's. Catches a regression dropping it.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)

    mod_remove_post(s.post.id, 'breaks rule 3', SRC_API, bearer(s.voter))

    entries = db.session.query(ModLog).all()
    assert len(entries) == 1
    assert entries[0].action == 'delete_post'
    assert entries[0].reason == 'breaks rule 3'


def test_removal_deletes_ordinary_notifications_about_the_post(db_session):
    """`:1073`'s delete, reached through `:1071`'s false arm.

    Catches a regression inverting `:1071`, which would keep ordinary
    notifications and delete the report ones instead.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    make_notification(s.voter, s.post, notif_type=NOTIF_POST)

    mod_remove_post(s.post.id, 'spam', SRC_API, bearer(s.voter))

    assert db.session.query(Notification).count() == 0


def test_removal_keeps_report_notifications(db_session):
    """`:1071`'s true arm via its FIRST disjunct (NOTIF_REPORT) and `:1072`'s
    continue.

    A report notification must survive the removal it reported. Catches a
    regression dropping the continue, which would destroy the moderation trail.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    make_notification(s.voter, s.post, notif_type=NOTIF_REPORT)

    mod_remove_post(s.post.id, 'spam', SRC_API, bearer(s.voter))

    remaining = db.session.query(Notification).all()
    assert len(remaining) == 1
    assert remaining[0].notif_type == NOTIF_REPORT


def test_removal_keeps_escalated_report_notifications(db_session):
    """`:1071`'s SECOND disjunct -- NOTIF_REPORT_ESCALATION, with the first
    disjunct false.

    `:1071` is `notif_type == NOTIF_REPORT or notif_type ==
    NOTIF_REPORT_ESCALATION`, one arc pair to coverage.py. The test above takes
    the first disjunct; this takes the second with the first false.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    make_notification(s.voter, s.post, notif_type=NOTIF_REPORT_ESCALATION)

    mod_remove_post(s.post.id, 'spam', SRC_API, bearer(s.voter))

    remaining = db.session.query(Notification).all()
    assert len(remaining) == 1
    assert remaining[0].notif_type == NOTIF_REPORT_ESCALATION


def test_removal_with_no_notifications_takes_the_loops_zero_exit(db_session):
    """`:1069`'s zero-iteration exit arc.

    A post nobody was notified about still removes cleanly. Catches a
    regression assuming at least one row.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)

    mod_remove_post(s.post.id, 'spam', SRC_API, bearer(s.voter))

    db.session.refresh(s.post)
    assert s.post.deleted is True


def test_removing_a_post_with_a_url_recalculates_cross_posts(db_session):
    """`:1052`'s true arm, reached when the post has a url.

    `make_post` leaves `url` unset, so every other test in this file takes
    the false arm. Seeding one here exercises `:1053`'s
    calculate_cross_posts(delete_only=True) call. Asserts the removal still
    completes, since :1053 mutating self.cross_posts is the only other
    observable effect and this post has none seeded.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    s.post.url = 'https://example.com/article'
    db.session.commit()

    mod_remove_post(s.post.id, 'spam', SRC_API, bearer(s.voter))

    db.session.refresh(s.post)
    assert s.post.deleted is True


def test_the_web_arm_returns_none(db_session, app):
    """`:1076`'s false arm and `:1079`'s bare return.

    The web caller at app/post/routes.py:1170 discards the value. Catches a
    regression making `:1077`'s two-tuple unconditional, which would change the
    contract for a caller that unpacks nothing.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)

    with web_ctx(app, s.voter):
        result = mod_remove_post(s.post.id, 'spam', SRC_WEB, None)

    assert result is None
    db.session.refresh(s.post)
    assert s.post.deleted is True


def test_a_moderator_restores_a_removed_post(db_session):
    """`:1091`'s false arm (permission granted) reached through the FIRST
    conjunct alone, `:1097`'s deleted flag and `:1098`'s deleted_by clear.

    Mirrors `test_a_moderator_removes_a_post`: the gate is spelled
    negatively, so the permitted path is the whole expression's FALSE arm,
    and a moderator makes `not is_moderator` False -- short-circuiting
    before `not user.is_admin_or_staff()` is even evaluated.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    s.post.deleted = True
    s.post.deleted_by = s.voter.id
    db.session.commit()

    user_id, post = mod_restore_post(s.post.id, 'appealed', SRC_API, bearer(s.voter))

    assert user_id == s.voter.id
    db.session.refresh(s.post)
    assert s.post.deleted is False
    assert s.post.deleted_by is None


def test_restoration_increments_both_counters(db_session):
    """`:1099`'s author.post_count and `:1100`'s community.post_count.

    The mirror of mod_remove_post's decrements. Catches a regression dropping
    either, which would leave the counters drifting after a remove/restore
    cycle.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    s.post.author.post_count = 4
    s.community.post_count = 6
    db.session.commit()

    mod_restore_post(s.post.id, 'appealed', SRC_API, bearer(s.voter))

    db.session.refresh(s.post.author)
    db.session.refresh(s.community)
    assert s.post.author.post_count == 5
    assert s.community.post_count == 7


def test_a_site_admin_who_is_not_a_moderator_may_restore_a_post(db_session):
    """`:1091`'s SECOND conjunct alone -- `not user.is_admin_or_staff()`.

    `:1091` is `not is_moderator and not is_admin_or_staff()`, one arc pair to
    coverage.py: the permitted path is the whole expression's FALSE arm.
    `test_a_moderator_restores_a_removed_post` reaches that false arm through
    the FIRST conjunct alone (is_moderator true, short-circuiting before the
    second is even evaluated). This test reaches it through the SECOND
    conjunct with the first true -- a site admin who is NOT a moderator of
    the community. Catches a regression that drops the admin/staff disjunct
    and gates restoration on moderator status alone, which the moderator
    test above cannot detect because it never exercises a non-moderator
    actor.

    Uses `make_site_admin(s.voter)`, not `s.author`: `s.author` is User id 1,
    and `User.is_admin()` returns True unconditionally for id 1, so a test
    built on it would pass through an accident of seeding order rather than
    through the role it claims to exercise.
    """
    s = seed_post_context(community_name='moderation')
    make_site_admin(s.voter)
    s.post.deleted = True
    s.post.deleted_by = s.voter.id
    db.session.commit()

    user_id, post = mod_restore_post(s.post.id, 'appealed', SRC_API, bearer(s.voter))

    assert user_id == s.voter.id
    db.session.refresh(s.post)
    assert s.post.deleted is False


def test_an_unprivileged_user_cannot_restore_a_post(db_session):
    """`:1091`'s true arm and `:1092`'s raise.

    Asserts the message and that the post stayed deleted, because a bare
    pytest.raises(Exception) is satisfied by any exception including an
    unrelated crash from the redis lock or the module-body import at
    `:1088-1089`.
    """
    s = seed_post_context(community_name='moderation')
    s.post.deleted = True
    db.session.commit()

    with pytest.raises(Exception, match='Does not have permission'):
        mod_restore_post(s.post.id, 'appealed', SRC_API, bearer(s.voter))

    db.session.refresh(s.post)
    assert s.post.deleted is True


def test_restoration_writes_the_reason_to_the_modlog(db_session):
    """`:1103-1105`'s add_to_modlog with action 'restore_post'."""
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)

    mod_restore_post(s.post.id, 'appealed on review', SRC_API, bearer(s.voter))

    entries = db.session.query(ModLog).all()
    assert len(entries) == 1
    assert entries[0].action == 'restore_post'
    assert entries[0].reason == 'appealed on review'


def test_restoring_a_post_with_a_url_recalculates_cross_posts(db_session):
    """`:1094`'s true arm, reached when the post has a url.

    `make_post` leaves `url` unset, so every other test in this file takes
    the false arm. Seeding one here exercises `:1095`'s
    calculate_cross_posts() call -- unlike mod_remove_post's :1053, this arm
    passes no delete_only argument. Asserts the restoration still completes,
    since :1095 mutating self.cross_posts is the only other observable
    effect and this post has none seeded.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    s.post.url = 'https://example.com/article'
    s.post.deleted = True
    db.session.commit()

    mod_restore_post(s.post.id, 'appealed', SRC_API, bearer(s.voter))

    db.session.refresh(s.post)
    assert s.post.deleted is False


def test_the_web_arm_returns_none_when_restoring(db_session, app):
    """`:1109`'s false arm and `:1112`'s bare return.

    Catches a regression making `:1110`'s two-tuple unconditional, which
    would change the contract for a caller that unpacks nothing. Named
    distinctly from mod_remove_post's `test_the_web_arm_returns_none` --
    two module-level functions sharing a name silently shadow each other,
    dropping the earlier one from collection entirely.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    s.post.deleted = True
    db.session.commit()

    with web_ctx(app, s.voter):
        result = mod_restore_post(s.post.id, 'appealed', SRC_WEB, None)

    assert result is None
    db.session.refresh(s.post)
    assert s.post.deleted is False
