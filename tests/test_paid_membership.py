"""Paid membership on the default domain (spec 2026-11-15-paid-membership-design.md, plan phase 1, M1-M7)."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup
from flask import current_app, g, session as flask_session
from flask_wtf.csrf import generate_csrf

from app import db
from app.models import CmsPage, MemberSubscription, Offer, PromoCode, PromoCodeUse, Referral, Site, SiteDomain, User, utcnow
from app.payments.gifts import REFERRAL_BENEFIT_SETTING, \
    referral_benefit, referral_gift_code
from app.utils import markdown_to_html
from app.auth.oauth_util import refuse_new_oauth_account
from app.auth.util import sync_user_with_ldap
from app.storefront import clear_domain_cache, default_domain, open_registrations, paid_registration, \
    public_registration_open, registration_mode_on
from app.payments.adapters.base import ProviderError
from app.payments.registration import registration_checkout, remove_unpaid_registrations
from app.referral import GENERATORS_SETTING, benefit_on, generate_code
from app.utils import get_setting, set_setting
from tests.factories import grant_permission, make_user, pay_link

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
        tiers = {field['value'] for field in BeautifulSoup(body, 'html.parser').find_all('input', attrs={'name': 'tier'})}
        assert tiers == {str(env.knight.id), str(env.duke.id)}   # a one-off is no tier

    def test_a_tier_can_be_preselected(self, env, rendering):
        assert checked(env, f'/auth/register?offer={env.duke.id}') == [str(env.duke.id)]

    def test_otherwise_the_domains_own_tier_is(self, env, rendering):
        assert checked(env, '/auth/register') == [str(env.knight.id)]

    def test_joining_makes_a_pending_account_and_pays_for_the_tier_chosen(self, env, provider):
        response = self.register(env, tier=env.duke.id)
        assert response.status_code == 200 and pay_link(response) == CHECKOUT   # the summary, then pay (X4)
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
        assert pay_link(response) == CHECKOUT and joiner().registration_pending_since is not None

    def test_a_referral_link_fills_in_its_code(self, env, rendering, code):
        assert self.code_field(env, f'/auth/register?ref={code}') == code

    def test_a_gift_code_link_fills_in_its_code(self, env, rendering):
        assert self.code_field(env, '/auth/register?code=Winter-Gift-2026') == 'winter-gift-2026'

    def test_a_posted_code_is_not_replaced_by_the_links(self, env, rendering, code):
        page = BeautifulSoup(self.register(env, question='typed-one-wrong').get_data(as_text=True), 'html.parser')
        assert page.find(attrs={'name': 'question'}).get_text().strip() == 'typed-one-wrong'

    def code_field(self, env, path):
        page = BeautifulSoup(env.client.get(path, base_url=env.base).get_data(as_text=True), 'html.parser')
        return page.find(attrs={'name': 'question'}).get_text().strip()


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

    def test_the_donations_page_offers_no_tiers(self, env):
        """Tiers are joined from the membership page's [checkout N] buttons (2026-11-16 DM1, DM2)."""
        page = BeautifulSoup(env.client.get('/donations', base_url=env.base).get_data(as_text=True), 'html.parser')
        assert not page.find_all('a', string='Join') and 'Furcas' not in page.get_text()


def gift_of(user):
    return MemberSubscription.query.filter_by(user_id=user.id, provider_kind='gift').first()


def default_gift(env, periods=1, offer=None):
    set_setting(REFERRAL_BENEFIT_SETTING, {'kind': 'gift', 'offer_id': (offer or env.knight).id, 'periods': periods})


class TestReferralCodesCarryGifts:
    """A member's referral code is a code row of its own, with the gift joining with it gives (PM7, plan G2)."""

    def referrer(self, env):
        set_setting(GENERATORS_SETTING, 'all')
        user = member(env, 'referrer')
        user.verified = True
        db.session.commit()
        return user

    def test_a_new_code_gives_the_default_gift(self, env):
        """Read when it is used, not copied onto the code (plan 2026-11-16 R3)."""
        default_gift(env, offer=env.duke, periods=1)
        user = self.referrer(env)
        generate_code(user)
        db.session.commit()
        row = referral_gift_code(user)
        assert (row.code, row.kind, row.owner_id) == (user.referral_code, 'gift', user.id)
        assert (row.benefit, row.offer_id, row.periods) == ('gift', env.duke.id, 1)   # its own (plan 2026-11-16 X1)

    def test_regenerating_keeps_the_gift_and_changes_the_code(self, env):
        default_gift(env, periods=1)
        user = self.referrer(env)
        generate_code(user)
        db.session.commit()
        row = referral_gift_code(user)
        row.benefit, row.offer_id, row.periods = 'gift', env.knight.id, 12   # an admin gave this member's code a year
        db.session.commit()
        generate_code(user)
        db.session.commit()
        again = referral_gift_code(user)
        assert again.id == row.id and again.periods == 12 and again.code == user.referral_code
        assert PromoCode.query.filter_by(owner_id=user.id).count() == 1

    def test_a_code_made_before_gifts_gets_the_default_when_first_used(self, env):
        user = self.referrer(env)
        generate_code(user)
        db.session.commit()
        PromoCode.query.filter_by(owner_id=user.id).delete()
        db.session.commit()
        default_gift(env, periods=2)
        assert referral_benefit(referral_gift_code(user), env.home).periods == 2

    def test_with_no_default_chosen_it_is_the_cheapest_tier_for_a_period(self, env):
        user = self.referrer(env)
        generate_code(user)
        db.session.commit()
        benefit = referral_benefit(referral_gift_code(user), env.home)
        assert (benefit.offer.id, benefit.periods) == (env.knight.id, 1)


