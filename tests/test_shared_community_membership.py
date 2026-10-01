"""Six functions of app/shared/community.py's membership surface:
`join_community` (:32), `leave_community` (:57), `block_community` (:85),
`unblock_community` (:103), `subscribe_community` (:394) and
`favorite_community` (:441).

`block_community` and `unblock_community` were this file's original scope --
both at 0.0% before it, 22 statements and 12 arcs missing, the fifth instance
of a shape this campaign has closed four times before (block_another_user,
block_domain, block_remote_instance, and this one). The src fork, bearer,
web_ctx, the id-1 burn, and the deliberate bystander-first seeding order all
transfer from tests/test_shared_domain.py. `join_community`, `leave_community`,
`subscribe_community` and `favorite_community` were added later in the same
round; the file now carries 36 tests over all six functions.

HISTORY WORTH KEEPING RATHER THAN ERASING:

`leave_community:60` carries this round's one pinned-then-inverted guard
(D596). It read `if not cm.is_owner or not cm.is_moderator:` -- by De Morgan,
`not (cm.is_owner and cm.is_moderator)` -- so the free-leave branch ran unless
a member was BOTH owner and moderator, letting a plain moderator
(is_moderator=True, is_owner=False, the shape federated moderators are
created in) leave freely instead of being refused at :75.
`test_leave_community_moderator_without_owner_is_refused` (:351) was pinned
against that defect under its old name,
`test_leave_community_moderator_without_owner_leaves_freely`, then Task 5
fixed :60 to `and` and the test was inverted and renamed to match; a reader
searching for the old name in the tree will not find it, and that is
intentional. (The round's OTHER pinned-then-inverted guard, D595's
`app/shared/site.py:19-20` `return`, belongs to `tests/test_shared_site.py`,
not to this file.)

The two SRC_WEB tests for `subscribe_community` (:543, :573, at the time of
this note) originally passed `None` as the `subscribe` argument and a
docstring claimed that proved the :398-399 override drove the outcome. It did
not: `None == False` is `False`, so `None` takes the same arm as `True` under
both the override and its deletion, and deleting :398-399 left both tests
green. Both call sites now pass `False`, which does distinguish the two
cases, and the docstrings say only what the algebra supports. Two further
tests (:607, :820 at the time of this note) exercise the OFF half of
:398-399's and :445-446's ternaries, which no earlier test in this file
reached.
"""
from types import SimpleNamespace

import pytest
from flask import current_app, get_flashed_messages
from sqlalchemy.exc import NoResultFound

from app import db
from app.constants import NOTIF_COMMUNITY, SRC_API, SRC_PLD, SRC_WEB
from app.models import CommunityBan, CommunityBlock, CommunityFavorite, CommunityMember, NotificationSubscription
from app.shared.community import (block_community, favorite_community, join_community,
                                  leave_community, subscribe_community, unblock_community)
from tests.factories import (ban_user_from_community, bearer, make_community, make_community_member,
                             make_instance, make_site, make_user, web_ctx)


def _seed_member():
    """A local user who is not id 1, and a Community.

    ORDER MATTERS. make_community (tests/factories.py:124) hardcodes
    instance_id=1 and user_id=1, so an Instance and a User must already
    occupy id 1 when it is called. The instance and the id-1 burn below fill
    both seats, which is why the burn is load-bearing twice over: it keeps
    `user` off the id-1 admin shortcut (app/models.py:1259-1261) AND gives
    the community an owner that exists.
    """
    instance = make_instance('test.piefed.local', software='piefed')
    burn = make_user(instance, 'burn-the-id-1-seat', local=True)
    assert burn.id == 1, 'the id-1 admin trap moved; re-derive before trusting this'

    user = make_user(instance, 'member', local=True)
    community = make_community('microblogs')
    db.session.commit()
    return SimpleNamespace(user=user, community=community, instance=instance)


def test_block_community_api_creates_the_block_and_returns_the_user(app, db_session):
    s = _seed_member()

    returned = block_community(s.community.id, SRC_API, bearer(s.user))

    assert returned == s.user.id
    assert db.session.query(CommunityBlock).filter_by(
        community_id=s.community.id, user_id=s.user.id).count() == 1


def test_block_community_web_creates_the_block_and_returns_none(app, db_session):
    """:100 returns None and leaves the flash to its caller.

    Both the return value and the row are asserted: a function that created
    nothing would also return None.
    """
    s = _seed_member()

    with web_ctx(app, s.user):
        returned = block_community(s.community.id, SRC_WEB)

    assert returned is None
    assert db.session.query(CommunityBlock).filter_by(
        community_id=s.community.id, user_id=s.user.id).count() == 1


def test_block_community_is_idempotent(app, db_session):
    """:92's `if not existing` -- the false arm.

    The second call must not add a second row and must still return the
    user's id from :98 rather than falling out early.
    """
    s = _seed_member()
    block_community(s.community.id, SRC_API, bearer(s.user))

    returned = block_community(s.community.id, SRC_API, bearer(s.user))

    assert returned == s.user.id
    assert db.session.query(CommunityBlock).count() == 1


