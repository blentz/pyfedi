"""The five follow and unfollow tasks -- AP Follow and Undo senders.

`app/shared/tasks/follows.py`, 280 lines. Five `@celery.task` functions:
`join_community:39`, `leave_community:112`, `leave_feed:162`,
`follow_user:216` and `unfollow_user:244`.

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
Every test in this file calls the task object directly --
`join_community(None, ...)`, never `.delay()` -- which goes through Celery's
`Task.__call__` straight into `run()` and hands back whatever the function
returns, independent of `task_always_eager`. Fact 146 is about
`task_selector`'s two DISPATCH arms (`app/shared/tasks/__init__.py:66` calls
`.delay()`, `:68` calls the task directly) and does not apply here, since
this module is never called through `task_selector`. So the return value is
observable -- and it is the only thing distinguishing two of the three arms.

`flash()` NEEDS A REQUEST CONTEXT AND THE `app` FIXTURE DOES NOT PUSH ONE.
tests/conftest.py:112 pushes only `application.app_context()`. A test touching
a `SRC_WEB` arm must push its own request context -- and doing so DISABLES
`patch_db_session`, because app/utils.py:3688 returns early inside a request
context. Those tests must assert through fresh queries, never on attributes of
objects the task touched.

THIS MODULE NEVER READS `Instance.inbox`. Every send in it targets an
ACTOR-level url instead -- `community.ap_inbox_url` (`:88`, `:153`),
`feed.ap_inbox_url` (`:205`), `to_follow.ap_inbox_url` (`:235`, `:275`). The
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
from app.constants import SRC_API, SRC_PLD, SRC_PLG, SRC_WEB
from app.models import ActivityPubLog, CommunityJoinRequest, FeedJoinRequest, UserFollowRequest
from app.shared.tasks.follows import (
    follow_user, join_community, leave_community, leave_feed, unfollow_user,
)
from tests.factories import (
    make_banned_instance, make_community, make_community_ban,
    make_community_join_request, make_feed, make_feed_join_request,
    make_instance, make_instance_block, make_local_feed, make_user,
    make_user_follow_request,
)

PEER_INBOX = 'https://peer.example/inbox'

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
    `leave_community:153` sign with the USER's key, so `with_keys=True` is
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
    before this runs -- `online=True` is not what makes `:74`'s and `:130`'s
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
    """`follow_user:216-240`. A `UserFollowRequest` at `:222-224`, then one
    Follow to the target's own inbox at `:235`.

    Signed with the FOLLOWER's key, not the target's -- `:235` passes
    `user.private_key`, where `user` is the follower loaded at `:220`.
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


def test_following_a_local_user_sends_nothing(db_session, http_mock):
    """`:221`'s `not to_follow.is_local()` conjunct False -- the `221->240`
    arc, never taken by any other test in this module. `is_local()` short
    circuits the whole condition before `to_follow.instance.online()` is
    ever evaluated, so no `UserFollowRequest` is written and nothing is sent.
    """
    s = _seed(with_keys=True)
    target = make_user(s.instance, 'localtarget', local=True)
    db.session.commit()

    follow_user(target.id, s.user.id, send_async=False)

    assert db.session.query(UserFollowRequest).count() == 0
    assert db.session.query(ActivityPubLog).count() == 0


