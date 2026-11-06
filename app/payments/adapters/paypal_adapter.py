"""PayPal's REST API: Subscriptions v1 for what recurs, Orders v2 for a one-off. Never offered on the onion (C11).

**Not run against PayPal.** Its API docs confirm the calls, but not the fields inside a webhook's payload, which are written here
from what PayPal is known to send. So a payload of another shape grants nothing rather than something wrong, and a sandbox
run is owed before anybody is charged.

A subscription starts on its first completed payment, not on being approved: an approval isn't money, and the amount can only
be checked against a payment (spec, Security). A browser coming back from PayPal does nothing: an order is captured when the
webhook says it was approved.
"""
import json
from datetime import datetime, timezone

import httpx

from app.models import MemberSubscription, Offer, Payment, PaymentProvider
from app.payments.adapters.base import InvalidWebhook, ProviderError
from app.payments.adapters.btcpay_adapter import from_minor, to_minor
from app.payments.credentials import CredentialsUnavailable, unseal
from app.payments.events import Paid, Refunded, SubscriptionEnded, SubscriptionStarted

LIVE, SANDBOX = 'https://api-m.paypal.com', 'https://api-m.sandbox.paypal.com'
TIMEOUT_SECONDS = 20
APPROVED = 'CHECKOUT.ORDER.APPROVED'
CAPTURED, CAPTURE_REFUNDED, CAPTURE_REVERSED = 'PAYMENT.CAPTURE.COMPLETED', 'PAYMENT.CAPTURE.REFUNDED', 'PAYMENT.CAPTURE.REVERSED'
SALE, SALE_REFUNDED, SALE_REVERSED = 'PAYMENT.SALE.COMPLETED', 'PAYMENT.SALE.REFUNDED', 'PAYMENT.SALE.REVERSED'
# Suspended is what repeated failed payments lead to, and nothing is being paid while it lasts. Coming back means subscribing again.
ENDED = ('BILLING.SUBSCRIPTION.CANCELLED', 'BILLING.SUBSCRIPTION.EXPIRED', 'BILLING.SUBSCRIPTION.SUSPENDED')
REQUIRED_EVENTS = (APPROVED, CAPTURED, CAPTURE_REFUNDED, CAPTURE_REVERSED, SALE, SALE_REFUNDED, SALE_REVERSED, *ENDED)
_SIGNATURE_HEADERS = {'auth_algo': 'PAYPAL-AUTH-ALGO', 'cert_url': 'PAYPAL-CERT-URL', 'transmission_id': 'PAYPAL-TRANSMISSION-ID',
                      'transmission_sig': 'PAYPAL-TRANSMISSION-SIG', 'transmission_time': 'PAYPAL-TRANSMISSION-TIME'}
_INTERVALS = {'MONTH': 'month', 'YEAR': 'year', 'WEEK': 'week', 'DAY': 'day'}


def _mapping(value, key: str) -> dict:
    found = value.get(key) if isinstance(value, dict) else None
    return found if isinstance(found, dict) else {}


def _text(value: dict, key: str) -> str:
    found = value.get(key)
    return found if isinstance(found, str) else ''


def _custom(offer: Offer, user) -> str:
    return f"{offer.id}:{user.id if user else ''}"


def _parse_custom(custom: str) -> tuple[int | None, int | None]:
    """(offer id, account id) from what was sent at checkout."""
    offer, _, user = custom.partition(':')
    return int(offer) if offer.isascii() and offer.isdigit() else None, int(user) if user.isascii() and user.isdigit() else None


def _approval(answer: dict) -> str:
    link = next((link.get('href') for link in answer.get('links') or [] if isinstance(link, dict) and link.get('rel') == 'approve'), None)
    if not link:
        raise ProviderError('PayPal gave no approval address')
    return link


