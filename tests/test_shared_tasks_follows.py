"""The five follow and unfollow tasks -- AP Follow and Undo senders.

`app/shared/tasks/follows.py`, 277 lines. Five `@celery.task` functions:
`join_community:39`, `leave_community:112`, `leave_feed:161`,
`follow_user:214` and `unfollow_user:242`.

NO FAN-OUT ANYWHERE IN THIS MODULE, which is what separates it from every
module this campaign has closed since sub-project 24. Each task sends AT MOST
ONE request, directly, to a single actor's inbox. There is no
`following_instances()`, no recipient guard, no Announce, no `domains_sent_to`.

WHAT IT HAS INSTEAD IS STATE. `CommunityJoinRequest`, `FeedJoinRequest` and
`UserFollowRequest` rows are created, read for their `uuid`, and deleted, and
the module's defects are all in the ordering of those operations against
`session.commit()`. A test that asserts only on the wire misses half of what
each task does: assert the row AND the request.

`join_community` FORKS THREE WAYS ON `src` INSIDE EACH OF TWO GUARDS.
`SRC_WEB` flashes and returns None, `SRC_PLD` returns a dict, `SRC_API` raises.
Under `task_always_eager` the wrapper returns the value directly (fact 146), so
the return value is observable -- and it is the only thing distinguishing two
of the three arms.

`flash()` NEEDS A REQUEST CONTEXT AND THE `app` FIXTURE DOES NOT PUSH ONE.
tests/conftest.py:112 pushes only `application.app_context()`. A test touching
a `SRC_WEB` arm must push its own request context -- and doing so DISABLES
`patch_db_session`, because app/utils.py:3688 returns early inside a request
context. Those tests must assert through fresh queries, never on attributes of
objects the task touched.

THIS MODULE NEVER READS `Instance.inbox`. Every send in it targets an
ACTOR-level url instead -- `community.ap_inbox_url` (`:88`, `:152`),
`feed.ap_inbox_url` (`:204`), `to_follow.ap_inbox_url` (`:233`, `:272`). The
four fan-out modules this campaign closed before it all deliver to
`instance.inbox`, so their harnesses set that field; a helper copied from one
of them into this module's tests would set a field nothing here consumes.
"""

import json
import socket
from types import SimpleNamespace

import pytest
from flask import current_app, get_flashed_messages

from app import db
from app.constants import SRC_API, SRC_PLD, SRC_WEB
from app.models import ActivityPubLog, CommunityJoinRequest, UserFollowRequest
from app.shared.tasks.follows import (
    follow_user, join_community, leave_community, leave_feed, unfollow_user,
)
from tests.factories import (
    make_banned_instance, make_community, make_community_ban,
    make_community_join_request, make_feed, make_instance, make_instance_block,
    make_user, make_user_follow_request,
)

PEER_INBOX = 'https://peer.example/inbox'
OTHER_INBOX = 'https://other.example/inbox'

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


def _seed(local_community=True, with_keys=False):
    """instance, user, community -- committed.

    `user` is the person joining or leaving. `join_community:88` and
    `leave_community:152` sign with the USER's key, so `with_keys=True` is
    required for any test that reaches a send.
    """
    instance = make_instance('test.piefed.local', software='piefed')
    user = make_user(instance, 'joiner', local=True, with_keys=with_keys)
    community = make_community('c1')
    if not local_community:
        community.ap_id = 'c1@peer.example'
        community.ap_profile_id = 'https://peer.example/c/c1'
        community.ap_public_url = 'https://peer.example/c/c1'
        community.ap_inbox_url = PEER_INBOX
        community.ap_domain = 'peer.example'
    db.session.commit()
    return SimpleNamespace(instance=instance, user=user, community=community)


def _make_online(s, online=True):
    """Move the community onto a real peer Instance and set its state.

    Two things this buys, neither of them an inbox: `make_community` hardcodes
    `instance_id=1` (tests/factories.py:141), and `_seed` creates
    `test.piefed.local` first, so id 1 is already a real, non-dormant instance
    before this runs -- `online=True` is not what makes `:74`'s and `:129`'s
    `community.instance.online()` true. What `online=False` DOES do is the only
    way to reach those checks' offline arms, since instance 1's `dormant` and
    `gone_forever` both default `False` and nothing else in `_seed` touches
    them. And moving the community here at all puts it on a DOMAIN --
    `peer.example` -- that later tasks need so `instance_banned('peer.example')`
    can match; `test.piefed.local` never can, being the local instance.

    `Instance.online()` is exactly `not (self.dormant or self.gone_forever)`
    (app/models.py:118-119). Returns the peer.
    """
    peer = make_instance('peer.example', software='lemmy')
    if not online:
        peer.dormant = True
        peer.gone_forever = True
    s.community.instance_id = peer.id
    db.session.commit()
    return peer


