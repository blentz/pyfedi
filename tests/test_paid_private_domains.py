"""A domain that charges at registration and requires login, beside a public one on the same site (spec M10-M12, phase 3)."""
import importlib.util
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from flask import current_app, g, session as flask_session
from flask_wtf.csrf import generate_csrf

from app import db
from app.domain_config import effective
from app.models import MemberSubscription, Offer, OfferDomain, Payment, PromoCode, Referral, Site, SiteDomain, User, utcnow
from app.payments.core import apply_event, cancel_subscription
from app.payments.events import Paid, SubscriptionEnded, SubscriptionStarted
from app.payments.registration import registration_offer, remove_unpaid_registrations
from app.payments.gifts import REFERRAL_BENEFIT_SETTING
from app.referral import GENERATORS_SETTING, generate_code
from app.storefront import clear_domain_cache
from app.utils import set_setting
from tests.schema_history import DomainConfigMigrations
from tests.factories import grant_permission, make_user, pay_link

PAID = 'https://paid.example'
PUBLIC = 'https://public.example'
CHECKOUT = 'https://checkout.example/session'

pytestmark = pytest.mark.usefixtures('oauth_providers_configured')


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    g.admin_ids = []
    site = db.session.get(Site, 1)
    site.private_instance = False
    site.registration_mode = 'Open'
    monkeypatch.setitem(current_app.config, 'STRIPE_SECRET_KEY', 'sk_test')
    offer = Offer(provider_kind='stripe', kind='subscription', title='Membership', offer_group='membership',
                  provider_price_id='price_m', amount_minor=500, currency='usd', interval='month')
    db.session.add(offer)
    db.session.flush()
    db.session.add(SiteDomain(domain=current_app.config['SERVER_NAME'], scheme='https', federates=True, is_default=True))
    paid = SiteDomain(domain='paid.example', scheme='https', federates=False, paid_membership=True,
                      registration_offer_id=offer.id, private_instance=True)
    public = SiteDomain(domain='public.example', scheme='https', federates=False)
    db.session.add_all([paid, public])
    db.session.commit()
    clear_domain_cache()
    yield SimpleNamespace(app=app, client=app.test_client(), offer=offer, paid=paid, public=public, site=site,
                          baseline=api_baseline)
    clear_domain_cache()


@pytest.fixture(autouse=True)
def no_captcha():
    with patch('app.auth.forms.captcha_enabled', return_value=False):
        yield


@pytest.fixture
def stripe_session():
    with patch('app.payments.adapters.stripe_adapter.StripeAdapter.checkout', return_value=CHECKOUT) as checkout:
        yield checkout


@pytest.fixture
def cancelled():
    with patch('app.payments.core.cancel_at_provider') as cancel:
        yield cancel


FORM = {'user_name': 'joiner', 'hp_field': '', 'real_email': 'joiner@example.com', 'password': 'a-good-password',
        'password2': 'a-good-password', 'timezone': 'UTC', 'submit': 'Register'}


def register(env, base=PAID, **overrides):
    return env.client.post('/auth/register', data={**FORM, **overrides}, base_url=base)


def joiner() -> User:
    return User.query.filter_by(user_name='joiner').first()


def login(client, user, base=PAID):
    with client.session_transaction(base_url=base) as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def real_csrf(env) -> str:
    """A POST behind login_required carries a real token, whatever WTF_CSRF_ENABLED says."""
    with env.app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with env.client.session_transaction() as session:
        session['csrf_token'] = raw
    return token


def pay(env, user, event='evt_1', reference='sub_1', amount=500):
    apply_event('stripe', event, [Paid(reference=f'in_{reference}', amount_minor=amount, currency='usd', user_id=user.id,
                                       offer_id=env.offer.id),
                                  SubscriptionStarted(reference=reference, user_id=user.id, offer_id=env.offer.id)])


def a_member(env, name='paying') -> User:
    member = make_user(env.baseline.instance_local, name, local=True)
    member.site_domain_id = env.paid.id
    db.session.commit()
    return member


