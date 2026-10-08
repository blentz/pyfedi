"""Relayed posts nobody here engaged with expire (spec: Expiry)."""
import time
from datetime import timedelta

from flask import current_app
from sqlalchemy import and_, exists, or_, select

from app import celery
from app.constants import POST_STATUS_REVIEWING
from app.models import (Community, CommunityMember, Post, PostBookmark, PostReply, PostVote, Report, User,
                        UserFollower, flush_cdn_cache, utcnow)
from app.utils import get_setting, get_task_session, patch_db_session

RELAY_EXPIRY_BATCH = 500
RELAY_EXPIRY_SECONDS = 600


def relay_retention_days() -> int:
    value = get_setting('relay_retention_days', 7)
    try:
        return int(value)
    except (TypeError, ValueError):
        current_app.logger.warning(f'relays: relay_retention_days is {value!r}, not a number; using 7')
        return 7


def _local_user_ids():
    """Ids of local users, as User.is_local() defines them."""
    return select(User.id).where(or_(User.ap_id.is_(None),
                                     User.ap_profile_id.startswith(current_app.config['SERVER_URL'], autoescape=True)))


def _kept_clause():
    """A post that someone here engaged with, or that must not vanish."""
    local = _local_user_ids()
    return or_(
        Post.sticky.is_(True),
        Post.status <= POST_STATUS_REVIEWING,
        exists().where(PostVote.post_id == Post.id, PostVote.user_id.in_(local)),
        exists().where(PostReply.post_id == Post.id, PostReply.user_id.in_(local)),
        exists().where(PostBookmark.post_id == Post.id),
        exists().where(Report.suspect_post_id == Post.id),
        exists().where(UserFollower.remote_user_id == Post.user_id, UserFollower.is_inward.is_(False),
                       UserFollower.local_user_id.in_(local)),
        exists().where(CommunityMember.community_id == Post.community_id, CommunityMember.user_id.in_(local),
                       Community.id == Post.community_id, Community.name != 'microblogs'),
    )


@celery.task
def expire_relayed_posts():
    days = relay_retention_days()
    if days <= 0:
        return {'deleted': 0, 'kept': 0, 'failed': 0}
    session = get_task_session()
    deleted = failed = 0
    cache_urls = []
    try:
        with patch_db_session(session):
            # arrival time: a post that arrived late with an old date is not expired the moment it lands
            old = and_(Post.relay_id.isnot(None), Post.created_at < utcnow() - timedelta(days=days))
            kept_clause = _kept_clause()
            kept = session.query(Post.id).filter(old, kept_clause).count()
            deadline = time.monotonic() + RELAY_EXPIRY_SECONDS
            last_id = 0   # ids ascend, so a post that failed in this run is never selected again
            while time.monotonic() < deadline:
                batch = session.query(Post).filter(old, ~kept_clause, Post.id > last_id).order_by(Post.id) \
                    .limit(RELAY_EXPIRY_BATCH).all()
                if not batch:
                    break
                last_id = batch[-1].id
                for post in batch:
                    post_id = post.id
                    try:
                        with session.begin_nested():
                            post.delete_dependencies(cache_urls=cache_urls)
                            session.delete(post)
                        deleted += 1
                    except Exception as e:
                        failed += 1
                        current_app.logger.warning(f'relays: could not expire post {post_id}: {type(e).__name__}')
                session.commit()
            if cache_urls:
                flush_cdn_cache(cache_urls)
        current_app.logger.info(f'relays: expired {deleted} relayed posts, kept {kept}, failed {failed}')
        return {'deleted': deleted, 'kept': kept, 'failed': failed}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
