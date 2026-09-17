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
from unittest.mock import MagicMock, patch

from app import db
from app.models import Community, CommunityMember, Feed, FeedItem, Instance, User
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


@pytest.mark.parametrize('raw, expected_lookup', [
    ('plain', '!plain@test.piefed.local'),
    ('!withbang', '!withbang@test.piefed.local'),
    ('withhost@remote.example', '!withhost@remote.example'),
    ('!both@remote.example', '!both@remote.example'),
])
def test_form_communities_to_ids_normalises_each_input_shape(app, db_session, raw, expected_lookup):
    """:621-624's two independent conditions, crossed.

    :621 prefixes '!' when absent; :623 appends '@' + SERVER_NAME when absent.
    The four rows are the four combinations, so neither condition can be
    satisfied by the other's input -- false-witness mechanism (d).

    search_for_community is patched on app.community.util, NOT app.shared.feed:
    :617 does the import inside the function body to break an import cycle, so a
    rebind on the importing module is never consulted.
    """
    s = _seed()
    with patch('app.community.util.search_for_community', return_value=s.community) as search:
        with app.test_request_context('/'):
            result = form_communities_to_ids(raw)

    assert search.call_args.args[0] == expected_lookup
    assert result == {s.community.id}


def test_form_communities_to_ids_skips_names_that_resolve_to_nothing(app, db_session):
    """:626's False arm. The positive control is in the same call.

    One name resolves and one does not, so the empty half cannot be produced by
    the loop never running -- false-witness mechanism (c). Asserting only that a
    miss yields an empty set would prove nothing.
    """
    s = _seed()
    lookups = {'!found@test.piefed.local': s.community, '!missing@test.piefed.local': None}
    with patch('app.community.util.search_for_community', side_effect=lambda x: lookups[x]):
        with app.test_request_context('/'):
            result = form_communities_to_ids('found\nmissing')

    assert result == {s.community.id}


def test_form_communities_to_ids_reads_every_line_and_returns_a_set(app, db_session):
    """:619 splits on newlines; :618/:627 accumulate into a set.

    Two distinct communities, so a mutant returning only the last would be
    caught. Compared as a set because the function returns one and the order of
    a set is not a claim this test makes.
    """
    s = _seed()
    lookups = {'!a@test.piefed.local': s.community,
               '!b@test.piefed.local': s.bystander_community}
    with patch('app.community.util.search_for_community', side_effect=lambda x: lookups[x]):
        with app.test_request_context('/'):
            result = form_communities_to_ids('a\nb')

    assert result == {s.community.id, s.bystander_community.id}
    assert s.community.id != s.bystander_community.id


def test_form_communities_to_ids_on_empty_input_searches_for_a_bare_host(app, db_session):
    """:619 on '' yields [''], not [], so the loop runs once.

    ''.strip().split('\\n') is [''], which :621 turns into '!' and :623 into
    '!@test.piefed.local'. This is not obviously intended; it is registered as a
    finding rather than repaired, and this test records the behaviour as it is.
    """
    with patch('app.community.util.search_for_community', return_value=None) as search:
        with app.test_request_context('/'):
            result = form_communities_to_ids('')

    assert search.call_args.args[0] == '!@test.piefed.local'
    assert result == set()


def test_existing_communities_returns_the_feed_s_community_ids(app, db_session):
    """:613-614. Two items on the feed under test and one on the bystander feed.

    FIX (round 1): the brief's original third row put the bystander feed's item
    on s.bystander_community, which is ALREADY one of the feed-under-test's two
    communities. Dropping the WHERE filter then returns the multiset
    {community, bystander_community, bystander_community}, which as a set is
    still {community, bystander_community} -- identical to the filtered
    result, so the mutant survived. A decoy community that appears on the
    bystander feed and NOWHERE on the feed under test is required so that
    removing the filter actually changes the returned set: filtered ->
    {community, bystander_community}; with `WHERE 1=1` -> that same set PLUS
    decoy_community.id, which is a set the assertion below rejects.

    Compared as a set -- these are query-planner rows and their order is not a
    claim.

    The annotation says `-> List` but the function returns a ScalarResult, a
    live iterator bound to the session. That divergence is registered, not
    repaired; this test consumes it as an iterator, which is what callers do.
    """
    s = _seed()
    decoy_community = make_community(name='decoy')
    assert decoy_community.id not in {s.community.id, s.bystander_community.id}
    make_feed_item(s.feed, s.community)
    make_feed_item(s.feed, s.bystander_community)
    make_feed_item(s.bystander_feed, decoy_community)

    result = set(existing_communities(s.feed.id))

    assert result == {s.community.id, s.bystander_community.id}
    assert s.feed.id != s.bystander_feed.id


