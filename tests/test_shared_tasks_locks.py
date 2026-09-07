"""`lock_object` and its four wrappers -- the AP Lock and Undo senders.

`app/shared/tasks/locks.py`, 145 lines: four `@celery.task` wrappers
(`lock_post:26`, `unlock_post:41`, `lock_post_reply:56`,
`unlock_post_reply:71`) delegating to `lock_object:85`. The sixth module in
which this campaign has met the wrappers-plus-handler shape, so the harness
below is transferred from `tests/test_shared_tasks_flags.py` rather than
rebuilt.

THE `is_undo` DIMENSION IS WHAT MAKES THIS MODULE DIFFERENT. Every path exists
twice, and `@context` is deleted at THREE different sites depending on which
path runs: `:107` strips it from the Lock before nesting it in an Undo, `:122`
strips it from the Undo, and `:125` strips it from the Lock on the non-undo
local path. Assert on the SERIALIZED BYTES, never on an in-memory dict --
`locks.py` deletes IN PLACE (`del lock['@context']`), so a recorder holding
`lock` reads it back after the delete.

THE FOUR WRAPPER LOOKUPS SPLIT TWO AND TWO. `:31` and `:46` are
`session.query(Post).get(post_id)` and return None for a missing id, so the
raise arrives later at `:87` as `AttributeError`; `:61` and `:76` are
`session.query(PostReply).filter_by(id=...).one()` and raise `NoResultFound` at
the lookup. Establish each by READING -- this is the fifth module in which the
campaign has met this trap.

DELIVERY REQUIRES COMMUNITY MEMBERSHIP, NOT MERELY AN INSTANCE ROW.
`Community.following_instances()` (app/models.py:842-851) joins
`CommunityMember`, so an Instance with no member of this community is never
returned and the loop body never runs. `_follower` below creates the member;
a helper that only made an Instance would leave every delivery assertion
passing against zero deliveries.

ORDER IS NEVER ASSERTED. `following_instances()` ends in an unordered
`.distinct().all()`, so which follower comes first is a property of the query
plan. Every assertion over delivered inboxes here is set-based.
"""

import json
import socket
from types import SimpleNamespace

import pytest
from sqlalchemy.orm.exc import NoResultFound

from app import db
from app.models import ActivityPubLog
from app.shared.tasks.locks import (
    lock_post, lock_post_reply, unlock_post, unlock_post_reply,
)
from tests.factories import (
    make_community, make_community_member, make_instance, make_post,
    make_post_reply, make_user,
)

PEER_INBOX = 'https://peer.example/inbox'
OTHER_INBOX = 'https://other.example/inbox'

_REAL_GETADDRINFO = socket.getaddrinfo

# Any globally routable literal will do -- the only property
# app/utils.py:5530 reads off it is `is_global`.
_EXAMPLE_TLD_ADDRESS = '93.184.216.34'


