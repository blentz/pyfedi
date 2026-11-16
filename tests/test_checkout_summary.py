"""Before paying, a summary of what it costs and covers, with a notice of a held code that lapsed and its dates
(plan 2026-11-16-referral-gifts-admins-checkout.md, X4-X6)."""
import json
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
import respx
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet
from flask import current_app, g, session as flask_session
from flask_wtf.csrf import generate_csrf

from app import db
from app.models import BtcDailyRate, MemberSubscription, Offer, Payment, PaymentProvider, PromoCode, PromoCodeUse, Site, utcnow
from app.payments.credentials import seal
from app.payments.offers import offers_changed
from app.payments.summary import lapse_of, summarise
from tests.factories import make_user

URL = 'https://pay.example'
STORE = 'store1'
INVOICES = f'{URL}/api/v1/stores/{STORE}/invoices'
STRIPE_URL = 'https://checkout.stripe.example/s'


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    monkeypatch.setitem(current_app.config, 'PAYMENT_SECRETS_KEY', Fernet.generate_key().decode())
    monkeypatch.setitem(current_app.config, 'STRIPE_SECRET_KEY', 'sk_test')
    g.admin_ids = []
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    db.session.add(PaymentProvider(kind='btcpay', enabled=True, credentials=seal({'api_key': 'k', 'webhook_secret': 's'}),
                                   settings={'url': URL, 'store_id': STORE}))
    prepaid = Offer(provider_kind='btcpay', kind='subscription', title='Furcas, monthly (Bitcoin)', offer_group='tiers',
                    amount_minor=666, currency='usd', interval='month')
    renewing = Offer(provider_kind='stripe', kind='subscription', title='Furcas, monthly (card)', offer_group='cards',
                     provider_price_id='price_m', amount_minor=666, currency='usd', interval='month')
    once = Offer(provider_kind='stripe', kind='donation', title='A tip', offer_group='tips', provider_price_id='price_t',
                 amount_minor=500, currency='usd')
    db.session.add_all([prepaid, renewing, once])
    db.session.add(BtcDailyRate(day=utcnow().date() - timedelta(days=1), currency='usd', rate=Decimal('50000'),
                                fixed_at=utcnow()))
    member = make_user(api_baseline.instance_local, 'member', local=True)
    db.session.commit()
    offers_changed()
    yield SimpleNamespace(app=app, client=app.test_client(), prepaid=prepaid, renewing=renewing, once=once, member=member)
    offers_changed()


def login(env):
    with env.client.session_transaction() as session:
        session['_user_id'], session['_fresh'] = str(env.member.id), True


def checkout(env, offer, held=None):
    with env.app.test_request_context():
        g.pop('csrf_token', None)
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with env.client.session_transaction() as session:
        session['csrf_token'] = raw
        if held is not None:
            session['discount_code'] = held
    return env.client.post(f'/donations/checkout/{offer.id}', data={'csrf_token': token})


def rows(answer) -> dict:
    page = BeautifulSoup(answer.get_data(as_text=True), 'html.parser')
    return {row.th.get_text(strip=True): row.td.get_text(' ', strip=True) for row in page.select('main table tr')}


def notice(answer) -> str:
    alert = BeautifulSoup(answer.get_data(as_text=True), 'html.parser').select_one('main .alert-warning')
    return alert.get_text(' ', strip=True) if alert else ''


def pay_link(answer) -> str:
    return BeautifulSoup(answer.get_data(as_text=True), 'html.parser').find('a', string='Pay')['href']


@pytest.fixture
def btcpay():
    with respx.mock(assert_all_called=False) as router:
        router.post(INVOICES).mock(return_value=httpx.Response(200, json={'id': 'inv1'}))
        yield router


@pytest.fixture
def stripe():
    with patch('app.payments.adapters.stripe_adapter.StripeAdapter.checkout', return_value=STRIPE_URL) as made:
        yield made


