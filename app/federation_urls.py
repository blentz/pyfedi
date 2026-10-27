"""The URLs a federating domain names what it federates by, and the key its instance actor signs with (plan C3).

Each federating domain is a separate ActivityPub server. What a row federates is named on its owner's domain, never on
the storefront the request came in on: a Celery task has no request, and a first-domain user's post read on a sister
domain is still the first domain's. Nothing a sister domain federates names the first domain (owner ruling).

For the default domain every helper returns exactly the string PieFed has always built from SERVER_URL, and its
instance actor signs with Site row 1's key, so the first domain's federation does not change by a byte.
"""
from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit, urlunsplit

from flask import abort, current_app
from sqlalchemy import func, select

from app import db
from app.branding import own_value
from app.local_names import local_named, named_by_actor
from app.storefront import DomainInfo, current_storefront, default_domain, domain_authority, domain_base_url, \
    federating_base, hosted_instance_ids, is_hosted_uri
# Defined in app.storefront, which app.models can import (this module imports app.models); named here too, as the
# helpers every federation decision uses
from app.storefront import domain_of_url, federating_domain_of_url, is_federating_url  # noqa: F401
# The modules, not the names: app.models and app.utils import much of the app, which may import this module
# (import cycle: models, utils)
import app.models as models
import app.utils as utils


def owner_domain(obj) -> DomainInfo | None:
    """The hosted domain `obj` is federated from, or None when it is not ours to federate (a remote account or
    community, or a row a remote account made). A local account, community or feed is its home domain's; a post,
    reply or private message its author's (the sender's); a vote its voter's, not the voted-on author's. A DomainInfo
    is its own owner. TypeError for anything else, a row with no author among them.

    The domain may be a local-only one (an onion-homed account's): a caller deciding federation checks `.federates`
    (or relies on `local_only.refused_outbound`) before it sends anything named by it."""
    if isinstance(obj, DomainInfo):
        return obj
    if isinstance(obj, (models.User, models.Community, models.Feed)):
        return obj.home_domain() if obj.is_local() else None
    if isinstance(obj, (models.Post, models.PostReply)):
        return owner_domain(obj.author)
    if isinstance(obj, models.ChatMessage):
        return owner_domain(obj.sender)
    if isinstance(obj, (models.PostVote, models.PostReplyVote)):
        return owner_domain(db.session.get(models.User, obj.user_id))
    raise TypeError(f'{type(obj).__name__} has no owning domain')


def owner_base(obj) -> str:
    """The origin `obj`'s owner names it by (`federating_base`): SERVER_URL itself for the default domain, and for a
    local-only domain's (the onion's), whose host is never stored or federated. ValueError when no hosted domain owns
    it."""
    domain = owner_domain(obj)
    if domain is None:
        raise ValueError(f'{type(obj).__name__} is not federated from a domain of ours')
    return federating_base(domain)


def naming_domain(obj) -> DomainInfo | None:
    """The hosted domain our URLs of `obj` are named on: its owner (`owner_domain`); for a remote post or comment,
    the local community it is in, as that community serves its copy (its context and replies collections). None
    when no domain of ours names it."""
    domain = owner_domain(obj)
    if domain is None and isinstance(obj, (models.Post, models.PostReply)):
        domain = owner_domain(obj.community)
    return domain


def object_base(obj) -> str:
    """The origin our URLs of a post or comment are named on: its `naming_domain`'s, else the default domain's
    (SERVER_URL), as before C3. For a post or comment it is the models' own `federation_base`, so the two cannot
    disagree. Unlike `owner_base`, never raises."""
    if isinstance(obj, (models.Post, models.PostReply)):
        return models.federation_base(obj)
    domain = naming_domain(obj)
    return federating_base(domain if domain is not None else default_domain())


def owned_elsewhere(obj) -> bool:
    """True when `obj` is named on another hosted domain (`naming_domain`) than the one the request came in on. Its
    ActivityPub document is not this domain's to serve: the answer is the 404 an unknown object gets, so a fetch
    never shows that two of our domains are one server (owner ruling)."""
    domain = naming_domain(obj)
    return domain is not None and domain.domain != current_storefront().domain