def _seed(local_community=True, with_keys=False):
    """instance, user, author, community, post, reply -- committed, in that
    binding order.

    `user` is the MODERATOR performing the lock; `author` wrote the post and
    the reply. The returned object has no field named `moderator`.

    ORDER IS LOAD-BEARING. `make_community` (tests/factories.py:122) hardcodes
    `instance_id=1` and the db_session teardown resets every sequence, so the
    local instance is created FIRST; a peer built before this call would take
    id 1 and leave the community's FK pointing at it.

    `local_community=False` sets `ap_id` AND `ap_profile_id` on peer.example,
    because `Community.is_local()` (app/models.py:795) is a DISJUNCTION whose
    `profile_id()` falls back to a computed default; `ap_id` alone leaves the
    community silently LOCAL (fact 112).
    """
    instance = make_instance('test.piefed.local', software='piefed')
    user = make_user(instance, 'themod', local=True, with_keys=with_keys)
    author = make_user(instance, 'author', local=True)
    community = make_community('c1')
    if with_keys:
        community.private_key = user.private_key
        community.public_key = user.public_key
    post = make_post(community, author, ap_id='https://test.piefed.local/post/1')
    reply = make_post_reply(post, author, body='a reply')
    if not local_community:
        community.ap_id = 'c1@peer.example'
        community.ap_profile_id = 'https://peer.example/c/c1'
        community.ap_public_url = 'https://peer.example/c/c1'
        community.ap_inbox_url = PEER_INBOX
        community.ap_domain = 'peer.example'
    db.session.commit()
    return SimpleNamespace(instance=instance, user=user, author=author,
                           community=community, post=post, reply=reply)


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
    """Put the community on a real peer Instance so `:89`'s gate passes.

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


def _follower(s, http_mock, inbox=PEER_INBOX, domain='follower.example'):
    """A remote instance that `following_instances()` will actually return.

    THREE things are required and the third is the one that is easy to miss:
    an Instance row, an inbox, and a USER ON THAT INSTANCE WHO IS A MEMBER OF
    THE COMMUNITY. `Community.following_instances()` (app/models.py:842-851)
    joins User on `User.instance_id` and CommunityMember on
    `CommunityMember.user_id`, filtering `CommunityMember.community_id` and
    `is_banned == False`. Without the membership the query returns nothing,
    the loop at `:140` never runs, and every delivery assertion in this file
    passes against zero deliveries.

    It also filters `Instance.id != 1`, so the follower must not be the local
    instance `_seed` created first.

    Returns `(route, instance)`.
    """
    inst = make_instance(domain, software='lemmy')
    inst.inbox = inbox
    member_user = make_user(inst, f'member_{domain.split(".")[0]}')
    make_community_member(member_user, s.community)
    db.session.commit()
    return http_mock.post(inbox).respond(200, json={}), inst


def _sent_activity(route, index=-1):
    """The JSON body of the request `route` captured, decoded from the bytes
    that were actually sent. Defaults to the most recent call.

    THE BYTES, NOT A DICT. `locks.py` deletes `@context` in place at `:107`,
    `:122` and `:125`, so a recorder holding the payload would be read back
    after the delete. respx captures what was handed to the transport at
    app/activitypub/signature.py:498.
    """
    return json.loads(route.calls[index].request.content)


def _delivered_inboxes(*routes):
    """The SET of inboxes that received a request. Never a list, never ordered
    -- `following_instances()` ends in an unordered `.distinct().all()`."""
    return {str(r.calls[i].request.url) for r in routes for i in range(len(r.calls))}


def test_lock_post_announces_to_a_following_instance(db_session, http_mock):
    """`lock_post:26` end to end on a LOCAL community: `.get()` at `:31`, the
    gate at `:89` passing, the Lock envelope at `:95-104`, the Announce wrapper
    at `:131-139`, and delivery at `:142`.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    lock_post(None, s.user.id, s.post.id)

    announce = _sent_activity(route)
    assert announce['type'] == 'Announce'
    assert announce['object']['type'] == 'Lock'
    assert announce['object']['object'] == s.post.public_url()
    assert announce['actor'] == s.community.public_url()