class TestTheSummary:
    def test_a_prepaid_period_says_from_when_to_when_in_bitcoin(self, env, btcpay):
        login(env)
        answer = checkout(env, env.prepaid)
        today = utcnow()
        assert answer.status_code == 200 and pay_link(answer) == f'{URL}/i/inv1'
        found = rows(answer)
        assert found['For'] == 'Furcas, monthly (Bitcoin)'
        assert found['Covers'].startswith(f"{today:%Y-%m-%d} to {today + timedelta(days=30):%Y-%m-%d}")
        assert found['Price'] == '$6.66' and found['Total'] == '$6.66'
        assert '0.00013320 BTC' in found['In bitcoin'] and '$50,000.00' in found['In bitcoin']
        assert f"{utcnow().date() - timedelta(days=1):%Y-%m-%d}" in found['In bitcoin']

    def test_time_left_carries_the_period_on(self, env, btcpay):
        ends = utcnow() + timedelta(days=10)
        db.session.add(MemberSubscription(user_id=env.member.id, offer_id=env.prepaid.id, provider_kind='btcpay',
                                          provider_reference='inv_0', status='active', current_period_end=ends))
        db.session.commit()
        login(env)
        assert rows(checkout(env, env.prepaid))['Covers'].startswith(
            f"{ends:%Y-%m-%d} to {ends + timedelta(days=30):%Y-%m-%d}")

    def test_a_renewing_one_says_when_it_is_first_charged(self, env, stripe):
        login(env)
        answer = checkout(env, env.renewing)
        assert pay_link(answer) == STRIPE_URL
        assert rows(answer)['Covers'] == f"Renews every month, first charged on {utcnow():%Y-%m-%d}."
        assert 'In bitcoin' not in rows(answer)

    def test_a_one_off_is_once(self, env, stripe):
        login(env)
        assert rows(checkout(env, env.once))['Covers'] == 'A single payment.'

    def test_a_guest_giving_once_sees_it_too(self, env, stripe):
        with env.client.session_transaction() as session:
            session.clear()
        answer = env.client.post(f'/donations/checkout/{env.once.id}')
        assert answer.status_code == 200 and pay_link(answer) == STRIPE_URL

    def test_a_discount_is_shown_with_what_it_takes_off(self, env, btcpay):
        db.session.add(PromoCode(code='spring-twenty', kind='percent', percent=20))
        db.session.commit()
        login(env)
        answer = checkout(env, env.prepaid, held='spring-twenty')
        found = rows(answer)
        assert found['Price'] == '$6.66' and found['Total'] == '$5.32'
        assert found['Code spring-twenty'].startswith('−$1.34 (20%)')
        assert notice(answer) == ''

    def test_the_bitcoin_invoice_says_what_it_is_for(self, env, btcpay):
        db.session.add(PromoCode(code='spring-twenty', kind='percent', percent=20))
        db.session.commit()
        login(env)
        checkout(env, env.prepaid, held='spring-twenty')
        described = json.loads(btcpay.calls[0].request.content)['metadata']['itemDesc']
        assert described == ('Furcas, monthly (Bitcoin): $6.66 - code spring-twenty $1.34 = $5.32; 0.00010640 BTC at '
                             f"$50,000.00 per BTC, the rate of {utcnow().date() - timedelta(days=1):%Y-%m-%d}")

    def test_a_provider_that_is_down_keeps_the_code_for_a_retry(self, env):
        db.session.add(PromoCode(code='spring-twenty', kind='percent', percent=20))
        db.session.commit()
        login(env)
        with respx.mock(assert_all_called=False) as router:
            router.post(INVOICES).mock(return_value=httpx.Response(500))
            assert checkout(env, env.prepaid, held='spring-twenty').status_code == 503
        with env.client.session_transaction() as session:
            assert session.get('discount_code') == 'spring-twenty'


def test_a_bitcoin_summary_with_no_invoice_recorded_shows_no_bitcoin(env):
    with env.app.test_request_context():
        found = summarise(env.prepaid, env.member, None, f'{URL}/i/unknown')
    assert (found.btc, found.rate, found.ends is not None) == (None, None, True)


class TestALapsedCode:
    def code(self, **values):
        row = PromoCode(code='spring-twenty', kind='percent', percent=20, **values)
        db.session.add(row)
        db.session.commit()
        return row

    def test_expired_with_its_date(self, env, btcpay):
        self.code(expires_at=datetime(2026, 11, 1, 23, 59, 59))
        login(env)
        answer = checkout(env, env.prepaid, held='spring-twenty')
        assert notice(answer) == 'Code spring-twenty expired on 2026-11-01, so it takes nothing off. The full price applies.'
        assert rows(answer)['Total'] == '$6.66' and 'Code spring-twenty' not in rows(answer)

    def test_used_up_with_the_date_of_its_last_use(self, env, btcpay):
        row = self.code(max_uses=1)
        other = make_user(None, 'other', local=True)
        db.session.add(PromoCodeUse(promo_code_id=row.id, user_id=other.id, used_at=utcnow() - timedelta(hours=2)))
        db.session.commit()
        login(env)
        assert notice(checkout(env, env.prepaid, held='spring-twenty')) == \
            f"Code spring-twenty was used up on {utcnow() - timedelta(hours=2):%Y-%m-%d}, so it takes nothing off. " \
            "The full price applies."

    def test_already_used_by_this_account_with_its_date(self, env, btcpay):
        row = self.code()
        db.session.add(PromoCodeUse(promo_code_id=row.id, user_id=env.member.id, used_at=datetime(2026, 10, 3, 12),
                                    payment_id=None))
        db.session.flush()
        use = PromoCodeUse.query.one()
        payment = Payment(user_id=env.member.id, offer_id=env.prepaid.id, provider_kind='btcpay', provider_reference='x',
                          amount_minor=532, currency='usd', status='paid')
        db.session.add(payment)
        db.session.flush()
        use.payment_id = payment.id
        db.session.commit()
        login(env)
        assert notice(checkout(env, env.prepaid, held='spring-twenty')) == \
            'You used code spring-twenty on 2026-10-03, and it can be used once. The full price applies.'

    def test_switched_off(self, env, btcpay):
        self.code(active=False)
        login(env)
        assert notice(checkout(env, env.prepaid, held='spring-twenty')) == \
            'Code spring-twenty has been switched off, so it takes nothing off. The full price applies.'

    def test_not_for_this_offer(self, env, btcpay):
        self.code(offer_id=env.renewing.id)
        login(env)
        assert notice(checkout(env, env.prepaid, held='spring-twenty')) == \
            'Code spring-twenty is not for Furcas, monthly (Bitcoin), so it takes nothing off. The full price applies.'

    def test_a_code_gone_altogether(self, env, btcpay):
        login(env)
        assert 'Code no-such-code is not for' in notice(checkout(env, env.prepaid, held='no-such-code'))

    def test_it_is_held_no_longer(self, env, btcpay):
        self.code(active=False)
        login(env)
        checkout(env, env.prepaid, held='spring-twenty')
        with env.client.session_transaction() as session:
            assert 'discount_code' not in session

    def test_uses_left_but_not_for_this_offer(self, env):
        assert lapse_of(self.code(max_uses=5, offer_id=env.renewing.id), env.member, env.prepaid).reason == 'not_for_this'

    def test_a_usable_one_is_no_lapse(self, env):
        assert lapse_of(self.code(), env.member, env.prepaid) is None and lapse_of(None, env.member, env.prepaid) is None
