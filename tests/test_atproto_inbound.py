"""ATProto P3: Bluesky into PieFed (spec 2026-11-23-atproto-federation-design.md §7; A4, A6).

tests/atproto_fake.py plays the PLC directory and the PDS: bob is an account there (the `inbound` fixture).
"""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import db
from app.activitypub.util import find_microblogging_community
from app.atproto import actors, ingest
from app.connections import Permanent
from app.connections.tokens import seal_tokens
from app.models import AtprotoRecord, Instance, Post, PostBoost, PostReply, SocialConnection, SocialCrosspost, User, \
    UserFollower

POST = 'app.bsky.feed.post'


def note(text='hello from bluesky', **fields):
    return {'$type': POST, 'text': text, 'createdAt': '2026-11-20T10:00:00.000Z', **fields}


def bob(world) -> User:
    return actors.actor_for(world.bob)


# --------------------------------------------------------------------------
# names and addresses
# --------------------------------------------------------------------------

def test_addresses_of_actors_and_posts():
    did = 'did:plc:abcdefghijklmnopqrstuvwx'
    assert actors.profile_url(did) == f'https://bsky.app/profile/{did}'
    uri = f'at://{did}/app.bsky.feed.post/3kabc'
    assert actors.post_url(uri) == f'https://bsky.app/profile/{did}/post/3kabc'
    assert actors.at_uri_of(actors.post_url(uri)) == uri
    assert actors.at_uri_of('https://bsky.app/profile/x') is None and actors.at_uri_of(None) is None


@pytest.mark.parametrize('name, expected', [
    ('@Alice.bsky.social', 'alice.bsky.social'), ('did:plc:abcdefghijklmnopqrstuvwx', 'did:plc:abcdefghijklmnopqrstuvwx'),
    ('did:web:example.com', 'did:web:example.com'), ('alice@mastodon.example', None), ('alice', None),
    ('https://bsky.app/profile/x', None), ('did:plc:short', None), ('', None), (None, None)])
def test_what_looks_like_an_atproto_name(name, expected):
    assert actors.looks_atproto(name) == expected


def test_is_atproto_and_did_of():
    user = User(ap_profile_id='https://bsky.app/profile/did:plc:abcdefghijklmnopqrstuvwx')
    assert actors.is_atproto(user) and actors.did_of(user) == 'did:plc:abcdefghijklmnopqrstuvwx'
    assert not actors.is_atproto(User(ap_profile_id='https://mastodon.example/users/a'))
    assert not actors.is_atproto(None) and actors.did_of(User(ap_profile_id=None)) is None


def test_blob_addresses():
    blob = {'$type': 'blob', 'ref': {'$link': 'bafk1'}}
    assert actors.blob_url('https://pds.example', 'did:plc:x', blob) == \
        'https://pds.example/xrpc/com.atproto.sync.getBlob?did=did%3Aplc%3Ax&cid=bafk1'
    for bad in (None, 'x', {}, {'ref': {}}, {'ref': {'$link': ''}}):
        assert actors.blob_url('https://pds.example', 'did:plc:x', bad) is None


def test_self_labels():
    assert actors.self_labels({'labels': {'values': [{'val': 'porn'}, {'val': 3}, 'x']}}) == {'porn'}
    assert actors.self_labels(None) == set() and actors.self_labels({'labels': []}) == set()


def test_the_instance_is_made_once_and_kept_atproto(app, db_session):
    first = actors.instance()
    assert (first.domain, first.software) == ('bsky.app', 'atproto') and actors.instance().id == first.id
    first.software = 'mastodon'
    db.session.commit()
    assert actors.instance().software == 'atproto'


# --------------------------------------------------------------------------
# actors
# --------------------------------------------------------------------------

def test_an_actor_is_made_from_its_did_and_profile(inbound):
    user = bob(inbound)
    assert (user.user_name, user.ap_id, user.title) == ('bob.bsky.example', 'bob.bsky.example', 'Bob')
    assert user.ap_profile_id == user.ap_public_url == actors.profile_url(inbound.bob)
    assert user.instance.software == 'atproto' and user.ap_inbox_url is None and not user.is_local()
    assert 'post' in user.about_html and user.avatar.source_url.endswith('cid=bafkavatar') and user.cover is None
    assert user.requires_login is False and user.ap_fetched_at is not None
    assert actors.actor_for(inbound.bob).id == user.id   # stored: not made twice


