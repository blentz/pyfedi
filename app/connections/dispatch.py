"""Sending, editing and deleting a post's copies on connected accounts (spec §5, §7, §8).

A row of social_crosspost follows one copy:

    waiting  the author chose this account; the post is not published yet (scheduled, processing, held for review)
    queued   to send          editing  to edit            deleting  to delete
    sending  being sent, edited or deleted by a worker that claimed it
    processing  the service is finishing it (an Instagram container): the sweep asks how it is getting on, at no
             cost of an attempt, for up to half an hour (spec 19.4)
    sent     the copy is there
    failed   could not be sent (last_error says why); the author may retry
    skipped  never sent, for a reason the author is shown (an NSFW post on a service that takes none)
    deleted  the copy is gone, or was never made before the post went
    manual   the service cannot delete it: the author deletes it there by hand (Instagram Login, C6)
    unconfirmed  its publishing call went out and no answer came back, on a service that cannot be asked again
             safely: it may be there. Never retried by itself; the author is told, checks, and may retry (spec 19.2)

Work is claimed by moving `next_attempt_at` an hour ahead in one conditional UPDATE, so the per-minute sweep (`flask
send-queue`) and an immediate task never do the same row twice, and a worker that died leaves the row to the sweep
an hour later. A transient failure sets the next attempt by backoff; five failed sends make the row `failed`. What a
send did so far (`progress`) is kept on the row across attempts, so a retry carries on rather than starting over.
"""
from datetime import timedelta

from flask import current_app
from flask_babel import _
from sqlalchemy import update

from app import celery, db
from app.clock import utcnow
from app.connections import (SERVICES, AuthFailed, Pending, Progress, QuotaWait, ServiceError, Transient,
                             Unconfirmed)
from app.connections.apps import KINDS, current, enabled, for_domain
from app.connections.eligibility import DROP, refusal
from app.connections.tokens import REAUTH, needs_reconnecting, open_tokens, usable_tokens
from app.constants import NOTIF_DEFAULT, POST_STATUS_PUBLISHED
from app.models import Notification, Post, SocialConnection, SocialCrosspost, User
from app.utils import get_task_session, patch_db_session

MAX_ATTEMPTS = 5
BACKOFF = (60, 300, 900, 3600)   # seconds before the 2nd, 3rd, 4th and 5th tries
LEASE = timedelta(hours=1)
SENT_STATES = ('sent', 'editing', 'deleting')
WORK = {'queued': 'send', 'processing': 'resume', 'editing': 'edit', 'deleting': 'delete'}
POLL = timedelta(seconds=30)    # between asks while the service works on a copy (the sweep runs each minute)
PROCESSING_LIMIT = 30 * 60      # seconds a copy may stay processing before it fails


# --------------------------------------------------------------------------
# what the post paths call
# --------------------------------------------------------------------------

def form_choice(input, web: bool) -> list[int] | None:
    """The accounts the author ticked: a web form's `crosspost_to` (none ticked is none), an API body's
    `crosspost_to` list, or None for an API call that does not say, which means each connection's default (C3)."""
    if web:
        field = getattr(input, 'crosspost_to', None)
        return [int(value) for value in (field.data or [])] if field is not None else []
    if isinstance(input, dict) and isinstance(input.get('crosspost_to'), list):
        return [value for value in input['crosspost_to'] if isinstance(value, int) and not isinstance(value, bool)]
    return None


def offered_connections(user, post_type: int) -> list:
    """The author's connections the post form offers for a post of this type, each with its service, which is on."""
    offered = []
    for connection in SocialConnection.query.filter_by(user_id=user.id).order_by(SocialConnection.id).all():
        service = SERVICES.get(connection.service)
        if enabled(service, user.site_domain_id) and service.fits_type(post_type):
            offered.append((connection, service))
    return offered


def record_choices(post: Post, chosen: list[int] | None) -> None:
    """Rows for the accounts this new post goes to, `waiting` until it is published. `chosen` None means the
    author's defaults. Only the author's own connections to services that are on count. The caller commits."""
    connections = SocialConnection.query.filter_by(user_id=post.user_id).all()
    for connection in connections:
        wanted = connection.default_on if chosen is None else connection.id in chosen
        if wanted and enabled(SERVICES.get(connection.service), post.author.site_domain_id):
            db.session.add(SocialCrosspost(post_id=post.id, connection_id=connection.id,
                                           service=connection.service, state='waiting'))


