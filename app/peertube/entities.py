"""PeerTube's entities, built from PieFed's rows, and what a video is (spec 2026-11-09-peertube-read-api-design §3).
Every key NewPipe reads with a throwing getter is always present (§2); a video never carries `followersCount` or
`videosLength`, by which NewPipe would take it for a channel or a playlist.

A video (V1) is a published video post a signed-out reader may read on the domain asked, with a file to play: an upload
whose media record is ready, or, off an onion, a post whose URL is itself a video file. A link to a video site has
nothing NewPipe can play, so it is not one."""
import re
from datetime import datetime

from flask import g, jsonify, request
from sqlalchemy import and_, func, or_

from app import db
from app.constants import POST_TYPE_VIDEO
from app.mastodon.entities import timestamp
from app.mastodon.timelines import readable
from app.models import Community, CommunityMember, Language, Post, User, media_of, rendition_name, renditions_of
from app.storefront import current_storefront, domain_base_url, storefront_link

API_LEVEL = '7.2.0'   # D4: the PeerTube API level served, which PeerTube's own app requires; not PieFed's version
DEFAULT_COUNT, MAX_COUNT = 15, 100   # PeerTube's
DESCRIPTION_CUT = 250   # a list's description, as PeerTube cuts it; NewPipe then asks /description for the rest
FILE_SUFFIXES = ('.mp4', '.webm')   # a remote URL NewPipe can play: its format is read from the extension
UNKNOWN = {'id': None, 'label': 'Unknown'}   # PeerTube's answer for an unset category, licence or language
_UUID = re.compile(r'00000000-0000-4000-8000-([0-9a-f]{12})')
_PG_INT_MAX = 2 ** 31 - 1
EPOCH = datetime(1970, 1, 1)   # for a date PieFed has not got


class BadRequest(ValueError):
    pass


def error(status: int, message: str):
    """PeerTube's error shape, never echoing what was asked."""
    return jsonify({'error': message, 'status': status}), status


def not_found():
    return error(404, 'Not found')


# what a video is

def _media_status():
    return Post.extensions['media']['status'].as_string()


def playable_clause():
    """The posts, among those the read layer lists, that are videos here (V1)."""
    upload = and_(_media_status() == 'ready', Post.extensions['media']['mime'].as_string().startswith('video/'))
    if current_storefront().is_onion:
        file = upload
    else:
        url = func.lower(Post.url)
        remote = and_(_media_status().is_(None), or_(*(url.like(f'%{suffix}') for suffix in FILE_SUFFIXES)))
        file = or_(upload, remote)
    return and_(Post.type == POST_TYPE_VIDEO, file)


def _is_upload(post: Post) -> bool:
    media = media_of(post)
    return media is not None and media.get('status') == 'ready' and str(media.get('mime', '')).startswith('video/')


def _is_remote_file(post: Post) -> bool:
    media = media_of(post)
    return (not current_storefront().is_onion and (media is None or media.get('status') is None)
            and isinstance(post.url, str) and post.url.lower().endswith(FILE_SUFFIXES))


def viewer() -> User | None:
    """Who this request reads as: the person its token names, or None for a signed-out reader."""
    return g.get('oauth_user')


def viewer_id() -> int | None:
    return viewer().id if viewer() is not None else None


def is_video(post: Post | None) -> bool:
    """`playable_clause` for one row, and readable here by this request's reader."""
    return (post is not None and post.type == POST_TYPE_VIDEO and (_is_upload(post) or _is_remote_file(post))
            and readable(post, viewer_id()))


def video_uuid(post: Post) -> str:
    """A UUID holding the post id, so finding a video by it needs no column (spec §3)."""
    return f'00000000-0000-4000-8000-{post.id:012x}'


def find_video(key: str) -> Post | None:
    """The video `key` (an id or a video_uuid) names, if it is one this request's reader may play here."""
    if key.isdigit() and len(key) <= 10:
        post_id = int(key)
    elif match := _UUID.fullmatch(key):
        post_id = int(match[1], 16)
    else:
        return None
    if not 0 < post_id <= _PG_INT_MAX:
        return None
    post = db.session.get(Post, post_id)
    return post if is_video(post) else None


