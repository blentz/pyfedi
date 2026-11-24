"""Connected accounts (spec docs/superpowers/specs/2026-11-13-connected-accounts-design.md).

A member links their account to accounts on other services, and their posts are published there too, reshaped for
each. This package holds what every service shares: the Service interface and its registry, which services are on,
sealed tokens (tokens.py), which posts go where (eligibility.py) and the sending, editing and deleting of copies
(dispatch.py). Each service is a module that registers one Service subclass.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from flask import current_app

from app.sealing import keyring
from app.utils import is_local_microblogs

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


class MaybeSent(Transient):
    """The request went out and no answer came back (a read timeout, a dropped connection, the call's deadline):
    the service may have acted on it (spec 19.2). Worth trying again where doing it twice does no harm."""


class Unconfirmed(ServiceError):
    """A copy's publishing call went out and no answer came back, and the service has no way to make it safe to
    send again (spec 19.2): it may be there. Never retried by itself; the author checks and may retry."""


@contextmanager
def publishing(label: str):
    """Around the one call that makes a copy public on a service that cannot repeat it safely (Facebook, Instagram):
    a MaybeSent from it becomes Unconfirmed."""
    try:
        yield
    except MaybeSent:
        raise Unconfirmed(f'{label} may have posted it: no answer came back. Check before trying again.') from None


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


class Progress:
    """What a send has done so far, kept on its row across attempts (spec 19.2): Bluesky's record key, YouTube's
    upload address. `save()` stores `data` at once, before the call it guards, so a worker that dies mid-call
    leaves it for the next attempt."""

    def __init__(self, data: dict | None = None, save=None):
        self.data = dict(data or {})
        self._save = save

    def save(self) -> None:
        if self._save is not None:
            self._save(dict(self.data))


@dataclass
class Pending:
    """The service is still working on the copy (an Instagram container, spec 19.4): `data` is what `resume` needs to
    carry on, kept in the row's progress until the sweep asks again."""
    data: dict


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
    default_on = True         # a new connection is ticked on the post form until the member says otherwise (C3)
    ticks_microblog = False   # ticked on every local microblog post whatever default_on says (ATProto spec A5)

    def configured(self) -> bool:
        """The admin's app credentials for this service are set."""
        raise NotImplementedError

    def fits(self, post) -> bool:
        """The post has the shape this service takes (spec §1 table)."""
        raise NotImplementedError

    def fits_type(self, post_type: int) -> bool:
        """A post of this type might fit: whether the post form offers this service at all."""
        return True

    def ticked(self, connection, community) -> bool:
        """Whether the post form ticks `connection` for a new post in `community` (and an API post that does not
        choose goes there)."""
        if self.ticks_microblog and community is not None:
            if is_local_microblogs(community):
                return True
        return connection.default_on

    def can_delete(self, variant: str | None) -> bool:
        return True

    def kind_of(self, variant: str | None) -> str:
        """Which app (app.connections.apps.KINDS) this service's connections of `variant` run under."""
        return self.name

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

    def send(self, post, tokens: dict, connection, progress: Progress | None = None) -> 'Sent | Pending':
        """Make the copy, or start it and answer Pending when the service finishes it later (spec 19.4). `progress`
        carries what earlier attempts at this copy did (spec 19.2)."""
        raise NotImplementedError

    def resume(self, post, tokens: dict, connection, progress: Progress) -> 'Sent | Pending':
        """Carry on with a copy `send` answered Pending for: ask how the service is getting on, finish it once ready."""
        raise Permanent('This service finishes its copies at once.')

    def edit(self, post, tokens: dict, remote_id: str) -> None:
        raise Permanent('Copies on this service cannot be edited.')

    def delete(self, tokens: dict, remote_id: str) -> None:
        raise NotImplementedError

    def revoke(self, tokens: dict) -> None:
        """End the tokens at the service, if it can. Best effort: a failure here never keeps a member connected."""

    def paused_until(self, connection) -> datetime | None:
        """Until when work for this connection waits, because the service's allowance for its app or account is
        nearly spent (spec 18.3); None to go ahead."""
        return None


SERVICES: dict[str, Service] = {}


def register(service: Service) -> Service:
    SERVICES[service.name] = service
    return service


def setting_name(service_name: str) -> str:
    return f'connections_enabled_{service_name}'


def sealing_available() -> bool:
    return keyring(KEY) is not None


def service_label(service_name: str, variant: str | None = None) -> str:
    service = SERVICES.get(service_name)
    return service.variant_label(variant) if service is not None else service_name


def main_domain() -> str:
    """The instance's main public domain: every connect and callback runs there (spec §3)."""
    return current_app.config['SERVER_NAME']


def main_base_url() -> str:
    """The main domain as the services know it: the address each service's redirect is registered under."""
    scheme = 'http' if current_app.config.get('HTTP_PROTOCOL') == 'http' else 'https'
    return f'{scheme}://{main_domain()}'
