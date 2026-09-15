"""`block_domain` and `unblock_domain` (app/shared/domain.py:9-51).

Both were at 14.000% before this file: 27 statements and 16 arcs missing.

These are structural twins of block_another_user/unblock_another_user
(app/shared/user.py:20-87), which sub-project 43 closed in
tests/test_shared_user_blocks.py -- the src fork, bearer, web_ctx and the
id-1 burn all transfer. What does NOT transfer is the user-side guards:
there is no self-block check and no admin/staff check here, and an unknown
domain is a silent no-op that still returns user_id on the API arm. Those
three are registered findings, not defects this round fixes, and the tests
below pin them as they are.
"""
from types import SimpleNamespace

from app import db
from app.constants import SRC_API, SRC_WEB
from app.models import Domain, DomainBlock
from app.shared.domain import block_domain, unblock_domain
from tests.factories import bearer, make_instance, make_user, web_ctx


def _seed_blocker():
    """A local user who is not id 1, and a Domain row to block.

    The first user minted in any test is id 1 deterministically --
    tests/conftest.py:131-132 runs `SELECT setval(c.oid, 1, false)` over every
    sequence after each test -- and `User.is_admin` (app/models.py:1259-1261)
    returns True for id 1 regardless of roles. Nothing in THIS module reads
    is_admin, but the burn is kept so a later test added to this file cannot
    inherit the trap silently. tests/test_shared_reply_make.py:247 is the
    precedent.
    """
    instance = make_instance('remote.example')
    burn = make_user(instance, 'burn-the-id-1-seat', local=True)
    assert burn.id == 1, 'the id-1 admin trap moved; re-derive before trusting this'

    blocker = make_user(instance, 'blocker', local=True)
    domain = Domain(name='spam.example', banned=False)
    db.session.add(domain)
    db.session.commit()
    return SimpleNamespace(blocker=blocker, domain=domain)


def test_block_domain_api_creates_the_block_and_returns_the_blocker(app, db_session):
    s = _seed_blocker()

    returned = block_domain('spam.example', SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(DomainBlock).filter_by(
        domain_id=s.domain.id, user_id=s.blocker.id).count() == 1


def test_block_domain_web_creates_the_block_and_returns_none(app, db_session):
    """:29 returns None and leaves the flash to its caller.

    Both the return value and the row are asserted: a function that created
    nothing would also return None.
    """
    s = _seed_blocker()

    with web_ctx(app, s.blocker):
        returned = block_domain('spam.example', SRC_WEB)

    assert returned is None
    assert db.session.query(DomainBlock).filter_by(
        domain_id=s.domain.id, user_id=s.blocker.id).count() == 1


def test_block_domain_is_idempotent(app, db_session):
    """:19's `if not existing_block` -- the false arm.

    The second call must not add a second row and must still return the
    blocker's id from :27 rather than falling out early.
    """
    s = _seed_blocker()
    block_domain('spam.example', SRC_API, bearer(s.blocker))

    returned = block_domain('spam.example', SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(DomainBlock).count() == 1


def test_block_domain_silently_does_nothing_for_an_unknown_domain(app, db_session):
    """PINS A DEFECT. :17's false arm.

    No Domain row named 'nosuch.example' exists, so nothing is written -- and
    the API arm still returns user_id at :27, indistinguishable from a
    successful block. A caller cannot tell the two apart. Registered rather
    than fixed: this round's production budget is the auth dedent.
    """
    s = _seed_blocker()

    returned = block_domain('nosuch.example', SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(DomainBlock).count() == 0


def test_block_domain_blocks_only_for_the_calling_user(app, db_session):
    """:18's filter is on BOTH domain_id and user_id.

    A second user's block of the same domain is seeded so that a lookup
    narrowed to domain_id alone would find it, conclude a block already
    exists, and skip :20-24. With one block row that fault is invisible.
    """
    s = _seed_blocker()
    bystander = make_user(make_instance('other.example'), 'bystander', local=True)
    db.session.add(DomainBlock(domain_id=s.domain.id, user_id=bystander.id))
    db.session.commit()

    block_domain('spam.example', SRC_API, bearer(s.blocker))
    db.session.expire_all()

    owners = {b.user_id for b in db.session.query(DomainBlock).all()}
    assert owners == {s.blocker.id, bystander.id}


def test_unblock_domain_api_removes_the_block(app, db_session):
    s = _seed_blocker()
    db.session.add(DomainBlock(domain_id=s.domain.id, user_id=s.blocker.id))
    db.session.commit()

    returned = unblock_domain('spam.example', SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(DomainBlock).count() == 0


def test_unblock_domain_web_removes_the_block_and_returns_none(app, db_session):
    s = _seed_blocker()
    db.session.add(DomainBlock(domain_id=s.domain.id, user_id=s.blocker.id))
    db.session.commit()

    with web_ctx(app, s.blocker):
        returned = unblock_domain('spam.example', SRC_WEB)

    assert returned is None
    assert db.session.query(DomainBlock).count() == 0


def test_unblock_domain_leaves_another_users_block_alone(app, db_session):
    """:41's filter is on BOTH columns, in the delete direction.

    The bystander's row is seeded first deliberately, so that a mutant
    narrowing the filter to domain_id alone would call .first() and find
    the bystander's row (inserted first in the heap), then incorrectly
    delete it. Reordering this deliberately to put blocker first would
    silently defeat the test, making the mutant survive.
    """
    s = _seed_blocker()
    bystander = make_user(make_instance('other.example'), 'bystander', local=True)
    db.session.add(DomainBlock(domain_id=s.domain.id, user_id=bystander.id))
    db.session.add(DomainBlock(domain_id=s.domain.id, user_id=s.blocker.id))
    db.session.commit()

    unblock_domain('spam.example', SRC_API, bearer(s.blocker))
    db.session.expire_all()

    owners = {b.user_id for b in db.session.query(DomainBlock).all()}
    assert owners == {bystander.id}


def test_unblock_domain_is_idempotent(app, db_session):
    """:42's `if existing_block` -- the false arm. No block exists, so the
    body is skipped and :49 still returns the caller's id."""
    s = _seed_blocker()

    returned = unblock_domain('spam.example', SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(DomainBlock).count() == 0


def test_unblock_domain_silently_does_nothing_for_an_unknown_domain(app, db_session):
    """PINS A DEFECT. :40's false arm, the unblock twin of :17's."""
    s = _seed_blocker()

    returned = unblock_domain('nosuch.example', SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(DomainBlock).count() == 0
