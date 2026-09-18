"""app/shared/feed.py's lifecycle layer: join_feed, leave_feed, make_feed and
delete_feed -- the four functions that sit on top of the wiring sub-project 49
covered.

MEASUREMENT BASIS. Before this file existed, leave_feed and make_feed had never
been executed by any test: one executed line each, their own def statements at
:113 and :152. join_feed and delete_feed were executed only by eleven tests in
tests/test_redirect_back.py (BackSiteContract, TestFeedDeleteRedirect) whose
subject is back()'s referrer policy, not feeds. Measured with per-test coverage
contexts over the whole suite, --cov=app.shared.feed --cov-context=test, on the
run that reported 5219 passed, 3 skipped, 6 subtests passed in 342.05s. Every
figure in this file's comments is stated with its basis.

THE NAME COLLISION. tests/factories.py:156 defines make_feed and
app/shared/feed.py:152 defines a different make_feed. This is the round that
tests the production one, so the factory is imported as make_feed_factory and
the bare name always means production.

SESSION DETACHMENT. join_feed ends with `finally: db.session.remove()`
(app/shared/feed.py:109-110), which detaches every ORM object the caller holds.
Re-query after calling it; do not read a seed object.
"""
import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy.orm.exc import NoResultFound

from app import db
from app.constants import SRC_API, SRC_WEB, SUBSCRIPTION_PENDING
from app.models import (Community, CommunityMember, Feed, FeedItem, FeedJoinRequest,
                        FeedMember, User)
from app.shared.feed import delete_feed, join_feed, leave_feed, make_feed
from tests.factories import (make_community, make_community_member, make_feed_item,
                             make_feed_join_request, make_feed_member, make_instance,
                             make_local_feed, make_user, web_ctx)
from tests.factories import make_feed as make_feed_factory


def _burn_a_seed():
    """Consume id 1 so nothing under test inherits the id-1 admin trap.

    app/models.py:1259-1261 returns True from is_admin() for id 1, and
    tests/conftest.py:131-132 resets every sequence after each test, so the
    first User minted in a test is deterministically id 1. The assert is live:
    if that behaviour changes this fails loudly rather than handing a later row
    id 1 and quietly making it an admin.
    """
    instance = make_instance('burn.piefed.local')
    burn = make_user(instance, 'burnseat')
    assert burn.id == 1
    return instance


def _seed():
    """Rows for the lifecycle layer, with every id deliberately distinct.

    These functions carry feed_id, user_id and community_id together, and
    sub-project 48 shipped three blind assertions because its seed minted rows
    in lockstep (D653, tests/README.md fact 272). Community and Feed are
    separate tables with separate sequences, and Users get their own besides,
    so a single bystander per table leaves community.id, feed.id and a user id
    all sitting at 2. The bystanders below are counted rather than guessed:
    three users (burn, owner, member) take 1-3, so the real Community is minted
    fourth and the real Feed fifth, and no id can coincide. The separation is
    asserted live rather than assumed.
    """
    instance = _burn_a_seed()
    owner = make_user(instance, 'feedowner')
    member = make_user(instance, 'feedmember')
    make_community(name='bystandercommunity1')
    make_community(name='bystandercommunity2')
    make_community(name='bystandercommunity3')
    community = make_community(name='infeedcommunity')
    make_feed_factory(instance, name='bystanderfeed')
    make_feed_factory(instance, name='idparitybreaker1')
    make_feed_factory(instance, name='idparitybreaker2')
    make_feed_factory(instance, name='idparitybreaker3')
    feed = make_feed_factory(instance, name='lifecyclefeed')
    feed.user_id = owner.id
    db.session.commit()

    assert len({community.id, feed.id, owner.id, member.id}) == 4
    return SimpleNamespace(instance=instance, owner=owner, member=member,
                           community=community, feed=feed)


# --------------------------------------------------------------------------
# Task 1: P1 and P2 -- leave_feed's two divergences from its web twin.
# --------------------------------------------------------------------------


def test_leave_feed_crashes_on_a_community_the_user_never_joined(app, db_session):
    """PIN (P1): leave_feed calls leave_community for every community in the
    feed, including ones the user never joined, and leave_community opens with
    .one() (app/shared/community.py:59), which raises when there is no row.

    feed_auto_leave defaults True (app/models.py:1043), so this is the ordinary
    path for anyone who joined the feed with feed_auto_follow off or who left
    one of its communities by hand.

    The web twin guards exactly this at app/feed/routes.py:637-639:
    `membership = CommunityMember.query.filter_by(...).first()` then
    `if membership and membership.joined_via_feed:`.
    """
    s = _seed()
    make_feed_member(s.member, s.feed)
    make_feed_item(s.feed, s.community)
    s.member.feed_auto_leave = True
    db.session.commit()
    assert CommunityMember.query.filter_by(user_id=s.member.id,
                                           community_id=s.community.id).first() is None

    with web_ctx(app, s.member):
        with patch('app.shared.feed.task_selector'):
            with pytest.raises(NoResultFound):
                leave_feed(s.feed, SRC_WEB)


def test_leave_feed_leaves_the_join_request_row_behind(app, db_session):
    """PIN (P2): leave_feed deletes the FeedMember row and nothing else, so a
    FeedJoinRequest survives -- and Feed.subscribed() (app/models.py:4224-4239)
    reads a surviving request as SUBSCRIPTION_PENDING.

    join_feed:37 only acts when feed_membership(...) == SUBSCRIPTION_NONMEMBER,
    so the stale row locks the user out of ever rejoining that feed. The
    consequence is asserted here, not just the residue: a stray row is untidy,
    a permanent PENDING is a user-facing defect.

    The web twin deletes it at app/feed/routes.py:630.
    """
    s = _seed()
    make_feed_member(s.member, s.feed)
    make_feed_join_request(s.member, s.feed)
    s.member.feed_auto_leave = False
    db.session.commit()

    with web_ctx(app, s.member):
        with patch('app.shared.feed.task_selector'):
            leave_feed(s.feed, SRC_WEB)

    assert FeedJoinRequest.query.filter_by(user_id=s.member.id,
                                           feed_id=s.feed.id).count() == 1
    assert Feed.query.get(s.feed.id).subscribed(s.member.id) == SUBSCRIPTION_PENDING
