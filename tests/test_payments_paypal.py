"""PayPal, against mocks: it has not been run against PayPal itself (plan phase 6)."""
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
from app.models import MemberSubscription, Offer, Payment, PaymentProvider, Site, utcnow
from app.payments.adapters.base import InvalidWebhook, ProviderError
from app.payments.adapters.paypal_adapter import LIVE, SANDBOX, PaypalAdapter
from app.payments.core import apply_event, end_expired_prepaid
from app.payments.credentials import seal
from app.payments.events import Paid, SubscriptionEnded, SubscriptionStarted
from app.payments.offers import offers_changed, offers_on
from app.utils import set_setting
from tests.factories import grant_permission, make_user

HEADERS = {'PAYPAL-AUTH-ALGO': 'SHA256withRSA', 'PAYPAL-CERT-URL': 'https://api.paypal.com/cert', 'PAYPAL-TRANSMISSION-ID': 't1',
           'PAYPAL-TRANSMISSION-SIG': 'sig', 'PAYPAL-TRANSMISSION-TIME': '2026-11-06T00:00:00Z'}


def connect(enabled=True, sandbox=True):
    db.session.add(PaymentProvider(kind='paypal', enabled=enabled, credentials=seal({'client_id': 'id', 'client_secret': 'shh'}),
                                   settings={'webhook_id': 'wh1', 'sandbox': sandbox}))
    db.session.commit()
    offers_changed()


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setitem(current_app.config, 'PAYMENT_SECRETS_KEY', Fernet.generate_key().decode())
    g.admin_ids = []
    db.session.get(Site, 1).private_instance = False
    plan = Offer(provider_kind='paypal', kind='subscription', title='Plan', offer_group='plans', provider_price_id='P-1',
                 amount_minor=500, currency='usd', interval='month', grants_donor=True)
    gift = Offer(provider_kind='paypal', kind='donation', title='Gift', offer_group='gifts', amount_minor=300, currency='usd')
    db.session.add_all([plan, gift])
    member = make_user(api_baseline.instance_local, 'member', local=True)
    db.session.commit()
    yield SimpleNamespace(app=app, client=app.test_client(), plan=plan, gift=gift, member=member, baseline=api_baseline)
    offers_changed()


@pytest.fixture
def adapter():
    return PaypalAdapter()


@pytest.fixture
def paypal():
    with respx.mock(assert_all_called=False) as router:
        router.post(f'{SANDBOX}/v1/oauth2/token').mock(return_value=httpx.Response(200, json={'access_token': 'tok'}))
        router.post(f'{SANDBOX}/v1/notifications/verify-webhook-signature').mock(
            return_value=httpx.Response(200, json={'verification_status': 'SUCCESS'}))
        yield router


@pytest.fixture(autouse=True)
def no_task():
    with patch('app.payments.core.cancel_at_provider'):
        yield


def hook(kind, resource, event_id='WH-1'):
    return json.dumps({'id': event_id, 'event_type': kind, 'resource': resource}).encode()


def sale(env, reference='S1', total='5.00', subscription='I-1'):
    return {'id': reference, 'billing_agreement_id': subscription, 'custom': f'{env.plan.id}:{env.member.id}',
            'amount': {'total': total, 'currency': 'USD'}}


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


class TestBeingConfigured:
    def test_nothing_stored_is_off(self, env, adapter):
        assert not adapter.available()

    def test_connected(self, env, adapter):
        connect()
        assert adapter.available()

    def test_disabled_is_off(self, env, adapter):
        connect(enabled=False)
        assert not adapter.available()

    def test_a_key_that_cant_open_it_is_off(self, env, adapter):
        db.session.add(PaymentProvider(kind='paypal', credentials='garbage'))
        db.session.commit()
        assert not adapter.available()

    def test_never_on_the_onion(self, env, adapter):
        connect()
        assert env.plan.id in {offer.id for offer in offers_on(None, False)}
        assert offers_on(None, True) == [] and not adapter.on_onion()

    def test_live_and_sandbox(self, adapter):
        assert adapter._base({'sandbox': True}) == SANDBOX and adapter._base({}) == LIVE


