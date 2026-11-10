"""What PeerTube's own app (Framasoft's, Flutter) requires of each entity it decodes: every non-null field, with its
type (spec 2026-11-09-peertube-login-design §2). Its decoders throw on a missing or mistyped field, and one bad
element fails a whole list, so each shape here is a contract an answer must meet.

Read from framasoft/peertube/mobile-application v2.3.0 (commit 9bd3a4d, 2026-11-04): ConfigServer in
lib/shared/config/config_models.dart, the token in lib/shared/auth/token_api_model.dart, User in
lib/features/user/user_model.dart, PlatformAPI (/config/about), Video in lib/features/video/video_model.dart, and the account, channel and image
models they hold.

A shape is a type (str, int, bool), a dict of shapes, or a one-element list holding the shape of every element."""

FILE = {'size': {'max': int}, 'extensions': [str]}

CONFIG = {
    'serverVersion': str,
    'client': {'videos': {'miniature': {'preferAuthorDisplayName': bool}, 'resumableUpload': {'maxChunkSize': int}},
               'menu': {'login': {'redirectOnSingleExternalAuth': bool}}},
    'instance': {'name': str, 'shortDescription': str, 'isNSFW': bool, 'defaultNSFWPolicy': str,
                 'defaultClientRoute': str},
    'search': {'remoteUri': {'users': bool, 'anonymous': bool},
               'searchIndex': {'enabled': bool, 'url': str, 'disableLocalSearch': bool, 'isDefaultSearch': bool}},
    'avatar': {'file': FILE},
    'banner': {'file': FILE},
    'video': {'image': FILE, 'file': {'extensions': [str]}},
    'videoCaption': {'file': FILE},
    'user': {'videoQuota': int, 'videoQuotaDaily': int},
    'import': {'videos': {'http': {'enabled': bool}, 'torrent': {'enabled': bool}},
               'videoChannelSynchronization': {'enabled': bool}, 'users': {'enabled': bool}},
    'live': {'enabled': bool, 'allowReplay': bool, 'latencySetting': {'enabled': bool}, 'maxDuration': int,
             'maxInstanceLives': int, 'maxUserLives': int,
             'transcoding': {'enabled': bool, 'remoteRunners': {'enabled': bool}, 'enabledResolutions': [int],
                             'profile': str, 'availableProfiles': [str]},
             'rtmp': {'port': int}},
    'defaults': {'publish': {'downloadEnabled': bool, 'commentsPolicy': int, 'privacy': int},
                 'p2p': {'webapp': {'enabled': bool}, 'embed': {'enabled': bool}},
                 'player': {'autoPlay': bool}},
    'signup': {'allowed': bool, 'allowedForCurrentIP': bool, 'requiresEmailVerification': bool, 'minimumAge': int},
    'storyboards': {'enabled': bool},
    'views': {'videos': {'watchingInterval': {'users': int}}},
    'plugin': {'registered': [{'name': str}], 'registeredExternalAuths': [{'name': str}],
               'registeredIdAndPassAuths': [{'name': str}]},
    # optional in the app's model, but an object the API sends is decoded whole: its own fields are then required
    'theme': {'registered': [{'name': str}], 'default': str, 'builtIn': [{'name': str}]},
}

IMAGE = {'createdAt': str, 'updatedAt': str}
ACTOR = {'id': int, 'url': str, 'name': str, 'host': str, 'displayName': str, 'avatars': [IMAGE]}
ACCOUNT = {**ACTOR, 'followingCount': int, 'followersCount': int, 'createdAt': str, 'updatedAt': str}
CHANNEL = {**ACCOUNT, 'banners': [IMAGE], 'isLocal': bool}
CONSTANT = {'label': str}
VIDEO = {'id': int, 'uuid': str, 'shortUUID': str, 'name': str, 'category': CONSTANT, 'licence': CONSTANT,
         'language': CONSTANT, 'privacy': {'id': int, 'label': str}, 'nsfw': bool, 'duration': int, 'views': int,
         'likes': int, 'dislikes': int, 'createdAt': str, 'updatedAt': str, 'publishedAt': str, 'isLive': bool,
         'account': ACTOR, 'channel': ACTOR, 'url': str}