def _peer_route(http_mock, inbox=PEER_INBOX):
    """A respx route for a delivery this test EXPECTS to happen.

    Do not register one for a test asserting nothing is sent: `http_mock` uses
    `respx.mock(assert_all_called=True)` (tests/conftest.py:342), so a
    registered route that never fires fails the test for the wrong reason.
    """
    return http_mock.post(inbox).respond(200, json={})


def _sent_activity(route, index=-1):
    """The JSON body of the request `route` captured, decoded from the bytes
    that were actually sent. Defaults to the most recent call.
    """
    return json.loads(route.calls[index].request.content)


def test_joining_a_remote_community_sends_a_follow(db_session, http_mock):
    """`join_community:74-89` end to end: the remote-and-online arm, the
    `CommunityJoinRequest` written at `:76`, and the Follow built at `:80-87`.

    THE ROW AND THE REQUEST ARE BOTH ASSERTED. `:75-77` writes the join
    request and `:88` sends the Follow; a test checking only one of the two
    would pass with the other silently broken, and the `uuid` written at `:76`
    is what `:79` puts in the activity id.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_online(s)
    route = _peer_route(http_mock)

    join_community(None, s.user.id, s.community.id, SRC_API)

    assert db.session.query(CommunityJoinRequest).count() == 1
    follow = _sent_activity(route)
    assert follow['type'] == 'Follow'
    assert follow['actor'] == s.user.public_url()
    assert follow['object'] == s.community.public_url()


def test_following_a_remote_user_sends_a_follow(db_session, http_mock):
    """`follow_user:214-238`. A `UserFollowRequest` at `:220-222`, then one
    Follow to the target's own inbox at `:233`.

    Signed with the FOLLOWER's key, not the target's -- `:233` passes
    `user.private_key`, where `user` is the follower loaded at `:218`.
    """
    s = _seed(with_keys=True)
    peer = make_instance('peer.example', software='lemmy')
    target = make_user(peer, 'target')
    target.ap_inbox_url = PEER_INBOX
    db.session.commit()
    route = _peer_route(http_mock)

    follow_user(target.id, s.user.id, send_async=False)

    assert db.session.query(UserFollowRequest).count() == 1
    follow = _sent_activity(route)
    assert follow['type'] == 'Follow'
    assert follow['object'] == target.public_url()


def test_joining_a_local_community_sends_nothing(db_session, http_mock):
    """`:74`'s first conjunct. A local community joins instantly: no
    `CommunityJoinRequest` row, no Follow.

    The oracle is the `ActivityPubLog` count, not the absence of a route:
    `signature.py:143` swallows respx's unmatched-request assertion into a
    failure row, so a send here would be invisible to a call count.
    """
    s = _seed(with_keys=True)

    result = join_community(None, s.user.id, s.community.id, SRC_API)

    assert db.session.query(CommunityJoinRequest).count() == 0
    assert db.session.query(ActivityPubLog).count() == 0
    assert result is True


def test_joining_an_offline_remote_community_sends_nothing(db_session, http_mock):
    """`:74`'s second conjunct, `community.instance.online()`. A remote
    community on a dormant instance also joins instantly.

    Separated from the local test because the two conjuncts fail
    independently, and coverage.py records one arc pair for the whole `if`.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_online(s, online=False)

    result = join_community(None, s.user.id, s.community.id, SRC_API)

    assert db.session.query(CommunityJoinRequest).count() == 0
    assert db.session.query(ActivityPubLog).count() == 0
    assert result is True


def test_joining_returns_the_preload_status_for_src_pld(db_session, http_mock):
    """`:100-101`'s `SRC_PLD` arm. The dict is the only thing distinguishing
    this arm from `SRC_API`'s `return True` at `:103`, and under
    `task_always_eager` the wrapper hands the value back directly (fact 146).
    """
    s = _seed(with_keys=True)

    result = join_community(None, s.user.id, s.community.id, SRC_PLD)

    assert result == {'status': 'joined'}


def test_joining_flashes_and_returns_none_for_src_web(db_session, http_mock):
    """`:96-98`'s `flash`, guarded by `:95`'s `src == SRC_WEB`.

    TWO THINGS ABOUT THIS TEST ARE LOAD-BEARING AND NEITHER IS OBVIOUS.

    First, `flash()` requires a request context and `tests/conftest.py:112`
    pushes only an app context, so the test pushes its own. Without it the
    task raises `RuntimeError: Working outside of request context` and the
    failure looks like a bug in the task rather than in the test.

    Second, pushing that context DISABLES `patch_db_session`:
    `app/utils.py:3685-3688` is `if has_request_context(): yield; return`. So
    `db.session` is NOT the task's session here, and every assertion below
    goes through a fresh query for that reason. An attribute read on an object
    this test built earlier would see the test's own session, not the task's.

    `get_flashed_messages` is read inside the same request context, before
    the context is popped -- flashed messages live in the request/session
    machinery, not in anything either fixture's teardown would preserve.
    """
    s = _seed(with_keys=True)

    with current_app.test_request_context('/'):
        result = join_community(None, s.user.id, s.community.id, SRC_WEB)
        messages = get_flashed_messages()

    assert result is None
    assert db.session.query(ActivityPubLog).count() == 0
    assert len(messages) == 1
    assert s.community.display_name() in messages[0]