def test_follow_user_reraises_rather_than_swallowing(db_session, http_mock):
    """`:236-238`'s `except Exception:` / `rollback()` / `raise`, never
    reached by any other test in this module.

    A `to_follow_id` that does not exist makes `:219`'s `.get()` return
    `None`, so `:221`'s `to_follow.is_local()` raises `AttributeError` --
    the cheapest way to reach the handler, same idiom as
    `test_leave_feed_reraises_rather_than_swallowing`.
    """
    s = _seed(with_keys=True)

    with pytest.raises(Exception):
        follow_user(999999, s.user.id, send_async=False)


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
    this arm from `SRC_API`'s `return True` at `:103`. The value is
    observable here because this test calls the task object directly rather
    than `.delay()`, going straight through `Task.__call__` into `run()`;
    fact 146 concerns `task_selector`'s dispatch arms, not this call path,
    and does not apply.
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

    `get_flashed_messages` is read inside the same request context, before
    the context is popped, and the message's own text is asserted rather than
    merely its presence -- `result is None` alone does not distinguish this
    flash existing from `:51` being deleted outright, and `len(messages) == 1`
    alone would not distinguish this flash from `:65`'s.
    """
    s = _seed(with_keys=True)
    make_community_ban(s.user, s.community)

    with current_app.test_request_context('/'):
        result = join_community(None, s.user.id, s.community.id, SRC_WEB)
        messages = get_flashed_messages()

    assert result is None
    assert db.session.query(CommunityJoinRequest).count() == 0
    assert len(messages) == 1
    assert messages[0] == 'You cannot join this community'


def test_a_blocked_instance_gets_a_flash_for_src_web(db_session, http_mock):
    """`:66`'s `return`, guarded by `:64`.

    `result is None` alone is the identical observable already asserted by
    `test_a_blocked_instance_join_async_returns_without_a_message` and
    `test_a_blocked_instance_with_an_unrecognized_src_falls_through_to_bare_return`,
    so it cannot by itself distinguish this arm from either of those. As in
    `test_a_banned_user_gets_a_flash_for_src_web`, `get_flashed_messages` is
    read inside the still-open request context and the message's own text is
    asserted.
    """
    s = _seed(local_community=False, with_keys=True)
    peer = _make_online(s)
    make_instance_block(s.user, peer)
    db.session.commit()

    with current_app.test_request_context('/'):
        result = join_community(None, s.user.id, s.community.id, SRC_WEB)
        messages = get_flashed_messages()

    assert result is None
    assert len(messages) == 1
    assert messages[0] == 'Community is on banned or blocked instance'


def test_a_banned_user_with_an_unrecognized_src_falls_through_to_bare_return(
        db_session, http_mock):
    """`:56`'s `elif src == SRC_API:` False, with none of `:50`/`:53`/`:56`
    matching -- the `56->58` arc coverage.py otherwise never takes.

    `SRC_PLG` (plugin-sourced calls) is a real `src` value this guard's
    three-way fork does not special-case; passing it falls out of the
    `if`/`elif` chain at `:56` and reaches `:58`'s bare `return` directly,
    the same statement `send_async` truthy already reaches by a different
    arc (`49->58`, covered by
    `test_a_banned_user_joining_async_returns_without_a_message`).
    """
    s = _seed(with_keys=True)
    make_community_ban(s.user, s.community)

    result = join_community(None, s.user.id, s.community.id, SRC_PLG)

    assert result is None
    assert db.session.query(CommunityJoinRequest).count() == 0


def test_a_blocked_instance_join_async_returns_without_a_message(db_session, http_mock):
    """`:63`'s `if not send_async:` False -- the `63->72` arc, symmetrical to
    `:49`'s `49->58` arc already covered for the banned-user guard, but never
    exercised for this second guard until now.

    `send_async` truthy skips the whole `:64-71` src fork and lands directly
    on `:72`'s bare `return`.
    """
    s = _seed(local_community=False, with_keys=True)
    peer = _make_online(s)
    make_instance_block(s.user, peer)
    db.session.commit()

    result = join_community(True, s.user.id, s.community.id, SRC_PLD)

    assert result is None
    assert db.session.query(CommunityJoinRequest).count() == 0


def test_a_blocked_instance_with_an_unrecognized_src_falls_through_to_bare_return(
        db_session, http_mock):
    """`:70`'s `elif src == SRC_API:` False, with none of `:64`/`:67`/`:70`
    matching -- the `70->72` arc, symmetrical to
    `test_a_banned_user_with_an_unrecognized_src_falls_through_to_bare_return`'s
    `56->58` arc but for the blocked-instance guard.
    """
    s = _seed(local_community=False, with_keys=True)
    peer = _make_online(s)
    make_instance_block(s.user, peer)
    db.session.commit()

    result = join_community(None, s.user.id, s.community.id, SRC_PLG)

    assert result is None
    assert db.session.query(CommunityJoinRequest).count() == 0


def test_leaving_a_remote_community_sends_an_undo(db_session, http_mock):
    """`:135-153`. Leaving a remote community deletes the join request and
    sends an Undo wrapping the original Follow.

    THE FOLLOW ID INSIDE THE UNDO IS THE POINT. `:135`'s `follow_id` is built
    from the uuid captured at `:126`, before `:127`'s `session.delete` -- so
    the remote end can match the Undo to the Follow it originally received.
    An Undo carrying a fresh or missing id is not an Undo of anything.

    THIS TEST DOES NOT OBSERVE A CRASH -- it passes both before and after the
    `:126` capture was added. Before that line existed, `:135`'s equivalent
    read `join_request.uuid` directly off the already-deleted-and-committed
    instance; a container probe (see task-5-report.md) showed that read
    succeeding rather than raising `ObjectDeletedError`, because
    `Session.commit()` EXPUNGES a just-deleted instance instead of expiring
    it -- `inspect(join_request)` showed `persistent=False, deleted=False,
    detached=True, expired=False` right after the commit, and a detached
    object's attributes are whatever was already loaded, no reload, no error.
    So the pre-fix code already produced the right uuid, by accident of that
    undocumented detach-not-expire behaviour. This test's value is pinning
    the id's PROVENANCE -- asserting it against the uuid captured before
    `leave_community` ever touches the row -- not catching a crash that does
    not occur.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_online(s)
    request = make_community_join_request(s.user, s.community)
    expected_uuid = str(request.uuid)
    db.session.commit()
    route = _peer_route(http_mock)

    leave_community(None, s.user.id, s.community.id)

    assert db.session.query(CommunityJoinRequest).count() == 0
    undo = _sent_activity(route)
    assert undo['type'] == 'Undo'
    assert undo['object']['type'] == 'Follow'
    assert undo['object']['id'].endswith(expected_uuid)


