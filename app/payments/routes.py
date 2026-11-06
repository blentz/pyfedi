"""Provider webhooks, and the member's page for paying and for stopping paying.

A browser coming back from a provider grants nothing: only a verified webhook does (spec, Security).
"""
from flask import abort, flash, redirect, request, url_for
from flask_babel import _
from flask_login import current_user

from app.models import MemberSubscription, Offer, Payment
from app.onion import clearnet_only, is_onion_request
from app.payments import bp
from app.payments.adapters import adapter_for
from app.payments.adapters.base import InvalidWebhook, ProviderError
from app.payments.core import apply_event, cancel_subscription
from app.payments.offers import active_subscriptions, offers_here
from app.utils import login_required, render_template

HISTORY_ROWS = 100


@bp.route('/payments/webhook/<kind>', methods=['POST'])
def payment_webhook(kind):
    adapter = adapter_for(kind)
    if adapter is None or not adapter.available():
        abort(404)
    try:
        event_id, events = adapter.parse_webhook(request.get_data(), request.headers)
    except InvalidWebhook:
        return 'invalid webhook', 400
    apply_event(kind, event_id, events)
    return 'ok'


@bp.route('/stripe_webhook', methods=['POST'])
def stripe_webhook():
    """What Stripe's dashboard has configured on every existing site (C7)."""
    return payment_webhook('stripe')


@bp.route('/donations')
def donations():
    """The offers, and the way to stop paying. Not /donate: that is a CMS page an admin may delete, and a donor must
    still be able to cancel. The seeded /donate page links here on every site, so with nothing to offer this says so,
    before asking anybody to log in for nothing, and on the page itself: a logged-out visitor has no session cookie, so
    a flash would not survive a redirect."""
    offers = offers_here()
    held = not is_onion_request() and current_user.is_authenticated and active_subscriptions(current_user)
    if not offers and not held:
        return render_template('generic_message.html', title=_('Donation plans'),
                               message=_('Donation plans are not available on this site.')), 404
    return _member_page(offers)


@login_required
def _member_page(offers):
    groups = {}
    for offer in offers:
        groups.setdefault(offer.offer_group, []).append(offer)
    payments = Payment.query.filter_by(user_id=current_user.id).order_by(Payment.created_at.desc()).limit(HISTORY_ROWS).all()
    titles = {offer.id: offer.title for offer in Offer.query.all()}
    return render_template('user/choose_plan.html', title=_('Choose a donation plan'), groups=groups,
                           subscriptions=active_subscriptions(current_user), payments=payments, titles=titles)


@bp.route('/donations/checkout/<int:offer_id>', methods=['POST'])
@clearnet_only('main.index')
@login_required
def checkout(offer_id):
    offer = next((offer for offer in offers_here() if offer.id == offer_id), None)
    if offer is None:
        abort(404)
    try:
        url = adapter_for(offer.provider_kind).checkout(offer, current_user,
                                                        url_for('payments.result', result='success', _external=True),
                                                        url_for('payments.result', result='failure', _external=True))
    except ProviderError:
        flash(_('The payment provider is unavailable at the moment. Please try again later.'), 'error')
        return redirect(url_for('payments.donations'))
    return redirect(url)


@bp.route('/donations/subscriptions/<int:subscription_id>/cancel', methods=['POST'])
@clearnet_only('main.index')
@login_required
def cancel(subscription_id):
    subscription = MemberSubscription.query.filter_by(id=subscription_id, user_id=current_user.id,
                                                      status='active').first()
    if subscription is None:
        abort(404)
    try:
        cancel_subscription(subscription)
    except ProviderError:
        flash(_('The payment provider is unavailable at the moment. Please try again later.'), 'error')
        return redirect(url_for('payments.donations'))
    flash(_('Your regular donation has been cancelled.'))
    return redirect(url_for('payments.donations'))


@bp.route('/donations/billing', methods=['POST'])
@clearnet_only('main.index')
@login_required
def billing():
    if not current_user.stripe_customer_id:   # nothing for the portal to manage yet
        flash(_('You do not have a donation to manage yet.'))
        return redirect(url_for('payments.donations'))
    try:
        return redirect(adapter_for('stripe').billing_portal_url(current_user.stripe_customer_id,
                                                                 url_for('payments.donations', _external=True)))
    except ProviderError:
        flash(_('The billing portal is unavailable at the moment. Please try again later.'), 'error')
        return redirect(url_for('payments.donations'))


@bp.route('/donations/return/<result>')
@login_required
def result(result):
    if result not in ('success', 'failure'):
        abort(404)
    return render_template('user/stripe_result.html', result=result)


@bp.route('/stripe_result/<result>')
def result_before_this_release(result):
    """Where a checkout begun before this release comes back."""
    return redirect(url_for('payments.result', result=result))
