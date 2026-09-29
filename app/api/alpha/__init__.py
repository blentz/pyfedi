from flask import Blueprint, current_app, jsonify
from flask_smorest import Blueprint as ApiBlueprint
from flask_limiter import RateLimitExceeded
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm.exc import NoResultFound
import sentry_sdk
from werkzeug.exceptions import HTTPException, UnprocessableEntity

# Non-documented routes in swagger UI
bp = Blueprint('api_alpha', __name__)

# Different blueprints to organize different api namespaces
site_bp = ApiBlueprint(
    "Site",
    __name__,
    url_prefix="/api/alpha",
    description="",
)

misc_bp = ApiBlueprint(
    "Misc",
    __name__,
    url_prefix="/api/alpha",
    description="",
)

comm_bp = ApiBlueprint(
    "Community",
    __name__,
    url_prefix="/api/alpha",
    description="",
)

feed_bp = ApiBlueprint(
    "Feed",
    __name__,
    url_prefix="/api/alpha",
    description="",
)

topic_bp = ApiBlueprint(
    "Topic",
    __name__,
    url_prefix="/api/alpha",
    description="",
)

user_bp = ApiBlueprint(
    "User",
    __name__,
    url_prefix="/api/alpha",
    description=""
)

reply_bp = ApiBlueprint(
    "Comment",
    __name__,
    url_prefix="/api/alpha",
    description=""
)

post_bp = ApiBlueprint(
    "Post",
    __name__,
    url_prefix="/api/alpha",
    description=""
)

private_message_bp = ApiBlueprint(
    "Private Message",
    __name__,
    url_prefix="/api/alpha",
    description=""
)

upload_bp = ApiBlueprint(
    "Upload",
    __name__,
    url_prefix="/api/alpha",
    description=""
)

admin_bp = ApiBlueprint(
    "Admin",
    __name__,
    url_prefix="/api/alpha",
    description=""
)


def shared_error_handler(e):
    """Shared error handler for all API blueprints"""
    if isinstance(e, RateLimitExceeded):
        response = {"code": 429, "message": str(e), "status": "Bad Request"}
        return jsonify(response), 429
    elif isinstance(e, NoResultFound):
        response = {"code": 400, "message": str(e), "status": "Not found"}
        return jsonify(response), 400
    elif isinstance(e, BlockingIOError):
        response = {"code": 400, "message": str(e), "status": "Bad credentials"}
        return jsonify(response), 400
    elif isinstance(e, UnprocessableEntity):
        # Log using the standard logging mechanism
        if current_app.config['SENTRY_DSN']:
            sentry_sdk.capture_exception(e)
        
        response = {"code": 400, "message": "Validation failed", "status": str(e.data['messages'])}
        return jsonify(response), 400
    elif isinstance(e, HTTPException) and isinstance(getattr(e, 'data', None), dict) \
            and 'message' in e.data:
        # flask_smorest's abort() stashes the reason it was given in `e.data`,
        # and `str(e)` is Werkzeug's generic description instead. Without this
        # branch, every deliberate refusal in app/api/alpha/routes.py -- all
        # 117 of them, including "alpha api is not enabled" -- reached the
        # caller as "400 Bad Request: The browser (or proxy) sent a request
        # that this server could not understand", for a request the server
        # understood perfectly and refused on purpose.
        response = {"code": e.code, "message": e.data['message'], "status": e.name}
        return jsonify(response), e.code
    else:
        if str(e) != 'incorrect_login' and str(e) != 'No object found.':
            current_app.logger.exception("API exception")
            if current_app.config['SENTRY_DSN']:
                sentry_sdk.capture_exception(e)
        # D1390. `str(e)` is the API's error contract -- the deliberate refusals
        # throughout app/shared and app/api/alpha/utils are bare
        # `Exception('incorrect_login')`, `Exception('access_denied')` and the
        # like, and the caller is meant to read them. A SQLAlchemyError's `str()`
        # is not that: it carries the driver's message, the full statement and the
        # bound parameters. Measured, the body a client received:
        #
        #     {"code":400,"message":"(psycopg2.errors.InvalidTextRepresentation)
        #      invalid input syntax for type boolean: \"not-a-bool\" ...
        #      [SQL: INSERT INTO \"user\" (user_name, banned) VALUES
        #      (%(n)s, %(b)s)] [parameters: {...}]"}
        #
        # -- schema, table and column names, and the query's shape, to any caller
        # who can provoke a database error. The exception is still logged and sent
        # to Sentry above, where it belongs; only what crosses the wire changes.
        #
        # Deliberately narrow: this covers the class whose `str()` embeds SQL.
        # Other internal types (ValueError, KeyError, AttributeError) still report
        # their own text, because suppressing those would change messages this
        # round has not enumerated -- and their text does not contain the schema.
        if isinstance(e, SQLAlchemyError):
            message = 'database error'
        else:
            message = str(e)
        response = {"code": 400, "message": message, "status": "Bad Request"}
        return jsonify(response), 400


# Register the shared error handler for all blueprints
blueprints = [
    site_bp,
    misc_bp,
    comm_bp,
    feed_bp,
    topic_bp,
    user_bp,
    reply_bp,
    post_bp,
    private_message_bp,
    upload_bp,
    admin_bp,
]
for blueprint in blueprints:
    blueprint.errorhandler(Exception)(shared_error_handler)

from app.api.alpha import routes
