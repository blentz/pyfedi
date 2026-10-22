"""Each federating domain answers WebFinger, host-meta, NodeInfo and the site documents for itself (plan C3, task 4).

shop.example federates beside hell.cloud and holds a goldie and a goldart of its own (`shop_rows`). Owner ruling:
nothing a sister domain publishes names hell.cloud, so with Site.name what prod's is ('hell.cloud', `site_named_hell`)
no shop body holds hell.cloud's host at all: the shop's site name is its own domain. WebFinger answers only for a row
on the domain the request came in on, NodeInfo counts only that domain's accounts and content, and every cached copy is
keyed by the host, so neither domain is ever answered with the other's. hell.cloud's own documents stay their golden
copies (tests/test_c3_golden_documents.py). The onion is local-only: it answers none of these to anyone."""
import json

import pytest
from flask import g

import app.api.alpha.routes as alpha_routes
from app import cache, db
from app.api.alpha.views import site_view
from app.models import BannedInstances, Instance, Site, SiteDomain
from app.storefront import clear_domain_cache, domain_for_host
from app.utils import get_setting
from tests.factories import make_post
from tests.test_c3_golden_documents import AP, DISCOVERY_PATHS, GOLDEN, SHOP, golden_site  # noqa: F401  (fixture)
from tests.test_onion_guard_limits import PROXY, a_session, onion_get
from tests.test_per_domain_actor_documents import SHOP_BASE, shop_rows, site_named_hell  # noqa: F401  (fixtures)
from tests.two_domains import HELL, ONION, request_on, two_domains  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures('golden_site', 'shop_rows', 'site_named_hell')


@pytest.fixture(autouse=True)
def onion_proxy(app, monkeypatch):
    monkeypatch.setitem(app.config, 'ONION_PROXY_IP', PROXY)


def get(app, path, host=SHOP):
    return app.test_client().get(path, base_url=f'https://{host}')


def shop_body(app, path) -> bytes:
    response = get(app, path)
    assert response.status_code == 200, path
    assert HELL.encode() not in response.data   # hell.cloud's host is nowhere in the shop's answer
    return response.data


def webfinger(resource: str) -> str:
    return f'/.well-known/webfinger?resource={resource}'


# --------------------------------------------------------------------------
# WebFinger
# --------------------------------------------------------------------------

def test_the_shops_account_answers_webfinger_with_the_shops_subject_and_links(app):
    answer = json.loads(shop_body(app, webfinger(f'acct:goldie@{SHOP}')))
    assert answer == {
        'subject': f'acct:goldie@{SHOP}',
        'aliases': [f'{SHOP_BASE}/u/goldie'],
        'links': [
            {'rel': 'http://webfinger.net/rel/profile-page', 'type': 'text/html', 'href': f'{SHOP_BASE}/u/goldie'},
            {'rel': 'self', 'type': 'application/activity+json', 'href': f'{SHOP_BASE}/u/goldie',
             'properties': {'https://www.w3.org/ns/activitystreams#type': 'Person'}},
            {'rel': 'https://w3id.org/fep/3b86/Create', 'template': f'{SHOP_BASE}/share?url={{object}}'},
        ]}


@pytest.mark.parametrize('resource', [f'acct:goldart@{SHOP}', f'{SHOP_BASE}/c/goldart'])
def test_the_shops_community_answers_webfinger_with_the_shops_subject_and_links(app, resource):
    answer = json.loads(shop_body(app, webfinger(resource)))
    assert answer['subject'] == f'acct:goldart@{SHOP}'
    assert answer['aliases'] == [f'{SHOP_BASE}/c/goldart']
    assert [link.get('href') for link in answer['links']] == [f'{SHOP_BASE}/c/goldart'] * 2 + [None]
    assert answer['links'][1]['properties'] == {'https://www.w3.org/ns/activitystreams#type': 'Group'}
    assert answer['links'][2] == {'rel': 'https://w3id.org/fep/3b86/Follow', 'template': '{object}/subscribe'}


def test_the_shops_instance_actor_answers_webfinger_for_the_shop(app):
    answer = json.loads(shop_body(app, webfinger(f'acct:{SHOP}@{SHOP}')))
    assert answer == {
        'subject': f'acct:{SHOP}@{SHOP}',
        'aliases': [f'{SHOP_BASE}/actor'],
        'links': [
            {'rel': 'http://webfinger.net/rel/profile-page', 'type': 'text/html', 'href': f'{SHOP_BASE}/about'},
            {'rel': 'self', 'type': 'application/activity+json', 'href': f'{SHOP_BASE}/actor'},
        ]}


