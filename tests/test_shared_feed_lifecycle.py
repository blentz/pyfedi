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
                        FeedMember, Role, User, user_role)
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


# --------------------------------------------------------------------------
# Task 2: P3 -- make_feed accepts is_instance_feed from anyone.
# --------------------------------------------------------------------------


def _api_feed_payload(**overrides):
    """The dict shape make_feed's SRC_API arm reads, at app/shared/feed.py:153-165.

    Every key is read unconditionally -- there is no .get() anywhere in that
    arm -- so a payload missing one raises KeyError rather than defaulting.
    app/api/alpha/utils/feed.py:145-164 is where the real one is built.
    """
    payload = {'url': 'apifeed', 'title': 'API feed', 'public': True, 'description': '',
               'icon_url': None, 'banner_url': None, 'nsfw': False, 'nsfl': False,
               'communities': '', 'is_instance_feed': False, 'show_child_posts': False,
               'parent_feed_id': None}
    payload.update(overrides)
    return payload


def _web_feed_form(**overrides):
    """The form shape make_feed's SRC_WEB arm reads, at app/shared/feed.py:167-180.

    A stub rather than the real AddCopyFeedForm: the production arm only ever
    reads `.data` off each field, and building the real form would drag in
    wtforms validation this round is not testing. The icon/banner uploads are
    passed as separate arguments, not form fields, so they are absent here.
    """
    fields = {'url': 'webfeed', 'title': 'Web feed', 'public': True, 'description': '',
              'nsfw': False, 'nsfl': False, 'communities': '', 'is_instance_feed': False,
              'show_child_posts': False, 'parent_feed_id': None}
    fields.update(overrides)
    return SimpleNamespace(**{k: SimpleNamespace(data=v) for k, v in fields.items()})


def _make_admin(user: User) -> Role:
    """Give `user` a role named exactly 'Admin'.

    app/models.py:1258-1264 is_admin() returns True for id 1 OR for any role
    named 'Admin'. The id-1 seat is burned by _burn_a_seed, so an admin control
    in this file has to come by the role, and the name has to be exact --
    grant_permission's f'role-{permission}' naming would not satisfy it.
    """
    role = Role(name='Admin', weight=0)
    db.session.add(role)
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
    db.session.commit()
    return role


def test_make_feed_api_arm_refuses_an_instance_feed_from_a_non_admin(app, db_session):
    """Was a PIN; INVERTED once the admin check landed.

    ORIGINAL PINNED CLAIM, now false: "is_instance_feed is copied straight out
    of the payload at app/shared/feed.py:163 and into the row, with no check on
    the caller anywhere between."

    The API arm refuses loudly rather than coercing, because an API client can
    be told it asked for something it may not have. The refusal is asserted to
    happen BEFORE the row is written -- a check that raised after the commit
    would leave the instance feed in place and still pass a bare pytest.raises.

    not-an-admin is asserted live rather than assumed: _burn_a_seed consumes id
    1, which app/models.py:1259-1261 treats as an admin.
    """
    s = _seed()
    assert not s.member.is_admin()

    with app.test_request_context('/'):
        with patch('app.shared.feed.authorise_api_user', return_value=s.member), \
                patch('app.shared.feed.RsaKeys.generate_keypair', return_value=('priv', 'pub')):
            with pytest.raises(Exception, match='is_instance_feed requires an admin account'):
                make_feed(_api_feed_payload(is_instance_feed=True), SRC_API, auth='Bearer x')

    assert Feed.query.filter_by(name='apifeed').first() is None


def test_make_feed_api_arm_still_honours_is_instance_feed_for_an_admin(app, db_session):
    """The control without which the fix is indistinguishable from deleting the
    feature. Same payload, same arm, an admin caller: the flag is honoured."""
    s = _seed()
    _make_admin(s.member)
    assert s.member.is_admin()

    with app.test_request_context('/'):
        with patch('app.shared.feed.authorise_api_user', return_value=s.member), \
                patch('app.shared.feed.RsaKeys.generate_keypair', return_value=('priv', 'pub')):
            make_feed(_api_feed_payload(is_instance_feed=True), SRC_API, auth='Bearer x')

    made = Feed.query.filter_by(name='apifeed').one()
    assert made.is_instance_feed is True
    assert made.user_id == s.member.id


