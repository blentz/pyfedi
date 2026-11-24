"""ATProto P4: a member's Bluesky identity at work (spec 2026-11-23-atproto-federation-design.md §6, §8; A8).

tests/atproto_fake.py plays the PDS: the author's identity is an account hosted there, and bob an account they meet.
Each action is taken through PieFed's own paths (the API arms of app.shared), and what reaches the PDS is read back.
"""
import uuid
from datetime import timedelta

import httpx
import pytest
from celery.exceptions import Retry

from app import db
from app.atproto import actors, identity, ingest
from app.clock import utcnow
from app.connections import Account, QuotaWait
from app.connections import bluesky
from app.connections import bluesky_hosted as hosted
from app.connections.bluesky import OAuthSession
from app.connections.tokens import seal_tokens
from app.constants import SRC_API
from app.models import AtprotoRecord, Language, Notification, PostReply, PostVote, SocialConnection, SocialCrosspost, UserBlock, \
    UserFollower
from app.shared.post import boost_post, unboost_post, vote_for_post
from app.shared.reply import delete_reply, make_reply, mod_remove_reply, mod_restore_reply, restore_reply, \
    vote_for_reply
from app.shared.user import block_another_user, follow_user_as, unblock_another_user, unfollow_user_as
from tests.factories import bearer, make_community_member
from tests.shared_fixtures.atproto import a_hosted, a_member, a_microblog

POST, LIKE, REPOST = 'app.bsky.feed.post', 'app.bsky.feed.like', 'app.bsky.feed.repost'
FOLLOW, BLOCK = 'app.bsky.graph.follow', 'app.bsky.graph.block'


def note(text='hello from bluesky', **fields):
    return {'$type': POST, 'text': text, 'createdAt': '2026-11-20T10:00:00.000Z', **fields}


@pytest.fixture
def world(inbound, monkeypatch):
    monkeypatch.setattr(identity, 'LOCK', f'test:{uuid.uuid4().hex}:identity:')
    inbound.identity = a_hosted(inbound, inbound.author, is_identity=True)
    inbound.me = inbound.identity.remote_id
    inbound.auth = bearer(inbound.author)
    return inbound


def bob_post(world, rkey='3kp', text='a post by bob', **fields):
    uri = world.pds.put(world.bob, POST, rkey, note(text, **fields))
    return ingest.post(world.bob, rkey, world.pds.records[uri], cid='bafyb')


def mine(world, collection) -> dict:
    """The records in the identity's repo of one collection, by URI."""
    prefix = f'at://{world.me}/{collection}/'
    return {uri: record for uri, record in world.pds.records.items() if uri.startswith(prefix)}


def reply(world, post, body='hi bob', parent_id=None):
    return make_reply({'body': body, 'notify_author': False, 'language_id': None}, post, parent_id, SRC_API,
                      world.auth)[1]


# --------------------------------------------------------------------------
# whose identity, and what a thing is on ATProto
# --------------------------------------------------------------------------

def test_the_identity_that_may_write(world):
    assert identity.identity_of(world.author.id).id == world.identity.id
    world.identity.slot_state = hosted.WAITING
    db.session.commit()
    assert identity.identity_of(world.author.id) is None
    world.identity.slot_state, world.identity.state = hosted.ACTIVE, 'reauth'
    db.session.commit()
    assert identity.identity_of(world.author.id) is None
    other = a_member('other')
    assert identity.identity_of(other.id) is None
    db.session.add(SocialConnection(user_id=other.id, service='bluesky', variant='oauth', remote_id='did:plc:o',
                                    remote_name='o', sealed_tokens=seal_tokens({}), is_identity=True))
    db.session.commit()
    assert identity.identity_of(other.id).variant == 'oauth'


def test_only_a_hosted_account_spends_the_budget(world, monkeypatch):
    spent = []
    monkeypatch.setattr(hosted, 'spend', spent.append)
    identity._spend({'kind': 'hosted'}, 'like')
    identity._spend({'did': 'did:plc:o'}, 'like')   # an own account: its own server's limits
    assert spent == ['like']


def test_a_session_for_each_kind(world):
    assert isinstance(identity.session_of(world.pds.tokens(world.me)), hosted.HostedSession)
    oauth = identity.session_of({'did': 'did:plc:o', 'pds': 'https://pds.example', 'access_token': 'a',
                                 'dpop_jwk': '{}'})
    assert isinstance(oauth, OAuthSession) and oauth.did == 'did:plc:o'


def test_what_a_post_or_reply_is_on_atproto(world):
    post = bob_post(world)
    assert identity.record_of(post) == (f'at://{world.bob}/{POST}/3kp', 'bafyb')
    theirs = ingest.post(world.bob, '3kr', note('re', reply={'parent': {'uri': f'at://{world.bob}/{POST}/3kp'}}),
                         cid='bafyr')
    assert identity.record_of(theirs) == (f'at://{world.bob}/{POST}/3kr', 'bafyr')
    ours = reply(world, post, 'sent')
    row = AtprotoRecord.query.filter_by(post_reply_id=ours.id, direction='out').one()
    assert identity.record_of(ours) == (row.uri, 'bafyc')
    row.cid = None
    db.session.commit()
    assert identity.record_of(ours) is None   # being made: not a record anyone can answer yet
    own = a_microblog(world)
    assert identity.record_of(own) is None
    db.session.add(SocialCrosspost(post_id=own.id, connection_id=world.identity.id, service='bluesky', state='sent',
                                   remote_id=f'at://{world.me}/{POST}/3kcopy'))
    db.session.commit()
    assert identity.record_of(own) == (f'at://{world.me}/{POST}/3kcopy', None)


