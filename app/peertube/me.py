"""The signed-in person as PeerTube's apps read and change it (spec 2026-11-09-peertube-login-design §3): GET and PUT
users/me. Every field the official app requires is there; each setting PieFed has not got is PeerTube's default,
the welcome and setup prompts answered as already seen."""
from flask import g, jsonify, request
from sqlalchemy import desc

from app import db
from app.models import Community, CommunityMember, User
from app.peertube import bp, entities
from app.peertube.auth import problem, signed_in
from app.peertube.videos import UNLIMITED, listable_communities
from app.user.profile_text import set_profile_text

MAX_DISPLAY_NAME, MAX_DESCRIPTION = 120, 5000
NSFW_POLICIES = {0: 'display', 1: 'do_not_list'}   # PieFed's hide_nsfw; its blur and fade are PeerTube's blur
# PeerTube's notification settings, each 1 (shown in the app, no email): PieFed's own settings stay on the web
NOTIFICATION_SETTINGS = ('newVideoFromSubscription', 'newCommentOnMyVideo', 'abuseAsModerator',
                         'videoAutoBlacklistAsModerator', 'blacklistOnMyVideo', 'myVideoPublished',
                         'myVideoImportFinished', 'newUserRegistration', 'newFollow', 'commentMention',
                         'newInstanceFollower', 'autoInstanceFollowing', 'abuseStateChange', 'abuseNewMessage',
                         'newPeerTubeVersion', 'newPluginVersion', 'myVideoStudioEditionFinished',
                         'myVideoTranscriptionGenerated')


def owned_channels(user: User) -> list[Community]:
    """The communities `user` owns, which are the channels they can publish to in PeerTube's terms."""
    owned = db.session.query(Community).join(CommunityMember, CommunityMember.community_id == Community.id).filter(
        CommunityMember.user_id == user.id, CommunityMember.is_owner == True, CommunityMember.is_banned == False) \
        .order_by(desc(Community.total_subscriptions_count), Community.id).limit(1000)
    return listable_communities(owned)


def _role(user: User) -> dict:
    return {'id': 0, 'label': 'Administrator'} if user.is_admin() else {'id': 2, 'label': 'User'}


def me(user: User) -> dict:
    when = entities.timestamp(user.created or entities.EPOCH)
    return {
        'id': user.id, 'username': user.user_name, 'email': user.email or '', 'emailVerified': True,
        'pendingEmail': None, 'createdAt': when, 'lastLoginDate': entities.timestamp(user.last_seen or user.created
                                                                                     or entities.EPOCH),
        'account': entities.account(user), 'role': _role(user), 'blocked': False, 'blockedReason': None,
        'nsfwPolicy': NSFW_POLICIES.get(user.hide_nsfw, 'blur'), 'p2pEnabled': False, 'videosHistoryEnabled': True,
        'autoPlayVideo': True, 'autoPlayNextVideo': True, 'autoPlayNextVideoPlaylist': True, 'videoLanguages': None,
        'theme': 'instance-default', 'noInstanceConfigWarningModal': True, 'noAccountSetupWarningModal': True,
        'noWelcomeModal': True, 'videoQuota': UNLIMITED, 'videoQuotaDaily': UNLIMITED,
        'videoChannels': [entities.channel(community) for community in owned_channels(user)],
        'notificationSettings': {name: 1 for name in NOTIFICATION_SETTINGS},
        'pluginAuth': None, 'twoFactorEnabled': False,
    }


@bp.route('/api/v1/users/me')
@signed_in()
def get_me():
    return jsonify(me(g.oauth_user))


def _text(fields: dict, name: str, limit: int) -> str | None:
    value = fields.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(name)
    return value.strip()


@bp.route('/api/v1/users/me', methods=['PUT'])
@signed_in('write')
def update_me():
    """The display name and description change; PeerTube's player and modal settings, which PieFed has not got, are
    taken and left as they are, as PeerTube leaves a field it does not know."""
    fields = request.get_json(silent=True)
    if not isinstance(fields, dict):
        fields = request.form.to_dict()
    try:
        display_name = _text(fields, 'displayName', MAX_DISPLAY_NAME)
        description = _text(fields, 'description', MAX_DESCRIPTION)
    except ValueError as invalid:
        return problem(400, 'invalid_request', f'{invalid} is too long or not text')
    if display_name is not None or description is not None:
        set_profile_text(g.oauth_user, display_name, description)
    return '', 204
