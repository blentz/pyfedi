"""A paid domain's first fee (M10). The account is made pending and can't log in, and only a verified webhook activates it.

No mail goes out until the fee is paid, so an unpaid signup can't be used to send mail to a stranger (plan S13). Somebody
who abandons the provider's page comes back by logging in: the right password leads to a new checkout, with no session
(S14). Whoever doesn't pay is removed by `remove_unpaid_registrations` (S16).
"""
from datetime import timedelta

from flask import redirect, url_for
from flask_babel import _

from app import celery, db
from app.models import MemberSubscription, Offer, PromoCode, User, utcnow
from app.payments.adapters import adapter_for
from app.payments.adapters.base import ProviderError
from app.payments.discounts import reserve, usable
from app.payments.offers import offers_here
from app.payments.summary import Lapsed, lapse_of, summarise
from app.payments.tasks import cancel_at_provider
from app.storefront import current_storefront, paid_registration
from app.utils import get_task_session, patch_db_session, render_template

UNPAID_ACCOUNT_HOURS = 48   # a checkout lasts less (Adapter.checkout), so nobody can pay for an account that is gone


def registration_tiers() -> list[Offer]:
    """What signing up here may pay for, while it can be paid for: every subscription on offer here in the group of the
    domain's own (paid membership plan M3). None on any other kind of domain, or while its own isn't on offer."""
    storefront = current_storefront()
    if not paid_registration(storefront):
        return []
    offers = offers_here()
    own = next((offer for offer in offers if offer.id == storefront.registration_offer_id), None)
    if own is None:
        return []
    return [offer for offer in offers if offer.kind == 'subscription' and offer.offer_group == own.offer_group]


def joinable_tier_ids(site) -> frozenset[int]:
    """The offers a visitor here is asked to join for, not to log in for (plan M6): the tiers, while registering is open."""
    if site is None or site.registration_mode == 'Closed':
        return frozenset()
    return frozenset(offer.id for offer in registration_tiers())


def registration_offer(chosen: int | None = None) -> Offer | None:
    """The tier `chosen`, if it is one here; else the domain's own. None where signing up can't be paid for."""
    tiers = registration_tiers()
    return next((offer for offer in tiers if offer.id == chosen), None) \
        or next((offer for offer in tiers if offer.id == current_storefront().registration_offer_id), None)


def registration_checkout(user: User):
    """To the provider's page, for a pending account, for the tier it chose. What registering and logging in both lead
    to."""
    offer = registration_offer(user.pending_offer_id)
    try:
        if offer is None:
            raise ProviderError('nothing to pay for')
        # The code given at /register, reserved for this checkout; one that can no longer be used is told, with why
        # and its date, and the full price applies (D1; plan 2026-11-16 X5)
        code = db.session.get(PromoCode, user.pending_promo_code_id) if user.pending_promo_code_id else None
        discount = code if code is not None and usable(code) and reserve(user, code, offer) is not None else None
        lapsed = lapse_of(code, user, offer) or Lapsed(code.code, 'not_for_this', None) \
            if code is not None and discount is None else None
        url = adapter_for(offer.provider_kind).checkout(
            offer, user, url_for('payments.result', result='success', _external=True),
            url_for('payments.result', result='failure', _external=True), discount=discount)
        if lapsed is not None:
            user.pending_promo_code_id = None   # held no longer
            db.session.commit()
        return render_template('payments/summary.html', title=_('Before you pay'),
                               summary=summarise(offer, user, discount, url, lapsed), pay_url=url, back_url=None)
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