class PaypalAdapter:
    kind = 'paypal'
    prepaid = False

    def _config(self) -> dict | None:
        row = PaymentProvider.query.filter_by(kind=self.kind).first()
        if row is None or not row.enabled or not row.credentials:
            return None
        try:
            return {**(row.settings or {}), **unseal(row.credentials)}
        except CredentialsUnavailable:
            return None   # C4

    def available(self) -> bool:
        config = self._config()
        return bool(config and config.get('client_id') and config.get('client_secret'))

    def on_onion(self) -> bool:
        return False   # C11

    def _configured(self) -> dict:
        config = self._config()
        if not config:
            raise ProviderError('PayPal is not configured')
        return config

    @staticmethod
    def _base(config: dict) -> str:
        return SANDBOX if config.get('sandbox') else LIVE

    def _token(self, config: dict) -> str:
        try:
            response = httpx.post(f'{self._base(config)}/v1/oauth2/token', data={'grant_type': 'client_credentials'},
                                  auth=(config.get('client_id', ''), config.get('client_secret', '')), timeout=TIMEOUT_SECONDS)
        except httpx.HTTPError as error:
            raise ProviderError(f'PayPal unreachable: {type(error).__name__}') from None
        if response.status_code != 200:
            raise ProviderError(f'PayPal refused: {response.status_code}')
        try:
            return response.json()['access_token']
        except (ValueError, KeyError, TypeError):
            raise ProviderError('PayPal answered with something else') from None

    def _send(self, method: str, config: dict, path: str, **kwargs) -> httpx.Response:
        token = self._token(config)
        headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json', **kwargs.pop('headers', {})}
        try:
            return httpx.request(method, f'{self._base(config)}{path}', headers=headers, timeout=TIMEOUT_SECONDS, **kwargs)
        except httpx.HTTPError as error:
            raise ProviderError(f'PayPal unreachable: {type(error).__name__}') from None

    def _request(self, method: str, config: dict, path: str, **kwargs) -> dict:
        response = self._send(method, config, path, **kwargs)
        if response.status_code not in (200, 201):
            raise ProviderError(f'PayPal refused: {response.status_code}')
        try:
            answer = response.json()
        except ValueError:
            raise ProviderError('PayPal answered with something else') from None
        if not isinstance(answer, dict):
            raise ProviderError('PayPal answered with something else')
        return answer

    def checkout(self, offer: Offer, user, success_url: str, cancel_url: str) -> str:
        config = self._configured()
        context = {'return_url': success_url, 'cancel_url': cancel_url, 'shipping_preference': 'NO_SHIPPING'}
        if offer.kind == 'subscription':
            return _approval(self._request('POST', config, '/v1/billing/subscriptions', json={
                'plan_id': offer.provider_price_id, 'custom_id': _custom(offer, user),
                'application_context': {**context, 'user_action': 'SUBSCRIBE_NOW'}}))
        return _approval(self._request('POST', config, '/v2/checkout/orders', json={
            'intent': 'CAPTURE', 'application_context': {**context, 'user_action': 'PAY_NOW'},
            'purchase_units': [{'custom_id': _custom(offer, user), 'amount': {
                'currency_code': offer.currency.upper(), 'value': from_minor(offer.amount_minor, offer.currency)}}]}))

    def parse_webhook(self, body: bytes, headers) -> tuple[str, list]:
        config = self._config() or {}
        if not config.get('webhook_id'):
            raise InvalidWebhook('no webhook id')
        signature = {name: headers.get(header) for name, header in _SIGNATURE_HEADERS.items()}
        if not all(signature.values()):
            raise InvalidWebhook('unsigned')
        try:
            text = body.decode('utf-8')
            event = json.loads(text)
        except ValueError:
            raise InvalidWebhook('not json') from None
        if not isinstance(event, dict) or not _text(event, 'id'):
            raise InvalidWebhook('not an event')
        # The event goes back to PayPal exactly as it came: a reserialised one fails the check
        content = json.dumps({**signature, 'webhook_id': config['webhook_id']})[:-1] + ', "webhook_event": ' + text + '}'
        answer = self._request('POST', config, '/v1/notifications/verify-webhook-signature', content=content)
        if answer.get('verification_status') != 'SUCCESS':
            raise InvalidWebhook('not from PayPal')
        return event['id'], self._events(config, _text(event, 'event_type'), _mapping(event, 'resource'))

    def _events(self, config: dict, kind: str, resource: dict) -> list:
        if kind == APPROVED:
            return self._capture(config, _text(resource, 'id'))
        if kind == CAPTURED:
            return self._capture_paid(resource)
        if kind == SALE:
            return self._sale_paid(config, resource)
        if kind in (CAPTURE_REFUNDED, CAPTURE_REVERSED):
            up = next((link.get('href') for link in resource.get('links') or []
                       if isinstance(link, dict) and link.get('rel') == 'up'), '')
            amount = _mapping(resource, 'amount')
            return self._refund(up.rsplit('/', 1)[-1] if isinstance(up, str) else '', _text(amount, 'value'), _text(amount, 'currency_code'))
        if kind in (SALE_REFUNDED, SALE_REVERSED):
            amount = _mapping(resource, 'amount')
            return self._refund(_text(resource, 'sale_id') or _text(resource, 'id'), _text(amount, 'total'), _text(amount, 'currency'))
        if kind in ENDED and _text(resource, 'id'):
            return [SubscriptionEnded(reference=resource['id'])]
        return []

    def _capture(self, config: dict, order_id: str) -> list:
        """What follows is PAYMENT.CAPTURE.COMPLETED, which is what counts. Already captured is what was asked for."""
        if not order_id:
            return []
        response = self._send('POST', config, f'/v2/checkout/orders/{order_id}/capture', content='{}',
                              headers={'PayPal-Request-Id': order_id})
        if response.status_code not in (200, 201) and 'ORDER_ALREADY_CAPTURED' not in response.text:
            raise ProviderError(f'PayPal refused: {response.status_code}')
        return []

    @staticmethod
    def _capture_paid(resource: dict) -> list:
        amount, reference = _mapping(resource, 'amount'), _text(resource, 'id')
        offer_id, user_id = _parse_custom(_text(resource, 'custom_id'))
        try:
            minor = to_minor(_text(amount, 'value'), _text(amount, 'currency_code'))
        except ValueError:
            return []
        return [Paid(reference=reference, amount_minor=minor, currency=_text(amount, 'currency_code'), user_id=user_id,
                     offer_id=offer_id)] if reference else []

    def _sale_paid(self, config: dict, resource: dict) -> list:
        """A subscription's payment, the first of which starts it. A later one must not start it again: that would end
        whatever else the member has moved to in its group."""
        amount, reference = _mapping(resource, 'amount'), _text(resource, 'id')
        subscription = _text(resource, 'billing_agreement_id')
        if not reference or not subscription:
            return []
        custom = _text(resource, 'custom') or _text(resource, 'custom_id') \
            or _text(self._request('GET', config, f'/v1/billing/subscriptions/{subscription}'), 'custom_id')
        offer_id, user_id = _parse_custom(custom)
        try:
            minor = to_minor(_text(amount, 'total'), _text(amount, 'currency'))
        except ValueError:
            return []
        events = [Paid(reference=reference, amount_minor=minor, currency=_text(amount, 'currency'), user_id=user_id, offer_id=offer_id)]
        if not MemberSubscription.query.filter_by(provider_kind=self.kind, provider_reference=subscription).first():
            events.append(SubscriptionStarted(reference=subscription, user_id=user_id, offer_id=offer_id))
        return events

    def _refund(self, reference: str, value: str, currency: str) -> list:
        """A whole refund only: part of one leaves the payment standing, as for Stripe."""
        payment = Payment.query.filter_by(provider_kind=self.kind, provider_reference=reference).first() if reference else None
        if payment is None:
            return []
        try:
            refunded = abs(to_minor(value, currency))
        except ValueError:
            return []
        return [Refunded(reference=reference)] if refunded == payment.amount_minor else []

    def cancel(self, reference: str) -> None:
        config = self._configured()
        response = self._send('POST', config, f'/v1/billing/subscriptions/{reference}/cancel', content='{"reason": "Cancelled by the member"}')
        if response.status_code not in (200, 204, 404, 422):   # gone, or already cancelled: what was asked for
            raise ProviderError(f'PayPal refused: {response.status_code}')

    def cancel_at_period_end(self, reference: str) -> datetime:
        """PayPal has no such thing: cancelling is at once. What's been paid for runs to the next billing date, which is
        read first, and our own record of it is what `core` keeps to (MemberSubscription.ends_at)."""
        config = self._configured()
        subscription = self._request('GET', config, f'/v1/billing/subscriptions/{reference}')
        try:
            ends = datetime.fromisoformat(_text(_mapping(subscription, 'billing_info'), 'next_billing_time').replace('Z', '+00:00'))
        except ValueError:
            raise ProviderError('PayPal gave no billing date') from None
        self.cancel(reference)
        return ends.astimezone(timezone.utc).replace(tzinfo=None)

    def billing_portal_url(self, customer: str, return_url: str) -> str:
        raise ProviderError('a member manages their card at PayPal')

    def describe_price(self, plan_id: str) -> dict:
        """Refuses a plan whose first payment wouldn't be its price, as every such payment would be recorded as a mismatch."""
        plan = self._request('GET', self._configured(), f'/v1/billing/plans/{plan_id}')
        cycles = [cycle for cycle in plan.get('billing_cycles') or [] if isinstance(cycle, dict)]
        regular = [cycle for cycle in cycles if cycle.get('tenure_type') == 'REGULAR']
        fee = _mapping(_mapping(plan, 'payment_preferences'), 'setup_fee')
        taxes = _mapping(plan, 'taxes')
        if len(regular) != 1 or len(cycles) != 1 or float(fee.get('value') or 0) or (float(taxes.get('percentage') or 0) and not taxes.get('inclusive')):
            raise ProviderError('a plan with a trial, a setup fee or added tax')
        frequency, price = _mapping(regular[0], 'frequency'), _mapping(_mapping(regular[0], 'pricing_scheme'), 'fixed_price')
        interval = _INTERVALS.get(_text(frequency, 'interval_unit'))
        if interval is None or frequency.get('interval_count', 1) != 1:
            raise ProviderError('a period this site cannot name')
        try:
            minor = to_minor(_text(price, 'value'), _text(price, 'currency_code'))
        except ValueError:
            raise ProviderError('no price') from None
        return {'amount_minor': minor, 'currency': _text(price, 'currency_code').lower(), 'interval': interval}

    def check_credentials(self, values: dict) -> None:
        self._token(values)
