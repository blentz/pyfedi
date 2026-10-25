"""A sister domain's reply, vote and post are stored directly and reach remote followers from the right domain (plan C4
task 5, spec 2026-10-19 per-domain identity §3.3 and the §5 required test).

hell.cloud (the test app's SERVER_NAME) and shop.example share one database. A shop.example account commenting on a
hell.cloud post in a hell.cloud community is stored at once: nothing is sent to ourselves over the network. A remote
server following the community gets what it would get if the two domains were separate servers: the community's
Announce, signed by the community's own key, wrapping the shop account's Create. The fan-out is upstream PieFed's
("exactly as if over the network"): the community, mentioned and parent authors, and only for a followers-only reply
the author's followers.

Every action runs through the shared function its route calls (app/shared/reply.py, app/shared/post.py), in a
request addressed to the actor's domain, with Celery eager. Every outbound request is recorded at the HTTP layer
(respx), so "nothing reached a hosted domain" means no request reached httpx, and a signature asserted is one a peer
would verify: the request is replayed into production's verifier, playing the peer.
"""
import json
from types import SimpleNamespace

import pytest
from flask import g, request
from flask_login import login_user

import app as app_pkg
from app import cache, db
from app.activitypub.signature import POST_REQUIRED, HttpSignature
from app.activitypub.signature_errors import KeyMismatchError
from app.constants import NOTIF_POST, POST_TYPE_ARTICLE, SRC_API
from app.models import ActivityPubLog, Notification, Post, PostReply, PostVote, Site, SiteDomain
from app.shared.post import make_post as shared_make_post, vote_for_post
from app.shared.reply import make_reply
from app.storefront import clear_domain_cache, domain_for_host, is_hosted_uri
from tests.factories import bearer, make_community_member, make_follow, make_instance, make_notification_subscription, \
    make_user
from tests.test_no_http_to_hosted import wire  # noqa: F401 -- fixture
from tests.test_per_domain_object_ids import _keyed, a_post
from tests.test_per_domain_signing import _key_id
from tests.test_shared_post_edit import _api_input
from tests.two_domains import HELL, request_on, two_domains  # noqa: F401 -- fixture

pytestmark = pytest.mark.usefixtures('site')

SHOP = 'shop.example'
HELL_BASE, SHOP_BASE = f'https://{HELL}', f'https://{SHOP}'
PEER_INBOX = 'https://peer.example/inbox'
PIEFED_INBOX = 'https://pf.example/inbox'
MASTO_INBOX = 'https://masto.example/inbox'


def _peer(domain, software):
    instance = make_instance(domain, software=software)
    instance.inbox = f'https://{domain}/inbox'
    db.session.commit()
    return instance


@pytest.fixture
def world(app, two_domains):  # noqa: F811 -- two_domains is the imported fixture
    """hal on hell.cloud and sam on shop.example, a community on each (pit on hell.cloud, wares on shop.example), each
    with its own key, and rem on peer.example (Lemmy) following both. hal's post in pit and sam's post in wares, each
    author subscribed to replies as make_post leaves them."""
    hell = two_domains.domains[0]
    db.session.add(SiteDomain(domain=SHOP, scheme='https', federates=True))
    db.session.commit()
    clear_domain_cache()
    shop = domain_for_host(SHOP)
    hal, sam = two_domains.local_user('hal', hell), two_domains.local_user('sam', shop)
    pit, wares = two_domains.local_community('pit', hell), two_domains.local_community('wares', shop)
    for row, kind in ((hal, 'u'), (sam, 'u'), (pit, 'c'), (wares, 'c')):
        _keyed(row, kind)
    rem = make_user(_peer('peer.example', 'lemmy'), 'rem')
    for community in (pit, wares):
        make_community_member(rem, community)
    hello, wares_post = a_post(pit, hal, 'hello'), a_post(wares, sam, 'wares news')
    for author, post in ((hal, hello), (sam, wares_post)):
        make_notification_subscription(author, post.id, NOTIF_POST)
    cache.clear()
    yield SimpleNamespace(hell=hell, shop=shop, hal=hal, sam=sam, pit=pit, wares=wares, rem=rem, hello=hello,
                          wares_post=wares_post)
    clear_domain_cache()