class TestRegistering:
    def test_the_page_says_what_it_costs(self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'WTF_CSRF_ENABLED', True)
        body = env.client.get('/auth/register', base_url=PAID).get_data(as_text=True)

        assert 'Membership' in body and '$5.00' in body and 'or use a username' not in body

    def test_a_signup_makes_a_pending_account_and_goes_to_the_provider(self, env, stripe_session):
        with patch('app.auth.util.send_email_verification') as mail:
            response = register(env)

        assert (response.status_code, pay_link(response)) == (200, CHECKOUT)
        user = joiner()
        assert user.registration_pending_since is not None and user.verified is False
        assert user.site_domain_id == env.paid.id
        mail.assert_not_called()

    def test_a_banned_address_is_not_charged(self, env, stripe_session):
        with patch('app.auth.util.user_ip_banned', return_value=True):
            register(env)

        assert joiner() is None
        stripe_session.assert_not_called()

    def test_with_no_way_to_pay_registration_is_closed(self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'STRIPE_SECRET_KEY', '')

        register(env)

        assert joiner() is None
        assert 'Registration is closed' in env.client.get('/auth/register', base_url=PAID).get_data(as_text=True)

    def test_the_sites_closed_mode_closes_it(self, env, stripe_session):
        env.site.registration_mode = 'Closed'
        db.session.commit()

        register(env)

        assert joiner() is None

    @pytest.mark.parametrize('mode', ['RequireApplication', 'ReferralCode'], ids=['application', 'referral'])
    def test_no_application_answer_or_referral_code_is_needed(self, env, stripe_session, mode):
        env.site.registration_mode = mode
        env.site.application_question = 'Why?'
        db.session.commit()

        assert pay_link(register(env)) == CHECKOUT
        assert joiner().registration_pending_since is not None

    def test_a_provider_failure_is_explained(self, env):
        with patch('app.payments.adapters.stripe_adapter.StripeAdapter.checkout', side_effect=_provider_error()):
            response = register(env)

        assert response.status_code == 503
        assert 'try again' in response.get_data(as_text=True)


def _provider_error():
    from app.payments.adapters.base import ProviderError
    return ProviderError('no')


class TestJoiningWithAnAdminsCode:
    """Owner 2026-11-20: an admin's gift or discount code admits on any domain that charges for joining and sells its
    tier there, not only on the default domain."""

    def code(self, kind='gift', offer=None, **values):
        row = PromoCode(code='paid-gift-code', kind=kind, offer_id=offer.id if offer else None, **values)
        db.session.add(row)
        db.session.commit()
        return row

    def test_a_gift_for_its_tier_admits_for_free(self, env, stripe_session):
        self.code(offer=env.offer, periods=1)
        register(env, question='PAID-GIFT-CODE')
        user = joiner()
        assert user is not None and user.registration_pending_since is None and not stripe_session.called
        gift = MemberSubscription.query.filter_by(user_id=user.id, provider_kind='gift').one()
        assert gift.offer_id == env.offer.id and user.site_domain_id == env.paid.id

    def test_a_gift_for_a_tier_not_sold_there_is_refused(self, env, stripe_session, monkeypatch):
        monkeypatch.setitem(current_app.config, 'WTF_CSRF_ENABLED', True)
        elsewhere = Offer(provider_kind='stripe', kind='subscription', title='Elsewhere', offer_group='other',
                          provider_price_id='price_e', amount_minor=900, currency='usd', interval='month')
        db.session.add(elsewhere)
        db.session.flush()
        db.session.add(OfferDomain(offer_id=elsewhere.id, site_domain_id=env.public.id))
        db.session.commit()
        self.code(offer=elsewhere, periods=1)
        body = register(env, question='paid-gift-code', csrf_token=csrf(env)).get_data(as_text=True)
        assert joiner() is None and not stripe_session.called
        assert 'That code cannot be used on this site.' in body

    def test_a_discount_for_any_tier_is_held_for_the_first_payment(self, env, stripe_session):
        discount = self.code(kind='percent', percent=20)
        assert pay_link(register(env, question='paid-gift-code')) == CHECKOUT
        assert joiner().pending_promo_code_id == discount.id

    def test_the_link_fills_the_code_in(self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'WTF_CSRF_ENABLED', True)
        page = BeautifulSoup(env.client.get('/auth/register?code=Paid-Gift-Code', base_url=PAID).get_data(as_text=True),
                             'html.parser')
        field = page.find(attrs={'name': 'question'})
        assert field.get_text().strip() == 'paid-gift-code' and not field.has_attr('required')
        assert 'Gift code, if you have one' in page.get_text() and 'Referral or gift code' not in page.get_text()