class TestCheckout:
    def test_a_subscription(self, env, adapter, paypal):
        connect()
        route = paypal.post(f'{SANDBOX}/v1/billing/subscriptions').mock(return_value=httpx.Response(201, json={
            'links': [{'rel': 'self', 'href': 'x'}, {'rel': 'approve', 'href': 'https://paypal.example/approve'}]}))

        assert adapter.checkout(env.plan, env.member, 'https://x/ok', 'https://x/no') == 'https://paypal.example/approve'
        sent = json.loads(route.calls[0].request.content)
        assert sent['plan_id'] == 'P-1' and sent['custom_id'] == f'{env.plan.id}:{env.member.id}'
        assert sent['application_context']['return_url'] == 'https://x/ok'
        assert route.calls[0].request.headers['Authorization'] == 'Bearer tok'

    def test_a_guests_one_off_names_nobody(self, env, adapter, paypal):
        connect()
        route = paypal.post(f'{SANDBOX}/v2/checkout/orders').mock(return_value=httpx.Response(201, json={
            'links': [{'rel': 'approve', 'href': 'https://paypal.example/o'}]}))

        assert adapter.checkout(env.gift, None, 'a', 'b') == 'https://paypal.example/o'
        unit = json.loads(route.calls[0].request.content)['purchase_units'][0]
        assert unit['custom_id'] == f'{env.gift.id}:' and unit['amount'] == {'currency_code': 'USD', 'value': '3.00'}

    def test_no_approval_address(self, env, adapter, paypal):
        connect()
        paypal.post(f'{SANDBOX}/v2/checkout/orders').mock(return_value=httpx.Response(201, json={'links': []}))
        with pytest.raises(ProviderError):
            adapter.checkout(env.gift, None, 'a', 'b')

    def test_not_configured(self, env, adapter):
        with pytest.raises(ProviderError):
            adapter.checkout(env.gift, None, 'a', 'b')

    @pytest.mark.parametrize('answer', [httpx.Response(401, text='shh'), httpx.Response(200, text='nonsense'),
                                        httpx.Response(200, json={'no': 'token'})], ids=['refused', 'not-json', 'no-token'])
    def test_a_token_refused_carries_no_secret(self, env, adapter, answer):
        connect()
        with respx.mock(assert_all_called=False) as router:
            router.post(f'{SANDBOX}/v1/oauth2/token').mock(return_value=answer)
            with pytest.raises(ProviderError) as refused:
                adapter.checkout(env.gift, None, 'a', 'b')

        assert 'shh' not in str(refused.value)

    def test_unreachable(self, env, adapter):
        connect()
        with respx.mock(assert_all_called=False) as router:
            router.post(f'{SANDBOX}/v1/oauth2/token').mock(side_effect=httpx.ConnectError('no'))
            with pytest.raises(ProviderError):
                adapter.checkout(env.gift, None, 'a', 'b')

    def test_unreachable_after_the_token(self, env, adapter, paypal):
        connect()
        paypal.post(f'{SANDBOX}/v2/checkout/orders').mock(side_effect=httpx.ConnectError('no'))
        with pytest.raises(ProviderError):
            adapter.checkout(env.gift, None, 'a', 'b')

    @pytest.mark.parametrize('answer', [httpx.Response(500), httpx.Response(200, text='nonsense'), httpx.Response(200, json=[1])],
                             ids=['refused', 'not-json', 'list'])
    def test_a_bad_answer(self, env, adapter, paypal, answer):
        connect()
        paypal.post(f'{SANDBOX}/v2/checkout/orders').mock(return_value=answer)
        with pytest.raises(ProviderError):
            adapter.checkout(env.gift, None, 'a', 'b')


