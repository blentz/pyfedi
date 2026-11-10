"""Mastodon's Status, built from PieFed's posts and comments (plan W1.2; spec 2026-11-07-mastodon-client-api-design
§3.3 and rulings M3, M5).

A titled community post shows as Mastodon shows a Lemmy post (M3): the title in bold, the body, the link, and the
community as a mention, so a reply stays in that community. A microblog post, or one whose body already starts with its
title, is its body. A comment is its body, in reply to its parent comment or else its post. Favourites are upvotes
(M5): `favourites_count` is the upvotes, a downvote has no form. Every field an app decodes is always present, with
`reblog`, `in_reply_to_id` and the like as null rather than left out (Elk compares them to null; plan W1.0).

A `Page` holds what one response's statuses share: the viewer's votes, bookmarks and boosts and each author's counts,
each loaded once for the whole page."""
from functools import cached_property

from flask import has_request_context, request
from markupsafe import escape

from app import db
from app.activitypub.serializers.microblog import link_hashtags, starts_with_title, web_url
from app.constants import POST_TYPE_GALLERY, POST_TYPE_IMAGE, POST_TYPE_LINK, POST_TYPE_VIDEO, \
    VISIBILITY_DIRECT, VISIBILITY_FOLLOWERS, VISIBILITY_UNLISTED
from app.mastodon.entities import absolute, account, timestamp
from app.mastodon.ids import status_id
from app.models import Post, PostBoost, PostBookmark, PostReply, PostReplyBookmark, PostReplyVote, PostVote, User
from app.reads.users import listable_counts
from app.storefront import current_storefront, domain_base_url
from app.utils import html_to_text, is_local_microblogs

VISIBILITY = {VISIBILITY_UNLISTED: 'unlisted', VISIBILITY_FOLLOWERS: 'private', VISIBILITY_DIRECT: 'direct'}


class Page:
    """The statuses of one response, as `viewer` (None: nobody signed in) is shown them."""

    def __init__(self, viewer: User | None, rows: list):
        self.viewer = viewer
        self.viewer_id = viewer.id if viewer is not None else None
        self.post_ids = [row.id for row in rows if isinstance(row, Post)]
        self.reply_ids = [row.id for row in rows if isinstance(row, PostReply)]
        self.authors = {row.author.id: row.author for row in rows}

    def _mine(self, column, user_column, ids, *extra) -> set[int]:
        if self.viewer_id is None or not ids:
            return set()
        return {value for value, in db.session.query(column).filter(user_column == self.viewer_id, column.in_(ids),
                                                                     *extra)}

    @cached_property
    def upvoted_posts(self) -> set[int]:
        return self._mine(PostVote.post_id, PostVote.user_id, self.post_ids, PostVote.effect > 0)

    @cached_property
    def upvoted_replies(self) -> set[int]:
        return self._mine(PostReplyVote.post_reply_id, PostReplyVote.user_id, self.reply_ids, PostReplyVote.effect > 0)

    @cached_property
    def bookmarked_posts(self) -> set[int]:
        return self._mine(PostBookmark.post_id, PostBookmark.user_id, self.post_ids)

    @cached_property
    def bookmarked_replies(self) -> set[int]:
        return self._mine(PostReplyBookmark.post_reply_id, PostReplyBookmark.user_id, self.reply_ids)

    @cached_property
    def boosted_posts(self) -> set[int]:
        return self._mine(PostBoost.post_id, PostBoost.user_id, self.post_ids)

    @cached_property
    def counts(self) -> dict[int, tuple]:
        return listable_counts(list(self.authors.values()), self.viewer_id)

    def account(self, user: User) -> dict:
        return account(user, self.viewer_id, counts=self.counts.get(user.id))

    def statuses(self, rows: list) -> list[dict]:
        return [status(row, self) for row in rows]


def _community_mention(post: Post) -> str:
    """The community as Mastodon marks up a mention of a group, linking its page on this domain."""
    community = post.community
    return (f'<p><span class="h-card"><a class="u-url mention" href="{escape(community.public_url())}">'
            f'@<span>{escape(community.name)}</span></a></span></p>')


def post_content(post: Post) -> str:
    """M3: bold title, body, link, community mention; a microblog post or one already starting with its title is its
    body."""
    original = post.body_html or ''
    body = link_hashtags(original, domain_base_url(current_storefront()))[0] if original else ''
    title = (post.title or '').strip()
    microblog = is_local_microblogs(post.community) or post.microblog
    parts = []
    if title and not starts_with_title(html_to_text(original).strip(), title):
        parts.append(f'<p><strong>{escape(title)}</strong></p>')
    parts.append(body)
    link = web_url(post)
    if link and post.type in (POST_TYPE_LINK, POST_TYPE_VIDEO):
        parts.append(f'<p><a href="{escape(link)}">{escape(link)}</a></p>')
    if not microblog:
        parts.append(_community_mention(post))
    return ''.join(parts)


