"""One gate in front of every view on a storefront that requires login to browse (spec M11, plan S1-S3).

A logged-out request is refused unless its endpoint is listed here. Denying by default is the point: a decorator on each
route left all of /api/alpha open to anybody on a private instance, because nothing told a route that was meant to be
open from one that had been forgotten. A new route is private until somebody lists it.

An ActivityPub Accept header opens nothing outside the federation endpoints: anybody can send that header.
"""
from flask import current_app, jsonify, request
from flask_login import current_user

from app.storefront import current_storefront
from app.utils import authorise_api_user, is_activitypub_request, login_redirect, login_required_here

# Open on every storefront: what somebody needs in order to join, log in or read what they'd be joining, and what isn't a
# reader at all (a provider's webhook, a health check, a signed media URL, an inbox's signed POST).
OPEN = frozenset({
    'static', 'bootstrap.static', 'onion_media.onion_media',
    'auth.login', 'auth.logout', 'auth.register', 'auth.resend_email', 'auth.reset_password_request', 'auth.reset_password',
    'auth.verify_email', 'auth.check_email', 'auth.please_wait', 'auth.not_trustworthy', 'auth.onion_wait',
    'auth.validation_required', 'auth.permission_denied', 'auth.passkey_options', 'auth.passkey_verification',
    'auth.google_login', 'auth.google_authorize', 'auth.mastodon_login', 'auth.mastodon_authorize', 'auth.discord_login',
    'auth.discord_authorize', 'auth.onboarding_instance_chooser',
    'main.login', 'main.about_page', 'main.privacy', 'main.robots', 'main.security', 'main.rsl', 'main.health', 'main.health2',
    'main.honey_pot', 'main.content_warning', 'main.anoobis', 'main.bot_challenge_result', 'main.service_worker',
    'main.static_manifest', 'main.receive_webhook',
    'main.index_rss',   # refuses a logged-out reader itself, with the 404 a feed reader expects
    'user.user_newsletter_unsubscribe', 'user.user_email_notifs_unsubscribe',   # an emailed link, with its own token
    'payments.payment_webhook', 'payments.stripe_webhook', 'payments.result', 'payments.result_before_this_release',
    'activitypub.shared_inbox',
    # What an app needs to show a login form, and to log in
    'Site.get_alpha_site', 'Site.get_alpha_site_version', 'Site.get_alpha_site_instance_chooser',
    'Site.get_alpha_site_instance_chooser_search', 'User.post_alpha_user_login', 'User.post_alpha_user_register',
    'User.get_alpha_user_get_captcha', 'User.post_alpha_user_verify_credentials',
})

# Open where the storefront federates, as other servers read them with no account. Each view that also serves a browser
# has its own check for that. A domain that doesn't federate has nothing to say to another server.
FEDERATION_BLUEPRINT = 'activitypub'
FEDERATION = frozenset({'main.instance_actor', 'main.instance_actor_outbox', 'main.site_outbox', 'main.jpeg_rendition'})
INDEX = 'main.index'   # the home page, which answers ActivityPub with the instance actor


def _federation_endpoint() -> bool:
    return request.endpoint in FEDERATION or request.endpoint.split('.')[0] == FEDERATION_BLUEPRINT \
        or (request.endpoint == INDEX and is_activitypub_request())


def _has_own_gate() -> bool:
    """A feed answers 404, and a member's reader gets in with its token, which the view's own decorator decides."""
    return getattr(current_app.view_functions[request.endpoint], 'has_own_login_gate', False)


def _api_caller_logged_in() -> bool:
    try:
        authorise_api_user(request.headers.get('Authorization'))
    except Exception:   # the one exception authorise_api_user raises for every way a token can be wrong
        return False
    return True


def refuse_logged_out() -> object | None:
    """before_request. None lets the request through. No endpoint means no route: the 404 handler is also what serves a
    CMS page, which an admin wrote to be read."""
    if request.endpoint is None or request.endpoint in OPEN:
        return None
    if current_storefront().federates and _federation_endpoint():
        return None
    if not login_required_here() or current_user.is_authenticated or _has_own_gate():
        return None
    if request.path.startswith('/api/'):
        return None if _api_caller_logged_in() else (jsonify(error='incorrect_login'), 401)
    if request.headers.get('HX-Request'):   # htmx swaps a page into a menu on a redirect, and nothing on a 401
        return '', 401
    return login_redirect()
