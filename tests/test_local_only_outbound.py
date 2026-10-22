"""Nothing by, for or about local-only content leaves the server (plan B task 7).

Local-only is an account or community of a hosted domain whose `federates` is off (here `abc.onion`), and any post or
comment stamped `local_only`. Every test watches the HTTP layer itself: a respx route that answers every outbound
request, so "nothing was sent" means no request reached httpx, not that a function returned early.
"""
import json
import socket
from types import SimpleNamespace

import httpx
import pytest
import respx
from sqlalchemy import event

from app import cli, db
from app.activitypub.routes import announce_activity_to_followers
from app.activitypub.signature import post_request, send_post_request
from app.constants import SRC_API
import app.local_only as local_only
from app.local_only import federated_mentions, outbound_refusal
from app.models import ActivityBatch, SendQueue, SiteDomain
from app.shared.tasks.deletes import delete_object
from app.shared.tasks.follows import join_community
from app.shared.tasks.likes import vote_for_post, vote_for_reply
from app.shared.tasks.notes import send_reply
from app.shared.tasks.pages import send_post
from app.storefront import clear_domain_cache, domain_rows
from tests.factories import make_community, make_community_member, make_instance, make_post, make_post_reply, \
    make_user

pytestmark = pytest.mark.usefixtures('site')

SERVER = 'https://test.piefed.local'
ONION = 'http://abc.onion'
PEER_INBOX = 'https://peer.example/inbox'

_REAL_GETADDRINFO = socket.getaddrinfo


def _getaddrinfo_without_the_network(host, *args, **kwargs):
    """`.example` hosts resolve to a public address without a resolver, as in test_shared_tasks_send_post.py."""
    if isinstance(host, str) and host.endswith('.example'):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 0))]
    return _REAL_GETADDRINFO(host, *args, **kwargs)


@pytest.fixture
def wire(monkeypatch):
    """Every outbound HTTP request, answered 202. `wire.calls` is what left the server."""
    monkeypatch.setattr(socket, 'getaddrinfo', _getaddrinfo_without_the_network)
    with respx.mock(assert_all_called=False) as router:
        yield router.route().mock(return_value=httpx.Response(202))


def _onion_actor(row, kind):
    row.ap_profile_id = f'{ONION}/{kind}/{row.name if kind == "c" else row.user_name}'
    row.ap_public_url = row.ap_profile_id
    if kind == 'c':
        row.ap_followers_url = f'{row.ap_profile_id}/followers'


@pytest.fixture
def env(db_session):
    local = make_instance('test.piefed.local', software='piefed')
    if db.session.query(SiteDomain).filter_by(is_default=True).first() is None:
        db.session.add(SiteDomain(domain='test.piefed.local', is_default=True))
    onion = SiteDomain(domain='abc.onion', scheme='http', federates=False)
    db.session.add(onion)
    db.session.commit()
    clear_domain_cache()
    peer = make_instance('peer.example', software='lemmy')
    peer.inbox = PEER_INBOX
    hal = make_user(local, 'hal', local=True, with_keys=True)
    ozzy = make_user(local, 'ozzy', local=True, with_keys=True)
    ozzy.site_domain_id = onion.id
    _onion_actor(ozzy, 'u')
    remote = make_user(peer, 'rem')
    hell = make_community('hell')
    hell.private_key, hell.public_key = hal.private_key, hal.public_key
    oc = make_community('oc')
    oc.site_domain_id = onion.id
    oc.private_key, oc.public_key = hal.private_key, hal.public_key
    _onion_actor(oc, 'c')
    away = make_community('away', host='peer.example')
    away.ap_id = 'away@peer.example'
    away.instance_id = peer.id
    away.ap_inbox_url = 'https://peer.example/c/away/inbox'
    db.session.commit()
    for community in (hell, oc, away):
        make_community_member(remote, community)
    yield SimpleNamespace(local=local, peer=peer, onion=onion, hal=hal, ozzy=ozzy, remote=remote, hell=hell, oc=oc,
                          away=away)
    clear_domain_cache()


