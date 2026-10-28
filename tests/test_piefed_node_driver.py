"""The FediTest node driver for PieFed (interop/conformance/feditest/nodedrivers/piefed.py), against the test database.

The conformance run is the driver's only other exercise, and it takes a full interop stack. These tests cover what it
reads from the database (users, follows, received text, replies, likers, authors), its polling and resolve retry, and
what it asks the alpha API for, with the API client replaced by a recorder.

The driver is loaded as the runner loads it, through feditest.load_node_drivers_from: its @nodedriver decorator refuses
to register a class imported any other way, and refuses one registered twice, so it is loaded once per process."""
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import feditest
import httpx
import pytest
from feditest.nodedrivers import DefaultAccountManager, NodeConfiguration

from app import db
from tests.factories import (make_community, make_follow, make_instance, make_post, make_post_reply, make_post_reply_vote,
                             make_post_vote, make_user)

NODEDRIVERS = Path(__file__).resolve().parent.parent / 'interop' / 'conformance' / 'feditest' / 'nodedrivers'
HOST = 'piefed.test'
PEER = 'mastodon.test'


def driver_module():
    if 'piefed.PieFedNodeDriver' not in feditest.all_node_drivers:
        feditest.load_node_drivers_from([str(NODEDRIVERS)])
    return sys.modules['piefed']


class Client:
    """The alpha API client, as a recorder: each call is kept, and resolve answers from `resolved`."""

    def __init__(self, resolved=None):
        self.calls, self.resolved = [], resolved or {}

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return {'post': {'ap_id': 'https://piefed.test/post/9'}, 'comment': {'ap_id': 'https://piefed.test/comment/9'}}
        return call

    def resolve(self, query):
        self.calls.append(('resolve', (query,), {}))
        return self.resolved[query]

    def microblogs_id(self):
        return 7


@pytest.fixture
def piefed(db_session, monkeypatch):
    module = driver_module()
    monkeypatch.setenv('DATABASE_URL', db.engine.url.render_as_string(hide_password=False))
    monkeypatch.setattr(module, 'POLL_DEADLINE', 0.05)   # the real 30s is for federation; nothing here is in flight
    monkeypatch.setattr(module, 'POLL_INTERVAL', 0.01)
    driver = module.PieFedNodeDriver()
    accounts = DefaultAccountManager([module.PieFedAccount(None, 'alice', 'pw')], [])
    node = module.PieFedNode('node', NodeConfiguration(driver, 'PieFed', None, HOST), accounts)
    yield SimpleNamespace(module=module, node=node)
    if node._conn is not None:
        node._conn.close()


@pytest.fixture
def world(db_session):
    local = make_instance(HOST, software='piefed')
    peer = make_instance(PEER, software='mastodon')
    alice = make_user(local, 'alice', local=True)
    bob = make_user(peer, 'bob')
    community = make_community('microblogs')
    db.session.commit()
    return SimpleNamespace(local=local, peer=peer, alice=alice, bob=bob, community=community)


def acct(name, host=HOST):
    return f'acct:{name}@{host}'


def test_the_runner_finds_the_driver(piefed):
    assert feditest.all_node_drivers['piefed.PieFedNodeDriver'] is piefed.module.PieFedNodeDriver


def test_a_local_account_is_found_by_name_and_a_remote_one_by_name_and_domain(piefed, world):
    assert piefed.node._user_id(acct('Alice')) == world.alice.id
    assert piefed.node._user_id(acct('bob', 'Mastodon.test')) == world.bob.id
    assert piefed.node._user_id(acct('bob')) is None   # a local bob, which there is not
    assert piefed.node._user_id(acct('nobody', PEER)) is None


def test_an_accepted_outward_follow_is_alice_following_bob(piefed, world):
    make_follow(world.alice, world.bob)
    assert piefed.node.actor_is_following_actor(acct('alice'), acct('bob', PEER)) is True
    assert piefed.node.actor_is_followed_by_actor(acct('bob', PEER), acct('alice')) is True
    assert piefed.node.actor_is_following_actor(acct('bob', PEER), acct('alice')) is False


def test_an_accepted_inward_follow_is_bob_following_alice(piefed, world):
    make_follow(world.alice, world.bob, is_inward=True)
    assert piefed.node.actor_is_followed_by_actor(acct('alice'), acct('bob', PEER)) is True
    assert piefed.node.actor_is_following_actor(acct('alice'), acct('bob', PEER)) is False


