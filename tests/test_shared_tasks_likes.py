"""`send_vote`, `vote_for_poll` and their wrappers -- the AP Like/Dislike path.

`app/shared/tasks/likes.py`, 236 lines. Two `@celery.task` wrappers
(`vote_for_post:24`, `vote_for_reply:40`) delegating to `send_vote:55`, plus a
third independent task `vote_for_poll:176`.

THIS MODULE HAS THREE DELIVERY MECHANISMS FOR ONE LOCAL COMMUNITY, which no
sibling module has. `:135`'s loop chooses per instance: `:141` writes an
`ActivityBatch` row for `piefed`/`pylova` peers and commits inside the loop;
`:145` appends a signed request and publishes them to redis after the loop when
`current_app.config['NOTIF_SERVER']` is truthy; `:152` sends directly
otherwise. `NOTIF_SERVER` defaults to `''` (config.py:131), so the second arm
needs a config override to reach.

THREE TASK SESSIONS ARE LIVE IN ONE `send_vote` CALL. The wrapper opens one at
`:26`/`:42` and `patch_db_session` installs it as `db.session`; `send_vote`
opens its OWN at `:56` and never patches it; and `instance_banned`
(app/utils.py:2336) opens another per call. The objects mix: `object` arrives
from the outer session while `user` at `:58` comes from the inner one, and
`user.has_blocked_instance()` (app/models.py:1467-1471) reads through
`db.session` -- the outer one. Nothing observed misbehaves because of this and
this file asserts nothing about it; it is registered as a measured mechanism.

`likes.py` COPIES BEFORE DELETING (`vote_public.copy()` at `:96` and `:110`/
`:112`, then `del`), where `locks.py` deletes IN PLACE. Assert on SERIALIZED
BYTES in both, for the reason `locks.py`'s file gives.
"""

import json
import socket
from types import SimpleNamespace

import pytest
from flask import current_app

from app import db
from app.models import ActivityBatch, ActivityPubLog
from app.shared.tasks.likes import vote_for_poll, vote_for_post, vote_for_reply
from tests.factories import (
    make_banned_instance, make_community, make_community_ban,
    make_community_member, make_instance, make_instance_block, make_poll,
    make_poll_choice, make_post, make_post_reply, make_user,
)

PEER_INBOX = 'https://peer.example/inbox'
OTHER_INBOX = 'https://other.example/inbox'

_REAL_GETADDRINFO = socket.getaddrinfo
_EXAMPLE_TLD_ADDRESS = '93.184.216.34'


def _seed(local_community=True, with_keys=False):
    """instance, user, author, community, post, reply -- committed, in that
    binding order.

    `user` is the VOTER; `author` wrote the post and the reply. The returned
    object has no field named `voter`.

    ORDER IS LOAD-BEARING. `make_community` (tests/factories.py:123) hardcodes
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
    """Put the community on a real peer Instance so the online gate passes.

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


def _follower(s, http_mock, inbox=PEER_INBOX, domain='follower.example',
              software='lemmy'):
    """A remote instance that `following_instances()` will actually return.

    Requires an Instance row, an inbox, AND a user on that instance who is a
    member of the community -- `following_instances()` (app/models.py:842-851)
    joins CommunityMember, so without the membership the loop never runs and
    every delivery assertion passes against zero deliveries.

    `software` matters here in a way it did not for locks.py: `:141` routes
    `piefed` and `pylova` instances to an ActivityBatch write instead of an
    HTTP send, so a caller wanting an HTTP assertion must leave the default.

    Returns `(route, instance)`. The route is registered unconditionally; a
    caller expecting a BATCH rather than a send should build the instance
    directly instead, because `http_mock`'s `assert_all_called=True` fails a
    route that never fires.
    """
    inst = make_instance(domain, software=software)
    inst.inbox = inbox
    member_user = make_user(inst, f'member_{domain.split(".")[0]}')
    make_community_member(member_user, s.community)
    db.session.commit()
    return http_mock.post(inbox).respond(200, json={}), inst


def _sent_activity(route, index=-1):
    """The JSON body of the request `route` captured, decoded from the bytes
    that were actually sent. Defaults to the most recent call.

    THE BYTES, NOT A DICT. `likes.py` copies before deleting
    (`vote_public.copy()` at `:96` and `:110`/`:112`), but assert on the wire
    bytes anyway, for the same reason `locks.py`'s file gives: respx captures
    what was handed to the transport at app/activitypub/signature.py:498.
    """
    return json.loads(route.calls[index].request.content)