def federating_storefront() -> DomainInfo:
    """The domain this request came in on, whose instance actor and discovery documents answer it. A local-only
    domain (the onion) has none another server may see: 404, as a local-only account's document is."""
    domain = current_storefront()
    if not domain.federates:
        abort(404)
    return domain


def domain_site_name(domain: DomainInfo, site) -> str:
    """The name `domain`'s site goes by in what it tells other servers and clients: `site.name` for the default
    domain; for any other its own display name (per-domain branding, spec §3.3), else the domain itself, as Site.name
    is the default domain's and nothing a sister domain publishes names the default domain (owner ruling)."""
    return own_value(domain, 'name') or (site.name if domain.is_default else domain_authority(domain))


def hosted_domain_names() -> list[str]:
    """SERVER_NAME and the domain of every hosted domain's instance row: the hosts a fan-out never sends to, as
    their accounts are told here."""
    rows = db.session.execute(select(models.Instance.domain).where(models.Instance.id.in_(hosted_instance_ids())))
    return [current_app.config['SERVER_NAME'], *rows.scalars()]


def activity_id(verb: str, owner, token: str | None = None) -> str:
    """An activity's id on `owner`'s domain: `<base>/activities/<verb>/<token>`, the token 15 random characters
    unless the caller keeps one of its own (a join request's uuid, a stable token). ValueError for a remote owner."""
    return f'{owner_base(owner)}/activities/{verb}/{token or utils.gibberish(15)}'


def instance_actor_url(domain: DomainInfo) -> str:
    """`domain`'s instance actor: SERVER_URL/actor for the default domain."""
    return f'{domain_base_url(domain)}/actor'


def instance_actor_public_key(domain: DomainInfo) -> str | None:
    """The public key `domain`'s instance actor (`/actor`, and `/`) publishes: Site row 1's for the default domain,
    every other domain's its site_domain row's. Read from the database on each call, as `instance_actor_signing`
    reads the private half."""
    if domain.is_default:
        query = select(models.Site.public_key).where(models.Site.id == 1)
    else:
        query = select(models.SiteDomain.public_key).where(models.SiteDomain.id == domain.id)
    return db.session.execute(query).scalar()


class MissingInstanceKey(LookupError):
    """A domain's instance actor has no private key to sign with."""


def instance_actor_signing(domain: DomainInfo) -> tuple[str, str]:
    """The private key `domain`'s instance actor signs with and its keyId. The default domain's is Site row 1's,
    every other domain's its site_domain row's. Read from the database on each call, not from the cached DomainInfo,
    so a key never sits in the cache and a changed key is used at once. MissingInstanceKey when there is none (the
    row is gone, or its key was never made): nothing is sent unsigned or signed by another domain's key."""
    if domain.is_default:
        query = select(models.Site.private_key).where(models.Site.id == 1)
    else:
        query = select(models.SiteDomain.private_key).where(models.SiteDomain.id == domain.id)
    private_key = db.session.execute(query).scalar()
    if not private_key:
        raise MissingInstanceKey(f'{domain.domain} has no instance actor key')
    return private_key, f'{instance_actor_url(domain)}#main-key'


# The paths our domains serve a post, comment, account, community or feed at (plan C4): a post by id, with or
# without its slug, or under its community's nice URL; a comment by id; an account, community or feed by name.
_ID_PATHS = ((re.compile(r'/post/([0-9]{1,10})(?:/([^/]+))?'), 'Post'),   # group 2: a slug, or a sub-resource
             (re.compile(r'/c/[^/]+/p/([0-9]{1,10})(?:/[^/]+)?'), 'Post'),
             (re.compile(r'/comment/([0-9]{1,10})'), 'PostReply'))
_NAME_PATHS = ((re.compile(r'/u/([^/]+)'), 'User'), (re.compile(r'/c/([^/]+)'), 'Community'),
               (re.compile(r'/f/([^/]+)'), 'Feed'))
