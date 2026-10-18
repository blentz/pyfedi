"""A rate-limited request under /api/ is answered in the API's JSON error shape; web pages keep the HTML page."""
import json

from werkzeug.exceptions import TooManyRequests

from app.errors.handlers import rate_limited_429


def _limited(app, path, retry_after=None):
    error = TooManyRequests('3 per 1 minute')
    if retry_after:
        error.retry_after = retry_after
    with app.test_request_context(path):
        response, status = rate_limited_429(error)
        return app.make_response((response, status))


def test_api_429_is_json_in_the_api_error_shape(app):
    response = _limited(app, '/api/alpha/post/list')
    assert response.status_code == 429
    assert response.mimetype == 'application/json'
    assert json.loads(response.get_data()) == {'code': 429, 'message': '3 per 1 minute',
                                                'status': 'Bad Request'}


def test_api_429_keeps_retry_after(app):
    response = _limited(app, '/api/alpha/post/list', retry_after=42)
    assert response.headers['Retry-After'] == '42'


def test_web_429_stays_html(app):
    response = _limited(app, '/auth/login')
    assert response.status_code == 429
    assert response.mimetype == 'text/html'