def test_announce_add_remove_builds_an_announce_wrapping_the_action(app, db_session):
    """:511-535. The Announce wraps an object whose `type` is the action.

    `action` is a parameter and reaches the JSON at :521 only. Asserting the
    embedded type is what makes :521 observable; asserting the outer "Announce"
    alone would pass under a mutant that ignored the argument.

    A remote member is required or the loop body never runs and the JSON is
    never sent, so this would assert on a structure nothing consumed.

    CORRECTION: the brief's version of this test never set instance.inbox, so
    the :560 delivery guard was always False and send was never called.
    session = get_task_session() here is the REAL function (unpatched), so the
    Instance :559 fetches through it is the genuine row -- unlike the
    rollback/close tests below, where get_task_session is mocked and the
    guard is satisfied by a Mock regardless of the real row's inbox.
    """
    s = _seed()
    s.feed.ap_following_url = 'https://test.piefed.local/f/wiringfeed/following'
    remote = make_user(s.instance, 'remotemember', local=False)
    s.instance.inbox = 'https://remote.example/inbox'
    db.session.commit()
    make_feed_member(remote, s.feed)

    with patch('app.shared.feed.send_post_request') as send:
        with patch('app.shared.feed.instance_banned', return_value=False):
            announce_feed_add_remove_to_subscribers('Remove', s.feed.id, s.community.id)

    assert send.call_count == 1
    activity = send.call_args.args[1]
    assert activity['type'] == 'Announce'
    assert activity['object']['type'] == 'Remove'
    assert activity['object']['object']['id'] == s.community.ap_public_url
    assert activity['object']['target']['id'] == s.feed.ap_following_url


def test_announce_add_remove_skips_the_feed_owner(app, db_session):
    """:549. The owner is skipped before the local/remote fork.

    _seed() assigns feed.user_id, which the factory leaves None -- with None
    the comparison at :549 is never equal and this branch would be
    unreachable, so the assignment is load-bearing rather than tidiness.

    FIX (round 1 of 5, Major): the prior version of this test asserted only
    send.call_count == 1. Both members are remote, so exactly one send
    happens whether the owner is skipped (correct) or the owner is sent to
    and the non-owner is skipped instead (the :549 `==`-to-`!=` mutant) --
    a counting oracle where an identity oracle is required, verified by hand
    (see task-4-report.md's mutant transcript). The owner and the non-owner
    are now placed on two DIFFERENT instances with two different inboxes, so
    send_post_request's first argument (instance.inbox, :561) discloses WHICH
    member's Instance row the surviving branch actually reached. Asserting
    that argument equals the non-owner's inbox -- not merely that a send
    happened -- is what a passing owner-check requires and a failing one
    cannot produce, because under the mutant the call carries the owner's
    instance's inbox instead.
    """
    s = _seed()
    owner_instance = s.instance
    owner_instance.inbox = 'https://ownerinstance.example/inbox'
    s.owner.ap_id = 'feedowner@remote.example'

    other_instance = make_instance('otherinstance.example')
    other_instance.inbox = 'https://otherinstance.example/inbox'
    other = make_user(other_instance, 'otherremote', local=False)
    db.session.commit()

    make_feed_member(s.owner, s.feed)
    make_feed_member(other, s.feed)
    assert s.feed.user_id == s.owner.id
    assert owner_instance.id != other_instance.id
    assert owner_instance.inbox != other_instance.inbox

    with patch('app.shared.feed.send_post_request') as send:
        with patch('app.shared.feed.instance_banned', return_value=False):
            announce_feed_add_remove_to_subscribers('Add', s.feed.id, s.community.id)

    assert send.call_count == 1
    assert send.call_args.args[0] == other_instance.inbox


@pytest.mark.parametrize('inbox, online, banned, expect_send', [
    ('https://remote.example/inbox', True, False, 1),
    (None, True, False, 0),
    ('https://remote.example/inbox', False, False, 0),
    ('https://remote.example/inbox', True, True, 0),
])
def test_announce_add_remove_delivery_guard_isolates_each_operand(
        app, db_session, inbox, online, banned, expect_send):
    """:560's three operands, one falsified per row against a passing control.

    Row 1 is the control. Each later row falsifies exactly one operand, so no
    row can pass because of another's condition -- false-witness mechanism (d).
    instance_banned is patched on app.shared.feed, where feed.py:21 bound it.
    """
    s = _seed()
    remote = make_user(s.instance, 'remotemember', local=False)
    db.session.commit()
    make_feed_member(remote, s.feed)
    s.instance.inbox = inbox
    db.session.commit()

    with patch('app.shared.feed.send_post_request') as send:
        with patch('app.shared.feed.instance_banned', return_value=banned):
            with patch.object(type(s.instance), 'online', return_value=online):
                announce_feed_add_remove_to_subscribers('Add', s.feed.id, s.community.id)

    assert send.call_count == expect_send


