"""Gift codes (spec 2026-11-15-paid-membership-design.md PM3, PM6, PM8; plan phase 2, G1-G7): a code gives a free period
of a tier, as a subscription of kind `gift` (adapters/gift_adapter.py), which runs out as a prepaid one does."""
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func

from app import db, limiter
from app.models import MemberSubscription, Offer, PromoCode, PromoCodeUse, User, utcnow
from app.payments.code_text import normalise
from app.payments.core import period_days, refresh_access, refresh_donor
from app.tier_roles import sync_tier_roles
from app.payments.discounts import DISCOUNT_KINDS
from app.payments.holding import held_until
from app.payments.offers import tiers_on
from app.utils import get_setting

# What a new member's referral code gets as its own (plan 2026-11-16 R3, X1): {'kind': AUTO | 'gift' | 'discount',
# 'offer_id', 'periods', 'discount_id'}. A gift's length is a number of its tier's billing periods (owner 2026-11-17)
REFERRAL_BENEFIT_SETTING = 'referral_benefit'
AUTO = 'auto'
OWN_KINDS = ('gift', 'discount')   # what a code stores: never nothing (plan 2026-11-16 X1)
BENEFIT_KINDS = (AUTO, *OWN_KINDS)
# Typing codes, at registration and on /donations alike: one bucket per address, so guessing is slow wherever it is tried
GIFT_CODE_LIMIT, GIFT_CODE_SCOPE = '10 per hour', 'gift_codes'


class GiftUnavailable(Exception):
    """The code could be used when found but not by the time it was: an account made with it is not made at all."""


def checking_codes():
    """The rate limit around trying a code (security review 2026-11-15): shared by every place a code is typed."""
    return limiter.shared_limit(GIFT_CODE_LIMIT, scope=GIFT_CODE_SCOPE)


def usable(code: PromoCode) -> bool:
    """Whether a gift code can be used now (G7): active, a tier and a length, not expired, not used up. A member's
    referral code may take its tier and length from its benefit (R1), so only its own limits are asked of it here."""
    if not code.active or code.kind != 'gift':
        return False
    if code.owner_id is None and (code.offer_id is None or not code.periods or code.periods <= 0):
        return False
    if code.expires_at is not None and code.expires_at <= utcnow():
        return False
    if code.max_uses is not None:
        used = db.session.execute(db.select(func.count()).select_from(PromoCodeUse)
                                  .where(PromoCodeUse.promo_code_id == code.id)).scalar()
        if used >= code.max_uses:
            return False
    return True


def find_gift_code(text: str | None) -> PromoCode | None:
    """The gift code `text` names, if it can be used now; else None, whatever the reason (G7, as RC3). Not a member's
    referral code: that admits only by the referral rules (RC1-RC13), which carry its gift (record_referral). Its row is
    locked until the caller's commit, so two uses at once cannot both slip under max_uses."""
    code = normalise(text)
    if code is None:
        return None
    row = PromoCode.query.filter_by(code=code, kind='gift', owner_id=None).with_for_update().first()
    return row if row is not None and usable(row) else None


def referral_gift_code(user: User) -> PromoCode | None:
    """`user`'s referral code as a code row, which says what joining with it gives (PM7, plan 2026-11-16 R1): made on
    the site's default the first time, its code kept in step with User.referral_code (regenerating keeps its benefit).
    None for a member with no code. The caller commits."""
    if not user.referral_code:
        return None
    row = PromoCode.query.filter_by(owner_id=user.id).first()
    if row is None:
        row = PromoCode(owner_id=user.id, kind='gift', code=user.referral_code, created_at=utcnow())
        db.session.add(row)
    elif row.code != user.referral_code:
        row.code = user.referral_code
    if row.benefit not in OWN_KINDS:   # new, or made before codes stored their own: the default, kept (X1)
        _store(row, _resolve(default_referral_choice(), user.home_domain()))
    db.session.flush()
    return row


def _store(row: PromoCode, benefit) -> None:
    """Keep `benefit` as the row's own. One that can't be had (no tier there, and the default isn't a discount) leaves
    the row without one, which refuses joining where joining is paid for (X2)."""
    if isinstance(benefit, Benefit) and benefit.discount is not None:
        row.benefit, row.offer_id, row.periods, row.discount_code_id = 'discount', None, None, benefit.discount.id
    elif isinstance(benefit, Benefit):
        row.benefit, row.offer_id, row.periods, row.discount_code_id = 'gift', benefit.offer.id, benefit.periods, None
    else:
        row.benefit, row.offer_id, row.periods, row.discount_code_id = None, None, None, None


@dataclass(frozen=True)
class Benefit:
    """What joining with a referral code gives: a free period of a tier, or a discount code (R1)."""
    offer: Offer | None = None
    periods: int | None = None   # how many of the tier's billing periods
    discount: PromoCode | None = None


