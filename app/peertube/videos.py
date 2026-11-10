"""PeerTube's videos, their comments, video search and the instance config (spec 2026-11-09-peertube-read-api-design
§3, §3.2, §3.4). Every list of posts is the read layer's for a signed-out reader (app.reads), narrowed to the videos
(V1); a video or a thread asked by id is found only if that same reader may read it."""
from flask import g, jsonify, request
from sqlalchemy import desc, func, or_

from app.models import Community, Post, Tag
from app.peertube import bp
from app.peertube import entities
from app.peertube.auth import signed_in
from app.peertube.entities import BadRequest, error, find_video, not_found, paged
from app.reads.posts import KEYSET_PAGES, Listing, post_listing
from app.reads.replies import WHOLE_THREAD, reply_tree, thread_replies
from app.reads.viewer import Restrictions
from app.federation_urls import domain_site_name
from app.local_only import hidden_here
from app.mastodon.entities import timestamp
from app.storefront import current_storefront, hosted_instance_ids
from app.visibility import can_view

SORTS = {
    '-trending': (desc(Post.ranking), desc(Post.posted_at)),
    '-hot': (desc(Post.ranking), desc(Post.posted_at)),
    '-likes': (desc(Post.score), desc(Post.posted_at)),
    '-publishedAt': (desc(Post.posted_at),),
    '-createdAt': (desc(Post.posted_at),),
    'publishedAt': (Post.posted_at,),
    '-views': (desc(Post.posted_at),),   # PieFed counts no views
}


@bp.errorhandler(BadRequest)
def _bad_request(_error):
    return error(400, 'Bad request')


def videos(listing: Listing, sort: str | None = None):
    """The videos `listing` names, as the request's sort orders them and its window pages them."""
    posts = post_listing(entities.viewer(), Restrictions(entities.viewer_id()), listing, KEYSET_PAGES).posts.filter(
        entities.playable_clause())
    if request.args.get('isLocal', '').lower() == 'true':
        posts = posts.filter(Post.instance_id.in_(hosted_instance_ids()))
    order = SORTS.get(sort or request.args.get('sort', '-publishedAt'), SORTS['-publishedAt'])
    return paged(posts.order_by(*order, desc(Post.id)), entities.video)


IMAGE_EXTENSIONS = ['.png', '.jpg', '.jpeg', '.gif', '.webp']
IMAGE_MAX_BYTES = 4 * 1024 * 1024
UNLIMITED = -1   # PeerTube's quota for no limit


@bp.route('/api/v1/config')
def config():
    """The instance as PeerTube's apps read it. Every field the official app requires is here (spec
    2026-11-09-peertube-login-design §2), each feature PieFed has not got (live, imports, plugins, captions) answered
    as switched off."""
    domain = current_storefront()
    image = {'size': {'max': IMAGE_MAX_BYTES}, 'extensions': IMAGE_EXTENSIONS}
    return jsonify({
        'instance': {'name': domain_site_name(domain, g.site) or domain.domain, 'shortDescription': '', 'isNSFW': False,
                     'defaultNSFWPolicy': 'do_not_list', 'defaultClientRoute': '/videos/trending',
                     'customizations': {'javascript': '', 'css': ''}},
        'serverVersion': entities.API_LEVEL, 'serverCommit': None,
        'client': {'videos': {'miniature': {'preferAuthorDisplayName': False},
                              'resumableUpload': {'maxChunkSize': 10 * 1024 * 1024}},
                   'menu': {'login': {'redirectOnSingleExternalAuth': False}}},
        'signup': {'allowed': False, 'allowedForCurrentIP': False, 'requiresEmailVerification': False,
                   'requiresApproval': False, 'minimumAge': 16},
        'search': {'remoteUri': {'users': False, 'anonymous': False},
                   'searchIndex': {'enabled': False, 'url': '', 'disableLocalSearch': False,
                                   'isDefaultSearch': False}},
        'avatar': {'file': image}, 'banner': {'file': image},
        'video': {'image': image, 'file': {'extensions': list(entities.FILE_SUFFIXES)}},
        'videoCaption': {'file': {'size': {'max': 0}, 'extensions': []}},
        'user': {'videoQuota': UNLIMITED, 'videoQuotaDaily': UNLIMITED},
        'import': {'videos': {'http': {'enabled': False}, 'torrent': {'enabled': False}},
                   'videoChannelSynchronization': {'enabled': False}, 'users': {'enabled': False}},
        'live': {'enabled': False, 'allowReplay': False, 'latencySetting': {'enabled': False}, 'maxDuration': 0,
                 'maxInstanceLives': 0, 'maxUserLives': 0,
                 'transcoding': {'enabled': False, 'remoteRunners': {'enabled': False}, 'enabledResolutions': [],
                                 'profile': 'default', 'availableProfiles': ['default']},
                 'rtmp': {'port': 1935}},
        'defaults': {'publish': {'downloadEnabled': True, 'commentsPolicy': 1, 'privacy': 1},
                     'p2p': {'webapp': {'enabled': False}, 'embed': {'enabled': False}},
                     'player': {'autoPlay': True}},
        'storyboards': {'enabled': False},
        'views': {'videos': {'watchingInterval': {'users': 5000, 'anonymous': 5000}}},
        'plugin': {'registered': [], 'registeredExternalAuths': [], 'registeredIdAndPassAuths': []},
        'theme': {'registered': [], 'default': 'default'},
    })


@bp.route('/api/v1/videos')
@signed_in(optional=True)
def list_videos():
    return videos(Listing(sort='', ignore_sticky=True))


