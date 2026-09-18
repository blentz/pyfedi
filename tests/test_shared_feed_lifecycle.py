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


def test_leave_feed_only_leaves_communities_the_user_joined_via_the_feed(app, db_session):
    """Was a PIN; INVERTED once the membership guard landed.

    ORIGINAL PINNED CLAIM, now false: "leave_feed calls leave_community for
    every community in the feed, so an ordinary unsubscribe raises
    NoResultFound (leave_community opens with .one(),
    app/shared/community.py:59) for any community the user never joined."

    The three communities here are the whole point, and each one distinguishes
    a different half of the guard:

    - never joined  -> the .first() half. Under the old code this row alone
      raised; under the new code it is skipped.
    - joined via the feed -> the call that must STILL fire. Without it "no
      exception" would be satisfied by a loop that called nothing at all.
    - joined on their own (joined_via_feed False) -> the joined_via_feed half.
      It is the only row that tells the two halves apart, and it is the
      behaviour change this fix deliberately makes: leave_feed no longer
      unsubscribes a user from communities they chose for themselves.

    feed_auto_leave is set explicitly rather than left to its True default
    (app/models.py:1043), so the test says which way it is testing.
    """
    s = _seed()
    never_joined = s.community
    joined_via_feed = make_community(name='joinedviafeed')
    joined_alone = make_community(name='joinedalone')
    make_feed_member(s.member, s.feed)
    for community in (never_joined, joined_via_feed, joined_alone):
        make_feed_item(s.feed, community)
    via = make_community_member(s.member, joined_via_feed)
    via.joined_via_feed = True
    make_community_member(s.member, joined_alone)  # joined_via_feed defaults False
    s.member.feed_auto_leave = True
    db.session.commit()

    assert CommunityMember.query.filter_by(user_id=s.member.id,
                                           community_id=never_joined.id).first() is None
    assert len({never_joined.id, joined_via_feed.id, joined_alone.id, s.member.id}) == 4

    with web_ctx(app, s.member):
        with patch('app.shared.feed.task_selector'), \
                patch('app.shared.feed.leave_community') as leave:
            leave_feed(s.feed, SRC_WEB)

    assert leave.call_count == 1
    assert leave.call_args.kwargs['community_id'] == joined_via_feed.id


def test_leave_feed_deletes_the_join_request_row(app, db_session):
    """Was a PIN; INVERTED once the FeedJoinRequest delete landed.

    ORIGINAL PINNED CLAIM, now false: "the request row survives, so
    Feed.subscribed() (app/models.py:4224-4239) reports SUBSCRIPTION_PENDING
    and join_feed:37, which only acts on SUBSCRIPTION_NONMEMBER, refuses every
    later attempt to rejoin."

    The second user's request row is the control. A delete missing its user_id
    filter passes any single-user version of this test, and that filter is the
    difference between unsubscribing one person and unsubscribing everybody
    waiting on the feed.
    """
    s = _seed()
    bystander = make_user(s.instance, 'otherpending')
    make_feed_member(s.member, s.feed)
    make_feed_join_request(s.member, s.feed)
    make_feed_join_request(bystander, s.feed)
    s.member.feed_auto_leave = False
    db.session.commit()

    with web_ctx(app, s.member):
        with patch('app.shared.feed.task_selector'):
            leave_feed(s.feed, SRC_WEB)

    assert FeedJoinRequest.query.filter_by(user_id=s.member.id,
                                           feed_id=s.feed.id).count() == 0
    assert FeedJoinRequest.query.filter_by(user_id=bystander.id,
                                           feed_id=s.feed.id).count() == 1
    assert Feed.query.get(s.feed.id).subscribed(s.member.id) != SUBSCRIPTION_PENDING
