"""`send_vote`, `vote_for_poll` and their wrappers -- the AP Like/Dislike path.

`app/shared/tasks/likes.py`, 239 lines. Two `@celery.task` wrappers
(`vote_for_post:24`, `vote_for_reply:40`) delegating to `send_vote:55`, plus a
third independent task `vote_for_poll:176`.

THIS MODULE HAS THREE DELIVERY MECHANISMS FOR ONE LOCAL COMMUNITY, which no
sibling module has. `:135`'s loop chooses per instance: `:141` routes a
`piefed`/`pylova` peer into the batch arm, where `:142-143` write an
`ActivityBatch` row and commit it; `:145` selects the async arm when
`current_app.config['NOTIF_SERVER']` is truthy, where `:146-149` sign the
announce and `:157-159` publish it to redis after the loop ends; `:152` sends
directly otherwise. `NOTIF_SERVER` defaults to `''` (config.py:131).

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


def test_a_private_community_sends_no_vote(db_session, http_mock):
    """D309's site in this module. Before this commit `:60` gated on
    `local_only` and `instance.online()` but not `Community.private`, so a
    vote in a private community federated out. This test pins down that
    `community.private` is now part of the guard.

    THE ORDER OF `:60`'s DISJUNCTS IS LOAD-BEARING, for the reason
    test_shared_tasks_locks.py's equivalent test gives for its `:89`: `private`
    sits BEFORE `not community.instance.online()`, and `or` short-circuits
    left to right, so a private community with no instance row
    (`Community.instance_id`, app/models.py:575, is a nullable FK) returns at
    the `private` check instead of raising `AttributeError` on
    `None.online()`. That is a side effect of this fix, not something this
    test asserts -- reordering the disjuncts would reopen the crash without
    failing this test, since this community always has an instance.

    The follower is built inline rather than through `_follower()` -- no
    route is registered, so `http_mock`'s `assert_all_called=True` can't fail
    on one that never fires. `following_instances()` still needs an Instance
    row, an inbox, and a member on it (app/models.py:842-851) for a count of
    0 to prove the guard fired rather than that the query was empty.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    s.community.private = True
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
    """`:67`'s first disjunct: a REMOTE community whose instance the voter
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
    """`:67`'s second disjunct, `instance_banned(community.instance.domain)`.

    Separated from the block test above because the two disjuncts fail
    independently and a single test could not tell which one returned.
    """
    s = _seed(local_community=False, with_keys=True)
    peer = _make_deliverable(s)
    make_banned_instance(peer.domain)
    db.session.commit()

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    assert db.session.query(ActivityPubLog).count() == 0


def test_an_emoji_vote_carries_its_content(db_session, http_mock):
    """`:88-89`'s `if emoji:` arm, which adds a `content` key the other paths
    never set."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote', emoji='\N{PARTY POPPER}')

    announce = _sent_activity(route)
    assert announce['object']['content'] == '\N{PARTY POPPER}'


def test_a_vote_without_an_emoji_omits_content(db_session, http_mock):
    """`:88`'s false arm. The control for the test above: without it, `content`
    being present is never distinguished from it being unconditional."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    announce = _sent_activity(route)
    assert 'content' not in announce['object']


def test_undoing_a_vote_wraps_a_context_free_copy(db_session, http_mock):
    """`:92-105`'s undo payload and `:122-130`'s local Announce around it.

    `vote_to_undo` IS A STRING, NOT A BOOLEAN: `:71` assigns it directly to
    `type`, so passing `'Like'` produces an Undo whose nested object has
    `type: 'Like'`.

    `:96-97` copies vote_public and deletes `@context` from the COPY, then
    `:110`/`:115` copies again and deletes from that. So the delivered Announce
    has `@context` at the top level only -- and the two nested absences fail
    independently, since `:97` and `:115` are separate statements.

    The top-level `'@context' in announce` assertion does NOT discriminate --
    `signature.py:100-101` reinjects it regardless of what `:127` did -- it is
    kept only to pin the envelope's shape, as in the sibling locks.py tests.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    vote_for_post(None, s.user.id, s.post.id, 'Like', 'upvote')

    announce = _sent_activity(route)
    assert '@context' in announce
    assert announce['object']['type'] == 'Undo'
    assert '@context' not in announce['object']
    assert announce['object']['object']['type'] == 'Like'
    assert '@context' not in announce['object']['object']


