"""Admin > Connected accounts: which services members may connect (spec 2026-11-13-connected-accounts-design.md §3).

A service is offered only when its app's credentials and CONNECTIONS_SECRETS_KEY are set in the environment; here the
admin switches each such service on or off. Off keeps members' connections but sends nothing.
"""
from flask import abort, flash, redirect, request, url_for
from flask_babel import _

from app.admin import bp
from app.connections import SERVICES, enabled, sealing_available, setting_name
from app.models import SocialConnection
from app import db
from app.utils import get_setting, login_required, permission_required, render_template, set_setting


@bp.route('/connections', methods=['GET', 'POST'])
@login_required
@permission_required('change instance settings')
def admin_connections():
    if request.method == 'POST':   # login_required has checked its CSRF token
        service = SERVICES.get(request.form.get('service', ''))
        if service is None or not service.configured() or not sealing_available():
            abort(400)
        on = request.form.get('on') == '1'
        set_setting(setting_name(service.name), on)
        flash(_('%(service)s is on: members may connect it.', service=service.label) if on
              else _('%(service)s is off: nothing is sent to it.', service=service.label))
        return redirect(url_for('admin.admin_connections'))
    counts = dict(db.session.query(SocialConnection.service, db.func.count(SocialConnection.id))
                  .group_by(SocialConnection.service).all())
    services = [{'service': service, 'configured': service.configured(), 'on': enabled(service),
                 'switched_on': bool(get_setting(setting_name(service.name), False)),
                 'connections': counts.get(service.name, 0)}
                for service in sorted(SERVICES.values(), key=lambda service: service.label)]
    return render_template('admin/connections.html', title=_('Connected accounts'), services=services,
                           sealing=sealing_available())
