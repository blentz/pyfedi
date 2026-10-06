"""Two functions of app/shared/community.py's moderation-and-lifecycle group:
`delete_community` (:487) and `restore_community` (:516).

STEP 1'S ORACLE TRAP, CONFIRMED BEFORE WRITING ANYTHING ELSE: `/usr/bin/grep
-rln "delete_community\\|restore_community" tests/ --include=*.py` returns
`tests/test_shared_tasks_deletes.py`. That file's `:55` reads
`from app.shared.tasks.deletes import (delete_community, restore_community,
...)` -- **a different module entirely**, the ActivityPub Celery task file,
which happens to define functions of the identical names. It is NOT an
oracle for `app.shared.community.delete_community` / `.restore_community`.
Confirmed by reading the import line directly; both functions here start
from zero prior coverage, hence 14 statements / 8 arcs missing each.

`_burn_a_seed()` and `_seed()` below are consumed by this round's later tasks
(2-4) against the rest of this file's moderation surface, so their shape
(a `SimpleNamespace` with `.instance`, `.user`, `.community`) and names are
fixed by the plan, not just local convenience.

D615, FIXED (owner ruling 2026-09-30): both guards are now
`community.is_owner(user) or user.is_admin()`. What follows describes the
three-operand guard they replaced.

THE THREE-OPERAND GUARD, at both `:494` and `:523`:
`if not (community.is_owner(user) or community.is_moderator(user) or
user.is_admin_or_staff()):`. `is_owner(user)` and `is_moderator(user)` are NOT
independent, and no test below claims otherwise. `moderators()`
(`app/models.py:716-722`) returns every non-banned `CommunityMember` row
where `is_owner OR is_moderator` holds. `is_moderator(user)` (`:740`) then
checks membership of `user.id` in THAT LIST -- it never reads the
`is_moderator` COLUMN. `is_owner(user)` (`:747`) checks the column on the
same list. So a row with `is_owner=True` is always admitted to
`moderators()` by the `OR`, which makes `is_moderator(user)` true for that
same user on the same call; the `(user_id, community_id)` primary key rules
out a second, differently-shaped row that could make the two diverge. THERE
IS NO REACHABLE STATE WHERE A USER IS AN OWNER BUT NOT A MODERATOR. The
owner test below therefore makes BOTH the first and second operands of
`:494`/`:523` true at once -- it isolates the {owner, moderator} pair from
the third operand (`is_admin_or_staff()`), not the first operand from the
second. The moderator-alone test (plain `is_moderator=True`,
`is_owner=False`, the shape federated moderators are created in) is the one
genuine, independent isolation of operand two; see this file's REDUNDANT
DISJUNCT comment below for why operand one is never isolated on its own and
no test attempts it.

Every test below that reaches `:510`/`:534` also patches
`app.shared.community.task_selector` (rebound there by this module's own
`from app.shared.tasks import task_selector`, never on `app.shared.tasks`
itself -- see tests/test_shared_community_membership.py's identical note)
and asserts its exact call. Un-patched, `task_selector`'s default
`send_async=True` runs the real Celery body
(`app/shared/tasks/deletes.py:89-113`) synchronously under this test
config's `task_always_eager` (`tests/conftest.py:106-107`) -- undetected,
not merely unasserted. `_seed()` mints an unused bystander `Community` first
specifically so the community under test never lands on the trivial id `1`
that conftest's per-test sequence reset (`:131-132`) would otherwise hand
it; without that, a mutant hardcoding `community_id=1` at `:510`/`:534`
would still satisfy an argument assertion built only from this test's own
seed, because the correct value would also happen to be `1`.

THE MISSING-ID DIVERGENCE (D614) IS FIXED: delete_community and
restore_community used to fail an unknown id with two different exceptions;
both now abort 404.

REMOVE_MOD_FROM_COMMUNITY'S TWO DEFECTS, BOTH NOW HISTORY: `:625`'s
`if existing_member:` used to have no `else`, so removing a moderator who
held no CommunityMember row at all still flashed 'Moderator removed', still
wrote a `remove_mod` ModLog entry naming them, and still fired the task --
the moderation log recorded a removal that never happened. Task 5 added the
`else` (refusing through this module's established `SRC_API` raise /
web-flash-and-return fork) and inverted the test that had pinned it; see
that test below for the fixed behaviour. The second defect -- `:624` (now
`:635`) stripping a community's last owner with no guard, unlike the route's
check at `app/community/routes.py:1477-1478` -- is fixed by Task 6, which
adds `if existing_member.is_owner and community.num_owners() == 1:` at the
top of `:625`'s branch, using the same wording and `'error'` category as the
route. The tests that had pinned the defect are inverted below; see them for
the fixed behaviour.
"""
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import NoResultFound
from werkzeug.exceptions import NotFound

from app import db
from app.constants import NOTIF_NEW_MOD, SRC_API, SRC_WEB
from app.models import CommunityMember, Conversation, ModLog, Notification, Role, user_role
from app.shared.community import (add_mod_to_community, delete_community, remove_mod_from_community,
                                  restore_community)
from tests.factories import (bearer, make_community, make_community_member, make_conversation,
                             make_instance, make_user, web_ctx)

# REDUNDANT DISJUNCT, REGISTERED NOT TESTED -- `community.is_owner(user) or`
# at `:494` and `:523`.
#
# `community.is_owner(user)` can never be the reason `:494`/`:523`'s guard
# passes, for any input, because its truth value is a subset of
# `community.is_moderator(user)`'s. `moderators()` (app/models.py:716-722)
# selects every non-banned CommunityMember row where `is_owner OR
# is_moderator` holds. `is_moderator(user)` (:740) tests membership of
# `user.id` in THAT LIST -- not the `is_moderator` column. `is_owner(user)`
# (:747) tests the `is_owner` column on the same list. A row with
# `is_owner=True` is therefore always admitted to `moderators()` by the OR,
# which makes `is_moderator(user)` true for the identical user on the
# identical call; the CommunityMember primary key (user_id, community_id)
# rules out a second row for this user in this community that could make
# the two diverge. Deleting `community.is_owner(user) or` cannot change
# `:494`/`:523`'s outcome for ANY input -- this is proved by reading the two
# method bodies above, not by any test's failure to kill a mutant that
# removes it, which is what this campaign's equivalence claims require.
#
# This is tests/README.md fact 75, cause 3, "Subsumption" (tests/README.md:
# 2997-3001): "a later conjunct implies this one. Prove it algebraically,
# not by observing zero failures" -- exactly this proof's shape, derived
# from the two method bodies above rather than from any test's silence.
# NOT cause 6: tests/README.md:3025 opens cause 6 with "the only cause on
# this list that is not about a clause", and `community.is_owner(user) or`
# IS a clause -- a disjunct in a boolean expression -- so cause 6 is
# disqualified by its own first sentence, not merely a worse fit. (A prior
# draft of this comment cited cause 6 anyway; the citation did not survive
# review and is corrected here.)
#
# THE WRINKLE, STATED RATHER THAN GLOSSED: cause 3's catalogued text is
# written for CONJUNCTS -- `A and B` where the later conjunct B implies the
# earlier A, so A is redundant. This site is the DISJUNCTIVE DUAL -- `A or
# B` where the EARLIER disjunct A (`is_owner(user)`) implies the LATER one B
# (`is_moderator(user)`), so `A or B` reduces to `B` and A is what is
# redundant. Same principle, mirrored across the operator; the catalogued
# prose does not yet say so. The taxonomy already has shape-sensitive edges
# elsewhere -- tests/README.md:2990-2991 notes that a mutation dropping a
# whole statement rather than a conjunct puts causes 1-5 out of scope, and
# that collapsing one arm of a ternary escapes 1-5, 6 and 8 alike -- and two
# extensions sit registered but unenacted against exactly that kind of gap:
# D578 (a tautology's mirror image, under cause 4) and D589 (configuration-
# scoped unkillability, proposed as a ninth cause). This disjunctive-dual
# reading of cause 3 is a third such gap, belongs beside those two in the
# same register, and is left for Task 9 to register formally rather than
# amending fact 75 from inside this file.
#
# NO TEST ISOLATES OPERAND ONE ALONE, and none should be written to. Doing
# so would require a CommunityMember row with `is_owner=True` and
# `is_moderator=False` for the same user in the same community -- a state
# production cannot reach, per the proof above. Monkeypatching
# `Community.is_moderator` to fake that shape was suggested in review and is
# rejected here: it would fabricate a state production cannot reach, which
# is a false witness of a different shape than the one this campaign hunts.
# Task 8's mutation pass should expect a mutant deleting `community.is_owner
# (user) or` to survive both this file's owner-alone tests and treat it as
# already explained here, not as an uncovered gap.


