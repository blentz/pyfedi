"""/admin/payments: how this instance takes money (spec 2026-11-05-monetization-infrastructure-design.md).

A credential goes in through a form and never comes back out: password fields render no value, a blank field keeps what
is stored, and a refusal from the provider leaves the old credentials in place.
"""
from babel.numbers import list_currencies
from flask import abort, current_app, flash, redirect, request, url_for
from flask_babel import _
from flask_wtf import FlaskForm

from app import db
from app.admin import bp
from app.admin.forms import BtcpayCredentialsForm, BtcpayOfferForm, LinkOfferForm, OfferForm, PaymentSettingsForm, \
    StripeCredentialsForm
from app.models import MemberSubscription, Offer, OfferDomain, Payment, PaymentProvider, SiteDomain, User
from app.payments.adapters import adapter_for
from app.payments.adapters.base import ProviderError
from app.payments.adapters.btcpay_adapter import REQUIRED_EVENTS as BTCPAY_EVENTS, REQUIRED_PERMISSIONS as BTCPAY_PERMISSIONS, \
    from_minor, to_minor
from app.payments.adapters.stripe_adapter import REQUIRED_EVENTS
from app.payments.credentials import CredentialsUnavailable, credentials_available, seal, unseal
from app.payments.offers import LINK, offers_changed
from app.storefront import domain_rows
from app.utils import get_setting, login_required, permission_required, render_template, roles_with, set_setting

LEDGER_PAGE = 50
CURRENCY_SETTING = 'payment_currency'
RETENTION_SETTING = 'payment_retention_days'
STATUSES = ('paid', 'refunded', 'mismatch')


def _stored_stripe() -> dict:
    """What is stored, or nothing: a value the key cannot open can only be replaced."""
    row = PaymentProvider.query.filter_by(kind='stripe').first()
    if row is None or not row.credentials:
        return {}
    try:
        return unseal(row.credentials)
    except CredentialsUnavailable:
        return {}


def _stripe_source() -> str:
    row = PaymentProvider.query.filter_by(kind='stripe').first()
    if row is not None and row.credentials:
        return 'stored'
    return 'environment' if current_app.config.get('STRIPE_SECRET_KEY') else 'none'


def _stored_btcpay() -> dict:
    row = PaymentProvider.query.filter_by(kind='btcpay').first()
    if row is None or not row.credentials:
        return {}
    try:
        return unseal(row.credentials)
    except CredentialsUnavailable:
        return {}


def _btcpay_form() -> BtcpayCredentialsForm:
    row = PaymentProvider.query.filter_by(kind='btcpay').first()
    return BtcpayCredentialsForm(enabled=row.enabled if row else True, **(row.settings or {} if row else {}))


def _page(stripe_form=None, settings_form=None, btcpay_form=None):
    row = PaymentProvider.query.filter_by(kind='stripe').first()
    btcpay = PaymentProvider.query.filter_by(kind='btcpay').first()
    stripe_form = stripe_form or StripeCredentialsForm(enabled=row.enabled if row else True)
    settings_form = settings_form or PaymentSettingsForm(retention_days=get_setting(RETENTION_SETTING))
    return render_template('admin/payments.html', title=_('Payments'), roles_with=roles_with('manage monetization'),
                           key_set=credentials_available(), source=_stripe_source(), stripe_form=stripe_form,
                           settings_form=settings_form, offers=Offer.query.order_by(Offer.offer_group, Offer.position,
                                                                                    Offer.id).all(),
                           import_form=FlaskForm(), currency=get_setting(CURRENCY_SETTING),
                           webhook_url=url_for('payments.payment_webhook', kind='stripe', _external=True),
                           required_events=REQUIRED_EVENTS, retention=get_setting(RETENTION_SETTING),
                           btcpay_form=btcpay_form or _btcpay_form(), btcpay_connected=bool(btcpay and btcpay.credentials),
                           btcpay_webhook_url=url_for('payments.payment_webhook', kind='btcpay', _external=True),
                           btcpay_events=BTCPAY_EVENTS, btcpay_permissions=BTCPAY_PERMISSIONS)


@bp.route('/payments', methods=['GET'])
@login_required
@permission_required('manage monetization')
def admin_payments():
    return _page()


