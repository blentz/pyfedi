# This file is part of PieFed, which is licensed under the GNU Affero General Public License (AGPL) version 3.0.
# You should have received a copy of the GPL along with this program. If not, see <http://www.gnu.org/licenses/>.

import logging
from logging.handlers import SMTPHandler, RotatingFileHandler
import os
from flask import Flask, request, current_app, session
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager, current_user
from flask_bootstrap import Bootstrap5
from flask_mail import Mail
from flask_babel import Babel, lazy_gettext as _l
from flask_caching import Cache
from flask_compress import Compress
from flask_limiter import Limiter
from flask_smorest import Api
from flask_bcrypt import Bcrypt
from werkzeug.middleware.proxy_fix import ProxyFix
from celery import Celery
from sqlalchemy_searchable import make_searchable
import httpx
from authlib.integrations.flask_client import OAuth

from config import Config
from app.pinned_http import pinned_transport
import sentry_sdk
from app.plugins import load_plugins


def get_locale():
    try:
        if current_user.is_authenticated and current_user.interface_language:
            return current_user.interface_language
        elif session.get('ui_language', None):
            return session['ui_language']
        else:
            try:
                # `or 'en'`: best_match returns None -- it does not raise --
                # when the request sends no Accept-Language header at all, or
                # one that matches nothing in LANGUAGES. The bare except below
                # only catches exceptions, so that None was returned to callers
                # as if it were a locale. dateparser.parse(languages=[None])
                # raises, which app/post/forms.py's bare `except Exception`
                # then reported to the user as "Invalid." for every reminder.
                return request.accept_languages.best_match(current_app.config['LANGUAGES']) or 'en'
            except:
                return 'en'
    except:
        return 'en'


def get_ip_address() -> str:
    """The client IP address, from the source this deployment is configured to trust.

    This is Flask-Limiter's key function (see `limiter` below) and, re-exported as
    `app.utils.ip_address`, the value IP bans, the honeypot ban, geolocation and the
    `user.ip_address` audit column are derived from. Everything here therefore has to
    come from a source the client cannot write: a client that can choose this value
    chooses its own rate-limit bucket and steps around an IP ban.

    By default it is `request.remote_addr`, which `ProxyFix(x_for=1)` (installed in
    `create_app`) has already resolved from the last entry of `X-Forwarded-For` - the
    entry appended by the one trusted reverse proxy PieFed sits behind. Caddy and
    nginx operators need to configure nothing.

    `TRUSTED_CLIENT_IP_HEADER` names a header to read instead. That is for the CDN
    case, where a single-IP header is authoritative and `X-Forwarded-For`'s last hop
    is the CDN rather than the client (Cloudflare: `CF-Connecting-IP`). Only set it
    when the named header is written by infrastructure you control.

    If the configured header holds a comma-separated list, the LAST entry is used.
    That is counter-intuitive and deliberate: every hop APPENDS the address it saw,
    so the last entry is the one written by the proxy nearest PieFed - the only entry
    a client cannot forge. The first entry is simply whatever the client sent, which
    is what this function used to return.
    """
    try:
        header_name = current_app.config.get('TRUSTED_CLIENT_IP_HEADER')
        ip = request.headers.get(header_name) if header_name else None
        if ip and ',' in ip:
            ip = ip[ip.rindex(',') + 1:]
        ip = ip.strip() if ip else ''
        if not ip:
            ip = request.remote_addr or ''
    except RuntimeError:  # no application or request context (e.g. a CLI command)
        ip = ''
    return ip


db = SQLAlchemy(session_options={"autoflush": False}, engine_options={'pool_size': Config.DB_POOL_SIZE, 'max_overflow': Config.DB_MAX_OVERFLOW, 'pool_recycle': 3600})
make_searchable(db.metadata)
migrate = Migrate()
login = LoginManager()
login.login_view = 'auth.login'
login.login_message = _l('Please log in to access this page.')
mail = Mail()
bootstrap = Bootstrap5()
babel = Babel(locale_selector=get_locale)
cache = Cache()
compress = Compress()
limiter = Limiter(get_ip_address, storage_uri='redis+'+Config.CACHE_REDIS_URL if Config.CACHE_REDIS_URL.startswith("unix://") else Config.CACHE_REDIS_URL)
celery = Celery(__name__, broker=Config.CELERY_BROKER_URL)
httpx_client = httpx.Client(http2=True, transport=pinned_transport(http2=True))  # R162
oauth = OAuth()
redis_client = None  # Will be initialized in create_app()
rest_api = Api()
app_bcrypt = Bcrypt()