def test_a_strong_reference_reads_a_missing_cid(world):
    assert identity.strong_ref(('at://x/y/z', 'c')) == {'uri': 'at://x/y/z', 'cid': 'c'}
    uri = world.pds.put(world.bob, POST, '3kq', note())
    assert identity.strong_ref((uri, None)) == {'uri': uri, 'cid': 'bafyg'}
    world.pds.fail['com.atproto.repo.getRecord'] = httpx.Response(200, json={'uri': uri})
    with pytest.raises(identity.Transient):
        identity.strong_ref((uri, None))


def test_a_replys_text(world):
    post = bob_post(world)
    short = reply(world, post, 'short')
    assert identity.reply_text(short) == 'short'
    long = reply(world, post, 'word ' * 100)
    text = identity.reply_text(long)
    assert len(text) <= 300 and text.endswith(long.ap_id) and '…' in text
    linked = reply(world, post, 'see [the docs](https://docs.example/a)')
    assert identity.reply_text(linked) == 'see the docs https://docs.example/a'
    warned = reply(world, post, 'spoilers')
    warned.content_warning = 'the ending'
    assert identity.reply_text(warned) == f'CW: the ending\n{warned.ap_id}'


# --------------------------------------------------------------------------
# each action, through PieFed's own paths
# --------------------------------------------------------------------------

def test_an_upvote_is_a_like_and_unvoting_deletes_it(world):
    post = bob_post(world)
    vote_for_post(post.id, 'upvote', True, None, SRC_API, auth=world.auth)
    (uri, record), = mine(world, LIKE).items()
    assert record['subject'] == {'uri': f'at://{world.bob}/{POST}/3kp', 'cid': 'bafyb'}
    row = AtprotoRecord.query.filter_by(uri=uri).one()
    assert (row.direction, row.cid, row.post_id, row.connection_id) == ('out', 'bafyc', post.id, world.identity.id)
    assert ingest.like(world.me, uri.rsplit('/', 1)[-1], record) is None   # its echo from Jetstream: ours
    vote_for_post(post.id, 'upvote', True, None, SRC_API, auth=world.auth)   # the same vote again takes it back
    assert mine(world, LIKE) == {} and AtprotoRecord.query.filter_by(direction='out').count() == 0


def test_a_downvote_or_a_private_vote_sends_nothing(world):
    post = bob_post(world)
    db.session.add(PostVote(user_id=world.author.id, author_id=post.user_id, post_id=post.id, effect=-1))
    db.session.commit()
    assert identity.sync(world.author.id, 'like_post', post.id) is None and mine(world, LIKE) == {}
    other = bob_post(world, '3kq')
    vote_for_post(other.id, 'upvote', False, None, SRC_API, auth=world.auth)   # voted privately
    assert mine(world, LIKE) == {}


def test_a_like_taken_back_privately_goes_too(world):
    post = bob_post(world)
    vote_for_post(post.id, 'upvote', True, None, SRC_API, auth=world.auth)
    assert len(mine(world, LIKE)) == 1
    vote_for_post(post.id, 'upvote', False, None, SRC_API, auth=world.auth)   # the same vote again, privately
    assert mine(world, LIKE) == {}


@pytest.mark.parametrize('reason', ['private community', 'local-only community', 'banned', 'followers only',
                                    'local-only member'])
def test_a_vote_that_may_not_be_told_is_never_a_like(world, monkeypatch, reason):
    post = bob_post(world)
    if reason == 'private community':
        world.microblogs.private = True
    elif reason == 'local-only community':
        world.microblogs.local_only = True
    elif reason == 'banned':
        monkeypatch.setattr(identity, 'user_banned_from_community', lambda user_id, community_id: True)
    elif reason == 'local-only member':
        monkeypatch.setattr(identity, 'user_local_only', lambda user: user.id == world.author.id)
    else:
        post.visibility = 'followers'
    db.session.commit()
    vote_for_post(post.id, 'upvote', True, None, SRC_API, auth=world.auth)
    assert mine(world, LIKE) == {}
    assert PostVote.query.filter_by(user_id=world.author.id, post_id=post.id).one().atproto_like is False


def test_an_upvote_of_a_reply(world):
    bob_post(world)
    theirs = ingest.post(world.bob, '3kr', note('re', reply={'parent': {'uri': f'at://{world.bob}/{POST}/3kp'}}),
                         cid='bafyr')
    vote_for_reply(theirs.id, 'upvote', True, None, SRC_API, auth=world.auth)
    (record,) = mine(world, LIKE).values()
    assert record['subject']['uri'] == f'at://{world.bob}/{POST}/3kr'
    assert AtprotoRecord.query.filter_by(direction='out').one().post_reply_id == theirs.id
    vote_for_reply(theirs.id, 'upvote', True, None, SRC_API, auth=world.auth)
    assert mine(world, LIKE) == {}
    vote_for_reply(theirs.id, 'upvote', False, None, SRC_API, auth=world.auth)   # voted privately
    assert mine(world, LIKE) == {}