def test_make_feed_web_arm_coerces_an_instance_feed_from_a_non_admin(app, db_session):
    """Was a PIN; INVERTED once the admin check landed.

    ORIGINAL PINNED CLAIM, now false: "the only gate on the web side is
    app/feed/routes.py:51's render_kw disabled widget, which constrains nothing
    on the server, so a POST carrying the field is honoured."

    The web arm coerces rather than raising: the form genuinely does not offer
    the field to a non-admin, so a POST carrying it is a forgery and the feed
    is still created -- without the flag. The assertion is therefore that the
    row EXISTS and reads False, not that creation failed. Both halves matter:
    a fix that aborted the whole creation would also pass "is_instance_feed is
    not True".
    """
    s = _seed()
    assert not s.member.is_admin()

    with web_ctx(app, s.member):
        with patch('app.shared.feed.RsaKeys.generate_keypair', return_value=('priv', 'pub')):
            make_feed(_web_feed_form(is_instance_feed=True), SRC_WEB)

    made = Feed.query.filter_by(name='webfeed').one()
    assert made.is_instance_feed is False
    assert made.user_id == s.member.id


def test_make_feed_web_arm_still_honours_is_instance_feed_for_an_admin(app, db_session):
    """The web arm's half of the same control: an admin's flag survives."""
    s = _seed()
    _make_admin(s.member)
    assert s.member.is_admin()

    with web_ctx(app, s.member):
        with patch('app.shared.feed.RsaKeys.generate_keypair', return_value=('priv', 'pub')):
            make_feed(_web_feed_form(is_instance_feed=True), SRC_WEB)

    assert Feed.query.filter_by(name='webfeed').one().is_instance_feed is True


# --------------------------------------------------------------------------
# Task 3: P4 -- the UnboundLocalError, and the asymmetric return.
# --------------------------------------------------------------------------


def test_leave_all_completes_for_an_account_that_joined_nothing(app, db_session):
    """Was a PIN; INVERTED once user_id was bound before the loops.

    ORIGINAL PINNED CLAIM, now false: "app/api/alpha/utils/community.py binds
    user_id ONLY inside its two loops, then reads it, so an account that has
    joined no community and no feed raises UnboundLocalError."

    user_view is patched and its user_id argument asserted, rather than the
    returned document inspected: the defect was always about WHICH id reaches
    the view, and a test that only checked "no exception" would pass against a
    fix that bound user_id to None.
    """
    from app.api.alpha.utils import community as community_api
    s = _seed()

    with app.test_request_context('/'):
        with patch.object(community_api, 'authorise_api_user', return_value=s.member), \
                patch.object(community_api, 'user_view') as view:
            community_api.post_community_leave_all('Bearer x')

    assert view.call_args.kwargs['user_id'] == s.member.id


def test_leave_all_keeps_the_real_user_id_for_an_account_that_has_a_feed(app, db_session):
    """The silent half of P4, which no exception ever marked.

    Before the repair the feed loop overwrote user_id with leave_feed's None,
    so any account subscribed to a feed reached user_view with user_id None
    while an account with only communities reached it with a real id. This test
    is the one that fails against a fix that only bound the variable up front
    and left leave_feed returning None -- the loop would overwrite it again.
    """
    from app.api.alpha.utils import community as community_api
    s = _seed()
    make_feed_member(s.member, s.feed)
    s.member.feed_auto_leave = False
    db.session.commit()

    with app.test_request_context('/'):
        with patch.object(community_api, 'authorise_api_user', return_value=s.member), \
                patch.object(community_api, 'user_view') as view, \
                patch('app.shared.feed.authorise_api_user', return_value=s.member.id), \
                patch('app.shared.feed.task_selector'):
            community_api.post_community_leave_all('Bearer x')

    assert view.call_args.kwargs['user_id'] == s.member.id
    assert FeedMember.query.filter_by(user_id=s.member.id, feed_id=s.feed.id).count() == 0


def test_leave_feed_returns_the_user_id_on_the_api_path_like_leave_community(app, db_session):
    """Was a PIN; INVERTED once leave_feed gained its SRC_API return.

    ORIGINAL PINNED CLAIM, now false: "leave_community returns user_id on the
    API path and leave_feed returns None on every path."

    Both halves stay in one test so the contract, not one function, is what is
    asserted: the callers assign both to the same local
    (app/api/alpha/utils/community.py:179 and :189).
    """
    s = _seed()
    make_feed_member(s.member, s.feed)
    s.member.feed_auto_leave = False
    community_member_of = make_community(name='leftbyapi')
    make_community_member(s.member, community_member_of)
    db.session.commit()

    with app.test_request_context('/'):
        with patch('app.shared.feed.authorise_api_user', return_value=s.member.id), \
                patch('app.shared.feed.task_selector'):
            feed_return = leave_feed(s.feed, SRC_API, auth='Bearer x')

    from app.shared.community import leave_community as production_leave_community
    with app.test_request_context('/'):
        with patch('app.shared.community.authorise_api_user', return_value=s.member.id), \
                patch('app.shared.community.task_selector'):
            community_return = production_leave_community(community_member_of.id, SRC_API,
                                                          auth='Bearer x')

    assert feed_return == s.member.id
    assert community_return == s.member.id


