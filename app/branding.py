"""What each domain looks like: its name, description, logos, theme, CSS, sidebar and about text (project 4).

A domain's `DomainBranding` row holds only what it sets. Every field it leaves NULL or blank is the first domain's,
which is `Site` row 1's; the first domain itself is exactly `Site`'s values.
"""
from __future__ import annotations

from typing import NamedTuple

from app import cache, db, shaped_memo_name
# The modules, not the names: app.storefront imports this module to clear its memo, so whichever of the two is imported
# first finds the other mid-import (import cycle: storefront)
import app.models as models
import app.storefront as storefront


class Branding(NamedTuple):
    name: str
    description: str
    logo: str
    logo_180: str
    logo_152: str
    logo_32: str
    logo_16: str
    default_theme: str
    additional_css: str
    sidebar_html: str
    about_html: str


def own_branding(domain: storefront.DomainInfo) -> models.DomainBranding | None:
    """The row `domain` stores its own values in, or None. Not memoised: the admin form and federation read it."""
    return db.session.get(models.DomainBranding, domain.id)


@cache.memoize(timeout=300, make_name=shaped_memo_name)
def _effective(site_domain_id: int | None, is_default: bool) -> Branding:
    site = db.session.get(models.Site, 1)
    row = None if is_default else db.session.get(models.DomainBranding, site_domain_id)
    return Branding(*(getattr(row, field, None) or getattr(site, field) or '' for field in Branding._fields))


def branding_for(domain: storefront.DomainInfo) -> Branding:
    """The values `domain` shows: its own where it set one, else Site row 1's. Memoised per domain id."""
    return _effective(domain.id, domain.is_default)


def current_branding() -> Branding:
    return branding_for(storefront.current_storefront())