class TestJoiningWithAReferralCode:
    """Owner 2026-11-20: a paid domain other than the default takes a referral code (was the default domain alone, G4),
    and admits with what it gives there: the site's default for that domain (RC10)."""

    @pytest.fixture
    def referrer(self, env):
        env.site.registration_mode = 'ReferralCode'
        db.session.commit()
        set_setting(GENERATORS_SETTING, 'all')
        user = make_user(env.baseline.instance_local, 'referrer', local=True)
        user.verified = True
        generate_code(user)
        db.session.commit()
        return user

    def test_it_admits_for_free_with_the_gift_it_gives_there(self, env, referrer, stripe_session):
        register(env, question=referrer.referral_code)
        user = joiner()
        assert user is not None and user.registration_pending_since is None and not stripe_session.called
        assert user.site_domain_id == env.paid.id
        assert MemberSubscription.query.filter_by(user_id=user.id, provider_kind='gift').one().offer_id == env.offer.id
        assert Referral.query.filter_by(referred_id=user.id).one().referrer_id == referrer.id

    def test_the_form_asks_for_a_referral_or_gift_code(self, env, referrer, monkeypatch):
        monkeypatch.setitem(current_app.config, 'WTF_CSRF_ENABLED', True)
        page = env.client.get(f'/auth/register?ref={referrer.referral_code}', base_url=PAID).get_data(as_text=True)
        assert 'Referral or gift code' in page and referrer.referral_code in page

    def elsewhere(self, env):
        """A subscription sold on the public domain only."""
        offer = Offer(provider_kind='stripe', kind='subscription', title='Elsewhere', offer_group='other',
                      provider_price_id='price_e', amount_minor=900, currency='usd', interval='month')
        db.session.add(offer)
        db.session.flush()
        db.session.add(OfferDomain(offer_id=offer.id, site_domain_id=env.public.id))
        db.session.commit()
        return offer

    def test_a_default_gift_of_a_tier_not_sold_there_gives_its_own_cheapest(self, env, referrer, stripe_session):
        """Security review 2026-11-20: never a membership of a tier the domain does not sell."""
        set_setting(REFERRAL_BENEFIT_SETTING, {'kind': 'gift', 'offer_id': self.elsewhere(env).id, 'periods': 12})
        register(env, question=referrer.referral_code)
        gift = MemberSubscription.query.filter_by(user_id=joiner().id, provider_kind='gift').one()
        assert gift.offer_id == env.offer.id and not stripe_session.called

    def test_its_own_gift_of_a_tier_not_sold_there_is_refused(self, env, referrer, stripe_session, monkeypatch):
        """A code at home on the paid domain, its own gift a tier sold elsewhere: no free account (security review
        2026-11-20)."""
        monkeypatch.setitem(current_app.config, 'WTF_CSRF_ENABLED', True)
        referrer.site_domain_id = env.paid.id
        row = PromoCode.query.filter_by(owner_id=referrer.id).one()
        row.benefit, row.offer_id, row.periods = 'gift', self.elsewhere(env).id, 12
        db.session.commit()
        register(env, question=referrer.referral_code, csrf_token=csrf(env))
        assert joiner() is None and not stripe_session.called

    def test_the_api_refuses_a_gift_of_a_tier_not_sold_there(self, env, referrer, monkeypatch):
        monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
        referrer.site_domain_id = env.paid.id
        row = PromoCode.query.filter_by(owner_id=referrer.id).one()
        row.benefit, row.offer_id, row.periods = 'gift', self.elsewhere(env).id, 12
        db.session.commit()
        with patch('app.auth.util.send_email_verification'), patch('app.auth.util.captcha_enabled', return_value=False):
            response = env.client.post('/api/alpha/user/register', base_url=PAID, json={
                'username': 'joiner', 'email': 'joiner@example.com', 'password': 'a-good-password',
                'password_verify': 'a-good-password', 'answer': referrer.referral_code})
        assert response.status_code == 400 and 'invalid_referral_code' in response.get_data(as_text=True)
        assert joiner() is None

    def test_a_domain_that_refuses_codes_refuses_it(self, env, referrer, stripe_session, monkeypatch):
        monkeypatch.setitem(current_app.config, 'WTF_CSRF_ENABLED', True)
        env.paid.referral_codes = False
        db.session.commit()
        clear_domain_cache()
        body = register(env, question=referrer.referral_code, csrf_token=csrf(env)).get_data(as_text=True)
        assert joiner() is None and not stripe_session.called and 'Invalid code' in body


def csrf(env):
    with env.app.test_request_context():
        g.pop('csrf_token', None)
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with env.client.session_transaction(base_url=PAID) as session:
        session['csrf_token'] = raw
    return token


