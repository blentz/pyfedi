"""What /admin/users says about each local member's subscription, and its filter.

One subscription speaks for a member: one held now (the newest), else one whose payment didn't match its offer, else
the one that ended last. Its key date depends on how it is paid for:

* paid_until: a prepaid period (BTCPay), which ends then unless paid again (current_period_end);
* renews: one that renews by itself (Stripe, PayPal), paid through renews_at; before the provider first said so,
  the newest payment for it stands in (last_paid);
* cancelled: it stops renewing, and runs until ends_at;
* mismatch: its first payment didn't match its offer, which an admin should look at;
* lapsed: it ended (ended_at).
"""
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, exists, func, not_

from app import db
from app.models import MemberSubscription, Offer, Payment, User

FILTERS = ('active', 'lapsed', 'never')
_RANK = {'active': 0, 'mismatch': 1, 'ended': 2}


@dataclass(frozen=True)
class SubscriptionStatus:
    title: str | None   # the offer's; None for one migrated with no offer
    state: str          # paid_until, renews, cancelled, mismatch or lapsed
    date: datetime | None
    since: datetime | None
    last_paid: datetime | None = None


def subscription_statuses(user_ids: list[int]) -> dict[int, SubscriptionStatus]:
    """By user id, for those who have ever held a subscription. Two queries, whatever the number of members."""
    if not user_ids:
        return {}
    rows = db.session.execute(
        db.select(MemberSubscription, Offer.title).outerjoin(Offer, MemberSubscription.offer_id == Offer.id)
        .where(MemberSubscription.user_id.in_(user_ids), MemberSubscription.status.in_(tuple(_RANK)))).all()
    chosen = {}
    for subscription, title in rows:
        key = (_RANK[subscription.status], -_timestamp(subscription.ended_at if subscription.status == 'ended'
                                                       else subscription.created_at))
        if subscription.user_id not in chosen or key < chosen[subscription.user_id][0]:
            chosen[subscription.user_id] = (key, subscription, title)
    undated = {(row.user_id, row.offer_id) for _, row, _ in chosen.values()
               if row.status == 'active' and row.current_period_end is None and row.ends_at is None and row.renews_at is None}
    last_paid = {}
    if undated:
        last_paid = {(user_id, offer_id): when for user_id, offer_id, when in db.session.execute(
            db.select(Payment.user_id, Payment.offer_id, func.max(Payment.created_at))
            .where(Payment.user_id.in_({user_id for user_id, _ in undated}), Payment.status == 'paid')
            .group_by(Payment.user_id, Payment.offer_id))}
    return {user_id: _status(row, title, last_paid) for user_id, (_, row, title) in chosen.items()}


def _timestamp(moment: datetime | None) -> float:
    return moment.timestamp() if moment else 0.0


def _status(row: MemberSubscription, title: str | None, last_paid: dict) -> SubscriptionStatus:
    since = row.created_at
    if row.status == 'ended':
        return SubscriptionStatus(title, 'lapsed', row.ended_at, since)
    if row.status == 'mismatch':
        return SubscriptionStatus(title, 'mismatch', None, since)
    if row.current_period_end is not None:
        return SubscriptionStatus(title, 'paid_until', row.current_period_end, since)
    if row.ends_at is not None:
        return SubscriptionStatus(title, 'cancelled', row.ends_at, since)
    return SubscriptionStatus(title, 'renews', row.renews_at, since,
                              None if row.renews_at else last_paid.get((row.user_id, row.offer_id)))


def subscription_filter(choice: str | None):
    """A clause on User for /admin/users' Subscription filter, or None for any. Lapsed means one ended and none is held
    now; never means a local account that has held none (a remote one couldn't have)."""
    held = exists().where(MemberSubscription.user_id == User.id, MemberSubscription.status == 'active')
    if choice == 'active':
        return held
    if choice == 'lapsed':
        return and_(exists().where(MemberSubscription.user_id == User.id, MemberSubscription.status == 'ended'),
                    not_(held))
    if choice == 'never':
        return and_(User.ap_id.is_(None), not_(exists().where(MemberSubscription.user_id == User.id)))
    return None