def grant_role(user, name):
    """A role with exactly this NAME, which is what `is_admin()` and
    `is_staff()` read."""
    role = Role(name=name, weight=0)
    db.session.add(role)
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
    db.session.commit()


def _make_site_admin(user):
    """Give `user` a role named exactly 'Admin'.

    Not exported from tests/factories.py; transcribed from
    tests/test_shared_reply_moderation.py:323-337, which notes
    `User.is_admin()` (app/models.py:1259-1265) checks role NAMES, not
    permissions, so `grant_permission` (tests/factories.py:365) cannot
    produce a site admin however it is called -- the name must be the
    literal string 'Admin'.
    """
    from app.models import Role, user_role
    role = Role(name='Admin', weight=0)
    db.session.add(role)
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
    db.session.commit()
    return role


def _burn_a_seed():
    """Mint and discard user id 1.

    app/models.py:1259-1261 makes id 1 an admin unconditionally, and
    conftest.py:131-132 resets every sequence between tests, so without this
    the first user a test creates is silently an admin and every permission
    guard in this file short-circuits through user.is_admin_or_staff().
    """
    inst = make_instance('burn.test')
    burn = make_user(inst, 'burn')
    assert burn.id == 1, f'expected the burn user at id 1, got {burn.id}'
    return inst


def _seed():
    """An instance, a non-admin user, and a local community, with id 1 burned.

    make_community (tests/factories.py:124) hardcodes instance_id=1 and
    user_id=1, so the burn is load-bearing twice: it dodges the admin trap and
    it supplies the row make_community's foreign keys point at.

    A bystander community is minted first, purely to consume Community id 1.
    conftest.py:131-132 resets every sequence between tests, so without this
    the returned community's id would deterministically be 1 in every test,
    and a mutant hardcoding `community_id=1` into `:510`/`:534`'s
    `task_selector(...)` call would be indistinguishable from correct code by
    any assertion built from this seed alone. The bystander is otherwise
    unused and unreferenced.
    """
    _burn_a_seed()
    instance = make_instance('test.piefed.local')
    user = make_user(instance, 'alice', local=True)
    make_community('bystander', host='bystander.example')
    community = make_community()
    return SimpleNamespace(instance=instance, user=user, community=community)


# `delete_community` (app/shared/community.py:487-513).


def test_delete_community_api_owner_alone_deletes_and_returns_user_id(app, db_session, monkeypatch):
    """`:488`'s SRC_API arm and `:496`'s false arm (the seeded community is
    local by default: `ap_id` is left `None`, so `Community.is_local`
    (`app/models.py:795-796`) short-circuits True through its own first
    operand), then `:512`'s true arm returning the caller's id.

    This makes `community.is_owner(user)` True, which -- per this module's
    docstring and the REDUNDANT DISJUNCT comment above -- ALSO makes
    `community.is_moderator(user)` True for the identical row: an owner's row
    is always a member of `moderators()` (`app/models.py:716-722`), and
    `is_moderator(user)` (`:740`) is a membership check over that same list,
    not the `is_moderator` column. `:494`'s guard therefore passes on either
    of its first two operands here; this test isolates that PAIR from the
    third operand (`is_admin_or_staff()`, isolated instead by the admin-alone
    test below), not the first operand from the second -- no reachable state
    does that.

    `task_selector` is patched on `app.shared.community` (never on
    `app.shared.tasks` -- the `from ... import` at this module's own top
    rebinds the name into `app.shared.community`'s globals) and the capture
    asserts BOTH the task key and that `community_id` is the seeded
    community's actual id, not a literal -- `_seed()`'s bystander community
    keeps that id off the trivial `1` a hardcoding mutant could otherwise
    hide behind.
    """
    s = _seed()
    member = make_community_member(s.user, s.community, is_moderator=False)
    member.is_owner = True
    db.session.commit()
    calls = []
    monkeypatch.setattr('app.shared.community.task_selector',
                         lambda task_key, **kw: calls.append((task_key, kw)))

    returned = delete_community(s.community.id, SRC_API, bearer(s.user))

    assert returned == s.user.id
    assert s.community.banned is True
    assert calls == [('delete_community', {'user_id': s.user.id, 'community_id': s.community.id})]


def test_delete_community_web_moderator_alone_is_refused(app, db_session, monkeypatch):
    """D615, fixed (owner ruling 2026-09-30). A plain moderator (not owner, no
    admin role) used to pass the guard and could soft-delete the whole
    community. Only the owner and instance admins may now, as the web route
    `community_delete` already required.
    """
    s = _seed()
    make_community_member(s.user, s.community, is_moderator=True)
    calls = []
    monkeypatch.setattr('app.shared.community.task_selector',
                         lambda task_key, **kw: calls.append((task_key, kw)))

    with web_ctx(app, s.user):
        with pytest.raises(Exception, match='incorrect_login'):
            delete_community(s.community.id, SRC_WEB)

    assert s.community.banned is False
    assert calls == []


def test_delete_community_staff_alone_is_refused(app, db_session):
    """D615: staff are not instance admins, so the ruling leaves them out too."""
    s = _seed()
    grant_role(s.user, 'Staff')

    with web_ctx(app, s.user):
        with pytest.raises(Exception, match='incorrect_login'):
            delete_community(s.community.id, SRC_WEB)

    assert s.community.banned is False


def test_delete_community_admin_alone_without_membership_deletes(app, db_session, monkeypatch):
    """`:494`'s guard passing through its THIRD operand alone: `s.user` holds
    no CommunityMember row at all -- `Community.moderators()`
    (`app/models.py:716-722`) returns an empty list for it, so `is_owner` and
    `is_moderator` (`:736-747`) are both False on their own -- and is instead
    a site admin via `_make_site_admin`, which grants a Role named exactly
    'Admin' so `User.is_admin()` (`app/models.py:1259-1265`) returns True and
    `is_admin_or_staff()` (`:1274-1275`) follows.

    See the owner-alone test's docstring above for why `task_selector` is
    patched on `app.shared.community` and what the argument assertion is
    defending against.
    """
    s = _seed()
    _make_site_admin(s.user)
    calls = []
    monkeypatch.setattr('app.shared.community.task_selector',
                         lambda task_key, **kw: calls.append((task_key, kw)))

    returned = delete_community(s.community.id, SRC_API, bearer(s.user))

    assert returned == s.user.id
    assert s.community.banned is True
    assert calls == [('delete_community', {'user_id': s.user.id, 'community_id': s.community.id})]


