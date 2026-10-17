"""Every direct read of SERVER_URL / SERVER_NAME in app/ is sorted (onion storefront spec §3.4).

A federation or identity use reads the config directly and is listed in
tests/storefront_url_inventory.py's CANONICAL_USES. A presentation use calls
`storefront_url()` instead, so it is not a direct read at all. Anything else is
unsorted, and this sweep names it.

The forms walked are every one app/ uses: `current_app.config['X']`,
`app.config['X']`, `<anything>.config.get('X')` (string literal keys, single or
double quoted, f-strings included since the subscript is a node inside the
f-string), and `Config.X` attribute reads. Assignments
(the app factory's assignment to `app.config['SERVER_URL']`) are writes, not reads,
and are not counted. The enclosing function is named by its qualified name
(`Class.method`, `outer.inner`), or '<module>' at module level.
"""
import ast
import pathlib
from collections import Counter

import pytest

from tests.app_source import app_trees
from tests.storefront_url_inventory import CANONICAL_USES

ROOT = pathlib.Path(__file__).resolve().parent.parent
KEYS = frozenset({'SERVER_URL', 'SERVER_NAME'})


def _is_config(node) -> bool:
    """`<x>.config` or a bare `config` name: the object a SERVER_* key is read from."""
    return (isinstance(node, ast.Attribute) and node.attr == 'config') or \
        (isinstance(node, ast.Name) and node.id == 'config')


def is_direct_read(node) -> bool:
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load) and _is_config(node.value):
        key = node.slice
        return isinstance(key, ast.Constant) and key.value in KEYS
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'get' \
            and _is_config(node.func.value) and node.args:
        key = node.args[0]
        return isinstance(key, ast.Constant) and key.value in KEYS
    if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and node.attr in KEYS:
        return isinstance(node.value, ast.Name) and node.value.id == 'Config'
    return False


def direct_read_sites() -> list[tuple[str, str, int]]:
    """(repo-relative file, enclosing qualified function name or '<module>', line) for every direct read."""
    sites = []
    for path, _source, tree in app_trees():
        relative = str(path.relative_to(ROOT))

        def visit(node, scope):
            for child in ast.iter_child_nodes(node):
                if is_direct_read(child):
                    sites.append((relative, '.'.join(scope) or '<module>', child.lineno))
                inner = scope + [child.name] if isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else scope
                visit(child, inner)

        visit(tree, [])
    return sites


def direct_uses() -> dict[str, dict[str, int]]:
    counts = Counter((file, function) for file, function, _line in direct_read_sites())
    uses: dict[str, dict[str, int]] = {}
    for (file, function), count in counts.items():
        uses.setdefault(file, {})[function] = count
    return uses


def _mismatches(actual: dict, listed: dict) -> list[str]:
    problems = []
    for file in sorted(actual.keys() | listed.keys()):
        found, allowed = actual.get(file, {}), listed.get(file, {})
        for function in sorted(found.keys() | allowed.keys()):
            if found.get(function, 0) != allowed.get(function, 0):
                problems.append(f'{file}:{function}: {found.get(function, 0)} direct reads, '
                                f'{allowed.get(function, 0)} inventoried')
    return problems


@pytest.mark.xfail(strict=True, reason='presentation uses pending Tasks 4b/4c')
def test_every_direct_use_is_inventoried():
    problems = _mismatches(direct_uses(), CANONICAL_USES)
    assert problems == [], (
        'classify each per spec §3.4: a federation/identity use is listed in '
        'tests/storefront_url_inventory.py CANONICAL_USES; a presentation use calls storefront_url():\n'
        + '\n'.join(problems))


def test_no_inventoried_use_is_stale():
    """Holds before Task 4c too: an inventoried function never has fewer reads than listed, so a removed or
    converted canonical use cannot leave its allowance behind for a presentation use to hide in."""
    actual = direct_uses()
    stale = [f'{file}:{function}: {actual.get(file, {}).get(function, 0)} direct reads, {count} inventoried'
             for file, functions in sorted(CANONICAL_USES.items()) for function, count in sorted(functions.items())
             if actual.get(file, {}).get(function, 0) < count]
    assert stale == [], 'lower or remove these CANONICAL_USES entries:\n' + '\n'.join(stale)


def _reads(source: str) -> list[int]:
    return [node.lineno for node in ast.walk(ast.parse(source)) if is_direct_read(node)]


@pytest.mark.parametrize('source', [
    "current_app.config['SERVER_URL']",
    'app.config["SERVER_NAME"]',
    "current_app.config.get('SERVER_NAME')",
    "flask_app.config.get('SERVER_URL', '')",
    "config['SERVER_URL']",
    "Config.SERVER_NAME",
    "f\"{current_app.config['SERVER_URL']}/post/1\"",
])
def test_the_walker_sees_every_read_form(source):
    assert _reads(source) == [1]


@pytest.mark.parametrize('source', [
    "app.config['SERVER_URL']" " = 'x'",   # split: test_parallel_workers' config-write sweep reads source text
    "current_app.config['HTTP_PROTOCOL']",
    "current_app.config.get('VERSION')",
    "settings.get('SERVER_URL')",
    "row['SERVER_URL']",
    "Other.SERVER_NAME",
    "current_app.config.get()",
    "current_app.config.get(key)",
    "current_app.config[key]",
    "'SERVER_URL'",
])
def test_the_walker_ignores_what_is_not_a_read(source):
    assert _reads(source) == []


def test_the_walker_names_the_enclosing_function(monkeypatch):
    tree = ast.parse("X = config['SERVER_URL']\n"
                     "class A:\n"
                     "    def m(self):\n"
                     "        def inner():\n"
                     "            return config['SERVER_NAME']\n"
                     "        return config['SERVER_URL'], config['SERVER_URL']\n")
    monkeypatch.setattr('tests.test_storefront_url_inventory.app_trees',
                        lambda: ((ROOT / 'app' / 'x.py', '', tree),))
    assert direct_uses() == {'app/x.py': {'<module>': 1, 'A.m.inner': 1, 'A.m': 2}}


def test_mismatches_name_file_and_function():
    assert _mismatches({'app/a.py': {'f': 2, 'g': 1}}, {'app/a.py': {'f': 2}, 'app/b.py': {'h': 1}}) == [
        'app/a.py:g: 1 direct reads, 0 inventoried',
        'app/b.py:h: 0 direct reads, 1 inventoried',
    ]
