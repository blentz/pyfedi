"""Which domain's feeds a reader's lists show (per-domain feeds, spec 2026-10-26-per-domain-feeds goal 2).

A list shows the feeds of one domain, the viewer's, and every remote feed. The rules are the community lists' own: a
domain's rows are `on_domain`, and a local-only domain's rows are shown only where `hidden_here` allows, so an onion
feed is never listed off the onion, whoever is looking (anti-goal 2).
"""
from flask_login import current_user
from sqlalchemy import and_, func, not_, or_, true

from app import db
from app.local_only import community_local_only, hidden_here, hidden_here_clause, hides_anything_here, hides_nothing, \
    without_hidden_here
from app.models import Community, Feed, FeedItem, User
from app.storefront import DomainInfo, current_storefront, default_domain, on_domain


def _owns_public_feed(domain: DomainInfo, signed_in: bool) -> bool:
    """Whether `domain` owns a public, unbanned local feed this reader may see (`hidden_here`). Not cached: a feed
    made, banned or made private must change the answer at once, and the answer differs by reader."""
    return Feed.query.filter(Feed.ap_id.is_(None), Feed.public.is_(True), Feed.banned.is_(False),
                             on_domain(domain, Feed), not_(hidden_here_clause(Feed, signed_in))).first() is not None


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
    if not domain.federates and not _owns_public_feed(domain, account is not None):
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


def feed_shown_here(feed: Feed, signed_in: bool | None = None) -> bool:
    """Whether this storefront shows `feed` to its reader: `hidden_here`, the shared local-only rule, which hides a
    local feed with its home domain and with its owner (ruling a3: no feed-only copy of the rule). An onion feed is
    shown on the onion only, to a signed-in reader. `signed_in` as in `hidden_here`."""
    return not hidden_here(feed, signed_in)


def feeds_shown_here_clause(signed_in: bool | None = None):
    """`feed_shown_here` as a filter on Feed."""
    return not_(hidden_here_clause(Feed, signed_in))


def feed_local_only(feed: Feed) -> bool:
    """True for a local feed that does not exist to other servers: its home domain does not federate (an onion feed),
    or its owner is a local-only account (a legacy row filed under the first domain, ruling N-m8). It is the shared
    rule's answer for a reader who is not signed in, which every storefront gives alike: the C2 rule for accounts and
    communities, so the federation rule and the display rule share their domain test."""
    return hidden_here(feed, signed_in=False)


def federating_feed_clause():
    """`feed_local_only` as a filter on Feed: keeps the feeds other servers may see. A plain true, with no subquery,
    while every domain federates."""
    clause = hidden_here_clause(Feed, signed_in=False)
    return true() if hides_nothing(clause) else not_(clause)


def shown_feed_communities(query, signed_in: bool | None = None):
    """`query` over Community (or over rows joined to it) without the communities this storefront hides from its reader,
    `hidden_here` as a filter (ruling a3: no feed-only copy of the rule). A clearnet feed that holds an onion community
    never shows it off the onion. `signed_in` as in `hidden_here`."""
    return without_hidden_here(query, Community, signed_in)


def community_shown_here(community: Community, signed_in: bool | None = None) -> bool:
    """`shown_feed_communities` for one row."""
    return not hidden_here(community, signed_in)


def shown_community_count(feed: Feed, signed_in: bool | None = None) -> int:
    """`feed.num_communities` without the communities this storefront hides from its reader, so a count never tells that
    one is there (`signed_in` as in `hidden_here`). The stored count when nothing is hidden here (no query)."""
    if not hides_anything_here():
        return feed.num_communities or 0
    held = db.session.query(func.count(FeedItem.id)).filter(FeedItem.feed_id == feed.id)
    shown = shown_feed_communities(held.join(Community, Community.id == FeedItem.community_id), signed_in)
    return max(0, (feed.num_communities or 0) - (held.scalar() - shown.scalar()))


def feed_may_join(community: Community, member: User) -> bool:
    """Whether a feed may join `member` to `community` for them. Never a local-only community outside the member's home
    domain: the membership would tie that account to a domain it does not use (an onion community and a clearnet
    account, or the reverse). A worker has no storefront, so it asks the domains and not `hidden_here`."""
    return not community_local_only(community) or community.home_domain().id == member.home_domain().id