class TestEveryOtherWayIn:
    def test_the_api_refuses(self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
        response = env.client.post('/api/alpha/user/register', base_url=PAID, json={
            'username': 'joiner', 'email': 'joiner@example.com', 'password': 'a-good-password', 'password_verify': 'a-good-password'})

        assert response.status_code == 400
        assert joiner() is None

    def test_clients_are_told_it_is_closed(self, env):
        from app.storefront import domain_for_host, registration_mode_on
        assert registration_mode_on(domain_for_host('paid.example'), env.site) == 'Closed'

    def test_oauth_buttons_are_not_offered(self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'MASTODON_OAUTH_CLIENT_ID', 'id')
        monkeypatch.setitem(current_app.config, 'WTF_CSRF_ENABLED', True)
        assert 'mastodon_login' not in env.client.get('/auth/register', base_url=PAID).get_data(as_text=True)

    def test_a_directory_is_not_written(self, env, stripe_session):
        member = a_member(env)
        with patch('app.auth.util.sync_user_to_ldap') as write:
            from app.auth.util import sync_user_with_ldap
            sync_user_with_ldap(member, 'a-good-password')

        write.assert_not_called()


class TestAPendingAccount:
    @pytest.fixture
    def pending(self, env, stripe_session):
        register(env)
        stripe_session.reset_mock()
        return joiner()

    def test_the_right_password_goes_back_to_the_provider_with_no_session(self, env, pending, stripe_session):
        response = env.client.post('/auth/login', base_url=PAID, data={
            'user_name': 'joiner', 'password': 'a-good-password', 'low_bandwidth_mode': '', 'submit': 'Log in'})

        assert pay_link(response) == CHECKOUT
        with env.client.session_transaction(base_url=PAID) as session:
            assert '_user_id' not in session

    def test_a_wrong_password_learns_nothing(self, env, pending, stripe_session):
        env.client.post('/auth/login', base_url=PAID, data={
            'user_name': 'joiner', 'password': 'another-wrong-password', 'low_bandwidth_mode': '', 'submit': 'Log in'})

        stripe_session.assert_not_called()

    def test_login_user_refuses_it(self, env, pending):
        assert pending.is_active is False

    def test_the_api_refuses_a_login(self, env, pending, monkeypatch):
        monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
        response = env.client.post('/api/alpha/user/login', base_url=PAID,
                                   json={'username': 'joiner', 'password': 'a-good-password'})

        assert response.status_code == 400
        assert 'jwt' not in (response.get_json() or {})

    def test_it_gets_no_verification_mail(self, env, pending):
        with patch('app.auth.routes.send_email_verification') as mail:
            env.client.post('/auth/resend_email', base_url=PAID, data={'email': 'joiner@example.com'})

        mail.assert_not_called()

    def test_its_token_verifies_nothing(self, env, pending):
        env.client.get(f'/auth/verify_email/{pending.verification_token}', base_url=PAID)

        db.session.refresh(pending)
        assert pending.verified is False

    def test_coming_back_from_the_provider_activates_nothing(self, env, pending):
        response = env.client.get('/donations/return/success', base_url=PAID)

        db.session.refresh(pending)
        assert response.status_code == 200
        assert 'as soon as the payment is confirmed' in response.get_data(as_text=True)
        assert pending.registration_pending_since is not None

    def test_a_failed_payment_says_how_to_try_again(self, env, pending):
        assert 'Log in' in env.client.get('/donations/return/failure', base_url=PAID).get_data(as_text=True)


class TestActivation:
    @pytest.fixture
    def pending(self, env, stripe_session, cancelled):
        register(env)
        return joiner()

    def test_a_verified_payment_activates_and_sends_the_mail(self, env, pending):
        with patch('app.payments.core.send_verification_email') as mail, \
                patch('app.payments.core.get_setting', return_value=True):
            pay(env, pending)

        db.session.refresh(pending)
        assert pending.registration_pending_since is None and pending.is_active
        mail.assert_called_once()

    def test_with_verification_off_the_account_is_ready(self, env, pending):
        with patch('app.payments.core.get_setting', return_value=False), \
                patch('app.payments.core.finalize_user_setup') as finish:
            pay(env, pending)

        finish.assert_called_once()

    def test_mail_failing_still_activates(self, env, pending):
        with patch('app.payments.core.send_verification_email', side_effect=OSError('down')), \
                patch('app.payments.core.get_setting', return_value=True):
            pay(env, pending)

        db.session.refresh(pending)
        assert pending.registration_pending_since is None

    def test_a_retried_webhook_does_not_mail_twice(self, env, pending):
        with patch('app.payments.core.send_verification_email') as mail, \
                patch('app.payments.core.get_setting', return_value=True):
            pay(env, pending)
            pay(env, pending)

        mail.assert_called_once()

    def test_the_wrong_amount_leaves_it_pending(self, env, pending):
        pay(env, pending, amount=1)

        db.session.refresh(pending)
        assert pending.registration_pending_since is not None
        assert MemberSubscription.query.filter_by(user_id=pending.id).one().status == 'mismatch'


class TestRemoval:
    @pytest.fixture
    def stale(self, env, stripe_session, cancelled):
        register(env)
        user = joiner()
        user.registration_pending_since = utcnow() - timedelta(hours=49)
        db.session.commit()
        return user

    def test_an_unpaid_account_goes_and_frees_its_name(self, env, stale):
        assert remove_unpaid_registrations() == 1
        db.session.expire_all()
        assert joiner() is None

    def test_a_recent_one_stays(self, env, stale):
        stale.registration_pending_since = utcnow() - timedelta(hours=47)
        db.session.commit()

        assert remove_unpaid_registrations() == 0

    def test_a_mismatched_subscription_is_cancelled_with_it(self, env, stale):
        pay(env, stale, amount=1)
        with patch('app.payments.registration.cancel_at_provider') as cancel:
            remove_unpaid_registrations()

        cancel.assert_called_once_with('stripe', 'sub_1')

    def test_a_payment_for_a_removed_account_cancels_the_subscription(self, env, stale, cancelled):
        gone = stale.id
        remove_unpaid_registrations()
        db.session.expire_all()

        apply_event('stripe', 'evt_late', [Paid(reference='in_late', amount_minor=500, currency='usd', user_id=gone,
                                                offer_id=env.offer.id),
                                           SubscriptionStarted(reference='sub_late', user_id=gone, offer_id=env.offer.id)])

        cancelled.assert_called_once_with('stripe', 'sub_late')
        assert Payment.query.filter_by(provider_reference='in_late').one().user_id is None


class TestTwoDomainsOnOneSite:
    def test_a_stranger_is_sent_to_log_in_on_the_private_one_only(self, env):
        assert env.client.get('/communities', base_url=PAID).status_code == 302
        assert env.client.get('/communities', base_url=PUBLIC).status_code == 200

    def test_the_api_is_closed_on_the_private_one_only(self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
        assert env.client.get('/api/alpha/post/list', base_url=PAID).status_code == 401
        assert env.client.get('/api/alpha/post/list', base_url=PUBLIC).status_code == 200

    def test_a_member_gets_in(self, env):
        login(env.client, a_member(env))

        assert env.client.get('/communities', base_url=PAID).status_code == 200

    def test_nothing_answers_another_server(self, env):
        client = env.client
        assert client.get('/.well-known/nodeinfo', base_url=PAID).status_code == 404
        assert client.get('/', base_url=PAID, headers={'Accept': 'application/activity+json'}).status_code == 404
        assert client.get('/c/general/outbox', base_url=PAID).status_code == 404

    def test_a_federation_page_sends_a_browser_on_to_its_own_check(self, env):
        assert env.client.get('/u/nobody', base_url=PAID).status_code == 404


class TestLapsing:
    @pytest.fixture
    def member(self, env, cancelled):
        member = a_member(env)
        with patch('app.payments.core.get_setting', return_value=True):
            pay(env, member)
        return member

    def end(self, env, member, days_ago=None):
        apply_event('stripe', 'evt_end', [SubscriptionEnded(reference='sub_1')])
        db.session.refresh(member)
        if days_ago is not None:
            member.access_lapsed_at = utcnow() - timedelta(days=days_ago)
            db.session.commit()

    def test_a_subscription_ending_starts_the_grace_period(self, env, member):
        self.end(env, member)

        assert member.access_lapsed_at is not None and not member.access_has_lapsed()

    def test_within_grace_nothing_changes(self, env, member):
        self.end(env, member, days_ago=6)
        login(env.client, member)

        assert env.client.get('/communities', base_url=PAID).status_code == 200

    def test_past_grace_every_page_leads_to_renewal(self, env, member):
        self.end(env, member, days_ago=8)
        login(env.client, member)

        response = env.client.get('/communities', base_url=PAID)

        # Settings -> Subscription, which says so and leads to the plans (2026-11-16 DM4)
        assert response.status_code == 302 and response.headers['Location'].endswith('/user/settings/subscription')

    @pytest.mark.parametrize('path', ['/donations', '/user/settings/subscription', '/user/settings/import_export',
                                      '/delete_account', '/auth/logout'],
                             ids=['donations', 'renew', 'export', 'delete', 'logout'])
    def test_past_grace_they_can_renew_or_leave(self, env, member, path, monkeypatch):
        monkeypatch.setitem(current_app.config, 'WTF_CSRF_ENABLED', True)
        self.end(env, member, days_ago=8)
        login(env.client, member)

        response = env.client.get(path, base_url=PAID)

        assert '/user/settings/subscription' not in response.headers.get('Location', '')

    def test_an_htmx_request_gets_nothing_to_swap_in(self, env, member):
        self.end(env, member, days_ago=8)
        login(env.client, member)

        assert env.client.get('/communities', base_url=PAID, headers={'HX-Request': 'true'}).status_code == 403

    def test_a_session_on_the_api_is_refused(self, env, member, monkeypatch):
        monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
        self.end(env, member, days_ago=8)
        login(env.client, member)

        response = env.client.get('/api/alpha/post/list', base_url=PAID)

        assert response.status_code == 403 and response.get_json() == {'error': 'access_lapsed'}

    def test_a_token_is_refused(self, env, member, monkeypatch):
        monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
        self.end(env, member, days_ago=8)
        member.verified = True
        member.password_updated_at = None
        db.session.commit()
        response = env.client.get('/api/alpha/user/unread_count', base_url=PAID,
                                  headers={'Authorization': f'Bearer {member.encode_jwt_token()}'})

        assert response.status_code == 403 and response.get_json() == {'error': 'access_lapsed'}

    def test_an_rss_token_stops_working(self, env, member):
        self.end(env, member, days_ago=8)
        member.rss_token = 'readertoken'
        db.session.commit()

        assert env.client.get('/index/feed?token=readertoken', base_url=PAID).status_code == 404

    def test_renewing_restores_access_at_once_and_nothing_was_lost(self, env, member):
        self.end(env, member, days_ago=8)
        login(env.client, member)
        with patch('app.payments.core.get_setting', return_value=True):
            pay(env, member, event='evt_2', reference='sub_2')

        db.session.refresh(member)
        assert member.access_lapsed_at is None and member.deleted is False
        assert env.client.get('/communities', base_url=PAID).status_code == 200

    def test_a_plan_change_in_the_group_never_lapses(self, env, member, cancelled):
        with patch('app.payments.core.get_setting', return_value=True):
            pay(env, member, event='evt_2', reference='sub_2')   # ends sub_1, which is in the same group

        db.session.refresh(member)
        assert MemberSubscription.query.filter_by(provider_reference='sub_1').one().status == 'ended'
        assert member.access_lapsed_at is None

    def test_an_account_that_never_held_a_plan_never_lapses(self, env):
        made_by_an_admin = a_member(env, 'staffmade')
        login(env.client, made_by_an_admin)

        assert made_by_an_admin.access_lapsed_at is None
        assert env.client.get('/communities', base_url=PAID).status_code == 200

    def test_an_admin_is_never_shut_out(self, env, member):
        self.end(env, member, days_ago=8)
        with patch.object(User, 'is_admin', return_value=True):
            assert not member.access_has_lapsed()

    def test_a_member_elsewhere_is_unaffected(self, env, cancelled):
        elsewhere = make_user(env.baseline.instance_local, 'elsewhere', local=True)
        elsewhere.site_domain_id = env.public.id
        db.session.commit()
        with patch('app.payments.core.get_setting', return_value=True):
            pay(env, elsewhere)
        apply_event('stripe', 'evt_end', [SubscriptionEnded(reference='sub_1')])

        db.session.refresh(elsewhere)
        assert elsewhere.access_lapsed_at is None and not elsewhere.access_has_lapsed()

    def test_a_domain_that_stops_charging_lets_them_back(self, env, member):
        self.end(env, member, days_ago=8)
        env.paid.set_paid_membership(False)
        db.session.commit()
        clear_domain_cache()

        assert not member.access_has_lapsed()


class TestCancelling:
    def test_on_a_paid_domain_it_runs_to_the_end_of_its_month(self, env, cancelled):
        """Charged no more at once; access to the end of its month, or its renewal date if sooner (plan 2026-11-15
        B6, replacing S22's provider's period end)."""
        member = a_member(env)
        with patch('app.payments.core.get_setting', return_value=True):
            pay(env, member)
        row = MemberSubscription.query.filter_by(provider_reference='sub_1').one()
        row.renews_at = utcnow() + timedelta(days=20)
        db.session.commit()
        with patch('app.payments.adapters.stripe_adapter.StripeAdapter.cancel') as cancel:
            cancel_subscription(row)

        cancel.assert_called_once_with('sub_1')
        db.session.refresh(member)
        assert row.status == 'active' and row.ends_at == row.renews_at and member.access_lapsed_at is None

    def test_the_page_shows_the_date_and_no_second_cancel(self, env, cancelled):
        member = a_member(env)
        with patch('app.payments.core.get_setting', return_value=True):
            pay(env, member)
        row = MemberSubscription.query.filter_by(provider_reference='sub_1').one()
        row.ends_at = utcnow() + timedelta(days=20)
        db.session.commit()
        login(env.client, member)

        body = env.client.get('/user/settings/subscription', base_url=PAID).get_data(as_text=True)   # Settings -> Subscription (2026-11-16 DM3)

        assert row.ends_at.strftime('%Y-%m-%d') in body and 'Cancel subscription' not in body

    def test_a_fee_to_belong_here_is_not_offered_as_a_donation(self, env):
        """/donations is for donations; a membership tier is offered on the membership page (2026-11-16 DM1, DM2)."""
        login(env.client, a_member(env))

        body = env.client.get('/donations', base_url=PAID).get_data(as_text=True)

        assert f'/donations/checkout/{env.offer.id}' not in body and 'Donate regularly' not in body

    def test_nor_elsewhere(self, env):
        elsewhere = make_user(env.baseline.instance_local, 'elsewhere', local=True)
        elsewhere.site_domain_id = env.public.id
        db.session.commit()
        login(env.client, elsewhere, base=PUBLIC)

        assert f'/donations/checkout/{env.offer.id}' not in env.client.get('/donations', base_url=PUBLIC).get_data(as_text=True)

    def test_the_period_ending_lapses_them(self, env, cancelled):
        member = a_member(env)
        with patch('app.payments.core.get_setting', return_value=True):
            pay(env, member)
        row = MemberSubscription.query.filter_by(provider_reference='sub_1').one()
        row.ends_at = utcnow()
        db.session.commit()

        apply_event('stripe', 'evt_end', [SubscriptionEnded(reference='sub_1')])

        db.session.refresh(member)
        assert row.ends_at is None and member.access_lapsed_at is not None

    def test_elsewhere_a_regular_donation_also_runs_to_its_months_end(self, env, cancelled):
        member = make_user(env.baseline.instance_local, 'elsewhere', local=True)
        member.site_domain_id = env.public.id
        db.session.commit()
        with patch('app.payments.core.get_setting', return_value=True):
            pay(env, member)
        row = MemberSubscription.query.filter_by(provider_reference='sub_1').one()
        with patch('app.payments.adapters.stripe_adapter.StripeAdapter.cancel') as cancel:
            cancel_subscription(row)

        cancel.assert_called_once_with('sub_1')
        assert row.status == 'active' and row.ends_at > utcnow()


class TestDomainSettings:
    @pytest.fixture
    def admin(self, env):
        admin = make_user(env.baseline.instance_local, 'domainadmin', local=True)
        grant_permission(admin, 'manage domains')
        grant_permission(admin, 'manage monetization')
        login(env.client, admin, base='https://' + current_app.config['SERVER_NAME'])
        return admin

    def save(self, env, row, **data):
        fields = {'domain': str(row.id), 'registration_mode': '', 'private_instance': 'n', 'registration_offer_id': '0',
                  'lapse_grace_days': '7', 'submit': 'Save', 'csrf_token': real_csrf(env), **data}
        return env.client.post('/admin/misc', data=fields, follow_redirects=True).get_data(as_text=True)

    def test_a_domain_can_be_made_paid_and_private(self, env, admin):
        before = utcnow()
        self.save(env, env.public, paid_membership='y', registration_offer_id=str(env.offer.id), private_instance='y',
                  lapse_grace_days='14')

        db.session.refresh(env.public)
        assert (env.public.paid_membership, env.public.registration_offer_id, env.public.private_instance,
                env.public.lapse_grace_days) == (True, env.offer.id, True, 14)
        assert env.public.paid_since >= before.replace(microsecond=0)   # it began charging now (plan M2)

    def test_referral_codes_can_be_turned_off(self, env, admin):
        """RC11a: the settings page sets whether a referral code opens the domain while it is closed."""
        assert env.public.referral_codes is True   # the default keeps RC11
        self.save(env, env.public, registration_mode='Closed')
        db.session.refresh(env.public)
        assert env.public.referral_codes is False and env.public.registration_mode == 'Closed'   # an unticked box

    def test_referral_codes_can_be_turned_on(self, env, admin):
        env.public.referral_codes = False
        db.session.commit()
        self.save(env, env.public, registration_mode='Closed', referral_codes='y')
        db.session.refresh(env.public)
        assert env.public.referral_codes is True

    def test_the_form_opens(self, env, admin, monkeypatch):
        monkeypatch.setitem(current_app.config, 'WTF_CSRF_ENABLED', True)
        assert 'Members pay a subscription' in env.client.get(f'/admin/misc?domain={env.public.id}').get_data(as_text=True)

    def test_the_old_settings_address_opens_the_misc_page_with_the_domain_picked(self, env, admin):
        response = env.client.get(f'/admin/domains/{env.public.id}/settings')
        assert response.status_code == 302 and response.headers['Location'].endswith(f'/admin/misc?domain={env.public.id}')

    def test_the_first_domain_has_no_such_fields(self, env, admin):
        """The first domain's registration and login are its registration mode and private instance, on its own page,
        which `manage domains` alone does not open: it opens a sister's instead."""
        default = SiteDomain.query.filter_by(is_default=True).one()
        body = env.client.get(f'/admin/misc?domain={default.id}').get_data(as_text=True)
        assert f'name="domain" value="{default.id}"' not in body

    def test_paid_needs_an_offer(self, env, admin):
        body = self.save(env, env.public, paid_membership='y')

        db.session.refresh(env.public)
        assert env.public.paid_membership is False and 'needs a subscription' in body

    def test_an_onion_cannot_be_paid(self, env, admin):
        onion = SiteDomain(domain='abc.onion', scheme='http', federates=False)
        db.session.add(onion)
        db.session.commit()

        body = self.save(env, onion, paid_membership='y', registration_offer_id=str(env.offer.id))

        db.session.refresh(onion)
        assert onion.paid_membership is False and 'Not a valid choice' in body   # Stripe isn't on offer there

    def test_a_federating_domain_cannot_turn_login_on(self, env, admin):
        federating = SiteDomain(domain='fed.example', scheme='https', federates=True)
        db.session.add(federating)
        db.session.commit()

        body = self.save(env, federating, private_instance='y')

        db.session.refresh(federating)
        assert effective(federating)['private_instance'] is False and 'already left' in body

    def test_a_federating_domain_already_on_can_stay_on(self, env, admin):
        federating = SiteDomain(domain='fed.example', scheme='https', federates=True, private_instance=True)
        db.session.add(federating)
        db.session.commit()

        self.save(env, federating, private_instance='y', lapse_grace_days='9')

        db.session.refresh(federating)
        assert federating.private_instance is True and federating.lapse_grace_days == 9

    def test_choosing_an_offer_needs_the_money_permission(self, env):
        admin = make_user(env.baseline.instance_local, 'domainsonly', local=True)
        grant_permission(admin, 'manage domains')
        login(env.client, admin, base='https://' + current_app.config['SERVER_NAME'])

        body = self.save(env, env.public, paid_membership='y', registration_offer_id=str(env.offer.id))

        db.session.refresh(env.public)
        assert env.public.paid_membership is False and 'manage monetization' in body

    def test_an_offer_a_domain_charges_cannot_be_deleted_or_stopped(self, env, admin):
        token = real_csrf(env)   # one per test: `g` keeps it, across clients
        env.client.post(f'/admin/payments/offers/{env.offer.id}/delete', data={'csrf_token': token})
        assert db.session.get(Offer, env.offer.id) is not None

        env.client.post(f'/admin/payments/offers/{env.offer.id}/edit', data={
            'title': 'Membership', 'price_id': 'price_m', 'offer_group': 'membership', 'position': '0', 'save': 'Save',
            'csrf_token': token})
        db.session.refresh(env.offer)
        assert env.offer.active is True


class TestWhatsLeftOver:
    def test_no_offer_is_asked_for_on_a_domain_that_doesnt_charge(self, env):
        with env.app.test_request_context('/', base_url=PUBLIC):
            from app.storefront import resolve_storefront
            resolve_storefront()
            assert registration_offer() is None

    def test_a_pending_account_logging_in_after_the_offer_went_is_told_so(self, env, stripe_session):
        register(env)
        env.offer.active = False
        db.session.commit()

        response = env.client.post('/auth/login', base_url=PAID, data={
            'user_name': 'joiner', 'password': 'a-good-password', 'low_bandwidth_mode': '', 'submit': 'Log in'})

        assert response.status_code == 503

    def test_a_failing_removal_leaves_everything(self, env, stripe_session):
        register(env)
        joiner().registration_pending_since = utcnow() - timedelta(hours=49)
        db.session.commit()
        with patch('app.payments.registration.cancel_at_provider'), \
                patch('sqlalchemy.orm.Session.delete', side_effect=RuntimeError('no')), pytest.raises(RuntimeError):
            remove_unpaid_registrations()

        db.session.expire_all()
        assert joiner() is not None

    def test_the_terms_box_stays_when_there_are_terms(self, env, stripe_session):
        env.site.tos_url = 'https://example.com/terms'
        db.session.commit()

        with patch('app.auth.util.render_template', return_value='the form again'):
            assert register(env).get_data(as_text=True) == 'the form again'   # the box wasn't ticked
        assert joiner() is None

    def test_an_api_login_writes_nothing_to_a_directory(self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
        member = a_member(env)
        member.set_password('a-good-password')
        db.session.commit()
        with patch('app.shared.auth.sync_user_to_ldap') as write:
            response = env.client.post('/api/alpha/user/login', base_url=PAID,
                                       json={'username': member.user_name, 'password': 'a-good-password'})

        assert response.status_code == 200
        write.assert_not_called()


class TestTheMigration:
    @pytest.mark.parametrize('private', [True, False], ids=['private', 'public'])
    def test_each_other_domain_takes_the_sites_setting(self, env, private):
        env.paid.paid_membership, env.paid.registration_offer_id = False, None
        env.site.private_instance = private
        db.session.commit()
        connection = db.session.connection()
        migration, later = _migration(), DomainConfigMigrations()
        with Operations.context(MigrationContext.configure(connection)):
            later.downgrade()   # the columns it tests moved to domain_config
            migration.downgrade()
            migration.upgrade()
            rows = dict(connection.execute(text('SELECT domain, login_required FROM site_domain')).fetchall())
        db.session.rollback()

        assert rows['public.example'] is private and rows['paid.example'] is private
        assert rows[current_app.config['SERVER_NAME']] is False   # the default domain's stays the site's

    def test_the_downgrade_closes_a_paid_domain_and_removes_the_unpaid(self, env, stripe_session):
        register(env)
        connection = db.session.connection()
        with Operations.context(MigrationContext.configure(connection)):
            DomainConfigMigrations().downgrade()
            _migration().downgrade()
            registration = connection.execute(text("SELECT registration FROM site_domain WHERE domain = 'paid.example'")).scalar()
            found = connection.execute(text('SELECT count(*) FROM "user" WHERE user_name = \'joiner\'')).scalar()
        db.session.rollback()

        assert (registration, found) == ('closed', 0)


def _migration(name='paid_private_domains'):
    path = next(Path(__file__).parent.parent.glob(f'migrations/versions/*_{name}.py'))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


class TestCheckoutExpiry:
    def test_a_pending_accounts_checkout_ends_before_the_account_does(self, env):
        from app.payments.adapters.stripe_adapter import StripeAdapter
        from app.payments.registration import UNPAID_ACCOUNT_HOURS
        member = a_member(env)
        member.registration_pending_since = utcnow()
        db.session.commit()
        with patch('stripe.checkout.Session.create') as create:
            StripeAdapter().checkout(env.offer, member, 'https://x/ok', 'https://x/no')

        expires = create.call_args.kwargs['expires_at']
        assert expires - utcnow().timestamp() < UNPAID_ACCOUNT_HOURS * 3600 + 86400
        assert create.call_args.kwargs['client_reference_id'] == str(member.id)

    def test_cancel_at_period_end_says_when(self, env):
        from app.payments.adapters.stripe_adapter import StripeAdapter
        with patch('stripe.Subscription.modify', return_value=SimpleNamespace(cancel_at=1800000000)) as modify:
            ends = StripeAdapter().cancel_at_period_end('sub_1')

        assert modify.call_args.kwargs['cancel_at_period_end'] is True
        assert ends.tzinfo is None and ends.year == 2027
