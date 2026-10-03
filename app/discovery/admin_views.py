"""The admin discovery page (interop D24): Podcast Index credentials, the channel/podcast pre-load
(Task 9 of the plan) and the data-source attribution. Lives on the admin blueprint."""
from flask import flash, redirect, url_for
from flask_babel import _

from app.admin import bp
from app.discovery.castopod import SETTING_KEY, SETTING_SECRET
from app.discovery.forms import PodcastIndexCredentialsForm
from app.utils import get_setting, login_required, permission_required, render_template, roles_with, set_setting


def _credentials_configured() -> bool:
    return bool(get_setting(SETTING_KEY, '') and get_setting(SETTING_SECRET, ''))


@bp.route('/federation/discovery', methods=['GET', 'POST'])
@login_required
@permission_required('change instance settings')
def admin_federation_discovery():
    credentials_form = PodcastIndexCredentialsForm()
    preload_form = None
    candidates = None

    if credentials_form.podcastindex_save.data and credentials_form.validate_on_submit():
        # A blank field means "leave it": the stored values are never sent back to the browser to resubmit
        api_key = (credentials_form.podcastindex_api_key.data or '').strip()
        api_secret = (credentials_form.podcastindex_api_secret.data or '').strip()
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

    return render_template('admin/federation_discovery.html', title=_('Federation settings - discovery'),
                           credentials_form=credentials_form, preload_form=preload_form, candidates=candidates,
                           credentials_configured=_credentials_configured(),
                           roles_with=roles_with('change instance settings'))
