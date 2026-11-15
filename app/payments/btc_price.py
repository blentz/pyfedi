"""Bitcoin's price, fixed once a day at midnight UTC (plan 2026-11-15-btc-daily-price.md, B1, B2, B5).

Day D's rate holds from just after D 00:00 UTC until D+1 00:00 UTC inclusive, so a checkout at exactly midnight takes
yesterday's. An hourly job fixes today's rate as soon as it can be read; until then, yesterday's holds.
"""
from datetime import datetime, time
from decimal import ROUND_FLOOR, Decimal, InvalidOperation

from babel.numbers import get_currency_precision
from flask import current_app
from sqlalchemy.dialects.postgresql import insert

from app import db
from app.models import BtcDailyRate, utcnow
from app.payments.errors import ProviderError
from app.payments.btcpay_client import btcpay_config, read_rate
from app.utils import get_setting

SATS = 100_000_000


def sats_for(price_minor: int, currency: str, rate: Decimal) -> int:
    """A price in a currency's minor unit, in sats at `rate`, rounded down: the member's way."""
    price = Decimal(price_minor) / 10 ** get_currency_precision(currency.upper())
    return int((price * SATS / rate).to_integral_value(rounding=ROUND_FLOOR))


def btc_text(sats: int) -> str:
    return f'{Decimal(sats) / SATS:.8f}'


def to_sats(text) -> int:
    """'0.00013320' as 13320. Raises ValueError for what is not a whole, positive number of sats."""
    try:
        sats = Decimal(str(text)) * SATS
    except (InvalidOperation, TypeError):
        raise ValueError('not an amount') from None
    if text is None or not sats.is_finite() or sats <= 0 or sats != sats.to_integral_value():
        raise ValueError('not a whole number of sats')
    return int(sats)


def rate_on(moment: datetime, currency: str) -> BtcDailyRate | None:
    """The rate a checkout at `moment` takes (B2): the latest day whose midnight is strictly before it."""
    rows = BtcDailyRate.query.filter(BtcDailyRate.currency == currency.lower(), BtcDailyRate.day <= moment.date()) \
        .order_by(BtcDailyRate.day.desc()).limit(2).all()
    return next((row for row in rows if datetime.combine(row.day, time()) < moment), None)


def fix_today(now: datetime | None = None, currency: str | None = None) -> BtcDailyRate | None:
    """Fix today's rate, once (B1). None if BTCPay isn't set up or can't say, in which case yesterday's holds."""
    now = now or utcnow()
    currency = (currency or _site_currency()).lower()
    existing = db.session.get(BtcDailyRate, (now.date(), currency))
    if existing is not None:
        return existing
    config = btcpay_config()
    if not config:
        return None
    try:
        rate = read_rate(config, currency)
    except ProviderError as error:
        current_app.logger.warning(f"BTCPay: no rate for {currency} today: {error}")
        return None
    db.session.execute(insert(BtcDailyRate).values(day=now.date(), currency=currency, rate=rate, fixed_at=utcnow())
                       .on_conflict_do_nothing())   # another worker fixed it first: theirs stands
    db.session.commit()
    return db.session.get(BtcDailyRate, (now.date(), currency))


def checkout_rate(now: datetime, currency: str) -> BtcDailyRate:
    """The rate for a checkout now; a site that has never fixed one fixes today's at once. ProviderError if there is
    none to be had, which the checkout shows as the provider being unavailable."""
    row = rate_on(now, currency) or fix_today(now, currency)
    if row is None:
        raise ProviderError('no bitcoin price fixed yet')
    return row


def todays_btc_price(price_minor: int | None, currency: str | None) -> str | None:
    """What /donations shows beside a BTCPay offer's price (B5), from the fixed rate only: showing a page reads nothing."""
    if price_minor is None or not currency:
        return None
    row = rate_on(utcnow(), currency)
    return btc_text(sats_for(price_minor, currency, row.rate)) if row else None


def _site_currency() -> str:
    return get_setting('payment_currency') or 'usd'
