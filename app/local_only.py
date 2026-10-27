"""Local-only content: the accounts and communities of a hosted domain whose `federates` setting is off.

They never leave this server (spec docs/superpowers/specs/2026-10-19-per-domain-identity-design.md §3.4).
Because that is decided per domain, a domain's `federates` setting is locked once it owns accounts or communities.
"""
import logging
import re
from urllib.parse import unquote, urlsplit

from flask import current_app, has_request_context, request
from flask_babel import _
from flask_login import current_user
from sqlalchemy import and_, any_, bindparam, exists, false, func, not_, or_, select, text, true
from sqlalchemy.orm import aliased, object_session
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


def _feed_hidden(feed, signed_in: bool) -> bool:
    """A feed is hidden with its home domain, and with its owner (ruling N-m8: a legacy row filed under the default
    domain that an onion account owns, or the feed would tell that the account owns one). A remote feed never is."""
    if feed.ap_id is not None:
        return False
    closed, default_closed = _hidden_domains(signed_in)
    if not closed and not default_closed:
        return False   # nothing is hidden here: no owner to read (review M4)
    if _actor_hidden(feed, signed_in):
        return True
    if feed.user_id is None:
        return False
    # through the feed's own session: a task reads its rows through one of its own (D663)
    owner = (object_session(feed) or db.session).get(models.User, feed.user_id)
    return _actor_hidden(owner, signed_in)


def _content_hidden(content, signed_in: bool) -> bool:
    if not content.local_only:
        return False
    if not signed_in:
        return True
    if _actor_hidden(content.author, True) or _actor_hidden(content.community, True):
        return True
    if not isinstance(content, models.PostReply):
        return False
    if _actor_hidden(content.post.author, True):
        return True
    ancestors = _ancestors(content)
    if ancestors is None:   # fail closed (ruling, N-m3): only an onion ancestor makes a clearnet reply local-only
        return hides_anything_here()
    return any(_actor_hidden(ancestor.author, True) for ancestor in ancestors)


def _ancestors(reply) -> list | None:
    """The replies `reply` answers, from its thread's top down: the rows its `path` names but the 0 it starts with and
    its own. None when the ancestry cannot be read: no path, a path without the reply itself or its parent, or an
    ancestor that is gone."""
    ids = [id_ for id_ in reply.path or () if id_]
    if reply.id not in ids or (reply.parent_id is not None and reply.parent_id not in ids):
        return None   # a path without itself or its parent: one built under a parent that had none
    rows = [db.session.get(models.PostReply, id_) for id_ in ids]
    if any(row is None for row in rows):
        return None
    return [row for row in rows if row.id != reply.id]


def hidden_here(obj, signed_in: bool | None = None) -> bool:
    """Whether this request's storefront hides `obj` (a User, Community, Feed, Post or PostReply) from its reader, who
    is signed in when `signed_in` says so (None: the session's login, local_only_visible()). Outside a request the
    storefront is the default domain's.

    An account or community is hidden when it is local-only (its home domain does not federate) and the reader is not
    signed in, or this storefront is not its home domain. A local feed is hidden as an account is, by its home
    domain, and also when its owner is hidden (per-domain feeds Task 7, ruling N-m8). A post or reply is hidden when
    it is local-only (stamped at creation, content_local_only) and either the reader is not signed in or it belongs
    to a local-only domain that is not this storefront: its author or its community is hidden here, or, for a reply,
    its post's author (the post page's gates apply to a reply, follow-up F5) or the author of any reply above it in
    its `path`. A domain's `federates` setting is locked once it owns rows, so whatever an onion account writes, or
    is written in an onion community, is stamped local-only. So is a clearnet account's reply further down an onion
    reply, in a clearnet community under a clearnet post: it could only have been written where the onion reply is
    seen, so it goes with that reply, everywhere (review I6), or listing it would tell that its author uses the
    onion. That is the only way such a reply is stamped, so a local-only reply whose ancestry cannot be read (no
    path, one without itself or its parent, or one naming a purged reply) is hidden wherever anything is: fail closed
    (ruling, N-m3). Accepted (re-review 2, m3): where the default domain does not federate and another local-only
    domain exists too, the default domain's own unreadable local-only replies are hidden on the default domain as
    well; only archived and legacy rows can be unreadable, and telling whose they are would need the ancestry that
    cannot be read. A remote row is never hidden."""
    signed_in = _signed_in(signed_in)
    if isinstance(obj, (models.Post, models.PostReply)):
        return _content_hidden(obj, signed_in)
    if isinstance(obj, models.Feed):
        return _feed_hidden(obj, signed_in)
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


