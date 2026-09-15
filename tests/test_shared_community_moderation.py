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
user.is_admin_or_staff()):`. Three tests per function isolate each operand
as the SOLE reason the guard passes -- an owner who is not a moderator and
not staff, a moderator who is not an owner and not staff, and a site admin
who holds no CommunityMember row at all -- so that a mutant deleting any one
operand still has to survive the other two tests it cannot pass.

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
    """
    _burn_a_seed()
    instance = make_instance('test.piefed.local')
    user = make_user(instance, 'alice', local=True)
    community = make_community()
    return SimpleNamespace(instance=instance, user=user, community=community)


# `delete_community` (app/shared/community.py:487-513).


def test_delete_community_api_owner_alone_deletes_and_returns_user_id(app, db_session):
    """`:488`'s SRC_API arm, `:494`'s guard passing through its FIRST operand
    alone (is_owner=True, is_moderator=False, and no admin role granted), and
    `:496`'s false arm (the seeded community is local by default: `ap_id` is
    left `None`, so `Community.is_local` (`app/models.py:795-796`) short-
    circuits True through its own first operand). `:512`'s true arm returns
    the caller's id.
    """
    s = _seed()
    member = make_community_member(s.user, s.community, is_moderator=False)
    member.is_owner = True
    db.session.commit()

    returned = delete_community(s.community.id, SRC_API, bearer(s.user))

    assert returned == s.user.id
    assert s.community.banned is True


def test_delete_community_web_moderator_alone_deletes_and_returns_none(app, db_session):
    """`:488`'s else arm (`current_user`), `:494`'s guard passing through its
    SECOND operand alone (is_moderator=True, is_owner=False, no admin role),
    and `:512`'s false arm: a non-API src falls off the end of the function
    and returns `None` rather than the user's id.
    """
    s = _seed()
    make_community_member(s.user, s.community, is_moderator=True)

    with web_ctx(app, s.user):
        returned = delete_community(s.community.id, SRC_WEB)

    assert returned is None
    assert s.community.banned is True


def test_delete_community_admin_alone_without_membership_deletes(app, db_session):
    """`:494`'s guard passing through its THIRD operand alone: `s.user` holds
    no CommunityMember row at all -- `Community.moderators()`
    (`app/models.py:716-722`) returns an empty list for it, so `is_owner` and
    `is_moderator` (`:736-747`) are both False on their own -- and is instead
    a site admin via `_make_site_admin`, which grants a Role named exactly
    'Admin' so `User.is_admin()` (`app/models.py:1259-1265`) returns True and
    `is_admin_or_staff()` (`:1274-1275`) follows.
    """
    s = _seed()
    _make_site_admin(s.user)

    returned = delete_community(s.community.id, SRC_API, bearer(s.user))

    assert returned == s.user.id
    assert s.community.banned is True


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


def test_restore_community_api_owner_alone_restores_and_returns_user_id(app, db_session):
    """`:517`'s SRC_API arm, `:523`'s guard passing through its FIRST operand
    alone, `:525`'s false arm (the community stays local, same reasoning as
    delete_community's twin above), and `:536`'s true arm returning the
    caller's id. The community starts banned so the state change from `True`
    to `False` is real, not a no-op.
    """
    s = _seed()
    s.community.banned = True
    db.session.commit()
    member = make_community_member(s.user, s.community, is_moderator=False)
    member.is_owner = True
    db.session.commit()

    returned = restore_community(s.community.id, SRC_API, bearer(s.user))

    assert returned == s.user.id
    assert s.community.banned is False


def test_restore_community_web_moderator_alone_restores_and_returns_none(app, db_session):
    """`:517`'s else arm, `:523`'s guard passing through its SECOND operand
    alone, and `:536`'s false arm: a non-API src returns `None`.
    """
    s = _seed()
    s.community.banned = True
    db.session.commit()
    make_community_member(s.user, s.community, is_moderator=True)

    with web_ctx(app, s.user):
        returned = restore_community(s.community.id, SRC_WEB)

    assert returned is None
    assert s.community.banned is False


def test_restore_community_admin_alone_without_membership_restores(app, db_session):
    """`:523`'s guard passing through its THIRD operand alone, same shape as
    delete_community's admin-alone test above: no CommunityMember row, a
    site-admin Role instead.
    """
    s = _seed()
    s.community.banned = True
    db.session.commit()
    _make_site_admin(s.user)

    returned = restore_community(s.community.id, SRC_API, bearer(s.user))

    assert returned == s.user.id
    assert s.community.banned is False


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