def test_a_refresh_follows_the_profile(inbound):
    user = bob(inbound)
    avatar = user.avatar
    inbound.pds.put(inbound.bob, 'app.bsky.actor.profile', 'self', {
        'displayName': '', 'avatar': {'ref': {'$link': 'bafkavatar'}}, 'banner': {'ref': {'$link': 'bafkbanner'}},
        'labels': {'values': [{'val': '!no-unauthenticated'}]}})
    inbound.pds.accounts[inbound.bob]['handle'] = 'bob.example'
    actors.refresh(user)
    assert user.avatar is avatar and user.cover.source_url.endswith('cid=bafkbanner')   # an unchanged avatar is kept
    assert (user.ap_id, user.title, user.about_html, user.requires_login) == ('bob.example', None, '', True)
    del inbound.pds.records[f'at://{inbound.bob}/app.bsky.actor.profile/self']
    actors.refresh(user)
    assert user.avatar is None and user.cover is None


def test_an_actor_without_a_handle_is_named_by_its_did(inbound):
    del inbound.pds.records[f'at://{inbound.bob}/app.bsky.actor.profile/self']
    inbound.pds.accounts[inbound.bob]['handle'] = ''
    assert bob(inbound).user_name == inbound.bob


def test_an_actor_that_cannot_be_read_is_not_made(inbound):
    assert actors.actor_for('did:plc:aaaaaaaaaaaaaaaaaaaaaaaa') is None
    assert actors.actor_for(inbound.bob, fetch_new=False) is None


def test_an_actor_whose_document_names_no_pds_is_not_made(inbound, monkeypatch):
    monkeypatch.setattr(actors, 'pds_of', lambda document: (_ for _ in ()).throw(Permanent('no pds')))
    assert actors.actor_for(inbound.bob) is None and User.query.filter_by(ap_domain='bsky.app').count() == 0


def test_a_record_that_is_not_there_or_not_json(inbound):
    assert actors.read_record('https://pds.example', inbound.bob, POST, 'none') is None
    inbound.pds.fail['com.atproto.repo.getRecord'] = httpx.Response(200, text='not json')
    assert actors.read_record('https://pds.example', inbound.bob, POST, 'x') is None
    inbound.pds.fail['com.atproto.repo.getRecord'] = httpx.Response(200, json={'value': []})
    assert actors.read_record('https://pds.example', inbound.bob, POST, 'x') is None


def test_finding_by_handle_or_did(inbound):
    assert actors.find('alice@mastodon.example', True) is None
    assert actors.find('@bob.bsky.example', False) is None   # not stored, not to be looked up
    user = actors.find('@bob.bsky.example', True)
    assert user is not None and actors.find('BOB.bsky.example', False).id == user.id
    assert actors.find(inbound.bob, False).id == user.id
    assert actors.find('nobody.bsky.example', True) is None


def test_the_refresh_task(inbound, monkeypatch):
    user = bob(inbound)
    user.title = 'old'
    db.session.commit()
    actors.refresh_task(user.id)
    db.session.expire_all()   # the task wrote through its own session
    assert db.session.get(User, user.id).title == 'Bob'
    inbound.pds.fail['com.atproto.repo.getRecord'] = httpx.Response(200, json={})
    monkeypatch.setattr(actors, 'did_document', lambda did: (_ for _ in ()).throw(Permanent('gone')))
    actors.refresh_task(user.id)   # logged, not raised
    actors.refresh_task(inbound.author.id)   # not an ATProto actor: nothing


def test_an_atproto_actor_is_never_refreshed_over_activitypub(inbound, monkeypatch):
    from app.activitypub import util
    user = bob(inbound)
    ran = []
    monkeypatch.setattr(actors, 'refresh_task', ran.append)
    monkeypatch.setattr(util, 'get_request', lambda *args, **kwargs: pytest.fail('fetched over ActivityPub'))
    util.refresh_user_profile_task(user.id)
    assert ran == [user.id]


# --------------------------------------------------------------------------
# looking up and following (A6)
# --------------------------------------------------------------------------

def test_a_profile_page_by_handle(inbound):
    response = inbound.client.get('/u/bob.bsky.example')
    assert response.status_code == 200 and 'Bob' in response.data.decode()
    assert inbound.client.get(f'/u/{inbound.bob}').status_code == 200


