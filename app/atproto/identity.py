"""A member's Bluesky identity at work (ATProto spec 2026-11-23 §6, A8): what they do here to ATProto content
becomes a record in their own repo, through whichever kind of account their identity is (hosted or OAuth).

    upvote of an ATProto post or reply   app.bsky.feed.like       (un-voting, or a downvote, deletes it)
    boost of an ATProto post             app.bsky.feed.repost
    reply in a thread on Bluesky         app.bsky.feed.post with `reply {root, parent}` (deleted with the reply)
    follow of an ATProto account         app.bsky.graph.follow
    block of an ATProto account          app.bsky.graph.block

Each is reconciled rather than replayed: `sync` compares what PieFed holds now (the vote, the boost, the reply, the
follow, the block) with the record the identity wrote (an `atproto_record` row, direction out), and makes or deletes
the record to match. So a like and its undo arriving in either order, a retry, or a lost task all end in the same
place. The row is written, with its record key, before the record is made: a write whose answer was lost is found
on the PDS the next time rather than made twice, and the record's echo from Jetstream is known as ours meanwhile.

A member with no identity acts here as before; nothing is sent. When an identity appears (`caught_up`), what they
did recently, and every follow and block, is reconciled, so the action they took before setting it up completes.
"""
from datetime import timedelta

import redis
from flask import current_app
from sqlalchemy import and_, or_

import app as app_pkg
from app import celery, db, storefront
from app.atproto import Session, actors
from app.atproto.records import POST, facets, new_tid, now
from app.atproto.resolve import did_document, pds_of
from app.clock import utcnow
from app.connections import SERVICES, QuotaWait, ServiceError, Transient
from app.connections import bluesky_hosted as hosted
from app.connections.apps import enabled
from app.connections.bluesky import OAuthSession, client_key
from app.connections.content import MARKDOWN_LINK, cut
from app.connections.http import call, check, fetch
from app.connections.tokens import REAUTH, usable_tokens
from app.local_only import community_local_only, user_local_only
from app.models import AtprotoRecord, Post, PostBoost, PostReply, PostReplyVote, PostVote, SocialConnection, \
    SocialCrosspost, User, UserBlock, UserFollower
from app.utils import get_task_session, patch_db_session, user_banned_from_community
from app.visibility import is_open

LIKE, REPOST = 'app.bsky.feed.like', 'app.bsky.feed.repost'
FOLLOW, BLOCK = 'app.bsky.graph.follow', 'app.bsky.graph.block'
# kind: (collection, the atproto_record column naming its subject, its share of the PDS-wide budget)
KINDS = {
    'like_post': (LIKE, 'post_id', 'like'),
    'like_reply': (LIKE, 'post_reply_id', 'like'),
    'repost': (REPOST, 'post_id', 'like'),
    'reply': (POST, 'post_reply_id', 'post'),
    'follow': (FOLLOW, 'user_id', 'follow'),
    'block': (BLOCK, 'user_id', 'follow'),
}
MAX_GRAPHEMES = 300
BACKOFF = (30, 120, 600, 1800, 3600)   # seconds before each retry of a write that failed for now
LOCK = 'atproto:identity:'           # + user, kind, subject: one reconciling of a record at a time
LOCK_SECONDS = 120
CATCH_UP = timedelta(days=7)           # how far back an identity's first sync reaches for votes, boosts, replies


class Busy(Exception):
    """Another worker is reconciling the same record."""


# --------------------------------------------------------------------------
# whose identity, and what a thing is on ATProto
# --------------------------------------------------------------------------

def can_write(connection: SocialConnection | None) -> bool:
    """Whether the connection can write now: not waiting to be reconnected, and, hosted here, active."""
    return connection is not None and connection.state != REAUTH and (
        connection.variant != hosted.HOSTED or connection.slot_state == hosted.ACTIVE)


def identity_of(user_id: int) -> SocialConnection | None:
    """The member's Bluesky identity, when it can write."""
    connection = SocialConnection.query.filter_by(user_id=user_id, service='bluesky', is_identity=True).first()
    return connection if can_write(connection) else None


def session_of(tokens: dict) -> Session:
    if tokens.get('kind') == hosted.HOSTED:
        return hosted.HostedSession(tokens)
    return OAuthSession(tokens)


