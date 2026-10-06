"""What counts as a video or podcast community (interop D24): one predicate for the Videos & Podcasts feed, the post
search filter and the community browse filter, so the three cannot drift. Imports only models, so the feed and search
modules can use it without a cycle."""
from sqlalchemy import func, select

from app.discovery import MEDIA_SOFTWARE
from app.models import Community, Instance, Post

_SOFTWARE_LIST = ', '.join(f"'{software}'" for software in MEDIA_SOFTWARE)   # constants, never user input
# home_page builds its community filter as SQL on alias `c`
MEDIA_COMMUNITY_SQL = f'c.instance_id IN (SELECT id FROM instance WHERE lower(software) IN ({_SOFTWARE_LIST}))'


def _media_instance_ids(platforms=MEDIA_SOFTWARE):
    return select(Instance.id).where(func.lower(Instance.software).in_(platforms))


def media_community_clause():
    return Community.instance_id.in_(_media_instance_ids())


def platform_community_clause(platform):
    """One platform's communities (the browse filter); the same instance-id select as media_community_clause."""
    return Community.instance_id.in_(_media_instance_ids((platform,)))


def media_post_clause():
    return Post.community_id.in_(select(Community.id).where(media_community_clause()))


def platform_of(community):
    instance = community.instance if community.instance_id else None
    software = (instance.software or '').lower() if instance is not None else ''
    return software if software in MEDIA_SOFTWARE else None
