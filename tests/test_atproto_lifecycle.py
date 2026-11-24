"""ATProto P5: accounts coming and going, and posts members write on Bluesky (spec
2026-11-23-atproto-federation-design.md §7, §8; A7).

tests/atproto_fake.py plays the PLC directory and the PDS: the author's Bluesky account is hosted there, and bob is
an account they follow."""
import uuid

import httpx
import pytest

from app import db
from app.atproto import actors, identity, imports, ingest, jetstream, lifecycle
from app.connections import bluesky_hosted as hosted
from app.connections.tokens import REAUTH, seal_tokens
from app.constants import POST_TYPE_ARTICLE, POST_TYPE_GALLERY, POST_TYPE_IMAGE, POST_TYPE_LINK
from app.models import AtprotoRecord, Notification, Post, PostReply, SocialConnection, SocialCrosspost, User, \
    UserFollower
from tests.factories import make_site
from tests.shared_fixtures.atproto import a_hosted, a_microblog

POST = 'app.bsky.feed.post'


def note(text='hello from bluesky', **fields):
    return {'$type': POST, 'text': text, 'createdAt': '2026-11-20T10:00:00.000Z', **fields}


@pytest.fixture
def world(inbound, monkeypatch):
    make_site()   # what a post made here reads (make_post)
    monkeypatch.setattr(identity, 'LOCK', f'test:{uuid.uuid4().hex}:identity:')
    inbound.identity = a_hosted(inbound, inbound.author, is_identity=True, import_own=True)
    inbound.me = inbound.identity.remote_id
    return inbound


def told(world) -> list[str]:
    return [row.title for row in Notification.query.filter_by(user_id=world.author.id, subtype='bluesky_account')]


# --------------------------------------------------------------------------
# an account someone follows goes inactive, comes back, or is deleted
# --------------------------------------------------------------------------

def test_an_inactive_account_is_hidden_until_it_comes_back(world):
    root = world.pds.put(world.bob, POST, '3kroot', note('the root'))
    made = ingest.post(world.bob, '3kroot', world.pds.records[root])
    first = ingest.post(world.bob, '3kr1', note('one', reply={'parent': {'uri': root}}))
    second = ingest.post(world.bob, '3kr2', note('two', reply={'parent': {'uri': f'at://{world.bob}/{POST}/3kr1'}}))
    gone = ingest.post(world.bob, '3kgone', note('deleted by bob'))
    ingest.delete(world.bob, POST, '3kgone')
    bob = actors.actor_for(world.bob, fetch_new=False)
    community = made.community
    counts = lambda: (db.session.get(Post, made.id).reply_count, community.post_count, community.post_reply_count,  # noqa: E731
                      db.session.get(PostReply, first.id).child_count, bob.post_count, bob.post_reply_count)
    made.reply_count, community.post_count, community.post_reply_count = 2, 5, 5   # as a live site has them
    db.session.get(PostReply, first.id).child_count, bob.post_count, bob.post_reply_count = 1, 3, 3
    db.session.commit()
    db.session.expire_all()
    before = counts()
    ingest.handle({'did': world.bob, '$type': 'network.bsky.jetstream.subscribeEvents#account', 'active': False,
                   'status': 'takendown'})
    db.session.expire_all()
    assert all(db.session.get(model, item.id).deleted for model, item in ((Post, made), (PostReply, first),
                                                                          (PostReply, second)))
    assert counts() == (before[0] - 2, before[1] - 1, before[2] - 2, before[3] - 1, before[4] - 1, before[5] - 2)
    ingest.handle({'did': world.bob, 'kind': 'account', 'account': {'active': True}})
    db.session.expire_all()
    assert not any(db.session.get(model, item.id).deleted for model, item in ((Post, made), (PostReply, first),
                                                                              (PostReply, second)))
    assert counts() == before and db.session.get(Post, gone.id).deleted   # its own delete stands


def test_bans_made_while_an_account_was_inactive_stand(world):
    from app.models import CommunityBan
    kept = ingest.post(world.bob, '3kk', note('in the microblogs'))
    bob = actors.actor_for(world.bob, fetch_new=False)
    lifecycle.account(world.bob, False, 'deactivated')
    db.session.add(CommunityBan(user_id=bob.id, community_id=kept.community_id, banned_by=world.author.id))
    db.session.commit()
    lifecycle.account(world.bob, True, None)
    db.session.expire_all()
    assert db.session.get(Post, kept.id).deleted   # the community's ban stands
    CommunityBan.query.delete()
    bob.banned = True
    db.session.commit()
    lifecycle.account(world.bob, True, None)
    db.session.expire_all()
    assert db.session.get(Post, kept.id).deleted   # the site's ban stands too


