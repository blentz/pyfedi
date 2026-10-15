"""A post in the local microblogs community as a Mastodon-native Note (spec 2026-10-14 §4.2, amended §10 A2)."""
from flask import current_app
from markupsafe import escape

from app.activitypub.util import post_count_collections
from app.constants import POST_TYPE_EVENT, POST_TYPE_IMAGE, POST_TYPE_LINK, POST_TYPE_POLL, POST_TYPE_VIDEO
from app.utils import ap_datetime, guess_mime_type, html_to_text, is_local_microblogs
from app.visibility import followers_only_addressing, is_open

PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'


def uses_microblog_note(post) -> bool:
    """A post goes out as a Note when it is in the local microblogs community. Polls and events keep their
    Question and Event shapes."""
    return is_local_microblogs(post.community) and post.type not in (POST_TYPE_POLL, POST_TYPE_EVENT)


def note_content(post) -> str:
    """The title as its own paragraph, then the body. A Note has no title, and the form requires one, so leaving it
    out would lose what the author wrote. Left out only when the body's text already starts with it."""
    body = post.body_html or ''
    title = (post.title or '').strip()
    if not title or html_to_text(body).strip().startswith(title):
        return body
    if post.type in (POST_TYPE_LINK, POST_TYPE_VIDEO) and post.url:
        return f'<p><a href="{escape(post.url)}">{escape(title)}</a></p>{body}'
    return f'<p>{escape(title)}</p>{body}'


def note_addressing(post, mentions: list) -> dict:
    """Public: to Public, cc the author's followers collection and each Mention. Followers-only: Amendment B."""
    if not is_open(post):
        return followers_only_addressing(post.author, mentions)
    return {'to': [PUBLIC],
            'cc': [post.author.followers_url()] + [mention.public_url() for mention in mentions],
            'interactionPolicy': {'canQuote': {'automaticApproval': [PUBLIC]}}}


def note_attachments(post) -> list:
    if post.type == POST_TYPE_IMAGE and post.image is not None and post.image.source_url:
        attachment = {'type': 'Document', 'mediaType': guess_mime_type(post.image.source_url),
                      'url': post.image.source_url, 'name': post.image.alt_text or ''}
        if post.image.width and post.image.height:
            attachment['width'] = post.image.width
            attachment['height'] = post.image.height
        return [attachment]
    if post.type in (POST_TYPE_LINK, POST_TYPE_VIDEO) and post.url:
        return [{'type': 'Link', 'href': post.url}]
    return []


def microblog_note(post, mentions: list) -> dict:
    """The Note, without @context. `mentions` are the users the body Mentions: the push finds them with fetching
    allowed, a fetch with it refused (mentioned_users(post, allow_fetch=False)), as for followers-only addressing."""
    server_url = current_app.config['SERVER_URL']
    addressing = note_addressing(post, mentions)
    content = note_content(post)
    tag = post.tags_for_activitypub()
    for mention in mentions:
        tag.append({'href': mention.public_url(), 'name': mention.mention_tag(), 'type': 'Mention'})
    note = {
        'id': post.public_url(),
        'type': 'Note',
        'attributedTo': post.author.public_url(),
        'to': addressing['to'],
        'cc': addressing['cc'],
        'content': content,
        'mediaType': 'text/html',
        'source': {'content': post.body or '', 'mediaType': 'text/markdown'},
        'published': ap_datetime(post.posted_at),
        'url': post.public_url(),
        'context': f'{server_url}/post/{post.id}/context',
        'replies': f'{server_url}/post/{post.id}/replies',
        'inReplyTo': None,
        'tag': tag,
        'attachment': note_attachments(post),
        'sensitive': bool(post.nsfw or post.nsfl or post.content_warning),
        'commentsEnabled': post.comments_enabled,
        'interactionPolicy': addressing['interactionPolicy'],
    }
    if post.language_id:
        note['contentMap'] = {post.language_code(): content}
    if post.content_warning:
        note['summary'] = post.content_warning
    if post.edited_at is not None:
        note['updated'] = ap_datetime(post.edited_at)
    if is_open(post):
        note.update(post_count_collections(post))
        if post.indexable:
            note['searchableBy'] = PUBLIC
    return note