def _feed_hidden_clause(signed_in: bool):
    feed = models.Feed
    by_domain = _actor_hidden_clause(feed, signed_in)
    if hides_nothing(by_domain):   # the owner's hidden domains are the same ones
        return by_domain
    owner = aliased(models.User)   # never correlated with the query's own
    return or_(by_domain, and_(feed.ap_id.is_(None),
                               exists().where(owner.id == feed.user_id, _actor_hidden_clause(owner, signed_in))))


def hidden_here_clause(model, signed_in: bool | None = None):
    """`hidden_here` as a condition on `model` (User, Community, Feed, Post or PostReply): true for the rows hidden
    here, never NULL, so `not_()` of it keeps the rest. `false()` when nothing is hidden. `without_hidden_here` filters
    a query by it."""
    signed_in = _signed_in(signed_in)
    if model is models.Feed:
        return _feed_hidden_clause(signed_in)
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
        owners.append(_unreadable_path_clause(model))   # fail closed (ruling, N-m3)
    return and_(model.local_only.is_(True), or_(*owners))


def _unreadable_path_clause(model):
    """True for a reply whose ancestry cannot be read (`_ancestors` is None): no path, a path without the reply itself
    or its parent, a path naming no reply, or one
    naming a reply that is gone. Never NULL."""
    step = func.unnest(model.path).table_valued('id').render_derived(name='hidden_step')
    gone = aliased(models.PostReply)
    named = select(1).select_from(step).where(step.c.id != 0)
    missing = named.where(~exists().where(gone.id == step.c.id))
    has_self = func.coalesce(model.id == any_(model.path), False)
    has_parent = func.coalesce(model.parent_id == any_(model.path), False)
    return or_(model.path.is_(None), ~has_self, and_(model.parent_id.isnot(None), ~has_parent), ~named.exists(),
               missing.exists())


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


# The ids a report names its target by, and the model each names (a Report is hidden here when any one is)
_REPORT_TARGETS = (('suspect_post_reply_id', 'PostReply'), ('suspect_post_id', 'Post'), ('suspect_user_id', 'User'),
                   ('suspect_community_id', 'Community'))


def keep_for_request(rows) -> list:
    """Hold `rows` for the rest of this request and return them. The session's identity map holds its rows weakly, so a
    row read ahead for later `session.get`s (a page's reporters, the targets of its notifications) is otherwise freed
    at once and read again, one query at a time (re-review 3, N2). Outside a request, nothing is kept."""
    rows = list(rows)
    if has_request_context():
        request.__dict__.setdefault('_kept_rows', []).extend(rows)
    return rows


def prime_reporters(reports) -> None:
    """Read the accounts that filed `reports` in one query and keep them for the request, so `reporter_hidden_here`
    finds each in the session: a report list costs one query for its reporters, not one per report (m5, N2)."""
    ids = {report.reporter_id for report in reports if report.reporter_id}
    if ids:
        keep_for_request(db.session.scalars(select(models.User).where(models.User.id.in_(ids))).unique())


def reporter_hidden_here(report) -> bool:
    """Whether the account that filed `report` is hidden here, so no report view names it (ruling, F6 round 3)."""
    reporter = db.session.get(models.User, report.reporter_id) if report.reporter_id else None
    return reporter is not None and hidden_here(reporter, True)


def report_hidden_here(report) -> bool:
    """Whether the report `report` is about a row hidden here (a signed-in reader, `hidden_here`): its post, reply,
    account or community, or a conversation one of whose members is. Off the onion such a report is no one's: its
    lists leave it out and its id answers as one nobody holds (follow-up F6). `hidden_report_clause` is the query form."""
    for column, name in _REPORT_TARGETS:
        id_ = getattr(report, column)
        if id_ and (row := db.session.get(getattr(models, name), id_)) is not None and hidden_here(row, True):
            return True
    conversation = db.session.get(models.Conversation, report.suspect_conversation_id) \
        if report.suspect_conversation_id else None
    return conversation is not None and conversation_hidden_here(conversation)


def hidden_report_clause():
    """`report_hidden_here` as a condition on Report, never NULL; `false()` when nothing is hidden. Each part is an
    EXISTS correlated with the report's own row, so a list costs the hidden test of its own targets only, not of every
    hidden row (re-review 2, m5)."""
    report = models.Report
    parts = []
    for column, name in _REPORT_TARGETS:
        model = getattr(models, name)
        clause = hidden_here_clause(model, True)
        if not hides_nothing(clause):
            parts.append(select(1).where(model.id == getattr(report, column), clause).correlate(report).exists())
    conversation = hidden_conversation_clause(report.suspect_conversation_id)
    if not hides_nothing(conversation):
        parts.append(conversation)
    return or_(*parts) if parts else false()


