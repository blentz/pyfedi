"""Relayed posts nobody here engaged with expire (spec: Expiry)."""
from datetime import timedelta

from flask import current_app
from sqlalchemy import and_, exists, or_, select

from app import celery
from app.constants import POST_STATUS_REVIEWING
from app.models import (Community, CommunityMember, Post, PostBookmark, PostReply, PostVote, Report, User,
                        UserFollower, utcnow)
from app.utils import get_setting, get_task_session, patch_db_session

RELAY_EXPIRY_BATCH = 500
RELAY_EXPIRY_MAX = 5000


def relay_retention_days() -> int:
    return int(get_setting('relay_retention_days', 7))


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
        return {'deleted': 0, 'kept': 0}
    session = get_task_session()
    deleted = 0
    try:
        with patch_db_session(session):
            old = and_(Post.relay_id.isnot(None), Post.posted_at < utcnow() - timedelta(days=days))
            kept_clause = _kept_clause()
            kept = session.query(Post.id).filter(old, kept_clause).count()
            doomed = [row[0] for row in session.query(Post.id).filter(old, ~kept_clause).order_by(Post.id)
                      .limit(RELAY_EXPIRY_MAX).all()]
            for start in range(0, len(doomed), RELAY_EXPIRY_BATCH):
                for post in session.query(Post).filter(Post.id.in_(doomed[start:start + RELAY_EXPIRY_BATCH])):
                    post.delete_dependencies()
                    session.delete(post)
                    deleted += 1
                session.commit()
        current_app.logger.info(f'relays: expired {deleted} relayed posts, kept {kept}')
        return {'deleted': deleted, 'kept': kept}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