@bp.route('/payments/stripe', methods=['POST'])
@login_required
@permission_required('manage monetization')
def admin_payments_stripe():
    form = StripeCredentialsForm()
    if not form.validate_on_submit():
        return _page(stripe_form=form)
    if not credentials_available():
        flash(_('Credentials cannot be stored until PAYMENT_SECRETS_KEY is set.'), 'error')
        return redirect(url_for('admin.admin_payments'))
    row = PaymentProvider.query.filter_by(kind='stripe').first() or PaymentProvider(kind='stripe')
    values = _stored_stripe()
    entered = {name: field.data for name, field in (('secret_key', form.secret_key),
                                                    ('webhook_secret', form.webhook_secret)) if field.data}
    if entered:
        values = {**values, **entered}
        try:
            adapter_for('stripe').check_credentials(values)
        except ProviderError:
            flash(_('Stripe refused that secret key. Nothing was changed.'), 'error')
            return redirect(url_for('admin.admin_payments'))
        row.credentials = seal(values)
    row.enabled = form.enabled.data
    db.session.add(row)
    db.session.commit()
    offers_changed()
    flash(_('Stripe settings saved.'))
    return redirect(url_for('admin.admin_payments'))


@bp.route('/payments/btcpay', methods=['POST'])
@login_required
@permission_required('manage monetization')
def admin_payments_btcpay():
    form = BtcpayCredentialsForm()
    if not form.validate_on_submit():
        return _page(btcpay_form=form)
    if not credentials_available():
        flash(_('Credentials cannot be stored until PAYMENT_SECRETS_KEY is set.'), 'error')
        return redirect(url_for('admin.admin_payments'))
    row = PaymentProvider.query.filter_by(kind='btcpay').first() or PaymentProvider(kind='btcpay')
    settings = {'url': form.url.data or '', 'onion_url': form.onion_url.data or '', 'store_id': form.store_id.data or ''}
    secrets = {**_stored_btcpay(), **{name: field.data for name, field in (('api_key', form.api_key),
                                                                           ('webhook_secret', form.webhook_secret)) if field.data}}
    if form.enabled.data:
        try:
            adapter_for('btcpay').check_credentials({**settings, **secrets})
        except (ProviderError, KeyError):
            flash(_('BTCPay Server refused those details, or could not be reached. Nothing was changed.'), 'error')
            return redirect(url_for('admin.admin_payments'))
    row.settings = settings
    if secrets:
        row.credentials = seal(secrets)
    row.enabled = form.enabled.data
    db.session.add(row)
    db.session.commit()
    offers_changed()
    flash(_('BTCPay settings saved.'))
    return redirect(url_for('admin.admin_payments'))


@bp.route('/payments/stripe/import', methods=['POST'])
@login_required
@permission_required('manage monetization')
def admin_payments_stripe_import():
    """Bring a site's Stripe setup in from its environment (C5). Running it again changes nothing already there."""
    if not credentials_available():
        flash(_('Credentials cannot be stored until PAYMENT_SECRETS_KEY is set.'), 'error')
        return redirect(url_for('admin.admin_payments'))
    row = PaymentProvider.query.filter_by(kind='stripe').first()
    if (row is None or not row.credentials) and current_app.config.get('STRIPE_SECRET_KEY'):
        row = row or PaymentProvider(kind='stripe')
        row.credentials = seal({'secret_key': current_app.config['STRIPE_SECRET_KEY'],
                                'webhook_secret': current_app.config.get('WEBHOOK_SIGNING_SECRET') or ''})
        db.session.add(row)
        db.session.commit()
    if _describe_legacy_offers():
        flash(_('Stripe settings imported.'))
    else:
        flash(_('Stripe settings imported, but some prices could not be read. Open those offers and save them.'), 'warning')
    offers_changed()
    return redirect(url_for('admin.admin_payments'))


def _describe_legacy_offers() -> bool:
    adapter = adapter_for('stripe')
    complete = True
    for offer in Offer.query.filter_by(provider_kind='stripe', amount_minor=None).all():
        try:
            _apply_price(offer, adapter.describe_price(offer.provider_price_id))
        except (ProviderError, CurrencyRefused):
            complete = False
    db.session.commit()
    return complete


class CurrencyRefused(Exception):
    pass


def _apply_price(offer: Offer, price: dict) -> None:
    """An offer's amount, currency and interval are Stripe's, so they cannot disagree with what it charges (plan, P6).
    The first price read sets the instance's one currency (C3)."""
    currency = get_setting(CURRENCY_SETTING)
    if currency and currency != price['currency']:
        raise CurrencyRefused()
    if not currency:
        set_setting(CURRENCY_SETTING, price['currency'])
    offer.amount_minor = price['amount_minor']
    offer.currency = price['currency']
    offer.interval = price['interval']
    offer.kind = 'subscription' if price['interval'] else 'donation'


@bp.route('/payments/settings', methods=['POST'])
@login_required
@permission_required('manage monetization')
def admin_payments_settings():
    form = PaymentSettingsForm()
    if not form.validate_on_submit():
        return _page(settings_form=form)
    set_setting(RETENTION_SETTING, form.retention_days.data)
    flash(_('Settings saved.'))
    return redirect(url_for('admin.admin_payments'))


def _domain_choices() -> list[tuple[int, str]]:
    return [(row.id, row.domain) for row in domain_rows() if row.id is not None]


