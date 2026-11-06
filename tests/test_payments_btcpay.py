"""BTCPay Server, with its HTTP answers as the real server gave them (plan phase 4; live: test_payments_btcpay_regtest.py)."""
import hashlib
import hmac
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
import respx
from flask import current_app, g, session as flask_session
from flask_wtf.csrf import generate_csrf

from app import db
from app.constants import DONOR_WHILE_SUBSCRIBED
from app.models import MemberSubscription, Offer, Payment, PaymentProvider, Site, SiteDomain, utcnow
from app.payments.adapters.base import InvalidWebhook, ProviderError
from app.payments.adapters.btcpay_adapter import BtcpayAdapter, from_minor, to_minor
from app.email import send_prepaid_reminder
from app.payments.core import apply_event, end_expired_prepaid, remind_expiring_prepaid
from app.payments.credentials import seal
from app.payments.offers import offers_changed, offers_on
from app.payments.retention import end_expired_subscriptions
from app.storefront import clear_domain_cache
from app.utils import set_setting
from tests.factories import grant_permission, make_user

URL = 'https://pay.example'
STORE = 'store1'
SECRET = 'whsec'
INVOICE = {'id': 'inv1', 'status': 'Settled', 'amount': '5.00', 'currency': 'USD', 'metadata': {'offer_id': '1', 'user_id': '1'}}


def connect(onion='', enabled=True):
    db.session.add(PaymentProvider(kind='btcpay', enabled=enabled, credentials=seal({'api_key': 'k', 'webhook_secret': SECRET}),
                                   settings={'url': URL, 'onion_url': onion, 'store_id': STORE}))
    db.session.commit()
    offers_changed()


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setitem(current_app.config, 'PAYMENT_SECRETS_KEY', Fernet.generate_key().decode())
    g.admin_ids = []
    site = db.session.get(Site, 1)
    site.private_instance = False
    plan = Offer(provider_kind='btcpay', kind='subscription', title='Prepaid', offer_group='plans', amount_minor=500,
                 currency='usd', interval='month', grants_donor=True)
    gift = Offer(provider_kind='btcpay', kind='donation', title='Gift', offer_group='gifts', amount_minor=300, currency='usd')
    db.session.add_all([plan, gift])
    member = make_user(api_baseline.instance_local, 'member', local=True)
    db.session.commit()
    yield SimpleNamespace(app=app, client=app.test_client(), plan=plan, gift=gift, member=member, site=site,
                          baseline=api_baseline)
    offers_changed()


@pytest.fixture
def adapter():
    return BtcpayAdapter()


@pytest.fixture(autouse=True)
def no_task():
    with patch('app.payments.core.cancel_at_provider'):
        yield


def login(client, user, base=None):
    kwargs = {'base_url': base} if base else {}
    with client.session_transaction(**kwargs) as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def signed(body: dict | bytes, secret=SECRET):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return raw, {'BTCPay-Sig': 'sha256=' + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()}


def hook(kind='InvoiceSettled', invoice='inv1', **extra):
    return {'deliveryId': 'd2', 'originalDeliveryId': 'd1', 'isRedelivery': True, 'type': kind, 'storeId': STORE,
            'invoiceId': invoice, **extra}


def invoice_for(env, offer=None, **extra):
    return {**INVOICE, 'metadata': {'offer_id': str((offer or env.plan).id), 'user_id': str(env.member.id)}, **extra}


class TestAmounts:
    @pytest.mark.parametrize('amount, currency, minor', [('5.00', 'USD', 500), ('5', 'usd', 500), ('500', 'JPY', 500)],
                             ids=['usd', 'whole', 'yen'])
    def test_to_minor(self, amount, currency, minor):
        assert to_minor(amount, currency) == minor

    def test_what_isnt_an_amount(self):
        with pytest.raises(ValueError):
            to_minor('lots', 'USD')

    def test_from_minor(self):
        assert from_minor(500, 'usd') == '5.00' and from_minor(500, 'jpy') == '500'