def test_lock_post_reply_sends_a_lock_to_a_remote_community(db_session, http_mock):
    """`lock_post_reply:56` on a REMOTE community: `.one()` at `:61`, and
    `:143-145`'s else arm sending the bare Lock to the community's own inbox
    rather than wrapping it in an Announce.

    The assertion that earns this test's place is `object`: it is the
    REPLY's url, which is what distinguishes `:61`'s lookup from `:31`'s
    reaching the same handler.

    NO `@context` ASSERTION BELONGS ON THIS PATH. On the remote, non-undo
    route `lock_object` never reaches any of its three deletion sites
    (`:107`, `:122`, `:125`) -- those all sit behind `is_undo` or
    `community.is_local()`, neither of which is true here -- so `lock` keeps
    the `@context` `:100` set. But even a regression that stripped it
    unconditionally would not be caught by asserting its presence: the Lock
    IS the top-level posted object on this path, and
    `send_post_request`/`post_request` reinjects a default `@context` at
    `app/activitypub/signature.py:100-101`
    (`if '@context' not in body: body['@context'] = default_context()`)
    before the bytes leave, using the SAME `default_context()` value either
    way. `_sent_activity` reads the post-reinjection bytes, so the key (and
    its value) is indistinguishable between "never deleted" and
    "deleted-then-reinjected" -- nothing on the wire discriminates them, and
    no assertion here can.

    Contrast the NESTED case: a later task's Announce test asserts the
    ABSENCE of `@context` inside the Lock nested at `announce['object']`.
    That assertion does discriminate, because the top-level reinjection at
    signature.py:100-101 never reaches into a nested object -- it only
    inspects the top-level `body` dict handed to it, so a nested `@context`
    that `lock_object` failed to delete would survive onto the wire and the
    absence assertion would catch it. The reinjection's blindness to nesting
    is exactly what makes that later assertion meaningful where this one
    would not be.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    route = http_mock.post(PEER_INBOX).respond(200, json={})

    lock_post_reply(None, s.user.id, s.reply.id)

    lock = _sent_activity(route)
    assert lock['type'] == 'Lock'
    assert lock['object'] == s.reply.public_url()
    assert lock['object'] != s.post.public_url()


def test_a_local_only_community_sends_no_lock(db_session, http_mock):
    """`:89`'s first disjunct.

    NO ROUTE IS REGISTERED. `http_mock` is built with `assert_all_called=True`
    (tests/conftest.py:342), so a route registered here and never called would
    fail this test for the wrong reason. The follower is built WITHOUT a route
    for the same reason.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    s.community.local_only = True
    db.session.commit()

    lock_post(None, s.user.id, s.post.id)

    assert db.session.query(ActivityPubLog).count() == 0


def test_an_offline_community_instance_sends_no_lock(db_session, http_mock):
    """`:89`'s third disjunct after Task 3, its second today.

    THE CONTROL IS TASK 1's SMOKE TEST, which runs the same path with an online
    instance and DOES deliver. Without that pairing, zero rows passes against
    any breakage that stops delivery for any reason.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s, online=False)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    db.session.commit()

    lock_post(None, s.user.id, s.post.id)

    assert db.session.query(ActivityPubLog).count() == 0


def _recording_task_session(monkeypatch):
    """Make `get_task_session` hand back a GENUINE Session that records
    `rollback()` and `close()`.

    THE SIXTH COPY of a helper that also lives in
    tests/test_shared_tasks_send_reply.py, test_shared_tasks_send_answer.py,
    test_shared_tasks_send_post.py, test_shared_tasks_add_remove.py and
    test_shared_tasks_flags.py. Duplicated rather than imported: this campaign
    keeps its test modules independent so a helper can be edited for one
    function's needs without silently changing another's assertions. The count
    is registered as D324, which this file's existence moves from five to six.

    The session is real -- only the observation is added, by wrapping the two
    methods rather than replacing the object. A fake session would prove the
    wrapper calls methods on a mock; this proves it calls them on the session
    the function actually used.

    THE PATCH TARGET IS THE LOCKS MODULE, NOT `app.utils`.
    `app/shared/tasks/locks.py:4` imports `get_task_session` into the locks
    namespace and all four wrappers resolve it there (`:28`, `:43`, `:58`,
    `:73`). Patching `app.utils.get_task_session` would apply cleanly, observe
    nothing, and leave the assertion trivially true against an empty list.
    """
    from app import db as _db
    from sqlalchemy.orm import Session as _Session
    import app.shared.tasks.locks as locks_module

    record = SimpleNamespace(calls=[])

    def _make():
        session = _Session(bind=_db.engine)
        real_rollback, real_close = session.rollback, session.close

        def rollback():
            record.calls.append('rollback')
            return real_rollback()

        def close():
            record.calls.append('close')
            return real_close()

        session.rollback = rollback
        session.close = close
        return session

    monkeypatch.setattr(locks_module, 'get_task_session', _make)
    return record


def test_a_missing_post_raises_AttributeError_and_rolls_back(
        db_session, monkeypatch):
    """`:31`'s `.get(post_id)` against an absent id, plus `:33-35`'s except arm
    and `:36-37`'s finally.

    `.get()` returns None rather than raising, so the failure arrives at `:87`
    (`object.community` on None) as `AttributeError` -- fifty-six lines later.
    Asserting `['rollback', 'close']` rather than merely `raises` is what makes
    this a test of the WRAPPER: a `raises`-only test cannot tell a rollback
    from its absence, and the ORDER distinguishes `finally` running after
    `except` from a wrapper that closed instead of rolling back.
    """
    s = _seed()
    record = _recording_task_session(monkeypatch)

    with pytest.raises(AttributeError):
        lock_post(None, s.user.id, s.post.id + 1000)

    assert record.calls == ['rollback', 'close']


def test_a_missing_post_on_the_unlock_path_also_raises_AttributeError(
        db_session, monkeypatch):
    """`:46`'s `.get(post_id)` and `:48-52`'s tail. The undo twin of the test
    above, written out separately because `unlock_post`'s except and finally
    are different lines from `lock_post`'s."""
    s = _seed()
    record = _recording_task_session(monkeypatch)

    with pytest.raises(AttributeError):
        unlock_post(None, s.user.id, s.post.id + 1000)

    assert record.calls == ['rollback', 'close']


