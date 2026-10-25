"""A sister's names resolve on the sister, and our own domains pass the allowlist (plan C4 task 4).

The first domain (SERVER_NAME) and shop.example share one database and each has its own `den` (C1). A name written without a
host means the domain acting, a URL means the domain it is on, and a host of ours never needs the instance allowlist
or ban list to be let through: only remote hosts do. No test here may reach the HTTP layer.
"""
import pytest
from flask import current_app, g

from app import db
from app.activitypub.actor import validate_remote_actor
from app.activitypub.util import find_actor_or_create
from app.api.alpha.utils.community import get_community
from app.api.alpha.utils.misc import get_resolve_object
from app.community.util import search_for_community
from app.feed.util import search_for_feed
from app.models import AllowedInstances, BannedInstances, Site
from app.shared.feed import form_communities_to_ids
from app.storefront import acting_for, clear_domain_cache, domain_for_host
from app.utils import set_setting
from tests.test_actor_handles import auth
from tests.test_hosted_url_lookup import SERVER, SHOP, env, wire  # noqa: F401 (fixtures)

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture(autouse=True)
def _site_in_g(app):
    g.site = db.session.get(Site, 1)


@pytest.mark.parametrize('suffix', ['', '/', '/?x=1'])
def test_resolving_a_sisters_community_url_finds_the_sisters_den(env, wire, suffix):
    found = get_resolve_object(None, {'q': f'https://{SHOP}/c/den{suffix}'}, user_id=env.first_alice.id)
    assert found['community']['community']['id'] == env.shop_den.id
    found = get_resolve_object(None, {'q': f'https://{SERVER}/c/den{suffix}'}, user_id=env.first_alice.id)
    assert found['community']['community']['id'] == env.first_den.id
    assert wire.calls.call_count == 0


@pytest.mark.parametrize('query', ['https://[shop.example/c/den/', 'https://[shop.example/c/den', 'http://[::1/c/x'])
def test_resolving_a_malformed_url_is_a_400_never_a_500(env, wire, app, monkeypatch, query):
    monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
    response = app.test_client().get('/api/alpha/resolve_object', query_string={'q': query},
                                     headers=auth(env.first_alice))
    assert response.status_code == 400
    assert wire.calls.call_count == 0


def test_a_bare_community_name_means_the_acting_domain(env, wire):
    with acting_for(domain_for_host(SHOP)):
        assert get_community(None, {'name': 'den'})['community_view']['community']['id'] == env.shop_den.id
        assert form_communities_to_ids('den') == {env.shop_den.id}
    assert get_community(None, {'name': 'den'})['community_view']['community']['id'] == env.first_den.id
    assert form_communities_to_ids('!den') == {env.first_den.id}
    assert wire.calls.call_count == 0


def test_our_own_domains_pass_a_closed_allowlist(env, wire):
    set_setting('use_allowlist', True)
    assert AllowedInstances.query.count() == 0
    assert validate_remote_actor(f'https://{SHOP}/u/alice') is True
    env.shop_alice.ap_profile_id = f'https://{SHOP}/u/alice'   # the fixture's accounts carry none
    env.first_alice.ap_profile_id = f'https://{SERVER}/u/alice'
    db.session.commit()
    assert find_actor_or_create(f'https://{SHOP}/u/alice') is env.shop_alice
    assert find_actor_or_create(f'https://{SERVER}/u/alice') is env.first_alice
    assert validate_remote_actor(f'https://{SHOP}/u/alice') is True
    assert search_for_community(f'!den@{SHOP}') is env.shop_den
    assert search_for_community(f'!den@{SERVER}') is env.first_den
    assert wire.calls.call_count == 0


def test_a_remote_host_off_the_allowlist_is_still_refused(env, wire):
    set_setting('use_allowlist', True)
    assert validate_remote_actor('https://peer.example/u/bob') is False
    assert search_for_community('!x@peer.example') is None
    db.session.add(AllowedInstances(domain='peer.example'))
    db.session.commit()
    assert validate_remote_actor('https://peer.example/u/bob') is True
    assert wire.calls.call_count == 0


def test_our_own_domains_pass_the_ban_list_check(env, wire):
    """An instance row for our own host on the ban list (a mistake, or a stale import) never cuts us off from ourselves."""
    db.session.add(BannedInstances(domain=SHOP))
    db.session.commit()
    assert validate_remote_actor(f'https://{SHOP}/u/alice') is True
    assert search_for_community(f'!den@{SHOP}') is env.shop_den
    db.session.add(BannedInstances(domain='peer.example'))
    db.session.commit()
    assert validate_remote_actor('https://peer.example/u/bob') is False
    assert search_for_community('!x@peer.example') is None


# -- one rule for "this server is ours" in the allowlist and ban gates (C4 final review P2, P3) -----------------------

@pytest.mark.parametrize('url', [f'https://evil.example@{SHOP}/u/alice', f'https://x:y@{SHOP}/u/alice',
                                 f'https://evil.example@{SERVER}/u/alice'])
