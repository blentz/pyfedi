"""Stripe behind the adapter contract (plan phase 1, Task 5). All against the SDK's mocks, plus a real signature."""
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import stripe
from cryptography.fernet import Fernet
from flask import current_app

from app import db
from app.models import Offer, PaymentProvider
from app.payments.adapters import adapter_for
from app.payments.adapters.base import InvalidWebhook, ProviderError
from app.payments.credentials import seal
from app.payments.events import CustomerKnown, Paid, Refunded, SubscriptionEnded, SubscriptionStarted
from tests.factories import make_instance, make_user

SECRET = 'whsec_test'
KEY = 'sk_test_env'


@pytest.fixture
def adapter(app, db_session, monkeypatch):
    monkeypatch.setitem(current_app.config, 'STRIPE_SECRET_KEY', KEY)
    monkeypatch.setitem(current_app.config, 'WEBHOOK_SIGNING_SECRET', SECRET)
    return adapter_for('stripe')


@pytest.fixture
def member(adapter):
    return make_user(make_instance('local.example'), 'member', local=True)


@pytest.fixture
def offer(adapter):
    row = Offer(provider_kind='stripe', kind='subscription', title='Plan', offer_group='tiers',
                provider_price_id='price_1', amount_minor=500, currency='usd')
    db.session.add(row)
    db.session.commit()
    return row


def signed(payload, secret=SECRET):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    timestamp = int(time.time())
    signature = hmac.new(secret.encode(), b'%d.%s' % (timestamp, body), hashlib.sha256).hexdigest()
    return body, {'Stripe-Signature': f't={timestamp},v1={signature}'}


def parse(adapter, event_type, obj):
    return adapter.parse_webhook(*signed({'id': 'evt_1', 'type': event_type, 'data': {'object': obj}}))


SESSION = {'id': 'cs_1', 'payment_status': 'paid', 'client_reference_id': '7', 'customer': 'cus_1', 'invoice': 'in_1',
           'payment_intent': None, 'subscription': 'sub_1', 'amount_subtotal': 500, 'currency': 'usd',
           'metadata': {'offer_id': '3'}}


class TestWho:
    def test_the_adapter_is_found_by_kind(self, adapter):
        assert adapter.kind == 'stripe'
        assert adapter_for('nothing') is None


class TestCredentials:
    def test_the_environment_serves_until_something_is_stored(self, adapter):
        assert adapter.available() and adapter._key() == KEY

    def test_nothing_anywhere_means_unavailable(self, adapter, monkeypatch):
        monkeypatch.setitem(current_app.config, 'STRIPE_SECRET_KEY', '')
        assert not adapter.available()
        with pytest.raises(ProviderError):
            adapter._key()

    def stored(self, monkeypatch, enabled=True):
        monkeypatch.setitem(current_app.config, 'PAYMENT_SECRETS_KEY', Fernet.generate_key().decode())
        db.session.add(PaymentProvider(kind='stripe', enabled=enabled,
                                       credentials=seal({'secret_key': 'sk_test_stored', 'webhook_secret': 'whsec_s'})))
        db.session.commit()

    def test_stored_credentials_win(self, adapter, monkeypatch):
        self.stored(monkeypatch)
        assert adapter._key() == 'sk_test_stored'

    def test_a_disabled_provider_is_off(self, adapter, monkeypatch):
        self.stored(monkeypatch, enabled=False)
        assert not adapter.available()

    def test_a_disabled_row_with_nothing_stored_is_off_too(self, adapter):
        db.session.add(PaymentProvider(kind='stripe', enabled=False))
        db.session.commit()
        assert not adapter.available()

    def test_losing_the_key_turns_stripe_off_and_does_not_fall_back(self, adapter, monkeypatch):
        self.stored(monkeypatch)
        monkeypatch.setitem(current_app.config, 'PAYMENT_SECRETS_KEY', '')
        assert not adapter.available()


