"""Posts, comments, private messages and activities are named on their owner's domain (plan C3, task 3).

shop.example federates beside hell.cloud (the test app's SERVER_NAME). A post's or comment's id is on its author's
home domain, whatever community it is in; an activity's id is on its actor's domain (a user, a community, or the
default domain for a feed). The send paths run here as Celery runs them: no request, so nothing can come from the
storefront. Owner ruling: nothing a sister domain federates names hell.cloud, and an ActivityPub fetch on one of our
domains for another of our domains' object is the 404 of an unknown object, so no fetch shows the two are one server.
hell.cloud's ids keep their shape (golden strings below)."""
import json
import re
from types import SimpleNamespace

import pytest

import app.activitypub.routes as activitypub_routes
import app.admin.util as admin_util
import app.chat.util as chat_util
import app.shared.tasks.blocks as blocks_module
import app.shared.tasks.deletes as deletes_module
import app.shared.tasks.follows as follows_module
import app.shared.tasks.likes as likes_module
import app.shared.tasks.notes as notes_module
import app.shared.feed as shared_feed
import app.shared.tasks.pages as pages_module
import app.user.utils as user_utils
from app import cache, db
from app.activitypub.routes import process_inbox_request
from app.activitypub.serializers.microblog import link_hashtags, microblog_note
from app.activitypub.util import comment_model_to_json, post_to_page
from app.federation_urls import activity_id, hosted_domain_names, object_base, owned_elsewhere
from app.models import ActivityPubLog, ChatMessage, CommunityFlair, CommunityMember, Conversation, Feed, Instance, \
    PostVote, Site, SiteDomain, Tag
from app.quotes import resolve_quotes
from app.shared.tasks.deletes import delete_object
from app.shared.tasks.follows import join_community, leave_community
from app.shared.tasks.likes import send_vote
from app.shared.tasks.notes import send_reply
from app.shared.tasks.pages import send_post
from app.storefront import clear_domain_cache, domain_for_host
from tests.factories import a_keypair, make_community, make_community_member, make_instance, make_post, \
    make_post_reply, make_user
from tests.test_onion_guard_limits import PROXY, a_session, onion_get
from tests.two_domains import HELL, ONION, request_on, two_domains  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures('site')

SHOP = 'shop.example'
HELL_BASE, SHOP_BASE = f'https://{HELL}', f'https://{SHOP}'
AP = {'Accept': 'application/activity+json'}
TOKEN = r'[0-9A-Za-z]{15}'


@pytest.fixture(autouse=True)
def onion_proxy(app, monkeypatch):
    monkeypatch.setitem(app.config, 'ONION_PROXY_IP', PROXY)


def _keyed(row, kind):
    row.private_key, row.public_key = a_keypair()
    row.ap_public_url = row.ap_profile_id
    if kind == 'u':
        row.ap_inbox_url = f'{row.ap_profile_id}/inbox'


@pytest.fixture
def sites(app, two_domains):
    """hal on hell.cloud, sam on shop.example, a community on each (pit, wares), a peer following both, and rem on
    the peer."""
    hell, onion = two_domains.domains
    db.session.add(SiteDomain(domain=SHOP, scheme='https', federates=True))
    db.session.commit()
    clear_domain_cache()
    shop = domain_for_host(SHOP)
    hal, sam = two_domains.local_user('hal', hell), two_domains.local_user('sam', shop)
    pit, wares = two_domains.local_community('pit', hell), two_domains.local_community('wares', shop)
    for row, kind in ((hal, 'u'), (sam, 'u'), (pit, 'c'), (wares, 'c')):
        _keyed(row, kind)
    peer = make_instance('peer.example', software='lemmy')
    peer.inbox = 'https://peer.example/inbox'
    rem = make_user(peer, 'rem')
    for community in (pit, wares):
        make_community_member(rem, community)
    db.session.commit()
    cache.clear()
    return SimpleNamespace(hell=hell, onion=onion, shop=shop, hal=hal, sam=sam, pit=pit, wares=wares, peer=peer,
                           rem=rem)


def captured(monkeypatch, *modules):
    """What each module's send_post_request was asked to send: (inbox, body, key id)."""
    sent = []
    for module in modules:
        monkeypatch.setattr(module, 'send_post_request',
                            lambda inbox, body, key, key_id, *args, **kwargs: sent.append((inbox, body, key_id)))
    return sent