class TestBeingConfigured:
    def test_nothing_stored_is_off(self, env, adapter):
        assert not adapter.available() and not adapter.on_onion()

    def test_connected(self, env, adapter):
        connect()
        assert adapter.available() and not adapter.on_onion()

    def test_an_onion_address_offers_it_on_the_onion(self, env, adapter):
        connect(onion='http://pay.onion')
        assert adapter.on_onion()

    def test_disabled_is_off(self, env, adapter):
        connect(enabled=False)
        assert not adapter.available()

    def test_a_key_that_cant_open_it_is_off_and_the_site_carries_on(self, env, adapter):
        db.session.add(PaymentProvider(kind='btcpay', credentials='garbage', settings={'url': URL, 'store_id': STORE}))
        db.session.commit()
        assert not adapter.available()

    def test_only_what_can_be_paid_for_is_offered_on_the_onion(self, env):
        connect()
        assert {offer.id for offer in offers_on(None, False)} >= {env.plan.id} and offers_on(None, True) == []

    def test_with_an_onion_address_it_is(self, env):
        connect(onion='http://pay.onion')
        assert env.plan.id in {offer.id for offer in offers_on(None, True)}


class TestCheckout:
    @respx.mock
    def test_an_invoice_is_made_and_the_address_built_here(self, env, adapter):
        connect()
        route = respx.post(f'{URL}/api/v1/stores/{STORE}/invoices').mock(return_value=httpx.Response(200, json={
            'id': 'inv1', 'checkoutLink': 'http://internal:49392/i/inv1'}))
        with env.app.test_request_context('/'):
            assert adapter.checkout(env.plan, env.member, 'https://x/ok', 'https://x/no') == f'{URL}/i/inv1'

        request = route.calls[0].request
        sent = json.loads(request.content)
        assert request.headers['Authorization'] == 'token k'
        assert sent['amount'] == '5.00' and sent['currency'] == 'USD'
        assert sent['metadata'] == {'offer_id': str(env.plan.id), 'user_id': str(env.member.id)}
        assert sent['checkout']['redirectURL'] == 'https://x/ok'

    @respx.mock
    def test_a_guest_names_no_account(self, env, adapter):
        connect()
        route = respx.post(f'{URL}/api/v1/stores/{STORE}/invoices').mock(return_value=httpx.Response(200, json={'id': 'inv1'}))
        with env.app.test_request_context('/'):
            adapter.checkout(env.gift, None, 'https://x/ok', 'https://x/no')

        assert 'user_id' not in json.loads(route.calls[0].request.content)['metadata']

    @respx.mock
    def test_on_the_onion_it_goes_to_the_servers_onion_address(self, env, adapter):
        connect(onion='http://pay.onion/')
        respx.post(f'{URL}/api/v1/stores/{STORE}/invoices').mock(return_value=httpx.Response(200, json={'id': 'inv1'}))
        with env.app.test_request_context('/'), patch('app.payments.adapters.btcpay_adapter.is_onion_request', return_value=True):
            assert adapter.checkout(env.plan, env.member, 'x', 'y') == 'http://pay.onion/i/inv1'

    def test_not_configured(self, env, adapter):
        with pytest.raises(ProviderError):
            adapter.checkout(env.plan, env.member, 'x', 'y')

    @pytest.mark.parametrize('answer', [httpx.Response(401, text='k must not appear'), httpx.ConnectError('no'),
                                        httpx.Response(200, text='not json')], ids=['refused', 'unreachable', 'nonsense'])
    @respx.mock
    def test_a_failure_carries_no_key(self, env, adapter, answer):
        connect()
        route = respx.post(f'{URL}/api/v1/stores/{STORE}/invoices')
        route.mock(side_effect=answer) if isinstance(answer, Exception) else route.mock(return_value=answer)
        with env.app.test_request_context('/'), pytest.raises(ProviderError) as refused:
            adapter.checkout(env.plan, env.member, 'x', 'y')

        assert 'must not appear' not in str(refused.value)


