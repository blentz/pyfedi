"""Which domain an /admin settings page edits (spec 2026-11-20-multitenant-domain-config-design.md, MT4).

Each domain is a tenant with a configuration of its own. A settings page edits one tenant: the domain picker in the
admin navigation chooses it, and the choice is the one the list pages' picker keeps (app.admin.scope), so a domain
picked on one page is the domain every page is for. A list's "All" is, on a settings page, the first domain.

A field a sister domain leaves blank is the first domain's (MT3): the page shows the first domain's value beside it.
"""
from __future__ import annotations

from typing import Callable, NamedTuple

from flask import g, request, session

from app import db
from app.admin.scope import SESSION_KEY, domain_choices
from app.domain_config import effective, own_config
from app.models import DomainConfig, SiteDomain, first_config
from app.storefront import DomainInfo, default_domain, domain_rows


class Tenant(NamedTuple):
    domain: DomainInfo                 # the domain the page edits
    choices: list[tuple[int, str]]     # the domains the picker offers: those the user may edit here
    first_id: int                      # the first domain's picker value

    @property
    def is_first(self) -> bool:
        return self.domain.is_default

    @property
    def id(self) -> int:
        """The picker's value for this domain: its row's id, 0 for a first domain no row names yet."""
        return self.domain.id or 0

    def stored(self) -> DomainConfig | None:
        """What this domain has set itself: the first domain's row, or a sister's own row (None when it has set
        nothing)."""
        return first_config() if self.is_first else own_config(self.domain.id)

    def target(self) -> DomainConfig:
        """The row a save writes to, made for a sister that has none yet."""
        return first_config() if self.is_first else db.session.get(SiteDomain, self.domain.id).config()

    def inherited(self) -> dict:
        """What a blank field shows here: the first domain's values; nothing on the first domain itself."""
        return {} if self.is_first else effective(default_domain())


def tenant_for(may_edit: Callable[[DomainInfo], bool]) -> Tenant | None:
    """The tenant a settings page edits, of the domains `may_edit` allows, or None when the user may edit none.

    A page shown (GET) is for the domain `?domain=<id>` picks, which is kept for the other pages; otherwise for the one
    picked before, else the first domain, else the first the user may edit.

    A save (POST) writes the domain its form was shown for, which the form names (a hidden `domain` field, else the
    address it was posted to; neither, the first domain), never the session: another tab, or a link from anywhere, may
    have picked another domain since, and the save would otherwise land on that one. A save for a domain the user may
    not edit is refused (None), never written to another; it never changes the pick.

    The picker offers a local-only domain only on itself (follow-up F6), as the list pickers do; one picked by its id
    (the Domains page, which names every domain to those who manage them, links to each) is offered beside them."""
    rows = [row for row in domain_rows() if may_edit(row)]
    if not rows:
        return None
    if request.method == 'POST':
        named = request.form.get('domain', type=int)
        named = request.args.get('domain', 0, type=int) if named is None else named
        domain = next((row for row in rows if row.id == named), None) if named else \
            next((row for row in rows if row.is_default), None)
        if domain is None:
            return None
    else:
        if 'domain' in request.args:
            session[SESSION_KEY] = request.args.get('domain', 0, type=int)
        picked = session.get(SESSION_KEY) or None
        domain = next((row for row in rows if picked is not None and row.id == picked), None) or \
            next((row for row in rows if row.is_default), rows[0])
    offered = {domain_id for domain_id, _domain in domain_choices(None)}
    choices = [(row.id or 0, row.domain) for row in rows if row.id is None or row.id in offered or row == domain]
    tenant = Tenant(domain, choices, default_domain().id or 0)
    g.admin_tenant = tenant   # the picker in admin/_tabbed_nav.html
    return tenant
