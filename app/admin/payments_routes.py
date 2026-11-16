"""/admin/payments: how this instance takes money (spec 2026-11-05-monetization-infrastructure-design.md).

A credential goes in through a form and never comes back out: password fields render no value, a blank field keeps what
is stored, and a refusal from the provider leaves the old credentials in place.
"""
import secrets
from datetime import datetime

from babel.numbers import list_currencies
from flask import abort, current_app, flash, redirect, request, url_for
from flask_babel import _
from flask_login import current_user
from flask_wtf import FlaskForm
from sqlalchemy import func

from app import db
from app.admin import bp
from app.admin.forms import BtcpayCredentialsForm, BtcpayOfferForm, ComplimentaryForm, DefaultBenefitForm, GiftCodeForm, \
    LinkOfferForm, MembershipForm, OfferForm, PaymentSettingsForm, PaypalCredentialsForm, PaypalOfferForm, ReferralCodeForm, \
    StripeCredentialsForm
from app.complimentary import KIND as COMPLIMENTARY, end_complimentary, grant_complimentary
from app.models import MemberSubscription, Offer, OfferDomain, Payment, PaymentProvider, PromoCode, PromoCodeUse, \
    SiteDomain, User, utcnow
from app.payments.adapters import adapter_for
from app.payments.adapters.base import ProviderError
from app.payments.adapters.btcpay_adapter import REQUIRED_EVENTS as BTCPAY_EVENTS, REQUIRED_PERMISSIONS as BTCPAY_PERMISSIONS, \
    from_minor, to_minor
from app.payments.adapters.stripe_adapter import REQUIRED_EVENTS
from app.payments.adapters.paypal_adapter import REQUIRED_EVENTS as PAYPAL_EVENTS
from app.payments.credentials import CredentialsUnavailable, credentials_available, seal, unseal
from app.payments.code_text import normalise
from app.payments.core import refresh_access
from app.payments.discounts import DISCOUNT_KINDS
from app.payments.gifts import AUTO, REFERRAL_BENEFIT_SETTING, Benefit, default_referral_choice, \
    referral_benefit
from app.payments.offers import LINK, offers_changed, offers_on, tiers_on
from app.storefront import clear_domain_cache, domain_rows
from app.utils import get_setting, login_required, permission_required, render_template, roles_with, set_setting

LEDGER_PAGE = 50
CURRENCY_SETTING = 'payment_currency'
RETENTION_SETTING = 'payment_retention_days'
STATUSES = ('paid', 'refunded', 'mismatch')
# A made code: no 0/o or 1/l, which are misread when a code is read out
CODE_ALPHABET, CODE_LENGTH, TYPED_CODE_MINIMUM = 'abcdefghjkmnpqrstuvwxyz23456789', 12, 8


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


def _stored_paypal() -> dict:
    return _stored_btcpay('paypal')


def _stored_btcpay(kind='btcpay') -> dict:
    row = PaymentProvider.query.filter_by(kind=kind).first()
    if row is None or not row.credentials:
        return {}
    try:
        return unseal(row.credentials)
    except CredentialsUnavailable:
        return {}


def _btcpay_form() -> BtcpayCredentialsForm:
    row = PaymentProvider.query.filter_by(kind='btcpay').first()
    return BtcpayCredentialsForm(enabled=row.enabled if row else True, **(row.settings or {} if row else {}))


def _paypal_form() -> PaypalCredentialsForm:
    row = PaymentProvider.query.filter_by(kind='paypal').first()
    return PaypalCredentialsForm(enabled=row.enabled if row else True, **(row.settings or {} if row else {}))


def _home_row() -> SiteDomain | None:
    return SiteDomain.query.filter_by(is_default=True).first()


def _membership_tiers(row: SiteDomain | None) -> list[Offer]:
    """What joining the default domain may cost: subscriptions usable there."""
    if row is None:
        return []
    return [offer for offer in offers_on(row.id, row.is_onion) if offer.kind == 'subscription']


def _membership_form(row: SiteDomain | None) -> MembershipForm:
    form = MembershipForm(membership=bool(row and row.registration == 'paid'),
                          membership_offer_id=(row.registration_offer_id if row else None) or 0,
                          membership_grace_days=row.lapse_grace_days if row and row.registration == 'paid' else 3)
    form.membership_offer_id.choices = [(0, _('None'))] + [(offer.id, offer.title) for offer in _membership_tiers(row)]
    return form