def test_a_missing_reply_raises_NoResultFound_and_rolls_back(
        db_session, monkeypatch):
    """`:61`'s `.filter_by(id=...).one()` against an absent id, plus `:63-67`.

    THE DIFFERENT EXCEPTION TYPE IS THE POINT. `.one()` raises at the LOOKUP,
    so `lock_object` is never entered at all -- unlike the post wrappers, which
    reach `:87`. Asserting the type is what records the asymmetry.
    """
    s = _seed()
    record = _recording_task_session(monkeypatch)

    with pytest.raises(NoResultFound):
        lock_post_reply(None, s.user.id, s.reply.id + 1000)

    assert record.calls == ['rollback', 'close']


def test_a_missing_reply_on_the_unlock_path_also_raises_NoResultFound(
        db_session, monkeypatch):
    """`:76`'s `.one()` and `:78-82`'s tail."""
    s = _seed()
    record = _recording_task_session(monkeypatch)

    with pytest.raises(NoResultFound):
        unlock_post_reply(None, s.user.id, s.reply.id + 1000)

    assert record.calls == ['rollback', 'close']


def test_lock_post_closes_the_session_on_the_happy_path(
        db_session, http_mock, monkeypatch):
    """`:36-37`'s finally on the SUCCESS path -- `close` with no `rollback`.

    The control for the four error tests: without it, `finally` running is only
    ever observed alongside an exception, and a wrapper that closed only in the
    `except` arm would pass everything else in this file.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    _follower(s, http_mock)
    record = _recording_task_session(monkeypatch)

    lock_post(None, s.user.id, s.post.id)

    assert record.calls == ['close']


def test_a_private_community_sends_no_lock(db_session, http_mock):
    """D309's site in this module. Before this commit `:89` gated on
    `local_only` and `instance.online()` but not `Community.private`, so a lock
    on content in a private community federated out. This test pins down that
    `community.private` is now part of the guard.

    THE ORDER OF `:89`'s DISJUNCTS IS LOAD-BEARING. `private` sits BEFORE
    `not community.instance.online()`, and `or` short-circuits left to right,
    so a private community with no instance row (`Community.instance_id`,
    app/models.py:575, is a nullable FK) returns at the `private` check instead
    of raising `AttributeError` on `None.online()`. That is a side effect of
    this fix, not something this test asserts -- reordering the disjuncts would
    reopen the crash without failing this test, since this community always has
    an instance.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    s.community.private = True
    db.session.commit()

    lock_post(None, s.user.id, s.post.id)

    assert db.session.query(ActivityPubLog).count() == 0


