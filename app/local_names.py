"""Local names are unique per domain (spec 2026-10-19 §3.1): the helpers that turn a name or handle into the domain
it is on, find that domain's row, and spell a row's name for a path."""
import re

from sqlalchemy import and_, false, func

from app.models import Community, User
from app.storefront import DomainInfo, current_storefront, domain_by_id, domain_for_host, on_domain, path_name


def split_local_name(raw: str) -> tuple[str, DomainInfo] | None:
    """`(name, domain)` for a local name or handle, or None. White space and one leading `@` or `!` come off. A bare
    `name` is on the current storefront; `name@host` is on the domain `host` names, when the site hosts it. The name
    keeps its case: `local_named` lowers both sides."""
    text = raw.strip()
    if text[:1] in ('@', '!'):
        text = text[1:]
    name, *host = text.split('@')
    if not name or len(host) > 1:
        return None
    if not host:
        return name, current_storefront()
    domain = domain_for_host(host[0])
    return (name, domain) if domain is not None else None


def local_named(model, name: str, domain: DomainInfo, exact_case: bool = False):
    """The clause for the local `model` (User or Community) called `name`, any case, on `domain`. `exact_case` keeps
    a lookup that has always matched the stored case alone as it was: passkey login, and stored_user's bare name."""
    column = User.user_name if model is User else Community.name
    named = column == name if exact_case else func.lower(column) == name.lower()
    return and_(named, model.ap_id.is_(None), on_domain(domain, model))


def local_clause(model, actor: str):
    """The clause for the local `model` an `<actor>` path segment names: a bare name on the storefront's domain,
    name@host on that hosted domain. A handle on a domain this site does not host names nothing here."""
    split = split_local_name(actor)
    return local_named(model, *split) if split is not None else false()


def named_by_actor(model, actor: str):
    """The clause for the row an `<actor>` path segment names: the local one `local_clause` finds, else, for a handle
    on a domain this site does not host, the remote row whose `ap_id` it is. Anything else names nothing."""
    split = split_local_name(actor)
    if split is not None:
        return local_named(model, *split)
    return model.ap_id == actor if '@' in actor else false()


def local_path_name(obj) -> str:
    """How a local user or community is named in a path: bare on its own domain's storefront, else `name@domain`."""
    name = obj.user_name if isinstance(obj, User) else obj.name
    return path_name(name, obj.site_domain_id)


def actor_handle(obj) -> str:
    """The handle naming a user or community on any storefront and outside a request: a remote row's `ap_id`, a local
    row's `name@domain` for its own domain. For work queued with a name, which a worker has no storefront to read."""
    if obj.ap_id:
        return obj.ap_id
    name = obj.user_name if isinstance(obj, User) else obj.name
    return f'{name}@{domain_by_id(obj.site_domain_id).domain}'


def target_id_key(name_key: str) -> str:
    """The `targets` key that holds the id of the account or community whose stored name is under `name_key`: the name
    key's trailing `_user_name` (else `_name`) becomes `_id`. `suspect_user_user_name` -> `suspect_user_id`,
    `author_user_name` -> `author_id`, `community_name` -> `community_id`."""
    return re.sub(r'(_user)?_name$', '_id', name_key)
