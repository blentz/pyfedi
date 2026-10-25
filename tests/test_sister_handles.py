"""A sister's names resolve on the sister, and our own domains pass the allowlist (plan C4 task 4).

hell.cloud (SERVER_NAME) and shop.example share one database and each has its own `den` (C1). A name written without a
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
from app.models import AllowedInstances, BannedInstances, Site
from app.shared.feed import form_communities_to_ids
from app.storefront import acting_for, domain_for_host
from app.utils import set_setting
from tests.test_hosted_url_lookup import SERVER, SHOP, env, wire  # noqa: F401 (fixtures)

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture(autouse=True)
def _site_in_g(app):
    g.site = db.session.get(Site, 1)


@pytest.mark.parametrize('suffix', ['', '/', '/?x=1'])
def test_resolving_a_sisters_community_url_finds_the_sisters_den(env, wire, suffix):
    found = get_resolve_object(None, {'q': f'https://{SHOP}/c/den{suffix}'}, user_id=env.hell_alice.id)
    assert found['community']['community']['id'] == env.shop_den.id
    found = get_resolve_object(None, {'q': f'https://{SERVER}/c/den{suffix}'}, user_id=env.hell_alice.id)
    assert found['community']['community']['id'] == env.hell_den.id
    assert wire.calls.call_count == 0


@pytest.mark.parametrize('query', ['https://[shop.example/c/den/', 'https://[shop.example/c/den', 'http://[::1/c/x'])
def test_resolving_a_malformed_url_is_a_400_never_a_500(env, wire, app, monkeypatch, query):
    from tests.test_actor_handles import auth
    monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
    response = app.test_client().get('/api/alpha/resolve_object', query_string={'q': query},
                                     headers=auth(env.hell_alice))
    assert response.status_code == 400
    assert wire.calls.call_count == 0


def test_a_bare_community_name_means_the_acting_domain(env, wire):
    with acting_for(domain_for_host(SHOP)):
        assert get_community(None, {'name': 'den'})['community_view']['community']['id'] == env.shop_den.id
        assert form_communities_to_ids('den') == {env.shop_den.id}
    assert get_community(None, {'name': 'den'})['community_view']['community']['id'] == env.hell_den.id
    assert form_communities_to_ids('!den') == {env.hell_den.id}
    assert wire.calls.call_count == 0


def test_our_own_domains_pass_a_closed_allowlist(env, wire):
    set_setting('use_allowlist', True)
    assert AllowedInstances.query.count() == 0
    assert validate_remote_actor(f'https://{SHOP}/u/alice') is True
    env.shop_alice.ap_profile_id = f'https://{SHOP}/u/alice'   # the fixture's accounts carry none
    env.hell_alice.ap_profile_id = f'https://{SERVER}/u/alice'
    db.session.commit()
    assert find_actor_or_create(f'https://{SHOP}/u/alice') is env.shop_alice
    assert find_actor_or_create(f'https://{SERVER}/u/alice') is env.hell_alice
    assert validate_remote_actor(f'https://{SHOP}/u/alice') is True
    assert search_for_community(f'!den@{SHOP}') is env.shop_den
    assert search_for_community(f'!den@{SERVER}') is env.hell_den
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