class TestJoiningWithACode:
    """At /register on a paid default domain a working code admits for free, with its gift (PM3, plan G4)."""

    def register(self, env, **values):
        return env.client.post('/auth/register', data={**FORM, **values}, base_url=env.base)

    @pytest.fixture
    def referral(self, env):
        env.site.registration_mode = 'ReferralCode'
        env.site.application_question = 'Your code'
        db.session.commit()
        set_setting(GENERATORS_SETTING, 'all')
        referrer = member(env, 'referrer')
        referrer.verified = True
        generate_code(referrer)
        db.session.commit()
        return referrer

    def test_a_referral_code_starts_its_gift(self, env, referral, provider):
        default_gift(env, periods=1)
        self.register(env, question=referral.referral_code, tier=env.duke.id)
        user = joiner()
        assert user.registration_pending_since is None and not provider.called
        gift = gift_of(user)
        assert gift.offer_id == env.knight.id and gift.current_period_end > utcnow() + timedelta(days=29)
        assert Referral.query.filter_by(referred_id=user.id, referrer_id=referral.id).count() == 1

    def test_a_referral_code_always_gives_something(self, env, referral, provider):
        """Never nothing (plan 2026-11-16 X1): a default once set to nothing is automatic."""
        set_setting(REFERRAL_BENEFIT_SETTING, {'kind': 'none'})
        PromoCode.query.filter_by(owner_id=referral.id).delete()
        db.session.commit()
        self.register(env, question=referral.referral_code)
        assert gift_of(joiner()).offer_id == env.knight.id and not provider.called

    def test_a_gift_code_admits_with_referral_codes_off(self, env, provider):
        db.session.add(PromoCode(code='gift-year', kind='gift', offer_id=env.duke.id, periods=12))
        db.session.commit()
        self.register(env, question='GIFT-YEAR')
        user = joiner()
        assert user.registration_pending_since is None and gift_of(user).offer_id == env.duke.id and not provider.called

    def test_the_code_field_shows_with_referral_codes_off(self, env, rendering):
        page = BeautifulSoup(env.client.get('/auth/register', base_url=env.base).get_data(as_text=True), 'html.parser')
        field = page.find(attrs={'name': 'question'})
        assert field is not None and not field.has_attr('required')

    def test_a_wrong_code_is_refused_and_nothing_charged(self, env, provider, rendering):
        self.register(env, question='not-a-code', csrf_token=csrf(env))
        assert joiner() is None and not provider.called

    def test_a_members_referral_code_is_not_a_gift_code_with_referral_codes_off(self, env, provider, rendering):
        """Off, referral codes admit nobody (RC3); its gift row must not let it in by the gift door."""
        set_setting(GENERATORS_SETTING, 'all')
        default_gift(env)
        referrer = member(env, 'referrer')
        referrer.verified = True
        generate_code(referrer)
        db.session.commit()
        self.register(env, question=referrer.referral_code, csrf_token=csrf(env))
        assert joiner() is None


class TestRedeeming:
    """A member adds a gift code's period on /donations (plan G4)."""

    def redeem(self, env, code):
        return env.client.post('/donations/redeem', data={'code': code, 'csrf_token': csrf(env)}, base_url=env.base)

    def test_a_member_redeems_a_gift_code(self, env):
        user = member(env, 'redeemer')
        login(env.client, user)
        db.session.add(PromoCode(code='gift-month', kind='gift', offer_id=env.knight.id, periods=1))
        db.session.commit()
        assert self.redeem(env, 'GIFT-MONTH').status_code == 302
        assert gift_of(user) is not None

    def test_a_lapsed_member_renews_with_a_gift_code(self, env):
        """Locked out (M12), they may still renew, and a gift code is renewing (G4)."""
        user = lapsed_since(member(env, 'redeemer'), days=30)
        db.session.add(MemberSubscription(user_id=user.id, offer_id=env.knight.id, provider_kind='stripe',
                                          provider_reference='sub_old', status='ended', ended_at=user.access_lapsed_at))
        db.session.add(PromoCode(code='gift-month', kind='gift', offer_id=env.knight.id, periods=1))
        db.session.commit()
        login(env.client, user)
        assert user.access_has_lapsed()
        answer = self.redeem(env, 'GIFT-MONTH')
        assert answer.status_code == 302 and answer.headers['Location'].endswith('/donations')
        assert gift_of(user) is not None and user.access_lapsed_at is None and not user.access_has_lapsed()

    def test_a_lapsed_member_may_clear_a_held_code(self, env):
        user = lapsed_since(member(env, 'redeemer'), days=30)
        db.session.add(MemberSubscription(user_id=user.id, offer_id=env.knight.id, provider_kind='stripe',
                                          provider_reference='sub_old', status='ended', ended_at=user.access_lapsed_at))
        db.session.commit()
        login(env.client, user)
        with env.client.session_transaction(base_url=env.base) as session:
            session['discount_code'] = 'twenty'
        env.client.post('/donations/code/clear', data={'csrf_token': csrf(env)}, base_url=env.base)
        with env.client.session_transaction(base_url=env.base) as session:
            assert 'discount_code' not in session

    def test_a_wrong_code_changes_nothing(self, env):
        user = member(env, 'redeemer')
        login(env.client, user)
        assert self.redeem(env, 'nope').status_code == 302 and gift_of(user) is None

    def test_a_referral_code_is_for_joining(self, env):
        set_setting(GENERATORS_SETTING, 'all')
        default_gift(env)
        referrer = member(env, 'referrer')
        referrer.verified = True
        generate_code(referrer)
        db.session.commit()
        user = member(env, 'redeemer')
        login(env.client, user)
        self.redeem(env, referrer.referral_code)
        assert gift_of(user) is None

    def test_an_empty_code_changes_nothing(self, env):
        user = member(env, 'redeemer')
        login(env.client, user)
        assert self.redeem(env, '').status_code == 302 and gift_of(user) is None

    def test_a_visitor_is_sent_to_log_in(self, env):
        response = env.client.post('/donations/redeem', data={'code': 'x'}, base_url=env.base)
        assert response.status_code == 302 and '/auth/login' in response.headers['Location']

    def test_the_membership_page_offers_it_to_a_member(self, env):
        login(env.client, member(env, 'reader'))
        assert 'name="code"' in env.client.get('/user/settings/subscription', base_url=env.base).get_data(as_text=True)