def test_delete_community_permission_refusal_raises_and_leaves_community_untouched(
        app, db_session):
    """`:494`'s true arm: no CommunityMember row and no admin role, so all
    three operands are False and `:495` raises. `community.banned` must
    survive as False -- a mutant that raised after banning would also satisfy
    a raise-only assertion.
    """
    s = _seed()

    with web_ctx(app, s.user):
        with pytest.raises(Exception, match='incorrect_login'):
            delete_community(s.community.id, SRC_WEB)

    assert s.community.banned is False


def test_delete_community_non_local_refusal_raises_and_leaves_community_untouched(
        app, db_session):
    """`:496`'s true arm. The guard at `:494` is satisfied first (the caller
    is the owner), so this isolates `:496` rather than re-testing `:494`.

    `make_community(host='remote.example')` alone would still be local:
    `Community.is_local` (`app/models.py:795-796`) is `self.ap_id is None or
    ...`, and the factory never sets `ap_id`, so the first operand alone
    would make it True regardless of host. Setting `ap_id` to a non-None,
    non-local URL directly is what makes the first operand False and forces
    evaluation of the second, `profile_id().startswith(SERVER_URL)`, which is
    also False because `ap_profile_id` is on `remote.example`, not
    `SERVER_NAME` (`test.piefed.local`, tests/conftest.py:69).
    """
    s = _seed()
    remote_community = make_community('faraway', host='remote.example')
    remote_community.ap_id = 'https://remote.example/c/faraway'
    db.session.commit()
    member = make_community_member(s.user, remote_community, is_moderator=False)
    member.is_owner = True
    db.session.commit()

    with web_ctx(app, s.user):
        with pytest.raises(Exception, match='Only local communities can be deleted'):
            delete_community(remote_community.id, SRC_WEB)

    assert remote_community.banned is False


def test_delete_community_missing_id_is_a_404(app, db_session):
    """D614, fixed: delete_community used `.get()` and then read
    `community.is_owner`, an AttributeError on None, while restore_community's
    `.one()` raised NoResultFound. Both twins now look the community up the
    same way and abort 404 for an unknown id."""
    s = _seed()

    with web_ctx(app, s.user):
        with pytest.raises(NotFound):
            delete_community(999999, SRC_WEB)


# `restore_community` (app/shared/community.py:516-537).


def test_restore_community_api_owner_alone_restores_and_returns_user_id(app, db_session, monkeypatch):
    """`:517`'s SRC_API arm and `:525`'s false arm (the community stays
    local, same reasoning as delete_community's twin above), then `:536`'s
    true arm returning the caller's id. The community starts banned so the
    state change from `True` to `False` is real, not a no-op.

    This makes `community.is_owner(user)` True, which -- per the REDUNDANT
    DISJUNCT comment near the top of this file -- ALSO makes
    `community.is_moderator(user)` True for the identical row (an owner's
    row is always a member of `moderators()`, `app/models.py:716-722`, and
    `is_moderator(user)`, `:740`, is a membership check over that list, not
    the column). `:523`'s guard therefore passes on either of its first two
    operands here; this isolates that PAIR from the third
    (`is_admin_or_staff()`, isolated by the admin-alone test below), not the
    first operand from the second -- no reachable state does that.

    `task_selector` is patched on `app.shared.community` (never
    `app.shared.tasks` -- the module-level rebinding this file's docstring
    describes) and the capture asserts the task key and that `community_id`
    is the seeded community's actual id, not a literal; `_seed()`'s
    bystander community keeps that id off the trivial `1` a hardcoding
    mutant could otherwise hide behind.
    """
    s = _seed()
    s.community.banned = True
    db.session.commit()
    member = make_community_member(s.user, s.community, is_moderator=False)
    member.is_owner = True
    db.session.commit()
    calls = []
    monkeypatch.setattr('app.shared.community.task_selector',
                         lambda task_key, **kw: calls.append((task_key, kw)))

    returned = restore_community(s.community.id, SRC_API, bearer(s.user))

    assert returned == s.user.id
    assert s.community.banned is False
    assert calls == [('restore_community', {'user_id': s.user.id, 'community_id': s.community.id})]


def test_restore_community_web_moderator_alone_is_refused(app, db_session, monkeypatch):
    """D615, fixed: restoring is held to the same rule as deleting -- the
    owner and instance admins only, not a plain moderator.
    """
    s = _seed()
    s.community.banned = True
    db.session.commit()
    make_community_member(s.user, s.community, is_moderator=True)
    calls = []
    monkeypatch.setattr('app.shared.community.task_selector',
                         lambda task_key, **kw: calls.append((task_key, kw)))

    with web_ctx(app, s.user):
        with pytest.raises(Exception, match='incorrect_login'):
            restore_community(s.community.id, SRC_WEB)

    assert s.community.banned is True
    assert calls == []


def test_restore_community_admin_alone_without_membership_restores(app, db_session, monkeypatch):
    """`:523`'s guard passing through its THIRD operand alone, same shape as
    delete_community's admin-alone test above: no CommunityMember row, a
    site-admin Role instead.

    See the owner-alone test's docstring above for the `task_selector` patch
    and argument assertion.
    """
    s = _seed()
    s.community.banned = True
    db.session.commit()
    _make_site_admin(s.user)
    calls = []
    monkeypatch.setattr('app.shared.community.task_selector',
                         lambda task_key, **kw: calls.append((task_key, kw)))

    returned = restore_community(s.community.id, SRC_API, bearer(s.user))

    assert returned == s.user.id
    assert s.community.banned is False
    assert calls == [('restore_community', {'user_id': s.user.id, 'community_id': s.community.id})]


def test_restore_community_permission_refusal_raises_and_leaves_community_untouched(
        app, db_session):
    """`:523`'s true arm: no CommunityMember row and no admin role, so `:524`
    raises. `community.banned` must survive unchanged (still True here, to
    prove the refusal does not flip it either way).
    """
    s = _seed()
    s.community.banned = True
    db.session.commit()

    with web_ctx(app, s.user):
        with pytest.raises(Exception, match='incorrect_login'):
            restore_community(s.community.id, SRC_WEB)

    assert s.community.banned is True


def test_restore_community_non_local_refusal_raises_and_leaves_community_untouched(
        app, db_session):
    """`:525`'s true arm, isolated from `:523` the same way as
    delete_community's non-local test: the guard is satisfied first (owner),
    so only the locality check is under test.
    """
    s = _seed()
    remote_community = make_community('faraway', host='remote.example')
    remote_community.ap_id = 'https://remote.example/c/faraway'
    remote_community.banned = True
    db.session.commit()
    member = make_community_member(s.user, remote_community, is_moderator=False)
    member.is_owner = True
    db.session.commit()

    with web_ctx(app, s.user):
        with pytest.raises(Exception, match='Only local communities can be restored'):
            restore_community(remote_community.id, SRC_WEB)

    assert remote_community.banned is True