def test_a_boost_is_a_repost(world):
    post = bob_post(world)
    boost_post(post.id, SRC_API, auth=world.auth)
    (record,) = mine(world, REPOST).values()
    assert record['subject']['uri'] == f'at://{world.bob}/{POST}/3kp'
    unboost_post(post.id, SRC_API, auth=world.auth)
    assert mine(world, REPOST) == {}


def test_a_reply_in_a_bluesky_thread_is_a_post_of_the_identity(world):
    post = bob_post(world)
    first = reply(world, post, 'hi bob')
    (uri, record), = mine(world, POST).items()
    parent = {'uri': f'at://{world.bob}/{POST}/3kp', 'cid': 'bafyb'}
    assert record['text'] == 'hi bob' and record['reply'] == {'root': parent, 'parent': parent}
    row = AtprotoRecord.query.filter_by(uri=uri).one()
    assert row.post_reply_id == first.id and row.cid == 'bafyc'
    assert ingest.post(world.me, uri.rsplit('/', 1)[-1], record) is None   # its echo
    second = reply(world, post, 'and see https://docs.example #tag', parent_id=first.id)
    identity.sync(world.author.id, 'reply', second.id)   # nothing left to do: made already
    nested = [r for r in mine(world, POST).values() if r['text'].startswith('and see')][0]
    assert nested['reply']['parent'] == {'uri': uri, 'cid': 'bafyc'} and nested['reply']['root'] == parent
    assert {feature['$type'] for facet in nested['facets'] for feature in facet['features']} == {
        'app.bsky.richtext.facet#link', 'app.bsky.richtext.facet#tag'}
    delete_reply(first.id, SRC_API, world.auth)
    assert uri not in world.pds.records
    restore_reply(first.id, SRC_API, world.auth)
    assert len(mine(world, POST)) == 2   # made again


def test_a_reply_with_a_language_goes_with_it(world):
    post = bob_post(world)
    english = Language.query.filter_by(code='en').one()
    made = make_reply({'body': 'in english', 'notify_author': False, 'language_id': english.id}, post, None, SRC_API,
                      world.auth)[1]
    assert made.language_id == english.id
    (record,) = mine(world, POST).values()
    assert record['langs'] == ['en']


def test_a_moderator_removing_and_restoring_a_reply(world):
    post = bob_post(world)
    make_community_member(world.author, world.microblogs, is_moderator=True)
    db.session.commit()
    made = reply(world, post)
    mod_remove_reply(made.id, 'spam', SRC_API, world.auth)
    assert mine(world, POST) == {}
    mod_restore_reply(made.id, 'not spam', SRC_API, world.auth)
    assert len(mine(world, POST)) == 1


def test_a_reply_under_a_members_copy_answering_bob(world):
    own = a_microblog(world)
    copy = world.pds.put(world.me, POST, '3kcopy', note('my post'))
    db.session.add(SocialCrosspost(post_id=own.id, connection_id=world.identity.id, service='bluesky', state='sent',
                                   remote_id=copy))
    db.session.commit()
    theirs = ingest.post(world.bob, '3kr', note('nice', reply={'parent': {'uri': copy}}), cid='bafyr')
    assert theirs.post_id == own.id   # bob's reply to the copy landed under the post
    answer = reply(world, own, 'thanks', parent_id=theirs.id)
    record = [r for r in mine(world, POST).values() if r['text'] == 'thanks'][0]
    assert record['reply'] == {'root': {'uri': copy, 'cid': 'bafyg'},
                               'parent': {'uri': f'at://{world.bob}/{POST}/3kr', 'cid': 'bafyr'}}
    assert AtprotoRecord.query.filter_by(post_reply_id=answer.id, direction='out').one()


def test_replies_that_stay_here(world, monkeypatch):
    own = a_microblog(world)
    assert reply(world, own, 'a thread nobody on Bluesky is in') is not None
    post = bob_post(world)
    kept = reply(world, post, 'mine')
    for stays in ('user_local_only', 'community_local_only'):   # a member, or a community, whose domain keeps it
        monkeypatch.setattr(identity, stays, lambda owner: True)
        assert identity.wanted(world.author.id, 'reply', kept.id) is None
        monkeypatch.setattr(identity, stays, lambda owner: False)
    delete_reply(kept.id, SRC_API, world.auth)
    kept.deleted = False
    kept.visibility = 'followers'
    db.session.commit()
    assert identity.wanted(world.author.id, 'reply', kept.id) is None
    assert identity.wanted(world.author.id, 'reply', 999999) is None
    other = a_member('other')
    assert identity.wanted(other.id, 'reply', kept.id) is None   # not theirs


def test_a_member_whose_domain_does_not_federate_tells_bluesky_nothing(world, monkeypatch):
    bob = actors.actor_for(world.bob)
    monkeypatch.setattr(identity, 'user_local_only', lambda user: True)
    follow_user_as(world.author, bob)
    block_another_user(bob.id, SRC_API, world.auth)
    assert mine(world, FOLLOW) == {} and mine(world, BLOCK) == {}
    assert identity.wanted(999999, 'follow', bob.id) is None and identity.may_tell(999999, bob_post(world)) is False


