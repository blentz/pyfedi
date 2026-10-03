"""The discovery fallback for community and people search (interop D24, goal (b)): used only when
nothing local matches. NSFW entries need the viewer's permission; a host banned since the last refresh
is hidden."""
from sqlalchemy import or_

from app import db
from app.models import DiscoveryEntry
from app.utils import instance_banned

FALLBACK_LIMIT = 20


def _contains_pattern(q: str) -> str:
    escaped = q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return f'%{escaped}%'


def discovery_fallback(kind: str, q: str, allow_nsfw: bool, limit: int = FALLBACK_LIMIT) -> list:
    q = (q or '').strip()
    if not q:
        return []
    pattern = _contains_pattern(q)
    query = db.session.query(DiscoveryEntry).filter(
        DiscoveryEntry.kind == kind,
        or_(DiscoveryEntry.name.ilike(pattern, escape='\\'), DiscoveryEntry.actor_url.ilike(pattern, escape='\\')))
    if not allow_nsfw:
        query = query.filter(DiscoveryEntry.nsfw == False)
    found = []
    for entry in query.order_by(DiscoveryEntry.followers.desc(), DiscoveryEntry.name).limit(limit * 2):
        if instance_banned(entry.host):
            continue
        found.append(entry)
        if len(found) >= limit:
            break
    return found


def viewer_allows_nsfw(user, site) -> bool:
    """The community-search rule, for a page that has no NSFW selector of its own."""
    return bool(site is not None and getattr(site, 'enable_nsfw', False) and user is not None
                and user.is_authenticated and getattr(user, 'hide_nsfw', 1) != 1)
