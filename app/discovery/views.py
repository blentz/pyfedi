"""Discovery routes on the main blueprint (interop D24)."""
from flask import abort, flash, redirect, request, url_for
from flask_babel import _

from app import db
from app.activitypub.util import find_actor_or_create
from app.discovery import KIND_COMMUNITY
from app.discovery.credits import podcast_byline
from app.discovery.filters import host_is_excluded
from app.discovery.sources import is_hostname
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
    if host_is_excluded(entry.host, frozenset()):   # banned (or off the allowlist) since the last refresh
        flash(_('%(name)s is on an instance this site does not federate with.', name=entry.name), 'warning')
        return _back_to_search(entry)
    actor = find_actor_or_create(entry.actor_url, community_only=entry.kind == KIND_COMMUNITY)
    if isinstance(actor, Community):
        return redirect(url_for('activitypub.community_profile', actor=actor.link()))
    if isinstance(actor, User):
        return redirect(url_for('activitypub.user_profile', actor=actor.link()))
    flash(_('%(name)s could not be reached. Please try again later.', name=entry.name), 'warning')
    return _back_to_search(entry)


def _back_to_search(entry):
    """The search the viewer came from, with their search text (the form's `q`) and, for people, the instance whose
    page it was (the form's `instance_domain`, taken only when it is a plain hostname) kept."""
    q = (request.form.get('q') or '').strip() or None
    if entry.kind == KIND_COMMUNITY:
        return redirect(url_for('main.list_communities', search=q))
    instance_domain = request.form.get('instance_domain')
    return redirect(url_for('instance.instance_people',
                            instance_domain=instance_domain if is_hostname(instance_domain) else 'all', q=q))


bp.app_template_global('podcast_byline')(podcast_byline)   # D24: the post byline for a podcast episode
