"""No code treats instance row 1 alone as "hosted here" (plan C2, task 3).

Each hosted domain has its own instance row (spec 2026-10-19 §3.1): row 1 is the default domain's, and the onion and
any other domain have rows of their own. So `instance_id == 1` and `Instance.id != 1` no longer mean "ours" and "a
peer": a peer list, a delivery fan-out or a health check that skips only row 1 takes in a sister domain's row, links
it to the default domain in public and sends requests to its host. "Hosted here" is
`app.storefront.hosted_instance_ids()`; "the request's domain" is `current_instance_id()`.

This sweep finds the spellings of an instance id compared with, assigned or bound to the literal 1, in app/ Python (by
AST) and in the templates (by pattern):
- a comparison `x.instance_id == 1`, `x.instance_id != 1`, `Instance.id != 1`, `instance.id == 1`, `instance_id != 1`;
- an assignment `x.instance_id = 1`, a keyword `instance_id=1` (a constructor or `filter_by`), a parameter default
  `admin_instance_id=1` and a dict entry `'source_instance_id': 1`;
- membership of 1: `x.instance_id in (1,)`, `Instance.id.in_([1])`, `x.instance_id.not_in((1,))`;
- `db.session.get(Instance, 1)`, `Instance.query.get(1)` and `filter_by(id=1)` on an Instance query;
- SQL text naming `instance_id = 1`, `instance_id != 1`, `instance_id IN (1, ...)`, or the id of the instance table, by
  name (`instance.id = 1`, `"instance".id <> 1`) or by the alias the text gives it (`JOIN "instance" as i` ... `i.id = 1`);
- in a template, `instance_id == 1` and `instance_id != 1`.

A site where row 1 is meant is in ALLOWED with the count of its findings and a one-line reason: `row 1 is the default
domain's: <why>`, or `blocking site, Task 5` for a site a later task moves (that task takes its entry out). Never add
a "hosted here" site to make the sweep pass; test it against `hosted_instance_ids()`.
"""
import ast
import re

from tests.app_source import APP, app_trees

TASK_5 = 'blocking site, Task 5'

ALLOWED = {
    # (path under the repo, enclosing function): (findings in it, reason)
    ('app/shared/site.py', 'block_remote_instance'): (1, TASK_5),
    ('app/user/routes.py', 'user_block_instance'): (1, TASK_5),
    ('app/user/routes.py', 'user_settings_block_instance'): (1, TASK_5),
    ('app/instance/routes.py', 'instance_block'): (1, TASK_5),
    ('app/chat/routes.py', 'block_instance'): (1, TASK_5),
    ('app/post/routes.py', 'post_block_instance'): (1, TASK_5),
    ('app/post/routes.py', 'post_reply_block_instance'): (1, TASK_5),
    ('app/post/routes.py', 'comment_emoji_list'): (1, "row 1 is the default domain's: emoji are site-wide, and the "
                                                      "site's own are stored on row 1, so they sort first"),
    ('app/admin/routes.py', 'admin_emoji_add'): (1, "row 1 is the default domain's: emoji are site-wide, and an "
                                                    "admin's are stored on row 1"),
    ('app/activitypub/util.py', 'lemmy_site_data'): (3, "row 1 is the default domain's: the Lemmy site document "
                                                        "describes the default domain's site, its emoji and admins"),
    ('app/feed/routes.py', 'feed_copy'): (1, "row 1 is the default domain's: feed names stay unique across the site "
                                             '(plan C1 ruling), so a local feed is stored on row 1'),
    ('app/shared/feed.py', 'make_feed'): (1, "row 1 is the default domain's: feed names stay unique across the site "
                                             '(plan C1 ruling), so a local feed is stored on row 1'),
    ('app/utils.py', '<module>'): (1, "row 1 is the default domain's: JOINED_SERVER_SQL reads the microblogs "
                                      "community, which is the default domain's"),
    ('app/activitypub/util.py', 'find_microblogging_community'): (1, "row 1 is the default domain's: the "
                                                                     "microblogs community is the default domain's"),
    ('app/post/util.py', 'convert_archived_replies_to_tree.create_real_reply'): (
        1, "row 1 is the default domain's: the transient author of an archived reply, never saved; "
           'author_site_domain_id beside it names its domain'),
    ('app/models.py', 'Post.new'): (1, "row 1 is the default domain's: an automated report, made by the site itself "
                                       '(reporter 1, the first admin)'),
    ('app/models.py', 'PostReply.new'): (2, "row 1 is the default domain's: an automated report, made by the site "
                                            'itself (reporter 1, the first admin)'),
    ('app/admin/util.py', 'move_community_images_to_here'): (2, "row 1 is the default domain's: an admin move "
                                                                'brings a remote community and its content home to '
                                                                'the default domain, whose row 1 the community takes'),
    ('app/cli.py', 'register.lemmy_import'): (5, "row 1 is the default domain's: in a Lemmy import, Lemmy's row 1 "
                                                 'is the imported site, which becomes the default domain'),
}