# paging

def whole(name: str, default: int) -> int:
    raw = request.args.get(name, '')
    if raw == '':
        return default
    if not raw.isdigit() or len(raw) > 9:
        raise BadRequest(name)
    return int(raw)


def window() -> tuple[int, int]:
    """PeerTube's start and count: start from 0, count 1 to 100."""
    start, count = whole('start', 0), whole('count', DEFAULT_COUNT)
    if not 0 < count <= MAX_COUNT:
        raise BadRequest('count')
    return start, count


def paged(query, entity):
    """`{total, data}` for this request's window of `query` (ordered by the caller), each row through `entity`."""
    start, count = window()
    total = query.order_by(None).count()
    return jsonify({'total': total, 'data': [entity(row) for row in query.offset(start).limit(count).all()]})


def empty():
    window()
    return jsonify({'total': 0, 'data': []})


# entities

def _base() -> str:
    return domain_base_url(current_storefront())


def _path(url: str | None) -> str | None:
    """A URL of this site (on any of its hosts) as the path NewPipe prefixes with the instance's base; None for one
    elsewhere."""
    if not url:
        return None
    url, base = storefront_link(url), _base()
    if url.startswith(base + '/'):
        return url[len(base):]
    return url if url.startswith('/') and not url.startswith('//') else None


def _when(moment) -> str:
    return timestamp(moment or EPOCH)


def _avatars(url: str | None, moment=None) -> list:
    """The actor's image, dated: PeerTube's app requires `createdAt` and `updatedAt` of every image it decodes."""
    path = _path(url)
    return [{'path': path, 'width': 120, 'createdAt': _when(moment), 'updatedAt': _when(moment)}] \
        if path and not path.endswith('/1px.gif') else []


def _host(handle: str) -> str:
    return handle.lstrip('!').rpartition('@')[2]


def _absolute(url: str) -> str:
    return _base() + url if url.startswith('/') else url


def host_of(actor) -> str:
    """The host an account or channel is named with."""
    return _host(actor.lemmy_link())


def image_url(url: str | None) -> str | None:
    """An image's whole URL, one of this site's on the domain asked; None for none, or the 1px placeholder."""
    if not url or url.endswith('/1px.gif'):
        return None
    return storefront_link(_absolute(url))


def account(user: User) -> dict:
    """An account, with what both NewPipe and PeerTube's own app require of one (its AccountDetails)."""
    return {'id': user.id, 'name': user.user_name, 'host': _host(user.lemmy_link()),
            'displayName': user.display_name(), 'url': _absolute(user.public_url()),
            'avatars': _avatars(user.avatar_image(), user.created), 'description': user.about or '',
            'followersCount': user.num_followers or 0, 'followingCount': user.num_following or 0,
            'createdAt': _when(user.created), 'updatedAt': _when(user.created)}


def _owner(community: Community) -> User | None:
    member = db.session.query(CommunityMember).join(User, User.id == CommunityMember.user_id).filter(
        CommunityMember.community_id == community.id, CommunityMember.is_banned == False, User.deleted == False,
        or_(CommunityMember.is_owner == True, CommunityMember.is_moderator == True)).order_by(
        CommunityMember.is_owner.desc(), CommunityMember.user_id).first()
    if member is not None:
        return db.session.get(User, member.user_id)
    creator = db.session.get(User, community.user_id) if community.user_id else None
    return creator if creator is not None and not creator.deleted else None


def _channel_summary(community: Community) -> dict:
    return {'id': community.id, 'name': community.name, 'host': _host(community.lemmy_link()),
            'displayName': community.title, 'url': _absolute(community.public_url()),
            'avatars': _avatars(community.icon_image(), community.created_at)}


