"""A paid domain's first fee (M10). The account is made pending and can't log in, and only a verified webhook activates it.

No mail goes out until the fee is paid, so an unpaid signup can't be used to send mail to a stranger (plan S13). Somebody
who abandons the provider's page comes back by logging in: the right password leads to a new checkout, with no session
(S14). Whoever doesn't pay is removed by `remove_unpaid_registrations` (S16).
"""
from datetime import timedelta

from flask import redirect, url_for
from flask_babel import _

from app import celery
from app.models import MemberSubscription, Offer, User, utcnow
from app.payments.adapters import adapter_for
from app.payments.adapters.base import ProviderError
from app.payments.offers import offers_here
from app.payments.tasks import cancel_at_provider
from app.storefront import current_storefront, paid_registration
from app.utils import get_task_session, patch_db_session, render_template

UNPAID_ACCOUNT_HOURS = 48   # a checkout lasts less (Adapter.checkout), so nobody can pay for an account that is gone


def registration_offer() -> Offer | None:
    """What signing up here pays for, while it can be paid for. None on any other kind of domain."""
    storefront = current_storefront()
    if not paid_registration(storefront):
        return None
    return next((offer for offer in offers_here() if offer.id == storefront.registration_offer_id), None)


def registration_checkout(user: User):
    """To the provider's page, for a pending account. What registering and logging in both lead to."""
    offer = registration_offer()
    try:
        if offer is None:
            raise ProviderError('nothing to pay for')
        return redirect(adapter_for(offer.provider_kind).checkout(
            offer, user, url_for('payments.result', result='success', _external=True),
            url_for('payments.result', result='failure', _external=True)))
    except ProviderError:
        return render_template('generic_message.html', title=_('Create new account'),
                               message=_('The payment provider is unavailable at the moment. Your account is saved: log in '
                                         'within two days to try again.')), 503


@celery.task
def remove_unpaid_registrations() -> int:
    """Daily. An account that never paid has never logged in, so nothing refers to it but its own subscription row, kept
    if the first payment didn't match its offer. That one is cancelled, or the provider would go on charging for it."""
    session = get_task_session()
    try:
        with patch_db_session(session):
            unpaid = session.query(User).filter(
                User.registration_pending_since < utcnow() - timedelta(hours=UNPAID_ACCOUNT_HOURS)).all()
            to_cancel = [(row.provider_kind, row.provider_reference) for row in session.query(MemberSubscription).filter(
                MemberSubscription.user_id.in_([user.id for user in unpaid]), MemberSubscription.status != 'ended')]
            for user in unpaid:
                session.delete(user)
            session.commit()
            for kind, reference in to_cancel:
                cancel_at_provider(kind, reference)
            return len(unpaid)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
