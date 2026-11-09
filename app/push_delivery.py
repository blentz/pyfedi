"""Web Push delivery (owner ruling 2026-11-09: push notifications, clearnet only): each new notification, whatever
made it (a web page, an API, an inbox task), is pushed to its person's subscriptions (app.mastodon.push).

Two listeners on every SQLAlchemy session: at a flush, the new notifications whose person has a subscription (one
query, so a site or a test with none pays only that); after the commit, a task for them. The task sends each to the
subscriptions that want it: the token unrevoked, still usable and bound to a clearnet domain, the alert on, the policy
met (all, followed: the person follows its author, follower: its author follows the person, none), and the
notifications list's own gate passed on the token's domain (hidden there, a muted or deleted author, a subject no
longer readable: app.mastodon.notifications.shown). A push service that answers 404 or 410 has forgotten the subscription, so it is
forgotten here too. Every message holds only what Mastodon's apps read: the notification's id and type, a title, a
body and an icon (the token itself is never stored, so `access_token` is empty). Whether a notification is hidden is
asked as the default domain asks it: a task serves no request, so it has no storefront of its own."""
import json

from flask import current_app, g
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app import celery, db, webpush
import app.mastodon.notifications as notifications_gate   # the list's own gate, shared
from app.mastodon.kinds import TYPES
from app.models import Notification, OAuthToken, PushSubscription, User, UserFollower
from app.oauth_server.tokens import usable_token
from app.storefront import domain_base_url, domain_for_host
from app.utils import get_task_session, patch_db_session

_PENDING = 'push_notification_ids'


@event.listens_for(Session, 'after_flush')
def _note_new_notifications(session, flush_context):
    new = [row for row in session.new if isinstance(row, Notification) and row.notif_type in TYPES]
    if not new:
        return
    subscribed = set(session.execute(select(PushSubscription.user_id).where(
        PushSubscription.user_id.in_({row.user_id for row in new}))).scalars())
    if subscribed:
        session.info.setdefault(_PENDING, set()).update(row.id for row in new if row.user_id in subscribed)


@event.listens_for(Session, 'after_commit')
def _push_after_commit(session):
    ids = session.info.pop(_PENDING, None)
    if ids:
        queue_push(sorted(ids))


@event.listens_for(Session, 'after_rollback')
def _forget_on_rollback(session):
    session.info.pop(_PENDING, None)


def queue_push(notification_ids: list[int]) -> None:
    """Always a Celery task, never the work itself: this runs inside the commit that made the notifications, whose
    session can emit no more SQL, and the delivery reads through the session (app.utils.patch_db_session)."""
    push_task.delay(notification_ids)


@celery.task
def push_task(notification_ids: list[int]):
    with current_app.app_context():
        deliver(notification_ids)


def _follows(follower_id: int, followed_id: int) -> bool:
    return UserFollower.query.filter(UserFollower.local_user_id == follower_id,
                                     UserFollower.remote_user_id == followed_id, UserFollower.is_inward == False,
                                     UserFollower.is_accepted == True).first() is not None


def _wanted(subscription: PushSubscription, notification: Notification) -> bool:
    if not subscription.alerts.get(TYPES[notification.notif_type]):
        return False
    policy, author_id = subscription.policy, notification.author_id
    if policy == 'none':
        return False
    if policy == 'followed':
        return author_id is not None and _follows(notification.user_id, author_id)
    if policy == 'follower':
        return author_id is not None and _follows(author_id, notification.user_id)
    return True


def _clearnet_domain(token: OAuthToken | None):
    """The domain a token was issued on, if it may push: unrevoked, still its person's, and not an onion."""
    if token is None or token.revoked_at is not None or not usable_token(token):
        return None
    domain = domain_for_host(token.domain)
    return domain if domain is not None and not domain.is_onion else None


def message(notification: Notification, base: str) -> bytes:
    """The payload Mastodon's apps read, its icon an absolute URL on `base`, the token's domain."""
    author = db.session.get(User, notification.author_id) if notification.author_id else None
    icon = (author.avatar_image() or '') if author is not None else ''
    return json.dumps({'access_token': '', 'preferred_locale': 'en', 'notification_id': str(notification.id),
                       'notification_type': TYPES[notification.notif_type],
                       'title': author.display_name() if author is not None else '', 'body': notification.title or '',
                       'icon': base + icon if icon.startswith('/') else icon}).encode()


def _told_on(domain, notification: Notification) -> bool:
    """Whether the notifications list would tell its person of `notification` on `domain` (the same gate,
    app.mastodon.notifications.shown, asked in a request to that domain: what is hidden, readable and muted there)."""
    with current_app.test_request_context(base_url=domain_base_url(domain)):
        g.storefront = domain
        return bool(notifications_gate.shown([notification], notification.user_id))


def deliver(notification_ids: list[int]) -> None:
    session = get_task_session()
    try:
        with patch_db_session(session):
            for notification_id in notification_ids:
                notification = session.get(Notification, notification_id)
                if notification is None or notification.notif_type not in TYPES:
                    continue
                by_domain = {}   # each token's own domain decides what may be told there
                for subscription in session.query(PushSubscription).filter_by(user_id=notification.user_id).all():
                    domain = _clearnet_domain(session.get(OAuthToken, subscription.token_id))
                    if domain is not None and _wanted(subscription, notification):
                        by_domain.setdefault(domain.domain, (domain, []))[1].append(subscription)
                for domain, subscriptions in by_domain.values():
                    if not _told_on(domain, notification):
                        continue
                    base = domain_base_url(domain)
                    for subscription in subscriptions:
                        try:
                            status = webpush.send(subscription.endpoint, subscription.p256dh, subscription.auth,
                                                  message(notification, base), base)
                        except Exception:   # one push service down must not keep the rest from theirs
                            current_app.logger.warning('web push to a subscription failed', exc_info=True)
                            continue
                        if status in (404, 410):
                            session.delete(subscription)
                            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
