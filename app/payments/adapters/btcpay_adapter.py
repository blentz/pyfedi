"""A self-hosted BTCPay Server, through its Greenfield API (M4). Checked against a real server on regtest:
compose.btcpay-regtest.yaml and tests/test_payments_btcpay_regtest.py.

Nothing recurs: a crypto subscription is a period paid for in advance, which the member renews (`prepaid`). There is
nothing to cancel at the provider, and no card to manage. An invoice that is settled is what grants anything, and its
amount is asked of the server rather than read from the webhook, which doesn't carry one.
"""
import hashlib
import hmac
import json
from decimal import Decimal, InvalidOperation

import httpx
from babel.numbers import get_currency_precision

from app.models import Offer, PaymentProvider
from app.onion import is_onion_request
from app.payments.adapters.base import InvalidWebhook, ProviderError
from app.payments.credentials import CredentialsUnavailable, unseal
from app.payments.events import Paid, Refunded, SubscriptionEnded, SubscriptionStarted

TIMEOUT_SECONDS = 20
INVOICE_MINUTES = 60   # how long the price holds. Slow confirmation after payment is the server's, and is allowed for
SIGNATURE_HEADER = 'BTCPay-Sig'
SETTLED, INVALID = 'InvoiceSettled', 'InvoiceInvalid'
REQUIRED_EVENTS = (SETTLED, INVALID)
REQUIRED_PERMISSIONS = ('btcpay.store.cancreateinvoice', 'btcpay.store.canviewinvoices')


def _number(value) -> int | None:
    return int(value) if isinstance(value, str) and value.isascii() and value.isdigit() else None


def to_minor(amount: str, currency: str) -> int:
    """'5.00' USD as 500. Raises ValueError for what isn't an amount."""
    try:
        return int((Decimal(amount) * 10 ** get_currency_precision(currency.upper())).to_integral_exact())
    except (InvalidOperation, TypeError) as error:
        raise ValueError('not an amount') from error


def from_minor(amount_minor: int, currency: str) -> str:
    precision = get_currency_precision(currency.upper())
    return f'{Decimal(amount_minor) / 10 ** precision:.{precision}f}'


class BtcpayAdapter:
    kind = 'btcpay'
    prepaid = True

    def _config(self) -> dict | None:
        """The address and store, which aren't secret, with the key and signing secret, which are sealed."""
        row = PaymentProvider.query.filter_by(kind=self.kind).first()
        if row is None or not row.enabled or not row.credentials:
            return None
        try:
            return {**(row.settings or {}), **unseal(row.credentials)}
        except CredentialsUnavailable:
            return None   # C4: no valid key means this is off, and the site carries on

    def available(self) -> bool:
        config = self._config()
        return bool(config and config.get('url') and config.get('store_id') and config.get('api_key'))

    def on_onion(self) -> bool:
        """Only with an onion address of its own, so that no redirect names a host to the wrong party (C11)."""
        config = self._config()
        return bool(config and config.get('onion_url'))

    def _request(self, method: str, config: dict, path: str, **kwargs) -> dict | list:
        """Not the site's pinned client: an operator's own server is often on a private address. An admin sets this
        address, and only a yes or no about it comes back to them."""
        try:
            response = httpx.request(method, f"{config['url'].rstrip('/')}/api/v1/stores/{config['store_id']}{path}",
                                     headers={'Authorization': f"token {config['api_key']}"}, timeout=TIMEOUT_SECONDS,
                                     **kwargs)
        except httpx.HTTPError as error:
            raise ProviderError(f'BTCPay unreachable: {type(error).__name__}') from None
        if response.status_code != 200:
            raise ProviderError(f'BTCPay refused: {response.status_code}')
        try:
            return response.json()
        except ValueError:
            raise ProviderError('BTCPay answered with something else') from None

    def _configured(self) -> dict:
        config = self._config()
        if not config:
            raise ProviderError('BTCPay is not configured')
        return config

    def checkout(self, offer: Offer, user, success_url: str, cancel_url: str) -> str:
        """The address is built here because the server names whichever host it was asked on, and this site may reach it
        by another name than a member's browser does."""
        config = self._configured()
        metadata = {'offer_id': str(offer.id)}
        if user:
            metadata['user_id'] = str(user.id)
        invoice = self._request('POST', config, '/invoices', json={
            'amount': from_minor(offer.amount_minor, offer.currency), 'currency': offer.currency.upper(), 'metadata': metadata,
            'checkout': {'redirectURL': success_url, 'redirectAutomatically': True, 'expirationMinutes': INVOICE_MINUTES}})
        base = config['onion_url'] if is_onion_request() else config['url']
        return f"{base.rstrip('/')}/i/{invoice['id']}"

    def parse_webhook(self, body: bytes, headers) -> tuple[str, list]:
        config = self._config() or {}
        secret = config.get('webhook_secret')
        if not secret:
            raise InvalidWebhook('no signing secret')
        expected = 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, headers.get(SIGNATURE_HEADER) or ''):
            raise InvalidWebhook('not from this server')
        try:
            event = json.loads(body)
        except ValueError:
            raise InvalidWebhook('not json') from None
        if not isinstance(event, dict) or event.get('storeId') != config.get('store_id'):
            raise InvalidWebhook('not for this store')
        invoice_id = event.get('invoiceId')
        # A redelivery has a new deliveryId and the same originalDeliveryId (seen on a real server)
        event_id = event.get('originalDeliveryId') or event.get('deliveryId') or hashlib.sha256(body).hexdigest()
        if not isinstance(invoice_id, str) or not invoice_id or not isinstance(event_id, str):
            return hashlib.sha256(body).hexdigest(), []
        if event.get('type') == SETTLED:
            return event_id, self._settled(config, invoice_id)
        if event.get('type') == INVALID:
            return event_id, [Refunded(reference=invoice_id), SubscriptionEnded(reference=invoice_id)]
        return event_id, []

    def _settled(self, config: dict, invoice_id: str) -> list:
        invoice = self._request('GET', config, f'/invoices/{invoice_id}')
        if invoice.get('status') != 'Settled':
            return []
        metadata = invoice.get('metadata') if isinstance(invoice.get('metadata'), dict) else {}
        user_id, offer_id = _number(metadata.get('user_id')), _number(metadata.get('offer_id'))
        currency = invoice.get('currency') or ''
        try:
            amount_minor = to_minor(invoice.get('amount'), currency)
        except (ValueError, TypeError):
            amount_minor = -1   # recorded, and matches no offer
        events = [Paid(reference=invoice_id, amount_minor=amount_minor, currency=currency, user_id=user_id, offer_id=offer_id)]
        offer = Offer.query.filter_by(id=offer_id).first() if offer_id else None
        if user_id and offer is not None and offer.kind == 'subscription':
            events.append(SubscriptionStarted(reference=invoice_id, user_id=user_id, offer_id=offer_id))
        return events

    def cancel(self, reference: str) -> None:
        """Nothing recurs, so there's nothing to stop."""

    def cancel_at_period_end(self, reference: str):
        raise ProviderError('a prepaid period just runs out')

    def billing_portal_url(self, customer: str, return_url: str) -> str:
        raise ProviderError('no card to manage')

    def describe_price(self, price_id: str) -> dict:
        raise ProviderError('an offer here sets its own price')

    def check_credentials(self, values: dict) -> None:
        self._request('GET', values, '/invoices', params={'take': 1})