VIDEO_FILE = {'id': int, 'resolution': CONSTANT, 'size': int, 'torrentUrl': str, 'torrentDownloadUrl': str,
              'fileUrl': str, 'fileDownloadUrl': str, 'fps': int}
VIDEO_DETAILS = {**VIDEO, 'isLocal': bool, 'account': ACCOUNT, 'channel': CHANNEL, 'blacklisted': bool,
                 'files': [VIDEO_FILE], 'streamingPlaylists': [{'id': int}]}   # a video's own page
USER = {'account': ACCOUNT, 'autoPlayNextVideo': bool, 'autoPlayNextVideoPlaylist': bool, 'autoPlayVideo': bool,
        'blocked': bool, 'createdAt': str, 'email': str, 'id': int, 'lastLoginDate': str,
        'noInstanceConfigWarningModal': bool, 'noAccountSetupWarningModal': bool, 'noWelcomeModal': bool,
        'role': {'id': int, 'label': str}, 'theme': str, 'username': str, 'videoChannels': [CHANNEL],
        'videoQuota': int, 'videoQuotaDaily': int, 'p2pEnabled': bool,
        'notificationSettings': {'newCommentOnMyVideo': int}}   # TubeLab reads that one with no null check
COMMENT = {'id': int, 'url': str, 'text': str, 'threadId': int, 'videoId': int, 'createdAt': str, 'updatedAt': str,
           'isDeleted': bool, 'totalReplies': int, 'totalRepliesFromVideoAuthor': int}
ACTOR_INFO = {'id': int, 'displayName': str, 'name': str, 'host': str, 'avatars': [{'fileUrl': str}]}
VIDEO_INFO = {'id': int, 'uuid': str, 'shortUUID': str, 'name': str}
NOTIFICATION = {'id': int, 'type': int, 'read': bool, 'createdAt': str, 'updatedAt': str}
NOTIFICATION_OBJECTS = {   # what each type the API sends carries (lib/features/notification/notification_model.dart)
    1: {'video': {**VIDEO_INFO, 'channel': ACTOR_INFO}},
    2: {'comment': {'id': int, 'threadId': int, 'account': ACTOR_INFO, 'video': VIDEO_INFO}},
    10: {'actorFollow': {'id': int, 'follower': ACTOR_INFO, 'state': str,
                         'following': {'type': str, 'name': str, 'displayName': str, 'host': str}}},
    11: {'comment': {'id': int, 'threadId': int, 'account': ACTOR_INFO, 'video': VIDEO_INFO}},
}
NOTIFICATION_TYPES = range(1, 23)   # UserNotificationType: one outside these fails the whole list
PRIVACIES = range(1, 6)   # VideoPrivacy: one outside these fails the decode

ABOUT = {'instance': {**{name: str for name in (
    'name', 'shortDescription', 'description', 'terms', 'codeOfConduct', 'hardwareInformation', 'creationReason',
    'moderationInformation', 'administrator', 'maintenanceLifetime', 'businessModel')},
    'languages': [str], 'categories': [int], 'banners': [IMAGE], 'avatars': [IMAGE]}}   # PlatformAPI

TOKEN = {'access_token': str, 'refresh_token': str, 'token_type': str}


def problems(value, shape, path='') -> list[str]:
    """Each way `value` breaks `shape`, by its path: a missing or null field, or one of another type."""
    if isinstance(shape, dict):
        if not isinstance(value, dict):
            return [f'{path or "."}: not an object']
        found = []
        for name, inner in shape.items():
            if value.get(name) is None:
                found.append(f'{path}.{name}: missing or null')
            else:
                found += problems(value[name], inner, f'{path}.{name}')
        return found
    if isinstance(shape, list):
        if not isinstance(value, list):
            return [f'{path}: not a list']
        return [problem for index, item in enumerate(value) for problem in problems(item, shape[0], f'{path}[{index}]')]
    if shape is int and isinstance(value, bool) or not isinstance(value, shape):   # a bool is an int to Python only
        return [f'{path}: {type(value).__name__}, not {shape.__name__}']
    return []
