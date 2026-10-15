"""A post in the local microblogs community as a Mastodon-native Note (spec 2026-10-14 §4.2, amended §10 A2)."""
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, NavigableString
from flask import current_app
from markupsafe import escape

import app.activitypub.util as activitypub_util   # the module: app.activitypub.util imports this one
from app.constants import POST_TYPE_EVENT, POST_TYPE_IMAGE, POST_TYPE_LINK, POST_TYPE_POLL, POST_TYPE_VIDEO
from app.utils import ap_datetime, guess_mime_type, html_to_text, is_local_microblogs
from app.visibility import followers_only_addressing, is_open

PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'

# `#` at the start of the text or after anything that is not a letter or digit, then a Unicode word that does not
# start with an underscore and holds a letter (Mastodon does not treat `#1` as a hashtag). `a#b` and a URL fragment
# inside text are therefore not hashtags.
HASHTAG_RE = re.compile(r'(?<![^\W_])#(?=\w*[^\W\d_])([^\W_]\w*)')
NOT_HASHTAG_PARENTS = ('a', 'code', 'pre')


def _web_url(post):
    """The post's url when it is http or https, else None: anything else (javascript:, data:) is never linked."""
    if not post.url:
        return None
    try:
        scheme = urlsplit(post.url).scheme.lower()
    except ValueError:      # `http://[x`: the API only checks the prefix
        return None
    return post.url if scheme in ('http', 'https') else None


def _starts_with_title(text: str, title: str) -> bool:
    """True when `text` starts with `title` as whole words: the next character is the end or not alphanumeric."""
    if not text.startswith(title):
        return False
    rest = text[len(title):]
    return not rest or not rest[0].isalnum()


def _hashtag_href(name: str) -> str:
    return f"{current_app.config['SERVER_URL']}/tag/{name}"


def link_hashtags(body_html: str) -> tuple:
    """(html, names): the body with each `#word` in its text nodes linked the way Mastodon marks up a hashtag, and
    the lowercased names in order of first use. Text inside <a>, <code> and <pre> and attribute values are never
    touched. Markup with no hashtag comes back byte for byte."""
    soup = BeautifulSoup(body_html, 'html.parser')
    names = []
    for text in soup.find_all(string=True):
        if type(text) is not NavigableString or text.find_parent(NOT_HASHTAG_PARENTS) is not None:
            continue    # a comment or doctype, or text that is already a link or code
        parts, last = [], 0
        for match in HASHTAG_RE.finditer(str(text)):
            name = match.group(1).lower()
            if name not in names:
                names.append(name)
            link = soup.new_tag('a', href=_hashtag_href(name), rel='tag')
            link['class'] = 'mention hashtag'
            link.append('#')
            span = soup.new_tag('span')
            span.string = match.group(1)
            link.append(span)
            parts += [NavigableString(str(text)[last:match.start()]), link]
            last = match.end()
        if parts:
            parts.append(NavigableString(str(text)[last:]))
            text.replace_with(*parts)
    return (str(soup), names) if names else (body_html, names)


def uses_microblog_note(post) -> bool:
    """A post goes out as a Note when it is in the local microblogs community. Polls and events keep their
    Question and Event shapes."""
    return is_local_microblogs(post.community) and post.type not in (POST_TYPE_POLL, POST_TYPE_EVENT)


def note_content(post) -> str:
    """The title as its own paragraph, then the body. A Note has no title, and the form requires one, so leaving it
    out would lose what the author wrote. Left out only when the body's text already starts with it as whole words."""
    original = post.body_html or ''
    body = link_hashtags(original)[0]
    title = (post.title or '').strip()
    if not title or _starts_with_title(html_to_text(original).strip(), title):
        return body
    if post.type in (POST_TYPE_LINK, POST_TYPE_VIDEO) and _web_url(post):
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
    if post.type in (POST_TYPE_LINK, POST_TYPE_VIDEO) and _web_url(post):
        return [{'type': 'Link', 'href': post.url}]
    return []


def microblog_note(post, mentions: list) -> dict:
    """The Note, without @context. `mentions` are the users the body Mentions: the push finds them with fetching
    allowed, a fetch with it refused (mentioned_users(post, allow_fetch=False)), as for followers-only addressing."""
    server_url = current_app.config['SERVER_URL']
    addressing = note_addressing(post, mentions)
    content = note_content(post)
    tag = post.tags_for_activitypub()    # body hashtags are stored in post.tags on create and edit (§10 A12)
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
        note.update(activitypub_util.post_count_collections(post))
        if post.indexable:
            note['searchableBy'] = PUBLIC
    return note
