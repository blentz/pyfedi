"""/donations is upstream's donation page; membership is managed in Settings -> Subscription, and the day's bitcoin
price is shown with BTCPay buttons and by [btcprice N] (plan 2026-11-16-donations-and-membership-pages.md, DM1-DM5)."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet
from flask import current_app, g, session as flask_session
from flask_wtf.csrf import generate_csrf

from app import db
from app.cms import cms_nav_links_changed, plans_page_url
from app.complimentary import grant_complimentary
from app.models import BtcDailyRate, CmsPage, MemberSubscription, Offer, Payment, PaymentProvider, PromoCode, Site, \
    SiteDomain, utcnow
from app.payments.core import remind_expiring_prepaid
from app.payments.credentials import seal
from app.payments.offers import is_tier, offers_changed, subscription_settings_shown
from app.storefront import clear_domain_cache
from app.utils import markdown_to_html
from tests.factories import make_user


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    monkeypatch.setitem(current_app.config, 'PAYMENT_SECRETS_KEY', Fernet.generate_key().decode())
    monkeypatch.setitem(current_app.config, 'STRIPE_SECRET_KEY', 'sk_test')
    g.admin_ids = []
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    db.session.add(PaymentProvider(kind='btcpay', enabled=True, credentials=seal({'api_key': 'k', 'webhook_secret': 's'}),
                                   settings={'url': 'https://pay.example', 'store_id': 'store1'}))
    tier = Offer(provider_kind='btcpay', kind='subscription', title='Furcas', offer_group='tiers', amount_minor=666,
                 currency='usd', interval='month')
    regular = Offer(provider_kind='stripe', kind='subscription', title='Monthly gift', offer_group='gifts',
                    provider_price_id='price_r', amount_minor=500, currency='usd', interval='month', grants_donor=True)
    once = Offer(provider_kind='btcpay', kind='donation', title='Tip jar', offer_group='tips', amount_minor=300,
                 currency='usd')
    db.session.add_all([tier, regular, once])
    db.session.flush()
    home = SiteDomain(domain=current_app.config['SERVER_NAME'], scheme='https', federates=True, is_default=True,
                      registration='paid', registration_offer_id=tier.id, lapse_grace_days=3,
                      paid_since=utcnow() - timedelta(days=100))
    db.session.add(home)
    db.session.add(BtcDailyRate(day=utcnow().date() - timedelta(days=1), currency='usd', rate=50000, fixed_at=utcnow()))
    member = make_user(api_baseline.instance_local, 'member', local=True)
    db.session.commit()
    clear_domain_cache()
    offers_changed()
    cms_nav_links_changed()
    yield SimpleNamespace(app=app, client=app.test_client(), tier=tier, regular=regular, once=once, member=member,
                          home=home, base=f"https://{current_app.config['SERVER_NAME']}")
    clear_domain_cache()
    offers_changed()
    cms_nav_links_changed()


def login(env, user=None):
    with env.client.session_transaction(base_url=env.base) as session:
        session['_user_id'], session['_fresh'] = str((user or env.member).id), True


def page(env, body, url='/subscribe'):
    db.session.add(CmsPage(url=url, title='Subscribe', body=body, body_html=markdown_to_html(body)))
    db.session.commit()
    cms_nav_links_changed()


def get(env, path):
    return env.client.get(path, base_url=env.base).get_data(as_text=True)


def post(env, path, **data):
    with env.app.test_request_context():
        g.pop('csrf_token', None)
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with env.client.session_transaction(base_url=env.base) as session:
        session['csrf_token'] = raw
    return env.client.post(path, data={'csrf_token': token, **data}, base_url=env.base)


def holding(env, offer, reference='sub_1', **values):
    row = MemberSubscription(user_id=env.member.id, offer_id=offer.id, provider_kind=offer.provider_kind,
                             provider_reference=reference, status='active', created_at=utcnow() - timedelta(days=3),
                             **values)
    db.session.add(row)
    db.session.commit()
    return row


class TestWhatATierIs:
    def test_a_subscription_in_a_domains_membership_group(self, env):
        assert is_tier(env.tier) and not is_tier(env.regular) and not is_tier(env.once)

    def test_with_no_domain_charging_there_are_none(self, env):
        env.home.registration_offer_id = None
        db.session.commit()
        clear_domain_cache()
        assert not is_tier(env.tier)


class TestDonationsIsUpstreams:
    def test_its_heading_text_and_piefeds_section(self, env):
        login(env)
        body = get(env, '/donations')
        assert 'Donate to' in body and 'Please choose from one of the plans below.' in body
        assert 'Prices are in US dollars.' in body
        assert 'Support PieFed development' in body
        assert 'https://www.patreon.com/PieFed' in body and 'https://liberapay.com/PieFed/' in body

    def test_it_offers_donations_not_tiers(self, env):
        login(env)
        body = get(env, '/donations')
        assert 'Tip jar' in body and 'Monthly gift' in body and 'Furcas' not in body
        assert f'/donations/checkout/{env.tier.id}' not in body

    def test_no_membership_parts_or_bitcoin_price(self, env):
        login(env)
        db.session.add(PromoCode(code='twenty', kind='percent', percent=20))
        db.session.commit()
        with env.client.session_transaction(base_url=env.base) as session:
            session['discount_code'] = 'twenty'
        body = get(env, '/donations')
        assert 'Have a code?' not in body and 'twenty' not in body and 'BTC' not in body
        assert 'Your membership' not in body and 'Your payments' not in body

    def test_a_regular_donation_is_managed_there(self, env):
        row = holding(env, env.regular)
        login(env)
        body = get(env, '/donations')
        assert 'Manage existing donation plan' in body and f'/donations/subscriptions/{row.id}/cancel' in body
        assert 'Please choose from one of the plans below.' not in body

    def test_a_tier_held_is_not_managed_there(self, env):
        row = holding(env, env.tier, reference='inv_1', current_period_end=utcnow() + timedelta(days=20))
        login(env)
        assert f'/donations/subscriptions/{row.id}/cancel' not in get(env, '/donations')

    def test_a_complimentary_member_is_told_and_offered_nothing(self, env):
        grant_complimentary(env.member)
        db.session.commit()
        login(env)
        body = get(env, '/donations')
        assert 'Your membership is complimentary' in body and '/donations/checkout/' not in body
        assert 'Support PieFed development' in body


class TestSettingsSubscription:
    SETTINGS = '/user/settings/subscription'

    def test_a_visitor_is_shown_no_tab(self, env):
        with env.app.test_request_context():
            assert not subscription_settings_shown()

    def test_a_visitor_is_asked_to_log_in(self, env):
        answer = env.client.get(self.SETTINGS, base_url=env.base)
        assert answer.status_code == 302 and '/auth/login' in answer.headers['Location']

    def test_it_is_a_settings_tab(self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'WTF_CSRF_ENABLED', True)   # a settings form draws its token
        login(env)
        body = get(env, '/user/settings/filters')
        assert f'href="{self.SETTINGS}"' in body

    def test_no_tab_where_nothing_is_paid_for(self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'WTF_CSRF_ENABLED', True)
        env.home.registration_offer_id = None
        db.session.commit()
        clear_domain_cache()
        login(env)
        assert self.SETTINGS not in get(env, '/user/settings/filters')

    def test_a_member_sees_their_tier_and_can_cancel_it(self, env):
        row = holding(env, env.tier, reference='inv_1', current_period_end=utcnow() + timedelta(days=20))
        login(env)
        body = get(env, self.SETTINGS)
        assert 'Furcas' in body and row.current_period_end.strftime('%Y-%m-%d') in body
        assert f'/donations/subscriptions/{row.id}/cancel' in body
        assert f'name="next" value="{self.SETTINGS}"' in body

    def test_it_leads_to_the_plans(self, env):
        page(env, f'[checkout {env.tier.id}]')
        login(env)
        assert 'href="/subscribe">See the plans' in get(env, self.SETTINGS)

    def test_cancelling_comes_back_to_it(self, env):
        row = holding(env, env.tier, reference='inv_1', current_period_end=utcnow() + timedelta(days=20))
        login(env)
        answer = post(env, f'/donations/subscriptions/{row.id}/cancel', next=self.SETTINGS)
        assert answer.status_code == 302 and answer.headers['Location'].endswith(self.SETTINGS)
        assert row.ends_at is not None

    def test_somewhere_else_is_not_where_it_goes_back_to(self, env):
        row = holding(env, env.tier, reference='inv_1', current_period_end=utcnow() + timedelta(days=20))
        login(env)
        answer = post(env, f'/donations/subscriptions/{row.id}/cancel', next='https://evil.example/')
        assert 'evil.example' not in answer.headers['Location']

    def test_a_regular_donation_is_left_to_donations(self, env):
        holding(env, env.regular)
        login(env)
        assert 'Monthly gift' not in get(env, self.SETTINGS)

    def test_the_code_box_and_a_held_code(self, env):
        db.session.add(PromoCode(code='twenty', kind='percent', percent=20))
        db.session.commit()
        login(env)
        with env.client.session_transaction(base_url=env.base) as session:
            session['discount_code'] = 'twenty'
        body = get(env, self.SETTINGS)
        assert 'Have a code?' in body and '/donations/redeem' in body and 'twenty' in body
        assert '/donations/code/clear' in body

    def test_redeeming_comes_back_to_it(self, env):
        db.session.add(PromoCode(code='gift-month', kind='gift', offer_id=env.tier.id, periods=1))
        db.session.commit()
        login(env)
        answer = post(env, '/donations/redeem', code='GIFT-MONTH', next=self.SETTINGS)
        assert answer.headers['Location'].endswith(self.SETTINGS)

    def test_complimentary_says_so_and_takes_no_code(self, env):
        grant_complimentary(env.member)
        db.session.commit()
        login(env)
        body = get(env, self.SETTINGS)
        assert 'Complimentary' in body and 'Have a code?' not in body and 'See the plans' not in body

    def test_their_payments_are_listed(self, env):
        db.session.add(Payment(user_id=env.member.id, offer_id=env.tier.id, provider_kind='btcpay',
                               provider_reference='inv_9', amount_minor=666, currency='usd', status='paid'))
        db.session.commit()
        login(env)
        assert 'Your payments' in get(env, self.SETTINGS)

    def test_a_lapsed_member_is_sent_there_and_told(self, env):
        holding(env, env.tier, reference='inv_old').status = 'ended'
        env.member.created = utcnow() - timedelta(days=10)
        env.member.access_lapsed_at = utcnow() - timedelta(days=10)
        db.session.commit()
        login(env)
        answer = env.client.get('/communities', base_url=env.base)
        assert answer.status_code == 302 and answer.headers['Location'].endswith(self.SETTINGS)
        assert 'Your subscription has ended' in get(env, self.SETTINGS)


class TestThePlansPage:
    def test_the_page_with_a_checkout_button(self, env):
        page(env, f'Our plans [checkout {env.tier.id}]')
        with env.app.test_request_context():
            assert plans_page_url() == '/subscribe'
            assert plans_page_url(external=True).endswith('/subscribe')

    def test_without_one_donations(self, env):
        with env.app.test_request_context():
            assert plans_page_url() == '/donations'

    def test_the_current_plan_and_complimentary_buttons_lead_to_settings(self, env):
        grant_complimentary(env.member)
        db.session.commit()
        page(env, f'[checkout {env.tier.id}]')
        login(env)
        assert 'href="/user/settings/subscription">Complimentary' in get(env, '/subscribe')

    def test_the_reminder_mail_leads_to_the_plans(self, env):
        page(env, f'[checkout {env.tier.id}]')
        holding(env, env.tier, reference='inv_1', current_period_end=utcnow() + timedelta(days=3))
        sent = []
        with patch('app.email.send_email', side_effect=lambda *args, **kwargs: sent.append((args, kwargs))):
            remind_expiring_prepaid()
        assert sent and '/subscribe' in sent[0][1]['text_body'] and '/subscribe' in sent[0][1]['html_body']


class TestBitcoinPrice:
    def test_beside_each_btcpay_button(self, env):
        page(env, f'[checkout {env.tier.id}]')
        login(env)
        assert '0.00013320 BTC today' in get(env, '/subscribe')

    def test_not_beside_another_providers(self, env):
        page(env, f'[checkout {env.regular.id}]')
        login(env)
        assert 'BTC' not in BeautifulSoup(get(env, '/subscribe'), 'html.parser').find('main').get_text()

    def test_the_shortcode_writes_the_amount(self, env):
        page(env, f'Furcas costs [btcprice {env.tier.id}] BTC today.')
        assert 'Furcas costs 0.00013320 BTC today.' in get(env, '/subscribe')

    @pytest.mark.parametrize('which', ['stripe', 'unknown', 'no_rate'])
    def test_the_shortcode_writes_nothing_it_cannot_know(self, env, which):
        if which == 'no_rate':
            BtcDailyRate.query.delete()
            db.session.commit()
        offer_id = {'stripe': env.regular.id, 'unknown': 99999, 'no_rate': env.tier.id}[which]
        page(env, f'Costs [btcprice {offer_id}] BTC.')
        assert 'Costs  BTC.' in get(env, '/subscribe')
