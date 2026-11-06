"""Provider webhooks, and the member's page for paying and for stopping paying.

A browser coming back from a provider grants nothing: only a verified webhook does (spec, Security).
"""
from flask import abort, flash, redirect, request, url_for
from flask_babel import _
from flask_login import current_user

from app import limiter
from app.models import MemberSubscription, Offer, Payment
from app.onion import clearnet_only, is_onion_request
from app.payments import bp
from app.payments.adapters import adapter_for
from app.payments.adapters.base import InvalidWebhook, ProviderError
from app.payments.core import active_subscriptions_of, apply_event, cancel_subscription
from app.payments.offers import LINK, offers_here
from app.storefront import current_storefront, paid_registration
from app.utils import login_required, login_required_if_private_instance, render_template

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
def donations():
    """The offers, and the way to stop paying. Not /donate: that is a CMS page an admin may delete, and a donor must
    still be able to cancel. The seeded /donate page links here on every site, so with nothing to offer this says so on
    the page itself: a logged-out visitor has no session cookie, so a flash would not survive a redirect."""
    offers = offers_here()
    member = current_user.is_authenticated and not is_onion_request()   # nothing here can act on them on the onion
    subscriptions = active_subscriptions_of(current_user) if member else []
    if not offers and not subscriptions:
        return render_template('generic_message.html', title=_('Donation plans'),
                               message=_('Donation plans are not available on this site.')), 404
    held = {subscription.offer_id for subscription in subscriptions}
    links = [offer for offer in offers if offer.kind == LINK]
    groups = {}
    for offer in offers:
        if offer.kind != LINK and offer.id not in held:   # choosing one held would start a second and cancel the first
            groups.setdefault(offer.offer_group, []).append(offer)
    payments = Payment.query.filter_by(user_id=current_user.id).order_by(Payment.created_at.desc()).limit(HISTORY_ROWS).all() \
        if member else []
    titles = {offer.id: offer.title for offer in Offer.query.all()}
    paid_domain = paid_registration(current_storefront())   # a fee to belong here is no donation, whatever else is on offer
    return render_template('user/choose_plan.html', title=_('Your membership') if paid_domain else _('Choose a donation plan'),
                           groups=groups, links=links, subscriptions=subscriptions, payments=payments, titles=titles,
                           member=member, paid_domain=paid_domain)


@bp.route('/donations/checkout/<int:offer_id>', methods=['POST'])
@login_required_if_private_instance
@clearnet_only('main.index')
@limiter.limit('20 per 10 minutes')
def checkout(offer_id):
    """A one-off needs no account (M8). A guest has no session to bind a CSRF token to, and a forged request acts for
    nobody and only makes an unpaid session at the provider, so the limit above is what guards this for them."""
    offer = next((offer for offer in offers_here() if offer.id == offer_id and offer.kind != LINK), None)
    if offer is None:
        abort(404)
    if current_user.is_anonymous and offer.kind == 'donation':
        return _start_checkout(offer, None)
    return _member_checkout(offer)


@login_required
def _member_checkout(offer):
    return _start_checkout(offer, current_user)


def _start_checkout(offer, user):
    try:
        url = adapter_for(offer.provider_kind).checkout(offer, user,
                                                        url_for('payments.result', result='success', _external=True),
                                                        url_for('payments.result', result='failure', _external=True))
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
    if subscription is None:
        abort(404)
    try:
        cancel_subscription(subscription)
    except ProviderError:
        flash(_('The payment provider is unavailable at the moment. Please try again later.'), 'error')
        return redirect(url_for('payments.donations'))
    flash(_('Your subscription will run until %(date)s.', date=subscription.ends_at.strftime('%Y-%m-%d')) if subscription.ends_at
          else _('Your regular donation has been cancelled.'))
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
def result(result):
    if result not in ('success', 'failure'):
        abort(404)
    joining = current_user.is_anonymous and paid_registration(current_storefront())
    return render_template('user/stripe_result.html', result=result, joining=joining)


@bp.route('/stripe_result/<result>')
def result_before_this_release(result):
    """Where a checkout begun before this release comes back."""
    return redirect(url_for('payments.result', result=result))