def _post(community, author, local_only=False, body=None):
    post = make_post(community, author, ap_id='pending')
    post.ap_id = f'{SERVER}/post/{post.id}'
    post.local_only = local_only
    post.body = body
    post.body_html = f'<p>{body}</p>' if body else ''
    db.session.commit()
    return post


def _reply(post, author, local_only=False):
    reply = make_post_reply(post, author)
    reply.ap_id = f'{SERVER}/comment/{reply.id}'
    reply.local_only = local_only
    db.session.commit()
    return reply


def _sent(wire):
    return [(str(call.request.url), call.request.content) for call in wire.calls]


# -- Review Focus 5 ---------------------------------------------------------------------------------------------------

def test_an_onion_users_post_vote_follow_and_delete_send_nothing(env, wire):
    post = _post(env.away, env.ozzy, local_only=True)
    send_post(post.id, session=db.session)
    remote_post = make_post(env.away, env.remote, ap_id='https://peer.example/post/7')
    vote_for_post(None, env.ozzy.id, remote_post.id, None, 'upvote')
    join_community(True, env.ozzy.id, env.away.id, SRC_API)
    delete_object(env.ozzy.id, post, is_post=True, session=db.session)
    assert _sent(wire) == []


# -- Review Focus 4 ---------------------------------------------------------------------------------------------------

def test_a_reply_vote_and_boost_on_local_only_content_send_nothing(env, wire):
    """A federating user's reply to an onion post in a federating community is stamped local-only and goes nowhere,
    nor does a vote on it, nor a boost (an Announce naming it; piefed has no user boost sender, so it is sent as
    any activity would be)."""
    post = _post(env.hell, env.ozzy, local_only=True)
    reply = _reply(post, env.hal, local_only=True)
    send_reply(reply.id, None, session=db.session)
    vote_for_post(None, env.hal.id, post.id, None, 'upvote')
    vote_for_reply(None, env.hal.id, reply.id, None, 'downvote')
    boost = {'id': f'{SERVER}/activities/announce/1', 'type': 'Announce', 'actor': env.hal.public_url(),
             'object': post.ap_id, 'to': ['https://www.w3.org/ns/activitystreams#Public']}
    send_post_request(PEER_INBOX, boost, env.hal.private_key, env.hal.public_url() + '#main-key')
    assert _sent(wire) == []


def test_a_federating_communitys_announce_of_a_local_only_reply_is_not_sent(env, wire, monkeypatch, app):
    post = _post(env.hell, env.ozzy, local_only=True)
    reply = _reply(post, env.remote)
    note = {'id': f'https://peer.example/comment/{reply.id}', 'type': 'Note', 'attributedTo': env.remote.public_url(),
            'inReplyTo': post.ap_id, 'content': 'hi'}
    create = {'id': 'https://peer.example/activities/create/1', 'type': 'Create',
              'actor': env.remote.public_url(), 'object': note}
    announce_activity_to_followers(env.hell, env.remote, create)            # the fast path
    other = make_instance('other.example', software='piefed')
    other.inbox = 'https://other.example/inbox'
    make_community_member(make_user(other, 'pf'), env.hell)
    announce_activity_to_followers(env.hell, env.remote, create, can_batch=True)   # the batch
    published = []
    monkeypatch.setitem(app.config, 'NOTIF_SERVER', 'x')
    monkeypatch.setattr('app.redis_client', SimpleNamespace(publish=lambda *a: published.append(a)))
    like = {'id': 'https://peer.example/activities/like/1', 'type': 'Like', 'actor': env.remote.public_url(),
            'object': post.ap_id}
    announce_activity_to_followers(env.hell, env.remote, like)                # the notification server
    assert _sent(wire) == [] and published == [] and db.session.query(ActivityBatch).count() == 0


# -- Review Focus 2 ---------------------------------------------------------------------------------------------------

def test_a_mention_of_an_onion_user_goes_out_as_plain_text(env, wire):
    post = _post(env.hell, env.hal, body='hello @ozzy@abc.onion')
    send_post(post.id, session=db.session)
    sent = _sent(wire)
    assert [url for url, _ in sent] == [PEER_INBOX]
    body = json.loads(sent[0][1])
    page = body['object']['object']
    assert [tag for tag in page['tag'] if tag.get('type') == 'Mention'] == []
    assert 'abc.onion' not in json.dumps(page['cc']) and 'abc.onion' not in json.dumps(body.get('cc'))
    assert '@ozzy@abc.onion' in page['content']


