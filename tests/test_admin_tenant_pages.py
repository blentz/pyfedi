"""The Sidebar, Chooser and Themes admin pages are for the picked domain (spec 2026-11-20-multitenant-domain-config,
MT4): one page each, with the domain picker, where each domain had a page of its own."""
import re

import pytest

from app import db
from app.admin.scope import SESSION_KEY
from app.domain_config import effective, own_config
from app.models import SiteDomain, first_config
from app.storefront import domain_for_host
from tests.conftest import forget_login
from tests.test_admin_domain_branding import SHOP

pytestmark = pytest.mark.usefixtures('site')


def get(client, path) -> str:
    forget_login()
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code, response.headers.get('Location'))
    return response.get_data(as_text=True)


def post(admin, path, **data):
    client, token = admin
    forget_login()
    return client.post(path, data={'csrf_token': token, **data})


# ---------------------------------------------------------------------------------------------------------------------
# Sidebar

def test_the_old_domain_sidebar_address_opens_the_sidebar_page_with_the_domain_picked(admin_domain_branding_admin,
                                                                                    admin_domain_branding_domains):
    shop = admin_domain_branding_domains['shop']
    response = admin_domain_branding_admin[0].get(f'/admin/domains/{shop.id}/sidebar')
    assert response.status_code == 302 and response.headers['Location'].endswith(f'/admin/sidebar?domain={shop.id}')
    assert admin_domain_branding_admin[0].get('/admin/domains/999/sidebar').status_code == 404


def test_the_sidebar_page_is_the_picked_domains(admin_domain_branding_admin, admin_domain_branding_domains):
    shop = admin_domain_branding_domains['shop']
    body = get(admin_domain_branding_admin[0], f'/admin/sidebar?domain={shop.id}')
    assert 'Sidebar boxes: shop.example.org' in body
    assert body.count(f'<input type="hidden" name="domain" value="{shop.id}">') == 2   # each sidebar's form
    assert 'Sidebar boxes: ' not in get(admin_domain_branding_admin[0], '/admin/sidebar?domain=0')


def test_a_press_changes_the_domain_its_form_names_whatever_was_picked_since(admin_domain_branding_admin,
                                                                             admin_domain_branding_domains):
    shop, first = admin_domain_branding_domains['shop'], admin_domain_branding_domains['first']
    before = first_config().home_sidebar
    get(admin_domain_branding_admin[0], f'/admin/sidebar?domain={first.id}')
    response = post(admin_domain_branding_admin, '/admin/sidebar', domain=str(shop.id), action='home:toggle:about')
    assert response.status_code == 302
    assert response.headers['Location'].endswith(f'/admin/sidebar?domain={shop.id}#sidebar-home')
    assert own_config(shop.id).home_sidebar and first_config().home_sidebar == before
    with admin_domain_branding_admin[0].session_transaction() as session:
        assert session[SESSION_KEY] == first.id


def test_a_branding_holder_elsewhere_is_turned_away(app, admin_domain_branding_domains):
    from tests.factories import make_user
    from tests.test_admin_site_profile import login
    user = make_user(admin_domain_branding_domains['instance'], 'nobodyhere', local=True)
    user.verified = True
    db.session.commit()
    client = app.test_client()
    login(client, user)
    response = client.get('/admin/sidebar')
    assert response.status_code == 302 and '/auth/permission_denied' in response.headers['Location']


# ---------------------------------------------------------------------------------------------------------------------
# Chooser

def test_a_sisters_chooser_entry_is_its_own_or_the_first_domains(admin_domain_branding_admin,
                                                                 admin_domain_branding_domains):
    shop = admin_domain_branding_domains['shop']
    first_config().elevator_pitch = 'The first pitch'
    db.session.commit()
    body = get(admin_domain_branding_admin[0], f'/admin/instance_chooser?domain={shop.id}')
    assert 'Instance chooser settings: shop.example.org' in body
    assert re.search(r'<input[^>]*id="elevator_pitch"[^>]*placeholder="The first pitch"', body) or \
        re.search(r'placeholder="The first pitch"[^>]*id="elevator_pitch"', body)
    for field in ('main_instance_list', 'crawl_known_instances'):
        assert re.search(rf'<input[^>]*\bid="{field}"[^>]*\bdisabled|<input[^>]*\bdisabled[^>]*\bid="{field}"', body)
    response = post(admin_domain_branding_admin, '/admin/instance_chooser', domain=str(shop.id),
                    enable_instance_chooser='y', number_of_admins='3', elevator_pitch='', main_instance_list='x.example')
    assert response.status_code == 200
    row = own_config(shop.id)
    assert (row.enable_instance_chooser, row.number_of_admins, row.elevator_pitch, row.financial_stability) == \
        (True, 3, '', None)
    assert effective(domain_for_host(SHOP))['elevator_pitch'] == 'The first pitch'
    from app.utils import get_setting
    assert get_setting('main_instance_list', 'piefed.social') == 'piefed.social'   # instance-wide: not from a sister


def test_the_first_domains_chooser_saves_the_list_settings(admin_domain_branding_admin, admin_domain_branding_domains):
    from app.utils import get_setting
    response = post(admin_domain_branding_admin, '/admin/instance_chooser', domain='0', enable_instance_chooser='y',
                    elevator_pitch='Ours', number_of_admins='2', main_instance_list='list.example',
                    crawl_known_instances='y')
    assert response.status_code == 200
    assert (first_config().enable_instance_chooser, first_config().elevator_pitch) == (True, 'Ours')
    assert (get_setting('main_instance_list'), get_setting('crawl_known_instances')) == ('list.example', True)
    body = get(admin_domain_branding_admin[0], '/admin/instance_chooser?domain=0')
    assert 'value="list.example"' in body and 'value="Ours"' in body


# ---------------------------------------------------------------------------------------------------------------------
# Themes

def test_the_themes_tab_opens_the_picked_domains_themes(admin_domain_branding_admin, admin_domain_branding_domains):
    shop, first = admin_domain_branding_domains['shop'], admin_domain_branding_domains['first']
    response = admin_domain_branding_admin[0].get(f'/admin/themes?domain={shop.id}')
    assert response.status_code == 302 and response.headers['Location'].endswith(f'/admin/domains/{shop.id}/themes')
    body = get(admin_domain_branding_admin[0], f'/admin/domains/{first.id}/themes')
    assert 'id="themes-tab"' in body and re.search(r'class="nav-link active"[^>]*id="themes-tab"', body)
    with admin_domain_branding_admin[0].session_transaction() as session:
        assert session[SESSION_KEY] == first.id   # the themes page is its domain's: the other pages follow
    picker = re.search(r'<form method="get" action="/admin/themes"[^>]*id="domain-picker"[^>]*>', body)
    shop_body = get(admin_domain_branding_admin[0], f'/admin/domains/{shop.id}/themes')
    assert picker or re.search(r'<form method="get" action="/admin/themes"', shop_body)


def test_the_themes_tab_turns_away_a_user_with_no_domain_whose_themes_they_manage(app, admin_site_profile_admin_client):
    """No site_domain row: no domain has themes of its own to manage."""
    client, _token = admin_site_profile_admin_client
    assert db.session.query(SiteDomain).count() == 0
    response = client.get('/admin/themes')
    assert response.status_code == 302 and '/auth/permission_denied' in response.headers['Location']