def _act(app, domain, user, action):
    """`action` in a request addressed to `domain` with `user` signed in, as the route calling it runs."""
    with request_on(app, domain):
        g.site = db.session.get(Site, 1)
        login_user(user)
        return action()


def _reply(app, author, domain, post):
    payload = {'body': 'a sister reply', 'notify_author': True, 'language_id': None, 'distinguished': False}
    return _act(app, domain, author, lambda: make_reply(payload, post, None, SRC_API, auth=bearer(author))[1])


def _sent(wire):  # noqa: F811 -- wire is the imported fixture
    """Each request that left the server: (url, body, keyId, the httpx request)."""
    return [(str(call.request.url), json.loads(call.request.content), _key_id(call.request), call.request)
            for call in wire.calls]


def _nothing_hosted(wire):  # noqa: F811
    assert [url for url, *_ in _sent(wire) if is_hosted_uri(url)] == []
    # and nothing even addressed one of ours (post_request logs a refused hosted delivery, or one to a hosted row's
    # empty inbox, as a failure): every activity handed to post_request was delivered
    assert [log.result for log in db.session.query(ActivityPubLog).filter_by(direction='out')] == \
        ['success'] * len(wire.calls)


def _verifies_at(monkeypatch, app, sent_request, public_key):
    """Production's verifier, run as the peer the request was addressed to would run it."""
    host = sent_request.url.host
    with monkeypatch.context() as peer:
        peer.setitem(app.config, 'SERVER_NAME', host)
        peer.setitem(app.config, 'SERVER_URL', f'https://{host}')
        with app.test_request_context(sent_request.url.raw_path.decode(), method=sent_request.method,
                                      headers=dict(sent_request.headers), data=sent_request.content):
            return HttpSignature.verify_request(request, public_key, POST_REQUIRED, enforce=True)


def _the_announce(wire, inbox, community):  # noqa: F811
    """The one delivery, to `inbox`: an Announce by `community`, on its domain, under its key."""
    sent = _sent(wire)
    assert [url for url, *_ in sent] == [inbox]
    _, body, key_id, sent_request = sent[0]
    base = community.public_url().split('/c/')[0]
    assert body['type'] == 'Announce'
    assert body['actor'] == community.public_url()
    assert body['id'].startswith(f'{base}/')
    assert key_id == f'{community.public_url()}#main-key'
    return body, sent_request


# -- a reply ----------------------------------------------------------------------------------------------------------

def test_a_shop_users_reply_on_a_hell_post_is_stored_and_announced_by_hells_community(app, world, wire,
                                                                                    monkeypatch):  # noqa: F811
    # sam has a remote follower: a public comment goes to the community, not to the author's followers (plan rule,
    # upstream PieFed), so their inbox gets nothing
    make_follow(world.sam, make_user(_peer('masto.example', 'mastodon'), 'max'), is_inward=True)

    reply = _reply(app, world.sam, world.shop, world.hello)

    assert MASTO_INBOX not in [url for url, *_ in _sent(wire)]
    stored = db.session.get(PostReply, reply.id)
    assert stored is not None and stored.post_id == world.hello.id and stored.user_id == world.sam.id
    assert stored.ap_id.startswith(f'{SHOP_BASE}/')
    _nothing_hosted(wire)

    announce, sent_request = _the_announce(wire, PEER_INBOX, world.pit)
    assert _verifies_at(monkeypatch, app, sent_request, world.pit.public_key) is True
    create = announce['object']
    assert create['type'] == 'Create'
    assert create['actor'] == f'{SHOP_BASE}/u/sam'
    assert create['id'].startswith(f'{SHOP_BASE}/')
    assert create['object']['id'] == stored.ap_id
    assert create['object']['inReplyTo'] == world.hello.public_url()
    assert create['audience'] == world.pit.public_url()

    # hal, the post's author on hell.cloud, is told here, not over the network
    notification = db.session.query(Notification).filter_by(user_id=world.hal.id).one()
    assert notification.author_id == world.sam.id
    assert notification.targets['comment_id'] == reply.id


