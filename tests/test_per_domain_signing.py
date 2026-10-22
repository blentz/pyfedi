"""Requests a domain's accounts cause are signed and named as that domain, and an inbound object is ours by its
domain (plan C3, task 5).

Each federating domain is a separate ActivityPub server, so a fetch a shop.example account caused is shop.example's:
its instance actor signs it (keyId `https://shop.example/actor#main-key`) and its User-Agent names shop.example,
never hell.cloud (owner ruling: nothing a sister domain federates names hell.cloud). Relays, housekeeping, the
discovery credits and anything no account caused stay the default domain's, byte for byte what they were. Inbound,
a URL on any federating domain is ours, a local-only one's (the onion) or a lookalike host's never is.

Every outbound request is recorded at the HTTP layer (respx), so a header asserted is the header sent.
"""
import json

import httpx
import pytest
import respx
from flask_login import login_user

from app import db
from app.activitypub import signature_rfc9421
from app.activitypub.actor import fetch_remote_actor_data
from app.activitypub.signature import HttpSignature, parse_signature_header, send_post_request
from app.activitypub.util import process_announce_of_uri, remote_object_to_json, served_by_us
from app.constants import APLOG_DUPLICATE, APLOG_IGNORED
from app.models import ActivityPubLog, Post, Relay, Site, SiteDomain, User
from app.relays.expiry import _local_user_ids
from app.relays.subscribe import add_relay
from app.storefront import acting_domain, acting_for, clear_domain_cache, domain_for_host, is_federating_url
from app.user.utils import search_for_user
from app.utils import authorise_api_user, user_agent
from tests.factories import a_keypair, inbox_activity, make_community, make_instance, make_site, make_user
from tests.two_domains import HELL, ONION, request_on, two_domains  # noqa: F401 -- two_domains is a fixture

SHOP = 'shop.example'
REMOTE = 'https://remote.example'
ACTOR_URL = f'{REMOTE}/u/carol'
# Golden: what hell.cloud has always sent (SERVER_NAME is test.piefed.local), before and after C3
HELL_KEY_ID = 'https://test.piefed.local/actor#main-key'
SHOP_KEY_ID = 'https://shop.example/actor#main-key'


def hell_ua(app):
    return f"PieFed/{app.config['VERSION']}; +https://test.piefed.local"


def shop_ua(app):
    return f"PieFed/{app.config['VERSION']}; +https://shop.example"


def _signing_site():
    """Site row 1, the default domain's instance actor, with a key (signing_peer may have made the row already)."""
    site = db.session.get(Site, 1) or make_site()
    site.private_key, site.public_key = a_keypair()
    db.session.commit()
    return site


class Domains:
    def __init__(self, two):
        hell, onion = two.domains
        db.session.add(SiteDomain(domain=SHOP, scheme='https', federates=True))   # its instance key is made on insert
        db.session.commit()
        clear_domain_cache()
        self.hell, self.onion, self.shop = hell, onion, domain_for_host(SHOP)
        self.hell_user = two.local_user('hellbob', hell)
        self.shop_user = two.local_user('shopbob', self.shop)
        self.onion_user = two.local_user('onionbob', onion)
        for user in (self.hell_user, self.shop_user, self.onion_user):
            user.private_key, user.public_key = a_keypair()
        db.session.commit()


@pytest.fixture
def domains(app, two_domains):  # noqa: F811 -- two_domains is the imported fixture
    _signing_site()
    yield Domains(two_domains)
    clear_domain_cache()


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False) as mock:
        yield mock


def _key_id(request: httpx.Request) -> str:
    if 'signature-input' in request.headers:
        return signature_rfc9421.parse_key_id(request.headers)
    return parse_signature_header(request.headers['Signature'])['keyid']


def _unsigned_then_signed(router, url, document):
    """`url` answers 401 to an unsigned GET and `document` to a signed one."""
    return router.get(url).mock(side_effect=lambda request: httpx.Response(200, json=document)
                                if 'Signature' in request.headers else httpx.Response(401))


