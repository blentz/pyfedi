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
from flask_login import login_user

from app import db
from app.models import Community, CommunityJoinRequest, CommunityMember, Feed, FeedItem, Instance, User
from app.shared.feed import (_feed_add_community, _feed_remove_community,
                             announce_feed_add_remove_to_subscribers,
                             announce_feed_delete_to_subscribers,
                             remote_subscriber_inboxes,
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

    FIX (mutation round): :436's joined_via_feed=True was asserted nowhere, so
    flipping it to False survived the whole file. That keyword is not
    decoration -- it is the value written into CommunityMember.joined_via_feed,
    which is precisely the column _feed_remove_community:456 reads to decide
    whether to auto-unfollow a member when the community leaves the feed. Both
    halves of that feature were wired through a constant no test observed, so
    they could have drifted apart silently. Asserted here rather than in the
    subscribe-guard table below because that table's other two rows expect no
    call at all, and call_args would be None for them.
    """
    s = _seed()
    with app.test_request_context('/'):
        with patch('app.community.routes.do_subscribe') as subscribe:
            _feed_add_community(s.community.id, 0, s.feed.id, s.actor.id)

    assert subscribe.call_count == 1
    assert subscribe.call_args.args[1] == s.actor.id
    assert subscribe.call_args.kwargs['joined_via_feed'] is True
    assert s.actor.id != s.community.id and s.actor.id != s.feed.id


def test_announce_add_remove_honours_feed_auto_follow_for_local_members(app, db_session):
    """Was a PIN; INVERTED once :550 was fixed.

    ORIGINAL PINNED CLAIM, now false: "every local feed member is subscribed
    unconditionally." ~~The fix matches the federated twin at
    app/activitypub/routes.py:1440.~~

    CORRECTED AT THE FINAL WHOLE-BRANCH REVIEW (D670). The fix matches the
    twin's CONDITION and NOT its control flow. The twin at
    app/activitypub/routes.py:1436-1444 guards with the same
    `if fm_user.is_local() and fm_user.feed_auto_follow:` -- but its loop body
    ENDS at :1444: no `continue`, no delivery block, nothing to fall through
    to. Here :556's `continue` sits INSIDE :551's body, so narrowing :551
    routed an opted-OUT local member past the `continue` and into the
    remote-delivery block at :559-561. That fall-through is pinned as CURRENT
    behaviour by
    test_announce_add_remove_delivers_to_an_opted_out_local_member at the end
    of this file; read its docstring before changing either site.

    Two members, differing only in the preference, are the control: one is
    subscribed and one is not, so this cannot pass by the call simply never
    firing. That control is what makes the assertion a kill rather than an
    emptiness claim -- false-witness mechanism (c).

    FIX (mutation round): :555's joined_via_feed=True was asserted nowhere --
    the same survivor as :436's, at the task's own do_subscribe call. See
    test_feed_add_community_uses_its_user_id_parameter_not_the_request_global
    for why the keyword is load-bearing. Asserted as a LIST rather than with
    all(), so the assertion cannot be satisfied vacuously by an empty
    call_args_list under some future mutant that stops calling do_subscribe
    altogether.
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
    assert [c.kwargs['joined_via_feed'] for c in subscribe.call_args_list] == [True]


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
    form = {'user_id': s.owner.id, 'new_feed_id': s.feed.id,
            'current_feed_id': 0, 'community_id': s.community.id}

    # __wrapped__: past login_required's CSRF check, which is not under test here
    with app.test_request_context('/', method='POST', data=form):
        login_user(attacker)
        with patch('app.community.routes.do_subscribe') as subscribe:
            with pytest.raises(NotFound):
                feed_add_community.__wrapped__()

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
    form = {'user_id': s.owner.id, 'new_feed_id': s.feed.id,
            'current_feed_id': s.bystander_feed.id, 'community_id': s.community.id}

    with app.test_request_context('/', method='POST', data=form):
        login_user(s.owner)
        with pytest.raises(NotFound):
            feed_add_community.__wrapped__()

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

    FIX (mutation round): this test inspected only the payload (args[1]) and
    never the signing credentials at :561, so BOTH credential mutants
    survived -- feed.private_key -> fm_user.private_key and
    feed.ap_profile_id -> fm_user.ap_profile_id. That is the identical gap
    Task 5 found and fixed at the sibling call :605, with the operands
    reversed: the FEED signs here, the USER signs there. The construction is
    :605's, deliberately: distinct non-None private_key values assigned by
    hand (no real RSA material is needed; send_post_request is mocked) and
    ap_profile_id values already distinct by construction (make_feed's
    '/f/<name>' vs make_user's '/users/<name>'), with both separations pinned
    by a live assert BEFORE the code runs. Without that pin a coincidence
    (e.g. both None) would satisfy the credential assertions under the mutant
    too, degrading them into an inert control that fails silently rather than
    at setup.
    """
    s = _seed()
    s.feed.ap_following_url = 'https://test.piefed.local/f/wiringfeed/following'
    s.feed.private_key = 'feedprivatekeymaterial'
    remote = make_user(s.instance, 'remotemember', local=False)
    remote.private_key = 'memberprivatekeymaterial'
    s.instance.inbox = 'https://remote.example/inbox'
    db.session.commit()
    make_feed_member(remote, s.feed)

    assert s.feed.private_key is not None and remote.private_key is not None
    assert s.feed.private_key != remote.private_key
    assert s.feed.ap_profile_id is not None and remote.ap_profile_id is not None
    assert s.feed.ap_profile_id != remote.ap_profile_id

    with patch('app.shared.feed.send_post_request') as send:
        with patch('app.shared.feed.instance_banned', return_value=False):
            announce_feed_add_remove_to_subscribers('Remove', s.feed.id, s.community.id)

    assert send.call_count == 1
    activity = send.call_args.args[1]
    assert activity['type'] == 'Announce'
    assert activity['object']['type'] == 'Remove'
    assert activity['object']['object']['id'] == s.community.ap_public_url
    assert activity['object']['target']['id'] == s.feed.ap_following_url
    assert send.call_args.args[2] == s.feed.private_key
    assert send.call_args.args[3] == s.feed.ap_profile_id + '#main-key'


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
    # NOT patch(...).start() + patch.stopall(): stopall() stops EVERY patcher in
    # unittest.mock's global registry, including the ones respx starts for
    # tests/conftest.py's session-scoped block_outbound_http. That silently
    # unblocked outbound HTTP for the whole rest of the session -- this file
    # sorts immediately before tests/test_shared_post_edit.py, whose
    # test_the_domain_block_is_skipped_for_a_hostless_url then failed with
    # "DID NOT RAISE Exception" and cascaded into 130 failures and 209 errors,
    # every one of which passed in isolation. Stop only what this test started.
    _task_session_patcher = patch('app.shared.feed.get_task_session')
    fake_session = _task_session_patcher.start()

    try:
        with patch('app.shared.feed.instance_banned', return_value=False):
            with patch('app.shared.feed.send_post_request', side_effect=RuntimeError('boom')):
                with pytest.raises(RuntimeError):
                    announce_feed_add_remove_to_subscribers('Add', s.feed.id, s.community.id)
    finally:
        _task_session_patcher.stop()

    assert fake_session.return_value.rollback.call_count == 1
    assert fake_session.return_value.close.call_count == 1


def test_announce_add_remove_closes_the_task_session_on_success(app, db_session):
    """:565-566's finally on the path where no exception was raised.

    The control for the test above: close is called in both cases, rollback in
    only one. Without this pair, a mutant deleting the rollback would be caught
    but a mutant moving close into the except arm would not.
    """
    s = _seed()
    # NOT patch(...).start() + patch.stopall(): stopall() stops EVERY patcher in
    # unittest.mock's global registry, including the ones respx starts for
    # tests/conftest.py's session-scoped block_outbound_http. That silently
    # unblocked outbound HTTP for the whole rest of the session -- this file
    # sorts immediately before tests/test_shared_post_edit.py, whose
    # test_the_domain_block_is_skipped_for_a_hostless_url then failed with
    # "DID NOT RAISE Exception" and cascaded into 130 failures and 209 errors,
    # every one of which passed in isolation. Stop only what this test started.
    _task_session_patcher = patch('app.shared.feed.get_task_session')
    fake_session = _task_session_patcher.start()

    try:
        announce_feed_add_remove_to_subscribers('Add', s.feed.id, s.community.id)
    finally:
        _task_session_patcher.stop()

    assert fake_session.return_value.rollback.call_count == 0
    assert fake_session.return_value.close.call_count == 1


def test_announce_delete_builds_a_delete_naming_the_feed_and_actor(app, db_session):
    """:576-585. The actor is the USER, not the feed -- unlike its twin, whose
    actor at :514 is the feed. Asserting both halves is what records that.

    instance.inbox must be set or :604's delivery guard is always False and
    send is never called -- the brief's original version of this test omitted
    it, which would have left send.call_args None.

    FIX (round 1): the original version of this test inspected only the
    payload (args[1]) and never the signing credentials at :605
    (user.private_key, user.ap_profile_id) -- a mutant swapping either for
    the FEED's equivalent passed silently. s.owner and s.feed are given
    distinct, non-None private_key values by hand (no real RSA material is
    needed; send_post_request is mocked) and their ap_profile_id values are
    already distinct by construction (make_user's '/users/<name>' vs
    make_feed's '/f/<name>'), with both separations pinned by a live assert
    before exercising the code -- otherwise a coincidence (e.g. both None)
    would make the credential assertions pass under the mutant too.
    """
    s = _seed()
    s.owner.ap_public_url = 'https://test.piefed.local/u/feedowner'
    s.owner.private_key = 'ownerprivatekeymaterial'
    s.feed.private_key = 'feedprivatekeymaterial'
    remote = make_user(s.instance, 'remotemember', local=False)
    s.instance.inbox = 'https://remote.example/inbox'
    db.session.commit()
    make_feed_member(remote, s.feed)

    assert s.owner.private_key is not None and s.feed.private_key is not None
    assert s.owner.private_key != s.feed.private_key
    assert s.owner.ap_profile_id is not None and s.feed.ap_profile_id is not None
    assert s.owner.ap_profile_id != s.feed.ap_profile_id

    with patch('app.shared.feed.send_post_request') as send:
        with patch('app.shared.feed.instance_banned', return_value=False):
            announce_feed_delete_to_subscribers(s.owner.id, s.feed.ap_public_url,
                                                remote_subscriber_inboxes(s.feed))

    activity = send.call_args.args[1]
    assert activity['type'] == 'Delete'
    assert activity['actor'] == s.owner.ap_public_url
    assert activity['object']['type'] == 'Feed'
    assert activity['object']['id'] == s.feed.ap_public_url
    assert send.call_args.args[2] == s.owner.private_key
    assert send.call_args.args[3] == s.owner.ap_profile_id + '#main-key'


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
            announce_feed_delete_to_subscribers(s.owner.id, s.feed.ap_public_url,
                                                remote_subscriber_inboxes(s.feed))

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
                announce_feed_delete_to_subscribers(s.owner.id, s.feed.ap_public_url,
                                                remote_subscriber_inboxes(s.feed))

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
                announce_feed_delete_to_subscribers(s.owner.id, s.feed.ap_public_url,
                                                remote_subscriber_inboxes(s.feed))

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

    # NOT patch(...).start() + patch.stopall(): stopall() stops EVERY patcher in
    # unittest.mock's global registry, including the ones respx starts for
    # tests/conftest.py's session-scoped block_outbound_http. That silently
    # unblocked outbound HTTP for the whole rest of the session -- this file
    # sorts immediately before tests/test_shared_post_edit.py, whose
    # test_the_domain_block_is_skipped_for_a_hostless_url then failed with
    # "DID NOT RAISE Exception" and cascaded into 130 failures and 209 errors,
    # every one of which passed in isolation. Stop only what this test started.
    _task_session_patcher = patch('app.shared.feed.get_task_session')
    fake_session = _task_session_patcher.start()

    def fake_query(model):
        query = MagicMock()
        if model is User:
            query.get.return_value = remote
        elif model is Instance:
            query.get.return_value = s.instance
        return query

    def fake_get(model, ident, *args, **kwargs):
        """The same mapping for `session.get(Model, id)`, which is what
        production calls since sub-project 71. Without it the fake answers a
        bare MagicMock, `fm_user.is_local()` is truthy, the member is skipped
        before send_post_request is reached, and this test fails with DID NOT
        RAISE rather than with anything about the rollback it exists for.
        """
        if model is User:
            return remote
        if model is Instance:
            return s.instance
        return MagicMock()

    fake_session.return_value.query.side_effect = fake_query
    fake_session.return_value.get.side_effect = fake_get

    try:
        with patch('app.shared.feed.instance_banned', return_value=False):
            with patch('app.shared.feed.send_post_request', side_effect=RuntimeError('boom')):
                with pytest.raises(RuntimeError):
                    announce_feed_delete_to_subscribers(s.owner.id, s.feed.ap_public_url,
                                                remote_subscriber_inboxes(s.feed))
    finally:
        _task_session_patcher.stop()

    assert fake_session.return_value.rollback.call_count == 1
    assert fake_session.return_value.close.call_count == 1


def test_feed_add_community_moving_from_another_feed_deletes_the_old_item(app, db_session):
    """:389-399. current_feed_id != 0 means the community is MOVING.

    Reached in production only through app/feed/routes.py:350, which after
    this round's authorization fix requires the signed-in user to own BOTH
    feeds. make_feed:226 and edit_feed:350 always pass current_feed_id=0.

    Both counts are asserted: a mutant decrementing the wrong feed would leave
    these two numbers swapped, and the ids differ by construction, so the
    assertion can tell them apart.

    FIX (mutation round): nothing in this file asserted that :410-412 ever
    creates the new FeedItem. Replacing all three statements -- the
    FeedItem(), the db.session.add and the commit -- with `pass` left all 51
    tests passing, because the only add-path effect anything checked was
    num_communities, which :415-418 writes through a DIFFERENT object. The
    function's primary side effect was unobserved. The new row is asserted by
    BOTH its ids rather than by existence alone, so a mutant attaching it to
    the wrong feed (feed_id=current_feed_id) or naming the wrong community
    also dies here.

    The two ids are re-derived live rather than assumed: _seed's own
    assertion keeps {community.id, feed.id, actor.id, bystander_feed.id}
    four distinct values, and the pair below pins the two this assertion
    depends on, so a seed that ever let them coincide fails loudly at setup
    instead of making the filter_by match for the wrong reason.
    """
    s = _seed()
    old_item = make_feed_item(s.bystander_feed, s.community)
    s.bystander_feed.num_communities = 1
    s.feed.num_communities = 0
    db.session.commit()
    assert s.bystander_feed.id != s.feed.id
    assert s.community.id != s.feed.id

    with patch('app.community.routes.do_subscribe'):
        _feed_add_community(s.community.id, s.bystander_feed.id, s.feed.id, s.actor.id)

    assert db.session.get(FeedItem, old_item.id) is None
    new_item = db.session.query(FeedItem).filter_by(
        feed_id=s.feed.id, community_id=s.community.id).first()
    assert new_item is not None
    assert new_item.id != old_item.id
    assert s.bystander_feed.num_communities == 0
    assert s.feed.num_communities == 1


@pytest.mark.parametrize('public, expected_actions', [
    (True, ['Remove', 'Add']),
    (False, []),
])
def test_feed_add_community_announces_only_for_public_feeds(app, db_session, public, expected_actions):
    """:402 and :421, crossed with the move path so both fire in one call.

    Both feeds take the same `public` value here, so the row with True proves
    BOTH announce sites fire and the row with False proves neither does. The
    actions are compared in order because they are two calls from one function
    body, not query-planner rows.

    current_app.debug is FORCED True here (via patch.dict on app.config,
    which the Flask `debug` property reads/writes -- app.config['DEBUG']
    has no default override in TestConfig, so it is False without this) to
    take :404/:423's synchronous arm and land the call directly on `announce`
    rather than on `announce.delay`. `patch.object(app, 'debug', ...)` cannot
    be used for this: `debug` is a class-level property with no deleter, and
    mock's patch.object always tries to delattr a non-local attribute on
    __exit__, raising AttributeError.
    """
    s = _seed()
    make_feed_item(s.bystander_feed, s.community)
    s.bystander_feed.num_communities = 1
    s.bystander_feed.public = public
    s.feed.public = public
    db.session.commit()

    with patch('app.shared.feed.announce_feed_add_remove_to_subscribers') as announce:
        with patch('app.community.routes.do_subscribe'):
            with patch.dict(app.config, {'DEBUG': True}):
                _feed_add_community(s.community.id, s.bystander_feed.id, s.feed.id, s.actor.id)

    assert [c.args[0] for c in announce.call_args_list] == expected_actions


def test_feed_add_community_dispatches_the_announce_asynchronously_when_not_debugging(app, db_session):
    """:407 and :426's else arms -- the .delay branch for BOTH announce sites.

    Uses the move path (like the :389 test) so both :404-407 (the old feed's
    Remove announce) and :421-426 (the new feed's Add announce) fire in one
    call; a non-move call only ever reaches :426, leaving :407 unmeasured.
    Asserted as DISPATCHED, not executed: the test observes that .delay was
    called, with which feed/community pair, and never that the task body ran.

    current_app.debug reads app.config['DEBUG'], which TestConfig never
    overrides, so it is already False here; patch.dict pins that explicitly
    so this test does not depend on config staying that way.
    (patch.object(app, 'debug', False) cannot be used: `debug` is a
    class-level property with no deleter, and mock always tries to delattr a
    non-local attribute on __exit__, raising AttributeError: property 'debug'
    of 'Flask' object has no deleter.)
    """
    s = _seed()
    make_feed_item(s.bystander_feed, s.community)
    s.bystander_feed.num_communities = 1
    s.bystander_feed.public = True
    s.feed.public = True
    db.session.commit()

    with patch('app.shared.feed.announce_feed_add_remove_to_subscribers') as announce:
        with patch('app.community.routes.do_subscribe'):
            with patch.dict(app.config, {'DEBUG': False}):
                _feed_add_community(s.community.id, s.bystander_feed.id, s.feed.id, s.actor.id)

    assert announce.call_count == 0
    assert [c.args[0] for c in announce.delay.call_args_list] == ['Remove', 'Add']
    assert announce.delay.call_args_list[0].args == ('Remove', s.bystander_feed.id, s.community.id)
    assert announce.delay.call_args_list[1].args == ('Add', s.feed.id, s.community.id)


@pytest.mark.parametrize('already_member, auto_follow, expect_subscribe', [
    (False, True, 1),
    (True, True, 0),
    (False, False, 0),
])
def test_feed_add_community_subscribe_guard_isolates_both_operands(
        app, db_session, already_member, auto_follow, expect_subscribe):
    """:429 and :431's two operands against a passing control.

    Row 1 subscribes. Row 2 falsifies only the membership operand, row 3 only
    the preference, so neither can pass because of the other -- false-witness
    mechanism (d).

    :431 reads the preference from the user resolved (at :430) out of the
    user_id PARAMETER after this round's fix; before it, it read the request
    global. There is no request context here at all, which is what proves the
    parameter is the source.
    """
    s = _seed()
    if already_member:
        make_community_member(s.actor, s.community)
    s.actor.feed_auto_follow = auto_follow
    db.session.commit()

    with patch('app.community.routes.do_subscribe') as subscribe:
        _feed_add_community(s.community.id, 0, s.feed.id, s.actor.id)

    assert subscribe.call_count == expect_subscribe


def test_feed_add_community_subscribes_via_ap_id_when_the_community_has_one(app, db_session):
    """:435's conditional expression -- `community.ap_id if community.ap_id else community.name`.

    Two communities differing only in ap_id, so both arms of the expression are
    observed and neither is reached by the other's input. This is fact 75
    cause 7's shape (an arm of a conditional expression) if either proves
    unkillable, but both are reachable here.
    """
    s = _seed()
    s.community.ap_id = 'wiring@remote.example'
    s.bystander_community.ap_id = None
    db.session.commit()

    with patch('app.community.routes.do_subscribe') as subscribe:
        _feed_add_community(s.community.id, 0, s.feed.id, s.actor.id)
        first = subscribe.call_args.args[0]
        _feed_add_community(s.bystander_community.id, 0, s.feed.id, s.actor.id)
        second = subscribe.call_args.args[0]

    assert first == 'wiring@remote.example'
    assert second == s.bystander_community.name
    assert first != second


def test_feed_remove_community_deletes_the_item_and_decrements_the_count(app, db_session):
    """:440-448, with a bystander item that must survive.

    The bystander sits on a DIFFERENT feed and names a DIFFERENT community
    (s.bystander_feed / s.bystander_community) than doomed's (s.feed /
    s.community), so the bystander's community appears nowhere on the feed
    under test. Reusing s.community for the bystander -- sharing an axis with
    the row under test instead of being fully orthogonal to it -- is the
    shape that produced a bystander whose value could not distinguish
    anything once a later comparison collapsed it (Task 3's Major). This
    construction proves the delete targets the one row named by BOTH
    arguments and leaves an unrelated row alone; it does not, and does not
    claim to, distinguish which single filter_by a mutant might drop, since
    `:440`'s query has no ORDER BY and this campaign does not assert on
    query-planner row order.

    num_communities is pre-seeded to 1 so the decrement is visible as 1-to-0,
    not 0-to-0.
    """
    s = _seed()
    doomed = make_feed_item(s.feed, s.community)
    survivor = make_feed_item(s.bystander_feed, s.bystander_community)
    s.feed.num_communities = 1
    db.session.commit()

    _feed_remove_community(s.community.id, s.feed.id)

    assert db.session.get(FeedItem, doomed.id) is None
    assert db.session.get(FeedItem, survivor.id) is not None
    assert s.feed.num_communities == 0


@pytest.mark.parametrize('local, auto_leave, joined_via_feed, expect_removed', [
    (True, True, True, True),
    (False, True, True, False),
    (True, False, True, False),
    (True, True, False, False),
    (True, True, None, False),
])
def test_feed_remove_community_member_guard_isolates_every_isolatable_operand(
        app, db_session, local, auto_leave, joined_via_feed, expect_removed):
    """:456's four operands against a passing control in row 1.

    RENAMED AT THE FINAL WHOLE-BRANCH REVIEW, per fact 273 ("either
    strengthen the body or narrow the name"). It was
    ~~test_feed_remove_community_member_guard_isolates_each_operand~~, which
    claimed more than any row can witness: operand 3
    (`cm.joined_via_feed is not None`) is PROVABLY SUBSUMED by operand 4
    (fact 75 cause 3; D662), so no row isolates it and none ever could. The
    body is unchanged and still correct -- rows 2, 3 and 4 isolate operands
    1, 2 and 4 -- and rows 4 and 5 still separate False from None, which is
    what kills the operand-3-and-4 PAIR drop. The name now states exactly
    that.

    ON THE THIRD AND FOURTH OPERANDS. `cm.joined_via_feed is not None and
    cm.joined_via_feed` -- for a Boolean column the truthiness test alone
    excludes None, so the `is not None` conjunct looks subsumed. It is NOT
    dropped from this table: rows 4 and 5 separate False from None, which is
    what a later mutation pass needs in order to decide whether the conjunct
    is killable. This test only establishes that the two values ARE
    separately reachable inputs to the guard (row 4 sets False, row 5 sets
    None, and both reach expect_removed=False); whether a mutant that deletes
    the `is not None and` conjunct survives is for that later pass to
    determine and name against fact 75, not for this coverage task to
    pre-judge -- do not force-fit cause 3 here.
    """
    s = _seed()
    make_feed_item(s.feed, s.community)
    s.feed.num_communities = 1
    member = make_user(s.instance, 'member', local=local)
    member.feed_auto_leave = auto_leave
    db.session.commit()
    cm = make_community_member(member, s.community)
    cm.joined_via_feed = joined_via_feed
    db.session.commit()

    with patch('app.shared.feed.community_membership', return_value=0):
        _feed_remove_community(s.community.id, s.feed.id)

    remaining = db.session.query(CommunityMember).filter_by(
        user_id=member.id, community_id=s.community.id).first()
    assert (remaining is None) is expect_removed


def test_feed_remove_community_never_unsubscribes_a_community_owner(app, db_session):
    """:458's guard against SUBSCRIPTION_OWNER.

    community_membership is patched on app.shared.feed, where feed.py:20-21
    bound it. A second, non-owner member is the positive control, so this
    cannot pass because the loop never ran.
    """
    from app.constants import SUBSCRIPTION_OWNER

    s = _seed()
    make_feed_item(s.feed, s.community)
    s.feed.num_communities = 1
    owner_member = make_user(s.instance, 'commowner', local=True)
    plain_member = make_user(s.instance, 'plainmember', local=True)
    db.session.commit()
    for u in (owner_member, plain_member):
        cm = make_community_member(u, s.community)
        cm.joined_via_feed = True
    db.session.commit()

    def membership(user, community):
        return SUBSCRIPTION_OWNER if user.id == owner_member.id else 0

    with patch('app.shared.feed.community_membership', side_effect=membership):
        _feed_remove_community(s.community.id, s.feed.id)

    still = {cm.user_id for cm in db.session.query(CommunityMember).filter_by(
        community_id=s.community.id).all()}
    assert still == {owner_member.id}


def test_feed_remove_community_sends_an_undo_follow_for_a_remote_community(app, db_session):
    """:461-485. The Undo wraps the Follow it is undoing.

    Asserting the nesting is what makes :470-483 observable; asserting only
    the outer type would pass under a mutant that sent an empty object. The
    community is remote and its instance is not gone_forever, so both guards
    at :461 and :462 take their True arms.

    Community.is_local() (app/models.py:795) reads self.profile_id(), not
    self.ap_id directly -- profile_id() falls back to ap_profile_id, which
    make_community sets to a LOCAL host by default. Setting only ap_id to a
    remote value leaves is_local() reading the still-local ap_profile_id and
    returning True, so ap_profile_id is overridden here too.

    FIX (mutation round): this test inspected only the payload (args[1]), so
    THREE mutants on :484-485 survived -- the destination
    (community.ap_inbox_url -> community.ap_public_url) and both signing
    credentials (user.private_key -> user.public_key, user.public_url() ->
    community.public_url()). This is the third of the module's three
    send_post_request call sites and the only one no task had examined; the
    same gap was closed at :605 by Task 5 and at :561 in this round.

    Note this site's destination is community.ap_inbox_url, NOT the
    instance.inbox the two announce tasks send to -- so the destination is
    asserted here as well as the credentials. The three separations are
    pinned live before the call: ap_inbox_url vs ap_public_url (both set
    above to deliberately different paths on the same host), the member's
    private_key vs public_key (distinct because with_keys=True generates a
    real RSA pair), and the member's public_url() vs the community's.
    """
    s = _seed()
    s.community.ap_id = 'wiring@remote.example'
    s.community.ap_profile_id = 'https://remote.example/c/wiring'
    s.community.ap_public_url = 'https://remote.example/c/wiring'
    s.community.ap_inbox_url = 'https://remote.example/inbox'
    s.instance.gone_forever = False
    make_feed_item(s.feed, s.community)
    s.feed.num_communities = 1
    member = make_user(s.instance, 'member', local=True, with_keys=True)
    db.session.commit()
    cm = make_community_member(member, s.community)
    cm.joined_via_feed = True
    db.session.commit()

    assert s.community.ap_inbox_url != s.community.ap_public_url
    assert member.private_key is not None and member.public_key is not None
    assert member.private_key != member.public_key
    assert member.public_url() != s.community.public_url()

    with patch('app.shared.feed.community_membership', return_value=0):
        with patch('app.shared.feed.send_post_request') as send:
            _feed_remove_community(s.community.id, s.feed.id)

    assert send.call_count == 1
    undo = send.call_args.args[1]
    assert undo['type'] == 'Undo'
    assert undo['object']['type'] == 'Follow'
    assert undo['object']['object'] == s.community.public_url()
    assert send.call_args.args[0] == s.community.ap_inbox_url
    assert send.call_args.args[2] == member.private_key
    assert send.call_args.args[3] == member.public_url() + '#main-key'


def test_feed_remove_community_reuses_the_join_request_uuid(app, db_session):
    """D89, fixed (owner ruling). The stored join request's uuid -- the id the
    original Follow was sent with -- was reused only when the instance was
    the hardcoded 'ovo.st'. Every peer now gets it whenever the row exists;
    the instance here is the ordinary one `_seed` makes.

    CommunityJoinRequest.uuid is a UUID(as_uuid=True) column defaulting to
    uuid.uuid4 (app/models.py:3631), NOT a string -- so the row is built by
    the factory and its generated value is read back, rather than a literal
    being assigned. gibberish is patched so the generated follow_id can never
    collide with the uuid and pass this assertion by accident.

    expected_uuid is captured BEFORE the call, not read off `jr` afterward:
    :489 deletes this same CommunityJoinRequest row inside
    _feed_remove_community, so reading `jr.uuid` post-call touches an expired
    ORM object and raises sqlalchemy.orm.exc.ObjectDeletedError instead of
    ever reaching the comparison -- a crash, not a kill, for a mutant that
    skips the lookup. Capturing the value first lets that mutant die by a
    clean AssertionError on the endswith check below.
    """
    s = _seed()
    s.community.ap_id = 'wiring@remote.example'
    s.community.ap_profile_id = 'https://remote.example/c/wiring'
    s.community.ap_public_url = 'https://remote.example/c/wiring'
    s.community.ap_inbox_url = 'https://remote.example/inbox'
    s.instance.gone_forever = False
    make_feed_item(s.feed, s.community)
    s.feed.num_communities = 1
    member = make_user(s.instance, 'member', local=True, with_keys=True)
    db.session.commit()
    cm = make_community_member(member, s.community)
    cm.joined_via_feed = True
    db.session.commit()
    jr = make_community_join_request(member, s.community)
    expected_uuid = str(jr.uuid)

    with patch('app.shared.feed.community_membership', return_value=0):
        with patch('app.shared.feed.gibberish', return_value='NOTTHEUUID'):
            with patch('app.shared.feed.send_post_request') as send:
                _feed_remove_community(s.community.id, s.feed.id)

    undo = send.call_args.args[1]
    assert undo['object']['id'].endswith(expected_uuid)
    assert 'NOTTHEUUID' not in undo['object']['id']


def test_feed_remove_community_generates_a_follow_id_with_no_stored_join_request(app, db_session):
    """D89's other half: the same setup as the test above, minus the
    CommunityJoinRequest row. The lookup returns None and the generated
    follow_id (built from the patched gibberish) is what reaches the
    Follow/Undo.
    """
    s = _seed()
    s.community.ap_id = 'wiring@remote.example'
    s.community.ap_profile_id = 'https://remote.example/c/wiring'
    s.community.ap_public_url = 'https://remote.example/c/wiring'
    s.community.ap_inbox_url = 'https://remote.example/inbox'
    s.instance.gone_forever = False
    make_feed_item(s.feed, s.community)
    s.feed.num_communities = 1
    member = make_user(s.instance, 'member', local=True, with_keys=True)
    db.session.commit()
    cm = make_community_member(member, s.community)
    cm.joined_via_feed = True
    db.session.commit()
    assert db.session.query(CommunityJoinRequest).filter_by(
        user_id=member.id, community_id=s.community.id).first() is None

    with patch('app.shared.feed.community_membership', return_value=0):
        with patch('app.shared.feed.gibberish', return_value='GENERATEDID'):
            with patch('app.shared.feed.send_post_request') as send:
                _feed_remove_community(s.community.id, s.feed.id)

    undo = send.call_args.args[1]
    assert undo['object']['id'].endswith('GENERATEDID')


def test_feed_remove_community_skips_delivery_to_a_dead_instance(app, db_session):
    """:462's False arm. The membership row is still removed at :488.

    That second assertion is the point: a mutant that skipped the whole body
    rather than only the delivery would leave the row in place, so asserting
    the send count alone would not distinguish them.

    ap_profile_id (not just ap_id) must move off the default local host: :461
    reads community.is_local(), which reads profile_id() -- ap_profile_id if
    set, else a SERVER_URL-prefixed fallback -- not ap_id directly.
    make_community sets ap_profile_id to a local host by default, so setting
    only ap_id here would leave is_local() reading True, take :461's False
    arm instead of its True arm, and never reach :462 at all. Caught by the
    branch-coverage pass: an earlier draft set only ap_id and left the
    [462, 487] arc (the True arm this test targets) missing.
    """
    s = _seed()
    s.community.ap_id = 'wiring@remote.example'
    s.community.ap_profile_id = 'https://remote.example/c/wiring'
    s.community.ap_public_url = 'https://remote.example/c/wiring'
    s.instance.gone_forever = True
    make_feed_item(s.feed, s.community)
    s.feed.num_communities = 1
    member = make_user(s.instance, 'member', local=True)
    db.session.commit()
    cm = make_community_member(member, s.community)
    cm.joined_via_feed = True
    db.session.commit()

    with patch('app.shared.feed.community_membership', return_value=0):
        with patch('app.shared.feed.send_post_request') as send:
            _feed_remove_community(s.community.id, s.feed.id)

    assert send.call_count == 0
    assert db.session.query(CommunityMember).filter_by(
        user_id=member.id, community_id=s.community.id).first() is None


def test_feed_remove_community_removes_membership_without_federating_for_a_local_community(app, db_session):
    """:461's False arm -- a local community needs no Undo.

    subscriptions_count is asserted because :490 decrements it inside the
    same `if proceed:` body (:487), so a mutant narrowing that body would be
    caught here.

    BODY STRENGTHENED AT THE FINAL WHOLE-BRANCH REVIEW, per fact 273. The
    name says "removes membership" and, until now, nothing here witnessed
    :488's CommunityMember delete -- only send.call_count and
    subscriptions_count. Three OTHER tests happened to witness the delete,
    which is exactly the shape fact 273 warns about: the next reader greps
    for the property and lands on the test whose name claims it. The row
    assertion below is added rather than the name narrowed, because the body
    CAN witness the claim.

    :487's `if proceed:` is a TAUTOLOGY: `proceed = True` is set
    unconditionally at :459 and is never reassigned anywhere in the function,
    so the False arm is dead code and no test here pretends to reach it. This
    is the instance that justifies enacting fact 75 cause 9 (D578 -- a
    tautology's mirror image of fact 75's dead-True-branch shape, proposed
    but not yet folded into tests/README.md's fact 75 at the time this test
    was written); cite cause 9 directly once that landing gives it a number.
    """
    s = _seed()
    s.community.ap_id = None
    s.community.subscriptions_count = 1
    make_feed_item(s.feed, s.community)
    s.feed.num_communities = 1
    member = make_user(s.instance, 'member', local=True)
    db.session.commit()
    cm = make_community_member(member, s.community)
    cm.joined_via_feed = True
    db.session.commit()

    with patch('app.shared.feed.community_membership', return_value=0):
        with patch('app.shared.feed.send_post_request') as send:
            _feed_remove_community(s.community.id, s.feed.id)

    assert send.call_count == 0
    assert s.community.subscriptions_count == 0
    assert db.session.query(CommunityMember).filter_by(
        user_id=member.id, community_id=s.community.id).first() is None


@pytest.mark.parametrize('public, expected_calls', [(True, 1), (False, 0)])
def test_feed_remove_community_announces_only_for_a_public_feed(app, db_session, public, expected_calls):
    """:497-501, with the debug fork's synchronous arm at :498.

    current_app.debug is FORCED True here (via patch.dict on app.config,
    which TestConfig never overrides, so it is False without this) to take
    :498's synchronous arm and land the call on `announce` itself rather than
    on `announce.delay` -- otherwise the True row's call would arrive on
    `.delay` and `announce.call_count` would read 0 regardless of `public`.
    """
    s = _seed()
    make_feed_item(s.feed, s.community)
    s.feed.num_communities = 1
    s.feed.public = public
    db.session.commit()

    with patch('app.shared.feed.announce_feed_add_remove_to_subscribers') as announce:
        with patch.dict(app.config, {'DEBUG': True}):
            _feed_remove_community(s.community.id, s.feed.id)

    assert announce.call_count == expected_calls
    if expected_calls:
        assert announce.call_args.args == ('Remove', s.feed.id, s.community.id)


def test_feed_remove_community_dispatches_the_announce_asynchronously_when_not_debugging(app, db_session):
    """:501's else arm. Asserted as dispatched, never as executed.

    current_app.debug reads app.config['DEBUG']; patch.dict pins it False
    explicitly rather than relying on TestConfig's default staying that way.
    `patch.object(app, 'debug', False)` cannot be used here: `debug` is a
    class-level property on Flask with no deleter, and mock's patch.object
    always tries to delattr a non-local attribute on __exit__, raising
    AttributeError: property 'debug' of 'Flask' object has no deleter.
    """
    s = _seed()
    make_feed_item(s.feed, s.community)
    s.feed.num_communities = 1
    s.feed.public = True
    db.session.commit()

    with patch('app.shared.feed.announce_feed_add_remove_to_subscribers') as announce:
        with patch.dict(app.config, {'DEBUG': False}):
            _feed_remove_community(s.community.id, s.feed.id)

    assert announce.delay.call_count == 1
    assert announce.call_count == 0


def test_announce_add_remove_does_not_deliver_to_an_opted_out_local_member(app, db_session):
    """D670, fixed: the local-member `continue` sat inside the
    `is_local() and feed_auto_follow` branch, so a local member who had opted
    out of auto-follow fell through into the remote-delivery block and was
    sent a federated Announce at their own instance (latent: the local
    instance normally has no inbox, set by hand here). The skip is now hoisted
    so every local member skips delivery; the consent gate on do_subscribe is
    unchanged, so the member is still not subscribed.
    """
    s = _seed()
    optout = make_user(s.instance, 'optoutlocal', local=True)
    optout.feed_auto_follow = False
    s.instance.inbox = 'https://optout.piefed.local/inbox'
    db.session.commit()
    make_feed_member(optout, s.feed)
    assert optout.is_local() is True
    assert optout.id != s.feed.user_id

    with patch('app.community.routes.do_subscribe') as subscribe:
        with patch('app.shared.feed.send_post_request') as send:
            with patch('app.shared.feed.instance_banned', return_value=False):
                announce_feed_add_remove_to_subscribers('Add', s.feed.id, s.community.id)

    assert subscribe.call_count == 0
    assert send.call_count == 0


# ==========================================================================
# Sub-project 50, Task 11: D661's eight carried-forward recipes, EXECUTED.
#
# Sub-project 49 registered twenty surviving mutants across eight findings
# (D661 items D-K), each with a closing test written out but -- for seven of
# the eight -- never run. Item (K) was run at 49's final review and found to be
# wrong as written. These are the remaining seven, executed rather than
# trusted: each test below was run against the unmutated tree first, then
# against the mutant it names, which must fail.
#
# LINE NUMBERS IN THESE DOCSTRINGS ARE D661's, WHICH ARE SUB-PROJECT 49's TREE.
# Sub-project 50's repairs added 31 lines above Group A, so every site moved.
# The mapping, derived by locating each site by content at 1668391e:
#   D661 :405 -> :436   :424 -> :455   :429 -> :460   :451 -> :482
#         :455 -> :487  :481 -> :512   :488 -> :519   :489 -> :520
#         :501 -> :532  :514 -> :545   :515 -> :546   :522 -> :553
#         :538 -> :569  :554 -> :585   :588 -> :619   :620 -> :651
#         :626 -> :657
# The register's rule applies as always: locate the code by its content -- the
# function name, the guard, the header string quoted -- never by the number.
# ==========================================================================


def test_announce_add_remove_names_the_feed_at_both_levels_and_carries_two_ids(app, db_session):
    """D661 item (D): the Announce's actor and id were asserted nowhere.

    Three mutants survived 49: swapping feed.ap_public_url to
    community.ap_public_url at the activity's actor, the same swap at the
    embedded object's actor, and replacing the activity id with a literal.

    The feed's and the community's ap_public_url are asserted distinct BEFORE
    the call, or the swap mutants would be satisfied by a coincidence rather
    than by the code. The two ids are asserted distinct from each other as
    well: they come from separate gibberish() calls under different path
    prefixes, and a mutant reusing one for the other would otherwise pass.

    Registered divergence, from D661: the DELETE twin does assert its actor;
    this add/remove twin did not.
    """
    s = _seed()
    s.feed.ap_following_url = 'https://test.piefed.local/f/wiringfeed/following'
    s.feed.private_key = 'feedprivatekeymaterial'
    remote = make_user(s.instance, 'remotemember', local=False)
    s.instance.inbox = 'https://remote.example/inbox'
    db.session.commit()
    make_feed_member(remote, s.feed)

    assert s.feed.ap_public_url and s.community.ap_public_url
    assert s.feed.ap_public_url != s.community.ap_public_url

    with patch('app.shared.feed.send_post_request') as send:
        with patch('app.shared.feed.instance_banned', return_value=False):
            announce_feed_add_remove_to_subscribers('Add', s.feed.id, s.community.id)

    activity = send.call_args.args[1]
    assert activity['actor'] == s.feed.ap_public_url
    assert activity['object']['actor'] == s.feed.ap_public_url
    assert '/activities/announce/' in activity['id']
    assert '/activities/feedadd/' in activity['object']['id']
    assert activity['id'] != activity['object']['id']


@pytest.mark.parametrize('community_has_ap_id', [True, False])
def test_announce_add_remove_subscribes_a_local_member_through_the_right_actor(
        app, db_session, community_has_ap_id):
    """D661 item (E): the ternary choosing the subscribe actor was unasserted,
    and BOTH collapses survived.

    D661 recorded that this is NOT fact 75 cause 7 -- the arms are genuinely
    distinguishable -- and that the identical expression in
    _feed_add_community has a dedicated test whose collapses both die. This is
    the plain assertion gap that was left.

    The two rows differ only in whether the community carries an ap_id, and the
    name and the ap_id are deliberately different strings, so neither collapse
    can pass as the other.
    """
    s = _seed()
    s.community.ap_id = 'wiring@far.piefed.local' if community_has_ap_id else None
    local_member = make_user(s.instance, 'localmember', local=True)
    local_member.feed_auto_follow = True
    db.session.commit()
    make_feed_member(local_member, s.feed)
    assert s.community.name != 'wiring@far.piefed.local'

    with patch('app.community.routes.do_subscribe') as subscribe:
        with patch('app.shared.feed.send_post_request'):
            announce_feed_add_remove_to_subscribers('Add', s.feed.id, s.community.id)

    assert subscribe.call_count == 1
    expected = 'wiring@far.piefed.local' if community_has_ap_id else s.community.name
    assert subscribe.call_args.args[0] == expected


def test_announce_add_remove_delivers_only_to_this_feeds_members(app, db_session):
    """D661 item (G), the :538 filter_by drop: the FeedMember lookup's feed_id
    filter was free because no fixture ever had a member of a second feed.

    The bystander here is a remote member of s.bystander_feed. Under the
    mutant every feed's members are announced to, so the send count rises from
    one to two and the delivery is addressed on behalf of the wrong feed.
    """
    s = _seed()
    s.feed.private_key = 'feedprivatekeymaterial'
    ours = make_user(s.instance, 'ourmember', local=False)
    theirs = make_user(s.instance, 'theirmember', local=False)
    s.instance.inbox = 'https://remote.example/inbox'
    db.session.commit()
    make_feed_member(ours, s.feed)
    make_feed_member(theirs, s.bystander_feed)

    with patch('app.shared.feed.send_post_request') as send:
        with patch('app.shared.feed.instance_banned', return_value=False):
            announce_feed_add_remove_to_subscribers('Add', s.feed.id, s.community.id)

    assert send.call_count == 1


def test_announce_delete_delivers_only_to_this_feeds_members(app, db_session):
    """D661 item (G), the :588 filter_by drop -- the delete twin of the test
    above. Both twins are covered separately rather than by a shared helper,
    because a shared helper is what hides the next divergence between them."""
    s = _seed()
    s.owner.private_key = 'ownerprivatekeymaterial'
    ours = make_user(s.instance, 'ourmember', local=False)
    theirs = make_user(s.instance, 'theirmember', local=False)
    s.instance.inbox = 'https://remote.example/inbox'
    db.session.commit()
    make_feed_member(ours, s.feed)
    make_feed_member(theirs, s.bystander_feed)

    with patch('app.shared.feed.send_post_request') as send:
        with patch('app.shared.feed.instance_banned', return_value=False):
            announce_feed_delete_to_subscribers(s.owner.id, s.feed.ap_public_url,
                                                remote_subscriber_inboxes(s.feed))

    assert send.call_count == 1


def test_feed_add_community_subscribe_probe_is_scoped_to_this_user_and_community(app, db_session):
    """D661 item (G), the two :429 filter_by drops on the membership probe.

    Two bystander memberships, one per filter: another user in the SAME
    community kills the user_id drop, and this user in ANOTHER community kills
    the community_id drop. Either mutant finds a row, concludes the acting user
    is already a member, and skips the subscribe that must happen.
    """
    s = _seed()
    other_user = make_user(s.instance, 'someoneelse')
    make_community_member(other_user, s.community)
    make_community_member(s.actor, s.bystander_community)
    s.actor.feed_auto_follow = True
    db.session.commit()

    with app.test_request_context('/'):
        with patch('app.community.routes.do_subscribe') as subscribe:
            _feed_add_community(s.community.id, 0, s.feed.id, s.actor.id)

    assert subscribe.call_count == 1
    assert subscribe.call_args.args[1] == s.actor.id


def test_feed_add_community_announces_with_both_ids_on_each_arm(app, db_session):
    """D661 item (F): the announce ids at :405 and :424 were unasserted -- the
    action literal was checked and the two ids that follow it were not -- and
    :501's whole .delay argument tuple was free, its test asserting only
    call_count == 1.

    One test covers all three sites because they are the same argument tuple in
    three places: the move's Remove, the add's Add, and the remove function's
    dispatched Remove. The feed ids and the community id are asserted pairwise
    distinct first, so a mutant passing the wrong one of them cannot coincide.
    """
    s = _seed()
    make_feed_item(s.bystander_feed, s.community)
    s.bystander_feed.public = True
    s.bystander_feed.num_communities = 1
    s.feed.public = True
    db.session.commit()
    assert len({s.feed.id, s.bystander_feed.id, s.community.id}) == 3

    with app.test_request_context('/'):
        with patch('app.shared.feed.announce_feed_add_remove_to_subscribers') as announce, \
                patch('app.shared.feed.current_app', new_callable=MagicMock) as current_app_stub:
            current_app_stub.debug = True
            _feed_add_community(s.community.id, s.bystander_feed.id, s.feed.id, s.actor.id)

    assert [call.args for call in announce.call_args_list] == [
        ('Remove', s.bystander_feed.id, s.community.id),
        ('Add', s.feed.id, s.community.id),
    ]

    make_feed_item(s.feed, s.community)
    s.feed.num_communities = 1
    db.session.commit()

    with app.test_request_context('/'):
        with patch('app.shared.feed.announce_feed_add_remove_to_subscribers') as announce, \
                patch('app.shared.feed.current_app', new_callable=MagicMock) as current_app_stub:
            current_app_stub.debug = False
            _feed_remove_community(s.community.id, s.feed.id)

    assert announce.delay.call_args.args == ('Remove', s.feed.id, s.community.id)


def test_feed_remove_community_only_sweeps_members_of_that_community(app, db_session):
    """D661 item (G), the :451 filter_by drop on the member sweep.

    The bystander is a member of s.bystander_community who satisfies every
    operand of the guard -- local, feed_auto_leave, joined_via_feed -- so the
    only thing keeping their membership is the community_id filter the mutant
    drops.
    """
    s = _seed()
    make_feed_item(s.feed, s.community)
    s.feed.num_communities = 1
    # The sweeping member must be LOCAL: :455's first operand is
    # user.is_local(), and make_user leaves ap_id set unless local=True, so a
    # seed actor never enters the loop body at all.
    sweeper = make_user(s.instance, 'localsweeper', local=True)
    sweeper.feed_auto_leave = True
    swept = make_community_member(sweeper, s.community)
    swept.joined_via_feed = True
    bystander_user = make_user(s.instance, 'elsewheremember', local=True)
    bystander_user.feed_auto_leave = True
    spared = make_community_member(bystander_user, s.bystander_community)
    spared.joined_via_feed = True
    s.community.subscriptions_count = 5
    db.session.commit()

    _feed_remove_community(s.community.id, s.feed.id)

    assert CommunityMember.query.filter_by(user_id=sweeper.id,
                                           community_id=s.community.id).count() == 0
    assert CommunityMember.query.filter_by(user_id=bystander_user.id,
                                           community_id=s.bystander_community.id).count() == 1
    # The counter is the assertion that actually catches the dropped filter.
    # A sweep over EVERY CommunityMember row still deletes only rows named by
    # the community being removed -- :488's delete names it explicitly -- so
    # the bystander's membership survives either way, and the visible
    # difference is that the bystander's iteration decrements this community's
    # subscriptions_count a second time. Found by running the mutant: the
    # first version of this test asserted only the two memberships and the
    # mutant survived it.
    assert db.session.get(Community, s.community.id).subscriptions_count == 4


def test_feed_remove_community_unsubscribes_only_the_member_it_is_processing(app, db_session):
    """D661 item (G), the :488 filter_by drop on the membership delete, and
    item (H), the CommunityJoinRequest delete that `pass` replaced unnoticed.

    The spared member is remote, so the guard's is_local() operand keeps them
    out of the sweep, and their membership and join request must both survive.
    Under the user_id drop every member of the community loses their row; under
    item (H)'s mutant the join request rows stay behind, which is the same
    stale-request shape that locks a user out in leave_feed.
    """
    s = _seed()
    make_feed_item(s.feed, s.community)
    s.feed.num_communities = 1
    sweeper = make_user(s.instance, 'localsweeper', local=True)
    sweeper.feed_auto_leave = True
    swept = make_community_member(sweeper, s.community)
    swept.joined_via_feed = True
    make_community_join_request(sweeper, s.community)
    remote_member = make_user(s.instance, 'remoteholdout', local=False)
    spared = make_community_member(remote_member, s.community)
    spared.joined_via_feed = True
    make_community_join_request(remote_member, s.community)
    db.session.commit()

    _feed_remove_community(s.community.id, s.feed.id)

    assert CommunityMember.query.filter_by(user_id=sweeper.id,
                                           community_id=s.community.id).count() == 0
    assert CommunityJoinRequest.query.filter_by(user_id=sweeper.id,
                                                community_id=s.community.id).count() == 0
    assert CommunityMember.query.filter_by(user_id=remote_member.id,
                                           community_id=s.community.id).count() == 1
    assert CommunityJoinRequest.query.filter_by(user_id=remote_member.id,
                                                community_id=s.community.id).count() == 1


def test_feed_remove_community_undo_carries_an_id_of_its_own(app, db_session):
    """D661 item (I): the Undo's id was unasserted, so the wrapper could carry
    the inner Follow's own id and nothing noticed.

    Both ids are asserted, and asserted DIFFERENT: they come from separate
    gibberish() calls under different path prefixes, so an Undo reusing the
    Follow's id is a distinguishable wrong answer rather than a cosmetic one.
    """
    s = _seed()
    remote_instance = make_instance('undo.piefed.local')
    remote_community = make_community(name='undocommunity', host='undo.piefed.local')
    remote_community.ap_id = 'undocommunity@undo.piefed.local'
    remote_community.ap_inbox_url = 'https://undo.piefed.local/c/undocommunity/inbox'
    remote_community.instance_id = remote_instance.id
    make_feed_item(s.feed, remote_community)
    s.feed.num_communities = 1
    sweeper = make_user(s.instance, 'localsweeper', local=True)
    sweeper.feed_auto_leave = True
    sweeper.private_key = 'actorprivatekeymaterial'
    member = make_community_member(sweeper, remote_community)
    member.joined_via_feed = True
    db.session.commit()

    with patch('app.shared.feed.send_post_request') as send:
        _feed_remove_community(remote_community.id, s.feed.id)

    assert send.call_count == 1
    undo = send.call_args.args[1]
    assert undo['type'] == 'Undo'
    assert '/activities/undo/' in undo['id']
    assert '/activities/follow/' in undo['object']['id']
    assert undo['id'] != undo['object']['id']


@pytest.mark.parametrize('raw, expected_lookup', [
    ('  spaced  ', '!spaced@test.piefed.local'),
    ('trailing@remote.example \nsecond@remote.example', '!trailing@remote.example'),
])
def test_form_communities_to_ids_strips_whitespace_at_both_sites(app, db_session, raw,
                                                                 expected_lookup):
    """D661 item (J): both .strip() calls were unasserted, because no fixture
    supplied leading or trailing whitespace.

    One row per site. The first row exercises the strip on the whole input:
    without it the lookup string carries the spaces inside the '!' prefix and
    the '@host' suffix. The second row exercises the per-entry strip, which
    only shows on a line that is NOT the last one -- the outer strip would
    otherwise have removed the same whitespace, which is why a single-line
    fixture cannot distinguish the two sites.
    """
    s = _seed()
    with patch('app.community.util.search_for_community', return_value=s.community) as search:
        with app.test_request_context('/'):
            form_communities_to_ids(raw)

    assert search.call_args_list[0].args[0] == expected_lookup