class TestWebhookCheck:
    def test_nothing_stored_accepts_nothing(self, env, adapter):
        with pytest.raises(InvalidWebhook):
            adapter.parse_webhook(hook('X', {}), HEADERS)

    def test_unsigned(self, env, adapter):
        connect()
        with pytest.raises(InvalidWebhook):
            adapter.parse_webhook(hook('X', {}), {})

    @pytest.mark.parametrize('body', [b'[[', b'[1]', b'{"event_type": "X"}'], ids=['not-json', 'list', 'no-id'])
    def test_not_an_event(self, env, adapter, body):
        connect()
        with pytest.raises(InvalidWebhook):
            adapter.parse_webhook(body, HEADERS)

    def test_paypal_says_no(self, env, adapter):
        connect()
        with respx.mock(assert_all_called=False) as router:
            router.post(f'{SANDBOX}/v1/oauth2/token').mock(return_value=httpx.Response(200, json={'access_token': 'tok'}))
            router.post(f'{SANDBOX}/v1/notifications/verify-webhook-signature').mock(
                return_value=httpx.Response(200, json={'verification_status': 'FAILURE'}))
            with pytest.raises(InvalidWebhook):
                adapter.parse_webhook(hook('X', {}), HEADERS)

    def test_the_event_goes_back_exactly_as_it_came(self, env, adapter, paypal):
        connect()
        raw = b'{"id":  "WH-1", "event_type": "X", "resource": {"total": "5.00"}}'
        adapter.parse_webhook(raw, HEADERS)

        sent = paypal.calls[-1].request.content.decode()
        assert raw.decode() in sent
        assert json.loads(sent)['webhook_id'] == 'wh1' and json.loads(sent)['transmission_sig'] == 'sig'

    def test_the_route_refuses_and_retries(self, env):
        connect()
        assert env.client.post('/payments/webhook/paypal', data=b'{}', headers=HEADERS).status_code == 400
        with respx.mock(assert_all_called=False) as router:
            router.post(f'{SANDBOX}/v1/oauth2/token').mock(return_value=httpx.Response(500))
            assert env.client.post('/payments/webhook/paypal', data=hook('X', {}), headers=HEADERS).status_code == 503


