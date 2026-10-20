"""Local names are unique per domain (spec 2026-10-19 §3.1): the helpers that turn a name or handle into the domain
it is on, find that domain's row, and spell a row's name for a path."""
from sqlalchemy import and_, func

from app.models import Community, User
from app.storefront import DomainInfo, current_storefront, domain_for_host, on_domain


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


def local_named(model, name: str, domain: DomainInfo):
    """The clause for the local `model` (User or Community) called `name`, any case, on `domain`."""
    column = User.user_name if model is User else Community.name
    return and_(func.lower(column) == name.lower(), model.ap_id.is_(None), on_domain(domain, model))


def local_path_name(obj) -> str:
    """How a local user or community is named in a path: bare on its own domain's storefront, else `name@domain`."""
    name = obj.user_name if isinstance(obj, User) else obj.name
    domain = obj.home_domain()
    return name if domain == current_storefront() else f'{name}@{domain.domain}'
