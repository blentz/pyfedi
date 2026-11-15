"""What a member holds now in an offer group, and until when (paid membership PM8: we never short anybody). Models
only, so the adapters can ask it without importing core."""
from datetime import datetime

from app import db
from app.models import MemberSubscription, Offer, User, utcnow


def held_until(user: User, group: str) -> datetime:
    """When what `user` holds now in `group` runs out, or now if nothing: a prepaid or gift period's end, a cancelled
    one's last day, or a renewing one's next billing date."""
    ends = db.session.execute(
        db.select(MemberSubscription.current_period_end, MemberSubscription.ends_at, MemberSubscription.renews_at)
        .join(Offer, MemberSubscription.offer_id == Offer.id)
        .where(MemberSubscription.user_id == user.id, MemberSubscription.status == 'active',
               Offer.offer_group == group)).all()
    return max([utcnow(), *(moment for row in ends for moment in row if moment is not None)])


def first_charge_after(user, offer: Offer) -> datetime | None:
    """When a renewing tier bought by `user` should first charge, if not now: when what they hold in its group ends."""
    if user is None or offer.kind != 'subscription':
        return None
    until = held_until(user, offer.offer_group)
    return until if until > utcnow() else None
