"""One gate, in front of every view, for the routes that name an account, community, post or reply (follow-up F6).

Off its own domain a local-only domain's rows are hidden from every reader (`hidden_here`). A route whose arguments
name one must answer exactly as a route naming a row nobody holds, or the difference tells a clearnet reader what
exists on the onion. So the answer for both is decided here, before any view runs, and is the same `abort(404)`:

- an id argument (`OBJECT_ARGUMENTS`) that names a hidden row, or no row at all, is 404;
- a name argument that is a handle on a hidden domain (`bob@<onion>`), or a host argument that is a hidden domain,
  is 404 whether or not the name is held there, so a lookup tells nothing either;
- a bare `/u/<name>` on the default domain also finds an account by its alternate name, on any domain, so that one
  is looked up, and 404 when it is hidden here.

It asks only the storefront's part of the rule (`signed_in=True`): what a logged-out reader is refused (plan C2) is each
view's own answer. When nothing is hidden on this storefront it asks nothing at all, so a site with no local-only
domain, and the onion itself, keep every answer they had. A name argument holding a NUL is 404 everywhere: it names no
row, and Postgres refuses it in a string; so is a host argument that names no host.

Every argument of every route is either in the table or on `NON_OBJECT_ARGUMENTS`, the arguments that name nothing a
reader could be hidden from; tests/test_hidden_routes_table.py walks the URL map and fails on any other."""
from flask import abort, request

from app import db
from app.local_only import hidden_here, hides_anything_here
from app.models import ChatMessage, Community, Conversation, Instance, Post, PostReply, User
from app.storefront import current_storefront, host_hidden_here, instance_visible_here, normalise_host
from app.user.utils import user_for_actor

_PG_INT_MAX = 2 ** 31 - 1   # a larger id names no row, and Postgres refuses it as an integer


def _host_hidden(host: str) -> bool:
    """Whether `host`, as a path spells it (any case, a port, a trailing dot), is a domain hidden here."""
    return host_hidden_here(normalise_host(host) or host)


def _handle_hidden(name: str) -> bool:
    """Whether the name or handle `name` is on a domain hidden here: `bob@<onion>`, `!art@<onion>`."""
    return any(_host_hidden(part) for part in name.split('@')[1:])


def _user_name_hidden(name: str) -> bool:
    """`_handle_hidden`, and for a bare name on the default domain, the account it finds by its alternate name too
    (user_for_actor, as the `/u/<actor>` views find it)."""
    if _handle_hidden(name):
        return True
    if '@' in name or not current_storefront().is_default:
        return False   # a bare name elsewhere is that storefront's own account, which is never hidden on it
    user = user_for_actor(name, allow_banned=True)
    return user is not None and hidden_here(user, True)


def _conversation_hidden(conversation) -> bool:
    return any(hidden_here(member, True) for member in conversation.members)


def _message_hidden(message) -> bool:
    return any(user is not None and hidden_here(user, True)
               for user in (db.session.get(User, message.sender_id), db.session.get(User, message.recipient_id)))


def _row_hidden(row) -> bool:
    return hidden_here(row, True)


# An id argument: the model it names, and whether that row is hidden here
_IDS = {
    'user_id': (User, _row_hidden), 'to': (User, _row_hidden), 'new_owner': (User, _row_hidden),
    'community_id': (Community, _row_hidden),
    'post_id': (Post, _row_hidden),
    'comment_id': (PostReply, _row_hidden), 'post_reply_id': (PostReply, _row_hidden),
    'conversation_id': (Conversation, _conversation_hidden),   # it names its members: the onion side, off the onion
    'message_id': (ChatMessage, _message_hidden),
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

OBJECT_ARGUMENTS = frozenset(_IDS) | frozenset(_NAMES)

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
    """Whether a name argument holds a NUL, which names no row and which Postgres refuses in a string, or a host
    argument names no host at all."""
    if not isinstance(value, str):
        return False
    return '\x00' in value or (name in _HOSTS and not normalise_host(value))


def actor_kind(rule: str):
    """What tells whether the `actor` of the route `rule` is hidden here, or None when the rule names no kind."""
    return next((hidden for prefix, hidden in _ACTORS.items() if rule.startswith(prefix)), None)


def _value_hidden(name: str, value, rule: str) -> bool:
    if name in _IDS:
        if not isinstance(value, int) or not 0 < value <= _PG_INT_MAX:
            return False   # no row can have it: the view answers as for any id nobody holds
        model, hidden = _IDS[name]
        row = db.session.get(model, value)
        return row is None or hidden(row)
    if name == 'actor':
        return actor_kind(rule)(value)
    return _NAMES[name](value)


def refuse_hidden_route() -> None:
    """The app's before_request: 404 when a view argument names a row hidden here, or, for an id, no row at all, so
    both answers are one (see the module's docstring)."""
    args, rule = request.view_args, request.url_rule
    if not args or rule is None or request.endpoint in EXEMPT_ENDPOINTS:
        return
    named = [(name, value) for name, value in args.items() if name == 'actor' or name in OBJECT_ARGUMENTS]
    if any(_junk(name, value) for name, value in named):
        abort(404)
    if not named or not hides_anything_here():
        return
    if any(_value_hidden(name, value, rule.rule) for name, value in named):
        abort(404)
