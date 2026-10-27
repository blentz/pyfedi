"""No federated id is built from SERVER_URL or SERVER_NAME (plan C3, task 3).

Each federating domain is a separate ActivityPub server, and what a row federates is named on its owner's domain
(`app.federation_urls`): a post or comment on its author's, an activity on its actor's, an account or community on its
own. SERVER_URL and SERVER_NAME name the default domain only, so a string built from them names the first domain whatever
the row's owner is, and a sister domain's activity would carry the first domain's name (owner ruling: nothing a sister
domain federates names the first domain). For the default domain the helpers return exactly the string SERVER_URL made.

This sweep finds, in app/ Python (by AST), a URL built on SERVER_URL or SERVER_NAME (an f-string, a `+` or a
`.replace()` whose origin is one of them, first or after `://`, directly or through a local alias such as
`server = current_app.config['SERVER_NAME']`; a handle `name@SERVER_NAME` is no URL) that is:
- assigned to a name, attribute or subscript named like an id or URL (`ap_*`, `id`, `url`, `href`, `*_id`, `*_url`)
  or `User-Agent` or `Link`;
- the value of a dict entry with such a key;
- a keyword argument with such a name (not of a query's `filter`/`filter_by`: that looks a row up, it names nothing);
- an argument of a signing or sending call (`send_post_request`, `signed_get_request` and the rest);
- the value of a `Link` header set with `headers.add`/`headers.set` (an ActivityPub alternate names an id).

A site that is meant to stay is in ALLOWED with the count of its findings under each of REASONS that applies. The entries of the
later tasks are gone: Task 4's (WebFinger, NodeInfo and the site documents), Task 5's (the instance-level keyId and the
User-Agent, which name the acting domain through `instance_actor_signing` and `utils.user_agent`) and Task 6's (a new
upload's stored URL and a local file's federated URL, on the owner's domain). Never add a federated id to make the
sweep pass; build it with `activity_id`, `owner_base` or `object_base`.
"""
import ast
import functools
import re

from tests.app_source import APP, app_trees

RENDERED = 'rendered link'
RELAYS = 'default-only by ruling: relays'
FEEDS = 'default-only by ruling: feeds'
REASONS = (RENDERED, RELAYS, FEEDS)

ALLOWED = {
    # (path under the repo, enclosing function): {reason: findings in it}
    # a notification's link, or a client API's view of a row: shown to our own users, never federated
    ('app/activitypub/util.py', 'create_post_reply'): {RENDERED: 1},
    ('app/activitypub/util.py', 'process_report'): {RENDERED: 2},
    ('app/activitypub/util.py', 'update_post_from_activity'): {RENDERED: 1},
    ('app/activitypub/util.py', 'update_post_reply_from_activity'): {RENDERED: 1},
    ('app/api/alpha/views.py', '_neutral_post'): {RENDERED: 1},
    ('app/api/alpha/views.py', 'private_message_view'): {RENDERED: 1},
    ('app/api/alpha/views.py', 'reply_removal_ack_view'): {RENDERED: 1},
    ('app/models.py', 'Post.new'): {RENDERED: 1},
    ('app/shared/post.py', 'report_post'): {RENDERED: 1},
    ('app/shared/reply.py', 'report_reply'): {RENDERED: 1},
    ('app/shared/tasks/notes.py', 'send_reply'): {RENDERED: 1},
    ('app/shared/tasks/pages.py', 'send_post'): {RENDERED: 1},
    # relays are subscribed by the default domain only (spec), so their activities are its instance actor's
    ('app/relays/subscribe.py', 'remove_relay'): {RELAYS: 1},
    ('app/relays/subscribe.py', 'send_follow_accept'): {RELAYS: 1},
    # feeds stay on the default domain (global names, C1 ruling)
    ('app/activitypub/routes.py', 'feed_followers'): {FEEDS: 1},
    ('app/activitypub/routes.py', 'feed_moderators_route'): {FEEDS: 1},
    ('app/activitypub/routes.py', 'feed_profile'): {FEEDS: 6},
    ('app/shared/feed.py', 'announce_feed_add_remove_to_subscribers'): {FEEDS: 2},
}

_CONFIG_KEYS = frozenset(('SERVER_URL', 'SERVER_NAME'))
_ID_NAME = re.compile(r'(?:ap_\w+|id|url|href|\w+_id|\w+_url|User-Agent|Link)')
_SEND_CALLS = frozenset(('send_post_request', 'post_request', 'post_request_in_background', 'signed_get_request',
                         'signed_request', 'send_to_remote_instance', 'send_to_remote_instance_fast'))
_LOOKUP_CALLS = frozenset(('filter', 'filter_by'))
_HEADER_CALLS = frozenset(('add', 'set', 'add_header'))


