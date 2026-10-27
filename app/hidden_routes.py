"""One gate, in front of every view, for the routes that name an account, community, post or reply (follow-up F6).

Off its own domain a local-only domain's rows are hidden from every reader (`hidden_here`). A route whose arguments
name one must answer exactly as a route naming a row nobody holds, or the difference tells a clearnet reader what
exists on the onion. So the answer for both is decided here, before any view runs, and is the same `abort(404)`:

- an id argument (`ID_ARGUMENTS`) that names a hidden row, or no row at all, is 404. This is intended: a view that
  answered an id nobody holds with a flash, a redirect or an htmx fragment answers 404 while anything is hidden on its
  storefront, since a different answer for the two would be the oracle (re-review N-m9);
- a name argument that is a handle on a hidden domain (`bob@<onion>`), or a host argument that is a hidden domain,
  is 404 whether or not the name is held there, so a lookup tells nothing either;
- a bare `/u/<name>` on the default domain also finds an account by its alternate name, on any domain, so that one
  is looked up, and 404 when it is hidden here.

It asks only the storefront's part of the rule (`signed_in=True`): what a logged-out reader is refused (plan C2) is each
view's own answer. When nothing is hidden on this storefront it asks nothing at all, so a site with no local-only
domain, and the onion itself, keep every answer they had. Any string argument holding a NUL is 404 everywhere, on
every route: it names no row, and Postgres refuses it in a string; so is a host argument that names no host.

Every argument of every route is either in the table or on `NON_OBJECT_ARGUMENTS`, the arguments that name nothing a
reader could be hidden from; tests/test_hidden_routes_table.py walks the URL map and fails on any other."""
from collections import Counter
from urllib.parse import urlsplit

from flask import abort, current_app, request
from sqlalchemy import func, select
from werkzeug.exceptions import HTTPException

from app import db
from app.local_names import default_domain_user, default_domain_users
from app.local_only import conversation_hidden_here, hidden_conversation_clause, hidden_default_domain, hidden_here, \
    hidden_here_clause, hidden_message_clause, hides_anything_here, keep_for_request, message_hidden_here
from app.models import ChatMessage, Community, Conversation, Instance, Notification, Post, PostReply, User
from app.storefront import current_storefront, default_domain, domain_for_host, host_hidden_here, instance_visible_here, \
    normalise_host, on_domain

_PG_INT_MAX = 2 ** 31 - 1   # a larger id names no row, and Postgres refuses it as an integer


def _host_hidden(host: str) -> bool:
    """Whether `host`, as a path spells it (any case, a port, a trailing dot), is a domain hidden here."""
    return host_hidden_here(normalise_host(host) or host)


def _handle_hidden(name: str) -> bool:
    """Whether the name or handle `name` is on a domain hidden here: `bob@<onion>`, `!art@<onion>`."""
    return any(_host_hidden(part) for part in name.split('@')[1:])


def _user_name_hidden(name: str) -> bool:
    """`_handle_hidden`, and for a bare name on the default domain, the account it finds by its alternate name too
    (as the `/u/<actor>` views find it)."""
    if _handle_hidden(name):
        return True
    if '@' in name or not current_storefront().is_default:
        return False   # a bare name elsewhere is that storefront's own account, which is never hidden on it
    user = default_domain_user(name, allow_banned=True)
    return user is not None and hidden_here(user, True)


def _row_hidden(row) -> bool:
    return hidden_here(row, True)


# An id argument: the model it names, and whether that row is hidden here
_IDS = {
    'user_id': (User, _row_hidden), 'to': (User, _row_hidden), 'new_owner': (User, _row_hidden),
    'community_id': (Community, _row_hidden),
    'post_id': (Post, _row_hidden),
    'comment_id': (PostReply, _row_hidden), 'post_reply_id': (PostReply, _row_hidden),
    'conversation_id': (Conversation, conversation_hidden_here),   # it names its members: the onion side, off the onion
    'message_id': (ChatMessage, message_hidden_here),   # with its conversation's members (m4)
    'instance_id': (Instance, lambda row: not instance_visible_here(row.id)),
}
# A name or host argument: whether its value is hidden here
_NAMES = {
    'person': _handle_hidden, 'community': _handle_hidden, 'community_name': _handle_hidden,
    'feed_owner': _handle_hidden,
    'domain': _host_hidden, 'instance_domain': _host_hidden,
}
_HOSTS = frozenset({'domain', 'instance_domain'})
# `actor` names a different kind of row by where it is: the rule's first path segment
_ACTORS = {'/u/': _user_name_hidden, '/c/': _handle_hidden, '/community/': _handle_hidden, '/f/': _handle_hidden,
           '/feed/': _handle_hidden}