_PG_INT_MAX = 2 ** 31 - 1   # a larger id names no row, and Postgres refuses it as an integer
# A route under /post/<id>/ with one more segment: a fixed name, or an any() converter's names
_POST_SUB_RULE = re.compile(r'/post/<int:post_id>/(?:<any\(([^)]*)\):\w+>|([^/<]+))')


def _post_sub_resources() -> frozenset[str]:
    """The names our own routes serve under /post/<id>/ (replies, context, likes, edit, ...), read once per app from
    its URL map, so the set cannot drift from the routes. A URL naming one is that resource, not the post."""
    names = current_app.extensions.get('post_sub_resources')
    if names is None:
        found = set()
        for rule in current_app.url_map.iter_rules():
            matched = _POST_SUB_RULE.fullmatch(rule.rule)
            if matched:
                found.update(name.strip() for name in (matched.group(1) or matched.group(2)).split(','))
        names = current_app.extensions['post_sub_resources'] = frozenset(found)
    return names


def _stored_form(parts) -> str:
    """A URL (its urlsplit `parts`) as a stored `ap_id` or `ap_profile_id` spells it: scheme and host in lower case,
    no trailing slash."""
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip('/'), parts.query,
                       parts.fragment))


def _stored_row(parts):
    """The row whose stored URL is the URL split into `parts` (`_stored_form`): only legacy default-domain rows
    carry one."""
    stored = _stored_form(parts)
    for model, column in ((models.Post, models.Post.ap_id), (models.PostReply, models.PostReply.ap_id),
                          (models.User, models.User.ap_profile_id), (models.Community, models.Community.ap_profile_id),
                          (models.Feed, models.Feed.ap_profile_id)):
        row = db.session.query(model).filter(column == stored).first()
        if row is not None:
            return row
    return None


def _named_row(model, name: str, domain: DomainInfo | None):
    """The row `/u/<name>`, `/c/<name>` or `/f/<name>` names on `domain`, as those routes look it up: a bare name is
    the local row on the URL's own domain, `name@host` the row that handle names (`named_by_actor`)."""
    if '@' in name:
        return db.session.query(model).filter(named_by_actor(model, name)).first()
    if domain is None:   # a URL whose authority names no domain row (userinfo): its names are nobody's
        return None
    return db.session.query(model).filter(local_named(model, name, domain)).first()


def local_object_for_url(url: str):
    """The post, comment, account, community or feed a URL on one of our own domains names, read from the database,
    so it is never fetched over HTTP (plan C4): one server answers for every hosted domain. None for a URL on no
    domain of ours (the caller fetches it as before), and for one of ours that names nothing; a caller then stops,
    with no fetch.

    First the row that stores `url` as its `ap_id` / `ap_profile_id` (host in any case, trailing slash dropped).
    Then the path: `/post/<id>[/<slug>]` (not a sub-resource such as `/post/<id>/replies`), `/c/<name>/p/<id>[/<slug>]`
    and `/comment/<id>` by primary key, whichever of our domains the URL is on, as the database is shared (only
    serving a document is per domain); `/u/<name>`, `/c/<name>` and `/f/<name>` on the URL's own domain, where local
    names are unique (C1)."""
    if not is_hosted_uri(url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:   # httpx reads a host in it that urllib refuses (an unbalanced bracket): it names nothing
        return None
    found = _stored_row(parts)
    if found is not None:
        return found
    path = unquote(parts.path).rstrip('/')   # as the router reads it
    if '\x00' in path:   # names no row, and Postgres refuses it in a string
        return None
    for pattern, model_name in _ID_PATHS:
        matched = pattern.fullmatch(path)
        if matched:
            if matched.lastindex == 2 and matched.group(2) in _post_sub_resources():
                return None   # /post/<id>/replies and the like: a collection or a page, not the post
            row_id = int(matched.group(1))
            return db.session.get(getattr(models, model_name), row_id) if row_id <= _PG_INT_MAX else None
    for pattern, model_name in _NAME_PATHS:
        matched = pattern.fullmatch(path)
        if matched:
            return _named_row(getattr(models, model_name), matched.group(1), domain_of_url(url))
    return None
