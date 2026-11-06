"""/admin/payments: how this instance takes money (spec 2026-11-05-monetization-infrastructure-design.md)."""
from flask_babel import _

from app.admin import bp
from app.utils import login_required, permission_required, render_template, roles_with


@bp.route('/payments', methods=['GET'])
@login_required
@permission_required('manage monetization')
def admin_payments():
    return render_template('admin/payments.html', title=_('Payments'), roles_with=roles_with('manage monetization'))