def post_published(post: Post, template: Post | None = None) -> None:
    """The post is published now: its waiting rows go, or are dropped or skipped (spec §5). A repeating schedule's
    new occurrence takes the template post's choices, which stay waiting for the next one."""
    if template is not None:
        for row in SocialCrosspost.query.filter_by(post_id=template.id, state='waiting'):
            db.session.add(SocialCrosspost(post_id=post.id, connection_id=row.connection_id, service=row.service,
                                           state='waiting'))
        db.session.flush()
    queued = []
    for row in SocialCrosspost.query.filter_by(post_id=post.id, state='waiting').all():
        service = SERVICES.get(row.service)
        reason = DROP if row.connection is None or not enabled(service, post.author.site_domain_id) \
            else refusal(post, row.connection, service)
        if reason == DROP:
            db.session.delete(row)
        elif reason is not None:
            row.state, row.last_error = 'skipped', reason[:255]
        else:
            row.state, row.variant, row.next_attempt_at = 'queued', row.connection.variant, utcnow()
            queued.append(row)
    db.session.commit()
    for row in queued:
        run(row.id)


def post_edited(post: Post) -> None:
    """C4: each copy is edited where the service allows it, or deleted when the post may no longer go there."""
    work = []
    for row in SocialCrosspost.query.filter_by(post_id=post.id, state='sent').all():
        service = SERVICES.get(row.service)
        if service is None or row.connection is None:
            continue
        if refusal(post, row.connection, service) is not None:
            row.state = 'deleting'
        elif service.can_edit:
            row.state = 'editing'
        else:
            continue
        row.next_attempt_at, row.attempts = utcnow(), 0
        work.append(row)
    db.session.commit()
    for row in work:
        run(row.id)


def post_gone(post: Post) -> None:
    """The post was deleted or removed: its copies are deleted, and copies not made yet never will be."""
    work = []
    for row in SocialCrosspost.query.filter_by(post_id=post.id).all():
        if row.state in ('sent', 'editing'):
            row.state, row.next_attempt_at, row.attempts = 'deleting', utcnow(), 0
            work.append(row)
        elif row.state in ('waiting', 'queued', 'failed', 'processing'):   # nothing of it is public yet
            row.state = 'deleted'
        elif row.state == 'unconfirmed':   # it may be there, under an id never learnt
            service = SERVICES.get(row.service)
            row.state, row.next_attempt_at = 'manual', None
            row.last_error = _('If it was posted on %(service)s, delete it there by hand.',
                               service=service.label if service is not None else row.service)
    db.session.commit()
    for row in work:
        run(row.id)


def post_restored(post: Post) -> None:
    """The post is back: its deleted copies are sent again as new posts (spec §7)."""
    for row in SocialCrosspost.query.filter_by(post_id=post.id, state='deleted').all():
        row.state, row.remote_id, row.remote_url, row.last_error, row.attempts = 'waiting', None, None, None, 0
        row.progress = None   # a new copy: nothing of the deleted one's sending carries over
    db.session.commit()
    if post.status == POST_STATUS_PUBLISHED:   # a scheduled or processing post keeps them waiting for its publish
        post_published(post)


def retry(row: SocialCrosspost) -> None:
    """The author asked for a failed or unconfirmed copy to be tried again."""
    row.state, row.attempts, row.last_error, row.next_attempt_at = 'queued', 0, None, utcnow()
    db.session.commit()
    run(row.id)


def disconnect(connection: SocialConnection) -> None:
    """Revoke the tokens at the service (best effort) and forget the connection. The copies stay where they are:
    a member who wants them gone deletes the post first (spec §4)."""
    service = SERVICES.get(connection.service)
    if service is not None:
        try:
            with for_domain(connection.user.site_domain_id):
                service.revoke(open_tokens(connection))
        except Exception as failure:   # a revoke never keeps a member connected
            current_app.logger.info(f'connections: revoke of {connection.service} connection {connection.id} '
                                    f'failed: {type(failure).__name__}')
    db.session.delete(connection)
    db.session.commit()


