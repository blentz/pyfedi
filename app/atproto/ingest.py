"""ATProto records into PieFed (ATProto spec 2026-11-23 §7): what the consumer (app.atproto.jetstream) and a follow's
backfill hand over.

A post becomes what a Mastodon Note becomes, through the same code (create_post, create_post_reply): its text and
facets as HTML, its pictures or video as attachments read from the author's PDS, its language, published at its own
time. A post with no reply goes to the local microblogs community; a reply goes under its parent, which is fetched
from its author's PDS when PieFed does not have it, toward the root, at most MAX_HOPS. A repost is a boost. A delete
deletes. Self-labels make a post NSFW (`porn`, `sexual`, `nudity`) or NSFL (`graphic-media`), and
`!no-unauthenticated`, on the post or its author, keeps it from readers who are not signed in.

Every record that comes in is kept in `atproto_record` by its at:// URI, so one delivered twice (Jetstream delivers
at least once) is made once, and its delete finds what it made. A record under a URI PieFed made itself (a copy, or
an identity's own write) is its echo and is skipped.
"""
import html
from datetime import datetime, timezone

from flask import current_app

from app import celery, db
from app.activitypub.util import create_post, create_post_reply, delete_post_or_comment, \
    find_microblogging_community, record_boost, remove_boost
from app.atproto import actors
from app.atproto.resolve import did_document, pds_of
from app.connections import ServiceError
from app.connections.http import call
from app.models import AtprotoRecord, Post, PostReply, SocialCrosspost, User
from app.utils import get_task_session, patch_db_session

POST, REPOST = 'app.bsky.feed.post', 'app.bsky.feed.repost'
PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'
MAX_HOPS = 20
NSFW = {'porn', 'sexual', 'nudity'}
NSFL = {'graphic-media'}


# --------------------------------------------------------------------------
# a record's shape
# --------------------------------------------------------------------------

def html_of(text: str, facets) -> str:
    """The post's text as HTML: its links and mentions as links, in the UTF-8 byte ranges its facets name; anything
    a facet names out of range, or overlapping another, is left as text."""
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


def links_of(embed) -> str:
    """What a post embeds that its text may not show: an external link, or the post it quotes, as links after it."""
    if not isinstance(embed, dict):
        return ''
    links = []
    record = embed.get('record') if embed.get('$type') in ('app.bsky.embed.record',
                                                           'app.bsky.embed.recordWithMedia') else None
    if isinstance(record, dict) and isinstance(record.get('record'), dict):   # recordWithMedia nests it once more
        record = record['record']
    if isinstance(record, dict) and str(record.get('uri', '')).startswith('at://') and '/app.bsky.feed.post/' in \
            record['uri']:
        links.append(actors.post_url(record['uri']))
    external = embed.get('external') if embed.get('$type') == 'app.bsky.embed.external' else None
    if isinstance(external, dict) and str(external.get('uri', '')).startswith(('https://', 'http://')):
        links.append(external['uri'])
    return ''.join(f'<p><a href="{html.escape(link, quote=True)}" rel="nofollow ugc">{html.escape(link)}</a></p>'
                   for link in links)


def activity_of(user: User, uri: str, record: dict, pds: str, parent: str | None = None) -> dict:
    """The record as the Create of a Note: what create_post and create_post_reply take."""
    url = actors.post_url(uri)
    content = html_of(record.get('text'), record.get('facets'))
    content += links_of(record.get('embed'))
    note = {'type': 'Note', 'id': url, 'url': url, 'attributedTo': user.ap_profile_id, 'content': content,
            'published': published(record), 'to': [PUBLIC], 'cc': [],
            'attachment': attachments_of(record.get('embed'), pds, actors.did_of(user)),
            'tag': [{'type': 'Hashtag', 'name': f'#{tag}'} for tag in tags_of(record.get('facets'))],
            'sensitive': bool(labels_of(record) & NSFW)}
    langs = record.get('langs')
    if isinstance(langs, list) and langs and isinstance(langs[0], str):
        note['contentMap'] = {langs[0]: content}
    if parent is not None:
        note['inReplyTo'] = parent
    return {'id': f'{url}#create', 'type': 'Create', 'actor': user.ap_profile_id, 'object': note, 'to': [PUBLIC]}


# --------------------------------------------------------------------------
# what is here already
# --------------------------------------------------------------------------

def ours(uri: str) -> bool:
    """A record PieFed made itself: a post's copy, or an identity's own write. It comes back as an echo."""
    return SocialCrosspost.query.filter_by(remote_id=uri).first() is not None or \
        AtprotoRecord.query.filter_by(uri=uri, direction='out').first() is not None


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
    if not isinstance(record, dict) or ours(uri):
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
    if isinstance(made, Post):
        made.nsfw = made.nsfw or bool(labels & NSFW)
        made.nsfl = made.nsfl or bool(labels & NSFL)
        made.requires_login = actors.NO_UNAUTHENTICATED in labels or user.requires_login
        db.session.commit()
        _keep(uri, cid, did, POST, post_id=made.id)
    else:
        _keep(uri, cid, did, POST, post_reply_id=made.id, post_id=made.post_id)
    return made


def fetched(uri: str, hops: int):
    """The post a reply answers: PieFed's, else read from its author's PDS and made, while `hops` remain."""
    found = stored(uri) if uri.startswith('at://') and f'/{POST}/' in uri else None
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
    if not isinstance(subject_uri, str) or ours(uri) or AtprotoRecord.query.filter_by(uri=uri).first():
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


def delete(did: str, collection: str, rkey: str) -> bool:
    """The author deleted a record: what PieFed made of it goes. Whether anything did."""
    uri = f'at://{did}/{collection}/{rkey}'
    row = AtprotoRecord.query.filter_by(uri=uri, direction='in').first()
    user = actors.actor_for(did, fetch_new=False)
    if row is None or user is None:
        return False
    if collection == REPOST and row.post_id is not None:   # a purged post takes its rows with it (CASCADE)
        remove_boost(db.session.get(Post, row.post_id), user)
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