def test_restore_community_missing_id_is_a_404(app, db_session):
    """D614, fixed: the restore twin of the delete test above."""
    s = _seed()

    with web_ctx(app, s.user):
        with pytest.raises(NotFound):
            restore_community(999999, SRC_WEB)


# `add_mod_to_community` (app/shared/community.py:540-605), first half only:
# `:542-562` (lookup, the permission guard, and the existing-member fork) and
# `:589-605` (the modlog write, the task_selector call, and the API return).
# `:564-588` -- the new_moderator.is_local() notify/chat-message fork -- is
# Task 3's; the tests below let that code execute (it must, since it sits
# between `:562` and `:589` in a straight-line function) but assert nothing
# about it. Every target user below is minted with `local=True` so that fork
# always takes its notify-in-app arm, which is a handful of local Notification
# writes with no outbound side effects (email, federation, chat) to stub out.
#
# `task_selector` is patched on `app.shared.community` for the same reason as
# this file's delete_community/restore_community tests above: the `from
# app.shared.tasks import task_selector` at this module's own top rebinds the
# name into `app.shared.community`'s globals, and `_seed()`'s bystander
# community keeps `community_id` off the trivial `1` a hardcoding mutant
# could otherwise hide behind.


def test_add_mod_to_community_owner_may_add(app, db_session, monkeypatch):
    """`:549`'s first operand alone: the actor is an owner and NOT an admin
    (`_seed()` burns user id 1, so `s.user` is never the id-1 admin trap).

    Exercises `:542-548` (the SRC_API auth arm, then the community and
    target lookups), `:551-558`'s else arm (the target holds no existing
    CommunityMember row here, so a new one is created), `:559-561`'s false
    arm (SRC_API skips the flash), `:589-590` (add_to_modlog writes a ModLog
    row -- this task's target range), and `:602-605` (the task_selector call
    and the SRC_API return of the actor's id).
    """
    s = _seed()
    member = make_community_member(s.user, s.community, is_moderator=False)
    member.is_owner = True
    db.session.commit()
    target = make_user(s.instance, 'bob', local=True)
    calls = []
    monkeypatch.setattr('app.shared.community.task_selector',
                         lambda task_key, **kw: calls.append((task_key, kw)))

    returned = add_mod_to_community(s.community.id, target.id, SRC_API, bearer(s.user))

    assert returned == s.user.id
    new_row = db.session.query(CommunityMember).filter_by(
        user_id=target.id, community_id=s.community.id).one()
    assert new_row.is_moderator is True
    modlog_row = db.session.query(ModLog).filter_by(action='add_mod').one()
    assert modlog_row.user_id == s.user.id
    assert modlog_row.target_user_id == target.id
    assert modlog_row.community_id == s.community.id
    assert calls == [('add_mod', {'user_id': s.user.id, 'mod_id': target.id,
                                  'community_id': s.community.id})]


def test_add_mod_to_community_admin_who_is_not_owner_may_add(app, db_session, monkeypatch):
    """`:549`'s second operand alone: the actor is staff and NOT an owner --
    `s.user` holds no CommunityMember row at all, so `community.is_owner`
    (`app/models.py:742-747`) is False on its own, and is instead a site
    admin via `_make_site_admin`.

    Also exercises `:559-561`'s true arm: SRC_WEB flashes 'Moderator added'
    (asserted via Flask's `get_flashed_messages`, available because
    `web_ctx` opens a real request context) and `:604-605`'s false arm
    returns `None` rather than the actor's id.
    """
    from flask import get_flashed_messages

    s = _seed()
    _make_site_admin(s.user)
    target = make_user(s.instance, 'bob', local=True)
    calls = []
    monkeypatch.setattr('app.shared.community.task_selector',
                         lambda task_key, **kw: calls.append((task_key, kw)))

    with web_ctx(app, s.user):
        returned = add_mod_to_community(s.community.id, target.id, SRC_WEB)
        flashed = get_flashed_messages()

    assert returned is None
    assert flashed == ['Moderator added']
    new_row = db.session.query(CommunityMember).filter_by(
        user_id=target.id, community_id=s.community.id).one()
    assert new_row.is_moderator is True
    assert calls == [('add_mod', {'user_id': s.user.id, 'mod_id': target.id,
                                  'community_id': s.community.id})]


def test_add_mod_to_community_plain_member_is_refused(app, db_session):
    """`:549` with BOTH operands false -- a plain, non-owner CommunityMember
    row and no admin role -- raises `'no_permission'` at `:550`.

    Note the string: `:550` raises `'no_permission'` where the twin at
    `:618` (`remove_mod_from_community`) raises `'incorrect_login'` for the
    same shape of condition. Asserting the actual string here, not assuming
    symmetry with the removal twin.

    No CommunityMember row is created or altered for the target, and no
    ModLog row is written: the guard raises before `:551-558` and `:589-590`
    ever run.
    """
    s = _seed()
    make_community_member(s.user, s.community, is_moderator=False)
    target = make_user(s.instance, 'bob', local=True)

    with web_ctx(app, s.user):
        with pytest.raises(Exception, match='no_permission'):
            add_mod_to_community(s.community.id, target.id, SRC_WEB)

    assert db.session.query(CommunityMember).filter_by(
        user_id=target.id, community_id=s.community.id).count() == 0
    assert db.session.query(ModLog).filter_by(action='add_mod').count() == 0


def test_add_mod_to_community_existing_member_is_promoted_without_new_row(
        app, db_session, monkeypatch):
    """`:554`'s true arm: the target already holds a CommunityMember row (a
    plain, non-moderator member), so `:555` flips its `is_moderator` column
    True in place. Asserting the row count stays at 1 catches a mutant that
    creates a second, duplicate row instead of reusing the existing one.
    """
    s = _seed()
    member = make_community_member(s.user, s.community, is_moderator=False)
    member.is_owner = True
    db.session.commit()
    target = make_user(s.instance, 'bob', local=True)
    make_community_member(target, s.community, is_moderator=False)
    calls = []
    monkeypatch.setattr('app.shared.community.task_selector',
                         lambda task_key, **kw: calls.append((task_key, kw)))

    returned = add_mod_to_community(s.community.id, target.id, SRC_API, bearer(s.user))

    assert returned == s.user.id
    rows = db.session.query(CommunityMember).filter_by(
        user_id=target.id, community_id=s.community.id).all()
    assert len(rows) == 1
    assert rows[0].is_moderator is True
    assert calls == [('add_mod', {'user_id': s.user.id, 'mod_id': target.id,
                                  'community_id': s.community.id})]


def test_add_mod_to_community_no_existing_member_creates_new_row(app, db_session, monkeypatch):
    """`:554`'s false arm: the target holds no CommunityMember row at all, so
    `:557-558` creates a new one with `is_moderator=True`. Asserting the row
    count reaches exactly 1 (not 0, and not more than 1) catches a mutant
    that skips the creation entirely or that runs it more than once.
    """
    s = _seed()
    member = make_community_member(s.user, s.community, is_moderator=False)
    member.is_owner = True
    db.session.commit()
    target = make_user(s.instance, 'bob', local=True)
    assert db.session.query(CommunityMember).filter_by(
        user_id=target.id, community_id=s.community.id).count() == 0
    calls = []
    monkeypatch.setattr('app.shared.community.task_selector',
                         lambda task_key, **kw: calls.append((task_key, kw)))

    returned = add_mod_to_community(s.community.id, target.id, SRC_API, bearer(s.user))

    assert returned == s.user.id
    rows = db.session.query(CommunityMember).filter_by(
        user_id=target.id, community_id=s.community.id).all()
    assert len(rows) == 1
    assert rows[0].is_moderator is True
    assert calls == [('add_mod', {'user_id': s.user.id, 'mod_id': target.id,
                                  'community_id': s.community.id})]


