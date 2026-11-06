"""The one place that writes the ledger, subscriptions and the Donor label, from an adapter's neutral events.

A provider's event is applied in one transaction with a record that it was applied, so a repeated webhook does nothing.
A row's reference is unique per provider, so two events about one payment, such as a checkout and its first invoice,
make one row. A provider is told to cancel only after that transaction commits.
"""
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import or_
from sqlalchemy.dialects.postgresql import insert

from app import db
from app.constants import DONOR_WHILE_SUBSCRIBED
from app.email import send_prepaid_reminder, send_verification_email
from app.models import MemberSubscription, Offer, Payment, PaymentEvent, User, utcnow
from app.payments.adapters import adapter_for
from app.payments.events import CustomerKnown, Paid, Refunded, SubscriptionEnded, SubscriptionStarted
from app.payments.tasks import cancel_at_provider
from app.storefront import domain_by_id, paid_registration
from app.utils import finalize_user_setup, get_setting

LEGACY_GROUP = 'legacy'   # the group of a subscription with no offer: one migrated from the environment
_MAX_ID = 2**31 - 1
PERIOD_DAYS = {'month': 30, 'year': 365}   # what one payment buys, where nothing recurs
REMINDER_DAYS = 7


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
        to_cancel, touched, joined = [], set(), []
        mismatched = False
        for event in events:
            if isinstance(event, Paid):
                mismatched = _paid(provider_kind, event, touched) or mismatched
            elif isinstance(event, Refunded):
                _refunded(provider_kind, event, touched)
            elif isinstance(event, SubscriptionStarted):
                to_cancel += _started(provider_kind, event, mismatched, touched, joined)
            elif isinstance(event, SubscriptionEnded):
                _ended(provider_kind, event, touched)
            elif isinstance(event, CustomerKnown):
                _customer_known(event)
        db.session.flush()
        for user in touched:
            refresh_donor(user)
            refresh_access(user)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    for kind, reference in to_cancel:
        cancel_at_provider(kind, reference)
    for user in joined:
        _welcome(user)
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


def _started(kind: str, event: SubscriptionStarted, mismatched: bool, touched: set, joined: list) -> list:
    """Record a subscription and end the others in its group (M7). What the provider must now cancel, as (kind, ref).
    One whose first payment did not match its offer is kept for the admin to see, and neither grants nor replaces. One
    for an account that is gone would go on charging nobody's card for nothing (plan S17); its payment stays on the
    ledger, for the admin to refund. A subscription is what a pending account was waiting for (M10)."""
    user, offer = _row(User, event.user_id), _row(Offer, event.offer_id)
    if user is None:
        return [(kind, event.reference)]
    db.session.execute(insert(MemberSubscription).values(user_id=user.id, offer_id=offer.id if offer else None,
                                                         provider_kind=kind, provider_reference=event.reference,
                                                         status='mismatch' if mismatched else 'active',
                                                         current_period_end=_prepaid_until(kind, user, offer),
                                                         created_at=utcnow())
                       .on_conflict_do_nothing())
    touched.add(user)
    if mismatched:
        return []
    if user.registration_pending_since is not None:
        user.registration_pending_since = None
        joined.append(user)
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


def _prepaid_until(kind: str, user: User, offer: Offer | None) -> datetime | None:
    """When a period paid for in advance runs out, or None for what recurs. Time left on anything in the group carries
    over: renewing early, or moving to another plan, costs nobody what they've paid for (M4, M7)."""
    if not adapter_for(kind).prepaid or offer is None:
        return None
    left = [row.current_period_end for row in active_subscriptions_of(user)
            if row.current_period_end is not None and _group_of(_row(Offer, row.offer_id)) == offer.offer_group]
    return max([utcnow(), *left]) + timedelta(days=PERIOD_DAYS[offer.interval])


def remind_expiring_prepaid() -> int:
    """What the daily task does first. Nothing renews by itself, so a member is told once, while there's time. A mail that
    fails is tried again the next day."""
    now, sent = utcnow(), 0
    due = MemberSubscription.query.filter(MemberSubscription.status == 'active', MemberSubscription.reminded_at.is_(None),
                                          MemberSubscription.current_period_end > now,
                                          MemberSubscription.current_period_end < now + timedelta(days=REMINDER_DAYS)).all()
    for subscription in due:
        user = db.session.get(User, subscription.user_id)
        if user.deleted or not user.email:
            continue
        offer = _row(Offer, subscription.offer_id)
        try:
            send_prepaid_reminder(user, offer.title if offer else '', subscription.current_period_end)
        except Exception as error:
            current_app.logger.warning(f'prepaid reminder failed for account {user.id}: {type(error).__name__}')
            continue
        subscription.reminded_at = now
        sent += 1
    db.session.commit()
    return sent