def test_leave_feed_still_returns_nothing_on_the_web_path(app, db_session):
    """The SRC_WEB half of the same return, which must NOT change: the web
    callers (app/community/routes.py:2578, app/feed/routes.py) ignore the
    return and leave_community's own web arm returns None. Without this row the
    new `if src == SRC_API` is satisfied by an unconditional return."""
    s = _seed()
    make_feed_member(s.member, s.feed)
    s.member.feed_auto_leave = False
    db.session.commit()

    with web_ctx(app, s.member):
        with patch('app.shared.feed.task_selector'):
            assert leave_feed(s.feed, SRC_WEB) is None


# --------------------------------------------------------------------------
# Task 4: delete_feed.
# --------------------------------------------------------------------------


def test_delete_feed_aborts_404_for_a_user_who_does_not_own_the_feed(app, db_session):
    """delete_feed's only authorization is `feed.user_id != user_id` -> abort(404).

    The surviving row is asserted as well as the abort: a check that fired
    AFTER the deletes would still raise NotFound and still have destroyed the
    feed. The factory does not set user_id (tests/factories.py:156), so _seed
    assigns it explicitly -- without that the comparison is None != id, which
    is true for everyone and makes the test pass for the wrong reason.
    """
    from werkzeug.exceptions import NotFound
    s = _seed()
    assert s.feed.user_id == s.owner.id != s.member.id

    with web_ctx(app, s.member):
        with pytest.raises(NotFound):
            delete_feed(s.feed.id, SRC_WEB)

    assert Feed.query.get(s.feed.id) is not None


def test_delete_feed_resolves_the_api_caller_through_authorise_api_user(app, db_session):
    """The SRC_API arm reads its user id from authorise_api_user rather than
    current_user, and that id is what the ownership check uses.

    Two assertions, because one of them alone is satisfiable by accident: the
    owner's id gets the feed deleted, and a different id gets a 404 out of the
    same call with no request user logged in at all.
    """
    from werkzeug.exceptions import NotFound
    s = _seed()

    with app.test_request_context('/'):
        with patch('app.shared.feed.authorise_api_user', return_value=s.member.id):
            with pytest.raises(NotFound):
                delete_feed(s.feed.id, SRC_API, auth='Bearer x')
    assert Feed.query.get(s.feed.id) is not None

    with app.test_request_context('/'):
        with patch('app.shared.feed.authorise_api_user', return_value=s.owner.id):
            delete_feed(s.feed.id, SRC_API, auth='Bearer x')
    assert Feed.query.get(s.feed.id) is None


@pytest.mark.parametrize('public, debug, expect_inline, expect_delayed', [
    (True, True, 1, 0),
    (True, False, 0, 1),
    (False, True, 0, 0),
    (False, False, 0, 0),
])
def test_delete_feed_announces_only_for_a_public_feed(app, db_session, public, debug,
                                                      expect_inline, expect_delayed):
    """The announce fork at app/shared/feed.py:401-405: `if feed.public:` then
    `if current_app.debug:` inline else `.delay`.

    Four rows rather than two: the public guard and the debug guard are
    separate branches, and a parametrisation over debug alone would leave
    `feed.public` never observed False. The .delay arm is asserted as
    DISPATCHED, not executed -- a test that let it run would be testing the
    broker.

    The announce is dispatched with (user_id, feed.id), and owner.id != feed.id
    is asserted in _seed, so an implementation that passed the feed id twice
    would be caught.
    """
    s = _seed()
    s.feed.public = public
    db.session.commit()

    announce = MagicMock()
    with web_ctx(app, s.owner):
        with patch('app.shared.feed.announce_feed_delete_to_subscribers', announce), \
                patch('app.shared.feed.current_app') as current_app_stub:
            current_app_stub.debug = debug
            delete_feed(s.feed.id, SRC_WEB)

    assert announce.call_count == expect_inline
    assert announce.delay.call_count == expect_delayed
    if expect_inline:
        assert announce.call_args.args == (s.owner.id, s.feed.id)
    if expect_delayed:
        assert announce.delay.call_args.args == (s.owner.id, s.feed.id)


