"""Messages admins write to members: who they reach, and sending them, now or at a set time (spec
2026-11-24-admin-messaging D10-D18)."""
from datetime import timedelta

from flask import current_app, url_for
from flask_babel import _
from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from app import celery, db
from app.email import mail_name, send_email
from app.messaging.catalog import MESSAGE_PLACEHOLDERS
from app.messaging.render import Mail, Value, compose
from app.models import AdminMessage, AdminMessageDelivery, AdminMessagePick, MemberSubscription, RoleGrant, \
    SiteDomain, User, utcnow
from app.storefront import home_storefront, storefront_url
from app.utils import get_task_session, patch_db_session

AUDIENCES = ('all', 'filtered', 'picked')
SUBSCRIPTION_FILTERS = ('any', 'none', 'offers')
STALE_AFTER = timedelta(minutes=15)   # a sending task silent this long has died (D17)
HEARTBEAT_EVERY = 25   # recipients between heartbeats


def _domain_clause(domain_ids: list):
    """Members whose home domain is one of these: the default's are also those with none (a foreign key keeps every
    other value a row's)."""
    default = db.session.scalar(select(SiteDomain.id).where(SiteDomain.is_default.is_(True)))
    clauses = [User.site_domain_id.in_(domain_ids)]
    if default in domain_ids:
        clauses.append(User.site_domain_id.is_(None))
    return or_(*clauses)


def _active_subscription(*extra):
    return exists().where(MemberSubscription.user_id == User.id, MemberSubscription.status == 'active', *extra)


def audience_query(message: AdminMessage):
    """Everyone `message` would go to, as of now (D12-D14)."""
    query = User.query.filter(User.ap_id.is_(None), User.verified.is_(True), User.banned.is_(False),
                              User.deleted.is_(False), User.email.is_not(None), User.email != '')
    if not message.service_notice:
        query = query.filter(User.newsletter.is_(True))   # MS4
    if message.audience == 'picked':
        return query.filter(User.id.in_(select(AdminMessagePick.user_id)
                                        .where(AdminMessagePick.message_id == message.id)))
    if message.audience == 'filtered':
        if message.domain_ids:
            query = query.filter(_domain_clause(list(message.domain_ids)))
        if message.role_ids:
            now = utcnow()
            query = query.filter(exists().where(RoleGrant.user_id == User.id, RoleGrant.role_id.in_(message.role_ids),
                                                or_(RoleGrant.expires_at.is_(None), RoleGrant.expires_at > now)))
        if message.subscription == 'any':
            query = query.filter(_active_subscription())
        elif message.subscription == 'none':
            query = query.filter(~_active_subscription())
        elif message.subscription == 'offers':
            query = query.filter(_active_subscription(MemberSubscription.offer_id.in_(message.offer_ids or [])))
    return query


def recipient_count(message: AdminMessage) -> int:
    return audience_query(message).count()


def render_for(message: AdminMessage, user: User) -> tuple[Mail, str | None]:
    """The message as `user` gets it, and its List-Unsubscribe link: none on a service notice (MS4), none without a
    token (as newsletter.html has always done). Call it under the user's storefront."""
    unsubscribe = None
    if not message.service_notice and user.verification_token:
        unsubscribe = url_for('user.user_newsletter_unsubscribe', user_id=user.id, token=user.verification_token,
                              _external=True)
    values = {'name': Value(user.display_name()), 'site_name': Value(mail_name(user)),
              'site_url': Value(storefront_url())}
    mail = compose(message.subject, message.body, MESSAGE_PLACEHOLDERS, values, unsubscribe,
                   _('Unsubscribe from the newsletter'))
    return mail, unsubscribe


def mail_one(message: AdminMessage, user: User, to: str | None = None) -> None:
    with home_storefront(user):   # each reader's links name their own domain (spec §3.2)
        mail, unsubscribe = render_for(message, user)
    send_email(mail.subject, sender=f'{mail_name(user)} <{current_app.config["MAIL_FROM"]}>',
               recipients=[to or user.email], text_body=mail.text_body, html_body=mail.html_body,
               list_unsubscribe=unsubscribe)