def test_block_community_blocks_only_for_the_calling_user(app, db_session):
    """:91's filter is on BOTH community_id and user_id.

    A second user's block of the same community is seeded so that a lookup
    narrowed to community_id alone would find it, conclude a block already
    exists, and skip :93-95. With one block row that fault is invisible.
    """
    s = _seed_member()
    bystander = make_user(make_instance('other.example'), 'bystander', local=True)
    db.session.add(CommunityBlock(community_id=s.community.id, user_id=bystander.id))
    db.session.commit()

    block_community(s.community.id, SRC_API, bearer(s.user))
    db.session.expire_all()

    owners = {b.user_id for b in db.session.query(CommunityBlock).all()}
    assert owners == {s.user.id, bystander.id}


def test_unblock_community_api_removes_the_block(app, db_session):
    s = _seed_member()
    db.session.add(CommunityBlock(community_id=s.community.id, user_id=s.user.id))
    db.session.commit()

    returned = unblock_community(s.community.id, SRC_API, bearer(s.user))

    assert returned == s.user.id
    assert db.session.query(CommunityBlock).count() == 0


def test_unblock_community_web_removes_the_block_and_returns_none(app, db_session):
    s = _seed_member()
    db.session.add(CommunityBlock(community_id=s.community.id, user_id=s.user.id))
    db.session.commit()

    with web_ctx(app, s.user):
        returned = unblock_community(s.community.id, SRC_WEB)

    assert returned is None
    assert db.session.query(CommunityBlock).count() == 0


def test_unblock_community_leaves_another_users_block_alone(app, db_session):
    """:109's filter is on BOTH columns, in the delete direction.

    The bystander's row is seeded first deliberately, so that a mutant
    narrowing the filter to community_id alone would call .first() and find
    the bystander's row (inserted first in the heap), then incorrectly
    delete it. Reordering this deliberately to put user first would
    silently defeat the test, making the mutant survive.
    """
    s = _seed_member()
    bystander = make_user(make_instance('other.example'), 'bystander', local=True)
    db.session.add(CommunityBlock(community_id=s.community.id, user_id=bystander.id))
    db.session.add(CommunityBlock(community_id=s.community.id, user_id=s.user.id))
    db.session.commit()

    unblock_community(s.community.id, SRC_API, bearer(s.user))
    db.session.expire_all()

    owners = {b.user_id for b in db.session.query(CommunityBlock).all()}
    assert owners == {bystander.id}


def test_unblock_community_is_idempotent(app, db_session):
    """:110's `if existing_block` -- the false arm. No block exists, so the
    body is skipped and :116 still returns the caller's id."""
    s = _seed_member()

    returned = unblock_community(s.community.id, SRC_API, bearer(s.user))

    assert returned == s.user.id
    assert db.session.query(CommunityBlock).count() == 0


# `join_community` and `leave_community` (app/shared/community.py:32-82).
#
# Both are unexercised before this file: nothing under tests/ calls
# app.shared.community.join_community or .leave_community. The names
# `join_community` and `leave_community` DO appear throughout
# tests/test_shared_tasks_follows.py, but those calls are into
# app/shared/tasks/follows.py's Celery tasks of the same name -- a different
# module entirely, reached only through task_selector. That file's coverage
# does not touch this one.
#
# task_selector is bound into this module's globals by :20's
# `from app.shared.tasks import task_selector`, so every test below rebinds
# `app.shared.community.task_selector` rather than `app.shared.tasks.task_selector`
# -- patching the latter would not intercept the call this module makes.


def test_join_community_api_send_async_true_inserts_member_and_returns_user_id(
        app, db_session, monkeypatch):
    """`:36`'s `not (current_app.debug or src == SRC_WEB)` with debug False
    (the test app's default, TestConfig does not set DEBUG) and src=SRC_API
    (not SRC_WEB) makes send_async True, so `:40`'s `if send_async or
    sync_retval is True:` is satisfied through its FIRST operand regardless
    of what task_selector returns. The double returns False to prove that.
    """
    s = _seed_member()
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: False)

    returned = join_community(s.community.id, SRC_API, bearer(s.user))

    assert returned == s.user.id
    assert db.session.query(CommunityMember).filter_by(
        user_id=s.user.id, community_id=s.community.id).count() == 1


def test_join_community_web_send_async_false_sync_retval_not_true_skips_insert(
        app, db_session, monkeypatch):
    """`:36`'s `src == SRC_WEB` forces send_async False regardless of debug,
    and the double's return (False) fails `:40`'s second operand too, so
    NEITHER operand of `:40`'s `or` is satisfied and the insert at
    `:41-46` never runs. `:48`/`:50` both miss (not API, not PLD), so `:52`'s
    bare `return` fires and the caller gets None.
    """
    s = _seed_member()
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: False)

    with web_ctx(app, s.user):
        returned = join_community(s.community.id, SRC_WEB, user_id=s.user.id)

    assert returned is None
    assert db.session.query(CommunityMember).filter_by(
        user_id=s.user.id, community_id=s.community.id).count() == 0