class TestCodesAreRateLimited:
    """Typing a code at registration and on /donations shares one limit per address (security review 2026-11-15)."""

    @pytest.fixture
    def scopes(self, monkeypatch):
        seen = []

        class Limit:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def shared_limit(limit, scope):
            seen.append((limit, scope))
            return Limit()
        monkeypatch.setattr('app.payments.gifts.limiter.shared_limit', shared_limit)
        return seen

    def test_at_registration(self, env, scopes, provider, rendering):
        env.client.post('/auth/register', data={**FORM, 'question': 'whatever', 'csrf_token': csrf(env)}, base_url=env.base)
        assert scopes == [('10 per hour', 'gift_codes')]

    def test_paying_with_no_code_is_not_counted(self, env, scopes, provider):
        env.client.post('/auth/register', data={**FORM, 'question': ''}, base_url=env.base)
        assert scopes == []

    def test_on_redeeming(self, env, scopes):
        login(env.client, member(env, 'redeemer'))
        env.client.post('/donations/redeem', data={'code': 'whatever', 'csrf_token': csrf(env)}, base_url=env.base)
        assert scopes == [('10 per hour', 'gift_codes')]


class TestACodeThatRanOutMeanwhile:
    def test_makes_no_account_rather_than_a_free_one(self, env, provider):
        db.session.add(PromoCode(code='gift-last', kind='gift', offer_id=env.knight.id, periods=1))
        db.session.commit()
        with patch('app.auth.util.start_gift', return_value=None):
            env.client.post('/auth/register', data={**FORM, 'question': 'gift-last'}, base_url=env.base)
        assert joiner() is None and not provider.called


def a_discount(env, code='spring-twenty', **values):
    row = PromoCode(code=code, kind='percent', percent=20, **values)
    db.session.add(row)
    db.session.commit()
    return row


class TestJoiningWithADiscount:
    """A discount code typed at /register is held by the pending account and taken off its first payment (D1)."""

    def register(self, env, **values):
        return env.client.post('/auth/register', data={**FORM, **values}, base_url=env.base)

    def test_it_is_taken_off_the_tier_chosen(self, env, provider):
        code = a_discount(env)
        response = self.register(env, question='SPRING-TWENTY', tier=env.knight.id)
        user = joiner()
        assert pay_link(response) == CHECKOUT and user.registration_pending_since is not None
        assert user.pending_promo_code_id == code.id and provider.call_args.kwargs['discount'].id == code.id
        use = PromoCodeUse.query.one()
        assert (use.user_id, use.offer_id, use.expected_minor) == (user.id, env.knight.id, 532)

    def test_one_for_another_tier_is_refused_and_makes_no_account(self, env, provider, rendering):
        a_discount(env, offer_id=env.duke.id)
        self.register(env, question='spring-twenty', tier=env.knight.id, csrf_token=csrf(env))
        assert joiner() is None and not provider.called

    def test_logging_in_again_takes_it_off_again(self, env, provider):
        code = a_discount(env)
        self.register(env, question='spring-twenty', tier=env.knight.id)
        provider.reset_mock()
        with env.app.test_request_context('/', base_url=env.base):
            g.site = env.site
            registration_checkout(joiner())
        assert provider.call_args.kwargs['discount'].id == code.id and PromoCodeUse.query.count() == 1

    def test_a_code_gone_since_is_paid_in_full(self, env, provider):
        code = a_discount(env)
        self.register(env, question='spring-twenty', tier=env.knight.id)
        code.active = False
        db.session.commit()
        provider.reset_mock()
        with env.app.test_request_context('/', base_url=env.base):
            g.site = env.site
            page = registration_checkout(joiner())
        assert provider.call_args.kwargs.get('discount') is None
        # and the summary says so, and the code is held no longer (plan 2026-11-16 X5)
        assert 'Code spring-twenty has been switched off, so it takes nothing off. The full price applies.' \
            in page.get_data(as_text=True)
        assert joiner().pending_promo_code_id is None

    def test_an_expired_code_is_told_with_its_last_day(self, env, provider):
        code = a_discount(env)
        self.register(env, question='spring-twenty', tier=env.knight.id)
        code.expires_at = utcnow() - timedelta(days=2)
        db.session.commit()
        with env.app.test_request_context('/', base_url=env.base):
            g.site = env.site
            page = registration_checkout(joiner())
        assert f'Code spring-twenty expired on {code.expires_at:%Y-%m-%d}' in page.get_data(as_text=True)