def schedule(message: AdminMessage, when) -> None:
    message.status, message.scheduled_for = 'scheduled', when


def unschedule(message: AdminMessage) -> bool:
    """Back to a draft, unless sending has begun: only a scheduled row changes, so a dispatch never loses the race."""
    result = db.session.execute(update(AdminMessage).where(AdminMessage.id == message.id,
                                                           AdminMessage.status == 'scheduled')
                                .values(status='draft', scheduled_for=None))
    db.session.commit()
    db.session.refresh(message)
    return result.rowcount == 1


def claim_due(now=None) -> list:
    """The ids of messages now due, each claimed by moving it to `sending` in one statement, so two runs never both
    take one (D15); and those whose sending task has gone quiet (D17)."""
    now = now or utcnow()
    due = db.session.execute(update(AdminMessage)
                             .where(or_(and_(AdminMessage.status == 'scheduled', AdminMessage.scheduled_for <= now),
                                        and_(AdminMessage.status == 'sending',
                                             or_(AdminMessage.heartbeat_at.is_(None),
                                                 AdminMessage.heartbeat_at < now - STALE_AFTER))))
                             .values(status='sending', heartbeat_at=now)
                             .returning(AdminMessage.id)).scalars().all()
    db.session.commit()
    return sorted(due)


def dispatch_due() -> int:
    """What send-queue does every few minutes: hand each due message to a task."""
    due = claim_due()
    for message_id in due:
        send_admin_message.delay(message_id)
    return len(due)


def send_now(message: AdminMessage) -> None:
    """Due at once, and sent without waiting for send-queue."""
    schedule(message, utcnow())
    db.session.commit()
    dispatch_due()


def _claim(message_id: int, user_id: int) -> bool:
    """This recipient, for this message, by this task alone (D16)."""
    row = db.session.execute(insert(AdminMessageDelivery)
                             .values(message_id=message_id, user_id=user_id, claimed_at=utcnow())
                             .on_conflict_do_nothing()
                             .returning(AdminMessageDelivery.user_id)).first()
    db.session.commit()
    return row is not None


def deliver(message_id: int) -> int:
    """Send a message being sent to everyone it reaches and has not yet been claimed for; then it is sent. The number
    mailed by this run."""
    message = db.session.get(AdminMessage, message_id)
    if message is None or message.status != 'sending':
        return 0
    mailed = handled = 0
    for (user_id,) in audience_query(message).order_by(User.id).with_entities(User.id).all():
        if not _claim(message.id, user_id):
            continue
        user = db.session.get(User, user_id)
        delivery = db.session.get(AdminMessageDelivery, (message.id, user_id))
        # Counted in SQL, not read and written back: a run that took over from a quiet one may overlap it (D17)
        try:
            mail_one(message, user)
        except Exception as error:
            delivery.error = type(error).__name__[:255]
            counts = {'failed_count': AdminMessage.failed_count + 1}
            current_app.logger.warning(f'admin message {message.id} to account {user_id} failed: {delivery.error}')
        else:
            delivery.sent_at = utcnow()
            counts = {'sent_count': AdminMessage.sent_count + 1}
            mailed += 1
        handled += 1
        if handled % HEARTBEAT_EVERY == 0:
            counts['heartbeat_at'] = utcnow()
        db.session.execute(update(AdminMessage).where(AdminMessage.id == message.id).values(**counts))
        db.session.commit()
    message.status, message.sent_at, message.heartbeat_at = 'sent', utcnow(), None
    db.session.commit()
    return mailed


@celery.task
def send_admin_message(message_id: int) -> int:
    session = get_task_session()
    try:
        with patch_db_session(session):
            return deliver(message_id)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