def test_join_community_web_sync_retval_true_inserts_despite_send_async_false(
        app, db_session, monkeypatch):
    """`:40`'s SECOND operand, `sync_retval is True`, taken in isolation:
    send_async is False (SRC_WEB), but the double returns True, so the
    insert at `:41-46` still runs. Distinguishes this from the previous test
    where the same send_async-False setup skipped the insert because the
    double returned False instead.
    """
    s = _seed_member()
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: True)

    with web_ctx(app, s.user):
        returned = join_community(s.community.id, SRC_WEB, user_id=s.user.id)

    assert returned is None
    assert db.session.query(CommunityMember).filter_by(
        user_id=s.user.id, community_id=s.community.id).count() == 1


def test_join_community_debug_true_forces_send_async_false_and_skips_insert(
        app, db_session, monkeypatch):
    """`:36`'s FIRST operand, `current_app.debug`, taken in isolation from
    `src == SRC_WEB`: src is SRC_API (not SRC_WEB) but debug is forced True,
    so send_async is still False. Combined with a double returning False
    (failing `:40`'s second operand too), the insert is skipped -- proving
    `current_app.debug` alone, not just the SRC_WEB check, drives `:36`.
    Restores the app's debug flag afterward since `app` is session-scoped.
    """
    s = _seed_member()
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: False)
    original_debug = current_app.config['DEBUG']
    current_app.config['DEBUG'] = True
    try:
        returned = join_community(s.community.id, SRC_API, bearer(s.user))
    finally:
        current_app.config['DEBUG'] = original_debug

    assert returned == s.user.id
    assert db.session.query(CommunityMember).filter_by(
        user_id=s.user.id, community_id=s.community.id).count() == 0


def test_join_community_pld_returns_sync_retval_verbatim(app, db_session, monkeypatch):
    """`:50`'s `elif src == SRC_PLD: return sync_retval` -- the THIRD return
    value no sibling function has. SRC_API returns the user id and every
    other function's WEB arm returns None, but this arm hands back whatever
    task_selector produced, unchanged. A dict (rather than True/False) proves
    it is not coerced to a bool on the way out.
    """
    s = _seed_member()
    sentinel = {'community_id': s.community.id}
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: sentinel)

    returned = join_community(s.community.id, SRC_PLD, user_id=s.user.id)

    assert returned is sentinel


def test_join_community_is_idempotent_for_an_existing_member(app, db_session, monkeypatch):
    """`:43`'s `if not existing_member:` -- the false arm. A second call for
    an already-joined user must not add a second row.
    """
    s = _seed_member()
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: False)
    join_community(s.community.id, SRC_API, bearer(s.user))

    returned = join_community(s.community.id, SRC_API, bearer(s.user))

    assert returned == s.user.id
    assert db.session.query(CommunityMember).filter_by(
        user_id=s.user.id, community_id=s.community.id).count() == 1


def test_leave_community_missing_member_raises_NoResultFound_where_siblings_use_first(
        app, db_session, monkeypatch):
    """`:59`'s `.filter_by(...).one()` raises `NoResultFound` for a caller who
    is not a member -- registering the divergence from join_community's
    `:41-42`, which uses `.first()` on the identical filter and would get
    None back rather than an exception.
    """
    s = _seed_member()
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: None)

    with pytest.raises(NoResultFound):
        leave_community(s.community.id, SRC_API, bearer(s.user))


def test_leave_community_neither_owner_nor_moderator_leaves_successfully(
        app, db_session, monkeypatch):
    """CONTROL: a plain member (is_owner=False, is_moderator=False) leaves
    freely through `:60`'s true arm. Must keep passing after Task 5 fixes
    `:60`'s De Morgan inversion, since neither role is held either way.
    """
    s = _seed_member()
    make_community_member(s.user, s.community, is_moderator=False)
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: None)

    returned = leave_community(s.community.id, SRC_API, bearer(s.user))

    assert returned == s.user.id
    assert db.session.query(CommunityMember).filter_by(
        user_id=s.user.id, community_id=s.community.id).count() == 0


def test_leave_community_moderator_without_owner_is_refused(app, db_session, monkeypatch):
    """`:60` used to read `if not cm.is_owner or not cm.is_moderator:`, which
    by De Morgan is `not (cm.is_owner and cm.is_moderator)` -- the leave path
    ran unless the member was BOTH owner and moderator. A plain moderator
    (is_moderator=True, is_owner=False, the shape federated moderators are
    created in -- app/activitypub/util.py:959) therefore left freely instead
    of being told to step down first at `:75`. That was a pinned defect;
    Task 5 fixed `:60` to gate on EITHER role and inverted this test to
    match.

    Now a plain moderator hits `:60`'s false arm and is refused, same as the
    owner-and-moderator control. The row must survive the refusal: a raise
    alone would also be satisfied by a mutant that raised after deleting it.
    """
    s = _seed_member()
    make_community_member(s.user, s.community, is_moderator=True)
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: None)

    with pytest.raises(Exception, match='Step down as a moderator before leaving the community'):
        leave_community(s.community.id, SRC_API, bearer(s.user))

    assert db.session.query(CommunityMember).filter_by(
        user_id=s.user.id, community_id=s.community.id).count() == 1


