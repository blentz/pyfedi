"""What a page in a custom theme loads from its own domain: the theme's stylesheet (custom themes spec §3.3)."""
from flask import Blueprint, Response, abort, request

from app.branding import onion_safe_css
from app.custom_themes import custom_identifier, custom_theme_here, theme_version
from app.storefront import current_storefront, storefront_host

bp = Blueprint('custom_themes', __name__)

LONG_CACHE = 'public, max-age=31536000, immutable'   # a save changes `v`, so a page never links an old copy
SHORT_CACHE = 'public, max-age=60'                   # an old or missing `v`: the theme as it is now, briefly


@bp.route('/theme/<int:theme_id>/styles.css', methods=['GET'])
def stylesheet(theme_id):
    """A custom theme's CSS, only on the domain that owns it (CT3). On the onion, off-site references are emptied."""
    domain = current_storefront()
    theme = custom_theme_here(custom_identifier(theme_id), domain)
    if theme is None:
        abort(404)
    css = onion_safe_css(theme.css, storefront_host(), f'custom theme {theme.id}') if domain.is_onion else theme.css
    response = Response(css, mimetype='text/css')
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Cache-Control'] = LONG_CACHE if request.args.get('v') == str(theme_version(theme)) \
        else SHORT_CACHE
    return response