def a_post(community, author, title='a post'):
    post = make_post(community, author, ap_id=None, title=title)
    post.generate_ap_id(community)
    db.session.commit()
    return post


def a_reply(post, author, parent=None):
    reply = make_post_reply(post, author)
    reply.ap_id = None
    if parent is not None:
        reply.parent_id = parent.id
    reply.ap_id = reply.profile_id()
    db.session.commit()
    return reply


def no_hell(body) -> bool:
    return HELL not in json.dumps(body)


# --------------------------------------------------------------------------
# object ids
# --------------------------------------------------------------------------

def test_a_shop_users_post_is_named_on_the_shop(app, sites):
    post = a_post(sites.wares, sites.sam)
    assert post.ap_id == f'{SHOP_BASE}/c/wares@{SHOP}/p/{post.id}/a-post'
    assert post.slug == f'/c/wares@{SHOP}/p/{post.id}/a-post'
    unsluggable = a_post(sites.wares, sites.sam, title='!!!')
    assert unsluggable.ap_id == f'{SHOP_BASE}/post/{unsluggable.id}'
    sites.wares.post_url_type = 'legacy'
    legacy = a_post(sites.wares, sites.sam)
    assert legacy.ap_id == f'{SHOP_BASE}/post/{legacy.id}'
    legacy.ap_id = None
    assert legacy.profile_id() == f'{SHOP_BASE}/post/{legacy.id}'


def test_a_post_or_comment_is_named_on_its_authors_domain_whatever_the_community(app, sites):
    in_shop = a_post(sites.wares, sites.hal)
    assert in_shop.ap_id == f'{HELL_BASE}/c/wares@{SHOP}/p/{in_shop.id}/a-post'
    in_hell = a_post(sites.pit, sites.sam)
    assert in_hell.ap_id.startswith(f'{SHOP_BASE}/c/pit@{HELL}/p/')
    assert a_reply(in_hell, sites.hal).ap_id.startswith(f'{HELL_BASE}/comment/')
    assert a_reply(in_shop, sites.sam).ap_id.startswith(f'{SHOP_BASE}/comment/')


def test_hells_ids_keep_their_shape(app, sites):
    """Golden strings: the default domain's ids are built from SERVER_URL exactly as before C3."""
    post = a_post(sites.pit, sites.hal)
    assert post.ap_id == f'https://test.piefed.local/c/pit@test.piefed.local/p/{post.id}/a-post'
    reply = a_reply(post, sites.hal)
    assert reply.ap_id == f'https://test.piefed.local/comment/{reply.id}'
    assert re.fullmatch(rf'https://test\.piefed\.local/activities/like/{TOKEN}', activity_id('like', sites.hal))
    assert re.fullmatch(rf'https://test\.piefed\.local/activities/announce/{TOKEN}', activity_id('announce', sites.pit))
    page = post_to_page(post)
    assert page['context'] == f'https://test.piefed.local/post/{post.id}/context'
    assert page['replies'] == f'https://test.piefed.local/post/{post.id}/replies'
    assert page['likes']['id'] == f'https://test.piefed.local/post/{post.id}/likes'


def test_a_remote_post_is_named_on_the_community_that_serves_it(app, sites):
    post = make_post(sites.wares, sites.rem, ap_id='https://peer.example/post/1')
    assert object_base(post) == SHOP_BASE
    assert object_base(make_post_reply(post, sites.rem)) == SHOP_BASE
    away = two_domains_away(sites)
    assert object_base(make_post(away, sites.rem, ap_id='https://peer.example/post/2')) == HELL_BASE
    message = ChatMessage(sender_id=sites.rem.id, body='hi')
    db.session.add(message)
    db.session.commit()
    assert object_base(message) == HELL_BASE   # no domain of ours names it: as before C3


def two_domains_away(sites):
    away = make_community('away', host='peer.example')
    away.ap_id, away.instance_id = 'away@peer.example', sites.peer.id
    away.ap_inbox_url = 'https://peer.example/c/away/inbox'
    db.session.commit()
    return away