def test_leave_community_owner_and_moderator_is_refused_via_api(app, db_session, monkeypatch):
    """CONTROL: a member who is BOTH owner and moderator hits `:60`'s false
    arm and `:72`'s SRC_API branch, which raises rather than leaving. Already
    correct today, and remains correct after Task 5's fix. The row must
    survive the refusal.
    """
    s = _seed_member()
    member = make_community_member(s.user, s.community, is_moderator=True)
    member.is_owner = True
    db.session.commit()
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: None)

    with pytest.raises(Exception, match='Step down as a moderator before leaving the community'):
        leave_community(s.community.id, SRC_API, bearer(s.user))

    assert db.session.query(CommunityMember).filter_by(
        user_id=s.user.id, community_id=s.community.id).count() == 1


def test_leave_community_owner_and_moderator_is_refused_via_web_and_flashes(
        app, db_session, monkeypatch):
    """CONTROL, web twin of the previous test: `:60`'s false arm reached via
    the web src instead flashes at `:75` and returns None rather than
    raising. The flash content is asserted, not just the return value, so a
    regression that dropped `:75`'s message but kept the early `return`
    would not slip past silently. The row must survive.
    """
    s = _seed_member()
    member = make_community_member(s.user, s.community, is_moderator=True)
    member.is_owner = True
    db.session.commit()
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: None)

    with web_ctx(app, s.user):
        returned = leave_community(s.community.id, SRC_WEB)
        flashed = get_flashed_messages()

    assert returned is None
    assert len(flashed) == 1
    assert 'step down as moderator' in flashed[0]
    assert db.session.query(CommunityMember).filter_by(
        user_id=s.user.id, community_id=s.community.id).count() == 1


def test_leave_community_web_flashes_when_not_bulk_leave(app, db_session, monkeypatch):
    """`:68`'s `if src == SRC_WEB and not bulk_leave:` -- both operands True.
    Default bulk_leave=False, so the successful web leave flashes.
    """
    s = _seed_member()
    make_community_member(s.user, s.community, is_moderator=False)
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: None)

    with web_ctx(app, s.user):
        returned = leave_community(s.community.id, SRC_WEB)
        flashed = get_flashed_messages()

    assert returned is None
    assert len(flashed) == 1
    assert 'left the community' in flashed[0]
    assert db.session.query(CommunityMember).filter_by(
        user_id=s.user.id, community_id=s.community.id).count() == 0


def test_leave_community_web_bulk_leave_skips_the_flash(app, db_session, monkeypatch):
    """`:68`'s SECOND operand, `not bulk_leave`, taken in isolation: src is
    still SRC_WEB, but bulk_leave=True flips `not bulk_leave` to False, so
    the `and` is not satisfied and `:69`'s flash never fires -- even though
    the leave itself still succeeds.
    """
    s = _seed_member()
    make_community_member(s.user, s.community, is_moderator=False)
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: None)

    with web_ctx(app, s.user):
        returned = leave_community(s.community.id, SRC_WEB, bulk_leave=True)
        flashed = get_flashed_messages()

    assert returned is None
    assert flashed == []
    assert db.session.query(CommunityMember).filter_by(
        user_id=s.user.id, community_id=s.community.id).count() == 0


def test_leave_community_a_third_source_reaches_no_flash_the_web_arm_cannot_isolate(
        app, db_session, monkeypatch):
    """`:68`'s FIRST operand, `src == SRC_WEB`, taken in isolation from
    `not bulk_leave`.

    The previous two tests both fix src=SRC_WEB, so neither can tell `:68`
    apart from a mutant that deletes the `src` operand entirely (`if not
    bulk_leave:`): both tests already have `src == SRC_WEB` true, so nothing
    about them would change. And the SRC_API tests elsewhere in this file
    can't tell them apart either, in the other direction -- they run with no
    request context, so under that mutant `flash()` would raise
    `RuntimeError: Working outside of request context` rather than fail an
    assertion, and a crash kill is not a kill unless a viable non-crashing
    variant of the same fault also dies. No test combines a non-WEB src with
    bulk_leave=False inside a request context except this one.

    SRC_PLD (app/constants.py:94, the admin preload path) is used here purely
    as a third source value that is neither SRC_WEB nor SRC_API, to isolate
    `:68`'s first operand; it is not how leave_community is called in
    production, and this docstring says so rather than implying otherwise.
    It works because `:58` is `authorise_api_user(auth) if src == SRC_API
    else current_user.id` -- SRC_PLD takes the current_user.id branch, so it
    still needs the logged-in request context `web_ctx` provides, while
    `:68`'s `src == SRC_WEB` is False, so no flash should occur. Under the
    `if not bulk_leave:` mutant, the flash fires anyway and this assertion
    fails -- a genuine, non-crashing kill.
    """
    s = _seed_member()
    make_community_member(s.user, s.community, is_moderator=False)
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: None)

    with web_ctx(app, s.user):
        returned = leave_community(s.community.id, SRC_PLD, bulk_leave=False)
        flashed = get_flashed_messages()

    assert returned is None
    assert flashed == []
    assert db.session.query(CommunityMember).filter_by(
        user_id=s.user.id, community_id=s.community.id).count() == 0