def test_add_mod_to_community_banned_user_cannot_be_added(app, db_session):
    """`:548` is `User.query.filter_by(id=person_id, banned=False).one()`, so
    a banned target raises `NoResultFound` before any permission work at
    `:549` or any membership work at `:551-558`.

    Its twin at `:616` (`remove_mod_from_community`) omits `banned=False`,
    which looks deliberate -- you want to be able to demote a banned
    moderator. That asymmetry is a test case here, not a finding.
    """
    s = _seed()
    member = make_community_member(s.user, s.community, is_moderator=False)
    member.is_owner = True
    db.session.commit()
    target = make_user(s.instance, 'bob', local=True)
    target.banned = True
    db.session.commit()

    with web_ctx(app, s.user):
        with pytest.raises(NoResultFound):
            add_mod_to_community(s.community.id, target.id, SRC_WEB)


# `add_mod_to_community`'s notify fork (app/shared/community.py:564-588). The
# local arm (`:564-574`) writes a `Notification` row and increments
# `unread_notifications`; the remote arm (`:575-587`) builds or reuses a
# `Conversation` and sends a chat message. `User.is_local()`
# (`app/models.py:1251-1252`) is `self.ap_id is None or
# self.ap_profile_id.startswith(SERVER_URL)`; `make_user(..., local=True)`
# leaves both `None` (the local arm), while `make_user(remote_instance, ...,
# local=False)` on a distinct instance domain sets both off that instance's
# own domain, which is never `SERVER_NAME` (`'test.piefed.local'`,
# tests/conftest.py:69) for a genuinely separate instance (the remote arm).


def test_add_mod_to_community_local_moderator_gets_notification_and_unread_count(
        app, db_session, monkeypatch):
    """`:564`'s true arm: the target is local, so `:567-571` builds a
    `Notification` row and `:572` separately increments
    `new_moderator.unread_notifications`. Asserting the increment apart from
    the row's existence is what catches a mutant that deletes `:572` alone --
    the notification would still be written, but the counter would stay at 0.

    Also asserts `notif_type` (must be `NOTIF_NEW_MOD`, not some other
    constant), `subtype`, `url`, and `targets['community_id']` -- the latter
    is the seeded, non-`1` community id (`_seed()` burns a bystander
    community first; see this file's module docstring), so a mutant
    corrupting `targets_data`'s `community_id` at `:565` cannot hide behind a
    coincidental match with a hardcoded `1`.

    `task_selector` is patched and its call asserted for the same reason as
    every other test in this file reaching `:602`.
    """
    s = _seed()
    member = make_community_member(s.user, s.community, is_moderator=False)
    member.is_owner = True
    db.session.commit()
    target = make_user(s.instance, 'bob', local=True)
    assert target.unread_notifications == 0
    calls = []
    monkeypatch.setattr('app.shared.community.task_selector',
                         lambda task_key, **kw: calls.append((task_key, kw)))

    returned = add_mod_to_community(s.community.id, target.id, SRC_API, bearer(s.user))

    assert returned == s.user.id
    notif = db.session.query(Notification).filter_by(
        user_id=target.id, author_id=s.user.id).one()
    assert notif.notif_type == NOTIF_NEW_MOD
    assert notif.subtype == 'new_moderator'
    assert notif.url == '/c/' + s.community.name
    assert notif.targets == {'gen': '0', 'community_id': s.community.id}
    assert target.unread_notifications == 1
    assert calls == [('add_mod', {'user_id': s.user.id, 'mod_id': target.id,
                                  'community_id': s.community.id})]


def test_add_mod_to_community_remote_moderator_creates_a_conversation(
        app, db_session, monkeypatch):
    """`:579`'s true arm -- no prior conversation for this pair, so
    `:580-584` creates one. Asserting the `Conversation` row COUNT (0 -> 1)
    is what the reuse test below turns into a kill: a mutant that always
    creates a fresh row regardless of `:579`'s outcome passes this test (0 ->
    1 either way) but fails the reuse test (1 -> 2 instead of staying at 1).

    `send_message` is patched on `app.chat.util`: since the import cycle fix
    (chat.util) `app.shared.community` imports that module and looks
    `send_message` up on it at call time. The capture
    asserts the community's name appears in the message body (`:586`) and
    that the call carries the newly created conversation's id.
    """
    s = _seed()
    member = make_community_member(s.user, s.community, is_moderator=False)
    member.is_owner = True
    db.session.commit()
    remote_instance = make_instance('remote.example')
    target = make_user(remote_instance, 'bob', local=False)
    assert db.session.query(Conversation).count() == 0
    task_calls = []
    monkeypatch.setattr('app.shared.community.task_selector',
                         lambda task_key, **kw: task_calls.append((task_key, kw)))
    send_calls = []
    monkeypatch.setattr(
        'app.chat.util.send_message',
        lambda message, conversation_id, user=None: send_calls.append(
            (message, conversation_id, user)))

    returned = add_mod_to_community(s.community.id, target.id, SRC_API, bearer(s.user))

    assert returned == s.user.id
    assert db.session.query(Conversation).count() == 1
    conversation = db.session.query(Conversation).one()
    assert {u.id for u in conversation.members} == {s.user.id, target.id}
    assert len(send_calls) == 1
    message, conversation_id, user_arg = send_calls[0]
    assert s.community.name in message
    assert conversation_id == conversation.id
    assert user_arg.id == s.user.id
    assert task_calls == [('add_mod', {'user_id': s.user.id, 'mod_id': target.id,
                                       'community_id': s.community.id})]


def test_add_mod_to_community_remote_moderator_reuses_an_existing_conversation(
        app, db_session, monkeypatch):
    """`:579`'s false arm -- `make_conversation` (`tests/factories.py:647`)
    seeded a conversation between the two parties first, so
    `Conversation.find_existing_conversation` returns it at `:577` and no
    second row is created. Asserting the row COUNT stays at 1 (not 2) is
    what kills a mutant that always builds a fresh `Conversation`; see the
    creation test above for that mutant's other half.
    """
    s = _seed()
    member = make_community_member(s.user, s.community, is_moderator=False)
    member.is_owner = True
    db.session.commit()
    remote_instance = make_instance('remote.example')
    target = make_user(remote_instance, 'bob', local=False)
    existing = make_conversation(s.user, target)
    assert db.session.query(Conversation).count() == 1
    task_calls = []
    monkeypatch.setattr('app.shared.community.task_selector',
                         lambda task_key, **kw: task_calls.append((task_key, kw)))
    send_calls = []
    monkeypatch.setattr(
        'app.chat.util.send_message',
        lambda message, conversation_id, user=None: send_calls.append(
            (message, conversation_id, user)))

    returned = add_mod_to_community(s.community.id, target.id, SRC_API, bearer(s.user))

    assert returned == s.user.id
    assert db.session.query(Conversation).count() == 1
    assert len(send_calls) == 1
    message, conversation_id, user_arg = send_calls[0]
    assert s.community.name in message
    assert conversation_id == existing.id
    assert user_arg.id == s.user.id
    assert task_calls == [('add_mod', {'user_id': s.user.id, 'mod_id': target.id,
                                       'community_id': s.community.id})]