def test_federated_mentions_drops_local_only_users(env):
    assert federated_mentions([env.hal, env.ozzy, env.remote]) == [env.hal, env.remote]


# -- retries ----------------------------------------------------------------------------------------------------------

def test_a_queued_retry_of_a_local_only_activity_is_dropped(env, wire, app, monkeypatch):
    post = _post(env.hell, env.ozzy, local_only=True)
    like = {'id': f'{SERVER}/activities/like/1', 'type': 'Like', 'actor': env.hal.public_url(), 'object': post.ap_id}
    from_onion = {'id': f'{SERVER}/activities/follow/1', 'type': 'Follow', 'actor': env.ozzy.public_url(),
                  'object': 'https://peer.example/c/away'}
    for payload in (like, from_onion):
        db.session.add(SendQueue(destination=PEER_INBOX, destination_domain='peer.example', actor='k',
                                 private_key=env.hal.private_key, payload=json.dumps(payload), retries=1,
                                 retry_reason='', send_after=post.posted_at.replace(year=2000)))
    db.session.commit()
    monkeypatch.setattr('app.cli.sleep', lambda seconds: None)
    monkeypatch.setattr('app.cli.requeue_stuck_media', lambda: None)
    cli.register(app)
    result = app.test_cli_runner().invoke(args=['send-queue'])
    assert result.exception is None, result.exception
    post_request(PEER_INBOX, like, env.hal.private_key, 'k')     # a task queued before this guard existed
    assert _sent(wire) == []
    assert db.session.query(SendQueue).count() == 0


# -- what is not local-only is untouched ------------------------------------------------------------------------------

def test_a_hell_cloud_post_in_a_hell_cloud_community_is_sent_unchanged(env, wire, monkeypatch):
    """The body that leaves is byte-for-byte the one sent with the guard and the mention filter switched off."""
    monkeypatch.setattr('app.utils.gibberish', lambda *a: 'fixed')   # where activity_id reads it
    post = _post(env.hell, env.hal, body='plain words')
    send_post(post.id, session=db.session)
    guarded = _sent(wire)
    monkeypatch.setattr('app.local_only.outbound_refusal', lambda body, session=None: None)
    monkeypatch.setattr('app.shared.tasks.pages.federated_mentions', lambda users: users)
    send_post(post.id, session=db.session)
    assert _sent(wire)[len(guarded):] == guarded and [url for url, _ in guarded] == [PEER_INBOX]


# -- outbound_refusal itself ------------------------------------------------------------------------------------------

def _queries(monkeypatch, fn):
    """`fn()` and the SQL it ran, with domain_rows() answered from memory as its cache answers it in production
    (the suite runs a NullCache)."""
    rows = domain_rows()
    monkeypatch.setattr('app.storefront.domain_rows', lambda: rows)
    seen = []

    def record(conn, cursor, statement, *args):
        seen.append(statement)
    event.listen(db.engine, 'before_cursor_execute', record)
    try:
        return fn(), seen
    finally:
        event.remove(db.engine, 'before_cursor_execute', record)


def test_an_onion_url_in_any_guarded_field_is_refused(env):
    for key in ('id', 'actor', 'object', 'target', 'inReplyTo', 'audience', 'to', 'cc', 'attributedTo'):
        body = {'id': f'{SERVER}/activities/x', 'type': 'Announce', 'actor': 'https://peer.example/u/a',
                'object': {'type': 'Note', key: [f'{ONION}/u/ozzy']}}
        assert outbound_refusal(body), key


def test_local_only_rows_are_refused_in_every_url_form(env):
    post = _post(env.hell, env.hal, local_only=True)
    reply = _reply(_post(env.hell, env.hal), env.hal, local_only=True)
    for url in (f'{SERVER}/post/{post.id}', f'{SERVER}/post/{post.id}/replies', f'{SERVER}/c/hell/p/{post.id}/slug',
                f'{SERVER}/comment/{reply.id}', f'{SERVER}/post/{reply.post_id}/comment/{reply.id}',
                f'{SERVER}/comment/' + ''.join(f'%{ord(digit):02X}' for digit in str(reply.id)),
                f'https://TEST.piefed.local./post/{post.id}'):
        assert outbound_refusal({'id': f'{SERVER}/activities/x', 'object': {'inReplyTo': url}}), url


