"""BTCPay prices are fixed once a day, at midnight UTC (plan 2026-11-15-btc-daily-price.md, B1-B5)."""
import json
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from flask import current_app, g

from app import cli, db
from app.models import BtcDailyRate, BtcpayInvoice, MemberSubscription, Offer, Payment, PaymentProvider, PromoCode, \
    Site, utcnow
from app.payments.adapters.base import ProviderError
from app.payments.adapters.btcpay_adapter import BtcpayAdapter
from app.payments.btc_price import btc_text, checkout_rate, fix_today, rate_on, sats_for, to_sats, todays_btc_price
from app.payments.core import apply_event
from app.payments.credentials import seal
from app.payments.discounts import find_discount_code
from app.payments.offers import offers_changed
from app.utils import set_setting
from tests.factories import make_user

URL = 'https://pay.example'
STORE = 'store1'
RATES = f'{URL}/api/v1/stores/{STORE}/rates'
INVOICES = f'{URL}/api/v1/stores/{STORE}/invoices'


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    monkeypatch.setitem(current_app.config, 'PAYMENT_SECRETS_KEY', Fernet.generate_key().decode())
    g.admin_ids = []
    db.session.get(Site, 1).private_instance = False
    plan = Offer(provider_kind='btcpay', kind='subscription', title='Furcas', offer_group='tiers', amount_minor=666,
                 currency='usd', interval='month')
    db.session.add(plan)
    db.session.add(PaymentProvider(kind='btcpay', enabled=True, credentials=seal({'api_key': 'k', 'webhook_secret': 's'}),
                                   settings={'url': URL, 'store_id': STORE}))
    member = make_user(api_baseline.instance_local, 'member', local=True)
    db.session.commit()
    set_setting('payment_currency', 'usd')
    offers_changed()
    yield SimpleNamespace(app=app, client=app.test_client(), plan=plan, member=member)
    offers_changed()


@pytest.fixture(autouse=True)
def no_task():
    with patch('app.payments.core.cancel_at_provider'):
        yield


def fixed(day: date, rate: str, currency='usd'):
    row = BtcDailyRate(day=day, currency=currency, rate=Decimal(rate), fixed_at=datetime.combine(day, datetime.min.time()))
    db.session.add(row)
    db.session.commit()
    return row


def rates_answer(rate='50000.00', errors=None):
    return httpx.Response(200, json=[{'currencyPair': 'BTC_USD', 'errors': errors or [], 'rate': rate}])


class TestArithmetic:
    def test_sats_are_rounded_down(self):
        # $6.66 at $50,000: 0.0001332 BTC exactly; at $30,000: 22200 sats exactly; at $70,000: 9514.28... -> 9514
        assert sats_for(666, 'usd', Decimal('50000')) == 13320
        assert sats_for(666, 'usd', Decimal('70000')) == 9514

    def test_a_currency_with_no_cents(self):
        assert sats_for(1000, 'jpy', Decimal('10000000')) == 10000

    def test_text_and_back(self):
        assert btc_text(13320) == '0.00013320' and to_sats('0.00013320') == 13320 and to_sats('1') == 100_000_000

    @pytest.mark.parametrize('text', ['0.000000001', 'abc', '', None, '-0.1'], ids=['part_sat', 'word', 'empty', 'none', 'negative'])
    def test_what_is_no_whole_number_of_sats(self, text):
        with pytest.raises(ValueError):
            to_sats(text)


class TestWhichRate:
    def test_a_day_holds_from_just_after_its_midnight(self, env):
        fixed(date(2026, 11, 14), '40000')
        fixed(date(2026, 11, 15), '50000')
        assert rate_on(datetime(2026, 11, 15, 0, 0, 1), 'usd').rate == Decimal('50000')
        assert rate_on(datetime(2026, 11, 15, 23, 59, 59), 'usd').rate == Decimal('50000')

    def test_exactly_midnight_is_yesterdays(self, env):
        fixed(date(2026, 11, 14), '40000')
        fixed(date(2026, 11, 15), '50000')
        assert rate_on(datetime(2026, 11, 15, 0, 0, 0), 'usd').rate == Decimal('40000')
        assert rate_on(datetime(2026, 11, 16, 0, 0, 0), 'usd').rate == Decimal('50000')

    def test_before_today_is_fixed_yesterdays_holds(self, env):
        fixed(date(2026, 11, 14), '40000')
        assert rate_on(datetime(2026, 11, 15, 3, 0), 'usd').rate == Decimal('40000')

    def test_another_currency_is_its_own(self, env):
        fixed(date(2026, 11, 15), '50000', currency='eur')
        assert rate_on(datetime(2026, 11, 15, 12, 0), 'usd') is None


