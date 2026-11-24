"""ATProto P4: notifications in (spec 2026-11-23-atproto-federation-design.md §7), and quotes out (§6).

What answers a member on Bluesky from someone nobody here follows reaches PieFed only through the member's own
listNotifications: tests/atproto_fake.py's PDS answers it from `notifications`."""
import uuid

import httpx
import pytest

from app import db
from app.atproto import actors, ingest, notify, records
from app.connections import bluesky
from app.connections import bluesky_hosted as hosted
from app.connections.tokens import seal_tokens
from app.models import PostReply, PostVote, SocialConnection, SocialCrosspost, UserFollower
from tests.atproto_fake import PDS
from tests.shared_fixtures.atproto import a_hosted, a_microblog

POST, LIKE, FOLLOW = 'app.bsky.feed.post', 'app.bsky.feed.like', 'app.bsky.graph.follow'


def note(text='hello', **fields):
    return {'$type': POST, 'text': text, 'createdAt': '2026-11-20T10:00:00.000Z', **fields}


@pytest.fixture
def world(inbound, monkeypatch):
    tag = uuid.uuid4().hex
    monkeypatch.setattr(notify, 'MARK', f'test:{tag}:notified:')
    monkeypatch.setattr(notify, 'DUE', f'test:{tag}:due:')
    inbound.identity = a_hosted(inbound, inbound.author, is_identity=True)
    inbound.me = inbound.identity.remote_id
    own = a_microblog(inbound)
    inbound.copy = inbound.pds.put(inbound.me, POST, '3kcopy', note('my post'))
    db.session.add(SocialCrosspost(post_id=own.id, connection_id=inbound.identity.id, service='bluesky',
                                   state='sent', remote_id=inbound.copy))
    db.session.commit()
    inbound.own = own
    return inbound


def item(world, collection, rkey, record, at, reason='like', did=None):
    did = did or world.bob
    return {'uri': f'at://{did}/{collection}/{rkey}', 'cid': f'c{rkey}', 'reason': reason, 'record': record,
            'indexedAt': at, 'author': {'did': did}}


def test_what_answers_a_member_comes_back(world):
    world.pds.notifications[world.me] = [
        item(world, FOLLOW, '3kf', {'subject': world.me}, '2026-11-24T10:03:00.000Z', 'follow'),
        item(world, POST, '3kr', note('nice', reply={'parent': {'uri': world.copy}}), '2026-11-24T10:02:00.000Z',
             'reply'),
        item(world, LIKE, '3kl', {'subject': {'uri': world.copy}}, '2026-11-24T10:01:00.000Z'),
        item(world, 'app.bsky.graph.starterpack', '3ks', {}, '2026-11-24T10:00:30.000Z', 'starterpack-joined'),
        {'uri': 'odd', 'indexedAt': '2026-11-24T10:00:20.000Z'}, 'odd', {'uri': 3},
        {'uri': f'at://{world.bob}/{LIKE}', 'record': {}, 'indexedAt': '2026-11-24T10:00:10.000Z'},
        item(world, LIKE, '3kn', None, '2026-11-24T10:00:00.000Z')]
    assert notify.poll(world.identity.id) == 3
    bob = actors.actor_for(world.bob, fetch_new=False)
    assert PostVote.query.filter_by(user_id=bob.id, post_id=world.own.id).count() == 1
    assert PostReply.query.filter_by(user_id=bob.id).one().post_id == world.own.id
    assert UserFollower.query.filter_by(local_user_id=world.author.id, remote_user_id=bob.id, is_inward=True).one()
    assert notify.poll(world.identity.id) == 0   # nothing newer
    world.pds.notifications[world.me].insert(0, item(world, LIKE, '3km', {'subject': {'uri': world.copy}},
                                                     '2026-11-24T10:04:00.000Z'))
    assert notify.poll(world.identity.id) == 1


def test_a_poll_reads_back_to_the_last_one_page_by_page(world, monkeypatch):
    monkeypatch.setattr(notify, 'PAGE', 2)
    stamps = [f'2026-11-24T10:0{minute}:00.000Z' for minute in range(9, 0, -1)]
    world.pds.notifications[world.me] = [item(world, LIKE, f'3k{n}', {'subject': {'uri': world.copy}}, at)
                                         for n, at in enumerate(stamps[-2:])]
    assert notify.poll(world.identity.id) == 2   # the first poll: one page
    world.pds.notifications[world.me] = [item(world, LIKE, f'3k{n}', {'subject': {'uri': world.copy}}, at)
                                         for n, at in enumerate(stamps)]
    taken = []
    monkeypatch.setattr(notify, 'take', lambda found: taken.append(found['indexedAt']) or True)
    assert notify.poll(world.identity.id) == 7 and taken == sorted(stamps[:-2])   # oldest first, to the mark
    monkeypatch.setattr(notify, 'MAX_PAGES', 1)
    world.pds.notifications[world.me] = [item(world, LIKE, f'3j{n}', {'subject': {'uri': world.copy}},
                                              f'2026-11-24T11:0{n}:00.000Z') for n in range(5)]
    taken.clear()
    assert notify.poll(world.identity.id) == 2   # at most MAX_PAGES a poll


