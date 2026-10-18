"""Session and remember-me cookies per storefront (onion storefront spec 2026-10-17, task 7).

The `Secure` flag is decided by the scheme of the storefront the request was addressed to: https storefronts get it,
a plain-http onion does not (Tor carries the traffic end to end encrypted, and a Secure cookie would not be stored over
http). `SESSION_COOKIE_SECURE=0` / `REMEMBER_COOKIE_SECURE=0` still turn it off everywhere. The scheme is the
storefront's configured one (`g.storefront`), never a request header or port. Cookies carry no `Domain`, so each
storefront's cookie is host-only. App config is shared between threads, so nothing here writes to it.

This module imports nothing from `app`, so `app/__init__.py` can build the login manager from it.
"""
from flask import current_app, g, has_request_context
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


class StorefrontLoginManager(LoginManager):
    def _set_cookie(self, response):
        configured = current_app.config.get('REMEMBER_COOKIE_SECURE', COOKIE_SECURE)
        super()._set_cookie(_SecureResponse(response, cookie_secure(configured)))