# `remove_mod_from_community` (app/shared/community.py:608-653).
#
# `task_selector` is patched on `app.shared.community` for the same reason as
# every other test in this file: the `from app.shared.tasks import
# task_selector` at this module's own top rebinds the name into
# `app.shared.community`'s globals, never `app.shared.tasks` itself.
#
# `:617`'s guard -- `if not community.is_owner(user) and not
# user.is_admin_or_staff():` -- is `is_owner` against `is_admin_or_staff()`,
# which ARE independent (unlike the REDUNDANT DISJUNCT comment's
# `is_owner`/`is_moderator` pair near the top of this file), so both operands
# are genuinely isolatable here. Three tests below isolate owner-alone,
# admin-alone, and neither, the same shape as `add_mod_to_community`'s guard
# tests above. Note the error string: `:618` raises `'incorrect_login'`,
# where `add_mod_to_community:550` raises `'no_permission'` for the same
# shape of condition -- asserted as written, not assumed symmetric.


def test_remove_mod_from_community_api_owner_alone_removes_and_returns_user_id(
        app, db_session, monkeypatch):
    """`:610-611`'s SRC_API arm, `:617`'s guard passing through its FIRST
    operand alone (owner, not admin -- `_seed()` burns user id 1, so `s.user`
    is never the id-1 admin trap), and `:663-664`'s true arm returning the
    caller's id.

    The target is seeded as BOTH `is_moderator=True` and (set directly after
    the factory call, which always writes `is_owner=False`) `is_owner=True`
    -- a co-owner-and-moderator row -- so `:625`'s true branch clearing both
    flags at `:634-635` is asserted as a real True-to-False transition on
    each column, not just on the one the factory happened to default True.
    """
    s = _seed()
    owner = make_community_member(s.user, s.community, is_moderator=False)
    owner.is_owner = True
    db.session.commit()
    target = make_user(s.instance, 'bob', local=True)
    target_member = make_community_member(target, s.community, is_moderator=True)
    target_member.is_owner = True
    db.session.commit()
    calls = []
    monkeypatch.setattr('app.shared.community.task_selector',
                         lambda task_key, **kw: calls.append((task_key, kw)))

    returned = remove_mod_from_community(s.community.id, target.id, SRC_API, bearer(s.user))

    assert returned == s.user.id
    assert target_member.is_moderator is False
    assert target_member.is_owner is False
    modlog_row = db.session.query(ModLog).filter_by(action='remove_mod').one()
    assert modlog_row.user_id == s.user.id
    assert modlog_row.target_user_id == target.id
    assert modlog_row.community_id == s.community.id
    assert calls == [('remove_mod', {'user_id': s.user.id, 'mod_id': target.id,
                                     'community_id': s.community.id})]


def test_remove_mod_from_community_web_owner_alone_removes_when_two_owners_remain(
        app, db_session, monkeypatch):
    """The SRC_WEB sibling of `:857` above, seeded identically (actor owner,
    target a co-owner-and-moderator, `community.num_owners() == 2`), added
    specifically to give `:626`'s `community.num_owners() == 1` operand a
    NON-CRASHING kill.

    Mutation testing found that `:857` alone -- SRC_API -- kills the mutant
    that deletes ` and community.num_owners() == 1` from `:626` (leaving
    `if existing_member.is_owner:`, refusing ANY owner removal) only by
    letting `:630`'s `raise Exception(msg)` escape uncaught. This campaign
    does not count a crash as a kill unless a viable non-crashing variant
    also dies, and no other test removed an owner from a community with a
    second owner remaining through SRC_WEB -- so before this test, that
    operand's killability rested entirely on an exception path. Under the
    mutant here, SRC_WEB's `else` arm at `:631-633` flashes the last-owner
    refusal message and returns -- no exception -- so the assertions below
    fail as a plain `AssertionError`, not a crash.
    """
    from flask import get_flashed_messages

    s = _seed()
    owner = make_community_member(s.user, s.community, is_moderator=False)
    owner.is_owner = True
    db.session.commit()
    target = make_user(s.instance, 'bob', local=True)
    target_member = make_community_member(target, s.community, is_moderator=True)
    target_member.is_owner = True
    db.session.commit()
    assert s.community.num_owners() == 2
    calls = []
    monkeypatch.setattr('app.shared.community.task_selector',
                         lambda task_key, **kw: calls.append((task_key, kw)))

    with web_ctx(app, s.user):
        returned = remove_mod_from_community(s.community.id, target.id, SRC_WEB)
        flashed = get_flashed_messages()

    assert returned is None
    assert flashed == ['Moderator removed']
    assert target_member.is_moderator is False
    assert target_member.is_owner is False
    assert s.community.num_owners() == 1
    assert calls == [('remove_mod', {'user_id': s.user.id, 'mod_id': target.id,
                                     'community_id': s.community.id})]


def test_remove_mod_from_community_admin_who_is_not_owner_may_remove_and_flashes(
        app, db_session, monkeypatch):
    """`:612-613`'s else arm (`current_user`), `:617`'s guard passing through
    its SECOND operand alone (staff, not owner -- `s.user` holds no
    CommunityMember row at all, so `community.is_owner` is False on its own,
    and is instead a site admin via `_make_site_admin`), `:645-646`'s web
    flash, and `:663`'s false arm: a non-API src falls off the end and
    returns `None` rather than the actor's id.
    """
    from flask import get_flashed_messages

    s = _seed()
    _make_site_admin(s.user)
    target = make_user(s.instance, 'bob', local=True)
    target_member = make_community_member(target, s.community, is_moderator=True)
    calls = []
    monkeypatch.setattr('app.shared.community.task_selector',
                         lambda task_key, **kw: calls.append((task_key, kw)))

    with web_ctx(app, s.user):
        returned = remove_mod_from_community(s.community.id, target.id, SRC_WEB)
        flashed = get_flashed_messages()

    assert returned is None
    assert flashed == ['Moderator removed']
    assert target_member.is_moderator is False
    assert target_member.is_owner is False
    modlog_row = db.session.query(ModLog).filter_by(action='remove_mod').one()
    assert modlog_row.target_user_id == target.id
    assert calls == [('remove_mod', {'user_id': s.user.id, 'mod_id': target.id,
                                     'community_id': s.community.id})]


def test_remove_mod_from_community_neither_owner_nor_admin_is_refused(app, db_session):
    """`:617` with BOTH operands false -- a plain, non-owner CommunityMember
    row for the actor and no admin role -- raises `'incorrect_login'` at
    `:618`, before `:620-636`'s membership work or `:648`'s modlog write ever
    run. The target's existing moderator row and the modlog table must both
    survive untouched.
    """
    s = _seed()
    make_community_member(s.user, s.community, is_moderator=False)
    target = make_user(s.instance, 'bob', local=True)
    target_member = make_community_member(target, s.community, is_moderator=True)

    with web_ctx(app, s.user):
        with pytest.raises(Exception, match='incorrect_login'):
            remove_mod_from_community(s.community.id, target.id, SRC_WEB)

    assert target_member.is_moderator is True
    assert db.session.query(ModLog).filter_by(action='remove_mod').count() == 0