def test_a_record_that_cannot_be_read_holds_nothing_up(world, monkeypatch):
    monkeypatch.setitem(notify.TAKES, LIKE, lambda *args: (_ for _ in ()).throw(notify.ServiceError('gone')))
    world.pds.notifications[world.me] = [item(world, LIKE, '3kl', {'subject': {'uri': world.copy}},
                                              '2026-11-24T10:00:00.000Z')]
    assert notify.poll(world.identity.id) == 1


def test_which_connections_may_read_and_are_polled(world, monkeypatch):
    oauth = SocialConnection(user_id=world.author.id, service='bluesky', variant='oauth',
                             remote_id='did:plc:oooooooooooooooooooooooo', remote_name='o',
                             sealed_tokens=seal_tokens({'did': 'did:plc:oooooooooooooooooooooooo', 'pds': PDS,
                                                        'access_token': 'a', 'expires_at': 9e9,
                                                        'scope': bluesky.SCOPE.replace(bluesky.NOTIFY_SCOPE, '')}))
    waiting = a_hosted(world, world.author, state=hosted.WAITING)
    reauth = SocialConnection(user_id=world.author.id, service='bluesky', variant='oauth', remote_id='did:plc:r',
                              remote_name='r', sealed_tokens=seal_tokens({}), state='reauth')
    db.session.add_all([oauth, reauth])
    db.session.commit()
    assert notify.pollable() == [world.identity.id, oauth.id] and waiting.id
    assert notify.poll(oauth.id) == 0   # granted before the scope was asked for: not polled
    assert notify.may_read({'kind': 'hosted'}) and notify.may_read({'scope': bluesky.SCOPE})
    assert not notify.may_read({}) and not notify.may_read({'scope': 'atproto'})


def test_polls_are_queued_two_minutes_apart(world, monkeypatch):
    queued, ran = [], []
    monkeypatch.setattr(notify.poll_task, 'delay', queued.append)
    assert notify.poll_due() == 1 and queued == [world.identity.id]
    assert notify.poll_due() == 0   # not due yet
    notify.app_pkg.redis_client.delete(f'{notify.DUE}{world.identity.id}')
    monkeypatch.setattr(world.app, 'debug', True)
    monkeypatch.setattr(notify, 'poll_task', ran.append)
    assert notify.poll_due() == 1 and ran == [world.identity.id]


def test_the_poll_task_logs_what_fails(world, monkeypatch):
    polled = []
    monkeypatch.setattr(notify, 'poll', polled.append)
    notify.poll_task(world.identity.id)
    monkeypatch.setattr(notify, 'poll', lambda connection_id: (_ for _ in ()).throw(notify.ServiceError('down')))
    notify.poll_task(world.identity.id)   # logged, not raised
    assert polled == [world.identity.id]


# --------------------------------------------------------------------------
# quotes out (§6): a microblog post that links a Bluesky post quotes it
# --------------------------------------------------------------------------

def session(world):
    return hosted.HostedSession(world.pds.tokens(world.me))


def test_a_post_linking_a_bluesky_post_quotes_it(world):
    quoted = world.pds.put(world.bob, POST, '3kq', note('quote me'))
    post = a_microblog(world, body='so true https://bsky.app/profile/bob.bsky.example/post/3kq')
    record = records.build_record(post, session(world))
    assert record['embed'] == {'$type': 'app.bsky.embed.record', 'record': {'uri': quoted, 'cid': 'bafyg'}}
    by_did = a_microblog(world, body=f'see https://bsky.app/profile/{world.bob}/post/3kq')
    assert records.quote_ref(by_did) == {'uri': quoted, 'cid': 'bafyg'}


def test_a_quote_with_pictures_or_a_card(world):
    ref = {'uri': 'at://did:plc:b/app.bsky.feed.post/3kq', 'cid': 'c'}
    images = {'$type': 'app.bsky.embed.images', 'images': []}
    assert records.quoting(images, ref) == {'$type': 'app.bsky.embed.recordWithMedia', 'media': images,
                                            'record': {'$type': 'app.bsky.embed.record', 'record': ref}}
    card = {'$type': 'app.bsky.embed.external', 'external': {}}
    assert records.quoting(card, ref) == {'$type': 'app.bsky.embed.record', 'record': ref}
    assert records.quoting(None, ref)['$type'] == 'app.bsky.embed.record'


@pytest.mark.parametrize('case', ['no-link', 'unresolved', 'unreadable', 'odd-answer', 'warning'])
def test_a_link_that_cannot_be_quoted_stays_a_link(world, case):
    world.pds.put(world.bob, POST, '3kq', note())
    body = {'no-link': 'just words', 'unresolved': 'https://bsky.app/profile/nobody.example/post/3kq',
            'unreadable': f'https://bsky.app/profile/{world.bob}/post/missing'}.get(
        case, f'https://bsky.app/profile/{world.bob}/post/3kq')
    if case == 'odd-answer':
        world.pds.fail['com.atproto.repo.getRecord'] = httpx.Response(200, json={'uri': 'at://elsewhere/x/y'})
    post = a_microblog(world, body=body, content_warning='spoilers' if case == 'warning' else None)
    record = records.build_record(post, session(world))
    assert record.get('embed', {}).get('$type') != 'app.bsky.embed.record'
    if case not in ('warning', 'odd-answer'):   # (the odd answer is given once)
        assert records.quote_ref(post) is None
