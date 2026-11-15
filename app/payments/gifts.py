"""Gift codes (spec 2026-11-15-paid-membership-design.md PM3, PM6, PM8; plan phase 2, G1-G7): a code gives a free period
of a tier, as a subscription of kind `gift` (adapters/gift_adapter.py), which runs out as a prepaid one does."""
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func

from app import db
from app.models import MemberSubscription, Offer, PromoCode, PromoCodeUse, User, utcnow
from app.payments.core import refresh_access, refresh_donor
from app.utils import get_setting

DEFAULT_GIFT_OFFER_SETTING = 'referral_gift_offer_id'   # the tier a member's new referral code gives (Q1)
DEFAULT_GIFT_DAYS_SETTING = 'referral_gift_days'


def normalise(text: str | None) -> str | None:
    """A code as stored: stripped and lower case, so it can be typed as it was read out."""
    return text.strip().lower() if isinstance(text, str) and text.strip() else None


def usable(code: PromoCode) -> bool:
    """Whether a gift code can be used now (G7): active, a tier and a length, not expired, not used up."""
    if not code.active or code.kind != 'gift' or code.offer_id is None or not code.days or code.days <= 0:
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
    """`user`'s referral code as a code row, which carries the gift joining with it gives (PM7, plan G2): made with the
    site's default gift the first time, its code kept in step with User.referral_code (regenerating keeps the gift). One
    with no gift of its own takes the default whenever one is set.
    None for a member with no code. The caller commits."""
    if not user.referral_code:
        return None
    row = PromoCode.query.filter_by(owner_id=user.id).first()
    if row is None:
        row = PromoCode(owner_id=user.id, kind='gift', code=user.referral_code, created_at=utcnow(),
                        offer_id=get_setting(DEFAULT_GIFT_OFFER_SETTING, None) or None,
                        days=get_setting(DEFAULT_GIFT_DAYS_SETTING, None) or None)
        db.session.add(row)
    else:
        if row.code != user.referral_code:
            row.code = user.referral_code
        if row.offer_id is None:   # no gift of its own: the default, whenever one is set (Q1)
            row.offer_id = get_setting(DEFAULT_GIFT_OFFER_SETTING, None) or None
            row.days = get_setting(DEFAULT_GIFT_DAYS_SETTING, None) or None
    db.session.flush()
    return row


def held_until(user: User, group: str) -> datetime:
    """When what `user` holds now in `group` runs out, or now if nothing: a prepaid or gift period's end, a cancelled
    one's last day, or a renewing one's next billing date."""
    ends = db.session.execute(
        db.select(MemberSubscription.current_period_end, MemberSubscription.ends_at, MemberSubscription.renews_at)
        .join(Offer, MemberSubscription.offer_id == Offer.id)
        .where(MemberSubscription.user_id == user.id, MemberSubscription.status == 'active',
               Offer.offer_group == group)).all()
    return max([utcnow(), *(moment for row in ends for moment in row if moment is not None)])


def start_gift(user: User, code: PromoCode, commit: bool = True) -> MemberSubscription | None:
    """Give `user` the code's free period, starting when what they hold in its tier's group ends (PM8: we never short
    anybody). None if this account has used this code before (G7). The caller has found the code (find_gift_code, or a
    referral's row under referrer_for's lock); this commits unless told not to (inside the account's own commit)."""
    if PromoCodeUse.query.filter_by(promo_code_id=code.id, user_id=user.id).first() is not None:
        return None
    offer = db.session.get(Offer, code.offer_id)
    subscription = MemberSubscription(user_id=user.id, offer_id=offer.id, provider_kind='gift',
                                      provider_reference=f'gift:{uuid.uuid4().hex}', status='active',
                                      current_period_end=held_until(user, offer.offer_group) + timedelta(days=code.days),
                                      created_at=utcnow())
    db.session.add(subscription)
    db.session.flush()
    db.session.add(PromoCodeUse(promo_code_id=code.id, user_id=user.id, subscription_id=subscription.id, used_at=utcnow()))
    db.session.flush()
    refresh_donor(user)
    refresh_access(user)
    if commit:
        db.session.commit()
    return subscription