def test_a_logged_out_reader_sees_only_known_actors(inbound, app):
    from app.activitypub.routes import resolve_remote_handle
    with app.test_request_context('/u/bob.bsky.example'):   # nobody signed in
        assert resolve_remote_handle('bob.bsky.example') is None   # never looked up for a stranger
        assert inbound.pds.calls.count('com.atproto.identity.resolveHandle') == 0
        user = bob(inbound)
        assert resolve_remote_handle('bob.bsky.example').id == user.id


def test_an_activitypub_request_never_looks_one_up(inbound):
    from app.activitypub.routes import resolve_remote_handle
    with inbound.app.test_request_context('/u/bob.bsky.example', headers={'Accept': 'application/activity+json'}):
        assert resolve_remote_handle('bob.bsky.example') is None


def test_a_lookup_that_fails_is_a_404(inbound, monkeypatch):
    monkeypatch.setattr(actors, 'find', lambda name, fetch_new: (_ for _ in ()).throw(RuntimeError('boom')))
    assert inbound.client.get('/u/bob.bsky.example').status_code == 404


def test_following_is_accepted_at_once_and_reads_the_latest_posts(inbound, monkeypatch):
    from app.shared.user import follow_user_as, unfollow_user_as
    for number in range(3):
        inbound.pds.put(inbound.bob, POST, f'3k{number}', note(f'post {number}'))
    inbound.pds.put(inbound.bob, POST, '3kreply', note('a reply', reply={
        'root': {'uri': 'at://x/app.bsky.feed.post/y'}, 'parent': {'uri': 'at://x/app.bsky.feed.post/y'}}))
    monkeypatch.setattr('app.shared.user.task_selector', lambda *args, **kwargs: pytest.fail('sent a Follow'))
    user = bob(inbound)
    follow_user_as(inbound.author, user)
    row = UserFollower.query.filter_by(local_user_id=inbound.author.id, remote_user_id=user.id).one()
    assert row.is_accepted is True and inbound.author.num_following == 1 and user.num_followers == 1
    assert sorted(post.body for post in Post.query.filter_by(user_id=user.id)) == ['post 0', 'post 1', 'post 2']
    unfollow_user_as(inbound.author, user)
    assert UserFollower.query.count() == 0


def test_following_and_unfollowing_from_the_profile_page(inbound):
    user = bob(inbound)
    inbound.pds.put(inbound.bob, POST, '3kp', note('from before'))
    for path in ('/u/bob.bsky.example/follow', f'/u/{inbound.bob}/follow'):
        response = inbound.client.post(path, data={'csrf_token': inbound.token}, headers={'HX-Request': 'true'})
        assert response.status_code == 200 and 'Done' in response.data.decode()
    assert UserFollower.query.filter_by(remote_user_id=user.id, is_accepted=True).count() == 1
    assert Post.query.filter_by(user_id=user.id).one().body == 'from before'
    inbound.client.post('/u/bob.bsky.example/unfollow', data={'csrf_token': inbound.token})
    assert UserFollower.query.count() == 0
    assert inbound.client.post('/u/nobody.bsky.example/follow', data={'csrf_token': inbound.token}).status_code == 404


def test_the_backfill_task(inbound, monkeypatch):
    user = bob(inbound)
    actors.backfill_task(inbound.author.id)   # not an ATProto actor: nothing
    inbound.pds.fail['com.atproto.repo.listRecords'] = httpx.Response(502)
    actors.backfill_task(user.id)   # logged, not raised
    ran = []
    monkeypatch.setattr(actors.backfill_task, 'delay', lambda *args: ran.append(args))
    actors.followed(inbound.author.id, user.id)
    monkeypatch.setattr(inbound.app, 'debug', True)
    monkeypatch.setattr(actors, 'backfill_task', lambda *args: ran.append(args))
    actors.followed(None, user.id, 5)
    assert ran == [(user.id, actors.FOLLOW_BACKFILL), (user.id, 5)]


def test_the_health_check_leaves_the_atproto_instance_alone(inbound, http_mock):
    from app.shared.tasks.maintenance import monitor_healthy_instances
    actors.instance()
    for row in Instance.query.filter(Instance.software != 'atproto').all():
        row.dormant = True   # only the ATProto instance left to look at: a fetch of it would fail the test
    db.session.commit()
    monitor_healthy_instances()


# --------------------------------------------------------------------------
# a record's shape
# --------------------------------------------------------------------------