def test_announce_add_remove_rolls_back_and_re_raises_on_failure(app, db_session):
    """:562-564. The except arm rolls the task session back and re-raises.

    The raise comes from send_post_request, which is the last thing the loop
    body does, so the failure is inside the try rather than before it. Asserting
    both the rollback AND the propagation is what separates this from :565's
    finally, which runs either way.
    """
    s = _seed()
    remote = make_user(s.instance, 'remotemember', local=False)
    db.session.commit()
    make_feed_member(remote, s.feed)
    fake_session = patch('app.shared.feed.get_task_session').start()

    try:
        with patch('app.shared.feed.instance_banned', return_value=False):
            with patch('app.shared.feed.send_post_request', side_effect=RuntimeError('boom')):
                with pytest.raises(RuntimeError):
                    announce_feed_add_remove_to_subscribers('Add', s.feed.id, s.community.id)
    finally:
        patch.stopall()

    assert fake_session.return_value.rollback.call_count == 1
    assert fake_session.return_value.close.call_count == 1


def test_announce_add_remove_closes_the_task_session_on_success(app, db_session):
    """:565-566's finally on the path where no exception was raised.

    The control for the test above: close is called in both cases, rollback in
    only one. Without this pair, a mutant deleting the rollback would be caught
    but a mutant moving close into the except arm would not.
    """
    s = _seed()
    fake_session = patch('app.shared.feed.get_task_session').start()

    try:
        announce_feed_add_remove_to_subscribers('Add', s.feed.id, s.community.id)
    finally:
        patch.stopall()

    assert fake_session.return_value.rollback.call_count == 0
    assert fake_session.return_value.close.call_count == 1


def test_announce_delete_builds_a_delete_naming_the_feed_and_actor(app, db_session):
    """:576-585. The actor is the USER, not the feed -- unlike its twin, whose
    actor at :514 is the feed. Asserting both halves is what records that.

    instance.inbox must be set or :604's delivery guard is always False and
    send is never called -- the brief's original version of this test omitted
    it, which would have left send.call_args None.
    """
    s = _seed()
    s.owner.ap_public_url = 'https://test.piefed.local/u/feedowner'
    remote = make_user(s.instance, 'remotemember', local=False)
    s.instance.inbox = 'https://remote.example/inbox'
    db.session.commit()
    make_feed_member(remote, s.feed)

    with patch('app.shared.feed.send_post_request') as send:
        with patch('app.shared.feed.instance_banned', return_value=False):
            announce_feed_delete_to_subscribers(s.owner.id, s.feed.id)

    activity = send.call_args.args[1]
    assert activity['type'] == 'Delete'
    assert activity['actor'] == s.owner.ap_public_url
    assert activity['object']['type'] == 'Feed'
    assert activity['object']['id'] == s.feed.ap_public_url


def test_announce_delete_skips_the_feed_owner(app, db_session):
    """:598-599, with an identity oracle rather than a call count.

    FIX, following task-4-report.md's mutant transcript on this function's
    twin: asserting only send.call_count == 1 holds whether the owner is
    skipped and the other member is sent to (correct) or the owner is sent to
    and the other member is skipped instead (the :598 `==`-to-`!=` mutant),
    because both members would otherwise be remote and share one Instance --
    exactly one send happens either way. The owner and the other member are
    placed on two DIFFERENT instances with two different inboxes, so
    send_post_request's first argument (instance.inbox, :605) discloses WHICH
    member's Instance row the surviving branch actually reached.
    """
    s = _seed()
    owner_instance = s.instance
    owner_instance.inbox = 'https://ownerinstance.example/inbox'
    s.owner.ap_id = 'feedowner@remote.example'

    other_instance = make_instance('deleteotherinstance.example')
    other_instance.inbox = 'https://deleteotherinstance.example/inbox'
    other = make_user(other_instance, 'otherremote', local=False)
    db.session.commit()

    make_feed_member(s.owner, s.feed)
    make_feed_member(other, s.feed)
    assert s.feed.user_id == s.owner.id
    assert owner_instance.id != other_instance.id
    assert owner_instance.inbox != other_instance.inbox

    with patch('app.shared.feed.send_post_request') as send:
        with patch('app.shared.feed.instance_banned', return_value=False):
            announce_feed_delete_to_subscribers(s.owner.id, s.feed.id)

    assert send.call_count == 1
    assert send.call_args.args[0] == other_instance.inbox


