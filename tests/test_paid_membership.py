"""Paid membership on the default domain (spec 2026-11-15-paid-membership-design.md, plan phase 1, M1-M7)."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup
from flask import current_app, g, session as flask_session
from flask_wtf.csrf import generate_csrf

from app import db
from app.models import CmsPage, MemberSubscription, Offer, Site, SiteDomain, User, utcnow
from app.utils import markdown_to_html
from app.auth.oauth_util import refuse_new_oauth_account
from app.auth.util import sync_user_with_ldap
from app.storefront import clear_domain_cache, default_domain, open_registrations, paid_registration, \
    public_registration_open, registration_mode_on
from app.payments.registration import registration_checkout
from app.referral import GENERATORS_SETTING, generate_code
from app.utils import set_setting
from tests.factories import grant_permission, make_user

CHECKOUT = 'https://checkout.example/session'


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    g.admin_ids = []
    site = db.session.get(Site, 1)
    site.private_instance = False
    site.registration_mode = 'Open'
    monkeypatch.setitem(current_app.config, 'STRIPE_SECRET_KEY', 'sk_test')
    knight = Offer(provider_kind='stripe', kind='subscription', title='Furcas', offer_group='tiers',
                   provider_price_id='price_k', amount_minor=666, currency='usd', interval='month', position=1)
    duke = Offer(provider_kind='stripe', kind='subscription', title='Astaroth', offer_group='tiers',
                 provider_price_id='price_d', amount_minor=1666, currency='usd', interval='month', position=2)
    gift = Offer(provider_kind='stripe', kind='donation', title='Gift', offer_group='gifts', provider_price_id='price_g',
                 amount_minor=500, currency='usd')
    db.session.add_all([knight, duke, gift])
    db.session.flush()
    home = SiteDomain(domain=current_app.config['SERVER_NAME'], scheme='https', federates=True, is_default=True,
                      registration='paid', registration_offer_id=knight.id, lapse_grace_days=3,
                      paid_since=utcnow() - timedelta(days=10))
    db.session.add(home)
    db.session.commit()
    clear_domain_cache()
    yield SimpleNamespace(app=app, client=app.test_client(), site=site, home=home, knight=knight, duke=duke, gift=gift,
                          baseline=api_baseline, base=f"https://{current_app.config['SERVER_NAME']}")
    clear_domain_cache()


def member(env, name='member', created_days_ago=1):
    user = make_user(env.baseline.instance_local, name, local=True)
    user.created = utcnow() - timedelta(days=created_days_ago)
    db.session.commit()
    return user


def lapsed_since(user, days):
    user.access_lapsed_at = utcnow() - timedelta(days=days)
    db.session.commit()
    return user


class TestTheDefaultDomainCanCharge:
    def test_paid_on_its_own_row_makes_it_paid(self, env):
        assert paid_registration(default_domain())

    def test_anyone_on_its_own_row_keeps_it_free(self, env):
        env.home.registration = 'anyone'
        db.session.commit()
        clear_domain_cache()
        assert not paid_registration(default_domain())

    def test_the_date_it_began_charging_is_known(self, env):
        assert default_domain().paid_since == env.home.paid_since


class TestLapsing:
    def test_a_member_who_joined_since_is_locked_after_the_grace(self, env):
        assert lapsed_since(member(env), days=4).access_has_lapsed()

    def test_not_within_the_grace(self, env):
        assert not lapsed_since(member(env), days=2).access_has_lapsed()

    def test_one_who_joined_before_it_charged_never_is(self, env):
        """PM5: grandfathered for good, even after holding a plan and letting it go."""
        assert not lapsed_since(member(env, created_days_ago=30), days=40).access_has_lapsed()

    def test_anyone_holding_a_role_never_is(self, env):
        user = lapsed_since(member(env), days=40)
        grant_permission(user, 'moderate all communities')
        assert not user.access_has_lapsed()

    def test_a_domain_with_no_start_date_keeps_the_old_rule(self, env):
        """Every sister domain made paid before this: no account is grandfathered by date."""
        env.home.paid_since = None
        db.session.commit()
        clear_domain_cache()
        assert lapsed_since(member(env, created_days_ago=30), days=40).access_has_lapsed()

    def test_a_pending_account_remembers_its_tier(self, env):
        user = member(env)
        user.pending_offer_id = env.duke.id
        db.session.commit()
        assert db.session.get(User, user.id).pending_offer_id == env.duke.id

    def test_a_removed_offer_forgets_it(self, env):
        user = member(env)
        user.pending_offer_id = env.gift.id
        db.session.commit()
        db.session.delete(env.gift)
        db.session.commit()
        db.session.expire_all()
        assert db.session.get(User, user.id).pending_offer_id is None


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def csrf(env):
    """One per test: the shared app context caches it."""
    with env.app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with env.client.session_transaction(base_url=env.base) as session:
        session['csrf_token'] = raw
    return token


class TestTheSwitch:
    """On the payments page, beside what it charges (plan M8): choosing what joining costs is monetization's."""

    @pytest.fixture
    def admin(self, env):
        holder = member(env, 'treasurer')
        grant_permission(holder, 'manage monetization')
        login(env.client, holder)
        return holder

    def switch(self, env, **values):
        return env.client.post('/admin/payments/membership', data={'save_membership': 'Save', 'csrf_token': csrf(env), **values},
                               base_url=env.base)

    def free(self, env):
        env.home.registration, env.home.paid_since = 'anyone', None
        db.session.commit()
        clear_domain_cache()

    def test_the_page_shows_it(self, env, admin):
        body = env.client.get('/admin/payments', base_url=env.base).get_data(as_text=True)
        assert 'name="membership_offer_id"' in body and 'Furcas' in body

    def test_turning_it_on_charges_and_dates_it(self, env, admin):
        self.free(env)
        before = utcnow()
        assert self.switch(env, membership='y', membership_offer_id=env.duke.id, membership_grace_days=3).status_code == 302
        row = db.session.get(SiteDomain, env.home.id)
        assert (row.registration, row.registration_offer_id, row.lapse_grace_days) == ('paid', env.duke.id, 3)
        assert row.paid_since >= before.replace(microsecond=0)
        assert paid_registration(default_domain())

    def test_saving_again_keeps_the_date(self, env, admin):
        since = env.home.paid_since
        assert self.switch(env, membership='y', membership_offer_id=env.knight.id, membership_grace_days=5).status_code == 302
        row = db.session.get(SiteDomain, env.home.id)
        assert row.paid_since == since and row.lapse_grace_days == 5

    def test_turning_it_off_frees_it(self, env, admin):
        self.switch(env, membership_offer_id=env.knight.id, membership_grace_days=3)
        row = db.session.get(SiteDomain, env.home.id)
        assert row.registration == 'anyone' and row.paid_since is None
        assert not paid_registration(default_domain())

    def test_a_one_off_cannot_be_what_joining_costs(self, env, admin):
        self.free(env)
        assert self.switch(env, membership='y', membership_offer_id=env.gift.id, membership_grace_days=3).status_code in (200, 302)
        assert db.session.get(SiteDomain, env.home.id).registration == 'anyone'

    def test_on_needs_a_tier(self, env, admin):
        self.free(env)
        self.switch(env, membership='y', membership_offer_id=0, membership_grace_days=3)
        assert db.session.get(SiteDomain, env.home.id).registration == 'anyone'

    def test_without_a_default_domain_row_there_is_nothing_to_switch(self, env, admin):
        db.session.delete(env.home)
        db.session.commit()
        clear_domain_cache()
        response = self.switch(env, membership='y', membership_offer_id=env.knight.id, membership_grace_days=3)
        assert response.status_code == 302 and SiteDomain.query.count() == 0

    def test_nobody_else_may(self, env):
        login(env.client, member(env, 'nobody'))
        assert self.switch(env, membership='y', membership_offer_id=env.knight.id,
                           membership_grace_days=3).status_code in (302, 403)
        assert db.session.get(SiteDomain, env.home.id).registration_offer_id == env.knight.id