def test_a_banned_user_cannot_join_and_raises_for_src_api(db_session, http_mock):
    """`:57`'s raise, guarded by `:56`'s `src == SRC_API`, inside `:48`'s
    banned check.

    `pytest.raises` matches on the message because all three `SRC_API` raises
    in this module are bare `Exception`; matching the type alone would not
    distinguish this guard from `:71`'s.
    """
    s = _seed(with_keys=True)
    make_community_ban(s.user, s.community)

    with pytest.raises(Exception, match='banned_from_community'):
        join_community(None, s.user.id, s.community.id, SRC_API)

    assert db.session.query(CommunityJoinRequest).count() == 0


def test_a_banned_user_gets_the_preload_flag_for_src_pld(db_session, http_mock):
    """`:54`'s `pre_load_message['user_banned'] = True`, guarded by `:53`'s
    `elif src == SRC_PLD`."""
    s = _seed(with_keys=True)
    make_community_ban(s.user, s.community)

    result = join_community(None, s.user.id, s.community.id, SRC_PLD)

    assert result == {'user_banned': True}


def test_a_banned_user_joining_async_returns_without_a_message(db_session, http_mock):
    """`:58`'s bare `return`, reached when `send_async` is truthy so `:49`'s
    `if not send_async:` is False and the whole `src` fork (`:50`-`:57`) is
    skipped.

    This is the arm an async caller takes, and it returns None rather than a
    message, which is why the preload and API callers pass `send_async` False.
    """
    s = _seed(with_keys=True)
    make_community_ban(s.user, s.community)

    result = join_community(True, s.user.id, s.community.id, SRC_PLD)

    assert result is None
    assert db.session.query(CommunityJoinRequest).count() == 0


def test_a_community_on_a_user_blocked_instance_cannot_be_joined(db_session, http_mock):
    """`:61`'s `user.has_blocked_instance(...)` disjunct, inside `:60`'s
    `not community.is_local()` conjunct. Per-user `InstanceBlock`, not a
    site-wide ban -- `:62`'s `instance_banned` reads a different table.
    """
    s = _seed(local_community=False, with_keys=True)
    peer = _make_online(s)
    make_instance_block(s.user, peer)
    db.session.commit()

    result = join_community(None, s.user.id, s.community.id, SRC_PLD)

    assert result == {'community_on_banned_or_blocked_instance': True}
    assert db.session.query(CommunityJoinRequest).count() == 0


def test_a_community_on_a_site_banned_instance_cannot_be_joined(db_session, http_mock):
    """`:62`'s `instance_banned(...)` disjunct. Site-wide `BannedInstances`
    keyed by domain, which is a different table and a different scope from
    `:61`'s per-user block."""
    s = _seed(local_community=False, with_keys=True)
    _make_online(s)
    make_banned_instance('peer.example')
    db.session.commit()

    result = join_community(None, s.user.id, s.community.id, SRC_PLD)

    assert result == {'community_on_banned_or_blocked_instance': True}


def test_a_blocked_instance_raises_for_src_api(db_session, http_mock):
    """`:71`'s raise, guarded by `:70`. Distinguished from `:57`'s by message."""
    s = _seed(local_community=False, with_keys=True)
    peer = _make_online(s)
    make_instance_block(s.user, peer)
    db.session.commit()

    with pytest.raises(Exception, match='community_on_banned_or_blocked_instance'):
        join_community(None, s.user.id, s.community.id, SRC_API)


def test_a_banned_user_gets_a_flash_for_src_web(db_session, http_mock):
    """`:52`'s `return`, guarded by `:50`'s `src == SRC_WEB`. The flash at
    `:51` needs a request context, and pushing one disables
    `patch_db_session` -- see `test_joining_flashes_and_returns_none_for_src_web`
    for why that matters.
    """
    s = _seed(with_keys=True)
    make_community_ban(s.user, s.community)

    with current_app.test_request_context('/'):
        result = join_community(None, s.user.id, s.community.id, SRC_WEB)

    assert result is None
    assert db.session.query(CommunityJoinRequest).count() == 0


def test_a_blocked_instance_gets_a_flash_for_src_web(db_session, http_mock):
    """`:66`'s `return`, guarded by `:64`."""
    s = _seed(local_community=False, with_keys=True)
    peer = _make_online(s)
    make_instance_block(s.user, peer)
    db.session.commit()

    with current_app.test_request_context('/'):
        result = join_community(None, s.user.id, s.community.id, SRC_WEB)

    assert result is None
