"""ATProto actors as remote Users (ATProto spec 2026-11-23 §4, §7; A6).

Every ATProto account is a remote `User` on one Instance row, `bsky.app`, with software `atproto`: blocking that
instance blocks the whole network with the existing tools. An actor's `ap_profile_id` and `ap_public_url` are its
bsky.app profile address (`https://bsky.app/profile/<did>`), which is where its profile links, and which carries
the DID; its `ap_id` and `user_name` are its handle, so it shows and links as `@handle`. It has no inbox: nothing
is sent to it over ActivityPub. Its posts' `ap_id`s are their bsky.app addresses too (`post_url`), since PieFed
keeps only http(s) ids; `at_uri_of` turns one back.

Following one (A6) is recorded here at once, accepted, and sends nothing: the consumer (app.atproto.jetstream)
reads the DIDs anyone here follows, and the actor's latest posts are read from its PDS (`followed`).
"""
import re
from urllib.parse import urlencode

from flask import current_app

from sqlalchemy import text

from app import celery, db
from app.atproto.names import DOMAIN, HANDLE, looks_atproto
from app.atproto.resolve import did_document, handle_of, pds_of, resolve_handle
from app.clock import utcnow
from app.connections import ServiceError
from app.connections.http import fetch
from app.models import File, Instance, User
from app.utils import get_task_session, markdown_to_html, patch_db_session

SOFTWARE = 'atproto'
WEB = f'https://{DOMAIN}'
NO_UNAUTHENTICATED = '!no-unauthenticated'
FOLLOW_BACKFILL = 20    # an actor's latest posts read when someone here first follows it (§7)


def instance() -> Instance:
    """The one Instance every ATProto actor belongs to, made on first use."""
    row = Instance.query.filter_by(domain=DOMAIN).first()
    if row is None:
        row = Instance(domain=DOMAIN, software=SOFTWARE, trusted=False)
        db.session.add(row)
        db.session.commit()
    elif row.software != SOFTWARE:
        row.software = SOFTWARE
        db.session.commit()
    return row


def is_atproto(user) -> bool:
    return bool(user is not None and user.ap_profile_id and user.ap_profile_id.startswith(f'{WEB}/profile/did:'))


def profile_url(did: str) -> str:
    return f'{WEB}/profile/{did}'


def did_of(user) -> str | None:
    return user.ap_profile_id.removeprefix(f'{WEB}/profile/') if is_atproto(user) else None


def post_url(uri: str) -> str:
    """A post's at:// URI as its bsky.app address: what PieFed keeps as its ap_id."""
    did, _collection, rkey = uri.removeprefix('at://').split('/', 2)
    return f'{WEB}/profile/{did}/post/{rkey}'


def at_uri_of(url: str) -> str | None:
    """The at:// URI of a post's bsky.app address, or None for any other address."""
    match = re.fullmatch(rf'{re.escape(WEB)}/profile/(did:[^/]+)/post/([A-Za-z0-9._~:-]+)', url or '')
    return f'at://{match.group(1)}/app.bsky.feed.post/{match.group(2)}' if match else None


def blob_url(pds: str, did: str, blob) -> str | None:
    """Where a blob of the actor's is read: its PDS's getBlob (public)."""
    cid = (blob or {}).get('ref', {}).get('$link') if isinstance(blob, dict) else None
    if not isinstance(cid, str) or not cid:
        return None
    return f'{pds}/xrpc/com.atproto.sync.getBlob?{urlencode({"did": did, "cid": cid})}'


def read_record(pds: str, did: str, collection: str, rkey: str) -> dict | None:
    """A public record from the actor's PDS: its value, or None when there is none (or the account is deactivated,
    which answers `{}`: §16)."""
    response = fetch('GET', f'{pds}/xrpc/com.atproto.repo.getRecord',
                     params={'repo': did, 'collection': collection, 'rkey': rkey})
    if response.status_code != 200:
        return None
    try:
        value = response.json().get('value')
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def self_labels(record: dict | None) -> set[str]:
    labels = (record or {}).get('labels') or {}
    values = labels.get('values') if isinstance(labels, dict) else None
    return {value.get('val') for value in values or [] if isinstance(value, dict) and isinstance(value.get('val'), str)}


def restamp(user_id: int, login_only: bool) -> None:
    """The account changed its `!no-unauthenticated`: its posts and replies, and ATProto replies under its posts,
    are stamped again (app.atproto.ingest.login_only). Members' own replies are left as they are: they federate."""
    db.session.execute(text('UPDATE post SET local_only = requires_login OR :flag WHERE user_id = :user'),
                       {'flag': login_only, 'user': user_id})
    db.session.execute(text(
        'UPDATE post_reply r SET local_only = r.requires_login OR (r.user_id = :user AND :flag) '
        'OR (r.user_id <> :user AND u.requires_login) '
        'OR EXISTS (SELECT 1 FROM post p WHERE p.id = r.post_id AND p.local_only) '
        'FROM "user" u WHERE u.id = r.user_id AND u.ap_domain = :domain '
        'AND (r.user_id = :user OR r.post_id IN (SELECT id FROM post WHERE user_id = :user))'),
        {'flag': login_only, 'user': user_id, 'domain': DOMAIN})


