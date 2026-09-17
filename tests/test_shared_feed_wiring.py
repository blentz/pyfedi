"""app/shared/feed.py's wiring layer: the helpers, the announce tasks and the
two _feed_* functions that sit beneath make_feed/edit_feed/delete_feed.

MEASUREMENT BASIS. Before this file existed, NO test anywhere imported
app.shared.feed -- `/usr/bin/grep -rln "shared.feed\\|shared import feed" tests/`
returned nothing. Ten test files carry "feed" in the name and none of them
touched this module, so its 14.711% was import-time execution of def statements
and decorators, not coverage. Every figure in this file's comments is stated
with its basis.

THE NAME COLLISION. tests/factories.py:156 defines make_feed and
app/shared/feed.py:152 defines a different make_feed. The factory is imported
here as make_feed_factory so the bare name always means production, the same
resolution sub-project 48 used for make_community.
"""
import pytest
from types import SimpleNamespace
from unittest.mock import patch

from app import db
from app.models import Community, CommunityMember, Feed, FeedItem, User
from app.shared.feed import (_feed_add_community, _feed_remove_community,
                             announce_feed_add_remove_to_subscribers,
                             announce_feed_delete_to_subscribers,
                             existing_communities, form_communities_to_ids)
from tests.factories import (make_community, make_community_join_request,
                             make_community_member, make_feed_item,
                             make_feed_member, make_instance, make_user, web_ctx)
from tests.factories import make_feed as make_feed_factory


def _burn_a_seed():
    """Consume id 1 so nothing under test inherits the id-1 admin trap.

    app/models.py:1259-1261 returns True from is_admin() for id 1, and
    tests/conftest.py:131-132 runs SELECT setval(c.oid, 1, false) on every
    sequence after each test, so the first User minted in a test is
    deterministically id 1. The assert is live: if the sequence behaviour ever
    changes, this fails loudly rather than silently handing a later row id 1.
    """
    instance = make_instance('burn.piefed.local')
    burn = make_user(instance, 'burnseat')
    assert burn.id == 1
    return instance


def _seed():
    """Rows for the wiring layer, with every id deliberately distinct.

    These functions take FOUR id parameters -- community_id, current_feed_id,
    feed_id and user_id -- and sub-project 48 shipped three blind assertions
    because its seed minted rows in lockstep and two ids coincided (D653, and
    tests/README.md fact 272). Bystanders are minted first so the rows under
    test never sit at the same ordinal, and the separation is asserted live
    rather than assumed.

    CORRECTION TO THE ORIGINAL PLAN. As first drafted this function minted
    exactly one bystander per table (one Community, one Feed) before its real
    row, on the theory that "bystander first" keeps the real rows apart. It
    does not: Community and Feed are separate tables, each with its own
    sequence starting at 1, and each contributes exactly one bystander plus
    one real row, so both real rows are unavoidably the SECOND row of their
    table -- id 2 in each -- no matter which order they are minted in
    (pigeonhole: two 2-row tables' "second" elements are both 2). That
    produced `community.id == feed.id == 2` and the assertion below failed
    against live Postgres with `assert 3 == 4` before either production file
    was touched. Two throwaway Feed rows are minted here solely to shift
    feed.id off community.id's value; they are not returned and no test reads
    them.
    """
    instance = _burn_a_seed()
    owner = make_user(instance, 'feedowner')
    actor = make_user(instance, 'feedactor')
    bystander_community = make_community(name='bystander')
    community = make_community(name='wiring')
    bystander_feed = make_feed_factory(instance, name='bystanderfeed')
    make_feed_factory(instance, name='idparitybreaker1')
    make_feed_factory(instance, name='idparitybreaker2')
    feed = make_feed_factory(instance, name='wiringfeed')
    feed.user_id = owner.id
    db.session.commit()

    assert len({community.id, feed.id, actor.id, bystander_feed.id}) == 4
    return SimpleNamespace(instance=instance, owner=owner, actor=actor,
                           community=community,
                           bystander_community=bystander_community,
                           feed=feed, bystander_feed=bystander_feed)


def test_feed_add_community_reads_the_request_global_not_its_user_id_parameter(app, db_session):
    """PIN of a live defect. INVERTED in Step 6 of this task.

    _feed_add_community's signature is (community_id, current_feed_id, feed_id,
    user_id). :429 correctly uses the user_id parameter. :430 then reads
    `current_user.feed_auto_follow` -- the request global -- instead.

    On SRC_API (app/api/alpha/utils/feed.py:166 and :202 reach this through
    make_feed and edit_feed) authentication is a bearer token resolved by
    authorise_api_user and there is NO logged-in current_user, so :430 reaches
    AnonymousUserMixin, which has no feed_auto_follow. That is an AttributeError
    on an ordinary API call.

    The module reads the preference off a resolved user at :49, :87, :135 and
    :455. :430 is the only site that does not, and the only one with user_id
    already in scope.

    The feed is left non-public so :421's announce fork does not fire and the
    pin stays narrow to :430.
    """
    s = _seed()
    assert s.feed.public is False
    with app.test_request_context('/'):
        with pytest.raises(AttributeError):
            _feed_add_community(s.community.id, 0, s.feed.id, s.actor.id)


def test_announce_add_remove_subscribes_local_members_ignoring_feed_auto_follow(app, db_session):
    """PIN of a live defect. INVERTED in Step 6 of this task.

    announce_feed_add_remove_to_subscribers:550-555 subscribes every local feed
    member to the community unconditionally. The user preference that exists to
    govern exactly this is not consulted.

    Proved by its own twin: app/activitypub/routes.py:1440 performs the same
    operation on the federated path and reads
    `if fm_user.is_local() and fm_user.feed_auto_follow:`.

    The member here has feed_auto_follow=False and is subscribed anyway.
    """
    s = _seed()
    member = make_user(s.instance, 'localmember', local=True)
    member.feed_auto_follow = False
    db.session.commit()
    make_feed_member(member, s.feed)

    with patch('app.community.routes.do_subscribe') as subscribe:
        announce_feed_add_remove_to_subscribers('Add', s.feed.id, s.community.id)

    assert subscribe.call_count == 1
    assert subscribe.call_args.args[1] == member.id
