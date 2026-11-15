"""What a member holds now in an offer group, and until when (paid membership PM8: we never short anybody). Models
only, so the adapters can ask it without importing core."""
from datetime import datetime, timedelta

from app import db
from app.models import MemberSubscription, Offer, Payment, User, utcnow

SAME_CHECKOUT = timedelta(hours=1)   # a first payment recorded just before its subscription row


def held_until(user: User, group: str) -> datetime:
    """When what `user` holds now in `group` runs out, or now if nothing. Only time paid for or given counts: a prepaid or
    gift period's end, and a renewing one's next billing date or last day once a payment for it has settled. A renewing
    one still in its free start (a deferred first charge, PM8) counts for nothing, or each new deferral could stand on
    the last and nothing would ever be paid (security review 2026-11-15)."""
    rows = db.session.execute(
        db.select(MemberSubscription).join(Offer, MemberSubscription.offer_id == Offer.id)
        .where(MemberSubscription.user_id == user.id, MemberSubscription.status == 'active',
               Offer.offer_group == group)).scalars().all()
    ends = []
    for row in rows:
        if row.current_period_end is not None:
            ends.append(row.current_period_end)
        elif _paid_for(row):
            ends += [moment for moment in (row.renews_at, row.ends_at) if moment is not None]
    return max([utcnow(), *ends])


def _paid_for(row: MemberSubscription) -> bool:
    """Whether a payment for this renewing subscription has settled: one for its offer from the same provider, made
    since it began (its first payment is recorded with it, so a little before counts too)."""
    return db.session.execute(db.select(Payment.id).where(
        Payment.user_id == row.user_id, Payment.offer_id == row.offer_id, Payment.provider_kind == row.provider_kind,
        Payment.status == 'paid', Payment.created_at >= row.created_at - SAME_CHECKOUT)).first() is not None


def first_charge_after(user, offer: Offer) -> datetime | None:
    """When a renewing tier bought by `user` should first charge, if not now: when what they hold in its group ends."""
    if user is None or offer.kind != 'subscription':
        return None
    until = held_until(user, offer.offer_group)
    return until if until > utcnow() else None