class TestWebhooks:
    @pytest.mark.parametrize('headers', [{}, {'BTCPay-Sig': 'sha256=nope'}], ids=['none', 'wrong'])
    def test_an_unsigned_one_is_refused(self, env, adapter, headers):
        connect()
        with pytest.raises(InvalidWebhook):
            adapter.parse_webhook(b'{}', headers)

    def test_with_no_secret_nothing_is_accepted(self, env, adapter):
        with pytest.raises(InvalidWebhook):
            adapter.parse_webhook(*signed(hook()))

    def test_not_json(self, env, adapter):
        connect()
        with pytest.raises(InvalidWebhook):
            adapter.parse_webhook(*signed(b'[['))

    @pytest.mark.parametrize('body', [[1], {**hook(), 'storeId': 'other'}], ids=['list', 'another-store'])
    def test_not_for_this_store(self, env, adapter, body):
        connect()
        with pytest.raises(InvalidWebhook):
            adapter.parse_webhook(*signed(body))

    def test_a_redelivery_is_the_same_event(self, env, adapter):
        connect()
        assert adapter.parse_webhook(*signed(hook('InvoiceProcessing')))[0] == 'd1'

    def test_no_invoice_named_is_nothing(self, env, adapter):
        connect()
        assert adapter.parse_webhook(*signed({'storeId': STORE, 'type': 'InvoiceSettled'}))[1] == []

    def test_any_other_event_is_nothing(self, env, adapter):
        connect()
        assert adapter.parse_webhook(*signed(hook('InvoiceExpired')))[1] == []

    def test_invalid_refunds_and_ends(self, env, adapter):
        connect()
        kinds = [type(event).__name__ for event in adapter.parse_webhook(*signed(hook('InvoiceInvalid')))[1]]
        assert kinds == ['Refunded', 'SubscriptionEnded']

    @respx.mock
    def test_settled_asks_the_server(self, env, adapter):
        connect()
        respx.get(f'{URL}/api/v1/stores/{STORE}/invoices/inv1').mock(return_value=httpx.Response(200, json=invoice_for(env)))
        events = adapter.parse_webhook(*signed(hook()))[1]

        assert [type(event).__name__ for event in events] == ['Paid', 'SubscriptionStarted']
        assert events[0].amount_minor == 500 and events[0].user_id == env.member.id

    @respx.mock
    def test_a_one_off_starts_nothing(self, env, adapter):
        connect()
        respx.get(f'{URL}/api/v1/stores/{STORE}/invoices/inv1').mock(
            return_value=httpx.Response(200, json=invoice_for(env, env.gift, amount='3.00')))
        assert [type(event).__name__ for event in adapter.parse_webhook(*signed(hook()))[1]] == ['Paid']

    @respx.mock
    def test_settled_that_isnt_on_the_server_grants_nothing(self, env, adapter):
        connect()
        respx.get(f'{URL}/api/v1/stores/{STORE}/invoices/inv1').mock(
            return_value=httpx.Response(200, json=invoice_for(env, status='Processing')))
        assert adapter.parse_webhook(*signed(hook()))[1] == []

    @respx.mock
    def test_an_amount_that_makes_no_sense_matches_nothing(self, env, adapter):
        connect()
        respx.get(f'{URL}/api/v1/stores/{STORE}/invoices/inv1').mock(
            return_value=httpx.Response(200, json=invoice_for(env, amount='lots')))
        assert adapter.parse_webhook(*signed(hook()))[1][0].amount_minor == -1

    @respx.mock
    def test_the_route_retries_when_the_server_is_down(self, env):
        connect()
        respx.get(f'{URL}/api/v1/stores/{STORE}/invoices/inv1').mock(return_value=httpx.Response(500))
        body, headers = signed(hook())

        assert env.client.post('/payments/webhook/btcpay', data=body, headers=headers).status_code == 503

    def test_the_route_refuses_a_bad_signature(self, env):
        connect()
        assert env.client.post('/payments/webhook/btcpay', data=b'{}').status_code == 400