def test_a_shop_posts_documents_name_only_the_shop(app, sites):
    post = a_post(sites.wares, sites.sam)
    post.body_html = '<p>see #gold</p>'
    post.tags.append(Tag(name='gold', display_as='gold'))
    flair = CommunityFlair(community_id=sites.wares.id, flair='new')
    db.session.add(flair)
    db.session.flush()
    post.flair.append(flair)
    db.session.commit()
    page = post_to_page(post)
    assert no_hell(page), json.dumps(page)
    assert page['context'] == f'{SHOP_BASE}/post/{post.id}/context'
    assert page['replies'] == f'{SHOP_BASE}/post/{post.id}/replies'
    assert page['likes']['id'] == f'{SHOP_BASE}/post/{post.id}/likes'
    assert {'type': 'Hashtag', 'href': f'{SHOP_BASE}/tag/gold', 'name': '#gold'} in page['tag']
    [community_tag] = [tag for tag in page['tag'] if tag['type'] == 'lemmy:CommunityTag']
    assert community_tag['id'] == f'{SHOP_BASE}/c/wares/tag/{flair.id}'
    reply = a_reply(post, sites.sam)
    note = comment_model_to_json(reply)
    assert no_hell(note), json.dumps(note)
    assert note['context'] == f'{SHOP_BASE}/post/{post.id}/context'
    assert note['likes']['id'] == f'{SHOP_BASE}/comment/{reply.id}/likes'


def test_a_flair_of_a_remote_community_is_named_by_whoever_names_our_copy(app, sites):
    away = two_domains_away(sites)
    flair = CommunityFlair(community_id=away.id, flair='old')
    db.session.add(flair)
    db.session.commit()
    assert away.flair_tag_id(flair, SHOP_BASE) == f'{SHOP_BASE}/c/away@peer.example/tag/{flair.id}'
    assert away.flair_for_ap(version=1)[0]['id'] == f'{HELL_BASE}/c/away@peer.example/tag/{flair.id}'
    shop_flair = CommunityFlair(community_id=sites.wares.id, flair='new')
    db.session.add(shop_flair)
    db.session.commit()
    assert sites.wares.flair_for_ap(version=1)[0]['id'] == f'{SHOP_BASE}/c/wares/tag/{shop_flair.id}'


def test_a_shop_microblog_notes_hashtags_and_collections_are_the_shops(app, sites):
    post = a_post(sites.wares, sites.sam)
    post.body_html = '<p>see #gold</p>'
    db.session.commit()
    note = microblog_note(post, [])
    assert note['context'] == f'{SHOP_BASE}/post/{post.id}/context'
    assert note['replies'] == f'{SHOP_BASE}/post/{post.id}/replies'
    assert link_hashtags('<p>#gold</p>', SHOP_BASE)[0] == \
        f'<p><a class="mention hashtag" href="{SHOP_BASE}/tag/gold" rel="tag">#<span>gold</span></a></p>'


# --------------------------------------------------------------------------
# Review Focus 1: a hell.cloud user replies in a shop.example community
# --------------------------------------------------------------------------

def test_a_hell_users_reply_in_a_shop_community(app, sites, monkeypatch):
    """The reply and its Create are hell's, signed by hal's key; the community's Announce is the shop's, signed by
    the community's key. Nothing is sent to a domain of ours: sam (on the shop) is told here."""
    other = make_instance('other.example', software='mastodon')
    other.inbox = 'https://other.example/inbox'
    rex = make_user(other, 'rex')
    shop_instance = db.session.get(Instance, sites.shop.instance_id)
    shop_instance.inbox = 'https://shop.example/inbox'   # a stale row: still never sent to
    db.session.commit()
    post = a_post(sites.wares, sites.sam)
    answered = a_reply(post, rex)
    answered.ap_id = 'https://other.example/notes/1'
    db.session.commit()
    reply = a_reply(post, sites.hal, parent=answered)
    assert reply.ap_id == f'{HELL_BASE}/comment/{reply.id}'
    sent = captured(monkeypatch, notes_module)

    send_reply(reply.id, answered.id, session=db.session)

    by_inbox = {inbox: (body, key_id) for inbox, body, key_id in sent}
    assert set(by_inbox) == {'https://peer.example/inbox', 'https://other.example/inbox'}
    announce, key_id = by_inbox['https://peer.example/inbox']
    assert key_id == f'{SHOP_BASE}/c/wares#main-key'
    assert re.fullmatch(rf'{SHOP_BASE}/activities/announce/{TOKEN}', announce['id'])
    assert announce['actor'] == f'{SHOP_BASE}/c/wares'
    create, key_id = by_inbox['https://other.example/inbox']
    assert key_id == f'{HELL_BASE}/u/hal#main-key'
    assert re.fullmatch(rf'{HELL_BASE}/activities/create/{TOKEN}', create['id'])
    assert announce['object']['id'] == create['id']
    assert create['object']['id'] == reply.ap_id