class StripCookieVaryForAnonymous:
    """WSGI middleware that removes 'Cookie' from the Vary header for requests
    that have no session cookie. This allows Cloudflare to cache responses for
    anonymous users, which Flask's session middleware prevents by always adding
    Vary: Cookie when it processes the session."""

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        has_session_cookie = 'session=' in environ.get('HTTP_COOKIE', '')

        def custom_start_response(status, headers, exc_info=None):
            if not has_session_cookie:
                new_headers = []
                for name, value in headers:
                    if name.lower() == 'vary':
                        value = ', '.join(
                            part.strip() for part in value.split(',')
                            if part.strip().lower() != 'cookie'
                        )
                    new_headers.append((name, value))
                headers = new_headers
            return start_response(status, headers, exc_info)

        return self.app(environ, custom_start_response)


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)
    if app.config['HTTP_PROTOCOL'] == 'mixed':  # mixed mode is for instances like retro.piefed.com which has a web ui that uses http while federation happens over https
        app.config["SERVER_URL"] = f"https://{app.config['SERVER_NAME']}"
    else:
        app.config["SERVER_URL"] = f"{app.config['HTTP_PROTOCOL']}://{app.config['SERVER_NAME']}"

    if app.config['SENTRY_DSN']:
        sentry_sdk.init(
            dsn=app.config["SENTRY_DSN"],
            enable_tracing=False,
        )

    app.wsgi_app = StripCookieVaryForAnonymous(ProxyFix(app.wsgi_app, x_for=1))

    app.config["API_TITLE"] = "PieFed 1.7 Alpha API"
    app.config["API_VERSION"] = "alpha 1.7"
    app.config["OPENAPI_VERSION"] = "3.1.1"
    if not app.config["SECRET_KEY"]:
        raise Exception('You must set SECRET_KEY to a random sequence of numbers and letters.')
    if app.config["SERVE_API_DOCS"]:
        app.config["OPENAPI_URL_PREFIX"] = "/api/alpha"
        app.config["OPENAPI_JSON_PATH"] = "/swagger.json"
        app.config["OPENAPI_SWAGGER_UI_PATH"] = "/swagger"
        app.config["OPENAPI_SWAGGER_UI_URL"] = "https://cdn.jsdelivr.net/npm/swagger-ui-dist/"
        app.config["API_SPEC_OPTIONS"] = {
            "security": [{"bearerAuth": []}],
            "components": {
                "securitySchemes": {
                    "bearerAuth": {
                        "type": "http",
                        "scheme": "bearer",
                        "bearerFormat": "JWT",
                    }
                }
            },
            "servers": [
                {
                    "url": f"{app.config['HTTP_PROTOCOL']}://{app.config['SERVER_NAME']}",
                    "description": "This instance",
                },
                {
                    "url": "https://piefed.social",
                    "description": "Flagship instance, stable branch",
                },
                {
                    "url": "https://crust.piefed.social",
                    "description": "Official development instance",
                },
            ],
            "info": {
                "title": "PieFed 1.7 Alpha API",
                "contact": {
                    "name": "Developer",
                    "url": "https://codeberg.org/rimu/pyfedi",
                },
                "license": {
                    "name": "AGPLv3",
                    "url": "https://www.gnu.org/licenses/agpl-3.0.en.html#license-text",
                },
            },
        }
    rest_api.init_app(app)
    rest_api.DEFAULT_ERROR_RESPONSE_NAME = None  # Don't include default errors, define them ourselves

    db.init_app(app)
    migrate.init_app(app, db, render_as_batch=True)
    login.init_app(app)
    mail.init_app(app)
    bootstrap.init_app(app)
    babel.init_app(app, locale_selector=get_locale)
    cache.init_app(app)
    compress.init_app(app)
    limiter.init_app(app)
    app_bcrypt.init_app(app)
    celery.conf.update(app.config)

    celery.conf.update(CELERY_ROUTES={
        'app.shared.tasks.users.check_user_application': {'queue': 'background'},
        'app.user.utils.purge_user_then_delete_task': {'queue': 'background'},
        'app.community.util.retrieve_mods_and_backfill': {'queue': 'background'},
        'app.discovery.backfill.backfill_discovered_community': {'queue': 'background'},
        'app.discovery.sync.*': {'queue': 'background'},
        'app.community.util.send_to_remote_instance_task': {'queue': 'send'},
        'app.activitypub.signature.post_request': {'queue': 'send'},
        # Maintenance tasks - all go to background queue
        'app.shared.tasks.maintenance.*': {'queue': 'background'},
        'app.admin.routes.*': {'queue': 'background'},
        'app.admin.util.*': {'queue': 'background'},
    })

    # Initialize redis_client
    global redis_client
    from app.utils import get_redis_connection  # cycle: app.utils imports db and the other extensions from this package
    redis_client = get_redis_connection(app.config['CACHE_REDIS_URL'])

    oauth.init_app(app)
    if app.config['GOOGLE_OAUTH_CLIENT_ID']:
        oauth.register(
            name='google',
            client_id=app.config['GOOGLE_OAUTH_CLIENT_ID'],
            client_secret=app.config['GOOGLE_OAUTH_SECRET'],
            access_token_url='https://oauth2.googleapis.com/token',
            authorize_url='https://accounts.google.com/o/oauth2/v2/auth',
            api_base_url='https://www.googleapis.com/',
            client_kwargs={'scope': 'email profile'}
        )
    if app.config["MASTODON_OAUTH_CLIENT_ID"]:
        oauth.register(
            name="mastodon",
            client_id=app.config["MASTODON_OAUTH_CLIENT_ID"],
            client_secret=app.config["MASTODON_OAUTH_SECRET"],
            access_token_url=f"https://{app.config['MASTODON_OAUTH_DOMAIN']}/oauth/token",
            authorize_url=f"https://{app.config['MASTODON_OAUTH_DOMAIN']}/oauth/authorize",
            api_base_url=f"https://{app.config['MASTODON_OAUTH_DOMAIN']}/api/v1/",
            client_kwargs={"response_type": "code"}
        )

    if app.config["DISCORD_OAUTH_CLIENT_ID"]:
        oauth.register(
            name="discord",
            client_id=app.config["DISCORD_OAUTH_CLIENT_ID"],
            client_secret=app.config["DISCORD_OAUTH_SECRET"],
            access_token_url="https://discord.com/api/oauth2/token",
            authorize_url="https://discord.com/api/oauth2/authorize",
            api_base_url="https://discord.com/api/",
            client_kwargs={"scope": "identify email"}
        )

    from app.main import bp as main_bp  # cycle: blueprint modules import db and the other extensions from this package
    app.register_blueprint(main_bp)

    from app.errors import bp as errors_bp  # cycle: blueprint modules import db and the other extensions from this package
    app.register_blueprint(errors_bp)

    from app.admin import bp as admin_bp  # cycle: blueprint modules import db and the other extensions from this package
    app.register_blueprint(admin_bp, url_prefix='/admin')

    from app.activitypub import bp as activitypub_bp  # cycle: blueprint modules import db and the other extensions from this package
    app.register_blueprint(activitypub_bp)

    from app.auth import bp as auth_bp  # cycle: blueprint modules import db and the other extensions from this package
    app.register_blueprint(auth_bp, url_prefix='/auth')

    from app.community import bp as community_bp  # cycle: blueprint modules import db and the other extensions from this package
    app.register_blueprint(community_bp, url_prefix='/community')

    from app.post import bp as post_bp  # cycle: blueprint modules import db and the other extensions from this package
    app.register_blueprint(post_bp)

    from app.user import bp as user_bp  # cycle: blueprint modules import db and the other extensions from this package
    app.register_blueprint(user_bp)

    from app.domain import bp as domain_bp  # cycle: blueprint modules import db and the other extensions from this package
    app.register_blueprint(domain_bp)

    from app.feed import bp as feed_bp  # cycle: blueprint modules import db and the other extensions from this package
    app.register_blueprint(feed_bp)

    from app.instance import bp as instance_bp  # cycle: blueprint modules import db and the other extensions from this package
    app.register_blueprint(instance_bp)

    from app.topic import bp as topic_bp  # cycle: blueprint modules import db and the other extensions from this package
    app.register_blueprint(topic_bp)

    from app.chat import bp as chat_bp  # cycle: blueprint modules import db and the other extensions from this package
    app.register_blueprint(chat_bp)

    from app.search import bp as search_bp  # cycle: blueprint modules import db and the other extensions from this package
    app.register_blueprint(search_bp)

    from app.tag import bp as tag_bp  # cycle: blueprint modules import db and the other extensions from this package
    app.register_blueprint(tag_bp)

    from app.dev import bp as dev_bp  # cycle: blueprint modules import db and the other extensions from this package
    app.register_blueprint(dev_bp)

    from app.api.alpha import bp as app_api_bp  # cycle: blueprint modules import db and the other extensions from this package
    app.register_blueprint(app_api_bp)

    # API Namespaces
    # cycle: blueprint modules import db and the other extensions from this package
    from app.api.alpha import site_bp, misc_bp, comm_bp, feed_bp, topic_bp, user_bp, \
                              reply_bp, post_bp, upload_bp, private_message_bp, admin_bp
    rest_api.register_blueprint(site_bp)
    rest_api.register_blueprint(misc_bp)
    rest_api.register_blueprint(comm_bp)
    rest_api.register_blueprint(feed_bp)
    rest_api.register_blueprint(topic_bp)
    rest_api.register_blueprint(user_bp)
    rest_api.register_blueprint(reply_bp)
    rest_api.register_blueprint(post_bp)
    rest_api.register_blueprint(upload_bp)
    rest_api.register_blueprint(private_message_bp)
    rest_api.register_blueprint(admin_bp)

    # send error reports via email
    if app.config['MAIL_SERVER'] and app.config['ERRORS_TO']:
        auth = None
        if app.config['MAIL_USERNAME'] or app.config['MAIL_PASSWORD']:
            auth = (app.config['MAIL_USERNAME'],
                    app.config['MAIL_PASSWORD'])
        secure = None
        if app.config['MAIL_USE_TLS']:
            secure = ()
        mail_handler = SMTPHandler(
            mailhost=(app.config['MAIL_SERVER'], app.config['MAIL_PORT']),
            fromaddr=(app.config['MAIL_FROM']),
            # D873: a comma-separated list, split, or SMTPHandler sends to one malformed address
            toaddrs=[addr.strip() for addr in app.config['ERRORS_TO'].split(',') if addr.strip()],
            subject='PieFed error',
            credentials=auth, secure=secure, timeout=5.0)
        mail_handler.setLevel(logging.ERROR)
        app.logger.addHandler(mail_handler)

    # log rotation
    if not os.path.exists('logs'):
        os.mkdir('logs')
    file_handler = RotatingFileHandler('logs/pyfedi.log',
                                       maxBytes=1002400, backupCount=15)
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s: %(message)s '
        '[in %(pathname)s:%(lineno)d]'))
    file_handler.setLevel(logging.INFO)
    app.logger.addHandler(file_handler)

    app.logger.setLevel(logging.INFO)

    # Load plugins
    load_plugins()

    from app.request_hooks import register_request_hooks  # cycle: app.request_hooks imports app.models, which imports db from this package
    # Must be registered after compress.init_app(app) above. Flask runs after_request
    # callbacks in reverse registration order, so registering here (last) makes our
    # after_request in app/request_hooks.py run FIRST and Flask-Compress run after it,
    # appending Accept-Encoding to the Vary header our hook just merged (see the comment
    # on response.vary.update(...) in app/request_hooks.py). Moving this call earlier
    # -- e.g. up next to the other init_app() calls -- would silently flip that order.
    # test_request_hooks.py's test_after_request_runs_before_flask_compress pins it.
    register_request_hooks(app)

    return app


from app import models
