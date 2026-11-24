"""ATProto records into PieFed (ATProto spec 2026-11-23 §7): what the consumer (app.atproto.jetstream) and a follow's
backfill hand over.

A post becomes what a Mastodon Note becomes, through the same code (create_post, create_post_reply): its text and
facets as HTML, its pictures or video as attachments read from the author's PDS, its language, published at its own
time. A post with no reply goes to the local microblogs community; a reply goes under its parent, which is fetched
from its author's PDS when PieFed does not have it, toward the root, at most MAX_HOPS. A repost is a boost. A delete
deletes. A post that quotes another names it as its Note's `quote`, shown as a card when PieFed has it.

What answers our own records comes back to them (§7): a reply to a member's post's Bluesky copy, or to a reply
their identity sent, lands under it; a like of one is the actor's upvote, a repost of a copy its boost; and a
follow of a member's identity makes the actor their follower. Other likes are dropped. A member's own Bluesky
accounts are never actors here (what they write outside PieFed is P5's import).

Self-labels make a post NSFW (`porn`, `sexual`, `nudity`) or NSFL (`graphic-media`), and `!no-unauthenticated`, on
the post or its author, keeps it from readers who are not signed in.

Every record that comes in is kept in `atproto_record` by its at:// URI, so one delivered twice (Jetstream delivers
at least once) is made once, and its delete finds what it made. A record under a URI PieFed made itself (a copy, or
an identity's own write) is its echo and is skipped.
"""
import html
from datetime import datetime, timezone

from flask import current_app
from flask_babel import _

from app import celery, db
from app.activitypub.util import create_post, create_post_reply, delete_post_or_comment, \
    find_microblogging_community, record_boost, remove_boost, undo_vote
from app.atproto import actors
from app.atproto.resolve import did_document, pds_of
from app.connections import ServiceError
from app.connections.http import call
from app.constants import NOTIF_FOLLOW
from app.models import AtprotoRecord, Notification, Post, PostReply, SocialConnection, SocialCrosspost, User, \
    UserFollower
from app.utils import can_upvote, get_task_session, patch_db_session

POST, REPOST = 'app.bsky.feed.post', 'app.bsky.feed.repost'
LIKE, FOLLOW = 'app.bsky.feed.like', 'app.bsky.graph.follow'
EMBEDS_RECORD = ('app.bsky.embed.record', 'app.bsky.embed.recordWithMedia')
PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'
MAX_HOPS = 20
NSFW = {'porn', 'sexual', 'nudity'}
NSFL = {'graphic-media'}


# --------------------------------------------------------------------------
# a record's shape
# --------------------------------------------------------------------------

def html_of(text: str, facets) -> str:
    """The post's text as HTML: its links and mentions as links, in the UTF-8 byte ranges its facets name; anything
    a facet names out of range, or overlapping another, is left as text. A mention links to the account's bsky.app
    profile, a member's too (a link to a page here would become a microblog post's link: the Note's Mention tag is
    what tells the member)."""
    data = (text or '').encode()
    spans = []
    for facet in facets if isinstance(facets, list) else []:
        index = facet.get('index') if isinstance(facet, dict) else None
        features = facet.get('features') if isinstance(facet, dict) else None
        if not isinstance(index, dict) or not isinstance(features, list) or not features:
            continue
        start, end = index.get('byteStart'), index.get('byteEnd')
        feature = features[0] if isinstance(features[0], dict) else {}
        kind = feature.get('$type')
        if kind == 'app.bsky.richtext.facet#link' and isinstance(feature.get('uri'), str) and \
                feature['uri'].startswith(('https://', 'http://')):
            href = feature['uri']
        elif kind == 'app.bsky.richtext.facet#mention' and isinstance(feature.get('did'), str):
            href = actors.profile_url(feature['did'])
        else:
            continue
        if type(start) is int and type(end) is int and 0 <= start < end <= len(data):
            spans.append((start, end, href))
    out, position = [], 0
    for start, end, href in sorted(spans):
        if start < position:
            continue
        out.append(_escape(data[position:start]))
        out.append(f'<a href="{html.escape(href, quote=True)}" rel="nofollow ugc">{_escape(data[start:end])}</a>')
        position = end
    out.append(_escape(data[position:]))
    return '<p>' + ''.join(out).replace('\n', '<br>') + '</p>'


def _escape(part: bytes) -> str:
    return html.escape(part.decode(errors='replace'), quote=False)


def labels_of(record: dict) -> set[str]:
    return actors.self_labels(record)