TEMPLATES_ALLOWED = {
    # (path under the repo, line): reason
    ('app/templates/post/post_options.html', 204): TASK_5,
    ('app/templates/post/post_reply_options.html', 126): TASK_5,
}

_SQL_COMPARED = r'\s*(?:=|==|!=|<>)\s*1\b'
_SQL_INSTANCE_ID_ONE = re.compile(r'\b\w*instance_id(?:' + _SQL_COMPARED + r'|\s+(?:NOT\s+)?IN\s*\((?:[^)]*,)?\s*1\s*(?:,[^)]*)?\))', re.I)
_SQL_INSTANCE_ALIAS = re.compile(r'\b(?:FROM|JOIN)\s+"?instance"?\s+(?:AS\s+)?(?!ON\b|WHERE\b|JOIN\b)(\w+)', re.I)


def _sql_findings(text: str) -> int:
    """1 when SQL text compares an instance id with 1: an `instance_id` column, or the id of the instance table by
    name or by an alias the text gives it."""
    if _SQL_INSTANCE_ID_ONE.search(text):
        return 1
    tables = ['"?instance"?'] + [re.escape(alias) for alias in _SQL_INSTANCE_ALIAS.findall(text)]
    return int(bool(re.search(r'(?<![\w.])(?:' + '|'.join(tables) + r')\.id' + _SQL_COMPARED, text, re.I)))


def _is_one(node) -> bool:
    return isinstance(node, ast.Constant) and type(node.value) is int and node.value == 1


def _is_instance_id_name(name: str) -> bool:
    return name == 'instance_id' or name.endswith('_instance_id')


def _is_instance_id(node) -> bool:
    """`x.instance_id`, `x.source_instance_id`, `instance_id`, `Instance.id`, `instance.id`, `x.instance.id`,
    `x['instance_id']`."""
    if isinstance(node, ast.Subscript):
        key = node.slice
        return isinstance(key, ast.Constant) and isinstance(key.value, str) and _is_instance_id_name(key.value)
    if isinstance(node, ast.Name):
        return _is_instance_id_name(node.id)
    if not isinstance(node, ast.Attribute):
        return False
    if _is_instance_id_name(node.attr):
        return True
    if node.attr != 'id':
        return False
    owner = node.value
    owner_name = owner.id if isinstance(owner, ast.Name) else owner.attr if isinstance(owner, ast.Attribute) else ''
    return owner_name == 'Instance' or owner_name.lower().endswith('instance')


def _holds_one(node) -> bool:
    """A literal list, tuple or set with 1 in it: `[1]`, `(1,)`, `{1, 2}`."""
    return isinstance(node, (ast.List, ast.Tuple, ast.Set)) and any(_is_one(element) for element in node.elts)


def _is_instance_query(receiver) -> bool:
    """Whether a `filter_by` receiver is a query of Instance: `Instance.query...` or `....query(Instance)...`."""
    node = receiver
    while True:
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr == 'query' and node.args and \
                    isinstance(node.args[0], ast.Name) and node.args[0].id == 'Instance':
                return True
            node = node.func
        elif isinstance(node, ast.Attribute):
            node = node.value
        else:
            return isinstance(node, ast.Name) and node.id == 'Instance'


