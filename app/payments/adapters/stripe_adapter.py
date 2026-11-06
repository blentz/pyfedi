"""Stripe, behind the adapter contract. Every Stripe word stops here: core sees only app/payments/events.py.

Credentials come from the sealed provider row, and until an admin imports them from the environment (C5). Every call
passes `api_key=` and never sets `stripe.api_key`, which is shared by every thread in the process. A ProviderError names
the kind of failure and never carries Stripe's message, which can echo part of a key.
"""
import hashlib

from flask import current_app

from app.models import Offer, PaymentProvider, User
from app.payments.adapters.base import InvalidWebhook, ProviderError
from app.payments.credentials import CredentialsUnavailable, unseal
from app.payments.events import CustomerKnown, Paid, Refunded, SubscriptionEnded, SubscriptionStarted

PAID = ('paid', 'no_payment_required')


def _stripe():
    import stripe  # lazy: stripe is slow to import (0.7s) and only billing uses it
    return stripe


def _text(mapping, key: str) -> str | None:
    value = mapping.get(key) if isinstance(mapping, dict) else None
    return value if isinstance(value, str) and value else None


def _mapping(mapping, key: str) -> dict:
    value = mapping.get(key) if isinstance(mapping, dict) else None
    return value if isinstance(value, dict) else {}