def mentioned(facets) -> dict:
    """The members a post mentions, by the DID of their Bluesky account (any of their Bluesky connections)."""
    dids = set()
    for facet in facets if isinstance(facets, list) else []:
        for feature in (facet.get('features') or []) if isinstance(facet, dict) else []:
            if isinstance(feature, dict) and feature.get('$type') == 'app.bsky.richtext.facet#mention' and \
                    isinstance(feature.get('did'), str):
                dids.add(feature['did'])
    if not dids:
        return {}
    rows = SocialConnection.query.filter(SocialConnection.service == 'bluesky',
                                         SocialConnection.remote_id.in_(dids)).all()
    return {row.remote_id: row.user for row in rows if row.user is not None and row.user.is_local()}


def tags_of(facets) -> list[str]:
    tags = []
    for facet in facets if isinstance(facets, list) else []:
        for feature in (facet.get('features') or []) if isinstance(facet, dict) else []:
            if isinstance(feature, dict) and feature.get('$type') == 'app.bsky.richtext.facet#tag' and \
                    isinstance(feature.get('tag'), str):
                tags.append(feature['tag'])
    return tags


def published(record: dict) -> str:
    """The record's own time, never later than now (a clock ahead, or a post dated in the future)."""
    try:
        moment = datetime.fromisoformat(str(record.get('createdAt')).replace('Z', '+00:00'))
    except ValueError:
        moment = None
    now = datetime.now(timezone.utc)
    if moment is None or moment.tzinfo is None or moment > now:
        moment = now
    return moment.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def attachments_of(embed, pds: str, did: str) -> list[dict]:
    """The post's pictures (with alt text) or video, as Note attachments read from the author's PDS."""
    if not isinstance(embed, dict):
        return []
    if embed.get('$type') == 'app.bsky.embed.recordWithMedia':
        embed = embed.get('media') if isinstance(embed.get('media'), dict) else {}
    found = []
    if embed.get('$type') == 'app.bsky.embed.images':
        for image in embed.get('images') or []:
            url = actors.blob_url(pds, did, image.get('image')) if isinstance(image, dict) else None
            if url:
                found.append({'type': 'Image', 'mediaType': 'image/jpeg', 'url': url,
                              'name': str(image.get('alt') or '')[:1500]})
    elif embed.get('$type') == 'app.bsky.embed.video':
        url = actors.blob_url(pds, did, embed.get('video'))
        if url:
            found.append({'type': 'Video', 'mediaType': 'video/mp4', 'url': url,
                          'name': str(embed.get('alt') or '')[:1500]})
    return found


def quoted_of(embed) -> str | None:
    """The at:// URI of the post a post quotes (app.bsky.embed.record, or with media), or None."""
    if not isinstance(embed, dict) or embed.get('$type') not in EMBEDS_RECORD:
        return None
    record = embed.get('record')
    if isinstance(record, dict) and isinstance(record.get('record'), dict):   # recordWithMedia nests it once more
        record = record['record']
    uri = record.get('uri') if isinstance(record, dict) else None
    return uri if isinstance(uri, str) and uri.startswith('at://') and f'/{POST}/' in uri else None


def quote_of(embed) -> str | None:
    """The address a quote post's `quote` names (spec 2026-10-12 C2, which shows it as a card when PieFed has it):
    a member's post or reply when the quoted record is ours, else the quoted post's bsky.app address."""
    uri = quoted_of(embed)
    if uri is None:
        return None
    local = mine(uri)
    return local.ap_id if local is not None and local.ap_id else actors.post_url(uri)


def links_of(embed) -> str:
    """What a post embeds that its text may not show: an external link, as a link after it (a quoted post is the
    Note's `quote`)."""
    if not isinstance(embed, dict):
        return ''
    links = []
    external = embed.get('external') if embed.get('$type') == 'app.bsky.embed.external' else None
    if isinstance(external, dict) and str(external.get('uri', '')).startswith(('https://', 'http://')):
        links.append(external['uri'])
    return ''.join(f'<p><a href="{html.escape(link, quote=True)}" rel="nofollow ugc">{html.escape(link)}</a></p>'
                   for link in links)


