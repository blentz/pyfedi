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
from app.shared.community import delete_community, restore_community
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
# This is tests/README.md fact 75, cause 6, "Redundant statement"
# (tests/README.md:3025-3045): the mutated clause's only observable effect
# (making the guard pass) is performed unconditionally, on every path where
# it would have mattered, by code that runs alongside it (`is_moderator`'s
# check over the same list). It is not cause 4 -- fact 75 has no cause 4(c)
# -- and not cause 8, which is scoped to a `try`/`except` whose body can
# never run; neither shape applies here.
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
