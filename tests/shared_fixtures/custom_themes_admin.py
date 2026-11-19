"""Fixtures from tests/test_custom_themes_admin.py that other test modules request (registered in tests/conftest.py)."""
import pytest

from app import db
from app.models import CustomTheme, Role, RolePermission, Site
from app.storefront import clear_domain_cache
from tests.factories import grant_permission, grant_role, limit_role
from tests.test_branding_pages import PROXY
from tests.test_custom_themes_admin import Actor


@pytest.fixture
def custom_themes_admin_world(app, two_domains, monkeypatch):
    monkeypatch.setitem(app.config, 'ONION_PROXY_IP', PROXY)   # onion pages come through the onion proxy
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    default, onion = two_domains.domains
    two_domains.local_user('founder', default)   # user 1 holds every permission: no actor here may be it
    holder = two_domains.local_user('holder', default)       # edit domain branding on the onion only
    role = Role(name='onion branding')
    db.session.add(role)
    db.session.commit()
    db.session.add(RolePermission(role_id=role.id, permission='edit domain branding'))
    db.session.commit()
    limit_role(role, onion)
    grant_role(holder, role)
    admin = two_domains.local_user('siteadmin', default)     # every domain, the first included
    grant_permission(admin, 'change instance settings')
    grant_permission(admin, 'edit domain branding')
    db.session.add_all([CustomTheme(site_domain_id=default.id, name='Daylight', base_theme='forest', css='.day{}'),
                        CustomTheme(site_domain_id=onion.id, name='Night', base_theme='forest', css='.night{}')])
    db.session.commit()
    clear_domain_cache()
    themes = {t.name: t for t in db.session.query(CustomTheme)}
    return dict(default=default, onion=onion, holder=Actor(app, holder), admin=Actor(app, admin),
                day=themes['Daylight'], night=themes['Night'], two_domains=two_domains)