class TestCheckout:
    def test_a_subscription(self, adapter, member, offer):
        with patch('stripe.checkout.Session.create', return_value=SimpleNamespace(url='https://stripe.test/c')) as create:
            url = adapter.checkout(offer, member, 'https://a/ok', 'https://a/no')
        assert url == 'https://stripe.test/c'
        params = create.call_args.kwargs
        assert params['api_key'] == KEY
        assert params['mode'] == 'subscription'
        assert params['client_reference_id'] == str(member.id)
        assert params['line_items'] == [{'price': 'price_1', 'quantity': 1}]
        assert params['metadata'] == params['subscription_data']['metadata'] == {'offer_id': str(offer.id)}
        assert (params['success_url'], params['cancel_url']) == ('https://a/ok', 'https://a/no')

    def test_a_one_off(self, adapter, member, offer):
        offer.kind = 'donation'
        with patch('stripe.checkout.Session.create', return_value=SimpleNamespace(url='u')) as create:
            adapter.checkout(offer, member, 'o', 'c')
        assert create.call_args.kwargs['mode'] == 'payment'
        assert 'subscription_data' not in create.call_args.kwargs

    @pytest.mark.parametrize('stored', ['', None], ids=['empty', 'none'])
    def test_a_newcomer_is_known_by_email(self, adapter, member, offer, stored):
        member.stripe_customer_id, member.email = stored, 'who@example.test'
        with patch('stripe.checkout.Session.create', return_value=SimpleNamespace(url='u')) as create:
            adapter.checkout(offer, member, 'o', 'c')
        assert create.call_args.kwargs['customer'] is None
        assert create.call_args.kwargs['customer_email'] == 'who@example.test'

    def test_a_returning_donor_is_known_by_customer(self, adapter, member, offer):
        member.stripe_customer_id = 'cus_1'
        with patch('stripe.checkout.Session.create', return_value=SimpleNamespace(url='u')) as create:
            adapter.checkout(offer, member, 'o', 'c')
        assert create.call_args.kwargs['customer'] == 'cus_1'
        assert create.call_args.kwargs['customer_email'] is None

    def test_a_refusal_carries_no_stripe_text(self, adapter, member, offer):
        refusal = stripe.AuthenticationError('Invalid API Key provided: sk_test_env')
        with patch('stripe.checkout.Session.create', side_effect=refusal), pytest.raises(ProviderError) as caught:
            adapter.checkout(offer, member, 'o', 'c')
        assert 'sk_test' not in str(caught.value)

    def test_the_global_key_is_never_set(self, adapter, member, offer):
        before = stripe.api_key
        with patch('stripe.checkout.Session.create', return_value=SimpleNamespace(url='u')):
            adapter.checkout(offer, member, 'o', 'c')
        assert stripe.api_key == before


class TestSignatures:
    def test_a_good_one_passes(self, adapter):
        assert parse(adapter, 'nothing.we.read', {}) == ('evt_1', [])

    def test_no_signature(self, adapter):
        with pytest.raises(InvalidWebhook):
            adapter.parse_webhook(b'{}', {})

    def test_the_wrong_secret(self, adapter):
        with pytest.raises(InvalidWebhook):
            adapter.parse_webhook(*signed({'id': 'e'}, secret='whsec_other'))

    def test_nonsense_for_a_signature(self, adapter):
        with pytest.raises(InvalidWebhook):
            adapter.parse_webhook(b'{}', {'Stripe-Signature': 'nonsense'})

    def test_a_replay(self, adapter):
        old = int(time.time()) - 3600
        body = b'{"id": "e"}'
        signature = hmac.new(SECRET.encode(), b'%d.%s' % (old, body), hashlib.sha256).hexdigest()
        with pytest.raises(InvalidWebhook):
            adapter.parse_webhook(body, {'Stripe-Signature': f't={old},v1={signature}'})

    @pytest.mark.parametrize('body', [b'not json', b'\xff\xfe'], ids=['not_json', 'not_utf8'])
    def test_a_signed_body_that_is_not_json(self, adapter, body):
        with pytest.raises(InvalidWebhook):
            adapter.parse_webhook(*signed(body))

    def test_no_secret_configured_refuses_everything(self, adapter, monkeypatch):
        monkeypatch.setitem(current_app.config, 'WEBHOOK_SIGNING_SECRET', '')
        with pytest.raises(InvalidWebhook):
            adapter.parse_webhook(*signed({'id': 'e'}))