def _is_config_ref(node) -> bool:
    """`<x>.config['SERVER_URL']`, `config["SERVER_NAME"]`, `<x>.config.get('SERVER_NAME', ...)`."""
    def is_config(value):
        return (isinstance(value, ast.Attribute) and value.attr == 'config') or \
            (isinstance(value, ast.Name) and value.id == 'config')
    if isinstance(node, ast.Subscript):
        key = node.slice
        return isinstance(key, ast.Constant) and key.value in _CONFIG_KEYS and is_config(node.value)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'get':
        return bool(node.args) and isinstance(node.args[0], ast.Constant) and node.args[0].value in _CONFIG_KEYS \
            and is_config(node.func.value)
    return False


def _parts(node) -> list:
    """The pieces a string expression is joined from, in order: an f-string's, a `+` chain's, or the node itself."""
    if isinstance(node, ast.JoinedStr):
        return [part.value if isinstance(part, ast.FormattedValue) else part for part in node.values]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _parts(node.left) + _parts(node.right)
    return [node]


def _is_server(node, aliases) -> bool:
    return _is_config_ref(node) or (isinstance(node, ast.Name) and node.id in aliases)


def _names_a_url_on_the_server(node, aliases) -> bool:
    """Whether the string `node` builds has SERVER_URL or SERVER_NAME (or an alias) as its origin: first, or right
    after a scheme's `://` (`https://{SERVER_NAME}/...`, a User-Agent's `+https://{SERVER_NAME}`). A handle
    (`name@SERVER_NAME`) or a title naming the server is no URL. `<{SERVER_URL}/...>` (a Link header) is one."""
    parts = _parts(node)
    if len(parts) < 2 and not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and
                               node.func.attr == 'replace'):
        return False
    if isinstance(node, ast.Call):   # path.replace('app/', f'{SERVER_URL}/'): the replacement names the origin
        return any(_names_a_url_on_the_server(arg, aliases) or _is_server(arg, aliases) for arg in node.args[1:])
    for index, part in enumerate(parts):
        if _is_server(part, aliases):
            before = parts[index - 1] if index else None
            if before is None or (isinstance(before, ast.Constant) and isinstance(before.value, str) and
                                  before.value.endswith(('://', '<'))):   # `<` opens a Link header's URL
                return True
    return False


def _built(node, aliases) -> bool:
    """Whether `node` holds a URL built on SERVER_URL or SERVER_NAME (or an alias of one)."""
    return any(_names_a_url_on_the_server(inner, aliases) for inner in ast.walk(node))


def _target_name(target) -> str:
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant) and \
            isinstance(target.slice.value, str):
        return target.slice.value
    return ''


def _is_id_name(name) -> bool:
    return isinstance(name, str) and _ID_NAME.fullmatch(name) is not None


