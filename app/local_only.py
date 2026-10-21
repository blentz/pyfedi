"""Local-only content: the accounts and communities of a hosted domain whose `federates` setting is off.

They never leave this server (spec docs/superpowers/specs/2026-10-19-per-domain-identity-design.md §3.4).
Because that is decided per domain, a domain's `federates` setting is locked once it owns accounts or communities.
"""
import logging
import re
from urllib.parse import unquote, urlsplit

from flask import current_app
from flask_babel import _
from flask_login import current_user
from sqlalchemy import and_, bindparam, not_, or_, text, true

import app as app_pkg
from app import db
# The module, not the names: app.models imports this module, and app.storefront imports app.models, so whichever is
# imported first finds the other mid-import (import cycle: models)
import app.storefront as storefront

logger = logging.getLogger(__name__)

# A domain owns the local rows stamped with its id. The default domain also owns the local rows no domain was
# stamped on (legacy rows); remote rows carry an ap_id and never count.
_OWNERS = text("""
    SELECT EXISTS (
        SELECT 1 FROM "user" u
        WHERE u.site_domain_id = :id
           OR (u.site_domain_id IS NULL AND u.ap_id IS NULL
               AND EXISTS (SELECT 1 FROM site_domain d WHERE d.id = :id AND d.is_default))
    ) OR EXISTS (
        SELECT 1 FROM community c
        WHERE c.site_domain_id = :id
           OR (c.site_domain_id IS NULL AND c.ap_id IS NULL
               AND EXISTS (SELECT 1 FROM site_domain d WHERE d.id = :id AND d.is_default))
    )
""")


def domain_has_owners(site_domain_id: int) -> bool:
    """True when the domain owns at least one local account or community."""
    with db.session.no_autoflush:
        return bool(db.session.execute(_OWNERS, {'id': site_domain_id}).scalar())


def user_local_only(user) -> bool:
    """True for a local account whose home domain does not federate. A remote account never is."""
    return user.ap_id is None and not user.home_domain().federates


def community_local_only(community) -> bool:
    """True for a local community whose home domain does not federate. A remote community never is."""
    return community.ap_id is None and not community.home_domain().federates


def refuses_remote_contact(sender, other) -> bool:
    """True when `sender` is local-only and `other` is a remote account: spec 3.4, it cannot follow or message one.
    The outbound guard would refuse the Follow or the message, so it is refused before anything is stored."""
    return not other.is_local() and user_local_only(sender)


REMOTE_CONTACT_REFUSAL = 'local_only_remote_contact'   # the Exception a shared action raises for it


def remote_contact_refusal_message() -> str:
    return _('Your account is not visible to other servers, so it cannot follow or message people there.')


def content_local_only(author, community, *parents) -> bool:
    """Whether what `author` makes in `community` (answering `parents`: a reply's direct parent and the thread's
    post, None where there is none) stays on this server.

    The author, the community or any of the parents being local-only is enough; a remote author's content is never
    stamped.
    """
    if author.ap_id is not None:
        return False
    return (user_local_only(author) or community_local_only(community) or
            any(parent is not None and parent.local_only for parent in parents))


def local_only_visible() -> bool:
    """True when this request's reader may see local-only content on a web page: a logged-in user. An RSS token does
    not count here, since feed URLs that carry one are easily passed on; RSS views ask local_only_visible_rss()."""
    return current_user.is_authenticated


def local_only_visible_rss() -> bool:
    """local_only_visible for an RSS view: the account the reader's `?token=` names. The session is ignored, so a
    cached feed depends only on its key, which holds the token."""
    # app.utils at call time: it imports app.models, which imports this module (import cycle: models)
    return app_pkg.utils.rss_token_user() is not None


def without_local_only(query, model, visible: bool | None = None):
    """`query` over `model` (Post or PostReply) without the local-only rows, unless the reader may see them.
    `visible` is that answer when the caller has it (an RSS view's token, an API call's token user), else
    local_only_visible()."""
    if visible is None:
        visible = local_only_visible()
    return query if visible else query.filter(model.local_only.is_(False))


def without_local_only_actors(query, model):
    """`query` over `model` (User or Community) without the local-only accounts or communities, unless the reader may
    see them. A logged-out visitor never sees one, as with local-only posts (owner ruling 2026-10-21)."""
    return query if local_only_visible() else query.filter(federating_clause(model))


def _closed_domains() -> tuple[list[int], bool]:
    """The ids of the hosted domains that do not federate, and whether the default domain is one of them (it owns the
    local rows no domain was stamped on). From the cached domain_rows(): no query."""
    closed = [row.id for row in storefront.domain_rows() if not row.federates and row.id is not None]
    return closed, not storefront.default_domain().federates


def any_domain_closed() -> bool:
    """True when some hosted domain does not federate, so there can be local-only rows at all."""
    closed, default_closed = _closed_domains()
    return bool(closed) or default_closed


