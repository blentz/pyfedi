"""ActivityPub and WebFinger routes find a local name on its own domain (plan C1, task 3). `bob` and `art` are on
The first domain, the onion and a second FEDERATING domain, shop.example. The shop's rows come first in the tables (the
updates in `twins` keep that order), so a lookup that ignores the domain finds them and, the shop federating, serves
their document instead of a 404. Each shop row differs from hell's in what the document says (its public URL, title,
member count), so serving the wrong one shows."""
import json

import pytest

from app import cache, db
from app.activitypub.actor import find_actor_by_url
from app.models import CommunityMember, SiteDomain
from app.storefront import clear_domain_cache, domain_for_host
from tests.test_onion_guard_limits import PROXY, a_session, onion_get
from tests.two_domains import HELL, ONION, default_request, no_http, onion_request, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')

AP = {'Accept': 'application/activity+json'}
SHOP = 'shop.example'


@pytest.fixture
def twins(app, two_domains):
    hell, onion = two_domains.domains
    db.session.add(SiteDomain(domain=SHOP, scheme='https', federates=True))   # the admin form refuses one until C3
    db.session.commit()
    clear_domain_cache()
    shop = domain_for_host(SHOP)
    shop_bob, shop_art = two_domains.local_user('bob', shop), two_domains.local_community('art', shop)
    onion_bob, onion_art = two_domains.local_user('bob', onion), two_domains.local_community('art', onion)
    hell_bob, hell_art = two_domains.local_user('bob', hell), two_domains.local_community('art', hell)
    for user in (shop_bob, onion_bob, hell_bob):
        user.ap_public_url = user.ap_profile_id   # as creation stores it: the document names its own domain's URL
    for community, title in ((shop_art, 'Shop art'), (onion_art, 'Onion art'), (hell_art, 'Hell art')):
        community.title = title
    db.session.add(CommunityMember(user_id=two_domains.local_user('fan', shop).id, community_id=shop_art.id))   # the shop's has a follower
    db.session.commit()
    return hell_bob, hell_art, onion_bob, onion_art, shop_bob, shop_art


def get(app, path, host=HELL, headers=AP):
    client = app.test_client()
    if host == ONION:   # through the onion guard: a session first, as a browser does it
        a_session(client)
        return onion_get(client, path, headers=headers)
    return client.get(path, headers=headers, base_url=f'https://{host}')


@pytest.fixture(autouse=True)
def onion_proxy(app, monkeypatch):
    monkeypatch.setitem(app.config, 'ONION_PROXY_IP', PROXY)


@pytest.mark.parametrize('host, home, title, followers', [(HELL, HELL, 'Hell art', 0), (SHOP, SHOP, 'Shop art', 1)])
@pytest.mark.parametrize('path', ['/u/bob', '/u/bob/outbox', '/u/bob/followers', '/u/bob/following', '/c/art',
                                  '/c/art/outbox', '/c/art/featured', '/c/art/moderators', '/c/art/followers',
                                  '/c/art/following'])
def test_each_federating_domains_ap_documents_name_its_own_row(app, twins, path, host, home, title, followers):
    """A bare name on either host is that host's row, so whichever row the unfiltered lookup picked, one host fails."""
    response = get(app, path, host=host)
    assert response.status_code == 200
    document = json.loads(response.data)
    if path.startswith('/u/'):
        assert document['id'].startswith(f'https://{home}/u/bob')
    if path == '/c/art':
        assert document['name'] == title
    if path == '/c/art/followers':
        assert document['totalItems'] == followers


def test_webfinger_answers_for_the_hosts_own_account(app, twins):
    hell_bob = twins[0]

    def answer():
        cache.clear()
        response = get(app, f'/.well-known/webfinger?resource=acct:bob@{HELL}', headers={})
        return response.status_code, response.data

    both = answer()
    db.session.delete(twins[4])
    db.session.delete(twins[2])
    db.session.commit()
    alone = answer()
    assert both == alone and both[0] == 200
    assert json.loads(both[1])['links'][1]['href'] == hell_bob.public_url()


def test_webfinger_for_a_community_names_its_domains_row(app, twins):
    cache.clear()
    response = get(app, f'/.well-known/webfinger?resource=acct:art@{HELL}', headers={})
    assert response.status_code == 200
    assert json.loads(response.data)['links'][1]['href'] == twins[1].public_url()


