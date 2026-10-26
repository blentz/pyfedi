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
from sqlalchemy import and_, any_, bindparam, exists, false, not_, or_, text, true
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import False_

import app as app_pkg
from app import db
# The modules, not the names: app.models imports this module, and app.storefront imports app.models, so whichever is
# imported first finds the other mid-import (import cycle: models)
import app.models as models
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
    """`query` over `model` (Post or PostReply) without the local-only rows this storefront hides from its reader
    (`hidden_here`): all of them from a reader who is not signed in, and on a storefront that federates from everyone
    (follow-up F6). `visible` is whether the reader is signed in when the caller has that answer (an RSS view's token,
    an API call's token user), else local_only_visible()."""
    return without_hidden_here(query, model, visible)


def without_local_only_actors(query, model, visible: bool | None = None):
    """`query` over `model` (User or Community) without the local-only accounts or communities this storefront hides
    from its reader (`hidden_here`). A logged-out visitor never sees one, as with local-only posts (owner ruling
    2026-10-21), and off its own domain nobody does (follow-up F6). `visible` is whether the reader is signed in when
    the caller has that answer (an API call's token user), else local_only_visible()."""
    return without_hidden_here(query, model, visible)


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


# --- hidden here (follow-up F6) -------------------------------------------------------------------------------------
# Owner rulings 2026-10-26: "yes, hide onion from logged-in clearnet viewers too", and for admins "No exception". A
# local-only domain's accounts, communities and content are shown only on that domain's own storefront, and there only
# to a signed-in reader (the C2 rule for a logged-out one stands). Every page and list asks one of the forms below:
# `hidden_here` for a row, `hidden_here_clause` for a query, `hidden_here_sql` for raw SQL. They agree (the parity test
# in tests/test_hidden_here.py), and each reads the domains from the cached domain_rows(), so none costs a query.

def _signed_in(signed_in: bool | None) -> bool:
    return local_only_visible() if signed_in is None else signed_in


def _hidden_domains(signed_in: bool) -> tuple[list[int], bool]:
    """The hosted domains whose local-only accounts and communities the reader may not see here, as _closed_domains
    has them: every one that does not federate for a reader who is not signed in, else every one but this storefront's
    own. On a storefront that federates that is all of them, for every reader."""
    closed, default_closed = _closed_domains()
    if not signed_in:
        return closed, default_closed
    here = storefront.current_storefront()
    if here.federates:
        return closed, default_closed
    return [id_ for id_ in closed if id_ != here.id], default_closed and not here.is_default


def _actor_hidden(actor, signed_in: bool) -> bool:
    if actor is None or actor.ap_id is not None:
        return False
    home, here = actor.home_domain(), storefront.current_storefront()
    return not home.federates and not (signed_in and not here.federates and home.id == here.id)


def _content_hidden(content, signed_in: bool) -> bool:
    if not content.local_only:
        return False
    if not signed_in:
        return True
    if _actor_hidden(content.author, True) or _actor_hidden(content.community, True):
        return True
    if not isinstance(content, models.PostReply):
        return False
    return _actor_hidden(content.post.author, True) or any(
        _actor_hidden(ancestor.author, True) for ancestor in _ancestors(content))


def _ancestors(reply) -> list:
    """The replies `reply` answers, from its thread's top down: the ids in its `path` but the 0 the path starts with
    and its own."""
    ids = [id_ for id_ in reply.path or () if id_ and id_ != reply.id]
    return [row for row in (db.session.get(models.PostReply, id_) for id_ in ids) if row is not None]


def hidden_here(obj, signed_in: bool | None = None) -> bool:
    """Whether this request's storefront hides `obj` (a User, Community, Post or PostReply) from its reader, who is
    signed in when `signed_in` says so (None: the session's login, local_only_visible()). Outside a request the
    storefront is the default domain's.

    An account or community is hidden when it is local-only (its home domain does not federate) and the reader is not
    signed in, or this storefront is not its home domain. A post or reply is hidden when it is local-only (stamped at
    creation, content_local_only) and either the reader is not signed in or it belongs to a local-only domain that
    is not this storefront: its author or its community is hidden here, or, for a reply, its post's author (the post
    page's gates apply to a reply, follow-up F5) or the author of any reply above it in its `path`. A domain's
    `federates` setting is locked once it owns rows, so whatever an onion account writes, or is written in an onion
    community, is stamped local-only. So is a clearnet account's reply further down an onion reply, in a clearnet
    community under a clearnet post: it could only have been written where the onion reply is seen, so it goes with
    that reply, everywhere (review I6), or listing it would tell that its author uses the onion. A reply stored with
    no path is judged by its own rows. A remote row is never hidden."""
    signed_in = _signed_in(signed_in)
    if isinstance(obj, (models.Post, models.PostReply)):
        return _content_hidden(obj, signed_in)
    return _actor_hidden(obj, signed_in)


def shown_here(rows, signed_in: bool | None = None) -> list:
    """`rows` without the ones `hidden_here` hides: for a list read from a cache shared by every storefront (a user's
    joined or moderated communities, a sidebar), which is narrowed when it is shown rather than when it is built."""
    return [row for row in rows if not hidden_here(row, signed_in)]


def hides_nothing(clause) -> bool:
    """Whether a `hidden_here_clause` hides no row at all (it is `false()`), so a caller can add no condition."""
    return isinstance(clause, False_)


def _actor_hidden_clause(model, signed_in: bool):
    closed, default_closed = _hidden_domains(signed_in)
    if not closed and not default_closed:
        return false()
    on_closed = and_(model.site_domain_id.isnot(None), model.site_domain_id.in_(closed)) if closed else false()
    if default_closed:
        on_closed = or_(on_closed, model.site_domain_id.is_(None))
    return and_(model.ap_id.is_(None), on_closed)