# `subscribe_community` and `favorite_community` (app/shared/community.py:394-484).
#
# `subscribe_community` already has 24 of 29 statements covered before this
# file: tests/test_api_community_subscriptions.py's `put_community_subscribe`
# calls reach it entirely through SRC_API, exercising :395's banned-community
# `.one()`, :401-402's lookup, both :403 arms, :409's raise, both :414 arms,
# :417's raise, :422's banned-from check and both its arms, :428-433's insert,
# and :435's SRC_API return. What that oracle CANNOT reach: :399 (the SRC_WEB
# override -- it never passes SRC_WEB), :427 (the SRC_WEB banned-from flash,
# same reason), :438 (the SRC_WEB return, same reason), and :412/:420 (the two
# flash arms below, which no SRC_WEB call can reach either -- see below).
# favorite_community has no oracle anywhere under tests/ --
# `/usr/bin/grep -rln "favorite_community" tests/` matches nothing outside
# this file before it -- so every statement of it below is new.
#
# `:399`'s override and `:401`'s existing_notification lookup run the identical
# filter (entity_id=community_id, user_id, type=NOTIF_COMMUNITY), so under
# SRC_WEB `subscribe == False` exactly when a matching row exists: :412 needs
# `subscribe == False` AND no row, :420 needs `subscribe == True` AND a row --
# both contradictions under that lockstep. SRC_API cannot reach them either:
# :409 and :417 always take the raise instead of the else. Neither production
# caller reaches :412/:420 -- app/community/routes.py:1720 passes SRC_WEB,
# app/api/alpha/utils/community.py:270 passes SRC_API -- but production-
# unreachability is not test-unreachability. Sub-project 43 drew that
# equivalence for two lines of this exact shape and the ruling was retracted
# as D564. `favorite_community:441-484` repeats the identical shape at
# :458/:466, gated by :445-446's override against :448's lookup. SRC_PLD
# (app/constants.py:94) is neither SRC_WEB nor SRC_API, so :398/:445 skip the
# override and the caller's `subscribe` argument survives, and :409/:417 and
# :455/:463 take their else arms instead of raising. The precedent, down to
# the constant, is tests/test_shared_post_interactions.py:577 (read in full
# before writing the tests below), tests/test_shared_reply_interactions.py:1018,
# and sub-project 43's D564 retraction itself (the user twin).
#
# ASYMMETRY, registered per this round's scope rather than fixed: :479's
# `cache.delete_memoized(favorite_communities, user_id)` runs unconditionally
# on every arm of favorite_community that reaches it (both raise arms above it
# skip it by unwinding first, same as any exception would). subscribe_community
# has no equivalent call anywhere, on any arm, including :429-433's insert,
# which is exactly the kind of write a memoized reader would need invalidated.
# The two "identical shape" functions are not identical here.


def test_subscribe_community_web_creates_via_override_and_returns_the_render(app, db_session):
    """:398's true arm and :399's override, then the ordinary create path
    (:414's false arm, :422's false arm, :428-433) and :435's false arm
    landing on :438's render.

    `community.notify_new_posts(user_id)` is False for a user with no
    NotificationSubscription row, so :399 sets `subscribe = True` --
    overriding the `False` this test passes in as the `subscribe` argument.
    That is a genuine kill of the "delete :398-399" mutant: `False == False`
    takes :403's true arm, whose :404 `if existing_notification:` is empty,
    so :407-412 flash and nothing is created -- the `count() == 1` assertion
    below would fail. Passing `None` instead would NOT prove this, because
    `None == False` is also False, so `None` takes the same arm as `True`
    under both the override and its deletion; see tests/README.md's fact on
    this (added alongside this fix) for the general shape.
    `make_site()` is required: :438's render calls `current_theme()`
    (app/utils.py:3228), which falls back to `Site.query.get(1)` and raises
    AttributeError on a None site with no Site row present.
    """
    make_site()
    s = _seed_member()

    with web_ctx(app, s.user):
        result = subscribe_community(s.community.id, False, SRC_WEB)

    assert result.status_code == 200
    assert db.session.query(NotificationSubscription).filter_by(
        entity_id=s.community.id, user_id=s.user.id, type=NOTIF_COMMUNITY).count() == 1


