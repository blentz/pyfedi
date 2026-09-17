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


def test_feed_add_community_uses_its_user_id_parameter_not_the_request_global(app, db_session):
    """Was a PIN; INVERTED once :430 was fixed.

    ORIGINAL PINNED CLAIM, now false: ":430 reads current_user, so the API path
    raises AttributeError." The fix resolves the acting user from the user_id
    parameter, so the call completes with no request user at all and
    do_subscribe receives the id that was passed in.
    """
    s = _seed()
    with app.test_request_context('/'):
        with patch('app.community.routes.do_subscribe') as subscribe:
            _feed_add_community(s.community.id, 0, s.feed.id, s.actor.id)

    assert subscribe.call_count == 1
    assert subscribe.call_args.args[1] == s.actor.id
    assert s.actor.id != s.community.id and s.actor.id != s.feed.id


def test_announce_add_remove_honours_feed_auto_follow_for_local_members(app, db_session):
    """Was a PIN; INVERTED once :550 was fixed.

    ORIGINAL PINNED CLAIM, now false: "every local feed member is subscribed
    unconditionally." The fix matches the federated twin at
    app/activitypub/routes.py:1440.

    Two members, differing only in the preference, are the control: one is
    subscribed and one is not, so this cannot pass by the call simply never
    firing. That control is what makes the assertion a kill rather than an
    emptiness claim -- false-witness mechanism (c).
    """
    s = _seed()
    optout = make_user(s.instance, 'optout', local=True)
    optout.feed_auto_follow = False
    optin = make_user(s.instance, 'optin', local=True)
    optin.feed_auto_follow = True
    db.session.commit()
    make_feed_member(optout, s.feed)
    make_feed_member(optin, s.feed)

    with patch('app.community.routes.do_subscribe') as subscribe:
        announce_feed_add_remove_to_subscribers('Add', s.feed.id, s.community.id)

    subscribed = {c.args[1] for c in subscribe.call_args_list}
    assert subscribed == {optin.id}


def test_feed_add_community_route_acts_only_as_the_signed_in_user(app, db_session):
    """Was a PIN; INVERTED once the ownership check was added.

    ORIGINAL PINNED CLAIM, now false: "supplying the victim's id together with
    the victim's own feed id satisfies the guard." The route now ignores the
    user_id parameter entirely and acts as current_user, so an attacker
    supplying the victim's ids is refused by the feed-ownership check.
    """
    from app.feed.routes import feed_add_community
    from werkzeug.exceptions import NotFound

    s = _seed()
    attacker = make_user(s.instance, 'attacker', local=True)
    db.session.commit()
    qs = (f'user_id={s.owner.id}&new_feed_id={s.feed.id}'
          f'&current_feed_id=0&community_id={s.community.id}')

    with web_ctx(app, attacker, query_string=qs):
        with patch('app.community.routes.do_subscribe') as subscribe:
            with pytest.raises(NotFound):
                feed_add_community()

    assert subscribe.call_count == 0


def test_feed_add_community_route_refuses_a_source_feed_the_user_does_not_own(app, db_session):
    """Was a PIN; INVERTED once current_feed_id was ownership-checked.

    ORIGINAL PINNED CLAIM, now false: "current_feed_id is never ownership-checked
    at all." The bystander feed's item survives and its count is untouched.

    The OWNER is signed in here, not an attacker, so the first guard passes and
    the second is the one under test. Without that separation this test would
    pass for the wrong reason -- false-witness mechanism (d).
    """
    from app.feed.routes import feed_add_community
    from werkzeug.exceptions import NotFound

    s = _seed()
    victim_item = make_feed_item(s.bystander_feed, s.community)
    s.bystander_feed.num_communities = 1
    db.session.commit()
    assert s.feed.user_id == s.owner.id
    assert s.bystander_feed.user_id != s.owner.id
    qs = (f'user_id={s.owner.id}&new_feed_id={s.feed.id}'
          f'&current_feed_id={s.bystander_feed.id}&community_id={s.community.id}')

    with web_ctx(app, s.owner, query_string=qs):
        with pytest.raises(NotFound):
            feed_add_community()

    assert db.session.get(FeedItem, victim_item.id) is not None
    assert s.bystander_feed.num_communities == 1