def test_a_hell_users_reply_in_a_shop_community_is_announced_by_the_shops_community(app, world, wire,
                                                                                   monkeypatch):  # noqa: F811
    """The mirror: the community, not the replier, decides which domain Announces."""
    reply = _reply(app, world.hal, world.hell, world.wares_post)

    stored = db.session.get(PostReply, reply.id)
    assert stored.ap_id.startswith(f'{HELL_BASE}/')
    _nothing_hosted(wire)

    announce, sent_request = _the_announce(wire, PEER_INBOX, world.wares)
    assert announce['actor'] == f'{SHOP_BASE}/c/wares'
    assert _verifies_at(monkeypatch, app, sent_request, world.wares.public_key) is True
    create = announce['object']
    assert create['type'] == 'Create'
    assert create['actor'] == f'{HELL_BASE}/u/hal'
    assert create['id'].startswith(f'{HELL_BASE}/')
    assert create['object']['id'] == stored.ap_id

    notification = db.session.query(Notification).filter_by(user_id=world.sam.id).one()
    assert notification.author_id == world.hal.id


def test_the_announce_does_not_verify_under_the_repliers_key(app, world, wire, monkeypatch):  # noqa: F811
    """The verifier above can say no: the Announce is the community's, not the replying account's."""
    _reply(app, world.sam, world.shop, world.hello)
    _, sent_request = _the_announce(wire, PEER_INBOX, world.pit)
    with pytest.raises(KeyMismatchError, match='Signature mismatch'):
        _verifies_at(monkeypatch, app, sent_request, world.sam.public_key)


# -- a vote -----------------------------------------------------------------------------------------------------------

def test_a_shop_users_vote_on_a_hell_post_is_stored_and_announced_by_hells_community(app, world, wire,
                                                                                   monkeypatch):  # noqa: F811
    before = db.session.get(Post, world.hello.id).up_votes or 0
    _act(app, world.shop, world.sam,
         lambda: vote_for_post(world.hello.id, 'upvote', True, None, SRC_API, auth=bearer(world.sam)))

    db.session.expire_all()
    assert db.session.query(PostVote).filter_by(user_id=world.sam.id, post_id=world.hello.id).one().effect > 0
    assert db.session.get(Post, world.hello.id).up_votes == before + 1
    _nothing_hosted(wire)

    announce, sent_request = _the_announce(wire, PEER_INBOX, world.pit)
    assert _verifies_at(monkeypatch, app, sent_request, world.pit.public_key) is True
    like = announce['object']
    assert like['type'] == 'Like'
    assert like['actor'] == f'{SHOP_BASE}/u/sam'
    assert like['id'].startswith(f'{SHOP_BASE}/')
    assert like['object'] == world.hello.public_url()


@pytest.fixture
def notifs(app, monkeypatch):
    """The piefed_notifs channel switched on: what `redis_client.publish` was handed, as (channel, decoded payload)."""
    published = []
    monkeypatch.setitem(app.config, 'NOTIF_SERVER', 'https://notifs.example')
    monkeypatch.setattr(app_pkg.redis_client, 'publish',
                        lambda channel, payload: published.append((channel, json.loads(payload))))
    return published


