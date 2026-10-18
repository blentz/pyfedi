import math
import time

from flask import jsonify, render_template, request

from app import db, limiter
from app.errors import bp
from app.models import CmsPage


@bp.app_errorhandler(404)
def not_found_error(error):
    # Skip page lookup for static files and known non-page paths to avoid unnecessary DB hits
    if (request.path.startswith('/static/') or
            request.path.startswith('/api/') or
            request.path.startswith('/.well-known/') or
            request.path.startswith('/admin/') or
            request.path.startswith('/auth/') or
            request.path.endswith(('.jpg', '.jpeg', '.png', '.gif', '.css', '.js', '.ico', '.svg', '.woff', '.woff2',
                                   '.ttf', '.webp', '.txt'))):
        return render_template('errors/404.html'), 404

    # Check if there's a page for this URL
    cms_page = CmsPage.query.filter(CmsPage.url == request.path).first()
    if cms_page:
        return render_template('cms_page.html', page=cms_page)

    # Fall back to standard 404 page. This used to return the bare string
    # 'not found', which meant the branch above -- the one that exists to SKIP
    # the rendered page for static files and asset paths -- was the only one
    # that rendered it, and a person who mistyped a URL got nine bytes of
    # unstyled text.
    return render_template('errors/404.html'), 404


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
        current = limiter.current_limit
        if not retry_after and current is not None and current.breached:
            retry_after = str(max(1, math.ceil(current.reset_at - time.time())))
        if retry_after:
            response.headers['Retry-After'] = retry_after
        return response, 429
    return render_template('errors/429.html', error=error), 429
