"""Fixtures from tests/test_branding_pages.py that other test modules request (registered in tests/conftest.py)."""
import pytest

from app import cache, db
from app.models import CmsPage, Instance, Site
from app.storefront import clear_domain_cache
from tests.test_branding_pages import CREATED, PROXY, SITE_VALUES


@pytest.fixture
def branded(app, two_domains, monkeypatch):
    """The first domain's Site values, all fixed, an onion beside it, and a community on the first domain."""
    site = db.session.get(Site, 1)
    for field, value in SITE_VALUES.items():
        setattr(site, field, value)
    site.private_instance = False
    site.show_inoculation_block = False   # a random pick per request
    site.sidebar = 'The golden sidebar'
    site.about = 'The golden about'
    site.created_at = CREATED
    site.updated = CREATED
    site.last_active = CREATED
    branded_instance = db.session.get(Instance, site.id)   # /about prints the instance row's creation date
    branded_instance.created_at = CREATED
    db.session.commit()
    monkeypatch.setitem(app.config, 'ONION_PROXY_IP', PROXY)
    default = two_domains.domains[0]
    community = two_domains.local_community('goldart', default)
    community.description = 'About *art*'
    community.description_html = '<p>About <em>art</em></p>'
    community.created_at = CREATED
    community.last_active = CREATED
    db.session.add(CmsPage(url='/donate', title='Donate', body='x', body_html='<p>x</p>', in_header=True, in_footer=True))
    db.session.commit()
    clear_domain_cache()
    cache.clear()
    yield two_domains
    cache.clear()
