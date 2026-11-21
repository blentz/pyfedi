"""What a domain publishes to other servers and apps as its own (spec 2026-11-20-multitenant-domain-config, phase 3c;
branding spec §3.3): each domain's own values, read from the row it stores them in, the first domain's being the
first row (MT1). Nothing a sister publishes names the first domain (owner ruling)."""
import pytest
from flask import current_app

from app import cache, db
from app.domain_config import own_row
from app.federation_urls import domain_site_name
from app.models import SiteDomain, first_config
from app.storefront import clear_domain_cache, default_domain, domain_by_id
from tests.test_admin_domain_branding import SHOP

pytestmark = pytest.mark.usefixtures('site')


def test_the_first_domains_own_row_is_the_first_row_and_a_sisters_its_own(admin_domain_branding_domains):
    shop = admin_domain_branding_domains['shop']
    assert own_row(default_domain()) is first_config()
    assert own_row(shop) is None   # it has set nothing
    db.session.get(SiteDomain, shop.id).config().name = 'The shop'
    db.session.commit()
    assert own_row(domain_by_id(shop.id)).name == 'The shop'


def test_a_domain_goes_by_its_own_name_or_its_own_host(admin_domain_branding_domains):
    shop = admin_domain_branding_domains['shop']
    assert domain_site_name(default_domain(), None) == 'The First Site'
    assert domain_site_name(shop, None) == SHOP   # never the first domain's name
    first_config().name = None
    db.session.commit()
    clear_domain_cache()
    assert domain_site_name(default_domain(), None) == current_app.config['SERVER_NAME']


def test_each_domain_publishes_its_own_contact_address(app, admin_domain_branding_domains):
    """Mastodon's instance documents, which a federating domain answers with."""
    fed = SiteDomain(domain='fed.example', scheme='https', federates=True)
    db.session.add(fed)
    first_config().contact_email = 'first@example.org'
    db.session.commit()
    clear_domain_cache()
    cache.clear()
    client = app.test_client()

    def contact(path, host):
        response = client.get(path, base_url=f'https://{host}')
        assert response.status_code == 200, response.get_data(as_text=True)[:300]
        document = response.get_json()
        return document['contact']['email'] if 'contact' in document else document['email']

    assert contact('/api/v2/instance', current_app.config['SERVER_NAME']) == 'first@example.org'
    assert contact('/api/v2/instance', 'fed.example') == ''   # never the first domain's
    fed.config().contact_email = 'fed@example.org'
    db.session.commit()
    cache.clear()
    assert contact('/api/v1/instance', 'fed.example') == 'fed@example.org'


def test_the_first_domains_site_actor_shows_its_own_logo(app, admin_domain_branding_domains):
    actor = app.test_client().get('/', headers={'Accept': 'application/activity+json'}).get_json()
    assert actor['icon']['url'].endswith('/static/images/piefed_logo_icon_t_75.png')   # none of its own
    first_config().logo = '/static/media/logo_first.png'
    db.session.commit()
    cache.clear()
    actor = app.test_client().get('/', headers={'Accept': 'application/activity+json'}).get_json()
    assert actor['icon']['url'].endswith('/static/media/logo_first.png')
