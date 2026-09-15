"""`block_remote_instance` and `unblock_remote_instance` (app/shared/site.py:11-49).

The module was at 86.957% before this file: 2 statements and 4 arcs missing.

ONE TEST HERE PINS A DEFECT. app/shared/site.py:14-19 refuses to block the
local instance -- the API arm raises at :17, the web arm flashes at :19 --
but the web arm does NOT return, so control falls to :21 and :23 creates the
block anyway. Both twins return: app/shared/user.py:29-31's
block_another_user flashes then returns, and app/shared/domain.py does the
same. That test asserts today's wrong behaviour and is INVERTED by the task
that adds the return. It says PINS A DEFECT in its docstring.

The defect is reachable through ordinary routes rather than only by a
crafted call: app/post/routes.py:1480 passes post.instance_id and
app/user/routes.py:881 passes user.instance_id, and app/shared/post.py:218
builds a Post with instance_id=user.instance_id, which is 1 for a local
user. Clicking "block instance" on a local post blocks your own instance.
"""
from types import SimpleNamespace

import pytest
from flask import get_flashed_messages

from app import db
from app.constants import SRC_API, SRC_WEB
from app.models import Instance, InstanceBlock
from app.shared.site import block_remote_instance, unblock_remote_instance
from tests.factories import bearer, make_instance, make_user, web_ctx


def _seed_blocker():
    """A local user who is not id 1, and a remote Instance that is not id 1.

    The first user minted in any test is id 1 deterministically --
    tests/conftest.py:131-132 runs `SELECT setval(c.oid, 1, false)` over every
    sequence after each test -- and `User.is_admin` (app/models.py:1259-1261)
    returns True for id 1 regardless of roles. Nothing in THIS module reads
    is_admin, but the burn is kept so a later test added to this file cannot
    inherit the trap silently.

    The burn ALSO makes the first Instance row id 1, which is what
    `block_remote_instance`'s :14 guard tests against -- so `local` below is
    genuinely the local instance and `remote` genuinely is not.
    """
    local = make_instance('test.piefed.local', software='piefed')
    assert local.id == 1, 'instance id 1 is the local instance; re-derive if this moved'

    burn = make_user(local, 'burn-the-id-1-seat', local=True)
    assert burn.id == 1, 'the id-1 admin trap moved; re-derive before trusting this'

    blocker = make_user(local, 'blocker', local=True)
    remote = make_instance('peer.example')
    db.session.commit()
    return SimpleNamespace(blocker=blocker, local=local, remote=remote)


def test_block_remote_instance_api_creates_the_block_and_returns_the_blocker(app, db_session):
    s = _seed_blocker()

    returned = block_remote_instance(s.remote.id, SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(InstanceBlock).filter_by(
        user_id=s.blocker.id, instance_id=s.remote.id).count() == 1


def test_block_remote_instance_web_creates_the_block_and_returns_none(app, db_session):
    """:32 returns None and leaves the flash to its caller.

    Both the return value and the row are asserted: a function that created
    nothing would also return None.
    """
    s = _seed_blocker()

    with web_ctx(app, s.blocker):
        returned = block_remote_instance(s.remote.id, SRC_WEB)

    assert returned is None
    assert db.session.query(InstanceBlock).filter_by(
        user_id=s.blocker.id, instance_id=s.remote.id).count() == 1


def test_block_remote_instance_is_idempotent(app, db_session):
    """:22's `if not existing` -- the false arm. The second call must not add
    a second row and must still return the blocker's id from :30."""
    s = _seed_blocker()
    block_remote_instance(s.remote.id, SRC_API, bearer(s.blocker))

    returned = block_remote_instance(s.remote.id, SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(InstanceBlock).count() == 1


def test_block_remote_instance_api_refuses_the_local_instance(app, db_session):
    """:16-17. The API arm raises and never reaches :21.

    THIS IS THE CONTROL, not a pin: it is the arm that already refuses
    correctly and must keep refusing after the web arm is fixed. The row
    count is asserted too, because a raise that happened AFTER the insert
    would still satisfy pytest.raises.
    """
    s = _seed_blocker()

    with pytest.raises(Exception, match='cannot block the local instance'):
        block_remote_instance(1, SRC_API, bearer(s.blocker))

    assert db.session.query(InstanceBlock).count() == 0


def test_block_remote_instance_web_blocks_the_local_instance_anyway(app, db_session):
    """PINS A DEFECT. :18-19 flashes the refusal and does NOT return, so :23
    creates the block the message just said was impossible.

    BOTH halves are asserted, and the second is the defect: the flash is what
    makes this look correct to a reader, and the row is what proves it is
    not. Asserting only the flash would pass against the fixed code too.

    THIS ASSERTION IS INVERTED by the task that adds the return.
    """
    s = _seed_blocker()

    with web_ctx(app, s.blocker):
        block_remote_instance(1, SRC_WEB)
        flashed = get_flashed_messages()

    assert flashed == ['You cannot block the local instance.']
    assert db.session.query(InstanceBlock).filter_by(
        user_id=s.blocker.id, instance_id=1).count() == 1


def test_unblock_remote_instance_api_removes_the_block(app, db_session):
    s = _seed_blocker()
    db.session.add(InstanceBlock(user_id=s.blocker.id, instance_id=s.remote.id))
    db.session.commit()

    returned = unblock_remote_instance(s.remote.id, SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(InstanceBlock).count() == 0


def test_unblock_remote_instance_web_removes_the_block_and_returns_none(app, db_session):
    s = _seed_blocker()
    db.session.add(InstanceBlock(user_id=s.blocker.id, instance_id=s.remote.id))
    db.session.commit()

    with web_ctx(app, s.blocker):
        returned = unblock_remote_instance(s.remote.id, SRC_WEB)

    assert returned is None
    assert db.session.query(InstanceBlock).count() == 0


def test_unblock_remote_instance_leaves_another_users_block_alone(app, db_session):
    """:38's filter is on BOTH user_id and instance_id.

    THE BYSTANDER'S ROW IS SEEDED FIRST, DELIBERATELY. `.first()` at :38 has
    no ORDER BY, so a heap scan over two rows inserted in one transaction
    returns them in insertion order. If the subject's row were seeded first, a
    mutant narrowing the filter to instance_id alone would return and delete
    that same row, and this assertion would pass either way -- false-witness
    mechanism (b). Seeded this way round, the mutant deletes the bystander's
    row and the assertion fails, which is the kill. Do not "tidy" the order.
    """
    s = _seed_blocker()
    bystander = make_user(s.local, 'bystander', local=True)
    db.session.add(InstanceBlock(user_id=bystander.id, instance_id=s.remote.id))
    db.session.add(InstanceBlock(user_id=s.blocker.id, instance_id=s.remote.id))
    db.session.commit()

    unblock_remote_instance(s.remote.id, SRC_API, bearer(s.blocker))
    db.session.expire_all()

    owners = {b.user_id for b in db.session.query(InstanceBlock).all()}
    assert owners == {bystander.id}


def test_unblock_remote_instance_is_idempotent(app, db_session):
    """:39's `if existing` -- the false arm. No block exists, so the body is
    skipped and :47 still returns the caller's id."""
    s = _seed_blocker()

    returned = unblock_remote_instance(s.remote.id, SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(InstanceBlock).count() == 0
