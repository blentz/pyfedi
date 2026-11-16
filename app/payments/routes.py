"""Provider webhooks, the donation page, and what a member's forms post to: paying, stopping paying, codes.

A browser coming back from a provider grants nothing: only a verified webhook does (spec, Security).
"""
from flask import abort, flash, g, redirect, request, session, url_for
from babel.numbers import get_currency_name
from flask_babel import _, get_locale
from flask_login import current_user
from flask_wtf import FlaskForm

from app import db, limiter
from app.cms import plans_page_url
from app.complimentary import KIND as COMPLIMENTARY, complimentary_of
from app.models import MemberSubscription, Offer, Payment
from app.onion import clearnet_only, is_onion_request
from app.payments import bp
from app.payments.adapters import adapter_for
from app.payments.adapters.base import InvalidWebhook, ProviderError
from app.payments.core import active_subscriptions_of, apply_event, cancel_subscription
from app.payments.discounts import HELD_CODE, find_discount_code, reserve
from app.payments.forms import RedeemCodeForm
from app.payments.gifts import checking_codes, find_gift_code, start_gift
from app.payments.offers import LINK, is_tier, membership_groups, offers_here
from app.payments.registration import joinable_tier_ids
from app.storefront import current_storefront, paid_registration
from app.utils import get_setting, login_required, login_required_if_private_instance, render_template, \
    safe_redirect_target

HISTORY_ROWS = 100
NOT_CANCELLED = ('gift', COMPLIMENTARY)   # by the member: neither is a payment (B6, K6)


@bp.route('/payments/webhook/<kind>', methods=['POST'])
def payment_webhook(kind):
    adapter = adapter_for(kind)
    if adapter is None or not adapter.available():
        abort(404)
    try:
        event_id, events = adapter.parse_webhook(request.get_data(), request.headers)
    except InvalidWebhook:
        return 'invalid webhook', 400
    except ProviderError:
        return 'try again', 503   # reading the event needed the provider: a provider's webhook is retried
    apply_event(kind, event_id, events)
    return 'ok'


@bp.route('/stripe_webhook', methods=['POST'])
def stripe_webhook():
    """What Stripe's dashboard has configured on every existing site (C7)."""
    return payment_webhook('stripe')


@bp.route('/donations')
@login_required_if_private_instance
@limiter.limit('100 per 10 minutes')   # which makes it session first on the onion: checkout is a POST, and can't be sent round
def donations():
    """Upstream's donation page (plan 2026-11-16 DM2): the donations on offer here and a donor's regular donations, to
    give or to stop. Membership is not here: tiers, codes and their status are on the membership page ([membership]).
    Not /donate: that is a CMS page an admin may delete, and a donor must still be able to cancel. The seeded /donate
    page links here on every site, so with nothing to offer this says so on the page itself: a logged-out visitor has
    no session cookie, so a flash would not survive a redirect."""
    groups = membership_groups()
    offers = [offer for offer in offers_here() if not is_tier(offer, groups)]
    onion = is_onion_request()
    member = current_user.is_authenticated and not onion   # the card stays off the onion
    # A prepaid period has nothing to manage at a provider, so it can be shown there. The rest couldn't be acted on.
    subscriptions = [row for row in active_subscriptions_of(current_user)
                     if row.provider_kind not in NOT_CANCELLED and not is_tier(_offer(row.offer_id), groups)
                     and (not onion or row.current_period_end)] if current_user.is_authenticated else []
    if not offers and not subscriptions:
        return render_template('generic_message.html', title=_('Donation plans'),
                               message=_('Donation plans are not available on this site.')), 404
    prepaid = {offer.id for offer in offers if offer.provider_kind != LINK and adapter_for(offer.provider_kind).prepaid}
    held = {subscription.offer_id for subscription in subscriptions} - prepaid   # one prepaid is renewed by paying again
    complimentary = complimentary_of(current_user) is not None
    # Choosing one held would start a second and cancel the first; complimentary membership pays nothing (K5)
    plans = [] if complimentary else [offer for offer in offers if offer.id not in held]
    currency = get_setting('payment_currency') or next((offer.currency for offer in offers if offer.currency), None)
    return render_template('user/choose_plan.html', title=_('Choose a donation plan'), plans=plans,
                           subscriptions=subscriptions, titles={offer.id: offer.title for offer in Offer.query.all()},
                           member=member, prepaid=prepaid, complimentary=complimentary,
                           currency_name=get_currency_name(currency.upper(), count=2, locale=str(get_locale() or 'en'))
                           if currency else None)


