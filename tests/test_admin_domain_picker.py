"""The domain picker every admin page with the admin tabs shows (spec 2026-11-20-multitenant-domain-config, MT4;
app.admin.picker). One choice, kept for every page: a settings page edits the picked domain, a list shows its rows, and
an instance-wide page says its settings are every domain's and keeps the pick for the pages that follow it."""
import re

import pytest

from app import db
from app.admin.scope import SESSION_KEY
from app.models import Offer, OfferDomain, SiteDomain, first_config
from app.storefront import clear_domain_cache, domain_for_host
from tests.conftest import forget_login
from tests.factories import grant_permission, make_user
from tests.test_admin_site_profile import login

pytestmark = pytest.mark.usefixtures('site')

FED = 'fed.example'


@pytest.fixture
def domains(admin_domain_branding_domains):
    """The first domain, the local-only shop and onion, and a federating sister: the one the first domain may name."""
    db.session.add(SiteDomain(domain=FED, scheme='https', federates=True))
    db.session.commit()
    clear_domain_cache()
    return {**admin_domain_branding_domains, 'fed': domain_for_host(FED)}


def get(client, path) -> str:
    forget_login()
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code, response.headers.get('Location'))
    return response.get_data(as_text=True)


def picker(body: str):
    """The picker's kind and options (value, label, selected), or None when the page has no picker to choose with."""
    form = re.search(r'<form method="get"[^>]*id="domain-picker" data-kind="(\w+)">(.*?)</form>', body, re.S)
    if form is None:
        return None
    return form.group(1), [(value, label.strip(), bool(selected)) for value, selected, label in
                           re.findall(r'<option value="(\d+)"( selected)?>([^<]*)</option>', form.group(2))]


def picked(client):
    with client.session_transaction() as session:
        return session.get(SESSION_KEY)


INSTANCE_PAGES = ['/admin/federation', '/admin/emoji', '/admin/instances', '/admin/activities', '/admin/newsletter']


@pytest.mark.parametrize('path', INSTANCE_PAGES)
def test_an_instance_wide_page_offers_every_domain_and_says_its_settings_are_every_domains(
        admin_domain_branding_admin, domains, path):
    first, fed = domains['first'], domains['fed']
    body = get(admin_domain_branding_admin[0], path)
    # the shop and the onion are local-only: never named off their own domain, to an admin no more than anyone (F6)
    assert picker(body) == ('instance', [('0', 'All', True), (str(first.id), f'{first.domain} (first domain)', False),
                                         (str(fed.id), FED, False)])
    assert 'they apply to every domain' in body
    assert 'js/admin_domain_picker.js' in body   # picking applies at once


def test_a_pick_made_on_an_instance_wide_page_is_the_one_the_settings_pages_edit(admin_domain_branding_admin,
                                                                                 domains):
    client, fed = admin_domain_branding_admin[0], domains['fed']
    kind, options = picker(get(client, f'/admin/federation?domain={fed.id}'))
    assert kind == 'instance' and (str(fed.id), fed.domain, True) in options
    assert picked(client) == fed.id
    assert f'Site profile: {FED}' in get(client, '/admin/site')
    kind, options = picker(get(client, '/admin/misc'))
    assert kind == 'settings' and (str(fed.id), fed.domain, True) in options


def test_one_pick_follows_from_page_to_page_whatever_the_page(admin_domain_branding_admin, domains):
    """Settings page, list, instance-wide page: each shows the same domain picked."""
    client, fed = admin_domain_branding_admin[0], domains['fed']
    get(client, f'/admin/site?domain={fed.id}')
    for path, kind in (('/admin/domains', 'list'), ('/admin/federation', 'instance'), ('/admin/sidebar', 'settings')):
        found_kind, options = picker(get(client, path))
        assert found_kind == kind and (str(fed.id), fed.domain, True) in options, path


def test_all_on_a_list_or_an_instance_wide_page_is_the_first_domain_on_a_settings_page(admin_domain_branding_admin,
                                                                                       domains):
    client, first = admin_domain_branding_admin[0], domains['first']
    kind, options = picker(get(client, '/admin/federation?domain=0'))
    assert options[0] == ('0', 'All', True)
    kind, options = picker(get(client, '/admin/site'))
    assert kind == 'settings' and options[0] == (str(first.id), f'{first.domain} (first domain)', True)
    assert all(value != '0' for value, _label, _selected in options)   # a settings page is for one domain


def test_a_pick_no_longer_offered_shows_all(admin_domain_branding_admin, domains):
    client = admin_domain_branding_admin[0]
    get(client, '/admin/federation?domain=999999')
    assert picker(get(client, '/admin/federation'))[1][0] == ('0', 'All', True)


def test_a_save_never_changes_the_pick(admin_domain_branding_admin, domains):
    client, token = admin_domain_branding_admin
    fed = domains['fed']
    get(client, f'/admin/site?domain={fed.id}')
    forget_login()
    client.post('/admin/federation?domain=0', data={'csrf_token': token})
    assert picked(client) == fed.id


def test_a_site_with_one_domain_shows_no_picker(app, admin_site_profile_admin_client):
    client = admin_site_profile_admin_client[0]
    assert db.session.query(SiteDomain).count() == 0
    for path in ('/admin/federation', '/admin/site', '/admin/misc'):
        body = get(client, path)
        assert 'id="domain-picker"' not in body and 'js/admin_domain_picker.js' not in body, path


