"""The Live view of the microblogs community: new posts arrive without a reload, the way
Mastodon's Live Feeds does.

Spec: docs/superpowers/specs/2026-10-07-microblog-live-feed-design.md
"""
from datetime import timedelta

from flask import current_app
from sqlalchemy import desc, func

from app import cache, db
from app.constants import POST_STATUS_REVIEWING, VISIBILITY_PUBLIC
from app.models import Community, Instance, Post, post_stored_hooks
from app import utils
from app.utils import instance_banned, utcnow

from app.discovery import MEDIA_SOFTWARE

LIVE_COMMUNITY = 'microblogs'
LIVE_KEY_PATTERN = r'^(any|local|popular|media|community:\d+|instance:\d+)$'
# A backfilled post is old but gets a new, high id; this window keeps it out of the stream
# while still admitting a post that federated in a few minutes late.
LIVE_WINDOW = timedelta(hours=1)
LIVE_LIMIT = 40
HOME_LIVE_FILTERS = ('subscribed', 'local', 'popular', 'media', 'all')


def _software(community) -> str:
    """The community's instance software, lower-cased; '' when the instance row is missing (D1007)."""
    instance = community.instance
    return (instance.software or '').lower() if instance is not None else ''


def is_live_community(community) -> bool:
    """A `microblogs` community that is local, or hosted on another PieFed instance."""
    if community.name != LIVE_COMMUNITY:
        return False
    return community.is_local() or _software(community) == 'piefed'


def is_local_microblogs(community) -> bool:
    """The row find_microblogging_community() returns: the community server views filter."""
    return (community.name == LIVE_COMMUNITY and community.instance_id == 1 and community.user_id == 1
            and community.ap_id is None)


def server_view_instance(host: str):
    """The Instance whose server view is /c/microblogs@<host>, else None (spec: Resolving).
    `host` is lower-cased by the caller."""
    if not host or host == current_app.config['SERVER_NAME'].lower():
        return None
    # A real community always wins: /c/microblogs@piefed.social stays PieFed's own community.
    if db.session.query(Community.id).filter(Community.ap_id == f'{LIVE_COMMUNITY}@{host}').first():
        return None
    instance = db.session.query(Instance).filter(func.lower(Instance.domain) == host).first()
    if instance is None or instance_banned(host):
        return None
    return instance


def microblog_server_view(actor: str):
    """The Instance a `microblogs@<host>` actor names, when it is a server view; else None."""
    name, at, host = actor.strip().lower().rpartition('@')
    if not at or name != LIVE_COMMUNITY:
        return None
    return server_view_instance(host)


@cache.memoize(timeout=300)
def server_view_actor(instance_id: int):
    """'microblogs@<host>' for an instance with a server view, else None. Memoized: post links
    call it once per teaser."""
    instance = db.session.get(Instance, instance_id)
    if instance is None or not instance.domain:
        return None
    host = instance.domain.lower()
    return f'{LIVE_COMMUNITY}@{host}' if server_view_instance(host) is not None else None


def post_community_link(post) -> str:
    """Where a post's community link points: its author's server view for a remote microblog in
    the microblogs community, else the community itself (spec: Discovery)."""
    community = post.community
    if is_local_microblogs(community) and post.instance_id and post.instance_id != 1:
        actor = server_view_actor(post.instance_id)
        if actor:
            return actor
    return community.link()


def live_available(community, user, content_type: str, page: int) -> bool:
    return user.is_authenticated and content_type == 'posts' and page == 1 and is_live_community(community)


def home_live_available(user, view_filter: str, page: int, tag: str) -> bool:
    return user.is_authenticated and view_filter in HOME_LIVE_FILTERS and page == 0 and not tag


def home_live_key(view_filter: str) -> str:
    """Subscribed includes followed authors in any community, so it wakes on every post, as All does."""
    return 'any' if view_filter in ('subscribed', 'all') else view_filter


def live_posts(posts_query, after: int) -> list:
    """Posts from `posts_query` newer than the cursor `after` (a Post.id: local ids only grow,
    unlike a peer's `published`), recent, unpinned, newest first."""
    # More than LIVE_LIMIT new posts: the newest LIVE_LIMIT come back and the cursor moves past
    # the rest (by design: show the latest).
    return posts_query.filter(Post.id > after, Post.posted_at > utcnow() - LIVE_WINDOW,
                              Post.sticky == False).order_by(desc(Post.posted_at)).limit(LIVE_LIMIT).all()


def live_feed_keys(post, community, backfill: bool) -> set:
    """The Live feeds a stored post belongs to (spec Amendment A, "Wake-up keys")."""
    if backfill or post.visibility != VISIBILITY_PUBLIC or post.status <= POST_STATUS_REVIEWING or post.deleted:
        return set()
    keys = {'any'}
    if is_live_community(community):
        keys.add(f'community:{community.id}')
    if community.instance_id == 1 and community.name != LIVE_COMMUNITY:
        keys.add('local')
    if community.show_popular:
        keys.add('popular')
    if _software(community) in MEDIA_SOFTWARE:
        keys.add('media')
    if is_local_microblogs(community) and post.instance_id and post.instance_id != 1:
        keys.add(f'instance:{post.instance_id}')   # its author's server view (spec 2026-10-08)
    return keys


def announce_live_post(post, community, backfill: bool) -> None:
    """Wake every open Live view this post belongs to. Payloads are empty: each viewer then
    fetches its own filtered posts, so a broadcast reveals nothing a filter would hide."""
    if not current_app.config['NOTIF_SERVER']:
        return
    keys = sorted(live_feed_keys(post, community, backfill))
    if not keys:
        return
    try:
        pipe = utils.get_redis_connection().pipeline()
        for key in keys:
            pipe.publish(f'live:{key}', '{}')
        pipe.execute()
    except Exception as exc:
        current_app.logger.warning(f'Live feed wake-up not sent: {exc}')


post_stored_hooks.append(announce_live_post)


SIDEBAR_SERVERS = 10


@cache.memoize(timeout=300)
def busiest_microblog_servers(community_id: int) -> list:
    """(host, posts in the last 24 h) for the servers with the most posts in the microblogs
    community, busiest first, only those with a server view (spec: Discovery)."""
    count = func.count(Post.id)
    rows = (db.session.query(func.lower(Instance.domain), count)
            .join(Instance, Instance.id == Post.instance_id)
            .filter(Post.community_id == community_id, Post.instance_id != 1, Post.deleted == False,
                    Post.posted_at > utcnow() - timedelta(hours=24))
            .group_by(func.lower(Instance.domain))
            .order_by(count.desc(), func.lower(Instance.domain))
            .limit(SIDEBAR_SERVERS * 2)      # headroom for hosts that have no view
            .all())
    return [(host, posts) for host, posts in rows if server_view_instance(host) is not None][:SIDEBAR_SERVERS]