def test_a_remote_community_receives_the_bare_vote(db_session, http_mock):
    """`:160-167`'s else arm with `vote_to_undo` None: `:165` selects
    vote_public and `:167` sends it to the community's own inbox, unwrapped.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    route = http_mock.post(PEER_INBOX).respond(200, json={})

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    vote = _sent_activity(route)
    assert vote['type'] == 'Like'
    assert vote['audience'] == s.community.public_url()


def test_a_remote_community_receives_the_bare_undo(db_session, http_mock):
    """`:162-163`'s arm selecting undo_public.

    THE DISCRIMINATING ASSERTION IS THE NESTED ABSENCE. The Undo's own
    `@context` proves nothing -- signature.py:100-101 would reinject it -- but
    the nested vote's absence, deleted at `:97`, is beyond the reinjection's
    reach.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    route = http_mock.post(PEER_INBOX).respond(200, json={})

    vote_for_post(None, s.user.id, s.post.id, 'Like', 'upvote')

    undo = _sent_activity(route)
    assert undo['type'] == 'Undo'
    assert undo['object']['type'] == 'Like'
    assert '@context' not in undo['object']


def test_a_piefed_follower_is_batched_rather_than_sent(db_session, http_mock):
    """`:141` routes a `piefed` peer into the batch arm instead of the send
    arms; `:142-143` write the `ActivityBatch` row and commit it, inside the
    loop, with no HTTP request involved.

    NO ROUTE IS REGISTERED for this instance, and that is the point: under
    `http_mock`'s `assert_all_called=True` a registered-but-unfired route
    would fail this test for the wrong reason, while an unexpected send would
    surface as an unmatched request instead. So the assertion pair is "one
    batch row" and "zero ActivityPubLog rows".

    The batch payload is `payload_copy` -- the context-free inner object
    built at `:110-115`, not the Announce built around it at `:122-130` --
    which is what `assert '@context' not in batches[0].payload` pins down.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    inst = make_instance('piefed.example', software='piefed')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_piefed')
    make_community_member(member, s.community)
    db.session.commit()

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    batches = db.session.query(ActivityBatch).all()
    assert len(batches) == 1
    assert batches[0].instance_id == inst.id
    assert batches[0].community_id == s.community.id
    assert batches[0].payload['type'] == 'Like'
    assert '@context' not in batches[0].payload
    assert db.session.query(ActivityPubLog).count() == 0


def test_a_pylova_follower_is_batched_too(db_session, http_mock):
    """`:141`'s second literal, `'pylova'`. Written out separately from the
    `piefed` test above because `or` short-circuits: a mutation deleting the
    `pylova` comparison would leave that test passing regardless.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    inst = make_instance('pylova.example', software='pylova')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_pylova')
    make_community_member(member, s.community)
    db.session.commit()

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    assert db.session.query(ActivityBatch).count() == 1


def test_a_notif_server_publishes_to_redis_instead_of_sending(
        db_session, http_mock, monkeypatch):
    """`:145` selects the async arm when `current_app.config['NOTIF_SERVER']`
    is truthy; `:146-149` signs the announce and appends it to `send_async`;
    `:157-159` then publishes it to redis after the loop ends, rather than
    sending it over HTTP.

    THE CAPTURE. `:155` does `from app import redis_client` INSIDE the
    function, so the name resolves at call time and patching `app.redis_client`
    before the call intercepts it -- the idiom `tests/README.md` already
    records for this client. The double records `(channel, payload)` so this
    can assert on WHAT was published; asserting merely that publish was
    called would pass against a payload carrying the wrong urls.

    NO ROUTE IS REGISTERED -- the whole point is that nothing goes out over
    HTTP on this path -- so an accidental send surfaces as an unmatched
    request under `http_mock`.
    """
    published = []

    class _Redis:
        def publish(self, channel, payload):
            published.append((channel, payload))

    monkeypatch.setattr('app.redis_client', _Redis())
    monkeypatch.setitem(current_app.config, 'NOTIF_SERVER', 'notifs.example')

    s = _seed(with_keys=True)
    _make_deliverable(s)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    db.session.commit()

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    assert len(published) == 1
    channel, payload = published[0]
    assert channel == 'http_posts:activity'
    body = json.loads(payload)
    assert body['urls'] == [PEER_INBOX]
    assert json.loads(body['data'])['type'] == 'Announce'


def test_no_notif_server_sends_directly(db_session, http_mock, monkeypatch):
    """`:150-152`'s else arm, and the control for the test above: without it,
    a direct send is never distinguished from an unconditional one.

    `NOTIF_SERVER` defaults to `''` (config.py:131), so this is the default
    path -- but set it explicitly rather than relying on the default, so the
    test still means what it says if the default ever changes.
    """
    monkeypatch.setitem(current_app.config, 'NOTIF_SERVER', '')

    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    assert _delivered_inboxes(route) == {PEER_INBOX}


