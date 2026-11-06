"""M6: past the admin's retention period, a settled payment keeps its amounts and loses its account.

A row still doing something for its member keeps the link until it stops: one paying for an active subscription, or one
holding up a Donor label, including a label granted for good. An admin who wants those rows to lapse gives the offer a
length. No retention set means nothing happens (plan, P4).
"""
from datetime import timedelta

from sqlalchemy import exists, func, or_

from app import celery
from app.models import MemberSubscription, Offer, Payment, utcnow
from app.payments.core import end_expired_prepaid
from app.utils import get_setting, get_task_session, patch_db_session


@celery.task
def pseudonymise_settled() -> int:
    session = get_task_session()
    try:
        with patch_db_session(session):
            days = get_setting('payment_retention_days')
            if not days:
                return 0
            now = utcnow()
            subscribed = exists().where(MemberSubscription.user_id == Payment.user_id,
                                        MemberSubscription.offer_id == Payment.offer_id,
                                        MemberSubscription.status == 'active')
            labelled = exists().where(
                Offer.id == Payment.offer_id, Offer.kind == 'donation', Offer.grants_donor.is_(True),
                Payment.status == 'paid',
                or_(Offer.donor_days.is_(None),
                    Payment.created_at + func.make_interval(0, 0, 0, Offer.donor_days) > now))
            cleared = session.query(Payment).filter(Payment.user_id.isnot(None),
                                                    Payment.settled_at < now - timedelta(days=days),
                                                    ~subscribed, ~labelled).update({Payment.user_id: None},
                                                                                   synchronize_session=False)
            session.commit()
            return cleared
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery.task
def end_expired_subscriptions() -> int:
    """Daily. A period paid for in advance runs out with nobody to say so (M4)."""
    session = get_task_session()
    try:
        with patch_db_session(session):
            return end_expired_prepaid()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