def _as(app, user):
    """A request in which `user` (None: nobody) is signed in."""
    context = app.test_request_context()
    context.push()
    if user is not None:
        login_user(user)
    return context


ACTOR_DOCUMENT = {'id': ACTOR_URL, 'type': 'Person', 'preferredUsername': 'carol', 'inbox': f'{ACTOR_URL}/inbox'}


def _fetch_actor():
    return fetch_remote_actor_data(ACTOR_URL)


def _fetch_object():
    return remote_object_to_json(ACTOR_URL, retry_delay=0)


# --------------------------------------------------------------------------
# Outbound: the acting domain signs and names the request
# --------------------------------------------------------------------------


@pytest.mark.parametrize('fetch', [_fetch_actor, _fetch_object], ids=['actor', 'object'])
@pytest.mark.parametrize('who, key_id, ua', [('shop_user', SHOP_KEY_ID, shop_ua), ('hell_user', HELL_KEY_ID, hell_ua),
                                             ('onion_user', HELL_KEY_ID, hell_ua), (None, HELL_KEY_ID, hell_ua)])
def test_a_signed_fetch_is_the_acting_accounts_domains(app, domains, router, fetch, who, key_id, ua):
    """A shop account's fetch is signed by the shop's instance actor and names the shop, both the unsigned first try
    and the signed one. A hell.cloud account's, an onion account's (the onion is local-only: it names nothing) and
    one nobody caused are hell.cloud's, byte for byte as before C3."""
    route = _unsigned_then_signed(router, ACTOR_URL, ACTOR_DOCUMENT)
    context = _as(app, getattr(domains, who) if who else None)
    try:
        assert fetch() == ACTOR_DOCUMENT
    finally:
        context.pop()
    unsigned, signed = (call.request for call in route.calls)
    assert 'Signature' not in unsigned.headers
    assert unsigned.headers['User-Agent'] == signed.headers['User-Agent'] == ua(app)
    assert _key_id(signed) == key_id


def test_with_no_request_the_default_domain_signs(app, domains, router):
    """A Celery task has no request and no account: hell.cloud's instance actor and User-Agent, as before C3."""
    route = _unsigned_then_signed(router, ACTOR_URL, ACTOR_DOCUMENT)
    assert _fetch_actor() == ACTOR_DOCUMENT
    signed = route.calls[-1].request
    assert _key_id(signed) == HELL_KEY_ID and signed.headers['User-Agent'] == hell_ua(app)


def test_a_shop_accounts_search_is_signed_by_the_shop(app, domains, router):
    """Finding a remote account by its handle (search_for_user), WebFinger and the actor fetch both."""
    webfinger = router.get(f'{REMOTE}/.well-known/webfinger').respond(200, json={
        'links': [{'rel': 'self', 'type': 'application/activity+json', 'href': ACTOR_URL}]})
    actor = router.get(ACTOR_URL).mock(side_effect=lambda request: httpx.Response(
        404 if 'Signature' in request.headers else 401))
    context = _as(app, domains.shop_user)
    try:
        assert search_for_user('carol@remote.example') is None
    finally:
        context.pop()
    assert webfinger.calls.last.request.headers['User-Agent'] == shop_ua(app)
    unsigned, signed = (call.request for call in actor.calls)
    assert unsigned.headers['User-Agent'] == signed.headers['User-Agent'] == shop_ua(app)
    assert _key_id(signed) == SHOP_KEY_ID


def test_a_search_with_no_instance_key_fetches_nothing_signed(app, domains, router):
    db.session.get(Site, 1).private_key = None
    db.session.commit()
    router.get(f'{REMOTE}/.well-known/webfinger').respond(200, json={
        'links': [{'rel': 'self', 'type': 'application/activity+json', 'href': ACTOR_URL}]})
    actor = router.get(ACTOR_URL).respond(401)
    assert search_for_user('carol@remote.example') is None
    assert [call.request.headers.get('Signature') for call in actor.calls] == [None]