ID_ARGUMENTS = frozenset(_IDS)   # each an `int:` argument wherever it is used (tests/test_hidden_routes_table.py)
OBJECT_ARGUMENTS = ID_ARGUMENTS | frozenset(_NAMES)

# The arguments that name no account, community, post or reply, or name one that an argument above already names
# (a page of a community, a revision of its wiki page). A value is None, or the rule prefixes it is allowed under.
NON_OBJECT_ARGUMENTS = {
    # what to show, and how
    'mode': None, 'sort': None, 'type': None, 'state': None, 'view_filter': None, 'filter': None, 'year': None,
    'vote_direction': None, 'federate': None, 'emoji': None, 'collection': None, 'feed_type': None, 'result': None,
    'offer_markdown_source': None, 'whatever': None,
    # secrets and one-time values: a token, a nonce, a signature
    'token': None, 'nonce': None, 'sig': None, 'key': None, 'uuid': None,
    # rows that are not an account, community, post or reply
    'tag': None, 'tag_id': None, 'topic_id': None, 'topic_path': None, 'topic_name': None, 'flair_id': None,
    'feed_id': None, 'feed_name': None, 'feedname': None, 'feed_path': None, 'emoji_id': None, 'filter_id': None,
    'image_id': None, 'file_id': None, 'passkey_id': None, 'entry_id': None, 'category_id': None,
    'activity_id': None, 'page_id': None, 'revision_id': None, 'slug': None, 'plan': None, 'subscription': None,
    'account_id': None,   # a Stripe customer, compared with the reader's own
    'domain_id': None,   # a link domain (/d/) or, for an admin, a hosted domain's settings
    # the reader's own rows, which show a hidden target as text with a note (app/local_names.py)
    'notification_id': None, 'report_id': None,
    # an activity: the result of one sent here, for its sender, and this storefront's own logged outbound ones, which
    # never name local-only content (it is never sent)
    'id': ('/activity_result/', '/activities/'),
    'filename': ('/static/', '/bootstrap/static/'),
}

# Routes that answer every id alike, so they tell nothing, and that an onion account must still reach wherever the link
# was built: the unsubscribe links of an email, which a task builds on the default domain for every account
EXEMPT_ENDPOINTS = frozenset({'user.user_newsletter_unsubscribe', 'user.user_email_notifs_unsubscribe'})


def _junk(name: str, value) -> bool:
    """Whether a string argument, any of them, holds a NUL, which names no row and which Postgres refuses in a string,
    or a host argument names no host at all."""
    if not isinstance(value, str):
        return False
    return '\x00' in value or (name in _HOSTS and not normalise_host(value))


def actor_kind(rule: str):
    """What tells whether the `actor` of the route `rule` is hidden here, or None when the rule names no kind."""
    return next((hidden for prefix, hidden in _ACTORS.items() if rule.startswith(prefix)), None)


def _value_hidden(name: str, value, rule: str, missing: bool = True) -> bool:
    """Whether the argument `name`'s `value` in the route `rule` names a row hidden here; an id nobody holds counts
    as hidden when `missing` says so (the gate's answer for both is one)."""
    if name in _IDS:
        if not isinstance(value, int) or not 0 < value <= _PG_INT_MAX:
            return False   # no row can have it: the view answers as for any id nobody holds
        model, hidden = _IDS[name]
        row = db.session.get(model, value)
        return missing if row is None else hidden(row)
    if name == 'actor':
        return actor_kind(rule)(value)
    return _NAMES[name](value)


def refuse_hidden_route() -> None:
    """The app's before_request: 404 when a view argument names a row hidden here, or, for an id, no row at all, so
    both answers are one (see the module's docstring)."""
    args, rule = request.view_args, request.url_rule
    if not args or rule is None:
        return
    if any(_junk(name, value) for name, value in args.items()):
        abort(404)
    if request.endpoint in EXEMPT_ENDPOINTS:
        return
    named = [(name, value) for name, value in args.items() if name == 'actor' or name in OBJECT_ARGUMENTS]
    if not named or not hides_anything_here():
        return
    if any(_value_hidden(name, value, rule.rule) for name, value in named):
        abort(404)


def off_this_host(url: str | None) -> bool:
    """Whether `url` is absolute and on a host other than this request's: never redirected to from here."""
    try:
        host = urlsplit(url or '').netloc
    except ValueError:
        return True
    return bool(host) and normalise_host(host) != normalise_host(request.host)


