"""Posts members write on Bluesky, outside PieFed (ATProto spec 2026-11-23 A7, §7; P5).

A post that one of a member's Bluesky accounts writes in a Bluesky app (or anywhere but PieFed) becomes the
member's own microblog post here, when the connection brings them in (`import_own`: on for an account hosted here,
offered for an OAuth one). It is made as the member makes one (app.shared.post.make_post), so Mastodon and the
rest get it by ActivityPub, and it goes to none of the member's connected accounts. A copy row (`social_crosspost`,
progress `imported`) ties it to its record: that is what marks it as written on Bluesky, keeps it from ever being
copied back, and lets a delete on either side delete the other. Replies written there are not brought in.

A record PieFed wrote itself is never brought back: its copy row names it (`remote_id`), or, while the copy is being
made, names its record key (`progress.rkey`), and an identity's other records have their own rows first.
"""
from flask import current_app, g

from app import db
from app.activitypub.util import find_microblogging_community
from app.atproto import actors
from app.atproto.records import web_url
from app.atproto.resolve import did_document, pds_of
from app.constants import POST_TYPE_ARTICLE, POST_TYPE_GALLERY, POST_TYPE_IMAGE, POST_TYPE_LINK, SRC_API
from app.models import AtprotoRecord, Language, Post, Site, SocialConnection, SocialCrosspost
from app.shared.post import delete_post, make_post
from app.utils import Authorised

POST = 'app.bsky.feed.post'
NSFW = {'porn', 'sexual', 'nudity', 'graphic-media'}
MAX_ALBUM = 4


def importing(did: str) -> SocialConnection | None:
    """The member's connection that brings in what this account writes elsewhere: the identity first."""
    return SocialConnection.query.filter_by(service='bluesky', remote_id=did, import_own=True).order_by(
        SocialConnection.is_identity.desc(), SocialConnection.id).first()


def made_here(did: str, rkey: str) -> bool:
    """A record PieFed wrote: a copy (made, or being made under this key), or an identity's record."""
    uri = f'at://{did}/{POST}/{rkey}'
    if SocialCrosspost.query.filter_by(remote_id=uri).first() is not None or \
            AtprotoRecord.query.filter_by(uri=uri, direction='out').first() is not None:
        return True
    connections = db.session.query(SocialConnection.id).filter_by(service='bluesky', remote_id=did)
    return SocialCrosspost.query.filter(SocialCrosspost.connection_id.in_(connections),
                                        SocialCrosspost.progress['rkey'].as_string() == rkey).first() is not None


def shape(did: str, record: dict) -> tuple[int, dict, list | None]:
    """What make_post takes for the record: its type, its input, and its pictures as an album (two or more)."""
    text = str(record.get('text') or '')
    embed = record.get('embed') if isinstance(record.get('embed'), dict) else {}
    if embed.get('$type') == 'app.bsky.embed.recordWithMedia':
        embed = embed.get('media') if isinstance(embed.get('media'), dict) else {}
    langs = record.get('langs')
    language = Language.query.filter_by(code=langs[0]).first() if isinstance(langs, list) and langs and \
        isinstance(langs[0], str) else None
    fields = {'title': '', 'body': text, 'url': None, 'nsfw': bool(actors.self_labels(record) & NSFW),
              'ai_generated': False, 'notify_author': True, 'language_id': language.id if language else None,
              'local_only': False, 'crosspost_to': []}   # a post from Bluesky goes to no connected account
    pictures = []
    if embed.get('$type') == 'app.bsky.embed.images':
        pds = pds_of(did_document(did))
        for image in (embed.get('images') or [])[:MAX_ALBUM]:
            url = actors.blob_url(pds, did, image.get('image')) if isinstance(image, dict) else None
            if url:
                pictures.append({'url': url, 'alt_text': str(image.get('alt') or '')[:1500]})
    if len(pictures) > 1:
        return POST_TYPE_GALLERY, fields, pictures
    if pictures:
        fields.update(url=pictures[0]['url'], image_alt_text=pictures[0]['alt_text'])
        return POST_TYPE_IMAGE, fields, None
    external = embed.get('external') if embed.get('$type') == 'app.bsky.embed.external' else None
    if isinstance(external, dict) and str(external.get('uri', '')).startswith(('https://', 'http://')):
        fields['url'] = external['uri']
        return POST_TYPE_LINK, fields, None
    return POST_TYPE_ARTICLE, fields, None


def imported(did: str, rkey: str, record: dict) -> Post | None:
    """A post the member's account wrote outside PieFed, as the member's own microblog post: the post, or None when
    it is not brought in (a reply, a connection that does not bring posts in, one of PieFed's own)."""
    connection = importing(did)
    if connection is None or record.get('reply') or made_here(did, rkey):
        return None
    member = connection.user
    community = find_microblogging_community()
    if member is None or member.banned or member.deleted or community is None:
        return None
    if g.get('site') is None:   # a worker has no request: make_post reads the site as a request does
        g.site = db.session.get(Site, 1)
    kind, fields, album = shape(did, record)
    try:
        _user_id, post = make_post(fields, community, kind, SRC_API, Authorised(member), album=album)
    except Exception as refused:   # what make_post refuses for the member (a banned domain, a rate limit)
        current_app.logger.info(f'atproto: post {did}/{rkey} not brought in: {refused}')
        return None
    uri = f'at://{did}/{POST}/{rkey}'
    db.session.add(SocialCrosspost(post_id=post.id, connection_id=connection.id, service='bluesky',
                                   variant=connection.variant, state='sent', remote_id=uri, remote_url=web_url(uri),
                                   progress={'imported': True}))
    db.session.commit()
    return post


def deleted(did: str, rkey: str) -> bool:
    """The member deleted on Bluesky a post brought in from there: it goes here too. Whether it was one."""
    row = SocialCrosspost.query.filter_by(remote_id=f'at://{did}/{POST}/{rkey}', service='bluesky').first()
    if row is None or not (row.progress or {}).get('imported') or row.post is None:
        return False
    row.state = 'deleted'   # gone there already: the delete here sends nothing back
    db.session.commit()
    if not row.post.deleted:
        delete_post(row.post_id, True, SRC_API, Authorised(row.post.author))
    return True
