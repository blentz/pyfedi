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