def without_hidden_reports(query):
    """`query` over Report without the reports `report_hidden_here` hides."""
    clause = hidden_report_clause()
    return query if hides_nothing(clause) else query.filter(not_(clause))


def message_hidden_here(message) -> bool:
    """Whether the message `message` is hidden here: with its conversation, when one of its members is (the one rule
    for a message, for the gate, the finder and the batch alike, re-review 2 m4). A message whose conversation cannot
    be read (none, or gone: legacy rows) is hidden wherever anything is: fail closed, as for a reply (N-m3, N4).
    `hidden_message_clause` is the query form."""
    conversation = db.session.get(models.Conversation, message.conversation_id) if message.conversation_id else None
    if conversation is None:
        return hides_anything_here()
    return conversation_hidden_here(conversation)


def hidden_message_clause():
    """`message_hidden_here` as a condition on ChatMessage, never NULL; `false()` when nothing is hidden."""
    message = models.ChatMessage
    by_conversation = hidden_conversation_clause(message.conversation_id)
    if hides_nothing(by_conversation):
        return by_conversation
    conversation = aliased(models.Conversation)
    return or_(message.conversation_id.is_(None), ~exists().where(conversation.id == message.conversation_id),
               by_conversation)


def without_hidden_messages(query):
    """`query` over ChatMessage without the messages `message_hidden_here` hides."""
    clause = hidden_message_clause()
    return query if hides_nothing(clause) else query.filter(not_(clause))


def _row_hidden_here(row) -> bool:
    if isinstance(row, models.Conversation):
        return conversation_hidden_here(row)
    if isinstance(row, models.ChatMessage):
        return message_hidden_here(row)
    if isinstance(row, models.Report):
        return report_hidden_here(row)
    if isinstance(row, models.CommunityFlair):   # with its community (re-review 2, m8)
        community = db.session.get(models.Community, row.community_id) if row.community_id else None
        return community is not None and hidden_here(community, True)
    return hidden_here(row, True)


def found_here(model, id_):
    """The `model` row `id_` names, or None: for an id nobody holds and for a row hidden here alike, so a lookup by an
    id a caller sent (a JSON body, a form field, a query string) answers both one way (follow-up F6). `model` is User,
    Community, Feed, Post, PostReply, Conversation, ChatMessage, Report or CommunityFlair; a conversation, a message, a
    report or a flair is hidden with its member, its target or its community. Only the storefront's part of the rule
    is asked (`signed_in=True`). An id that is not a whole number in Postgres's range names nothing."""
    try:
        id_ = int(id_)
    except (TypeError, ValueError):
        return None
    if not 0 < id_ <= _PG_INT_MAX:
        return None
    row = db.session.get(model, id_)
    return None if row is None or _row_hidden_here(row) else row


def one_here(query, model):
    """`query.one()` without the rows hidden here: the same NoResultFound for a hidden row as for none. `model` is the
    query's entity: User, Community, Feed, Post, PostReply (by `hidden_here`) or ChatMessage (by its conversation)."""
    if model is models.ChatMessage:
        return without_hidden_messages(query).one()
    return without_hidden_here(query, model, True).one()


def hidden_default_domain() -> bool:
    """Whether this storefront hides the default domain's local-only rows: the default domain does not federate and
    this storefront is another."""
    return _hidden_domains(True)[1]


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
    """`hidden_here_clause` for raw SQL: a condition, never NULL, on the `model` row (User, Community, Feed, Post or
    PostReply) aliased `alias`; 'false' when nothing is hidden. The ids are integers read from the database, so they
    are written into the SQL."""
    signed_in = _signed_in(signed_in)
    if model is models.Feed:
        by_domain = _actor_hidden_sql(alias, signed_in)
        if by_domain == 'false':
            return by_domain
        return (f'({by_domain} OR ({alias}.ap_id IS NULL AND EXISTS (SELECT 1 FROM "user" hidden_owner WHERE '
                f'hidden_owner.id = {alias}.user_id AND {_actor_hidden_sql("hidden_owner", signed_in)})))')
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
        owners.append(f'({alias}.path IS NULL OR NOT COALESCE({alias}.id = ANY({alias}.path), false) '
                      f'OR ({alias}.parent_id IS NOT NULL AND NOT COALESCE({alias}.parent_id = ANY({alias}.path), false)) '
                      f'OR NOT EXISTS (SELECT 1 FROM unnest({alias}.path) AS hidden_step(id) '
                      f'WHERE hidden_step.id <> 0) OR EXISTS (SELECT 1 FROM unnest({alias}.path) AS hidden_step(id) '
                      f'WHERE hidden_step.id <> 0 AND NOT EXISTS (SELECT 1 FROM post_reply hidden_gone '
                      f'WHERE hidden_gone.id = hidden_step.id)))')   # fail closed (ruling, N-m3)
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