def record_of(item) -> tuple[str, str | None] | None:
    """The ATProto post a PieFed post or reply is, as (uri, cid): one that came in, a member's reply their identity
    sent, or (a post) its Bluesky copy, whose cid is read when needed. None when it is none."""
    if isinstance(item, PostReply):
        where = AtprotoRecord.post_reply_id == item.id
    else:
        where = and_(AtprotoRecord.post_id == item.id, AtprotoRecord.post_reply_id.is_(None))
    row = AtprotoRecord.query.filter(where, AtprotoRecord.collection == POST, or_(
        AtprotoRecord.direction == 'in', AtprotoRecord.cid.is_not(None))).order_by(AtprotoRecord.id).first()
    if row is not None:
        return row.uri, row.cid
    if isinstance(item, Post):
        copy = SocialCrosspost.query.filter(SocialCrosspost.post_id == item.id, SocialCrosspost.service == 'bluesky',
                                            SocialCrosspost.state.in_(('sent', 'editing')),
                                            SocialCrosspost.remote_id.like('at://%')).order_by(
            SocialCrosspost.id).first()
        if copy is not None:
            return copy.remote_id, None
    return None


def strong_ref(found: tuple[str, str | None]) -> dict:
    """A record's strong reference ({uri, cid}): the cid read from its author's PDS when PieFed has none."""
    uri, cid = found
    if not cid:
        did, collection, rkey = uri.removeprefix('at://').split('/', 2)
        answer = call('GET', f'{pds_of(did_document(did))}/xrpc/com.atproto.repo.getRecord',
                      params={'repo': did, 'collection': collection, 'rkey': rkey}).json()
        cid = answer.get('cid') if isinstance(answer, dict) else None
        if not isinstance(cid, str) or not cid:
            raise Transient(f'{uri} has no cid yet')
    return {'uri': uri, 'cid': cid}


def thread_of(reply: PostReply) -> tuple | None:
    """A member's reply's (root, parent) records, when its thread is on Bluesky: the post and the reply it answers
    both have records, and one of them is an ATProto account's."""
    parent = db.session.get(PostReply, reply.parent_id) if reply.parent_id else reply.post
    post = reply.post
    if parent is None or post is None or not (actors.is_atproto(post.author) or actors.is_atproto(parent.author)):
        return None
    root, answered = record_of(post), record_of(parent)
    return (root, answered) if root is not None and answered is not None else None


# --------------------------------------------------------------------------
# what PieFed holds now
# --------------------------------------------------------------------------

def wanted(user_id: int, kind: str, target_id: int):
    """What the identity's record of this should say, or None when there should be none: the subject's record
    (a like or repost), the reply itself (a reply), or the account (a follow or block)."""
    if kind in ('like_post', 'repost'):
        post = db.session.get(Post, target_id)
        if post is None or post.deleted or not actors.is_atproto(post.author):
            return None
        model = PostVote if kind == 'like_post' else PostBoost
        mine = model.query.filter_by(user_id=user_id, post_id=post.id)
        if kind == 'like_post':   # a public upvote: a private one is never marked (`voted`)
            mine = mine.filter(PostVote.effect > 0, PostVote.atproto_like.is_(True))
        if not may_tell(user_id, post):
            return None
        return post if mine.first() is not None else None
    if kind == 'like_reply':
        reply = db.session.get(PostReply, target_id)
        if reply is None or reply.deleted or not actors.is_atproto(reply.author) or not may_tell(user_id, reply):
            return None
        mine = PostReplyVote.query.filter(PostReplyVote.user_id == user_id, PostReplyVote.post_reply_id == reply.id,
                                          PostReplyVote.effect > 0, PostReplyVote.atproto_like.is_(True)).first()
        return reply if mine is not None else None
    if kind == 'reply':
        reply = db.session.get(PostReply, target_id)
        if reply is None or reply.user_id != user_id or reply.deleted or not is_open(reply) or \
                user_local_only(reply.author) or community_local_only(reply.community):
            return None
        return reply if thread_of(reply) is not None else None
    actor = db.session.get(User, target_id)
    if actor is None or not actors.is_atproto(actor):
        return None
    if kind == 'follow':
        mine = UserFollower.query.filter_by(local_user_id=user_id, remote_user_id=actor.id, is_inward=False,
                                            is_accepted=True).first()
    else:
        mine = UserBlock.query.filter_by(blocker_id=user_id, blocked_id=actor.id).first()
    return actor if mine is not None else None


def reply_text(reply: PostReply) -> str:
    """The reply as a post's text: within 300 characters, with the link back when it is cut. Behind a content
    warning, the warning and the link."""
    link = reply.ap_id or ''
    if reply.content_warning:
        return cut(f'CW: {reply.content_warning}', MAX_GRAPHEMES - len(link) - 1) + '\n' + link
    text = MARKDOWN_LINK.sub(lambda m: f'{m.group(1)} {m.group(2)}'.strip(), reply.body or '').strip()
    if len(text) <= MAX_GRAPHEMES:
        return text
    return f'{cut(text, MAX_GRAPHEMES - len(link) - 1)}\n{link}'


