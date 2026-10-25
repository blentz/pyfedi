"""The domains this site answers on (spec 2026-10-17 §3.1).

`SERVER_NAME` stays the canonical federation domain. With an empty `site_domain` table it is the one
implicit default, so installs that never ran the migration's seed still work.
"""
from __future__ import annotations

import hashlib
import ipaddress
import math
import re
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from typing import NamedTuple
from urllib.parse import unquote, unquote_to_bytes, urlsplit, urlunsplit

import httpx
import idna
from bs4 import BeautifulSoup
from bs4.dammit import EntitySubstitution
from bs4.formatter import HTMLFormatter
from flask import Response, current_app, g, has_app_context, has_request_context, request
from flask.globals import request_ctx
from flask_babel import _
from flask_login import current_user
from sqlakeyset import InvalidPage
from sqlalchemy import and_, case, desc, func, or_, tuple_
from sqlakeyset.results import unserialize_bookmark
from sqlalchemy.exc import SQLAlchemyError

from app import cache, db, shaped_memo_name
from app.onion import onion_proxy_address
from app.onion_media import MEDIA_SUFFIXES, proxy_url
# The module, not the name: app.models imports this module for File's media URLs, so whichever of the two is
# imported first finds the other mid-import (import cycle: models)
import app.models as models
# The module: it imports this one for DomainInfo (import cycle: branding)
import app.branding as branding


class DomainInfo(NamedTuple):
    domain: str
    scheme: str
    federates: bool
    registration: str
    is_default: bool
    is_onion: bool
    id: int | None   # the site_domain row; None only for the synthesised default
    instance_id: int   # the domain's instance row; DEFAULT_INSTANCE_ID for the default (spec 2026-10-19 §3.1)


# Made by `flask init-db` as the local instance, row 1 is the default domain's. Each other domain has a row of its own.
DEFAULT_INSTANCE_ID = 1


_REGISTRATION_FROM_MODE = {'Open': 'anyone', 'RequireApplication': 'anyone', 'Closed': 'admin'}


def registration_from_mode(mode: str) -> str:
    """Map `site.registration_mode` to a `site_domain.registration` value. Unknown modes are closed."""
    return _REGISTRATION_FROM_MODE.get(mode, 'admin')


def live_registration(storefront: DomainInfo, site) -> str:
    """Who may create `storefront`'s accounts, as a `site_domain.registration` value: `site.registration_mode`'s for
    the default domain (its own column is ignored, as in `public_registration_open`), the row's own for any other."""
    return registration_from_mode(site.registration_mode) if storefront.is_default else storefront.registration


def public_registration_open(storefront: DomainInfo) -> bool:
    """Whether anyone may sign up on `storefront`. The default domain is governed by `Site.registration_mode` alone
    (its own `registration` column is ignored); any other domain takes public sign-ups only when it is 'anyone', and
    then `Site.registration_mode` applies as well. A 'staff' or 'admin' domain's accounts are made by the admin
    add-user form alone."""
    return storefront.is_default or storefront.registration == 'anyone'


def registration_mode_on(storefront: DomainInfo, site) -> str:
    """`site.registration_mode` as `storefront` offers it: 'Closed' where public sign-up is not open
    (`public_registration_open`). The default domain's is the Site row's own."""
    return site.registration_mode if public_registration_open(storefront) else 'Closed'


_NOT_IN_AUTHORITY = frozenset('@/\\?#')


def _bare_host(authority: str) -> str:
    """The host of an authority (`host`, `host:port`, `[v6]:port`), lower-cased, brackets and trailing dot stripped;
    '' when it names none. A bare IPv6 literal (`::1`, as `site_domain.domain` holds it) is its own host."""
    authority = authority.strip()
    # urlsplit would read `x@host` as userinfo and drop a tab or newline from it: a value with either, a path or
    # query delimiter, or any other whitespace or control character is not an authority this site can be named by
    if any(c in _NOT_IN_AUTHORITY or c.isspace() or not c.isprintable() for c in authority):
        return ''
    if authority.count(':') > 1 and not authority.startswith('['):
        authority = f'[{authority}]'
    try:
        return (urlsplit(f'//{authority}').hostname or '').rstrip('.')
    except ValueError:   # an unbalanced IPv6 bracket
        return ''


def normalise_host(host: str) -> str:
    """Lower-case, port stripped, trailing dot stripped, IPv6 brackets stripped. The form `site_domain.domain` holds."""
    return _bare_host(host)


def _authority(storefront) -> str:
    """`host[:port]` for building URLs on `storefront`. It comes from configuration only, never from the request:
    the Host header's port would let a client put its own port into absolute URLs. SERVER_NAME's host uses
    SERVER_NAME verbatim (a dev install on 127.0.0.1:5000 keeps its port); any other registered domain is its bare
    host."""
    server_name = current_app.config.get('SERVER_NAME') or ''
    if storefront.domain == _bare_host(server_name):
        return server_name
    return f'[{storefront.domain}]' if ':' in storefront.domain else storefront.domain


def _synthesised_default() -> DomainInfo:
    protocol = current_app.config['HTTP_PROTOCOL']
    scheme = 'https' if protocol in ('https', 'mixed') else protocol
    domain = normalise_host(current_app.config['SERVER_NAME'])
    return DomainInfo(domain, scheme, True, 'anyone', True, domain.endswith('.onion'), None, DEFAULT_INSTANCE_ID)


@cache.memoize(timeout=300, make_name=shaped_memo_name)
def domain_rows() -> tuple[DomainInfo, ...]:
    rows = db.session.query(models.SiteDomain).order_by(models.SiteDomain.id).all()
    if not rows:
        return (_synthesised_default(),)
    return tuple(DomainInfo(r.domain, r.scheme, r.federates, r.registration, r.is_default, r.is_onion, r.id,
                            r.instance_id) for r in rows)


def clear_domain_cache() -> None:
    cache.delete_memoized(domain_rows)
    cache.delete_memoized(branding._effective)
    _hosted_destinations.cache_clear()


def domain_for_host(host: str) -> DomainInfo | None:
    """The row serving `host`, or None. SERVER_NAME's host is always served: when no row names it (a seed made under
    another SERVER_NAME, or SERVER_NAME changed since) it is the synthesised default, so the canonical site and its
    inbox never answer 421. `check_default_domain` logs that mismatch at startup."""
    wanted = normalise_host(host)
    found = next((row for row in domain_rows() if normalise_host(row.domain) == wanted), None)
    if found is None and wanted == _synthesised_default().domain:
        return _synthesised_default()
    return found


def default_domain() -> DomainInfo:
    return next((row for row in domain_rows() if row.is_default), _synthesised_default())


def domain_by_id(site_domain_id: int | None) -> DomainInfo:
    """The row with this id; the default domain for None or an id no row has (a row since removed)."""
    return next((row for row in domain_rows() if row.id == site_domain_id), None) or default_domain()


def instance_id_of(domain: DomainInfo) -> int:
    """The instance row `domain`'s accounts, communities and content belong to."""
    return domain.instance_id


def hosted_instance_ids() -> frozenset[int]:
    """Every hosted domain's instance row: content on any of them is stored here, whichever storefront shows it.
    Read from `domain_rows`, so `clear_domain_cache` clears it too."""
    return frozenset(instance_id_of(row) for row in domain_rows())


# What a post (or community) on the current domain's own instance row, or on a sister domain's, adds to its hot
# ranking, and the tier it takes in a list's last sort key. One unit of `Post.ranking` is about 12.5 hours or a
# tenfold score (owner ruling, plan C2 task 7).
OWN_DOMAIN_BOOST = 1.0
SISTER_DOMAIN_BOOST = 0.5
KEYSET_SLACK = 1e-9   # a rounding margin on a keyset cursor translated by a tier's boost
# How deep a hot feed is read in the boosted order: the web feed's window. Past it a list by page number falls back
# to the plain order (owner ruling, plan C2 final wave): an OFFSET walk past it cannot read every tier that deep.
BOOST_WINDOW = 1000


def sister_instance_ids() -> frozenset[int]:
    """The other hosted domains' instance rows: every hosted row but the current storefront's."""
    return hosted_instance_ids() - {current_instance_id()}


def domain_tiers() -> list[tuple[float, tuple[int, ...] | None]]:
    """The boost tiers of a hot feed as (boost, instance rows): the remote rows at 0 (instance rows None: every row
    but a hosted one), then the current domain's own at OWN_DOMAIN_BOOST, then the sister domains' at
    SISTER_DOMAIN_BOOST when there are any. Every row is in exactly one, which is how `boosted_top_sql` and
    `tiered_post_ids` bound the rows they read; the boost a row is ranked by is `domain_boost_sql`'s."""
    tiers = [(0.0, None), (OWN_DOMAIN_BOOST, (current_instance_id(),))]
    if sisters := sister_instance_ids():
        tiers.append((SISTER_DOMAIN_BOOST, tuple(sorted(sisters))))
    return tiers


def domain_boost_sql(alias: str, column: str = 'instance_id') -> tuple[str, dict]:
    """A raw SQL number for rows of the table aliased `alias`: OWN_DOMAIN_BOOST for those on the current domain's
    instance row, SISTER_DOMAIN_BOOST for a sister domain's, else 0 (a NULL instance too). With no sisters the
    sister arm is left out, as an empty `IN ()` is not valid SQL. Returns the expression and the parameters to bind;
    `domain_boost_expr` is its twin for a SQLAlchemy query."""
    sql = f'CASE WHEN {alias}.{column} = :own_instance THEN {OWN_DOMAIN_BOOST} '
    params = {'own_instance': current_instance_id()}
    if sisters := sister_instance_ids():
        sql += f'WHEN {alias}.{column} IN :sister_instances THEN {SISTER_DOMAIN_BOOST} '
        params['sister_instances'] = tuple(sorted(sisters))
    return sql + 'ELSE 0 END', params


