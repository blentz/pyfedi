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

THE DIVERGENCE, REGISTERED NOT FIXED: `delete_community:493` reads community
with `db.session.query(Community).get(community_id)`, which returns `None`
for an absent id, so `:494`'s `community.is_owner(user)` raises
`AttributeError` on `None`. `restore_community:522` reads with
`.filter_by(id=community_id).one()`, which raises `NoResultFound` directly
for the same absent id. The two tests near the bottom of this file pin
today's actual, divergent behaviour rather than papering over it; this
round's production budget is spent elsewhere (on remove_mod_from_community's
two defects), so the divergence stands as found.
"""
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import NoResultFound

from app import db
from app.constants import SRC_API, SRC_WEB
from app.models import CommunityMember, ModLog
from app.shared.community import add_mod_to_community, delete_community, restore_community
from tests.factories import (bearer, make_community, make_community_member, make_instance,
                             make_user, web_ctx)

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


def test_delete_community_web_moderator_alone_deletes_and_returns_none(app, db_session, monkeypatch):
    """`:488`'s else arm (`current_user`), `:494`'s guard passing through its
    SECOND operand alone (is_moderator=True, is_owner=False, no admin role --
    a plain moderator, the shape federated moderators are created in, and the
    one case of the three that is genuinely separable; see the REDUNDANT
    DISJUNCT comment above for why operand one never is), and `:512`'s false
    arm: a non-API src falls off the end of the function and returns `None`
    rather than the user's id.

    See the previous test's docstring for why `task_selector` is patched on
    `app.shared.community` and what the argument assertion is defending
    against.
    """
    s = _seed()
    make_community_member(s.user, s.community, is_moderator=True)
    calls = []
    monkeypatch.setattr('app.shared.community.task_selector',
                         lambda task_key, **kw: calls.append((task_key, kw)))

    with web_ctx(app, s.user):
        returned = delete_community(s.community.id, SRC_WEB)

    assert returned is None
    assert s.community.banned is True
    assert calls == [('delete_community', {'user_id': s.user.id, 'community_id': s.community.id})]


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


def test_delete_community_missing_id_raises_AttributeError_not_NoResultFound(app, db_session):
    """`:493` uses `.get()`, which returns `None` for an absent row, so
    `:494`'s `community.is_owner(user)` raises `AttributeError` on `None`.

    This pins the divergence rather than the intended behaviour. Registered
    as a finding this round, NOT fixed -- the production budget is spent on
    remove_mod_from_community's two defects. Asserting the actual behaviour
    keeps the test honest about what the code does today; see the module
    docstring's DIVERGENCE note and this file's companion test below for
    `restore_community`'s `.one()` twin at `:522`.
    """
    s = _seed()

    with web_ctx(app, s.user):
        with pytest.raises(AttributeError):
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


def test_restore_community_web_moderator_alone_restores_and_returns_none(app, db_session, monkeypatch):
    """`:517`'s else arm, `:523`'s guard passing through its SECOND operand
    alone (plain moderator, not owner, not staff -- the one of the three
    that is genuinely separable; see the REDUNDANT DISJUNCT comment above),
    and `:536`'s false arm: a non-API src returns `None`.

    See the previous test's docstring for the `task_selector` patch and
    argument assertion.
    """
    s = _seed()
    s.community.banned = True
    db.session.commit()
    make_community_member(s.user, s.community, is_moderator=True)
    calls = []
    monkeypatch.setattr('app.shared.community.task_selector',
                         lambda task_key, **kw: calls.append((task_key, kw)))

    with web_ctx(app, s.user):
        returned = restore_community(s.community.id, SRC_WEB)

    assert returned is None
    assert s.community.banned is False
    assert calls == [('restore_community', {'user_id': s.user.id, 'community_id': s.community.id})]


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


def test_restore_community_missing_id_raises_NoResultFound(app, db_session):
    """`:522`'s `.one()` on an absent row raises `NoResultFound` directly --
    the divergence's other half. See
    test_delete_community_missing_id_raises_AttributeError_not_NoResultFound
    above and the module docstring's DIVERGENCE note for the full pairing.
    """
    s = _seed()

    with web_ctx(app, s.user):
        with pytest.raises(NoResultFound):
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