class TestAMembersDiscount:
    """On /donations a discount code is held for the member's next checkout (D1)."""

    def redeem(self, env, code):
        return env.client.post('/donations/redeem', data={'code': code, 'csrf_token': env.token}, base_url=env.base)

    @pytest.fixture
    def user(self, env):
        user = member(env, 'buyer')
        login(env.client, user)
        env.token = csrf(env)   # one per test: the app context caches it
        return user

    def test_it_is_held_and_shown(self, env, user):
        a_discount(env)
        self.redeem(env, 'Spring-Twenty')
        with env.client.session_transaction(base_url=env.base) as session:
            assert session['discount_code'] == 'spring-twenty'
        assert 'spring-twenty' in env.client.get('/donations', base_url=env.base).get_data(as_text=True)

    def test_checkout_takes_it_off_and_reserves_it(self, env, user, provider):
        code = a_discount(env)
        self.redeem(env, 'spring-twenty')
        response = env.client.post(f'/donations/checkout/{env.knight.id}', data={'csrf_token': env.token}, base_url=env.base)
        assert pay_link(response) == CHECKOUT and provider.call_args.kwargs['discount'].id == code.id
        assert PromoCodeUse.query.one().expected_minor == 532
        with env.client.session_transaction(base_url=env.base) as session:
            assert 'discount_code' not in session

    def test_one_that_does_not_apply_is_told_and_the_full_price_applies(self, env, user, provider):
        """No longer stops the checkout: the summary says so (plan 2026-11-16 X5)."""
        a_discount(env, offer_id=env.duke.id)
        self.redeem(env, 'spring-twenty')
        response = env.client.post(f'/donations/checkout/{env.knight.id}', data={'csrf_token': env.token}, base_url=env.base)
        assert pay_link(response) == CHECKOUT and provider.call_args.kwargs['discount'] is None
        assert 'Code spring-twenty is not for Furcas' in response.get_data(as_text=True)

    def test_it_can_be_cleared(self, env, user, provider):
        a_discount(env)
        self.redeem(env, 'spring-twenty')
        env.client.post('/donations/code/clear', data={'csrf_token': env.token}, base_url=env.base)
        env.client.post(f'/donations/checkout/{env.knight.id}', data={'csrf_token': env.token}, base_url=env.base)
        assert provider.call_args.kwargs.get('discount') is None


    def test_clearing_needs_its_token(self, env, user, monkeypatch):
        a_discount(env)
        self.redeem(env, 'spring-twenty')
        monkeypatch.setitem(current_app.config, 'WTF_CSRF_ENABLED', True)
        env.client.post('/donations/code/clear', data={}, base_url=env.base)
        with env.client.session_transaction(base_url=env.base) as session:
            assert session.get('discount_code') == 'spring-twenty'


    def test_a_provider_that_is_down_keeps_the_code_for_another_try(self, env, user):
        """Its answer is a page and a 503, not a redirect: the code stays held (found in a browser check)."""
        a_discount(env)
        self.redeem(env, 'spring-twenty')
        with patch('app.payments.adapters.stripe_adapter.StripeAdapter.checkout', side_effect=ProviderError('down')):
            response = env.client.post(f'/donations/checkout/{env.knight.id}', data={'csrf_token': env.token},
                                       base_url=env.base)
        assert response.status_code == 503
        with env.client.session_transaction(base_url=env.base) as session:
            assert session.get('discount_code') == 'spring-twenty'


# ---------------------------------------------------------------------------------------------------------------------
# What a member's referral code gives (plan 2026-11-16-referral-benefits.md, R1-R5): one benefit, a gift or a discount or
# nothing, chosen by an admin from what the code owner's domain offers; by default one billing period of its cheapest
# tier.

@pytest.fixture
def referrer(env):
    env.site.registration_mode = 'ReferralCode'
    env.site.application_question = 'Your code'
    db.session.commit()
    set_setting(GENERATORS_SETTING, 'all')
    user = member(env, 'referrer')
    user.verified = True
    generate_code(user)
    db.session.commit()
    return user


def row_of(user) -> PromoCode:
    row = referral_gift_code(user)
    db.session.commit()
    return row


def fresh_row(user) -> PromoCode:
    """The member's code made again, so that it takes the default as it is now (each code keeps its own, X1)."""
    PromoCode.query.filter_by(owner_id=user.id).delete()
    db.session.commit()
    return row_of(user)




def join_with(env, **values):
    return env.client.post('/auth/register', data={**FORM, **values}, base_url=env.base)




