"""The webhook routes, end to end through a real signature (plan phase 1, Task 6).

What the old /stripe_webhook tests pinned moved here and to tests/test_payments_stripe_adapter.py.
"""
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import stripe
from flask import current_app, g

from app import db
from app.models import MemberSubscription, Offer, Payment, PaymentEvent, PaymentProvider, Site
from tests.factories import make_user

SECRET = 'whsec_test'
PERIOD_END = 4102444800   # 2100-01-01T00:00:00Z: a paid-through date always in the future (it was today, once)


@pytest.fixture(autouse=True)
def stripe_subscription():
    """A checkout asks Stripe what the new subscription is paid through."""
    found = SimpleNamespace(to_dict=lambda: {'items': {'data': [{'current_period_end': PERIOD_END}]}})
    with patch('stripe.Subscription.retrieve', return_value=found) as retrieve:
        yield retrieve


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    monkeypatch.setitem(current_app.config, 'STRIPE_SECRET_KEY', 'sk_test')
    monkeypatch.setitem(current_app.config, 'WEBHOOK_SIGNING_SECRET', SECRET)
    offer = Offer(provider_kind='stripe', kind='subscription', title='Plan', offer_group='tiers',
                  provider_price_id='price_1', amount_minor=500, currency='usd', grants_donor=True)
    db.session.add(offer)
    member = make_user(api_baseline.instance_local, 'member', local=True)
    db.session.commit()
    return SimpleNamespace(client=app.test_client(), member=member, offer=offer)


def signed_post(client, payload, path='/payments/webhook/stripe', secret=SECRET):
    body = json.dumps(payload).encode()
    timestamp = int(time.time())
    signature = hmac.new(secret.encode(), b'%d.%s' % (timestamp, body), hashlib.sha256).hexdigest()
    return client.post(path, data=body, headers={'Stripe-Signature': f't={timestamp},v1={signature}'})


def completed(env, event_id='evt_1', subscription='sub_1', invoice='in_1'):
    return {'id': event_id, 'type': 'checkout.session.completed', 'data': {'object': {
        'id': 'cs_1', 'payment_status': 'paid', 'client_reference_id': str(env.member.id), 'customer': 'cus_1',
        'invoice': invoice, 'subscription': subscription, 'amount_subtotal': 500, 'currency': 'usd',
        'metadata': {'offer_id': str(env.offer.id)}}}}


@pytest.mark.parametrize('path', ['/payments/webhook/stripe', '/stripe_webhook'], ids=['new', 'existing_sites'])
def test_a_completed_checkout_is_recorded(env, path):
    response = signed_post(env.client, completed(env), path)
    assert response.status_code == 200
    assert Payment.query.one().amount_minor == 500
    assert MemberSubscription.query.one().provider_reference == 'sub_1'
    assert env.member.is_donor() and env.member.stripe_customer_id == 'cus_1'


def test_it_needs_no_login_and_no_csrf_token_on_a_private_site(env):
    db.session.get(Site, 1).private_instance = True
    db.session.commit()
    assert signed_post(env.client, completed(env)).status_code == 200


def test_the_same_event_twice_changes_nothing(env):
    signed_post(env.client, completed(env))
    assert signed_post(env.client, completed(env)).status_code == 200
    assert Payment.query.count() == 1


def test_a_bad_signature_changes_nothing(env):
    assert signed_post(env.client, completed(env), secret='whsec_other').status_code == 400
    assert Payment.query.count() == 0 and MemberSubscription.query.count() == 0


def test_no_signature_changes_nothing(env):
    assert env.client.post('/payments/webhook/stripe', data=b'{}').status_code == 400


def test_an_unknown_provider_is_not_found(env):
    assert signed_post(env.client, completed(env), '/payments/webhook/nothing').status_code == 404


def test_a_disabled_provider_is_not_found(env):
    db.session.add(PaymentProvider(kind='stripe', enabled=False))
    db.session.commit()
    assert signed_post(env.client, completed(env)).status_code == 404


def test_an_event_nobody_reads_is_acknowledged(env):
    assert signed_post(env.client, {'id': 'evt_2', 'type': 'customer.created', 'data': {'object': {}}}).status_code == 200


def test_switching_plan_cancels_the_old_one_at_stripe(env, app):
    signed_post(env.client, completed(env))
    with patch('app.payments.tasks.cancel_at_provider_task.delay') as cancel:
        signed_post(env.client, completed(env, 'evt_2', subscription='sub_2', invoice='in_2'))
    cancel.assert_called_once_with('stripe', 'sub_1')


def test_a_refund_of_a_subscription_payment_marks_its_invoice(env):
    """The whole way through, as the current Stripe API sends it: the charge names no invoice."""
    signed_post(env.client, completed(env))
    found = SimpleNamespace(data=[SimpleNamespace(invoice='in_1')])
    refund = {'id': 'evt_2', 'type': 'charge.refunded', 'data': {'object': {'refunded': True, 'payment_intent': 'pi_1'}}}
    with patch('stripe.InvoicePayment.list', return_value=found), patch('app.payments.core.cancel_at_provider') as cancel:
        assert signed_post(env.client, refund).status_code == 200
    assert Payment.query.one().status == 'refunded'
    # and the subscription stops being charged, running to the end of its month (plan 2026-11-15 B6)
    cancel.assert_called_once_with('stripe', 'sub_1')
    assert MemberSubscription.query.filter_by(provider_reference='sub_1').one().ends_at is not None


def test_stripe_being_down_during_a_refund_asks_it_to_send_again(env):
    signed_post(env.client, completed(env))
    refund = {'id': 'evt_2', 'type': 'charge.refunded', 'data': {'object': {'refunded': True, 'payment_intent': 'pi_1'}}}
    with patch('stripe.InvoicePayment.list', side_effect=stripe.APIConnectionError('down')):
        assert signed_post(env.client, refund).status_code == 503
    assert Payment.query.one().status == 'paid' and PaymentEvent.query.count() == 1


def test_a_failure_answers_500_so_stripe_tries_again(env, app, monkeypatch):
    monkeypatch.setitem(app.config, 'PROPAGATE_EXCEPTIONS', False)
    with patch('app.payments.routes.apply_event', side_effect=RuntimeError):
        assert signed_post(env.client, completed(env)).status_code == 500


def test_a_checkout_and_its_renewals_keep_the_renewal_date(env):
    signed_post(env.client, completed(env))
    row = MemberSubscription.query.one()
    assert row.renews_at == datetime.fromtimestamp(PERIOD_END, timezone.utc).replace(tzinfo=None)
    env.member.stripe_customer_id = 'cus_1'
    db.session.commit()
    later = PERIOD_END + 30 * 86400
    answer = signed_post(env.client, {'id': 'evt_2', 'type': 'invoice.paid', 'data': {'object': {
        'id': 'in_2', 'customer': 'cus_1', 'subtotal': 500, 'currency': 'usd', 'parent': {'subscription_details': {'subscription': 'sub_1'}},
        'lines': {'data': [{'price': {'id': 'price_1'}, 'period': {'end': later}}]}}}})
    assert answer.status_code == 200
    db.session.expire_all()
    assert MemberSubscription.query.one().renews_at == datetime.fromtimestamp(later, timezone.utc).replace(tzinfo=None)