def test_a_follow_and_a_block(world):
    bob = actors.actor_for(world.bob)
    follow_user_as(world.author, bob)
    (record,) = mine(world, FOLLOW).values()
    assert record['subject'] == world.bob and record['$type'] == FOLLOW
    unfollow_user_as(world.author, bob)
    assert mine(world, FOLLOW) == {}
    block_another_user(bob.id, SRC_API, world.auth)
    (record,) = mine(world, BLOCK).values()
    assert record['subject'] == world.bob
    unblock_another_user(bob.id, SRC_API, world.auth)
    assert mine(world, BLOCK) == {}


def test_what_is_not_bluesky_sends_nothing(world, monkeypatch):
    queued = []
    monkeypatch.setattr(identity, 'later', lambda *args: queued.append(args))
    own = a_microblog(world)
    other = a_member('other')
    identity.voted(world.author.id, own, public=True)
    identity.boosted(world.author.id, own)
    identity.followed(world.author.id, other)
    identity.blocked(world.author.id, other)
    remote = PostReply(user_id=None)
    identity.replied(remote)
    assert queued == []
    for kind, target in (('like_post', 999999), ('like_reply', 999999), ('follow', other.id), ('block', 999999)):
        assert identity.wanted(world.author.id, kind, target) is None
    assert identity.wanted(world.author.id, 'like_post', own.id) is None


def test_a_member_with_no_identity_sends_nothing_and_catches_up_later(world):
    db.session.delete(world.identity)
    db.session.commit()
    post = bob_post(world)
    bob = actors.actor_for(world.bob)
    vote_for_post(post.id, 'upvote', True, None, SRC_API, auth=world.auth)
    boost_post(post.id, SRC_API, auth=world.auth)
    made = reply(world, post, 'said before')
    follow_user_as(world.author, bob)
    block_another_user(bob.id, SRC_API, world.auth)
    theirs = ingest.post(world.bob, '3kr', note('re', reply={'parent': {'uri': f'at://{world.bob}/{POST}/3kp'}}),
                         cid='bafyr')
    vote_for_reply(theirs.id, 'upvote', True, None, SRC_API, auth=world.auth)
    old = bob_post(world, '3kold')
    db.session.add(PostVote(user_id=world.author.id, author_id=old.user_id, post_id=old.id, effect=1,
                            atproto_like=True, created_at=utcnow() - timedelta(days=30)))   # too long ago
    private = bob_post(world, '3kpriv')
    vote_for_post(private.id, 'upvote', False, None, SRC_API, auth=world.auth)   # voted privately: never sent
    db.session.commit()
    assert world.pds.records.keys() == {uri for uri in world.pds.records if f'/{world.bob}/' in uri}
    connection = a_hosted(world, world.author, is_identity=True)
    world.me = connection.remote_id
    assert identity.caught_up(world.author.id) == 6
    assert len(mine(world, LIKE)) == 2 and len(mine(world, REPOST)) == 1 and len(mine(world, FOLLOW)) == 1
    assert len(mine(world, BLOCK)) == 1 and [r['text'] for r in mine(world, POST).values()] == ['said before']
    assert made.id


def test_starting_a_hosted_account_catches_up(world, monkeypatch):
    caught = []
    monkeypatch.setattr(identity, 'caught_up', caught.append)
    monkeypatch.setattr(hosted, 'backfill', lambda connection: 0)
    monkeypatch.setattr(hosted, 'put_profile_later', lambda user_id: None)
    hosted._after_start(world.identity)
    assert caught == [world.author.id]


def test_what_was_undone_while_it_could_not_write_is_caught_up(world):
    post = bob_post(world)
    vote_for_post(post.id, 'upvote', True, None, SRC_API, auth=world.auth)
    world.identity.slot_state = hosted.DEACTIVATED
    db.session.commit()
    PostVote.query.delete()
    db.session.commit()
    world.identity.slot_state = hosted.ACTIVE
    db.session.commit()
    assert identity.caught_up(world.author.id) == 1 and mine(world, LIKE) == {}


# --------------------------------------------------------------------------
# reconciling: retries, races, failures
# --------------------------------------------------------------------------

def test_a_write_whose_answer_was_lost_is_found_not_made_twice(world):
    post = bob_post(world)
    db.session.add(PostVote(user_id=world.author.id, author_id=post.user_id, post_id=post.id, effect=1,
                            atproto_like=True))
    db.session.commit()
    world.pds.fail['com.atproto.repo.createRecord'] = httpx.Response(502)
    with pytest.raises(identity.Transient):
        identity.sync(world.author.id, 'like_post', post.id)
    row = AtprotoRecord.query.filter_by(direction='out').one()
    assert row.cid is None and mine(world, LIKE) == {}
    identity.sync(world.author.id, 'like_post', post.id)   # not there: made under the same key
    assert list(mine(world, LIKE)) == [row.uri] and row.cid == 'bafyc'
    row.cid = None   # as if the answer of a write that worked was lost
    db.session.commit()
    assert identity.sync(world.author.id, 'like_post', post.id) == 'made'
    assert row.cid == 'bafyg' and len(mine(world, LIKE)) == 1
    assert identity.sync(world.author.id, 'like_post', post.id) is None