def referral_benefit(row: PromoCode, domain) -> Benefit | None:
    """The gift or discount a referral code's row gives on its owner's `domain` (X1): its own. One with none of its
    own, or whose tier or discount code is gone, takes the site's default now (and keeps it: a gone choice is never
    taken for nothing, which would admit for free), and a default whose choice is gone is automatic. None only where
    nothing can be given: no tier on the domain, and the default isn't a discount (X2)."""
    if row.benefit in OWN_KINDS:
        own = _benefit_of(row.benefit, row.offer_id, row.periods, row.discount_code_id, domain)
        if own is not _GONE:
            return own
    benefit = _resolve(default_referral_choice(), domain)
    _store(row, benefit)
    return benefit


def default_referral_benefit(domain) -> Benefit | None:
    """What a new referral code on `domain` would get: the default, without making the code."""
    return _resolve(default_referral_choice(), domain)


def _resolve(chosen: dict, domain) -> Benefit | None:
    default = _benefit_of(chosen.get('kind'), chosen.get('offer_id'), chosen.get('periods'), chosen.get('discount_id'),
                          domain)
    return _benefit_of(AUTO, None, None, None, domain) if default is _GONE else default


_GONE = object()   # a choice whose tier or discount code is no more


def _benefit_of(kind, offer_id, periods, discount_id, domain):
    if kind == AUTO:   # one billing period of the cheapest tier there
        tiers = tiers_on(domain)
        return Benefit(offer=tiers[0], periods=1) if tiers else None
    if kind == 'gift':
        offer = db.session.get(Offer, offer_id) if offer_id else None
        if offer is None or offer.kind != 'subscription':
            return _GONE
        return Benefit(offer=offer, periods=periods or 1)
    # 'discount': the kinds there are were asked by the caller (OWN_KINDS, default_referral_choice)
    discount = db.session.get(PromoCode, discount_id) if discount_id else None
    if discount is None or discount.kind not in DISCOUNT_KINDS or discount.owner_id is not None:
        return _GONE
    return Benefit(discount=discount)


def default_referral_choice() -> dict:
    """The site's default (R3, X1): a gift or a discount, or automatic. Automatic until an admin chooses, and for a
    setting that is neither (as 'none' was). A default gift set before R3 was moved here by migration c6f1b4e8a2d5."""
    chosen = get_setting(REFERRAL_BENEFIT_SETTING, None)
    if isinstance(chosen, dict) and chosen.get('kind') in BENEFIT_KINDS:
        return chosen
    return {'kind': AUTO}


def start_gift(user: User, code: PromoCode, commit: bool = True, offer: Offer | None = None,
               periods: int | None = None) -> MemberSubscription | None:
    """Give `user` the code's free period, starting when what they hold in its tier's group ends (PM8: we never short
    anybody). None if this account has used this code before (G7). The caller has found the code (find_gift_code, or a
    referral's row under referrer_for's lock); this commits unless told not to (inside the account's own commit). A
    referral code's gift may be its default's, given as `offer` and `periods` (R3). Its length is that many of the tier's
    billing periods, each what one payment buys (owner 2026-11-17)."""
    # Locked and checked again here, at the moment of use: whatever happened since it was found, max_uses holds
    code = PromoCode.query.filter_by(id=code.id).with_for_update().first()
    if code is None or not usable(code):
        return None
    if PromoCodeUse.query.filter_by(promo_code_id=code.id, user_id=user.id).first() is not None:
        return None
    offer = offer or db.session.get(Offer, code.offer_id)
    days = period_days(offer, periods or code.periods)
    subscription = MemberSubscription(user_id=user.id, offer_id=offer.id, provider_kind='gift',
                                      provider_reference=f'gift:{uuid.uuid4().hex}', status='active',
                                      current_period_end=held_until(user, offer.offer_group) + timedelta(days=days),
                                      created_at=utcnow())
    db.session.add(subscription)
    db.session.flush()
    db.session.add(PromoCodeUse(promo_code_id=code.id, user_id=user.id, subscription_id=subscription.id, used_at=utcnow()))
    db.session.flush()
    refresh_donor(user)
    refresh_access(user)
    sync_tier_roles(user)
    if commit:
        db.session.commit()
    return subscription


def deletable(code: PromoCode | None) -> bool:
    """Whether `code` may be deleted, by an admin or the member it was made for: a gift code an admin made. Not a
    member's referral code, which is their code (they regenerate it), nor a discount code, whose uses hold checkouts
    in progress (D4)."""
    return code is not None and code.kind == 'gift' and code.owner_id is None


def delete_gift_code(code: PromoCode) -> None:
    """Delete a gift code (owner 2026-11-19): it can no longer be used, and leaves every list. Whoever used it keeps the
    period it gave, as a gift subscription names no code; its uses go with it (they cascade). Locked first, so a use
    under way finishes before it goes, or finds it gone. The caller commits."""
    locked = PromoCode.query.filter_by(id=code.id).with_for_update().first()
    if locked is not None:
        db.session.delete(locked)
        db.session.flush()