def _page(stripe_form=None, settings_form=None, btcpay_form=None, paypal_form=None, membership_form=None):
    home = _home_row()
    paypal = PaymentProvider.query.filter_by(kind='paypal').first()
    row = PaymentProvider.query.filter_by(kind='stripe').first()
    btcpay = PaymentProvider.query.filter_by(kind='btcpay').first()
    stripe_form = stripe_form or StripeCredentialsForm(enabled=row.enabled if row else True)
    settings_form = settings_form or PaymentSettingsForm(retention_days=get_setting(RETENTION_SETTING))
    return render_template('admin/payments.html', title=_('Payments'), roles_with=roles_with('manage monetization'),
                           key_set=credentials_available(), source=_stripe_source(), stripe_form=stripe_form,
                           settings_form=settings_form, offers=Offer.query.filter(Offer.provider_kind != COMPLIMENTARY)
                           .order_by(Offer.offer_group, Offer.position, Offer.id).all(),
                           import_form=FlaskForm(), currency=get_setting(CURRENCY_SETTING),
                           webhook_url=url_for('payments.payment_webhook', kind='stripe', _external=True),
                           required_events=REQUIRED_EVENTS, retention=get_setting(RETENTION_SETTING),
                           btcpay_form=btcpay_form or _btcpay_form(), btcpay_connected=bool(btcpay and btcpay.credentials),
                           btcpay_webhook_url=url_for('payments.payment_webhook', kind='btcpay', _external=True),
                           btcpay_events=BTCPAY_EVENTS, btcpay_permissions=BTCPAY_PERMISSIONS,
                           paypal_form=paypal_form or _paypal_form(), paypal_connected=bool(paypal and paypal.credentials),
                           paypal_webhook_url=url_for('payments.payment_webhook', kind='paypal', _external=True),
                           paypal_events=PAYPAL_EVENTS, home=home,
                           membership_form=membership_form or _membership_form(home))


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


@bp.route('/payments/paypal', methods=['POST'])
@login_required
@permission_required('manage monetization')
def admin_payments_paypal():
    form = PaypalCredentialsForm()
    if not form.validate_on_submit():
        return _page(paypal_form=form)
    if not credentials_available():
        flash(_('Credentials cannot be stored until PAYMENT_SECRETS_KEY is set.'), 'error')
        return redirect(url_for('admin.admin_payments'))
    row = PaymentProvider.query.filter_by(kind='paypal').first() or PaymentProvider(kind='paypal')
    settings = {'webhook_id': form.webhook_id.data or '', 'sandbox': form.sandbox.data}
    secrets = {**_stored_paypal(), **{name: field.data for name, field in (('client_id', form.client_id),
                                                                           ('client_secret', form.client_secret)) if field.data}}
    if form.enabled.data:
        try:
            adapter_for('paypal').check_credentials({**settings, **secrets})
        except ProviderError:
            flash(_('PayPal refused those details, or could not be reached. Nothing was changed.'), 'error')
            return redirect(url_for('admin.admin_payments'))
    row.settings = settings
    if secrets:
        row.credentials = seal(secrets)
    row.enabled = form.enabled.data
    db.session.add(row)
    db.session.commit()
    offers_changed()
    flash(_('PayPal settings saved.'))
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


@bp.route('/payments/membership', methods=['POST'])
@login_required
@permission_required('manage monetization')
def admin_payments_membership():
    """Turning it on dates it, once: an account made before then stays free for good (PM5, plan M2). Turning it off
    forgets the date, so turning it on again grandfathers everyone who joined in between."""
    row = _home_row()
    if row is None:
        flash(_('This site has no domain settings yet: add its domain on the domains page first.'), 'error')
        return redirect(url_for('admin.admin_payments'))
    form = _membership_form(row)
    if not form.validate_on_submit():
        return _page(membership_form=form)
    tiers = {offer.id for offer in _membership_tiers(row)}
    if form.membership.data:
        if form.membership_offer_id.data not in tiers:
            flash(_('Choose a subscription that is on offer here.'), 'error')
            return redirect(url_for('admin.admin_payments'))
        if row.registration != 'paid':
            row.paid_since = utcnow()
        row.registration = 'paid'
        row.registration_offer_id = form.membership_offer_id.data
        row.lapse_grace_days = form.membership_grace_days.data
    else:
        row.registration, row.paid_since = 'anyone', None
    db.session.commit()
    clear_domain_cache()
    flash(_('Membership saved.'))
    return redirect(url_for('admin.admin_payments'))