def test_remove_mod_from_community_non_member_web_is_refused_and_writes_no_modlog_entry(
        app, db_session, monkeypatch):
    """`:625`'s `if existing_member:` now has an `else` (Task 5): a target
    who holds no CommunityMember row at all is refused at `:637-643` before
    `:645-646`'s flash, `:648-649`'s modlog write, or `:661`'s task can run.

    FORMERLY A PIN. Until Task 5, `:625` had no `else`, so this same
    non-member removal still flashed 'Moderator removed', still wrote a
    `remove_mod` ModLog entry naming the stranger, and still fired the task
    -- the moderation log recorded a removal that never happened. The ModLog
    assertion below is the load-bearing one, not the flash: a fix that only
    changed the flash text but left the audit write in place would still
    fail it.

    The flash CATEGORY is asserted via `get_flashed_messages(with_categories=
    True)`, not just the text: this refusal flashes `'warning'`
    (`app/shared/community.py:642`), where the last-owner refusal elsewhere
    in this file flashes `'error'` -- a category swap between the two would
    otherwise go undetected, and the `'error'` category on the sibling
    matters especially because it deliberately matches
    `app/community/routes.py:1478`.
    """
    s = _seed()
    owner = make_community_member(s.user, s.community, is_moderator=False)
    owner.is_owner = True
    db.session.commit()
    stranger = make_user(s.instance, 'stranger', local=True)
    assert db.session.query(CommunityMember).filter_by(
        user_id=stranger.id, community_id=s.community.id).count() == 0
    calls = []
    monkeypatch.setattr('app.shared.community.task_selector',
                         lambda task_key, **kw: calls.append((task_key, kw)))

    from flask import get_flashed_messages

    with web_ctx(app, s.user):
        remove_mod_from_community(s.community.id, stranger.id, SRC_WEB)
        flashed = get_flashed_messages(with_categories=True)

    assert flashed == [('warning', 'That user is not a moderator of this community.')]
    assert db.session.query(ModLog).filter_by(
        action='remove_mod', target_user_id=stranger.id).count() == 0
    assert db.session.query(CommunityMember).filter_by(
        user_id=stranger.id, community_id=s.community.id).count() == 0
    assert calls == []


def test_remove_mod_from_community_non_member_api_raises_and_writes_no_modlog_entry(
        app, db_session, monkeypatch):
    """Same non-member refusal as the web test above, but through `:610-611`'s
    SRC_API arm: `:639-640`'s `raise Exception(msg)` fires instead of the web
    flash, still before any ModLog write or task dispatch.
    """
    s = _seed()
    owner = make_community_member(s.user, s.community, is_moderator=False)
    owner.is_owner = True
    db.session.commit()
    stranger = make_user(s.instance, 'stranger', local=True)
    assert db.session.query(CommunityMember).filter_by(
        user_id=stranger.id, community_id=s.community.id).count() == 0
    calls = []
    monkeypatch.setattr('app.shared.community.task_selector',
                         lambda task_key, **kw: calls.append((task_key, kw)))

    with pytest.raises(Exception, match='not a moderator'):
        remove_mod_from_community(s.community.id, stranger.id, SRC_API, bearer(s.user))

    assert db.session.query(ModLog).filter_by(
        action='remove_mod', target_user_id=stranger.id).count() == 0
    assert db.session.query(CommunityMember).filter_by(
        user_id=stranger.id, community_id=s.community.id).count() == 0
    assert calls == []


def test_remove_mod_from_community_plain_member_target_is_refused_and_writes_no_modlog_entry(
        app, db_session, monkeypatch):
    """`:620-624`'s `existing_member` lookup now carries an `or_(is_moderator
    == True, is_owner == True)` predicate alongside the `user_id`/
    `community_id` filter, so a plain, non-moderator `CommunityMember` row
    (the shape `join_community:44` creates for every subscriber, with both
    flags defaulting False) no longer satisfies the lookup. `existing_member`
    comes back `None` and `:637-643`'s `else` refuses the removal, exactly as
    it already does for a target with no row at all.

    FORMERLY A PIN OF A CRITICAL DEFECT, THIS ROUND'S HEADLINE FIX. Before the
    `or_` predicate was added, this exact removal -- target holds a plain
    member row, never a moderator or owner -- took `:625`'s TRUE branch: it
    set two already-False flags, flashed 'Moderator removed', and wrote a
    `remove_mod` ModLog entry naming a user who was never a moderator. That is
    verbatim the shape of the defect `43b18996` claims to have fixed; its
    `else` only refused a target with NO row, never a member who is not a
    moderator. The pin (`assert flashed == ['Moderator removed']` and a
    `ModLog` count of 1) was confirmed to PASS against the pre-fix code by
    running it, then inverted here. The ModLog assertion is the load-bearing
    one, matching this file's non-member refusal test above: a fix that only
    changed the flash text but left the audit write in place would still
    fail it.
    """
    from flask import get_flashed_messages

    s = _seed()
    owner = make_community_member(s.user, s.community, is_moderator=False)
    owner.is_owner = True
    db.session.commit()
    target = make_user(s.instance, 'bob', local=True)
    make_community_member(target, s.community, is_moderator=False)
    calls = []
    monkeypatch.setattr('app.shared.community.task_selector',
                         lambda task_key, **kw: calls.append((task_key, kw)))

    with web_ctx(app, s.user):
        remove_mod_from_community(s.community.id, target.id, SRC_WEB)
        flashed = get_flashed_messages(with_categories=True)

    assert flashed == [('warning', 'That user is not a moderator of this community.')]
    assert db.session.query(ModLog).filter_by(
        action='remove_mod', target_user_id=target.id).count() == 0
    assert db.session.query(CommunityMember).filter_by(
        user_id=target.id, community_id=s.community.id).one().is_moderator is False
    assert calls == []


def test_remove_mod_from_community_bystander_community_member_is_refused(
        app, db_session, monkeypatch):
    """`:622`'s `CommunityMember.community_id == community_id` predicate,
    isolated: the target is a moderator (even an owner) of `_seed()`'s
    bystander community (`:186-206`) but holds no row at all in `s.community`,
    the community under test. Without the `community_id` filter, the lookup
    at `:620-624` would find the target's bystander-community row -- it
    matches on `user_id` and satisfies the `is_moderator`/`is_owner` `or_` --
    and treat it as if it belonged to `s.community`. `tests/README.md` fact
    75 cause 2, read on its own terms: "the excluded set is empty under every
    fixture in the file... Fixable" -- no test before this one ever gave a
    user membership in a second, distinct community, so a mutant dropping
    `community_id` from the filter was unkillable, a gap rather than an
    equivalence (NOT cause 3: nothing here is implied by anything else in the
    filter; NOT cause 6, which is expressly not about a clause at all).

    The target's bystander-community row is left completely untouched
    (`is_moderator` stays True) and `s.community` gets no `ModLog` row --
    the two assertions that would catch a mutant treating the bystander row
    as the target's membership in `s.community`.
    """
    s = _seed()
    from app.models import Community
    bystander = db.session.query(Community).filter_by(name='bystander').one()
    owner = make_community_member(s.user, s.community, is_moderator=False)
    owner.is_owner = True
    db.session.commit()
    target = make_user(s.instance, 'bob', local=True)
    bystander_member = make_community_member(target, bystander, is_moderator=True)
    bystander_member.is_owner = True
    db.session.commit()
    calls = []
    monkeypatch.setattr('app.shared.community.task_selector',
                         lambda task_key, **kw: calls.append((task_key, kw)))

    from flask import get_flashed_messages

    with web_ctx(app, s.user):
        remove_mod_from_community(s.community.id, target.id, SRC_WEB)
        flashed = get_flashed_messages(with_categories=True)

    assert flashed == [('warning', 'That user is not a moderator of this community.')]
    assert db.session.query(ModLog).filter_by(action='remove_mod').count() == 0
    assert bystander_member.is_moderator is True
    assert bystander_member.is_owner is True
    assert db.session.query(CommunityMember).filter_by(
        user_id=target.id, community_id=s.community.id).count() == 0
    assert calls == []


