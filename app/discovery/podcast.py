"""Castopod's Podcast actor as a community (interop D24, spec D4 and decision 5).

A podcast actor is a person-like author (G1: it gets a User row, the technical owner, so Post.user_id and
the per-author machinery keep working) and the thing a reader subscribes to (a Community row). Both rows
carry the same ap_profile_id; that column is unique per table, not across tables. Imports only models and
app.discovery.filters (models and app.utils), so app/activitypub/* can import this at module top without
a cycle.
"""
from flask import current_app
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import object_session

from app import db
from app.discovery.filters import clean_https_url
from app.models import Community, CommunityJoinRequest, Instance, Site, User, utcnow

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


def podcast_twin_user(community) -> User | None:
    """The podcast User sharing this Community's ap_profile_id, when the Community is a podcast twin; else None.
    Remote Group communities carry no user_id, so they pay no query."""
    if community is None or not community.user_id or not community.ap_profile_id:
        return None
    owner = db.session.get(User, community.user_id)
    return owner if owner is not None and owner.ap_profile_id == community.ap_profile_id else None


def podcast_person_follow_target(community, requestor) -> User | None:
    """R2: an Accept/Reject from a podcast that answers a follow of the podcast as a PERSON. The twin User, when
    `community` is a podcast twin and `requestor` has no join request for it; else None (the community path)."""
    twin = podcast_twin_user(community)
    if twin is None or requestor is None:
        return None
    joining = db.session.query(CommunityJoinRequest).filter_by(user_id=requestor.id,
                                                               community_id=community.id).first()
    return None if joining else twin


def podcast_twin_named_by_other(community, author) -> bool:
    """R2: True when `community` is a podcast twin found in an activity's addressing and the activity's VERIFIED
    author -- the signed sender the inbox resolved (a User), or the actor id of an object fetched from its own
    host -- is not the podcast itself: a third party mentioning the podcast as a person keeps the microblogs
    routing. The activity's own actor/attributedTo fields are never consulted: anyone can write the podcast's id
    there. No verified author counts as someone else."""
    twin = podcast_twin_user(community)
    if twin is None:
        return False
    author_id = author.ap_profile_id if isinstance(author, User) else author
    return not (isinstance(author_id, str) and author_id.lower() == twin.ap_profile_id.lower())


def podcast_route_for(user) -> Community | None | bool:
    """Where a top-level Note from `user` with no community goes: the podcast's Community; None when the user has
    no Community twin at all (a person, so the caller keeps its microblogs routing); False when a twin exists but
    is banned or its User is banned or deleted (the caller drops the Note rather than leak it into microblogs)."""
    if user is None or user.is_local() or not user.ap_profile_id:
        return None
    twin = db.session.query(Community).filter(Community.ap_profile_id == user.ap_profile_id).first()
    if twin is None:
        return None
    if twin.banned or user.banned or user.deleted:
        return False
    return twin


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


def sync_podcast_twin_ban(user, banned=None, deleted=None) -> None:
    """R3: mirror a remote podcast User's ban or deletion onto its Community twin (banned = banned or deleted), so
    every community read path hides it without a check of its own. An unban mirrors back. `banned`/`deleted` are
    the values being assigned when called from the attribute events below, which fire before the assignment."""
    session = object_session(user)
    if session is None or user.ap_id is None or not user.ap_profile_id or user.id is None:
        return
    banned = user.banned if banned is None else banned
    deleted = user.deleted if deleted is None else deleted
    with session.no_autoflush:
        twin = session.query(Community).filter(Community.ap_profile_id == user.ap_profile_id,
                                               Community.user_id == user.id).first()
    if twin is not None:
        twin.banned = bool(banned or deleted)


# Attribute events rather than a call at each site: a remote User is banned or deleted from ~10 ORM sites (admin
# ban/delete, actor Delete, refresh finding the actor gone, purge, Block from a site admin, unban), and these
# catch all of them, plus any added later, with no seam in upstream files. active_history loads the old value so
# an assignment that changes nothing (an unban of an unbanned user) leaves the twin alone. Raw-SQL writes bypass
# the ORM: admin delete bans through the ORM first, and the ban-expiry task calls
# unban_podcast_twins_of_expired_bans below.
@event.listens_for(User.banned, 'set', active_history=True)
def _user_banned_set(target, value, oldvalue, initiator):
    if bool(value) != bool(oldvalue):
        sync_podcast_twin_ban(target, banned=value)


@event.listens_for(User.deleted, 'set', active_history=True)
def _user_deleted_set(target, value, oldvalue, initiator):
    if bool(value) != bool(oldvalue):
        sync_podcast_twin_ban(target, deleted=value)


def unban_podcast_twins_of_expired_bans(session, cutoff) -> None:
    """R3 for the raw-SQL ban-expiry task: unban the twins of podcast Users whose temporary ban ends now. Run it
    before the User update, which it mirrors (same predicate)."""
    session.execute(text(
        'UPDATE community SET banned = false FROM "user" u '
        'WHERE community.user_id = u.id AND community.ap_profile_id = u.ap_profile_id AND u.ap_id IS NOT NULL '
        'AND u.banned IS TRUE AND u.deleted IS NOT TRUE AND u.banned_until < :cutoff AND u.banned_until IS NOT NULL'
    ), {'cutoff': cutoff})
