"""The domains this site answers on (spec 2026-10-17 §3.1).

`SERVER_NAME` stays the canonical federation domain. With an empty `site_domain` table it is the one
implicit default, so installs that never ran the migration's seed still work.
"""
from __future__ import annotations

from typing import NamedTuple

from flask import current_app
from sqlalchemy.exc import SQLAlchemyError

from app import cache, db
from app.models import SiteDomain


class DomainInfo(NamedTuple):
    domain: str
    scheme: str
    federates: bool
    registration: str
    is_default: bool
    is_onion: bool


_REGISTRATION_FROM_MODE = {'Open': 'anyone', 'RequireApplication': 'anyone', 'Closed': 'admin'}


def registration_from_mode(mode: str) -> str:
    """Map `site.registration_mode` to a `site_domain.registration` value. Unknown modes are closed."""
    return _REGISTRATION_FROM_MODE.get(mode, 'admin')


def normalise_host(host: str) -> str:
    """Lower-case, port stripped, trailing dot stripped."""
    return host.lower().partition(':')[0].rstrip('.')


def _synthesised_default() -> DomainInfo:
    protocol = current_app.config['HTTP_PROTOCOL']
    scheme = 'https' if protocol in ('https', 'mixed') else protocol
    domain = current_app.config['SERVER_NAME']
    return DomainInfo(domain, scheme, True, 'anyone', True, domain.endswith('.onion'))


@cache.memoize(timeout=300)
def domain_rows() -> tuple[DomainInfo, ...]:
    rows = db.session.query(SiteDomain).order_by(SiteDomain.id).all()
    if not rows:
        return (_synthesised_default(),)
    return tuple(DomainInfo(r.domain, r.scheme, r.federates, r.registration, r.is_default, r.is_onion)
                 for r in rows)


def clear_domain_cache() -> None:
    cache.delete_memoized(domain_rows)


def domain_for_host(host: str) -> DomainInfo | None:
    wanted = normalise_host(host)
    return next((row for row in domain_rows() if row.domain == wanted), None)


def default_domain() -> DomainInfo:
    return next((row for row in domain_rows() if row.is_default), _synthesised_default())


def is_registered_host(host: str) -> bool:
    return domain_for_host(host) is not None


def check_default_domain() -> None:
    """Log an error when the default row is not SERVER_NAME. Quiet when the table does not exist yet
    (CLI commands and `flask db upgrade` run this before the migration has made it)."""
    try:
        default = default_domain()
    except SQLAlchemyError:
        db.session.rollback()
        return
    if not any(row.is_default for row in domain_rows()):
        current_app.logger.error('site_domain has rows but none is the default; using SERVER_NAME')
    elif default.domain != current_app.config['SERVER_NAME']:
        current_app.logger.error('site_domain default %s does not match SERVER_NAME %s',
                                 default.domain, current_app.config['SERVER_NAME'])
