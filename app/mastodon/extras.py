"""The small reads Mastodon apps make at startup and from their menus (plan W1.2; inventory in plan W1.0): lists (M2: a
community the person joined), custom emojis, preferences, filters, followed tags and read markers. An app tolerates a
failure of most of them, but a 404 still costs a logged error or an empty screen, and an answer costs nothing."""
from flask import g, jsonify

from app import db
from app.local_only import hidden_here
from app.mastodon import bp
from app.mastodon.entities import absolute
from app.models import Community, CommunityMember, Emoji
from app.oauth_server.protect import require_oauth
from app.storefront import DEFAULT_INSTANCE_ID

_PG_INT_MAX = 2 ** 31 - 1


def _list(community: Community) -> dict:
    return {'id': str(community.id), 'title': community.title or community.name, 'replies_policy': 'list',
            'exclusive': False}


def _joined():
    return db.session.query(Community).join(CommunityMember, CommunityMember.community_id == Community.id).filter(
        CommunityMember.user_id == g.oauth_user.id, CommunityMember.is_banned == False)


@bp.route('/api/v1/lists')
@require_oauth('read:lists')
def lists():
    joined = _joined().order_by(Community.title, Community.name)
    return jsonify([_list(community) for community in joined if not hidden_here(community, True)])


@bp.route('/api/v1/lists/<int:list_id>')
@require_oauth('read:lists')
def one_list(list_id):
    community = _joined().filter(Community.id == list_id).first() if list_id <= _PG_INT_MAX else None
    if community is None or hidden_here(community, True):
        return jsonify({'error': 'Record not found'}), 404
    return jsonify(_list(community))


@bp.route('/api/v1/custom_emojis')
@require_oauth(user_required=False, optional=True)
def custom_emojis():
    """This instance's own emojis (another instance's come with the statuses that use them)."""
    emojis = Emoji.query.filter(Emoji.instance_id == DEFAULT_INSTANCE_ID).order_by(Emoji.token)
    answer = []
    for emoji in emojis:
        shortcode = (emoji.token or '').strip(':')
        if shortcode and emoji.url:
            url = absolute(emoji.url)
            answer.append({'shortcode': shortcode, 'url': url, 'static_url': url, 'visible_in_picker': True,
                           'category': emoji.category or None})
    return jsonify(answer)


@bp.route('/api/v1/preferences')
@require_oauth('read:accounts')
def preferences():
    return jsonify({'posting:default:visibility': 'public', 'posting:default:sensitive': False,
                    'posting:default:language': None, 'reading:expand:media': 'default',
                    'reading:expand:spoilers': False})


@bp.route('/api/v1/filters')
@bp.route('/api/v2/filters')
@require_oauth('read:filters')
def filters():
    """None: PieFed's keyword filters are applied to the listings themselves, so nothing is left for an app to hide."""
    return jsonify([])


@bp.route('/api/v1/followed_tags')
@require_oauth('read:follows')
def followed_tags():
    return jsonify([])


@bp.route('/api/v1/markers')
@require_oauth('read:statuses')
def markers():
    """No read positions are kept yet: an empty object, which every app takes as no marker."""
    return jsonify({})
