"""The URLs a federating domain names what it federates by, and the key its instance actor signs with (plan C3).

Each federating domain is a separate ActivityPub server. What a row federates is named on its owner's domain, never on
the storefront the request came in on: a Celery task has no request, and a hell.cloud user's post read on a sister
domain is still hell.cloud's. Nothing a sister domain federates names hell.cloud (owner ruling).

For the default domain every helper returns exactly the string PieFed has always built from SERVER_URL, and its
instance actor signs with Site row 1's key, so hell.cloud's federation does not change by a byte.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from flask import current_app
from sqlalchemy import select

from app import db
from app.storefront import DomainInfo, current_storefront, default_domain, domain_authority, domain_base_url, \
    domain_rows, hosted_instance_ids
# The modules, not the names: app.models and app.utils import much of the app, which may import this module
# (import cycle: models, utils)
import app.models as models
import app.utils as utils


def owner_domain(obj) -> DomainInfo | None:
    """The hosted domain `obj` is federated from, or None when it is not ours to federate (a remote account or
    community, or a row a remote account made). A local account or community is its home domain's; a post, reply or
    private message its author's (the sender's); a vote its voter's, not the voted-on author's; a local feed the
    default domain's, as feeds have no domain of their own. A DomainInfo is its own owner. TypeError for anything
    else, a row with no author among them.

    The domain may be a local-only one (an onion-homed account's): a caller deciding federation checks `.federates`
    (or relies on `local_only.refused_outbound`) before it sends anything named by it."""
    if isinstance(obj, DomainInfo):
        return obj
    if isinstance(obj, (models.User, models.Community)):
        return obj.home_domain() if obj.is_local() else None
    if isinstance(obj, models.Feed):
        return default_domain() if obj.is_local() else None
    if isinstance(obj, (models.Post, models.PostReply)):
        return owner_domain(obj.author)
    if isinstance(obj, models.ChatMessage):
        return owner_domain(obj.sender)
    if isinstance(obj, (models.PostVote, models.PostReplyVote)):
        return owner_domain(db.session.get(models.User, obj.user_id))
    raise TypeError(f'{type(obj).__name__} has no owning domain')


def owner_base(obj) -> str:
    """The origin `obj`'s owner names it by (`domain_base_url`): SERVER_URL itself for the default domain.
    ValueError when no hosted domain owns it."""
    domain = owner_domain(obj)
    if domain is None:
        raise ValueError(f'{type(obj).__name__} is not federated from a domain of ours')
    return domain_base_url(domain)


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
    return domain_base_url(domain if domain is not None else default_domain())


def owned_elsewhere(obj) -> bool:
    """True when `obj` is named on another hosted domain (`naming_domain`) than the one the request came in on. Its
    ActivityPub document is not this domain's to serve: the answer is the 404 an unknown object gets, so a fetch
    never shows that two of our domains are one server (owner ruling)."""
    domain = naming_domain(obj)
    return domain is not None and domain.domain != current_storefront().domain


def hosted_domain_names() -> list[str]:
    """SERVER_NAME and the domain of every hosted domain's instance row: the hosts a fan-out never sends to, as
    their accounts are told here."""
    rows = db.session.execute(select(models.Instance.domain).where(models.Instance.id.in_(hosted_instance_ids())))
    return [current_app.config['SERVER_NAME'], *rows.scalars()]


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


def federating_domain_of_url(url: str | None) -> DomainInfo | None:
    """`domain_of_url`'s domain when it federates, else None. What federation code asks: a local-only domain's URL is
    never an object a peer may name, nor one we send."""
    found = domain_of_url(url)
    return found if found is not None and found.federates else None


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
