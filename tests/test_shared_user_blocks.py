"""`block_another_user` and `unblock_another_user` (app/shared/user.py:20-87).

Both functions were at literally zero coverage before this file: 24
statements / 14 arcs and 16 / 10 respectively, every one missing.

THE ROLE TRAP. `block_another_user:33-35` reads
`SELECT role_id FROM "user_role" WHERE user_id = :person_id` with `.scalar()`
and compares the result against the INTEGERS ROLE_ADMIN (4) and ROLE_STAFF
(3) from app/constants.py:80-81. tests/factories.py:365's `grant_permission`
mints a fresh Role per call with a sequential id, and tests/conftest.py:131
resets every sequence between tests -- so the third or fourth
`grant_permission` call in a test would make its subject staff or admin BY
ID, refusing a block for a reason the test never intended. Every role in this
file is therefore created with an EXPLICIT id, and named something that is
neither 'Admin' nor 'Staff' so that `User.is_admin()`'s name path
(app/models.py:1263) cannot satisfy an assertion the id comparison was
supposed to carry.
"""
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app import db
from app.constants import NOTIF_USER, ROLE_ADMIN, ROLE_STAFF, SRC_API, SRC_WEB
from app.models import NotificationSubscription, Role, UserBlock, user_role
from app.shared.user import block_another_user, unblock_another_user
from tests.factories import bearer, make_instance, make_user, web_ctx


def _seed_blockers():
    """Two local users, neither of them id 1.

    The first user minted in any test is id 1, deterministically:
    tests/conftest.py:131-132 runs `SELECT setval(c.oid, 1, false)` over every
    sequence after every test. `User.is_admin` (app/models.py:1259-1261)
    returns True for id 1 regardless of roles. Nothing in THIS file reads
    is_admin -- block_another_user compares role ids, not names -- but the
    burn is kept so that a later test added to this file cannot inherit the
    trap silently. tests/test_shared_reply_make.py:247 is the precedent.
    """
    instance = make_instance('remote.example')
    burn = make_user(instance, 'burn-the-id-1-seat', local=True)
    assert burn.id == 1, 'the id-1 admin trap moved; re-derive before trusting this'

    blocker = make_user(instance, 'blocker', local=True)
    target = make_user(instance, 'target', local=True)
    db.session.commit()
    return SimpleNamespace(blocker=blocker, target=target)


def _give_role_with_id(user, role_id):
    """Put `user` in `user_role` against a role whose id is exactly role_id.

    `user_role.role_id` is a foreign key to `role.id` (app/models.py:937), so
    the Role row has to exist. The name deliberately avoids 'Admin' and
    'Staff' -- block_another_user reads the ID, and a name that also satisfies
    User.is_admin() would let a future assertion pass for the wrong reason.
    """
    db.session.add(Role(id=role_id, name=f'role-with-id-{role_id}', weight=0))
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=user.id, role_id=role_id))
    db.session.commit()


