"""Pixelfed's own routes beside Mastodon's, for its official app (plan W1.4a; spec 2026-11-10-pixelfed-app-design §2,
§3). Each answers in Pixelfed's shape, read from its server (app/Http/Controllers/Api/ApiV1Dot1Controller.php and
Stories/StoryApiV1Controller.php), through W1's reads and app/shared, with W1's visibility, blocks and limits. What
PieFed has no such thing for answers empty: no stories until W1.4c (ruling X3), no chats until W1.4b (X2), no
Pixelfed push relay (W1's push is Mastodon's Web Push), no collections, no computed trends (as W1's /trends)."""
from flask import current_app, g, jsonify, request

from app import db
from app.constants import POST_TYPE_GALLERY, POST_TYPE_IMAGE, POST_TYPE_VIDEO, SRC_API
from app.mastodon import bp
from app.mastodon.entities import account
from app.mastodon.instance import IMAGE_TYPES, MAX_CHARACTERS, VIDEO_TYPES
from app.mastodon.statuses import Page
from app.mastodon.timelines import readable
from app.models import File, Post, User, UserFollower
from app.oauth_server.protect import require_oauth
from app.reads import users as reads_users
from app.reads.posts import KEYSET_PAGES, Listing, post_listing
from app.reads.viewer import Restrictions
from app.shared import post as shared_post
from app.client_identity import PIXELFED_VERSION
from app.utils import Authorised, max_album_images

MAX_ALT_TEXT = 1500
MAX_BIO = 1000   # the web form's (app.user.forms)
TRENDING = {'daily': 'TopDay', 'monthly': 'TopMonth', 'yearly': 'TopYear'}
TRENDING_COUNT = 20
_SHOWN = (POST_TYPE_IMAGE, POST_TYPE_GALLERY, POST_TYPE_VIDEO)


def _viewer_id() -> int | None:
    return g.oauth_user.id if g.oauth_user else None


@bp.route('/api/v2/config')
@require_oauth('read', optional=True)
def pixelfed_config():
    """Pixelfed's server configuration, which its app reads for its limits (the bio's length among them)."""
    site = g.site
    return jsonify({
        'version': PIXELFED_VERSION, 'open_registration': site.registration_mode == 'Open',
        'uploader': {'max_photo_size': current_app.config['MAX_CONTENT_LENGTH'] // 1024,
                     'max_caption_length': MAX_CHARACTERS, 'max_altext_length': MAX_ALT_TEXT,
                     'album_limit': max_album_images(), 'image_quality': 90, 'max_collection_length': 0,
                     'optimize_image': True, 'optimize_video': True,
                     'media_types': ','.join(IMAGE_TYPES + VIDEO_TYPES), 'mime_types': list(IMAGE_TYPES + VIDEO_TYPES),
                     'enforce_account_limit': False},
        'activitypub': {'enabled': True, 'remote_follow': True},
        'ab': {'hls': False},
        'site': {'name': site.name, 'domain': request.host, 'url': request.host_url.rstrip('/'),
                 'description': site.description or ''},
        'account': {'max_avatar_size': current_app.config['MAX_CONTENT_LENGTH'] // 1024, 'max_bio_length': MAX_BIO,
                    'max_name_length': 50, 'min_password_length': 8, 'max_account_size': 0},
        'username': {'remote': {'formats': ['@'], 'format': '@', 'custom': None}},
        'features': {'timelines': {'local': True, 'network': True}, 'mobile_apis': True, 'stories': True,
                     'video': True, 'import': {'instagram': False}, 'label': {'covid': {'enabled': False}},
                     'hashtags': {'enabled': True}, 'groups': False},
    })


@bp.route('/api/v2.1/config')
def pixelfed_app_config():
    """What Pixelfed's app (2.0.1) reads before anyone signs in, and refuses a server by: its version must be 0.14.4 or
    later. Pixelfed's own shape (app/Services/AppService.php getConfig), with PieFed's limits."""
    site = g.site
    media_types = list(IMAGE_TYPES + VIDEO_TYPES)
    upload_limit = current_app.config['MAX_CONTENT_LENGTH']
    return jsonify({
        'version': PIXELFED_VERSION, 'oauth': True,
        'signup': {'curated': site.registration_mode == 'RequireApplication', 'mobile_registration': False},
        'features': {'stories': True, 'video': True, 'timelines': {'local': True, 'network': True},
                     'content_credentials': {'enabled': False, 'signing': False, 'ai_labels': False}},
        'limits': {
            'account': {'max_avatar_size': upload_limit // 1024, 'max_bio_length': MAX_BIO, 'max_name_length': 50,
                        'min_password_length': 8},
            'collections': {'max_collection_length': 0},
            'direct_messages': {'max_characters': MAX_CHARACTERS, 'max_media_attachments': 0,
                                'group_chats': {'enabled': False, 'max_participants': 0}},
            'media': {'image_size_limit': upload_limit, 'max_album_limit': max_album_images(),
                      'max_alt_text_length': MAX_ALT_TEXT, 'supported_mime_types': media_types,
                      'video_size_limit': upload_limit},
            'statuses': {'max_characters': MAX_CHARACTERS}}})


@bp.route('/api/v1.1/compose/settings')
@require_oauth('read')
def compose_settings():
    """What its camera screen limits a post to."""
    return jsonify({'default_license': 1, 'media_descriptions': False, 'max_altext_length': MAX_ALT_TEXT,
                    'max_media_attachments': max_album_images(), 'max_caption_length': MAX_CHARACTERS,
                    'default_scope': 'public', 'default_visibility': 'public'})