def _gift_tiers() -> list[Offer]:
    """What a gift may be a free period of: the subscriptions on offer."""
    return Offer.query.filter_by(kind='subscription', active=True).order_by(Offer.offer_group, Offer.position, Offer.id).all()


def _tier_choices(none: bool = False) -> list[tuple[int, str]]:
    return ([(0, _('None'))] if none else []) + [(offer.id, offer.title) for offer in _gift_tiers()]


def _codes_page(default_form=None, code_form=None):
    codes = PromoCode.query.order_by(PromoCode.owner_id.isnot(None), PromoCode.created_at.desc(), PromoCode.id.desc()).all()
    uses = dict(db.session.execute(db.select(PromoCodeUse.promo_code_id, func.count())
                                   .group_by(PromoCodeUse.promo_code_id)).all())
    owners = {user.id: user for user in User.query.filter(User.id.in_(
        {code.owner_id or code.assigned_to for code in codes if code.owner_id or code.assigned_to}))}
    tiers = {offer.id: offer.title for offer in Offer.query.all()}
    if default_form is None:
        chosen = default_referral_choice()
        default_form = DefaultBenefitForm(benefit=_choice_of(chosen['kind'], chosen.get('offer_id'),
                                                             chosen.get('discount_id')),
                                          days=chosen.get('days'))
    default_form.benefit.choices = _benefit_choices(None, own=False)
    # What each member's referral code gives on its owner's domain: its own, one without taking the default (X1)
    gives = {code.id: _describe(referral_benefit(code, owners[code.owner_id].home_domain()))
             for code in codes if code.owner_id in owners}
    db.session.commit()
    code_form = code_form or GiftCodeForm()
    code_form.offer_id.choices = _tier_choices(none=True)
    return render_template('admin/payments_codes.html', title=_('Codes'), codes=codes, uses=uses, owners=owners, gives=gives,
                           tiers=tiers, default_form=default_form, code_form=code_form, currency=get_setting(CURRENCY_SETTING),
                           roles_with=roles_with('manage monetization'))


def _editable_offer(offer_id: int) -> Offer:
    """An offer the admin may edit or delete: not the hidden complimentary one, which only grants name (K1)."""
    offer = db.session.get(Offer, offer_id)
    if offer is None or offer.provider_kind == COMPLIMENTARY:
        abort(404)
    return offer


def _still_charged(user: User) -> bool:
    """Whether a provider will charge the account again: a subscription that renews by itself and is not cancelled.
    Complimentary membership takes no payment (K5), so it waits until that has been stopped."""
    return any(row.ends_at is None and row.provider_kind != COMPLIMENTARY
               and not getattr(adapter_for(row.provider_kind), 'prepaid', False)
               for row in MemberSubscription.query.filter_by(user_id=user.id, status='active'))


@bp.route('/payments/complimentary/<int:user_id>', methods=['POST'])
@login_required
@permission_required('manage monetization')
def admin_payments_complimentary(user_id):
    """Give a local account complimentary membership, or end it (plan 2026-11-15-complimentary-membership.md, K6)."""
    user = db.session.get(User, user_id)
    if user is None or user.deleted or not user.is_local():
        abort(404)
    form = ComplimentaryForm()
    if form.validate_on_submit() and (form.grant.data or form.end.data):   # a button pressed, or nothing to do
        if form.end.data and user.is_admin():   # every admin keeps it (plan 2026-11-16 X3)
            flash(_("An admin's complimentary membership stays while they are an admin."), 'error')
        elif form.end.data:
            if end_complimentary(user):
                refresh_access(user)
                flash(_('Complimentary membership ended.'))
        elif user.registration_pending_since is not None:
            flash(_('This account has not finished joining.'), 'error')
        elif _still_charged(user):
            flash(_('A provider still charges this account. Cancel its subscription first.'), 'error')
        else:
            grant_complimentary(user)
            flash(_('Complimentary membership given.'))
        db.session.commit()
    return redirect(url_for('admin.admin_user_edit', user_id=user.id))


