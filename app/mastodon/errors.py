"""How the Mastodon API answers a write that failed (plan W1.3), one rule for every route: a shared function's abort
keeps its status; its deliberate refusal (a bare Exception carrying its reason, as the alpha API's handler reads it)
is a 422 with that reason; anything else is a fault, logged here and answered 500 with no text, so an internal message
never reaches an app."""
from flask import current_app, jsonify
from werkzeug.exceptions import HTTPException

from app import db


def failure(error: Exception, refusals=()):
    """The response to `error`; `refusals` are further exception predicates whose message is a refusal's too."""
    db.session.rollback()
    if isinstance(error, HTTPException):
        return jsonify({'error': error.description or error.name}), error.code
    if type(error) is Exception or any(refused(error) for refused in refusals):
        return jsonify({'error': str(error) or 'Validation failed'}), 422
    current_app.logger.exception('mastodon write failed')
    return jsonify({'error': 'Something went wrong'}), 500
