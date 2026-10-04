"""The discovery directory section of community and people search (interop D24, goal (b)): shown on the first
page below the local results, leaving out what this server already knows. NSFW entries need the viewer's
permission; a host banned since the last refresh is hidden."""
from sqlalchemy import exists, func, or_

from app import db
from app.discovery import KIND_COMMUNITY
from app.discovery.filters import host_is_excluded
from app.models import Community, DiscoveryEntry, Instance, User
from app.utils import blocked_or_banned_instances

FALLBACK_LIMIT = 20
# Pages of limit*2 rows read while the instance filter drops rows. The filter (wildcard bans, allowlist mode)
# is host_is_excluded's, which SQL cannot repeat exactly, so it runs on each page instead of in the query.
FALLBACK_PAGES = 5


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
    """Directory entries matching `q` that this server does not know yet (no Community, for a community entry, or
    User, for a person, with that actor id), without the hosts the viewer blocked or is banned from, and through the
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
    # Opening an entry creates its row here; from then on it is a local result, so it is not offered twice.
    # ap_profile_id is stored lower-cased (actor_json_to_model), so only the entry side is folded and the index serves.
    known = Community if kind == KIND_COMMUNITY else User
    query = query.filter(~exists().where(known.ap_profile_id == func.lower(DiscoveryEntry.actor_url)))
    if not allow_nsfw:
        query = query.filter(DiscoveryEntry.nsfw == False)
    if host:
        query = query.filter(func.lower(DiscoveryEntry.host) == host.lower())
    blocked = _viewer_blocked_hosts(viewer_id)
    # id breaks ties, so that consecutive pages neither repeat nor skip a row
    query = query.order_by(DiscoveryEntry.followers.desc(), DiscoveryEntry.name, DiscoveryEntry.id)
    page_size = limit * 2
    found = []
    for page in range(FALLBACK_PAGES):
        rows = query.offset(page * page_size).limit(page_size).all()
        for entry in rows:
            if (entry.host or '').lower() in blocked or host_is_excluded(entry.host, frozenset()):
                continue
            found.append(entry)
            if len(found) >= limit:
                return found
        if len(rows) < page_size:
            break
    return found


def viewer_allows_nsfw(user, site) -> bool:
    """The community-search rule, for a page that has no NSFW selector of its own."""
    return bool(site is not None and getattr(site, 'enable_nsfw', False) and user is not None
                and user.is_authenticated and getattr(user, 'hide_nsfw', 1) != 1)
