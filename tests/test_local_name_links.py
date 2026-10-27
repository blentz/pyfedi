"""No link or redirect to a local user or community is built from its bare name (plan C1, final fix wave).

Local names are unique per domain (spec 2026-10-19 §3.1), and a bare name in a path means the storefront's own row:
`/u/bob` on the onion is the onion's bob. So a link the app builds from `user.user_name` or `community.name` names
the storefront's namesake whenever the row is on another domain. `link()` is the one spelling: bare on the row's own
storefront, `name@domain` elsewhere, a remote row's `ap_id`; `local_path_name` is the same for a local row, and
`stored_link()` the storefront-independent spelling a notification or report stores (bare on the default domain).

This sweep finds the spellings that bypass them, in app/ Python (by AST) and in the templates (by pattern):
- the pre-C1 spelling of `link()`, `x.ap_id if ... else x.name` (or `x.user_name`);
- `'/u/' + x.user_name`, `'/c/' + x.name` and the f-string `f'/u/{x.user_name}'`, unless `@domain` follows;
- `actor=x.name` or `actor=x.user_name` as a keyword (a `url_for` to a profile route);
- a stored target (`'..._user_name': x.user_name`, `'community_name': x.name`), which a template renders as a path;
- in a template, `/u/{{ ... x.user_name ... }}`, `'/c/' + x.name` and `url_for(..., actor=... x.name)`.

A site that builds such a path legitimately is in ALLOWED with a one-line reason: a federation id on the default
domain (per-domain federation identity is C3), or a feed (feed names stay unique across the site). Never add a link a
visitor follows here to make the sweep pass; build it from `link()`.
"""
import ast
import re

from tests.app_source import APP, app_trees

ALLOWED = {
    # (path under the repo, enclosing function): reason
    ('app/cli.py', 'register.init_db'): 'federation id of the first admin, on SERVER_URL: the default domain',
    ('app/utils.py', 'finalize_user_setup'): "federation ids, on the row's own home domain's origin (a full URL, so "
                                             'the host names the domain)',
    ('app/models.py', 'Community.profile_id'): 'federation id fallback on SERVER_URL for a row with none stored; '
                                               'per-domain federation identity is C3',
    ('app/models.py', 'Community.public_url'): 'federation URL fallback on SERVER_URL for a row with none stored; '
                                               'per-domain federation identity is C3',
    ('app/models.py', 'User.public_url'): 'federation URL fallback on SERVER_URL for a row with none stored; '
                                          'per-domain federation identity is C3',
    ('app/shared/tasks/groups.py', 'edit_community'): "the Group id an Update federates, on the community's own home "
                                                      "domain's origin (a full URL, so the host names the domain; C3)",
    ('app/community/routes.py', 'community_move'): 'the community a move request names is remote (the route refuses '
                                                   'a local one), never a bare local name',
    ('app/models.py', 'Community.flair_tag_id'): "a flair's federated id, on the community's own home domain's "
                                                 'origin (a full URL, so the host names the domain; plan C3)',
    ('app/utils.py', 'archive_post.serialize_tree'): "an archived reply's author: author_site_domain_id beside it says "
                                                     'which domain the name is on, and the author read back links by it',
}

TEMPLATES_ALLOWED = {
    # (path under the repo, line): reason
}

NAME_ATTRS = {'name', 'user_name'}
PROFILE_PREFIXES = ('/u/', '/c/')


def _is_bare_name(node) -> bool:
    return isinstance(node, ast.Attribute) and node.attr in NAME_ATTRS


def _ends_in_profile_prefix(node) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.endswith(PROFILE_PREFIXES)
    if isinstance(node, ast.JoinedStr) and node.values:
        return _ends_in_profile_prefix(node.values[-1])
    return False


def _qualified_fstring_part(values, index) -> bool:
    """Whether the f-string part after `values[index]` starts `@`: `/c/{community.name}@{community.ap_domain}`."""
    following = values[index + 1] if index + 1 < len(values) else None
    return isinstance(following, ast.Constant) and str(following.value).startswith('@')


# the targets a notification or report stores and a template renders as `/u/<value>` or `/c/<value>`
_STORED_NAME_KEY = re.compile(r'(_user_name|community_name)$')


def _bare_or_guarded(node) -> bool:
    """`x.name`, or `x.name if <guard> else <fallback>`."""
    return _is_bare_name(node) or isinstance(node, ast.IfExp) and _is_bare_name(node.body)


def _finding(node) -> bool:
    if isinstance(node, ast.IfExp):
        return isinstance(node.body, ast.Attribute) and node.body.attr == 'ap_id' and _bare_or_guarded(node.orelse)
    if isinstance(node, ast.Dict):
        return any(isinstance(key, ast.Constant) and isinstance(key.value, str) and
                   _STORED_NAME_KEY.search(key.value) and _bare_or_guarded(value)
                   for key, value in zip(node.keys, node.values))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _ends_in_profile_prefix(node.left) and _is_bare_name(node.right)
    if isinstance(node, ast.JoinedStr):
        values = node.values
        return any(isinstance(part, ast.FormattedValue) and _is_bare_name(part.value) and
                   _ends_in_profile_prefix(values[i - 1]) and not _qualified_fstring_part(values, i)
                   for i, part in enumerate(values) if i > 0)
    if isinstance(node, ast.keyword):
        return node.arg == 'actor' and _is_bare_name(node.value)
    return False