class TestFixingToday:
    @respx.mock
    def test_it_reads_the_stores_rate_once_a_day(self, env):
        route = respx.get(RATES).mock(return_value=rates_answer('50000.12'))
        now = datetime(2026, 11, 15, 0, 0, 5)
        row = fix_today(now)
        assert (row.day, row.currency, row.rate) == (date(2026, 11, 15), 'usd', Decimal('50000.12'))
        assert fix_today(datetime(2026, 11, 15, 1, 0, 5)) == row and route.call_count == 1
        assert route.calls[0].request.url.params['currencyPair'] == 'BTC_USD'
        assert route.calls[0].request.headers['Authorization'] == 'token k'

    @respx.mock
    @pytest.mark.parametrize('answer', [rates_answer(errors=['no source']), rates_answer(rate='0'),
                                        rates_answer(rate='nope'), httpx.Response(403), httpx.Response(200, json=[])],
                             ids=['errors', 'zero', 'not_a_number', 'refused', 'missing'])
    def test_a_rate_it_cannot_use_fixes_nothing_and_yesterdays_holds(self, env, answer):
        fixed(date(2026, 11, 14), '40000')
        respx.get(RATES).mock(return_value=answer)
        assert fix_today(datetime(2026, 11, 15, 0, 0, 5)) is None
        assert rate_on(datetime(2026, 11, 15, 12, 0), 'usd').rate == Decimal('40000')

    def test_without_btcpay_nothing_is_read(self, env):
        PaymentProvider.query.filter_by(kind='btcpay').delete()
        db.session.commit()
        assert fix_today(datetime(2026, 11, 15, 0, 0, 5)) is None

    @respx.mock
    def test_the_job_fixes_it(self, env):
        respx.get(RATES).mock(return_value=rates_answer('50000'))
        cli.register(env.app)   # pyfedi.py registers the commands; the test app has none
        result = env.app.test_cli_runner().invoke(args=['fix_btc_price'])
        assert result.exit_code == 0 and BtcDailyRate.query.count() == 1


class TestCheckout:
    @respx.mock
    def test_a_new_site_reads_and_fixes_todays_rate_at_once(self, env):
        respx.get(RATES).mock(return_value=rates_answer('50000'))
        assert checkout_rate(datetime(2026, 11, 15, 12, 0), 'usd').rate == Decimal('50000')
        assert BtcDailyRate.query.count() == 1

    @respx.mock
    def test_with_no_rate_at_all_btcpay_is_unavailable(self, env):
        respx.get(RATES).mock(return_value=httpx.Response(500))
        with pytest.raises(ProviderError):
            checkout_rate(datetime(2026, 11, 15, 12, 0), 'usd')

    @respx.mock
    def test_the_invoice_is_in_bitcoin_at_the_days_rate_and_recorded(self, env):
        fixed(utcnow().date(), '50000')
        route = respx.post(INVOICES).mock(return_value=httpx.Response(200, json={'id': 'inv1'}))
        with env.app.test_request_context('/'):
            BtcpayAdapter().checkout(env.plan, env.member, 'ok', 'no')
        sent = json.loads(route.calls[0].request.content)
        assert (sent['amount'], sent['currency']) == ('0.00013320', 'BTC')
        record = db.session.get(BtcpayInvoice, 'inv1')
        assert (record.offer_id, record.user_id, record.expected_minor, record.currency, record.sats, record.rate_day) == \
            (env.plan.id, env.member.id, 666, 'usd', 13320, utcnow().date())

    @respx.mock
    @pytest.mark.parametrize('answer', [{}, {'id': ''}, ['inv1']], ids=['no_id', 'empty_id', 'not_an_object'])
    def test_an_answer_naming_no_invoice_records_nothing(self, env, answer):
        fixed(utcnow().date(), '50000')
        respx.post(INVOICES).mock(return_value=httpx.Response(200, json=answer))
        with env.app.test_request_context('/'), pytest.raises(ProviderError):
            BtcpayAdapter().checkout(env.plan, env.member, 'ok', 'no')
        assert BtcpayInvoice.query.count() == 0

    def test_an_offer_with_no_price_shows_no_bitcoin_price(self, env):
        fixed(utcnow().date(), '50000')
        assert todays_btc_price(None, 'usd') is None and todays_btc_price(666, None) is None
        assert todays_btc_price(666, 'usd') == '0.00013320'

    @respx.mock
    def test_a_discount_is_taken_off_before_converting(self, env):
        fixed(utcnow().date(), '50000')
        db.session.add(PromoCode(code='half', kind='percent', percent=50))
        db.session.commit()
        route = respx.post(INVOICES).mock(return_value=httpx.Response(200, json={'id': 'inv1'}))
        with env.app.test_request_context('/'):
            BtcpayAdapter().checkout(env.plan, env.member, 'ok', 'no', discount=find_discount_code('half'))
        assert json.loads(route.calls[0].request.content)['amount'] == '0.00006660'
        assert db.session.get(BtcpayInvoice, 'inv1').expected_minor == 333

    @respx.mock
    def test_the_plans_page_shows_todays_bitcoin_price(self, env):
        fixed(utcnow().date(), '50000')
        with env.client.session_transaction() as session:
            session['_user_id'], session['_fresh'] = str(env.member.id), True
        assert '0.00013320 BTC' in env.client.get('/donations').get_data(as_text=True)


