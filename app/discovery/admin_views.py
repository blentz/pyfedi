"""The admin discovery page (interop D24): proactive sync settings, the sync status, and the data-source
attribution. Lives on the admin blueprint."""
from flask import current_app, flash, redirect, request, url_for
from flask_babel import _

from app import db
from app.admin import bp
from app.discovery.forms import DiscoverySyncForm, DiscoverySyncNowForm
from app.discovery.sync import reconcile_sync_task, sync_per_host, sync_platforms
from app.models import Community, DiscoverySync
from app.utils import get_setting, login_required, permission_required, render_template, roles_with, set_setting


@bp.route('/federation/discovery', methods=['GET', 'POST'])
@login_required
@permission_required('change instance settings')
def admin_federation_discovery():
    sync_form, now_form = DiscoverySyncForm(), DiscoverySyncNowForm()
    if request.method == 'POST' and now_form.sync_now.data and now_form.validate_on_submit():
        if current_app.debug:
            reconcile_sync_task()
        else:
            reconcile_sync_task.delay()
        flash(_('Sync started in the background.'))
        return redirect(url_for('admin.admin_federation_discovery'))
    if request.method == 'POST' and sync_form.sync_save.data and sync_form.validate_on_submit():
        set_setting('discovery_sync_per_host', sync_form.sync_per_host.data)
        set_setting('discovery_sync_platforms', sync_form.sync_platforms.data or [])
        set_setting('discovery_external_search', bool(sync_form.sync_external_search.data))
        flash(_('Discovery settings saved. They take effect at the next sync.'))
        return redirect(url_for('admin.admin_federation_discovery'))
    if request.method == 'GET':
        sync_form.sync_per_host.data = sync_per_host()
        sync_form.sync_platforms.data = sync_platforms()
        sync_form.sync_external_search.data = get_setting('discovery_external_search', True)
    synced = db.session.query(DiscoverySync, Community).join(Community, Community.id == DiscoverySync.community_id) \
        .order_by(Community.ap_domain, Community.name).all()
    return render_template('admin/federation_discovery.html', title=_('Federation settings - discovery'),
                           sync_form=sync_form, now_form=now_form, synced=synced,
                           roles_with=roles_with('change instance settings'))