def test_a_deleted_account_goes(world, monkeypatch):
    purged = []
    monkeypatch.setattr(lifecycle, 'purge_remote_user_then_delete', purged.append)
    bob = actors.actor_for(world.bob)
    lifecycle.account(world.bob, False, 'deleted')
    assert purged == [bob.id]
    bob.deleted = True
    db.session.commit()
    lifecycle.account(world.bob, False, 'deleted')   # gone already
    lifecycle.account('did:plc:nobodyknowsthisoneatall', False, 'takendown')
    lifecycle.account(world.bob, 'odd', None)
    assert purged == [bob.id]


def test_an_identity_event_reads_the_account_again(world):
    bob = actors.actor_for(world.bob)
    world.pds.accounts[world.bob]['handle'] = 'robert.bsky.example'
    lifecycle.identity(world.bob)
    assert bob.ap_id == 'robert.bsky.example'
    lifecycle.identity('did:plc:nobodyknowsthisoneatall')   # nobody here: nothing


# --------------------------------------------------------------------------
# a member's own account
# --------------------------------------------------------------------------

def test_a_hosted_account_suspended_and_restored(world, monkeypatch):
    caught = []
    monkeypatch.setattr(identity, 'caught_up', caught.append)
    lifecycle.account(world.me, False, 'takendown')
    assert world.identity.slot_state == hosted.SUSPENDED and identity.identity_of(world.author.id) is None
    lifecycle.account(world.me, False, 'deactivated')   # PieFed's own turning off, or a member's: nothing to tell
    lifecycle.account(world.me, True, None)
    assert world.identity.slot_state == hosted.ACTIVE and caught == [world.author.id]
    lifecycle.account(world.me, True, None)   # active and active: nothing
    world.identity.slot_state = hosted.DEACTIVATED
    db.session.commit()
    lifecycle.account(world.me, False, 'suspended')   # turned off here: stays as it is
    assert world.identity.slot_state == hosted.DEACTIVATED
    assert len(told(world)) == 2 and caught == [world.author.id]


def test_an_own_account_suspended_and_restored(world, monkeypatch):
    caught = []
    monkeypatch.setattr(identity, 'caught_up', caught.append)
    own = SocialConnection(user_id=world.author.id, service='bluesky', variant='oauth',
                           remote_id='did:plc:oooooooooooooooooooooooo', remote_name='me.bsky.example',
                           sealed_tokens=seal_tokens({}))
    db.session.add(own)
    db.session.commit()
    lifecycle.account(own.remote_id, False, 'suspended')
    assert (own.state, own.last_error) == (REAUTH, 'Bluesky: account suspended')
    lifecycle.account(own.remote_id, True, None)
    assert (own.state, own.last_error) == ('ok', None) and caught == [world.author.id]
    own.state, own.last_error = REAUTH, 'HTTP 401'   # its tokens ended: a reconnect, not Bluesky, mends that
    db.session.commit()
    lifecycle.account(own.remote_id, True, None)
    assert own.state == REAUTH


def test_an_own_account_deleted_on_bluesky(world):
    own = SocialConnection(user_id=world.author.id, service='bluesky', variant='oauth',
                           remote_id='did:plc:oooooooooooooooooooooooo', remote_name='me.bsky.example',
                           sealed_tokens=seal_tokens({}))
    db.session.add(own)
    db.session.commit()
    lifecycle.account(own.remote_id, False, 'deleted')
    assert SocialConnection.query.filter_by(remote_id=own.remote_id).first() is None
    world.identity.slot_state = hosted.DELETING
    db.session.commit()
    lifecycle.account(world.me, False, 'deleted')   # PieFed's own deletion: its daily task finishes it
    assert db.session.get(SocialConnection, world.identity.id) is not None
    world.identity.slot_state = hosted.ACTIVE
    db.session.commit()
    identity_id = world.identity.id
    lifecycle.account(world.me, False, 'deleted')
    assert db.session.get(SocialConnection, identity_id) is None and len(told(world)) == 2


def test_a_hosted_account_that_moved_away(world):
    world.pds.accounts[world.me]['handle'] = 'me.elsewhere.example'
    world.pds.router.route(path='/.well-known/atproto-did').respond(404)
    world.pds.servers[world.me] = 'https://elsewhere.example'
    ingest.handle({'did': world.me, 'kind': 'identity'})
    assert world.identity.slot_state == hosted.MOVED and world.identity.remote_name == 'me.elsewhere.example'
    assert identity.identity_of(world.author.id) is None and len(told(world)) == 1
    lifecycle.identity(world.me)   # moved already: told once
    assert len(told(world)) == 1


