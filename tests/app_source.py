"""The source of every module under app/, read and parsed once per worker.

Many tests are source sweeps: they walk app/**/*.py and ast.parse each file to
assert that some pattern is absent or present. A full parse of app/ measured
about 1.9 s (plus the file reads through the container bind mount), and every
sweep re-did it, so each cost about 3 s and a suite run parsed app/ some 25
times. The tree does not change while the suite runs, so it is read and parsed
here once per worker process and shared.

Callers must treat the trees as read-only: they are shared by every test in the
worker, so a NodeTransformer or an assignment to a node would leak into the next
sweep. A sweep that needs to mutate a tree parses its own copy.
"""
import ast
import functools
import pathlib

APP = pathlib.Path(__file__).resolve().parent.parent / 'app'


@functools.cache
def app_files() -> tuple[tuple[pathlib.Path, str], ...]:
    """(path, source text) for every *.py under app/ except __pycache__, sorted by path."""
    return tuple((path, path.read_text(encoding='utf8'))
                 for path in sorted(APP.rglob('*.py'))
                 if '__pycache__' not in path.parts)


@functools.cache
def app_trees() -> tuple[tuple[pathlib.Path, str, ast.Module], ...]:
    """(path, source text, parsed tree) for the same files as app_files()."""
    return tuple((path, source, ast.parse(source)) for path, source in app_files())
