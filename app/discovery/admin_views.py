"""The admin discovery page (interop D24): the channel/podcast pre-load
(Task 9 of the plan) and the data-source attribution. Lives on the admin blueprint."""
from flask import current_app, flash, redirect, url_for
from flask_babel import _, ngettext

from app.admin import bp
from app.discovery.forms import DiscoveryPreloadForm
from app.discovery.preload import PRELOAD_USER_ID, preload_candidates, preload_discovered_communities, \
    preload_user_can_subscribe
from app.utils import login_required, permission_required, render_template, roles_with


@bp.route('/federation/discovery', methods=['GET', 'POST'])
@login_required
@permission_required('change instance settings')
def admin_federation_discovery():
    preload_form = DiscoveryPreloadForm()
    candidates = None

    if (preload_form.preload_preview.data or preload_form.preload_subscribe.data) and preload_form.validate_on_submit():
        candidates = preload_candidates(preload_form.preload_count.data, preload_form.preload_platforms.data)
        if preload_form.preload_subscribe.data:
            entry_ids = [entry.id for entry in candidates]
            if not entry_ids:
                flash(_('Nothing new to subscribe to.'), 'warning')
                return redirect(url_for('admin.admin_federation_discovery'))
            if not preload_user_can_subscribe(PRELOAD_USER_ID):
                flash(_('User %(id)d, who subscribes for the pre-load, cannot subscribe: the account is missing, '
                        'deleted or banned.', id=PRELOAD_USER_ID), 'error')
                return redirect(url_for('admin.admin_federation_discovery'))
            if current_app.debug:
                preload_discovered_communities(entry_ids, PRELOAD_USER_ID)
            else:
                preload_discovered_communities.delay(entry_ids, PRELOAD_USER_ID)
            flash(ngettext('Subscribing to %(num)d channel or podcast in the background.',
                           'Subscribing to %(num)d channels and podcasts in the background.',
                           len(entry_ids)))
            return redirect(url_for('admin.admin_federation_discovery'))

    return render_template('admin/federation_discovery.html', title=_('Federation settings - discovery'),
                           preload_form=preload_form, candidates=candidates,
                           roles_with=roles_with('change instance settings'))