def test_a_hosted_domains_accounts_are_never_sent_to(app, sites):
    shop_instance = db.session.get(Instance, sites.shop.instance_id)
    assert set(hosted_domain_names()) == {HELL, ONION, SHOP} == \
        {app.config['SERVER_NAME'], *(row.domain for row in Instance.query.filter(
            Instance.id.in_([sites.hell.instance_id, sites.onion.instance_id, shop_instance.id])))}


# --------------------------------------------------------------------------
# Review Focus 2: a shop user's activities, as a Celery worker sends them (no request)
# --------------------------------------------------------------------------

def assert_shops(body, verb, key_id, actor=f'{SHOP_BASE}/u/sam'):
    assert no_hell(body), json.dumps(body)
    assert re.fullmatch(rf'{SHOP_BASE}/activities/{verb}/{TOKEN}', body['id']), body['id']
    assert key_id == f'{actor}#main-key'


def test_a_shop_users_like_and_its_undo_and_the_announces_are_the_shops(app, sites, monkeypatch):
    post = a_post(sites.wares, sites.sam)
    db.session.add(PostVote(user_id=sites.sam.id, post_id=post.id, author_id=sites.sam.id, effect=1))
    db.session.commit()
    sites.peer.software = 'lemmy'
    db.session.commit()
    sent = captured(monkeypatch, likes_module)

    send_vote(sites.sam.id, post, None, 'upvote', None)
    send_vote(sites.sam.id, post, 'Like', 'upvote', None)

    [(_, like, like_key), (_, undo, undo_key)] = sent
    assert_shops(like, 'announce', like_key, f'{SHOP_BASE}/c/wares')
    assert re.fullmatch(rf'{SHOP_BASE}/activities/like/{TOKEN}', like['object']['id'])
    assert_shops(undo, 'announce', undo_key, f'{SHOP_BASE}/c/wares')
    assert re.fullmatch(rf'{SHOP_BASE}/activities/undo/{TOKEN}', undo['object']['id'])


def test_a_shop_users_follow_and_undo_of_a_remote_community_are_the_shops(app, sites, monkeypatch):
    away = two_domains_away(sites)
    sent = captured(monkeypatch, follows_module)

    join_community(False, sites.sam.id, away.id, 'web')
    leave_community(False, sites.sam.id, away.id)

    [(_, follow, follow_key), (_, undo, undo_key)] = sent
    assert no_hell(follow) and follow_key == f'{SHOP_BASE}/u/sam#main-key'
    assert re.fullmatch(rf'{SHOP_BASE}/activities/follow/[0-9a-f-]+', follow['id'])
    assert_shops(undo, 'undo', undo_key)
    assert undo['object']['id'] == follow['id']


def test_a_shop_users_delete_and_update_are_the_shops(app, sites, monkeypatch):
    post = a_post(sites.wares, sites.sam)
    sent = captured(monkeypatch, deletes_module, pages_module)

    send_post(post.id, edit=True, session=db.session)
    delete_object(sites.sam.id, post, is_post=True, session=db.session)

    [(_, update, update_key), (_, delete, delete_key)] = sent
    assert_shops(update, 'announce', update_key, f'{SHOP_BASE}/c/wares')
    assert re.fullmatch(rf'{SHOP_BASE}/activities/update/{TOKEN}', update['object']['id'])
    assert update['object']['object']['id'] == post.ap_id
    assert_shops(delete, 'announce', delete_key, f'{SHOP_BASE}/c/wares')
    assert re.fullmatch(rf'{SHOP_BASE}/activities/delete/{TOKEN}', delete['object']['id'])