class TestNothingToStop:
    def test_cancelling_is_nothing(self, adapter):
        assert adapter.cancel('inv1') is None

    @pytest.mark.parametrize('call', [lambda a: a.cancel_at_period_end('x'), lambda a: a.billing_portal_url('c', 'r'),
                                      lambda a: a.describe_price('p')], ids=['period-end', 'portal', 'price'])
    def test_what_it_doesnt_do(self, adapter, call):
        with pytest.raises(ProviderError):
            call(adapter)

    def test_the_cancel_route_refuses(self, env):
        row = MemberSubscription(user_id=env.member.id, offer_id=env.plan.id, provider_kind='btcpay', provider_reference='inv1',
                                 status='active', current_period_end=utcnow() + timedelta(days=9))
        db.session.add(row)
        db.session.commit()
        login(env.client, env.member)
        with env.app.test_request_context():
            token = generate_csrf()
            raw = flask_session['csrf_token']
        with env.client.session_transaction() as session:
            session['csrf_token'] = raw

        assert env.client.post(f'/donations/subscriptions/{row.id}/cancel', data={'csrf_token': token}).status_code == 404


def pay(env, reference, offer=None, amount=500):
    from app.payments.events import Paid, SubscriptionStarted
    offer = offer or env.plan
    apply_event('btcpay', f'evt_{reference}', [
        Paid(reference=reference, amount_minor=amount, currency='USD', user_id=env.member.id, offer_id=offer.id),
        SubscriptionStarted(reference=reference, user_id=env.member.id, offer_id=offer.id)])
    return MemberSubscription.query.filter_by(provider_reference=reference).one()


class TestPrepaidPeriods:
    def test_a_payment_buys_a_period(self, env):
        row = pay(env, 'inv1')

        assert timedelta(days=29) < row.current_period_end - utcnow() <= timedelta(days=30)
        assert env.member.donor_until == DONOR_WHILE_SUBSCRIBED

    def test_a_year(self, env):
        env.plan.interval = 'year'
        db.session.commit()
        assert pay(env, 'inv1').current_period_end - utcnow() > timedelta(days=364)

    def test_renewing_early_keeps_what_was_left(self, env):
        first = pay(env, 'inv1')
        second = pay(env, 'inv2')

        assert first.status == 'ended' and second.status == 'active'
        assert second.current_period_end - utcnow() > timedelta(days=59)

    def test_a_period_already_over_counts_from_today(self, env):
        first = pay(env, 'inv1')
        first.current_period_end = utcnow() - timedelta(days=5)
        db.session.commit()

        assert pay(env, 'inv2').current_period_end - utcnow() <= timedelta(days=30)

    def test_another_plan_in_the_group_keeps_it_too(self, env):
        other = Offer(provider_kind='btcpay', kind='subscription', title='Bigger', offer_group='plans', amount_minor=900,
                      currency='usd', interval='month')
        db.session.add(other)
        db.session.commit()
        pay(env, 'inv1')

        assert pay(env, 'inv2', other, 900).current_period_end - utcnow() > timedelta(days=59)

    def test_the_wrong_amount_buys_nothing(self, env):
        row = pay(env, 'inv1', amount=1)

        assert row.status == 'mismatch' and env.member.donor_until is None

    def test_a_period_running_out_ends_it(self, env):
        row = pay(env, 'inv1')
        row.current_period_end = utcnow() - timedelta(minutes=1)
        db.session.commit()

        assert end_expired_prepaid() == 1
        db.session.refresh(env.member)
        assert row.status == 'ended' and env.member.donor_until is None

    def test_one_with_time_left_stays(self, env):
        pay(env, 'inv1')
        assert end_expired_prepaid() == 0

    def test_the_task(self, env):
        row = pay(env, 'inv1')
        row.current_period_end = utcnow() - timedelta(minutes=1)
        db.session.commit()

        assert end_expired_subscriptions() == 1

    def test_the_task_failing_is_loud(self, env):
        with patch('app.payments.retention.end_expired_prepaid', side_effect=RuntimeError('no')), pytest.raises(RuntimeError):
            end_expired_subscriptions()

    def test_a_refund_ends_it(self, env):
        from app.payments.events import Refunded, SubscriptionEnded
        row = pay(env, 'inv1')
        apply_event('btcpay', 'evt_bad', [Refunded(reference='inv1'), SubscriptionEnded(reference='inv1')])

        assert row.status == 'ended' and Payment.query.filter_by(provider_reference='inv1').one().status == 'refunded'


