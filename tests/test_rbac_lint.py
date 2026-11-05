"""RBAC build step 6: lint tests that keep the pre-RBAC checks from coming back.

Spec docs/superpowers/specs/2026-11-03-rbac-design.md, Tests and Schema (RB16):

* no `is_staff` / `is_admin_or_staff` anywhere in `app/`;
* no role constants (`ROLE_STAFF`, `ROLE_ADMIN`, `ROLE_STAFF_NAME`, `ROLE_ADMIN_NAME`, `EDITABLE_ROLE_IDS`), and no
  role found by its name or its id;
* `anywhere=True` only in navigation code, never in routes, `shared/` or the API;
* no scopable permission checked without the object it is about (`on=`) outside navigation code.

Each test walks the source with `ast` for Python (a regex cannot tell a call from the same words in a docstring) and
with a parenthesis-balanced scan for templates. What a later step removes is listed here, by file and count, and
the test fails both when a new one appears and when one goes without being taken off the list. Step 8 emptied the
lists of what it was to scope; what is left is instance-wide on purpose.
"""
import ast
import pathlib
import re
from collections import Counter

from app.permissions import SCOPABLE_PERMISSIONS

APP_ROOT = pathlib.Path(__file__).resolve().parent.parent / 'app'
TEMPLATES = APP_ROOT / 'templates'

# Navigation: the menus ask "does this user hold it anywhere", which is what a link should follow.
NAV_TEMPLATES = {'templates/base.html', 'templates/admin/_nav.html', 'templates/admin/_tabbed_nav.html'}


def rel(path):
    return str(path.relative_to(APP_ROOT))


def python_files():
    return sorted(APP_ROOT.rglob('*.py'))


def template_files():
    return sorted(TEMPLATES.rglob('*.html'))


def parsed(path):
    return ast.parse(path.read_text(), filename=str(path))


def enclosing_functions(tree):
    """Map each node to the name of the innermost function or method it is in, as 'Class.method' or 'function'."""
    owner = {}

    def visit(node, name):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inner = f'{name}.{child.name}' if name and not name.islower() else child.name
                visit(child, inner)
            elif isinstance(child, ast.ClassDef):
                visit(child, child.name)
            else:
                owner[child] = name
                visit(child, name)
    visit(tree, None)
    return owner


def call_name(node):
    return getattr(node.func, 'id', None) or getattr(node.func, 'attr', None)


def template_calls(text, function):
    """Each `function(...)` call in a template, as (line, the argument text with its parentheses)."""
    for match in re.finditer(rf'\b{function}\(', text):
        depth, start = 0, match.end() - 1
        for end in range(start, len(text)):
            depth += {'(': 1, ')': -1}.get(text[end], 0)
            if depth == 0:
                break
        yield text.count('\n', 0, match.start()) + 1, text[start:end + 1]


# --------------------------------------------------------------------------
# is_staff / is_admin_or_staff
# --------------------------------------------------------------------------

GONE = {'is_staff', 'is_admin_or_staff'}


def test_nothing_asks_is_staff_or_is_admin_or_staff():
    """Staff is an ordinary role: what it may do is its permissions, asked with `user_access`."""
    found = []
    for path in python_files():
        for node in ast.walk(parsed(path)):
            named = (node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name)
                     else node.name if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else None)
            if named in GONE:
                found.append(f'{rel(path)}:{node.lineno}')
    for path in template_files():
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r'\bis_(admin_or_)?staff\b', line):
                found.append(f'{rel(path)}:{number}')
    assert found == []


# --------------------------------------------------------------------------
# Role constants, and roles found by name or id
# --------------------------------------------------------------------------

ROLE_CONSTANTS = {'ROLE_STAFF', 'ROLE_ADMIN', 'ROLE_STAFF_NAME', 'ROLE_ADMIN_NAME', 'EDITABLE_ROLE_IDS'}


def test_no_role_constant_is_left():
    found = [f'{rel(path)}:{node.lineno}' for path in python_files() for node in ast.walk(parsed(path))
             if (isinstance(node, ast.Name) and node.id in ROLE_CONSTANTS)
             or (isinstance(node, ast.alias) and node.name in ROLE_CONSTANTS)]
    assert found == []




