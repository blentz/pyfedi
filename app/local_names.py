"""Local names are unique per domain (spec 2026-10-19 §3.1): the helpers that turn a name or handle into the domain
it is on, find that domain's row, and spell a row's name for a path."""
import re

from flask import url_for
from markupsafe import Markup, escape
from sqlalchemy import and_, false, func, select

from app.models import Community, User
from app import db
from app.storefront import DomainInfo, current_storefront, default_domain, domain_by_id, domain_for_host, on_domain, \
    path_name


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


def stored_name_id(model, stored: str) -> int | None:
    """The id of the user or community a name stored in `targets` names, or None. Stored names are the same on every
    storefront: bare is the default domain's, `name@domain` that hosted domain's (else a remote row's `ap_id`)."""
    clause = local_named(model, stored, default_domain()) if '@' not in stored else named_by_actor(model, stored)
    return db.session.scalar(select(model.id).where(clause).limit(1))


def target_href(targets: dict | None, name_key: str) -> str | None:
    """Where the account or community whose stored name is `targets[name_key]` is linked, by id so the link works on
    whichever domain the viewer is on: `/user/<id>` or `/community/<id>`. The id is `targets[target_id_key(name_key)]`;
    a row stored without one is found by its name (`stored_name_id`). None when the name is empty or finds nothing: the
    caller shows it as text, never as a `/u/<name>` path that might reach a namesake."""
    targets = targets or {}
    name = targets.get(name_key)
    if not name:
        return None
    is_user = name_key.endswith('_user_name')
    id_ = targets.get(target_id_key(name_key))
    if not isinstance(id_, int):
        id_ = stored_name_id(User if is_user else Community, name)
    if id_ is None:
        return None
    return url_for('user.show_profile_by_id', user_id=id_) if is_user \
        else url_for('community.show_community_by_id', community_id=id_)


def target_link(targets: dict | None, name_key: str, css_class: str = '') -> Markup:
    """`targets[name_key]` as a link by `target_href`, or as plain text when it has none."""
    name = (targets or {}).get(name_key) or ''
    href = target_href(targets, name_key)
    if href is None:
        return escape(name)
    css = Markup(' class="{}"').format(css_class) if css_class else Markup('')
    return Markup('<a{} href="{}">{}</a>').format(css, href, name)


def notification_href(notification) -> str | None:
    """Where following `notification` goes, or None when it names nothing. A url is followed as stored, except the
    `/c/<stored name>` a new-moderator notification kept before follow-up F1, which can name a namesake on another
    domain: that goes to the community by id, from its targets or else a lookup of the name (`stored_name_id`)."""
    url = notification.url
    if not url.startswith('/c/'):
        return url
    community_id = (notification.targets or {}).get('community_id') or stored_name_id(Community, url[len('/c/'):])
    return None if community_id is None else url_for('community.show_community_by_id', community_id=community_id)