def test_an_account_that_cannot_write_keeps_its_record_until_it_can(world):
    post = bob_post(world)
    vote_for_post(post.id, 'upvote', True, None, SRC_API, auth=world.auth)
    world.identity.slot_state = hosted.DEACTIVATED
    db.session.commit()
    PostVote.query.filter_by(user_id=world.author.id).delete()
    db.session.commit()
    assert identity.sync(world.author.id, 'like_post', post.id) is None and len(mine(world, LIKE)) == 1
    world.identity.slot_state = hosted.ACTIVE
    db.session.commit()
    assert identity.sync(world.author.id, 'like_post', post.id) == 'deleted' and mine(world, LIKE) == {}


def test_a_member_whose_identity_went_away_keeps_nothing_pending(world):
    post = bob_post(world)
    db.session.add(PostVote(user_id=world.author.id, author_id=post.user_id, post_id=post.id, effect=1,
                            atproto_like=True))
    db.session.commit()
    world.identity.state = 'reauth'
    db.session.commit()
    assert identity.sync(world.author.id, 'like_post', post.id) is None


def test_the_task_waits_for_a_busy_lock_the_budget_and_a_failure(world, monkeypatch):
    post = bob_post(world)
    db.session.add(PostVote(user_id=world.author.id, author_id=post.user_id, post_id=post.id, effect=1,
                            atproto_like=True))
    db.session.commit()
    args = (world.author.id, 'like_post', post.id)
    lock = identity.app_pkg.redis_client.lock(f'{identity.LOCK}{world.author.id}:like_post:{post.id}', timeout=5)
    lock.acquire()
    with pytest.raises(identity.Busy):
        identity.sync(*args)
    with pytest.raises(Retry) as retry:
        identity.sync_task.apply(args=args)
    assert retry.value.when == 5
    lock.release()
    spend = hosted.spend
    monkeypatch.setattr(hosted, 'spend', lambda kind: (_ for _ in ()).throw(
        QuotaWait('wait', utcnow() + timedelta(seconds=40))))
    with pytest.raises(Retry) as retry:
        identity.sync_task.apply(args=args)
    assert 30 <= retry.value.when <= 40
    monkeypatch.setattr(hosted, 'spend', spend)
    world.pds.fail['com.atproto.repo.createRecord'] = httpx.Response(503)
    with pytest.raises(Retry) as retry:
        identity.sync_task.apply(args=args)
    assert retry.value.when == identity.BACKOFF[0]
    world.pds.fail['com.atproto.repo.createRecord'] = httpx.Response(503)
    identity.sync_task.apply(args=args, retries=len(identity.BACKOFF))   # given up: logged
    world.pds.fail['com.atproto.repo.createRecord'] = httpx.Response(400, json={'error': 'InvalidRequest'})
    identity.sync_task.apply(args=args)   # refused: logged, not retried
    assert mine(world, LIKE) == {}
    identity.sync_task.apply(args=args)
    assert len(mine(world, LIKE)) == 1


def test_a_lock_that_outlived_its_time(world, monkeypatch):
    class Lock:
        def acquire(self, blocking):
            return True

        def release(self):
            raise identity.redis.exceptions.LockError('expired')
    monkeypatch.setattr(identity.app_pkg.redis_client, 'lock', lambda *args, **kwargs: Lock())
    assert identity.sync(world.author.id, 'like_post', 999999) is None


def test_later_runs_at_once_in_debug_and_queues_otherwise(world, monkeypatch):
    ran, queued = [], []
    monkeypatch.setattr(identity.sync_task, 'apply', lambda args: ran.append(args))
    monkeypatch.setattr(identity.sync_task, 'delay', lambda *args: queued.append(args))
    identity.later(1, 'follow', 2)
    monkeypatch.setattr(world.app, 'debug', True)
    identity.later(3, 'block', 4)
    assert queued == [(1, 'follow', 2)] and ran == [(3, 'block', 4)]


def test_the_sweep_takes_up_what_a_lost_task_left(world, monkeypatch):
    post = bob_post(world)
    bob = actors.actor_for(world.bob)
    old = utcnow() - timedelta(hours=2)
    for rkey, columns in (('3ka', {'collection': LIKE, 'post_id': post.id}),
                          ('3kb', {'collection': FOLLOW, 'user_id': bob.id}),
                          ('3kc', {'collection': 'app.bsky.graph.list', 'user_id': bob.id}),
                          ('3kd', {'collection': LIKE, 'post_id': post.id, 'created_at': utcnow()}),
                          ('3ke', {'collection': REPOST, 'post_id': post.id, 'cid': 'done'})):
        db.session.add(AtprotoRecord(uri=f'at://{world.me}/x/{rkey}', did=world.me, rkey=rkey, direction='out',
                                     connection_id=world.identity.id, **{'created_at': old, **columns}))
    db.session.commit()
    other = a_hosted(world, a_member('other'), state=hosted.DEACTIVATED, is_identity=True)
    db.session.add(AtprotoRecord(uri=f'at://{other.remote_id}/x/3kf', did=other.remote_id, rkey='3kf',
                                 direction='out', connection_id=other.id, collection=LIKE, post_id=post.id,
                                 created_at=old))   # an account turned off: caught up when it is on again
    db.session.commit()
    queued = []
    monkeypatch.setattr(identity, 'later', lambda *args: queued.append(args))
    assert identity.sweep() == 2
    assert sorted(queued) == [(world.author.id, 'follow', bob.id), (world.author.id, 'like_post', post.id)]
    assert identity.sweep() == 0   # taken up: not again for an hour


