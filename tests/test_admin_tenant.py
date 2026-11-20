"""Admin settings pages edit one domain, the one the picker chose (spec 2026-11-20-multitenant-domain-config, MT4).

The Site page edits the picked domain's profile and look; the first domain's is the first domain's configuration row,
a sister's is its own row, a blank field there being the first domain's. `edit domain branding` on a sister lets its
holder change the branding fields; the rest take `change instance settings`."""
import io
import re

import pytest
from app import db
from app.admin.scope import SESSION_KEY
from app.domain_config import own_config
from app.models import Site, SiteDomain, first_config
from app.storefront import clear_domain_cache, domain_rows
from tests.conftest import forget_login
from tests.factories import grant_permission, limit_role, make_user
from tests.test_admin_domain_branding import flashes, own_row, save
from tests.test_admin_site_profile import csrf, login

pytestmark = pytest.mark.usefixtures('site')


def get(client, path) -> str:
    forget_login()
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code, response.headers.get('Location'))
    return response.get_data(as_text=True)


def picker(body: str) -> list[tuple[str, str, bool]]:
    """The picker's options: value, label, and whether it is the one selected."""
    form = re.search(r'<form method="get"[^>]*id="domain-picker">.*?</form>', body, re.S)
    assert form, 'no picker'
    return [(value, label.strip(), bool(selected)) for value, selected, label in
            re.findall(r'<option value="(\d+)"( selected)?>([^<]*)</option>', form.group(0))]


@pytest.fixture
def brander(app, admin_domain_branding_domains):
    """Holds `edit domain branding` on the shop domain alone, signed in."""
    user = make_user(admin_domain_branding_domains['instance'], 'shopbrander', local=True)
    user.verified = True
    db.session.commit()
    limit_role(grant_permission(user, 'edit domain branding'), admin_domain_branding_domains['shop'])
    client = app.test_client()
    login(client, user)
    return client, csrf(app, client)


# ---------------------------------------------------------------------------------------------------------------------
# The picker

def test_the_site_page_offers_each_domain_and_names_the_first(admin_domain_branding_admin, admin_domain_branding_domains):
    first, shop = admin_domain_branding_domains['first'], admin_domain_branding_domains['shop']
    # the shop and the onion federate with nobody: named only on themselves, or once picked by id (F6), so the first
    # domain's page has nothing else to offer
    assert 'id="domain-picker"' not in get(admin_domain_branding_admin[0], '/admin/site')
    options = picker(get(admin_domain_branding_admin[0], f'/admin/site?domain={shop.id}'))
    assert options == [(str(first.id), f'{first.domain} (first domain)', False), (str(shop.id), shop.domain, True)]


def test_the_pick_is_kept_and_shared_with_the_list_pages(admin_domain_branding_admin, admin_domain_branding_domains):
    client = admin_domain_branding_admin[0]
    shop = admin_domain_branding_domains['shop']
    get(client, f'/admin/site?domain={shop.id}')
    with client.session_transaction() as session:
        assert session[SESSION_KEY] == shop.id
    assert 'Site profile: shop.example.org' in get(client, '/admin/site')
    # a list's "All" is, on a settings page, the first domain
    assert 'Site profile: ' not in get(client, '/admin/site?domain=0')


def test_a_domain_the_user_may_not_edit_is_never_picked(brander, admin_domain_branding_domains):
    """A holder on the shop alone asking for the onion, or the first domain, gets the shop's page."""
    client = brander[0]
    for domain in ('onion', 'first'):
        body = get(client, f"/admin/site?domain={admin_domain_branding_domains[domain].id}")
        assert 'Site profile: shop.example.org' in body
    assert 'id="domain-picker"' in body and 'Settings for shop.example.org.' in body   # one choice: named, not offered


def test_a_site_with_no_domain_rows_edits_the_first_domain(app, admin_site_profile_admin_client):
    """No site_domain row names the first domain: the page edits the first domain's row, with no picker."""
    client, token = admin_site_profile_admin_client
    assert db.session.query(SiteDomain).count() == 0
    body = get(client, '/admin/site')
    assert 'id="domain-picker"' not in body
    forget_login()
    response = client.post('/admin/site', content_type='multipart/form-data', data={
        'name': 'Rowless', 'contact_email': 'admin@example.com', 'csrf_token': token, 'logo': (io.BytesIO(b''), '')})
    assert response.status_code == 302
    assert first_config().name == 'Rowless'


# ---------------------------------------------------------------------------------------------------------------------
# What each user may change

def test_the_navigation_offers_the_site_page_to_a_branding_holder_and_not_misc(brander, admin_domain_branding_domains):
    body = get(brander[0], '/admin/site')
    assert 'href="/admin/site"' in body and 'href="/admin/misc"' not in body


def test_a_branding_holder_changes_the_branding_and_nothing_else(brander, admin_domain_branding_domains):
    shop = admin_domain_branding_domains['shop']
    body = get(brander[0], '/admin/site')
    def tag(field):
        return re.search(rf'<(input|textarea|select)[^>]*\bid="{field}"[^>]*>', body).group(0)
    for field in ('announcement', 'legal_information', 'contact_email', 'tos_url', 'source_url'):
        assert ' disabled' in tag(field), field
    assert ' disabled' not in tag('name')
    assert body.count('Changing this needs the change instance settings permission.') == 7
    response = save(brander, shop, announcement='Not mine to set', tos_url='https://elsewhere.example/tos')
    assert response.status_code == 302
    row = own_row(shop)
    assert row.name == 'Shop Branded'
    assert (row.announcement, row.tos_url) == (None, None)


