"""`ban_person` and its four wrappers -- the AP Block and Undo senders.

`app/shared/tasks/blocks.py`, 190 lines. Four `@celery.task` wrappers
(`ban_from_site:41`, `unban_from_site:55`, `ban_from_community:69`,
`unban_from_community:83`) delegating to `ban_person:96`.

THE HANDLER FORKS STRUCTURALLY ON `community_id` AT `:101`, producing two
different envelopes from one function: a community ban addresses the community
and may Announce to its followers, while a site ban addresses the server root
and fans out to every non-mastodon instance. `:130-133` forks again on the same
condition to set `audience`. Combined with `is_undo` that is four shapes before
any delivery path is chosen.

THREE DELIVERY PATHS, AND ONLY THE THIRD USES `following_instances()`:
  `:158-163` site ban -- a RAW `Instance` query filtered only on
      `software != 'mastodon'`, guarded inline by
      `instance.inbox and instance.online() and instance.id != 1`, then returns.
      A recipient here needs NO community membership.
  `:166-168` remote community -- one direct send to `ap_inbox_url`, then returns.
  `:172-190` local communities -- an Announce per community, delivered to each
      `following_instances()` row passing `instance.inbox and
      instance.online()`, plus a FALLBACK send at `:190` to the banned user's
      own instance when it was not already covered.

THE FALLBACK EXISTS FOR A REASON THE COMMENT AT `:189` STATES:
`following_instances()` excludes instances whose only follower was the person
just banned, so their home instance would otherwise never learn of the ban.
Any change to that line has to preserve that case.
"""

import json
import socket
from types import SimpleNamespace

import pytest

from app import db
from app.models import ActivityPubLog
from app.shared.tasks.blocks import (
    ban_from_community, ban_from_site, unban_from_community, unban_from_site,
)
from tests.factories import (
    make_community, make_community_member, make_instance, make_user,
)

PEER_INBOX = 'https://peer.example/inbox'
OTHER_INBOX = 'https://other.example/inbox'
LOCAL_INBOX = 'https://test.piefed.local/inbox'

_REAL_GETADDRINFO = socket.getaddrinfo
_EXAMPLE_TLD_ADDRESS = '93.184.216.34'


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

    Transferred from this campaign's other AP-sender harnesses. `online=False`
    sets both `dormant` and `gone_forever` (`Instance.online()` is exactly
    `not (self.dormant or self.gone_forever)`, app/models.py:118-119). Returns
    the peer.
    """
    peer = make_instance('follower-home.example', software='lemmy')
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
    """instance, user, mod, community -- committed.

    `user` is the person being banned; `mod` is the person doing it.
    `ban_person` loads both (`:99-100`) and signs with the MOD's key on the
    site-ban and remote-community paths, and with the COMMUNITY's key on the
    local-Announce path -- so `with_keys=True` supplies all three.
    """
    instance = make_instance('test.piefed.local', software='piefed')
    mod = make_user(instance, 'themod', local=True, with_keys=with_keys)
    user = make_user(instance, 'banned', local=True)
    community = make_community('c1')
    if with_keys:
        community.private_key = mod.private_key
        community.public_key = mod.public_key
    if not local_community:
        community.ap_id = 'c1@peer.example'
        community.ap_profile_id = 'https://peer.example/c/c1'
        community.ap_public_url = 'https://peer.example/c/c1'
        community.ap_inbox_url = PEER_INBOX
        community.ap_domain = 'peer.example'
    db.session.commit()
    return SimpleNamespace(instance=instance, user=user, mod=mod,
                           community=community)


def _follower(s, http_mock, inbox=PEER_INBOX, domain='follower.example'):
    """A remote instance `following_instances()` will actually return.

    THREE parts, and the third is the one that is easy to miss: an Instance
    row, an inbox, and a USER ON THAT INSTANCE WHO IS A MEMBER OF THE
    COMMUNITY. Without the membership the query returns nothing, `:185`'s loop
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


