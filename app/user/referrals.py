"""The user's referral code page (spec docs/superpowers/specs/2026-10-31-referral-codes-design.md)."""
from flask import abort, flash, make_response, redirect, url_for
from flask_babel import _
from flask_login import current_user

from app import db
from app.models import Offer, PromoCode, PromoCodeUse
from app.payments.discounts import DISCOUNT_KINDS, usable as discount_usable
from app.payments.gifts import Benefit, default_referral_benefit, deletable as gift_deletable, delete_gift_code, \
    join_domain, referral_gift_code, usable as gift_usable
from app.payments.offers import money, period_text
from app.permissions import user_access
from app.referral import (assigned_codes, benefit_on, caps, generate_code, lifetime_referrals, referral_domain,
                          referral_link, referral_page_available, referral_qr_png, referral_qr_svg, referrals_available,
                          remaining_signups, uses_of_current_code)
from app.storefront import domain_base_url, paid_registration
from app.user import bp
from app.utils import get_setting, login_required, render_template


def _available_or_404():
    if not referrals_available(current_user):
        abort(404)


@bp.route('/user/settings/referrals', methods=['GET'])
@login_required
def user_referrals():
    if not referral_page_available(current_user):
        abort(404)
    assigned = [_assigned(row) for row in assigned_codes(current_user)]
    if not referrals_available(current_user):   # only codes made for them to hand out
        db.session.commit()
        return render_template('user/referrals.html', title=_('Referral code'), user=current_user, own=False,
                               assigned=assigned)
    # The domain the link registers people on: this one, where a code makes the account (RC9, RC10 2026-11-19)
    domain = referral_domain(current_user)
    link = referral_link(current_user, domain) if current_user.referral_code else None
    charges = paid_registration(domain)
    benefit = None
    if charges:   # what joining there with it gives: its own gift or discount, or a new code's (plan 2026-11-16 X1)
        row = referral_gift_code(current_user)
        benefit = benefit_on(current_user, row, domain) if row is not None else default_referral_benefit(domain)
        db.session.commit()
    per_code, per_user = caps()
    code_left, lifetime_left = remaining_signups(current_user)
    return render_template('user/referrals.html', title=_('Referral code'), user=current_user, own=True, link=link,
                           domain=domain.domain,
                           assigned=assigned,
                           qr_svg=referral_qr_svg(link) if link else None,
                           uses=uses_of_current_code(current_user), lifetime=lifetime_referrals(current_user),
                           per_code=per_code, per_user=per_user, code_left=code_left, lifetime_left=lifetime_left,
                           exempt=user_access('exempt from account limits', current_user.id), charges=charges,
                           gives=_gives(benefit) if charges else None)


def _assigned_link(row: PromoCode) -> str | None:
    """Where a code made for the member is used to join: /register on the domain they are on when the code admits
    there, else on one it does (`join_domain`, owner 2026-11-20). None when it admits nowhere: a member enters it on
    their Subscription page instead."""
    domain = join_domain(row)
    return domain_base_url(domain) + url_for('auth.register', code=row.code) if domain is not None else None


def _assigned(row: PromoCode) -> dict:
    """One code made for the member to hand out, as their referral page shows it (owner 2026-11-16)."""
    if row.kind in DISCOUNT_KINDS:
        gives, can_be_used = _gives(Benefit(discount=row)), discount_usable(row)
    else:
        offer = db.session.get(Offer, row.offer_id) if row.offer_id else None
        gives = _gives(Benefit(offer=offer, periods=row.periods)) if offer is not None and row.periods else None
        can_be_used = gift_usable(row)
    link = _assigned_link(row)
    uses = db.session.query(PromoCodeUse).filter_by(promo_code_id=row.id).count()
    return {'row': row, 'gives': gives, 'usable': can_be_used, 'uses': uses, 'link': link,
            'qr_svg': referral_qr_svg(link) if link else None, 'deletable': gift_deletable(row)}


def _gives(benefit) -> str | None:
    """What joining with a member's code gives, in words; None for nothing it can give."""
    if benefit is None:
        return None
    if benefit.offer is not None:
        return _('%(length)s of %(tier)s, free', length=period_text(benefit.periods, benefit.offer.interval),
                 tier=benefit.offer.title)
    discount = benefit.discount
    tier = db.session.get(Offer, discount.offer_id) if discount.offer_id else None
    off = _('%(percent)d%% off', percent=discount.percent) if discount.kind == 'percent' \
        else _('%(amount)s off', amount=money(discount.amount_minor, get_setting('payment_currency') or 'usd'))
    return _('%(off)s their first payment for %(tier)s', off=off, tier=tier.title) if tier is not None \
        else _('%(off)s their first payment for any plan', off=off)


@bp.route('/user/settings/referrals/generate', methods=['POST'])
@login_required
def user_referrals_generate():
    _available_or_404()
    regenerating = bool(current_user.referral_code)
    generate_code(current_user)
    db.session.commit()
    flash(_('Your new referral code is ready. Links and QR codes for the old one no longer work.') if regenerating
          else _('Your referral code is ready.'))
    return redirect(url_for('user.user_referrals'))


@bp.route('/user/settings/referrals/qr.png', methods=['GET'])
@login_required
def user_referrals_qr_png():
    _available_or_404()
    if not current_user.referral_code:
        abort(404)
    response = make_response(referral_qr_png(referral_link(current_user)))
    response.headers['Content-Type'] = 'image/png'
    response.headers['Content-Disposition'] = 'attachment; filename="referral-qr.png"'
    response.headers['Cache-Control'] = 'no-store'
    return response


@bp.route('/user/settings/referrals/codes/<int:code_id>/qr.png', methods=['GET'])
@login_required
def user_assigned_code_qr_png(code_id):
    """The QR code of a code made for the member, to join with (owner 2026-11-16)."""
    row = next((row for row in assigned_codes(current_user) if row.id == code_id), None)
    link = _assigned_link(row) if row is not None else None
    if link is None:
        abort(404)
    response = make_response(referral_qr_png(link))
    response.headers['Content-Type'] = 'image/png'
    response.headers['Content-Disposition'] = f'attachment; filename="code-{row.id}-qr.png"'
    response.headers['Cache-Control'] = 'no-store'
    return response


@bp.route('/user/settings/referrals/codes/<int:code_id>/delete', methods=['POST'])
@login_required
def user_assigned_code_delete(code_id):
    """Delete a gift code an admin made for the member to hand out (owner 2026-11-19). Whoever used it keeps what it
    gave. Their referral page, if it has nothing left to show, gives way to their settings."""
    row = next((row for row in assigned_codes(current_user) if row.id == code_id), None)
    if not gift_deletable(row):
        abort(404)
    text = row.code
    delete_gift_code(row)
    db.session.commit()
    flash(_('Code %(code)s deleted.', code=text))
    return redirect(url_for('user.user_referrals') if referral_page_available(current_user)
                    else url_for('user.user_settings'))