class TestReferralDefault:
    def test_it_is_one_billing_period_of_the_cheapest_tier(self, env, referrer):
        benefit = referral_benefit(row_of(referrer), env.home)
        assert (benefit.offer.id, benefit.periods, benefit.discount) == (env.knight.id, 1, None)

    def test_a_yearly_cheapest_tier_gives_a_year(self, env, referrer):
        env.knight.interval, env.knight.amount_minor = 'year', 100
        db.session.commit()
        assert referral_benefit(fresh_row(referrer), env.home).periods == 1   # one year

    def test_a_code_keeps_what_it_was_made_with_when_the_default_changes(self, env, referrer):
        """Each code stores its own (plan 2026-11-16 X1); a code made after the change takes the new default."""
        row = row_of(referrer)
        set_setting(REFERRAL_BENEFIT_SETTING, {'kind': 'gift', 'offer_id': env.duke.id, 'periods': 2})
        benefit = referral_benefit(row, env.home)
        assert (benefit.offer.id, benefit.periods) == (env.knight.id, 1)
        generate_code(referrer)   # a new code: the same row, its benefit kept
        db.session.commit()
        assert referral_benefit(row_of(referrer), env.home).offer.id == env.knight.id
        other = member(env, 'newcomer')
        other.verified = True
        generate_code(other)
        db.session.commit()
        assert (row_of(other).benefit, row_of(other).offer_id, row_of(other).periods) == ('gift', env.duke.id, 2)

    def test_it_can_be_a_discount(self, env, referrer):
        discount = a_discount(env)
        set_setting(REFERRAL_BENEFIT_SETTING, {'kind': 'discount', 'discount_id': discount.id})
        assert referral_benefit(fresh_row(referrer), env.home).discount == discount

    def test_it_is_never_nothing(self, env, referrer):
        """A default once set to nothing (plan 2026-11-16 R3) is automatic now (X1)."""
        set_setting(REFERRAL_BENEFIT_SETTING, {'kind': 'none'})
        PromoCode.query.filter_by(owner_id=referrer.id).delete()
        db.session.commit()
        benefit = referral_benefit(row_of(referrer), env.home)
        assert (benefit.offer.id, benefit.periods) == (env.knight.id, 1)

    def test_a_domain_with_no_tiers_gives_nothing_by_default(self, env, referrer):
        Offer.query.filter_by(kind='subscription').update({Offer.active: False})
        db.session.commit()
        assert referral_benefit(fresh_row(referrer), env.home) is None


class TestReferralBenefitAwayFromHome:
    """RC10 (2026-11-19): a code is used on any domain. Its own gift or discount was chosen from its home domain's
    tiers, so elsewhere it gives the site's default for that domain, and keeps its own."""

    def test_another_domains_member_gives_the_default_here_and_keeps_its_own(self, env, referrer):
        shop = SiteDomain(domain='shop.example', is_default=False, registration='anyone')
        db.session.add(shop)
        db.session.commit()
        clear_domain_cache()
        referrer.site_domain_id = shop.id
        row = row_of(referrer)
        row.benefit, row.offer_id, row.periods = 'gift', env.duke.id, 12
        db.session.commit()
        benefit = benefit_on(referrer, row, default_domain())
        assert (benefit.offer.id, benefit.periods) == (env.knight.id, 1)   # the default here
        db.session.refresh(row)
        assert (row.benefit, row.offer_id, row.periods) == ('gift', env.duke.id, 12)   # its own, untouched

    def test_at_home_it_gives_its_own(self, env, referrer):
        row = row_of(referrer)
        row.benefit, row.offer_id, row.periods = 'gift', env.duke.id, 12
        db.session.commit()
        benefit = benefit_on(referrer, row, default_domain())
        assert (benefit.offer.id, benefit.periods) == (env.duke.id, 12)


class TestTheApiCannotTakeADiscount:
    """Security review 2026-11-19: the API's signup has no checkout, so a referral code that gives a discount, which
    the web form sends to payment, must not make a free account there."""

    def test_a_discount_code_is_refused(self, env, referrer, monkeypatch):
        monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
        discount = a_discount(env)
        row = row_of(referrer)
        row.benefit, row.discount_code_id = 'discount', discount.id
        db.session.commit()
        body = {'username': 'freeloader', 'email': 'f@example.com', 'password': 'a-good-password',
                'password_verify': 'a-good-password', 'answer': referrer.referral_code}
        with patch('app.auth.util.send_email_verification'), patch('app.auth.util.sync_user_with_ldap'), \
                patch('app.auth.util.captcha_enabled', return_value=False):
            response = env.client.post('/api/alpha/user/register', json=body, base_url=env.base)
        assert response.status_code == 400 and 'invalid_referral_code' in response.get_data(as_text=True)
        assert User.query.filter_by(user_name='freeloader').count() == 0
        assert PromoCodeUse.query.filter_by(promo_code_id=row.id).count() == 0