# --- the notification stamp (follow-up F6) ---------------------------------------------------------------------------
# `notification.local_only` says a notification may name a local-only domain's row; one without it names none, on any
# storefront, so a count of the shown notifications reads only the stamped ones (app.hidden_routes._hidden_counts).
# Migration c6e2a8f4b1d9 backfilled it with a frozen SQL copy of this rule, which it dropped once done; the parity test
# (tests/test_notification_stamp.py) holds this rule to stamping at least what that copy stamps.

_ABSOLUTE = re.compile(r'[A-Za-z][A-Za-z0-9+.-]*://([^/?#]*)(.*)', re.DOTALL)
_QUERY_OR_FRAGMENT = re.compile(r'[?#].*', re.DOTALL)
_NAMES_NOTHING = frozenset(('', '/', '/admin/reports', '/notifications', '/user/follow_requests'))
_STAMP_ID = '([0-9]{1,18})'
_STAMP_PATHS = ((re.compile(f'/post/{_STAMP_ID}(?:/comment/{_STAMP_ID})?'), ('post', 'reply')),
                (re.compile(f'/comment/{_STAMP_ID}'), ('reply',)),
                (re.compile(f'/user/{_STAMP_ID}'), ('user',)),
                (re.compile(f'/community/{_STAMP_ID}'), ('community',)),
                (re.compile(f'/chat/{_STAMP_ID}'), ('conversation',)),
                (re.compile(f'/chat/ban_from_mod/{_STAMP_ID}/{_STAMP_ID}'), ('user', 'community')))
_STAMP_TARGETS = (('post_id', 'post'), ('suspect_post_id', 'post'), ('comment_id', 'reply'),
                  ('suspect_comment_id', 'reply'), ('author_id', 'user'), ('suspect_user_id', 'user'),
                  ('community_id', 'community'), ('feed_id', 'feed'))
# An account or community (aliased u) is local-only by its home domain; one whose domain has no row is the default's
_STAMP_ACTOR = ('u.ap_id IS NULL AND (u.site_domain_id = ANY(CAST(:closed AS integer[])) OR (CAST(:default_closed AS '
                'boolean) AND (u.site_domain_id IS NULL OR u.site_domain_id <> ALL(CAST(:hosted AS integer[])))))')
_STAMP_ROWS = {
    'post': "SELECT 'post', id FROM post WHERE id = ANY(CAST(:post AS integer[])) AND local_only",
    'reply': "SELECT 'reply', id FROM post_reply WHERE id = ANY(CAST(:reply AS integer[])) AND local_only",
    'user': f'SELECT \'user\', u.id FROM "user" u WHERE u.id = ANY(CAST(:user AS integer[])) AND {_STAMP_ACTOR}',
    'community': f"SELECT 'community', u.id FROM community u WHERE u.id = ANY(CAST(:community AS integer[])) "
                 f"AND {_STAMP_ACTOR}",
    # a local feed is local-only by its home domain, or by its owner's (per-domain feeds Task 7, ruling N-m8)
    'feed': f"SELECT 'feed', f.id FROM feed f WHERE f.id = ANY(CAST(:feed AS integer[])) AND f.ap_id IS NULL AND "
            f"(EXISTS (SELECT 1 FROM feed u WHERE u.id = f.id AND {_STAMP_ACTOR}) OR "
            f"EXISTS (SELECT 1 FROM \"user\" u WHERE u.id = f.user_id AND {_STAMP_ACTOR}))",
    'conversation': f'SELECT \'conversation\', m.conversation_id FROM conversation_member m JOIN "user" u ON '
                    f'u.id = m.user_id WHERE m.conversation_id = ANY(CAST(:conversation AS integer[])) '
                    f'AND {_STAMP_ACTOR}',
}