def role_lookups(path):
    """Each place `path` finds a role by name or id, as the function it is in.

    By name: `Role.name == ...` (or `!=`, `in`), `Role.query.filter_by(name=...)`, or a 'Admin' / 'Staff' literal
    compared with anything. By id: `db.session.get(Role, 4)`, `Role.query.get(4)`, or `Role.id == 4`; an id the
    admin chose (`db.session.get(Role, form.role.data)`) is not a fixed role. Creating a role (`Role(name=...)`) is
    not finding one.
    """
    tree = parsed(path)
    owner = enclosing_functions(tree)
    found = []

    def is_role(node, attribute):
        return (isinstance(node, ast.Attribute) and node.attr == attribute and isinstance(node.value, ast.Name)
                and node.value.id == 'Role')

    def is_number(node):
        return isinstance(node, ast.Constant) and isinstance(node.value, int)

    def queries_role(node):
        """`Role.query.<x>(...)`."""
        return (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == 'query' and isinstance(node.func.value.value, ast.Name)
                and node.func.value.value.id == 'Role')

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            if any(is_role(o, 'name') for o in operands) \
                    or any(isinstance(o, ast.Constant) and o.value in ('Admin', 'Staff') for o in operands) \
                    or (any(is_role(o, 'id') for o in operands) and any(is_number(o) for o in operands)):
                found.append(owner.get(node))
        elif isinstance(node, ast.Call):
            if queries_role(node) and ((node.func.attr == 'get' and node.args and is_number(node.args[0]))
                                       or any(k.arg == 'name' for k in node.keywords)):
                found.append(owner.get(node))
            elif call_name(node) == 'get' and len(node.args) == 2 and isinstance(node.args[0], ast.Name) \
                    and node.args[0].id == 'Role' and is_number(node.args[1]):
                found.append(owner.get(node))
    return found


def test_no_role_is_found_by_its_name_or_id():
    """RB16: Admin is the built-in role, found by `Role.built_in`; every other role is whatever an admin made it,
    so a name or an id says nothing about what it may do."""
    found = {}
    for path in python_files():
        where = role_lookups(path)
        if where:
            found[f'app/{rel(path)}'] = set(where)
    assert found == {}


def test_the_role_lookup_lint_catches_each_form(tmp_path):
    source = tmp_path / 'sample.py'
    source.write_text('\n'.join([
        'def by_name(): return Role.query.filter_by(name=x).first()',
        'def by_compare(): return User.roles.any(Role.name == x)',
        'def by_literal(r): return r.name == "Staff"',
        'def by_get(): return db.session.get(Role, 4)',
        'def by_query_get(): return Role.query.get(3)',
        'def by_id(): return Role.query.filter(Role.id == 4)',
        'def creating(): return Role(name="Staff")',
        'def by_flag(): return Role.query.filter(Role.built_in.is_(True))',
        'def chosen(): return db.session.get(Role, form.role.data)',
    ]))
    assert sorted(role_lookups(source)) == ['by_compare', 'by_get', 'by_id', 'by_literal', 'by_name',
                                            'by_query_get']


# --------------------------------------------------------------------------
# anywhere=True is for menus
# --------------------------------------------------------------------------

def authorising(path):
    """Code that authorises an action: routes, the shared actions both the web and the API call, and the API."""
    name = rel(path)
    return path.name == 'routes.py' or name.startswith(('shared/', 'api/'))


def test_anywhere_is_asked_only_by_menus():
    """`anywhere=True` passes a holder scoped to any domain. A link may follow that; an action may not, or a holder
    on X could act on Y."""
    found = [f'{rel(path)}:{node.lineno}' for path in python_files() if authorising(path)
             for node in ast.walk(parsed(path))
             if isinstance(node, ast.Call) and call_name(node) == 'user_access'
             and any(k.arg == 'anywhere' for k in node.keywords)]
    assert found == []


# --------------------------------------------------------------------------
# A scopable permission names what it is about
# --------------------------------------------------------------------------

