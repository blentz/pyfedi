"""Castopod's Podcast actor as a community (interop D24, spec D4 and decision 5).

A podcast actor is a person-like author (G1: it gets a User row, the technical owner, so Post.user_id and
the per-author machinery keep working) and the thing a reader subscribes to (a Community row). Both rows
carry the same ap_profile_id; that column is unique per table, not across tables. Imports only models and
app.discovery.filters (models and app.utils), so app/activitypub/* can import this at module top without
a cycle.
"""
from flask import current_app
from sqlalchemy.exc import IntegrityError

from app import db
from app.discovery.filters import clean_https_url
from app.models import Community, Instance, Site, User, utcnow

RSS_URL_LIMIT = 2048   # Community.rss_url is String(2048)
AP_URL_LIMIT = 255     # Community.ap_outbox_url is String(255)


def _refused(user) -> bool:
    return user is None or user.is_local() or not user.ap_profile_id or user.banned or user.deleted


def podcast_community_for(user) -> Community | None:
    """The Community sharing a remote podcast user's ap_profile_id. None for a local, banned or deleted user,
    and None when that Community is banned: an admin's ban on either row hides the podcast."""
    if _refused(user):
        return None
    return db.session.query(Community).filter(Community.ap_profile_id == user.ap_profile_id,
                                              Community.banned == False).first()


def ensure_podcast_community(user: User, actor_json: dict) -> Community | None:
    """Find or create the Community for a remote podcast user, keeping its rss_url current. None for a local,
    banned or deleted user (no Community is created for them), for an existing Community that is banned,
    and for a `sensitive` podcast on a site with NSFW off."""
    if _refused(user) or not isinstance(actor_json, dict):
        return None
    rss_url = clean_https_url(actor_json.get('rssFeed'), RSS_URL_LIMIT)
    community = db.session.query(Community).filter(Community.ap_profile_id == user.ap_profile_id).first()
    if community is not None:
        if community.banned:
            return None
        if rss_url and community.rss_url != rss_url:
            community.rss_url = rss_url
            db.session.commit()
        return community
    sensitive = actor_json.get('sensitive') is True
    if sensitive:
        site = db.session.get(Site, 1)   # not g.site: this runs from Celery too
        if site is None or not site.enable_nsfw:
            return None
    instance = db.session.get(Instance, user.instance_id) if user.instance_id else None
    community = Community(name=user.user_name, title=user.title or user.user_name,
                          description=user.about or '', description_html=user.about_html or '',
                          nsfw=sensitive, user_id=user.id, instance_id=user.instance_id,
                          ap_id=user.ap_id, ap_profile_id=user.ap_profile_id, ap_public_url=user.ap_public_url,
                          ap_followers_url=user.ap_followers_url, ap_inbox_url=user.ap_inbox_url,
                          ap_outbox_url=clean_https_url(actor_json.get('outbox'), AP_URL_LIMIT),
                          ap_preferred_username=user.ap_preferred_username, ap_domain=user.ap_domain,
                          public_key=user.public_key, rss_url=rss_url, ap_fetched_at=utcnow(),
                          created_at=user.created or utcnow(), last_active=utcnow(), first_federated_at=utcnow(),
                          content_retention=current_app.config['DEFAULT_CONTENT_RETENTION'],
                          default_post_type='link', subscriptions_count=0,
                          show_popular=instance.popular if instance else True,
                          show_all=not instance.silenced if instance else True)
    db.session.add(community)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return podcast_community_for(user)
    return community
