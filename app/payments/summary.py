"""What a checkout costs, shown before paying (plan 2026-11-16-referral-gifts-admins-checkout.md, X4-X6): the offer,
what it covers, the price, any discount and the total, the bitcoin amount at the day's rate, and why a held code that
lapsed no longer takes anything off, with its dates."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func

from app import db
from app.models import BtcpayInvoice, BtcDailyRate, Offer, PromoCode, PromoCodeUse, User, utcnow
from app.payments.adapters import adapter_for
from app.payments.btc_price import btc_text
from app.payments.code_text import normalise
from app.payments.core import PERIOD_DAYS, prepaid_start
from app.payments.discounts import DISCOUNT_KINDS, HOLD, discounted
from app.payments.holding import first_charge_after

BTCPAY = 'btcpay'


@dataclass(frozen=True)
class Lapsed:
    """A held code that no longer takes anything off (X5)."""
    code: str
    reason: str            # expired, used_up, inactive, used_by_you, not_for_this
    when: datetime | None  # the last day, the last use, or this account's use


@dataclass(frozen=True)
class Summary:
    offer: Offer
    price_minor: int | None
    discount: PromoCode | None
    total_minor: int | None
    starts: datetime | None      # a prepaid period's start, or a renewing one's first charge
    ends: datetime | None        # a prepaid period's end
    btc: str | None = None
    rate: Decimal | None = None
    rate_day: object = None
    lapsed: Lapsed | None = None

    @property
    def off_minor(self) -> int | None:
        return None if self.discount is None or self.price_minor is None else self.price_minor - self.total_minor


def held_code_row(text: str | None) -> PromoCode | None:
    """The discount code `text` names, whether or not it can be used now: to say why it can't."""
    code = normalise(text)
    if code is None:
        return None
    return PromoCode.query.filter(PromoCode.code == code, PromoCode.kind.in_(DISCOUNT_KINDS),
                                  PromoCode.owner_id.is_(None)).first()


def lapse_of(code: PromoCode | None, user: User | None, offer: Offer) -> Lapsed | None:
    """Why `code` takes nothing off `offer` for `user` now, with its date; None if it would."""
    if code is None:
        return None
    now = utcnow()
    if not code.active:
        return Lapsed(code.code, 'inactive', None)
    if code.expires_at is not None and code.expires_at <= now:
        return Lapsed(code.code, 'expired', code.expires_at)
    if user is not None:
        paid = PromoCodeUse.query.filter(PromoCodeUse.promo_code_id == code.id, PromoCodeUse.user_id == user.id,
                                         PromoCodeUse.payment_id.isnot(None)).first()
        if paid is not None:
            return Lapsed(code.code, 'used_by_you', paid.used_at)
    if code.max_uses is not None:
        counted = PromoCodeUse.query.filter(PromoCodeUse.promo_code_id == code.id,
                                            (PromoCodeUse.payment_id.isnot(None)) | (PromoCodeUse.used_at > now - HOLD))
        if user is not None:
            counted = counted.filter(PromoCodeUse.user_id != user.id)   # their own hold is theirs to use
        if counted.count() >= code.max_uses:
            last = db.session.query(func.max(PromoCodeUse.used_at)).filter(PromoCodeUse.promo_code_id == code.id).scalar()
            return Lapsed(code.code, 'used_up', last)
    if discounted(code, offer) is None:
        return Lapsed(code.code, 'not_for_this', None)
    return None


def summarise(offer: Offer, user: User | None, discount: PromoCode | None, pay_url: str,
              lapsed: Lapsed | None = None) -> Summary:
    """The summary of a checkout just made at `pay_url` (X4)."""
    price = offer.amount_minor
    total = discounted(discount, offer) if discount is not None and price is not None else price
    starts = ends = None
    if offer.kind == 'subscription' and offer.interval in PERIOD_DAYS:
        if adapter_for(offer.provider_kind).prepaid:
            starts = prepaid_start(user, offer)
            ends = starts + timedelta(days=PERIOD_DAYS[offer.interval])
        else:
            starts = first_charge_after(user, offer) or utcnow()
    btc = rate = rate_day = None
    if offer.provider_kind == BTCPAY:
        invoice = db.session.get(BtcpayInvoice, pay_url.rstrip('/').rsplit('/', 1)[-1])
        if invoice is not None:
            row = db.session.get(BtcDailyRate, (invoice.rate_day, invoice.currency))
            btc, rate_day = btc_text(invoice.sats), invoice.rate_day
            rate = row.rate if row is not None else None
    return Summary(offer=offer, price_minor=price, discount=discount, total_minor=total, starts=starts, ends=ends,
                   btc=btc, rate=rate, rate_day=rate_day, lapsed=lapsed)