def test_a_follower_without_an_inbox_is_skipped_and_the_loop_continues(
        db_session, http_mock):
    """`:136`'s `instance.inbox` conjunct, inverted into a `continue` at
    `:139`.

    The inboxless follower is created FIRST so it takes the lower id and is
    returned first by `following_instances()`'s unordered `.distinct().all()`
    (app/models.py:842-851). If that query-plan assumption ever breaks, this
    test degrades to LAX -- it would still pass under a loop that aborted on
    the skip instead of continuing past it -- never to FLAKY. Creating the
    good follower first would let such an aborting loop deliver once and pass,
    which is the defect this ordering avoids.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    dud = make_instance('inboxless.example', software='lemmy')
    dud.inbox = None
    dud_member = make_user(dud, 'member_inboxless')
    make_community_member(dud_member, s.community)
    db.session.commit()
    route, _good = _follower(s, http_mock, inbox=OTHER_INBOX,
                             domain='good.example')

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    assert _delivered_inboxes(route) == {OTHER_INBOX}
    assert len(route.calls) == 1


def test_a_blocked_follower_is_skipped_and_the_loop_continues(
        db_session, http_mock):
    """`:137`'s `not user.has_blocked_instance(instance.id)` conjunct,
    inverted into a `continue` at `:139`.

    The blocked follower is created FIRST, for the same lower-id,
    LAX-not-FLAKY reason `test_a_follower_without_an_inbox_is_skipped...`
    gives.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    blocked = make_instance('blocked.example', software='lemmy')
    blocked.inbox = PEER_INBOX
    blocked_member = make_user(blocked, 'member_blocked')
    make_community_member(blocked_member, s.community)
    make_instance_block(s.user, blocked)
    db.session.commit()
    route, _good = _follower(s, http_mock, inbox=OTHER_INBOX,
                             domain='good.example')

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    assert _delivered_inboxes(route) == {OTHER_INBOX}


def test_a_poll_vote_announces_to_a_following_instance(db_session, http_mock):
    """`vote_for_poll:176` on a local community: `:202`'s is_local arm,
    `:206`'s `del payload['@context']`, and delivery at `:230`.

    The nested absence is the discriminating assertion, for the reason this
    file's other Announce tests give.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    make_poll(s.post)
    make_poll_choice(s.post, 'yes')
    route, _inst = _follower(s, http_mock)

    vote_for_poll(None, s.user.id, s.post.id, 'yes')

    announce = _sent_activity(route)
    assert announce['object']['type'] == 'PollVote'
    assert announce['object']['choice_text'] == 'yes'
    assert '@context' not in announce['object']


def test_a_remote_poll_vote_is_sent_bare(db_session, http_mock):
    """`:232-233`'s else arm.

    NO `@context` ASSERTION BELONGS ON THIS PATH. `payload` here is the
    TOP-LEVEL body handed to `post_request`, and `signature.py:100-101`
    reinjects a default `@context` at the top level whenever it is absent --
    so even if `:198`'s `'@context': default_context()` were deleted
    outright, the key would still be on the wire and this test could not
    tell. Contrast the local test above, whose
    `assert '@context' not in announce['object']` DOES discriminate: that
    assertion is on the NESTED object, which the top-level-only reinjection
    never reaches.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    make_poll(s.post)
    make_poll_choice(s.post, 'yes')
    route = http_mock.post(PEER_INBOX).respond(200, json={})

    vote_for_poll(None, s.user.id, s.post.id, 'yes')

    payload = _sent_activity(route)
    assert payload['type'] == 'PollVote'


