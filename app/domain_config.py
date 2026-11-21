"""Each domain's configuration: the site is multitenant, each domain it answers on a tenant (spec
2026-11-20-multitenant-domain-config-design.md).

A domain's `DomainConfig` row holds what it sets. The first domain's row (the one with no `site_domain_id`) holds every value; a sister
domain's NULL, or '' in a text column, is the first domain's value (MT3). `effective` resolves that, once per domain,
and `site_view` lays it over `Site`'s instance-wide columns as the request's `g.site`, so code reading
`g.site.<name>` reads the value of the domain the request is on.
"""
from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.orm import Session

from app import cache, db, shaped_memo_name
import app.models as models

# A logo's sized copies: a domain with a logo of its own shows only its own (branding spec §5, how a blank logo falls
# back), a blank size being PieFed's default, never the first domain's icon beside the domain's own logo
ICON_COLUMNS = frozenset({'logo_180', 'logo_152', 'logo_32', 'logo_16'})
# A domain with a logo or a dark logo of its own never shows the first domain's dark logo: a blank one is its own logo
DARK_COLUMNS = frozenset({'logo_dark', 'logo_dark_152'})


def own_config(site_domain_id: int | None) -> models.DomainConfig | None:
    """The row sister domain `site_domain_id` stores its own values in, or None: for the first domain, which has no
    sister's row, as for a sister that has set nothing yet. Not memoised: admin forms and federation read it."""
    row = db.session.get(models.SiteDomain, site_domain_id) if site_domain_id is not None else None
    return None if row is None or row.is_default else row.own_config


def _inherits(value) -> bool:
    return value is None or value == ''


def resolve(row: models.DomainConfig | None, first: models.DomainConfig) -> dict:
    """Sister row `row`'s values over `first`'s (MT3); `first`'s own when `row` is None. Its logo's sized copies and its
    dark logo are its own alone once it has a logo (or dark logo) of its own."""
    own_logo = row is not None and not _inherits(row.logo)
    own_dark = own_logo or (row is not None and not _inherits(row.logo_dark))

    def value(name):
        if (own_logo and name in ICON_COLUMNS) or (own_dark and name in DARK_COLUMNS):
            return getattr(row, name) or ''
        mine = getattr(row, name) if row is not None else None
        return getattr(first, name) if _inherits(mine) else mine
    return {name: value(name) for name in models.TENANT_COLUMNS}


@cache.memoize(timeout=300, make_name=shaped_memo_name)
def _effective(site_domain_id: int | None, is_default: bool) -> dict:
    first = models.first_config()
    return resolve(None if is_default else own_config(site_domain_id), first)


def effective(domain) -> dict:
    """What domain `domain` (a DomainInfo or a SiteDomain row) is configured with: every TENANT_COLUMNS value,
    inherited ones filled in. Memoised per domain; `storefront.clear_domain_cache` clears it."""
    return _effective(domain.id, domain.is_default)


def clear() -> None:
    cache.delete_memoized(_effective)


def here(name: str):
    """`name` as the domain the request is on has it; outside a request (a Celery task), as the first domain has it.
    For code that reads a setting about what the user in front of it does or sees, and may run without `g.site`."""
    import app.storefront as storefront   # cycle: storefront imports this module
    return effective(storefront.current_storefront())[name]


def every_domains(name: str) -> list:
    """`name` as each domain has it: the first domain's, then each sister's."""
    import app.storefront as storefront   # cycle: storefront imports this module
    return [getattr(models.first_config(), name),
            *(effective(row)[name] for row in storefront.domain_rows() if not row.is_default)]


def any_domain(name: str) -> bool:
    """The storage rule (spec MT2): content that switch `name` lets a domain show (NSFW, NSFL) is stored when any
    domain shows it. Each domain then shows it, or hides it, by its own switch."""
    return any(bool(value) for value in every_domains(name))


def every_domain(name: str) -> bool:
    """The storage rule for a filter that drops incoming content (a gif reply, a "this" comment, a chan image): it is
    dropped only when every domain has the filter on, as a domain without it would show what was dropped."""
    return all(bool(value) for value in every_domains(name))


class SiteView:
    """The request's `g.site`: `Site`'s instance-wide columns with the request's domain's configuration over them, read
    by the names `Site` had. A plain object, never in the session: writing to it changes this request alone."""

    def __init__(self, instance: dict, tenant: dict):
        self.__dict__.update(instance)
        self.__dict__.update(tenant)

    def __getattr__(self, name):
        """Site's methods (its admins, its activity counts): looked up when called, as app.models imports this module
        before it defines Site. None of them reads a column."""
        if name in _SITE_METHODS:
            method = getattr(models.Site, name)
            return method if name in _STATIC_METHODS else method.__get__(self)
        raise AttributeError(name)


_STATIC_METHODS = frozenset({'admins', 'listed_role_holders'})
_SITE_METHODS = _STATIC_METHODS | frozenset({
    'active_now', 'active_daily', 'active_weekly', 'active_monthly', 'active_6monthly', 'all_active_6monthly',
    'all_active_monthly', 'all_active_weekly', 'all_active_daily'})


def site_view(instance: dict, domain) -> SiteView:
    """`g.site` for a request on `domain`: `instance`, `Site`'s columns as `get_site_as_dict` gives them, with
    `domain`'s configuration over them."""
    return SiteView(instance, effective(domain))


_CHANGED = 'domain_config_changed'


@event.listens_for(Session, 'after_flush')
def _note_a_change(session, flush_context):
    """A flush that writes a domain's configuration or a domain row: its memoised values go when it commits."""
    if any(isinstance(row, (models.DomainConfig, models.SiteDomain))
           for row in (*session.new, *session.dirty, *session.deleted)):
        session.info[_CHANGED] = True


@event.listens_for(Session, 'after_commit')
def _clear_after_a_change(session):
    if session.info.pop(_CHANGED, False):
        import app.storefront as storefront   # cycle: storefront imports this module
        storefront.clear_domain_cache()


@event.listens_for(Session, 'after_rollback')
def _forget_a_change(session):
    session.info.pop(_CHANGED, None)