def test_with_the_notifs_channel_a_shop_users_vote_is_published_for_the_peer_and_never_for_a_hosted_inbox(
        app, world, wire, notifs, monkeypatch):  # noqa: F811
    """A vote on a local community goes through piefed_notifs (likes.py send_vote) when NOTIF_SERVER is set: the
    second channel out of the server. A stale peer row whose inbox names shop.example follows pit too, the one way a
    hosted inbox reaches the fan-out (hosted Instance rows are filtered and carry none): it is never published,
    and the remote follower's delivery is, as the community's Announce."""
    stale = make_instance('mirror.example', software='lemmy')
    stale.inbox = 'https://Shop.Example/inbox'
    make_community_member(make_user(stale, 'mira'), world.pit)

    _act(app, world.shop, world.sam,
         lambda: vote_for_post(world.hello.id, 'upvote', True, None, SRC_API, auth=bearer(world.sam)))

    assert wire.calls.call_count == 0   # nothing over httpx: the votes went to the channel
    urls = [url for _, payload in notifs for url in payload['urls']]
    assert [url for url in urls if is_hosted_uri(url)] == []
    assert urls == [PEER_INBOX]
    (channel, payload), = notifs
    assert channel == 'http_posts:activity'
    announce = json.loads(payload['data'])
    assert announce['type'] == 'Announce' and announce['actor'] == world.pit.public_url()
    assert announce['object']['type'] == 'Like' and announce['object']['actor'] == f'{SHOP_BASE}/u/sam'


def test_with_the_notifs_channel_a_reply_still_goes_over_http_and_never_to_a_hosted_inbox(
        app, world, wire, notifs, monkeypatch):  # noqa: F811
    """Only votes use the channel (likes.py is publish_async_posts' one outbound caller): a reply is still sent by
    post_request, to the peer alone, with NOTIF_SERVER set."""
    _reply(app, world.sam, world.shop, world.hello)

    assert notifs == []
    _nothing_hosted(wire)
    _, sent_request = _the_announce(wire, PEER_INBOX, world.pit)
    assert _verifies_at(monkeypatch, app, sent_request, world.pit.public_key) is True


# -- a post -----------------------------------------------------------------------------------------------------------

def test_a_shop_users_post_in_a_hell_community_reaches_community_and_author_followers(app, world, wire,
                                                                                     monkeypatch):  # noqa: F811
    """pages.py send_post: the community's followers and the author's piefed/mbin followers get the community's
    Announce; any other follower (Mastodon here) the author's own Create."""
    piefed_fan = make_user(_peer('pf.example', 'piefed'), 'pia')
    masto_fan = make_user(_peer('masto.example', 'mastodon'), 'max')
    for fan in (piefed_fan, masto_fan):
        make_follow(world.sam, fan, is_inward=True)

    _, post = _act(app, world.shop, world.sam,
                   lambda: shared_make_post(_api_input(title='from the shop'), world.pit, POST_TYPE_ARTICLE, SRC_API,
                                            auth=bearer(world.sam)))

    stored = db.session.get(Post, post.id)
    assert stored.community_id == world.pit.id and stored.ap_id.startswith(f'{SHOP_BASE}/')
    _nothing_hosted(wire)

    by_inbox = {url: (body, key_id, sent_request) for url, body, key_id, sent_request in _sent(wire)}
    assert sorted(by_inbox) == sorted([PEER_INBOX, PIEFED_INBOX, MASTO_INBOX])
    assert len(wire.calls) == 3
    for inbox in (PEER_INBOX, PIEFED_INBOX):
        body, key_id, sent_request = by_inbox[inbox]
        assert body['type'] == 'Announce' and body['actor'] == world.pit.public_url()
        assert body['id'].startswith(f'{HELL_BASE}/')
        assert key_id == f'{world.pit.public_url()}#main-key'
        assert _verifies_at(monkeypatch, app, sent_request, world.pit.public_key) is True
        assert body['object']['type'] == 'Create'
        assert body['object']['actor'] == f'{SHOP_BASE}/u/sam'
        assert body['object']['id'].startswith(f'{SHOP_BASE}/')
        assert body['object']['object']['id'] == stored.ap_id
    body, key_id, sent_request = by_inbox[MASTO_INBOX]
    assert body['type'] == 'Create' and body['actor'] == f'{SHOP_BASE}/u/sam'
    assert body['id'].startswith(f'{SHOP_BASE}/')
    assert key_id == f'{SHOP_BASE}/u/sam#main-key'
    assert _verifies_at(monkeypatch, app, sent_request, world.sam.public_key) is True
    assert body['object']['id'] == stored.ap_id
