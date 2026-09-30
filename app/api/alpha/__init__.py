from flask import Blueprint, current_app, jsonify
from flask_smorest import Blueprint as ApiBlueprint
from flask_limiter import RateLimitExceeded
from marshmallow import ValidationError
from sqlalchemy.orm.exc import NoResultFound
import sentry_sdk
from werkzeug.exceptions import HTTPException, UnprocessableEntity

from app.models import PostReplyValidationError

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
        # D1390, D895. `str(e)` is the API's error contract only for the
        # deliberate refusals: the bare `Exception('incorrect_login')`,
        # `Exception('access_denied')` and the like throughout app/shared and
        # app/api/alpha/utils, PostReply.new's PostReplyValidationError, and
        # validation and HTTP errors. Anything else is an internal error whose
        # `str()` may name a table, a column, the full SQL statement and its
        # bound parameters, or a path -- a SQLAlchemyError measurably did. It is
        # logged and sent to Sentry above; the caller gets a generic 500.
        if type(e) is Exception or isinstance(e, (PostReplyValidationError, ValidationError, HTTPException)):
            response = {"code": 400, "message": str(e), "status": "Bad Request"}
            return jsonify(response), 400
        response = {"code": 500, "message": "internal error", "status": "Internal Server Error"}
        return jsonify(response), 500


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