def name_links() -> list[tuple[str, str, int]]:
    """(path, enclosing function, line) for every bare-name link spelling under app/."""
    found = []
    for path, _source, tree in app_trees():
        relative = str(path.relative_to(APP.parent))

        def visit(node, scope):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    inner = f'{scope}.{child.name}' if scope else child.name
                else:
                    inner = scope
                if _finding(child):
                    found.append((relative, scope or '<module>', getattr(child, 'lineno', None) or node.lineno))
                visit(child, inner)

        visit(tree, '')
    return sorted(set(found))


_NAME = r'[\w.]*\.(?:user_name|name)\b(?!\s*\()'
TEMPLATE_PATTERNS = (
    re.compile(r'/[uc]/\{\{[^}]*?' + _NAME),                                     # /u/{{ x.user_name }}
    re.compile(r'''['"]/?[uc]/['"]\s*\+\s*\(?\s*''' + _NAME),                   # '/c/' + x.name
    re.compile(r'''url_for\(\s*['"](?!feed\.|activitypub\.feed_profile)[\w.]+['"][^)]*?'''
               r'''\bactor\s*=\s*[^,)]*?''' + _NAME),                           # url_for(.., actor=x.name)
)


def template_links(text: str) -> list[int]:
    """The line of each bare-name link spelling in a template's `text`."""
    return sorted({text.count('\n', 0, match.start()) + 1
                   for pattern in TEMPLATE_PATTERNS for match in pattern.finditer(text)})


def template_findings() -> list[tuple[str, int]]:
    found = []
    for path in sorted((APP / 'templates').rglob('*.html')):
        relative = str(path.relative_to(APP.parent))
        found += [(relative, line) for line in template_links(path.read_text(encoding='utf8'))]
    return found


def test_no_link_is_built_from_a_bare_local_name():
    bare = [f'{path}:{line} in {scope}' for path, scope, line in name_links() if (path, scope) not in ALLOWED]
    assert bare == [], ('build these from link() (stored_link() for stored targets), or, if the path is not a link '
                        'to a local row a visitor follows, add it to ALLOWED with its reason: ' + ', '.join(bare))


def test_no_template_link_is_built_from_a_bare_local_name():
    bare = [f'{path}:{line}' for path, line in template_findings() if (path, line) not in TEMPLATES_ALLOWED]
    assert bare == [], 'build these from link(), or add them to TEMPLATES_ALLOWED with a reason: ' + ', '.join(bare)


def test_every_allowed_site_is_still_there():
    """An entry outlives its site only by mistake: a site moved onto `link()` takes its entry out."""
    present = {(path, scope) for path, scope, _line in name_links()}
    assert sorted(set(ALLOWED) - present) == []
    assert sorted(set(TEMPLATES_ALLOWED) - set(template_findings())) == []


def test_the_sweep_sees_each_spelling():
    source = '''
def links(x, community, user, feed):
    url_for('user.edit_profile', actor=user.user_name)
    redirect('/c/' + community.name)
    f'{base}/u/{user.user_name}'
    actor = community.ap_id if community.ap_id is not None else community.name
    f'/c/{community.name}@{community.ap_domain}'
    redirect('/c/' + community.link())
    url_for('user.edit_profile', actor=user.link())
    community.ap_id if community.ap_id else community.title
    '/post/' + community.name
    targets = {'reporter_user_name': user.user_name}
    targets = {'suspect_user_user_name': user.ap_id if user.ap_id else user.user_name if user else ''}
    targets = {'author_user_name': user.stored_link(), 'post_title': post.title}
'''
    hits = [node.lineno for node in ast.walk(ast.parse(source)) if _finding(node)]
    assert sorted(hits) == [3, 4, 5, 6, 12, 13]


def test_the_template_sweep_sees_each_spelling():
    text = '\n'.join([
        '<a hx-post="/u/{{ user.user_name if user.is_local() else user.ap_id }}/block">',
        '<a href="/u/{{ admin.user_name }}">',
        "{{ (site_url + '/c/' + post.community.name)|tojson }}",
        "<a href=\"{{ url_for('activitypub.community_profile', actor=c.ap_id if c.ap_id is not none else c.name) }}\">",
        '<a hx-post="/u/{{ user.link() }}/block">',
        "<a href=\"{{ url_for('activitypub.feed_profile', actor=feed.ap_id if feed.ap_id else feed.name) }}\">",
        '<a href="/u/{{ notification.targets.author_user_name }}">',
        "{{ url_for('user.report_profile', actor=user.link()) }} {{ user.display_name() }}",
    ])
    assert template_links(text) == [1, 2, 3, 4]
