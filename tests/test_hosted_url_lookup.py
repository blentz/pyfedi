"""A URL on one of our own domains is looked up in the database, never fetched (plan C4 task 3).

One server hosts every SiteDomain row and SERVER_NAME, over one database, so a post, comment, account, community or
feed named by a URL on any of them is a row here already. `federation_urls.local_object_for_url` finds it, and every
caller that used to fetch such a URL stops at the database: a miss is a miss, never a GET of ourselves. Each test with
a hosted URL watches the HTTP layer (`wire`, as test_no_http_to_hosted.py does) and asserts nothing reached it.
"""
import socket
from types import SimpleNamespace

import httpx
import pytest
import respx
from flask import g, session as flask_session
from flask_wtf.csrf import generate_csrf

import app.federation_urls as federation_urls
import app.shared.tasks.maintenance as maintenance
from app import db
from app.activitypub.util import process_microblog_announce, resolve_remote_post_from_search
from app.community.util import retrieve_mods_and_backfill
from app.constants import APLOG_ANNOUNCE, APLOG_IGNORED, POST_STATUS_REVIEWING, VISIBILITY_DIRECT, VISIBILITY_FOLLOWERS
from app.discovery import backfill, credits, sync
from app.discovery.filters import host_is_excluded
from app.feed.util import initialise_new_communities
from app.models import DiscoveryEntry, DiscoverySync, InstanceChooser, PostBoost, SiteDomain, UserExtraField, utcnow
from app.relays import inbound
from app.storefront import clear_domain_cache
from tests.factories import make_community, make_feed_item, make_follow, make_instance, make_local_feed, make_post, \
    make_post_reply, make_user
from tests.test_no_http_to_hosted import SERVER, _getaddrinfo_without_the_network
from tests.test_relays_inbound import make_relay

pytestmark = pytest.mark.usefixtures('site')

SHOP = 'shop.example'


@pytest.fixture
def wire(monkeypatch):
    """As in test_no_http_to_hosted.py: every outbound HTTP request, answered 202. `wire.calls` is what left."""
    monkeypatch.setattr(socket, 'getaddrinfo', _getaddrinfo_without_the_network)
    with respx.mock(assert_all_called=False) as router:
        yield router.route().mock(return_value=httpx.Response(202, html='<html></html>'))


@pytest.fixture
def env(db_session):
    """hell.cloud (SERVER_NAME) and shop.example, each with an alice and a den: local names are unique per domain
    (C1), so the same name on both is two rows. A shop post in shop's den with a hell reply, a site-wide feed, and a
    legacy hell post whose ap_id is a URL no path form matches."""
    local = make_instance(SERVER, software='piefed')
    if db.session.query(SiteDomain).filter_by(is_default=True).first() is None:
        db.session.add(SiteDomain(domain=SERVER, is_default=True))
    shop_row = SiteDomain(domain=SHOP)
    db.session.add(shop_row)
    db.session.commit()
    clear_domain_cache()
    shop = make_instance(SHOP, software='piefed')
    peer = make_instance('peer.example', software='lemmy')
    hell_alice = make_user(local, 'alice', local=True)   # id 1: make_community's owner
    shop_alice = make_user(shop, 'alice', local=True)
    shop_alice.site_domain_id = shop_row.id
    hell_den = make_community('den')
    shop_den = make_community('den', host=SHOP)
    shop_den.site_domain_id, shop_den.instance_id = shop_row.id, shop.id
    db.session.commit()
    post = make_post(shop_den, shop_alice, None, title='a shop post')
    legacy = make_post(hell_den, hell_alice, f'https://{SERVER}/p/legacy', title='a legacy post')
    reply = make_post_reply(post, hell_alice)
    feed = make_local_feed('reads')
    yield SimpleNamespace(local=local, shop=shop, peer=peer, shop_row=shop_row, hell_alice=hell_alice,
                          shop_alice=shop_alice, hell_den=hell_den, shop_den=shop_den, post=post, legacy=legacy,
                          reply=reply, feed=feed)
    clear_domain_cache()