def test_ordinary_content_is_not_refused_and_costs_one_query_per_table(env, monkeypatch):
    post = _post(env.hell, env.hal)
    reply = _reply(post, env.hal)
    body = {'id': f'{SERVER}/activities/x', 'actor': env.hal.public_url(),
            'object': [{'id': post.ap_id}, {'id': reply.ap_id, 'inReplyTo': post.ap_id}, {'id': f'{SERVER}/post/99'}]}
    refused, queries = _queries(monkeypatch, lambda: outbound_refusal(body))
    assert refused is None and len(queries) == 2


def test_a_body_naming_nothing_of_ours_costs_no_query(env, monkeypatch):
    body = {'id': f'{SERVER}/activities/like/1', 'type': 'Like', 'actor': env.hal.public_url(),
            'object': 'https://peer.example/post/3', 'to': ['https://www.w3.org/ns/activitystreams#Public'],
            'content': f'{ONION}/u/ozzy is only text here'}
    refused, queries = _queries(monkeypatch, lambda: outbound_refusal(body))
    assert refused is None and queries == []


def test_a_refusal_is_logged_at_debug_without_a_user_name(env, wire, caplog):
    caplog.set_level('DEBUG', logger='app.local_only')
    body = {'id': f'{SERVER}/activities/follow/1', 'type': 'Follow', 'actor': env.ozzy.public_url(),
            'object': 'https://peer.example/c/away'}
    send_post_request(PEER_INBOX, body, env.ozzy.private_key, env.ozzy.public_url() + '#main-key')
    refusals = [record for record in caplog.records if 'not sent' in record.getMessage()]
    assert _sent(wire) == [] and len(refusals) == 1
    assert all(record.levelname == 'DEBUG' and 'ozzy' not in record.getMessage() for record in refusals)


def test_a_url_that_cannot_be_parsed_is_refused(env):
    """Fail closed: a guarded URL urlsplit rejects names a host nobody can check."""
    body = {'id': f'{SERVER}/activities/x', 'actor': env.hal.public_url(), 'object': 'http://[abc.onion]/u/x'}
    assert outbound_refusal(body)


def test_an_id_beyond_the_database_range_is_not_refused_on_that_basis(env):
    body = {'id': f'{SERVER}/activities/x', 'actor': env.hal.public_url(), 'object': f'{SERVER}/post/2147483648',
            'inReplyTo': f'{SERVER}/comment/99999999999999999999'}
    assert outbound_refusal(body) is None


def test_a_post_quoting_a_local_only_post_is_refused(env):
    quoted = _post(env.hell, env.ozzy, local_only=True)
    for key in ('quote', 'quoteUrl', 'quoteUri', '_misskey_quote', 'interactionTarget', 'interactingObject',
                'instrument'):
        body = {'id': f'{SERVER}/activities/create/1', 'type': 'Create', 'actor': env.hal.public_url(),
                'object': {'id': f'{SERVER}/post/{quoted.id + 1}', 'type': 'Note', key: quoted.ap_id}}
        assert outbound_refusal(body), key


def test_each_delivery_is_checked_once(env, wire, monkeypatch):
    """The send queue path (.delay) walks the body in post_request only, not again when it is queued."""
    real, calls = local_only.outbound_refusal, []
    monkeypatch.setattr('app.local_only.outbound_refusal', lambda body, session=None: calls.append(1) or
                        real(body, session))
    body = {'id': f'{SERVER}/activities/like/1', 'type': 'Like', 'actor': env.hal.public_url(),
            'object': 'https://peer.example/post/3'}
    send_post_request(PEER_INBOX, body, env.hal.private_key, env.hal.public_url() + '#main-key')
    assert len(calls) == 1 and [url for url, _ in _sent(wire)] == [PEER_INBOX]
