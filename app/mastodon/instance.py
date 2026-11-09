"""Mastodon's instance documents, v1 and v2, for the domain asked (spec 2026-11-07-mastodon-client-api-design §3.2).
An app reads one before anyone signs in, to size its composer and uploads. Built from what the caller counted
(nodeinfo's usage), so this module needs no route."""
from flask import current_app, g

from app import db, webpush
from app.branding import own_value
from app.federation_urls import domain_site_name
from app.mastodon.entities import absolute
from app.models import Language
from app.storefront import domain_authority, open_registrations, registration_mode_on

# The Mastodon release whose API PieFed answers, raised as the phases add what it has (plan W1): apps choose their
# features by it, so it never claims an endpoint that is not here yet
MASTODON_COMPAT = '4.0.0'
SOURCE_URL = 'https://codeberg.org/rimu/pyfedi'
MAX_CHARACTERS = 5000
MAX_MEDIA_ATTACHMENTS = 4
IMAGE_TYPES = ('image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/avif', 'image/heic', 'image/heif')
VIDEO_TYPES = ('video/mp4', 'video/webm')
LOGO = '/static/images/piefed_logo_icon_t_512.png'


def version() -> str:
    return f"{MASTODON_COMPAT} (compatible; PieFed {current_app.config['VERSION']})"


def _languages() -> list[str]:
    language = db.session.get(Language, g.site.language_id) if g.site.language_id else None
    return [language.code] if language is not None and language.code else ['en']


def _configuration() -> dict:
    size = current_app.config['MAX_CONTENT_LENGTH']
    return {
        'accounts': {'max_featured_tags': 0, 'max_pinned_statuses': 0},
        'statuses': {'max_characters': MAX_CHARACTERS, 'max_media_attachments': MAX_MEDIA_ATTACHMENTS,
                     'characters_reserved_per_url': 23},
        'media_attachments': {'supported_mime_types': list(IMAGE_TYPES + VIDEO_TYPES), 'image_size_limit': size,
                              'image_matrix_limit': 16777216, 'video_size_limit': size, 'video_frame_rate_limit': 60,
                              'video_matrix_limit': 2304000},
        'polls': {'max_options': 10, 'max_characters_per_option': 50, 'min_expiration': 300,
                  'max_expiration': 2629746},
        'translation': {'enabled': False},
    }


def _own(domain, field: str) -> str:
    """`domain`'s own `field`, or the Site row's on the default domain alone: nothing a sister domain publishes names
    the default domain (owner ruling, app.federation_urls.domain_site_name)."""
    return own_value(domain, field) or ((getattr(g.site, field) or '') if domain.is_default else '')


def _contact(domain) -> str:
    """The default domain's contact email; a sister domain has none of its own to publish."""
    return (g.site.contact_email or '') if domain.is_default else ''


def _registration(domain) -> tuple[bool, bool]:
    return open_registrations(domain, g.site), registration_mode_on(domain, g.site) == 'RequireApplication'


def instance_v1(domain, usage: dict) -> dict:
    """GET /api/v1/instance: what it said before (title, uri, stats, registrations), and the rest of Mastodon's."""
    enabled, approval = _registration(domain)
    return {
        'uri': domain_authority(domain), 'title': domain_site_name(domain, g.site),
        'short_description': _own(domain, 'description'), 'description': _own(domain, 'about_html'),
        'email': _contact(domain), 'version': version(), 'urls': {},
        'stats': {'user_count': usage['users']['total'],
                  'status_count': usage['localPosts'] + usage['localComments'], 'domain_count': 1},
        'thumbnail': absolute(_own(domain, 'logo'), LOGO), 'languages': _languages(), 'registrations': enabled,
        'approval_required': approval, 'invites_enabled': False, 'configuration': _configuration(),
        'contact_account': None, 'rules': [],
    }


def _vapid(domain) -> dict:
    """The key an app subscribes to Web Push with: on a clearnet domain only (push is clearnet-only, app.mastodon.push)."""
    return {} if domain.is_onion else {'vapid': {'public_key': webpush.server_key()}}


def instance_v2(domain, usage: dict) -> dict:
    """GET /api/v2/instance; Tusky needs its `domain` and `version`."""
    enabled, approval = _registration(domain)
    return {
        'domain': domain_authority(domain), 'title': domain_site_name(domain, g.site), 'version': version(),
        'source_url': SOURCE_URL, 'description': _own(domain, 'description'),
        'usage': {'users': {'active_month': usage['users']['activeMonth']}},
        'thumbnail': {'url': absolute(_own(domain, 'logo'), LOGO)}, 'languages': _languages(),
        'configuration': {'urls': {}, **_configuration(), **_vapid(domain)},
        'registrations': {'enabled': enabled, 'approval_required': approval, 'message': None},
        'contact': {'email': _contact(domain), 'account': None}, 'rules': [],
    }