class TestEvents:
    def events(self, adapter, kind, resource):
        return adapter.parse_webhook(hook(kind, resource), HEADERS)[1]

    def test_an_approved_order_is_captured(self, env, adapter, paypal):
        connect()
        route = paypal.post(f'{SANDBOX}/v2/checkout/orders/O1/capture').mock(return_value=httpx.Response(201, json={}))

        assert self.events(adapter, 'CHECKOUT.ORDER.APPROVED', {'id': 'O1'}) == []
        assert route.calls[0].request.headers['PayPal-Request-Id'] == 'O1'

    def test_already_captured_is_fine(self, env, adapter, paypal):
        connect()
        paypal.post(f'{SANDBOX}/v2/checkout/orders/O1/capture').mock(return_value=httpx.Response(422, json={'details': [{'issue': 'ORDER_ALREADY_CAPTURED'}]}))
        assert self.events(adapter, 'CHECKOUT.ORDER.APPROVED', {'id': 'O1'}) == []

    def test_a_capture_failing_is_retried(self, env, adapter, paypal):
        connect()
        paypal.post(f'{SANDBOX}/v2/checkout/orders/O1/capture').mock(return_value=httpx.Response(500))
        with pytest.raises(ProviderError):
            self.events(adapter, 'CHECKOUT.ORDER.APPROVED', {'id': 'O1'})

    def test_no_order_named(self, env, adapter, paypal):
        connect()
        assert self.events(adapter, 'CHECKOUT.ORDER.APPROVED', {}) == []

    def test_a_capture_is_a_payment(self, env, adapter, paypal):
        connect()
        paid = self.events(adapter, 'PAYMENT.CAPTURE.COMPLETED', {'id': 'C1', 'custom_id': f'{env.gift.id}:', 'amount': {'value': '3.00', 'currency_code': 'USD'}})[0]

        assert (paid.reference, paid.amount_minor, paid.user_id, paid.offer_id) == ('C1', 300, None, env.gift.id)

    @pytest.mark.parametrize('resource', [{'custom_id': '1:', 'amount': {'value': '3.00', 'currency_code': 'USD'}},
                                          {'id': 'C1', 'custom_id': '1:', 'amount': {'value': 'lots', 'currency_code': 'USD'}}],
                             ids=['no-id', 'no-amount'])
    def test_a_capture_of_another_shape_grants_nothing(self, env, adapter, paypal, resource):
        connect()
        assert self.events(adapter, 'PAYMENT.CAPTURE.COMPLETED', resource) == []

    def test_a_first_sale_starts_the_subscription(self, env, adapter, paypal):
        connect()
        events = self.events(adapter, 'PAYMENT.SALE.COMPLETED', sale(env))

        assert [type(event).__name__ for event in events] == ['Paid', 'SubscriptionStarted']
        assert events[0].user_id == env.member.id and events[1].reference == 'I-1'

    def test_a_later_sale_doesnt_start_it_again(self, env, adapter, paypal):
        connect()
        db.session.add(MemberSubscription(user_id=env.member.id, offer_id=env.plan.id, provider_kind='paypal', provider_reference='I-1', status='active'))
        db.session.commit()

        assert [type(event).__name__ for event in self.events(adapter, 'PAYMENT.SALE.COMPLETED', sale(env))] == ['Paid']

    def test_a_sale_with_no_custom_id_asks_paypal(self, env, adapter, paypal):
        connect()
        paypal.get(f'{SANDBOX}/v1/billing/subscriptions/I-1').mock(return_value=httpx.Response(200, json={'custom_id': f'{env.plan.id}:{env.member.id}'}))
        resource = {**sale(env), 'custom': ''}

        assert self.events(adapter, 'PAYMENT.SALE.COMPLETED', resource)[0].user_id == env.member.id

    @pytest.mark.parametrize('change', [{'billing_agreement_id': ''}, {'id': ''}, {'amount': {'total': 'lots', 'currency': 'USD'}}],
                             ids=['no-subscription', 'no-id', 'no-amount'])
    def test_a_sale_of_another_shape_grants_nothing(self, env, adapter, paypal, change):
        connect()
        assert self.events(adapter, 'PAYMENT.SALE.COMPLETED', {**sale(env), **change}) == []

    @pytest.mark.parametrize('kind', ['BILLING.SUBSCRIPTION.CANCELLED', 'BILLING.SUBSCRIPTION.EXPIRED', 'BILLING.SUBSCRIPTION.SUSPENDED'],
                             ids=['cancelled', 'expired', 'suspended'])
    def test_it_ending(self, env, adapter, paypal, kind):
        connect()
        assert self.events(adapter, kind, {'id': 'I-1'}) == [SubscriptionEnded(reference='I-1')]

    def test_an_ending_naming_nothing(self, env, adapter, paypal):
        connect()
        assert self.events(adapter, 'BILLING.SUBSCRIPTION.EXPIRED', {}) == []

    def test_anything_else_is_nothing(self, env, adapter, paypal):
        connect()
        assert self.events(adapter, 'BILLING.PLAN.CREATED', {'id': 'x'}) == []

    def a_payment(self, env, reference='C1', amount=300):
        db.session.add(Payment(provider_kind='paypal', provider_reference=reference, amount_minor=amount, currency='usd', status='paid'))
        db.session.commit()

    @pytest.mark.parametrize('kind', ['PAYMENT.CAPTURE.REFUNDED', 'PAYMENT.CAPTURE.REVERSED'], ids=['refunded', 'reversed'])
    def test_a_whole_capture_refund(self, env, adapter, paypal, kind):
        connect()
        self.a_payment(env)
        events = self.events(adapter, kind, {'amount': {'value': '3.00', 'currency_code': 'USD'}, 'links': [
            {'rel': 'self', 'href': 'x'}, {'rel': 'up', 'href': f'{SANDBOX}/v2/payments/captures/C1'}]})

        assert [event.reference for event in events] == ['C1']

    def test_part_of_one_leaves_the_payment_standing(self, env, adapter, paypal):
        connect()
        self.a_payment(env)
        assert self.events(adapter, 'PAYMENT.CAPTURE.REFUNDED', {'amount': {'value': '1.00', 'currency_code': 'USD'}, 'links': [
            {'rel': 'up', 'href': 'x/C1'}]}) == []

    def test_a_sale_refund_can_be_negative(self, env, adapter, paypal):
        connect()
        self.a_payment(env, 'S1', 500)
        events = self.events(adapter, 'PAYMENT.SALE.REFUNDED', {'id': 'R1', 'sale_id': 'S1', 'amount': {'total': '-5.00', 'currency': 'USD'}})

        assert [event.reference for event in events] == ['S1']

    def test_a_reversal_names_the_sale_as_itself(self, env, adapter, paypal):
        connect()
        self.a_payment(env, 'S1', 500)
        assert self.events(adapter, 'PAYMENT.SALE.REVERSED', {'id': 'S1', 'amount': {'total': '5.00', 'currency': 'USD'}})[0].reference == 'S1'

    @pytest.mark.parametrize('resource', [{'sale_id': 'nobody', 'amount': {'total': '5.00', 'currency': 'USD'}},
                                          {'sale_id': 'S1', 'amount': {'total': 'lots', 'currency': 'USD'}}, {}],
                             ids=['unknown', 'no-amount', 'empty'])
    def test_a_refund_of_another_shape_is_nothing(self, env, adapter, paypal, resource):
        connect()
        self.a_payment(env, 'S1', 500)
        assert self.events(adapter, 'PAYMENT.SALE.REFUNDED', resource) == []