def reconnect_domain(domain_id: int, kind: str, app) -> int:
    """C11: the domain's members' connections under `kind` made with another app than `app` (or with any, when the
    domain has none left) connect again. A Bluesky key is a secret: a new one changes no client. How many were
    newly marked."""
    if kind == 'bluesky':
        return 0
    marked = 0
    rows = SocialConnection.query.join(User, User.id == SocialConnection.user_id).filter(
        User.site_domain_id == domain_id).all()
    for connection in rows:
        service = SERVICES.get(connection.service)
        if service is None or service.kind_of(connection.variant) != kind:
            continue
        if app is None or connection.app_client_id != app.client_id:
            marked += connection.state != REAUTH
            needs_reconnecting(connection, service, _('This site now uses another app for %(service)s: connect '
                                                      'your account again.', service=service.label))
    db.session.commit()
    return marked


def purge_user(user) -> None:
    """An account is purged: each copy is deleted now, one try each, then every connection goes (spec §7)."""
    rows = SocialCrosspost.query.join(Post, Post.id == SocialCrosspost.post_id).filter(
        Post.user_id == user.id, SocialCrosspost.state.in_(SENT_STATES)).all()
    for row in rows:
        row.state, row.next_attempt_at = 'deleting', utcnow()
    db.session.commit()
    for row in rows:
        perform(row.id)
    for connection in SocialConnection.query.filter_by(user_id=user.id).all():
        disconnect(connection)


# --------------------------------------------------------------------------
# the work
# --------------------------------------------------------------------------

def run(row_id: int) -> None:
    if current_app.debug:
        crosspost_task(row_id)
    else:
        crosspost_task.delay(row_id)


@celery.task
def crosspost_task(row_id: int) -> None:
    session = get_task_session()
    try:
        with patch_db_session(session):
            perform(row_id)
    finally:
        session.close()


def sweep() -> int:
    """Every row whose next attempt is due, from `flask send-queue` each minute. Returns how many it started."""
    due = [row_id for (row_id,) in db.session.query(SocialCrosspost.id).filter(
        SocialCrosspost.state.in_(tuple(WORK)), SocialCrosspost.next_attempt_at <= utcnow()).all()]
    for row_id in due:
        run(row_id)
    return len(due)


def _claim(row_id: int) -> str | None:
    """Take the row for this worker: the work to do, or None when it is not due or another worker has it."""
    now = utcnow()
    row = db.session.get(SocialCrosspost, row_id)
    if row is None or row.state not in WORK or row.next_attempt_at is None or row.next_attempt_at > now:
        return None
    work, state = WORK[row.state], row.state
    taken = db.session.execute(update(SocialCrosspost).where(
        SocialCrosspost.id == row_id, SocialCrosspost.state == state,
        SocialCrosspost.next_attempt_at == row.next_attempt_at).values(next_attempt_at=now + LEASE))
    db.session.commit()
    return work if taken.rowcount == 1 else None


def perform(row_id: int) -> None:
    """Do the row's due work once, and record what came of it."""
    work = _claim(row_id)
    if work is None:
        return
    row = db.session.get(SocialCrosspost, row_id)
    service = SERVICES.get(row.service)
    connection = row.connection
    if work == 'delete' and service is not None and not service.can_delete(row.variant):
        row.state, row.next_attempt_at = 'manual', None
        row.last_error = _('Delete it by hand on %(service)s.', service=service.label)
        db.session.commit()
        return
    if service is None or connection is None or (
            work in ('send', 'resume') and not enabled(service, connection.user.site_domain_id)):
        _give_up(row, work, _('This account is no longer connected.'))
        return
    with for_domain(connection.user.site_domain_id):   # the member's home domain's app (C10)
        _perform(row_id, work, service, connection)