def _default_findings(node) -> int:
    """Parameters named like an instance id with the default 1."""
    arguments = node.args
    positional = arguments.posonlyargs + arguments.args
    pairs = list(zip(positional[len(positional) - len(arguments.defaults):], arguments.defaults))
    pairs += [(arg, default) for arg, default in zip(arguments.kwonlyargs, arguments.kw_defaults) if default]
    return sum(1 for arg, default in pairs if _is_instance_id_name(arg.arg) and _is_one(default))


def _findings(node) -> int:
    """How many literal-1 instance ids `node` itself holds (not counting its children)."""
    if isinstance(node, ast.Compare):
        sides = [node.left, *node.comparators]
        return int(any(_is_one(side) or _holds_one(side) for side in sides) and
                   any(_is_instance_id(side) for side in sides))
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return int(node.value is not None and _is_one(node.value) and any(_is_instance_id(t) for t in targets))
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return _default_findings(node)
    if isinstance(node, ast.Dict):
        return sum(1 for key, value in zip(node.keys, node.values)
                   if isinstance(key, ast.Constant) and isinstance(key.value, str) and
                   _is_instance_id_name(key.value) and _is_one(value))
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _sql_findings(node.value)
    if not isinstance(node, ast.Call):
        return 0
    count = sum(1 for keyword in node.keywords if keyword.arg and _is_instance_id_name(keyword.arg) and
                _is_one(keyword.value))
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in ('in_', 'not_in', 'notin_') and _is_instance_id(func.value):
        count += sum(1 for arg in node.args if _holds_one(arg))
    if isinstance(func, ast.Attribute) and func.attr == 'get' and len(node.args) == 1 and _is_one(node.args[0]) and \
            _is_instance_query(func.value):
        count += 1
    if isinstance(func, ast.Attribute) and func.attr == 'get' and len(node.args) == 2 and \
            isinstance(node.args[0], ast.Name) and node.args[0].id == 'Instance' and _is_one(node.args[1]):
        count += 1
    if isinstance(func, ast.Attribute) and func.attr == 'filter_by' and _is_instance_query(func.value):
        count += sum(1 for keyword in node.keywords if keyword.arg == 'id' and _is_one(keyword.value))
    return count


def literal_ones() -> list[tuple[str, str, int]]:
    """(path, enclosing function, line) for every literal-1 instance id under app/, one per finding."""
    found = []
    for path, _source, tree in app_trees():
        relative = str(path.relative_to(APP.parent))
        if relative.startswith('app/migrations/'):   # a migration is history: it ran against the rows of its day
            continue

        def visit(node, scope):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    inner = f'{scope}.{child.name}' if scope else child.name
                else:
                    inner = scope
                owner = inner if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else scope   # a default
                found.extend([(relative, owner or '<module>', getattr(child, 'lineno', None) or node.lineno)] *
                             _findings(child))
                visit(child, inner)

        visit(tree, '')
    return sorted(found)


def _counts() -> dict[tuple[str, str], int]:
    counts = {}
    for path, scope, _line in literal_ones():
        counts[(path, scope)] = counts.get((path, scope), 0) + 1
    return counts


_TEMPLATE_PATTERN = re.compile(r'\b(?:\w*instance_id|instance\.id)\s*(?:==|!=)\s*1\b')


def template_literal_ones(text: str) -> list[int]:
    """The line of each literal-1 instance id in a template's `text`."""
    return sorted({text.count('\n', 0, match.start()) + 1 for match in _TEMPLATE_PATTERN.finditer(text)})


def template_findings() -> list[tuple[str, int]]:
    found = []
    for path in sorted((APP / 'templates').rglob('*.html')):
        relative = str(path.relative_to(APP.parent))
        found += [(relative, line) for line in template_literal_ones(path.read_text(encoding='utf8'))]
    return found


def test_no_instance_id_is_compared_with_a_literal_one():
    counts = _counts()
    unlisted = [f'{path} in {scope}: {count}' for (path, scope), count in sorted(counts.items())
                if count > ALLOWED.get((path, scope), (0, ''))[0]]
    lines = [f'{path}:{line}' for path, scope, line in literal_ones()
             if f'{path} in {scope}: {counts[(path, scope)]}' in unlisted]
    assert unlisted == [], ('test these against hosted_instance_ids() (hosted here) or current_instance_id() (the '
                            "request's domain), or, if row 1 itself is meant, add them to ALLOWED with the count and "
                            'reason: ' + ', '.join(lines))


