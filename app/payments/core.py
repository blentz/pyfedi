"""The one place that writes the ledger, subscriptions and the Donor label, from an adapter's neutral events.

A provider's event is applied in one transaction with a record that it was applied, so a repeated webhook does nothing.
A row's reference is unique per provider, so two events about one payment, such as a checkout and its first invoice,
make one row. A provider is told to cancel only after that transaction commits.
"""
from datetime import timedelta

from sqlalchemy.dialects.postgresql import insert

from app import db
from app.constants import DONOR_WHILE_SUBSCRIBED
from app.models import MemberSubscription, Offer, Payment, PaymentEvent, User, utcnow
from app.payments.adapters import adapter_for
from app.payments.events import CustomerKnown, Paid, Refunded, SubscriptionEnded, SubscriptionStarted
from app.payments.tasks import cancel_at_provider

LEGACY_GROUP = 'legacy'   # the group of a subscription with no offer: one migrated from the environment
_MAX_ID = 2**31 - 1


def _row(model, row_id):
    """The row, or None for an id that names nothing. An id comes off the wire, and Postgres refuses one out of range."""
    if not isinstance(row_id, int) or isinstance(row_id, bool) or not 0 < row_id <= _MAX_ID:
        return None
    return db.session.get(model, row_id)


def _matches(offer: Offer | None, event: Paid) -> bool:
    if offer is None or offer.amount_minor is None:
        return True
    return event.amount_minor == offer.amount_minor and event.currency.lower() == (offer.currency or '').lower()


def _group_of(offer: Offer | None) -> str:
    return offer.offer_group if offer else LEGACY_GROUP


def apply_event(provider_kind: str, event_id: str, events: list) -> bool:
    """Apply one provider event's neutral events. False if this event was applied before."""
    try:
        applied = db.session.execute(insert(PaymentEvent).values(provider_kind=provider_kind, event_id=event_id,
                                                                 received_at=utcnow())
                                     .on_conflict_do_nothing().returning(PaymentEvent.id)).first()
        if applied is None:
            db.session.rollback()
            return False
        to_cancel, touched = [], set()
        mismatched = False
        for event in events:
            if isinstance(event, Paid):
                mismatched = _paid(provider_kind, event, touched) or mismatched
            elif isinstance(event, Refunded):
                _refunded(provider_kind, event, touched)
            elif isinstance(event, SubscriptionStarted):
                to_cancel += _started(provider_kind, event, mismatched, touched)
            elif isinstance(event, SubscriptionEnded):
                _ended(provider_kind, event, touched)
            elif isinstance(event, CustomerKnown):
                _customer_known(event)
        db.session.flush()
        for user in touched:
            refresh_donor(user)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    for kind, reference in to_cancel:
        cancel_at_provider(kind, reference)
    return True


def _paid(kind: str, event: Paid, touched: set) -> bool:
    """Record a payment. True if it does not match its offer, in which case it grants nothing."""
    user, offer = _row(User, event.user_id), _row(Offer, event.offer_id)
    matches = _matches(offer, event)
    now = utcnow()
    db.session.execute(insert(Payment).values(user_id=user.id if user else None, offer_id=offer.id if offer else None,
                                              provider_kind=kind, provider_reference=event.reference,
                                              amount_minor=event.amount_minor, currency=event.currency.lower(),
                                              status='paid' if matches else 'mismatch', created_at=now, settled_at=now)
                       .on_conflict_do_nothing())
    if user:
        touched.add(user)
    return not matches


def _refunded(kind: str, event: Refunded, touched: set) -> None:
    payment = Payment.query.filter_by(provider_kind=kind, provider_reference=event.reference).first()
    if payment is None:
        return
    payment.status = 'refunded'
    payment.settled_at = utcnow()
    user = db.session.get(User, payment.user_id) if payment.user_id else None
    if user:
        touched.add(user)


def _started(kind: str, event: SubscriptionStarted, mismatched: bool, touched: set) -> list:
    """Record a subscription and end the others in its group (M7). What the provider must now cancel, as (kind, ref).
    One whose first payment did not match its offer is kept for the admin to see, and neither grants nor replaces."""
    user, offer = _row(User, event.user_id), _row(Offer, event.offer_id)
    if user is None:
        return []
    db.session.execute(insert(MemberSubscription).values(user_id=user.id, offer_id=offer.id if offer else None,
                                                         provider_kind=kind, provider_reference=event.reference,
                                                         status='mismatch' if mismatched else 'active',
                                                         created_at=utcnow())
                       .on_conflict_do_nothing())
    touched.add(user)
    if mismatched:
        return []
    replaced = []
    group = _group_of(offer)
    for other in MemberSubscription.query.filter(MemberSubscription.user_id == user.id,
                                                 MemberSubscription.status == 'active',
                                                 MemberSubscription.provider_reference != event.reference):
        if _group_of(_row(Offer, other.offer_id)) == group:
            other.status = 'ended'
            other.ended_at = utcnow()
            replaced.append((other.provider_kind, other.provider_reference))
    return replaced


def _ended(kind: str, event: SubscriptionEnded, touched: set) -> None:
    subscription = MemberSubscription.query.filter_by(provider_kind=kind, provider_reference=event.reference).first()
    if subscription is None:
        return
    subscription.status = 'ended'
    subscription.ended_at = utcnow()
    user = db.session.get(User, subscription.user_id)
    if user:
        touched.add(user)


def _customer_known(event: CustomerKnown) -> None:
    user = _row(User, event.user_id)
    if user:
        user.stripe_customer_id = event.customer


def cancel_subscription(subscription: MemberSubscription) -> None:
    """The member cancelling. The provider first: if it refuses, nothing here has changed and they are still paying."""
    adapter_for(subscription.provider_kind).cancel(subscription.provider_reference)
    subscription.status = 'ended'
    subscription.ended_at = utcnow()
    db.session.flush()
    refresh_donor(db.session.get(User, subscription.user_id))
    db.session.commit()


def refresh_donor(user: User) -> None:
    """User.donor_until from what the member holds (M9, plan P1)."""
    subscriptions = db.session.execute(
        db.select(Offer.grants_donor).select_from(MemberSubscription).outerjoin(Offer, MemberSubscription.offer_id == Offer.id)
        .where(MemberSubscription.user_id == user.id, MemberSubscription.status == 'active')).all()
    # no offer: migrated from the environment, where every plan granted it
    if any(grants is None or grants for grants, in subscriptions):
        user.donor_until = DONOR_WHILE_SUBSCRIBED
        return
    until = None
    payments = db.session.execute(
        db.select(Payment.created_at, Offer.donor_days).join(Offer, Payment.offer_id == Offer.id)
        .where(Payment.user_id == user.id, Payment.status == 'paid', Offer.kind == 'donation',
               Offer.grants_donor.is_(True))).all()
    for created_at, days in payments:
        ends = DONOR_WHILE_SUBSCRIBED if days is None else created_at + timedelta(days=days)
        until = ends if until is None or ends > until else until
    user.donor_until = until


def holds_offer(user: User, offer: Offer) -> bool:
    """What a later perk asks. A subscription is held while active, a one-off once paid and not refunded."""
    if offer.kind == 'subscription':
        return MemberSubscription.query.filter_by(user_id=user.id, offer_id=offer.id, status='active').first() is not None
    return Payment.query.filter_by(user_id=user.id, offer_id=offer.id, status='paid').first() is not None