def test_an_absent_post_votes_nowhere(db_session, http_mock):
    """`:181`'s false arm -- the first of the function's two guards.

    `.get()` at `:179` returns None for an absent id, so `if post:` is what
    stops an absent post from ever reaching the community gate below it.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)

    vote_for_poll(None, s.user.id, s.post.id + 1000, 'yes')

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_poll_vote_in_a_private_community_federates_nowhere(
        db_session, http_mock):
    """The gate `vote_for_poll` did not have.

    Every other sender in this package gates on the community before
    federating. `vote_for_poll` checked only `if post:` and then federated a
    poll vote out of a private or local-only community with no check at all.
    This is not a D309 omission -- those are guards missing a conjunct -- it is
    the guard's absence.

    This test FAILS before the gate is added: a real ActivityPubLog row is
    written and a real request attempted.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    make_poll(s.post)
    make_poll_choice(s.post, 'yes')
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    s.community.private = True
    db.session.commit()

    vote_for_poll(None, s.user.id, s.post.id, 'yes')

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_poll_vote_in_a_local_only_community_federates_nowhere(
        db_session, http_mock):
    """The same gate's first disjunct. Separated from the private test because
    the two fail independently and one test could not say which returned."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    make_poll(s.post)
    make_poll_choice(s.post, 'yes')
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    s.community.local_only = True
    db.session.commit()

    vote_for_poll(None, s.user.id, s.post.id, 'yes')

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_poll_vote_to_an_offline_instance_federates_nowhere(
        db_session, http_mock):
    """The same gate's third disjunct."""
    s = _seed(with_keys=True)
    _make_deliverable(s, online=False)
    make_poll(s.post)
    make_poll_choice(s.post, 'yes')
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    db.session.commit()

    vote_for_poll(None, s.user.id, s.post.id, 'yes')

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_poll_voteless_follower_is_skipped_and_the_loop_continues(
        db_session, http_mock):
    """`vote_for_poll` has its OWN delivery loop at `:224-231`, separate from
    `send_vote`'s at `:135-152` -- a different function with a different
    guard on the same shape. This closes the residual left by this file's
    other skip tests, which all exercise `send_vote`'s loop via
    `vote_for_post`/`vote_for_reply` and never reach `vote_for_poll`'s own.

    The inboxless follower is created FIRST, for the same lower-id,
    LAX-not-FLAKY reason `test_a_follower_without_an_inbox_is_skipped...`
    gives for `send_vote`'s loop: `following_instances()` ends in an
    unordered `.distinct().all()`.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    make_poll(s.post)
    make_poll_choice(s.post, 'yes')
    dud = make_instance('inboxless.example', software='lemmy')
    dud.inbox = None
    dud_member = make_user(dud, 'member_inboxless')
    make_community_member(dud_member, s.community)
    db.session.commit()
    route, _good = _follower(s, http_mock, inbox=OTHER_INBOX,
                             domain='good.example')

    vote_for_poll(None, s.user.id, s.post.id, 'yes')

    assert _delivered_inboxes(route) == {OTHER_INBOX}
    assert len(route.calls) == 1


def _recording_task_session(monkeypatch):
    """Make `get_task_session` hand back a GENUINE Session that records
    `rollback()` and `close()`.

    The SEVENTH copy of this helper across the suite. Duplicated rather than
    imported: this campaign keeps its test modules independent so a helper can
    be edited for one function's needs without silently changing another's
    assertions. The count is registered as D324.

    THE PATCH TARGET IS THE LIKES MODULE, NOT `app.utils`. `likes.py:5` imports
    `get_task_session` into the likes namespace and every function resolves it
    there. Patching `app.utils.get_task_session` would apply cleanly, observe
    nothing, and leave the assertions trivially true against an empty list.

    NOTE WHAT THIS DOES AND DOES NOT REACH IN THIS MODULE. `send_vote` opens
    its OWN session at `:56`, which this patch intercepts, AND runs inside a
    wrapper whose session it also intercepts -- so a `send_vote` failure
    records TWO closes, one per session. Assert on the ORDER and the presence
    of 'rollback', not on an exact list length, unless you have read which
    sessions a given path opens.
    """
    from app import db as _db
    from sqlalchemy.orm import Session as _Session
    import app.shared.tasks.likes as likes_module

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

    monkeypatch.setattr(likes_module, 'get_task_session', _make)
    return record


def test_a_database_failure_in_vote_for_poll_rolls_back_and_re_raises(
        db_session, http_mock, monkeypatch):
    """`vote_for_poll`'s bare `except:` arm at `:235` and its `finally` at
    `:238-239`, reached WITHOUT a faked exception in the task's own logic.

    The session's `execute` is made to raise. VERIFIED BY TRACEBACK where it
    first fires rather than assumed: under this project's sqlalchemy, the
    raise lands at `:179`'s `session.query(Post).get(post_id)` -- the FIRST
    lookup in the function -- because `Query.get()` funnels through
    `Session.execute()` on a fresh Session's identity-map miss.

    The recorded ORDER fixes `finally` running after `except`, which a
    `raises`-only test cannot observe.
    """
    from app import db as _db
    from sqlalchemy.orm import Session as _Session
    import app.shared.tasks.likes as likes_module

    s = _seed(with_keys=True)
    _make_deliverable(s)
    calls = []

    def _make():
        session = _Session(bind=_db.engine)
        real_rollback, real_close = session.rollback, session.close

        def rollback():
            calls.append('rollback')
            return real_rollback()

        def close():
            calls.append('close')
            return real_close()

        def execute(*_args, **_kwargs):
            raise RuntimeError('database refused the query')

        session.rollback = rollback
        session.close = close
        session.execute = execute
        return session

    monkeypatch.setattr(likes_module, 'get_task_session', _make)

    with pytest.raises(RuntimeError):
        vote_for_poll(None, s.user.id, s.post.id, 'yes')

    assert calls == ['rollback', 'close']


def test_vote_for_poll_closes_its_session_on_the_happy_path(
        db_session, http_mock, monkeypatch):
    """The same `finally` (`:238-239`) on the SUCCESS path -- `close` with no
    `rollback`.

    The control for the test above: without it, `finally` running is only ever
    observed alongside an exception, and a handler that closed only in the
    `except` arm would pass everything else in this file.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    make_poll(s.post)
    make_poll_choice(s.post, 'yes')
    _follower(s, http_mock)
    record = _recording_task_session(monkeypatch)

    vote_for_poll(None, s.user.id, s.post.id, 'yes')

    assert record.calls == ['close']