@pytest.mark.parametrize('num_communities', [1, 0])
def test_delete_feed_removes_the_feed_and_its_items(app, db_session, num_communities):
    """The `if feed.num_communities > 0:` guard at :409 decides whether the
    FeedItem rows are deleted EXPLICITLY -- it does not decide whether they
    survive.

    Both rows assert zero surviving FeedItem rows, because the ORM removes them
    with the feed either way: a scoping probe with num_communities forced to 0
    and a real FeedItem present reported `orphan FeedItem rows: 0`. The guard
    is therefore covered here for its arcs, and this docstring says plainly
    that it is not load-bearing for the outcome, rather than implying a
    stale counter leaks rows.

    The FeedMember row is asserted gone as well: :407 deletes it before the
    feed, and it is the row the announce task reads, which is why the ordering
    comment at :399-400 exists.
    """
    s = _seed()
    make_feed_item(s.feed, s.community)
    make_feed_member(s.member, s.feed)
    s.feed.num_communities = num_communities
    db.session.commit()

    with web_ctx(app, s.owner):
        delete_feed(s.feed.id, SRC_WEB)

    assert Feed.query.get(s.feed.id) is None
    assert FeedItem.query.filter_by(feed_id=s.feed.id).count() == 0
    assert FeedMember.query.filter_by(feed_id=s.feed.id).count() == 0


# --------------------------------------------------------------------------
# Task 5: the rest of leave_feed.
# --------------------------------------------------------------------------


def test_leave_feed_accepts_a_feed_id_as_well_as_a_feed(app, db_session):
    """The preamble at :114-118 takes either shape and must reach the same
    effect. Both arms are exercised here against the same starting state, and
    the assertion is the effect (membership gone, count decremented), not that
    the call returned.

    subscriptions_count starts at 7 rather than 1: `-= 1` and `= 0` are
    indistinguishable from a starting value of 1, which is the shape of mutant
    sub-project 49 registered at D661 item (F).
    """
    s = _seed()
    make_feed_member(s.member, s.feed)
    s.feed.subscriptions_count = 7
    s.member.feed_auto_leave = False
    db.session.commit()

    with web_ctx(app, s.member):
        with patch('app.shared.feed.task_selector'):
            leave_feed(s.feed.id, SRC_WEB)

    assert FeedMember.query.filter_by(user_id=s.member.id, feed_id=s.feed.id).count() == 0
    assert Feed.query.get(s.feed.id).subscriptions_count == 6


def test_leave_feed_rejects_an_argument_that_is_neither_a_feed_nor_an_int(app, db_session):
    """The preamble's third path: neither branch binds feed_id, so :122 reads a
    name that was never assigned.

    Tested as a CONTRACT rather than declared unreachable. No caller produces
    it today -- app/api/alpha/utils/feed.py:137 and :189 pass a Feed, and
    app/community/routes.py:2578 passes a Feed -- but the signature types the
    parameter `int | Feed`, so the third path is reachable by anyone honouring
    the annotation, and an UnboundLocalError is a worse answer than a TypeError.
    Registered as covered, not as unreachable.
    """
    s = _seed()
    with web_ctx(app, s.member):
        with pytest.raises(UnboundLocalError):
            leave_feed('lifecyclefeed', SRC_WEB)


def test_leave_feed_refuses_the_owner_on_the_api_path(app, db_session):
    """The owner arm, SRC_API half: raises with a specific message.

    The message is asserted because a bare `Exception` is otherwise
    indistinguishable from any other failure in the call -- including the
    NoResultFound that the .one() at :122 raises for a non-member, which is a
    different bug with the same pytest.raises(Exception) signature.

    The surviving FeedMember row is the second assertion: a refusal that fired
    after the delete would still raise.
    """
    s = _seed()
    make_feed_member(s.owner, s.feed, is_owner=True)
    db.session.commit()

    with app.test_request_context('/'):
        with patch('app.shared.feed.authorise_api_user', return_value=s.owner.id):
            with pytest.raises(Exception, match='You cannot leave your own feed'):
                leave_feed(s.feed, SRC_API, auth='Bearer x')

    assert FeedMember.query.filter_by(user_id=s.owner.id, feed_id=s.feed.id).count() == 1


def test_leave_feed_refuses_the_owner_on_the_web_path_without_raising(app, db_session):
    """The owner arm, SRC_WEB half: flashes and returns, and the membership
    survives. A test that only asserted "no exception" would pass against a
    function that deleted the row and flashed anyway."""
    s = _seed()
    make_feed_member(s.owner, s.feed, is_owner=True)
    s.feed.subscriptions_count = 7
    db.session.commit()

    with web_ctx(app, s.owner):
        assert leave_feed(s.feed, SRC_WEB) is None

    assert FeedMember.query.filter_by(user_id=s.owner.id, feed_id=s.feed.id).count() == 1
    assert Feed.query.get(s.feed.id).subscriptions_count == 7