def test_subscribe_community_web_banned_user_flashes_and_creates_nothing(app, db_session):
    """:422's true arm reached via SRC_WEB, and :427's flash -- NOT one of the
    two lockstep-unreachable arms: this branch is gated on
    `communities_banned_from`, not on the :398/:401 lockstep, so a plain
    SRC_WEB call reaches it with no third source value needed.

    The row must NOT exist afterward: a mutant that flashed but inserted
    anyway would pass a flash-only assertion.

    `False` is passed as the `subscribe` argument (not `None`) for the same
    reason as the test above: with :398-399 present, `notify_new_posts` is
    False for this banned user with no notification row, so the override
    sets `subscribe = True` regardless of the argument, and control reaches
    the banned check at :422 either way. With :398-399 DELETED, the literal
    `False` argument survives and takes :403's true arm instead, producing
    the "did not exist" flash rather than the banned one -- a genuine kill
    that `None` (which also survives :398-399's deletion but happens to take
    the same non-False arm here) would not have been.
    """
    make_site()
    s = _seed_member()
    ban_user_from_community(s.user, s.community)

    with web_ctx(app, s.user):
        result = subscribe_community(s.community.id, False, SRC_WEB)
        flashed = get_flashed_messages()

    assert result.status_code == 200
    assert len(flashed) == 1
    assert 'banned from this community' in flashed[0]
    assert db.session.query(NotificationSubscription).filter_by(
        entity_id=s.community.id, user_id=s.user.id).count() == 0


def test_subscribe_community_web_override_off_removes_an_existing_subscription(app, db_session):
    """The OFF half of :399's ternary, never exercised by the two web tests
    above: both seed a user with NO NotificationSubscription row, so
    `community.notify_new_posts(user_id)` is always False there and the
    override always computes `True`. `coverage.py` does not branch-measure
    a conditional expression, so `[]`/`[]` on this function was consistent
    with that half never running at all.

    Here a NotificationSubscription row is seeded FIRST, making
    `notify_new_posts(user_id)` True, so :399 computes `subscribe = False`
    -- overriding the `True` passed in as the argument. :403's true arm is
    then taken, :404's `if existing_notification:` is True, and :405-406
    delete the seeded row. The row's absence afterward is the assertion:
    a mutant that skipped the override (leaving `subscribe = True`) would
    instead reach :414's else-branch, find `existing_notification` truthy,
    flash "already existed", and leave the row in place.
    """
    make_site()
    s = _seed_member()
    db.session.add(NotificationSubscription(
        name='pre-existing', user_id=s.user.id, entity_id=s.community.id, type=NOTIF_COMMUNITY))
    db.session.commit()

    with web_ctx(app, s.user):
        result = subscribe_community(s.community.id, True, SRC_WEB)

    assert result.status_code == 200
    assert db.session.query(NotificationSubscription).filter_by(
        entity_id=s.community.id, user_id=s.user.id, type=NOTIF_COMMUNITY).count() == 0


def test_subscribe_community_a_third_source_reaches_the_flash_branches_the_web_arm_cannot(
        app, db_session):
    """:403's false arm and :412's flash, then :414's true arm, :417's false
    arm and :420's flash.

    NEITHER SRC_WEB NOR SRC_API CAN REACH :412/:420 -- see this section's
    header comment for the full derivation; in short, SRC_WEB keeps
    `subscribe` and `existing_notification` in lockstep via the identical
    filter :399 and :401 both use, so the "mismatched" arms these two flashes
    sit behind are jointly unreachable there, and SRC_API's :409/:417 always
    take the raise instead. A third source value is the only way in: SRC_PLD
    (app/constants.py:94, the admin preload path) is used here PURELY as such
    a value to reach these two statements. IT IS NOT HOW SUBSCRIBE_COMMUNITY
    IS CALLED IN PRODUCTION -- app/community/routes.py:1720 passes SRC_WEB and
    app/api/alpha/utils/community.py:270 passes SRC_API, and those are the
    only two callers -- and this docstring says so rather than implying
    otherwise. The precedent, down to the constant, is
    tests/test_shared_post_interactions.py:577-639 against the twin
    `subscribe_post`.

    `web_ctx` is used even though this is not an SRC_WEB call, because :396's
    else-arm reads `current_user.id` for any non-SRC_API source, and :438
    renders for any non-SRC_API source too; `make_site()` is there for that
    render, same as the two tests above.

    THE ASSERTIONS ARE ON `flashed`'s CONTENT, NOT ON `result`, and that is
    load-bearing: under SRC_PLD, :435's `if src == SRC_API:` is always False,
    so control reaches :438's render whether or not the flash call is there --
    deleting `flash(_(msg))` outright would still return a normal render and
    pass a result-only assertion silently. The row counts are the second half:
    0 after the first call and 1 after the second, separating "refused and
    flashed" from "flashed and then also wrote", which is what reaching :412
    or :420 from the wrong outer arm would look like.
    """
    make_site()
    s = _seed_member()

    with web_ctx(app, s.user):
        result = subscribe_community(s.community.id, False, SRC_PLD)
        flashed = get_flashed_messages()

    assert result.status_code == 200
    assert len(flashed) == 1
    assert 'did not exist' in flashed[0]
    assert db.session.query(NotificationSubscription).filter_by(
        entity_id=s.community.id, user_id=s.user.id).count() == 0

    subscribe_community(s.community.id, True, SRC_API, auth=bearer(s.user))

    with web_ctx(app, s.user):
        result = subscribe_community(s.community.id, True, SRC_PLD)
        flashed = get_flashed_messages()

    assert result.status_code == 200
    assert len(flashed) == 1
    assert 'already existed' in flashed[0]
    assert db.session.query(NotificationSubscription).filter_by(
        entity_id=s.community.id, user_id=s.user.id).count() == 1