def test_a_shop_communitys_accept_of_a_follow_is_the_shops(app, sites, monkeypatch):
    sent = captured(monkeypatch, activitypub_routes)
    CommunityMember.query.filter_by(user_id=sites.rem.id, community_id=sites.wares.id).delete()
    db.session.commit()

    process_inbox_request({'id': 'https://peer.example/activities/follow/1', 'type': 'Follow',
                           'actor': sites.rem.ap_profile_id, 'object': f'{SHOP_BASE}/c/wares'}, True)

    [(_, accept, key_id)] = sent
    assert no_hell(accept), json.dumps(accept)
    assert re.fullmatch(rf'{SHOP_BASE}/activities/accept/[0-9A-Za-z]{{32}}', accept['id'])
    assert key_id == f'{SHOP_BASE}/c/wares#main-key'


@pytest.mark.parametrize('kind', ['community', 'feed'])
def test_a_follow_of_another_servers_actor_is_not_answered(app, sites, monkeypatch, kind):
    """No domain of ours names an Accept for a community or feed that is not ours, nor holds its key."""
    sent = captured(monkeypatch, activitypub_routes)
    if kind == 'community':
        target = two_domains_away(sites).ap_profile_id
    else:
        feed = Feed(name='farfeed', title='Far feed', public=True, instance_id=sites.peer.id, ap_id='farfeed@peer.example',
                    ap_profile_id='https://peer.example/f/farfeed', ap_public_url='https://peer.example/f/farfeed',
                    ap_domain='peer.example')
        db.session.add(feed)
        db.session.commit()
        target = feed.ap_profile_id

    process_inbox_request({'id': 'https://peer.example/activities/follow/2', 'type': 'Follow',
                           'actor': sites.rem.ap_profile_id, 'object': target}, True)

    assert sent == []


def test_a_shop_users_private_message_is_the_shops(app, sites, monkeypatch):
    conversation = Conversation(user_id=sites.sam.id)
    conversation.members.append(sites.sam)
    conversation.members.append(sites.rem)
    db.session.add(conversation)
    db.session.commit()
    sent = captured(monkeypatch, chat_util)

    message = chat_util.send_message('hello', conversation.id, user=sites.sam)
    message.edited_at = message.created_at
    chat_util.update_message(message)

    assert message.ap_id == f'{SHOP_BASE}/private_message/{message.id}'
    [(_, create, create_key), (_, update, update_key)] = sent
    assert_shops(create, 'create', create_key)
    assert_shops(update, 'update', update_key)


# --------------------------------------------------------------------------
# a remote server's activity on a shop post
# --------------------------------------------------------------------------

def test_a_remote_like_of_a_shop_post_is_matched(app, sites, monkeypatch):
    post = a_post(sites.wares, sites.sam)
    captured(monkeypatch, activitypub_routes)

    process_inbox_request({'id': 'https://peer.example/activities/like/1', 'type': 'Like',
                           'actor': sites.rem.ap_profile_id, 'object': post.ap_id}, True)

    assert PostVote.query.filter_by(user_id=sites.rem.id, post_id=post.id).count() == 1
    assert post.is_local()


def test_a_quote_of_a_shop_posts_own_url_is_found(app, sites):
    post = a_post(sites.wares, sites.sam)
    hell_post = a_post(sites.pit, sites.hal)
    found = resolve_quotes({f'{SHOP_BASE}/post/{post.id}', f'{SHOP_BASE}/post/{hell_post.id}',
                            f'{HELL_BASE}/post/{post.id}', f'http://{SHOP}/post/{post.id}'})
    assert found == {f'{SHOP_BASE}/post/{post.id}': post}


# --------------------------------------------------------------------------
# an ActivityPub fetch through another of our domains is a 404 (owner ruling)
# --------------------------------------------------------------------------

def fetch(app, path, host, headers=AP):
    client = app.test_client()
    if host == ONION:
        a_session(client)
        return onion_get(client, path, headers=headers)
    return client.get(path, headers=headers, base_url=f'https://{host}')


@pytest.fixture
def owned(sites):
    """A post, a comment and a private message of each of hell's and the shop's, and a public feed of hell's."""
    rows = {}
    for name, community, author in (('hell', sites.pit, sites.hal), ('shop', sites.wares, sites.sam)):
        post = a_post(community, author)
        reply = a_reply(post, author)
        conversation = Conversation(user_id=author.id)
        db.session.add(conversation)
        db.session.flush()
        message = ChatMessage(sender_id=author.id, recipient_id=sites.rem.id, conversation_id=conversation.id,
                              body='hi', body_html='<p>hi</p>')
        db.session.add(message)
        db.session.flush()
        message.ap_id = message.ap_id or f'{object_base(message)}/private_message/{message.id}'
        rows[name] = SimpleNamespace(post=post, reply=reply, message=message, user=author, community=community)
    feed = Feed(name='hellfeed', title='Hell feed', public=True, user_id=sites.hal.id,
                instance_id=sites.hell.instance_id, ap_profile_id=f'{HELL_BASE}/f/hellfeed',
                ap_public_url=f'{HELL_BASE}/f/hellfeed')
    db.session.add(feed)
    db.session.commit()
    cache.clear()
    return rows