def test_a_handle_that_does_not_resolve_back_is_not_taken(world):
    world.pds.accounts[world.me]['handle'] = 'spoofed.example'
    world.pds.fail['com.atproto.identity.resolveHandle'] = httpx.Response(400, json={'error': 'InvalidRequest'})
    name = world.identity.remote_name
    lifecycle.identity(world.me)
    assert world.identity.remote_name == name and world.identity.slot_state == hosted.ACTIVE


def test_a_moved_account_is_connected_again_as_the_identity(world, monkeypatch):
    from app.connections import Account, bluesky
    world.identity.slot_state = hosted.MOVED
    db.session.commit()
    page = world.client.get('/user/settings/connections').data.decode()
    assert 'Moved to another server' in page and 'Connect it again' in page
    monkeypatch.setattr(bluesky.SERVICE, 'authorize_url',
                        lambda variant, state, redirect_uri, pending, hint='': f'https://x.example/a?state={state}')
    monkeypatch.setattr(bluesky.SERVICE, 'finish', lambda variant, args, redirect_uri, pending: [Account(
        remote_id=world.me, remote_name='me.elsewhere.example', variant='oauth', identity=True,
        tokens={'did': world.me})])
    monkeypatch.setattr(identity, 'caught_up', lambda user_id: None)
    response = world.client.post('/user/settings/connections/bluesky/connect',
                                 data={'csrf_token': world.token, 'identity': '1', 'hint': world.me})
    state = response.location.split('state=')[1]
    world.client.get(f'/user/settings/connections/bluesky/callback?state={state}&code=c')
    db.session.expire_all()
    connection = db.session.get(SocialConnection, world.identity.id)
    assert (connection.variant, connection.slot_state, connection.is_identity) == ('oauth', None, True)


def test_badges_and_the_import_switch(world):
    world.identity.slot_state = hosted.SUSPENDED
    db.session.commit()
    page = world.client.get('/user/settings/connections').data.decode()
    assert 'Suspended by Bluesky' in page and 'Stop bringing in posts I write on Bluesky' in page
    world.client.post(f'/user/settings/connections/{world.identity.id}/import-own',
                      data={'csrf_token': world.token, 'import_own': '0'})
    db.session.expire_all()
    assert db.session.get(SocialConnection, world.identity.id).import_own is False
    other = SocialConnection(user_id=world.author.id, service='fake', remote_id='x', remote_name='x',
                             sealed_tokens=seal_tokens({}))
    db.session.add(other)
    db.session.commit()
    assert world.client.post(f'/user/settings/connections/{other.id}/import-own',
                             data={'csrf_token': world.token, 'import_own': '1'}).status_code == 404


# --------------------------------------------------------------------------
# posts members write on Bluesky (A7)
# --------------------------------------------------------------------------

def test_a_post_written_on_bluesky_becomes_the_members(world):
    record = note('written in a Bluesky app, long enough to be a title', langs=['en'])
    uri = world.pds.put(world.me, POST, '3kapp', record)
    made = ingest.handle({'did': world.me, 'collection': POST, 'operation': 'create', 'rkey': '3kapp',
                          'record': record}) or Post.query.filter_by(user_id=world.author.id).one()
    assert made.user_id == world.author.id and made.community_id == world.microblogs.id and not made.local_only
    assert made.body.startswith('written in a Bluesky app') and made.language.code == 'en'
    copy = SocialCrosspost.query.filter_by(post_id=made.id).one()
    assert (copy.remote_id, copy.state, copy.progress) == (uri, 'sent', {'imported': True})
    assert ingest.post(world.me, '3kapp', record) is None   # once
    assert User.query.filter_by(ap_profile_id=actors.profile_url(world.me)).first() is None   # never an actor
    # deleted there: deleted here, and nothing is sent back
    ingest.handle({'did': world.me, 'collection': POST, 'operation': 'delete', 'rkey': '3kapp'})
    db.session.expire_all()
    assert db.session.get(Post, made.id).deleted and copy.state == 'deleted'
    assert 'com.atproto.repo.deleteRecord' not in world.pds.calls
    assert imports.deleted(world.me, '3kapp')   # again: already deleted here