def test_an_api_caller_acts_as_its_domain(app, domains):
    """A client app's call (authorise_api_user) is the account's, as a signed-in browser's is."""
    with app.test_request_context():
        assert acting_domain() == domains.hell
        authorise_api_user(f'Bearer {domains.shop_user.encode_jwt_token()}')
        assert acting_domain() == domains.shop
        assert user_agent() == shop_ua(app)


def test_an_acting_for_block_wins_and_a_local_only_domain_acts_as_the_default(app, domains):
    with app.test_request_context():
        login_user(domains.shop_user)
        with acting_for(domains.hell):
            assert acting_domain() == domains.hell
        with acting_for(domains.onion):   # the onion makes no request named by it
            assert acting_domain() == domains.hell
        assert acting_domain() == domains.shop


RELAY_ACTOR = 'https://relay.example/actor'


def test_a_relay_subscription_is_the_default_domains_whoever_adds_it(app, domains, router):
    """Relays are the default domain's only (spec): a shop admin adding one still fetches and follows it as
    hell.cloud's instance actor."""
    fetched = router.get(RELAY_ACTOR).respond(200, json={
        'id': RELAY_ACTOR, 'type': 'Application', 'inbox': 'https://relay.example/inbox',
        'publicKey': {'id': f'{RELAY_ACTOR}#main-key', 'publicKeyPem': a_keypair()[1]}})
    followed = router.post('https://relay.example/inbox').respond(202)
    context = _as(app, domains.shop_user)
    try:
        relay = add_relay(RELAY_ACTOR)
    finally:
        context.pop()
    assert db.session.get(Relay, relay.id).actor_id == RELAY_ACTOR
    assert fetched.calls.last.request.headers['User-Agent'] == hell_ua(app)
    follow = followed.calls.last.request
    assert _key_id(follow) == HELL_KEY_ID and follow.headers['User-Agent'] == hell_ua(app)
    assert json.loads(follow.content)['actor'] == 'https://test.piefed.local/actor'


@pytest.mark.parametrize('who, ua', [('shop_user', shop_ua), ('hell_user', hell_ua)])
def test_an_accounts_own_delivery_names_its_domain(app, domains, router, who, ua):
    """A delivery signed by an account's own key names the signer's domain, whoever is signed in: the User-Agent
    and the keyId never disagree."""
    user = getattr(domains, who)
    inbox = router.post(f'{REMOTE}/inbox').respond(202)
    activity = {'id': f'{user.public_url()}/activities/like/1', 'type': 'Like', 'actor': user.public_url(),
                'object': f'{REMOTE}/post/1'}
    context = _as(app, domains.hell_user if who == 'shop_user' else domains.shop_user)
    try:
        send_post_request(f'{REMOTE}/inbox', activity, user.private_key, f'{user.public_url()}#main-key')
    finally:
        context.pop()
    sent = inbox.calls.last.request
    assert _key_id(sent) == f'{user.public_url()}#main-key'
    assert sent.headers['User-Agent'] == ua(app)


def test_with_no_instance_key_nothing_is_signed(app, domains, router):
    """MissingInstanceKey: the default domain with no Site key signs nothing and sends nothing signed."""
    db.session.get(Site, 1).private_key = None
    db.session.commit()
    route = router.get(ACTOR_URL).respond(401)
    assert _fetch_actor() is None
    assert remote_object_to_json(ACTOR_URL, retry_delay=0) is None
    assert all('Signature' not in call.request.headers for call in route.calls) and len(route.calls) == 2


# --------------------------------------------------------------------------
# Inbound: is it ours
# --------------------------------------------------------------------------