@pytest.mark.parametrize('host, resource', [
    (HELL, f'acct:goldie@{SHOP}'), (HELL, f'acct:goldart@{SHOP}'), (HELL, f'{SHOP_BASE}/u/goldie'),
    (HELL, f'acct:{SHOP}@{SHOP}'),
    (SHOP, f'acct:goldie@{HELL}'), (SHOP, f'acct:goldart@{HELL}'), (SHOP, f'https://{HELL}/u/goldie'),
    (SHOP, f'acct:{HELL}@{HELL}'), (SHOP, f'acct:{SHOP}@{HELL}'), (HELL, f'acct:{HELL}@{SHOP}'),
    # feeds stay on the default domain (C1 ruling): the shop has none, whatever its name
    (SHOP, f'acct:~goldfeed@{SHOP}'), (SHOP, f'acct:goldfeed@{SHOP}'), (SHOP, f'acct:~goldfeed@{HELL}'),
])
def test_webfinger_answers_only_for_a_row_on_the_domain_asked(app, host, resource):
    """A resource on another of our domains is that domain's to answer: asked of this one it is a 404, as an unknown
    account is. Replaces C1's test_webfinger_answers_only_for_the_default_domain."""
    assert get(app, webfinger(resource), host=host).status_code == 404


@pytest.mark.parametrize('resource', [f'acct:goldie@{HELL}', f'acct:goldie@{SHOP}', f'acct:{ONION}@{ONION}',
                                      f'acct:goldie@{ONION}'])
def test_the_onion_answers_no_webfinger(app, two_domains, resource):
    two_domains.local_user('goldie', two_domains.domains[1])
    client = app.test_client()
    a_session(client)
    assert onion_get(client, webfinger(resource), headers={}).status_code == 404
    assert get(app, webfinger(resource), host=HELL).status_code == (200 if HELL in resource else 404)


@pytest.mark.parametrize('first, second', [(SHOP, HELL), (HELL, SHOP)])
def test_a_webfinger_answer_is_remembered_per_host(app, first, second):
    """The same resource asked of both hosts: each host's own answer, whichever is asked first (Review Focus 3)."""
    expected = {SHOP: 200, HELL: 404}
    for host in (first, second, first):
        assert get(app, webfinger(f'acct:goldie@{SHOP}'), host=host).status_code == expected[host], host


# --------------------------------------------------------------------------
# host-meta, NodeInfo and the site documents
# --------------------------------------------------------------------------

def test_the_shops_host_meta_points_at_the_shops_webfinger(app):
    template = f'{SHOP_BASE}/.well-known/webfinger?resource={{uri}}'
    assert f'template="{template}"'.encode() in shop_body(app, '/.well-known/host-meta')
    assert json.loads(shop_body(app, '/.well-known/host-meta.json')) == {'links': [{'rel': 'lrdd',
                                                                                     'template': template}]}


def test_the_shops_nodeinfo_index_links_the_shops_nodeinfo(app):
    links = json.loads(shop_body(app, '/.well-known/nodeinfo'))['links']
    assert [link['href'] for link in links] == [f'{SHOP_BASE}/nodeinfo/2.0', f'{SHOP_BASE}/nodeinfo/2.1']


@pytest.fixture
def shop_post(app, golden_site, shop_rows):
    """A post by the shop's goldie: the shop's post count differs from hell's."""
    shop, user, community = shop_rows
    make_post(community, user, f'{SHOP_BASE}/post/1')
    cache.clear()


@pytest.mark.usefixtures('shop_post')
@pytest.mark.parametrize('path', ['/nodeinfo/2.0', '/nodeinfo/2.1', '/nodeinfo/2.0.json', '/nodeinfo/2.1.json'])
def test_nodeinfo_counts_each_domains_own_accounts_and_content(app, path):
    shop = json.loads(shop_body(app, path))['usage']
    hell = json.loads(get(app, path, host=HELL).data)['usage']
    assert shop == {'users': {'total': 1, 'activeHalfyear': 1, 'activeMonth': 1}, 'localPosts': 1,
                    'localComments': 0}
    # hell counts its own goldie only, not the shop's as well (it counted every federating domain's before Task 4)
    assert hell == {'users': {'total': 1, 'activeHalfyear': 1, 'activeMonth': 1}, 'localPosts': 0,
                    'localComments': 0}


@pytest.mark.usefixtures('shop_post')
def test_the_shops_mastodon_instance_document_is_the_shops(app):
    assert json.loads(shop_body(app, '/api/v1/instance')) == {
        'title': SHOP, 'uri': SHOP, 'stats': {'user_count': 1, 'status_count': 1, 'domain_count': 1},
        'registrations': False, 'approval_required': False}
    assert json.loads(get(app, '/api/v1/instance', host=HELL).data)['stats']['status_count'] == 0


@pytest.mark.usefixtures('shop_post')
def test_the_shops_lemmy_site_document_is_the_shops(app, shop_rows):
    shop = shop_rows[0]
    document = json.loads(shop_body(app, '/api/v3/site'))
    site = document['site_view']['site']
    assert site['name'] == SHOP
    assert site['actor_id'] == f'{SHOP_BASE}/'
    assert site['inbox_url'] == f'{SHOP_BASE}/inbox'
    assert site['icon'] == f'{SHOP_BASE}/static/images/piefed_logo_icon_t_75.png'
    assert site['public_key'] == db.session.get(SiteDomain, shop.id).public_key and site['public_key']
    assert site['description'] == 'Fixed for the golden copies'   # the description is shared
    counts = document['site_view']['counts']
    assert (counts['users'], counts['posts'], counts['comments'], counts['communities']) == (1, 1, 0, 1)
    assert document['admins'] == []   # hell's admin is hell's account: the shop names none of hell's