@pytest.mark.parametrize('bulk_leave', [True, False])
def test_leave_feed_skips_the_community_sweep_during_a_bulk_leave(app, db_session, bulk_leave):
    """`if not bulk_leave:` at :141 guards the whole community sweep; the
    caller that passes it -- app/api/alpha/utils/community.py:189, the
    leave-all path -- handles community memberships itself, which is what the
    comment at :142-143 claims.

    The user here IS a member of the feed's community, joined via the feed, so
    the non-bulk row must call leave_community and the bulk row must not. A
    version of this test whose user had no membership would pass both rows
    against a guard that did nothing.
    """
    s = _seed()
    make_feed_member(s.member, s.feed)
    make_feed_item(s.feed, s.community)
    membership = make_community_member(s.member, s.community)
    membership.joined_via_feed = True
    s.member.feed_auto_leave = True
    db.session.commit()

    with web_ctx(app, s.member):
        with patch('app.shared.feed.task_selector'), \
                patch('app.shared.feed.leave_community') as leave:
            leave_feed(s.feed, SRC_WEB, bulk_leave=bulk_leave)

    assert leave.call_count == (0 if bulk_leave else 1)


def test_leave_feed_does_not_sweep_communities_when_feed_auto_leave_is_off(app, db_session):
    """`if user.feed_auto_leave:` at :144. The column defaults True
    (app/models.py:1043), so this is the arm a test has to opt into, and the
    user is otherwise identical to the one in the sweep test above: same
    membership, same joined_via_feed, same feed item."""
    s = _seed()
    make_feed_member(s.member, s.feed)
    make_feed_item(s.feed, s.community)
    membership = make_community_member(s.member, s.community)
    membership.joined_via_feed = True
    s.member.feed_auto_leave = False
    db.session.commit()

    with web_ctx(app, s.member):
        with patch('app.shared.feed.task_selector'), \
                patch('app.shared.feed.leave_community') as leave:
            leave_feed(s.feed, SRC_WEB)

    assert leave.call_count == 0


def test_leave_feed_dispatches_the_leave_task_with_both_ids(app, db_session):
    """:125's task_selector call. Both ids are asserted, and _seed keeps
    member.id and feed.id distinct, so an implementation passing the same id
    twice -- the shape D653 recorded -- cannot pass.

    The task name is asserted too: 'leave_feed' resolves through
    app/shared/tasks/__init__.py:22 to a DIFFERENT leave_feed, the Celery one
    in app/shared/tasks/follows.py:162, and a typo here would dispatch
    something else entirely or nothing at all.
    """
    s = _seed()
    make_feed_member(s.member, s.feed)
    s.member.feed_auto_leave = False
    db.session.commit()
    assert s.member.id != s.feed.id

    with web_ctx(app, s.member):
        with patch('app.shared.feed.task_selector') as task:
            leave_feed(s.feed, SRC_WEB)

    assert task.call_count == 1
    assert task.call_args.args == ('leave_feed',)
    assert task.call_args.kwargs == {'user_id': s.member.id, 'feed_id': s.feed.id}


# --------------------------------------------------------------------------
# Task 6: make_feed.
# --------------------------------------------------------------------------


def test_make_feed_api_arm_writes_every_derived_field(app, db_session):
    """The API arm end to end, asserting the fields that are NOT straight
    copies of the input.

    url appears in six columns with three different treatments: name and
    machine_name verbatim, ap_profile_id LOWERCASED (:221), and the other four
    ap_* urls verbatim (:222-226). That divergence is registered as R6 in this
    round's design and is latent today only because both callers slugify and
    .lower() before calling; it is asserted here as CURRENT behaviour so Group
    C, which rebuilds these fields in edit_feed(from_scratch=True), inherits a
    statement of what they are rather than an assumption.

    The keypair is patched: RsaKeys.generate_keypair() is seconds of entropy
    this test would otherwise pay for and never assert.
    """
    s = _seed()
    payload = _api_feed_payload(url='MixedCase', title='Mixed', description='hello',
                                show_child_posts=True, nsfw=True, nsfl=True)

    with app.test_request_context('/'):
        with patch('app.shared.feed.authorise_api_user', return_value=s.member), \
                patch('app.shared.feed.RsaKeys.generate_keypair',
                      return_value=('the-private-key', 'the-public-key')):
            make_feed(payload, SRC_API, auth='Bearer x')

    made = Feed.query.filter_by(name='MixedCase').one()
    server = app.config['SERVER_NAME']
    assert made.machine_name == 'MixedCase'
    assert made.title == 'Mixed'
    assert made.user_id == s.member.id
    assert made.private_key == 'the-private-key'
    assert made.public_key == 'the-public-key'
    assert made.show_posts_in_children is True
    assert made.nsfw is True and made.nsfl is True
    assert made.public is True
    assert made.subscriptions_count == 1
    assert made.instance_id == 1
    assert made.ap_domain == server
    assert made.ap_profile_id == f'https://{server}/f/mixedcase'
    assert made.ap_public_url == f'https://{server}/f/MixedCase'
    assert made.ap_followers_url == f'https://{server}/f/MixedCase/followers'
    assert made.ap_following_url == f'https://{server}/f/MixedCase/following'
    assert made.ap_outbox_url == f'https://{server}/f/MixedCase/outbox'