def activity_of(user: User, uri: str, record: dict, pds: str, parent: str | None = None) -> dict:
    """The record as the Create of a Note: what create_post and create_post_reply take."""
    url = actors.post_url(uri)
    members = mentioned(record.get('facets'))
    content = html_of(record.get('text'), record.get('facets'))
    content += links_of(record.get('embed'))
    note = {'type': 'Note', 'id': url, 'url': url, 'attributedTo': user.ap_profile_id, 'content': content,
            'published': published(record), 'to': [PUBLIC], 'cc': [],
            'attachment': attachments_of(record.get('embed'), pds, actors.did_of(user)),
            'tag': [{'type': 'Hashtag', 'name': f'#{tag}'} for tag in tags_of(record.get('facets'))] +
            [{'type': 'Mention', 'href': member.profile_id(), 'name': f'@{member.user_name}'}
             for member in {member.id: member for member in members.values()}.values()],
            'sensitive': bool(labels_of(record) & NSFW)}
    langs = record.get('langs')
    if isinstance(langs, list) and langs and isinstance(langs[0], str):
        note['contentMap'] = {langs[0]: content}
    if parent is not None:
        note['inReplyTo'] = parent
    quote = quote_of(record.get('embed'))
    if quote is not None:
        note['quote'] = quote
    return {'id': f'{url}#create', 'type': 'Create', 'actor': user.ap_profile_id, 'object': note, 'to': [PUBLIC]}


# --------------------------------------------------------------------------
# what is here already
# --------------------------------------------------------------------------

def ours(uri: str) -> bool:
    """A record PieFed made itself: a post's copy, or an identity's own write. It comes back as an echo."""
    return SocialCrosspost.query.filter_by(remote_id=uri).first() is not None or \
        AtprotoRecord.query.filter_by(uri=uri, direction='out').first() is not None


def mine(uri: str):
    """What a record of ours is here: the Post a Bluesky copy was made of, or the PostReply an identity sent. None
    for any other record."""
    copy = SocialCrosspost.query.filter(SocialCrosspost.remote_id == uri,
                                        SocialCrosspost.state.in_(('sent', 'editing'))).first()
    if copy is not None:
        return copy.post
    row = AtprotoRecord.query.filter_by(uri=uri, direction='out', collection=POST).first()
    return db.session.get(PostReply, row.post_reply_id) if row is not None and row.post_reply_id else None


def ours_did(did: str) -> bool:
    """A member's own Bluesky account (any connection): what it does outside PieFed is theirs to import (A7, P5),
    never an ATProto actor of its own here."""
    return SocialConnection.query.filter_by(service='bluesky', remote_id=did).first() is not None


def stored(uri: str):
    """The Post or PostReply PieFed has for a post's at:// URI, or None."""
    url = actors.post_url(uri)
    return Post.get_by_ap_id(url) or PostReply.get_by_ap_id(url)


def _keep(uri: str, cid, did: str, collection: str, **targets) -> None:
    rkey = uri.rsplit('/', 1)[-1]
    if AtprotoRecord.query.filter_by(uri=uri).first() is None:
        db.session.add(AtprotoRecord(uri=uri, cid=cid, did=did, collection=collection, rkey=rkey, direction='in',
                                     **targets))
        db.session.commit()


def blocked(user: User | None) -> bool:
    return user is None or user.banned or user.deleted or actors.instance().defederated


# --------------------------------------------------------------------------
# records in
# --------------------------------------------------------------------------

def post(did: str, rkey: str, record: dict, cid=None, hops: int = MAX_HOPS):
    """A post record into PieFed: the Post or PostReply made (or there already), or None when it is not taken."""
    uri = f'at://{did}/{POST}/{rkey}'
    if not isinstance(record, dict) or ours(uri) or ours_did(did):
        return None
    existing = stored(uri)
    if existing is not None:
        return existing
    user = actors.actor_for(did)
    if blocked(user):
        return None
    pds = pds_of(did_document(did))
    reply = record.get('reply') if isinstance(record.get('reply'), dict) else None
    if reply is not None:
        parent_uri = (reply.get('parent') or {}).get('uri') if isinstance(reply.get('parent'), dict) else None
        parent = fetched(parent_uri, hops - 1) if isinstance(parent_uri, str) else None
        if parent is None:
            return None
        made = create_post_reply(False, parent.community, parent.ap_id, activity_of(user, uri, record, pds,
                                                                                    parent.ap_id), user)
    else:
        made = create_post(False, find_microblogging_community(), activity_of(user, uri, record, pds), user)
    if made is None:
        return None
    labels = labels_of(record)
    made.requires_login = actors.NO_UNAUTHENTICATED in labels   # the record's own ask; its account's is the user's
    made.local_only = made.local_only or login_only(made, user)
    if isinstance(made, Post):
        made.nsfw = made.nsfw or bool(labels & NSFW)
        made.nsfl = made.nsfl or bool(labels & NSFL)
        db.session.commit()
        _keep(uri, cid, did, POST, post_id=made.id)
    else:
        db.session.commit()
        _keep(uri, cid, did, POST, post_reply_id=made.id, post_id=made.post_id)
    return made