def test_unfollowing_sends_an_undo_carrying_the_original_follow_id(db_session, http_mock):
    """`:252-256`. Unfollowing a remote user deletes the `UserFollowRequest`
    and sends an Undo wrapping the original Follow.

    SAME DEFECT, SAME FIX, AS `leave_community:126`. `:256`'s
    `to_follow_ap_id` is built from the uuid captured at `:253`, before
    `:254`'s `session.delete` -- so the remote end can match the Undo to the
    Follow it originally received. `leave_feed:178` already captures first
    too; `unfollow_user` was the only one of the three left with the wrong
    order, and the `if join_request:` guard at `:252` was never the problem
    here -- only the ordering under it was.

    THIS TEST DOES NOT OBSERVE A CRASH -- it passes both before and after
    the `:253` capture was added. Before that line existed, `:256`'s
    equivalent read `join_request.uuid` directly off the
    already-deleted-and-committed instance; per the container probe run for
    `leave_community` (task-5-report.md), that read succeeds rather than
    raising `ObjectDeletedError`, because `Session.commit()` EXPUNGES a
    just-deleted instance instead of expiring it -- a detached object's
    attributes are whatever was already loaded, no reload, no error. So the
    pre-fix code already produced the right uuid here too, by accident of
    that undocumented detach-not-expire behaviour. This test's value is
    pinning the id's PROVENANCE -- asserting it against the uuid captured
    before `unfollow_user` ever touches the row -- not catching a crash that
    does not occur. This is defensive hardening against relying on that
    undocumented behaviour, not a crash fix.
    """
    s = _seed(with_keys=True)
    peer = make_instance('peer.example', software='lemmy')
    target = make_user(peer, 'target')
    target.ap_inbox_url = PEER_INBOX
    db.session.commit()
    request = make_user_follow_request(s.user, target)
    expected_uuid = str(request.uuid)
    db.session.commit()
    route = _peer_route(http_mock)

    unfollow_user(target.id, s.user.id, send_async=False)

    assert db.session.query(UserFollowRequest).count() == 0
    undo = _sent_activity(route)
    assert undo['type'] == 'Undo'
    assert undo['object']['type'] == 'Follow'
    assert undo['object']['id'].endswith(expected_uuid)


def test_leaving_a_local_community_sends_nothing(db_session, http_mock):
    """`:123`'s return, guarded by `:122`'s `community.is_local()`. Reached
    after the cache invalidation at `:119-120`, which runs for every caller.
    """
    s = _seed(with_keys=True)
    make_community_join_request(s.user, s.community)
    db.session.commit()

    leave_community(None, s.user.id, s.community.id)

    assert db.session.query(ActivityPubLog).count() == 0
    assert db.session.query(CommunityJoinRequest).count() == 1


