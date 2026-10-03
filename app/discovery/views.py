"""Discovery routes on the main blueprint (interop D24)."""
from flask import abort, flash, redirect, url_for
from flask_babel import _

from app import db
from app.activitypub.util import find_actor_or_create
from app.discovery import KIND_COMMUNITY
from app.discovery.credits import podcast_byline
from app.main import bp
from app.models import Community, DiscoveryEntry, User
from app.utils import login_required


@bp.route('/discovery/<int:entry_id>/resolve', methods=['POST'])
@login_required
def discovery_resolve(entry_id):
    """Fetch a directory entry's actor (find_actor_or_create, with its usual guards) and open its page here,
    where the ordinary Join or Follow button is. This is the first time anything is fetched from it."""
    entry = db.session.get(DiscoveryEntry, entry_id)
    if entry is None:
        abort(404)
    actor = find_actor_or_create(entry.actor_url, community_only=entry.kind == KIND_COMMUNITY)
    if isinstance(actor, Community):
        return redirect(url_for('activitypub.community_profile', actor=actor.link()))
    if isinstance(actor, User):
        return redirect(url_for('activitypub.user_profile', actor=actor.link()))
    flash(_('%(name)s could not be reached. Please try again later.', name=entry.name), 'warning')
    if entry.kind == KIND_COMMUNITY:
        return redirect(url_for('main.list_communities'))
    return redirect(url_for('instance.instance_people', instance_domain='all'))


bp.app_template_global('podcast_byline')(podcast_byline)   # D24: the post byline for a podcast episode