@bp.route('/user/settings/subscription')
@login_required
def subscription():
    """Settings -> Subscription (plan 2026-11-16 DM3): the member's membership (tiers, a gift, complimentary
    membership; regular donations are /donations'), cancelling, a held code, the code box and their payments. Where a
    lapsed member is sent, so the login gate lets them in."""
    onion = is_onion_request()
    member = not onion   # cancelling, the card and the history stay off the onion
    groups = membership_groups()
    offers = {offer.id: offer for offer in Offer.query.all()}
    subscriptions = [row for row in active_subscriptions_of(current_user)
                     if (row.provider_kind in NOT_CANCELLED or is_tier(offers.get(row.offer_id), groups))
                     and (not onion or row.current_period_end or row.provider_kind == COMPLIMENTARY)]
    complimentary = complimentary_of(current_user) is not None
    payments = Payment.query.filter_by(user_id=current_user.id).order_by(Payment.created_at.desc()) \
        .limit(HISTORY_ROWS).all() if member else []
    return render_template('user/subscription.html', title=_('Subscription'), here=request.path, member=member,
                           subscriptions=subscriptions, titles={offer_id: offer.title for offer_id, offer in offers.items()},
                           payments=payments, complimentary=complimentary, lapsed=current_user.access_has_lapsed(),
                           plans_url=plans_page_url(), redeem_form=None if complimentary else RedeemCodeForm(),
                           held_code=None if complimentary else find_discount_code(session.get(HELD_CODE)),
                           currency=get_setting('payment_currency'))


def _offer(offer_id: int | None) -> Offer | None:
    return db.session.get(Offer, offer_id) if offer_id else None


def _back():
    """Back to the page a member's form was on (the membership page, DM3), or /donations."""
    return redirect(safe_redirect_target(request.form.get('next'), url_for('payments.donations')))


@bp.route('/donations/redeem', methods=['POST'])
@login_required
def redeem():
    """A member adds a gift code's free period (paid membership G4). A member's referral code is for joining, so it is
    not found here (find_gift_code)."""
    form = RedeemCodeForm()
    if complimentary_of(current_user) is not None:
        return _complimentary()
    if form.validate_on_submit():
        with checking_codes():   # a wrong code says nothing more than any other, but guessing is slowed (shared bucket)
            gift = find_gift_code(form.code.data)
            discount = find_discount_code(form.code.data) if gift is None else None
        subscription = start_gift(current_user, gift) if gift is not None else None
        if subscription is not None:
            flash(_('Thank you. Your subscription now runs until %(date)s.',
                    date=subscription.current_period_end.strftime('%Y-%m-%d')))
        elif discount is not None:   # held for the next checkout (plan phase 3 D1)
            session[HELD_CODE] = discount.code
            flash(_('Code %(code)s will be taken off your next payment.', code=discount.code))
        else:
            db.session.rollback()   # the code's row lock
            flash(_('That code cannot be used.'), 'error')
    return _back()


@bp.route('/donations/code/clear', methods=['POST'])
@login_required
def clear_code():
    """Forget a held discount code. The site's CSRF protection turns away a POST without its token before this runs."""
    session.pop(HELD_CODE, None)
    return _back()


