"""Which app a service call uses (spec §18, rulings C8-C12).

An app is the service's developer app: a Meta app (`facebook`, which also serves Instagram through Facebook), an
Instagram Login app (`instagram`), a Google client (`youtube`) or a Bluesky key (`bluesky`). A domain may have its
own, sealed in `connection_app`; otherwise the instance's from the environment (§3) is used. Each call runs in one
domain's context (`for_domain`): a member's home domain at Connect (C10), the domain recorded on a connection
afterwards, and the services read their app through `current(kind)`.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from flask import current_app

from app import db
from app.connections import KEY, SERVICES, Service, sealing_available, setting_name
from app.models import ConnectionApp
from app.sealing import SealUnavailable, seal_with, unseal_with
from app.utils import get_setting, set_setting

KINDS = ('facebook', 'instagram', 'youtube', 'bluesky')
# the environment's names for the instance's apps: (client id, secret field: environment name)
INSTANCE = {
    'facebook': ('FACEBOOK_APP_ID', {'client_secret': 'FACEBOOK_APP_SECRET'}),
    'instagram': ('INSTAGRAM_APP_ID', {'client_secret': 'INSTAGRAM_APP_SECRET'}),
    'youtube': ('YOUTUBE_OAUTH_CLIENT_ID', {'client_secret': 'YOUTUBE_OAUTH_SECRET'}),
    'bluesky': (None, {'key': 'BLUESKY_OAUTH_KEY'}),
}
SWITCHES = ('on', 'off')   # a domain's own setting for a service; none means it follows the instance (C12)

_domain: ContextVar = ContextVar('connections_domain', default=None)


@dataclass(frozen=True)
class App:
    kind: str
    client_id: str
    secret: dict = field(default_factory=dict)
    domain_id: int | None = None   # None: the instance's app

    def get(self, name: str, default=None):
        return self.secret.get(name, default)


def instance_app(kind: str) -> App | None:
    if kind not in INSTANCE:   # a service with no developer app of these kinds
        return None
    client_name, secrets = INSTANCE[kind]
    values = {name: current_app.config.get(env) or '' for name, env in secrets.items()}
    client_id = current_app.config.get(client_name) or '' if client_name else 'instance'
    if not client_id or not all(values.values()):
        return None
    return App(kind, client_id, values, None)


def domain_app(kind: str, domain_id: int | None) -> App | None:
    if domain_id is None:
        return None
    row = ConnectionApp.query.filter_by(site_domain_id=domain_id, kind=kind).first()
    if row is None:
        return None
    try:
        secret = unseal_with(KEY, row.sealed_secret)
    except SealUnavailable:
        return None
    return App(kind, row.client_id, secret, domain_id)


def app_for(kind: str, domain_id: int | None) -> App | None:
    """The domain's own app, else the instance's (C8)."""
    return domain_app(kind, domain_id) or instance_app(kind)


@contextmanager
def for_domain(domain_id: int | None):
    """Run service calls as `domain_id`'s: its app, its addresses."""
    token = _domain.set(domain_id)
    try:
        yield
    finally:
        _domain.reset(token)


def context_domain() -> int | None:
    return _domain.get()


def current(kind: str) -> App | None:
    return app_for(kind, _domain.get())


def save_domain_app(domain_id: int, kind: str, client_id: str, secret: dict) -> bool:
    """Store a domain's own app. Whether it is a different app from the one its connections were made with (a new
    client id, C11). The caller commits."""
    row = ConnectionApp.query.filter_by(site_domain_id=domain_id, kind=kind).first()
    changed = row is not None and row.client_id != client_id
    if row is None:
        row = ConnectionApp(site_domain_id=domain_id, kind=kind)
        db.session.add(row)
    row.client_id = client_id
    row.sealed_secret = seal_with(KEY, secret)
    return changed


def remove_domain_app(domain_id: int, kind: str) -> bool:
    """Forget a domain's own app. Whether there was one. The caller commits."""
    row = ConnectionApp.query.filter_by(site_domain_id=domain_id, kind=kind).first()
    if row is not None:
        db.session.delete(row)
        db.session.flush()
    return row is not None


def switch_name(service: str, domain_id: int) -> str:
    return f'connections_switch_{service}_{domain_id}'


def domain_switch(service: str, domain_id: int | None) -> str | None:
    """A domain's own 'on' or 'off' for a service, or None to follow the instance (C12)."""
    if domain_id is None:
        return None
    value = get_setting(switch_name(service, domain_id), None)
    return value if value in SWITCHES else None


def set_domain_switch(service: str, domain_id: int, value: str | None) -> None:
    set_setting(switch_name(service, domain_id), value if value in SWITCHES else None)


def enabled(service: Service | None, domain_id: int | None = None) -> bool:
    """Members of `domain_id` may connect it and their posts go to it (C12): the token key is set, an app is there
    for the domain (its own or the instance's), and the domain's switch is on, or follows the instance's, which is
    on. A service switched off keeps its connections but sends nothing (spec §3)."""
    if service is None or not sealing_available():
        return False
    switch = domain_switch(service.name, domain_id)
    if switch == 'off':
        return False
    with for_domain(domain_id):
        if not service.configured():
            return False
    return switch == 'on' or bool(get_setting(setting_name(service.name), False))


def enabled_services(domain_id: int | None = None) -> list[Service]:
    return [service for service in SERVICES.values() if enabled(service, domain_id)]


def connections_offered(user) -> bool:
    """The settings menu shows Connected accounts: a service is on for the member's domain, or they have a
    connection to show."""
    if not user.is_authenticated:
        return False
    return bool(enabled_services(user.site_domain_id) or user.social_connections)
