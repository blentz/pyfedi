"""What counts as a video or podcast community (interop D24): one predicate for the Videos & Podcasts feed, the post
search filter and the community browse filter, so the three cannot drift. Imports only models, so the feed and search
modules can use it without a cycle."""
from sqlalchemy import and_, func, or_, select

from app.constants import COMMUNITY_KIND_PODCAST, COMMUNITY_KIND_VIDEO_CHANNEL
from app.discovery import MEDIA_SOFTWARE
from app.models import Community, Instance, Post

_SOFTWARE_LIST = ', '.join(f"'{software}'" for software in MEDIA_SOFTWARE)   # constants, never user input
# This server's own communities have no remote software to read: their kind says what they are, and the platform
# filter files a video channel under PeerTube and a podcast under Castopod.
_OWN_KIND_PLATFORM = {COMMUNITY_KIND_VIDEO_CHANNEL: 'peertube', COMMUNITY_KIND_PODCAST: 'castopod'}
_OWN_KIND_LIST = ', '.join(f"'{kind}'" for kind in _OWN_KIND_PLATFORM)
# home_page builds its community filter as SQL on alias `c`
MEDIA_COMMUNITY_SQL = (f'(c.instance_id IN (SELECT id FROM instance WHERE lower(software) IN ({_SOFTWARE_LIST})) '
                       f'OR (c.ap_id IS NULL AND c.kind IN ({_OWN_KIND_LIST})))')


def _media_instance_ids(platforms=MEDIA_SOFTWARE):
    return select(Instance.id).where(func.lower(Instance.software).in_(platforms))


def _own_community_clause(kinds):
    return and_(Community.ap_id.is_(None), Community.kind.in_(kinds))


def media_community_clause():
    return or_(Community.instance_id.in_(_media_instance_ids()), _own_community_clause(_OWN_KIND_PLATFORM))


def platform_community_clause(platform):
    """One platform's communities (the browse filter); the same instance-id select as media_community_clause."""
    own_kinds = [kind for kind, own_platform in _OWN_KIND_PLATFORM.items() if own_platform == platform]
    return or_(Community.instance_id.in_(_media_instance_ids((platform,))), _own_community_clause(own_kinds))


def media_post_clause():
    return Post.community_id.in_(select(Community.id).where(media_community_clause()))


def platform_of(community):
    instance = community.instance if community.instance_id else None
    software = (instance.software or '').lower() if instance is not None else ''
    return software if software in MEDIA_SOFTWARE else None
