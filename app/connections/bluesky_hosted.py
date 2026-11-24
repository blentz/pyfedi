"""Bluesky accounts PieFed hosts (ATProto spec 2026-11-23-atproto-federation-design.md §8, §9; A3, A8-A10).

A member creates `@<name>.<their home domain>` in one click. The account lives on the Bluesky PDS run beside
PieFed (ATPROTO_PDS_URL), which owns its repo, its handle's well-known answer and its firehose; PieFed holds the
account's own password and session, sealed like every connection's tokens. It is a Bluesky connection of the
`hosted` variant, always the member's identity, and its copies go through the same pipeline as an OAuth one's.

Slots (§9): at most ATPROTO_MAX_ACCOUNTS are active at once, and the PDS holds at most ATPROTO_HOSTED_LIMIT
accounts in all (the relay's account limit). A member who asks when none is free waits, in order of followers then
of joining; the daily task (`daily`) fills free slots and, by ATPROTO_SLOT_POLICY, frees them: `sticky` turns an
account off after 90 days with no copy (its member may turn it on again), `ranked` keeps the top N. An admin may grant a slot at once, or pin one. Every write
takes its turn in the PDS-wide budget (app.atproto.budget).

A waiting row has no account yet unless it had one before (it was deactivated): its remote_id is then the DID,
otherwise `waiting:<user id>`, and its tokens hold only the handle asked for.
"""
import json
import re
import secrets
import time
from datetime import timedelta

import jwt
from flask import current_app
from flask_babel import _

import app as app_pkg
from app import celery, db, storefront
from app.atproto import Session, budget
from app.clock import utcnow
from app.connections import SERVICES, AuthFailed, Permanent, QuotaWait, ServiceError, Transient, setting_name
from app.connections.apps import domain_switch, enabled
from app.connections.content import MARKDOWN_LINK, as_jpeg as _fit, cut, image_bytes
from app.connections.eligibility import refusal
from app.connections.tokens import open_tokens, seal_tokens, usable_tokens
from app.connections.http import call, check, fetch
from app.constants import COMMUNITY_KIND_MICROBLOGS, NOTIF_DEFAULT
from app.models import Community, Notification, Post, SocialConnection, SocialCrosspost, User, UserFollower
from app.utils import get_setting, get_task_session, patch_db_session

HOSTED = 'hosted'
ACTIVE, WAITING, DEACTIVATED, MOVED = 'active', 'waiting', 'deactivated', 'moved'
DELETING = 'deleting'         # the PDS has not yet confirmed the account's deletion: tried again daily
HOSTING = 'bluesky_hosting'     # the switch's name: "Members can create accounts here", for the instance and per domain
LABEL_MIN, LABEL_MAX = 3, 18    # the PDS's limits on the first label of a handle under its own domains
INACTIVE = timedelta(days=90)   # sticky: a slot with no copy for this long is freed
BACKFILL = 50                   # the member's latest microblog posts copied when their account starts
VIDEO_CHUNK = 1 << 20
CREATE_SPACING_MS = 1000        # account creations at most one a second (the relay allows five)
PROFILE_IMAGE_BYTES = 950_000   # app.bsky.actor.profile takes avatar and banner up to 1,000,000 bytes
HANDLE_ERRORS = ('HandleNotAvailable', 'InvalidHandle', 'UnsupportedDomain')
DESCRIBE_KEY = 'atproto:describe'   # the PDS's describeServer, kept ten minutes
CREATE_KEY = 'atproto:create'       # held for a second after each account creation
# an account's email: under a reserved domain that never receives mail (RFC 2606), so no reset mail can reach
# anyone, even if the PDS is given SMTP; PieFed holds the password and never needs one
ACCOUNT_EMAIL_DOMAIN = 'accounts.invalid'
MAX_NUMBER = 99                     # the highest numbered alternative a member's own name may take


class HandleRefused(ServiceError):
    """The PDS will not give this handle (taken, reserved or invalid): `suggestion` is a label it may give."""

    def __init__(self, message: str, suggestion: str):
        super().__init__(message)
        self.suggestion = suggestion