def boosted_top_sql(columns: str, outer_columns: str, from_where: str, rank: str, tail: tuple[str, ...], limit: int,
                    lead: tuple[str, ...] = ()) -> tuple[str, dict]:
    """The first `limit` posts of `from_where` (which ends in its WHERE clause, over `post` as p) in the order
    `lead DESC, p.<rank> + boost DESC, tail DESC`, the boost being `domain_boost_sql`'s. Returns the SQL and the
    parameters to bind (the caller binds its own as well). `columns` is what each row selects and `outer_columns` what
    the answer returns of them.

    Ordering the whole table by `ranking + CASE ...` scores every row; ordering it by `ranking` walks
    ix_post_feed_hot and stops. So this reads each tier's first `limit` rows by plain `ranking`, in the order the index
    holds them, and sorts only that union (at most three times `limit`) by the boosted value. It returns what the
    naive boosted order would. A boost is never negative, so a post's boosted value is at least its ranking. A post
    that is outside the first `limit` of its tier by ranking has `limit` posts of that tier above it, ranked above it
    boosted as well, since the boost is a constant within a tier: so it is outside the first `limit` of the union, and
    so of everything. Each row is in one tier (the first arm is the remote rows), so the first `limit` of the union
    by the boosted value is the first `limit` of all rows by it.
    Each arm keeps the WHERE clause, so `deleted = false AND status > 0` still selects the partial index."""
    boost, params = domain_boost_sql('t', 'tier_instance_id')
    params['tier_limit'] = limit
    params['hosted_instances'] = tuple(sorted(hosted_instance_ids()))
    ordered = tuple(dict.fromkeys((*lead, rank, *tail)))
    select = f'SELECT {columns}, p.instance_id AS tier_instance_id, ' + ', '.join(f'p.{c} AS tier_{c}' for c in ordered)
    order = ', '.join(f'p.{c} DESC' for c in (*lead, rank, *tail))
    conditions = [' AND (p.instance_id IS NULL OR p.instance_id NOT IN :hosted_instances)',
                  ' AND p.instance_id = :own_instance']
    if 'sister_instances' in params:
        conditions.append(' AND p.instance_id IN :sister_instances')
    arms = [f'({select} {from_where}{condition} ORDER BY {order} LIMIT :tier_limit)' for condition in conditions]
    outer_order = ', '.join([*(f't.tier_{c} DESC' for c in lead), f't.tier_{rank} + {boost} DESC',
                             *(f't.tier_{c} DESC' for c in tail)])
    sql = f'SELECT {outer_columns} FROM ({" UNION ALL ".join(arms)}) AS t ORDER BY {outer_order} LIMIT :tier_limit'
    return sql, params


def tiered_post_ids(posts, rank, tail: tuple, limit: int, place: tuple | None = None, lead: tuple = ()) -> list[int]:
    """The ids `boosted_top_sql` selects, for a ORM query: the first `limit` rows of `posts` (filtered, and ordered by
    any sticky columns already) in the order `lead, rank + boost, tail, id`, found tier by tier by plain `rank`.
    `place` is a keyset cursor's forward place (sqlakeyset's: the value of every order column of the page's query, in
    that order) and `lead` the columns that come before the boosted value in it. Each tier reads on from the cursor
    in two cuts. The index range is `(lead, rank) <= (the cursor's lead, its rank - boost)`, widened a hair so that a
    float's rounding cannot drop a row: a row value, so that a row below the cursor on a lead column is in it whatever
    its rank (the index orders by the lead columns first, so it is still a range of it). The exact cut is
    `(lead, rank + boost, tail, id) < place`, which is what keeps the rows that tie the cursor on its rank apart, by
    the keys after it. The argument for why this is the naive order's first `limit` is `boosted_top_sql`'s."""
    ids = set()
    for boost, instance_ids in domain_tiers():
        arm = posts.filter(or_(models.Post.instance_id.is_(None),
                               models.Post.instance_id.not_in(hosted_instance_ids()))) \
            if instance_ids is None else posts.filter(models.Post.instance_id.in_(instance_ids))
        if place is not None:
            bound = place[len(lead)] - boost + KEYSET_SLACK
            arm = arm.filter(tuple_(*lead, rank) <= tuple_(*place[:len(lead)], bound) if lead else rank <= bound,
                             tuple_(*lead, rank + boost, *tail, models.Post.id) < tuple_(*place))
        arm = arm.order_by(desc(rank), *(desc(c) for c in tail), desc(models.Post.id))
        ids.update(row.id for row in arm.with_entities(models.Post.id).limit(limit))   # the ids only, not whole posts
    return list(ids)


def unboosted_order_by(rank, tail: tuple) -> list:
    """`boosted_order_by` without the boost: `rank`, then `tail`, then the id. The order of a page past BOOST_WINDOW,
    which the index reads straight to its OFFSET."""
    return [desc(rank), *(desc(c) for c in tail), desc(models.Post.id)]


def boosted_order_by(rank, tail: tuple, by_id: bool = True) -> list:
    """The ORDER BY of a hot or scaled feed on a SQLAlchemy query: `rank + boost`, then `tail`, then the id (which a
    keyset page needs, and a page of ids does not)."""
    return [desc(rank + domain_boost_expr(models.Post.instance_id)), *(desc(c) for c in tail),
            *([desc(models.Post.id)] if by_id else [])]


def check_place(place: tuple, columns: tuple) -> None:
    """Raise InvalidPage unless the keyset cursor `place` has one value of the right type for each of `columns` (the
    order columns of the page's query, in order): a number for a number, a timestamp for a timestamp. A cursor this
    server never handed out would otherwise reach the database as a comparison of mismatched types. A number for a
    float column must be a finite one a float holds: a huge int would overflow in Python or in Postgres, and NaN or
    infinity is no place a page ever ends at."""
    if len(place) != len(columns):
        raise InvalidPage('a cursor of another shape')
    for value, column in zip(place, columns):
        wanted = column.type.python_type
        if wanted is float:
            wanted = (int, float)
        if not isinstance(value, wanted) or (isinstance(value, bool) and wanted is not bool):
            raise InvalidPage('a cursor of other types')
        if wanted == (int, float) and not finite(value):
            raise InvalidPage('a cursor number no float holds')


def finite(number: int | float) -> bool:
    """Whether `number` is a finite float, or an int one converts to."""
    try:
        return math.isfinite(float(number))
    except OverflowError:
        return False


def boosted_posts(posts, rank, tail: tuple, limit: int, bookmark=None, lead: tuple = ()):
    """`posts` (a Post query, filtered and ordered by any sticky columns already) in the order of `boosted_order_by`,
    and cut to the rows `tiered_post_ids` finds: `limit` is how many rows the caller will show, one more than which
    is read to tell whether there is a next page. `bookmark` is the keyset cursor the caller will page by; a bookmark
    that pages backwards is not cut, and a malformed one is left to the caller's own keyset page to refuse. A cursor
    that parses but whose values are of the wrong shape or types raises InvalidPage, as the caller refuses a bad one."""
    place = None
    if bookmark:
        try:
            place, backwards = unserialize_bookmark(bookmark)
        except (ValueError, TypeError):
            return posts.order_by(*boosted_order_by(rank, tail))
        if place is not None:
            check_place(place, (*lead, rank, *tail, models.Post.id))
        if backwards:
            return posts.order_by(*boosted_order_by(rank, tail))
    ids = tiered_post_ids(posts, rank, tail, limit + 1, place, lead)
    return posts.filter(models.Post.id.in_(ids)).order_by(*boosted_order_by(rank, tail))


def domain_boost_expr(column):
    """`domain_boost_sql` for a SQLAlchemy query, `column` being the table's `instance_id`."""
    arms = [(column == current_instance_id(), OWN_DOMAIN_BOOST)]
    if sisters := sister_instance_ids():
        arms.append((column.in_(sorted(sisters)), SISTER_DOMAIN_BOOST))
    return case(*arms, else_=0)


def hidden_instance_ids() -> frozenset[int]:
    """The instance rows this storefront does not show: each local-only (`federates=False`) domain's, but for the
    storefront's own. Off its own domain an onion's row is never listed or shown, so nothing links it to the clearnet
    (owner ruling, plan C2 task 6). Outside a request the storefront is the default, so they are all hidden. For a
    query: `Instance.id.not_in(hidden_instance_ids())`."""
    here = current_instance_id()
    return frozenset(instance_id_of(row) for row in domain_rows() if not row.federates and instance_id_of(row) != here)


def normalised_host(name: str) -> str:
    """A host name as `hidden_host_names` compares it: lower-case, without trailing dots or a leading `www.`."""
    return name.lower().rstrip('.').removeprefix('www.')


def hidden_host_names() -> frozenset[str]:
    """The host names of the domains whose instance rows `hidden_instance_ids` hides, normalised: a link to one is
    not listed or shown off its own storefront either."""
    here = current_instance_id()
    return frozenset(normalised_host(row.domain) for row in domain_rows()
                     if not row.federates and instance_id_of(row) != here)


def host_hidden_here(name: str) -> bool:
    """Whether a link host `name` (a `Domain` row's) names a hidden domain: the host itself, or any name under it,
    since Tor ignores the subdomains of an onion address. Written in any case and with a trailing dot alike."""
    host = normalised_host(name)
    return any(host == hidden or host.endswith('.' + hidden) for hidden in hidden_host_names())


