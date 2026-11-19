"""What a page in a custom theme loads from its own domain: the theme's stylesheet, script and files (custom themes spec
§3.3, §3.7; theme code spec §3.3)."""
import os

from flask import Blueprint, Response, abort, request, send_file

from app.branding import onion_safe_css
from app.custom_themes import SVG, custom_identifier, custom_theme_here, theme_version
from app.storefront import current_storefront, storefront_host

bp = Blueprint('custom_themes', __name__)

LONG_CACHE = 'public, max-age=31536000, immutable'   # a save changes `v`, so a page never links an old copy
SHORT_CACHE = 'public, max-age=60'                   # an old or missing `v`: the theme as it is now, briefly
FILE_CACHE = 300                                     # a file's URL stays the same when it is replaced
# An SVG opened by itself runs nothing and loads nothing (loaded from CSS, as an image, it never could)
SVG_POLICY = "default-src 'none'; style-src 'unsafe-inline'; sandbox"


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


@bp.route('/theme/<int:theme_id>/scripts.js', methods=['GET'])
def script(theme_id):
    """A custom theme's own scripts.js (theme code spec §3.3), only on the domain that owns it (CT3)."""
    theme = custom_theme_here(custom_identifier(theme_id), current_storefront())
    if theme is None or not theme.script:
        abort(404)
    response = Response(theme.script, mimetype='application/javascript')
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Cache-Control'] = LONG_CACHE if request.args.get('v') == str(theme_version(theme)) \
        else SHORT_CACHE
    return response


@bp.route('/theme/<int:theme_id>/files/<filename>', methods=['GET'])
def theme_file(theme_id, filename):
    """One of a custom theme's files, as `url(files/<filename>)` in its CSS names it, with the type its content was
    checked to be, only on the domain that owns the theme (CT3)."""
    theme = custom_theme_here(custom_identifier(theme_id), current_storefront())
    file = next((file for file in theme.files if file.filename == filename), None) if theme is not None else None
    if file is None or not os.path.isfile(f'app{file.path}'):
        abort(404)
    response = send_file(os.path.abspath(f'app{file.path}'), mimetype=file.mime_type, max_age=FILE_CACHE,
                         conditional=True)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    if file.mime_type == SVG:
        response.headers['Content-Security-Policy'] = SVG_POLICY
    return response
