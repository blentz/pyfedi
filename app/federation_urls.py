"""The URLs a federating domain names what it federates by, and the key its instance actor signs with (plan C3).

Each federating domain is a separate ActivityPub server. What a row federates is named on its owner's domain, never on
the storefront the request came in on: a Celery task has no request, and a hell.cloud user's post read on a sister
domain is still hell.cloud's. Nothing a sister domain federates names hell.cloud (owner ruling).

For the default domain every helper returns exactly the string PieFed has always built from SERVER_URL, and its
instance actor signs with Site row 1's key, so hell.cloud's federation does not change by a byte.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from sqlalchemy import select

from app import db
from app.storefront import DomainInfo, default_domain, domain_authority, domain_base_url, domain_rows
# The modules, not the names: app.models and app.utils import much of the app, which may import this module
# (import cycle: models, utils)
import app.models as models
import app.utils as utils


def owner_domain(obj) -> DomainInfo | None:
    """The hosted domain `obj` is federated from, or None when it is not ours to federate (a remote account or
    community, or a row a remote account made). A local account or community is its home domain's; a post, reply or
    private message its author's (the sender's); a vote its voter's, not the voted-on author's; a local feed the
    default domain's, as feeds have no domain of their own. A DomainInfo is its own owner. TypeError for anything
    else, a row with no author among them."""
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


def domain_of_url(url: str | None) -> DomainInfo | None:
    """The hosted domain whose authority is exactly `url`'s, ignoring case, else None. Not a prefix or suffix match:
    shop.example.evil and evil.shop.example are not shop.example, and neither is a URL with userinfo or another port.
    The default domain's authority is SERVER_NAME verbatim, its port included."""
    try:
        authority = urlsplit(url or '').netloc.lower()
    except ValueError:   # an unbalanced IPv6 bracket
        return None
    if not authority:
        return None
    return next((row for row in domain_rows() if domain_authority(row).lower() == authority), None)


def activity_id(verb: str, owner, token: str | None = None) -> str:
    """An activity's id on `owner`'s domain: `<base>/activities/<verb>/<token>`, the token 15 random characters
    unless the caller keeps one of its own (a join request's uuid, a stable token). ValueError for a remote owner."""
    return f'{owner_base(owner)}/activities/{verb}/{token or utils.gibberish(15)}'


def instance_actor_url(domain: DomainInfo) -> str:
    """`domain`'s instance actor: SERVER_URL/actor for the default domain."""
    return f'{domain_base_url(domain)}/actor'


def instance_actor_signing(domain: DomainInfo) -> tuple[str, str]:
    """The private key `domain`'s instance actor signs with and its keyId. The default domain's is Site row 1's,
    every other domain's its site_domain row's. Read from the database on each call, not from the cached DomainInfo,
    so a key never sits in the cache and a changed key is used at once."""
    if domain.is_default:
        query = select(models.Site.private_key).where(models.Site.id == 1)
    else:
        query = select(models.SiteDomain.private_key).where(models.SiteDomain.id == domain.id)
    return db.session.execute(query).scalar(), f'{instance_actor_url(domain)}#main-key'