def attachment(file) -> dict:
    url = absolute(file.view_url())
    entity = {'id': str(file.id), 'type': 'image', 'url': url, 'preview_url': absolute(file.thumbnail_url() or url),
              'remote_url': None, 'preview_remote_url': None, 'text_url': None,
              'description': file.alt_text or None, 'blurhash': None, 'meta': {}}
    if file.width and file.height:
        entity['meta'] = {'original': {'width': file.width, 'height': file.height,
                                       'size': f'{file.width}x{file.height}',
                                       'aspect': file.width / file.height}}
    return entity


def _attachments(post: Post) -> list[dict]:
    if post.type not in (POST_TYPE_IMAGE, POST_TYPE_GALLERY):
        return []
    return [attachment(file) for file in post.album_images() if file.view_url()]


def _card(post: Post) -> dict | None:
    link = web_url(post)
    if not link or post.type not in (POST_TYPE_LINK, POST_TYPE_VIDEO):
        return None
    image = absolute(post.image.view_url(), '') if post.image is not None else ''
    return {'url': link, 'title': post.title or link, 'description': '',
            'type': 'video' if post.type == POST_TYPE_VIDEO else 'link', 'author_name': '', 'author_url': '',
            'provider_name': '', 'provider_url': '', 'html': '', 'width': 0, 'height': 0, 'image': image or None,
            'embed_url': '', 'blurhash': None}


def _tags(row) -> list[dict]:
    base = domain_base_url(current_storefront())
    return [{'name': tag.name, 'url': f'{base}/tag/{tag.name}'} for tag in getattr(row, 'tags', [])]


def _parent(reply: PostReply):
    if reply.parent_id is not None:
        parent = db.session.get(PostReply, reply.parent_id)
        if parent is not None:
            return parent
    return reply.post


def status(row: Post | PostReply, page: Page) -> dict:
    is_reply = isinstance(row, PostReply)
    parent = _parent(row) if is_reply else None
    if is_reply:
        content = row.body_html or ''
        replies = row.child_count or 0
        favourited = row.id in page.upvoted_replies
        bookmarked = row.id in page.bookmarked_replies
        reblogged, reblogs = False, 0
    else:
        content = post_content(row)
        replies = row.reply_count or 0
        favourited = row.id in page.upvoted_posts
        bookmarked = row.id in page.bookmarked_posts
        reblogged, reblogs = row.id in page.boosted_posts, len(row.post_boosts or [])
    entity = {
        'id': status_id(row), 'uri': row.profile_id(), 'url': row.public_url(),
        'created_at': timestamp(row.posted_at or row.created_at),
        'edited_at': timestamp(row.edited_at) if row.edited_at else None,
        'account': page.account(row.author), 'content': content,
        'visibility': VISIBILITY.get(row.visibility, 'public'),
        'sensitive': bool(row.nsfw or getattr(row, 'nsfl', False) or row.content_warning),
        'spoiler_text': row.content_warning or '',
        'media_attachments': [] if is_reply else _attachments(row),
        'application': None, 'mentions': [], 'tags': _tags(row), 'emojis': [],
        'reblogs_count': reblogs, 'favourites_count': row.up_votes or 0, 'replies_count': replies,
        'in_reply_to_id': status_id(parent) if parent is not None else None,
        'in_reply_to_account_id': str(parent.user_id) if parent is not None else None,
        'reblog': None, 'poll': None, 'card': None if is_reply else _card(row),
        'language': row.language_code() if row.language_id else None, 'text': None,
    }
    if page.viewer_id is not None:
        entity.update(favourited=favourited, reblogged=reblogged, muted=False, bookmarked=bookmarked,
                      pinned=False if is_reply else bool(row.sticky))
    if has_request_context() and request.args.get('_pe') == '1':
        entity.update(pixelfed_fields(row, entity))
    return entity


def pixelfed_fields(row: Post | PostReply, entity: dict) -> dict:
    """Pixelfed's extended status (`_pe=1`; spec 2026-11-10-pixelfed-app-design §3): its app's profile grid shows only
    statuses whose `pf_type` is a photo, an album or a video, and picks its player by it. `liked_by` names nobody: who
    voted is shown to moderators alone on PieFed, and the app takes null."""
    kinds = [attachment['type'] for attachment in entity['media_attachments']]
    if any(kind in ('video', 'gifv') for kind in kinds):
        pf_type = 'video'
    elif len(kinds) > 1:
        pf_type = 'photo:album'
    elif kinds == ['image']:
        pf_type = 'photo'
    else:
        pf_type = 'text'
    return {'pf_type': pf_type, 'reply_count': entity['replies_count'], 'liked_by': None, 'local': row.is_local()}
