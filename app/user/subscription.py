import httpx
from flask import redirect, url_for, flash, request, current_app
from flask_login import logout_user, current_user
from flask_babel import _, lazy_gettext as _l

from app import db
from app.pinned_http import pinned_transport
from app.models import User, CmsPage
from app.user import bp
from app.utils import render_template, login_required


@bp.route('/donate')
def choose_plan():
    cms_page = CmsPage.query.filter(CmsPage.url == '/donate').first()
    if current_user.is_authenticated:
        # inspired by https://stripe.com/docs/payments/checkout/subscriptions/starting
        if current_user.stripe_subscription_id is not None:
            flash(_('Thank you for supporting %(instance_name)s! Any choice you make below will replace your current donation plan.', instance_name=current_app.config['SERVER_NAME']))

        if current_app.config['STRIPE_SECRET_KEY']:
            return render_template('user/choose_plan.html', title=_('Choose a donation plan'),
                                   instance_name=current_app.config['SERVER_NAME'], monthly_small=current_app.config['STRIPE_MONTHLY_SMALL'],
                                   monthly_big=current_app.config['STRIPE_MONTHLY_BIG'], monthly_small_text=current_app.config['STRIPE_MONTHLY_SMALL_TEXT'],
                                   monthly_big_text=current_app.config['STRIPE_MONTHLY_BIG_TEXT']
                                   )
        else:
            return render_template('donate.html', title=_('Donate'), cms_page=cms_page)
    else:
        if current_app.config['STRIPE_SECRET_KEY']:
            flash(_('Log in to donate to %(instance_name)s or donate to the PieFed project using the button below.', instance_name=current_app.config['SERVER_NAME']))
        return render_template('donate.html', title=_('Donate'), cms_page=cms_page)


@bp.route('/stripe_redirect/<plan>', methods=['GET'])
@login_required
def stripe_redirect(plan):
    import stripe  # lazy: stripe is slow to import (0.7s) and only billing uses it
    stripe.api_key = current_app.config['STRIPE_SECRET_KEY']
    if current_user.stripe_customer_id is None or current_user.stripe_customer_id == '':             # Stripe won't let us use both customer and email, so
        stripe_customer = None                              # only specify the email for new subscriptions
        stripe_customer_email = current_user.email
    else:
        stripe_customer = current_user.stripe_customer_id
        stripe_customer_email = None

    stripe_session = None

    if plan == 'monthly_small':
        stripe_session = stripe.checkout.Session.create(
            client_reference_id=current_user.id,
            customer=stripe_customer,
            customer_email=stripe_customer_email,
            # payment_method_types=['card'],
            line_items=[{
                'price': current_app.config['STRIPE_MONTHLY_SMALL'],
                'quantity': 1,
            }],
            mode='subscription',
            success_url=url_for('user.stripe_result', result='success', _external=True),
            cancel_url=url_for('user.stripe_result', result='failure', _external=True),
        )
    elif plan == 'monthly_big':
        stripe_session = stripe.checkout.Session.create(
            client_reference_id=current_user.id,
            customer=stripe_customer,
            customer_email=stripe_customer_email,
            # payment_method_types=['card'],
            line_items=[{
                'price': current_app.config['STRIPE_MONTHLY_BIG'],
                'quantity': 1,
            }],
            mode='subscription',
            success_url=url_for('user.stripe_result', result='success', _external=True),
            cancel_url=url_for('user.stripe_result', result='failure', _external=True),
        )
    elif plan == 'billing':
        if stripe_customer is None:       # nothing for the portal to manage yet
            flash(_('You do not have a donation to manage yet.'))
            return redirect(url_for('user.choose_plan'))

        with httpx.Client(timeout=10, transport=pinned_transport()) as client:  # R162
            response = client.post(
                'https://api.stripe.com/v1/billing_portal/sessions',
                data={'customer': stripe_customer, 'return_url': url_for('user.choose_plan', _external=True)},
                auth=(current_app.config['STRIPE_SECRET_KEY'], '')
            )

        try:
            stripe_session = response.json()
        except ValueError:
            stripe_session = {}
        if not isinstance(stripe_session, dict) or 'url' not in stripe_session:
            current_app.logger.error(f'Stripe billing portal refused: {response.status_code} {response.text[:500]}')
            flash(_('The billing portal is unavailable at the moment. Please try again later.'), 'error')
            return redirect(url_for('user.choose_plan'))
        return redirect(stripe_session['url'])

    return render_template('user/stripe_redirect.html', title=_('Please wait...'),
                           key=current_app.config['STRIPE_PUBLISHABLE_KEY'], stripe_session=stripe_session)