def _names_a_paid_domain(offer: Offer) -> bool:
    return SiteDomain.query.filter_by(registration_offer_id=offer.id).first() is not None


def _save_offer(form: OfferForm, offer: Offer) -> bool:
    """False, with the reason on the form, if Stripe's price can't be used or a paid domain depends on this offer."""
    if not form.active.data and _names_a_paid_domain(offer):
        form.active.errors.append(_('A paid domain charges this at registration. Change the domain first.'))
        return False
    if offer.provider_price_id != form.price_id.data or offer.amount_minor is None:
        try:
            _apply_price(offer, adapter_for('stripe').describe_price(form.price_id.data))
        except ProviderError:
            form.price_id.errors.append(_('Stripe could not find that price.'))
            return False
        except CurrencyRefused:
            form.price_id.errors.append(_('That price is in a different currency from the rest of this site.'))
            return False
    offer.provider_kind = 'stripe'
    offer.title = form.title.data
    offer.provider_price_id = form.price_id.data
    offer.offer_group = form.offer_group.data
    offer.grants_donor = form.grants_donor.data
    offer.donor_days = None if offer.kind == 'subscription' else form.donor_days.data
    offer.position = form.position.data or 0
    offer.active = form.active.data
    _save_domains(offer, form)
    return True


def _save_link(form: LinkOfferForm, link: Offer) -> None:
    link.provider_kind = link.kind = LINK
    link.offer_group = 'links'
    link.title = form.title.data
    link.url = form.url.data
    link.position = form.position.data or 0
    link.active = form.active.data
    _save_domains(link, form)


def _save_domains(offer: Offer, form) -> None:
    db.session.add(offer)
    db.session.flush()
    OfferDomain.query.filter_by(offer_id=offer.id).delete()
    for site_domain_id in form.domains.data or []:
        db.session.add(OfferDomain(offer_id=offer.id, site_domain_id=site_domain_id))
    db.session.commit()
    offers_changed()


def _save_btcpay_offer(form: BtcpayOfferForm, offer: Offer) -> bool:
    """False, with the reason on the form. BTCPay prices an invoice in this site's one currency (C3)."""
    code = form.currency.data.strip().upper()
    if code not in list_currencies():
        form.currency.errors.append(_('That is not a currency.'))
        return False
    currency = get_setting(CURRENCY_SETTING)
    if currency and currency != code.lower():
        form.currency.errors.append(_('That is a different currency from the rest of this site.'))
        return False
    if not form.active.data and _names_a_paid_domain(offer):
        form.active.errors.append(_('A paid domain charges this at registration. Change the domain first.'))
        return False
    if not currency:
        set_setting(CURRENCY_SETTING, code.lower())
    offer.provider_kind = 'btcpay'
    offer.title = form.title.data
    offer.amount_minor = to_minor(str(form.amount.data), code)
    offer.currency = code.lower()
    offer.interval = form.interval.data or None
    offer.kind = 'subscription' if form.interval.data else 'donation'
    offer.offer_group = form.offer_group.data
    offer.grants_donor = form.grants_donor.data
    offer.donor_days = None if offer.kind == 'subscription' else form.donor_days.data
    offer.position = form.position.data or 0
    offer.active = form.active.data
    _save_domains(offer, form)
    return True


@bp.route('/payments/btcpay/offers/add', methods=['GET', 'POST'])
@login_required
@permission_required('manage monetization')
def admin_payments_btcpay_offer_add():
    form = BtcpayOfferForm(currency=(get_setting(CURRENCY_SETTING) or '').upper())
    form.domains.choices = _domain_choices()
    if form.validate_on_submit() and _save_btcpay_offer(form, Offer()):
        flash(_('Offer saved.'))
        return redirect(url_for('admin.admin_payments'))
    return render_template('admin/payments_btcpay_offer.html', title=_('Add BTCPay offer'), form=form,
                           roles_with=roles_with('manage monetization'))


def _edit_btcpay_offer(offer: Offer):
    form = BtcpayOfferForm(obj=offer)
    form.domains.choices = _domain_choices()
    if request.method == 'GET':
        form.amount.data = from_minor(offer.amount_minor, offer.currency)
        form.currency.data = offer.currency.upper()
        form.interval.data = offer.interval or ''
        form.domains.data = [row.site_domain_id for row in OfferDomain.query.filter_by(offer_id=offer.id)]
    if form.validate_on_submit() and _save_btcpay_offer(form, offer):
        flash(_('Offer saved.'))
        return redirect(url_for('admin.admin_payments'))
    return render_template('admin/payments_btcpay_offer.html', title=_('Edit offer'), form=form, offer=offer,
                           delete_form=FlaskForm(), roles_with=roles_with('manage monetization'))