def test_the_kind_of_a_row():
    assert identity.kind_of(AtprotoRecord(collection=LIKE, post_reply_id=4)) == ('like_reply', 4)
    assert identity.kind_of(AtprotoRecord(collection=POST, post_reply_id=5)) == ('reply', 5)
    assert identity.kind_of(AtprotoRecord(collection=BLOCK, user_id=6)) == ('block', 6)
    assert identity.kind_of(AtprotoRecord(collection=LIKE)) is None


# --------------------------------------------------------------------------
# the in-place offer (§8)
# --------------------------------------------------------------------------

def test_the_offer_on_a_bluesky_post(world, monkeypatch):
    post = bob_post(world)
    assert identity.offer(world.author, post) is None   # an identity already
    db.session.delete(world.identity)
    db.session.commit()
    with world.app.test_request_context(base_url=f'https://{world.home}'):
        offer = identity.offer(world.author, post)
    assert offer['create']['handle'] == f'author.{world.home}' and offer['connect'] is False and offer['at_home']
    assert offer['settings'] == f'https://{world.home}/user/settings/connections'
    assert identity.offer(world.author, a_microblog(world)) is None
    assert identity.offer(world.author, None) is None
    assert identity.offer(type('Anonymous', (), {'is_authenticated': False})(), post) is None
    monkeypatch.setattr(hosted, 'own_label', lambda user: (_ for _ in ()).throw(
        hosted.HandleRefused('taken', 'author-2')))
    with world.app.test_request_context(base_url=f'https://{world.home}'):
        assert identity.offer(world.author, post)['create']['label'] == 'author-2'
    monkeypatch.setattr(hosted, 'hosting_on', lambda domain_id: False)
    assert identity.offer(world.author, post) is None   # neither a Create nor a Connect is possible
    monkeypatch.setattr(identity, 'client_key', lambda: object())
    with world.app.test_request_context(base_url=f'https://{world.home}'):
        offer = identity.offer(world.author, post)
    assert offer['create'] is None and offer['connect'] is True
    waiting = a_hosted(world, world.author, state=hosted.WAITING)
    assert identity.offer(world.author, post) is None and waiting.id   # one on the way


def test_a_bluesky_post_offers_an_account_in_place(world, monkeypatch):
    monkeypatch.setitem(world.app.config, 'WTF_CSRF_ENABLED', True)
    post = bob_post(world)
    db.session.delete(world.identity)
    db.session.commit()
    del world.pds.accounts[world.me]   # its handle is free again
    page = world.client.get(f'/post/{post.id}').data.decode()
    assert 'This post is from Bluesky' in page and f'Create @author.{world.home}' in page
    assert f'name="next" value="/post/{post.id}"' in page
    assert 'bluesky_badge' in page and '@bob.bsky.example' in page   # an ATProto author: @handle and the badge
    response = world.client.post('/user/settings/connections/bluesky/create', data={
        'csrf_token': world.token, 'label': 'author', 'next': f'/post/{post.id}'})
    assert response.status_code == 302 and response.location.endswith(f'/post/{post.id}')
    assert 'This post is from Bluesky' not in world.client.get(f'/post/{post.id}').data.decode()


def test_the_settings_page_offers_an_own_account_as_the_identity(world):
    db.session.delete(world.identity)
    oauth = SocialConnection(user_id=world.author.id, service='bluesky', variant='oauth',
                             remote_id='did:plc:oooooooooooooooooooooooo', remote_name='me.bsky.example',
                             sealed_tokens=seal_tokens({}))
    db.session.add(oauth)
    db.session.commit()
    page = world.client.get('/user/settings/connections').data.decode()
    assert 'Use for follows and replies' in page and 'value="did:plc:oooooooooooooooooooooooo"' in page
    oauth.is_identity = True
    db.session.commit()
    page = world.client.get('/user/settings/connections').data.decode()
    assert 'Use for follows and replies' not in page and 'Follows and replies' in page


