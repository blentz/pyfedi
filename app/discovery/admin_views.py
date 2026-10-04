"""The admin discovery page (interop D24): Podcast Index credentials, the channel/podcast pre-load
(Task 9 of the plan) and the data-source attribution. Lives on the admin blueprint."""
from flask import current_app, flash, redirect, url_for
from flask_babel import _, ngettext

from app.admin import bp
from app.discovery.castopod import SETTING_KEY, SETTING_SECRET
from app.discovery.forms import DiscoveryPreloadForm, PodcastIndexCredentialsForm
from app.discovery.preload import PRELOAD_USER_ID, preload_candidates, preload_discovered_communities
from app.utils import get_setting, login_required, permission_required, render_template, roles_with, set_setting


def _credentials_status() -> str:
    """Which halves of the pair are stored, never the values."""
    has_key, has_secret = bool(get_setting(SETTING_KEY, '')), bool(get_setting(SETTING_SECRET, ''))
    if has_key and has_secret:
        return _('Status: configured')
    if has_key:
        return _('Status: incomplete: API secret missing')
    if has_secret:
        return _('Status: incomplete: API key missing')
    return _('Status: not set')


@bp.route('/federation/discovery', methods=['GET', 'POST'])
@login_required
@permission_required('change instance settings')
def admin_federation_discovery():
    credentials_form = PodcastIndexCredentialsForm()
    preload_form = DiscoveryPreloadForm()
    candidates = None

    if credentials_form.podcastindex_save.data and credentials_form.validate_on_submit():
        # A blank field means "leave it": the stored values are never sent back to the browser to resubmit.
        # The form has already stripped both.
        api_key = credentials_form.podcastindex_api_key.data or ''
        api_secret = credentials_form.podcastindex_api_secret.data or ''
        if not api_key and not api_secret:
            flash(_('No credentials entered.'), 'warning')
            return redirect(url_for('admin.admin_federation_discovery'))
        if api_key:
            set_setting(SETTING_KEY, api_key)
        if api_secret:
            set_setting(SETTING_SECRET, api_secret)
        flash(_('Podcast Index credentials saved.'))
        return redirect(url_for('admin.admin_federation_discovery'))
    if credentials_form.podcastindex_remove.data and credentials_form.validate_on_submit():
        set_setting(SETTING_KEY, '')
        set_setting(SETTING_SECRET, '')
        flash(_('Podcast Index credentials removed.'))
        return redirect(url_for('admin.admin_federation_discovery'))

    if (preload_form.preload_preview.data or preload_form.preload_subscribe.data) and preload_form.validate_on_submit():
        candidates = preload_candidates(preload_form.preload_count.data, preload_form.preload_platforms.data)
        if preload_form.preload_subscribe.data:
            entry_ids = [entry.id for entry in candidates]
            if current_app.debug:
                preload_discovered_communities(entry_ids, PRELOAD_USER_ID)
            else:
                preload_discovered_communities.delay(entry_ids, PRELOAD_USER_ID)
            flash(ngettext('Subscribing to %(num)d channel or podcast in the background.',
                           'Subscribing to %(num)d channels and podcasts in the background.',
                           len(entry_ids)))
            return redirect(url_for('admin.admin_federation_discovery'))

    return render_template('admin/federation_discovery.html', title=_('Federation settings - discovery'),
                           credentials_form=credentials_form, preload_form=preload_form, candidates=candidates,
                           credentials_status=_credentials_status(),
                           roles_with=roles_with('change instance settings'))