def test_leaving_an_offline_community_deletes_the_request_but_sends_nothing(
        db_session, http_mock):
    """`:133`'s return, guarded by `:130`'s `not community.instance.online()`.

    THE ROW IS STILL DELETED. `:127`'s delete and `:128`'s commit run before
    the guard, so leaving an offline instance removes the local record and
    simply does not tell the remote end. Asserting only "nothing was sent"
    would miss that.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_online(s, online=False)
    make_community_join_request(s.user, s.community)
    db.session.commit()

    leave_community(None, s.user.id, s.community.id)

    assert db.session.query(CommunityJoinRequest).count() == 0
    assert db.session.query(ActivityPubLog).count() == 0


def test_leaving_a_community_on_a_blocked_instance_sends_nothing(
        db_session, http_mock):
    """`:131`'s `user.has_blocked_instance(...)` disjunct."""
    s = _seed(local_community=False, with_keys=True)
    peer = _make_online(s)
    make_instance_block(s.user, peer)
    make_community_join_request(s.user, s.community)
    db.session.commit()

    leave_community(None, s.user.id, s.community.id)

    assert db.session.query(ActivityPubLog).count() == 0


def test_leaving_a_community_on_a_banned_instance_sends_nothing(
        db_session, http_mock):
    """`:132`'s `instance_banned(...)` disjunct -- the site-wide table, not
    `:131`'s per-user one."""
    s = _seed(local_community=False, with_keys=True)
    _make_online(s)
    make_banned_instance('peer.example')
    make_community_join_request(s.user, s.community)
    db.session.commit()

    leave_community(None, s.user.id, s.community.id)

    assert db.session.query(ActivityPubLog).count() == 0


def test_leave_community_reraises_rather_than_swallowing(db_session, http_mock):
    """`:154-156`'s `except Exception:` / `rollback()` / `raise`, never
    reached by any other test in this module.

    A community id that does not exist makes `:117`'s `.one()` raise
    `NoResultFound` before the function does anything else observable --
    the cheapest way to reach the handler, same idiom as
    `test_leave_feed_reraises_rather_than_swallowing`.
    """
    s = _seed(with_keys=True)

    with pytest.raises(Exception):
        leave_community(None, s.user.id, 999999)


def test_unfollowing_with_no_request_row_sends_a_gibberish_follow_id(
        db_session, http_mock):
    """`:258`'s else arm, taken when `:252`'s `if join_request:` is False.

    The Undo still goes out, carrying a fabricated Follow id. Whether a remote
    end can match it is not this test's claim -- the claim is that the arm
    exists and sends.
    """
    s = _seed(with_keys=True)
    peer = make_instance('peer.example', software='lemmy')
    target = make_user(peer, 'target')
    target.ap_inbox_url = PEER_INBOX
    db.session.commit()
    route = _peer_route(http_mock)

    unfollow_user(target.id, s.user.id, send_async=False)

    undo = _sent_activity(route)
    assert undo['type'] == 'Undo'
    assert undo['object']['type'] == 'Follow'


def test_unfollowing_a_local_user_sends_nothing(db_session, http_mock):
    """`:249`'s `not to_follow.is_local()` conjunct False -- the `249->280`
    arc, never taken by any other test in this module. Symmetrical to
    `test_following_a_local_user_sends_nothing`: `is_local()` short circuits
    the condition before `to_follow.instance.online()` is evaluated, so no
    row is queried, deleted, or sent.
    """
    s = _seed(with_keys=True)
    target = make_user(s.instance, 'localtarget', local=True)
    db.session.commit()

    unfollow_user(target.id, s.user.id, send_async=False)

    assert db.session.query(UserFollowRequest).count() == 0
    assert db.session.query(ActivityPubLog).count() == 0


def test_unfollow_user_reraises_rather_than_swallowing(db_session, http_mock):
    """`:276-278`'s `except Exception:` / `rollback()` / `raise`, never
    reached by any other test in this module.

    A `to_follow_id` that does not exist makes `:247`'s `.get()` return
    `None`, so `:249`'s `to_follow.is_local()` raises `AttributeError` --
    the cheapest way to reach the handler, same idiom as
    `test_leave_feed_reraises_rather_than_swallowing`.
    """
    s = _seed(with_keys=True)

    with pytest.raises(Exception):
        unfollow_user(999999, s.user.id, send_async=False)