def _site_instance(http_mock, domain='peer.example', software='lemmy',
                   inbox=PEER_INBOX):
    """An Instance the SITE-BAN path will deliver to.

    Distinct from `_follower` and deliberately so: `:159` queries Instance
    directly, filtered only on `software != 'mastodon'`, so a site-ban
    recipient needs NO community membership -- only a row with an inbox, a
    non-mastodon software string, and an id that is not 1.

    Returns `(route, instance)`.
    """
    inst = make_instance(domain, software=software)
    inst.inbox = inbox
    db.session.commit()
    return http_mock.post(inbox).respond(200, json={}), inst


def test_ban_from_site_fans_out_to_every_non_mastodon_instance(
        db_session, http_mock):
    """`ban_from_site:41` end to end: the instance-ban fork at `:108-112`, the
    Block envelope at `:116-129`, `:133`'s audience, and the site fan-out at
    `:158-163`.

    The Block goes out UNWRAPPED on this path -- there is no Announce -- so no
    `@context` assertion belongs on it: `signature.py:100-101` reinjects at the
    top level, which is exactly where this object sits.
    """
    s = _seed(with_keys=True)
    route, _inst = _site_instance(http_mock)

    ban_from_site(None, s.user.id, s.mod.id, None, 'spam', False)

    block = _sent_activity(route)
    assert block['type'] == 'Block'
    assert block['object'] == s.user.public_url()
    assert block['actor'] == s.mod.public_url()
    assert block['target'].endswith('/')


def test_ban_from_community_announces_to_the_communitys_followers(
        db_session, http_mock):
    """`ban_from_community:69` on a LOCAL community: the community fork at
    `:102-107`, `:131`'s audience, and the Announce loop at `:172-188`.

    The nested `@context` absence discriminates -- `:154` deletes it from the
    Block before `:179` nests it, and the reinjection never reaches inside.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    ban_from_community(None, s.user.id, s.mod.id, s.community.id, None, 'spam')

    announce = _sent_activity(route)
    assert announce['type'] == 'Announce'
    assert announce['object']['type'] == 'Block'
    assert '@context' not in announce['object']


def test_a_local_only_community_ban_sends_nothing(db_session, http_mock):
    """`:104`'s first disjunct, reached only via the community fork at `:101`."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    s.community.local_only = True
    db.session.commit()

    ban_from_community(None, s.user.id, s.mod.id, s.community.id, None, 'spam')

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_private_community_ban_sends_nothing(db_session, http_mock):
    """D309's site in this module, FIRST of two conjuncts this fix adds.

    Before this commit `:104` read `if community.local_only:` alone, omitting
    `private` AND the `instance.online()` check every other site in this family
    already carried -- a WIDER gap than the eight closed before it.

    `private` sits before the `online()` call: `Community.instance_id` is a
    nullable FK and `or` short-circuits left to right, so a private community
    with no instance row returns at the guard rather than raising. This test
    does not assert that; reordering would reopen it without failing here.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    s.community.private = True
    db.session.commit()

    ban_from_community(None, s.user.id, s.mod.id, s.community.id, None, 'spam')

    assert db.session.query(ActivityPubLog).count() == 0


def test_an_offline_community_instance_ban_sends_nothing(db_session, http_mock):
    """The SECOND conjunct. Separated because the two fail independently."""
    s = _seed(with_keys=True)
    _make_deliverable(s, online=False)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    db.session.commit()

    ban_from_community(None, s.user.id, s.mod.id, s.community.id, None, 'spam')

    assert db.session.query(ActivityPubLog).count() == 0


def test_unban_from_site_wraps_the_block_in_an_undo(db_session, http_mock):
    """`unban_from_site:55` -> `is_undo=True` on the instance fork.

    `:136` strips the Block's `@context` before `:142` nests it; `:151` strips
    the Undo's own. On this path the Undo is the top-level object, so the
    reinjection at `signature.py:100-101` supplies one anyway -- which is why
    the NESTED absence is the assertion that discriminates and the top-level
    presence is not asserted at all.
    """
    s = _seed(with_keys=True)
    route, _inst = _site_instance(http_mock)

    unban_from_site(None, s.user.id, s.mod.id, None, 'appealed')

    undo = _sent_activity(route)
    assert undo['type'] == 'Undo'
    assert undo['object']['type'] == 'Block'
    assert '@context' not in undo['object']


def test_unban_from_community_announces_a_nested_undo(db_session, http_mock):
    """`unban_from_community:83` on a local community: three levels, with
    `@context` at the top only.

    `:136` strips the Block, `:151` strips the Undo, and `:180` gives the
    Announce its own. Both inner absences fail independently.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    unban_from_community(None, s.user.id, s.mod.id, s.community.id, None, 'ok')

    announce = _sent_activity(route)
    assert announce['object']['type'] == 'Undo'
    assert '@context' not in announce['object']
    assert announce['object']['object']['type'] == 'Block'
    assert '@context' not in announce['object']['object']