def channel(community: Community) -> dict:
    """A channel as a list or its own page shows it: `followersCount` marks it one in a list."""
    owner = _owner(community)
    owner_account = account(owner) if owner is not None else {'name': community.name, 'url': _absolute(
        community.public_url()), 'host': _host(community.lemmy_link()), 'displayName': community.title, 'avatars': []}
    return {**_channel_summary(community), 'description': community.description or '',
            'followersCount': community.total_subscriptions_count or community.subscriptions_count or 0,
            'followingCount': 0, 'createdAt': _when(community.created_at), 'updatedAt': _when(community.created_at),
            'isLocal': community.is_local(), 'ownerAccount': owner_account, 'banners': []}


def _language(post: Post) -> dict:
    language = db.session.get(Language, post.language_id) if post.language_id else None
    return {'id': language.code, 'label': language.name} if language is not None else UNKNOWN


def _cut(text: str) -> str:
    return text if len(text) <= DESCRIPTION_CUT else text[:DESCRIPTION_CUT - 3] + '...'


def _images(post: Post) -> dict:
    image = post.image
    if image is None:
        return {}
    base = _base()
    found = {'thumbnailPath': _path(image.thumbnail_url(base=base)), 'previewPath': _path(image.medium_url(base=base))}
    return {key: value for key, value in found.items() if value}


def _file(url: str, label_id: int, label: str, size: int, fps: dict) -> dict:
    """One progressive file, from the domain asked: the stored URL names the uploader's domain, and JSON gets no link
    rewrite, so an onion's viewer would otherwise be sent to the clearnet. `torrentUrl` is always there."""
    url = storefront_link(url)
    return {'fileUrl': url, 'fileDownloadUrl': url, 'torrentUrl': '', 'magnetUri': '',
            'resolution': {'id': label_id, 'label': label}, 'size': size, **fps}


def files(post: Post) -> list:
    """The progressive files (§3.1): an upload's stored file then its renditions (W2.1b), or a remote file's URL."""
    if not _is_upload(post):
        return [_file(post.url, 0, 'Original', 0, {})]
    media = media_of(post)
    height = media.get('height') if type(media.get('height')) is int else 0
    size = media.get('bytes') if type(media.get('bytes')) is int else 0
    fps = {'fps': media['fps']} if type(media.get('fps')) is int else {}
    return [_file(post.url, height, f'{height}p' if height else 'Original', size, fps)] + [
        _file(rendition_name(post.url, copy['side']), copy['side'], f"{copy['side']}p", copy['bytes'], fps)
        for copy in renditions_of(post)]


def video(post: Post, whole: bool = False) -> dict:
    """A video as a list shows it, or, `whole`, as its own page does."""
    media = media_of(post) or {}
    description = post.body or ''
    found = {
        'id': post.id, 'uuid': video_uuid(post), 'shortUUID': video_uuid(post), 'name': post.title or '',
        'description': description if whole else _cut(description),
        'duration': media.get('duration_s') if type(media.get('duration_s')) is int else 0,
        'views': 0, 'viewers': 0, 'likes': post.up_votes or 0, 'dislikes': post.down_votes or 0,
        'nsfw': bool(post.nsfw or post.nsfl), 'isLive': False, 'isLocal': post.is_local(),
        'publishedAt': timestamp(post.posted_at), 'originallyPublishedAt': None,
        'createdAt': timestamp(post.posted_at), 'updatedAt': timestamp(post.edited_at or post.posted_at),
        'category': UNKNOWN, 'licence': UNKNOWN, 'language': _language(post),
        'privacy': {'id': 1, 'label': 'Public'}, 'url': _absolute(post.public_url()),
        'embedPath': f'/videos/embed/{video_uuid(post)}',
        'account': account(post.author), 'channel': _channel_summary(post.community),
        **_images(post),
    }
    if whole:
        found.update({
            'files': files(post), 'streamingPlaylists': [], 'tags': [tag.name for tag in post.tags if not tag.banned],
            'support': None, 'descriptionPath': f'/api/v1/videos/{post.id}/description',
            'commentsEnabled': bool(post.comments_enabled), 'downloadEnabled': True, 'waitTranscoding': False,
            'state': {'id': 1, 'label': 'Published'},
        })
    return found