def build(kind: str, subject) -> dict:
    """The record for `kind` about `subject` (what `wanted` answered)."""
    collection = KINDS[kind][0]
    if kind in ('follow', 'block'):
        return {'$type': collection, 'subject': actors.did_of(subject), 'createdAt': now()}
    if kind != 'reply':
        return {'$type': collection, 'subject': strong_ref(record_of(subject)), 'createdAt': now()}
    root, parent = thread_of(subject)
    record = {'$type': POST, 'text': reply_text(subject), 'createdAt': now(),
              'reply': {'root': strong_ref(root), 'parent': strong_ref(parent)}}
    if found := facets(record['text']):
        record['facets'] = found
    if subject.language is not None and subject.language.code and subject.language.code != 'und':
        record['langs'] = [subject.language.code]
    return record


# --------------------------------------------------------------------------
# reconciling
# --------------------------------------------------------------------------

def written(user_id: int, kind: str, target_id: int) -> AtprotoRecord | None:
    """The record any of the member's Bluesky connections wrote for this, made or still being made."""
    collection, column, _share = KINDS[kind]
    return AtprotoRecord.query.join(SocialConnection, SocialConnection.id == AtprotoRecord.connection_id).filter(
        SocialConnection.user_id == user_id, AtprotoRecord.direction == 'out', AtprotoRecord.collection == collection,
        getattr(AtprotoRecord, column) == target_id).first()


def _spend(tokens: dict, share: str) -> None:
    if tokens.get('kind') == hosted.HOSTED:
        hosted.spend(share)


def _exists(session: Session, row: AtprotoRecord) -> str | None:
    """The cid of the record under the row's key, when an earlier attempt made it (records are public: no token)."""
    response = fetch('GET', f'{session.pds}/xrpc/com.atproto.repo.getRecord',
                     params={'repo': session.did, 'collection': row.collection, 'rkey': row.rkey})
    if response.status_code == 400:   # RecordNotFound: the attempt never made it
        return None
    cid = check(response).json().get('cid')
    return cid if isinstance(cid, str) and cid else None


def make(connection: SocialConnection, row: AtprotoRecord | None, kind: str, target_id: int, subject) -> str:
    """Write the record (or find the one an earlier attempt wrote) and keep its cid. Its URI."""
    collection, column, share = KINDS[kind]
    tokens = usable_tokens(connection.id, SERVICES['bluesky'])
    session = session_of(tokens)
    if row is not None and (cid := _exists(session, row)) is not None:
        row.cid = cid
        db.session.commit()
        return row.uri
    record = build(kind, subject)
    if row is None:
        rkey = new_tid()
        row = AtprotoRecord(uri=f'at://{session.did}/{collection}/{rkey}', did=session.did, collection=collection,
                            rkey=rkey, direction='out', connection_id=connection.id, **{column: target_id})
        db.session.add(row)
        db.session.commit()
    _spend(tokens, share)
    answer = session.xrpc('POST', 'com.atproto.repo.createRecord', json={
        'repo': session.did, 'collection': collection, 'rkey': row.rkey, 'record': record}).json()
    row.cid = str(answer.get('cid') or '-')
    db.session.commit()
    return row.uri


def unmake(row: AtprotoRecord) -> None:
    """Delete the record and forget it."""
    tokens = usable_tokens(row.connection_id, SERVICES['bluesky'])
    session = session_of(tokens)
    _spend(tokens, 'delete')
    session.xrpc('POST', 'com.atproto.repo.deleteRecord', json={
        'repo': session.did, 'collection': row.collection, 'rkey': row.rkey})
    db.session.delete(row)
    db.session.commit()


def sync(user_id: int, kind: str, target_id: int) -> str | None:
    """Make the identity's record match what PieFed holds: 'made', 'deleted', or None (nothing to do). Raises
    Busy while another worker does the same, and ServiceError (QuotaWait, Transient...) as the calls do."""
    lock = app_pkg.redis_client.lock(f'{LOCK}{user_id}:{kind}:{target_id}', timeout=LOCK_SECONDS,
                                     blocking_timeout=0)
    if not lock.acquire(blocking=False):
        raise Busy()
    try:
        row = written(user_id, kind, target_id)
        subject = wanted(user_id, kind, target_id)
        if row is None and subject is None or row is not None and subject is not None and row.cid:
            return None
        connection = db.session.get(SocialConnection, row.connection_id) if row is not None else identity_of(user_id)
        if not can_write(connection):   # turned off or to be reconnected: caught up when it can again
            return None
        if subject is None:
            unmake(row)
            return 'deleted'
        make(connection, row, kind, target_id, subject)
        return 'made'
    finally:
        try:
            lock.release()
        except redis.exceptions.LockError:   # it outlived LOCK_SECONDS: another may hold it now
            pass


