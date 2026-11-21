"""The Misc page edits the picked domain (spec 2026-11-20-multitenant-domain-config MT4).

A sister's settings may each be the first domain's (NULL, so a change there applies here too) or its own. The
instance-wide ones are shown on a sister's page and changed on the first domain's. Who may join a sister, and who may
read it, are its own (never inherited) and are on its page too; the first domain's are its registration mode and
private instance."""
import html
import re

import pytest

from app import db
from app.domain_config import effective, own_config
from app.models import Language, Site, SiteDomain, first_config
from app.storefront import clear_domain_cache, domain_for_host
from app.utils import get_setting
from tests.conftest import forget_login
from tests.factories import grant_permission, make_user
from tests.test_admin_domain_branding import SHOP, own_row
from tests.test_admin_site_profile import csrf, login
from tests.two_domains import request_on

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def english(admin_domain_branding_domains):
    language = Language(code='en', name='English')
    db.session.add(language)
    db.session.commit()
    return language


def get(client, path) -> str:
    forget_login()
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code, response.headers.get('Location'))
    return response.get_data(as_text=True)


def post(admin, domain, **data):
    client, token = admin
    forget_login()
    return client.post('/admin/misc', data={'domain': str(domain.id), 'csrf_token': token, 'submit': 'Save', **data})


def tag(body, field) -> str:
    return re.search(rf'<(input|textarea|select)[^>]*\bid="{field}"[^>]*>', body).group(0)


def options(body, field) -> list[tuple[str, str, bool]]:
    select = re.search(rf'<select[^>]*\bid="{field}".*?</select>', body, re.S).group(0)
    return [(re.search(r'value="([^"]*)"', attributes).group(1), html.unescape(label.strip()),
             ' selected' in attributes) for attributes, label in re.findall(r'<option([^>]*)>([^<]*)</option>', select)]


def test_a_sisters_page_offers_the_first_domains_value_for_each_setting(admin_domain_branding_admin,
                                                                        admin_domain_branding_domains, english):
    shop = admin_domain_branding_domains['shop']
    first_config().enable_downvotes = False
    first_config().allow_video_file_uploads = 'admins'
    db.session.commit()
    body = get(admin_domain_branding_admin[0], f'/admin/misc?domain={shop.id}')
    assert 'Misc settings: shop.example.org' in body
    assert options(body, 'enable_downvotes')[:1] == [('', "The first domain's: No", True)]
    assert options(body, 'allow_video_file_uploads')[0] == ('', "The first domain's: Admins and staff", True)
    assert re.search(r'placeholder="500"', tag(body, 'max_video_upload_mb'))


def test_a_sister_saves_its_own_values_and_the_first_domains(admin_domain_branding_admin, admin_domain_branding_domains,
                                                             english):
    shop = admin_domain_branding_domains['shop']
    response = post(admin_domain_branding_admin, shop, enable_downvotes='n', email_verification='', captcha_enabled='y',
                    allow_video_file_uploads='users', max_video_upload_mb='12', nsfw_country_restriction=' gb ',
                    language_id=str(english.id), default_filter='', max_album_images='')
    assert response.status_code == 200 and 'Settings saved.' in response.get_data(as_text=True)
    row = own_row(shop)
    assert (row.enable_downvotes, row.email_verification, row.captcha_enabled) == (False, None, True)
    assert (row.allow_video_file_uploads, row.max_video_upload_bytes, row.nsfw_country_restriction) == \
        ('users', 12 * 1048576, 'gb')
    assert (row.language_id, row.default_filter, row.max_album_images) == (english.id, None, None)
    # the first domain's, until it changes
    assert effective(shop)['email_verification'] is True
    first_config().email_verification = False
    db.session.commit()
    assert effective(domain_for_host(SHOP))['email_verification'] is False
    # what the shop's own requests read
    with request_on(admin_domain_branding_admin[0].application, domain_for_host(SHOP)):
        assert get_setting('allow_video_file_uploads') == 'users'
        assert get_setting('max_album_images') == 4   # the first domain's


def test_a_sisters_page_shows_its_own_values(admin_domain_branding_admin, admin_domain_branding_domains, english):
    shop = admin_domain_branding_domains['shop']
    config = db.session.get(SiteDomain, shop.id).config()
    config.enable_nsfw, config.nsfw_country_restriction, config.max_audio_upload_bytes = True, 'gb', 3 * 1048576
    db.session.commit()
    body = get(admin_domain_branding_admin[0], f'/admin/misc?domain={shop.id}')
    assert ('y', 'Yes', True) in options(body, 'enable_nsfw')
    assert re.search(r'>\s*GB</textarea>', body)
    assert 'value="3"' in tag(body, 'max_audio_upload_mb')