def test_the_alpha_site_view_names_the_shop(app, shop_rows):
    with request_on(app, shop_rows[0]):
        g.site = db.session.get(Site, 1)
        site = site_view(None)['site']
    assert site['name'] == SHOP
    assert site['actor_id'] == f'{SHOP_BASE}/'
    assert site['icon'] == f'{SHOP_BASE}/static/images/piefed_logo_icon_t_75.png'
    with request_on(app, domain_for_host(HELL)):
        g.site = db.session.get(Site, 1)
        hell_site = site_view(None)['site']
    assert (hell_site['name'], hell_site['actor_id']) == (HELL, f'https://{HELL}/')


CACHED = ['/.well-known/host-meta', '/.well-known/host-meta.json', '/.well-known/nodeinfo', '/nodeinfo/2.0',
          '/nodeinfo/2.0.json', '/nodeinfo/2.1', '/nodeinfo/2.1.json', '/api/v1/instance', '/api/v3/site']


@pytest.mark.parametrize('path', CACHED)
@pytest.mark.parametrize('shop_first', [True, False])
def test_each_host_gets_its_own_cached_copy(app, path, shop_first):
    """Review Focus 3: two hosts in a row each get their own body, in either order, and again from the cache."""
    bodies = {}
    for host in ((SHOP, HELL) if shop_first else (HELL, SHOP)) * 2:
        response = get(app, path, host=host)
        assert response.status_code == 200, (host, path)
        assert bodies.setdefault(host, response.data) == response.data
    assert HELL.encode() not in bodies[SHOP]
    assert SHOP.encode() not in bodies[HELL]


@pytest.mark.parametrize('path', CACHED)
def test_the_onion_answers_no_discovery_document(app, path):
    """The onion federates with no one, so it describes no server to one: 404, as its instance actor is."""
    client = app.test_client()
    a_session(client)
    assert onion_get(client, path, headers={}).status_code == 404
    assert get(app, path, host=HELL).status_code == 200   # and the 404 is not hell's cached answer


@pytest.mark.parametrize('name, path', sorted(DISCOVERY_PATHS.items()))
def test_hells_discovery_documents_stay_their_golden_copies_beside_the_shops(app, name, path):
    """hell's answers are unchanged by the shop's goldie and goldart (the counts are hell's own: one account), with
    the site named as the golden copies were captured."""
    db.session.get(Site, 1).name = 'Golden Site'
    db.session.commit()
    cache.clear()
    if 'goldfeed' not in path:   # the shop has no feed
        shop_body(app, path.replace(HELL, SHOP))   # the shop's answer, cached first
    response = get(app, path, host=HELL)
    assert response.status_code == 200
    assert response.data == (GOLDEN / name).read_bytes()


# --------------------------------------------------------------------------
# Task 4 fix round 1
# --------------------------------------------------------------------------

@pytest.fixture
def alpha_api(app, monkeypatch):
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    monkeypatch.setattr(alpha_routes, 'get_setting', lambda name, default=None:
                        True if name == 'enable_instance_chooser' else get_setting(name, default))


def alpha_get(app, path, host):
    g.site = db.session.get(Site, 1)
    response = get(app, path, host=host)
    assert response.status_code == 200, (path, host, response.status_code)
    return response


@pytest.mark.usefixtures('alpha_api')
def test_the_shops_alpha_site_documents_name_only_the_shop(app, two_domains, shop_rows):
    """I1: the shop names none of hell's admins, and counts its own accounts (two, hell has one)."""
    two_domains.local_user('extra', shop_rows[0])
    site = alpha_get(app, '/api/alpha/site', SHOP)
    assert HELL.encode() not in site.data
    assert site.get_json()['admins'] == []
    assert (site.get_json()['site']['name'], site.get_json()['site']['user_count']) == (SHOP, 2)
    chooser = alpha_get(app, '/api/alpha/site/instance_chooser', SHOP)
    assert HELL.encode() not in chooser.data
    assert (chooser.get_json()['name'], chooser.get_json()['mau']) == (SHOP, 2)
    # hell's are as they were: its own admin, name and counts
    hell_site = alpha_get(app, '/api/alpha/site', HELL).get_json()
    assert [admin['person']['actor_id'] for admin in hell_site['admins']] == [f'https://{HELL}/u/goldie']
    assert (hell_site['site']['name'], hell_site['site']['user_count']) == (HELL, 1)
    hell_chooser = alpha_get(app, '/api/alpha/site/instance_chooser', HELL).get_json()
    assert (hell_chooser['name'], hell_chooser['mau']) == (HELL, 1)
