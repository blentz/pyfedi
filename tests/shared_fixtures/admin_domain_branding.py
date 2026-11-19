"""Fixtures from tests/test_admin_domain_branding.py that other test modules request (registered in tests/conftest.py)."""
import pytest
from flask import current_app

from app import db
from app.models import Site, SiteDomain
from app.storefront import clear_domain_cache, domain_for_host
from tests.factories import (
    INSTANCE_SETTINGS,
    grant_permissions,
    make_instance,
    make_user,
)
from tests.test_admin_domain_branding import ONION, REPO, SHOP
from tests.test_admin_site_profile import csrf, login


@pytest.fixture
def admin_domain_branding_admin(app, admin_domain_branding_domains):
    user = make_user(admin_domain_branding_domains['instance'], 'brandadmin', local=True)
    user.verified = True
    db.session.commit()
    grant_permissions(user, *INSTANCE_SETTINGS)
    client = app.test_client()
    login(client, user)
    return client, csrf(app, client)


@pytest.fixture
def admin_domain_branding_domains(app, db_session, admin_domain_branding_media):
    """The first domain at SERVER_NAME, a clearnet sister and the onion. Site row 1 is open to visitors."""
    first = current_app.config['SERVER_NAME']
    instance = make_instance(first, software='piefed')
    make_user(instance, 'founder', local=True)   # user 1, who passes every permission check
    db.session.add(SiteDomain(domain=first, scheme='https', is_default=True))
    db.session.add(SiteDomain(domain=SHOP, scheme='https', federates=False))
    db.session.add(SiteDomain(domain=ONION, scheme='http', federates=False))
    site = db.session.get(Site, 1)
    site.private_instance = False
    site.show_inoculation_block = False
    site.name = 'The First Site'
    site.about_html = '<p>The first about</p>'
    db.session.commit()
    clear_domain_cache()
    yield {'first': domain_for_host(first), 'shop': domain_for_host(SHOP), 'onion': domain_for_host(ONION),
           'instance': instance}
    clear_domain_cache()


@pytest.fixture
def admin_domain_branding_media(tmp_path, monkeypatch):
    """A scratch working directory: an empty `app/static/media`, the real templates and PWA manifests (theme_list
    and /manifest.json read them by a relative path)."""
    directory = tmp_path / 'app' / 'static' / 'media'
    directory.mkdir(parents=True)
    (tmp_path / 'app' / 'templates').symlink_to(REPO / 'app' / 'templates')
    (tmp_path / 'app' / 'static' / 'pwa_manifests').symlink_to(REPO / 'app' / 'static' / 'pwa_manifests')
    monkeypatch.chdir(tmp_path)
    return directory