def test_instance_wide_settings_are_shown_on_a_sister_and_changed_on_the_first_domain(
        admin_domain_branding_admin, admin_domain_branding_domains, english):
    shop = admin_domain_branding_domains['shop']
    body = get(admin_domain_branding_admin[0], f'/admin/misc?domain={shop.id}')
    for field in ('honeypot', 'read_posts_cutoff', 'onion_pow_difficulty', 'log_activitypub_json'):
        assert ' disabled' in tag(body, field), field
    assert body.count('Applies to every domain.') == 11
    assert "It is changed on the first domain&#39;s page." in body or "It is changed on the first domain's page." in body
    honeypot = db.session.get(Site, 1).honeypot
    post(admin_domain_branding_admin, shop, honeypot='' if honeypot else 'y', read_posts_cutoff='3')
    db.session.expire_all()
    assert db.session.get(Site, 1).honeypot is honeypot and get_setting('read_posts_cutoff', 180) == 180
    first = get(admin_domain_branding_admin[0], '/admin/misc?domain=0')
    assert ' disabled' not in tag(first, 'honeypot') and 'Applies to every domain.' in first


def test_a_sisters_registration_and_login_may_be_the_first_domains_and_its_payment_is_its_own(
        admin_domain_branding_admin, admin_domain_branding_domains, english):
    """MT6, MT7: who may register and who may read are settings like the rest; whether members pay is its own."""
    shop = admin_domain_branding_domains['shop']
    first_config().registration_mode = 'RequireApplication'
    db.session.commit()
    body = get(admin_domain_branding_admin[0], f'/admin/misc?domain={shop.id}')
    assert options(body, 'registration_mode')[0] == ('', "The first domain's: Require application", True)
    assert options(body, 'private_instance')[0] == ('', "The first domain's: No", True)
    assert 'id="paid_membership"' in body and 'Danger Zone' not in body
    post(admin_domain_branding_admin, shop, registration_mode='Closed', private_instance='', lapse_grace_days='9',
         registration_offer_id='0')
    db.session.expire_all()
    row = db.session.get(SiteDomain, shop.id)
    assert (row.registration_mode, row.private_instance, row.lapse_grace_days, row.referral_codes) == \
        ('Closed', None, 9, False)
    first = get(admin_domain_branding_admin[0], '/admin/misc?domain=0')
    assert 'id="registration_mode"' in first and 'id="paid_membership"' not in first


@pytest.fixture
def domain_manager(app, admin_domain_branding_domains):
    """Holds `manage domains` alone."""
    user = make_user(admin_domain_branding_domains['instance'], 'domainmanager', local=True)
    user.verified = True
    db.session.commit()
    grant_permission(user, 'manage domains')
    client = app.test_client()
    login(client, user)
    return client, csrf(app, client)


def test_manage_domains_alone_changes_who_may_join_and_nothing_else(domain_manager, admin_domain_branding_domains,
                                                                    english):
    shop = admin_domain_branding_domains['shop']
    body = get(domain_manager[0], f'/admin/misc?domain={shop.id}')
    assert ' disabled' in tag(body, 'enable_downvotes') and ' disabled' not in tag(body, 'registration_mode')
    response = post(domain_manager, shop, enable_downvotes='n', registration_mode='Closed', lapse_grace_days='7',
                    registration_offer_id='0')
    assert response.status_code == 200
    db.session.expire_all()
    assert db.session.get(SiteDomain, shop.id).registration_mode == 'Closed'
    assert own_config(shop.id).enable_downvotes is None
    # the first domain's page is not theirs: a save naming it is refused
    response = post(domain_manager, admin_domain_branding_domains['first'], enable_downvotes='n')
    assert response.status_code == 302 and '/auth/permission_denied' in response.headers['Location']


def test_change_instance_settings_alone_shows_who_may_join_and_does_not_change_it(app, admin_domain_branding_domains,
                                                                                   english):
    user = make_user(admin_domain_branding_domains['instance'], 'settingsonly', local=True)
    user.verified = True
    db.session.commit()
    grant_permission(user, 'change instance settings')
    client = app.test_client()
    login(client, user)
    token = csrf(app, client)
    shop = admin_domain_branding_domains['shop']
    body = get(client, f'/admin/misc?domain={shop.id}')
    assert ' disabled' in tag(body, 'registration_mode') and 'Changing this needs the manage domains permission.' in body
    post((client, token), shop, registration_mode='Closed', enable_downvotes='n')
    db.session.expire_all()
    assert db.session.get(SiteDomain, shop.id).registration_mode is None
    assert own_config(shop.id).enable_downvotes is False


def test_a_referral_generators_value_not_on_offer_keeps_the_stored_one(admin_domain_branding_admin,
                                                                       admin_domain_branding_domains, english):
    shop = admin_domain_branding_domains['shop']
    db.session.get(SiteDomain, shop.id).config().referral_generators = 'all'
    db.session.commit()
    post(admin_domain_branding_admin, shop, referral_generators='everyone-and-their-dog')
    db.session.expire_all()
    assert own_config(shop.id).referral_generators == 'all'
    post(admin_domain_branding_admin, shop, referral_generators='')
    db.session.expire_all()
    assert own_config(shop.id).referral_generators is None


def test_the_old_domain_settings_page_is_gone(admin_domain_branding_admin, admin_domain_branding_domains):
    assert admin_domain_branding_admin[0].get('/admin/domains/999/settings').status_code == 404
    body = get(admin_domain_branding_admin[0], '/admin/domains')
    assert f"/admin/misc?domain={admin_domain_branding_domains['onion'].id}" in body
    clear_domain_cache()