def test_a_settings_admin_sets_a_sisters_profile(admin_domain_branding_admin, admin_domain_branding_domains):
    shop = admin_domain_branding_domains['shop']
    response = save(admin_domain_branding_admin, shop, announcement='# Shop news', contact_email='',
                    tos_url='https://shop.example.org/tos')
    assert response.status_code == 302
    row = own_row(shop)
    assert (row.announcement, row.tos_url, row.contact_email) == ('# Shop news', 'https://shop.example.org/tos', '')
    assert '<h1>Shop news</h1>' in row.announcement_html


def test_the_first_domain_needs_a_contact_address(admin_domain_branding_admin, admin_domain_branding_domains):
    first = admin_domain_branding_domains['first']
    response = save(admin_domain_branding_admin, first, name='Never stored', contact_email='')
    assert response.status_code == 200
    assert 'The first domain needs a contact address.' in response.get_data(as_text=True)
    assert db.session.get(Site, 1).name == 'The First Site'
    assert flashes(admin_domain_branding_admin) == []


# ---------------------------------------------------------------------------------------------------------------------
# Inheritance

def test_a_sisters_blank_field_shows_the_first_domains_value(admin_domain_branding_admin, admin_domain_branding_domains):
    shop = admin_domain_branding_domains['shop']
    body = get(admin_domain_branding_admin[0], f'/admin/site?domain={shop.id}')
    assert re.search(r'<input[^>]*id="name"[^>]*placeholder="The First Site"', body)
    assert "Left blank, this is the first domain&#39;s." in body or "Left blank, this is the first domain's." in body
    first_body = get(admin_domain_branding_admin[0], '/admin/site?domain=0')
    assert 'Left blank, this is the first domain' not in first_body


def test_a_sister_shows_the_first_domains_logo_until_it_has_its_own(admin_domain_branding_admin,
                                                                   admin_domain_branding_domains):
    site = db.session.get(Site, 1)
    site.logo = '/static/media/logo_First.png'
    db.session.commit()
    clear_domain_cache()
    shop = admin_domain_branding_domains['shop']
    body = get(admin_domain_branding_admin[0], f'/admin/site?domain={shop.id}')
    assert 'src="/static/media/logo_First.png"' in body and 'until this domain has one of its own' in body


@pytest.mark.parametrize('posted, stored', [
    pytest.param({'default_theme': ''}, '', id="the first domain's"),
    pytest.param({'default_theme': 'forest'}, 'forest', id='one on offer'),
    pytest.param({'default_theme': 'no-such-theme'}, 'dark', id='one not on offer keeps the stored one'),
    pytest.param({}, 'dark', id='none posted keeps the stored one'),
])
def test_a_sisters_theme(admin_domain_branding_admin, admin_domain_branding_domains, posted, stored):
    shop = admin_domain_branding_domains['shop']
    db.session.get(SiteDomain, shop.id).config().default_theme = 'dark'
    db.session.commit()
    client, token = admin_domain_branding_admin
    forget_login()
    response = client.post(f'/admin/site?domain={shop.id}', content_type='multipart/form-data',
                           data={'name': 'Shop', 'csrf_token': token, 'logo': (io.BytesIO(b''), ''), **posted})
    assert response.status_code == 302
    assert own_config(shop.id).default_theme == stored


def test_the_first_domain_offers_no_inherited_theme(admin_domain_branding_admin, admin_domain_branding_domains):
    body = get(admin_domain_branding_admin[0], '/admin/site?domain=0')
    select = re.search(r'<select[^>]*id="default_theme".*?</select>', body, re.S).group(0)
    assert 'value=""' not in select
    shop = admin_domain_branding_domains['shop']
    body = get(admin_domain_branding_admin[0], f'/admin/site?domain={shop.id}')
    select = re.search(r'<select[^>]*id="default_theme".*?</select>', body, re.S).group(0)
    assert re.search(r'<option[^>]*value=""[^>]*>The first domain', select)


def test_misc_no_longer_has_the_theme_or_the_css(app):
    from app.admin.forms import SiteMiscForm
    with app.test_request_context():
        form = SiteMiscForm(formdata=None, meta={'csrf': False})
    assert not hasattr(form, 'default_theme') and not hasattr(form, 'additional_css')
    assert hasattr(form, 'additional_js')   # the JS stays with the whole permission (phase 2b)


def test_no_session_value_names_a_row_that_is_gone(admin_domain_branding_admin, admin_domain_branding_domains):
    """A pick of a domain deleted since falls back to the first domain."""
    client = admin_domain_branding_admin[0]
    with client.session_transaction() as session:
        session[SESSION_KEY] = 999
    assert 'Site profile: ' not in get(client, '/admin/site')
    assert all(row.id != 999 for row in domain_rows())
