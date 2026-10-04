"""The discovery fallback for community and people search (interop D24, goal (b)): used only when
nothing local matches. NSFW entries need the viewer's permission; a host banned since the last refresh
is hidden."""
from sqlalchemy import func, or_

from app import db
from app.discovery.filters import host_is_excluded
from app.models import DiscoveryEntry, Instance
from app.utils import blocked_or_banned_instances

FALLBACK_LIMIT = 20


def _contains_pattern(q: str) -> str:
    escaped = q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return f'%{escaped}%'


def _viewer_blocked_hosts(viewer_id) -> set:
    """Hosts of the instances the viewer blocked or is banned from."""
    instance_ids = blocked_or_banned_instances(viewer_id) if viewer_id else []
    if not instance_ids:
        return set()
    return {d.lower() for (d,) in db.session.query(Instance.domain).filter(Instance.id.in_(instance_ids)) if d}


def discovery_fallback(kind: str, q: str, allow_nsfw: bool, limit: int = FALLBACK_LIMIT, viewer_id: int = None,
                       host: str = None) -> list:
    """Directory entries matching `q`, without the hosts the viewer blocked or is banned from, and through the
    full instance filter (banned instances, allowlist mode, banned Domains). `host` restricts them to one instance.
    The PeerTube isolation list is applied at refresh time (and copied into banned instances by init-db), so it is
    not fetched on a request."""
    q = (q or '').strip()
    if not q:
        return []
    pattern = _contains_pattern(q)
    query = db.session.query(DiscoveryEntry).filter(
        DiscoveryEntry.kind == kind,
        or_(DiscoveryEntry.name.ilike(pattern, escape='\\'), DiscoveryEntry.actor_url.ilike(pattern, escape='\\')))
    if not allow_nsfw:
        query = query.filter(DiscoveryEntry.nsfw == False)
    if host:
        query = query.filter(func.lower(DiscoveryEntry.host) == host.lower())
    blocked = _viewer_blocked_hosts(viewer_id)
    found = []
    for entry in query.order_by(DiscoveryEntry.followers.desc(), DiscoveryEntry.name).limit(limit * 2):
        if (entry.host or '').lower() in blocked or host_is_excluded(entry.host, frozenset()):
            continue
        found.append(entry)
        if len(found) >= limit:
            break
    return found


def viewer_allows_nsfw(user, site) -> bool:
    """The community-search rule, for a page that has no NSFW selector of its own."""
    return bool(site is not None and getattr(site, 'enable_nsfw', False) and user is not None
                and user.is_authenticated and getattr(user, 'hide_nsfw', 1) != 1)