def test_the_announce_carries_a_top_level_context_and_a_bare_lock(
        db_session, http_mock):
    """`:125`'s `del lock['@context']` on the non-undo local path, and
    `:131-139`'s Announce.

    THIS ASSERTION DISCRIMINATES HERE AND WOULD NOT HAVE ONE SUB-PROJECT AGO.
    `app/activitypub/signature.py:100-101` reinjects `@context` TOP-LEVEL ONLY.
    In `flags.py` the Flag WAS the top-level object, so its `@context` was
    present whether or not the builder set it and no assertion could tell.
    Here the top level is the Announce and the Lock is NESTED, which the
    reinjection never reaches -- so `'@context' not in announce['object']`
    fails if `:125` stops deleting. The top-level `'@context' in announce`
    assertion below does NOT discriminate -- the reinjection supplies it
    either way -- it is kept only to pin the envelope's shape.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    lock_post(None, s.user.id, s.post.id)

    announce = _sent_activity(route)
    assert '@context' in announce
    assert '@context' not in announce['object']
    assert announce['object']['type'] == 'Lock'


def test_the_undo_announce_nests_a_context_free_undo_around_a_context_free_lock(
        db_session, http_mock):
    """The is_undo local path, where `@context` is deleted TWICE: `:107` strips
    it from the Lock before `:113` nests it in the Undo, and `:122` strips it
    from the Undo before `:135` nests THAT in the Announce.

    So the delivered object has `@context` at exactly one level out of three.
    Both inner assertions fail independently -- `:107` and `:122` are separate
    statements and a regression could drop either. The top-level
    `'@context' in announce` assertion does not discriminate (the reinjection
    would supply it regardless); it is kept only to pin the envelope's shape.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    unlock_post(None, s.user.id, s.post.id)

    announce = _sent_activity(route)
    assert '@context' in announce
    assert announce['object']['type'] == 'Undo'
    assert '@context' not in announce['object']
    assert '@context' not in announce['object']['object']
    assert announce['object']['object']['type'] == 'Lock'


def test_a_remote_lock_keeps_its_context(db_session, http_mock):
    """`:143-145`'s else arm. No Announce, so the Lock is the top-level object
    and KEEPS the `@context` set at `:100` -- `:125` never ran, because it sits
    behind `community.is_local()`, which is false on this path.

    Note what this test cannot prove: `signature.py:100-101` would reinject
    `@context` here anyway, so its presence is not evidence that `:100` set it.
    What the assertion does establish is the SHAPE -- a bare Lock rather than
    an Announce -- and `type` is what carries that.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    route = http_mock.post(PEER_INBOX).respond(200, json={})

    lock_post(None, s.user.id, s.post.id)

    lock = _sent_activity(route)
    assert lock['type'] == 'Lock'
    assert '@context' in lock


def test_a_remote_undo_wraps_a_context_free_lock(db_session, http_mock):
    """`:143-145` with is_undo: the Undo is top-level, and `:107` stripped the
    Lock's `@context` before `:113` nested it. `:122` never runs on this path
    -- it sits behind `community.is_local()`, which is false here -- but its
    absence is unobservable since `:107` already removed the key it would
    have deleted from a different object (the Undo, not the Lock).

    THIS IS THE REMOTE PATH'S DISCRIMINATING ASSERTION. The Undo's own
    `@context` proves nothing (reinjection would supply it), but the nested
    Lock's ABSENCE is beyond the reinjection's reach and fails if `:107`
    stops deleting.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    route = http_mock.post(PEER_INBOX).respond(200, json={})

    unlock_post(None, s.user.id, s.post.id)

    undo = _sent_activity(route)
    assert undo['type'] == 'Undo'
    assert undo['object']['type'] == 'Lock'
    assert '@context' not in undo['object']


def test_the_announce_addresses_the_communitys_followers(db_session, http_mock):
    """`:130`'s `cc = [community.ap_followers_url]`, which REPLACES the
    `cc = [community.public_url()]` set at `:94` for the non-announce paths.

    The rebinding at `:130` is easy to miss because `cc` is built at `:94`,
    used in the Lock at `:103`, and then overwritten -- so the Lock nested
    inside the Announce carries the OLD cc while the Announce carries the new
    one. Both are asserted here. `:130` REBINDS the name `cc` to a new list
    rather than mutating the old one in place, so the list already stored on
    `lock['cc']` at `:103` is unaffected by the rebinding.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)
    s.community.ap_followers_url = 'https://test.piefed.local/c/c1/followers'
    db.session.commit()

    lock_post(None, s.user.id, s.post.id)

    announce = _sent_activity(route)
    assert announce['cc'] == ['https://test.piefed.local/c/c1/followers']
    assert announce['object']['cc'] == [s.community.public_url()]
