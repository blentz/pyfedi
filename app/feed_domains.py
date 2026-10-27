"""Which domain's feeds a reader's lists show (per-domain feeds, spec 2026-10-26-per-domain-feeds goal 2).

A list shows the feeds of one domain, the viewer's, and every remote feed. The rules are the community lists' own: a
domain's rows are `on_domain`, and a local-only domain's rows are shown only where `hidden_here` allows, so an onion
feed is never listed off the onion, whoever is looking (anti-goal 2).
"""
from flask_login import current_user
from sqlalchemy import and_, not_, or_

from app.local_only import hidden_here, hidden_here_clause
from app.models import Feed
from app.storefront import DomainInfo, current_storefront, default_domain, on_domain


def _owns_public_feed(domain: DomainInfo) -> bool:
    """Whether `domain` owns a public, unbanned local feed."""
    return Feed.query.filter(Feed.ap_id.is_(None), Feed.public.is_(True), Feed.banned.is_(False),
                             on_domain(domain, Feed)).first() is not None


def feed_view_domain(account=None) -> DomainInfo:
    """The domain whose feeds a viewer's lists show. `account` is the API's token user; else the signed-in local user;
    else none. That is the account's home domain, or the storefront when there is no account. An account homed on a
    local-only domain other than the storefront browses as logged out, so it sees the storefront's feeds. A local-only
    domain that owns no public feed shows the default domain's."""
    here = current_storefront()
    if account is None and current_user.is_authenticated:
        account = current_user
    domain = here
    if account is not None:
        home = account.home_domain()
        if home.federates or home.id == here.id:
            domain = home
    if not domain.federates and not _owns_public_feed(domain):
        return default_domain()
    return domain


def feeds_on_domain_clause(domain: DomainInfo):
    """The feeds a list on `domain` offers: every remote feed, and the local ones `domain` owns. It does not depend on
    the reader, so a list cached per domain may use it; `listed_feeds_clause` adds the reader's part."""
    return or_(Feed.ap_id.isnot(None), and_(Feed.ap_id.is_(None), on_domain(domain, Feed)))


def listed_feeds_clause(domain: DomainInfo, signed_in: bool | None = None):
    """`feeds_on_domain_clause` without the feeds this storefront hides from its reader (`hidden_here`; `signed_in`
    as there)."""
    return and_(feeds_on_domain_clause(domain), not_(hidden_here_clause(Feed, signed_in)))


def feed_listed(feed: Feed, domain: DomainInfo, signed_in: bool | None = None) -> bool:
    """`listed_feeds_clause(domain)` for one row."""
    if feed.ap_id is None and feed.home_domain().id != domain.id:
        return False
    return not hidden_here(feed, signed_in)