def test_an_instance_wide_page_shows_no_picker_to_one_who_cannot_change_instance_settings(app, domains):
    user = make_user(domains['instance'], 'mailer', local=True)
    user.verified = True
    db.session.commit()
    grant_permission(user, 'send newsletter')
    client = app.test_client()
    login(client, user)
    body = get(client, '/admin/newsletter')
    assert 'id="typeTab"' in body   # the admin tabs are there
    assert 'id="domain-picker"' not in body


# ---------------------------------------------------------------------------------------------------------------------
# The payments page lists the offers on the picked domain

@pytest.fixture
def offers(domains):
    """An offer on every domain (no domains named), one on the federating sister, one on the first domain and the
    onion, and one on the onion alone."""
    def offer(title, *on):
        row = Offer(provider_kind='link', kind='link', title=title, offer_group='links', url='https://pay.example/')
        db.session.add(row)
        db.session.flush()
        db.session.add_all(OfferDomain(offer_id=row.id, site_domain_id=domain.id) for domain in on)
        return row
    rows = {'everywhere': offer('Everywhere'), 'fed': offer('Fed only', domains['fed']),
            'first': offer('First and onion', domains['first'], domains['onion']),
            'onion': offer('Onion only', domains['onion'])}
    db.session.commit()
    return rows


@pytest.fixture
def treasury(admin_domain_branding_admin):
    """The admin, holding `manage monetization` too: the payments page's permission."""
    grant_permission(_admin_user(), 'manage monetization')
    return admin_domain_branding_admin[0]


def offer_rows(body):
    """Each listed offer's title and what its Domains cell says."""
    return {title: on for title, on in re.findall(
        r'<tr>\s*<td>([^<]+)</td>\s*<td>links</td>\s*<td>.*?</td>\s*<td>(?:Yes|No)</td>\s*<td>([^<]*)</td>', body, re.S)}


def test_all_lists_every_offer_and_names_only_what_may_be_named_here(treasury, domains, offers):
    body = get(treasury, '/admin/payments?domain=0')
    first = domains['first'].domain
    assert picker(body)[0] == 'list'
    # the onion is local-only: counted, never named off its own domain (F6)
    assert offer_rows(body) == {'Everywhere': 'All', 'Fed only': FED, 'First and onion': f'{first}, 1 more',
                                'Onion only': '1 more'}
    assert domains['onion'].domain not in body


def test_a_picked_domain_lists_the_offers_on_it(treasury, domains, offers):
    client = treasury
    assert set(offer_rows(get(client, f'/admin/payments?domain={domains["fed"].id}'))) == {'Everywhere', 'Fed only'}
    assert set(offer_rows(get(client, f'/admin/payments?domain={domains["first"].id}'))) == {'Everywhere',
                                                                                             'First and onion'}
    assert set(offer_rows(get(client, '/admin/payments'))) == {'Everywhere', 'First and onion'}   # the pick is kept


def test_a_domain_no_offer_is_on_says_so(treasury, domains):
    client = treasury
    db.session.add(Offer(provider_kind='link', kind='link', title='Fed only', offer_group='links',
                         url='https://pay.example/'))
    db.session.flush()
    db.session.add(OfferDomain(offer_id=Offer.query.one().id, site_domain_id=domains['fed'].id))
    db.session.commit()
    body = get(client, f'/admin/payments?domain={domains["first"].id}')
    assert 'id="no-offers-here"' in body and 'Fed only' not in body
    assert 'id="no-offers-here"' not in get(client, '/admin/payments?domain=0')   # "All" lists it


def test_the_picker_on_the_payments_page_is_offered_to_a_treasurer(app, domains, offers):
    """Payments are `manage monetization`'s: its holder picks the domain without changing instance settings."""
    user = make_user(domains['instance'], 'treasurer', local=True)
    user.verified = True
    db.session.commit()
    grant_permission(user, 'manage monetization')
    client = app.test_client()
    login(client, user)
    kind, options = picker(get(client, '/admin/payments'))
    assert kind == 'list' and [value for value, _label, _selected in options] == \
        ['0', str(domains['first'].id), str(domains['fed'].id)]


# ---------------------------------------------------------------------------------------------------------------------
# The navigation follows the pick

def test_the_registration_tabs_are_the_picked_domains(admin_domain_branding_admin, domains):
    client, fed = admin_domain_branding_admin[0], domains['fed']
    first_config().registration_mode = 'Open'
    db.session.get(SiteDomain, fed.id).config().registration_mode = 'RequireApplication'
    db.session.commit()
    clear_domain_cache()
    grant_permission(_admin_user(), 'approve registrations')
    assert 'href="/admin/approve_registrations"' in get(client, '/admin/federation?domain=0')   # some domain asks
    assert 'href="/admin/approve_registrations"' not in get(client, '/admin/federation?domain='
                                                            f'{domains["first"].id}')
    assert 'href="/admin/approve_registrations"' in get(client, f'/admin/federation?domain={fed.id}')


def test_the_referrals_tab_is_the_picked_domains(admin_domain_branding_admin, domains):
    client, fed = admin_domain_branding_admin[0], domains['fed']
    first_config().registration_mode = 'ReferralCode'
    db.session.get(SiteDomain, fed.id).config().registration_mode = 'Open'
    db.session.commit()
    clear_domain_cache()
    grant_permission(_admin_user(), 'administer all users')
    assert 'href="/admin/referrals"' in get(client, '/admin/federation?domain=0')
    assert 'href="/admin/referrals"' not in get(client, f'/admin/federation?domain={fed.id}')


def _admin_user():
    from app.models import User
    return User.query.filter_by(user_name='brandadmin').one()
