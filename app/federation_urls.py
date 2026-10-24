"""The URLs a federating domain names what it federates by, and the key its instance actor signs with (plan C3).

Each federating domain is a separate ActivityPub server. What a row federates is named on its owner's domain, never on
the storefront the request came in on: a Celery task has no request, and a hell.cloud user's post read on a sister
domain is still hell.cloud's. Nothing a sister domain federates names hell.cloud (owner ruling).

For the default domain every helper returns exactly the string PieFed has always built from SERVER_URL, and its
instance actor signs with Site row 1's key, so hell.cloud's federation does not change by a byte.
"""
from __future__ import annotations

from flask import abort, current_app
from sqlalchemy import select

from app import db
from app.storefront import DomainInfo, current_storefront, default_domain, domain_authority, domain_base_url, \
    federating_base, hosted_instance_ids
# Defined in app.storefront, which app.models can import (this module imports app.models); named here too, as the
# helpers every federation decision uses
from app.storefront import domain_of_url, federating_domain_of_url, is_federating_url  # noqa: F401
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
    domain, the domain itself for any other, as Site.name is the default domain's (hell.cloud on prod) and nothing a
    sister domain publishes names hell.cloud (owner ruling)."""
    return site.name if domain.is_default else domain_authority(domain)


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