def test_favorite_community_api_creates_the_favorite_and_returns_the_user_id(app, db_session):
    """:449's false arm (the `else:` at :460), :461's false arm, :468's false
    arm, :475-477's insert, and :481's true arm returning the user id.
    """
    s = _seed_member()

    returned = favorite_community(s.community.id, True, SRC_API, auth=bearer(s.user))

    assert returned == s.user.id
    assert db.session.query(CommunityFavorite).filter_by(
        community_id=s.community.id, user_id=s.user.id).count() == 1


def test_favorite_community_api_toggle_off_removes_it(app, db_session):
    """:449's true arm and :461's true arm, both taken across the two calls:
    the second call's `subscribe=False` hits :449 true, and the existing row
    from the first call hits :450 true, deleting it.
    """
    s = _seed_member()
    favorite_community(s.community.id, True, SRC_API, auth=bearer(s.user))

    returned = favorite_community(s.community.id, False, SRC_API, auth=bearer(s.user))

    assert returned == s.user.id
    assert db.session.query(CommunityFavorite).count() == 0


def test_favorite_community_api_remove_nonexistent_raises(app, db_session):
    """:449's true arm, :450's false arm, :454's message, :455's true arm and
    :456's raise. The row must not exist either before or after.
    """
    s = _seed_member()

    with pytest.raises(Exception, match='A favorite for this community did not exist.'):
        favorite_community(s.community.id, False, SRC_API, auth=bearer(s.user))

    assert db.session.query(CommunityFavorite).count() == 0


def test_favorite_community_api_add_existing_raises(app, db_session):
    """:461's true arm, :462's message, :463's true arm and :464's raise. The
    original row must survive the second, refused call.
    """
    s = _seed_member()
    favorite_community(s.community.id, True, SRC_API, auth=bearer(s.user))

    with pytest.raises(Exception, match='A favorite for this community already existed.'):
        favorite_community(s.community.id, True, SRC_API, auth=bearer(s.user))

    assert db.session.query(CommunityFavorite).filter_by(
        community_id=s.community.id, user_id=s.user.id).count() == 1


def test_favorite_community_api_banned_user_raises(app, db_session):
    """:468's true arm, :469's message, :470's true arm and :471's raise. No
    row must be created.
    """
    s = _seed_member()
    ban_user_from_community(s.user, s.community)

    with pytest.raises(Exception, match='You are banned from this community.'):
        favorite_community(s.community.id, True, SRC_API, auth=bearer(s.user))

    assert db.session.query(CommunityFavorite).count() == 0


def test_favorite_community_banned_community_raises_NoResultFound(app, db_session):
    """:442's `.filter_by(id=community_id, banned=False).one()` -- a banned
    community has no row matching `banned=False`, so `.one()` raises rather
    than returning the row with `banned=True` on it.
    """
    s = _seed_member()
    s.community.banned = True
    db.session.commit()

    with pytest.raises(NoResultFound):
        favorite_community(s.community.id, True, SRC_API, auth=bearer(s.user))


def test_favorite_community_web_creates_via_override_and_returns_the_render(app, db_session):
    """:445's true arm and :446's override, then the ordinary create path
    (:449's false arm, :461's false arm, :468's false arm, :475-477) and
    :481's false arm landing on :484's render.

    `community_id in favorite_communities(user_id)` is False for a user with
    no CommunityFavorite row, so :446 sets `subscribe = True` -- overriding
    the `False` this test passes in as the `subscribe` argument, the same
    proof-of-override shape as subscribe_community's web test above.
    """
    make_site()
    s = _seed_member()

    with web_ctx(app, s.user):
        result = favorite_community(s.community.id, False, SRC_WEB)

    assert result.status_code == 200
    assert db.session.query(CommunityFavorite).filter_by(
        community_id=s.community.id, user_id=s.user.id).count() == 1


def test_favorite_community_web_banned_user_flashes_and_creates_nothing(app, db_session):
    """:468's true arm reached via SRC_WEB, and :473's flash -- NOT one of the
    two lockstep-unreachable arms below: this branch is gated on
    `communities_banned_from`, not on the :445/:448 lockstep, so a plain
    SRC_WEB call (not favorited, so :446's override leaves `subscribe = True`)
    reaches it with no third source value needed.
    """
    make_site()
    s = _seed_member()
    ban_user_from_community(s.user, s.community)

    with web_ctx(app, s.user):
        result = favorite_community(s.community.id, False, SRC_WEB)
        flashed = get_flashed_messages()

    assert result.status_code == 200
    assert len(flashed) == 1
    assert 'banned from this community' in flashed[0]
    assert db.session.query(CommunityFavorite).filter_by(
        community_id=s.community.id, user_id=s.user.id).count() == 0