def test_connecting_an_own_account_as_the_identity_from_a_post(world, monkeypatch):
    db.session.delete(world.identity)
    db.session.commit()
    seen, caught = [], []

    def authorize_url(variant, state, redirect_uri, pending, hint=''):
        seen.append(dict(pending))
        return f'https://pds.example/authorize?state={state}'

    def finish(variant, args, redirect_uri, pending):
        did = f'did:plc:{args["code"] * 24}'[:32]
        return [Account(remote_id=did, remote_name='me.bsky.example', variant='oauth', identity=bool(
            pending.get('identity')), tokens={'did': did, 'scope': bluesky.IDENTITY_SCOPE})]
    monkeypatch.setattr(bluesky.SERVICE, 'authorize_url', authorize_url)
    monkeypatch.setattr(bluesky.SERVICE, 'finish', finish)
    monkeypatch.setattr(identity, 'caught_up', caught.append)

    def connect(code, **form):
        response = world.client.post('/user/settings/connections/bluesky/connect',
                                     data={'csrf_token': world.token, **form})
        state = response.location.split('state=')[1]
        return world.client.get(f'/user/settings/connections/bluesky/callback?state={state}&code={code}')

    response = connect('a', identity='1', hint='me.bsky.example', next='/post/7')
    assert seen[-1] == {'identity': True} and response.location.endswith('/post/7')
    first = SocialConnection.query.filter_by(remote_id='did:plc:' + 'a' * 24).one()
    assert first.is_identity and caught == [world.author.id]
    response = connect('b', identity='1', next='//elsewhere.example/x')   # an identity already; never another site
    assert response.location.endswith('/user/settings/connections')
    assert not SocialConnection.query.filter_by(remote_id='did:plc:' + 'b' * 24).one().is_identity
    assert caught == [world.author.id]
    connect('a')   # the identity, signed in again (it needed reconnecting): what it missed is caught up
    assert seen[-1] == {} and caught == [world.author.id] * 2   # a plain sign-in asks for no more than copies


# --------------------------------------------------------------------------
# what answers our records comes back (§7)
# --------------------------------------------------------------------------

def a_copy(world, rkey='3kcopy'):
    own = a_microblog(world)
    uri = world.pds.put(world.me, POST, rkey, note('my post'))
    db.session.add(SocialCrosspost(post_id=own.id, connection_id=world.identity.id, service='bluesky', state='sent',
                                   remote_id=uri))
    db.session.commit()
    return own, uri


def test_a_like_of_a_copy_or_a_sent_reply_is_an_upvote(world):
    own, uri = a_copy(world)
    liked = ingest.like(world.bob, '3kl', {'subject': {'uri': uri, 'cid': 'bafyg'}}, 'c')
    bob = actors.actor_for(world.bob, fetch_new=False)
    assert liked.id == own.id and PostVote.query.filter_by(user_id=bob.id, post_id=own.id).one().effect > 0
    row = AtprotoRecord.query.filter_by(uri=f'at://{world.bob}/{LIKE}/3kl').one()
    assert (row.direction, row.post_id, row.vote_kind) == ('in', own.id, 'like')
    assert ingest.like(world.bob, '3kl', {'subject': {'uri': uri}}) is None   # delivered twice: once
    assert ingest.delete(world.bob, LIKE, '3kl') and PostVote.query.filter_by(user_id=bob.id).count() == 0
    post = bob_post(world)
    answer = reply(world, post, 'sent')
    sent = AtprotoRecord.query.filter_by(post_reply_id=answer.id, direction='out').one()
    assert ingest.like(world.bob, '3km', {'subject': {'uri': sent.uri}}).id == answer.id
    assert ingest.delete(world.bob, LIKE, '3km')


def test_likes_that_are_dropped(world):
    own, uri = a_copy(world)
    post = bob_post(world)
    for record in ({'subject': {'uri': f'at://{world.bob}/{POST}/3kp'}}, {'subject': 'x'}, None,
                   {'subject': {'uri': f'at://{world.me}/{POST}/unknown'}}):
        assert ingest.like(world.bob, '3kx', record) is None   # not ours, or not a like of anything
    assert ingest.like(world.me, '3ky', {'subject': {'uri': uri}}) is None   # the member's own account
    bob = actors.actor_for(world.bob)
    block_another_user(bob.id, SRC_API, world.auth)
    assert ingest.like(world.bob, '3kz', {'subject': {'uri': uri}}) is None
    unblock_another_user(bob.id, SRC_API, world.auth)
    own.deleted = True
    db.session.commit()
    assert ingest.like(world.bob, '3kz', {'subject': {'uri': uri}}) is None
    assert post.id and PostVote.query.filter_by(post_id=own.id).count() == 0


def test_a_like_whose_target_went_is_forgotten(world):
    own, uri = a_copy(world)
    ingest.like(world.bob, '3kl', {'subject': {'uri': uri}})
    row = AtprotoRecord.query.filter_by(collection=LIKE).one()
    row.post_id = None
    db.session.commit()
    assert ingest.delete(world.bob, LIKE, '3kl') and AtprotoRecord.query.filter_by(collection=LIKE).count() == 0


def test_a_follow_of_an_identity_makes_a_follower(world):
    world.author.ap_manually_approves_followers = False
    db.session.commit()
    member = ingest.follow(world.bob, '3kf', {'subject': world.me}, 'c')
    bob = actors.actor_for(world.bob, fetch_new=False)
    assert member.id == world.author.id
    row = UserFollower.query.filter_by(local_user_id=world.author.id, remote_user_id=bob.id, is_inward=True).one()
    assert row.is_accepted is True and world.author.unread_notifications == 1
    assert ingest.follow(world.bob, '3kf', {'subject': world.me}) is None   # once
    assert ingest.follow(world.bob, '3kg', {'subject': world.me}).id == world.author.id   # again: no second row
    assert UserFollower.query.filter_by(is_inward=True).count() == 1
    assert ingest.delete(world.bob, FOLLOW, '3kf') and UserFollower.query.filter_by(is_inward=True).count() == 0


