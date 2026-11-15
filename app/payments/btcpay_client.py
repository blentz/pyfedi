"""The BTCPay Server connection: its settings, and a call to its Greenfield API for the configured store. Shared by the
adapter and by the daily price (btc_price), which the adapter itself needs, so it sits apart from both."""
from decimal import Decimal, InvalidOperation

import httpx

from app.models import PaymentProvider
from app.payments.errors import ProviderError
from app.payments.credentials import CredentialsUnavailable, unseal

KIND = 'btcpay'
TIMEOUT_SECONDS = 20


def btcpay_config() -> dict | None:
    """The address and store, which aren't secret, with the key and signing secret, which are sealed."""
    row = PaymentProvider.query.filter_by(kind=KIND).first()
    if row is None or not row.enabled or not row.credentials:
        return None
    try:
        return {**(row.settings or {}), **unseal(row.credentials)}
    except CredentialsUnavailable:
        return None   # C4: no valid key means this is off, and the site carries on


def btcpay_request(method: str, config: dict, path: str, **kwargs) -> dict | list:
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


def read_rate(config: dict, currency: str) -> Decimal:
    """What one bitcoin costs in `currency` now, from the store's own rate source (plan 2026-11-15 B1). Needs the key
    permission btcpay.store.canviewstoresettings."""
    pair = f'BTC_{currency.upper()}'
    answer = btcpay_request('GET', config, '/rates', params={'currencyPair': pair})
    found = next((entry for entry in answer if isinstance(entry, dict) and entry.get('currencyPair') == pair), None) \
        if isinstance(answer, list) else None
    if found is None or found.get('errors'):
        raise ProviderError(f'BTCPay has no rate for {pair}')
    try:
        rate = Decimal(str(found.get('rate')))
    except InvalidOperation:
        raise ProviderError(f'BTCPay gave no usable rate for {pair}') from None
    if not rate.is_finite() or rate <= 0:
        raise ProviderError(f'BTCPay gave no usable rate for {pair}')
    return rate