def test_announce_delete_skips_local_members_without_subscribing_them(app, db_session):
    """:600-601. THE DIVERGENCE FROM THE TWIN, asserted rather than described.

    announce_feed_add_remove_to_subscribers:551-556 subscribes local members.
    This function simply skips them. do_subscribe is patched so that a mutant
    importing the twin's behaviour here would be caught by the call count,
    not merely by the absence of a call.

    Local and remote members are placed on two DIFFERENT instances with two
    different inboxes -- the same fix as the owner-skip test above -- so that
    a mutant inverting :600's condition (which would skip the remote member
    instead and let the local member's send through, holding the call count
    at 1 either way) is caught by WHICH inbox the surviving call carries.
    """
    s = _seed()
    local_instance = make_instance('deletelocalmember.example')
    local_instance.inbox = 'https://deletelocalmember.example/inbox'
    local_member = make_user(local_instance, 'localmember', local=True)

    remote_instance = make_instance('deleteremotemember.example')
    remote_instance.inbox = 'https://deleteremotemember.example/inbox'
    remote = make_user(remote_instance, 'remotemember', local=False)
    db.session.commit()

    make_feed_member(local_member, s.feed)
    make_feed_member(remote, s.feed)
    assert local_instance.id != remote_instance.id
    assert local_instance.inbox != remote_instance.inbox

    with patch('app.shared.feed.send_post_request') as send:
        with patch('app.community.routes.do_subscribe') as subscribe:
            with patch('app.shared.feed.instance_banned', return_value=False):
                announce_feed_delete_to_subscribers(s.owner.id, s.feed.id)

    assert send.call_count == 1
    assert send.call_args.args[0] == remote_instance.inbox
    assert subscribe.call_count == 0


@pytest.mark.parametrize('inbox, online, banned, expect_send', [
    ('https://remote.example/inbox', True, False, 1),
    (None, True, False, 0),
    ('https://remote.example/inbox', False, False, 0),
    ('https://remote.example/inbox', True, True, 0),
])
def test_announce_delete_delivery_guard_isolates_each_operand(
        app, db_session, inbox, online, banned, expect_send):
    """:604's three operands, one falsified per row against a passing control.

    Written separately from the twin's identical-looking table at :560
    because the two functions have already diverged at :548/:598, and a
    shared helper would hide the next divergence as well.
    """
    s = _seed()
    remote = make_user(s.instance, 'remotemember', local=False)
    db.session.commit()
    make_feed_member(remote, s.feed)
    s.instance.inbox = inbox
    db.session.commit()

    with patch('app.shared.feed.send_post_request') as send:
        with patch('app.shared.feed.instance_banned', return_value=banned):
            with patch.object(type(s.instance), 'online', return_value=online):
                announce_feed_delete_to_subscribers(s.owner.id, s.feed.id)

    assert send.call_count == expect_send


def test_announce_delete_rolls_back_and_re_raises_on_failure(app, db_session):
    """:606-608, with :609-610's finally proved by the close count.

    :598's fm_user comes from `session.query(User)` -- the TASK session --
    unlike the twin's :548, which reads the real `User.query`. Mocking
    get_task_session wholesale therefore also intercepts the member lookup
    here, not merely the Instance lookup as in the twin's version of this
    test (registered divergence). A query.side_effect keyed on the model
    class is required so fm_user resolves to a real, non-owner, non-local
    User -- otherwise fm_user.is_local() is a truthy MagicMock and the
    member is skipped before send_post_request is ever reached, and
    pytest.raises(RuntimeError) would fail with nothing raised.
    """
    s = _seed()
    remote = make_user(s.instance, 'remotemember', local=False)
    s.instance.inbox = 'https://remote.example/inbox'
    db.session.commit()
    make_feed_member(remote, s.feed)

    fake_session = patch('app.shared.feed.get_task_session').start()

    def fake_query(model):
        query = MagicMock()
        if model is User:
            query.get.return_value = remote
        elif model is Instance:
            query.get.return_value = s.instance
        return query

    fake_session.return_value.query.side_effect = fake_query

    try:
        with patch('app.shared.feed.instance_banned', return_value=False):
            with patch('app.shared.feed.send_post_request', side_effect=RuntimeError('boom')):
                with pytest.raises(RuntimeError):
                    announce_feed_delete_to_subscribers(s.owner.id, s.feed.id)
    finally:
        patch.stopall()

    assert fake_session.return_value.rollback.call_count == 1
    assert fake_session.return_value.close.call_count == 1