def test_make_feed_gives_the_creator_an_owner_membership(app, db_session):
    """:250's FeedMember carries is_owner=True, and that flag is what
    leave_feed's owner refusal reads. Asserted explicitly: a membership row
    created with the flag defaulted False would let the creator leave their own
    feed, and the row's mere existence would not show it."""
    s = _seed()
    with app.test_request_context('/'):
        with patch('app.shared.feed.authorise_api_user', return_value=s.member), \
                patch('app.shared.feed.RsaKeys.generate_keypair', return_value=('a', 'b')):
            make_feed(_api_feed_payload(), SRC_API, auth='Bearer x')

    made = Feed.query.filter_by(name='apifeed').one()
    membership = FeedMember.query.filter_by(feed_id=made.id, user_id=s.member.id).one()
    assert membership.is_owner is True


@pytest.mark.parametrize('parent_given', [True, False])
def test_make_feed_sets_parent_feed_id_only_when_one_is_given(app, db_session, parent_given):
    """:229-232. The else arm assigns None explicitly, so both arms are
    observable on the row. The parent used is a real Feed the seed already
    minted, and its id is asserted distinct from the new feed's, so a mutant
    assigning the wrong id is distinguishable."""
    s = _seed()
    parent = s.feed if parent_given else None
    payload = _api_feed_payload(parent_feed_id=parent.id if parent else None)

    with app.test_request_context('/'):
        with patch('app.shared.feed.authorise_api_user', return_value=s.member), \
                patch('app.shared.feed.RsaKeys.generate_keypair', return_value=('a', 'b')):
            make_feed(payload, SRC_API, auth='Bearer x')

    made = Feed.query.filter_by(name='apifeed').one()
    if parent_given:
        assert made.parent_feed_id == parent.id != made.id
    else:
        assert made.parent_feed_id is None


@pytest.mark.parametrize('field, url_key, sizes, id_attr', [
    ('icon', 'icon_url', (40, 250), 'icon_id'),
    ('banner', 'banner_url', (878, 1600), 'image_id'),
])
@pytest.mark.parametrize('url_value, is_image, expect_file', [
    (None, False, False),
    ('https://example.test/not-an-image', False, False),
    ('https://example.test/picture.png', True, True),
])
def test_make_feed_stores_an_image_only_when_the_url_is_one(app, db_session, field, url_key,
                                                            sizes, id_attr, url_value,
                                                            is_image, expect_file):
    """:234-245, both blocks, each in three states: no url at all, a url that
    is not an image, and a url that is.

    The middle state is what isolates the second operand of the conjunction;
    without it `url and is_image_url(url)` is covered by two rows that never
    disagree. is_image_url is patched rather than fed a real url, so the test
    says which answer it is testing instead of depending on that function's
    rules.

    A decoy File is minted first so the row under test cannot land on id 1 and
    make a wrong-id assertion pass by coincidence. make_image_sizes is asserted
    with its full argument tuple, including the size pair, which is the only
    thing that distinguishes the icon block from the banner block.
    """
    from app.models import File
    s = _seed()
    decoy = File(source_url='https://example.test/decoy.png')
    db.session.add(decoy)
    db.session.commit()

    payload = _api_feed_payload(**{url_key: url_value})
    with app.test_request_context('/'):
        with patch('app.shared.feed.authorise_api_user', return_value=s.member), \
                patch('app.shared.feed.RsaKeys.generate_keypair', return_value=('a', 'b')), \
                patch('app.shared.feed.is_image_url', return_value=is_image), \
                patch('app.shared.feed.make_image_sizes') as sizer:
            make_feed(payload, SRC_API, auth='Bearer x')

    made = Feed.query.filter_by(name='apifeed').one()
    stored_id = getattr(made, id_attr)
    if expect_file:
        stored = File.query.get(stored_id)
        assert stored.source_url == url_value
        assert stored.id != decoy.id
        assert sizer.call_args.args == (stored_id, sizes[0], sizes[1], 'feeds', False)
    else:
        assert stored_id is None
        assert sizer.call_count == 0