def _perform(row_id: int, work: str, service, connection) -> None:
    row = db.session.get(SocialCrosspost, row_id)
    kind = service.kind_of(connection.variant)
    app = current(kind) if kind in KINDS else None
    if kind in KINDS and (app is None or (kind != 'bluesky' and connection.app_client_id
                                          and app.client_id != connection.app_client_id)):
        message = _('This account was connected through an app this site no longer uses: connect it again.')
        needs_reconnecting(connection, service, message)   # C11, met at the next call
        _give_up(row, work, message)
        return
    if app is not None and connection.app_client_id is None:   # connected before apps were recorded: it is this app's
        connection.app_client_id = app.client_id
        db.session.commit()
    until = service.paused_until(connection)
    if until is not None:   # the service's allowance is nearly spent: wait, at no cost of an attempt (spec 18.3)
        row.next_attempt_at = until
        row.last_error = _('Waiting: %(service)s asked for a pause.', service=service.label)
        db.session.commit()
        return
    try:
        tokens = usable_tokens(connection.id, service)
        row = db.session.get(SocialCrosspost, row_id)
        post = row.post
        if work in ('send', 'resume'):
            progress = Progress(row.progress, save=lambda data: _keep(row_id, data))
            sent = (service.send(post, tokens, connection, progress) if work == 'send'
                    else service.resume(post, tokens, connection, progress))
            row = db.session.get(SocialCrosspost, row_id)
            if isinstance(sent, Pending):
                _processing(row, work, sent, service)
                db.session.commit()
                return
            row.remote_id, row.remote_url, row.sent_at = sent.remote_id[:512], (sent.remote_url or '')[:1024] or None, utcnow()
            row.state, row.progress = 'sent', None
        elif work == 'edit':
            service.edit(post, tokens, row.remote_id)
            row.state, row.edited_at = 'sent', utcnow()
        else:
            service.delete(tokens, row.remote_id)
            row.state = 'deleted'
        row.next_attempt_at, row.last_error = None, None
        row.attempts += work != 'resume'   # asking how a copy is getting on costs nothing
    except QuotaWait as wait:
        row = db.session.get(SocialCrosspost, row_id)
        row.next_attempt_at, row.last_error = wait.until, wait.message
    except Transient as failure:
        row = db.session.get(SocialCrosspost, row_id)
        row.attempts += 1
        if row.attempts >= MAX_ATTEMPTS:
            _give_up(row, work, failure.message, commit=False)
        else:
            delay = max(BACKOFF[min(row.attempts, len(BACKOFF)) - 1], failure.retry_after or 0)
            row.next_attempt_at, row.last_error = utcnow() + timedelta(seconds=delay), failure.message
    except Unconfirmed as failure:
        row = db.session.get(SocialCrosspost, row_id)
        row.attempts += 1
        row.state, row.next_attempt_at, row.last_error = 'unconfirmed', None, failure.message
        db.session.add(Notification(
            title=_('Your post may be on %(service)s already: check %(name)s before trying again',
                    service=service.label, name=connection.remote_name)[:150],
            url=f'/post/{row.post_id}', user_id=connection.user_id, author_id=connection.user_id,
            notif_type=NOTIF_DEFAULT, subtype='crosspost_unconfirmed', targets={'crosspost_id': row.id}))
    except AuthFailed as failure:
        row = db.session.get(SocialCrosspost, row_id)
        needs_reconnecting(row.connection, service, failure.message)
        _give_up(row, work, failure.message, commit=False)
    except ServiceError as failure:
        row = db.session.get(SocialCrosspost, row_id)
        row.attempts += 1
        _give_up(row, work, failure.message, commit=False)
    except Exception as failure:
        db.session.rollback()
        current_app.logger.exception(f'connections: {work} of crosspost {row_id} failed')
        row = db.session.get(SocialCrosspost, row_id)
        row.attempts += 1
        _give_up(row, work, _('Something went wrong here (%(error)s).', error=type(failure).__name__),
                 commit=False)
    db.session.commit()


def _processing(row: SocialCrosspost, work: str, pending: Pending, service) -> None:
    """The service is still working on the copy: ask again in a while, or fail it once it has taken too long."""
    now = utcnow().timestamp()
    since = (row.progress or {}).get('processing_since', now) if work == 'resume' else now
    if work == 'send':
        row.attempts += 1
    if now - since > PROCESSING_LIMIT:
        _give_up(row, work, _('%(service)s took too long to finish it.', service=service.label), commit=False)
        return
    row.state, row.progress = 'processing', {**pending.data, 'processing_since': since}
    row.next_attempt_at, row.last_error = utcnow() + POLL, None


def _keep(row_id: int, data: dict) -> None:
    """Store a send's progress now, ahead of the call it guards (Progress.save)."""
    row = db.session.get(SocialCrosspost, row_id)
    row.progress = data
    db.session.commit()


def _give_up(row: SocialCrosspost, work: str, message: str, commit: bool = True) -> None:
    """A send that cannot be made fails; an edit or delete that cannot be made leaves the copy as it was (sent)."""
    row.state = 'failed' if work in ('send', 'resume') else 'sent'
    row.next_attempt_at, row.last_error = None, message[:255]
    if commit:
        db.session.commit()