def object_paths(row) -> list[str]:
    name, community = row.user.user_name, row.community.name
    return [f'/post/{row.post.id}', f'/post/{row.post.id}/replies', f'/post/{row.post.id}/context',
            f'/post/{row.post.id}/likes', row.post.slug, f'/comment/{row.reply.id}', f'/comment/{row.reply.id}/likes',
            f'/u/{name}', f'/u/{name}/outbox', f'/u/{name}/followers', f'/u/{name}/following', f'/u/{name}/liked',
            f'/c/{community}', f'/c/{community}/outbox', f'/c/{community}/featured', f'/c/{community}/moderators',
            f'/c/{community}/followers', f'/c/{community}/following']


@pytest.mark.parametrize('owner, other', [('hell', SHOP), ('shop', HELL), ('hell', ONION)])
def test_another_of_our_domains_objects_are_unknown_here(app, sites, owned, owner, other):
    row = owned[owner]
    home = HELL if owner == 'hell' else SHOP
    for path in object_paths(row):
        spelt = path
        if path.startswith(('/u/', '/c/')):   # name@host reaches the other domain's row from this one
            kind, rest = path[1], path[3:]
            name, _, collection = rest.partition('/')
            spelt = f'/{kind}/{name}@{home}' + (f'/{collection}' if collection else '')
        assert fetch(app, spelt, other).status_code == 404, (spelt, other)
        assert fetch(app, path, home).status_code == 200, (path, home)


@pytest.mark.parametrize('path', ['/f/hellfeed', '/f/hellfeed/outbox', '/f/hellfeed/following',
                                  '/f/hellfeed/moderators', '/f/hellfeed/followers'])
def test_a_feed_is_hells_and_unknown_on_the_shop(app, sites, owned, path):
    assert fetch(app, path, SHOP).status_code == 404
    assert fetch(app, path, HELL).status_code == 200


def test_a_gone_account_of_another_domain_is_no_tombstone_here(app, sites, owned):
    sites.sam.deleted = True
    db.session.commit()
    assert fetch(app, f'/u/sam@{SHOP}', HELL).status_code == 404
    assert fetch(app, f'/u/sam@{SHOP}/outbox', HELL).status_code == 404
    assert fetch(app, '/u/sam', SHOP).status_code == 410
    assert fetch(app, '/u/sam/outbox', SHOP).status_code == 410


def test_a_private_message_is_unknown_on_another_of_our_domains(app, sites, owned):
    for name, other in (('hell', SHOP), ('shop', HELL)):
        message = owned[name].message
        assert message.ap_id.startswith(HELL_BASE if name == 'hell' else SHOP_BASE)
        assert fetch(app, f'/private_message/{message.id}', other).status_code == 404


def test_an_activity_is_looked_up_on_the_domain_asked(app, sites):
    token = 'abcdefghijklmno'
    for base in (HELL_BASE, SHOP_BASE):
        db.session.add(ActivityPubLog(direction='out', activity_id=f'{base}/activities/like/{token}',
                                      activity_type='Like', result='success',
                                      activity_json=json.dumps({'id': f'{base}/activities/like/{token}',
                                                                'to': ['https://www.w3.org/ns/activitystreams#Public']})))
    db.session.commit()
    for host, base in ((HELL, HELL_BASE), (SHOP, SHOP_BASE)):
        response = fetch(app, f'/activities/like/{token}', host)
        assert response.status_code == 200 and json.loads(response.data)['id'] == f'{base}/activities/like/{token}'
    db.session.query(ActivityPubLog).filter(ActivityPubLog.activity_id.startswith(SHOP_BASE)).delete()
    db.session.commit()
    assert fetch(app, f'/activities/like/{token}', SHOP).status_code == 404   # hell's is not the shop's


