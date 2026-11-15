"""A self-hosted BTCPay Server, through its Greenfield API (M4). Checked against a real server on regtest:
compose.btcpay-regtest.yaml and tests/test_payments_btcpay_regtest.py.

Nothing recurs: a crypto subscription is a period paid for in advance, which the member renews (`prepaid`). There is
nothing to cancel at the provider, and no card to manage. An invoice that is settled is what grants anything, and its
amount is asked of the server rather than read from the webhook, which doesn't carry one.

An invoice is in bitcoin at the day's fixed rate (btc_price), and recorded with what was asked; its settlement is checked
against that record (plan 2026-11-15-btc-daily-price.md, B3, B4).
"""
import hashlib
import hmac
import json
from decimal import Decimal, InvalidOperation

from babel.numbers import get_currency_precision

from app import db
from app.models import BtcpayInvoice, Offer, utcnow
from app.onion import is_onion_request
from app.payments.adapters.base import InvalidWebhook, ProviderError
from app.payments.discounts import discounted
from app.payments.btc_price import btc_text, checkout_rate, sats_for, to_sats
from app.payments.btcpay_client import btcpay_config, btcpay_request, read_rate
from app.payments.events import Paid, Refunded, SubscriptionEnded, SubscriptionStarted
from app.utils import get_setting

INVOICE_MINUTES = 60   # how long the price holds. Slow confirmation after payment is the server's, and is allowed for
SIGNATURE_HEADER = 'BTCPay-Sig'
SETTLED, INVALID = 'InvoiceSettled', 'InvoiceInvalid'
REQUIRED_EVENTS = (SETTLED, INVALID)
REQUIRED_PERMISSIONS = ('btcpay.store.cancreateinvoice', 'btcpay.store.canviewinvoices',
                        'btcpay.store.canviewstoresettings')   # the store's rate, for the daily price


def _number(value) -> int | None:
    return int(value) if isinstance(value, str) and value.isascii() and value.isdigit() else None


def to_minor(amount: str, currency: str) -> int:
    """'5.00' USD as 500. Raises ValueError for what isn't an amount."""
    try:
        return int((Decimal(amount) * 10 ** get_currency_precision(currency.upper())).to_integral_exact())
    except (InvalidOperation, TypeError) as error:
        raise ValueError('not an amount') from error


def _paid_in_full(invoice: dict, sats: int) -> bool:
    """The invoice is in bitcoin for exactly the sats this site asked."""
    try:
        return str(invoice.get('currency') or '').upper() == 'BTC' and to_sats(invoice.get('amount')) == sats
    except ValueError:
        return False


def from_minor(amount_minor: int, currency: str) -> str:
    precision = get_currency_precision(currency.upper())
    return f'{Decimal(amount_minor) / 10 ** precision:.{precision}f}'


class BtcpayAdapter:
    kind = 'btcpay'
    prepaid = True

    def _config(self) -> dict | None:
        return btcpay_config()

    def available(self) -> bool:
        config = self._config()
        return bool(config and config.get('url') and config.get('store_id') and config.get('api_key'))

    def on_onion(self) -> bool:
        """Only with an onion address of its own, so that no redirect names a host to the wrong party (C11)."""
        config = self._config()
        return bool(config and config.get('onion_url'))

    def _request(self, method: str, config: dict, path: str, **kwargs) -> dict | list:
        return btcpay_request(method, config, path, **kwargs)

    def _configured(self) -> dict:
        config = self._config()
        if not config:
            raise ProviderError('BTCPay is not configured')
        return config

    def checkout(self, offer: Offer, user, success_url: str, cancel_url: str, discount=None) -> str:
        """The address is built here because the server names whichever host it was asked on, and this site may reach it
        by another name than a member's browser does."""
        config = self._configured()
        metadata = {'offer_id': str(offer.id)}
        if user:
            metadata['user_id'] = str(user.id)
        price = offer.amount_minor
        if discount is not None:   # the price after a code reserved at checkout (plan phase 3 D6)
            price = discounted(discount, offer)
            if price is None:
                raise ProviderError('that code does not apply')
        # In bitcoin at the day's fixed rate (plan 2026-11-15 B2, B3), and recorded with what was asked
        rate = checkout_rate(utcnow(), offer.currency)
        sats = sats_for(price, offer.currency, rate.rate)
        invoice = self._request('POST', config, '/invoices', json={
            'amount': btc_text(sats), 'currency': 'BTC', 'metadata': metadata,
            'checkout': {'redirectURL': success_url, 'redirectAutomatically': True, 'expirationMinutes': INVOICE_MINUTES}})
        if not isinstance(invoice, dict) or not isinstance(invoice.get('id'), str) or not invoice['id']:
            raise ProviderError('BTCPay made no invoice')
        db.session.add(BtcpayInvoice(invoice_id=invoice['id'], offer_id=offer.id, user_id=user.id if user else None,
                                     expected_minor=price, currency=offer.currency.lower(), sats=sats, rate_day=rate.day,
                                     created_at=utcnow()))
        db.session.commit()
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
        asked = db.session.get(BtcpayInvoice, invoice_id)
        if asked is not None:   # one this site made: what it asked is what counts (B4)
            user_id, offer_id, currency = asked.user_id, asked.offer_id, asked.currency
            amount_minor = asked.expected_minor if _paid_in_full(invoice, asked.sats) else -1
        else:   # made before the daily price: in the site's currency, read as it always was
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
        read_rate(values, get_setting('payment_currency') or 'usd')   # the daily price needs it (B1)