def _call_name(call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ''


def _aliases(scope) -> frozenset[str]:
    """Names a function (or, for a module, its top-level statements) binds to SERVER_URL or SERVER_NAME itself
    (`server_url = current_app.config['SERVER_URL']`), or to a URL built on one where the name is not itself an id's
    (that assignment is a finding of its own)."""
    nodes = list(ast.walk(scope)) if not isinstance(scope, ast.Module) else scope.body
    aliases = set()
    while True:
        found = set(aliases)
        for node in nodes:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name, value = node.targets[0].id, node.value
                if _is_server(value, aliases) or (_names_a_url_on_the_server(value, aliases) and
                                                  not _is_id_name(name)):
                    found.add(name)
        if found == aliases:
            return frozenset(aliases)
        aliases = found


def _findings(node, aliases) -> int:
    """How many federated ids `node` itself builds from SERVER_URL or SERVER_NAME (not counting its children)."""
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return int(node.value is not None and any(_is_id_name(_target_name(t)) for t in targets) and
                   _built(node.value, aliases))
    if isinstance(node, ast.Dict):
        return sum(1 for key, value in zip(node.keys, node.values)
                   if isinstance(key, ast.Constant) and _is_id_name(key.value) and _built(value, aliases))
    if not isinstance(node, ast.Call):
        return 0
    name = _call_name(node)
    count = 0 if name in _LOOKUP_CALLS else sum(1 for keyword in node.keywords
                                                if _is_id_name(keyword.arg) and _built(keyword.value, aliases))
    if name in _SEND_CALLS:
        count += sum(1 for arg in [*node.args, *(keyword.value for keyword in node.keywords)]
                     if _built(arg, aliases))
    if name in _HEADER_CALLS and node.args and isinstance(node.args[0], ast.Constant) and \
            _is_id_name(node.args[0].value):   # headers.add('Link', '<https://{SERVER_NAME}/...>; rel=...')
        count += sum(1 for arg in node.args[1:] if _built(arg, aliases))
    return count


@functools.cache
def server_built_ids() -> tuple[tuple[str, str, int], ...]:
    """(path, enclosing function, line) for every federated id built from SERVER_URL or SERVER_NAME under app/. The
    sweep is the same for every test here, so it runs once (it was three times, about 3 s each)."""
    found = []
    for path, source, tree in app_trees():
        relative = str(path.relative_to(APP.parent))
        if relative.startswith('app/migrations/'):   # a migration is history: it ran against the rows of its day
            continue
        if not any(key in source for key in _CONFIG_KEYS):   # every finding starts at a read of one of them
            continue

        def visit(node, scope, aliases):
            for child in ast.iter_child_nodes(node):
                inner, inner_aliases = scope, aliases
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    inner = f'{scope}.{child.name}' if scope else child.name
                    if not isinstance(child, ast.ClassDef):
                        inner_aliases = aliases | _aliases(child)
                found.extend([(relative, inner or '<module>', getattr(child, 'lineno', None) or node.lineno)] *
                             _findings(child, inner_aliases))
                visit(child, inner, inner_aliases)

        visit(tree, '', _aliases(tree))
    return tuple(sorted(found))


def _counts() -> dict[tuple[str, str], int]:
    counts = {}
    for path, scope, _line in server_built_ids():
        counts[(path, scope)] = counts.get((path, scope), 0) + 1
    return counts


def _allowed(path, scope) -> int:
    return sum(ALLOWED.get((path, scope), {}).values())


def test_no_federated_id_is_built_from_server_url_or_server_name():
    counts = _counts()
    unlisted = [f'{path} in {scope}: {count}' for (path, scope), count in sorted(counts.items())
                if count > _allowed(path, scope)]
    lines = [f'{path}:{line}' for path, scope, line in server_built_ids()
             if f'{path} in {scope}: {counts[(path, scope)]}' in unlisted]
    assert unlisted == [], ("build these on the owner's domain (federation_urls.activity_id, owner_base, object_base), "
                            'or, if the default domain is meant, add them to ALLOWED with the count under one of '
                            'REASONS: ' + ', '.join(lines))


def test_every_allowed_site_is_still_there():
    """An entry outlives its site only by mistake: a site moved onto the owner's domain takes its entry out (Tasks 4
    to 6 remove theirs), and a count above the findings left would let a new one in."""
    counts = _counts()
    assert sorted(site for site in ALLOWED if counts.get(site, 0) != _allowed(*site)) == []


def test_every_allowed_site_has_a_reason():
    assert [reason for reasons in ALLOWED.values() for reason in reasons if reason not in REASONS] == []
    assert [site for site, reasons in ALLOWED.items() if not reasons or 0 in reasons.values()] == []


def test_the_sweep_sees_each_spelling():
    source = '''
def sites(post, user, flask_app):
    vote_id = f"{current_app.config['SERVER_URL']}/activities/like/{gibberish(15)}"
    post.ap_id = current_app.config["SERVER_URL"] + '/post/1'
    activity = {'id': f"{current_app.config['SERVER_URL']}/activities/x", 'type': 'Like'}
    Notification(url=f"{current_app.config['SERVER_URL']}/post/1")
    send_post_request(inbox, body, key, f"{current_app.config['SERVER_URL']}/actor#main-key")
    server = current_app.config['SERVER_NAME']
    doc = {'id': f'https://{server}/f/x', 'inbox': f'https://{server}/f/x/inbox'}
    headers = {'User-Agent': f'PieFed; +https://{current_app.config["SERVER_NAME"]}'}
    image_url = path.replace('app/', f"{current_app.config['SERVER_URL']}/")
    name = flask_app.config.get('SERVER_NAME', 'localhost')
    post.url = f'https://{name}/x'
    User.query.filter_by(ap_profile_id=f"{current_app.config['SERVER_URL']}/u/x")
    title = f"{current_app.config['SERVER_NAME']} news"
    server_url = current_app.config['SERVER_URL']
    other = {'id': activity_id('like', user), 'to': [server_url], 'context': f'{server_url}/post/1/context'}
    note = {'replies': f'{server_url}/post/1/replies', 'url': f'{server_url}/post/1'}
    if url.startswith(current_app.config['SERVER_URL']):
        pass
    {'attributedTo': current_app.config['SERVER_URL'], 'actor_id': current_app.config['SERVER_URL']}
    ap_id = f'{ap_id}@{current_app.config["SERVER_NAME"]}'
    community_ap_id = community_ap_id + '@' + current_app.config['SERVER_NAME']
    community = f"{community}@{current_app.config['SERVER_NAME']}"
    {'url': community, 'id': 'https://' + current_app.config['SERVER_NAME'] + '/x'}
    response.headers.add('Link', f'<https://{current_app.config["SERVER_NAME"]}/post/1>; rel="alternate"')
    resp.headers.set('Link', f'<{server_url}/f/x>; rel="alternate"; type="text/html"')
    response.headers['Link'] = f'<{current_app.config["SERVER_URL"]}/u/x>; rel="alternate"'
    response.headers.add('Link', f'<{object_base(post)}/post/1>; rel="alternate"')
    response.headers.add('Vary', f'{current_app.config["SERVER_URL"]}')
'''
    tree = ast.parse(source)
    function = tree.body[0]
    aliases = _aliases(function)
    hits = []
    for node in ast.walk(tree):
        hits += [getattr(node, 'lineno', 0)] * _findings(node, aliases)
    assert sorted(hits) == [3, 4, 5, 6, 7, 9, 10, 11, 13, 18, 25, 26, 27, 28]
