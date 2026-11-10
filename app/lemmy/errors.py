"""Lemmy's error shape: a 4xx with `{"error": "<code>"}`, the one Jerboa parses (its library's ErrorResponse) and
names on its login screen (spec 2026-11-10-lemmy-v3-design §3). Alpha's handlers refuse with alpha's codes, which are
Lemmy's where Lemmy has the same one; alpha's own handler decides what is logged and what is an internal error, and
this one only reshapes its answer. An endpoint not served answers 404 not_found, never a 500 (plan §3)."""
from flask import jsonify
from sqlalchemy.orm.exc import NoResultFound
from werkzeug.exceptions import HTTPException

from app.api.alpha import shared_error_handler
from app.lemmy import bp



def _names_nothing(message: str) -> bool:
    """Alpha's ways of saying there is no such thing: 'post not found', 'domain_not_found', 'No object found.'."""
    return message.endswith(('not found', 'not_found')) or message == 'No object found.'


def refusal(code: str, status: int = 400):
    return jsonify(error=code), status


def lemmy_error(error):
    if isinstance(error, NoResultFound) or (isinstance(error, HTTPException) and error.code == 404):
        return refusal('not_found', 404)
    answer, status = shared_error_handler(error)
    message = answer.get_json()['message']
    if _names_nothing(message):
        return refusal('not_found', 404)
    if status == 429:
        return refusal('rate_limit_error', 429)
    if status >= 500:
        return refusal('unknown', status)
    return refusal(message, status)


# Flask looks for a handler by status code before it looks by class, and the app's own 404 (a CMS page) and 429 are by
# code, so each code an API refusal may have is registered here too
for raised in (Exception, 400, 401, 403, 404, 405, 413, 415, 422, 429):
    bp.register_error_handler(raised, lemmy_error)


@bp.route('/api/v3/<path:_anything>', methods=['GET', 'POST', 'PUT', 'DELETE'])
def not_served(_anything):
    return refusal('not_found', 404)