def _number(value) -> int | None:
    """A whole number from what Stripe holds as text or as a number, or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return int(value) if isinstance(value, str) and value.isascii() and value.isdigit() else None


def _offer_id_for_price(price_id: str | None) -> int | None:
    if not price_id:
        return None
    offer = Offer.query.filter_by(provider_kind='stripe', provider_price_id=price_id).first()
    return offer.id if offer else None


def _invoice_price_id(invoice: dict) -> str | None:
    """The first line's price, wherever this webhook's API version keeps it."""
    lines = _mapping(invoice, 'lines').get('data')
    line = lines[0] if isinstance(lines, list) and lines and isinstance(lines[0], dict) else {}
    return _text(_mapping(line, 'price'), 'id') or _text(_mapping(_mapping(line, 'pricing'), 'price_details'), 'price')


def _session_events(session: dict) -> list:
    """What a finished checkout means. One still waiting for a delayed payment method means nothing yet."""
    if session.get('payment_status') not in PAID:
        return []
    user_id = _number(session.get('client_reference_id'))
    offer_id = _number(_mapping(session, 'metadata').get('offer_id'))
    subscription = _text(session, 'subscription')
    events = []
    customer = _text(session, 'customer')
    if customer:
        events.append(CustomerKnown(user_id, customer))
    amount, currency = _number(session.get('amount_subtotal')), _text(session, 'currency')
    reference = _text(session, 'invoice') or _text(session, 'payment_intent') or _text(session, 'id')
    if amount and currency and reference:
        events.append(Paid(reference, amount, currency, user_id, offer_id))
    if subscription:
        events.append(SubscriptionStarted(subscription, user_id, offer_id))
    return events


def _invoice_events(invoice: dict) -> list:
    amount, currency, reference = _number(invoice.get('subtotal')), _text(invoice, 'currency'), _text(invoice, 'id')
    if not (amount and currency and reference):
        return []
    customer = _text(invoice, 'customer')
    user = User.query.filter_by(stripe_customer_id=customer).first() if customer else None
    return [Paid(reference, amount, currency, user.id if user else None, _offer_id_for_price(_invoice_price_id(invoice)))]


def _refund_events(charge: dict) -> list:
    """A whole refund only: part of one leaves the payment standing. A payment is known by its invoice or its intent."""
    if charge.get('refunded') is not True:
        return []
    return [Refunded(reference) for reference in (_text(charge, 'invoice'), _text(charge, 'payment_intent')) if reference]


def _subscription_ended_events(subscription: dict) -> list:
    reference = _text(subscription, 'id')
    return [SubscriptionEnded(reference)] if reference else []


_HANDLERS = {
    'checkout.session.completed': _session_events,
    'checkout.session.async_payment_succeeded': _session_events,
    'invoice.paid': _invoice_events,
    'customer.subscription.deleted': _subscription_ended_events,
    'charge.refunded': _refund_events,
}


class StripeAdapter:
    kind = 'stripe'

    def _config(self) -> dict | None:
        row = PaymentProvider.query.filter_by(kind=self.kind).first()
        if row is not None and row.credentials:
            if not row.enabled:
                return None
            try:
                return unseal(row.credentials)
            except CredentialsUnavailable:
                return None   # C4: no valid key means Stripe is off, and the site carries on
        if row is not None and not row.enabled:
            return None
        if current_app.config.get('STRIPE_SECRET_KEY'):
            return {'secret_key': current_app.config['STRIPE_SECRET_KEY'],
                    'webhook_secret': current_app.config.get('WEBHOOK_SIGNING_SECRET') or ''}
        return None

    def available(self) -> bool:
        config = self._config()
        return bool(config and config.get('secret_key'))

    def _key(self) -> str:
        config = self._config()
        if not config or not config.get('secret_key'):
            raise ProviderError('Stripe is not configured')
        return config['secret_key']

    def _call(self, function, *args, **kwargs):
        stripe = _stripe()
        try:
            return function(stripe)(*args, **kwargs)
        except stripe.StripeError as error:
            raise ProviderError(f'Stripe refused: {type(error).__name__}') from None

    def checkout(self, offer, user, success_url: str, cancel_url: str) -> str:
        key = self._key()
        customer = user.stripe_customer_id or None
        params = {'client_reference_id': str(user.id), 'line_items': [{'price': offer.provider_price_id, 'quantity': 1}],
                  'mode': 'subscription' if offer.kind == 'subscription' else 'payment',
                  'metadata': {'offer_id': str(offer.id)}, 'success_url': success_url, 'cancel_url': cancel_url,
                  # Stripe takes one or the other
                  'customer': customer, 'customer_email': None if customer else user.email}
        if offer.kind == 'subscription':
            params['subscription_data'] = {'metadata': {'offer_id': str(offer.id)}}
        session = self._call(lambda stripe: stripe.checkout.Session.create, api_key=key, **params)
        return session.url

    def parse_webhook(self, body: bytes, headers) -> tuple[str, list]:
        stripe = _stripe()
        config = self._config() or {}
        secret = config.get('webhook_secret')
        if not secret:
            raise InvalidWebhook('no signing secret')
        try:
            event = stripe.Webhook.construct_event(body.decode('utf-8'), headers.get('Stripe-Signature'), secret)
        except (ValueError, stripe.SignatureVerificationError):
            # UnicodeDecodeError and json's error are ValueErrors
            raise InvalidWebhook('not from Stripe') from None
        event = event.to_dict()   # stripe >= 13: a StripeObject is no dict (no .get)
        event_id = _text(event, 'id') or hashlib.sha256(body).hexdigest()
        handler = _HANDLERS.get(event.get('type')) if isinstance(event.get('type'), str) else None
        return event_id, handler(_mapping(_mapping(event, 'data'), 'object')) if handler else []

    def cancel(self, reference: str) -> None:
        stripe = _stripe()
        key = self._key()
        try:
            stripe.Subscription.cancel(reference, api_key=key)
        except stripe.InvalidRequestError as error:
            if error.code != 'resource_missing':   # already gone is what was asked for
                raise ProviderError(f'Stripe refused: {type(error).__name__}') from None
        except stripe.StripeError as error:
            raise ProviderError(f'Stripe refused: {type(error).__name__}') from None

    def billing_portal_url(self, customer: str, return_url: str) -> str:
        key = self._key()
        session = self._call(lambda stripe: stripe.billing_portal.Session.create, customer=customer,
                             return_url=return_url, api_key=key)
        return session.url

    def describe_price(self, price_id: str) -> dict:
        price = self._call(lambda stripe: stripe.Price.retrieve, price_id, api_key=self._key())
        recurring = price.recurring
        return {'amount_minor': price.unit_amount, 'currency': price.currency.lower(),
                'interval': recurring.interval if recurring else None}

    def validate_credentials(self, values: dict) -> None:
        self._call(lambda stripe: stripe.Price.list, limit=1, api_key=values.get('secret_key') or '')