def test_no_template_compares_an_instance_id_with_a_literal_one():
    found = [f'{path}:{line}' for path, line in template_findings() if (path, line) not in TEMPLATES_ALLOWED]
    assert found == [], ('test these against hosted_instance_ids() (or a model helper on it), or add them to '
                         'TEMPLATES_ALLOWED with a reason: ' + ', '.join(found))


def test_every_allowed_site_is_still_there():
    """An entry outlives its site only by mistake: a site moved onto hosted_instance_ids takes its entry out, and a
    count above the findings left would let a new literal 1 in."""
    counts = _counts()
    assert sorted(site for site, (count, _reason) in ALLOWED.items() if counts.get(site, 0) != count) == []
    assert sorted(set(TEMPLATES_ALLOWED) - set(template_findings())) == []


def test_every_allowed_site_has_a_reason():
    reasons = [reason for _count, reason in ALLOWED.values()] + list(TEMPLATES_ALLOWED.values())
    assert [reason for reason in reasons
            if reason != TASK_5 and not reason.startswith("row 1 is the default domain's: ")] == []


def test_the_sweep_sees_each_spelling():
    source = '''
def sites(x, instance, member, admin_instance_id=1, *, other_instance_id=1):
    Instance.query.filter(Instance.id != 1)
    x.instance_id == 1
    1 != instance.id
    member.instance.id != 1
    instance_id != 1
    x.instance_id = 1
    User.query.filter_by(instance_id=1)
    Report(source_instance_id=1)
    {'source_instance_id': 1}
    db.session.get(Instance, 1)
    Instance.query.filter_by(id=1)
    text('SELECT 1 FROM "post" WHERE instance_id = 1')
    text('SELECT 1 FROM "post" WHERE instance_id != 1')
    text('SELECT c.id FROM community c JOIN instance i ON c.instance_id = i.id WHERE i.trusted OR i.id = 1')
    x.instance_id == 2
    x.instance_id == True
    x.community_id == 1
    User.query.filter_by(id=1)
    db.session.get(User, 1)
    {'instance_id': x}
    x.subscriptions_count = 1
    x.instance_id in hosted_instance_ids()
    text('SELECT 1 FROM "post" WHERE instance_id = 10')
    creator['instance_id'] == 1
    creator['id'] == 1
    Instance.query.get(1)
    Instance.id.in_([1])
    x.instance_id.not_in((1, 2))
    x.instance_id in (1,)
    text('SELECT 1 FROM "instance" WHERE instance.id != 1')
    text('SELECT 1 FROM "instance" WHERE "instance".id = 1')
    text('SELECT c.id FROM community c JOIN "instance" AS inst ON c.instance_id = inst.id WHERE inst.id <> 1')
    text('SELECT 1 FROM "post" WHERE instance_id IN (1)')
    text('SELECT 1 FROM "post" WHERE instance_id NOT IN (2, 1, 3)')
    User.query.get(1)
    Instance.query.get(x)
    x.instance_id.in_(hosted)
    x.community_id.in_([1])
    x.instance_id in (2, 3)
    text('SELECT c.id FROM community c JOIN "instance" i ON c.instance_id = i.id WHERE c.id = 1')
    text('SELECT 1 FROM "post" WHERE instance_id IN (10, 21)')
'''
    hits = []
    for node in ast.walk(ast.parse(source)):
        hits += [getattr(node, 'lineno', 0)] * _findings(node)
    assert sorted(hits) == [2, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 26, 28, 29, 30, 31, 32, 33, 34, 35, 36]


def test_the_template_sweep_sees_each_spelling():
    text = '\n'.join([
        '{% if post.instance_id and post.instance_id != 1 -%}',
        '{% if user.instance_id == 1 -%}',
        '{% if instance.id == 1 %}',
        '{% if post.instance_id in hosted_instance_ids %}',
        '{% if post.instance_id == 10 %}',
        '{% if post.community_id == 1 %}',
    ])
    assert template_literal_ones(text) == [1, 2, 3]
