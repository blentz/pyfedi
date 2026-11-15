"""Discount codes (spec 2026-11-15-paid-membership-design.md PM6, Q2, Q3; plan phase 3, D1-D8): percent or an amount
off a first payment. A use is reserved at checkout with the price expected, so the payment at that price is no
mismatch (core._paid) and max_uses holds; a reservation left a day unpaid frees its use."""
from datetime import timedelta

from sqlalchemy import func, or_

from app import db
from app.models import Offer, PromoCode, PromoCodeUse, User, utcnow
from app.payments.code_text import normalise

DISCOUNT_KINDS = ('percent', 'amount')
HOLD = timedelta(days=1)   # how long an unpaid checkout keeps its use
NO_CODES = ('paypal', 'link')   # PayPal has no coupons for subscriptions (Q3); a link-out takes nothing here


def _uses(code: PromoCode) -> int:
    """Uses that count: paid, or reserved within the hold."""
    return db.session.execute(db.select(func.count()).select_from(PromoCodeUse).where(
        PromoCodeUse.promo_code_id == code.id,
        or_(PromoCodeUse.payment_id.isnot(None), PromoCodeUse.used_at > utcnow() - HOLD))).scalar()


def usable(code: PromoCode) -> bool:
    if not code.active or code.kind not in DISCOUNT_KINDS or code.owner_id is not None:
        return False
    if code.expires_at is not None and code.expires_at <= utcnow():
        return False
    return code.max_uses is None or _uses(code) < code.max_uses


def find_discount_code(text: str | None, lock: bool = False) -> PromoCode | None:
    """The discount code `text` names, if it can be used now; else None, whatever the reason (D8)."""
    code = normalise(text)
    if code is None:
        return None
    query = PromoCode.query.filter(PromoCode.code == code, PromoCode.kind.in_(DISCOUNT_KINDS), PromoCode.owner_id.is_(None))
    row = (query.with_for_update() if lock else query).first()
    return row if row is not None and usable(row) else None


def discounted(code: PromoCode, offer: Offer) -> int | None:
    """The offer's price after `code`, in its minor unit; None where the code does not apply (D2, D6): another offer,
    PayPal or a link-out, no price, or nothing left to pay (free is what gifts are for). Rounded down: the member's way."""
    if code.kind not in DISCOUNT_KINDS or offer.provider_kind in NO_CODES or offer.amount_minor is None:
        return None
    if code.offer_id is not None and code.offer_id != offer.id:
        return None
    if code.kind == 'percent':
        if not code.percent or not 0 < code.percent < 100:
            return None
        price = offer.amount_minor * (100 - code.percent) // 100
    else:
        if not code.amount_minor or code.amount_minor <= 0:
            return None
        price = offer.amount_minor - code.amount_minor
    return price if 0 < price < offer.amount_minor else None


def reserve(user: User, code: PromoCode, offer: Offer) -> PromoCodeUse | None:
    """Hold `code`'s use for `user`'s checkout of `offer` at its discounted price (D4). The code's row is locked and its
    limits checked again; one account uses a code once, and reserving again before paying moves the same hold. None if
    it cannot be used, does not apply, or this account has paid with it. Commits."""
    price = discounted(code, offer)
    locked = PromoCode.query.filter_by(id=code.id).with_for_update().first()
    use = PromoCodeUse.query.filter_by(promo_code_id=code.id, user_id=user.id).first()
    if price is None or locked is None or (use is not None and use.payment_id is not None):
        db.session.rollback()
        return None
    if use is None and not usable(locked):
        db.session.rollback()
        return None
    if use is None:
        use = PromoCodeUse(promo_code_id=code.id, user_id=user.id)
        db.session.add(use)
    use.offer_id, use.expected_minor, use.used_at = offer.id, price, utcnow()
    db.session.commit()
    return use


def settle(user_id: int | None, offer: Offer | None, amount_minor: int, payment_id: int) -> bool:
    """Whether a payment of `amount_minor` for `offer` was a reserved discount (D5): if so, its use is marked paid."""
    if user_id is None or offer is None:
        return False
    use = PromoCodeUse.query.filter_by(user_id=user_id, offer_id=offer.id, expected_minor=amount_minor,
                                       payment_id=None).first()
    if use is None:
        return False
    use.payment_id = payment_id
    return True