def end_expired_prepaid() -> int:
    """What the daily task does. The same as a provider saying a subscription has ended."""
    now = utcnow()
    expired = MemberSubscription.query.filter(MemberSubscription.status == 'active',
                                              or_(MemberSubscription.current_period_end < now, MemberSubscription.ends_at < now)).all()
    for subscription in expired:
        subscription.status = 'ended'
        subscription.ended_at = utcnow()
    db.session.flush()
    for user in {db.session.get(User, subscription.user_id) for subscription in expired}:
        refresh_donor(user)
        refresh_access(user)
    db.session.commit()
    return len(expired)


def _ended(kind: str, event: SubscriptionEnded, touched: set) -> None:
    subscription = MemberSubscription.query.filter_by(provider_kind=kind, provider_reference=event.reference).first()
    if subscription is None:
        return
    if subscription.ends_at is not None and subscription.ends_at > utcnow():
        return   # a provider that cancels at once. It has been paid for until then, and the daily task ends it
    subscription.status = 'ended'
    subscription.ended_at = utcnow()
    subscription.ends_at = None
    touched.add(db.session.get(User, subscription.user_id))   # the column is a required foreign key


def _customer_known(event: CustomerKnown) -> None:
    user = _row(User, event.user_id)
    if user:
        user.stripe_customer_id = event.customer


def _welcome(user: User) -> None:
    """The fee is paid. Mail failing leaves an account that can ask for it again at /auth/resend_email: the webhook
    has been applied, and a retry would do nothing."""
    if not get_setting('email_verification', True):
        finalize_user_setup(user)
        return
    try:
        send_verification_email(user)
    except Exception as error:
        current_app.logger.warning(f'paid registration: no verification mail for account {user.id}: {type(error).__name__}')


def cancel_subscription(subscription: MemberSubscription) -> None:
    """The member cancelling. The provider first: if it refuses, nothing here has changed and they are still paying. On
    a paid domain the plan runs out, rather than taking away what they have paid for (plan S22)."""
    user = db.session.get(User, subscription.user_id)
    adapter = adapter_for(subscription.provider_kind)
    if _pays_for_access(user, subscription):
        subscription.ends_at = adapter.cancel_at_period_end(subscription.provider_reference)
    else:
        adapter.cancel(subscription.provider_reference)
        subscription.status = 'ended'
        subscription.ended_at = utcnow()
        db.session.flush()
        refresh_donor(user)
        refresh_access(user)
    db.session.commit()


def _registration_group(user: User) -> str | None:
    """The offer group that pays for this account's domain, if its domain charges."""
    domain = domain_by_id(user.site_domain_id)
    if not paid_registration(domain):
        return None
    offer = db.session.get(Offer, domain.registration_offer_id)
    return offer.offer_group if offer else None


def _pays_for_access(user: User, subscription: MemberSubscription) -> bool:
    group = _registration_group(user)
    return group is not None and _group_of(_row(Offer, subscription.offer_id)) == group


def refresh_access(user: User) -> None:
    """User.access_lapsed_at, from what the member holds in the group that pays for their domain (M12, plan S19). A plan
    change in the group never lapses, and somebody who has held no plan there never does: an account an admin made, or
    one from before the domain charged, has nothing to lapse from."""
    group = _registration_group(user)
    if group is None:
        user.access_lapsed_at = None
        return
    held = [row for row in MemberSubscription.query.filter_by(user_id=user.id)
            if _group_of(_row(Offer, row.offer_id)) == group]
    if any(row.status == 'active' for row in held):
        user.access_lapsed_at = None
    elif any(row.status == 'ended' for row in held) and user.access_lapsed_at is None:
        user.access_lapsed_at = utcnow()


def end_subscriptions_of(user: User) -> None:
    """The account is being deleted: stop charging it, and let its ledger rows keep their amounts without it (M6). The
    provider is told after the commit, with retries, and a provider that is down does not hold up the deletion."""
    subscriptions = active_subscriptions_of(user)
    for subscription in subscriptions:
        subscription.status = 'ended'
        subscription.ended_at = utcnow()
    Payment.query.filter_by(user_id=user.id).update({Payment.user_id: None})
    user.donor_until = None
    db.session.commit()
    for subscription in subscriptions:
        cancel_at_provider(subscription.provider_kind, subscription.provider_reference)


def active_subscriptions_of(user: User) -> list[MemberSubscription]:
    return MemberSubscription.query.filter_by(user_id=user.id, status='active').order_by(MemberSubscription.id).all()


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