def test_favorite_community_web_override_off_removes_an_existing_favorite(app, db_session):
    """The OFF half of :446's ternary, never exercised by the two web tests
    above: both seed a user with NO CommunityFavorite row, so
    `community_id in favorite_communities(user_id)` is always False there
    and the override always computes `True`. `coverage.py` does not
    branch-measure a conditional expression, so `[]`/`[]` on this function
    was consistent with that half never running at all.

    Here a CommunityFavorite row is seeded FIRST, making the membership
    test True, so :446 computes `subscribe = False` -- overriding the
    `True` passed in as the argument. :449's true arm is then taken,
    :450's `if existing_fave:` is True, and :451-452 delete the seeded row.
    The row's absence afterward is the assertion: a mutant that skipped the
    override (leaving `subscribe = True`) would instead reach :461's
    else-branch, find `existing_fave` truthy, flash "already existed", and
    leave the row in place.
    """
    make_site()
    s = _seed_member()
    db.session.add(CommunityFavorite(user_id=s.user.id, community_id=s.community.id))
    db.session.commit()

    with web_ctx(app, s.user):
        result = favorite_community(s.community.id, True, SRC_WEB)

    assert result.status_code == 200
    assert db.session.query(CommunityFavorite).filter_by(
        community_id=s.community.id, user_id=s.user.id).count() == 0


def test_favorite_community_a_third_source_reaches_the_flash_branches_the_web_arm_cannot(
        app, db_session):
    """:449's false arm and :458's flash, then :461's true arm, :463's false
    arm and :466's flash -- favorite_community's own instance of the shape
    this campaign retracted D564 over.

    NEITHER SRC_WEB NOR SRC_API CAN REACH :458/:466. SRC_WEB keeps `subscribe`
    and `existing_fave` in lockstep via the identical filter :446 and :448
    both use (community_id, user_id), so the "mismatched" arms these two
    flashes sit behind are jointly unreachable there, and SRC_API's
    :455/:463 always take the raise instead. SRC_PLD (app/constants.py:94,
    the admin preload path) is used here PURELY as a third source value to
    reach these two statements. IT IS NOT HOW FAVORITE_COMMUNITY IS CALLED IN
    PRODUCTION -- app/community/routes.py:1729 passes SRC_WEB and there is no
    SRC_API caller at all today -- and this docstring says so rather than
    implying otherwise. The precedent, down to the constant, is
    tests/test_shared_post_interactions.py:577-639 and this file's own
    subscribe_community twin above.

    `web_ctx` is used even though this is not an SRC_WEB call, for the same
    reason as subscribe_community's SRC_PLD test: :443's else-arm reads
    `current_user.id` for any non-SRC_API source, and :484 renders for any
    non-SRC_API source too. `make_site()` covers that render.

    THE ASSERTIONS ARE ON `flashed`'s CONTENT, NOT ON `result`: under SRC_PLD,
    :481's `if src == SRC_API:` is always False, so control reaches :484's
    render whether or not the flash call is there. The row counts are the
    second half, separating "refused and flashed" from "flashed and then also
    wrote" the way test_subscribe_community_a_third_source... does.
    """
    make_site()
    s = _seed_member()

    with web_ctx(app, s.user):
        result = favorite_community(s.community.id, False, SRC_PLD)
        flashed = get_flashed_messages()

    assert result.status_code == 200
    assert len(flashed) == 1
    assert 'did not exist' in flashed[0]
    assert db.session.query(CommunityFavorite).filter_by(
        community_id=s.community.id, user_id=s.user.id).count() == 0

    favorite_community(s.community.id, True, SRC_API, auth=bearer(s.user))

    with web_ctx(app, s.user):
        result = favorite_community(s.community.id, True, SRC_PLD)
        flashed = get_flashed_messages()

    assert result.status_code == 200
    assert len(flashed) == 1
    assert 'already existed' in flashed[0]
    assert db.session.query(CommunityFavorite).filter_by(
        community_id=s.community.id, user_id=s.user.id).count() == 1


def test_the_notification_toggle_is_rendered_through_the_app_wrapper(app, db_session):
    """D604, fixed: app/shared/community.py rendered its toggle partials with
    flask.render_template, returning a bare string, where every sibling shared
    module uses app.utils.render_template (theme lookup, protocol rewrite,
    headers). The two toggles now use the wrapper and return a Response."""
    make_site()
    s = _seed_member()

    with web_ctx(app, s.user):
        result = subscribe_community(s.community.id, False, SRC_WEB)

    assert result.status_code == 200
    assert 'Link' in result.headers