class TestACompletedCheckout:
    def test_a_subscription(self, adapter):
        assert parse(adapter, 'checkout.session.completed', SESSION)[1] == [
            CustomerKnown(7, 'cus_1'), Paid('in_1', 500, 'usd', 7, 3), SubscriptionStarted('sub_1', 7, 3)]

    def test_a_delayed_payment_method_that_came_through(self, adapter):
        assert len(parse(adapter, 'checkout.session.async_payment_succeeded', SESSION)[1]) == 3

    def test_a_one_off_is_known_by_its_intent(self, adapter):
        session = {**SESSION, 'invoice': None, 'subscription': None, 'payment_intent': 'pi_1'}
        assert parse(adapter, 'checkout.session.completed', session)[1] == [
            CustomerKnown(7, 'cus_1'), Paid('pi_1', 500, 'usd', 7, 3)]

    def test_with_nothing_else_it_is_known_by_its_session(self, adapter):
        session = {**SESSION, 'invoice': None, 'subscription': None, 'payment_intent': None}
        assert parse(adapter, 'checkout.session.completed', session)[1][1].reference == 'cs_1'

    @pytest.mark.parametrize('status', ['unpaid', None, 7], ids=['unpaid', 'none', 'number'])
    def test_a_payment_not_yet_made_grants_nothing(self, adapter, status):
        assert parse(adapter, 'checkout.session.completed', {**SESSION, 'payment_status': status})[1] == []

    def test_no_customer_says_nothing_about_one(self, adapter):
        events = parse(adapter, 'checkout.session.completed', {**SESSION, 'customer': None})[1]
        assert not any(isinstance(event, CustomerKnown) for event in events)

    def test_a_free_start_records_no_payment(self, adapter):
        events = parse(adapter, 'checkout.session.completed', {**SESSION, 'amount_subtotal': 0,
                                                               'payment_status': 'no_payment_required'})[1]
        assert not any(isinstance(event, Paid) for event in events)
        assert any(isinstance(event, SubscriptionStarted) for event in events)

    @pytest.mark.parametrize('reference', [None, 'abc', '', 7, True, '٣', {}],
                             ids=['none', 'text', 'empty', 'number', 'bool', 'arabic_digit', 'object'])
    def test_a_reference_is_a_whole_number_or_nobody(self, adapter, reference):
        event = parse(adapter, 'checkout.session.completed', {**SESSION, 'client_reference_id': reference})[1][1]
        assert event.user_id == (7 if reference == 7 else None)

    @pytest.mark.parametrize('metadata', [None, {}, {'offer_id': 'x'}, 'x'], ids=['none', 'empty', 'text', 'not_object'])
    def test_no_usable_offer_means_none(self, adapter, metadata):
        assert parse(adapter, 'checkout.session.completed', {**SESSION, 'metadata': metadata})[1][1].offer_id is None


class TestAnInvoice:
    INVOICE = {'id': 'in_2', 'customer': 'cus_1', 'subtotal': 500, 'currency': 'usd',
               'lines': {'data': [{'price': {'id': 'price_1'}}]}}

    def test_a_renewal_finds_its_member_and_offer(self, adapter, member, offer):
        member.stripe_customer_id = 'cus_1'
        db.session.commit()
        assert parse(adapter, 'invoice.paid', self.INVOICE)[1] == [Paid('in_2', 500, 'usd', member.id, offer.id)]

    def test_the_newer_api_keeps_the_price_elsewhere(self, adapter, offer):
        invoice = {**self.INVOICE, 'lines': {'data': [{'pricing': {'price_details': {'price': 'price_1'}}}]}}
        assert parse(adapter, 'invoice.paid', invoice)[1][0].offer_id == offer.id

    @pytest.mark.parametrize('lines', [None, {}, {'data': []}, {'data': ['x']}, {'data': [{'price': {'id': 'gone'}}]}],
                             ids=['none', 'empty', 'no_lines', 'odd_line', 'unknown_price'])
    def test_an_unknown_offer_and_customer_still_record_the_money(self, adapter, lines):
        invoice = {**self.INVOICE, 'customer': None, 'lines': lines}
        assert parse(adapter, 'invoice.paid', invoice)[1] == [Paid('in_2', 500, 'usd', None, None)]

    @pytest.mark.parametrize('field, value', [('subtotal', 0), ('subtotal', None), ('currency', None), ('id', None)],
                             ids=['free', 'no_amount', 'no_currency', 'no_id'])
    def test_no_money_or_no_name_records_nothing(self, adapter, field, value):
        assert parse(adapter, 'invoice.paid', {**self.INVOICE, field: value})[1] == []