def settled_invoice(amount, currency='BTC', **extra):
    return {'id': 'inv1', 'status': 'Settled', 'amount': amount, 'currency': currency,
            'metadata': {'offer_id': '1', 'user_id': '1'}, **extra}


def record(env, sats=13320, expected=666):
    db.session.add(BtcpayInvoice(invoice_id='inv1', offer_id=env.plan.id, user_id=env.member.id, expected_minor=expected,
                                 currency='usd', sats=sats, rate_day=utcnow().date()))
    db.session.commit()


class TestSettling:
    def events(self, env, invoice):
        respx.get(f'{INVOICES}/inv1').mock(return_value=httpx.Response(200, json=invoice))
        return BtcpayAdapter()._settled(BtcpayAdapter()._config(), 'inv1')

    @respx.mock
    def test_paid_in_full_is_the_price_asked_in_the_sites_currency(self, env):
        record(env)
        paid = self.events(env, settled_invoice('0.00013320'))[0]
        assert (paid.amount_minor, paid.currency, paid.user_id, paid.offer_id) == (666, 'usd', env.member.id, env.plan.id)

    @respx.mock
    def test_it_grants_the_period(self, env):
        record(env)
        apply_event('btcpay', 'e1', self.events(env, settled_invoice('0.00013320')))
        assert Payment.query.one().status == 'paid'
        assert MemberSubscription.query.filter_by(user_id=env.member.id, status='active').count() == 1

    @respx.mock
    @pytest.mark.parametrize('invoice', [settled_invoice('0.00013319'), settled_invoice('6.66', currency='USD'),
                                         settled_invoice('nonsense')], ids=['other_sats', 'other_currency', 'nonsense'])
    def test_anything_else_is_a_mismatch(self, env, invoice):
        record(env)
        apply_event('btcpay', 'e1', self.events(env, invoice))
        assert Payment.query.one().status == 'mismatch'
        assert MemberSubscription.query.filter_by(user_id=env.member.id, status='active').count() == 0

    @respx.mock
    def test_the_record_names_who_and_what_not_the_metadata(self, env):
        other = Offer(provider_kind='btcpay', kind='subscription', title='Bael', offer_group='tiers', amount_minor=6660,
                      currency='usd', interval='month')
        db.session.add(other)
        db.session.commit()
        record(env)
        paid = self.events(env, settled_invoice('0.00013320', metadata={'offer_id': str(other.id), 'user_id': '999'}))[0]
        assert (paid.offer_id, paid.user_id) == (env.plan.id, env.member.id)

    @respx.mock
    def test_an_invoice_made_before_this_release_is_read_as_before(self, env):
        events = self.events(env, settled_invoice('6.66', currency='USD',
                                                   metadata={'offer_id': str(env.plan.id), 'user_id': str(env.member.id)}))
        assert (events[0].amount_minor, events[0].currency) == (666, 'USD')