def test_a_url_with_userinfo_gets_no_exemption_from_the_allowlist(env, wire, url):
    """The exemption reads the authority as named, not the host httpx would connect to: userinfo never counts, so a
    closed allowlist refuses the authority as it refuses any other it does not list."""
    set_setting('use_allowlist', True)
    assert validate_remote_actor(url) is False
    assert wire.calls.call_count == 0


@pytest.mark.parametrize('server', [f'{SHOP}#x', f'{SHOP}/x', f'{SHOP}\\x'])
def test_a_community_server_that_is_no_authority_gets_no_exemption(env, wire, server):
    set_setting('use_allowlist', True)
    assert search_for_community(f'!den@{server}') is None
    assert wire.calls.call_count == 0


@pytest.mark.parametrize('server', [SHOP, f'{SHOP}:443', f'{SHOP}:80', SHOP.upper()])
def test_our_host_on_a_port_it_answers_is_exempt(env, wire, server):
    """The egress port rule: a domain naming no port is ours with none, 80 or 443."""
    set_setting('use_allowlist', True)
    assert validate_remote_actor(f'https://{server}/u/alice') is True
    assert search_for_community(f'!den@{server}') is env.shop_den
    assert wire.calls.call_count == 0


def test_a_port_none_of_our_domains_names_is_not_ours_to_the_allowlist(env, wire):
    """Controller ruling: shop.example:8443 is not exempt, so a closed allowlist refuses it like any remote server,
    and nothing is fetched (a hostile handle cannot make us fetch our own host on a port it chose)."""
    set_setting('use_allowlist', True)
    assert validate_remote_actor(f'https://{SHOP}:8443/u/alice') is False
    assert search_for_community(f'!den@{SHOP}:8443') is None
    assert validate_remote_actor('https://peer.example:8443/u/bob') is False
    assert wire.calls.call_count == 0


def test_a_port_none_of_our_domains_names_is_on_the_ban_list_like_any_server(env, wire):
    db.session.add(BannedInstances(domain=f'{SHOP}:8443'))
    db.session.commit()
    assert validate_remote_actor(f'https://{SHOP}:8443/u/alice') is False
    assert search_for_community(f'!den@{SHOP}:8443') is None
    with pytest.raises(Exception, match='blocked'):
        search_for_feed(f'~reads@{SHOP}:8443')
    assert search_for_feed(f'~reads@{SHOP}') is env.feed
    assert wire.calls.call_count == 0


def test_a_domain_with_an_explicit_port_is_exempt_only_on_that_port(env, wire, app, monkeypatch):
    """SERVER_NAME is the one authority that may carry a port (a dev install on host:port)."""
    monkeypatch.setitem(app.config, 'SERVER_NAME', f'{SERVER}:8443')
    clear_domain_cache()
    set_setting('use_allowlist', True)
    try:
        assert validate_remote_actor(f'https://{SERVER}:8443/u/alice') is True
        assert search_for_community(f'!den@{SERVER}:8443') is env.first_den
        for other in (SERVER, f'{SERVER}:443', f'{SERVER}:9000'):
            assert validate_remote_actor(f'https://{other}/u/alice') is False, other
            assert search_for_community(f'!den@{other}') is None, other
        assert wire.calls.call_count == 0
    finally:
        clear_domain_cache()


@pytest.mark.parametrize('host', [SHOP, SERVER])
def test_a_banned_own_host_still_resolves_in_every_gate(env, wire, host):
    db.session.add(BannedInstances(domain=host))
    db.session.commit()
    den = env.shop_den if host == SHOP else env.first_den
    assert validate_remote_actor(f'https://{host}/u/alice') is True
    assert search_for_community(f'!den@{host}') is den
    assert search_for_feed(f'~reads@{host}') is env.feed
    assert wire.calls.call_count == 0


def test_a_banned_remote_feed_host_is_still_refused(env, wire):
    db.session.add(BannedInstances(domain='peer.example', reason='spam'))
    db.session.commit()
    with pytest.raises(Exception, match='peer.example is blocked. Reason: spam'):
        search_for_feed('~x@peer.example')
    assert wire.calls.call_count == 0


@pytest.mark.parametrize('server', [f'{SHOP}#x', f'{SHOP}/x', f'{SHOP}?x', f'{SHOP}\\x', f'{SERVER}#x'])
def test_a_handle_whose_server_is_no_authority_is_never_fetched(env, wire, server):
    """httpx reads https://shop.example#x/.well-known/webfinger as a GET of shop.example's root, which no discovery
    guard knows as WebFinger: a server with a path, query or fragment character names no host, and is not looked up."""
    assert search_for_community(f'!den@{server}') is None
    assert search_for_feed(f'~reads@{server}') is None
    assert find_actor_or_create(f'@alice@{server}') is None
    assert wire.calls.call_count == 0