def test_remove_mod_from_community_refuses_to_strip_the_last_owner(
        app, db_session, monkeypatch):
    """Task 6's guard: `existing_member.is_owner and community.num_owners()
    == 1` refuses at the top of `:625`'s branch, before `:634-635` ever
    clears the flags, matching the route's own check at
    `app/community/routes.py:1477-1478` -- the wording and `'error'` category
    are copied from there deliberately, so the two paths say the same thing.

    FORMERLY A PIN. Until Task 6, `:624` (now `:635`) set
    `existing_member.is_owner = False` with no guard at all, so this same
    self-removal by a community's sole owner silently succeeded and left the
    community with zero owners -- an invariant the route-based path has
    always refused. The load-bearing assertion is `community.num_owners()`
    staying at 1 afterwards -- naming the invariant the guard protects (a
    community must keep an owner), not merely the flag that would have
    flipped -- per `app/models.py:762`'s `num_owners()`.

    The actor is the sole owner AND an admin, removing themselves: `:617`
    needs the caller to be owner-or-admin, and making the actor a site admin
    (via `_make_site_admin`) means the guard passes on that operand alone
    regardless of the self-targeting removal that follows. The row is seeded
    with the `is_moderator` COLUMN False and `is_owner` set True directly
    afterward -- per this file's REDUNDANT DISJUNCT comment, `is_owner(user)`
    implies the `is_moderator(user)` METHOD (an OR-based membership check
    over `moderators()`), not the column, so this row is this community's
    only owner while the column below stays False, untouched by the guard.
    """
    from flask import get_flashed_messages

    s = _seed()
    _make_site_admin(s.user)
    owner_member = make_community_member(s.user, s.community, is_moderator=False)
    owner_member.is_owner = True
    db.session.commit()
    assert s.community.num_owners() == 1
    calls = []
    monkeypatch.setattr('app.shared.community.task_selector',
                         lambda task_key, **kw: calls.append((task_key, kw)))

    with web_ctx(app, s.user):
        remove_mod_from_community(s.community.id, s.user.id, SRC_WEB)
        flashed = get_flashed_messages(with_categories=True)

    assert flashed == [('error', 'A community must have one or more owners. '
                                  'Make someone else an owner before removing '
                                  'this owner.')]
    assert s.community.num_owners() == 1
    assert owner_member.is_owner is True
    assert owner_member.is_moderator is False
    assert db.session.query(ModLog).filter_by(action='remove_mod').count() == 0
    assert calls == []


def test_remove_mod_from_community_api_refuses_to_strip_the_last_owner(
        app, db_session, monkeypatch):
    """Same guard as above, through `:610-611`'s SRC_API arm: `:630`'s
    `raise Exception(msg)` fires instead of the web flash, before any flag
    is cleared, ModLog write happens, or task fires. `community.num_owners()`
    staying at 1 is the load-bearing assertion, matching the web test above.
    """
    s = _seed()
    _make_site_admin(s.user)
    owner_member = make_community_member(s.user, s.community, is_moderator=False)
    owner_member.is_owner = True
    db.session.commit()
    assert s.community.num_owners() == 1
    calls = []
    monkeypatch.setattr('app.shared.community.task_selector',
                         lambda task_key, **kw: calls.append((task_key, kw)))

    with pytest.raises(Exception, match='one or more owners'):
        remove_mod_from_community(s.community.id, s.user.id, SRC_API, bearer(s.user))

    assert s.community.num_owners() == 1
    assert owner_member.is_owner is True
    assert owner_member.is_moderator is False
    assert db.session.query(ModLog).filter_by(action='remove_mod').count() == 0
    assert calls == []


def test_remove_mod_from_community_a_plain_moderator_is_removed_from_a_one_owner_community(
        app, db_session, monkeypatch):
    """The guard's FIRST operand alone: `community.num_owners() == 1` is
    TRUE (someone else is the community's sole owner), but the removal
    target is a plain moderator (`is_owner=False`), so `existing_member.
    is_owner` is False and the guard does not fire -- the removal proceeds.

    Without `existing_member.is_owner and`, a mutant reading just
    `community.num_owners() == 1` would refuse this removal too, which is
    wrong: removing a plain moderator can never take a community's last
    owner away. That wrongful refusal is what makes this test kill that
    mutant.
    """
    from flask import get_flashed_messages

    s = _seed()
    owner = make_community_member(s.user, s.community, is_moderator=False)
    owner.is_owner = True
    db.session.commit()
    target = make_user(s.instance, 'bob', local=True)
    target_member = make_community_member(target, s.community, is_moderator=True)
    assert s.community.num_owners() == 1
    calls = []
    monkeypatch.setattr('app.shared.community.task_selector',
                         lambda task_key, **kw: calls.append((task_key, kw)))

    with web_ctx(app, s.user):
        remove_mod_from_community(s.community.id, target.id, SRC_WEB)
        flashed = get_flashed_messages()

    assert flashed == ['Moderator removed']
    assert target_member.is_moderator is False
    assert target_member.is_owner is False
    assert s.community.num_owners() == 1
    modlog_row = db.session.query(ModLog).filter_by(action='remove_mod').one()
    assert modlog_row.target_user_id == target.id
    assert calls == [('remove_mod', {'user_id': s.user.id, 'mod_id': target.id,
                                     'community_id': s.community.id})]


def test_delete_community_web_owner_deletes_and_returns_nothing(app, db_session, monkeypatch):
    """The web arm of a permitted delete: the community is soft-deleted and the
    task selected, but nothing is returned (the route does its own flash and
    redirect), unlike the API arm which returns the caller's id."""
    s = _seed()
    member = make_community_member(s.user, s.community, is_moderator=False)
    member.is_owner = True
    db.session.commit()
    calls = []
    monkeypatch.setattr('app.shared.community.task_selector',
                        lambda task_key, **kw: calls.append((task_key, kw)))

    with web_ctx(app, s.user):
        returned = delete_community(s.community.id, SRC_WEB)

    assert returned is None
    assert s.community.banned is True
    assert calls == [('delete_community', {'user_id': s.user.id, 'community_id': s.community.id})]


def test_restore_community_web_owner_restores_and_returns_nothing(app, db_session, monkeypatch):
    """The web arm of a permitted restore: the ban is lifted and the task
    selected, with no return value for the route to consume."""
    s = _seed()
    s.community.banned = True
    db.session.commit()
    member = make_community_member(s.user, s.community, is_moderator=False)
    member.is_owner = True
    db.session.commit()
    calls = []
    monkeypatch.setattr('app.shared.community.task_selector',
                        lambda task_key, **kw: calls.append((task_key, kw)))

    with web_ctx(app, s.user):
        returned = restore_community(s.community.id, SRC_WEB)

    assert returned is None
    assert s.community.banned is False
    assert calls == [('restore_community', {'user_id': s.user.id, 'community_id': s.community.id})]