def lookup(url):
    return federation_urls.local_object_for_url(url)


# -- local_object_for_url ---------------------------------------------------------------------------------------------

def test_every_path_form_resolves_to_its_row_on_either_domain(env, wire):
    p, r = env.post.id, env.reply.id
    expected = {
        f'https://{SHOP}/post/{p}': env.post,
        f'https://{SHOP}/post/{p}/a-slug': env.post,
        f'https://{SHOP}/c/den/p/{p}': env.post,
        f'https://{SHOP}/c/den/p/{p}/a-slug': env.post,
        f'https://{SHOP}/comment/{r}': env.reply,
        f'https://{SERVER}/comment/{r}': env.reply,
        f'https://{SHOP}/u/alice': env.shop_alice,
        f'https://{SERVER}/u/alice': env.hell_alice,
        f'https://{SHOP}/u/Alice/': env.shop_alice,
        f'https://{SERVER}/u/alice@{SHOP}': env.shop_alice,   # a handle in the path names its own domain's row
        f'https://{SHOP}/c/Den': env.shop_den,
        f'https://{SERVER}/c/Den': env.hell_den,
        f'https://{SHOP}/c/den': env.shop_den,                # its stored ap_profile_id
        f'https://{SERVER}/f/reads': env.feed,
        f'https://{SHOP}/f/Reads': env.feed,                  # feeds are named site-wide
        f'HTTP://SHOP.EXAMPLE/post/{p}/': env.post,
    }
    for url, row in expected.items():
        assert lookup(url) is row, url
    assert wire.calls.call_count == 0


def test_a_post_asked_for_on_the_wrong_sister_is_still_that_post(env, wire):
    assert lookup(f'https://{SERVER}/post/{env.post.id}') is env.post
    assert lookup(f'https://{SERVER}/c/den/p/{env.post.id}/slug') is env.post
    assert wire.calls.call_count == 0


def test_a_stored_url_matches_whatever_case_its_host_and_however_it_ends(env, wire):
    assert lookup(f'https://{SERVER.upper()}/p/legacy/') is env.legacy
    assert lookup(f'https://{SERVER}/p/legacy') is env.legacy
    assert wire.calls.call_count == 0


@pytest.mark.parametrize('path', ['/post/999999', '/comment/999999', '/post/99999999999', '/post/2147483648',
                                  '/c/den/p/999999/slug', '/u/nobody', '/c/nothing', '/f/nothing', '/u/a%00b',
                                  '/about', '/post/abc', '/post/1/comment/2/extra', '/', ''])
def test_a_url_of_ours_that_names_nothing_is_none_without_http(env, wire, path):
    assert lookup(f'https://{SHOP}{path}') is None
    assert wire.calls.call_count == 0


@pytest.mark.parametrize('url', ['https://peer.example/post/1', 'https://shop.example.evil/post/1', '', None, 12345,
                                 'https://shop.example:8443/post/1'])
def test_a_url_on_no_domain_of_ours_is_none(env, wire, url):
    assert lookup(url) is None
    assert wire.calls.call_count == 0


def test_a_name_in_a_url_with_userinfo_names_nobody(env, wire):
    """httpx would connect to shop with userinfo, so it is ours (is_hosted_uri), but no domain row's authority names
    it: its ids still resolve, its names do not."""
    assert lookup(f'https://x@{SHOP}/u/alice') is None
    assert lookup(f'https://x@{SHOP}/post/{env.post.id}') is env.post
    for url in (f'https://[@{SHOP}/post/{env.post.id}', f'https://x]@{SHOP}/post/{env.post.id}'):
        assert lookup(url) is None, url   # httpx reads shop as the host; urllib refuses the authority
    assert wire.calls.call_count == 0


# -- resolve_remote_post_from_search: the search page, the discovery view, an inbox Move ------------------------------

def test_a_hosted_post_url_resolves_from_the_database(env, wire):
    assert resolve_remote_post_from_search(f'https://{SHOP}/c/den/p/{env.post.id}/slug') is env.post
    assert wire.calls.call_count == 0