def test_a_failure_inside_send_vote_rolls_back_and_re_raises(
        db_session, http_mock, monkeypatch):
    """`send_vote`'s bare `except:` at `:168`, the end of its body, reached by
    a NATURAL raise: the post is absent, so `:59`'s `object.community` raises
    AttributeError on None.

    TWO SESSIONS ARE RECORDED HERE and that is the observation. The wrapper
    (`vote_for_post`) opens one at `:26` and `send_vote` opens its own at
    `:56`, both intercepted by the same patch, so a failure inside `send_vote`
    rolls back and closes each of them in turn.

    MEASURED BEFORE ASSERTING: a bare `assert record.calls == []` was run
    first; the actual sequence recorded is
    `['rollback', 'close', 'rollback', 'close']`, NOT the
    `['rollback', 'close', 'close']` a single-`rollback` guess would predict.
    `send_vote`'s own `except`/`finally` (`:169`, `:172`) fire first, rolling
    back and closing its OWN session; the exception then propagates out of
    `patch_db_session` and `vote_for_post`'s `except`/`finally` (`:33`, `:36`)
    roll back and close the OUTER session too -- `vote_for_post`'s `except`
    calls `session.rollback()` unconditionally on whatever session it holds,
    regardless of whether that session's own transaction did anything. So
    BOTH sessions are rolled back, not just the inner one.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    record = _recording_task_session(monkeypatch)

    with pytest.raises(AttributeError):
        vote_for_post(None, s.user.id, s.post.id + 1000, None, 'upvote')

    assert record.calls == ['rollback', 'close', 'rollback', 'close']


def test_a_missing_reply_in_vote_for_reply_rolls_back_and_re_raises(
        db_session, http_mock, monkeypatch):
    """`vote_for_reply`'s bare `except:` at `:48` and its `finally` at
    `:51-52` -- `vote_for_reply`'s OWN pair, distinct from both
    `vote_for_post`'s (`:32`, `:35-36`) and `vote_for_poll`'s (`:235`,
    `:238-239`). This closes the residual left by this file's other two
    handler-failure tests, neither of which reaches `vote_for_reply` at all.

    `:45`'s `.filter_by(id=reply_id).one()` raises `NoResultFound` directly
    on a missing id -- unlike `vote_for_post`'s `.get()`, which returns None
    and defers the failure fifty-five lines to `send_vote`. So only ONE
    session is ever opened here (the wrapper's own, at `:42`) and
    `send_vote` is never entered.
    """
    from sqlalchemy.orm.exc import NoResultFound

    s = _seed(with_keys=True)
    _make_deliverable(s)
    record = _recording_task_session(monkeypatch)

    with pytest.raises(NoResultFound):
        vote_for_reply(None, s.user.id, s.reply.id + 1000, None, 'upvote')

    assert record.calls == ['rollback', 'close']


def test_a_banned_follower_is_skipped_and_the_loop_continues(
        db_session, http_mock):
    """`:138`'s `not instance_banned(instance.domain)` conjunct, inverted
    into a `continue` at `:139`. `instance_banned` (app/utils.py:2336) opens
    its own task session, so the `BannedInstances` row must be committed to
    be visible to it.

    The banned follower is created FIRST, for the same lower-id,
    LAX-not-FLAKY reason `test_a_follower_without_an_inbox_is_skipped...`
    gives.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    banned = make_instance('banned.example', software='lemmy')
    banned.inbox = PEER_INBOX
    banned_member = make_user(banned, 'member_banned')
    make_community_member(banned_member, s.community)
    make_banned_instance('banned.example')
    db.session.commit()
    route, _good = _follower(s, http_mock, inbox=OTHER_INBOX,
                             domain='good.example')

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    assert _delivered_inboxes(route) == {OTHER_INBOX}