def user_from_reference(client_reference_id):
    """The account a checkout session names, or None.

    `client_reference_id` is whatever is attached to the session at checkout,
    so it is not necessarily a user id, and it is not necessarily a number at
    all. Handing it straight to the database raises, and a webhook that raises
    is one Stripe retries for days.
    """
    try:
        user_id = int(client_reference_id)
    except (TypeError, ValueError):
        return None
    return db.session.get(User, user_id)


@bp.route('/stripe_webhook', methods=['POST'])
def stripe_webhook():
    import stripe  # lazy: stripe is slow to import (0.7s) and only billing uses it
    # inspired by https://stripe.com/docs/payments/checkout/fulfillment#webhooks
    # During development, run this in a terminal:
    # stripe listen --forward-to https://your_dev_url/stripe_webhook
    stripe.api_key = current_app.config['STRIPE_SECRET_KEY']

    payload = request.data.decode("utf-8")
    sig_header = request.headers.get("Stripe-Signature", None)

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, current_app.config['WEBHOOK_SIGNING_SECRET'])
    except ValueError:
        # Invalid payload
        return 'invalid payload', 400
    except stripe.error.SignatureVerificationError:
        # Invalid signature
        return 'could not verify signature', 400

    event = event.to_dict()             # stripe >= 13: a StripeObject is no dict (no .get)
    data = event.get('data')            # every value below comes off the wire
    stripe_object = data.get('object') if isinstance(data, dict) else None
    if not isinstance(stripe_object, dict):
        stripe_object = {}

    # Handle the checkout.session.completed event
    # Fulfill the purchase...
    if event.get('type') == 'checkout.session.completed':
        u = user_from_reference(stripe_object.get('client_reference_id'))
        if u is None:   # could not find user, bail
            return 'Ok'
        if stripe_object.get('customer'):   # a session with no customer of its own must not erase the one we have
            u.stripe_customer_id = stripe_object['customer']
        if stripe_object.get('subscription') is not None:
            if u.stripe_subscription_id is not None and u.stripe_subscription_id != stripe_object['subscription']:  # Remove previous subscription, if any
                try:
                    stripe.Subscription.delete(u.stripe_subscription_id)
                except Exception:
                    pass

            u.stripe_subscription_id = stripe_object['subscription']

        db.session.commit()
    # Handle the customer.subscription.deleted event
    elif event.get('type') == 'customer.subscription.deleted':
        subscription_id = stripe_object.get('id')
        u = User.query.filter_by(stripe_subscription_id=subscription_id).first() if subscription_id else None
        if u is not None:
            u.stripe_subscription_id = None
            db.session.commit()

    # more subscription types at https://stripe.com/docs/api/events/types

    return 'ok'


@bp.route('/stripe_result/<result>')
@login_required
def stripe_result(result):
    if result == 'success' or result == 'failure':
        return render_template('user/stripe_result.html', result=result)
    else:
        return ''


@bp.route('/plan_unsubscribe/<account_id>/<subscription>')
@login_required
def plan_unsubscribe(account_id, subscription):
    import stripe  # lazy: stripe is slow to import (0.7s) and only billing uses it
    if current_user.stripe_subscription_id is None:
        return render_template('generic_message.html', title=_('You are not donating'), message=_('There are no regular donations set to occur in the future.'))
    elif current_user.stripe_subscription_id == subscription:
        stripe.api_key = current_app.config['STRIPE_SECRET_KEY']
        stripe.Subscription.delete(current_user.stripe_subscription_id)
        current_user.stripe_subscription_id = None
        db.session.commit()
        return render_template('generic_message.html', title=_('Regular donation cancelled'), message=_('Your donation has been cancelled.'))
    else:
        return render_template('generic_message.html', title=_('Donation not found'),
                               message=_('That is not one of your regular donations.'))

