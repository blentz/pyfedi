"""A person's settings and a link's metadata in Lemmy's API (spec 2026-11-10-lemmy-v3-design §2), over alpha's
handlers. Lemmy's settings that PieFed has no such thing for (a theme, keyboard navigation, the listing mode) are not
kept: Lemmy's site document says what PieFed does instead."""
from flask import jsonify, request
from marshmallow import EXCLUDE

from app import db, limiter
from app.api.alpha.routes import METADATA_LIMIT, METADATA_SCOPE, is_trusted_request
from app.api.alpha.schema import GetSiteMetadataRequest, UserSaveSettingsRequest
from app.api.alpha.utils.site import get_site_metadata
from app.api.alpha.utils.user import put_user_save_user_settings
from app.lemmy import arguments, bp
from app.utils import authorise_api_user

# Lemmy's settings under alpha's names, where alpha's take the same value
SAME = {'show_nsfw': 'show_nsfw', 'show_read_posts': 'show_read_posts', 'display_name': 'display_name',
        'bio': 'bio', 'matrix_user_id': 'matrix_user_id', 'avatar': 'avatar', 'banner': 'cover',
        'bot_account': 'bot', 'send_notifications_to_email': 'email_unread'}
LISTINGS = {'All': 'all', 'Local': 'local', 'Subscribed': 'subscribed', 'ModeratorView': 'moderating'}


def _sort(lemmy: str) -> str:
    """A Lemmy post sort as the default alpha keeps: Top of any span is Top, the comment sorts Active."""
    if lemmy.startswith('Top'):
        return 'Top'
    return {'MostComments': 'Active', 'NewComments': 'Active', 'Controversial': 'Hot'}.get(lemmy, lemmy)


@bp.route('/api/v3/user/save_user_settings', methods=['PUT'])
def save_user_settings():
    given = request.get_json(silent=True)
    given = {name: value for name, value in given.items() if value is not None} if isinstance(given, dict) else {}
    raw = {alpha: given[lemmy] for lemmy, alpha in SAME.items() if lemmy in given}
    if isinstance(given.get('default_sort_type'), str):
        raw['default_sort_type'] = _sort(given['default_sort_type'])
    if isinstance(given.get('show_bot_accounts'), bool):
        raw['bot_visibility'] = 'Show' if given['show_bot_accounts'] else 'Hide'
    if given.get('blur_nsfw') is True and given.get('show_nsfw') is not False:
        raw['nsfw_visibility'] = 'Blur'
    data = UserSaveSettingsRequest().load(raw, unknown=EXCLUDE)
    listing = LISTINGS.get(given.get('default_listing_type'))
    put_user_save_user_settings(arguments.auth(), data)
    if listing is not None:   # alpha keeps no default listing: the person's own setting, set here
        user = authorise_api_user(arguments.auth(), return_type='model')
        user.default_filter = listing
        db.session.commit()
    return jsonify(success=True)


@bp.route('/api/v3/post/site_metadata')
def site_metadata():
    data = GetSiteMetadataRequest().load({'url': request.args.get('url', '')}, unknown=EXCLUDE)
    with limiter.shared_limit(METADATA_LIMIT, scope=METADATA_SCOPE, exempt_when=is_trusted_request):   # alpha's
        alpha = get_site_metadata(arguments.auth(), data)['metadata']
    return jsonify(metadata={'title': alpha.get('title') or None, 'description': alpha.get('description') or None,
                             'image': alpha.get('image') or None, 'embed_video_url': alpha.get('embed_video_url') or None})