class TestReminders:
    def due(self, env, days=5):
        row = pay(env, 'inv1')
        row.current_period_end = utcnow() + timedelta(days=days)
        db.session.commit()
        return row

    def test_a_period_about_to_run_out_is_mentioned_once(self, env):
        row = self.due(env)
        with patch('app.payments.core.send_prepaid_reminder') as mail:
            assert remind_expiring_prepaid() == 1
            assert remind_expiring_prepaid() == 0

        mail.assert_called_once_with(env.member, 'Prepaid', row.current_period_end)
        assert row.reminded_at is not None

    def test_one_with_longer_to_go_isnt(self, env):
        self.due(env, days=8)
        with patch('app.payments.core.send_prepaid_reminder') as mail:
            remind_expiring_prepaid()

        mail.assert_not_called()

    def test_one_already_over_isnt(self, env):
        self.due(env, days=-1)
        with patch('app.payments.core.send_prepaid_reminder') as mail:
            remind_expiring_prepaid()

        mail.assert_not_called()

    def test_what_recurs_isnt(self, env):
        db.session.add(MemberSubscription(user_id=env.member.id, offer_id=env.plan.id, provider_kind='stripe',
                                          provider_reference='sub_s', status='active'))
        db.session.commit()
        with patch('app.payments.core.send_prepaid_reminder') as mail:
            remind_expiring_prepaid()

        mail.assert_not_called()

    def test_renewing_earns_a_new_reminder(self, env):
        self.due(env)
        with patch('app.payments.core.send_prepaid_reminder'):
            remind_expiring_prepaid()

        assert pay(env, 'inv2').reminded_at is None

    def test_a_failed_mail_is_tried_again(self, env):
        row = self.due(env)
        with patch('app.payments.core.send_prepaid_reminder', side_effect=OSError('down')):
            assert remind_expiring_prepaid() == 0

        assert row.reminded_at is None

    def test_nobody_to_mail(self, env):
        self.due(env)
        env.member.email = ''
        db.session.commit()
        with patch('app.payments.core.send_prepaid_reminder') as mail:
            remind_expiring_prepaid()

        mail.assert_not_called()

    def test_a_plan_since_removed_still_gets_one(self, env):
        row = self.due(env)
        row.offer_id = None
        db.session.commit()
        with patch('app.payments.core.send_prepaid_reminder') as mail:
            remind_expiring_prepaid()

        assert mail.call_args.args[1] == ''

    def test_the_mail_says_when_and_where(self, env):
        row = self.due(env)
        with patch('app.email.send_email') as send:   # no request: the daily task has none
            send_prepaid_reminder(env.member, 'Prepaid', row.current_period_end)

        date = row.current_period_end.strftime('%Y-%m-%d')
        assert date in send.call_args.args[0]
        assert date in send.call_args.kwargs['text_body'] and '/donations' in send.call_args.kwargs['html_body']

    def test_the_daily_task_sends_them(self, env):
        self.due(env)
        with patch('app.payments.core.send_prepaid_reminder') as mail:
            end_expired_subscriptions()

        mail.assert_called_once()