def test_leaving_a_remote_feed_sends_an_undo(db_session, http_mock):
    """`:187-206`. `leave_feed` captures the uuid at `:178` BEFORE deleting at
    `:179`, which is the ordering `leave_community` and `unfollow_user` lacked
    until this sub-project fixed them. The correct idiom was always in this
    file, one function below the first defect.
    """
    s = _seed(with_keys=True)
    peer = make_instance('peer.example', software='lemmy')
    feed = make_feed(peer, 'peerfeed')
    feed.ap_inbox_url = PEER_INBOX
    db.session.commit()
    request = make_feed_join_request(s.user, feed)
    expected_uuid = str(request.uuid)
    db.session.commit()
    route = _peer_route(http_mock)

    leave_feed(None, s.user.id, feed.id)

    assert db.session.query(FeedJoinRequest).count() == 0
    undo = _sent_activity(route)
    assert undo['type'] == 'Undo'
    assert undo['object']['id'].endswith(expected_uuid)


def test_leaving_a_local_feed_sends_nothing(db_session, http_mock):
    """`:174`'s return, guarded by `:173`'s `feed.is_local()`, after the three
    cache invalidations at `:169-171`."""
    s = _seed(with_keys=True)
    feed = make_local_feed('localfeed')
    db.session.commit()

    leave_feed(None, s.user.id, feed.id)

    assert db.session.query(ActivityPubLog).count() == 0


def test_leaving_an_offline_feed_with_no_pending_request_sends_nothing(
        db_session, http_mock):
    """`:177`'s `if join_request:` False -- the `177->179` arc, taken when no
    `FeedJoinRequest` row exists for this user/feed pair, so `:178`'s uuid
    capture never runs.

    THE INSTANCE MUST ALSO BE OFFLINE, and that is not incidental. If the
    guard at `:182-184` passed instead, execution would reach `:189`'s
    `f"...{uuid}"` with `uuid` never assigned -- an `UnboundLocalError`. Going
    offline routes through `:185`'s `return` first, which is also this
    test's real target: the `182->185` arc, never taken by any other test in
    this module because every other `leave_feed` test either has a pending
    request (`:178` already assigns `uuid`) or is local (`:173` returns
    before reaching this guard at all).

    PROOF THAT `:188`'S FALSE ARM IS UNREACHABLE (marked `# pragma: no
    branch` in `follows.py`, not tested here). `Instance.online()` is
    exactly `not (self.dormant or self.gone_forever)` (app/models.py:
    118-119). Passing this test's own guard at `:182` requires `not
    feed.instance.online()` to be False, i.e. `online()` True, which by that
    definition requires `gone_forever` to already be False -- there is no
    session write, commit, or refresh of `feed.instance` between `:182` and
    `:188` that could change it in between. So by the time `:188`'s `if not
    feed.instance.gone_forever:` runs, `gone_forever` is guaranteed False and
    the condition is always True; the `188->212` arc coverage.py reports
    missing is not merely untested, it is provably dead given `online()`'s
    own definition -- the same shape of proof this campaign has used before
    for a conjunct made redundant by an earlier filter.
    """
    peer = make_instance('peer.example', software='lemmy')
    peer.dormant = True
    feed = make_feed(peer, 'peerfeed')
    feed.ap_inbox_url = PEER_INBOX
    db.session.commit()
    s = _seed(with_keys=True)

    leave_feed(None, s.user.id, feed.id)

    assert db.session.query(FeedJoinRequest).count() == 0
    assert db.session.query(ActivityPubLog).count() == 0


def test_leave_feed_reraises_rather_than_swallowing(db_session, http_mock):
    """`:210`'s `raise`, added by this sub-project.

    Before this commit `:208-209` was `except Exception:` / `rollback()` with
    no re-raise, alone among the five tasks in this module. A failure inside
    `leave_feed` was swallowed: the caller saw success, Celery recorded
    nothing, and the user's feed membership silently failed to leave.

    A feed id that does not exist makes `:167`'s `.one()` raise
    `NoResultFound`, which is the cheapest way to reach the handler --
    `:166`'s `User.get(user_id)` does not raise on a missing row, and the
    user in this test exists anyway.
    """
    s = _seed(with_keys=True)

    with pytest.raises(Exception):
        leave_feed(None, s.user.id, 999999)