class _Plan:
    """What one link or notification names, gathered without a query, so a page's worth resolve in one query per
    model: `hidden` when that is already known (a hidden host or handle, a hidden instance row), else the rows and the
    bare `/u/` names that decide it."""
    __slots__ = ('hidden', 'rows', 'names', 'stored')

    def __init__(self):
        self.hidden, self.rows, self.names, self.stored = False, [], [], []


def _plan_path(plan: _Plan, url: str | None) -> None:
    """Add what `url`, one of our own pages, names to `plan`, by the gate's table. An absolute URL on a hidden host is
    hidden whatever its path; a path that is no page of ours, or another site's, names nothing."""
    if not url:
        return
    try:
        parts = urlsplit(url)
    except ValueError:   # a URL nobody can parse names no page of ours
        return
    if parts.netloc and _host_hidden(parts.netloc):
        plan.hidden = True
        return
    if parts.netloc and domain_for_host(parts.netloc) is None:
        return   # another site's page: its path names none of our rows
    try:
        rule, args = current_app.url_map.bind_to_environ(request.environ).match(
            parts.path, method='GET', return_rule=True)
    except HTTPException:   # not a page of ours, or a redirect
        return
    for name, value in args.items():
        if (name != 'actor' and name not in OBJECT_ARGUMENTS) or _junk(name, value):
            continue
        if name in _IDS:
            if isinstance(value, int) and 0 < value <= _PG_INT_MAX:
                plan.rows.append((_IDS[name][0], value))
        elif name == 'actor' and actor_kind(rule.rule) is _user_name_hidden and '@' not in value \
                and current_storefront().is_default:
            plan.names.append(value)   # found by its alternate name too: looked up with the page's others
        elif (actor_kind(rule.rule) if name == 'actor' else _NAMES[name])(value):
            plan.hidden = True


# The ids a notification's `targets` may hold of what it is about
_NOTIFIED = (('post_id', Post), ('comment_id', PostReply), ('suspect_post_id', Post),
             ('suspect_comment_id', PostReply), ('community_id', Community), ('author_id', User),
             ('suspect_user_id', User))
# the name-built links a notification stored before follow-up F1: the model, and the targets key of its id
_STORED_LINKS = {'/c/': (Community, 'community_id'), '/u/': (User, 'suspect_user_id')}


def _plan_notification(notification) -> _Plan:
    """`notification`'s plan: its link as stored and as following it resolves it (notification_href), its author,
    and its targets' ids."""
    plan, targets = _Plan(), notification.targets or {}
    if getattr(notification, 'local_only', True) is False:
        return plan   # stamped as naming no local-only row: hidden nowhere (notification_names_local_only)
    _plan_path(plan, notification.url)
    stored = _STORED_LINKS.get((notification.url or '')[:3])
    if stored is not None and not isinstance(targets.get(stored[1]), int):
        plan.stored.append((stored[0], notification.url[3:]))   # notification_href finds it by its stored name
    author_id = getattr(notification, 'author_id', None)
    if isinstance(author_id, int):
        plan.rows.append((User, author_id))
    plan.rows.extend((model, targets[key]) for key, model in _NOTIFIED if isinstance(targets.get(key), int))
    return plan


def _hidden_ids(model, ids: set) -> set:
    """The ids of `ids` whose `model` rows are hidden here, in one query. The rows themselves are read in one more, so
    the page's own lookups of them (target_link) find them in the session."""
    ids = {id_ for id_ in ids if 0 < id_ <= _PG_INT_MAX}
    if not ids:
        return set()
    if model is Conversation:
        clause = hidden_conversation_clause(Conversation.id)
    elif model is ChatMessage:
        clause = hidden_message_clause()
    elif model is Instance:
        return {id_ for id_ in ids if not instance_visible_here(id_)}
    else:
        keep_for_request(db.session.scalars(select(model).where(model.id.in_(ids))).unique())   # for target_link
        clause = hidden_here_clause(model, True)   # never false(): the caller has seen that something is hidden
    return set(db.session.scalars(select(model.id).where(model.id.in_(ids), clause)))


def _stored_hidden(model, names: set) -> set:
    """Which bare names a notification stored before follow-up F1 name a row hidden here: the default domain's
    (stored_name_id), so only when the default domain itself is hidden here. A handle's host decides it already."""
    bare = {name.lower() for name in names if '@' not in name}
    if not bare or not hidden_default_domain():
        return {name for name in names if '@' in name and _handle_hidden(name)}
    column = User.user_name if model is User else Community.name
    held = set(db.session.scalars(select(func.lower(column)).where(func.lower(column).in_(bare),
                                                                    model.ap_id.is_(None),
                                                                    on_domain(default_domain(), model))))
    return {name for name in names if name.lower() in held or ('@' in name and _handle_hidden(name))}


