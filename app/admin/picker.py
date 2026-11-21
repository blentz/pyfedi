"""The domain picker every /admin page with the admin tabs shows (spec 2026-11-20-multitenant-domain-config-design.md,
MT4; RB8).

One choice, kept in the session, says which domain the admin is working on, and every page follows it:

- a settings page (its route asked `app.admin.tenant.tenant_for`) edits the picked domain;
- a list (its route asked `app.admin.scope.scope_for`) shows the picked domain's rows, or every domain's for "All";
- any other page holds instance-wide settings, which apply to every domain: it says so and keeps the pick for the
  pages that follow it.
"""
from __future__ import annotations

from typing import NamedTuple

from flask import g, request, session
from flask_babel import _
from flask_login import current_user

from app.admin import bp
from app.admin.scope import SESSION_KEY, domain_choices
from app.permissions import user_access
from app.storefront import default_domain, domain_rows, registration_mode_of

SETTINGS, LIST, INSTANCE = 'settings', 'list', 'instance'


class Picker(NamedTuple):
    kind: str                          # SETTINGS, LIST or INSTANCE
    choices: list[tuple[int, str]]     # the domains offered: (picker value, domain)
    selected: int                      # the picked domain's value; 0 for "All"
    first_id: int                      # the first domain's value, labelled as the first domain
    all_label: str | None              # the "All" option's label; None where a page is for one domain
    only: str | None = None            # with one domain to offer: the domain the page says it is for, if it says

    @property
    def offered(self) -> bool:
        """Whether there is a choice to make: more than one domain."""
        return len(self.choices) > 1


@bp.before_request
def keep_the_pick():
    """`?domain=<id>` on any admin page shown picks that domain for every page (0: all of them). A save never changes
    the pick: it writes the domain its own form names (app.admin.tenant.tenant_for). What kind of page this is, its
    route says afresh (tenant_for, scope_for), never a page drawn before it in the same application context."""
    g.pop('admin_tenant', None)
    g.pop('admin_scope', None)
    if request.method == 'GET' and 'domain' in request.args:
        session[SESSION_KEY] = request.args.get('domain', 0, type=int)


@bp.app_template_global('admin_picker')
def admin_picker() -> Picker | None:
    """The picker for the page being drawn (admin/_domain_picker.html draws it when it has a choice to offer), or None
    on an instance-wide page the user may not change instance settings on: scoped holders work on lists and settings
    pages, which say which of their domains they are for."""
    first_id = default_domain().id or 0
    tenant = g.get('admin_tenant')
    if tenant is not None:
        only = tenant.domain.domain if not tenant.is_first else None   # a sister's page says whose it is
        return Picker(SETTINGS, tenant.choices, tenant.id, first_id, None, only)
    scope = g.get('admin_scope')
    if scope is not None:
        only = scope.choices[0][1] if len(scope.choices) == 1 and not scope.unfiltered else None   # a holder's one domain
        return Picker(LIST, scope.choices, scope.picked or 0, first_id, _('All') if scope.unfiltered else _('All of mine'),
                      only)
    if not user_access('change instance settings', current_user.get_id()):
        return None
    choices = domain_choices(None)
    picked = session.get(SESSION_KEY) or 0
    return Picker(INSTANCE, choices, picked if picked in {domain_id for domain_id, _domain in choices} else 0,
                  first_id, _('All'))


@bp.app_template_global('admin_registration_modes')
def admin_registration_modes() -> set[str]:
    """The registration modes of the domain the picker has picked, or of every domain when it has picked none: what
    the admin navigation offers registration applications and referrals for."""
    picked = session.get(SESSION_KEY) or None
    rows = domain_rows()
    chosen = [row for row in rows if picked is not None and row.id == picked] or rows
    return {registration_mode_of(row) for row in chosen}