class TestReferralCodesOwn:
    def test_a_gift_of_its_own(self, env, referrer):
        row = row_of(referrer)
        row.benefit, row.offer_id, row.periods = 'gift', env.duke.id, 12
        db.session.commit()
        benefit = referral_benefit(row, env.home)
        assert (benefit.offer.id, benefit.periods, benefit.discount) == (env.duke.id, 12, None)

    def test_a_gift_with_no_units_is_one_billing_period(self, env, referrer):
        row = row_of(referrer)
        row.benefit, row.offer_id, row.periods = 'gift', env.duke.id, None
        db.session.commit()
        assert referral_benefit(row, env.home).periods == 1

    def test_a_discount_of_its_own(self, env, referrer):
        discount = a_discount(env)
        row = row_of(referrer)
        row.benefit, row.discount_code_id = 'discount', discount.id
        db.session.commit()
        benefit = referral_benefit(row, env.home)
        assert (benefit.offer, benefit.discount) == (None, discount)

    @pytest.mark.parametrize('stored', [None, 'none'], ids=['made_before', 'once_nothing'])
    def test_one_without_its_own_takes_the_default_and_keeps_it(self, env, referrer, stored):
        row = row_of(referrer)
        row.benefit, row.offer_id, row.periods = stored, None, None
        db.session.commit()
        benefit = referral_benefit(row, env.home)
        assert (benefit.offer.id, benefit.periods) == (env.knight.id, 1)
        assert (row.benefit, row.offer_id, row.periods) == ('gift', env.knight.id, 1)

    @pytest.mark.parametrize('broken', ['gone_offer', 'gone_discount', 'not_a_discount'])
    def test_a_choice_that_is_gone_falls_back_to_the_default(self, env, referrer, broken):
        """Not to nothing, which admits for free (audit 2026-11-16)."""
        row = row_of(referrer)
        if broken == 'gone_offer':
            row.benefit, row.offer_id = 'gift', None
        elif broken == 'gone_discount':
            row.benefit, row.discount_code_id = 'discount', None
        else:
            gift_code = PromoCode(code='gift-month', kind='gift', offer_id=env.knight.id, periods=1)
            db.session.add(gift_code)
            db.session.flush()
            row.benefit, row.discount_code_id = 'discount', gift_code.id
        set_setting(REFERRAL_BENEFIT_SETTING, {'kind': 'gift', 'offer_id': env.duke.id, 'periods': 7})
        db.session.commit()
        benefit = referral_benefit(row, env.home)
        assert (benefit.offer.id, benefit.periods) == (env.duke.id, 7)

    @pytest.mark.parametrize('default', [{'kind': 'gift', 'offer_id': 999999}, {'kind': 'discount', 'discount_id': 999999},
                                         {'kind': 'nonsense'}, 'not a dict'],
                             ids=['gone_tier', 'gone_discount', 'unknown_kind', 'not_a_dict'])
    def test_a_default_that_is_gone_or_garbled_is_automatic(self, env, referrer, default):
        set_setting(REFERRAL_BENEFIT_SETTING, default)
        benefit = referral_benefit(row_of(referrer), env.home)
        assert (benefit.offer.id, benefit.periods) == (env.knight.id, 1)

    def test_a_new_code_stores_the_default_as_its_own(self, env, referrer):
        row = row_of(referrer)
        assert (row.benefit, row.offer_id, row.periods, row.discount_code_id) == ('gift', env.knight.id, 1, None)

    def test_where_nothing_can_be_given_it_stores_nothing(self, env, referrer):
        Offer.query.filter_by(kind='subscription').update({Offer.active: False})
        PromoCode.query.filter_by(owner_id=referrer.id).delete()
        db.session.commit()
        assert row_of(referrer).benefit is None


class TestJoiningWithAReferralBenefit:
    def test_the_default_gift_admits_for_free(self, env, referrer, provider):
        join_with(env, question=referrer.referral_code, tier=env.duke.id)
        user = joiner()
        assert user.registration_pending_since is None and not provider.called
        assert gift_of(user).offer_id == env.knight.id
        assert Referral.query.filter_by(referred_id=user.id, referrer_id=referrer.id).count() == 1

    def test_a_discount_admits_as_a_paying_account_with_it_held(self, env, referrer, provider):
        discount = a_discount(env)
        row = row_of(referrer)
        row.benefit, row.discount_code_id = 'discount', discount.id
        db.session.commit()
        answer = join_with(env, question=referrer.referral_code, tier=env.duke.id)
        user = joiner()
        assert answer.status_code == 200 and pay_link(answer) == CHECKOUT
        assert user.registration_pending_since is not None and user.pending_promo_code_id == discount.id
        assert gift_of(user) is None
        assert provider.call_args.kwargs['discount'] == discount
        assert PromoCodeUse.query.filter_by(promo_code_id=discount.id, user_id=user.id).one().expected_minor == 1332
        assert Referral.query.filter_by(referred_id=user.id, referrer_id=referrer.id).count() == 1

    def test_a_discount_for_another_tier_is_refused(self, env, referrer, provider, rendering):
        discount = a_discount(env, offer_id=env.knight.id)
        row = row_of(referrer)
        row.benefit, row.discount_code_id = 'discount', discount.id
        db.session.commit()
        body = join_with(env, question=referrer.referral_code, tier=env.duke.id, csrf_token=csrf(env)).get_data(as_text=True)
        assert joiner() is None and 'That code cannot be used for this tier.' in body and not provider.called

    @pytest.mark.parametrize('limit', ['inactive', 'expired', 'used_up'])
    def test_a_discount_past_its_limits_is_refused(self, env, referrer, provider, rendering, limit):
        values = {'inactive': {'active': False}, 'expired': {'expires_at': utcnow() - timedelta(minutes=1)},
                  'used_up': {'max_uses': 1}}[limit]
        discount = a_discount(env, **values)
        if limit == 'used_up':
            db.session.add(PromoCodeUse(promo_code_id=discount.id, user_id=referrer.id, payment_id=None))
        row = row_of(referrer)
        row.benefit, row.discount_code_id = 'discount', discount.id
        db.session.commit()
        join_with(env, question=referrer.referral_code, tier=env.knight.id, csrf_token=csrf(env))
        assert joiner() is None and not provider.called

    @pytest.mark.parametrize('limit', ['inactive', 'expired', 'used_up'])
    def test_the_referral_codes_own_limits_hold_for_its_discount(self, env, referrer, provider, rendering, limit):
        """Security review 2026-11-16: the referral code's own switch, last day and uses, as for its gift (R5)."""
        discount = a_discount(env)
        row = row_of(referrer)
        row.benefit, row.discount_code_id = 'discount', discount.id
        if limit == 'inactive':
            row.active = False
        elif limit == 'expired':
            row.expires_at = utcnow() - timedelta(minutes=1)
        else:
            row.max_uses = 1
            db.session.add(PromoCodeUse(promo_code_id=row.id, user_id=referrer.id))
        db.session.commit()
        join_with(env, question=referrer.referral_code, tier=env.knight.id, csrf_token=csrf(env))
        assert joiner() is None and not provider.called

    def test_a_discount_signup_is_a_use_of_the_referral_code(self, env, referrer, provider):
        discount = a_discount(env)
        row = row_of(referrer)
        row.benefit, row.discount_code_id, row.max_uses = 'discount', discount.id, 1
        db.session.commit()
        join_with(env, question=referrer.referral_code, tier=env.knight.id)
        assert PromoCodeUse.query.filter_by(promo_code_id=row.id, user_id=joiner().id).count() == 1

    @pytest.mark.parametrize('limit', ['inactive', 'expired', 'used_up'])
    def test_a_gift_it_can_no_longer_give_refuses_the_account(self, env, referrer, provider, limit):
        """Not admitted for free, which would then be free for good (audit 2026-11-16; R5)."""
        row = row_of(referrer)
        if limit == 'inactive':
            row.active = False
        elif limit == 'expired':
            row.expires_at = utcnow() - timedelta(minutes=1)
        else:
            row.max_uses = 1
            db.session.add(PromoCodeUse(promo_code_id=row.id, user_id=referrer.id))
        db.session.commit()
        answer = join_with(env, question=referrer.referral_code, tier=env.knight.id)
        assert answer.status_code == 302 and joiner() is None and not provider.called
        assert Referral.query.count() == 0

    def test_a_gift_taken_by_somebody_else_first_refuses_the_account(self, env, referrer, provider):
        """The referral code's row is re-checked under its lock as the account is made (start_gift); losing the race
        makes no account, as a gift code does."""
        with patch('app.referral.start_gift', return_value=None):
            join_with(env, question=referrer.referral_code, tier=env.knight.id)
        assert joiner() is None and Referral.query.count() == 0

    def test_an_abandoned_discount_signup_is_cleared_with_its_uses(self, env, referrer, provider):
        discount = a_discount(env)
        row = row_of(referrer)
        row.benefit, row.discount_code_id = 'discount', discount.id
        db.session.commit()
        join_with(env, question=referrer.referral_code, tier=env.knight.id)
        user = joiner()
        user.registration_pending_since = utcnow() - timedelta(days=3)
        db.session.commit()
        assert remove_unpaid_registrations() == 1
        db.session.expire_all()
        assert joiner() is None and Referral.query.count() == 0
        assert PromoCodeUse.query.filter(PromoCodeUse.promo_code_id.in_([row.id, discount.id])).count() == 0

    def test_on_a_domain_that_does_not_charge_it_gives_nothing(self, env, referrer, provider):
        env.home.registration = 'anyone'
        db.session.commit()
        clear_domain_cache()
        join_with(env, question=referrer.referral_code)
        user = joiner()
        assert user is not None and gift_of(user) is None
        assert Referral.query.filter_by(referred_id=user.id).count() == 1

    def test_nothing_to_give_refuses_the_account(self, env, referrer, provider):
        """Never admitted with nothing (plan 2026-11-16 X2), as one past its limits isn't (R6)."""
        with patch('app.referral.referral_benefit', return_value=None):
            join_with(env, question=referrer.referral_code, tier=env.knight.id)
        assert joiner() is None and Referral.query.count() == 0 and not provider.called


