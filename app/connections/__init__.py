"""Connected accounts (spec docs/superpowers/specs/2026-11-13-connected-accounts-design.md).

A member links their account to accounts on other services, and their posts are published there too, reshaped for
each. This package holds what every service shares: the Service interface and its registry, which services are on,
sealed tokens (tokens.py), which posts go where (eligibility.py) and the sending, editing and deleting of copies
(dispatch.py). Each service is a module that registers one Service subclass.
"""
from dataclasses import dataclass
from datetime import datetime

from flask import current_app

from app.sealing import keyring
from app.utils import get_setting

KEY = 'CONNECTIONS_SECRETS_KEY'


class ServiceError(Exception):
    """A call to a service failed. `message` is shown to the member: it never carries a token."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message[:255]


class Transient(ServiceError):
    """Worth trying again later (a 429, a 5xx, a timeout); `retry_after` is the service's own wish, in seconds."""

    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class Permanent(ServiceError):
    """The service refused this for good (a 4xx that is not about the token)."""


class AuthFailed(ServiceError):
    """The tokens no longer work: the member has to connect again."""


class QuotaWait(ServiceError):
    """An allowance is spent (YouTube's daily uploads, spec §8): wait until `until`, which costs no attempt."""

    def __init__(self, message: str, until: datetime):
        super().__init__(message)
        self.until = until


@dataclass
class Sent:
    remote_id: str
    remote_url: str | None = None


@dataclass
class Account:
    """An account a member chose at the service's sign-in, ready to be stored as a SocialConnection."""
    remote_id: str
    remote_name: str
    tokens: dict
    variant: str | None = None


class Service:
    """One service. A subclass sets the names and implements the calls; the defaults are what most services need."""
    name = ''                 # SocialConnection.service
    label = ''                # shown to members
    variants: tuple = ()      # ways to connect (C6); () means one way
    hint_label = ''           # when set, the Connect button has a text box for it (Bluesky's handle or server)
    takes_nsfw = False        # NSFW/NSFL posts go here (spec §5 default: Bluesky only, with self-labels)
    can_edit = False          # copies are edited when the post is (C4)

    def configured(self) -> bool:
        """The admin's app credentials for this service are set."""
        raise NotImplementedError

    def fits(self, post) -> bool:
        """The post has the shape this service takes (spec §1 table)."""
        raise NotImplementedError

    def fits_type(self, post_type: int) -> bool:
        """A post of this type might fit: whether the post form offers this service at all."""
        return True

    def can_delete(self, variant: str | None) -> bool:
        return True

    def variant_label(self, variant: str | None) -> str:
        return self.label

    def authorize_url(self, variant: str | None, state: str, redirect_uri: str, pending: dict, hint: str = '') -> str:
        """Where the Connect button sends the member. `pending` is kept (sealed) for the callback; `hint` is what the
        member typed in the box `hint_label` asks for."""
        raise NotImplementedError

    def finish(self, variant: str | None, args: dict, redirect_uri: str, pending: dict) -> list:
        """The callback's query `args` into the accounts the member may connect (one or more)."""
        raise NotImplementedError

    def expired(self, tokens: dict) -> bool:
        expires_at = tokens.get('expires_at')
        return expires_at is not None and datetime.now().timestamp() >= expires_at - 60

    def refresh(self, tokens: dict) -> dict:
        """New tokens for expired ones. Raises AuthFailed when the service will not refresh them."""
        raise AuthFailed('This connection has expired.')

    def send(self, post, tokens: dict, connection) -> Sent:
        raise NotImplementedError

    def edit(self, post, tokens: dict, remote_id: str) -> None:
        raise Permanent('Copies on this service cannot be edited.')

    def delete(self, tokens: dict, remote_id: str) -> None:
        raise NotImplementedError

    def revoke(self, tokens: dict) -> None:
        """End the tokens at the service, if it can. Best effort: a failure here never keeps a member connected."""


SERVICES: dict[str, Service] = {}


def register(service: Service) -> Service:
    SERVICES[service.name] = service
    return service


def setting_name(service_name: str) -> str:
    return f'connections_enabled_{service_name}'


def sealing_available() -> bool:
    return keyring(KEY) is not None


def enabled(service: Service | None) -> bool:
    """Members may connect it and posts go to it: the admin switched it on, its credentials and the token key are
    set. A service switched off keeps its connections but sends nothing (spec §3)."""
    return (service is not None and sealing_available() and service.configured()
            and bool(get_setting(setting_name(service.name), False)))


def enabled_services() -> list[Service]:
    return [service for service in SERVICES.values() if enabled(service)]


def service_label(service_name: str, variant: str | None = None) -> str:
    service = SERVICES.get(service_name)
    return service.variant_label(variant) if service is not None else service_name


def connections_offered(user) -> bool:
    """The settings menu shows Connected accounts: a service is on, or the member has a connection to show."""
    if enabled_services():
        return True
    return bool(user.is_authenticated and user.social_connections)


def main_domain() -> str:
    """The instance's main public domain: every connect and callback runs there (spec §3)."""
    return current_app.config['SERVER_NAME']


def main_base_url() -> str:
    """The main domain as the services know it: the address each service's redirect is registered under."""
    scheme = 'http' if current_app.config.get('HTTP_PROTOCOL') == 'http' else 'https'
    return f'{scheme}://{main_domain()}'