@bp.route('/payments/codes', methods=['GET'])
@login_required
@permission_required('manage monetization')
def admin_payments_codes():
    """Gift codes and members' referral codes (paid membership plan phase 2, G6)."""
    return _codes_page()


@bp.route('/payments/codes/default', methods=['POST'])
@login_required
@permission_required('manage monetization')
def admin_payments_codes_default():
    form = DefaultBenefitForm()
    form.benefit.choices = _benefit_choices(None, own=False)
    if not form.validate_on_submit():
        return _codes_page(default_form=form)
    kind, _sep, number = form.benefit.data.partition(':')
    chosen = {'kind': kind}
    if kind == 'gift':
        chosen.update(offer_id=int(number), days=form.days.data or None)
    elif kind == 'discount':
        chosen['discount_id'] = int(number)
    set_setting(REFERRAL_BENEFIT_SETTING, chosen)
    flash(_('Default saved.'))
    return redirect(url_for('admin.admin_payments_codes'))


def _benefit_choices(domain, own: bool) -> list[tuple[str, str]]:
    """What a referral code may give (plan 2026-11-16 R2): a free period of a tier on `domain`, or a discount code that
    applies there (to one of its tiers or to any offer). The default (domain None) chooses from every tier on offer.
    A code may also be left on the default; the default may be automatic: the cheapest tier, one billing period."""
    tiers = tiers_on(domain) if domain is not None else _gift_tiers()
    tier_ids = {offer.id for offer in tiers}
    discounts = [code for code in PromoCode.query.filter(PromoCode.kind.in_(DISCOUNT_KINDS), PromoCode.owner_id.is_(None))
                 .order_by(PromoCode.code) if code.offer_id is None or code.offer_id in tier_ids]
    first = [] if own else [(AUTO, _('One billing period of the cheapest tier'))]   # never nothing (X1)
    return first + \
        [(f'gift:{offer.id}', _('A free period of %(tier)s', tier=offer.title)) for offer in tiers] + \
        [(f'discount:{code.id}', _('Discount code %(code)s', code=code.code)) for code in discounts]


def _choice_of(kind, offer_id, discount_id) -> str | None:
    return f'gift:{offer_id}' if kind == 'gift' else f'discount:{discount_id}' if kind == 'discount' else kind


def _describe(benefit: Benefit | None) -> str:
    if benefit is None:
        return _('Nothing it can give: joining with it is refused')
    if benefit.discount is not None:
        return _('Discount code %(code)s', code=benefit.discount.code)
    return _('%(tier)s for %(days)d days', tier=benefit.offer.title, days=benefit.days)


def _code_refusal(form: GiftCodeForm, kind: str) -> str | None:
    """What a code of `kind` is missing, or None (G6, D7)."""
    if kind == 'gift' and not (form.offer_id.data and form.days.data):
        return _('A gift needs a tier and its days.')
    if kind == 'percent' and not form.percent.data:
        return _('Say how many percent it takes off, from 1 to 99.')
    if kind == 'amount' and not form.amount.data:
        return _('Say how much it takes off.')
    return None


_NO_SUCH_MEMBER = object()


def _member_named(form: GiftCodeForm):
    """The id of the local account the code is for (owner 2026-11-16), None for nobody's; on a name that is no local
    account, _NO_SUCH_MEMBER with the field told so."""
    name = (form.user_name.data or '').strip()
    if not name:
        return None
    user = User.query.filter(func.lower(User.user_name) == name.lower(), User.ap_id.is_(None),
                             User.deleted.is_(False)).first()
    if user is None:
        form.user_name.errors = list(form.user_name.errors) + [_('No local account is called that.')]
        return _NO_SUCH_MEMBER
    return user.id