@pytest.fixture
def treasurer(env):
    g.admin_ids = []
    user = member(env, 'treasurer')
    grant_permission(user, 'manage monetization')
    with env.client.session_transaction(base_url=env.base) as session:
        session['_user_id'], session['_fresh'] = str(user.id), True
    return user


def admin_post(env, path, **data):
    with env.app.test_request_context():
        g.pop('csrf_token', None)
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with env.client.session_transaction(base_url=env.base) as session:
        session['csrf_token'] = raw
    return env.client.post(path, data={'csrf_token': token, **data}, base_url=env.base)


def benefit_options(env, path, name='benefit'):
    page = BeautifulSoup(env.client.get(path, base_url=env.base).get_data(as_text=True), 'html.parser')
    return {option['value']: option.get_text(strip=True) for option in page.find('select', attrs={'name': name}).find_all('option')}


class TestReferralBenefitAdmin:
    def test_a_referral_codes_choices_are_its_domains_tiers_and_discounts(self, env, referrer, treasurer):
        discount = a_discount(env)
        mine = PromoCode.query.filter_by(owner_id=referrer.id).first() or row_of(referrer)
        choices = benefit_options(env, f'/admin/payments/codes/{mine.id}')
        assert set(choices) == {f'gift:{env.knight.id}', f'gift:{env.duke.id}', f'discount:{discount.id}'}   # never nothing (X1)

    def test_a_discount_for_an_offer_not_here_is_not_a_choice(self, env, referrer, treasurer):
        elsewhere = Offer(provider_kind='stripe', kind='subscription', title='Elsewhere', offer_group='other',
                          provider_price_id='price_e', amount_minor=900, currency='usd', interval='month', active=False)
        db.session.add(elsewhere)
        db.session.commit()
        discount = a_discount(env, offer_id=elsewhere.id)
        assert f'discount:{discount.id}' not in benefit_options(env, f'/admin/payments/codes/{row_of(referrer).id}')

    def test_setting_a_discount(self, env, referrer, treasurer):
        discount = a_discount(env)
        row = row_of(referrer)
        admin_post(env, f'/admin/payments/codes/{row.id}', benefit=f'discount:{discount.id}', active='y')
        row = db.session.get(PromoCode, row.id)
        assert (row.benefit, row.discount_code_id, row.offer_id) == ('discount', discount.id, None)

    def test_setting_a_gift_replaces_the_discount(self, env, referrer, treasurer):
        discount = a_discount(env)
        row = row_of(referrer)
        row.benefit, row.discount_code_id = 'discount', discount.id
        db.session.commit()
        admin_post(env, f'/admin/payments/codes/{row.id}', benefit=f'gift:{env.duke.id}', periods='2', active='y')
        row = db.session.get(PromoCode, row.id)
        assert (row.benefit, row.offer_id, row.periods, row.discount_code_id) == ('gift', env.duke.id, 2, None)

    @pytest.mark.parametrize('choice', ['gift-one-off', 'none', 'default'])
    def test_a_choice_not_offered_is_refused(self, env, referrer, treasurer, choice):
        row = row_of(referrer)
        benefit = {'gift-one-off': f'gift:{env.gift.id}'}.get(choice, choice)
        admin_post(env, f'/admin/payments/codes/{row.id}', benefit=benefit, active='y')
        row = db.session.get(PromoCode, row.id)
        assert (row.benefit, row.offer_id) == ('gift', env.knight.id)

    def test_the_default_is_saved(self, env, treasurer):
        discount = a_discount(env)
        admin_post(env, '/admin/payments/codes/default', benefit=f'discount:{discount.id}')
        assert get_setting(REFERRAL_BENEFIT_SETTING) == {'kind': 'discount', 'discount_id': discount.id}
        admin_post(env, '/admin/payments/codes/default', benefit=f'gift:{env.duke.id}', periods='')
        assert get_setting(REFERRAL_BENEFIT_SETTING) == {'kind': 'gift', 'offer_id': env.duke.id, 'periods': None}
        admin_post(env, '/admin/payments/codes/default', benefit='auto')
        assert get_setting(REFERRAL_BENEFIT_SETTING) == {'kind': 'auto'}
        admin_post(env, '/admin/payments/codes/default', benefit='none')   # never nothing (X1)
        assert get_setting(REFERRAL_BENEFIT_SETTING) == {'kind': 'auto'}

    def test_the_default_starts_automatic(self, env, treasurer):
        assert 'auto' in benefit_options(env, '/admin/payments/codes')
        page = BeautifulSoup(env.client.get('/admin/payments/codes', base_url=env.base).get_data(as_text=True), 'html.parser')
        assert page.find('select', attrs={'name': 'benefit'}).find('option', selected=True)['value'] == 'auto'

    def test_the_list_says_a_default_gift_and_the_edit_page_shows_a_last_day(self, env, referrer, treasurer):
        row = row_of(referrer)
        row.expires_at = utcnow() + timedelta(days=9)
        db.session.commit()
        cell = BeautifulSoup(env.client.get('/admin/payments/codes', base_url=env.base).get_data(as_text=True),
                             'html.parser').find('tr', id=f'code-{row.id}').get_text()
        assert 'Furcas for 1 month' in cell
        page = env.client.get(f'/admin/payments/codes/{row.id}', base_url=env.base).get_data(as_text=True)
        assert row.expires_at.strftime('%Y-%m-%d') in page

    def test_the_list_says_what_each_referral_code_gives(self, env, referrer, treasurer):
        discount = a_discount(env)
        row = row_of(referrer)
        row.benefit, row.discount_code_id = 'discount', discount.id
        db.session.commit()
        cell = BeautifulSoup(env.client.get('/admin/payments/codes', base_url=env.base).get_data(as_text=True),
                             'html.parser').find('tr', id=f'code-{row.id}').get_text()
        assert 'spring-twenty' in cell