def test_a_pending_follow_or_an_unknown_account_is_not_a_follow(piefed, world):
    make_follow(world.alice, world.bob, is_accepted=None)
    assert piefed.node.actor_is_following_actor(acct('alice'), acct('bob', PEER)) is False
    assert piefed.node.actor_is_following_actor(acct('alice'), acct('nobody', PEER)) is False


def test_a_received_note_reads_as_its_text_without_the_mention_that_addressed_it(piefed, world):
    post = make_post(world.community, world.bob, ap_id='https://mastodon.test/n/1')
    post.body_html = '<p>hello there https://piefed.test/u/alice</p>'
    reply = make_post_reply(post, world.bob, body='a reply')
    reply.ap_id = 'https://mastodon.test/n/2'
    db.session.commit()

    assert piefed.node.actor_has_received_object(acct('alice'), post.ap_id) == 'hello there'
    assert piefed.node.note_content(acct('alice'), reply.ap_id) == 'a reply'


def test_a_deleted_or_unknown_note_is_not_received(piefed, world):
    post = make_post(world.community, world.bob, ap_id='https://mastodon.test/n/3')
    post.deleted = True
    db.session.commit()

    assert piefed.node.actor_has_received_object(acct('alice'), post.ap_id) is None
    assert piefed.node.actor_has_received_object(acct('alice'), 'https://mastodon.test/n/none') is None


def test_direct_replies_are_one_level_down_and_not_deleted(piefed, world):
    post = make_post(world.community, world.bob, ap_id='https://mastodon.test/n/4')
    top, gone, child = (make_post_reply(post, world.alice, body=body) for body in ('top', 'gone', 'child'))
    top.ap_id, gone.ap_id, child.ap_id = 'https://piefed.test/c/1', 'https://piefed.test/c/2', 'https://piefed.test/c/3'
    gone.deleted = True
    child.parent_id = top.id
    db.session.commit()

    assert piefed.node.direct_replies_to_object(acct('alice'), post.ap_id) == [top.ap_id]
    assert piefed.node.direct_replies_to_object(acct('alice'), top.ap_id) == [child.ap_id]
    assert piefed.node.direct_replies_to_object(acct('alice'), child.ap_id) == []


def test_the_likers_of_a_post_or_reply_are_its_upvoters(piefed, world):
    carol = make_user(world.peer, 'carol')
    post = make_post(world.community, world.alice, ap_id='https://piefed.test/post/5')
    reply = make_post_reply(post, world.alice, body='r')
    reply.ap_id = 'https://piefed.test/comment/5'
    db.session.commit()
    make_post_vote(world.bob, post, 1.0)
    make_post_vote(carol, post, -1.0)
    make_post_reply_vote(carol, reply, 1.0)

    assert piefed.node.object_likers(acct('alice'), post.ap_id) == [acct('bob', PEER)]
    assert piefed.node.object_likers(acct('alice'), reply.ap_id) == [acct('carol', PEER)]


def test_an_author_is_named_on_its_own_host(piefed, world):
    post = make_post(world.community, world.bob, ap_id='https://mastodon.test/n/6')
    reply = make_post_reply(post, world.alice, body='r')
    reply.ap_id = 'https://piefed.test/comment/6'
    db.session.commit()

    assert piefed.node.object_author(acct('alice'), post.ap_id) == acct('bob', PEER)
    assert piefed.node.object_author(acct('alice'), reply.ap_id) == acct('alice')
    assert piefed.node.object_author(acct('alice'), 'https://mastodon.test/n/none') is None


def test_a_read_returns_as_soon_as_it_is_ready_and_its_last_answer_at_the_deadline(piefed):
    answers = iter([[], ['a'], ['b']])
    assert piefed.node._await(lambda: next(answers)) == ['a']
    reads = []
    assert piefed.node._await(lambda: reads.append(1) or None) is None
    assert len(reads) > 1   # it kept reading until the deadline


def test_resolve_retries_a_400_until_it_answers(piefed):
    answers = iter([400, 400, None])

    class Resolving:
        def resolve(self, query):
            status = next(answers)
            if status:
                raise httpx.HTTPStatusError('no', request=httpx.Request('GET', 'https://x'), response=httpx.Response(status))
            return {'post': query}

    assert piefed.node._resolve(Resolving(), 'q') == {'post': 'q'}