class TestThePage:
    def test_a_held_period_shows_its_date_and_can_be_renewed(self, env):
        connect()
        row = pay(env, 'inv1')
        login(env.client, env.member)
        body = env.client.get('/donations').get_data(as_text=True)

        assert row.current_period_end.strftime('%Y-%m-%d') in body
        assert 'Pay for one month' in body and 'Cancel regular donation' not in body
        assert 'Your subscriptions' in body and 'Your regular donations' not in body

    def test_on_the_onion_a_period_shows_and_a_card_doesnt(self, env):
        connect(onion='http://pay.onion')
        row = pay(env, 'inv1')
        recurring = MemberSubscription(user_id=env.member.id, offer_id=env.gift.id, provider_kind='stripe',
                                       provider_reference='sub_x', status='active')
        db.session.add(recurring)
        db.session.commit()
        login(env.client, env.member)
        with patch('app.payments.routes.is_onion_request', return_value=True), \
                patch('app.payments.offers.is_onion_request', return_value=True):
            body = env.client.get('/donations').get_data(as_text=True)

        assert row.current_period_end.strftime('%Y-%m-%d') in body
        assert 'Cancel regular donation' not in body and 'Your payments' not in body

    def test_a_stranger_can_give_once(self, env):
        connect()
        body = env.client.get('/donations').get_data(as_text=True)

        assert 'Donate once' in body and 'Log in to' in body

    @respx.mock
    def test_paying_goes_to_the_server(self, env):
        connect()
        respx.post(f'{URL}/api/v1/stores/{STORE}/invoices').mock(return_value=httpx.Response(200, json={'id': 'inv1'}))

        response = env.client.post(f'/donations/checkout/{env.gift.id}')

        assert response.headers['Location'] == f'{URL}/i/inv1'