def federating_clause(model):
    """A filter that keeps the rows of `model` (User or Community) that are not local-only: remote ones, and local
    ones whose home domain federates. A row that is not local-only is never dropped for a NULL."""
    closed, default_closed = _closed_domains()
    if not closed and not default_closed:
        return true()
    on_closed = and_(model.site_domain_id.isnot(None), model.site_domain_id.in_(closed))
    if default_closed:
        on_closed = or_(on_closed, model.site_domain_id.is_(None))
    return or_(model.ap_id.isnot(None), not_(on_closed))


def federating_sql(alias: str) -> str:
    """federating_clause for a raw SQL query: a condition on the "user" or community row aliased `alias`. The ids
    are integers read from the database, so they are written into the SQL."""
    closed, default_closed = _closed_domains()
    if not closed and not default_closed:
        return 'true'
    ids = ', '.join(str(int(id_)) for id_ in closed)
    on_closed = f'({alias}.site_domain_id IS NOT NULL AND {alias}.site_domain_id IN ({ids}))' if closed else 'false'
    if default_closed:
        on_closed = f'({on_closed} OR {alias}.site_domain_id IS NULL)'
    return f'({alias}.ap_id IS NOT NULL OR NOT {on_closed})'


def federated_mentions(users: list) -> list:
    """`users` without the local-only ones. A Mention of a local-only account goes out as the plain text it was typed
    as: no Mention tag, no addressee and no delivery, so the activity naming them is still sent."""
    return [user for user in users if not user_local_only(user)]


# The fields of an activity, or of any object inside it, that name actors and objects (quotes and quote
# authorizations included)
_GUARDED_KEYS = frozenset(('id', 'actor', 'object', 'target', 'inReplyTo', 'audience', 'to', 'cc', 'attributedTo',
                           'quote', 'quoteUrl', 'quoteUri', '_misskey_quote', 'interactionTarget', 'interactingObject',
                           'instrument'))
# Our own URLs of a post or a comment: /post/<id>[/...], /post/<id>/comment/<id>[/...], /comment/<id>[/...] and the
# nice URL /c/<community>/p/<id>/<slug>
_OBJECT_PATH = re.compile(r'^/(?:post/(?P<post>\d+)(?:/comment/(?P<nested>\d+))?|comment/(?P<comment>\d+)|'
                          r'c/[^/]+/p/(?P<nice>\d+))(?:/|$)')
_LOCAL_ONLY_POSTS = text('SELECT 1 FROM "post" WHERE id IN :ids AND local_only LIMIT 1').bindparams(
    bindparam('ids', expanding=True))
_LOCAL_ONLY_REPLIES = text('SELECT 1 FROM "post_reply" WHERE id IN :ids AND local_only LIMIT 1').bindparams(
    bindparam('ids', expanding=True))
_PG_INT_MAX = 2 ** 31 - 1


def _guarded_urls(value, guarded: bool):
    """Every string under a guarded key, anywhere in `value` (dicts and lists walked once)."""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield from _guarded_urls(inner, key in _GUARDED_KEYS)
    elif isinstance(value, list):
        for inner in value:
            yield from _guarded_urls(inner, guarded)
    elif guarded and isinstance(value, str):
        yield value


def outbound_refusal(body: dict, session=None) -> str | None:
    """Why the activity `body` must not leave this server, or None when it may.

    It may not when a URL in a guarded field (_GUARDED_KEYS, at any depth) is on a hosted domain that does not
    federate, or is one of our own post or comment URLs whose row is stamped local-only. Domains come from the cached
    domain_rows(), so a body naming nothing of ours costs no query; the post and comment ids it names cost one query
    per table, read through `session` (db.session when None).
    """
    federates = {storefront.normalise_host(row.domain): row.federates for row in storefront.domain_rows()}
    federates.setdefault(storefront.normalise_host(current_app.config['SERVER_NAME']), True)
    post_ids, reply_ids = set(), set()
    for url in _guarded_urls(body, False):
        try:
            parts = urlsplit(url)
            host = storefront.normalise_host(parts.hostname or '')
        except ValueError:   # fail closed: a URL nobody can parse names a host nobody can check
            return 'it names a URL that cannot be parsed'
        if host not in federates:
            continue
        if not federates[host]:
            return 'it names a domain that does not federate'
        found = _OBJECT_PATH.match(unquote(parts.path))   # as the router reads it
        if found:
            for name, ids in (('post', post_ids), ('nice', post_ids), ('nested', reply_ids), ('comment', reply_ids)):
                if found.group(name) and int(found.group(name)) <= _PG_INT_MAX:
                    ids.add(int(found.group(name)))
    session = session or db.session
    with session.no_autoflush:
        if post_ids and session.execute(_LOCAL_ONLY_POSTS, {'ids': list(post_ids)}).first():
            return 'it names a local-only post'
        if reply_ids and session.execute(_LOCAL_ONLY_REPLIES, {'ids': list(reply_ids)}).first():
            return 'it names a local-only comment'
    return None


def refused_outbound(body, uri, session=None) -> bool:
    """True, after one DEBUG line naming no account, when `body` must not be sent to `uri` (outbound_refusal)."""
    reason = outbound_refusal(body, session) if isinstance(body, dict) else None
    if reason:
        logger.debug('local-only activity not sent to %s: %s', storefront.normalise_host(
            urlsplit(uri or '').hostname or ''), reason)
    return bool(reason)