def _resolve(plans: list) -> list:
    """Whether each of `plans` names something hidden here: one query per model for all of them together."""
    wanted = {}
    for plan in plans:
        for model, id_ in plan.rows:
            wanted.setdefault(model, set()).add(id_)
    hidden = {model: _hidden_ids(model, ids) for model, ids in wanted.items()}
    users = default_domain_users({name for plan in plans for name in plan.names})
    stored = {}
    for plan in plans:
        for model, name in plan.stored:
            stored.setdefault(model, set()).add(name)
    stored_hidden = {model: _stored_hidden(model, names) for model, names in stored.items()}
    return [plan.hidden
            or any(id_ in hidden[model] for model, id_ in plan.rows)
            or any(users[name] is not None and hidden_here(users[name], True) for name in plan.names)
            or any(name in stored_hidden[model] for model, name in plan.stored)
            for plan in plans]


def path_hidden_here(url: str | None) -> bool:
    """Whether `url`, one of our own pages (a notification's link), names a row hidden here, so it is never followed
    or linked: the redirect would spell the onion's name (follow-up F6). See `_plan_path`. A row that is gone names
    nothing hidden."""
    if not url or not hides_anything_here():
        return False
    plan = _Plan()
    _plan_path(plan, url)
    return _resolve([plan])[0]


def hidden_notifications(notifications) -> list:
    """Whether each of `notifications` is about something hidden here (follow-up F6): its link, as stored or as
    following it resolves it, names a hidden row, or its author, or its targets name a hidden post, reply, community
    or account. Its page then shows only the note (`hidden_note`). A page of them costs one query per model, whatever
    their number (N-m4), and none at all where nothing is hidden."""
    notifications = list(notifications)
    if not notifications or not hides_anything_here():
        return [False] * len(notifications)
    return _resolve([_plan_notification(notification) for notification in notifications])


def notification_hidden(notification) -> bool:
    """`hidden_notifications` for one notification."""
    return hidden_notifications([notification])[0]


def _hidden_counts(user_id: int, unread_only: bool) -> Counter:
    """The notifications of the account `user_id` hidden here (`hidden_notifications`), only the unread ones when
    `unread_only` says so, counted by `(read, notif_type, subtype)`. Nothing, and no query, where nothing is hidden.
    Elsewhere one query, on the partial index of the stamped ones, reads the columns the batch looks at, of only the
    notifications stamped as naming a local-only row; the batch costs one query per model. So the rows read are the
    onion-related ones, whatever the number of others (re-review 3, I1)."""
    if not hides_anything_here():
        return Counter()
    query = select(Notification.url, Notification.targets, Notification.author_id, Notification.read,
                   Notification.notif_type, Notification.subtype).where(Notification.user_id == user_id,
                                                                        Notification.local_only.is_(True))
    if unread_only:
        query = query.where(Notification.read.is_(False))
    rows = db.session.execute(query).all()
    return Counter((bool(row.read), row.notif_type, row.subtype)
                   for row, hidden in zip(rows, hidden_notifications(rows)) if hidden)


def hidden_unread(user_id: int) -> Counter:
    """The unread notifications of the account `user_id` hidden here, counted by `(notif_type, subtype)`."""
    return Counter({(notif_type, subtype): count
                    for (_, notif_type, subtype), count in _hidden_counts(user_id, True).items()})


def hidden_by_read_state(user_id: int) -> Counter:
    """All the notifications of the account `user_id` hidden here, counted by whether they are read, so a total
    counts none of them, read or not (round 4 security scan)."""
    counts = Counter()
    for (read, _, _), count in _hidden_counts(user_id, False).items():
        counts[read] += count
    return counts


def unread_shown(user) -> int:
    """`user.unread_notifications` less the unread ones hidden here: the count a page or a client shows, so it never
    promises notifications the list leaves out (re-review 2, m1). Computed once per request and count."""
    unread = max(user.unread_notifications or 0, 0)
    if not unread or not hides_anything_here():
        return unread
    shown = request.__dict__.setdefault('_unread_shown', {})   # this request's, which a page asks several times
    if (user.id, unread) not in shown:
        shown[(user.id, unread)] = max(0, unread - sum(hidden_unread(user.id).values()))
    return shown[(user.id, unread)]