@bp.route('/payments/links/add', methods=['GET', 'POST'])
@login_required
@permission_required('manage monetization')
def admin_payments_link_add():
    form = LinkOfferForm()
    form.domains.choices = _domain_choices()
    if form.validate_on_submit():
        _save_link(form, Offer())
        flash(_('Link saved.'))
        return redirect(url_for('admin.admin_payments'))
    return render_template('admin/payments_link.html', title=_('Add link'), form=form,
                           roles_with=roles_with('manage monetization'))


@bp.route('/payments/offers/add', methods=['GET', 'POST'])
@login_required
@permission_required('manage monetization')
def admin_payments_offer_add():
    form = OfferForm()
    form.domains.choices = _domain_choices()
    if form.validate_on_submit() and _save_offer(form, Offer()):
        flash(_('Offer saved.'))
        return redirect(url_for('admin.admin_payments'))
    return render_template('admin/payments_offer.html', title=_('Add offer'), form=form,
                           roles_with=roles_with('manage monetization'))


@bp.route('/payments/offers/<int:offer_id>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('manage monetization')
def admin_payments_offer_edit(offer_id):
    offer = db.session.get(Offer, offer_id) or abort(404)
    if offer.kind == LINK:
        return _edit_link(offer)
    if offer.provider_kind == 'btcpay':
        return _edit_btcpay_offer(offer)
    form = OfferForm(obj=offer)
    form.domains.choices = _domain_choices()
    if request.method == 'GET':
        form.price_id.data = offer.provider_price_id
        form.domains.data = [row.site_domain_id for row in OfferDomain.query.filter_by(offer_id=offer.id)]
    if form.validate_on_submit() and _save_offer(form, offer):
        flash(_('Offer saved.'))
        return redirect(url_for('admin.admin_payments'))
    return render_template('admin/payments_offer.html', title=_('Edit offer'), form=form, offer=offer,
                           delete_form=FlaskForm(), roles_with=roles_with('manage monetization'))


def _edit_link(link: Offer):
    form = LinkOfferForm(obj=link)
    form.domains.choices = _domain_choices()
    if request.method == 'GET':
        form.domains.data = [row.site_domain_id for row in OfferDomain.query.filter_by(offer_id=link.id)]
    if form.validate_on_submit():
        _save_link(form, link)
        flash(_('Link saved.'))
        return redirect(url_for('admin.admin_payments'))
    return render_template('admin/payments_link.html', title=_('Edit link'), form=form, offer=link,
                           delete_form=FlaskForm(), roles_with=roles_with('manage monetization'))


@bp.route('/payments/offers/<int:offer_id>/delete', methods=['POST'])
@login_required
@permission_required('manage monetization')
def admin_payments_offer_delete(offer_id):
    offer = db.session.get(Offer, offer_id) or abort(404)
    if _names_a_paid_domain(offer):
        flash(_('A paid domain charges this at registration. Change the domain first.'), 'error')
    elif Payment.query.filter_by(offer_id=offer.id).first() or MemberSubscription.query.filter_by(offer_id=offer.id).first():
        flash(_('An offer somebody has paid for cannot be deleted. Stop offering it instead.'), 'error')
    else:
        db.session.delete(offer)
        db.session.commit()
        offers_changed()
        flash(_('Offer deleted.'))
    return redirect(url_for('admin.admin_payments'))


@bp.route('/payments/ledger', methods=['GET'])
@login_required
@permission_required('manage monetization')
def admin_payments_ledger():
    status = request.args.get('status')
    offer_id = request.args.get('offer_id', type=int)
    page = request.args.get('page', 1, type=int)
    query = Payment.query
    if status in STATUSES:
        query = query.filter_by(status=status)
    else:
        status = None
    offer = db.session.get(Offer, offer_id) if offer_id and 0 < offer_id < 2**31 else None
    if offer:
        query = query.filter_by(offer_id=offer.id)
    payments = query.order_by(Payment.created_at.desc(), Payment.id.desc()).paginate(page=page, per_page=LEDGER_PAGE,
                                                                                    error_out=False)
    users = {user.id: user for user in User.query.filter(User.id.in_({row.user_id for row in payments.items
                                                                      if row.user_id}))}
    args = {'status': status, 'offer_id': offer.id if offer else None}
    return render_template('admin/payments_ledger.html', title=_('Payment ledger'), payments=payments, users=users,
                           statuses=STATUSES, status=status, offers=Offer.query.order_by(Offer.title).all(),
                           offer=offer, titles={row.id: row.title for row in Offer.query.all()},
                           next_url=url_for('admin.admin_payments_ledger', page=payments.next_num, **args)
                           if payments.has_next else None,
                           prev_url=url_for('admin.admin_payments_ledger', page=payments.prev_num, **args)
                           if payments.has_prev else None,
                           retention=get_setting(RETENTION_SETTING), roles_with=roles_with('manage monetization'))