def test_a_member_who_approves_followers_is_asked(world, monkeypatch):
    from app.shared.user import answer_follow_request
    world.author.ap_manually_approves_followers = True
    db.session.commit()
    assert ingest.follow(world.bob, '3kf', {'subject': world.me}).id == world.author.id
    bob = actors.actor_for(world.bob, fetch_new=False)
    row = UserFollower.query.filter_by(local_user_id=world.author.id, remote_user_id=bob.id, is_inward=True).one()
    notification = Notification.query.filter_by(user_id=world.author.id).one()
    assert row.is_accepted is None and notification.url == '/user/follow_requests'
    monkeypatch.setattr('app.shared.user.ap_signature.send_post_request',
                        lambda *args, **kwargs: pytest.fail('answered over ActivityPub'))
    assert answer_follow_request(world.author, bob.id, True) and row.is_accepted is True   # Bluesky asks no answer


@pytest.mark.parametrize('refusal', ['instance_banned', 'refuses_remote_contact'])
def test_a_follow_from_a_banned_network_or_to_a_member_who_stays_here(world, monkeypatch, refusal):
    monkeypatch.setattr(ingest, refusal, lambda *args: True)
    assert ingest.follow(world.bob, '3kf', {'subject': world.me}) is None


def test_follows_that_are_dropped(world):
    assert ingest.follow(world.bob, '3kf', {'subject': 'did:plc:nobodyhere'}) is None
    assert ingest.follow(world.bob, '3kf', {'subject': 3}) is None and ingest.follow(world.bob, '3kf', None) is None
    assert ingest.follow(world.me, '3kf', {'subject': world.me}) is None
    bob = actors.actor_for(world.bob)
    block_another_user(bob.id, SRC_API, world.auth)
    assert ingest.follow(world.bob, '3kf', {'subject': world.me}) is None
    unblock_another_user(bob.id, SRC_API, world.auth)
    world.author.banned = True
    db.session.commit()
    assert ingest.follow(world.bob, '3kf', {'subject': world.me}) is None
    assert UserFollower.query.filter_by(is_inward=True).count() == 0


def test_replies_and_reposts_of_our_records_land_on_them(world):
    own, uri = a_copy(world)
    answer = ingest.post(world.bob, '3kr', note('nice', reply={'parent': {'uri': uri}}))
    assert answer.post_id == own.id
    boosted = ingest.repost(world.bob, '3kb', {'subject': {'uri': uri}})
    assert boosted.id == own.id
    assert ingest.repost(world.me, '3kc', {'subject': {'uri': uri}}) is None   # the member's own account


def test_likes_and_follows_by_event(world):
    own, uri = a_copy(world)
    ingest.handle({'did': world.bob, 'collection': LIKE, 'operation': 'create', 'rkey': '3kl',
                   'record': {'subject': {'uri': uri}}})
    ingest.handle({'did': world.bob, 'collection': FOLLOW, 'operation': 'create', 'rkey': '3kf',
                   'record': {'subject': world.me}})
    assert PostVote.query.filter_by(post_id=own.id).count() == 1
    assert UserFollower.query.filter_by(is_inward=True).count() == 1


def test_a_quote_post_names_what_it_quotes(world):
    own, uri = a_copy(world)
    record = note('look at this', embed={'$type': 'app.bsky.embed.record', 'record': {'uri': uri, 'cid': 'x'}})
    quoting = ingest.post(world.bob, '3kq', record)
    assert quoting.quote_ap_id == own.ap_id   # the member's post, shown as a card
    other = world.pds.put(world.bob, POST, '3ko', note('elsewhere'))
    record = note('and this', embed={'$type': 'app.bsky.embed.recordWithMedia',
                                     'record': {'record': {'uri': other}}, 'media': {'$type': 'app.bsky.embed.images',
                                                                                     'images': []}})
    assert ingest.post(world.bob, '3kq2', record).quote_ap_id == actors.post_url(other)
    assert ingest.quote_of(None) is None


def test_a_mention_of_a_member_is_a_mention_here(world):
    text = f'hi @author.{world.home} and @bob'
    start = text.index('@author')
    end = start + len(f'@author.{world.home}')
    facets = [{'index': {'byteStart': start, 'byteEnd': end},
               'features': [{'$type': 'app.bsky.richtext.facet#mention', 'did': world.me}]},
              {'index': {'byteStart': end + 5, 'byteEnd': end + 9},
               'features': [{'$type': 'app.bsky.richtext.facet#mention', 'did': world.bob}]},
              {'features': ['odd']}, 'odd']
    members = ingest.mentioned(facets)
    assert list(members) == [world.me] and members[world.me].id == world.author.id
    html = ingest.html_of(text, facets)
    assert f'href="{actors.profile_url(world.me)}"' in html and f'href="{actors.profile_url(world.bob)}"' in html
    activity = ingest.activity_of(actors.actor_for(world.bob), f'at://{world.bob}/{POST}/3km',
                                  note(text, facets=facets), 'https://pds.example')
    assert {'type': 'Mention', 'href': world.author.profile_id(), 'name': '@author'} in activity['object']['tag']
    assert ingest.mentioned(None) == {} and ingest.mentioned([{'features': [{'$type': 'x'}]}]) == {}
    made = ingest.post(world.bob, '3km', note(text, facets=facets))
    assert made is not None and made.url is None   # a mention is no link of the post's