@bp.route('/api/v1.2/stories/carousel')
@require_oauth('read')
def stories_carousel():
    """No stories yet (W1.4c, ruling X3): Pixelfed's own answer when there are none."""
    return jsonify({'self': [], 'nodes': []})


def _trending(sort: str):
    viewer_id = _viewer_id()
    posts = post_listing(g.oauth_user, Restrictions(viewer_id), Listing(sort=sort, ignore_sticky=True),
                         KEYSET_PAGES).posts.filter(Post.type.in_(_SHOWN)).limit(TRENDING_COUNT * 2)
    rows = [post for post in posts if readable(post, viewer_id)][:TRENDING_COUNT]
    return jsonify(Page(g.oauth_user, rows).statuses(rows))


@bp.route('/api/v1.1/discover/posts/trending')
@require_oauth('read', optional=True)
def trending_posts():
    """The photos and videos most voted for in the day, month or year (`range`), as the viewer may see them."""
    return _trending(TRENDING.get(request.args.get('range', 'daily'), 'TopDay'))


@bp.route('/api/v1.1/discover/posts/network/trending')
@require_oauth('read', optional=True)
def network_trending_posts():
    return _trending('TopDay')


@bp.route('/api/v1.1/discover/posts/hashtags')
@bp.route('/api/v1.1/discover/accounts/popular')
@bp.route('/api/v1.1/collections/self')
@require_oauth('read', optional=True)
def nothing_listed():
    """No trends are computed (as W1's /api/v1/trends), and PieFed has no collections: an empty list."""
    return jsonify([])


@bp.route('/api/v1.1/accounts/username/<username>')
@require_oauth('read', optional=True)
def account_by_username(username):
    """The local account `username` names (with or without @ and this host), or Pixelfed's [] for none."""
    name = username.lstrip('@').split('@')[0]
    try:
        person = reads_users.person_named(name, None, None)
        reads_users.refuse_hidden_person(person, g.oauth_user is not None)
    except Exception:   # nobody by that name here, or one hidden here: one answer
        return jsonify([])
    return jsonify(account(person, _viewer_id()))


@bp.route('/api/v1.1/accounts/mutuals/<int:account_id>')
@require_oauth('read')
def mutual_followers(account_id):
    """The accounts the viewer follows that also follow `account_id`."""
    if account_id >= 2 ** 31:
        return jsonify([])
    followed = db.session.query(UserFollower.remote_user_id).filter(
        UserFollower.local_user_id == g.oauth_user.id, UserFollower.is_inward.is_(False),
        UserFollower.is_accepted.is_(True))
    theirs = db.session.query(UserFollower.local_user_id).filter(
        UserFollower.remote_user_id == account_id, UserFollower.is_inward.is_(False),
        UserFollower.is_accepted.is_(True))
    people = User.query.filter(User.id.in_(followed), User.id.in_(theirs), User.deleted.is_(False)).limit(40).all()
    return jsonify([account(person, g.oauth_user.id) for person in people])


@bp.route('/api/v1.1/report', methods=['POST'])
@require_oauth('write')
def report():
    """A report of a post, through PieFed's report (app.shared.post). An account is reported on the web, by one of
    its posts here."""
    fields = request.get_json(silent=True)
    fields = fields if isinstance(fields, dict) else {}
    if fields.get('object_type') != 'post':
        return jsonify({'error': 'Report one of the account\'s posts, or report the account on the web',
                        'error_code': 'ERROR_INVALID_OBJECT_TYPE'}), 400
    raw = str(fields.get('object_id') or '')
    post = db.session.get(Post, int(raw)) if raw.isdigit() and len(raw) <= 10 else None
    if post is None or not readable(post, g.oauth_user.id):
        return jsonify({'error': 'Record not found'}), 404
    if post.user_id == g.oauth_user.id:
        return jsonify({'error': 'Cannot self report', 'error_code': 'ERROR_NO_SELF_REPORTS'}), 400
    reason = str(fields.get('report_type') or 'other')[:255]
    try:
        shared_post.report_post(post, {'reason': reason, 'description': '', 'report_remote': True}, SRC_API,
                                Authorised(g.oauth_user))
    except Exception as error:   # a duplicate or another refusal of the shared report
        db.session.rollback()
        return jsonify({'error': str(error)}), 400
    return jsonify({'msg': 'Successfully sent report', 'code': 200})


@bp.route('/api/pixelfed/v1/app/settings')
@require_oauth('read')
def app_settings():
    """The app's own stored settings: none kept here, Pixelfed's answer for none."""
    return jsonify([])


@bp.route('/api/v1.1/nag/state')
@require_oauth('read')
def push_nag_state():
    """Pixelfed's push relay (an Expo gateway) is not PieFed's; W1's push is Mastodon's Web Push."""
    return jsonify({'active': False})


@bp.route('/api/v1.1/push/state')
@bp.route('/api/v1.1/push/compare', methods=['POST'])
@bp.route('/api/v1.1/push/update', methods=['POST'])
@bp.route('/api/v1.1/push/disable', methods=['POST'])
@require_oauth('push')
def push_not_supported():
    """Pixelfed's own answer when its push relay is off."""
    return jsonify({'error': 'Push notifications are not supported on this server.'}), 404


@bp.route('/api/v1.1/accounts/avatar', methods=['DELETE'])
@require_oauth('write')
def delete_avatar():
    """The avatar removed, as the web's Remove avatar does; Pixelfed answers with the account."""
    user = g.oauth_user
    if user.avatar_id:
        picture = db.session.get(File, user.avatar_id)
        user.avatar_id = None
        if picture is not None:
            picture.delete_from_disk()
            db.session.delete(picture)
        db.session.commit()
    return jsonify(account(user, user.id))