@bp.route('/donations/checkout/<int:offer_id>', methods=['POST'])
@login_required_if_private_instance
@limiter.limit('20 per 10 minutes')
def checkout(offer_id):
    """A one-off needs no account (M8). A guest has no session to bind a CSRF token to, and a forged request acts for
    nobody and only makes an unpaid session at the provider, so the limit above is what guards this for them."""
    offer = next((offer for offer in offers_here() if offer.id == offer_id and offer.kind != LINK), None)
    if offer is None:
        abort(404)
    if current_user.is_anonymous and offer.kind == 'donation':
        return _start_checkout(offer, None)
    if complimentary_of(current_user) is not None:
        return _complimentary()
    return _member_checkout(offer)


def _complimentary():
    """Somebody holding complimentary membership is not asked to pay, and their payment is not taken (K5)."""
    flash(_('Your membership is complimentary: there is nothing to pay.'))
    return _back()


@login_required
def _member_checkout(offer):
    """With a discount code held (D1): reserved for this offer, or the checkout stops, saying so."""
    discount = None
    if session.get(HELD_CODE):
        found = find_discount_code(session[HELD_CODE])
        if found is None or reserve(current_user, found, offer) is None:
            flash(_('Your code cannot be used for this. Clear it to pay the full price.'), 'error')
            return _back()
        discount = found
    answer = _start_checkout(offer, current_user, discount)
    # A redirect to the provider: the code is on its way. A provider that is down answers (page, 503): kept for a retry
    if discount is not None and not isinstance(answer, tuple):
        session.pop(HELD_CODE, None)
    return answer


def _start_checkout(offer, user, discount=None):
    try:
        url = adapter_for(offer.provider_kind).checkout(offer, user,
                                                        url_for('payments.result', result='success', _external=True),
                                                        url_for('payments.result', result='failure', _external=True),
                                                        discount=discount)
    except ProviderError:
        return render_template('generic_message.html', title=_('Donation plans'),
                               message=_('The payment provider is unavailable at the moment. Please try again later.')), 503
    return redirect(url)


@bp.route('/donations/subscriptions/<int:subscription_id>/cancel', methods=['POST'])
@clearnet_only('main.index')
@login_required
def cancel(subscription_id):
    subscription = MemberSubscription.query.filter_by(id=subscription_id, user_id=current_user.id,
                                                      status='active').first()
    # Gifts and complimentary membership are not payments; one already cancelled has its end
    if subscription is None or subscription.provider_kind in NOT_CANCELLED or subscription.ends_at is not None:
        abort(404)
    try:
        cancel_subscription(subscription)
    except ProviderError:
        flash(_('The payment provider is unavailable at the moment. Please try again later.'), 'error')
        return _back()
    flash(_('Your subscription will run until %(date)s.', date=subscription.ends_at.strftime('%Y-%m-%d')) if subscription.ends_at
          else _('Your regular donation has been cancelled.'))
    return _back()


@bp.route('/donations/billing', methods=['POST'])
@clearnet_only('main.index')
@login_required
def billing():
    if not current_user.stripe_customer_id:   # nothing for the portal to manage yet
        flash(_('You do not have a donation to manage yet.'))
        return _back()
    try:
        return redirect(adapter_for('stripe').billing_portal_url(current_user.stripe_customer_id,
                                                                 url_for('payments.donations', _external=True)))
    except ProviderError:
        flash(_('The billing portal is unavailable at the moment. Please try again later.'), 'error')
        return _back()


@bp.route('/donations/return/<result>')
def result(result):
    if result not in ('success', 'failure'):
        abort(404)
    joining = current_user.is_anonymous and paid_registration(current_storefront())
    return render_template('user/stripe_result.html', result=result, joining=joining)


@bp.route('/stripe_result/<result>')
def result_before_this_release(result):
    """Where a checkout begun before this release comes back."""
    return redirect(url_for('payments.result', result=result))