class TestCancelling:
    def test_at_once(self, env, adapter, paypal):
        connect()
        route = paypal.post(f'{SANDBOX}/v1/billing/subscriptions/I-1/cancel').mock(return_value=httpx.Response(204))
        adapter.cancel('I-1')
        assert route.called

    def test_already_gone_is_what_was_asked_for(self, env, adapter, paypal):
        connect()
        paypal.post(f'{SANDBOX}/v1/billing/subscriptions/I-1/cancel').mock(return_value=httpx.Response(422))
        adapter.cancel('I-1')

    def test_refused(self, env, adapter, paypal):
        connect()
        paypal.post(f'{SANDBOX}/v1/billing/subscriptions/I-1/cancel').mock(return_value=httpx.Response(500))
        with pytest.raises(ProviderError):
            adapter.cancel('I-1')

    def test_the_billing_date_is_read_first(self, env, adapter, paypal):
        connect()
        paypal.get(f'{SANDBOX}/v1/billing/subscriptions/I-1').mock(return_value=httpx.Response(200, json={'billing_info': {'next_billing_time': '2026-12-01T10:00:00Z'}}))
        cancel = paypal.post(f'{SANDBOX}/v1/billing/subscriptions/I-1/cancel').mock(return_value=httpx.Response(204))

        ends = adapter.cancel_at_period_end('I-1')

        assert (ends.year, ends.month, ends.tzinfo) == (2026, 12, None) and cancel.called

    def test_no_date_cancels_nothing(self, env, adapter, paypal):
        connect()
        paypal.get(f'{SANDBOX}/v1/billing/subscriptions/I-1').mock(return_value=httpx.Response(200, json={}))
        cancel = paypal.post(f'{SANDBOX}/v1/billing/subscriptions/I-1/cancel').mock(return_value=httpx.Response(204))

        with pytest.raises(ProviderError):
            adapter.cancel_at_period_end('I-1')
        assert not cancel.called

    def test_no_portal(self, adapter):
        with pytest.raises(ProviderError):
            adapter.billing_portal_url('c', 'r')


class TestWhatsBeenPaidForRunsOut:
    def row(self, env, ends):
        apply_event('paypal', 'evt_1', [Paid(reference='S1', amount_minor=500, currency='USD', user_id=env.member.id, offer_id=env.plan.id),
                                        SubscriptionStarted(reference='I-1', user_id=env.member.id, offer_id=env.plan.id)])
        row = MemberSubscription.query.filter_by(provider_reference='I-1').one()
        row.ends_at = ends
        db.session.commit()
        return row

    def test_its_immediate_event_is_ignored_until_then(self, env):
        row = self.row(env, utcnow() + timedelta(days=9))
        apply_event('paypal', 'evt_2', [SubscriptionEnded(reference='I-1')])

        assert row.status == 'active'

    def test_the_daily_task_ends_it(self, env):
        row = self.row(env, utcnow() - timedelta(minutes=1))

        assert end_expired_prepaid() == 1 and row.status == 'ended'


