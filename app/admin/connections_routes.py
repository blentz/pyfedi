"""Admin > Connected accounts (spec 2026-11-13-connected-accounts-design.md §3, §18).

The instance's page switches each service on or off for every domain that follows it, and lists the callback
addresses each domain using the instance's apps needs registered in them (C9). A domain's page, for holders of
`manage domain connected accounts` on that domain (C8), sets the domain's own apps, which its members use instead,
and its own switches (C12). A domain's new app, or its removal, ends the connections made with another app (C11).
"""
from flask import abort, current_app, flash, redirect, request, url_for
from flask_babel import _, lazy_gettext as _l
from flask_login import current_user

from app import db, storefront
from app.admin import bp
from app.connections import SERVICES, sealing_available, setting_name
from app.connections.apps import (KINDS, app_for, domain_app, domain_switch, enabled, for_domain, instance_app,
                                  remove_domain_app, save_domain_app, set_domain_switch)
from app.connections import bluesky_hosted as hosted
from app.connections.bluesky import key_id_of, new_key_jwk
from app.connections.dispatch import reconnect_domain
from app.models import SiteDomain, SocialConnection
from app.utils import get_setting, login_required, permission_required, render_template, set_setting, user_access

KIND_LABELS = {'facebook': _l('Meta app (Facebook Pages, and Instagram through Facebook)'),
               'instagram': _l('Instagram app (Instagram Login)'), 'youtube': _l('Google client (YouTube)'),
               'bluesky': _l('Bluesky key')}
CALLBACK_SERVICES = {'facebook': ('facebook', 'instagram'), 'instagram': ('instagram',), 'youtube': ('youtube',)}
MAX_FIELD = 255


def callbacks(domain, kind: str) -> list[str]:
    """The redirect addresses `kind`'s app must have registered for this domain's members (C9)."""
    base = storefront.domain_base_url(domain)
    return [f'{base}/user/settings/connections/{service}/callback' for service in CALLBACK_SERVICES.get(kind, ())]


@bp.route('/connections', methods=['GET', 'POST'])
@login_required
@permission_required('change instance settings')
def admin_connections():
    if request.method == 'POST' and request.form.get('action') in HOSTING_ACTIONS:
        _hosting_action(request.form['action'])
        return redirect(url_for('admin.admin_connections'))
    if request.method == 'POST':   # login_required has checked its CSRF token
        service = SERVICES.get(request.form.get('service', ''))
        if service is None or not sealing_available():
            abort(400)
        with for_domain(None):
            if not service.configured():
                abort(400)
        on = request.form.get('on') == '1'
        set_setting(setting_name(service.name), on)
        flash(_('%(service)s is on: members may connect it.', service=service.label) if on
              else _('%(service)s is off: nothing is sent to it.', service=service.label))
        return redirect(url_for('admin.admin_connections'))
    counts = dict(db.session.query(SocialConnection.service, db.func.count(SocialConnection.id))
                  .group_by(SocialConnection.service).all())
    services = []
    for service in sorted(SERVICES.values(), key=lambda service: service.label):
        with for_domain(None):
            configured = service.configured()
        services.append({'service': service, 'configured': configured, 'on': enabled(service),
                         'switched_on': bool(get_setting(setting_name(service.name), False)),
                         'connections': counts.get(service.name, 0)})
    registrations = []   # C9: every domain using an instance app needs its addresses in that app
    for domain in storefront.domain_rows():
        for kind in CALLBACK_SERVICES:
            if instance_app(kind) is not None and (domain.is_default or domain_app(kind, domain.id) is None):
                registrations.append((domain.domain, KIND_LABELS[kind], callbacks(domain, kind)))
    return render_template('admin/connections.html', title=_('Connected accounts'), services=services,
                           sealing=sealing_available(), registrations=registrations, hosting=_hosting_summary())


HOSTING_ACTIONS = ('hosting', 'grant', 'pin', 'unpin')


def _hosting_summary() -> dict | None:
    """Bluesky accounts hosted here (ATProto spec §9, §10): the switch, the slots, the waiting list and the active
    accounts. None when the PDS is not set."""
    if not hosted.configured():
        return None
    return {'on': bool(get_setting(setting_name(hosted.HOSTING), False)), 'domains': hosted.handle_domains(),
            'active': hosted.active_count(), 'max': current_app.config['ATPROTO_MAX_ACCOUNTS'],
            'accounts': hosted.account_count(), 'limit': current_app.config['ATPROTO_HOSTED_LIMIT'],
            'policy': current_app.config['ATPROTO_SLOT_POLICY'], 'waiting': hosted.waiting_list(),
            'active_rows': hosted.hosted_rows().filter_by(slot_state=hosted.ACTIVE)
            .order_by(SocialConnection.id).all(), 'followers': hosted.followers}


def _hosting_action(action: str) -> None:
    if not hosted.configured() or not sealing_available():
        abort(400)
    if action == 'hosting':
        on = request.form.get('on') == '1'
        set_setting(setting_name(hosted.HOSTING), on)
        flash(_('Members can create Bluesky accounts here.') if on
              else _('Members can no longer create Bluesky accounts here. Those made already keep working.'))
        return
    connection = db.session.get(SocialConnection, request.form.get('connection_id', 0, type=int))
    if connection is None or connection.variant != hosted.HOSTED:
        abort(404)
    if action == 'grant':
        if connection.slot_state != hosted.WAITING:
            abort(400)
        if hosted.grant(connection):
            connection.slot_pinned = True   # granted by hand: kept whatever the policy
            db.session.commit()
            flash(_('@%(handle)s has a place now.', handle=connection.remote_name))
        else:
            flash(_('Bluesky could not make that account just now: try again later.'), 'error')
        return
    if connection.slot_state != hosted.ACTIVE:
        abort(400)
    connection.slot_pinned = action == 'pin'
    db.session.commit()
    flash(_('Saved.'))