def _apply_code(form: GiftCodeForm, code: PromoCode) -> None:
    code.offer_id = form.offer_id.data or None
    code.days = form.days.data if code.kind == 'gift' else None
    code.percent = form.percent.data if code.kind == 'percent' else None
    code.amount_minor = to_minor(str(form.amount.data), get_setting(CURRENCY_SETTING) or 'usd') \
        if code.kind == 'amount' else None
    code.max_uses = form.max_uses.data or None
    code.expires_at = datetime.combine(form.expires_at.data, datetime.max.time()).replace(microsecond=0) \
        if form.expires_at.data else None
    code.active = form.active.data


@bp.route('/payments/codes/add', methods=['POST'])
@login_required
@permission_required('manage monetization')
def admin_payments_codes_add():
    form = GiftCodeForm()
    form.offer_id.choices = _tier_choices(none=True)
    if not form.validate_on_submit():
        return _codes_page(code_form=form)
    kind = form.kind.data if form.kind.data in ('gift', 'percent', 'amount') else 'gift'
    refusal = _code_refusal(form, kind)
    if refusal is not None:
        form.kind.errors = list(form.kind.errors) + [refusal]
        return _codes_page(code_form=form)
    member = _member_named(form)
    if member is _NO_SUCH_MEMBER:
        return _codes_page(code_form=form)
    text = normalise(form.code.data) or ''.join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    if len(text) < TYPED_CODE_MINIMUM:
        form.code.errors.append(_('A code needs at least %(count)d characters, so it cannot be guessed.',
                                  count=TYPED_CODE_MINIMUM))
        return _codes_page(code_form=form)
    if PromoCode.query.filter_by(code=text).first() is not None:
        form.code.errors.append(_('That code is taken.'))
        return _codes_page(code_form=form)
    code = PromoCode(code=text, kind=kind, created_by=current_user.id, created_at=utcnow(), assigned_to=member)
    _apply_code(form, code)
    db.session.add(code)
    db.session.commit()
    flash(_('Code %(code)s saved.', code=text))
    return redirect(url_for('admin.admin_payments_codes'))


@bp.route('/payments/codes/<int:code_id>', methods=['GET', 'POST'])
@login_required
@permission_required('manage monetization')
def admin_payments_code_edit(code_id):
    """A code's gift, limits and whether it can be used; a member's referral code gets a gift of its own (PM7)."""
    code = db.session.get(PromoCode, code_id) or abort(404)
    if code.owner_id is not None:
        return _edit_referral_code(code)
    form = GiftCodeForm(obj=code)
    del form.code
    del form.kind   # a code's kind stays what it was made as
    form.offer_id.choices = _tier_choices(none=True)
    if request.method == 'GET':
        if code.expires_at:
            form.expires_at.data = code.expires_at.date()
        if code.amount_minor:
            form.amount.data = from_minor(code.amount_minor, get_setting(CURRENCY_SETTING) or 'usd')
        if code.assigned_to:
            form.user_name.data = db.session.get(User, code.assigned_to).user_name
    if form.validate_on_submit() and _code_refusal(form, code.kind) is None \
            and (member := _member_named(form)) is not _NO_SUCH_MEMBER:
        _apply_code(form, code)
        code.assigned_to = member
        db.session.commit()
        flash(_('Code saved.'))
        return redirect(url_for('admin.admin_payments_codes'))
    owner = db.session.get(User, code.owner_id) if code.owner_id else None
    return render_template('admin/payments_code.html', title=_('Code'), code=code, owner=owner, form=form,
                           roles_with=roles_with('manage monetization'))


def _edit_referral_code(code: PromoCode):
    """A member's referral code: its one benefit, from what its owner's domain offers (R1, R2), and its own limits."""
    owner = db.session.get(User, code.owner_id)
    referral_benefit(code, owner.home_domain())   # one with none of its own, or a gone choice, takes the default (X1)
    db.session.commit()
    form = ReferralCodeForm(obj=code)
    form.benefit.choices = _benefit_choices(owner.home_domain(), own=True)
    if request.method == 'GET':
        form.benefit.data = _choice_of(code.benefit, code.offer_id, code.discount_code_id)
        if code.expires_at:
            form.expires_at.data = code.expires_at.date()
    if form.validate_on_submit():
        kind, _sep, number = form.benefit.data.partition(':')
        code.benefit = kind
        code.offer_id = int(number) if kind == 'gift' else None
        code.days = form.days.data if kind == 'gift' else None
        code.discount_code_id = int(number) if kind == 'discount' else None
        code.max_uses = form.max_uses.data or None
        code.expires_at = datetime.combine(form.expires_at.data, datetime.max.time()).replace(microsecond=0) \
            if form.expires_at.data else None
        code.active = form.active.data
        db.session.commit()
        flash(_('Code saved.'))
        return redirect(url_for('admin.admin_payments_codes'))
    return render_template('admin/payments_referral_code.html', title=_('Code'), code=code, owner=owner, form=form,
                           roles_with=roles_with('manage monetization'))


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