def a_plan(**change):
    return {'billing_cycles': [{'tenure_type': 'REGULAR', 'frequency': {'interval_unit': 'MONTH', 'interval_count': 1},
                                'pricing_scheme': {'fixed_price': {'value': '5.00', 'currency_code': 'USD'}}}], **change}


class TestPlans:
    def test_its_price_is_read(self, env, adapter, paypal):
        connect()
        paypal.get(f'{SANDBOX}/v1/billing/plans/P-1').mock(return_value=httpx.Response(200, json=a_plan()))

        assert adapter.describe_price('P-1') == {'amount_minor': 500, 'currency': 'usd', 'interval': 'month'}

    @pytest.mark.parametrize('plan', [
        a_plan(payment_preferences={'setup_fee': {'value': '1.00'}}), a_plan(taxes={'percentage': '10', 'inclusive': False}),
        {'billing_cycles': [{'tenure_type': 'TRIAL'}, *a_plan()['billing_cycles']]}, {'billing_cycles': []},
        {'billing_cycles': [{**a_plan()['billing_cycles'][0], 'frequency': {'interval_unit': 'MONTH', 'interval_count': 3}}]},
        {'billing_cycles': [{**a_plan()['billing_cycles'][0], 'pricing_scheme': {}}]},
    ], ids=['setup-fee', 'tax', 'trial', 'no-cycles', 'quarterly', 'no-price'])
    def test_what_cant_be_checked_is_refused(self, env, adapter, paypal, plan):
        connect()
        paypal.get(f'{SANDBOX}/v1/billing/plans/P-1').mock(return_value=httpx.Response(200, json=plan))
        with pytest.raises(ProviderError):
            adapter.describe_price('P-1')

    def test_tax_included_is_fine(self, env, adapter, paypal):
        connect()
        paypal.get(f'{SANDBOX}/v1/billing/plans/P-1').mock(return_value=httpx.Response(200, json=a_plan(taxes={'percentage': '10', 'inclusive': True})))
        assert adapter.describe_price('P-1')['amount_minor'] == 500


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
        return {'client_id': 'id', 'client_secret': 'shh', 'webhook_id': 'wh1', 'sandbox': 'y', 'enabled': 'y', 'csrf_token': token, **values}

    def test_connecting_checks_and_seals(self, env, admin, paypal):
        env.client.post('/admin/payments/paypal', data=self.form(admin))

        row = PaymentProvider.query.filter_by(kind='paypal').one()
        assert row.settings == {'webhook_id': 'wh1', 'sandbox': True} and 'shh' not in row.credentials and PaypalAdapter().available()

    def test_refused_changes_nothing(self, env, admin):
        with respx.mock(assert_all_called=False) as router:
            router.post(f'{SANDBOX}/v1/oauth2/token').mock(return_value=httpx.Response(401))
            env.client.post('/admin/payments/paypal', data=self.form(admin))

        assert PaymentProvider.query.filter_by(kind='paypal').first() is None

    def test_disabling_with_nothing_entered(self, env, admin):
        env.client.post('/admin/payments/paypal', data=self.form(admin, client_id='', client_secret='', enabled=''))

        row = PaymentProvider.query.filter_by(kind='paypal').one()
        assert row.enabled is False and row.credentials is None

    def test_no_encryption_key_stores_nothing(self, env, admin):
        with patch('app.admin.payments_routes.credentials_available', return_value=False):
            env.client.post('/admin/payments/paypal', data=self.form(admin))

        assert PaymentProvider.query.filter_by(kind='paypal').first() is None

    def test_a_field_too_long_shows_the_page_again(self, env, admin):
        with patch('app.admin.payments_routes.render_template', return_value='page'):
            assert env.client.post('/admin/payments/paypal', data=self.form(admin, webhook_id='x' * 300)).get_data(as_text=True) == 'page'

    def offer(self, token, **values):
        return {'title': 'New', 'plan_id': '', 'amount': '', 'currency': 'usd', 'offer_group': 'plans', 'position': '0', 'active': 'y',
                'csrf_token': token, 'save': 'Save', **values}

    def test_a_plan_offer_reads_paypal(self, env, admin, paypal):
        connect()
        paypal.get(f'{SANDBOX}/v1/billing/plans/P-9').mock(return_value=httpx.Response(200, json=a_plan()))
        env.client.post('/admin/payments/paypal/offers/add', data=self.offer(admin, plan_id='P-9'))

        offer = Offer.query.filter_by(title='New').one()
        assert (offer.provider_kind, offer.kind, offer.provider_price_id, offer.amount_minor, offer.interval) == ('paypal', 'subscription', 'P-9', 500, 'month')

    @pytest.mark.parametrize('answer', [httpx.Response(404), httpx.Response(200, json=a_plan(taxes={'percentage': '5'}))], ids=['unknown', 'tax'])
    def test_a_plan_it_cant_use_is_refused(self, env, admin, paypal, answer):
        connect()
        paypal.get(f'{SANDBOX}/v1/billing/plans/P-9').mock(return_value=answer)
        with patch('app.admin.payments_routes.render_template', return_value='form'):
            env.client.post('/admin/payments/paypal/offers/add', data=self.offer(admin, plan_id='P-9'))

        assert Offer.query.filter_by(title='New').first() is None

    def test_a_plan_in_another_currency_is_refused(self, env, admin, paypal):
        connect()
        set_setting('payment_currency', 'eur')
        paypal.get(f'{SANDBOX}/v1/billing/plans/P-9').mock(return_value=httpx.Response(200, json=a_plan()))
        with patch('app.admin.payments_routes.render_template', return_value='form'):
            env.client.post('/admin/payments/paypal/offers/add', data=self.offer(admin, plan_id='P-9'))

        assert Offer.query.filter_by(title='New').first() is None

    def test_a_one_off_at_an_amount(self, env, admin):
        env.client.post('/admin/payments/paypal/offers/add', data=self.offer(admin, amount='12.50', donor_days='9'))

        offer = Offer.query.filter_by(title='New').one()
        assert (offer.kind, offer.amount_minor, offer.currency, offer.provider_price_id, offer.donor_days) == ('donation', 1250, 'usd', None, 9)

    def test_a_second_one_off_keeps_the_currency(self, env, admin):
        set_setting('payment_currency', 'usd')
        env.client.post('/admin/payments/paypal/offers/add', data=self.offer(admin, amount='2'))

        assert Offer.query.filter_by(title='New').one().currency == 'usd'

    @pytest.mark.parametrize('values', [{}, {'amount': '2', 'currency': 'XXQ'}, {'amount': '2', 'currency': 'eur'}], ids=['nothing', 'no-such', 'another'])
    def test_what_isnt_an_amount_is_refused(self, env, admin, values):
        set_setting('payment_currency', 'usd')
        with patch('app.admin.payments_routes.render_template', return_value='form'):
            env.client.post('/admin/payments/paypal/offers/add', data=self.offer(admin, **values))

        assert Offer.query.filter_by(title='New').first() is None

    def test_an_offer_a_domain_charges_cant_be_stopped(self, env, admin):
        with patch('app.admin.payments_routes._names_a_paid_domain', return_value=True), \
                patch('app.admin.payments_routes.render_template', return_value='form'):
            env.client.post(f'/admin/payments/offers/{env.gift.id}/edit', data=self.offer(admin, active='', amount='3'))

        db.session.refresh(env.gift)
        assert env.gift.active is True

    def test_editing(self, env, admin):
        env.client.post(f'/admin/payments/offers/{env.gift.id}/edit', data=self.offer(admin, title='Renamed', amount='7'))

        db.session.refresh(env.gift)
        assert env.gift.title == 'Renamed' and env.gift.amount_minor == 700

    def test_the_pages_open(self, env, admin, monkeypatch):
        monkeypatch.setitem(current_app.config, 'WTF_CSRF_ENABLED', True)
        assert 'PayPal plan ID' in env.client.get('/admin/payments/paypal/offers/add').get_data(as_text=True)
        assert '3.00' in env.client.get(f'/admin/payments/offers/{env.gift.id}/edit').get_data(as_text=True)
        body = env.client.get('/admin/payments').get_data(as_text=True)
        assert 'PAYMENT.SALE.COMPLETED' in body and '/payments/webhook/paypal' in body
