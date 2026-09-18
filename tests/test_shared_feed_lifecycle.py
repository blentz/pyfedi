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