def verified_handle(did: str, document: dict) -> str:
    """The handle the DID document claims, when it is a handle and resolves back to this DID (ATProto's two-way
    check: a document may claim any name); else the DID itself, which is what the actor is then called."""
    claimed = (handle_of(document) or '').lower()
    if claimed and HANDLE.fullmatch(claimed) and resolve_handle(claimed) == did:
        return claimed
    return did


def refresh(user: User) -> User:
    """Read the actor's DID document and profile again, and store what they say."""
    did = did_of(user)
    document = did_document(did)
    pds = pds_of(document)
    handle = verified_handle(did, document)
    profile = read_record(pds, did, 'app.bsky.actor.profile', 'self') or {}
    if handle != did:   # one actor per handle: another that claimed it before keeps only its DID
        for other in User.query.filter(User.ap_domain == DOMAIN, User.ap_id == handle, User.id != user.id).all():
            other.user_name = other.ap_id = did_of(other)
    user.user_name = user.ap_id = handle[:255]
    user.title = (profile.get('displayName') or '').strip()[:256] or None
    about = (profile.get('description') or '').strip()
    user.about = about
    user.about_html = markdown_to_html(about) if about else ''
    login_only = NO_UNAUTHENTICATED in self_labels(profile)
    if login_only != bool(user.requires_login) and user.id is not None:
        restamp(user.id, login_only)
    user.requires_login = login_only
    for field, column in (('avatar', 'avatar'), ('banner', 'cover')):
        url = blob_url(pds, did, profile.get(field))
        current = getattr(user, column)
        if url is None:
            setattr(user, column, None)
        elif current is None or current.source_url != url:
            setattr(user, column, File(source_url=url))
    user.ap_fetched_at = utcnow()
    db.session.commit()
    return user


def actor_for(did: str, fetch_new: bool = True) -> User | None:
    """The remote User for a DID: the one stored, else (when `fetch_new`) made from its DID document and profile.
    None when it cannot be read."""
    user = User.query.filter_by(ap_profile_id=profile_url(did)).first()
    if user is not None or not fetch_new:
        return user
    user = User(user_name=did, ap_id=did, ap_profile_id=profile_url(did), ap_public_url=profile_url(did),
                ap_domain=DOMAIN, instance_id=instance().id, ap_inbox_url=None, created=utcnow(),
                indexable=True, searchable=True)
    try:
        did_document(did)   # an actor nobody can resolve is not made
    except ServiceError:
        return None
    db.session.add(user)
    db.session.flush()
    try:
        return refresh(user)
    except ServiceError as failure:
        db.session.rollback()
        current_app.logger.info(f'atproto: actor {did} not read: {failure.message}')
        return None


def find(name: str, fetch_new: bool) -> User | None:
    """The actor a handle or DID names: the stored one, else (when `fetch_new`) resolved and made. None for a name
    that is no ATProto one, or that resolves to nothing."""
    text = looks_atproto(name)
    if text is None:
        return None
    if text.startswith('did:'):
        return actor_for(text, fetch_new)
    user = User.query.filter_by(ap_id=text, ap_domain=DOMAIN).first()
    if user is not None or not fetch_new:
        return user
    did = resolve_handle(text)
    return actor_for(did) if did else None


@celery.task
def refresh_task(user_id: int) -> None:
    session = get_task_session()
    try:
        with patch_db_session(session):
            user = session.get(User, user_id)
            if user is not None and is_atproto(user):
                try:
                    refresh(user)
                except ServiceError as failure:
                    current_app.logger.info(f'atproto: refresh of user {user_id} failed: {failure.message}')
    finally:
        session.close()


# --------------------------------------------------------------------------
# following (A6)
# --------------------------------------------------------------------------

def followed(user_id: int | None, actor_id: int, limit: int = FOLLOW_BACKFILL) -> None:
    """Someone here followed the actor: read its latest posts into their feed (§7). The consumer takes its DID up
    the next time it reads the follows, and asks again then (`limit` posts) for what came in between."""
    if current_app.debug:
        backfill_task(actor_id, limit)
    else:
        backfill_task.delay(actor_id, limit)


@celery.task
def backfill_task(actor_id: int, limit: int = FOLLOW_BACKFILL) -> None:
    from app.atproto import ingest   # cycle: app.atproto.ingest imports this module
    session = get_task_session()
    try:
        with patch_db_session(session):
            user = session.get(User, actor_id)
            if user is None or not is_atproto(user):
                return
            try:
                ingest.backfill(user, limit)
            except ServiceError as failure:
                current_app.logger.info(f'atproto: backfill of {user.ap_id} failed: {failure.message}')
    finally:
        session.close()