def test_a_browser_request_is_answered_as_before(app, sites, owned):
    """HTML is unaffected: the shop's storefront answers a hell post's page as C2 left it (a login first), not 404."""
    post_id = owned['hell'].post.id
    response = fetch(app, f'/post/{post_id}', SHOP, headers={})
    assert (response.status_code, response.headers['Location']) == (302, f'/auth/login?next=/post/{post_id}')
    assert fetch(app, f'/post/{post_id}', HELL, headers={}).status_code == 302   # as for anyone logged out


def test_owned_elsewhere_reads_the_requests_domain(app, sites, owned):
    with request_on(app, sites.shop):
        assert owned_elsewhere(owned['hell'].post) and not owned_elsewhere(owned['shop'].post)
        assert not owned_elsewhere(make_post(two_domains_away(sites), sites.rem, ap_id='https://peer.example/p/9'))
    assert owned_elsewhere(owned['shop'].reply) and not owned_elsewhere(owned['hell'].reply)   # no request: default


# --------------------------------------------------------------------------
# what a remote account has no domain of ours to name
# --------------------------------------------------------------------------

@pytest.mark.parametrize('module', [admin_util, user_utils])
def test_no_undo_follow_is_sent_for_a_remote_account(app, sites, monkeypatch, module):
    away = two_domains_away(sites)
    sent = captured(monkeypatch, module)
    module.unsubscribe_from_community(away, sites.rem)
    module.unsubscribe_from_community(away, sites.sam)
    [(_, undo, key_id)] = sent
    assert_shops(undo, 'undo', key_id)


def test_a_shop_moderators_site_ban_targets_the_shop(app, sites, monkeypatch):
    sent = captured(monkeypatch, blocks_module)
    blocks_module.ban_person(db.session, sites.rem.id, sites.sam.id, None, None, 'spam', False)
    [(_, block, key_id)] = sent
    assert block['target'] == f'{SHOP_BASE}/'
    assert_shops(block, 'block', key_id)


# --------------------------------------------------------------------------
# Fix round 1
# --------------------------------------------------------------------------

def test_a_shop_owners_feed_delete_is_the_shops(app, sites, monkeypatch):
    """I1: the Delete is the owner's activity, so its id is on the owner's domain, as its actor and key are."""
    sent = captured(monkeypatch, shared_feed)
    shared_feed.announce_feed_delete_to_subscribers(sites.sam.id, f'{HELL_BASE}/f/samsfeed',
                                                    ['https://peer.example/inbox'])
    [(inbox, delete, key_id)] = sent
    assert inbox == 'https://peer.example/inbox'
    assert re.fullmatch(rf'{SHOP_BASE}/delete/{TOKEN}', delete['id'])
    assert delete['actor'] == f'{SHOP_BASE}/u/sam' and key_id == f'{SHOP_BASE}/u/sam#main-key'


def test_a_hell_owners_feed_delete_keeps_its_shape(app, sites, monkeypatch):
    sent = captured(monkeypatch, shared_feed)
    shared_feed.announce_feed_delete_to_subscribers(sites.hal.id, f'{HELL_BASE}/f/halsfeed',
                                                    ['https://peer.example/inbox'])
    [(_, delete, _)] = sent
    assert re.fullmatch(rf'https://test\.piefed\.local/delete/{TOKEN}', delete['id'])


def page_alternates(response) -> list[str]:
    return [link for link in response.headers.getlist('Link') if 'application/activity+json' in link]


@pytest.mark.parametrize('owner', ['hell', 'shop'])
def test_a_posts_page_advertises_its_activitypub_id_on_its_owners_domain(app, sites, owner):
    """I2: the HTML page's ActivityPub alternate is the URL that answers the fetch, on the post's domain."""
    author, community, host, base = (sites.hal, sites.pit, HELL, HELL_BASE) if owner == 'hell' else \
        (sites.sam, sites.wares, SHOP, SHOP_BASE)
    post = a_post(community, author)
    db.session.get(Site, 1).private_instance = False   # a logged-out visitor sees the page
    db.session.commit()
    response = app.test_client().get(f'/post/{post.id}', base_url=f'https://{host}')
    assert response.status_code == 200
    assert page_alternates(response) == [f'<{base}/post/{post.id}>; rel="alternate"; type="application/activity+json"']
    assert fetch(app, f'/post/{post.id}', host).status_code == 200   # the advertised URL answers there