def test_what_is_not_brought_in(world):
    assert imports.imported(world.me, '3kr', note('a reply', reply={'parent': {'uri': 'at://x/y/z'}})) is None
    own = a_microblog(world)
    db.session.add(SocialCrosspost(post_id=own.id, connection_id=world.identity.id, service='bluesky',
                                   state='sending', progress={'rkey': '3kmaking'}))
    db.session.add(AtprotoRecord(uri=f'at://{world.me}/{POST}/3kreply', did=world.me, collection=POST,
                                 rkey='3kreply', direction='out', connection_id=world.identity.id))
    db.session.commit()
    assert imports.imported(world.me, '3kmaking', note()) is None   # a copy being made: its echo
    assert imports.imported(world.me, '3kreply', note()) is None    # an identity's reply
    world.identity.import_own = False
    db.session.commit()
    assert imports.imported(world.me, '3kno', note()) is None
    world.identity.import_own = True
    world.author.banned = True
    db.session.commit()
    assert imports.imported(world.me, '3kban', note()) is None
    assert not imports.deleted(world.me, '3knothing') and not imports.deleted(world.me, '3kmaking')


def test_a_post_make_post_refuses_is_not_brought_in(world, monkeypatch):
    monkeypatch.setattr(imports, 'make_post', lambda *args, **kwargs: (_ for _ in ()).throw(Exception('limited')))
    assert imports.imported(world.me, '3kx', note()) is None


def test_no_microblogs_community_brings_nothing(world, monkeypatch):
    monkeypatch.setattr(imports, 'find_microblogging_community', lambda: None)
    assert imports.imported(world.me, '3kx', note()) is None


def test_the_shape_of_what_is_brought_in(world):
    image = lambda link: {'image': {'ref': {'$link': link}}, 'alt': f'alt {link}'}  # noqa: E731
    kind, fields, album = imports.shape(world.me, note(embed={'$type': 'app.bsky.embed.images',
                                                              'images': [image('a'), image('b'), 'odd']}))
    assert kind == POST_TYPE_GALLERY and [picture['alt_text'] for picture in album] == ['alt a', 'alt b']
    kind, fields, album = imports.shape(world.me, note(labels={'values': [{'val': 'porn'}]}, embed={
        '$type': 'app.bsky.embed.recordWithMedia', 'media': {'$type': 'app.bsky.embed.images',
                                                             'images': [image('c')]}}))
    assert kind == POST_TYPE_IMAGE and fields['image_alt_text'] == 'alt c' and 'cid=c' in fields['url']
    assert fields['nsfw'] and album is None
    kind, fields, _album = imports.shape(world.me, note(embed={'$type': 'app.bsky.embed.external',
                                                               'external': {'uri': 'https://news.example/a'}}))
    assert kind == POST_TYPE_LINK and fields['url'] == 'https://news.example/a'
    for odd in ({'$type': 'app.bsky.embed.external', 'external': {'uri': 'javascript:x'}},
                {'$type': 'app.bsky.embed.recordWithMedia'}, 'odd'):
        assert imports.shape(world.me, note(embed=odd))[0] == POST_TYPE_ARTICLE
    assert imports.shape(world.me, note(langs=['zz-unknown']))[1]['language_id'] is None
    assert imports.shape(world.me, note(text=None))[1]['body'] == ''


@pytest.mark.usefixtures('private_static_tree')   # its picture is stored, in a scratch tree
def test_a_post_with_a_picture_is_brought_in(world):
    record = note('a picture from my phone', embed={'$type': 'app.bsky.embed.images', 'images': [
        {'image': {'ref': {'$link': 'bafkpic'}}, 'alt': 'a sunset'}]})
    made = imports.imported(world.me, '3kpic', record)
    assert made is not None and made.url and 'bafkpic' in made.url
    assert imports.imported(world.me, '3kmore', note('and another')) is not None   # the site read once already


# --------------------------------------------------------------------------
# who the stream follows, and a blocked network
# --------------------------------------------------------------------------

def test_the_stream_follows_accounts_that_bring_posts_in_and_none_while_the_network_is_blocked(world):
    bob = actors.actor_for(world.bob)
    db.session.add(UserFollower(local_user_id=world.author.id, remote_user_id=bob.id, is_inward=False,
                                is_accepted=True))
    own = SocialConnection(user_id=world.author.id, service='bluesky', variant='oauth', import_own=True,
                           remote_id='did:plc:oooooooooooooooooooooooo', remote_name='o', sealed_tokens='x')
    db.session.add(own)
    db.session.commit()
    assert jetstream.wanted_dids() == sorted([world.bob, world.me, own.remote_id])
    actors.instance().defederated = True
    db.session.commit()
    assert jetstream.wanted_dids() == sorted([world.me, own.remote_id])


def test_a_blocked_network_takes_no_identity_records(world):
    actors.instance().defederated = True
    db.session.commit()
    assert identity.sync(world.author.id, 'follow', actors.actor_for(world.bob, fetch_new=True).id) is None