def _delivered_inboxes(*routes):
    """The SET of inboxes that received a request. Never a list, never ordered
    -- `following_instances()` ends in an unordered `.distinct().all()`."""
    return {str(r.calls[i].request.url) for r in routes for i in range(len(r.calls))}


def test_vote_for_post_announces_a_like_to_a_following_instance(
        db_session, http_mock):
    """`vote_for_post:24` end to end: `.get()` at `:29`, `federate` defaulting
    True at `:30`, and the local-community Announce at `:122-130` delivered
    directly at `:152`.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    announce = _sent_activity(route)
    assert announce['type'] == 'Announce'
    assert announce['object']['type'] == 'Like'
    assert announce['object']['object'] == s.post.public_url()


def test_vote_for_reply_announces_a_dislike_naming_the_reply(
        db_session, http_mock):
    """`vote_for_reply:40`: `.one()` at `:45`, and `:73`'s downvote arm
    selecting `Dislike` rather than `Like`.

    Two assertions carry this test: the nested `type` (which pins `:73`'s
    else arm) and the nested `object` (which pins that `:45`'s lookup, not
    `:29`'s, reached the handler).
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    vote_for_reply(None, s.user.id, s.reply.id, None, 'downvote')

    announce = _sent_activity(route)
    assert announce['object']['type'] == 'Dislike'
    assert announce['object']['object'] == s.reply.public_url()


def test_federate_false_loads_the_post_and_sends_nothing(db_session, http_mock):
    """`:30`'s false arm -- a wrapper-level federation switch no other module
    in this package has.

    NO ROUTE IS REGISTERED, and the ActivityPubLog count is what proves the
    silence. `post_request` writes its row unconditionally at
    app/activitypub/signature.py:105, before the transport, so a count of 0
    means `send_vote` was never entered rather than that delivery failed.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    db.session.commit()

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote', federate=False)

    assert db.session.query(ActivityPubLog).count() == 0


def test_federate_false_on_the_reply_wrapper_also_sends_nothing(
        db_session, http_mock):
    """`:46`'s false arm. Written out separately from `:30`'s rather than
    parametrised -- they are different lines in different functions, and a
    parametrised pass would cover one under a name that does not say which.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    db.session.commit()

    vote_for_reply(None, s.user.id, s.reply.id, None, 'upvote', federate=False)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_local_only_community_sends_no_vote(db_session, http_mock):
    """`:60`'s first disjunct."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    s.community.local_only = True
    db.session.commit()

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    assert db.session.query(ActivityPubLog).count() == 0


def test_an_offline_community_instance_sends_no_vote(db_session, http_mock):
    """`:60`'s last disjunct. The control is Task 8's smoke test, which runs
    the same path with an online instance and DOES deliver."""
    s = _seed(with_keys=True)
    _make_deliverable(s, online=False)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    db.session.commit()

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_user_banned_from_the_community_sends_no_vote(db_session, http_mock):
    """`:63-65`. `session.query(CommunityBan).filter_by(...).first()` runs on
    send_vote's OWN task session (`:56`), so the ban row must be committed --
    which `make_community_ban` does.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    make_community_ban(s.user, s.community)
    db.session.commit()

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_blocked_remote_community_sends_no_vote(db_session, http_mock):
    """`:66-68`'s first disjunct: a REMOTE community whose instance the voter
    has blocked.

    `:66`'s `if not community.is_local():` guards this pair, so the local
    smoke tests never reach it -- which is why this test uses
    `_seed(local_community=False)`.
    """
    s = _seed(local_community=False, with_keys=True)
    peer = _make_deliverable(s)
    make_instance_block(s.user, peer)
    db.session.commit()

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_banned_remote_community_sends_no_vote(db_session, http_mock):
    """`:66-68`'s second disjunct, `instance_banned(community.instance.domain)`.

    Separated from the block test above because the two conjuncts fail
    independently and a single test could not tell which one returned.
    """
    s = _seed(local_community=False, with_keys=True)
    peer = _make_deliverable(s)
    make_banned_instance(peer.domain)
    db.session.commit()

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    assert db.session.query(ActivityPubLog).count() == 0
