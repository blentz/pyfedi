"""Session and remember-me cookies per storefront (onion storefront spec 2026-10-17, task 7).

The `Secure` flag is decided by the scheme of the storefront the request was addressed to: https storefronts get it,
a plain-http onion does not (Tor carries the traffic end to end encrypted, and a Secure cookie would not be stored over
http). `SESSION_COOKIE_SECURE=0` (which also feeds `REMEMBER_COOKIE_SECURE`) still turns it off everywhere. The scheme is the
storefront's configured one (`g.storefront`), never a request header or port. Cookies carry no `Domain`, so each
storefront's cookie is host-only. App config is shared between threads, so nothing here writes to it.

This module imports nothing from `app`, so `app/__init__.py` can build the login manager from it.
"""
from flask import Response, current_app, g, has_request_context
from flask.sessions import SecureCookieSessionInterface
from flask_login import LoginManager
from flask_login.config import COOKIE_SECURE


def cookie_secure(configured) -> bool:
    """`configured`, unless the current request's storefront is plain http. Outside a request, or before a storefront
    is resolved, `configured` alone."""
    if has_request_context() and 'storefront' in g:
        return bool(configured) and g.storefront.scheme == 'https'
    return bool(configured)


class StorefrontSessionInterface(SecureCookieSessionInterface):
    def get_cookie_secure(self, app) -> bool:
        return cookie_secure(super().get_cookie_secure(app))


class _SecureResponse:
    """The one method Flask-Login calls on its response (`set_cookie`), always with one `secure` value, whatever the caller passed."""

    def __init__(self, response, secure: bool):
        self._response = response
        self._secure = secure

    def set_cookie(self, *args, **kwargs):
        return self._response.set_cookie(*args, **{**kwargs, 'secure': self._secure})


def secure_set_cookies(response: Response) -> Response:
    """`after_request`: every `Set-Cookie` of the response gets `Secure` when `cookie_secure` says so and loses it
    when not. This covers the ad-hoc `response.set_cookie` calls (preferences, low_bandwidth, ...) in one place. Only
    the `Secure` attribute changes: names, values, `Domain`, `Path`, `SameSite` and order are kept, deleted cookies
    included."""
    headers = response.headers.getlist('Set-Cookie')
    if not headers:
        return response
    secure = cookie_secure(current_app.config['SESSION_COOKIE_SECURE'])
    response.headers.remove('Set-Cookie')
    for header in headers:
        attributes = [part for part in header.split('; ') if part.lower() != 'secure']
        response.headers.add('Set-Cookie', '; '.join(attributes + ['Secure'] if secure else attributes))
    return response


class StorefrontLoginManager(LoginManager):
    # Overrides Flask-Login's private `_set_cookie` (flask-login==0.6.3, pinned in requirements.txt). That version
    # reads REMEMBER_COOKIE_SECURE from app.config and passes it to `response.set_cookie(secure=...)`; the only way to
    # decide it per storefront without writing the shared config is to wrap the response. Re-check this on upgrade.
    def _set_cookie(self, response):
        configured = current_app.config.get('REMEMBER_COOKIE_SECURE', COOKIE_SECURE)
        super()._set_cookie(_SecureResponse(response, cookie_secure(configured)))
