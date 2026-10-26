from datetime import datetime
import os

import flask
from flask_babel import get_locale, ngettext
from flask_login import current_user
from flask_wtf.csrf import generate_csrf

from app import db
from app.branding import Branding, current_branding
from app.activitypub.entitlement import secure_mode_cache_headers, signed_fetch_refusal
from app.community.live import post_community_link
from app.community.util import actor_to_community
import pendulum
from flask import before_render_template, session, g, json, request, current_app
from sqlalchemy import text
from app.constants import POST_TYPE_LINK, POST_TYPE_IMAGE, POST_TYPE_ARTICLE, POST_TYPE_VIDEO, POST_TYPE_POLL, \
    SUBSCRIPTION_MODERATOR, SUBSCRIPTION_MEMBER, SUBSCRIPTION_OWNER, SUBSCRIPTION_PENDING, ROLE_ADMIN_NAME, VERSION, \
    POST_TYPE_EVENT, AP_MEDIA_TYPES, MEDIA_KIND_VIDEO, MEDIA_KIND_AUDIO
from app.models import Site, utcnow
from app.onion import is_onion_request, stamp_activity
from app.onion_guard import require_onion_session, resolve_onion_api_rate_key
from app.local_only import user_local_only
from app.storefront import adopt_host_storefront, check_default_domain, current_storefront, hosted_instance_ids, \
    is_local_link, resolve_storefront, rewrite_response_for_storefront, storefront_host, storefront_link, storefront_url
from app.shared.media import can_upload_media, media_kind_for, media_of, upload_byte_limit
from app.utils import can_moderate, getmtime, gibberish, shorten_string, shorten_url, digits, user_access, community_membership, joined_server_views, \
    can_upvote, can_downvote, shorten_number, ap_datetime, current_theme, community_link_to_href, \
    in_sorted_list, role_access, first_paragraph, person_link_to_href, feed_membership, html_to_text, remove_images, \
    notif_id_to_string, feed_link_to_href, get_setting, set_setting, show_explore, human_filesize, can_upload_video, can_upload_audio, \
    debug_checkpoint, compaction_level, humanize_number, round_invisible_digits, get_site_as_dict, localize_datetime, \
    display_back_button, favorite_communities, get_event_start, reply_filter_keyword, user_banned_from_community, \
    PROBE_PATH_RE, fetched_by_hand, log_probe_refused, log_register_attempt
from app.quotes import prime_quotes, quote_state
from app.variants import shown, switch_url
from app.visibility import can_view

MEDIA_UPLOAD_SLACK = 1024 * 1024   # the form fields around the file (A13.2)

# strict-dynamic allows scripts dynamically added by nonce-validated scripts (needed for htmx)
SCRIPT_POLICY = "script-src 'self' 'nonce-{nonce}' 'strict-dynamic'; object-src 'none'; base-uri 'none';"
# The onion's backstop (per-domain branding spec §3.4): a page loads its images, fonts, styles, media, frames and
# connections, and anything else default-src covers, from the onion alone. data: and blob: name bytes already in the
# page or the browser (the captcha image, a file chosen for upload shown before it is sent), never a host. Inline style
# is allowed: templates and htmx write style attributes, which no nonce can cover, and inline CSS can only fetch
# through url() and @import, which img-src, font-src and style-src still hold to the onion. Remote media reaches the
# page through the onion media proxy, a remote iframe becomes a placeholder and a link (storefront._proxy_remote).
_ONION_LOADS = " img-src 'self' data: blob:; font-src 'self' data:; style-src 'self' 'unsafe-inline';" \
               " connect-src 'self'; media-src 'self'; frame-src 'self';"
ONION_POLICY = "default-src 'self'; " + SCRIPT_POLICY + _ONION_LOADS
# A response made before before_request made the nonce (a limit Flask-Limiter checks in its own before_request,
# require_onion_session's answers, OPTIONS)
ONION_POLICY_WITHOUT_NONCE = "default-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none';" + _ONION_LOADS

