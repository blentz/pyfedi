"""`block_community` and `unblock_community` (app/shared/community.py:85-118).

Both are at 0.0% before this file: 22 statements and 12 arcs missing.

These are the fifth instance of a shape this campaign has closed four times --
block_another_user, block_domain, block_remote_instance, and now this.
The src fork, bearer, web_ctx, the id-1 burn, and the deliberate bystander-first
seeding order all transfer from tests/test_shared_domain.py.
"""
from types import SimpleNamespace

import pytest
from flask import current_app, get_flashed_messages
from sqlalchemy.exc import NoResultFound

from app import db
from app.constants import SRC_API, SRC_PLD, SRC_WEB
from app.models import CommunityBlock, CommunityMember
from app.shared.community import block_community, join_community, leave_community, unblock_community
from tests.factories import bearer, make_community, make_community_member, make_instance, make_user, web_ctx


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


def test_leave_community_moderator_without_owner_leaves_freely(app, db_session, monkeypatch):
    """PINS A DEFECT: app/shared/community.py:60 reads
    `if not cm.is_owner or not cm.is_moderator:`, which by De Morgan is
    `not (cm.is_owner and cm.is_moderator)` -- the leave path runs unless
    the member is BOTH owner and moderator. A plain moderator
    (is_moderator=True, is_owner=False, the shape federated moderators are
    created in -- app/activitypub/util.py:959) therefore leaves freely
    instead of being told to step down first at `:75`.

    This test asserts today's wrong behaviour on purpose: no exception is
    raised and the CommunityMember row is gone. Task 5 inverts this
    assertion to expect a refusal once `:60` is corrected to gate on EITHER
    role rather than both.
    """
    s = _seed_member()
    make_community_member(s.user, s.community, is_moderator=True)
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: None)

    returned = leave_community(s.community.id, SRC_API, bearer(s.user))

    assert returned == s.user.id
    assert db.session.query(CommunityMember).filter_by(
        user_id=s.user.id, community_id=s.community.id).count() == 0


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