@pytest.mark.parametrize('path', ['/post/999999', '/u/alice', '/comment/{reply}'])
def test_a_hosted_url_that_is_no_post_is_none_without_a_fetch(env, wire, monkeypatch, path):
    fetched = []
    monkeypatch.setattr('app.activitypub.util.remote_object_to_json', lambda *a, **k: fetched.append(a))
    assert resolve_remote_post_from_search(f'https://{SHOP}{path.format(reply=env.reply.id)}') is None
    assert fetched == [] and wire.calls.call_count == 0


def search_for(app, env, address):
    """The retrieve_remote_post page's answer to `address`, asked by hell's alice."""
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(env.hell_alice.id)
        sess['_fresh'] = True
    with app.test_request_context():
        g.pop('csrf_token', None)   # flask-wtf keeps a token on g, which outlives one request in the test's context
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as sess:
        sess['csrf_token'] = raw
    response = client.post('/retrieve_remote_post', data={'address': address, 'csrf_token': token})
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_search_for_a_sister_post_url_shows_the_post(app, env, wire):
    page = search_for(app, env, f'https://{SHOP}/c/den/p/{env.post.id}/slug')
    assert 'a shop post' in page and 'Post not found.' not in page
    assert wire.calls.call_count == 0


def _deleted(env):
    env.post.deleted = True


def _followers_only(env):
    env.post.visibility = VISIBILITY_FOLLOWERS


def _direct(env):
    env.post.visibility = VISIBILITY_DIRECT


def _private_community(env):
    env.shop_den.private = True


def _local_only_community(env):
    env.shop_den.local_only = True


def _local_only_post(env):
    env.post.local_only = True


def _unpublished(env):
    env.post.status = POST_STATUS_REVIEWING


@pytest.mark.parametrize('hide', [_deleted, _followers_only, _direct, _private_community, _local_only_community,
                                  _local_only_post, _unpublished])
def test_a_hosted_post_no_peer_may_fetch_is_not_found_by_url(app, env, wire, hide):
    """Our ActivityPub post route serves none of these to a peer, so a search by its URL, which before C4 fetched
    that route, finds nothing either (security review of dbbb74c91)."""
    hide(env)
    db.session.commit()
    for url in (f'https://{SHOP}/post/{env.post.id}', f'https://{SERVER}/c/den/p/{env.post.id}/slug'):
        page = search_for(app, env, url)
        assert 'Post not found.' in page and 'a shop post' not in page, url
        assert resolve_remote_post_from_search(url) is None
    assert wire.calls.call_count == 0


def test_the_discovery_view_never_opens_a_hosted_post(app, env, wire, monkeypatch):
    """The discovery view resolves wider-network results only: a hosted URL is refused before any lookup (a 400 for
    a host the video cleaner refuses, else the excluded-host redirect back to the search)."""
    env.post.visibility = VISIBILITY_FOLLOWERS
    db.session.commit()
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', False)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(env.hell_alice.id)
        sess['_fresh'] = True
    response = client.post('/discovery/video/resolve', data={'url': f'https://{SHOP}/post/{env.post.id}'})
    assert response.status_code in (302, 400)
    assert f'/post/{env.post.id}' not in response.headers.get('Location', '')
    assert wire.calls.call_count == 0


# -- a boost of a hosted object ---------------------------------------------------------------------------------------

@pytest.fixture
def booster(env):
    remote = make_user(env.peer, 'booster')
    remote.ap_fetched_at = utcnow()   # fresh: its own profile is not refreshed over HTTP mid-test
    make_follow(env.hell_alice, remote)
    return remote


@pytest.fixture
def ap_logs(monkeypatch):
    calls = []
    monkeypatch.setattr('app.activitypub.util.log_incoming_ap',
                        lambda id, kind, result, saved_json, message=None, session=None: calls.append(
                            (kind, result, message)))
    return calls