class TestTheReferralPageSaysWhatTheCodeGives:
    """Not "approved straight away" where joining is paid for: the code's gift or discount (owner, 2026-11-16)."""

    def page(self, env, user):
        with env.client.session_transaction(base_url=env.base) as session:
            session['_user_id'], session['_fresh'] = str(user.id), True
        return env.client.get('/user/settings/referrals', base_url=env.base).get_data(as_text=True)

    def test_a_gift(self, env, referrer):
        body = self.page(env, referrer)
        assert 'joining with your code gives them 1 month of Furcas, free.' in body
        assert 'approved straight away' not in body

    @pytest.mark.parametrize('for_tier', [True, False], ids=['one_tier', 'any_plan'])
    def test_a_percent_discount(self, env, referrer, for_tier):
        discount = a_discount(env, offer_id=env.duke.id if for_tier else None)
        row = row_of(referrer)
        row.benefit, row.discount_code_id = 'discount', discount.id
        db.session.commit()
        expected = '20% off their first payment for Astaroth' if for_tier else '20% off their first payment for any plan'
        assert f'joining with your code gives them {expected}.' in self.page(env, referrer)

    def test_an_amount_off(self, env, referrer):
        set_setting('payment_currency', 'usd')
        discount = PromoCode(code='five-off', kind='amount', amount_minor=500)
        db.session.add(discount)
        db.session.commit()
        row = row_of(referrer)
        row.benefit, row.discount_code_id = 'discount', discount.id
        db.session.commit()
        assert '$5.00 off their first payment for any plan' in self.page(env, referrer)

    def test_before_a_code_is_made_it_says_what_one_would_give(self, env, referrer):
        PromoCode.query.filter_by(owner_id=referrer.id).delete()
        referrer.referral_code = None
        db.session.commit()
        assert '1 month of Furcas, free' in self.page(env, referrer)

    def test_nothing_to_give_says_it_cannot_be_used(self, env, referrer):
        with patch('app.user.referrals.benefit_on', return_value=None):
            body = self.page(env, referrer)
        assert 'Your code cannot be used to join at the moment' in body

    def test_where_joining_is_free_it_is_approved_straight_away(self, env, referrer):
        env.home.registration = 'anyone'
        db.session.commit()
        clear_domain_cache()
        assert 'their account is approved straight away' in self.page(env, referrer)