def login_only(content, user: User) -> bool:
    """Whether readers who are not signed in may not see it (`!no-unauthenticated`, §7): its own ask, its account's,
    or, for a reply, the post it is under being hidden from them. Such content is stamped local_only, which every
    list, page and the API already keep from such readers; PieFed never federates a Bluesky record anyway."""
    if content.requires_login or user.requires_login:
        return True
    return isinstance(content, PostReply) and content.post is not None and bool(content.post.local_only)


def fetched(uri: str, hops: int):
    """The post a reply answers: PieFed's, else read from its author's PDS and made, while `hops` remain."""
    found = (stored(uri) or mine(uri)) if uri.startswith('at://') and f'/{POST}/' in uri else None
    if found is not None or hops <= 0 or not uri.startswith('at://') or f'/{POST}/' not in uri:
        return found
    did, _collection, rkey = uri.removeprefix('at://').split('/', 2)
    try:
        record = actors.read_record(pds_of(did_document(did)), did, POST, rkey)
    except ServiceError:
        return None
    return post(did, rkey, record, hops=hops) if record is not None else None


def repost(did: str, rkey: str, record: dict, cid=None) -> Post | None:
    """A repost of a post, as the actor's boost of it: of a top-level post only (PieFed boosts posts)."""
    uri = f'at://{did}/{REPOST}/{rkey}'
    subject = (record or {}).get('subject') if isinstance(record, dict) else None
    subject_uri = subject.get('uri') if isinstance(subject, dict) else None
    if not isinstance(subject_uri, str) or ours(uri) or ours_did(did) or \
            AtprotoRecord.query.filter_by(uri=uri).first():
        return None
    user = actors.actor_for(did)
    if blocked(user):
        return None
    target = fetched(subject_uri, 1)
    if not isinstance(target, Post):
        return None
    record_boost(target, user)
    _keep(uri, cid, did, REPOST, post_id=target.id)
    return target


def like(did: str, rkey: str, record: dict, cid=None):
    """A like of one of our records (a member's post's copy, or a reply their identity sent), as the actor's upvote
    of it here. Any other like is dropped (§7). The Post or PostReply voted on, or None."""
    uri = f'at://{did}/{LIKE}/{rkey}'
    subject = (record or {}).get('subject') if isinstance(record, dict) else None
    subject_uri = subject.get('uri') if isinstance(subject, dict) else None
    if not isinstance(subject_uri, str) or ours(uri) or ours_did(did) or \
            AtprotoRecord.query.filter_by(uri=uri).first():
        return None
    target = mine(subject_uri)
    if target is None or target.deleted:
        return None
    user = actors.actor_for(did)
    if blocked(user) or target.author.has_blocked_user(user.id) or not can_upvote(user, target.community):
        return None
    target.vote(user, 'upvote', None)
    column = 'post_reply_id' if isinstance(target, PostReply) else 'post_id'
    _keep(uri, cid, did, LIKE, vote_kind='like', **{column: target.id})
    return target


def follow(did: str, rkey: str, record: dict, cid=None) -> User | None:
    """A follow of a member's Bluesky identity: the actor follows the member here (Bluesky follows need no
    approval), and the member is told. The member followed, or None."""
    uri = f'at://{did}/{FOLLOW}/{rkey}'
    subject = (record or {}).get('subject') if isinstance(record, dict) else None
    if not isinstance(subject, str) or ours(uri) or ours_did(did) or AtprotoRecord.query.filter_by(uri=uri).first():
        return None
    connection = SocialConnection.query.filter_by(service='bluesky', is_identity=True, remote_id=subject).first()
    member = connection.user if connection is not None else None
    if member is None or member.deleted or member.banned:
        return None
    user = actors.actor_for(did)
    if blocked(user) or member.has_blocked_user(user.id) or member.has_blocked_instance(user.instance_id):
        return None
    if UserFollower.query.filter_by(local_user_id=member.id, remote_user_id=user.id, is_inward=True).first() is None:
        db.session.add(UserFollower(local_user_id=member.id, remote_user_id=user.id, is_accepted=True, is_inward=True))
        db.session.add(Notification(title=_('You have a new follower'), url=f'/user/{user.id}', user_id=member.id,
                                    author_id=user.id, notif_type=NOTIF_FOLLOW, subtype='new_follower',
                                    targets={'gen': '0', 'author_id': user.id,
                                             'author_user_name': user.stored_link()}))
        member.unread_notifications += 1
        db.session.commit()
    _keep(uri, cid, did, FOLLOW, user_id=member.id)
    return member