class TestOtherEvents:
    def test_a_subscription_ending(self, adapter):
        assert parse(adapter, 'customer.subscription.deleted', {'id': 'sub_1'})[1] == [SubscriptionEnded('sub_1')]

    @pytest.mark.parametrize('obj', [{}, {'id': None}, {'id': 7}], ids=['empty', 'none', 'number'])
    def test_an_ending_naming_nothing(self, adapter, obj):
        assert parse(adapter, 'customer.subscription.deleted', obj)[1] == []

    def test_a_whole_refund_names_both_references(self, adapter):
        charge = {'refunded': True, 'invoice': 'in_1', 'payment_intent': 'pi_1'}
        assert parse(adapter, 'charge.refunded', charge)[1] == [Refunded('in_1'), Refunded('pi_1')]

    def test_part_of_a_refund_changes_nothing(self, adapter):
        assert parse(adapter, 'charge.refunded', {'refunded': False, 'payment_intent': 'pi_1'})[1] == []


class TestOddShapes:
    @pytest.mark.parametrize('payload', [
        {'id': 'e'}, {'id': 'e', 'type': 'charge.refunded'}, {'id': 'e', 'type': 'charge.refunded', 'data': None},
        {'id': 'e', 'type': 'charge.refunded', 'data': {'object': 'x'}}, {'id': 'e', 'type': 7},
        {'id': 'e', 'type': 'checkout.session.completed', 'data': {}}],
        ids=['no_type', 'no_data', 'null_data', 'object_not_object', 'number_type', 'data_without_object'])
    def test_they_mean_nothing_and_do_not_raise(self, adapter, payload):
        assert adapter.parse_webhook(*signed(payload)) == ('e', [])

    def test_an_event_with_no_id_is_named_by_its_body(self, adapter):
        body, headers = signed({'type': 'nothing'})
        assert adapter.parse_webhook(body, headers)[0] == hashlib.sha256(body).hexdigest()


class TestCancelling:
    def test_it_cancels_with_the_key_it_has(self, adapter):
        with patch('stripe.Subscription.cancel') as cancel:
            adapter.cancel('sub_1')
        cancel.assert_called_once_with('sub_1', api_key=KEY)

    def test_already_gone_is_done(self, adapter):
        gone = stripe.InvalidRequestError('No such subscription', 'id', code='resource_missing')
        with patch('stripe.Subscription.cancel', side_effect=gone):
            adapter.cancel('sub_1')

    def test_another_refusal_is_a_failure(self, adapter):
        refused = stripe.InvalidRequestError('Nope', 'id', code='other')
        with patch('stripe.Subscription.cancel', side_effect=refused), pytest.raises(ProviderError):
            adapter.cancel('sub_1')

    def test_an_outage_is_a_failure(self, adapter):
        with patch('stripe.Subscription.cancel', side_effect=stripe.APIConnectionError('down')), \
                pytest.raises(ProviderError):
            adapter.cancel('sub_1')


class TestTheRest:
    def test_the_billing_portal(self, adapter):
        with patch('stripe.billing_portal.Session.create', return_value=SimpleNamespace(url='https://p')) as create:
            assert adapter.billing_portal_url('cus_1', 'https://back') == 'https://p'
        create.assert_called_once_with(customer='cus_1', return_url='https://back', api_key=KEY)

    def test_a_recurring_price_is_described(self, adapter):
        price = SimpleNamespace(unit_amount=500, currency='USD', recurring=SimpleNamespace(interval='month'))
        with patch('stripe.Price.retrieve', return_value=price):
            assert adapter.describe_price('price_1') == {'amount_minor': 500, 'currency': 'usd', 'interval': 'month'}

    def test_a_one_off_price_is_described(self, adapter):
        price = SimpleNamespace(unit_amount=500, currency='usd', recurring=None)
        with patch('stripe.Price.retrieve', return_value=price):
            assert adapter.describe_price('price_1')['interval'] is None

    def test_good_credentials_pass(self, adapter):
        with patch('stripe.Price.list') as listing:
            adapter.validate_credentials({'secret_key': 'sk_test_new'})
        listing.assert_called_once_with(limit=1, api_key='sk_test_new')

    def test_bad_credentials_fail(self, adapter):
        with patch('stripe.Price.list', side_effect=stripe.AuthenticationError('bad')), pytest.raises(ProviderError):
            adapter.validate_credentials({})