@pytest.mark.parametrize('status, deadline', [(500, 30), (400, 0)])
def test_resolve_gives_up_on_another_status_at_once_and_on_a_400_at_the_deadline(piefed, monkeypatch, status, deadline):
    monkeypatch.setattr(piefed.module, 'POLL_DEADLINE', deadline)

    class Refusing:
        def resolve(self, query):
            raise httpx.HTTPStatusError('no', request=httpx.Request('GET', 'https://x'), response=httpx.Response(status))

    with pytest.raises(httpx.HTTPStatusError):
        piefed.node._resolve(Refusing(), 'q')


def test_a_client_is_made_once_per_account_and_only_for_one_the_plan_names(piefed, monkeypatch):
    """The plan's accounts carry no role; FediTest allocates one to a role when a test first obtains it, and only an
    allocated account is matched, as in a run, where a test obtains its actors before it acts as them."""
    made = []
    monkeypatch.setattr(piefed.module, 'PieFed', lambda user, password: made.append((user, password)) or object())
    piefed.node.obtain_actor_acct_uri('sender')
    assert piefed.node._client(acct('alice')) is piefed.node._client(acct('alice'))
    assert made == [('alice', 'pw')]
    with pytest.raises(ValueError, match='No account mallory'):
        piefed.node._client(acct('mallory'))


@pytest.fixture
def client(piefed, monkeypatch):
    client = Client({
        '@bob@mastodon.test': {'person': {'person': {'id': 3}}},
        'https://mastodon.test/n/7': {'post': {'post': {'id': 11}}},
        'https://mastodon.test/c/8': {'comment': {'comment': {'id': 12, 'post_id': 11}}},
    })
    monkeypatch.setattr(piefed.node, '_client', lambda actor: client)
    return client


def test_a_note_addresses_its_recipients_by_mention_in_microblogs(piefed, client):
    ap_id = piefed.node.make_create_note(acct('alice'), 'hi', deliver_to=[acct('bob', PEER)])
    assert ap_id == 'https://piefed.test/post/9'
    assert client.calls == [('post', ('hi @bob@mastodon.test', 'hi @bob@mastodon.test', 7), {})]


def test_a_reply_goes_under_a_post_or_a_comment(piefed, client):
    piefed.node.make_reply_note(acct('alice'), 'https://mastodon.test/n/7', 'to the post')
    piefed.node.make_reply_note(acct('alice'), 'https://mastodon.test/c/8', 'to the comment')
    assert [call for call in client.calls if call[0] == 'reply'] == [
        ('reply', (11, 'to the post'), {}), ('reply', (11, 'to the comment', 12), {})]


def test_a_like_and_its_undo_are_votes_on_a_post_or_a_comment(piefed, client):
    piefed.node.like_object(acct('alice'), 'https://mastodon.test/n/7')
    piefed.node.unlike_object(acct('alice'), 'https://mastodon.test/c/8')
    assert [call for call in client.calls if call[0] != 'resolve'] == [('like_post', (11, 1), {}), ('like_comment', (12, 0), {})]


def test_follow_unfollow_edit_and_delete_act_on_the_resolved_ids(piefed, client):
    piefed.node.make_follow(acct('alice'), acct('bob', PEER))
    piefed.node.make_unfollow(acct('alice'), acct('bob', PEER))
    piefed.node.update_note(acct('alice'), 'https://mastodon.test/n/7', 'new text')
    piefed.node.delete_object(acct('alice'), 'https://mastodon.test/n/7')
    assert [call for call in client.calls if call[0] != 'resolve'] == [
        ('follow_user', (3,), {}), ('unfollow_user', (3,), {}),
        ('edit', (11,), {'title': 'new text', 'body': 'new text'}), ('delete', (11,), {})]


def test_a_dropped_database_connection_is_opened_again(piefed, world):
    assert piefed.node._user_id(acct('alice')) == world.alice.id
    first = piefed.node._conn
    first.close()
    assert piefed.node._user_id(acct('alice')) == world.alice.id
    assert piefed.node._conn is not first and not piefed.node._conn.closed


def test_the_account_identifiers_are_the_plans_accounts(piefed):
    assert piefed.node.obtain_actor_acct_uri() == acct('alice')
    assert piefed.node.obtain_account_identifier() == acct('alice')
    assert piefed.node.obtain_actor_document_uri() == 'https://piefed.test/u/alice'


def test_the_unit_tests_and_the_runner_pin_one_feditest():
    """What is tested here is the FediTest the conformance runner runs."""
    root = Path(__file__).resolve().parent.parent
    pin = re.compile(r'github\.com/fediverse-devnet/feditest@([0-9a-f]{40})')
    assert pin.findall((root / 'requirements-test.txt').read_text()) == \
        pin.findall((root / 'interop' / 'conformance' / 'Containerfile').read_text())
