"""`edit_community` -- the AP Update sender for a local community's Group actor.

`app/shared/tasks/groups.py`, 149 lines, ONE function at `:52`. Fifty-four
statements and TWENTY-FOUR branches -- the densest ratio of any module this
campaign has closed. Most of that density is `:85-116`, where four optional
fields each add an arm and two of them branch AGAIN on whether the stored
image url is absolute (`:90`, `:101`).

TWO EARLY RETURNS, AND THE SECOND IS ONLY REACHABLE PAST THE FIRST. `:59`
returns if the community is `local_only`, `private`, or its instance is not
`online()`; `:62-63` returns unless the acting user moderates the community.
`Community.is_moderator` (app/models.py:736-740) checks `moderator.user_id ==
user.id` over `self.moderators()`. So every test past the guards needs a
moderator, which `_seed` supplies.

`:59` DEREFERENCES `community.instance` -- THE FIRST TIME THIS MODULE EVER
HAS. `Community.instance_id` is a nullable FK, so a community row with no
instance would raise `AttributeError` on `not community.instance.online()`
where it previously federated without incident. `private` is ordered before
the `online()` call precisely so a PRIVATE such community still returns at
the guard via short-circuit rather than reaching the dereference; a non-private
community with a null `instance_id`, however, would still raise. No test in
this file constructs that state -- every seeded community gets a real
instance via `make_community`/`_make_deliverable` -- so this is a new crash
surface being registered, not one being tested here, and it is expected to be
rare in practice since every known creation path sets `instance_id`.

DELIVERY REQUIRES COMMUNITY MEMBERSHIP, NOT MERELY AN INSTANCE ROW.
`Community.following_instances()` (app/models.py:842-851) joins
CommunityMember and filters `Instance.id != 1`, so a recipient needs an
Instance, an inbox, AND a user on that instance who is a member of the
community. Without the membership the loop at `:140` never runs and every
delivery assertion passes against zero deliveries.

ORDER IS NEVER ASSERTED. `following_instances()` ends in an unordered
`.distinct().all()`.
"""

import json
import socket
from types import SimpleNamespace

import pytest

from app import db
from app.models import ActivityPubLog
from app.shared.tasks.groups import edit_community
from tests.factories import (
    make_community, make_community_member, make_file, make_instance, make_user,
)

PEER_INBOX = 'https://peer.example/inbox'
OTHER_INBOX = 'https://other.example/inbox'

_REAL_GETADDRINFO = socket.getaddrinfo
_EXAMPLE_TLD_ADDRESS = '93.184.216.34'


def _peer(domain='peer.example', software='lemmy'):
    """A remote Instance. ALWAYS call AFTER `_seed()` -- see `_seed` for why."""
    return make_instance(domain, software=software)


def _getaddrinfo_without_the_network(host, *args, **kwargs):
    """`socket.getaddrinfo`, answering for `.example` hosts without a resolver.

    Everything else is delegated to the real function unchanged.
    """
    if isinstance(host, str) and (host == 'example' or host.endswith('.example')):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '',
                 (_EXAMPLE_TLD_ADDRESS, 0))]
    return _REAL_GETADDRINFO(host, *args, **kwargs)


@pytest.fixture(autouse=True)
def _peer_example_resolves_without_a_resolver(monkeypatch):
    """Keep delivery off the machine's DNS resolver.

    THIS IS NOT OPTIONAL AND MUST NOT BE DELETED AS UNNECESSARY. Signing runs
    `is_invalid_get_request_uri`, which reaches `socket.getaddrinfo` at
    app/utils.py:5520 for POST as well as GET, and FAILS OPEN at :5521-5522.
    On a machine whose resolver hijacks NXDOMAIN into a wildcard A record,
    these tests start failing at app/utils.py:5530's `is_global` check instead.
    """
    monkeypatch.setattr(socket, 'getaddrinfo', _getaddrinfo_without_the_network)