class TestOtherWaysInAreClosed:
    """Paying needs the provider's page, which only /register leads to (plan M5): every other way of making an account
    is closed on a paid default domain, as on a paid sister domain (S9, S11). A referral code still admits (M4)."""

    def test_the_api_refuses(self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
        response = env.client.post('/api/alpha/user/register', base_url=env.base, json={
            'username': 'joiner', 'email': 'joiner@example.com', 'password': 'a-good-password',
            'password_verify': 'a-good-password'})
        assert response.status_code == 400 and 'registration_closed' in response.get_data(as_text=True)
        assert User.query.filter_by(user_name='joiner').first() is None

    def test_it_is_not_public(self, env):
        assert not public_registration_open(default_domain())

    def test_clients_are_told_it_is_closed(self, env):
        assert registration_mode_on(default_domain(), env.site) == 'Closed'
        assert not open_registrations(default_domain(), env.site)

    def test_with_referral_codes_clients_are_still_offered_the_code(self, env):
        env.site.registration_mode = 'ReferralCode'
        db.session.commit()
        assert registration_mode_on(default_domain(), env.site) == 'RequireApplication'
        assert not open_registrations(default_domain(), env.site)

    def test_an_oauth_signup_is_refused(self, env):
        with env.app.test_request_context('/', base_url=env.base):
            g.site = env.site
            assert refuse_new_oauth_account() is not None

    def test_a_directory_is_not_written(self, env):
        with patch('app.auth.util.sync_user_to_ldap') as write:
            sync_user_with_ldap(member(env), 'a-good-password')
        write.assert_not_called()

    def test_a_free_default_domain_is_open_as_before(self, env):
        env.home.registration = 'anyone'
        db.session.commit()
        clear_domain_cache()
        assert public_registration_open(default_domain())
        assert registration_mode_on(default_domain(), env.site) == 'Open'


FORM = {'user_name': 'joiner', 'hp_field': '', 'real_email': 'joiner@example.com', 'password': 'a-good-password',
        'password2': 'a-good-password', 'timezone': 'UTC', 'submit': 'Register'}


@pytest.fixture(autouse=True)
def no_captcha():
    with patch('app.auth.forms.captcha_enabled', return_value=False):
        yield


@pytest.fixture
def rendering(monkeypatch):
    """The registration page renders its CSRF field only with CSRF on, as the paid-domain tests find."""
    monkeypatch.setitem(current_app.config, 'WTF_CSRF_ENABLED', True)


@pytest.fixture
def provider():
    with patch('app.payments.adapters.stripe_adapter.StripeAdapter.checkout', return_value=CHECKOUT) as checkout:
        yield checkout


def checked(env, path):
    page = BeautifulSoup(env.client.get(path, base_url=env.base).get_data(as_text=True), 'html.parser')
    return [field['value'] for field in page.find_all('input', attrs={'name': 'tier'}) if field.has_attr('checked')]


def joiner():
    return User.query.filter_by(user_name='joiner').first()


class TestJoining:
    """Pick any tier, pay, join (PM1, PM2, plan M3)."""

    def register(self, env, **values):
        return env.client.post('/auth/register', data={**FORM, **values}, base_url=env.base)

    def test_the_page_lists_every_tier_with_its_price(self, env, rendering):
        body = env.client.get('/auth/register', base_url=env.base).get_data(as_text=True)
        assert 'Furcas' in body and '$6.66' in body and 'Astaroth' in body and '$16.66' in body
        assert 'Gift' not in body   # a one-off is no tier

    def test_a_tier_can_be_preselected(self, env, rendering):
        assert checked(env, f'/auth/register?offer={env.duke.id}') == [str(env.duke.id)]

    def test_otherwise_the_domains_own_tier_is(self, env, rendering):
        assert checked(env, '/auth/register') == [str(env.knight.id)]

    def test_joining_makes_a_pending_account_and_pays_for_the_tier_chosen(self, env, provider):
        response = self.register(env, tier=env.duke.id)
        assert response.status_code == 302 and response.headers['Location'] == CHECKOUT
        user = joiner()
        assert user.registration_pending_since is not None and user.pending_offer_id == env.duke.id
        assert provider.call_args.args[0].id == env.duke.id

    def test_with_no_choice_it_is_the_domains_own_tier(self, env, provider):
        self.register(env)
        assert joiner().pending_offer_id == env.knight.id and provider.call_args.args[0].id == env.knight.id

    def test_something_that_is_no_tier_is_refused(self, env, provider, rendering):
        self.register(env, tier=env.gift.id, csrf_token=csrf(env))
        assert joiner() is None and not provider.called

    def test_logging_in_again_goes_back_to_the_tier_chosen(self, env, provider):
        self.register(env, tier=env.duke.id)
        provider.reset_mock()
        with env.app.test_request_context('/', base_url=env.base):
            g.site = env.site
            registration_checkout(joiner())
        assert provider.call_args.args[0].id == env.duke.id

    def test_a_tier_no_longer_offered_falls_back_to_the_domains(self, env, provider):
        self.register(env, tier=env.duke.id)
        env.duke.active = False
        db.session.commit()
        provider.reset_mock()
        with env.app.test_request_context('/', base_url=env.base):
            g.site = env.site
            registration_checkout(joiner())
        assert provider.call_args.args[0].id == env.knight.id


class TestCodeOrPay:
    """With referral codes on, a code still admits for free; without one the visitor pays (plan M4)."""

    @pytest.fixture
    def code(self, env):
        env.site.registration_mode = 'ReferralCode'
        env.site.application_question = 'Your referral code'
        db.session.commit()
        set_setting(GENERATORS_SETTING, 'all')
        referrer = member(env, 'referrer')
        referrer.verified = True
        generate_code(referrer)
        db.session.commit()
        return referrer.referral_code

    def register(self, env, **values):
        return env.client.post('/auth/register', data={**FORM, **values}, base_url=env.base)

    def test_the_page_offers_both(self, env, rendering, code):
        body = env.client.get('/auth/register', base_url=env.base).get_data(as_text=True)
        assert 'name="question"' in body and 'Furcas' in body

    def test_a_browser_may_leave_the_code_empty(self, env, rendering, code):
        """The browser checks `required` before anything is sent: a code field marked so would never let anyone pay."""
        page = BeautifulSoup(env.client.get('/auth/register', base_url=env.base).get_data(as_text=True), 'html.parser')
        assert not page.find(attrs={'name': 'question'}).has_attr('required')

    def test_a_working_code_admits_for_free(self, env, code, provider):
        self.register(env, question=code, tier=env.duke.id)
        user = joiner()
        assert user is not None and user.registration_pending_since is None and not provider.called

    def test_a_wrong_code_is_refused_and_nothing_charged(self, env, code, provider):
        self.register(env, question='not-a-code', tier=env.duke.id)
        assert joiner() is None and not provider.called

    def test_no_code_means_paying(self, env, code, provider):
        response = self.register(env, question='', tier=env.duke.id)
        assert response.headers['Location'] == CHECKOUT and joiner().registration_pending_since is not None


class TestJoinButtons:
    """A visitor on a paid domain is asked to join, not to log in (plan M6)."""

    def page(self, env, body):
        db.session.add(CmsPage(url='/subscribe', title='Subscribe', body=body, body_html=markdown_to_html(body)))
        db.session.commit()
        return BeautifulSoup(env.client.get('/subscribe', base_url=env.base).get_data(as_text=True), 'html.parser')

    def test_a_tier_says_join_and_leads_to_registering_for_it(self, env):
        link = self.page(env, f'[checkout {env.duke.id}]').find('a', string='Join')
        assert link is not None and link['href'] == f'/auth/register?offer={env.duke.id}'

    def test_a_one_off_is_still_given_at_once(self, env):
        page = self.page(env, f'[checkout {env.gift.id}]')
        assert page.find('a', string='Join') is None and page.find('form', action=f'/donations/checkout/{env.gift.id}')

    def test_a_subscription_that_is_no_tier_still_asks_to_log_in(self, env):
        other = Offer(provider_kind='stripe', kind='subscription', title='Patron', offer_group='patrons',
                      provider_price_id='price_p', amount_minor=900, currency='usd', interval='month')
        db.session.add(other)
        db.session.commit()
        assert self.page(env, f'[checkout {other.id}]').find('a', string='Log in to subscribe') is not None

    def test_with_registration_closed_it_asks_to_log_in(self, env):
        env.site.registration_mode = 'Closed'
        db.session.commit()
        assert self.page(env, f'[checkout {env.duke.id}]').find('a', string='Join') is None

    def test_a_member_still_pays_at_once(self, env):
        login(env.client, member(env))
        assert self.page(env, f'[checkout {env.duke.id}]').find('form', action=f'/donations/checkout/{env.duke.id}')

    def test_the_plans_page_says_join_too(self, env):
        page = BeautifulSoup(env.client.get('/donations', base_url=env.base).get_data(as_text=True), 'html.parser')
        hrefs = {link['href'] for link in page.find_all('a', string='Join')}
        assert hrefs == {f'/auth/register?offer={env.knight.id}', f'/auth/register?offer={env.duke.id}'}