def viewer_can_view(obj) -> bool:
    """can_view for the request's viewer, for templates that show one object inside another (R1)."""
    return can_view(obj, current_user.get_id())


class OnUse:
    """A template value computed only when a template uses it. Most pages never name the storefront, and outside a
    request (an email from a background job) finding the default storefront reads site_domain. Unlike werkzeug's
    LocalProxy it forwards no attribute lookups, so Jinja's probes of its context values do not compute it."""

    def __init__(self, compute):
        self._compute = compute

    def __str__(self):
        return self._compute()

    def __bool__(self):
        return bool(self._compute())

    def __add__(self, other):
        return self._compute() + other


class BrandingOnUse:
    """The template value `branding`: the request's domain's `Branding` (app/branding.py), looked up when a template
    first reads one of its fields and then kept for the rest of the render. As with OnUse, a render that names no field
    (an email from a background job) never looks it up, and a probe of the context values for anything else
    (prime_quotes asks each one for `items`) finds nothing."""

    def __init__(self):
        self._branding = None

    def __getattr__(self, field):
        if field not in Branding._fields:
            raise AttributeError(field)
        if self._branding is None:
            self._branding = current_branding()
        return getattr(self._branding, field)


def register_request_hooks(app):
    with app.app_context():
        check_default_domain()

    @app.context_processor
    def app_context_processor():
        return dict(getmtime=getmtime, instance_domain=OnUse(storefront_host), site_url=OnUse(storefront_url),
                    debug_mode=current_app.debug, on_onion=is_onion_request(),
                    pendulum=pendulum, locale=g.locale if hasattr(g, 'locale') else None, notif_server=OnUse(lambda: storefront_link(current_app.config['NOTIF_SERVER'])),
                    site=g.site if hasattr(g, 'site') else None, branding=BrandingOnUse(), nonce=g.nonce if hasattr(g, 'nonce') else None,
                    admin_ids=g.admin_ids if hasattr(g, 'admin_ids') else [], low_bandwidth=g.low_bandwidth if hasattr(g, 'low_bandwidth') else None,
                    can_translate=current_app.config['TRANSLATE_ENDPOINT'] != '', can_detect_ai=current_app.config['DETECT_AI_ENDPOINT'] != '',
                    POST_TYPE_LINK=POST_TYPE_LINK, POST_TYPE_IMAGE=POST_TYPE_IMAGE, notif_id_to_string=notif_id_to_string,
                    POST_TYPE_ARTICLE=POST_TYPE_ARTICLE, POST_TYPE_VIDEO=POST_TYPE_VIDEO, POST_TYPE_POLL=POST_TYPE_POLL,
                    POST_TYPE_EVENT=POST_TYPE_EVENT,
                    SUBSCRIPTION_MODERATOR=SUBSCRIPTION_MODERATOR, SUBSCRIPTION_MEMBER=SUBSCRIPTION_MEMBER,
                    SUBSCRIPTION_OWNER=SUBSCRIPTION_OWNER, SUBSCRIPTION_PENDING=SUBSCRIPTION_PENDING, VERSION=VERSION)

    @app.shell_context_processor
    def make_shell_context():
        return {'db': db, 'app': app}

    app.jinja_env.globals['len'] = len
    app.jinja_env.globals['digits'] = digits
    app.jinja_env.globals['str'] = str
    app.jinja_env.globals['shorten_number'] = shorten_number
    app.jinja_env.globals['community_membership'] = community_membership
    app.jinja_env.globals['feed_membership'] = feed_membership
    app.jinja_env.globals['json_loads'] = json.loads
    app.jinja_env.globals['user_access'] = user_access
    app.jinja_env.globals['can_moderate'] = can_moderate
    app.jinja_env.globals['role_access'] = role_access
    app.jinja_env.globals['ap_datetime'] = ap_datetime
    app.jinja_env.globals['can_upvote'] = can_upvote
    app.jinja_env.globals['can_downvote'] = can_downvote
    app.jinja_env.globals['can_upload_video'] = can_upload_video
    app.jinja_env.globals['can_upload_audio'] = can_upload_audio
    app.jinja_env.globals['can_upload_media'] = can_upload_media
    app.jinja_env.globals['media_kind_for'] = media_kind_for
    app.jinja_env.globals['media_of'] = media_of
    app.jinja_env.globals['show_explore'] = show_explore
    app.jinja_env.globals['in_sorted_list'] = in_sorted_list
    app.jinja_env.globals['theme'] = current_theme
    app.jinja_env.globals['current_branding'] = current_branding   # for macro files, which see no context values
    app.jinja_env.globals['file_exists'] = os.path.exists
    app.jinja_env.globals['first_paragraph'] = first_paragraph
    app.jinja_env.globals['ngettext'] = ngettext
    app.jinja_env.globals['html_to_text'] = html_to_text
    app.jinja_env.globals['csrf_token'] = generate_csrf
    app.jinja_env.globals['debug_checkpoint'] = debug_checkpoint
    app.jinja_env.globals['compaction_level'] = compaction_level
    app.jinja_env.globals['humanize_number'] = humanize_number
    app.jinja_env.globals['round_invisible_digits'] = round_invisible_digits
    app.jinja_env.globals['localize_datetime'] = localize_datetime
    app.jinja_env.globals['display_back_button'] = display_back_button
    app.jinja_env.globals['favorite_communities'] = favorite_communities
    app.jinja_env.globals["get_event_start"] = get_event_start
    app.jinja_env.globals['reply_filter_keyword'] = reply_filter_keyword
    app.jinja_env.globals['user_banned_from_community'] = user_banned_from_community  # D995
    app.jinja_env.globals['viewer_can_view'] = viewer_can_view
    app.jinja_env.globals['quote_state'] = quote_state
    app.jinja_env.globals['shown'] = shown
    app.jinja_env.globals['switch_url'] = switch_url
    before_render_template.connect(prime_quotes, app)  # C2: one lookup per table per rendered page
    app.jinja_env.globals['post_community_link'] = post_community_link
    app.jinja_env.globals['joined_server_views'] = joined_server_views
    app.jinja_env.globals['hosted_instance_ids'] = hosted_instance_ids   # stored on one of our domains
    app.jinja_env.globals['user_local_only'] = user_local_only
    app.jinja_env.filters['community_links'] = community_link_to_href
    app.jinja_env.filters['feed_links'] = feed_link_to_href
    app.jinja_env.filters['person_links'] = person_link_to_href
    app.jinja_env.filters['shorten'] = shorten_string
    app.jinja_env.filters['shorten_url'] = shorten_url
    app.jinja_env.filters['storefront_link'] = storefront_link   # a stored URL of ours, shown on this storefront
    app.jinja_env.filters['remove_images'] = remove_images
    app.jinja_env.filters["human_filesize"] = human_filesize

    app.before_request(resolve_storefront)   # first: an unregistered host is refused before anything else runs
    app.before_request(require_onion_session)   # spec 3.7a C: a cookieless onion request is sent round for a cookie
    app.before_request(resolve_onion_api_rate_key)   # spec 3.7: onion API limits per account, found before the view
    # Registered before after_request below, so it runs after it (Flask runs them in reverse) and before Flask-Compress
    app.after_request(rewrite_response_for_storefront)

    @app.before_request
    def cap_media_upload():
        """A13.2: a media upload is refused at its kind's limit, before the body is read past it. Werkzeug answers
        413 for a larger Content-Length and stops a chunked or lying body at the cap. Registered first, so nothing
        has read the stream under the global MAX_CONTENT_LENGTH yet.

        The cap is raised above the global limit only for a user who may upload media to the community the URL
        names; everyone else keeps the global limit (or the kind's, if that is lower). An unknown community keeps
        the global cap: add_post answers it with a 404."""
        if request.method != 'POST' or request.endpoint != 'community.add_post':
            return
        community = actor_to_community(request.view_args['actor'])
        if community is None:
            return
        # The untyped /submit, which the main "Create post" buttons link to, is the community's default type there
        kind = request.view_args.get('type') or community.default_post_type
        if kind not in (MEDIA_KIND_VIDEO, MEDIA_KIND_AUDIO):
            return
        limit = upload_byte_limit(kind) + MEDIA_UPLOAD_SLACK
        if not (current_user.is_authenticated and kind == media_kind_for(community)
                and can_upload_media(current_user, community)):
            limit = min(current_app.config['MAX_CONTENT_LENGTH'], limit)
        request.max_content_length = limit

    @app.before_request
    def before_request():
        # Handle CORS preflight requests for all routes
        if request.method == 'OPTIONS':
            response = flask.make_response('', 200)
            response.headers['Access-Control-Allow-Origin'] = current_app.config.get('CORS_ALLOW_ORIGIN', '*')
            response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, Accept, User-Agent'
            #response.headers['Vary'] = 'Origin'
            return response

        # Store nonce in g (g is per-request, unlike session)
        g.nonce = gibberish()
        g.locale = str(get_locale())
        g.low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'
        if request.path != '/inbox' and not request.path.startswith('/static/'):        # do not load g.site on shared inbox, to increase chance of duplicate detection working properly
            site = get_site_as_dict()
            g.site = Site(**site)
            g.admin_ids = get_setting('admin_ids')    # get_setting is cached in redis
            if g.admin_ids is None:
                g.admin_ids = list(db.session.execute(
                    text("""SELECT u.id FROM "user" u WHERE u.id = 1
                            UNION
                            SELECT u.id
                            FROM "user" u
                            JOIN user_role ur ON u.id = ur.user_id AND u.deleted = false AND u.banned = false
                            JOIN role r ON r.id = ur.role_id AND r.name = :role_admin_name
                            ORDER BY id"""),
                    {'role_admin_name': ROLE_ADMIN_NAME}).scalars())  # D481: by name, as is_admin()
                set_setting('admin_ids', g.admin_ids)
            refusal = signed_fetch_refusal()   # S6 secure mode: an unsigned ActivityPub fetch is refused
            if refusal is not None:
                return refusal

        if current_user.is_authenticated:
            stamp_activity(current_user)
        else:
            if 'Windows' in request.user_agent.string:
                current_user.font = 'inter'
            else:
                current_user.font = ''
            if session.get('Referer') is None and \
                    request.headers.get('Referer') is not None and \
                    not is_local_link(request.headers.get('Referer')):   # a Referer from off this site
                session['Referer'] = request.headers.get('Referer')

    @app.after_request
    def after_request(response):
        # First, before anything reads the storefront: this hook runs before rewrite_response_for_storefront
        adopt_host_storefront()
        # Add CORS headers to all responses
        response.headers['Access-Control-Allow-Origin'] = current_app.config.get('CORS_ALLOW_ORIGIN', '*')
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, Accept, User-Agent'

        secure_mode_cache_headers(response)   # S6 secure mode: a signed answer must not reach a shared cache

        # Here, not in the view: the limiter answers 429 before register() runs, and those POSTs count too
        if request.method == 'POST' and request.endpoint == 'auth.register' and fetched_by_hand():
            log_register_attempt()   # every POST counts, a signed-in one and an invalid form too
        if 400 <= response.status_code < 500 and PROBE_PATH_RE.match(request.path) and fetched_by_hand():
            log_probe_refused()

        # An ActivityPub answer's content type follows the Accept header (ap_media_type), so it varies on it
        ap_response = response.mimetype in AP_MEDIA_TYPES
        if ap_response:
            response.vary.add('Accept')
        # Don't set cookies for static resources or ActivityPub responses to make them cachable
        if request.path.startswith('/static/') or request.path.startswith('/bootstrap/static/') or ap_response:
            # Remove session cookies that mess up caching
            # Why the pragma below: this module already does
            # `from flask import session` at the top (module import time), so if
            # flask did not expose `session`, that import would fail and this
            # module -- and the app -- would never load. The false side of this
            # condition is therefore provably unreachable, not merely unlikely.
            if 'session' in dir(flask):  # pragma: no branch
                session.modified = False
            # Cache headers for static resources
            # Skip manifest.json as it varies by User-Agent and has its own cache headers
            if (request.path.startswith('/static/') or request.path.startswith('/bootstrap/static/')) and '/manifest.json' not in request.path:
                response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'  # 1 year
                # Set Vary to only Accept-Encoding for static files to allow upstream proxy caching
                # Static files don't vary by cookie or language, only by Accept-Encoding for compression
                if 'Vary' in response.headers:
                    vary = response.headers['Vary']
                    vary_parts = [v.strip() for v in vary.split(',')]
                    # Only keep Accept-Encoding, remove everything else (Cookie, Accept-Language, etc.)
                    vary_parts = [v for v in vary_parts if v.lower() == 'accept-encoding']
                    if vary_parts:
                        response.headers['Vary'] = ', '.join(vary_parts)
                    else:
                        del response.headers['Vary']
        else:
            if not current_app.config['ALLOW_AI_CRAWLERS']:
                response.headers.add('Link', f'<{storefront_url()}/rsl.xml>; rel="license"; type="application/rsl+xml"')
            onion = current_storefront().is_onion   # every onion page has the policy, a registration page too
            if onion or 'auth/register' not in request.path:
                if "api/alpha/swagger" not in request.path:
                    # Don't set CSP header for htmx fragment requests - they use parent page's CSP
                    is_htmx = request.headers.get('HX-Request') == 'true'
                    if not is_htmx and response.status_code != 304:
                        nonce = g.get('nonce')
                        # setdefault: a view's own policy (the onion media proxy's sandbox) stands
                        if onion:
                            response.headers.setdefault('Content-Security-Policy', ONION_POLICY.format(nonce=nonce)
                                                        if nonce else ONION_POLICY_WITHOUT_NONCE)
                        elif nonce and current_user.is_authenticated:
                            response.headers.setdefault('Content-Security-Policy', SCRIPT_POLICY.format(nonce=nonce))
                if current_app.config['HTTP_PROTOCOL'] == 'https':
                    response.headers['Strict-Transport-Security'] = 'max-age=63072000; includeSubDomains; preload'
                response.headers['X-Content-Type-Options'] = 'nosniff'
                if '/embed' not in request.path:
                    response.headers['X-Frame-Options'] = 'DENY'

        # API responses must never be cached by proxies
        if request.path.startswith('/api/'):
            response.headers.setdefault('Cache-Control', 'no-store')

        # Caching headers for html pages - pages are automatically translated and should not be cached while logged in.
        if response.content_type.startswith('text/html'):
            if current_user.is_authenticated or request.path.startswith('/auth/') or "api/alpha/swagger" in request.path:
                response.headers.setdefault(
                    'Cache-Control',
                    'no-store, no-cache, must-revalidate, private'
                )
            else:
                # Don't let flask set a session cookie for logged out users
                flask.session.modified = False
            # Merge rather than setdefault: views like return_304() set their own Vary, and Flask-Compress
            # appends Accept-Encoding after this runs. Overwriting either way would break nginx's proxy_cache.
            response.vary.update(('Accept-Language', 'Cookie'))
        return response

    @app.teardown_appcontext
    def shutdown_session(exception=None):
        if exception:
            db.session.rollback()
        db.session.remove()