def hidden_here_clause(model, signed_in: bool | None = None):
    """`hidden_here` as a condition on `model` (User, Community, Post or PostReply): true for the rows hidden here,
    never NULL, so `not_()` of it keeps the rest. `false()` when nothing is hidden. `without_hidden_here` filters a
    query by it."""
    signed_in = _signed_in(signed_in)
    if model not in (models.Post, models.PostReply):
        return _actor_hidden_clause(model, signed_in)
    if not signed_in:
        return model.local_only.is_(True)
    author, community = aliased(models.User), aliased(models.Community)   # never correlated with the query's own
    by_author = _actor_hidden_clause(author, True)
    if hides_nothing(by_author):   # the hidden domains are the same for both: no local-only domain but this one
        return false()
    owners = [exists().where(author.id == model.user_id, by_author),
              exists().where(community.id == model.community_id, _actor_hidden_clause(community, True))]
    if model is models.PostReply:
        post, post_author = aliased(models.Post), aliased(models.User)
        owners.append(exists().where(post.id == model.post_id, post_author.id == post.user_id,
                                     _actor_hidden_clause(post_author, True)))
        ancestor, ancestor_author = aliased(models.PostReply), aliased(models.User)
        owners.append(exists().where(ancestor.id == any_(model.path), ancestor_author.id == ancestor.user_id,
                                     _actor_hidden_clause(ancestor_author, True)))
    return and_(model.local_only.is_(True), or_(*owners))


def without_hidden_here(query, model, signed_in: bool | None = None):
    """`query` over `model` without the rows `hidden_here` hides; the query itself when nothing is hidden."""
    clause = hidden_here_clause(model, signed_in)
    return query if hides_nothing(clause) else query.filter(not_(clause))


def hidden_conversation_clause(conversation_id):
    """True for the conversation `conversation_id` (a column) when one of its members is hidden here, for a signed-in
    reader (`hidden_here`): it names that member, so off the onion a conversation with an onion account is no one's
    (follow-up F6). `false()` when nothing is hidden. `without_hidden_conversations` filters a query by it."""
    member, membership = aliased(models.User), models.conversation_member.alias()   # never the query's own join
    by_member = _actor_hidden_clause(member, True)
    if hides_nothing(by_member):
        return by_member
    return exists().where(membership.c.conversation_id == conversation_id, member.id == membership.c.user_id,
                          by_member)


def conversation_hidden_here(conversation) -> bool:
    """`hidden_conversation_clause` for a Conversation row."""
    return any(_actor_hidden(member, True) for member in conversation.members)


def without_hidden_conversations(query, conversation_id):
    """`query` without the rows whose conversation (the column `conversation_id`) has a member hidden here."""
    clause = hidden_conversation_clause(conversation_id)
    return query if hides_nothing(clause) else query.filter(not_(clause))


def hides_anything_here() -> bool:
    """Whether this storefront hides any local-only domain's accounts, communities or content from a signed-in reader:
    on a storefront that federates, whenever some domain does not; on a local-only one, whenever another does not."""
    closed, default_closed = _hidden_domains(True)
    return bool(closed) or default_closed


def _actor_hidden_sql(alias: str, signed_in: bool) -> str:
    closed, default_closed = _hidden_domains(signed_in)
    if not closed and not default_closed:
        return 'false'
    ids = ', '.join(str(int(id_)) for id_ in closed)
    on_closed = f'({alias}.site_domain_id IS NOT NULL AND {alias}.site_domain_id IN ({ids}))' if closed else 'false'
    if default_closed:
        on_closed = f'({on_closed} OR {alias}.site_domain_id IS NULL)'
    return f'({alias}.ap_id IS NULL AND {on_closed})'


def hidden_here_sql(alias: str, model, signed_in: bool | None = None) -> str:
    """`hidden_here_clause` for raw SQL: a condition, never NULL, on the `model` row (User, Community, Post or
    PostReply) aliased `alias`; 'false' when nothing is hidden. The ids are integers read from the database, so they
    are written into the SQL."""
    signed_in = _signed_in(signed_in)
    if model not in (models.Post, models.PostReply):
        return _actor_hidden_sql(alias, signed_in)
    if not signed_in:
        return f'{alias}.local_only'
    author = _actor_hidden_sql('hidden_author', True)
    if author == 'false':
        return 'false'
    owners = [f'EXISTS (SELECT 1 FROM "user" hidden_author WHERE hidden_author.id = {alias}.user_id AND {author})',
              f'EXISTS (SELECT 1 FROM community hidden_community WHERE hidden_community.id = {alias}.community_id '
              f'AND {_actor_hidden_sql("hidden_community", True)})']
    if model is models.PostReply:
        owners.append(f'EXISTS (SELECT 1 FROM post hidden_post JOIN "user" hidden_post_author ON '
                      f'hidden_post_author.id = hidden_post.user_id WHERE hidden_post.id = {alias}.post_id AND '
                      f'{_actor_hidden_sql("hidden_post_author", True)})')
        owners.append(f'EXISTS (SELECT 1 FROM post_reply hidden_ancestor JOIN "user" hidden_ancestor_author ON '
                      f'hidden_ancestor_author.id = hidden_ancestor.user_id WHERE hidden_ancestor.id = '
                      f'ANY({alias}.path) AND {_actor_hidden_sql("hidden_ancestor_author", True)})')
    return f'({alias}.local_only AND ({" OR ".join(owners)}))'


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