def test_webfinger_answers_for_a_row_only_on_its_own_federating_domain(app, twins):
    """Each federating domain answers WebFinger for its own rows, named on it, and for no other domain's (plan C3,
    task 4): the shop's bob and art are the shop's to answer, hell's are hell's, and the onion's are no one's."""
    shop_bob, shop_art = twins[4], twins[5]
    for resource, row in ((f'acct:bob@{SHOP}', shop_bob), (f'acct:art@{SHOP}', shop_art),
                          (f'acct:bob@{SHOP.upper()}', shop_bob), (f'http://{SHOP}/u/bob', shop_bob)):
        cache.clear()
        assert get(app, f'/.well-known/webfinger?resource={resource}', headers={}).status_code == 404, resource
        answer = get(app, f'/.well-known/webfinger?resource={resource}', host=SHOP, headers={})
        assert answer.status_code == 200, resource
        assert json.loads(answer.data)['subject'] == f'acct:{"art" if row is shop_art else "bob"}@{SHOP}'
        assert json.loads(answer.data)['links'][1]['href'] == row.public_url()
    for host in (HELL, SHOP):
        assert get(app, f'/.well-known/webfinger?resource=acct:bob@{ONION}', host=host, headers={}).status_code == 404
        assert get(app, f'/.well-known/webfinger?resource=acct:bob@{HELL}', host=host,
                   headers={}).status_code == (200 if host == HELL else 404)


@pytest.mark.parametrize('path', ['outbox', 'featured', 'moderators', 'followers', 'following'])
def test_a_collection_id_is_the_actor_documents_whatever_the_name_is_spelt_like(app, twins, path):
    actor = json.loads(get(app, '/c/art').data)
    for spelt in ('Art', f'art@{HELL}', f'ART@{HELL.upper()}'):
        response = get(app, f'/c/{spelt}/{path}')
        assert response.status_code == 200, spelt
        assert json.loads(response.data)['id'] == actor[path], spelt
    assert actor[path] == f'https://{HELL}/c/art/{path}'


@pytest.mark.parametrize('path', [f'/u/nobody@{ONION}', f'/c/nobody@{ONION}', f'/u/nobody@{HELL}'])
def test_a_hosted_handle_with_no_account_is_404_and_fetches_nothing(app, twins, no_http, path):
    assert get(app, path, headers={}).status_code == 404
    assert no_http == []


def test_an_uppercase_handle_resolves(app, twins, no_http):
    ap = get(app, f'/u/BOB@{HELL.upper()}', headers=AP)
    assert ap.status_code == 200 and json.loads(ap.data)['id'] == twins[0].public_url()
    html = get(app, f'/u/BOB@{HELL}', host=ONION, headers={})   # found: a profile page wants a login
    assert html.status_code == 302 and '/auth/login' in html.headers['Location']
    assert no_http == []


def test_a_bare_name_means_the_storefronts_domain(app, two_domains):
    hell, onion = two_domains.domains
    two_domains.local_user('solo', hell)
    two_domains.local_user('duo', onion)
    assert get(app, '/u/solo', host=ONION, headers={}).status_code == 404
    assert get(app, '/u/duo', host=ONION, headers={}).status_code == 302
    assert get(app, '/u/duo', headers={}).status_code == 404


def test_find_actor_by_url_takes_a_bare_name_on_the_storefront(app, twins):
    with default_request(app):
        assert find_actor_by_url('bob') == twins[0]
    with onion_request(app):
        assert find_actor_by_url('BOB') == twins[2]


def test_find_actor_by_url_does_not_borrow_hells_profile_id_on_the_onion(app, two_domains):
    hell, _ = two_domains.domains
    two_domains.local_user('solo', hell)
    with onion_request(app):
        assert find_actor_by_url('solo') is None


@pytest.mark.parametrize('path', ['/u/!', '/c/!', f'/u/a@b@{ONION}'])
def test_a_name_that_names_nothing_is_404_and_fetches_nothing(app, twins, no_http, path):
    assert get(app, path, headers={}).status_code == 404
    assert no_http == []