def test_text_and_facets_as_html():
    text = 'héllo https://x.example/a and @bob.bsky.example\nbye <b>'
    data = text.encode()
    link = data.index(b'https')
    mention = data.index(b'@bob')
    facets = [{'index': {'byteStart': link, 'byteEnd': link + 19},
               'features': [{'$type': 'app.bsky.richtext.facet#link', 'uri': 'https://x.example/a'}]},
              {'index': {'byteStart': mention, 'byteEnd': mention + 17},
               'features': [{'$type': 'app.bsky.richtext.facet#mention', 'did': 'did:plc:b'}]},
              {'index': {'byteStart': link + 2, 'byteEnd': link + 5},   # overlaps the link: left as text
               'features': [{'$type': 'app.bsky.richtext.facet#link', 'uri': 'https://y.example'}]},
              {'index': {'byteStart': 0, 'byteEnd': 999}, 'features': [{'$type': 'app.bsky.richtext.facet#link',
                                                                        'uri': 'https://z.example'}]},
              {'index': {'byteStart': 0, 'byteEnd': 2}, 'features': [{'$type': 'app.bsky.richtext.facet#link',
                                                                      'uri': 'javascript:alert(1)'}]},
              {'index': {'byteStart': 0, 'byteEnd': 2}, 'features': [{'$type': 'app.bsky.richtext.facet#tag'}]},
              {'index': 'x', 'features': []}, {'features': ['x']}, 'nonsense']
    assert ingest.html_of(text, facets) == (
        '<p>héllo <a href="https://x.example/a" rel="nofollow ugc">https://x.example/a</a> and '
        '<a href="https://bsky.app/profile/did:plc:b" rel="nofollow ugc">@bob.bsky.example</a><br>bye &lt;b&gt;</p>')
    assert ingest.html_of(None, None) == '<p></p>'


def test_tags_and_times():
    facets = [{'features': [{'$type': 'app.bsky.richtext.facet#tag', 'tag': 'piefed'}, {'$type': 'x'}]}, 'odd']
    assert ingest.tags_of(facets) == ['piefed'] and ingest.tags_of(None) == []
    assert ingest.published({'createdAt': '2026-11-20T10:00:00.000Z'}) == '2026-11-20T10:00:00Z'
    future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    for record in ({'createdAt': future}, {'createdAt': 'nonsense'}, {}, {'createdAt': '2026-11-20T10:00:00'}):
        assert abs(datetime.fromisoformat(ingest.published(record).replace('Z', '+00:00')) -
                   datetime.now(timezone.utc)) < timedelta(seconds=5)


def test_attachments_and_embedded_links():
    image = {'image': {'ref': {'$link': 'bafkimg'}}, 'alt': 'a cat'}
    images = {'$type': 'app.bsky.embed.images', 'images': [image, 'odd', {'image': {}}]}
    found = ingest.attachments_of(images, 'https://pds.example', 'did:plc:b')
    assert [(item['type'], item['name']) for item in found] == [('Image', 'a cat')]
    assert ingest.attachments_of({'$type': 'app.bsky.embed.recordWithMedia', 'media': images}, 'p', 'd')[0]['type'] == 'Image'
    video = {'$type': 'app.bsky.embed.video', 'video': {'ref': {'$link': 'bafkvid'}}}
    assert ingest.attachments_of(video, 'p', 'd')[0]['mediaType'] == 'video/mp4'
    assert ingest.attachments_of({'$type': 'app.bsky.embed.video'}, 'p', 'd') == []
    assert ingest.attachments_of(None, 'p', 'd') == [] and ingest.attachments_of({'$type': 'x'}, 'p', 'd') == []
    quote = {'$type': 'app.bsky.embed.record', 'record': {'uri': 'at://did:plc:q/app.bsky.feed.post/3kq'}}
    assert 'https://bsky.app/profile/did:plc:q/post/3kq' in ingest.links_of(quote)
    nested = {'$type': 'app.bsky.embed.recordWithMedia', 'record': {'record': quote['record']}}
    assert 'post/3kq' in ingest.links_of(nested)
    external = {'$type': 'app.bsky.embed.external', 'external': {'uri': 'https://news.example/a'}}
    assert 'https://news.example/a' in ingest.links_of(external)
    for nothing in (None, {'$type': 'app.bsky.embed.external', 'external': {'uri': 'javascript:x'}},
                    {'$type': 'app.bsky.embed.record', 'record': {'uri': 'at://did:plc:q/app.bsky.graph.list/l'}}):
        assert ingest.links_of(nothing) == ''