def _stamp_plan(notification, closed_hosts: list, our_hosts: list):
    """True when `notification` names a local-only row whatever the rows hold (a local-only host, a link the rule
    cannot read), else the `(kind, id)` rows whose being local-only decides it: its author, its targets' ids and the
    ids its link names. A link on another site's host names none of our rows."""
    named = []

    def add(kind: str, value) -> None:
        if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= _PG_INT_MAX:
            named.append((kind, value))   # a larger id names no row

    add('user', notification.author_id)
    targets = notification.targets
    if isinstance(targets, dict):
        for key, kind in _STAMP_TARGETS:
            add(kind, targets.get(key))
    link = notification.url or ''
    absolute = _ABSOLUTE.match(link)
    if absolute:
        host = absolute.group(1).lower()
        if any(closed in host for closed in closed_hosts):
            return True
        if not any(ours in host for ours in our_hosts):
            return named   # another site's page names none of our rows
        link = absolute.group(2)
    path = _QUERY_OR_FRAGMENT.sub('', link, count=1)
    if path in _NAMES_NOTHING:
        return named
    for pattern, kinds in _STAMP_PATHS:
        found = pattern.fullmatch(path)
        if found:
            for kind, digits in zip(kinds, found.groups()):
                if digits is not None:
                    add(kind, int(digits))
            return named
    return True   # a link this rule cannot read (a name, another page): the per-link rule decides it


def notification_stamps(notifications, session=None) -> list[bool]:
    """Whether each of `notifications` (rows with `url`, `targets` and `author_id`) may name a local-only domain's row,
    so `notification.local_only` must be set: its author, a row its `targets` name (only integer ids count), or a row
    or host its link names is local-only, or its link is one this rule cannot read (`/u/<name>`, `/c/<name>`, a page
    it does not know), which the per-link rule (`app.hidden_routes.hidden_notifications`) judges when it is shown.

    Every one is False at once, with no query, on a site where every domain federates. Otherwise the rows they name
    are looked up in one query through `session` (db.session when None), whatever their number, and none when they
    name none."""
    notifications = list(notifications)
    if not notifications or not any_domain_closed():
        return [False] * len(notifications)
    rows = storefront.domain_rows()
    server_host = (current_app.config.get('SERVER_NAME') or '').split(':')[0].strip('.')
    our_hosts = [host.lower() for host in [row.domain for row in rows] + [storefront.default_domain().domain,
                                                                          server_host] if host]
    closed_hosts = [row.domain.lower() for row in rows if not row.federates]
    plans = [_stamp_plan(notification, closed_hosts, our_hosts) for notification in notifications]
    wanted = {}
    for plan in plans:
        for kind, id_ in plan if plan is not True else ():
            wanted.setdefault(kind, set()).add(id_)
    local = set()
    if wanted:
        closed, default_closed = _closed_domains()
        params = {kind: sorted(ids) for kind, ids in wanted.items()}
        params.update(closed=closed, default_closed=default_closed,
                      hosted=[row.id for row in rows if row.id is not None])
        query = ' UNION ALL '.join(_STAMP_ROWS[kind] for kind in sorted(wanted))
        local = {(kind, id_) for kind, id_ in (session or db.session).execute(text(query), params)}
    return [plan is True or any(row in local for row in plan) for plan in plans]


_RESTAMP = text('UPDATE notification SET local_only = true WHERE id = ANY(:ids)')


def restamp_notifications(since_id: int | None = None, since=None, batch: int = 1000) -> int:
    """Set `notification.local_only` to true where `notification_stamps` says so, for the notifications from id
    `since_id` on, or created at or after `since` (naive UTC), or for all of them when neither is given: `batch` at a
    time in id order, each batch committed. It never clears a stamp: over-including is the safe side, so a stale
    domain list or a rule regression cannot hide what is stamped (re-review 5, m3). Returns how many stamps it set,
    so a second run returns 0. `flask restamp-notifications` runs it once after the deploy of migration c6e2a8f4b1d9,
    for what code without the hook wrote meanwhile (re-review 4, M3)."""
    notification = models.Notification
    query = select(notification.id, notification.url, notification.targets, notification.author_id,
                   notification.local_only).order_by(notification.id).limit(batch)
    if since_id is not None:
        query = query.where(notification.id >= since_id)
    if since is not None:
        query = query.where(notification.created_at >= since)
    changed, last = 0, 0
    while rows := db.session.execute(query.where(notification.id > last)).all():
        missing = [row.id for row, stamp in zip(rows, notification_stamps(rows)) if stamp and not row.local_only]
        if missing:
            db.session.execute(_RESTAMP, {'ids': missing})
        db.session.commit()
        changed, last = changed + len(missing), rows[-1].id
    return changed