@pytest.mark.parametrize('url, ours', [
    ('https://test.piefed.local/post/1', True),
    ('https://shop.example/post/1', True),
    ('https://SHOP.example./u/shopbob', True),
    ('https://shop.example:443/u/shopbob', True),
    ('https://shop.example.evil/post/1', False),
    ('https://evil.shop.example/post/1', False),
    ('https://test.piefed.local.evil/post/1', False),
    ('http://abc.onion/post/1', False),        # local-only: never an object a peer may name
    ('https://shop.example:8443/post/1', False),
    ('http://shop.example/post/1', False),        # shop is https: another scheme is not its URL
    ('http://test.piefed.local/post/1', False),   # nor is SERVER_URL's host over another scheme
    ('//shop.example/post/1', False),
    ('javascript://shop.example/post/1', False),
    (None, False),
    ({'id': 'https://shop.example/post/1'}, False),
])
def test_a_url_is_ours_on_every_federating_domain_only(app, domains, url, ours):
    assert is_federating_url(url) is ours


def test_a_shop_post_is_local(app, domains):
    post = Post(user_id=domains.shop_user.id, ap_id=f'https://{SHOP}/post/7')
    post.author = domains.shop_user
    assert post.is_local()
    post.ap_id = f'https://{SHOP}.evil/post/7'
    assert not post.is_local()


def _announce_of(actor_url, signing_peer):
    local_object = {'id': f'{REMOTE}/objects/1', 'type': 'Note', 'actor': actor_url,
                    'object': f'{REMOTE}/objects/1'}
    return inbox_activity(signing_peer, activity_type='Announce', object=local_object)


LOCAL_CONTENT = 'Activity about local content which is already present'