def test_make_feed_adds_every_community_the_form_resolved(app, db_session):
    """:254-257. form_communities_to_ids and _feed_add_community are Group A's,
    covered by tests/test_shared_feed_wiring.py, so they are patched here and
    this test asserts DISPATCH: one call per resolved id, with all four
    positional arguments.

    The current_feed_id argument is the literal 0, which _feed_add_community
    reads as "not a move" (app/shared/feed.py:420). The ids are decoys chosen
    to collide with nothing the seed minted, and feed.id, user.id and both
    community ids are asserted pairwise distinct, because this call passes four
    ids adjacently and D653 is the entry about exactly that.
    """
    s = _seed()
    with app.test_request_context('/'):
        with patch('app.shared.feed.authorise_api_user', return_value=s.member), \
                patch('app.shared.feed.RsaKeys.generate_keypair', return_value=('a', 'b')), \
                patch('app.shared.feed.form_communities_to_ids', return_value={71, 82}) as resolver, \
                patch('app.shared.feed._feed_add_community') as adder:
            make_feed(_api_feed_payload(communities='!a@b\n!c@d'), SRC_API, auth='Bearer x')

    made = Feed.query.filter_by(name='apifeed').one()
    assert resolver.call_args.args == ('!a@b\n!c@d',)
    assert adder.call_count == 2
    assert {call.args for call in adder.call_args_list} == {
        (71, 0, made.id, s.member.id),
        (82, 0, made.id, s.member.id),
    }
    assert len({71, 82, made.id, s.member.id}) == 4


def test_make_feed_web_arm_converts_the_description_exactly_once(app, db_session):
    """The web arm converts the description at :191 and Feed(...) converts it
    again at :204, so the conversion is applied twice to the same string.

    That is inert, and this test is what says so by execution rather than by
    reading the regex: piefed_markdown_to_lemmy_markdown turns `(\\S)(\\r\\n)`
    into `\\1  \\2`, so the second pass sees a space before the newline and
    matches nothing. The input carries a CRLF precisely so a non-idempotent
    conversion would show.

    description_html is asserted too, because the two arms feed it differently:
    the web arm passes the already-converted string and the API arm passes the
    raw one.
    """
    from app.utils import piefed_markdown_to_lemmy_markdown
    s = _seed()
    raw = 'first line\r\nsecond line'
    once = piefed_markdown_to_lemmy_markdown(raw)
    assert once != raw
    assert piefed_markdown_to_lemmy_markdown(once) == once

    with web_ctx(app, s.member):
        with patch('app.shared.feed.RsaKeys.generate_keypair', return_value=('a', 'b')):
            make_feed(_web_feed_form(description=raw), SRC_WEB)

    made = Feed.query.filter_by(name='webfeed').one()
    assert made.description == once
    assert made.description_html is not None


# --------------------------------------------------------------------------
# Task 7: join_feed's local arm.
#
# Every test below re-queries after the call. join_feed ends with
# `finally: db.session.remove()` (:109-110), which detaches every object the
# caller holds; a scoping probe that read a seed row afterwards got
# DetachedInstanceError.
# --------------------------------------------------------------------------


def test_join_feed_aborts_404_when_no_feed_matches(app, db_session):
    """:104-105. The lookup at :34 filters name AND ap_id None, so a remote
    feed's name does not resolve on the local arm -- which is why the seed's
    factory feed (ap_id set) is used here rather than an invented name: it
    proves the ap_id half of the filter is doing something."""
    from werkzeug.exceptions import NotFound
    s = _seed()
    assert Feed.query.filter_by(name='lifecyclefeed').one().ap_id is not None

    with web_ctx(app, s.member):
        with pytest.raises(NotFound):
            join_feed('lifecyclefeed', s.member.id)


def test_join_feed_subscribes_a_local_user_to_a_local_feed(app, db_session):
    """The local arm's happy path: a FeedMember row, the subscriptions_count
    incremented, and the three memoized entries deleted.

    subscriptions_count starts at 7 so `+= 1` is distinguishable from `= 1`,
    which is the value make_feed writes and therefore the mutant most likely to
    pass unnoticed.

    The cache.delete_memoized calls are asserted as CALLS: tests/conftest.py
    sets CACHE_TYPE = 'NullCache', so their effect is unobservable by
    construction (D602, D589), and asserting the effect would prove nothing.
    """
    s = _seed()
    local = make_local_feed(name='localjoinfeed', public=True)
    local.subscriptions_count = 7
    s.member.feed_auto_follow = False
    db.session.commit()
    feed_id, member_id = local.id, s.member.id

    with web_ctx(app, s.member):
        with patch('app.shared.feed.cache.delete_memoized') as bust:
            join_feed('localjoinfeed', member_id)

    assert FeedMember.query.filter_by(user_id=member_id, feed_id=feed_id).count() == 1
    assert Feed.query.get(feed_id).subscriptions_count == 8
    assert bust.call_count == 3


def test_join_feed_strips_the_actor_it_is_given(app, db_session):
    """:28. Without the strip, ' localjoinfeed ' misses the exact-match lookup
    at :34 and the call aborts 404, so this is a behaviour with a live
    consequence rather than tidiness."""
    s = _seed()
    local = make_local_feed(name='localjoinfeed', public=True)
    s.member.feed_auto_follow = False
    db.session.commit()
    feed_id, member_id = local.id, s.member.id

    with web_ctx(app, s.member):
        join_feed('  localjoinfeed  ', member_id)

    assert FeedMember.query.filter_by(user_id=member_id, feed_id=feed_id).count() == 1


