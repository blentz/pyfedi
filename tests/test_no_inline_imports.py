"""Function-level imports in app/ are a ratchet: none without a reason, and fewer over time.

Owner ruling 2026-10-01: every import that can sit at the top of its module does.
What stays inside a function says why, on its own line or the comment line just
above it:

* ``# cycle: <reason>`` -- at the top it would close an import cycle (the module it
  names imports from this one, or reaches it, while this one is mid-import);
* ``# lazy: <reason>`` -- deferred for a reason that is not a cycle (an optional or
  slow library used on one path, a plugin that registers itself on import).

A name that would cycle as a from-import can often still be hoisted as a module
import (``import app.x.y as y_mod``, the name looked up when called); try that
before adding a ``# cycle:``. ``redis_client`` is read as ``app_pkg.redis_client``
for a different reason: create_app rebinds it, so it must be read at call time.

KEPT is the number left when the ruling was carried out. Lower it when you remove
one; it must never go up.
"""
import ast
import pathlib
from tests.app_source import app_trees

APP = pathlib.Path(__file__).resolve().parent.parent / 'app'

KEPT = 85
TAGS = ('# lazy:', '# cycle:')


def function_level_imports():
    """(path, lineno, tagged) for every import inside a function or lambda body."""
    found = []
    for path, source, tree in app_trees():
        lines = source.splitlines()

        def tagged(lineno):
            if any(tag in lines[lineno - 1] for tag in TAGS):
                return True
            previous = lines[lineno - 2].strip() if lineno >= 2 else ''
            return previous.startswith('#') and any(tag in previous for tag in TAGS)

        def visit(node, in_function):
            for child in ast.iter_child_nodes(node):
                if in_function and isinstance(child, (ast.Import, ast.ImportFrom)):
                    found.append((path.relative_to(APP.parent), child.lineno, tagged(child.lineno)))
                visit(child, in_function or isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)))

        visit(tree, False)
    return found


def test_every_function_level_import_says_why():
    untagged = [f'{path}:{lineno}' for path, lineno, tagged in function_level_imports() if not tagged]
    assert untagged == [], (
        'hoist these to the top of their module, or mark them `# cycle: <reason>` / '
        f'`# lazy: <reason>`: {untagged}')


def test_the_kept_ones_only_go_down():
    kept = sum(1 for _path, _lineno, tagged in function_level_imports() if tagged)
    assert kept <= KEPT, f'{kept} function-level imports kept, more than the {KEPT} allowed'
