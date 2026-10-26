"""Local names are unique per domain (spec 2026-10-19 §3.1): the helpers that turn a name or handle into the domain
it is on, find that domain's row, and spell a row's name for a path."""
import re

from flask import current_app, url_for
from flask_babel import _
from markupsafe import Markup, escape
from sqlalchemy import and_, false, func, or_, select

from app.models import Community, Post, PostReply, User
from app import db
from app.constants import AUTOMATED_REPORTER
from app.local_only import any_domain_closed, hidden_here
from app.storefront import DomainInfo, current_storefront, default_domain, domain_by_id, domain_for_host, \
    host_hidden_here, on_domain, path_name


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


def find_local_user(actor_url: str, allow_banned: bool = False) -> User:
    """Find a local user by URL or alt name."""
    alt_user_name = actor_url.rsplit('/', 1)[-1]
    user = db.session.query(User).filter(or_(User.ap_profile_id == actor_url, User.alt_user_name == alt_user_name),
                                         User.ap_id == None)
    if not allow_banned:
        user = user.filter_by(banned=False)
    return user.first()


def default_domain_user(actor: str, allow_banned: bool = False) -> User | None:
    """The account a bare `/u/<actor>` names on the default domain: by the actor URL SERVER_URL builds, as it always
    was (which also finds an alternate name, on any domain), read as find_actor_by_url reads it, stripped and
    lower-cased. user_for_actor and the route gate (app.hidden_routes) both ask this, so they cannot disagree on
    which account a spelling names (follow-up F6)."""
    return find_local_user(f'{current_app.config["SERVER_URL"]}/u/{actor.strip()}'.strip().lower(), allow_banned)


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


def hidden_target(model, id_: int | None) -> bool:
    """Whether the `model` row `id_` (a User, Community, Post or PostReply) is hidden here (`hidden_here`), so a report
    or notification names it as text, never a link (follow-up F6). Report and notification pages have signed-in
    readers only, so it is the storefront that decides. The row is read only when some domain is local-only."""
    if not any_domain_closed():
        return False
    row = db.session.get(model, id_)
    return row is not None and hidden_here(row, True)


def hidden_note() -> Markup:
    """What a report or notification says beside a target hidden here (owner ruling, follow-up F6)."""
    return Markup(' <small class="text-body-secondary">{}</small>').format(_('Open this on the onion to see it.'))


def _target(targets: dict | None, name_key: str) -> tuple[str | None, bool]:
    """`target_href`'s answer, and whether the target is hidden here."""
    targets = targets or {}
    name = targets.get(name_key)
    if not name or name == AUTOMATED_REPORTER:
        return None, False
    is_user = name_key.endswith('_user_name')
    model = User if is_user else Community
    id_ = targets.get(target_id_key(name_key))
    if not isinstance(id_, int):
        id_ = stored_name_id(model, name)
    if id_ is None:
        return None, False
    if hidden_target(model, id_):
        return None, True
    return (url_for('user.show_profile_by_id', user_id=id_) if is_user
            else url_for('community.show_community_by_id', community_id=id_)), False


def target_href(targets: dict | None, name_key: str) -> str | None:
    """Where the account or community whose stored name is `targets[name_key]` is linked, by id so the link works on
    whichever domain the viewer is on: `/user/<id>` or `/community/<id>`. The id is `targets[target_id_key(name_key)]`;
    a row stored without one is found by its name (`stored_name_id`). None when the name is empty or finds nothing, or
    names a row hidden here (follow-up F6): the caller shows it as text, never as a `/u/<name>` path that might reach a
    namesake."""
    return _target(targets, name_key)[0]


def target_hidden(targets: dict | None, name_key: str) -> bool:
    """Whether the account or community whose stored name is `targets[name_key]` is hidden here (follow-up F6)."""
    return _target(targets, name_key)[1]


def target_link(targets: dict | None, name_key: str, css_class: str = '') -> Markup:
    """`targets[name_key]` as a link by `target_href`, or as plain text when it has none; a target hidden here is
    plain text with `hidden_note` beside it."""
    name = (targets or {}).get(name_key) or ''
    href, hidden = _target(targets, name_key)
    if href is None:
        return escape(name) + hidden_note() if hidden else escape(name)
    css = Markup(' class="{}"').format(css_class) if css_class else Markup('')
    return Markup('<a{} href="{}">{}</a>').format(css, href, name)


_CONTENT_LINKS = {'suspect_post_id': (Post, '/post/{}'), 'suspect_comment_id': (PostReply, '/comment/{}')}


def content_link(targets: dict | None, id_key: str, text, css_class: str = '') -> Markup:
    """A report's link to the post or comment `targets[id_key]` names (`suspect_post_id` or `suspect_comment_id`),
    reading `text`; plain text with `hidden_note` when it is hidden here (follow-up F6), and plain text when there is
    no usable id."""
    model, path = _CONTENT_LINKS[id_key]
    id_ = (targets or {}).get(id_key)
    if not isinstance(id_, int):
        return escape(text or '')
    if hidden_target(model, id_):
        return escape(text or '') + hidden_note()
    css = Markup(' class="{}"').format(css_class) if css_class else Markup('')
    return Markup('<a{} href="{}">{}</a>').format(css, path.format(id_), text or '')


def content_hidden(targets: dict | None, id_key: str) -> bool:
    """Whether the post or comment `targets[id_key]` names (`suspect_post_id` or `suspect_comment_id`) is hidden here,
    so a report shows its title or text but not its body (follow-up F6)."""
    id_ = (targets or {}).get(id_key)
    return isinstance(id_, int) and hidden_target(_CONTENT_LINKS[id_key][0], id_)


def instance_link(domain: str | None) -> Markup:
    """A report's link to the instance `domain`, or the name as text with `hidden_note` when it is a domain hidden
    here (follow-up F6): its page answers 404 there, and a link would name it."""
    if host_hidden_here(domain or ''):
        return escape(domain) + hidden_note()
    return Markup('<a href="/instance/{0}">{0}</a>').format(domain or '')


# a report's suspect, as the community's moderation page links it: the model, and the report's column of its id
_REPORT_SUSPECTS = ((PostReply, 'suspect_post_reply_id'), (Post, 'suspect_post_id'), (User, 'suspect_user_id'))


def report_target_hidden(report) -> bool:
    """Whether the post, comment or account `report` is about is hidden here, so its page is not linked (F6)."""
    return any(getattr(report, column) and hidden_target(model, getattr(report, column))
               for model, column in _REPORT_SUSPECTS)


# the name-built urls a notification stored before follow-up F1: (model, targets key of its id, endpoint, url argument)
_STORED_URLS = {'/c/': (Community, 'community_id', 'community.show_community_by_id', 'community_id'),
                '/u/': (User, 'suspect_user_id', 'user.show_profile_by_id', 'user_id')}


def notification_href(notification) -> str | None:
    """Where following `notification` goes, or None when it names nothing. A url is followed as stored, except the
    `/c/<name>` and `/u/<name>` a notification kept before follow-up F1: a stored name can name a namesake on another
    domain, so that goes by id, from its targets or else a lookup of the name (`stored_name_id`)."""
    url = notification.url
    if not url:
        return None
    kind = _STORED_URLS.get(url[:3])
    if kind is None:
        return url
    model, key, endpoint, argument = kind
    found = (notification.targets or {}).get(key)
    if not isinstance(found, int):
        found = stored_name_id(model, url[3:])
    return None if found is None else url_for(endpoint, **{argument: found})