def host_not_hidden_clause(column):
    """`host_hidden_here` negated, as a filter on a column of host names (`Domain.name`); None when nothing is
    hidden, so a caller adds no condition at all."""
    hidden = hidden_host_names()
    if not hidden:
        return None
    name = func.lower(func.rtrim(func.coalesce(column, ''), '.'))
    escaped = (h.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') for h in sorted(hidden))
    return and_(*(~or_(name == host, name.like('%.' + pattern, escape='\\'))
                  for host, pattern in zip(sorted(hidden), escaped)))


def instance_visible_here(instance_id: int) -> bool:
    """Whether the current storefront shows the instance row `instance_id`: any row but another local-only domain's."""
    return instance_id not in hidden_instance_ids()


def on_domain(domain: DomainInfo, model=None):
    """A filter for the local rows of `model` (users by default) that belong to `domain`: its own, and on the default
    domain the ones no domain was stamped on."""
    column = (model or models.User).site_domain_id
    if domain.is_default:
        return or_(column == domain.id, column.is_(None))
    return column == domain.id


ON_DOMAIN_PARAM = 'storefront_site_domain_id'


def on_domain_sql(domain: DomainInfo, alias: str = 'c') -> str:
    """`on_domain` for raw SQL over a community (or user) table aliased `alias`; the caller binds
    `ON_DOMAIN_PARAM` to `domain.id`. It does not say the row is local: a remote row has no domain either, so the
    default domain's NULL arm matches it. Put the local test beside it. The synthesised default's id is None: its
    `= NULL` arm matches nothing and the NULL arm takes the unstamped rows."""
    column = f'{alias}.site_domain_id'
    if not domain.is_default:
        return f'{column} = :{ON_DOMAIN_PARAM}'
    return f'({column} = :{ON_DOMAIN_PARAM} OR {column} IS NULL)'


def domain_base_url(info: DomainInfo) -> str:
    """The origin that identifies accounts and communities owned by `info`, for ActivityPub URLs stored at creation.
    The default domain is SERVER_URL itself, so its identity stays byte-for-byte what it was before storefronts."""
    if info.is_default:
        return current_app.config['SERVER_URL']
    return f'{info.scheme}://{_authority(info)}'


def federating_base(info: DomainInfo) -> str:
    """The origin anything of `info`'s is named on where it is stored or federated: `domain_base_url(info)` for a
    domain that federates, the default domain's (SERVER_URL) for a local-only one. The onion's host is never stored:
    a page shows the stored URL on whichever host serves it (C1 I4, C2), as it did before plan C3."""
    return domain_base_url(info if info.federates or info.is_default else default_domain())


def domain_authority(info: DomainInfo) -> str:
    """`host[:port]` that names `info`'s owned rows as `ap_domain`; SERVER_NAME verbatim for the default domain."""
    return current_app.config['SERVER_NAME'] if info.is_default else _authority(info)


# The port a URL of each scheme names when it names none. A URL is ours only over http or https: a scheme-relative
# reference, javascript:, gopher: and the rest name no ActivityPub object.
_DEFAULT_PORTS = {'http': ':80', 'https': ':443'}


def _comparable(scheme: str, authority: str) -> str:
    """`authority` lower-cased, with the scheme's default port and a trailing dot on the host dropped, as
    `storefront.is_local_host` ignores them. Any other port stays: `host:8443` is not `host`."""
    authority = authority.lower()
    host, port = authority, ''
    if ':' in authority and not authority.endswith(']'):   # host:port; a bracketed IPv6 literal with no port ends in ]
        host, _, port = authority.rpartition(':')
        port = f':{port}'
    if port == _DEFAULT_PORTS[scheme]:
        port = ''
    return host.rstrip('.') + port


def domain_of_url(url: str | None) -> DomainInfo | None:
    """The hosted domain an http(s) `url` names, else None: its authority is that domain's, ignoring case, a trailing
    dot and the scheme's default port (as `storefront.is_local_host` does). Not a prefix or suffix match:
    shop.example.evil and evil.shop.example are not shop.example, and neither is a URL with userinfo, another port,
    or another scheme (a scheme-relative `//shop.example/` too). The default domain's authority is SERVER_NAME
    verbatim, its port included.

    Any hosted domain, a local-only one too (the onion). A caller deciding federation, an inbound "is this object
    ours" or an outbound id or signature, uses `federating_domain_of_url` or checks `.federates` itself."""
    try:
        parts = urlsplit(url or '')
    except ValueError:   # an unbalanced IPv6 bracket
        return None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not parts.netloc or '@' in parts.netloc:
        return None
    authority = _comparable(scheme, parts.netloc)
    return next((row for row in domain_rows() if _comparable(scheme, domain_authority(row)) == authority), None)


def _destination(url: str) -> tuple[str, int | None] | None:
    """The (host, port) httpx connects to for an http(s) `url`, as httpx itself parses it: userinfo dropped, an IDN
    host in punycode, lower case, no trailing dot, and None for the scheme's default port. None when httpx would send
    nothing (another scheme, no host, a URL it refuses)."""
    try:
        target = httpx.URL(url)
    except (httpx.InvalidURL, ValueError):   # a bad IDN label, an unbalanced bracket
        return None
    host = target.raw_host.decode('ascii', 'replace').rstrip('.').lower()
    if target.scheme not in _DEFAULT_PORTS or not host:
        return None
    return host, target.port


def is_hosted_uri(uri: str | None) -> bool:
    """Whether a request to `uri` would reach this server: the host httpx connects to is one we host, any SiteDomain
    row (local-only too) or SERVER_NAME. Nothing is posted to, or fetched over ActivityPub from, such a URI (plan C4):
    the activity or object is ours already.

    The egress test, so it asks where the request goes, not what the URL names: unlike `domain_of_url`, userinfo
    (`https://x@shop.example/`) does not hide our host, and an IDN domain matches in either spelling. A port matches a
    domain whose authority names that port; a domain naming none is every default port (80 or 443). A value that is
    not a string (a peer's malformed field) names nothing of ours: the caller's own validation refuses it."""
    destination = _destination(uri) if isinstance(uri, str) else None
    if destination is None:
        return False
    host, port = destination
    default = _synthesised_default()   # SERVER_NAME is always ours, as in domain_for_host
    for ours_host, ours_port in _hosted_destinations((*domain_rows(), default), domain_authority(default)):
        if ours_host == host and (port == ours_port or (ours_port is None and port in (80, 443))):
            return True
    return False


def is_hosted_server(server: str | None) -> bool:
    """Whether a server named as `host[:port]` (a netloc, or a handle's domain) is ours to the allowlist and ban list:
    the one rule those gates use (plan C4). Ours by both tests: `is_local_host` reads the authority as named, so
    userinfo or a path, query or fragment character is never ours, and `is_hosted_uri` adds the egress port rule, so
    a domain naming no port is ours with none, 80 or 443 and an explicit port only with itself. A port none of our
    domains names is not ours: the lists govern it as any remote server, so a handle cannot pick a port of our host."""
    return is_local_host(server) and is_hosted_uri(f'https://{server}')


@lru_cache(maxsize=8)
def _hosted_destinations(rows: tuple[DomainInfo, ...], default_authority: str) -> tuple[tuple[str, int | None], ...]:
    """`_destination` of each hosted domain in `rows`, SERVER_NAME's among them, parsed once per set of rows and
    default authority (`domain_authority` gives SERVER_NAME for the default domain, so it is in the key): the key is
    the rows themselves, so a changed row is a new entry, never a stale answer. clear_domain_cache clears it with
    domain_rows."""
    found = (_destination(f'{row.scheme}://{domain_authority(row)}') for row in rows)
    return tuple(destination for destination in found if destination is not None)


def federating_domain_of_url(url: str | None) -> DomainInfo | None:
    """`domain_of_url`'s domain when it federates, else None. What federation code asks: a local-only domain's URL is
    never an object a peer may name, nor one we send."""
    found = domain_of_url(url)
    return found if found is not None and found.federates else None


def is_federating_url(url) -> bool:
    """Whether `url` names something a federating domain of ours serves: the inbound "is it ours" test (plan C3).
    `federating_domain_of_url`'s domain, over that domain's own scheme (SERVER_URL's for the default): an object of
    ours is named on its domain's base URL, so `http://shop.example/...` is not ours when shop.example is https. Every
    federating domain's URL is, a local-only domain's (the onion) never is, nor is a lookalike host or any value that
    is not an http(s) URL string. (`domain_of_url` itself takes either scheme: a link pasted over http is still to
    one of our domains.)"""
    if not isinstance(url, str):
        return False
    domain = federating_domain_of_url(url)
    return domain is not None and urlsplit(url).scheme.lower() == urlsplit(domain_base_url(domain)).scheme


def is_registered_host(host: str) -> bool:
    return domain_for_host(host) is not None


def is_local_host(host: str | None) -> bool:
    """Whether `host` names this site: SERVER_NAME (the canonical host, ours even before a site_domain row names it)
    or any registered domain. Case, a port and a trailing dot are ignored.

    The one answer to "is this domain ours" (spec §3.4): a link pasted from the onion is as local as one from the
    clearnet. The canonical host is compared first, so the common case reads no rows."""
    bare = _bare_host(host or '')
    return bool(bare) and (bare == _bare_host(current_app.config.get('SERVER_NAME') or '') or is_registered_host(bare))


def is_served_name(name: str | None) -> bool:
    """Whether `name` is, exactly and ignoring case only, a name this site answers on: SERVER_NAME as configured
    (its port included) or a registered domain. Stricter than `is_local_host`, which also takes a port or a trailing
    dot: for an identifier a peer resolves, such as a WebFinger acct: domain, as before the storefronts."""
    name = (name or '').lower()
    return bool(name) and (name == (current_app.config.get('SERVER_NAME') or '').lower() or
                           (name == normalise_host(name) and is_registered_host(name)))


def _link_host(url) -> str:
    """The bare host of absolute (or scheme-relative) `url`, '' when it names none. The host is urlsplit's, after
    any userinfo. A browser reads `\\` as `/`, so a URL with a backslash has no host we can name."""
    if not isinstance(url, str) or '\\' in url:
        return ''
    try:
        return _bare_host(urlsplit(url).hostname or '')
    except ValueError:
        return ''


def is_local_link(url) -> bool:
    """Whether absolute (or scheme-relative) `url` points at this site: `is_local_host` of its host (`_link_host`).
    A URL with a backslash is not local."""
    return is_local_host(_link_host(url))


def check_default_domain() -> None:
    """Log an error when the default row is not SERVER_NAME, and a warning when an onion domain is registered while
    `ONION_PROXY_IP` is unset (or not one address): no request is then treated as onion, so onion visitors are
    banned, limited and recorded as the one shared tor address. Quiet when the table does not exist yet
    (CLI commands and `flask db upgrade` run this before the migration has made it)."""
    try:
        default = default_domain()
    except SQLAlchemyError:
        db.session.rollback()
        return
    if not any(row.is_default for row in domain_rows()):
        current_app.logger.error('site_domain has rows but none is the default; using SERVER_NAME')
    elif normalise_host(default.domain) != normalise_host(current_app.config['SERVER_NAME']):
        current_app.logger.error('site_domain default %s does not match SERVER_NAME %s',
                                 default.domain, current_app.config['SERVER_NAME'])
    if onion_proxy_address() is None and any(row.is_onion for row in domain_rows()):
        current_app.logger.warning('an onion domain is registered but ONION_PROXY_IP is unset or not one address; '
                                   'no request will be treated as onion')


def current_storefront() -> DomainInfo:
    """The storefront this request was addressed to; the default one outside a request."""
    return g.storefront if has_request_context() and 'storefront' in g else default_domain()


def current_instance_id() -> int:
    """The instance row of the storefront this request was addressed to; the default's (row 1) outside a request."""
    return instance_id_of(current_storefront())


# The domain an `acting_for` block makes instance-level requests as, else None
_acting: ContextVar[DomainInfo | None] = ContextVar('storefront_acting_domain', default=None)


# Where `act_in_this_request` keeps the API caller's domain: the request's environ, which ends with the request (g is
# the app context's, and outlives a request pushed inside an app context a test or a worker holds)
_ACTING_ENVIRON = 'piefed.acting_domain'


def acting_domain() -> DomainInfo:
    """The federating domain an instance-level request (an unsigned fetch's User-Agent, a fetch signed by an instance
    actor) is made as, plan C3:
    - the one an `acting_for` block names (a Celery task's run is one: app.celery_acting, the domain that queued it);
    - else, in a request, the home domain of the local account acting in it (the API caller `authorise_api_user`
      accepted, or the signed-in user), as a fetch a shop account caused is the shop's;
    - else the domain the request came in on, as an inbox POST to a sister domain is that domain's to answer;
    - else the default domain, for relays, housekeeping and anything no account or domain caused.
    A local-only domain (the onion) makes no request another server sees named by it, so it and its accounts act as
    the default domain, as before C3.

    Known edge (re-review N2): the storefront step reads `g.storefront`, which `home_storefront(user)` and
    `default_storefront()` re-pin for their block, so an anonymous request's fetch or task published inside such a
    block (mail to one account) acts as the pinned domain, not the one the request came in on. No fetch is made in
    those blocks today; one that is should run in an `acting_for` block."""
    domain = _acting.get()
    if domain is None and has_request_context():
        domain = request.environ.get(_ACTING_ENVIRON) or _signed_in_domain() or current_storefront()
    return domain if domain is not None and domain.federates else default_domain()


def act_in_this_request(domain: DomainInfo) -> None:
    """Make the rest of this request act as `domain` (`acting_domain`): an API caller's home domain."""
    request.environ[_ACTING_ENVIRON] = domain


def _signed_in_domain() -> DomainInfo | None:
    if not getattr(current_user, 'is_authenticated', False) or not current_user.is_local():
        return None
    return current_user.home_domain()


@contextmanager
def acting_for(domain: DomainInfo):
    """Make the block's instance-level requests as `domain` (the default domain when it does not federate), whoever
    is signed in: a relay's or the discovery credits' fetches are the default domain's even when a sister domain's
    admin asked for them."""
    token = _acting.set(domain if domain.federates else default_domain())
    try:
        yield
    finally:
        _acting.reset(token)


@contextmanager
def acting_as_default():
    """`acting_for(default_domain())`; usable as a decorator, `@acting_as_default()`."""
    with acting_for(default_domain()):
        yield


def path_name(name: str, site_domain_id: int | None) -> str:
    """How the local `name` on the domain with this `site_domain_id` (None: the default's) is written in a path: bare
    on the current storefront, else `name@domain`. The common case reads no domain row."""
    current = current_storefront()
    if site_domain_id == current.id or (site_domain_id is None and current.is_default):
        return name
    domain = domain_by_id(site_domain_id)
    return name if domain == current else f'{name}@{domain.domain}'


def stored_path_name(name: str, site_domain_id: int | None) -> str:
    """How the local `name` on the domain with this `site_domain_id` (None: the default's) is written in stored data
    that later renders as a path (a notification's or report's targets): bare on the default domain, else
    `name@domain`, whichever storefront writes it. A name stored before C1 is bare, and means the default domain."""
    domain = domain_by_id(site_domain_id)
    return name if domain.is_default else f'{name}@{domain.domain}'


def link_host(domain: DomainInfo) -> str:
    """`host[:port]` that names `domain` in a handle (`name@host`): SERVER_NAME for the default domain, else its host."""
    return _authority(domain)


def storefront_url() -> str:
    """Origin of the current storefront. Use for links a visitor follows, never for federation identity."""
    storefront = current_storefront()
    return f'{storefront.scheme}://{_authority(storefront)}'


def storefront_host() -> str:
    """`host[:port]` of the current storefront, for naming this site in text a visitor reads."""
    return _authority(current_storefront())


def storefront_cache_key() -> str:
    """`key_prefix` for an `@cache.cached` presentation view (spec §3.4a). Flask-Caching's default key is the path
    alone, so a page cached on one storefront, and the storefront URLs in it, would be served on every other. The
    discovery documents (NodeInfo, host-meta, the site documents) are keyed by it too: each federating domain's names
    that domain (plan C3)."""
    return f'view/{current_storefront().domain}{request.path}'


def storefront_query_cache_key(*_view_args, **_view_kwargs) -> str:
    """`make_cache_key` for an `@cache.cached` presentation view that reads its query string: `storefront_cache_key`
    plus a hash of the arguments, in any order. Flask-Caching's `query_string=True` ignores `key_prefix`, so a view
    keyed by its query string names its storefront through this instead. The view's own arguments are in the path."""
    pairs = str(tuple(sorted(request.args.items(multi=True)))).encode()
    return f'{storefront_cache_key()}?{hashlib.md5(pairs, usedforsecurity=False).hexdigest()}'


def storefront_memo_name(fname: str) -> str:
    """`make_name` for an `@cache.memoize` function whose result holds storefront URLs: one entry per storefront.
    `cache.delete_memoized(f)` with no arguments still clears every storefront's entry, because the version it
    bumps is keyed by `fname`; with arguments it would clear only the current storefront's."""
    return f'{fname}@{_memo_domain.get() or current_storefront().domain}'


_memo_domain: ContextVar[str | None] = ContextVar('storefront_memo_domain', default=None)


def delete_memoized_everywhere(fn, *args, **kwargs) -> None:
    """`cache.delete_memoized(fn, *args, **kwargs)` on every storefront: each registered domain and the default.
    For a `storefront_memo_name` function called with arguments, the plain call deletes only the entry the current
    storefront keyed, and every other storefront went on serving the stale one. Only these arguments' entries go."""
    for domain in sorted({row.domain for row in domain_rows()} | {default_domain().domain}):
        token = _memo_domain.set(domain)
        try:
            cache.delete_memoized(fn, *args, **kwargs)
        finally:
            _memo_domain.reset(token)


def _moved_to(url, base: str):
    """`url` on origin `base` when it points at this site (any registered host); any other URL, or a value that is
    not a string, unchanged."""
    if not is_local_link(url):
        return url
    parts = urlsplit(url)
    origin = urlsplit(base)
    return urlunsplit((origin.scheme, origin.netloc, parts.path, parts.query, parts.fragment))


def storefront_link(url):
    """`url` moved onto the current storefront when it points at this site. For stored links shown where no HTML
    rewrite reaches (RSS)."""
    return _moved_to(url, storefront_url())


def canonical_link(url):
    """`url` moved onto the canonical origin when it points at this site: what a URL a client read on any storefront
    becomes before it is stored or federated."""
    return _moved_to(url, canonical_url())


def link_on(url, base: str):
    """`url` moved onto origin `base` (a hosted domain's `domain_base_url`) when it points at this site: a local file
    named on the domain that stores or federates it (plan C3), as every host serves the same files.
    `canonical_link` is this for the default domain."""
    return _moved_to(url, base)


_UPLOAD_PATH = '/static/media/'   # the served media tree: every file of ours a URL names


def is_upload_link(url) -> bool:
    """Whether `url` names a file of ours: a link to one of our hosts into the served media tree."""
    return is_local_link(url) and urlsplit(url).path.startswith(_UPLOAD_PATH)


def stored_link(url, upload_base: str):
    """`url`, a link pasted into something stored and federated, as it is stored (plan C3): a file of ours on
    `upload_base`, the federating domain that names it (every host serves the same files); a link to a local-only host
    of ours (the onion) on the default domain's base, as that host is never stored; any other link, a federating
    domain's of ours too, as typed: it names that domain's object, which another of our domains does not serve (the
    404 rule). A value that is not a link of ours comes back unchanged."""
    if not is_local_link(url):
        return url
    if is_upload_link(url):
        return _moved_to(url, upload_base)
    if federating_domain_of_url(url) is None:
        return _moved_to(url, canonical_url())
    return url


def canonical_url() -> str:
    """The federation origin. It never varies with the request host."""
    return current_app.config['SERVER_URL']


# Stored and rendered links (spec §3.5). Attributes only, never text.
# Characters a browser strips from a URL before parsing it (tab, LF, CR). We strip them too, so this module and the
# browser cannot disagree about the host (`is_safe_redirect_target` uses the same table).
URL_STRIPPED_BY_BROWSERS = str.maketrans('', '', '\t\n\r')
_STORED_LINK_ATTRS = frozenset({'href', 'src', 'srcset'})
_RENDER_LINK_ATTRS = _STORED_LINK_ATTRS | {'action'}
# the characters a browser strips from both ends of a URL
_C0_OR_SPACE = ''.join(chr(c) for c in range(0x21))
_REDIRECT_HEADERS = ('Location', 'HX-Redirect')


class _SourceOrderFormatter(HTMLFormatter):
    """bs4's 'minimal' formatter (only &, < and > escaped; script and style text verbatim) without its alphabetical
    sort of attributes: a rewritten page keeps its attributes in the order the template wrote them."""

    def attributes(self, tag):
        return list(tag.attrs.items())


_FORMATTER = _SourceOrderFormatter(entity_substitution=EntitySubstitution.substitute_xml)


def _local_hosts() -> frozenset[str]:
    """Every bare host of this site: SERVER_NAME's and each registered domain's, lower-case."""
    hosts = {_bare_host(current_app.config.get('SERVER_NAME') or '')} | {normalise_host(r.domain) for r in domain_rows()}
    return frozenset(hosts - {''})


def _origin(storefront) -> str:
    return f'{storefront.scheme}://{_authority(storefront)}'


# `/u/name` or `/c/name` and nothing else but a query and a fragment: `name` holds no `@`, so it is on its own domain
_PROFILE_PATH = re.compile(r'^/(u|c)/([^/@?#]+)(?=[?#]|$)')


def _rebase(url, base: str, hosts: frozenset[str], qualify: bool = True):
    """`url` on origin `base` when it is an http(s) or scheme-relative URL whose host is in `hosts`. Everything after
    the authority (path, query, fragment, a bare '?') is kept byte for byte, except that, with `qualify` (a page being
    rendered, a redirect), the exact path `/u/<name>` or `/c/<name>` of a host other than `base`'s names that host
    (`/u/<name>@<host>`): the account is not on `base`'s domain. Content being stored is never qualified (owner
    ruling 2026-10-20): it would write the onion host into what federates. Any other value comes back unchanged."""
    if _link_host(url) not in hosts:
        return url
    parts = urlsplit(url)
    start = url.find(f'//{parts.netloc}')
    if parts.scheme not in ('', 'http', 'https') or start < 0:   # < 0: urlsplit dropped a tab or newline from it
        return url
    rest = url[start + 2 + len(parts.netloc):]
    source = _link_host(url)
    if qualify and source != _link_host(base):
        rest = _PROFILE_PATH.sub(lambda m: f'/{m[1]}/{m[2]}@{source}', rest, count=1)
    return base + rest


# srcset (and a URL list in general) splits on these five characters only, not on Unicode white space
_SPACE_RUN = re.compile(r'[\t\n\f\r ]*')
_NON_SPACE_RUN = re.compile(r'[^\t\n\f\r ]+')


def _srcset_urls(srcset: str) -> list[tuple[int, int]]:
    """The (start, end) of each candidate URL in a `srcset`: the first run free of ASCII white space at the start and
    after each comma, less the commas that end it (`a.jpg, b.jpg 2x`, `a,b.jpg,`). A scan, not a pattern: linear in `srcset`
    whatever runs of commas, spaces or letters it holds."""
    spans, i, size = [], 0, len(srcset)
    while True:
        i = _SPACE_RUN.match(srcset, i).end()
        if i < size and srcset[i] != ',':
            stop = _NON_SPACE_RUN.match(srcset, i).end()
            while srcset[stop - 1] == ',':
                stop -= 1   # the first character is no comma, so this stops
            spans.append((i, stop))
            i = stop
        comma = srcset.find(',', i)
        if comma < 0:
            return spans
        i = comma + 1


def _rebase_srcset(srcset: str, base: str, hosts: frozenset[str], qualify: bool = True) -> str:
    out, last = [], 0
    for start, end in _srcset_urls(srcset):
        out += [srcset[last:start], _rebase(srcset[start:end], base, hosts, qualify)]
        last = end
    return ''.join(out) + srcset[last:]


def _is_link_attr(tag, name: str, render: bool) -> bool:
    if name in _STORED_LINK_ATTRS:
        return True
    # On render, also what JavaScript and a page's metadata follow: a form's action, <meta content> (og:url,
    # og:image) and data-* attributes (data-sse-url). _rebase only moves a value that is a URL of ours.
    return render and (name in _RENDER_LINK_ATTRS or name.startswith('data-') or
                       (name == 'content' and tag.name == 'meta'))


def _rebase_links(html: str, base: str, hosts: frozenset[str], render: bool, embedded: frozenset[str] = frozenset(),
                  s3_host: str = '', proxy: bool = False) -> str:
    """`html` with its link attributes on `base` where their host is in `hosts`. The parse is skipped when no host
    appears in the text at all (case-insensitively), and `html` itself comes back when no attribute moved, so markup
    is re-serialised only when a link really changes. With `embedded`, a stored link also has any URL of those hosts
    inside it moved (`?u=http://abc.onion/x`), as `canonicalise_text` moves them in text. With `s3_host`, the one
    parse also hides what loads from that host (`_hide_s3`), and is never skipped: no raw-text check can see a host
    spelled with an entity or a percent-escape. With `proxy` (an onion storefront), the same parse then points remote
    media at the onion's media proxy (`_proxy_remote`), and is never skipped either: any host may be remote."""
    lowered = html.lower()
    if not s3_host and not proxy and not any(host in lowered for host in hosts):
        return html
    soup = BeautifulSoup(html, 'html.parser', on_duplicate_attribute='ignore')   # the first, as a browser does
    changed = False
    for tag in soup.find_all(True):
        if tag.decomposed:   # inside something already replaced or removed
            continue
        if s3_host and _hide_s3_or_the_tag(soup, tag, s3_host):
            changed = True
        if tag.parent is None or tag.decomposed:   # replaced, removed or unwrapped: nothing left to rebase
            continue
        if proxy and _proxy_remote_or_the_tag(soup, tag):   # after S3 hiding: what it hid is gone by now
            changed = True
        if tag.parent is None or tag.decomposed:
            continue
        for name, value in tag.attrs.items():
            if not _is_link_attr(tag, name, render):
                continue
            moved = (_rebase_srcset(value, base, hosts, render) if name == 'srcset'
                     else _rebase(value, base, hosts, qualify=render))   # stored content moves its host alone
            if embedded:
                moved = _rebase_text(moved, base, embedded)
            if moved != value:
                tag.attrs[name] = moved
                changed = True
    return soup.decode(formatter=_FORMATTER) if changed else html


def _local_only_hosts() -> frozenset[str]:
    """The bare hosts of ours that do not federate (the onion): the only ones stored text is rewritten from. A
    federating domain's host (SERVER_NAME's included) names that domain's object and stays as typed (plan C3)."""
    federating = {_bare_host(current_app.config.get('SERVER_NAME') or '')} | \
        {normalise_host(r.domain) for r in domain_rows() if r.federates or r.is_default}
    return _local_hosts() - federating


def canonicalise_links(html: str, base: str | None = None) -> str:
    """`html` with every `href`/`src`/`srcset` naming a local-only host of this site (`_local_only_hosts`) moved onto
    `base`, the federating owner's base (the canonical origin when no owner is known), for content about to be stored
    or federated. A sister federating domain's link keeps its host (plan C3); one on the canonical host is spelt on the
    canonical origin. The end of `allowlist_html`. A link of ours is also moved where it sits
    inside another URL (`https://ex.com/?u=http://abc.onion/x`, `https://web.archive.org/web/1/http://abc.onion/p`)
    when it appears literally, as `canonicalise_text` moves it in text: leaving it would store the onion host, and
    the privacy invariant outranks fidelity to what the author typed. Only the host moves, never the path: an
    onion-typed `/u/<name>` is stored as the canonical `/u/<name>`, as Markdown stores it, and is qualified only when
    a page renders it. A percent-encoded copy stays as written. With
    no app context (a fuzz campaign) there is no registry of hosts, so `html` comes back as it is."""
    if not has_app_context():
        return html
    hosts = _local_only_hosts()
    html = _rebase_links(html, base or canonical_url(), hosts, render=False, embedded=hosts)
    # the canonical host's own links on the canonical origin, as before: `http://` and port spellings made SERVER_URL
    return _rebase_links(html, canonical_url(), frozenset({_bare_host(current_app.config.get('SERVER_NAME') or '')}),
                         render=False)


# An http(s) or scheme-relative URL's start and authority in plain text (Markdown): not inside a word, another
# scheme or a path. Only `[userinfo@]host[:port]` is matched, so whatever follows a bare host (`]`, `~`, `_`, `,`, a
# path) stays text: `_rebase` keeps everything after the authority, so where the URL itself ends does not matter.
_TEXT_URL_AUTHORITY = re.compile(r'(?:(?<![^\W_])(?<![+.-])https?:|(?<![^\W_])(?<![+.:/-]))//'
                                 r'(?:[^\s/?#@<>()\[\]"\'`\\{}|^*]*@)?'   # userinfo
                                 r'(?:\[[0-9A-Fa-f:.]*\]|(?:[^\W_]|[.-])+)'   # an IPv6 literal, or a name
                                 r'(?::[0-9]*)?', re.IGNORECASE)
_TRAILING_PUNCTUATION = '.,;:!?'


def _rebase_text(text: str, base: str, hosts: frozenset[str]) -> str:
    """`text` with every URL of `hosts` in it moved onto `base`, `text` itself when none moved."""
    lowered = text.lower()
    if not any(host in lowered for host in hosts):
        return text

    def move(match):
        authority = match[0].rstrip(_TRAILING_PUNCTUATION)
        return _rebase(authority, base, hosts, qualify=False) + match[0][len(authority):]

    moved = _TEXT_URL_AUTHORITY.sub(move, text)
    return text if moved == text else moved


def canonicalise_text(text: str | None, base: str | None = None) -> str | None:
    """`text` (Markdown, a plain-text body) with every absolute http(s) or scheme-relative URL on a local-only host of
    this site (`_local_only_hosts`) moved onto `base`, the federating owner's base (the canonical origin when no owner
    is known), for text about to be stored or federated (a federating domain's URL keeps its host, plan C3):
    `source.content` is rendered in preference to the HTML. Hosts match as `is_local_link` matches them (case, a port, a trailing dot
    ignored); everything after the authority is kept byte for byte. Foreign hosts, a federating host of ours and
    text that is not a URL are left alone, and `text` itself comes back when nothing moved. Sentence punctuation
    straight after a bare host stays in the text. A URL of ours is moved wherever it appears literally in the text, inside
    inline code, a fenced block or a foreign URL (`https://ex.com/?u=http://abc.onion/x`): leaving it would store the
    onion host, and the privacy invariant outranks fidelity to what the author typed. A percent-encoded copy stays
    as written. With no app context (a fuzz
    campaign) there is no registry of hosts, so nothing moves."""
    if not text or not has_app_context():
        return text
    return _rebase_text(text, base or canonical_url(), _local_only_hosts())


def _on_default_storefront(storefront) -> bool:
    """`is_default` first: the default storefront's every response passes here, and it then reads no domain rows
    (an error handler's response may come from a request whose session can no longer query)."""
    return storefront.is_default or storefront.domain == default_domain().domain


# Media on the S3 public host is not loaded on an onion storefront (owner ruling 2026-10-18): the link rewrite's one
# parse replaces it with a placeholder, so the onion never makes a request to that clearnet host.
_MEDIA_TAGS = frozenset({'img', 'video', 'audio', 'picture', 'iframe', 'embed', 'object'})
_MEDIA_PARTS = frozenset({'source', 'track'})   # part of the <video>, <audio> or <picture> around them
_MEDIA_CONTAINERS = frozenset({'video', 'audio', 'picture'})
_REMOVED_TAGS = frozenset({'link', 'meta', 'script'})   # a preload, a preview image, a script: nothing to show
# The attributes a browser reads as a URL to load or follow. Never alt, title or a form field's value: they are
# text, and a post's edit form holds its URL in one.
_URL_ATTRS = frozenset({'src', 'srcset', 'href', 'xlink:href', 'poster', 'data', 'action', 'formaction', 'background',
                        'ping', 'manifest', 'cite', 'longdesc'})
_PRECONNECT_RELS = frozenset({'preconnect', 'dns-prefetch'})
_CSS_ESCAPE = re.compile(r'\\(?:([0-9a-fA-F]{1,6})[ \t\n\r\f]?|(.))', re.DOTALL)
_QUOTE = re.compile(r'["\']')
_SPECIAL_SCHEME = re.compile(r'([A-Za-z][A-Za-z0-9+.-]*):')
_SPECIAL_SCHEMES = frozenset({'http', 'https', 'ws', 'wss', 'ftp'})
# Code points a browser refuses in a host once it is decoded and mapped (the WHATWG forbidden domain code points)
_FORBIDDEN_IN_HOST = frozenset(''.join(chr(c) for c in range(0x21)) + '#%/:<>?@[\\]^|\x7f')
UNMAPPABLE = '\0unmappable'   # browser_host of a host UTS-46 cannot map: neither "this host" nor "another", see below


_IPV4_DIGITS = {10: frozenset('0123456789'), 16: frozenset('0123456789abcdefABCDEF'), 8: frozenset('01234567')}


def _ipv4_part(part: str):
    """One part of a WHATWG IPv4 address as an int, None when it is no number (int() alone takes `-1`, `1_0`)."""
    radix = 10
    if part[:2] in ('0x', '0X'):
        part, radix = part[2:], 16
    elif len(part) > 1 and part[0] == '0':
        part, radix = part[1:], 8
    if not set(part) <= _IPV4_DIGITS[radix]:
        return None
    return int(part, radix) if part else 0


def _ipv4_host(host: str) -> str:
    """The dotted-decimal address of a host that ends in a number (`3405803781`, `0xcb.0.113.5`), as the WHATWG
    IPv4 parser reads it; '' when it is no address, which a browser refuses."""
    parts = host.split('.')
    if parts[-1] == '' and len(parts) > 1:
        parts.pop()
    numbers = [_ipv4_part(part) for part in parts] if len(parts) <= 4 and '' not in parts else [None]
    if None in numbers or any(n > 255 for n in numbers[:-1]) or numbers[-1] >= 256 ** (5 - len(numbers)):
        return ''
    value = numbers[-1] + sum(n * 256 ** (3 - i) for i, n in enumerate(numbers[:-1]))
    return str(ipaddress.IPv4Address(value))


def _ends_in_a_number(host: str) -> bool:
    parts = host.split('.')
    if parts[-1] == '' and len(parts) > 1:
        parts.pop()   # one trailing dot only: `a.1..` ends in an empty part, so in no number
    last = parts[-1]
    if last[:2] in ('0x', '0X'):
        return set(last[2:]) <= _IPV4_DIGITS[16]
    return last.isascii() and last.isdecimal()


def browser_host(url) -> str:
    """The bare host a browser loads absolute or scheme-relative `url` from, '' when it names none or the browser
    refuses it. It follows the WHATWG URL host parser: C0 controls and spaces stripped from both ends, tab and line
    breaks removed (the table `is_safe_redirect_target` uses), `\\` as `/`, any number of slashes after a special
    scheme or in front of a scheme-relative authority (`https:host`, `https:///host`, `///host`), userinfo and
    port ignored, the host percent-decoded and mapped by UTS-46 (`idna.uts46_remap`: case, fullwidth and ideographic
    stops, the characters it deletes), a trailing dot dropped. A host ending in a number is the IPv4 address it
    names, an IPv6 literal its compressed address. A host UTS-46 cannot map comes back as UNMAPPABLE, which callers
    that hide things must treat as a match: whether a browser loads it is not worth betting on."""
    if not isinstance(url, str):
        return ''
    candidate = url.strip(_C0_OR_SPACE).translate(URL_STRIPPED_BY_BROWSERS).replace('\\', '/')
    scheme = _SPECIAL_SCHEME.match(candidate)
    if scheme and scheme[1].lower() in _SPECIAL_SCHEMES:
        authority = candidate[scheme.end():].lstrip('/')
    elif candidate.startswith('//'):
        authority = candidate.lstrip('/')
    else:
        return ''
    authority = re.split(r'[/?#]', authority, maxsplit=1)[0].rpartition('@')[2]
    if authority.startswith('['):
        literal, closed, after = authority[1:].partition(']')
        try:
            if not closed or after.partition(':')[0]:
                return ''
            address = ipaddress.IPv6Address(literal)
            return str(address.ipv4_mapped or address.compressed)   # the IPv4 host a dual-stack client reaches
        except ValueError:
            return ''
    try:
        host = unquote_to_bytes(authority.partition(':')[0]).decode('utf-8')
    except UnicodeDecodeError:
        return ''
    except UnicodeEncodeError:
        return UNMAPPABLE   # a lone surrogate: no browser text holds one, so no answer is worth betting on
    try:
        host = idna.uts46_remap(host, std3_rules=False, transitional=False)
    except idna.IDNAError:
        return UNMAPPABLE
    if not host.isascii():
        # one spelling for an IDN: its labels in punycode (idna.encode would refuse hosts a browser loads)
        host = '.'.join(label if label.isascii() else 'xn--' + label.encode('punycode').decode()
                        for label in host.split('.'))
    if _FORBIDDEN_IN_HOST & set(host):
        return ''
    if _ends_in_a_number(host):
        return _ipv4_host(host)
    return host.rstrip('.')


def _s3_host() -> str:
    """The host of `S3_PUBLIC_URL` (`host`, `host/prefix`, `https://host/`, `203.0.113.5:9000`), '' when S3 is not
    configured or the value names no host, read as `browser_host` reads a URL so the two compare."""
    public = (current_app.config.get('S3_PUBLIC_URL') or '').strip()
    host = browser_host(public if '//' in public else f'//{public}') if public else ''
    return '' if host == UNMAPPABLE else host


# CSS Syntax 3 section 4, on the raw value. A name is a whole run of ident code points and escapes (an ident, a
# function's name, a number's unit, a hash or an at-keyword: none of them opens or closes anything, so they need not be
# told apart), and it is url( only when, escapes undone, it is "url" in any case. A string ends at its quote or an
# unescaped newline; escapes are undone per token only.
_CSS_NAME_ESCAPE = r'\\(?:[0-9a-fA-F]{1,6}[ \t\n]?|[^\n0-9a-fA-F])'
_CSS_TOKEN = re.compile(r'([@#]?(?:[\w\x00\x80-\U0010ffff-]|' + _CSS_NAME_ESCAPE + r')+)(\()?|["\']|/\*',
                        re.ASCII | re.DOTALL)
_CSS_SPACE = re.compile(r'[ \t\n\r\f]*')
_CSS_STRING = {'"': re.compile(r'[^"\\\n]*(?:\\.[^"\\\n]*)*', re.DOTALL),
               "'": re.compile(r"[^'\\\n]*(?:\\.[^'\\\n]*)*", re.DOTALL)}
_CSS_BARE_URL = re.compile(r'[^)\\]*(?:\\.[^)\\]*)*', re.DOTALL)
_CSS_NEWLINE = re.compile(r'\r\n?|\f')


def _css_code_point(digits: str) -> str:
    """The character of a hex escape; U+FFFD for zero, a surrogate or past U+10FFFF (CSS Syntax 4.3.7)."""
    code = int(digits, 16)
    return chr(code) if 0 < code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF else '\ufffd'


def _css_unescape(text: str) -> str:
    return _CSS_ESCAPE.sub(lambda m: _css_code_point(m[1]) if m[1] else '' if m[2] == '\n' else m[2], text)


def _css_url_spans(style: str) -> tuple[str, list[tuple[int, int, str]]]:
    """`style` with its newlines normalised, and the `(start, end, url)` of each token `_css_urls` finds in it:
    the span of the raw text in that normalised value, and the URL with its escapes undone."""
    style = _CSS_NEWLINE.sub('\n', style)
    found, pos = [], 0
    while True:
        token = _CSS_TOKEN.search(style, pos)
        if token is None:
            return style, found
        pos = token.end()
        if token[1]:
            if not token[2] or _css_unescape(token[1]).lower() != 'url':
                continue   # a name, or another function: what follows is scanned like the rest
            pos = _CSS_SPACE.match(style, pos).end()
            if style[pos:pos + 1] in _CSS_STRING:
                continue   # url("x"): the string is the token
            end = _CSS_BARE_URL.match(style, pos).end()
            found.append((pos, end, _css_unescape(style[pos:end]).strip()))
            pos = end + 1
        elif token[0] == '/*':
            end = style.find('*/', pos)
            if end < 0:
                return style, found
            pos = end + 2
        else:
            end = _CSS_STRING[token[0]].match(style, pos).end()
            if end > pos:
                found.append((pos, end, _css_unescape(style[pos:end])))
            pos = end + 1


def _css_urls(style: str) -> list[str]:
    """The `url(...)` tokens and quoted strings (`image-set('x' 1x)`, `src("x")`) of a `style` value, in one
    left-to-right scan of the raw value as CSS Syntax tokenises it (newlines normalised first): a `url(` inside a
    string, a comment or a longer name (`xurl(`, `1url(`) is not a token, `u\\72l(` and `\\75 rl(` are, a quote
    inside an unquoted `url(...)` is not a string, and an escaped quote, `*/` or newline opens or closes nothing.
    Escapes are undone on each token found, not before. A string ends at its quote or an unescaped line break, an
    unclosed `url(` and comment at the end. Every character is passed once, so the scan is linear."""
    return [url for _start, _end, url in _css_url_spans(style)[1]]


_PERCENT_PASSES = 4   # %2563 is %63 is c: decoded until it no longer changes, at most this often
_IPV4_RUN = re.compile(r'[0-9a-fx.]+')   # all an IPv4 host is made of, once read as a browser reads a host
_IPV6_LITERAL = re.compile(r'\[[0-9a-f:.]*\]')


class _Uts46Table(dict):
    """A `str.translate` table: each code point mapped by UTS-46 as a browser maps a host (case, fullwidth forms,
    ideographic stops, the code points it ignores dropped), itself when UTS-46 refuses it. Filled as characters are
    met, up to a bound, so a page of distinct characters cannot grow it without end."""

    def __missing__(self, code: int) -> str:
        try:
            mapped = idna.uts46_remap(chr(code), std3_rules=False, transitional=False)
        except idna.IDNAError:
            mapped = chr(code)
        if len(self) < 0x10000:
            self[code] = mapped
        return mapped


_UTS46 = _Uts46Table()


def _read_as_a_host(text: str) -> str:
    """All of `text` read the way a browser reads a host: line breaks normalised and CSS escapes undone as CSS does,
    tab and line breaks removed and percent-decoded until stable, then UTS-46-mapped one character at a time (which
    lower-cases) and NFC-composed. Applied to the whole value, not only to what looks like a host, so no spelling of
    a host in it survives as a different text."""
    text = _css_unescape(_CSS_NEWLINE.sub('\n', text))
    for _pass in range(_PERCENT_PASSES):
        decoded = unquote(text, errors='replace').translate(URL_STRIPPED_BY_BROWSERS)
        if decoded == text:
            break
        text = decoded
    return unicodedata.normalize('NFC', text.translate(_UTS46))


def _is_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _names_host(text: str, host: str) -> bool:
    """Whether `text` names `host` in any spelling a browser reads as that host: `text` is read as a host
    (`_read_as_a_host`) and searched for the host in punycode and in Unicode. An address host is also found in any
    numeric spelling (`3405803781`, `0xcb.0.113.5`, `[::ffff:cb00:7105]`): every run that could be an address is
    read by `browser_host`. A backstop for what the scan of a `style` value does not read as a URL; it hides too much
    (a host in a comment or a font name) rather than too little."""
    value = _read_as_a_host(text)
    spellings = {host}
    try:
        spellings.add(idna.decode(host))
    except (idna.IDNAError, UnicodeError):
        pass   # an address, or a name with no Unicode spelling
    if any(spelling in value for spelling in spellings):
        return True
    candidates = set(_IPV4_RUN.findall(value)) | set(_IPV6_LITERAL.findall(value)) if _is_address(host) else ()
    return any(browser_host(f'//{candidate}') == host for candidate in candidates)


def _points_at(tag, name: str, value, host: str) -> bool:
    """Whether attribute `name` of `tag` makes the browser load `value` from `host`."""
    if not isinstance(value, str):
        return False
    hosts = (host, UNMAPPABLE)
    if name == 'srcset':
        return any(browser_host(value[start:end]) in hosts for start, end in _srcset_urls(value))
    if name == 'style':
        return any(browser_host(url.strip('\'" ')) in hosts for url in _css_urls(value)) or _names_host(value, host)
    if name == 'href' and tag.name == 'link' and _PRECONNECT_RELS & set(tag.get('rel') or ()):
        return browser_host(value) in hosts or browser_host(f'//{value}') in hosts   # base.html writes no scheme
    # a data-* value counts only when it is itself a URL
    return (name in _URL_ATTRS or name.startswith('data-')) and browser_host(value) in hosts


def _hide_s3(soup, tag, host: str) -> bool:
    """Replace what loads from the S3 public host `host` on `tag`; whether anything changed. An `img`, `video`,
    `audio`, `picture`, `iframe`, `embed` or `object` with a URL attribute (`src`, `srcset`, `poster`, `data-*`,
    `style` url(...) and image-set(...)) on that host becomes a placeholder, a `source` or `track` taking its `video`,
    `audio` or `picture` with it. A `link`, `meta` or `script` is removed, a link (`a`) loses its anchor and keeps its
    text, and any other tag loses the attribute. Hosts are compared (`browser_host`); a `style` value is also hidden
    when it names the host in any spelling (`_names_host`). The text of a `<style>` block or a script is not read."""
    hits = [name for name, value in tag.attrs.items() if _points_at(tag, name, value, host)]
    if not hits:
        return False
    if tag.name in _REMOVED_TAGS:
        tag.decompose()
    elif tag.name == 'a':
        tag.unwrap()
    elif tag.name in _MEDIA_TAGS | _MEDIA_PARTS:
        media = tag.parent if tag.name in _MEDIA_PARTS and tag.parent.name in _MEDIA_CONTAINERS else tag
        media.replace_with(_placeholder(soup)).decompose()
    else:
        for name in hits:
            del tag.attrs[name]
    return True


def _placeholder(soup):
    placeholder = soup.new_tag('span', attrs={'class': 'onion-media-hidden'})
    placeholder.string = _('Media hidden on the onion site')
    return placeholder


def _hide_s3_or_the_tag(soup, tag, host: str) -> bool:
    """`_hide_s3`, failing closed: should it raise, the error is logged and `tag` is replaced by the placeholder
    whole, so the page is still served and still makes no request to the S3 host."""
    try:
        return _hide_s3(soup, tag, host)
    except Exception:
        current_app.logger.exception('storefront: the S3 media check failed; the element is hidden')
        tag.replace_with(_placeholder(soup)).decompose()
        return True


# Remote media on an onion page loads through the onion (onion media proxy spec 2026-10-19, section 3.1): the link
# rewrite's one parse points it at the signed proxy, so the visitor's browser never asks a remote host for it.
_PROXIED_TAGS = frozenset({'img', 'video', 'audio', 'source', 'track', 'picture'})
_EMBED_TAGS = frozenset({'iframe', 'embed', 'object'})
_MEDIA_EXTENSIONS = tuple(MEDIA_SUFFIXES)


def _is_remote(url) -> bool:
    """Whether a browser loads `url` from a host that is not one of ours. One UTS-46 cannot map counts as remote."""
    host = browser_host(url)
    return bool(host) and (host == UNMAPPABLE or not is_local_host(host))


def _fetchable(url: str) -> str | None:
    """Remote `url` as the absolute http(s) URL the proxy fetches, https for a scheme-relative one; None when it is
    written in a spelling only a browser reads (`https:host`, a backslash, a tab, a space at an end)."""
    if url != url.strip(_C0_OR_SPACE) or url != url.translate(URL_STRIPPED_BY_BROWSERS) or '\\' in url:
        return None
    if url.startswith('//'):
        url = 'https:' + url
    try:
        parts = urlsplit(url)
    except ValueError:   # an unbalanced bracket in the authority: no client fetches it
        return None
    if parts.scheme.lower() not in ('http', 'https') or not url[len(parts.scheme) + 1:].startswith('//') \
            or not parts.netloc:
        return None
    return url


def _is_media_path(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return path.endswith(_MEDIA_EXTENSIONS) or '/pictrs/image/' in path


def _proxied(url, media_only: bool = False):
    """`url` as its proxy URL when it is remote (and, with `media_only`, a media file); `url` itself when it is not;
    None when it is remote and cannot be fetched as written, so the caller hides it."""
    if not isinstance(url, str) or not _is_remote(url):
        return url
    remote = _fetchable(url)
    if remote is not None and media_only and not _is_media_path(remote):
        return url   # a link to a remote page: the visitor's choice to leave
    return None if remote is None else proxy_url(remote)


def _proxied_srcset(srcset: str) -> str | None:
    out, last = [], 0
    for start, end in _srcset_urls(srcset):
        moved = _proxied(srcset[start:end])
        if moved is None:
            return None
        out += [srcset[last:start], moved]
        last = end
    return ''.join(out) + srcset[last:]


def _proxied_style(style: str) -> str | None:
    """`style` with each remote `url(...)` or string (`_css_url_spans`) replaced, at its own position, by its proxy
    URL; None when one cannot be fetched as written, or a remote one is still there afterwards. `style` itself when
    nothing is remote, otherwise with its newlines normalised."""
    text, spans = _css_url_spans(style)
    out, last = [], 0
    for start, end, url in spans:
        url = url.strip('\'" ')
        moved = _proxied(url) if url else url
        if moved is None:
            return None
        if moved != url:
            out += [text[last:start], moved]
            last = end
    if not out:
        return None if any(_is_remote(url.strip('\'" ')) for _s, _e, url in spans) else style
    result = ''.join(out) + text[last:]
    return None if any(_is_remote(url.strip('\'" ')) for url in _css_urls(result)) else result


def _proxied_value(name: str, value):
    """Attribute `name` of a media element with its remote URLs on the proxy, None when one must be hidden."""
    if not isinstance(value, str):
        return value
    if name == 'srcset':
        return _proxied_srcset(value)
    if name == 'style':
        return _proxied_style(value)
    return _proxied(value) if name in ('src', 'poster') or name.startswith('data-') else value


def _proxy_remote(soup, tag) -> bool:
    """Point what `tag` loads from a remote host at the media proxy; whether anything changed. On an `img`, `video`,
    `audio`, `source`, `track` or `picture`: `src`, `poster`, `data-*`, each `srcset` candidate and `style` url(...).
    On any other element: `style` url(...) (a community, feed or profile banner), and on an `a` also `href`, only
    when it names a media file (an extension of MEDIA_SUFFIXES, or a `/pictrs/image/` path). A remote `iframe`, `embed`
    or `object` becomes the placeholder and a plain link to it, which loads nothing until the visitor follows it.
    What cannot be proxied as written is hidden: a media element becomes the placeholder, an `a` keeps its text, and
    any other element loses the `style`."""
    if tag.name in _EMBED_TAGS:
        if not any(_proxied_value(name, value) != value or (name in _URL_ATTRS and _is_remote(value))
                   for name, value in tag.attrs.items()):   # `data` on an `object`: the embed itself
            return False
        target = next((tag[name] for name in ('src', 'data') if _is_remote(tag.get(name))), None)
        shown = [_placeholder(soup)]
        if target:
            link = soup.new_tag('a', attrs={'href': target, 'rel': 'nofollow noreferrer'})
            link.string = target
            shown.append(link)
        tag.replace_with(*shown).decompose()
        return True
    if tag.name in _PROXIED_TAGS:
        moved = {name: _proxied_value(name, value) for name, value in tag.attrs.items()}
    else:   # any element's style, a banner's background-image among them (spec section 2)
        moved = {'style': _proxied_value('style', tag.get('style'))}
        if tag.name == 'a':
            moved['href'] = _proxied(tag.get('href'), media_only=True)
    moved = {name: value for name, value in moved.items() if value != tag.get(name)}
    if not moved:
        return False
    if tag.name in _PROXIED_TAGS and None in moved.values():
        tag.replace_with(_placeholder(soup)).decompose()
    elif tag.name == 'a' and 'href' in moved and moved['href'] is None:
        tag.unwrap()
    else:   # a style that cannot be proxied as written goes; the element and its content stay
        tag.attrs.update({name: value for name, value in moved.items() if value is not None})
        for name in [name for name, value in moved.items() if value is None]:
            del tag.attrs[name]
    return True


def _proxy_remote_or_the_tag(soup, tag) -> bool:
    """`_proxy_remote`, failing closed as `_hide_s3_or_the_tag` does: should it raise, the error is logged and `tag` is
    replaced by the placeholder whole, so the page is still served and asks no remote host for the element."""
    try:
        return _proxy_remote(soup, tag)
    except Exception:
        current_app.logger.exception('storefront: the remote media proxy rewrite failed; the element is hidden')
        tag.replace_with(_placeholder(soup)).decompose()
        return True


def _s3_host_to_hide(storefront) -> str:
    """The S3 host to hide on `storefront`: '' off the onion, with S3 unset, or when that host is one of ours (the
    link rewrite moves it onto the onion). A clearnet or S3-less install reads nothing more than that."""
    if not storefront.is_onion:
        return ''
    host = _s3_host()
    return host if host and host not in _local_hosts() else ''


def rewrite_links(body: str, storefront: DomainInfo) -> str:
    """`body` with the links of this site's other hosts (`href`, `src`, `srcset`, `action`, `<meta content>`,
    `data-*`) moved onto `storefront`, and on an onion storefront whatever loads from the S3 public host hidden
    (`_hide_s3`) and remote media pointed at the media proxy (`_proxy_remote`), in one parse. Off the onion, `body`
    itself, unparsed, when no such host appears in it and there is no S3 host to hide; an onion page is always
    parsed. Links already on `storefront` are left alone, so a clearnet page that names only its own storefront is
    never parsed."""
    return _rebase_links(body, _origin(storefront), _local_hosts() - {normalise_host(storefront.domain)}, render=True,
                         s3_host=_s3_host_to_hide(storefront), proxy=storefront.is_onion)


def storefront_html(html: str | None) -> str | None:
    """Stored HTML as the current storefront shows it: `rewrite_links` off the default storefront. For HTML that
    leaves in something other than a text/html response (an RSS item body)."""
    storefront = current_storefront()
    if html is None or _on_default_storefront(storefront):
        return html
    return rewrite_links(html, storefront)


def rewrite_response_for_storefront(response: Response) -> Response:
    """`after_request`: a response on a storefront other than the default has its registered-host links moved onto
    that storefront (spec §3.5), once per response instead of at each `|safe` in the templates, and on an onion
    storefront its remote media pointed at the media proxy (`rewrite_links`). A redirect header
    naming another host of ours moves too; a relative one is already on the storefront. Streamed, passthrough
    (SSE, files) and encoded (Content-Encoding) bodies are never read. set_data recomputes Content-Length; status
    and other headers are kept."""
    storefront = current_storefront()
    if _on_default_storefront(storefront):
        return response
    base, hosts = _origin(storefront), _local_hosts() - {normalise_host(storefront.domain)}
    for header in _REDIRECT_HEADERS:
        if header in response.headers:
            response.headers[header] = _rebase(response.headers[header], base, hosts)
    if (response.mimetype == 'text/html' and not response.direct_passthrough and not response.is_streamed
            and 'Content-Encoding' not in response.headers):   # an encoded body is bytes, never decoded here
        body = response.get_data(as_text=True)
        rewritten = rewrite_links(body, storefront)
        if rewritten is not body:
            response.set_data(rewritten)
    return response


def adopt_host_storefront() -> None:
    """For a response made before `resolve_storefront` ran (a limit Flask-Limiter checks in its own before_request,
    registered first): `g.storefront` from the Host header, so the after_request hooks treat it as the request's
    domain's, the onion's policy and rewriting included. An unregistered host stays unresolved: it is the default
    domain's, as it was."""
    if 'storefront' not in g:
        storefront = domain_for_host(request.host)
        if storefront is not None:
            g.storefront = storefront


HEALTH_ENDPOINTS = frozenset({'main.health', 'main.health2'})   # probes name localhost or an internal hostname


def _bind(storefront) -> None:
    """Point this request's URL building (`url_for(_external=True)`) at `storefront`."""
    request_ctx.url_adapter = current_app.url_map.bind(_authority(storefront), script_name=request.script_root,
                                                       url_scheme=storefront.scheme)


@contextmanager
def _pinned(storefront):
    """Render under `storefront` for the duration of the block, whatever the request's Host was; the request's own
    storefront and URL adapter come back afterwards. `storefront_memo_name` reads `g.storefront`, so a memoized
    function called in the block keys its entry by the pinned domain."""
    had_storefront, before = 'storefront' in g, g.get('storefront')
    adapter = request_ctx.url_adapter
    try:   # the pin itself inside: a binding that fails is undone too
        g.storefront = storefront
        _bind(storefront)
        yield
    finally:
        request_ctx.url_adapter = adapter
        if had_storefront:
            g.storefront = before
        else:
            del g.storefront


@contextmanager
def default_storefront():
    """Render under the default storefront for the duration of the block, whatever the request's Host was.

    For mail with no single recipient account: one message to several admins, or to an address that has no account
    (an invite, an applicant's contact address). Mail to one account uses `home_storefront`. Ruling I1 of the
    storefront ledger, that third-party mail always names the default domain, is replaced by recipient home
    domains (per-domain identity spec §3.2). Outside a request the default storefront is already in effect."""
    if not has_request_context():
        yield
        return
    with _pinned(default_domain()):
        yield


@contextmanager
def home_storefront(user):
    """Render under `user`'s home domain for the duration of the block, for mail or any message sent to that one
    account: an onion account's links name the onion, a clearnet account's its own domain even when an onion
    request triggered the send. Outside a request (a cron job) a request context for the domain is pushed, so
    `url_for(_external=True)` names it there too."""
    domain = user.home_domain()
    if has_request_context():
        with _pinned(domain):
            yield
        return
    with current_app.test_request_context(base_url=_origin(domain)), _pinned(domain):
        yield


def resolve_storefront() -> Response | None:
    """First `before_request`: set `g.storefront` from the Host header, or refuse an unregistered host with 421.

    Flask 3.1 builds the request's URL adapter with SERVER_NAME as the server name, so a request on another
    registered host still routes (3.1 no longer restricts requests to SERVER_NAME) but `url_for(_external=True)`
    would name SERVER_NAME. The adapter is rebound to the storefront's own domain and scheme, so external URLs
    follow the host. SERVER_NAME itself is untouched."""
    storefront = domain_for_host(request.host)
    if storefront is None and request.endpoint in HEALTH_ENDPOINTS:
        storefront = default_domain()   # health checks are exempt from the 421
    if storefront is None:
        return Response('Misdirected Request: this host is not served here.\n', status=421, mimetype='text/plain')
    g.storefront = storefront
    _bind(storefront)
    return None


