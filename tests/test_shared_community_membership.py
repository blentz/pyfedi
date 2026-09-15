"""`block_community` and `unblock_community` (app/shared/community.py:85-118).

Both are at 0.0% before this file: 22 statements and 12 arcs missing.

These are the fifth instance of a shape this campaign has closed four times --
block_another_user, block_domain, block_remote_instance, and now this.
The src fork, bearer, web_ctx, the id-1 burn, and the deliberate bystander-first
seeding order all transfer from tests/test_shared_domain.py.
"""
from types import SimpleNamespace

from app import db
from app.constants import SRC_API, SRC_WEB
from app.models import CommunityBlock
from app.shared.community import block_community, unblock_community
from tests.factories import bearer, make_community, make_instance, make_user, web_ctx


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