def boost(booster, uri):
    return {'id': f'{booster.ap_profile_id}/statuses/1/activity', 'type': 'Announce',
            'actor': booster.ap_profile_id, 'object': uri}


def test_a_boost_of_a_sister_post_by_its_path_is_recorded_without_a_fetch(env, wire, booster, ap_logs):
    found = process_microblog_announce(boost(booster, f'https://{SHOP}/post/{env.post.id}'), 'b1', False)
    assert found is env.post
    assert PostBoost.query.filter_by(post_id=env.post.id, user_id=booster.id).count() == 1
    assert wire.calls.call_count == 0


@pytest.mark.parametrize('hide', [_local_only_post, _unpublished])
def test_a_boost_by_path_of_a_post_we_would_not_serve_is_ignored(env, wire, booster, ap_logs, hide):
    """Before C4 the URL was fetched from our own post route, which refuses a local-only (404) or unpublished (403)
    post: a peer's boost of one by its guessable URL records nothing (review I1)."""
    hide(env)
    db.session.commit()
    assert process_microblog_announce(boost(booster, f'https://{SHOP}/post/{env.post.id}'), 'b3', False) is None
    assert ap_logs == [(APLOG_ANNOUNCE, APLOG_IGNORED, 'Boosted post is not served to other servers')]
    assert PostBoost.query.count() == 0
    assert wire.calls.call_count == 0


def test_a_boost_of_a_hosted_object_we_lack_is_dropped_without_a_fetch(env, wire, booster, ap_logs):
    uri = f'https://{SHOP}/post/999999'
    assert process_microblog_announce(boost(booster, uri), 'b2', False) is None
    assert ap_logs == [(APLOG_ANNOUNCE, APLOG_IGNORED, f'Boosted object is on a domain of ours and not here: {uri}')]
    assert wire.calls.call_count == 0


# -- a relayed object -------------------------------------------------------------------------------------------------

def test_a_relayed_hosted_object_we_lack_is_dropped_without_a_fetch(env, wire, monkeypatch):
    lines = []
    monkeypatch.setattr(inbound, 'log_incoming_ap', lambda *args: lines.append(args))
    uri = f'https://{SHOP}/post/999999'
    inbound.process_relayed_announce(make_relay().id, uri)
    assert [line[2:] for line in lines] == [(APLOG_IGNORED, None, 'relayed object on a domain of ours, not here')]
    assert wire.calls.call_count == 0


def test_a_relayed_hosted_object_we_have_is_left_alone(env, wire, monkeypatch):
    lines = []
    monkeypatch.setattr(inbound, 'log_incoming_ap', lambda *args: lines.append(args))
    inbound.process_relayed_announce(make_relay().id, f'https://{SHOP}/c/den/p/{env.post.id}/slug')
    assert lines == [] and wire.calls.call_count == 0


# -- a local community's moderators, outbox and featured posts --------------------------------------------------------

@pytest.fixture
def fetches(monkeypatch):
    """Every remote_object_to_json the backfill and discovery modules make."""
    calls = []
    for module in ('app.community.util', 'app.discovery.backfill', 'app.discovery.sync'):
        monkeypatch.setattr(f'{module}.remote_object_to_json', lambda *a, **k: calls.append(a))
    return calls


def _with_collections(community, host):
    community.ap_moderators_url = f'https://{host}/c/den/moderators'
    community.ap_outbox_url = f'https://{host}/c/den/outbox'
    community.ap_featured_url = f'https://{host}/c/den/featured'
    db.session.commit()


def test_an_empty_local_community_is_never_backfilled_over_http(env, wire, fetches):
    _with_collections(env.shop_den, SHOP)
    assert retrieve_mods_and_backfill(env.shop_den.id, SHOP, 'den') is None
    assert fetches == [] and wire.calls.call_count == 0


def test_an_empty_local_community_added_to_a_feed_is_not_fetched(env, wire, fetches):
    _with_collections(env.hell_den, SERVER)
    env.hell_den.post_count = 0
    env.feed.num_communities = 1
    make_feed_item(env.feed, env.hell_den)
    db.session.commit()
    initialise_new_communities(env.feed)
    assert fetches == [] and wire.calls.call_count == 0