@bp.route('/api/v1/videos/<video_id>')
@signed_in(optional=True)
def get_video(video_id):
    post = find_video(video_id)
    return jsonify(entities.video(post, whole=True)) if post is not None else not_found()


@bp.route('/api/v1/videos/<video_id>/description')
@signed_in(optional=True)
def video_description(video_id):
    post = find_video(video_id)
    return jsonify({'description': post.body or ''}) if post is not None else not_found()


@bp.route('/api/v1/videos/<video_id>/captions')
@signed_in(optional=True)
def video_captions(video_id):
    return jsonify({'total': 0, 'data': []}) if find_video(video_id) is not None else not_found()


@bp.route('/api/v1/videos/<video_id>/chapters')
@signed_in(optional=True)
def video_chapters(video_id):
    return jsonify({'chapters': []}) if find_video(video_id) is not None else not_found()


@bp.route('/api/v1/videos/<video_id>/storyboards')
@signed_in(optional=True)
def video_storyboards(video_id):
    return jsonify({'storyboards': []}) if find_video(video_id) is not None else not_found()


# comments (§3.2)

def _thread(post: Post) -> list[dict]:
    """The post's readable replies as a tree: deleted ones, and any whose parent is not shown, left out."""
    reader = entities.viewer_id()
    rows = [reply for reply in thread_replies(post, entities.viewer(), Restrictions(reader), 'old', WHOLE_THREAD)
            if not reply.deleted and can_view(reply, reader) and not hidden_here(reply, reader is not None)]
    return reply_tree(rows)


def _descendants(entry: dict):
    for child in entry['replies']:
        yield child['comment']
        yield from _descendants(child)


def comment_entity(entry: dict, post: Post, thread_id: int) -> dict:
    reply = entry['comment']
    below = list(_descendants(entry))
    return {'id': reply.id, 'threadId': thread_id, 'inReplyToCommentId': reply.parent_id, 'videoId': post.id,
            'url': reply.public_url(), 'text': reply.body_html or '', 'createdAt': timestamp(reply.posted_at),
            'updatedAt': timestamp(reply.edited_at or reply.posted_at), 'deletedAt': None, 'isDeleted': False,
            'totalReplies': len(below),
            'totalRepliesFromVideoAuthor': sum(1 for row in below if row.user_id == post.user_id),
            'account': entities.account(reply.author)}


def _branch(entry: dict, post: Post, thread_id: int) -> dict:
    return {'comment': comment_entity(entry, post, thread_id),
            'children': [_branch(child, post, thread_id) for child in entry['replies']]}


@bp.route('/api/v1/videos/<video_id>/comment-threads')
@signed_in(optional=True)
def comment_threads(video_id):
    post = find_video(video_id)
    if post is None:
        return not_found()
    start, count = entities.window()
    threads = _thread(post)
    return jsonify({'total': len(threads), 'totalNotDeletedComments': len(threads),
                    'data': [comment_entity(entry, post, entry['comment'].id)
                             for entry in threads[start:start + count]]})


@bp.route('/api/v1/videos/<video_id>/comment-threads/<int:thread_id>')
@signed_in(optional=True)
def comment_thread(video_id, thread_id):
    post = find_video(video_id)
    if post is None:
        return not_found()
    entry = next((entry for entry in _thread(post) if entry['comment'].id == thread_id), None)
    return jsonify(_branch(entry, post, thread_id)) if entry is not None else not_found()


# search (§3.4)

def _tags() -> list[str]:
    named = request.args.getlist('tagsOneOf') + request.args.getlist('tagsOneOf[]')
    return [tag.strip().lower().lstrip('#') for value in named for tag in value.split(',') if tag.strip()][:5]


@bp.route('/api/v1/search/videos')
@signed_in(optional=True)
def search_videos():
    query = request.args.get('search', '').strip()[:256]
    listing = Listing(sort='', ignore_sticky=True, query=query)
    posts = post_listing(entities.viewer(), Restrictions(entities.viewer_id()), listing, KEYSET_PAGES).posts.filter(
        entities.playable_clause())
    if tags := _tags():
        posts = posts.filter(Post.tags.any(Tag.name.in_(tags) & (Tag.banned == False)))
    order = SORTS.get(request.args.get('sort', '-publishedAt'), SORTS['-publishedAt'])
    return paged(posts.order_by(*order, desc(Post.id)), entities.video)


def listable_communities(query):
    """The communities this request's reader may open here (a private one is no channel)."""
    return [community for community in query if not community.banned and not community.private
            and not hidden_here(community, entities.viewer() is not None)]


@bp.route('/api/v1/search/video-channels')
@signed_in(optional=True)
def search_channels():
    start, count = entities.window()
    text = request.args.get('search', '').strip()[:256].lower()
    if not text:
        return jsonify({'total': 0, 'data': []})
    pattern = '%' + text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
    found = listable_communities(Community.query.filter(
        Community.banned == False, or_(Community.name.ilike(pattern), func.lower(Community.title).like(pattern)))
        .order_by(desc(Community.total_subscriptions_count), Community.id).limit(1000))
    return jsonify({'total': len(found), 'data': [entities.channel(c) for c in found[start:start + count]]})


@bp.route('/api/v1/search/video-playlists')
@signed_in(optional=True)
def search_playlists():
    return entities.empty()


@bp.route('/api/v1/video-playlists')
@signed_in(optional=True)
def list_playlists():
    return entities.empty()


@bp.route('/api/v1/video-playlists/<int:playlist_id>')
@signed_in(optional=True)
def get_playlist(playlist_id):
    return not_found()