def test_a_remote_community_ban_is_sent_direct(db_session, http_mock):
    """`:166-168`. `community.is_local()` is False, so no Announce is built and
    the Block goes straight to the community's own inbox signed with the MOD's
    key rather than the community's.

    `:103` sets `communities = []` for a non-local community, which is why the
    loop at `:172` would have nothing to iterate even if `:168` did not return
    first.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    route = http_mock.post(PEER_INBOX).respond(200, json={})

    ban_from_community(None, s.user.id, s.mod.id, s.community.id, None, 'spam')

    block = _sent_activity(route)
    assert block['type'] == 'Block'
    assert block['audience'] == s.community.public_url()


def test_a_mastodon_instance_receives_no_site_ban(db_session, http_mock):
    """`:159`'s `Instance.software != 'mastodon'` filter.

    NO ROUTE IS REGISTERED for the mastodon instance -- under
    `assert_all_called=True` a registered route that never fires would fail
    this test for the wrong reason, while an unexpected send would surface as
    an unmatched request.
    """
    s = _seed(with_keys=True)
    masto = make_instance('masto.example', software='mastodon')
    masto.inbox = OTHER_INBOX
    db.session.commit()
    route, _inst = _site_instance(http_mock)

    ban_from_site(None, s.user.id, s.mod.id, None, 'spam', False)

    assert _delivered_inboxes(route) == {PEER_INBOX}


def test_the_local_instance_receives_no_site_ban(db_session, http_mock):
    """`:161`'s `instance.id != 1` conjunct, isolated from the other two.

    `_seed`'s local instance is built by `make_instance`, which never sets
    `inbox` (tests/factories.py:33-37) -- so left alone it would already be
    excluded by `:161`'s FIRST conjunct, `instance.inbox`, discriminating
    nothing about `id != 1`. This test assigns `LOCAL_INBOX` to that instance
    deliberately, so the first two conjuncts (`instance.inbox`,
    `instance.online()`) both pass and `id != 1` is the only thing left to
    exclude it. `LOCAL_INBOX` is registered with no route: if `id != 1` ever
    stopped excluding this row, the send would hit an unmatched request under
    `assert_all_called=True` rather than silently succeeding.

    The sequence reset in tests/conftest.py (see the comment at :189-190)
    makes `_seed`'s first instance id 1, which this test confirms explicitly
    rather than assuming.
    """
    s = _seed(with_keys=True)
    s.instance.inbox = LOCAL_INBOX
    db.session.commit()
    assert s.instance.id == 1
    route, _inst = _site_instance(http_mock)

    ban_from_site(None, s.user.id, s.mod.id, None, 'spam', False)

    assert _delivered_inboxes(route) == {PEER_INBOX}
    assert len(route.calls) == 1
