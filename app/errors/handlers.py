import math
import time

from flask import current_app, jsonify, render_template, request

from app import db, limiter
from app.errors import bp
from app.cms import members_only_refusal
from app.models import CmsPage
from app.peertube.entities import problem


# A routing error's words in each API's format: 404, the path names no route; 405, its route takes other methods
_ROUTING = {404: ('not_found', 'not found', 'Not Found', 'Not found', 'Record not found'),
            405: ('method_not_allowed', 'method not allowed', 'Method Not Allowed', 'Method not allowed',
                  'Method not allowed')}


def api_routing_error(status: int):
    """The routing error `status` of the API whose prefix the path is under, in that API's own error format (ruling
    FR1), or None for a path no API owns. Mastodon and PeerTube share /api/v1 and /api/v2: PeerTube's problem+json
    carries the `error` Mastodon's clients read."""
    code, message, name, title, mastodon = _ROUTING[status]
    path = request.path
    if path.startswith('/api/v3/') or path == '/api/v3':
        return jsonify({'error': code}), status
    if path.startswith('/api/alpha/'):
        return jsonify({'code': status, 'message': message, 'status': name, 'error': code}), status
    if path.startswith(('/api/v1.1/', '/api/pixelfed/')):
        return jsonify({'error': mastodon}), status
    if path.startswith(('/api/v1/', '/api/v2/')):
        return problem(status, code, title)
    if path.startswith('/oauth/') and not path.startswith('/oauth/authorize'):
        return jsonify({'error': code}), status
    return None


@bp.app_errorhandler(404)
def not_found_error(error):
    answer = api_routing_error(404)
    if answer is not None:
        return answer
    # Skip page lookup for static files and known non-page paths to avoid unnecessary DB hits
    if (request.path.startswith('/static/') or
            request.path.startswith('/api/') or
            request.path.startswith('/.well-known/') or
            request.path.startswith('/admin/') or
            request.path.startswith('/auth/') or
            request.path.endswith(('.jpg', '.jpeg', '.png', '.gif', '.css', '.js', '.ico', '.svg', '.woff', '.woff2',
                                   '.ttf', '.webp', '.txt'))):
        return render_template('errors/404.html'), 404

    # Check if there's a page for this URL. A path holding a NUL names none, and Postgres refuses it in a string.
    if '\x00' in request.path:
        return render_template('errors/404.html'), 404
    cms_page = CmsPage.query.filter(CmsPage.url == request.path).first()
    if cms_page:
        return members_only_refusal(cms_page) or render_template('cms_page.html', page=cms_page)

    # Fall back to standard 404 page. This used to return the bare string
    # 'not found', which meant the branch above -- the one that exists to SKIP
    # the rendered page for static files and asset paths -- was the only one
    # that rendered it, and a person who mistyped a URL got nine bytes of
    # unstyled text.
    return render_template('errors/404.html'), 404


@bp.app_errorhandler(405)
def method_not_allowed_405(error):
    """An API's own 405; any other path's as flask-smorest's handler of every HTTPException has answered it."""
    answer = api_routing_error(405)
    return answer if answer is not None else (jsonify({'code': 405, 'status': error.name}), 405)


@bp.app_errorhandler(500)
def internal_error_500(error):
    db.session.rollback()
    return render_template('errors/500.html'), 500


@bp.app_errorhandler(401)
def internal_error_401(error):
    db.session.rollback()
    return render_template('errors/401.html'), 401


@bp.app_errorhandler(413)
def request_too_large_413(error):
    # R207: a body over MAX_CONTENT_LENGTH. Flask prefers this code handler to the API blueprints' own, so it answers them in their JSON shape
    if request.path.startswith('/api/'):
        return jsonify({"code": 413, "message": "request too large", "status": error.name}), 413
    return render_template('errors/413.html'), 413


@bp.app_errorhandler(429)
def rate_limited_429(error):
    # The API's clients parse JSON in its own error shape (see shared_error_handler), not a rendered page
    if request.path.startswith('/api/'):
        # flask_smorest's abort() keeps the reason in `data`, as shared_error_handler reads it
        data = getattr(error, 'data', None)
        message = data['message'] if isinstance(data, dict) and 'message' in data else error.description
        response = jsonify({"code": 429, "message": message, "status": "Bad Request"})
        retry_after = dict(error.get_headers()).get('Retry-After')
        # The limiter only adds its own headers when RATELIMIT_HEADERS_ENABLED is set; its breached limit's window
        # reset says how long a client has to back off either way
        if not retry_after:
            try:
                current = limiter.current_limit
                if current is not None and current.breached:
                    retry_after = str(max(1, math.ceil(current.reset_at - time.time())))
            except Exception as e:   # reset_at reads the limiter's storage; a 429 is still the answer without it
                current_app.logger.warning('No Retry-After on an API 429: %s', e)
        if retry_after:
            response.headers['Retry-After'] = retry_after
        return response, 429
    return render_template('errors/429.html', error=error), 429