def delete(did: str, collection: str, rkey: str) -> bool:
    """The author deleted a record: what PieFed made of it goes. Whether anything did."""
    uri = f'at://{did}/{collection}/{rkey}'
    row = AtprotoRecord.query.filter_by(uri=uri, direction='in').first()
    user = actors.actor_for(did, fetch_new=False)
    if row is None or user is None:
        return False
    if collection == REPOST and row.post_id is not None:   # a purged post takes its rows with it (CASCADE)
        remove_boost(db.session.get(Post, row.post_id), user)
    elif collection == LIKE:
        target = db.session.get(PostReply, row.post_reply_id) if row.post_reply_id else \
            db.session.get(Post, row.post_id) if row.post_id else None
        if target is not None:
            undo_vote(None, None, target.ap_id, user)
    elif collection == FOLLOW and row.user_id is not None:
        UserFollower.query.filter_by(local_user_id=row.user_id, remote_user_id=user.id, is_inward=True).delete()
    elif collection == POST:
        target = db.session.get(PostReply, row.post_reply_id) if row.post_reply_id else \
            db.session.get(Post, row.post_id) if row.post_id else None
        if target is not None and not target.deleted:
            delete_post_or_comment(user, target, False, {'id': f'{actors.post_url(uri)}#delete'}, '')
    db.session.delete(row)
    db.session.commit()
    return True


def backfill(user: User, limit: int) -> int:
    """The actor's latest posts that answer nobody, read from its PDS (§7: when someone here first follows it).
    How many were taken."""
    did = actors.did_of(user)
    pds = pds_of(did_document(did))
    answer = call('GET', f'{pds}/xrpc/com.atproto.repo.listRecords',
                  params={'repo': did, 'collection': POST, 'limit': limit}).json()
    taken = 0
    for item in answer.get('records') or []:
        value = item.get('value') if isinstance(item, dict) else None
        uri = item.get('uri') if isinstance(item, dict) else None
        if not isinstance(value, dict) or value.get('reply') or not isinstance(uri, str):
            continue
        taken += post(did, uri.rsplit('/', 1)[-1], value, item.get('cid')) is not None
    return taken


# --------------------------------------------------------------------------
# the consumer's events
# --------------------------------------------------------------------------

def handle(event: dict) -> None:
    """One Jetstream event: a commit to a collection PieFed takes, or an identity event (a new handle)."""
    did = event.get('did')
    if not isinstance(did, str):
        return
    if event.get('kind') == 'identity' or event.get('$type', '').endswith('#identity'):
        user = actors.actor_for(did, fetch_new=False)
        if user is not None:
            actors.refresh(user)
        return
    commit = event.get('commit') if isinstance(event.get('commit'), dict) else event
    collection, operation, rkey = commit.get('collection'), commit.get('operation'), commit.get('rkey')
    if not isinstance(rkey, str) or not isinstance(collection, str):
        return
    if operation == 'delete':
        delete(did, collection, rkey)
    elif operation == 'create' and collection == POST:
        post(did, rkey, commit.get('record'), commit.get('cid'))
    elif operation == 'create' and collection == REPOST:
        repost(did, rkey, commit.get('record'), commit.get('cid'))
    elif operation == 'create' and collection == LIKE:
        like(did, rkey, commit.get('record'), commit.get('cid'))
    elif operation == 'create' and collection == FOLLOW:
        follow(did, rkey, commit.get('record'), commit.get('cid'))
    elif collection == 'app.bsky.actor.profile' and operation in ('create', 'update'):
        user = actors.actor_for(did, fetch_new=False)
        if user is not None:
            actors.refresh(user)


@celery.task
def event_task(event: dict) -> None:
    session = get_task_session()
    try:
        with patch_db_session(session):
            try:
                handle(event)
            except ServiceError as failure:
                current_app.logger.info(f'atproto: event of {event.get("did")} not taken: {failure.message}')
    finally:
        session.close()


def take(event: dict) -> None:
    """Hand an event to a worker (the consumer never waits on the database)."""
    if current_app.debug:
        event_task(event)
    else:
        event_task.delay(event)