class TestAdmin:
    @pytest.fixture
    def admin(self, env):
        admin = make_user(env.baseline.instance_local, 'moneyadmin', local=True)
        grant_permission(admin, 'manage monetization')
        login(env.client, admin)
        with env.app.test_request_context():
            token = generate_csrf()
            raw = flask_session['csrf_token']
        with env.client.session_transaction() as session:
            session['csrf_token'] = raw
        return token

    def form(self, token, **values):
        return {'url': URL, 'onion_url': '', 'store_id': STORE, 'api_key': 'k', 'webhook_secret': SECRET, 'enabled': 'y',
                'csrf_token': token, **values}

    @respx.mock
    def test_connecting_checks_the_key_and_seals_it(self, env, admin):
        respx.get(f'{URL}/api/v1/stores/{STORE}/invoices').mock(return_value=httpx.Response(200, json=[]))
        env.client.post('/admin/payments/btcpay', data=self.form(admin))

        row = PaymentProvider.query.filter_by(kind='btcpay').one()
        assert row.settings['url'] == URL and 'k' not in row.settings.values() and BtcpayAdapter().available()

    @respx.mock
    def test_a_refused_key_changes_nothing(self, env, admin):
        respx.get(f'{URL}/api/v1/stores/{STORE}/invoices').mock(return_value=httpx.Response(401))
        env.client.post('/admin/payments/btcpay', data=self.form(admin))

        assert PaymentProvider.query.filter_by(kind='btcpay').first() is None

    def test_disabling_needs_no_server(self, env, admin):
        env.client.post('/admin/payments/btcpay', data=self.form(admin, enabled=''))

        assert PaymentProvider.query.filter_by(kind='btcpay').one().enabled is False

    @pytest.mark.parametrize('values', [{'url': 'ftp://x'}, {'url': 'https://x/path'}, {'onion_url': 'https://clearnet.example'}],
                             ids=['scheme', 'path', 'not-onion'])
    def test_a_bad_address_is_refused(self, env, admin, values, monkeypatch):
        monkeypatch.setitem(current_app.config, 'WTF_CSRF_ENABLED', True)
        env.client.post('/admin/payments/btcpay', data=self.form(admin, **values))

        assert PaymentProvider.query.filter_by(kind='btcpay').first() is None

    def test_with_no_encryption_key_nothing_is_stored(self, env, admin):
        with patch('app.admin.payments_routes.credentials_available', return_value=False):
            env.client.post('/admin/payments/btcpay', data=self.form(admin))

        assert PaymentProvider.query.filter_by(kind='btcpay').first() is None

    @respx.mock
    def test_blank_secrets_keep_what_is_stored(self, env, admin):
        connect()
        route = respx.get(f'https://new.example/api/v1/stores/{STORE}/invoices').mock(return_value=httpx.Response(200, json=[]))
        env.client.post('/admin/payments/btcpay', data=self.form(admin, api_key='', webhook_secret='', url='https://new.example/'))

        row = PaymentProvider.query.filter_by(kind='btcpay').one()
        assert route.called and row.settings['url'] == 'https://new.example' and BtcpayAdapter().available()

    @respx.mock
    def test_secrets_that_cant_be_opened_must_be_entered_again(self, env, admin):
        db.session.add(PaymentProvider(kind='btcpay', credentials='garbage', settings={'url': URL, 'store_id': STORE}))
        db.session.commit()
        env.client.post('/admin/payments/btcpay', data=self.form(admin, api_key='', webhook_secret=''))

        assert PaymentProvider.query.filter_by(kind='btcpay').one().credentials == 'garbage'   # no key to ask with

    def test_saving_with_nothing_entered_stores_no_secret(self, env, admin):
        env.client.post('/admin/payments/btcpay', data=self.form(admin, url='', store_id='', api_key='', webhook_secret='', enabled=''))

        row = PaymentProvider.query.filter_by(kind='btcpay').one()
        assert row.credentials is None and row.enabled is False

    def test_a_second_offer_keeps_the_sites_currency(self, env, admin):
        set_setting('payment_currency', 'usd')
        env.client.post('/admin/payments/btcpay/offers/add', data=self.offer(admin))

        assert Offer.query.filter_by(title='New').one().currency == 'usd'

    def offer(self, token, **values):
        return {'title': 'New', 'amount': '12.50', 'currency': 'usd', 'interval': 'year', 'offer_group': 'plans', 'position': '0',
                'active': 'y', 'csrf_token': token, 'save': 'Save', **values}

    def test_adding_an_offer_sets_the_sites_currency(self, env, admin):
        env.client.post('/admin/payments/btcpay/offers/add', data=self.offer(admin))

        offer = Offer.query.filter_by(title='New').one()
        assert (offer.amount_minor, offer.currency, offer.kind, offer.interval) == (1250, 'usd', 'subscription', 'year')

    def test_a_one_off_has_no_period(self, env, admin):
        env.client.post('/admin/payments/btcpay/offers/add', data=self.offer(admin, interval='', donor_days='9'))

        offer = Offer.query.filter_by(title='New').one()
        assert (offer.kind, offer.interval, offer.donor_days) == ('donation', None, 9)

    @pytest.mark.parametrize('code', ['XXQ', 'eur'], ids=['no-such', 'another'])
    def test_the_wrong_currency_is_refused(self, env, admin, code):
        set_setting('payment_currency', 'usd')
        with patch('app.admin.payments_routes.render_template', return_value='form'):
            env.client.post('/admin/payments/btcpay/offers/add', data=self.offer(admin, currency=code))

        assert Offer.query.filter_by(title='New').first() is None

    def test_the_form_opens(self, env, admin, monkeypatch):
        monkeypatch.setitem(current_app.config, 'WTF_CSRF_ENABLED', True)
        assert 'Amount' in env.client.get('/admin/payments/btcpay/offers/add').get_data(as_text=True)
        body = env.client.get(f'/admin/payments/offers/{env.plan.id}/edit').get_data(as_text=True)
        assert '5.00' in body and 'value="USD"' in body
        assert 'BTCPay Server' in env.client.get('/admin/payments').get_data(as_text=True)

    def test_editing(self, env, admin):
        env.client.post(f'/admin/payments/offers/{env.plan.id}/edit', data=self.offer(admin, title='Renamed', amount='7'))

        db.session.refresh(env.plan)
        assert env.plan.title == 'Renamed' and env.plan.amount_minor == 700

    def test_an_offer_a_domain_charges_cant_be_stopped(self, env, admin):
        db.session.add(SiteDomain(domain=current_app.config['SERVER_NAME'], scheme='https', federates=True, is_default=True))
        db.session.add(SiteDomain(domain='paid.example', scheme='https', federates=False, registration='paid',
                                  registration_offer_id=env.plan.id))
        db.session.commit()
        clear_domain_cache()
        with patch('app.admin.payments_routes.render_template', return_value='form'):
            env.client.post(f'/admin/payments/offers/{env.plan.id}/edit', data=self.offer(admin, active=''))

        db.session.refresh(env.plan)
        clear_domain_cache()
        assert env.plan.active is True