# --------------------------------------------------------------------------
# records in
# --------------------------------------------------------------------------

def test_a_post_goes_to_the_microblogs_with_its_labels(inbound):
    user = bob(inbound)
    made = ingest.post(inbound.bob, '3kone', note('a #tagged post', langs=['en'], labels={'values': [
        {'val': 'sexual'}, {'val': 'graphic-media'}, {'val': '!no-unauthenticated'}]}, facets=[
        {'index': {'byteStart': 2, 'byteEnd': 9}, 'features': [{'$type': 'app.bsky.richtext.facet#tag',
                                                                 'tag': 'tagged'}]}]), cid='bafyc')
    assert isinstance(made, Post) and made.user_id == user.id
    assert made.community_id == find_microblogging_community().id
    assert made.ap_id == f'https://bsky.app/profile/{inbound.bob}/post/3kone'
    assert (made.nsfw, made.nsfl, made.requires_login) == (True, True, True)
    row = AtprotoRecord.query.filter_by(uri=f'at://{inbound.bob}/{POST}/3kone').one()
    assert (row.direction, row.post_id, row.cid, row.collection) == ('in', made.id, 'bafyc', POST)
    assert ingest.post(inbound.bob, '3kone', note()).id == made.id   # delivered twice: made once
    assert Post.query.filter_by(user_id=user.id).count() == 1


def test_a_post_by_a_login_only_account(inbound):
    user = bob(inbound)
    user.requires_login = True
    db.session.commit()
    assert ingest.post(inbound.bob, '3kq', note()).requires_login is True


def test_our_own_echo_is_skipped(inbound):
    uri = f'at://{inbound.bob}/{POST}/3kcopy'
    connection = SocialConnection(user_id=inbound.author.id, service='bluesky', variant='oauth', remote_id=inbound.bob,
                                  remote_name='x', sealed_tokens=seal_tokens({}))
    db.session.add(connection)
    db.session.commit()
    post = Post.query.first() or ingest.post(inbound.bob, '3kother', note())
    db.session.add(SocialCrosspost(post_id=post.id, connection_id=connection.id, service='bluesky', state='sent',
                                   remote_id=uri))
    db.session.add(AtprotoRecord(uri=f'at://{inbound.bob}/{POST}/3kout', did=inbound.bob, collection=POST,
                                 rkey='3kout', direction='out'))
    db.session.commit()
    assert ingest.post(inbound.bob, '3kcopy', note()) is None
    assert ingest.post(inbound.bob, '3kout', note()) is None
    assert ingest.post(inbound.bob, '3kodd', 'not a record') is None


def test_a_blocked_network_or_actor_brings_nothing(inbound):
    user = bob(inbound)
    user.banned = True
    db.session.commit()
    assert ingest.post(inbound.bob, '3kb', note()) is None
    user.banned = False
    actors.instance().defederated = True
    db.session.commit()
    assert ingest.post(inbound.bob, '3kb', note()) is None
    assert ingest.post('did:plc:aaaaaaaaaaaaaaaaaaaaaaaa', '3kb', note()) is None   # no such actor


def test_a_post_create_post_refuses_is_not_kept(inbound, monkeypatch):
    bob(inbound)
    monkeypatch.setattr(ingest, 'create_post', lambda *args: None)
    assert ingest.post(inbound.bob, '3kr', note()) is None and AtprotoRecord.query.count() == 0


def test_a_reply_goes_under_its_parent_fetched_toward_the_root(inbound):
    carol = inbound.pds.account('carol.bsky.example')
    root = inbound.pds.put(carol, POST, '3kroot', note('the root'))
    middle = inbound.pds.put(inbound.bob, POST, '3kmid', note('a reply', reply={'root': {'uri': root},
                                                                               'parent': {'uri': root}}))
    reply = ingest.post(inbound.bob, '3kleaf', note('a reply to the reply', reply={'root': {'uri': root},
                                                                                 'parent': {'uri': middle}}))
    assert isinstance(reply, PostReply) and reply.body.startswith('a reply to the reply')
    parent = PostReply.query.filter_by(ap_id=actors.post_url(middle)).one()
    assert reply.parent_id == parent.id and parent.post.ap_id == actors.post_url(root)
    assert AtprotoRecord.query.filter_by(uri=f'at://{inbound.bob}/{POST}/3kleaf').one().post_reply_id == reply.id