@celery.task(bind=True, max_retries=None)
def sync_task(self, user_id: int, kind: str, target_id: int) -> None:
    session = get_task_session()
    wait = None
    try:
        with patch_db_session(session):
            try:
                sync(user_id, kind, target_id)
            except Busy:
                wait = 5
            except QuotaWait as quota:
                wait = max(1, int((quota.until - utcnow()).total_seconds()))
            except Transient as failure:
                if self.request.retries < len(BACKOFF):
                    wait = failure.retry_after or BACKOFF[self.request.retries]
                else:
                    current_app.logger.info(f'atproto: {kind} {target_id} of user {user_id} gave up: {failure.message}')
            except ServiceError as failure:
                current_app.logger.info(f'atproto: {kind} {target_id} of user {user_id} not sent: {failure.message}')
    finally:
        session.close()
    if wait is not None:
        raise self.retry(countdown=wait)


def later(user_id: int, kind: str, target_id: int) -> None:
    if current_app.debug:
        sync_task.apply(args=(user_id, kind, target_id))
    else:
        sync_task.delay(user_id, kind, target_id)


# --------------------------------------------------------------------------
# what the action paths call
# --------------------------------------------------------------------------

def involved(user_id: int) -> bool:
    """Whether the member has an identity, or records one wrote: else nothing they do here goes to Bluesky."""
    return identity_of(user_id) is not None or db.session.query(AtprotoRecord.id).join(
        SocialConnection, SocialConnection.id == AtprotoRecord.connection_id).filter(
        SocialConnection.user_id == user_id, AtprotoRecord.direction == 'out').first() is not None


def may_tell(user_id: int, item) -> bool:
    """Whether the member's action on a post or reply may be told outside this site: as send_vote decides for an
    ActivityPub vote, the community federates and is not private, the member is not banned from it, and the post or
    reply is open to anyone."""
    community = item.community
    return community is not None and not community.local_only and not community.private and \
        not community_local_only(community) and not user_banned_from_community(user_id, community.id) and \
        is_open(item)


def voted(user_id: int, item, public: bool) -> None:
    """The member voted on a post or reply, or took the vote back. A public upvote that may be told is marked
    (`atproto_like`), whether or not the member has an identity yet, so a catch-up later sends it; a private vote
    is unmarked, so it never is, and a like made before goes."""
    if not actors.is_atproto(item.author):
        return
    model, column = (PostReplyVote, PostReplyVote.post_reply_id) if isinstance(item, PostReply) else \
        (PostVote, PostVote.post_id)
    model.query.filter(model.user_id == user_id, column == item.id).update(
        {'atproto_like': public and may_tell(user_id, item)}, synchronize_session=False)
    db.session.commit()
    if involved(user_id):
        later(user_id, 'like_reply' if isinstance(item, PostReply) else 'like_post', item.id)


def boosted(user_id: int, post: Post) -> None:
    if actors.is_atproto(post.author) and involved(user_id):
        later(user_id, 'repost', post.id)


def replied(reply: PostReply) -> None:
    """The member made, deleted, restored, or had removed a reply."""
    if reply.user_id is not None and reply.author is not None and reply.author.is_local() and involved(reply.user_id):
        later(reply.user_id, 'reply', reply.id)


def followed(user_id: int, actor: User) -> None:
    if actors.is_atproto(actor) and involved(user_id):
        later(user_id, 'follow', actor.id)


def blocked(user_id: int, actor: User) -> None:
    if actors.is_atproto(actor) and involved(user_id):
        later(user_id, 'block', actor.id)