# About the whole instance on purpose: what is acted on has no home domain, so no scoped grant could reach it.
UNSCOPED_ON_PURPOSE = {
    'community/routes.py': {'administer all communities': 1},   # fixup_from_remote: a remote community
    'instance/routes.py': {'administer all users': 1},          # a remote instance's people
    # a conversation, and so its reports: reporting one, and listing and resolving them
    'api/alpha/utils/private_message.py': {'administer all users': 5},
}


def unscoped_checks():
    """Each `user_access` of a scopable permission that names no object, as {file: Counter(permission)}."""
    found = {}
    for path in python_files():
        for node in ast.walk(parsed(path)):
            if isinstance(node, ast.Call) and call_name(node) == 'user_access' and node.args \
                    and isinstance(node.args[0], ast.Constant) and node.args[0].value in SCOPABLE_PERMISSIONS \
                    and len(node.args) < 3 and not any(k.arg in ('on', 'anywhere') for k in node.keywords):
                found.setdefault(rel(path), Counter())[node.args[0].value] += 1
    for path in template_files():
        if rel(path) in NAV_TEMPLATES:
            continue
        for _, arguments in template_calls(path.read_text(), 'user_access'):
            permission = re.match(r"\(\s*'([^']+)'", arguments)
            if permission and permission.group(1) in SCOPABLE_PERMISSIONS \
                    and not re.search(r'\b(on|anywhere)\s*=', arguments):
                found.setdefault(rel(path), Counter())[permission.group(1)] += 1
    return found


def test_a_scopable_permission_names_what_it_is_about():
    """`on=None` asks about the whole instance, so a holder scoped to a domain is refused even on that domain's own
    objects. Outside the menus, a scopable permission is asked about the object acted on."""
    assert unscoped_checks() == {path: Counter(counts) for path, counts in UNSCOPED_ON_PURPOSE.items()}


def test_the_template_scan_reads_nested_parentheses():
    text = "{% if user_access('ban users', current_user.get_id(), on=user) %}\n{% if user_access('ban users', x()) %}"
    assert [(n, '=' in a) for n, a in template_calls(text, 'user_access')] == [(1, True), (2, False)]


# --------------------------------------------------------------------------
# A route a scoped holder may open filters what it shows, or asks about what it acts on
# --------------------------------------------------------------------------

def held_anywhere_routes_that_neither_filter_nor_name_an_object():
    """Each function under `@permission_held_anywhere(...)` must call `scope_for` (a list, filtered to the holder's
    domains), `creatable_domains` (the add-user form, RB13), or ask `user_access(..., on=...)` / `may_act_on_user` (one
    object, on its home domain). Otherwise a
    holder on domain X would see, or act on, everything."""
    found = []
    for path in python_files():
        for node in ast.walk(parsed(path)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(isinstance(d, ast.Call) and call_name(d) == 'permission_held_anywhere'
                       for d in node.decorator_list):
                continue
            calls = [n for n in ast.walk(node) if isinstance(n, ast.Call)]
            if not any(call_name(c) in ('scope_for', 'may_act_on_user', 'creatable_domains') or
                       (call_name(c) == 'user_access' and any(k.arg == 'on' for k in c.keywords)) for c in calls):
                found.append(f'{rel(path)}:{node.name}')
    return found


def test_every_route_a_scoped_holder_may_open_filters_or_names_its_object():
    assert held_anywhere_routes_that_neither_filter_nor_name_an_object() == []


def test_the_held_anywhere_lint_finds_a_route_that_does_neither(tmp_path, monkeypatch):
    sample = tmp_path / 'app'
    sample.mkdir()
    (sample / 'routes.py').write_text('\n'.join([
        "@permission_held_anywhere('administer all users')",
        'def lists_everything(): return User.query.all()',
        "@permission_held_anywhere('administer all users')",
        "def filtered(): return User.query.filter(scope_for('administer all users').clause(User.site_domain_id))",
        "@permission_held_anywhere('administer all users')",
        "def one(user): return user_access('administer all users', 1, on=user)",
    ]))
    import tests.test_rbac_lint as lint
    monkeypatch.setattr(lint, 'APP_ROOT', sample)
    assert lint.held_anywhere_routes_that_neither_filter_nor_name_an_object() == ['routes.py:lists_everything']
