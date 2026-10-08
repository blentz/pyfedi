"""The admin relays page (spec: Admin and CLI). Lives on the admin blueprint."""
from datetime import timedelta

from flask import abort, flash, g, redirect, request, url_for
from flask_babel import _
from sqlalchemy import func

from app import db
from app.admin import bp
from app.constants import ALLOWLIST_STRONG
from app.models import Post, Relay, utcnow
from app.relays.expiry import relay_retention_days
from app.relays.forms import RelayActionForm, RelayAddForm, RelaySettingsForm
from app.relays.subscribe import RelayError, add_relay, remove_relay, retry_relay
from app.utils import login_required, permission_required, render_template, set_setting


@bp.route('/federation/relays', methods=['GET', 'POST'])
@login_required
@permission_required('change instance settings')
def admin_federation_relays():
    add_form, action_form, settings_form = RelayAddForm(), RelayActionForm(), RelaySettingsForm()
    if request.method == 'POST':
        if add_form.relay_add.data and add_form.validate_on_submit():
            try:
                relay = add_relay(add_form.relay_url.data)
                flash(_('Subscribed to %(url)s; waiting for it to accept.', url=relay.url))
            except RelayError as error:
                flash(str(error), 'error')
        elif action_form.relay_id.data and action_form.validate_on_submit():
            raw_id = action_form.relay_id.data
            relay = (db.session.get(Relay, int(raw_id)) if raw_id.isascii() and raw_id.isdigit() else None) \
                or abort(404)
            if action_form.relay_remove.data:
                remove_relay(relay)
                flash(_('Relay removed.'))
            else:
                try:
                    retry_relay(relay)
                    flash(_('Subscription request sent again.'))
                except RelayError as error:
                    flash(str(error), 'error')
        elif settings_form.relay_settings_save.data and settings_form.validate_on_submit():
            set_setting('relay_retention_days', settings_form.relay_retention.data)
            flash(_('Relay settings saved.'))
        else:
            for form in (add_form, settings_form):
                for errors in form.errors.values():
                    for error in errors:
                        flash(str(error), 'error')
        return redirect(url_for('admin.admin_federation_relays'))
    settings_form.relay_retention.data = relay_retention_days()
    since = utcnow() - timedelta(hours=24)
    counts = dict(db.session.query(Post.relay_id, func.count(Post.id)).filter(
        Post.relay_id.isnot(None), Post.created_at > since).group_by(Post.relay_id).all())
    relays = db.session.query(Relay).order_by(Relay.created_at).all()
    return render_template('admin/federation_relays.html', title=_('Federation settings - relays'),
                           relays=relays, counts=counts, add_form=add_form, action_form=action_form,
                           settings_form=settings_form,
                           allowlist_note=g.site.allowlist_mode >= ALLOWLIST_STRONG)
