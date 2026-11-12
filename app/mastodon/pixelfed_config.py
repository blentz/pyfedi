"""Pixelfed's server configuration, apart from Pixelfed's routes (app.mastodon.pixelfed) so that nodeinfo
(app.activitypub.routes), which gives it to PixelDroid (ruling S6), needs none of the API they import."""
from flask import current_app, g, request

from app.client_identity import PIXELFED_VERSION
from app.mastodon.instance import IMAGE_TYPES, MAX_CHARACTERS, VIDEO_TYPES
from app.utils import max_album_images

MAX_ALT_TEXT = 1500
MAX_BIO = 1000   # the web form's (app.user.forms)


def pixelfed_configuration() -> dict:
    """Pixelfed's server configuration: what its apps read for its limits (the bio's length among them), and
    PixelDroid in nodeinfo's metadata (app.activitypub.routes, ruling S6)."""
    site = g.site
    return {
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
    }
