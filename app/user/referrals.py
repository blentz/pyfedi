"""The user's referral code page (spec docs/superpowers/specs/2026-10-31-referral-codes-design.md)."""
from flask import abort, flash, g, make_response, redirect, url_for
from flask_babel import _
from flask_login import current_user

from app import db
from app.referral import (caps, generate_code, lifetime_referrals, referral_link, referral_qr_png, referral_qr_svg,
                          referrals_available, remaining_signups, uses_of_current_code)
from app.user import bp
from app.utils import login_required, render_template


def _available_or_404():
    if not referrals_available(current_user):
        abort(404)


@bp.route('/user/settings/referrals', methods=['GET'])
@login_required
def user_referrals():
    _available_or_404()
    link = referral_link(current_user) if current_user.referral_code else None
    per_code, per_user = caps()
    code_left, lifetime_left = remaining_signups(current_user)
    return render_template('user/referrals.html', title=_('Referral code'), user=current_user, link=link,
                           qr_svg=referral_qr_svg(link) if link else None,
                           uses=uses_of_current_code(current_user), lifetime=lifetime_referrals(current_user),
                           per_code=per_code, per_user=per_user, code_left=code_left, lifetime_left=lifetime_left,
                           exempt=current_user.is_admin())


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