def test_block_another_user_api_creates_the_block_and_returns_the_blocker(app, db_session):
    s = _seed_blockers()

    returned = block_another_user(s.target.id, SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(UserBlock).filter_by(
        blocker_id=s.blocker.id, blocked_id=s.target.id).count() == 1


def test_block_another_user_web_creates_the_block_and_returns_none(app, db_session):
    """The web arm returns None and leaves the flash to its caller (:58).

    Both the return value and the row are asserted: a function that created
    nothing would also return None.
    """
    s = _seed_blockers()

    with web_ctx(app, s.blocker):
        returned = block_another_user(s.target.id, SRC_WEB)

    assert returned is None
    assert db.session.query(UserBlock).filter_by(
        blocker_id=s.blocker.id, blocked_id=s.target.id).count() == 1


def test_block_another_user_deletes_the_targets_subscription_to_the_blocker(app, db_session):
    """:46-48's raw DELETE, which is easy to mistake for symmetry.

    Read the parameters: `entity_id = :current_user AND user_id = :user_id`
    with current_user bound to the BLOCKER's id and user_id to the TARGET's.
    So the row removed is the one where the TARGET subscribes to the BLOCKER
    -- not the blocker's own subscription to the target. Two subscriptions are
    seeded, in both directions, and only one may survive. With a single
    subscription this test would pass under a swapped binding.
    """
    s = _seed_blockers()
    db.session.add(NotificationSubscription(
        name='target follows blocker', user_id=s.target.id,
        entity_id=s.blocker.id, type=NOTIF_USER))
    db.session.add(NotificationSubscription(
        name='blocker follows target', user_id=s.blocker.id,
        entity_id=s.target.id, type=NOTIF_USER))
    db.session.commit()

    block_another_user(s.target.id, SRC_API, bearer(s.blocker))
    db.session.expire_all()

    survivors = {(n.user_id, n.entity_id)
                 for n in db.session.query(NotificationSubscription).all()}
    assert survivors == {(s.blocker.id, s.target.id)}


def test_block_another_user_is_idempotent(app, db_session):
    """:43's `if not existing_block` -- the false arc.

    The second call must not add a second row, and must still return the
    blocker's id from :56 rather than falling out of the function early.
    """
    s = _seed_blockers()
    block_another_user(s.target.id, SRC_API, bearer(s.blocker))

    returned = block_another_user(s.target.id, SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(UserBlock).filter_by(
        blocker_id=s.blocker.id, blocked_id=s.target.id).count() == 1


def test_block_another_user_api_refuses_self(app, db_session):
    s = _seed_blockers()

    with pytest.raises(Exception, match='cannot_block_self'):
        block_another_user(s.blocker.id, SRC_API, bearer(s.blocker))

    assert db.session.query(UserBlock).count() == 0


def test_block_another_user_web_refuses_self_without_raising(app, db_session):
    """The web arm flashes and returns (:30-31) where the API arm raises.

    The row count is the assertion that matters: a `return` that skipped the
    flash would also produce no rows, so the flash is checked too, through the
    session's `_flashes`.
    """
    s = _seed_blockers()

    with web_ctx(app, s.blocker):
        from flask import session
        returned = block_another_user(s.blocker.id, SRC_WEB)
        flashed = [message for _category, message in session.get('_flashes', [])]

    assert returned is None
    assert flashed == ['You cannot block yourself.']
    assert db.session.query(UserBlock).count() == 0


def test_block_another_user_api_refuses_an_admin_by_role_id(app, db_session):
    """ROLE_ADMIN is 4 and the comparison at :35 is against that INTEGER.

    The role is named 'role-with-id-4', not 'Admin', so nothing here can be
    satisfied by User.is_admin()'s name path.
    """
    s = _seed_blockers()
    _give_role_with_id(s.target, ROLE_ADMIN)

    with pytest.raises(Exception, match='cannot_block_admin_or_staff'):
        block_another_user(s.target.id, SRC_API, bearer(s.blocker))

    assert db.session.query(UserBlock).count() == 0


def test_block_another_user_api_refuses_staff_by_role_id(app, db_session):
    """The second operand of :35's `or`. Without this test that operand could
    be deleted outright and every other test in this file would stay green."""
    s = _seed_blockers()
    _give_role_with_id(s.target, ROLE_STAFF)

    with pytest.raises(Exception, match='cannot_block_admin_or_staff'):
        block_another_user(s.target.id, SRC_API, bearer(s.blocker))

    assert db.session.query(UserBlock).count() == 0


def test_block_another_user_allows_a_target_holding_an_unprivileged_role(app, db_session):
    """The positive control for the role check.

    Without this, every role-check test above would also pass against a
    version of :35 that refused EVERY user holding any role at all.
    """
    s = _seed_blockers()
    _give_role_with_id(s.target, 2)

    returned = block_another_user(s.target.id, SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(UserBlock).count() == 1


def test_block_another_user_web_refuses_an_admin_without_raising(app, db_session):
    s = _seed_blockers()
    _give_role_with_id(s.target, ROLE_ADMIN)

    with web_ctx(app, s.blocker):
        from flask import session
        returned = block_another_user(s.target.id, SRC_WEB)
        flashed = [message for _category, message in session.get('_flashes', [])]

    assert returned is None
    assert flashed == ['You cannot block admin or staff.']
    assert db.session.query(UserBlock).count() == 0


def test_unblock_another_user_api_removes_the_block(app, db_session):
    s = _seed_blockers()
    db.session.add(UserBlock(blocker_id=s.blocker.id, blocked_id=s.target.id))
    db.session.commit()

    returned = unblock_another_user(s.target.id, SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(UserBlock).count() == 0


def test_unblock_another_user_web_removes_the_block_and_returns_none(app, db_session):
    s = _seed_blockers()
    db.session.add(UserBlock(blocker_id=s.blocker.id, blocked_id=s.target.id))
    db.session.commit()

    with web_ctx(app, s.blocker):
        returned = unblock_another_user(s.target.id, SRC_WEB)

    assert returned is None
    assert db.session.query(UserBlock).count() == 0


def test_unblock_another_user_leaves_someone_elses_block_alone(app, db_session):
    """:74's filter is on BOTH blocker_id and blocked_id.

    A third user's block of the same target is seeded so that a filter
    narrowed to blocked_id alone would delete a row it must not touch. With
    only one block row present, that fault is invisible.
    """
    s = _seed_blockers()
    bystander = make_user(make_instance('other.example'), 'bystander', local=True)
    db.session.add(UserBlock(blocker_id=s.blocker.id, blocked_id=s.target.id))
    db.session.add(UserBlock(blocker_id=bystander.id, blocked_id=s.target.id))
    db.session.commit()

    unblock_another_user(s.target.id, SRC_API, bearer(s.blocker))
    db.session.expire_all()

    remaining = {(b.blocker_id, b.blocked_id)
                 for b in db.session.query(UserBlock).all()}
    assert remaining == {(bystander.id, s.target.id)}


def test_unblock_another_user_is_idempotent(app, db_session):
    """:75's `if existing_block` -- the false arc. No block exists, so the
    body is skipped and :84 still returns the caller's id."""
    s = _seed_blockers()

    returned = unblock_another_user(s.target.id, SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(UserBlock).count() == 0


def test_unblock_another_user_api_refuses_self(app, db_session):
    s = _seed_blockers()

    with pytest.raises(Exception, match='cannot_unblock_self'):
        unblock_another_user(s.blocker.id, SRC_API, bearer(s.blocker))


def test_unblock_another_user_web_refuses_self_without_raising(app, db_session):
    s = _seed_blockers()

    with web_ctx(app, s.blocker):
        from flask import session
        returned = unblock_another_user(s.blocker.id, SRC_WEB)
        flashed = [message for _category, message in session.get('_flashes', [])]

    assert returned is None
    assert flashed == ['You cannot unblock yourself.']