def test_a_reply_whose_parent_cannot_be_had(inbound):
    bob(inbound)
    missing = note('lost', reply={'parent': {'uri': f'at://{inbound.bob}/{POST}/gone'}})
    assert ingest.post(inbound.bob, '3kl', missing) is None
    assert ingest.post(inbound.bob, '3kl', note('odd', reply={'parent': 'x'})) is None
    assert ingest.post(inbound.bob, '3kl', note('far', reply={'parent': {'uri': 'at://x/app.bsky.feed.like/y'}})) is None
    assert ingest.fetched(f'at://{inbound.bob}/{POST}/gone', 0) is None
    assert ingest.fetched('at://did:plc:aaaaaaaaaaaaaaaaaaaaaaaa/app.bsky.feed.post/x', 3) is None   # no DID document


def test_a_repost_is_a_boost(inbound):
    carol = inbound.pds.account('carol.bsky.example')
    target = inbound.pds.put(carol, POST, '3kt', note('worth sharing'))
    boosted = ingest.repost(inbound.bob, '3kre', {'subject': {'uri': target}})
    user = actors.actor_for(inbound.bob, fetch_new=False)
    assert isinstance(boosted, Post) and PostBoost.query.filter_by(user_id=user.id, post_id=boosted.id).count() == 1
    assert ingest.repost(inbound.bob, '3kre', {'subject': {'uri': target}}) is None   # again: once
    assert ingest.repost(inbound.bob, '3kx', {'subject': 'x'}) is None
    assert ingest.repost(inbound.bob, '3kx', {'subject': {'uri': f'at://{carol}/{POST}/none'}}) is None
    user.banned = True
    db.session.commit()
    assert ingest.repost(inbound.bob, '3ky', {'subject': {'uri': target}}) is None


def test_deletes(inbound):
    carol = inbound.pds.account('carol.bsky.example')
    root = inbound.pds.put(carol, POST, '3kroot', note('the root'))
    made = ingest.post(inbound.bob, '3kp', note())
    reply = ingest.post(inbound.bob, '3kr', note('re', reply={'parent': {'uri': root}}))
    ingest.repost(inbound.bob, '3kb', {'subject': {'uri': root}})
    assert ingest.delete(inbound.bob, POST, '3kp') and db.session.get(Post, made.id).deleted
    assert ingest.delete(inbound.bob, POST, '3kr') and db.session.get(PostReply, reply.id).deleted
    assert ingest.delete(inbound.bob, ingest.REPOST, '3kb') and PostBoost.query.count() == 0
    assert not ingest.delete(inbound.bob, POST, '3kp')   # gone already
    assert not ingest.delete('did:plc:aaaaaaaaaaaaaaaaaaaaaaaa', POST, 'x')


def test_a_delete_of_what_is_gone_here_already(inbound):
    made = ingest.post(inbound.bob, '3kp', note())
    made.deleted = True
    db.session.commit()
    row = AtprotoRecord.query.one()
    row.post_id = None
    db.session.add(AtprotoRecord(uri=f'at://{inbound.bob}/{ingest.REPOST}/3kz', did=inbound.bob,
                                 collection=ingest.REPOST, rkey='3kz', direction='in', post_id=None))
    db.session.commit()
    assert ingest.delete(inbound.bob, POST, '3kp') and ingest.delete(inbound.bob, ingest.REPOST, '3kz')


def test_backfill_takes_posts_that_answer_nobody(inbound):
    inbound.pds.put(inbound.bob, POST, '3k1', note('one'))
    inbound.pds.put(inbound.bob, POST, '3k2', note('two', reply={'parent': {'uri': 'at://x/y/z'}}))
    inbound.pds.fail['com.atproto.repo.listRecords'] = httpx.Response(200, json={'records': [
        {'uri': f'at://{inbound.bob}/{POST}/3k1', 'value': note('one')}, 'odd', {'uri': 3, 'value': {}},
        {'uri': f'at://{inbound.bob}/{POST}/3k2', 'value': note('two', reply={'parent': {'uri': 'at://x/y/z'}})}]})
    assert ingest.backfill(bob(inbound), 20) == 1


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------