def _make_deliverable(s, online=True):
    """Move the community off instance id 1 onto a real peer Instance.

    Transferred from this campaign's other AP-sender harnesses, where an
    equivalent helper puts the community's OWN instance into the state a
    same-shaped online/offline gate reads before delivery. `edit_community`
    has NO such gate -- it never inspects `community.instance` at all, only
    `local_only` (`:59`) and `is_moderator` (`:62`) before building the
    envelope, and the loop at `:140` checks each FOLLOWING instance's
    `.online()`, never the community's own. So this helper's effect is inert
    with respect to every assertion in this file; it is kept only for parity
    with the shared harness shape, and for the `online=False` toggle in case
    a later test in this file needs it.

    `Instance.online()` (app/models.py:118-119) is exactly
    `not (self.dormant or self.gone_forever)`, so `online=False` sets both.
    Returns the peer.
    """
    peer = _peer()
    if not online:
        peer.dormant = True
        peer.gone_forever = True
    s.community.instance_id = peer.id
    db.session.commit()
    return peer


def _sent_activity(route, index=-1):
    """The JSON body of the request `route` captured, decoded from the bytes
    that were actually sent. Defaults to the most recent call.
    """
    return json.loads(route.calls[index].request.content)


def _delivered_inboxes(*routes):
    """The SET of inboxes that received a request. Never a list, never ordered
    -- `following_instances()` ends in an unordered `.distinct().all()`."""
    return {str(r.calls[i].request.url) for r in routes for i in range(len(r.calls))}


def _seed(local_community=True, with_keys=False):
    """instance, user, community -- committed, with `user` a MODERATOR.

    ORDER IS LOAD-BEARING. `make_community` (tests/factories.py:123) hardcodes
    `instance_id=1` and the db_session teardown resets every sequence, so the
    local instance is created FIRST; a peer built before this call would take
    id 1 and leave the community's FK pointing at it.

    THE MODERATOR MEMBERSHIP IS NOT OPTIONAL. `edit_community:62` returns at
    `:63` unless `community.is_moderator(user)`, which checks
    `moderator.user_id == user.id` over `self.moderators()`
    (app/models.py:736-740). Without it every test past `:62` would assert
    against an early return.

    `local_community=False` sets `ap_id` AND `ap_profile_id` on peer.example,
    because `Community.is_local()` (app/models.py:796) is a DISJUNCTION whose
    `profile_id()` falls back to a computed default; `ap_id` alone leaves the
    community silently LOCAL.
    """
    instance = make_instance('test.piefed.local', software='piefed')
    user = make_user(instance, 'themod', local=True, with_keys=with_keys)
    community = make_community('c1')
    if with_keys:
        community.private_key = user.private_key
        community.public_key = user.public_key
    make_community_member(user, community, is_moderator=True)
    if not local_community:
        community.ap_id = 'c1@peer.example'
        community.ap_profile_id = 'https://peer.example/c/c1'
        community.ap_public_url = 'https://peer.example/c/c1'
        community.ap_inbox_url = PEER_INBOX
        community.ap_domain = 'peer.example'
    db.session.commit()
    return SimpleNamespace(instance=instance, user=user, community=community)


def _follower(s, http_mock, inbox=PEER_INBOX, domain='follower.example'):
    """A remote instance `following_instances()` will actually return.

    THREE parts, and the third is the one that is easy to miss: an Instance
    row, an inbox, and a USER ON THAT INSTANCE WHO IS A MEMBER OF THE
    COMMUNITY. Without the membership the query returns nothing, `:140`'s loop
    never runs, and every delivery assertion here passes against zero
    deliveries.

    Returns `(route, instance)`.
    """
    inst = make_instance(domain, software='lemmy')
    inst.inbox = inbox
    member_user = make_user(inst, f'member_{domain.split(".")[0]}')
    make_community_member(member_user, s.community)
    db.session.commit()
    return http_mock.post(inbox).respond(200, json={}), inst