def test_join_feed_does_nothing_twice_for_an_existing_member(app, db_session):
    """:96-99, the else arm. The membership count is asserted to stay at 1 and
    the count NOT to move: an implementation that added a second row would
    still flash the same message.

    feed_membership is not patched -- Feed.subscribed does the real lookup --
    so this also pins that an existing FeedMember is what makes the arm fire.
    """
    s = _seed()
    local = make_local_feed(name='localjoinfeed', public=True)
    local.subscriptions_count = 7
    make_feed_member(s.member, local)
    db.session.commit()
    feed_id, member_id = local.id, s.member.id

    with web_ctx(app, s.member):
        join_feed('localjoinfeed', member_id)

    assert FeedMember.query.filter_by(user_id=member_id, feed_id=feed_id).count() == 1
    assert Feed.query.get(feed_id).subscriptions_count == 7


@pytest.mark.parametrize('src, expect_flash', [(SRC_WEB, True), (SRC_API, False)])
def test_join_feed_flashes_only_on_the_web_path(app, db_session, src, expect_flash):
    """:94's `success is True and src == SRC_WEB`, and :98's `src == SRC_WEB`
    in the else arm -- both covered by the two rows here.

    THE STRANDED ARM, STATED RATHER THAN CHASED: `success` is assigned True at
    :39 and never reassigned, so `success is True` is a tautology and its False
    arm is unreachable. That is fact 75 CAUSE 9, the same shape as D669's
    `if proceed:` in _feed_remove_community. Nothing here can cover it and no
    test should pretend to; the arc it strands is reported with the round's
    coverage figures.
    """
    s = _seed()
    local = make_local_feed(name='localjoinfeed', public=True)
    s.member.feed_auto_follow = False
    db.session.commit()
    member_id = local_id = None
    member_id, local_id = s.member.id, local.id

    with web_ctx(app, s.member):
        with patch('app.shared.feed.flash') as flash_stub:
            join_feed('localjoinfeed', member_id, src)

    assert flash_stub.call_count == (1 if expect_flash else 0)
    assert FeedMember.query.filter_by(user_id=member_id, feed_id=local_id).count() == 1


@pytest.mark.parametrize('auto_follow, debug, expect_inline, expect_delayed', [
    (True, True, 2, 0),
    (True, False, 0, 2),
    (False, True, 0, 0),
    (False, False, 0, 0),
])
def test_join_feed_subscribes_to_the_feeds_communities_only_when_asked(
        app, db_session, auto_follow, debug, expect_inline, expect_delayed):
    """:49-57. Two guards, two arms each: feed_auto_follow, then
    current_app.debug choosing between an inline call and a dispatch.

    TWO FeedItem communities, not one, so the loop is a loop and a mutant that
    subscribed to only the first is visible in the call count. One of them has
    ap_id set and one does not, because :53 chooses between community.ap_id and
    community.name -- with a single community that ternary is covered but never
    observed to differ.

    do_subscribe is patched at app.community.routes, where the deferred import
    at :38 resolves it; a rebind on app.shared.feed would not take.
    """
    s = _seed()
    local = make_local_feed(name='localjoinfeed', public=True)
    remote_community = make_community(name='remotecommunity', host='far.piefed.local')
    # make_community leaves ap_id None whatever host it is given -- measured,
    # not assumed: without this line both communities took the .name arm of
    # :53's ternary and the two arms were never observed to differ.
    remote_community.ap_id = 'remotecommunity@far.piefed.local'
    local_community = make_community(name='localcommunity')
    assert local_community.ap_id is None
    make_feed_item(local, remote_community)
    make_feed_item(local, local_community)
    s.member.feed_auto_follow = auto_follow
    db.session.commit()
    member_id = s.member.id
    expected_actors = {'remotecommunity@far.piefed.local', 'localcommunity'}

    subscribe = MagicMock()
    with web_ctx(app, s.member):
        with patch('app.community.routes.do_subscribe', subscribe), \
                patch('app.shared.feed.current_app') as current_app_stub:
            current_app_stub.debug = debug
            join_feed('localjoinfeed', member_id, SRC_API)

    assert subscribe.call_count == expect_inline
    assert subscribe.delay.call_count == expect_delayed
    calls = subscribe.call_args_list if expect_inline else subscribe.delay.call_args_list
    if calls:
        assert {call.args[0] for call in calls} == expected_actors
        assert {call.args[1] for call in calls} == {member_id}
        assert all(call.kwargs == {'joined_via_feed': True} for call in calls)
