"""What an /admin list shows a holder whose grant is on some domains only (spec 2026-11-03-rbac-design.md, What scoped
holders see; RB8).

A list route is opened with `@permission_held_anywhere(permission)` and filters its query with `scope_for(permission)`,
so a holder on domain X sees X's rows and nobody sees an unfiltered list they could not act on. The domain picker
narrows the list to one of the holder's domains; the choice is kept in the session, so paging keeps it.
"""
from typing import NamedTuple

from flask import g, request, session
from flask_login import current_user
from sqlalchemy import and_, or_, select, true

from app.models import Community, Report, User
from app.permissions import REPORT_ON_USER, domain_clause, domains_for
from app.storefront import domain_rows

SESSION_KEY = 'admin_domain'


class Scope(NamedTuple):
    domains: frozenset[int] | None   # what the list is filtered to: None for everything
    choices: list[tuple[int, str]]   # the domains the picker offers: every one the holder may see
    picked: int | None                # the one picked, or None for all of them

    def clause(self, column):
        """`column`, a `site_domain_id`, within this scope."""
        return domain_clause(column, self.domains)

    @property
    def unfiltered(self) -> bool:
        return self.domains is None

    @property
    def held(self) -> bool:
        """Whether the permission is held anywhere at all: on the whole instance, or on some domain."""
        return self.domains is None or bool(self.domains)

    def community_clause(self, community_id_column):
        """A row whose `community_id_column` names a community homed within this scope."""
        if self.unfiltered:
            return true()
        return community_id_column.in_(select(Community.id).where(self.clause(Community.site_domain_id)))

    def user_clause(self, user_id_column):
        """A row whose `user_id_column` names an account homed within this scope."""
        if self.unfiltered:
            return true()
        return user_id_column.in_(select(User.id).where(self.clause(User.site_domain_id)))

    def report_clause(self):
        """A report about something within this scope: an account's, by where it is homed; anything else's, by the
        community it is in (app.permissions.home_domain_id)."""
        if self.unfiltered:
            return true()
        return or_(and_(Report.type == REPORT_ON_USER, self.user_clause(Report.suspect_user_id)),
                   and_(Report.type != REPORT_ON_USER, self.community_clause(Report.in_community_id)))


def scope_for(permission: str) -> Scope:
    """The current user's scope for `permission`, narrowed by the picker. `?domain=<id>` picks one domain and
    `?domain=0` picks them all again; a domain the holder may not see is ignored."""
    held = domains_for(permission, current_user.get_id())
    choices = [(row.id, row.domain) for row in domain_rows() if row.id is not None and (held is None or row.id in held)]
    if 'domain' in request.args:
        session[SESSION_KEY] = request.args.get('domain', 0, type=int)
    picked = session.get(SESSION_KEY) or None
    if picked not in {domain_id for domain_id, _ in choices}:
        picked = None
    scope = Scope(frozenset({picked}) if picked else held, choices, picked)
    g.admin_scope = scope   # the picker in admin/_tabbed_nav.html
    return scope