def caught_up(user_id: int) -> int:
    """The member has an identity now: what they did before it (every follow and block of an ATProto account, and
    their votes, boosts and replies of the last CATCH_UP) is reconciled. How many were looked at."""
    since = utcnow() - CATCH_UP
    domain = User.ap_domain == actors.DOMAIN
    work = [('follow', actor_id) for (actor_id,) in db.session.query(UserFollower.remote_user_id).join(
        User, User.id == UserFollower.remote_user_id).filter(
        UserFollower.local_user_id == user_id, UserFollower.is_inward.is_(False), UserFollower.is_accepted.is_(True),
        domain)]
    work += [('block', actor_id) for (actor_id,) in db.session.query(UserBlock.blocked_id).join(
        User, User.id == UserBlock.blocked_id).filter(UserBlock.blocker_id == user_id, domain)]
    work += [('like_post', post_id) for (post_id,) in db.session.query(PostVote.post_id).join(
        User, User.id == PostVote.author_id).filter(PostVote.user_id == user_id, PostVote.effect > 0,
                                                    PostVote.atproto_like.is_(True), PostVote.created_at >= since,
                                                    domain)]
    work += [('like_reply', reply_id) for (reply_id,) in db.session.query(PostReplyVote.post_reply_id).join(
        User, User.id == PostReplyVote.author_id).filter(PostReplyVote.user_id == user_id, PostReplyVote.effect > 0,
                                                         PostReplyVote.atproto_like.is_(True),
                                                         PostReplyVote.created_at >= since, domain)]
    work += [('repost', post_id) for (post_id,) in db.session.query(PostBoost.post_id).join(
        Post, Post.id == PostBoost.post_id).join(User, User.id == Post.user_id).filter(
        PostBoost.user_id == user_id, PostBoost.created_at >= since, domain)]
    work += [('reply', reply_id) for (reply_id,) in db.session.query(PostReply.id).filter(
        PostReply.user_id == user_id, PostReply.posted_at >= since, PostReply.deleted.is_(False)).order_by(
        PostReply.id)]
    work += [kind_of(row) for row in AtprotoRecord.query.join(
        SocialConnection, SocialConnection.id == AtprotoRecord.connection_id).filter(
        SocialConnection.user_id == user_id, AtprotoRecord.direction == 'out')]   # undone while it could not write
    work = list(dict.fromkeys(item for item in work if item is not None))
    for kind, target_id in work:
        later(user_id, kind, target_id)
    return len(work)


def kind_of(row: AtprotoRecord) -> tuple[str, int] | None:
    """An out row's (kind, subject id): what `sync` takes to reconcile it."""
    for kind, (collection, column, _share) in KINDS.items():
        target = getattr(row, column)
        if collection == row.collection and target is not None:
            return kind, target
    return None


def sweep() -> int:
    """Records a lost task left half made (a row with no cid an hour on), of accounts that can write: reconciled
    again, at most hourly (the row's time moves on each time). How many."""
    stale = AtprotoRecord.query.join(SocialConnection, SocialConnection.id == AtprotoRecord.connection_id).filter(
        AtprotoRecord.direction == 'out', AtprotoRecord.cid.is_(None),
        AtprotoRecord.created_at < utcnow() - timedelta(hours=1), SocialConnection.state != REAUTH,
        or_(SocialConnection.variant != hosted.HOSTED, SocialConnection.slot_state == hosted.ACTIVE)).with_entities(
        SocialConnection.user_id, AtprotoRecord).all()
    work = []
    for user_id, row in stale:
        row.created_at = utcnow()
        found = kind_of(row)
        if found is not None:
            work.append((user_id, *found))
    db.session.commit()
    for item in work:
        later(*item)
    return len(work)


# --------------------------------------------------------------------------
# the in-place offer (§8)
# --------------------------------------------------------------------------

def offer(user, post) -> dict | None:
    """What a member with no Bluesky identity is offered on an ATProto post, so their likes, boosts and replies
    there reach Bluesky: Create first (when accounts may be made for their domain), then Connect an existing
    account (when this site is a Bluesky client). None for anyone else, or when neither is possible."""
    if not getattr(user, 'is_authenticated', False) or post is None or not actors.is_atproto(post.author):
        return None
    if SocialConnection.query.filter(SocialConnection.user_id == user.id, SocialConnection.service == 'bluesky',
                                     or_(SocialConnection.is_identity.is_(True),
                                         SocialConnection.variant == hosted.HOSTED)).first() is not None:
        return None   # an identity already, or one on the way (waiting, turned off)
    create = None
    if hosted.hosting_on(user.site_domain_id):
        try:
            label = hosted.own_label(user)
        except hosted.HandleRefused as refused:
            label = refused.suggestion
        create = {'label': label, 'handle': hosted.handle_of(user, label)}
    connect = client_key() is not None and enabled(SERVICES['bluesky'], user.site_domain_id)
    if create is None and not connect:
        return None
    home = storefront.domain_by_id(user.site_domain_id)
    return {'create': create, 'connect': connect, 'at_home': storefront.current_storefront().domain == home.domain,
            'settings': f'{storefront.domain_base_url(home)}/user/settings/connections'}