def _domain_or_404(domain_id: int) -> SiteDomain:
    row = db.session.get(SiteDomain, domain_id) or abort(404)
    if not user_access('manage domain connected accounts', current_user.get_id(), on=row):
        abort(403)
    return row


@bp.route('/domains/<int:domain_id>/connections', methods=['GET', 'POST'])
@login_required
def admin_domain_connections(domain_id):
    row = _domain_or_404(domain_id)
    if row.is_default:
        return redirect(url_for('admin.admin_connections'))
    domain = storefront.domain_by_id(row.id)
    if request.method == 'POST':
        if not sealing_available():
            abort(400)
        action = request.form.get('action', '')
        if action == 'switch':
            _save_switch(row.id)
        elif action == 'hosting' and _domain_hosting(domain) is not None:
            value = request.form.get('value') or None
            if value not in (None, 'on', 'off'):
                abort(400)
            set_domain_switch(hosted.HOSTING, row.id, value)
            flash(_('Saved.'))
        elif action in ('save', 'remove', 'make-key') and request.form.get('kind') in KINDS:
            _save_app(row.id, request.form['kind'], action)
        else:
            abort(400)
        return redirect(url_for('admin.admin_domain_connections', domain_id=row.id))
    apps = []
    for kind in KINDS:
        own = domain_app(kind, row.id)
        apps.append({'kind': kind, 'label': KIND_LABELS[kind], 'own': own, 'instance': instance_app(kind) is not None,
                     'callbacks': callbacks(domain, kind),
                     'daily_uploads': own.get('daily_uploads') if own is not None else None})
    switches = []
    for service in sorted(SERVICES.values(), key=lambda service: service.label):
        with for_domain(row.id):
            configured = service.configured()
        switches.append({'service': service, 'own': domain_switch(service.name, row.id), 'configured': configured,
                         'on': enabled(service, row.id),
                         'instance_on': bool(get_setting(setting_name(service.name), False))})
    return render_template('admin/domain_connections.html', title=_('Connected accounts: %(domain)s', domain=row.domain),
                           row=row, apps=apps, switches=switches, sealing=sealing_available(),
                           hosting=_domain_hosting(domain),
                           bluesky_metadata=f'{storefront.domain_base_url(domain)}/connections/bluesky/client-metadata.json')


def _domain_hosting(domain) -> dict | None:
    """The domain's "Bluesky accounts on this domain" switch (ATProto spec §10): offered when the PDS is set and gives
    handles under this domain, which federates and is no onion."""
    if not hosted.configured() or not domain.federates or domain.is_onion or domain.domain not in hosted.handle_domains():
        return None
    return {'own': domain_switch(hosted.HOSTING, domain.id), 'instance_on': bool(get_setting(setting_name(hosted.HOSTING),
                                                                                            False)),
            'on': hosted.hosting_on(domain.id)}


def _save_switch(domain_id: int) -> None:
    service = SERVICES.get(request.form.get('service', ''))
    value = request.form.get('value') or None
    if service is None or value not in (None, 'on', 'off'):
        abort(400)
    if value == 'on':
        with for_domain(domain_id):
            if not service.configured():   # C12: on only with an app, the domain's own or the instance's
                abort(400)
    set_domain_switch(service.name, domain_id, value)
    flash(_('%(service)s: saved.', service=service.label))


def _save_app(domain_id: int, kind: str, action: str) -> None:
    if action == 'remove':
        remove_domain_app(domain_id, kind)
        db.session.commit()
        flash(_('This domain uses the instance\'s app again, where there is one.'))
    elif kind == 'bluesky':
        if action != 'make-key':
            abort(400)
        key = new_key_jwk()
        save_domain_app(domain_id, kind, key_id_of(key), {'key': key})
        db.session.commit()
        flash(_('This domain has its own Bluesky key now.'))
    else:
        if action != 'save':
            abort(400)
        client_id = request.form.get('client_id', '').strip()[:MAX_FIELD]
        secret = request.form.get('client_secret', '').strip()[:MAX_FIELD]
        if not client_id:
            abort(400)
        own = domain_app(kind, domain_id)
        if not secret:   # left blank: the stored secret stays, for the same app only
            if own is None or own.client_id != client_id:
                flash(_('Give the app secret too.'), 'error')
                return
            secret = own.get('client_secret')
        values = {'client_secret': secret}
        if kind == 'youtube':
            daily = request.form.get('daily_uploads', '').strip()
            if daily:
                if not daily.isdigit():
                    abort(400)
                values['daily_uploads'] = int(daily)
        save_domain_app(domain_id, kind, client_id, values)
        db.session.commit()
        flash(_('Saved.'))
    ended = reconnect_domain(domain_id, kind, app_for(kind, domain_id))
    if ended:
        flash(_('%(count)d connections were made with another app: their members are asked to connect again.',
                count=ended), 'warning')