@pytest.mark.parametrize('actor_url, ours', [
    ('https://shop.example/u/shopbob', True),
    ('https://test.piefed.local/u/hellbob', True),
    ('https://shop.example.evil/u/shopbob', False),
    ('http://abc.onion/u/onionbob', False),
])
def test_an_inbound_announce_of_a_shop_post_is_ours(app, domains, signing_peer, monkeypatch, actor_url, ours):
    """The shared inbox drops an Announce of our own content before anything else: a shop account's is ours too,
    a lookalike's or the onion's never is."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    with app.test_client() as client:
        response = client.post('/inbox', json=_announce_of(actor_url, signing_peer))
    assert response.status_code == (200 if ours else 400)   # ours: dropped before the signature check
    messages = [row.exception_message for row in ActivityPubLog.query.all()]
    assert (LOCAL_CONTENT in messages) is ours, messages


@pytest.mark.parametrize('uri, ours', [('https://shop.example/post/7', True),
                                       ('https://shop.example.evil/post/7', False)])
def test_a_community_announce_of_a_shop_post_is_ours(app, domains, monkeypatch, uri, ours):
    logged, resolved = [], []
    monkeypatch.setattr('app.activitypub.util.log_incoming_ap',
                        lambda id, aplog_type, aplog_result, saved_json, message=None, session=None:
                        logged.append((aplog_type, aplog_result, message)))
    monkeypatch.setattr('app.activitypub.util.resolve_remote_post', lambda *args, **kwargs: resolved.append(args))
    community = make_community('news', 'lemmy.example')
    announce = {'id': f'{REMOTE}/activities/announce/1', 'type': 'Announce', 'actor': community.ap_profile_id,
                'object': uri}
    assert process_announce_of_uri(announce, community, announce['id'], False) is None
    assert ((APLOG_DUPLICATE, APLOG_IGNORED, LOCAL_CONTENT) in logged) is ours
    assert bool(resolved) is not ours


def test_a_shop_communitys_own_icon_is_served_by_us(app, domains):
    assert served_by_us(f'https://{SHOP}/static/media/a.png', None)
    assert not served_by_us(f'https://{SHOP}.evil/static/media/a.png', None)
    assert not served_by_us(f'http://{ONION}/static/media/a.png', None)


# --------------------------------------------------------------------------
# Inbound signatures: per federating host
# --------------------------------------------------------------------------


def _follow_of(local_user, sender):
    return inbox_activity(sender, activity_type='Follow', object_uri=local_user.public_url())


@pytest.mark.usefixtures('redis_double')
@pytest.mark.parametrize('host, accepted', [(SHOP, True), (HELL, True), (ONION, False), (f'{SHOP}.evil', False)])
def test_a_signature_for_a_federating_host_verifies_and_one_for_the_onion_does_not(app, domains, signing_peer,
                                                                                    monkeypatch, host, accepted):
    """R-HOSTPIN per federating domain: a peer signs for the domain it posts to. The onion is local-only, no server
    a peer may address, so a signature naming it is refused (cavage, signed Host)."""
    monkeypatch.setitem(app.config, 'DEBUG', True)
    dispatched = []
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request', lambda *a, **kw: dispatched.append(a))
    activity = _follow_of(domains.shop_user, signing_peer)
    _uri, headers, body = HttpSignature.signed_request(f'https://{host}/inbox', activity, signing_peer.private_key,
                                                       f'{signing_peer.ap_profile_id}#main-key', send_via_async=True)
    response = app.test_client().post('/inbox', data=body, headers=headers, content_type='application/activity+json')
    assert (response.status_code == 200) is accepted, response.status_code
    assert len(dispatched) == (1 if accepted else 0)


@pytest.mark.usefixtures('redis_double')
@pytest.mark.parametrize('host, accepted', [(SHOP, True), (ONION, False)])
def test_an_rfc9421_target_is_the_federating_hosts_own(app, domains, signing_peer, monkeypatch, router, host,
                                                       accepted):
    """@target-uri on a federating sister domain is that domain's origin; on the onion it is SERVER_URL's, so a
    signature made for the onion's own URI does not verify (and the signer's key, refetched once, is the same)."""
    monkeypatch.setitem(app.config, 'DEBUG', True)
    router.get(signing_peer.ap_profile_id).respond(404)
    dispatched = []
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request', lambda *a, **kw: dispatched.append(a))
    scheme = 'http' if host == ONION else 'https'
    body = json.dumps(_follow_of(domains.shop_user, signing_peer)).encode('utf8')
    headers = signature_rfc9421.sign('post', f'{scheme}://{host}/inbox', body, signing_peer.private_key,
                                     f'{signing_peer.ap_profile_id}#main-key')
    headers['Host'] = host
    response = app.test_client().post('/inbox', data=body, headers=headers, content_type='application/activity+json')
    assert (response.status_code == 200) is accepted, response.status_code
    assert len(dispatched) == (1 if accepted else 0)


@pytest.mark.usefixtures('redis_double')
def test_a_create_sent_to_both_domains_inboxes_is_processed_once(app, domains, signing_peer, monkeypatch):
    """A peer delivers one Create to each domain whose accounts it addresses: the shop's inbox and hell.cloud's.
    The activity dedupe (keyed on id and actor, not on the host it came in on) lets only the first through: the
    second is answered 200 and logged as ignored, not refused (a signature refusal is a 400). With a dedupe key
    that differed per delivery both were dispatched (checked by mutation)."""
    monkeypatch.setitem(app.config, 'DEBUG', True)
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    dispatched = []
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request', lambda *a, **kw: dispatched.append(a))
    note = {'id': f'{signing_peer.ap_profile_id}/notes/1', 'type': 'Note', 'attributedTo': signing_peer.ap_profile_id,
            'to': [domains.shop_user.public_url(), domains.hell_user.public_url()], 'content': 'hello both'}
    activity = inbox_activity(signing_peer, activity_type='Create', object=note, to=note['to'])
    responses = []
    for host in (SHOP, HELL):
        _uri, headers, body = HttpSignature.signed_request(f'https://{host}/inbox', dict(activity),
                                                           signing_peer.private_key,
                                                           f'{signing_peer.ap_profile_id}#main-key', send_via_async=True)
        responses.append(app.test_client().post('/inbox', data=body, headers=headers,
                                                content_type='application/activity+json'))
    assert [response.status_code for response in responses] == [200, 200]
    assert len(dispatched) == 1
    duplicates = ActivityPubLog.query.filter_by(activity_id=activity['id'], result=APLOG_IGNORED[1]).all()
    assert len(duplicates) == 1


# --------------------------------------------------------------------------
# Fix round 1
# --------------------------------------------------------------------------


@pytest.mark.parametrize('host, key_id, ua', [(SHOP, SHOP_KEY_ID, shop_ua), (HELL, HELL_KEY_ID, hell_ua),
                                              (ONION, HELL_KEY_ID, hell_ua)])
def test_an_anonymous_request_acts_as_the_federating_storefront_it_came_in_on(app, domains, host, key_id, ua):
    """Ruling (a): with no account acting, a request on a federating sister domain acts as that domain (an inbox
    POST to shop.example is shop's to answer); the onion, local-only, acts as the default."""
    with request_on(app, domain_for_host(host)):
        assert acting_domain().domain == (SHOP if host == SHOP else HELL)
        assert user_agent() == ua(app)


@pytest.mark.usefixtures('redis_double')
@pytest.mark.parametrize('host, key_id, ua', [(SHOP, SHOP_KEY_ID, shop_ua), (HELL, HELL_KEY_ID, hell_ua)])
def test_a_rotated_keys_refetch_is_signed_by_the_inbox_it_was_delivered_to(app, domains, signing_peer, router,
                                                                         monkeypatch, host, key_id, ua):
    """A signer whose stored key is stale delivers to shop.example's inbox: the one refetch of its actor is the
    shop instance actor's, named as the shop, never hell.cloud seconds after the shop was written to."""
    monkeypatch.setitem(app.config, 'DEBUG', True)
    dispatched = []
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request', lambda *a, **kw: dispatched.append(a))
    current_pem = signing_peer.public_key
    while signing_peer.public_key == current_pem:   # rotated: the stored key is another pool pair's
        signing_peer.public_key = a_keypair()[1]
    db.session.commit()
    refetch = router.get(signing_peer.ap_profile_id).respond(200, json={
        'id': signing_peer.ap_profile_id, 'type': 'Person',
        'publicKey': {'id': f'{signing_peer.ap_profile_id}#main-key', 'owner': signing_peer.ap_profile_id,
                      'publicKeyPem': current_pem}})
    activity = _follow_of(domains.shop_user, signing_peer)
    _uri, headers, body = HttpSignature.signed_request(f'https://{host}/inbox', activity, signing_peer.private_key,
                                                       f'{signing_peer.ap_profile_id}#main-key', send_via_async=True)
    response = app.test_client().post('/inbox', data=body, headers=headers, content_type='application/activity+json')
    assert response.status_code == 200
    assert len(dispatched) == 1
    sent = refetch.calls.last.request
    assert _key_id(sent) == key_id and sent.headers['User-Agent'] == ua(app)


def test_relay_retention_counts_an_account_local_by_its_url_not_by_a_prefix(app, domains):
    lookalike = make_user(make_instance('test.piefed.local.evil'), 'mallory')
    lookalike.ap_profile_id = 'https://test.piefed.local.evil/u/mallory'
    legacy = make_user(make_instance('legacy.example'), 'legacy')
    legacy.ap_profile_id = 'https://test.piefed.local/u/legacy'
    db.session.commit()
    local = set(db.session.execute(_local_user_ids()).scalars())
    assert legacy.id in local and lookalike.id not in local
    assert domains.shop_user.id in local and db.session.get(User, domains.shop_user.id).ap_id is None


def test_an_api_callers_domain_does_not_outlive_its_request(app, domains):
    """The API caller's domain is the request's, not the app context's: under one app context (a test's, a
    worker's) the next request does not inherit it."""
    with app.app_context():
        with app.test_request_context():
            authorise_api_user(f'Bearer {domains.shop_user.encode_jwt_token()}')
            assert acting_domain() == domains.shop
        with app.test_request_context():
            assert acting_domain() == domains.hell