def _save_paypal_offer(form: PaypalOfferForm, offer: Offer) -> bool:
    """A plan's price is PayPal's, so it can't disagree with what it charges. Without one, this is a one-off at the
    amount given. False, with the reason on the form."""
    if not form.active.data and _names_a_paid_domain(offer):
        form.active.errors.append(_('A paid domain charges this at registration. Change the domain first.'))
        return False
    if form.plan_id.data:
        try:
            _apply_price(offer, adapter_for('paypal').describe_price(form.plan_id.data))
        except ProviderError:
            form.plan_id.errors.append(_('PayPal could not find that plan, or it has a trial, a setup fee or added tax.'))
            return False
        except CurrencyRefused:
            form.plan_id.errors.append(_('That plan is in a different currency from the rest of this site.'))
            return False
        offer.kind = 'subscription'
    else:
        code = (form.currency.data or '').strip().upper()
        if form.amount.data is None or code not in list_currencies():
            form.amount.errors.append(_('Give a plan, or an amount and a currency.'))
            return False
        currency = get_setting(CURRENCY_SETTING)
        if currency and currency != code.lower():
            form.currency.errors.append(_('That is a different currency from the rest of this site.'))
            return False
        if not currency:
            set_setting(CURRENCY_SETTING, code.lower())
        offer.amount_minor, offer.currency = to_minor(str(form.amount.data), code), code.lower()
        offer.interval, offer.kind = None, 'donation'
    offer.provider_kind = 'paypal'
    offer.provider_price_id = form.plan_id.data or None
    offer.title = form.title.data
    offer.offer_group = form.offer_group.data
    offer.grants_donor = form.grants_donor.data
    offer.donor_days = None if offer.kind == 'subscription' else form.donor_days.data
    offer.position = form.position.data or 0
    offer.active = form.active.data
    _save_domains(offer, form)
    return True


@bp.route('/payments/paypal/offers/add', methods=['GET', 'POST'])
@login_required
@permission_required('manage monetization')
def admin_payments_paypal_offer_add():
    form = PaypalOfferForm(currency=(get_setting(CURRENCY_SETTING) or '').upper())
    form.domains.choices = _domain_choices()
    if form.validate_on_submit() and _save_paypal_offer(form, Offer()):
        flash(_('Offer saved.'))
        return redirect(url_for('admin.admin_payments'))
    return render_template('admin/payments_paypal_offer.html', title=_('Add PayPal offer'), form=form,
                           roles_with=roles_with('manage monetization'))


def _edit_paypal_offer(offer: Offer):
    form = PaypalOfferForm(obj=offer)
    form.domains.choices = _domain_choices()
    if request.method == 'GET':
        form.plan_id.data = offer.provider_price_id
        form.amount.data = from_minor(offer.amount_minor, offer.currency)
        form.currency.data = offer.currency.upper()
        form.domains.data = [row.site_domain_id for row in OfferDomain.query.filter_by(offer_id=offer.id)]
    if form.validate_on_submit() and _save_paypal_offer(form, offer):
        flash(_('Offer saved.'))
        return redirect(url_for('admin.admin_payments'))
    return render_template('admin/payments_paypal_offer.html', title=_('Edit offer'), form=form, offer=offer,
                           delete_form=FlaskForm(), roles_with=roles_with('manage monetization'))


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
    offer = _editable_offer(offer_id)
    if offer.kind == LINK:
        return _edit_link(offer)
    if offer.provider_kind == 'btcpay':
        return _edit_btcpay_offer(offer)
    if offer.provider_kind == 'paypal':
        return _edit_paypal_offer(offer)
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
    offer = _editable_offer(offer_id)
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