# -- discovery --------------------------------------------------------------------------------------------------------

def test_our_own_hosts_are_excluded_from_discovery(env):
    assert host_is_excluded(SHOP, frozenset())
    assert host_is_excluded(SERVER, frozenset())
    assert host_is_excluded('SHOP.EXAMPLE', frozenset())
    assert not host_is_excluded('peer.example', frozenset())


def test_discovery_backfill_of_a_hosted_community_is_skipped(env, wire, fetches):
    assert backfill.run_backfill(env.shop_den.id) is None
    assert fetches == [] and wire.calls.call_count == 0


@pytest.mark.parametrize('actor_url', [f'https://{SHOP}/c/den', f'https://{SHOP}/c/gone'])
def test_discovery_sync_never_adds_a_hosted_community(env, wire, fetches, monkeypatch, actor_url):
    monkeypatch.setattr(sync, 'find_actor_or_create', lambda *a, **k: pytest.fail('must not resolve our own URL'))
    entry = DiscoveryEntry(kind='community', platform='peertube', actor_url=actor_url, name='den', host=SHOP,
                           source='test')
    db.session.add(entry)
    db.session.commit()
    assert sync._add(entry, {}) is False
    assert db.session.query(DiscoverySync).count() == 0
    assert fetches == [] and wire.calls.call_count == 0


# -- podcast credits --------------------------------------------------------------------------------------------------

PODCAST = SimpleNamespace(ap_profile_id='https://pod.example/@show', ap_public_url=None)


@pytest.fixture
def actor_fetches(monkeypatch):
    calls = []
    monkeypatch.setattr(credits, 'fetch_actor_document', lambda url: calls.append(url))
    return calls


def test_a_credit_naming_a_sister_account_is_that_account(env, wire, actor_fetches):
    db.session.add(UserExtraField(user_id=env.shop_alice.id, label='podcast', text=PODCAST.ap_profile_id))
    db.session.commit()
    assert credits.verified_credit_user(f'https://{SHOP}/u/alice', PODCAST) == env.shop_alice.id
    assert credits.verified_credit_user(f'https://{SERVER}/u/alice', PODCAST) is None   # hell's alice: no link
    assert actor_fetches == [] and wire.calls.call_count == 0


def test_a_credit_naming_a_hosted_url_we_do_not_store_is_not_fetched(env, wire, actor_fetches):
    assert credits.verified_credit_user(f'https://{SHOP}/@ghost', PODCAST) is None
    assert credits.verified_credit_user(f'https://{SHOP}/post/{env.post.id}', PODCAST) is None
    assert actor_fetches == [] and wire.calls.call_count == 0


# -- the instance chooser ---------------------------------------------------------------------------------------------

def test_the_instance_chooser_never_asks_our_own_domains(env, http_mock, monkeypatch):
    nodes = [{'domain': d, 'uptime_alltime': 99, 'monthsmonitored': 12} for d in (SHOP, SERVER, 'peer.example')]
    http_mock.post('https://api.fediverse.observer/').respond(200, json={'data': {'nodes': nodes}})
    http_mock.get('https://peer.example/api/alpha/site/instance_chooser').respond(200, json={'nsfw': False})
    asked = []
    real_get_request = maintenance.get_request

    def recording_get_request(url, *args, **kwargs):
        asked.append(url)
        return real_get_request(url, *args, **kwargs)

    monkeypatch.setattr(maintenance, 'get_request', recording_get_request)
    maintenance.refresh_instance_chooser()
    assert asked == ['https://peer.example/api/alpha/site/instance_chooser']
    db.session.expire_all()
    assert {row.domain for row in db.session.query(InstanceChooser)} == {'peer.example'}
    assert all(httpx.URL(str(call.request.url)).host == 'peer.example' or 'observer' in str(call.request.url)
               for call in http_mock.calls)