def test_edit_community_announces_an_update_to_a_following_instance(
        db_session, http_mock):
    """`edit_community` end to end on a LOCAL community: both guards passed,
    the Group envelope built at `:68-116`, the Update at `:120-128`, and the
    Announce at `:130-138` delivered at `:142`.

    The nested `@context` absence is the discriminating assertion. The
    `update` dict built at `:120-128` never sets an `@context` key at all --
    only the `announce` dict wrapping it does, at `:137`. If this test
    asserted `'@context' in announce` that would hold either way, because
    `app/activitypub/signature.py:100-101` reinjects a default `@context` at
    the TOP LEVEL whenever one is missing. But that reinjection never reaches
    a NESTED object, so `'@context' not in announce['object']` genuinely
    discriminates: a regression that added an `@context` key inside `update`
    would be caught by this assertion and by no top-level one.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    edit_community(None, s.user.id, s.community.id)

    announce = _sent_activity(route)
    assert announce['type'] == 'Announce'
    assert announce['object']['type'] == 'Update'
    assert '@context' not in announce['object']
    assert announce['object']['object']['type'] == 'Group'
    assert announce['object']['object']['preferredUsername'] == s.community.name


def test_a_remote_community_receives_the_update_unwrapped(
        db_session, http_mock):
    """`:143-144`'s else arm: no Announce, the Update goes straight to
    `community.ap_inbox_url` signed with the USER's key rather than the
    community's.

    NO `@context` ASSERTION BELONGS ON THE UPDATE ITSELF HERE. It is the
    top-level object on this path, so `signature.py:100-101` reinjects a
    default `@context` whether or not `edit_community` ever set one -- the
    module never sets `@context` on `update` at all (`:120-128`), so its
    presence on the wire would prove only the reinjection ran, not anything
    about this function. The nested Group's shape is what this test pins.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    route = http_mock.post(PEER_INBOX).respond(200, json={})

    edit_community(None, s.user.id, s.community.id)

    update = _sent_activity(route)
    assert update['type'] == 'Update'
    assert update['object']['type'] == 'Group'


def test_a_non_moderator_sends_nothing(db_session, http_mock):
    """`:62`'s true arm. Reached only past `:59`, so the community must be
    neither local_only nor (post-fix) private.

    NO ROUTE IS REGISTERED -- `http_mock` uses `assert_all_called=True`, so a
    route that never fires would fail this test for the wrong reason. The
    follower is built inline WITHOUT a route so the loop has a candidate,
    which is what makes the zero count mean "the guard returned" rather than
    "the query was empty".

    `post_request` writes its ActivityPubLog row at
    app/activitypub/signature.py:105 before any network attempt, so a count of
    0 distinguishes a guard return from a failed delivery.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    outsider = make_user(s.instance, 'outsider', local=True)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    db.session.commit()

    edit_community(None, outsider.id, s.community.id)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_local_only_community_sends_nothing(db_session, http_mock):
    """`:59`'s first disjunct."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    s.community.local_only = True
    db.session.commit()

    edit_community(None, s.user.id, s.community.id)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_private_community_sends_nothing(db_session, http_mock):
    """D309's site in this module, FIRST of the two conjuncts this fix adds.

    Before this commit `:59` read `if community.local_only:` alone -- it
    omitted `private` AND the `instance.online()` check that every other member
    of this family already carried. That makes this a WIDER gap than the eight
    sites closed before it.

    `private` is placed BEFORE the `online()` call: `Community.instance_id` is
    a nullable FK and `or` short-circuits left to right, so a private community
    with no instance row returns at the guard rather than raising
    AttributeError. That is a side effect this test does not assert --
    reordering would reopen the crash without failing it.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    s.community.private = True
    db.session.commit()

    edit_community(None, s.user.id, s.community.id)

    assert db.session.query(ActivityPubLog).count() == 0


def test_an_offline_community_instance_sends_nothing(db_session, http_mock):
    """The SECOND conjunct this fix adds. `Instance.online()`
    (app/models.py:118-119) is exactly `not (self.dormant or
    self.gone_forever)`, so `_make_deliverable(s, online=False)` sets both.

    Separated from the private test because the two fail independently and one
    test could not say which conjunct returned.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s, online=False)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    db.session.commit()

    edit_community(None, s.user.id, s.community.id)

    assert db.session.query(ActivityPubLog).count() == 0