# --------------------------------------------------------------------------
# the PDS
# --------------------------------------------------------------------------

def pds_url() -> str:
    return current_app.config.get('ATPROTO_PDS_URL') or ''


def configured() -> bool:
    return bool(pds_url() and current_app.config.get('ATPROTO_PDS_ADMIN_PASSWORD'))


def _admin() -> tuple[str, str]:
    return 'admin', current_app.config['ATPROTO_PDS_ADMIN_PASSWORD']


def _error_of(response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body.get('error') if isinstance(body, dict) else None


def _handle_refused(response) -> bool:
    """The PDS will not give the handle asked for: a handle error code, or (as the PDS answers a taken one, §16)
    InvalidRequest with a message about the handle."""
    if response.status_code != 400:
        return False
    error = _error_of(response)
    if error in HANDLE_ERRORS:
        return True
    message = response.json().get('message') if error == 'InvalidRequest' else None
    return isinstance(message, str) and 'handle' in message.lower()


def describe() -> dict:
    """The PDS's describeServer: its handle domains (`availableUserDomains`) and whether it wants invites. Kept ten
    minutes."""
    cached = app_pkg.redis_client.get(DESCRIBE_KEY)
    if cached:
        return json.loads(cached)
    answer = call('GET', f'{pds_url()}/xrpc/com.atproto.server.describeServer').json()
    app_pkg.redis_client.set(DESCRIBE_KEY, json.dumps(answer), ex=600)
    return answer


def handle_domains() -> list[str]:
    try:
        domains = describe().get('availableUserDomains') or []
    except ServiceError:
        return []
    return [domain.lstrip('.') for domain in domains if isinstance(domain, str)]


def _expiry(access: str) -> float:
    try:
        return float(jwt.decode(access, options={'verify_signature': False})['exp'])
    except (jwt.PyJWTError, KeyError, TypeError, ValueError):
        return time.time() + 600


def _tokens(answer: dict, password: str) -> dict:
    """A session answer (createAccount, createSession, refreshSession) as a hosted connection's tokens."""
    return {'kind': HOSTED, 'did': answer['did'], 'handle': answer.get('handle') or '', 'password': password,
            'access_token': answer['accessJwt'], 'refresh_token': answer['refreshJwt'],
            'expires_at': _expiry(answer['accessJwt']), 'pds': pds_url()}


def sign_in(did: str, password: str) -> dict:
    """A new session with the account's password; when the PDS no longer takes it (it was lost or changed), the
    admin sets a new one first (§16: admin.updateAccountPassword recovers an account)."""
    response = fetch('POST', f'{pds_url()}/xrpc/com.atproto.server.createSession',
                     json={'identifier': did, 'password': password})
    if response.status_code in (400, 401) and _error_of(response) == 'AuthenticationRequired':
        password = secrets.token_urlsafe(32)
        try:
            call('POST', f'{pds_url()}/xrpc/com.atproto.admin.updateAccountPassword', auth=_admin(),
                 json={'did': did, 'password': password})
        except Permanent as failure:
            raise AuthFailed(failure.message) from None
        response = fetch('POST', f'{pds_url()}/xrpc/com.atproto.server.createSession',
                         json={'identifier': did, 'password': password})
    return _tokens(check(response).json(), password)


def refresh(tokens: dict) -> dict:
    """New tokens: the refresh token's session (a replayed one is accepted within a grace period, §16), else a new
    sign-in with the password."""
    response = fetch('POST', f"{tokens['pds']}/xrpc/com.atproto.server.refreshSession",
                     headers={'Authorization': f"Bearer {tokens['refresh_token']}"})
    if response.status_code < 400:
        return {**_tokens(response.json(), tokens['password']), 'pds': tokens['pds']}
    if response.status_code == 429 or response.status_code >= 500:
        check(response)
    return sign_in(tokens['did'], tokens['password'])


class HostedSession(Session):
    """A hosted account's tokens as an app.atproto Session: each call with its own access token."""

    def __init__(self, tokens: dict):
        self.tokens = tokens
        self.did = tokens['did']
        self.pds = tokens['pds']

    def xrpc(self, method: str, nsid: str, **kwargs):
        headers = {**kwargs.pop('headers', {}), 'Authorization': f"Bearer {self.tokens['access_token']}"}
        stream = kwargs.pop('stream', None)
        if stream is not None:
            stream.seek(0)
            kwargs['content'] = iter(lambda: stream.read(VIDEO_CHUNK), b'')
        response = fetch(method, f'{self.pds}/xrpc/{nsid}', seconds=kwargs.pop('seconds', 120), headers=headers,
                         **kwargs)
        if response.status_code == 400 and _error_of(response) == 'ExpiredToken':
            raise Transient(_('The Bluesky session expired: trying again.'))
        return check(response)


def spend(kind: str) -> None:
    """Take a write's turn in the PDS-wide budget, or wait for it (QuotaWait: no attempt is spent)."""
    wait = budget.take(kind)
    if wait is not None:
        raise QuotaWait(_('Waiting: Bluesky takes only so many posts at once from this site.'),
                        utcnow() + timedelta(seconds=wait))


# --------------------------------------------------------------------------
# who may create one, and which handle
# --------------------------------------------------------------------------

def home_domain(user):
    return storefront.domain_by_id(user.site_domain_id)


def hosting_on(domain_id: int | None) -> bool:
    """Members of this domain may create accounts: the PDS is set, Bluesky is on for the domain, the switch is on
    (the domain's own, else the instance's), and the PDS gives handles under the domain, which federates and is no
    onion."""
    if not configured() or not enabled(SERVICES.get('bluesky'), domain_id):
        return False
    domain = storefront.domain_by_id(domain_id)
    if not domain.federates or domain.is_onion:
        return False
    switch = domain_switch(HOSTING, domain.id)
    if switch == 'off' or (switch is None and not get_setting(setting_name(HOSTING), False)):
        return False
    return domain.domain in handle_domains()


def label_of(name: str) -> str:
    """A member's name as the first label of a handle (A9): lower case, `_` and `.` as `-`, other characters
    dropped, within the PDS's 3 to 18 characters."""
    label = re.sub(r'[^a-z0-9-]', '', re.sub(r'[_.]', '-', (name or '').lower()))
    label = re.sub(r'-{2,}', '-', label).strip('-')[:LABEL_MAX].rstrip('-')
    if len(label) < LABEL_MIN:
        label = f'{label}-pf' if label else 'member'
    return label


def alternative(label: str, number: int = 2) -> str:
    """The label with a number after it, within the length the PDS takes: `alice` -> `alice-2`."""
    suffix = f'-{number}'
    return f'{label[:LABEL_MAX - len(suffix)].rstrip("-")}{suffix}'


def number_of(base: str, label: str) -> int | None:
    """Which of the member's own labels this is: 1 for the name itself, n for its n-th alternative, None for any
    other."""
    if label == base:
        return 1
    return next((number for number in range(2, MAX_NUMBER + 1) if alternative(base, number) == label), None)


def belongs_to_another(user, label: str) -> bool:
    """Another member of the same home domain has `label` as their own name (bob_2's is `bob-2`): nobody else may
    take it as a numbered alternative. Read on Create and rename only, so a plain scan of the domain's names."""
    domain = User.site_domain_id.is_(None) if user.site_domain_id is None else User.site_domain_id == user.site_domain_id
    names = db.session.query(User.user_name).filter(User.ap_id.is_(None), User.id != user.id, domain)
    return any(label_of(name) == label for (name,) in names)


def own_label(user, wanted: str | None = None) -> str:
    """The label a member may have: their own name as a label, or one of its numbered alternatives (A9) that is not
    another member's own name. Nobody may take another member's name, or a name of the site's: HandleRefused offers
    one of their own."""
    base = label_of(user.user_name)
    wanted = (wanted or '').strip().lower() or base
    if number_of(base, wanted) is None:
        raise HandleRefused(_('A Bluesky handle here is your own name.'), base)
    if wanted != base and belongs_to_another(user, wanted):
        raise HandleRefused(_("@%(handle)s is another member's name.", handle=handle_of(user, wanted)),
                            next_label(user, wanted))
    return wanted


def next_label(user, label: str) -> str:
    """The member's next own label after `label`, past any that is another member's own name."""
    base = label_of(user.user_name)
    for number in range((number_of(base, label) or 1) + 1, MAX_NUMBER + 1):
        if not belongs_to_another(user, alternative(base, number)):
            return alternative(base, number)
    raise HandleRefused(_('No handle of your name is free.'), base)


def handle_of(user, label: str) -> str:
    return f'{label}.{home_domain(user).domain}'


# --------------------------------------------------------------------------
# slots
# --------------------------------------------------------------------------

def hosted_rows():
    return SocialConnection.query.filter_by(service='bluesky', variant=HOSTED)


def has_account(connection: SocialConnection) -> bool:
    return connection.remote_id.startswith('did:')


def active_count() -> int:
    return hosted_rows().filter_by(slot_state=ACTIVE).count()


def account_count() -> int:
    return hosted_rows().filter(SocialConnection.remote_id.like('did:%')).count()


def slot_free(new_account: bool) -> bool:
    if active_count() >= current_app.config['ATPROTO_MAX_ACCOUNTS']:
        return False
    return not new_account or account_count() < current_app.config['ATPROTO_HOSTED_LIMIT']


def followers(user_id: int) -> int:
    """Everyone who follows the member, here and elsewhere (§9's order)."""
    return UserFollower.query.filter(UserFollower.is_accepted.is_(True), db.or_(
        db.and_(UserFollower.local_user_id == user_id, UserFollower.is_inward.is_(True)),
        db.and_(UserFollower.remote_user_id == user_id, UserFollower.is_inward.is_(False)))).count()


def _rank(rows: list) -> list:
    """Rows in §9's order: most followers first, then who joined first."""
    users = {user.id: user for user in User.query.filter(User.id.in_([row.user_id for row in rows])).all()}
    return sorted(rows, key=lambda row: (-followers(row.user_id), users[row.user_id].created or utcnow(), row.id))


def waiting_list() -> list:
    return _rank(hosted_rows().filter_by(slot_state=WAITING).all())


def place_of(connection: SocialConnection) -> int:
    return next(number for number, row in enumerate(waiting_list(), 1) if row.id == connection.id)


def hosted_of(user) -> SocialConnection | None:
    return hosted_rows().filter_by(user_id=user.id).first()


# --------------------------------------------------------------------------
# an account's life
# --------------------------------------------------------------------------

def _space_creations() -> None:
    """Account creations at most one a second, across workers."""
    for _attempt in range(3):
        if app_pkg.redis_client.set(CREATE_KEY, '1', nx=True, px=CREATE_SPACING_MS):
            return
        time.sleep(CREATE_SPACING_MS / 1000)
    raise Transient(_('Bluesky accounts are being made for others just now: try again in a moment.'))


def create_account(user, label: str) -> dict:
    """The account on the PDS, under `<label>.<home domain>`: its tokens. HandleRefused offers the next label."""
    handle = handle_of(user, label)
    password = secrets.token_urlsafe(32)
    body = {'handle': handle, 'email': f'{secrets.token_hex(12)}@{ACCOUNT_EMAIL_DOMAIN}', 'password': password}
    if describe().get('inviteCodeRequired'):
        body['inviteCode'] = call('POST', f'{pds_url()}/xrpc/com.atproto.server.createInviteCode', auth=_admin(),
                                  json={'useCount': 1}).json()['code']
    _space_creations()
    response = fetch('POST', f'{pds_url()}/xrpc/com.atproto.server.createAccount', json=body)
    if _handle_refused(response):
        raise HandleRefused(_('@%(handle)s cannot be had.', handle=handle), next_label(user, label))
    return _tokens(check(response).json(), password)


def _become_identity(connection: SocialConnection) -> None:
    """A created account is always the member's identity (A8): any other gives way."""
    SocialConnection.query.filter(SocialConnection.user_id == connection.user_id,
                                  SocialConnection.id != connection.id).update({'is_identity': False})
    db.session.flush()
    connection.is_identity = True


def _start(connection: SocialConnection, tokens: dict) -> None:
    """The account is there and active: store it, and send its profile and recent posts. The caller commits."""
    connection.remote_id, connection.remote_name = tokens['did'], tokens['handle'][:255]
    connection.sealed_tokens = seal_tokens(tokens)
    connection.slot_state, connection.state, connection.last_error = ACTIVE, 'ok', None
    _become_identity(connection)


def request_account(user, label: str | None = None) -> SocialConnection:
    """The member asked for an account (Create): made now when a slot is free, else they wait. A member who already
    has one gets it back (reactivated, or still waiting). Raises HandleRefused when the PDS will not give the
    handle, and ServiceError when it cannot be reached."""
    connection = hosted_of(user)
    if connection is not None and connection.slot_state == DELETING and delete_account(connection):
        connection = None   # the old one is gone at last: a new one may be made
    if connection is not None:
        if connection.slot_state == DELETING:
            raise Transient(_('Your last Bluesky account is still being deleted: try again later.'))
        if connection.slot_state == DEACTIVATED:
            reactivate(connection)
        return connection
    label = own_label(user, label)
    connection = SocialConnection(user_id=user.id, service='bluesky', variant=HOSTED, default_on=False,
                                  import_own=True, remote_id=f'waiting:{user.id}', remote_name=handle_of(user, label),
                                  sealed_tokens=seal_tokens({'label': label}), slot_state=WAITING)
    if slot_free(new_account=True):
        tokens = create_account(user, label)
        db.session.add(connection)
        _start(connection, tokens)
        db.session.commit()
        _after_start(connection)
        return connection
    db.session.add(connection)
    db.session.commit()
    return connection


def _after_start(connection: SocialConnection) -> None:
    put_profile_later(connection.user_id)
    backfill(connection)


def grant(connection: SocialConnection) -> bool:
    """Give a waiting member their slot: reactivate their account, or make it under the handle they asked for (or
    the next free one). Whether it worked; the member is told."""
    try:
        if has_account(connection):
            tokens = sign_in(connection.remote_id, open_tokens(connection)['password'])
            HostedSession(tokens).xrpc('POST', 'com.atproto.server.activateAccount')
        else:
            try:   # checked again: the label a row was stored with may no longer be this member's to have
                label = own_label(connection.user, open_tokens(connection).get('label'))
            except HandleRefused as refused:
                label = refused.suggestion
            for _attempt in range(5):
                try:
                    tokens = create_account(connection.user, label)
                    break
                except HandleRefused as refused:
                    label = refused.suggestion
            else:
                return False
    except ServiceError as failure:
        current_app.logger.info(f'atproto: slot for connection {connection.id} not granted: {failure.message}')
        return False
    _start(connection, tokens)
    db.session.add(Notification(
        title=_('Your Bluesky account @%(handle)s is ready', handle=connection.remote_name)[:150],
        url='/user/settings/connections', user_id=connection.user_id, author_id=connection.user_id,
        notif_type=NOTIF_DEFAULT, subtype='bluesky_slot', targets={'connection_id': connection.id}))
    db.session.commit()
    _after_start(connection)
    return True


def deactivate(connection: SocialConnection, state: str = DEACTIVATED) -> None:
    """The account reads as deactivated on Bluesky and keeps its DID and handle (§8). `state` is DEACTIVATED (the
    member turned it off) or WAITING (its slot was freed)."""
    spend('delete')
    HostedSession(usable_tokens(connection.id, SERVICES['bluesky'])).xrpc(
        'POST', 'com.atproto.server.deactivateAccount', json={})
    connection.slot_state = state
    connection.slot_pinned = False
    db.session.commit()


def reactivate(connection: SocialConnection) -> None:
    """The member turned their account on again: at once when a slot is free, else they wait for one."""
    if slot_free(new_account=False):
        if not grant(connection):
            raise Transient(_('Bluesky could not be reached: try again in a moment.'))
    else:
        connection.slot_state = WAITING
        db.session.commit()


def delete_account(connection: SocialConnection) -> bool:
    """The account goes from the PDS for good (the member deleted it, or their PieFed account went), and then its
    row. Until the PDS confirms it, the row stays, DELETING (it takes no copies and holds no place), with the
    password that reaches the account, and the daily task asks again. Whether it is gone."""
    if has_account(connection):
        try:
            if not configured():
                raise Transient(_('The Bluesky server is not set up here.'))
            call('POST', f'{pds_url()}/xrpc/com.atproto.admin.deleteAccount', auth=_admin(),
                 json={'did': connection.remote_id})
        except ServiceError as failure:
            current_app.logger.warning(f'atproto: deleting {connection.remote_id} failed: {failure.message}')
            connection.slot_state, connection.slot_pinned = DELETING, False
            connection.is_identity = False
            db.session.commit()
            return False
    db.session.delete(connection)
    db.session.commit()
    return True


def rename(connection: SocialConnection, label: str) -> str:
    """A new handle under the same domain (§8): the DID stays, the old handle stops resolving at once. The new
    handle; HandleRefused when it cannot be had."""
    label = own_label(connection.user, label)
    handle = handle_of(connection.user, label)
    spend('profile')
    response = fetch('POST', f'{pds_url()}/xrpc/com.atproto.identity.updateHandle', json={'handle': handle},
                     headers={'Authorization': f"Bearer {usable_tokens(connection.id, SERVICES['bluesky'])['access_token']}"})
    if _handle_refused(response):
        raise HandleRefused(_('@%(handle)s cannot be had.', handle=handle), next_label(connection.user, label))
    check(response)
    connection.remote_name = handle
    db.session.commit()
    return handle


def app_password(connection: SocialConnection) -> str:
    """A new app password, for signing in to Bluesky apps as this account (§8). Shown once, never stored."""
    session = HostedSession(usable_tokens(connection.id, SERVICES['bluesky']))
    name = f'App password {utcnow():%Y-%m-%d %H:%M:%S}'
    return session.xrpc('POST', 'com.atproto.server.createAppPassword', json={'name': name}).json()['password']


# --------------------------------------------------------------------------
# the profile
# --------------------------------------------------------------------------

def profile_record(user, session: Session) -> dict:
    """app.bsky.actor.profile for the member: their name, their bio with a link to their profile here, their
    avatar and banner."""
    link = user.public_url()
    about = MARKDOWN_LINK.sub(lambda m: f'{m.group(1)} {m.group(2)}'.strip(), user.about or '').strip()
    description = f'{cut(about, 256 - len(link) - 2)}\n\n{link}' if about else link
    record = {'$type': 'app.bsky.actor.profile', 'displayName': cut((user.title or user.user_name or '').strip(), 64),
              'description': description[:256]}
    for field, file in (('avatar', user.avatar), ('banner', user.cover)):
        data = image_bytes(file) if file is not None else None
        jpeg = _fit(data, PROFILE_IMAGE_BYTES, 2000) if data else None
        if jpeg is not None:
            record[field] = session.xrpc('POST', 'com.atproto.repo.uploadBlob', content=jpeg[0],
                                         headers={'Content-Type': 'image/jpeg'}).json()['blob']
    return record


def put_profile(user_id: int) -> int | None:
    """Send the member's profile to their hosted account, if it is active: None when done (or nothing to do), else
    the seconds to wait for the budget."""
    connection = hosted_rows().filter_by(user_id=user_id, slot_state=ACTIVE).first()
    if connection is None:
        return None
    wait = budget.take('profile')
    if wait is not None:
        return wait
    session = HostedSession(usable_tokens(connection.id, SERVICES['bluesky']))
    session.xrpc('POST', 'com.atproto.repo.putRecord', json={
        'repo': session.did, 'collection': 'app.bsky.actor.profile', 'rkey': 'self',
        'record': profile_record(connection.user, session)})
    return None


@celery.task(bind=True, max_retries=None)
def profile_task(self, user_id: int) -> None:
    session = get_task_session()
    try:
        with patch_db_session(session):
            try:
                wait = put_profile(user_id)
            except ServiceError as failure:
                current_app.logger.info(f'atproto: profile of user {user_id} not sent: {failure.message}')
                return
    finally:
        session.close()
    if wait is not None:
        raise self.retry(countdown=wait)


def profile_changed(user) -> None:
    """The member saved their profile: their hosted account, if active, shows it too."""
    if configured() and hosted_rows().filter_by(user_id=user.id, slot_state=ACTIVE).first() is not None:
        put_profile_later(user.id)


def put_profile_later(user_id: int) -> None:
    """The member's profile changed, or their account started: send it, once the budget allows."""
    if current_app.debug:
        profile_task.apply(args=(user_id,))
    else:
        profile_task.delay(user_id)


# --------------------------------------------------------------------------
# backfill and the daily task
# --------------------------------------------------------------------------

def backfill(connection: SocialConnection) -> int:
    """Queue the member's latest microblog posts for their new account, with their own times, last in the budget
    (§6). How many."""
    service = SERVICES['bluesky']
    candidates = Post.query.join(Community, Community.id == Post.community_id).filter(
        Post.user_id == connection.user_id, Community.kind == COMMUNITY_KIND_MICROBLOGS,
        Post.deleted.is_(False)).order_by(Post.posted_at.desc()).limit(BACKFILL * 4).all()
    done = {row.post_id for row in SocialCrosspost.query.filter_by(connection_id=connection.id)}
    queued = 0
    for post in candidates:
        if queued == BACKFILL:
            break
        if post.id in done or not post.community.is_local() or refusal(post, connection, service) is not None:
            continue
        db.session.add(SocialCrosspost(post_id=post.id, connection_id=connection.id, service='bluesky',
                                       variant=HOSTED, state='queued', next_attempt_at=utcnow(),
                                       progress={'backfill': True}))
        queued += 1
    db.session.commit()
    return queued


def _last_copy(connection: SocialConnection):
    row = SocialCrosspost.query.filter(SocialCrosspost.connection_id == connection.id,
                                       SocialCrosspost.sent_at.isnot(None)).order_by(
        SocialCrosspost.sent_at.desc()).first()
    return row.sent_at if row is not None else connection.created_at


def daily() -> dict:
    """Free slots by the policy, then fill free slots from the waiting list in order (§9). What it did."""
    freed = granted = 0
    if not configured():
        return {'deleted': 0, 'freed': 0, 'granted': 0}
    deleted = sum(delete_account(row) for row in hosted_rows().filter_by(slot_state=DELETING).all())
    policy = current_app.config.get('ATPROTO_SLOT_POLICY')
    active = hosted_rows().filter_by(slot_state=ACTIVE).all()
    if policy == 'ranked':
        keep = current_app.config['ATPROTO_MAX_ACCOUNTS'] - sum(row.slot_pinned for row in active)
        candidates = [row for row in active if not row.slot_pinned] + waiting_list()
        ranked = _rank(candidates)[:max(0, keep)]
        leaving = [row for row in active if not row.slot_pinned and row not in ranked]
    else:
        leaving = [row for row in active if not row.slot_pinned and _last_copy(row) < utcnow() - INACTIVE]
    for row in leaving:   # ranked: they wait to rank again; sticky: off until the member turns it on again
        try:
            deactivate(row, WAITING if policy == 'ranked' else DEACTIVATED)
            freed += 1
            if policy != 'ranked':
                db.session.add(Notification(
                    title=_('Your Bluesky account @%(handle)s was turned off after 90 days without posts: turn it on '
                            'again any time', handle=row.remote_name)[:150],
                    url='/user/settings/connections', user_id=row.user_id, author_id=row.user_id,
                    notif_type=NOTIF_DEFAULT, subtype='bluesky_slot_freed', targets={'connection_id': row.id}))
                db.session.commit()
        except ServiceError as failure:
            current_app.logger.info(f'atproto: slot of connection {row.id} not freed: {failure.message}')
            db.session.rollback()
    for row in waiting_list():
        if not slot_free(new_account=not has_account(row)):
            if active_count() >= current_app.config['ATPROTO_MAX_ACCOUNTS']:
                break
            continue   # the PDS is full of accounts: only those who have one may come back
        granted += grant(row)
    return {'deleted': deleted, 'freed': freed, 'granted': granted}


@celery.task
def daily_task() -> None:
    session = get_task_session()
    try:
        with patch_db_session(session):
            daily()
    finally:
        session.close()
