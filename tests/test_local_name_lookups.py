"""No bare-name lookup of a local user or community is left unscoped (plan C1, task 4).

Local names are unique per domain (spec 2026-10-19 §3.1): `bob` on hell.cloud and `bob` on the onion are two
accounts, so a lookup by name alone finds whichever row the database returns first. Every local lookup goes through
`app/local_names.py` (`split_local_name` + `local_named`). This sweep finds the spellings of a name lookup that
bypass it: `User.user_name ==`, `func.lower(User.user_name)`, `Community.name ==`, `func.lower(Community.name)`, and a
`filter_by(` that names `user_name=` or `name=` together with `ap_id=None`.

A site that legitimately looks up a name on any domain is in ALLOWED with a one-line reason: a remote-row lookup, a
lookup keyed by `ap_profile_id`, or one scoped by `ap_domain`. Never add a bare-name local lookup here to make the sweep pass; move it onto `local_named`.

Feed names stay unique across the site (plan C1 ruling), so a `filter_by` on a Feed query is not a finding.
"""
import ast

from tests.app_source import APP, app_trees

ALLOWED = {
    # (path under the repo, enclosing function): reason
    ('app/api/alpha/utils/post.py', 'get_post_list'): 'scoped by ap_domain: a community named with its instance',
    ('app/api/alpha/utils/post.py', 'get_post_list2'): 'scoped by ap_domain: a community named with its instance',
    ('app/api/alpha/views.py', 'community_view'): 'scoped by ap_domain: a community named with its instance',
    ('app/shared/community.py', 'get_comm_flair_list'): 'scoped by ap_domain: a community named with its instance',
    ('app/api/alpha/utils/user.py', 'get_user'): 'scoped by ap_domain: the remote arm; a local name goes through '
                                                 'local_named',
    ('app/user/routes.py', 'edit_user_note'): 'deliberately every domain and instance: "apply to all" notes every '
                                              'namesake, local or remote; a namesake search, not an identity lookup',
    ('app/models.py', 'UserRegistration.search_similar_names'): 'deliberately every domain and instance: look-alike '
                                                                'accounts, local or remote, for the admin approving a '
                                                                'sign-up; a namesake search, not an identity lookup',
}

USER_COLUMNS = {('User', 'user_name'), ('Community', 'name')}


def _is_name_column(node) -> bool:
    return isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and \
        (node.value.id, node.attr) in USER_COLUMNS


def _is_feed_query(receiver) -> bool:
    """Whether a `filter_by` receiver is a query of the Feed model itself: `Feed.query...` or `....query(Feed)...`.
    A join to Feed, or FeedItem and FeedMember, is not."""
    node = receiver
    while True:
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr == 'query' and node.args and \
                    isinstance(node.args[0], ast.Name) and node.args[0].id == 'Feed':
                return True
            node = node.func
        elif isinstance(node, ast.Attribute):
            node = node.value
        else:
            return isinstance(node, ast.Name) and node.id == 'Feed'


def _finding(node) -> bool:
    if isinstance(node, ast.Compare):
        sides = [node.left, *node.comparators]
        return any(isinstance(op, ast.Eq) for op in node.ops) and any(_is_name_column(side) for side in sides)
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == 'lower' and isinstance(func.value, ast.Name) and \
            func.value.id == 'func':
        return any(_is_name_column(arg) for arg in node.args)
    if isinstance(func, ast.Attribute) and func.attr == 'filter_by':
        keywords = {keyword.arg: keyword.value for keyword in node.keywords}
        by_name = 'user_name' in keywords or 'name' in keywords
        local = isinstance(keywords.get('ap_id'), ast.Constant) and keywords['ap_id'].value is None
        return by_name and local and not _is_feed_query(func.value)
    return False


def name_lookups() -> list[tuple[str, str, int]]:
    """(path, enclosing function, line) for every bare-name lookup spelling under app/."""
    found = []
    for path, _source, tree in app_trees():
        relative = str(path.relative_to(APP.parent))
        if relative == 'app/local_names.py':   # the one place a local name is looked up
            continue

        def visit(node, scope):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    inner = f'{scope}.{child.name}' if scope else child.name
                else:
                    inner = scope
                if _finding(child):
                    found.append((relative, scope or '<module>', child.lineno))
                visit(child, inner)

        visit(tree, '')
    return sorted(set(found))


def test_no_bare_name_lookup_is_left_unscoped():
    unscoped = [f'{path}:{line} in {scope}' for path, scope, line in name_lookups() if (path, scope) not in ALLOWED]
    assert unscoped == [], ('move these onto app/local_names.py (split_local_name + local_named), or, if the '
                            'lookup is not of a local name, add it to ALLOWED with its reason: ' + ', '.join(unscoped))


def test_every_allowed_site_is_still_there():
    """An entry outlives its site only by mistake: a site moved onto `local_named` takes its entry out."""
    present = {(path, scope) for path, scope, _line in name_lookups()}
    assert sorted(set(ALLOWED) - present) == []


def test_the_sweep_sees_each_spelling():
    source = '''
def lookups(x):
    User.query.filter(User.user_name == x)
    User.query.filter(func.lower(User.user_name) > x)
    Community.query.filter(x == Community.name)
    Community.query.filter(func.lower(Community.name).in_(x))
    User.query.filter_by(user_name=x, ap_id=None)
    Community.query.filter_by(name=x, ap_id=None)
    Feed.query.filter_by(name=x, ap_id=None)
    Community.query.filter_by(name=x)
    db.session.query(Feed).filter_by(name=x, ap_id=None)
    FeedItem.query.filter_by(name=x, ap_id=None)
    Community.query.join(Feed).filter_by(name=x, ap_id=None)
'''
    hits = [node.lineno for node in ast.walk(ast.parse(source)) if _finding(node)]
    assert sorted(hits) == [3, 4, 5, 6, 7, 8, 12, 13]