def test_events(inbound, monkeypatch):
    user = bob(inbound)
    commit = '#commit'
    ingest.handle({'$type': commit, 'did': inbound.bob, 'collection': POST, 'operation': 'create', 'rkey': '3ke',
                   'record': note('by event'), 'cid': 'c'})
    assert Post.query.filter_by(user_id=user.id).one().body == 'by event'
    inbound.pds.put(inbound.bob, POST, '3kt', note('target'))
    ingest.handle({'kind': 'commit', 'did': inbound.bob, 'commit': {
        'collection': ingest.REPOST, 'operation': 'create', 'rkey': '3kb',
        'record': {'subject': {'uri': f'at://{inbound.bob}/{POST}/3kt'}}}})
    assert PostBoost.query.count() == 1
    ingest.handle({'did': inbound.bob, 'collection': POST, 'operation': 'delete', 'rkey': '3ke'})
    assert Post.query.filter_by(body='by event').one().deleted
    inbound.pds.put(inbound.bob, 'app.bsky.actor.profile', 'self', {'displayName': 'Robert'})
    ingest.handle({'did': inbound.bob, 'collection': 'app.bsky.actor.profile', 'operation': 'update', 'rkey': 'self'})
    assert user.title == 'Robert'
    inbound.pds.accounts[inbound.bob]['handle'] = 'robert.example'
    ingest.handle({'$type': 'network.bsky.jetstream.subscribeEvents#identity', 'did': inbound.bob})
    assert user.ap_id == 'robert.example'
    for odd in ({}, {'did': 3}, {'did': inbound.bob, 'collection': POST, 'operation': 'create'},
                {'did': 'did:plc:aaaaaaaaaaaaaaaaaaaaaaaa', 'kind': 'identity'},
                {'did': 'did:plc:aaaaaaaaaaaaaaaaaaaaaaaa', 'collection': 'app.bsky.actor.profile',
                 'operation': 'update', 'rkey': 'self'},
                {'did': inbound.bob, 'collection': 'app.bsky.feed.like', 'operation': 'create', 'rkey': 'x'}):
        ingest.handle(odd)   # nothing to do, nothing raised


def test_the_event_task(inbound, monkeypatch):
    seen = []
    monkeypatch.setattr(ingest, 'handle', seen.append)
    ingest.event_task({'did': 'x'})
    monkeypatch.setattr(ingest, 'handle', lambda event: (_ for _ in ()).throw(Permanent('no')))
    ingest.event_task({'did': 'y'})   # logged, not raised
    queued = []
    monkeypatch.setattr(ingest.event_task, 'delay', queued.append)
    ingest.take({'did': 'z'})
    monkeypatch.setattr(inbound.app, 'debug', True)
    monkeypatch.setattr(ingest, 'event_task', queued.append)
    ingest.take({'did': 'w'})
    assert seen == [{'did': 'x'}] and queued == [{'did': 'z'}, {'did': 'w'}]


# --------------------------------------------------------------------------
# signed-in readers only (§7)
# --------------------------------------------------------------------------

def test_a_record_is_kept_once_and_a_like_delete_finds_nothing_to_undo(inbound):
    bob(inbound)
    uri = f'at://{inbound.bob}/app.bsky.feed.like/3kl'
    ingest._keep(uri, None, inbound.bob, 'app.bsky.feed.like')
    ingest._keep(uri, None, inbound.bob, 'app.bsky.feed.like')   # a race delivered it twice: one row
    assert AtprotoRecord.query.filter_by(uri=uri).count() == 1
    assert ingest.delete(inbound.bob, 'app.bsky.feed.like', '3kl') and AtprotoRecord.query.count() == 0


def test_a_login_only_post_and_its_replies_are_hidden_from_strangers(inbound, app, monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # the post page renders form.csrf_token
    from app.local_only import hidden_here, hidden_here_clause
    from app.visibility import can_view
    carol = inbound.pds.account('carol.bsky.example')
    root = inbound.pds.put(carol, POST, '3kroot', note('quiet', labels={'values': [{'val': '!no-unauthenticated'}]}))
    reply = ingest.post(inbound.bob, '3kr', note('re', reply={'parent': {'uri': root}}))
    post = reply.post
    assert post.requires_login
    assert not can_view(post, None) and can_view(post, inbound.author.id)
    with app.test_request_context():
        assert hidden_here(post, signed_in=False) and hidden_here(reply, signed_in=False)
        assert not hidden_here(post, signed_in=True)
        assert Post.query.filter(hidden_here_clause(Post, signed_in=False)).count() == 1
        assert PostReply.query.filter(hidden_here_clause(PostReply, signed_in=False)).count() == 1
    assert inbound.client.get(f'/post/{post.id}').status_code == 200
