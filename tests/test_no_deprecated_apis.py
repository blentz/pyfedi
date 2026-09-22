"""A ceiling on deprecated API usage, mirroring coverage_floors.ini.

Coverage has a floor file that only ever rises. Warnings had nothing, which is
how 430 warning sites accumulated unnoticed while sixty-nine sub-projects
pursued "0 errors and 0 warnings": the suite's count went 7,768 -> 7,857 ->
8,326 -> 8,373 without anything failing.

These ceilings only ever FALL. Lowering one is a sub-project's deliverable;
raising one to make a run pass defeats the point exactly as lowering a coverage
floor would.

Counted by reading the source rather than by catching warnings at runtime,
because a deprecated call on a line no test reaches still ships -- app/nntp's
two are exactly that shape, and a runtime count would score them zero.
"""
import ast
import re
from functools import lru_cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


# This file quotes both patterns in its own regex literals, so it has to be
# excluded from its own scan or it counts itself.
SELF = 'tests/test_no_deprecated_apis.py'


@lru_cache(maxsize=None)
def _sources(*roots):
    """Every source file under `roots`, with its text, read ONCE for the whole
    session.

    Three rows in this module walk the same two trees, and `_legacy_calls`
    used to read each file a second time on top of that -- four full reads of
    app/ and tests/ per run, which measured 4.18s in the slowest row alone.
    The tree does not change while the suite runs, so the result is cached;
    the tuple return type is what makes it cacheable.
    """
    found = []
    for root in roots:
        for path in sorted((ROOT / root).rglob('*.py')):
            if '__pycache__' in path.parts:
                continue
            if path.relative_to(ROOT).as_posix() == SELF:
                continue
            found.append((path, path.read_text()))
    return tuple(found)


def _count(pattern, *roots, skip=()):
    hits = []
    for path, text in _sources(*roots):
        relative = path.relative_to(ROOT).as_posix()
        if relative in skip:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                hits.append(f'{relative}:{number}')
    return hits


# `datetime.utcnow()` -- deprecated in 3.12, scheduled for removal. The
# replacement is app/models.py's own utcnow(), which returns naive UTC and is
# therefore exactly what the deprecated call returned; datetime.now(UTC) is NOT
# a drop-in, because it returns an aware datetime that cannot be compared with
# the naive datetimes in the database.
UTCNOW = re.compile(r'\bdatetime\.utcnow\(\)')

# app/nntp is exempt at 2, not 0: neither module is imported by the suite (both
# measure 0.0% coverage), so neither contributes a warning to the run, and
# nntpserver.py is a generic NNTP implementation with no app imports that
# should not be coupled to app.models for this. They belong to the app/nntp
# sub-project. See sub-project 70's R1.
UTCNOW_EXEMPT = {'app/nntp/nntpserver.py', 'app/nntp/server.py'}

# SQLAlchemy's legacy Query.get(), and Flask-SQLAlchemy's get_or_404 which
# calls it internally. Sub-project 71 migrated all 830 call sites, so the
# ceiling is now ZERO.
#
# COUNTED FROM THE AST, not by regex. A textual count scores comments and
# docstrings: 94 lines still mention the old API in prose -- three of them
# commented-out code in app/ -- and none of them is a call. The migration
# itself was AST-based for the same reason; a text rewrite would have edited
# tests/conftest.py's documentation and a test whose subject IS get_or_404.
LEGACY_GET_CEILING = 0

# Only this file, and only because it quotes the old API in its own messages.
# tests/test_domain_routes.py used to be exempt too -- it holds a test about
# the get_or_404 guard -- but that test now demonstrates the replacement
# production actually uses, so nothing there needs excusing.
LEGACY_EXEMPT = {'tests/test_no_deprecated_apis.py'}


def _legacy_calls(path, text):
    """Every live `X.query.get(...)`, `X.query.get_or_404(...)` and
    `<session>.query(X).get(...)` in one file, as `path:lineno`.
    """
    # Both shapes this looks for -- `X.query.get(...)` and
    # `<session>.query(X).get(...)` -- contain the substring `.query`, so a
    # file without it cannot match and need not be parsed. Worth 2.81s ->
    # 2.49s, no more: most files in app/ and tests/ do carry it. The rest of
    # the cost is the AST walk itself, which is what this row is.
    if '.query' not in text:
        return []

    found = []
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        func = node.func
        if func.attr not in ('get', 'get_or_404'):
            continue
        owner = func.value
        if (isinstance(owner, ast.Attribute) and owner.attr == 'query'
                and isinstance(owner.value, ast.Name)
                and owner.value.id[:1].isupper()):
            found.append(node.lineno)
        elif (isinstance(owner, ast.Call)
              and isinstance(owner.func, ast.Attribute)
              and owner.func.attr == 'query' and len(owner.args) == 1
              and isinstance(owner.args[0], ast.Name)):
            # Both `.get(...)` AND `.get_or_404(...)` on a session query. The
            # migration's own finder handled get_or_404 only in the
            # `X.query.get_or_404(...)` form and missed two live sites in this
            # shape; they were found by the warning count failing to reach the
            # third-party floor, not by the migrator.
            found.append(node.lineno)
    return found


def test_no_new_uses_of_deprecated_utcnow():
    """Zero everywhere the suite can reach. The two exemptions are named
    individually so adding a third fails this row rather than widening a glob.
    """
    hits = _count(UTCNOW, 'app', 'tests', skip=UTCNOW_EXEMPT)

    assert hits == [], (
        f'datetime.utcnow() is deprecated; use app.models.utcnow(), which '
        f'returns the same naive UTC value. Found: {hits}')


def test_the_two_nntp_exemptions_are_still_the_only_ones():
    """The exemption list is itself asserted, so it cannot quietly outlive the
    reason for it: if either call is fixed, this row fails and the exemption
    comes off.
    """
    hits = _count(UTCNOW, 'app', 'tests')

    assert sorted(hit.rsplit(':', 1)[0] for hit in hits) == [
        'app/nntp/nntpserver.py', 'app/nntp/server.py']


def test_no_legacy_query_get_calls_remain():
    """Zero, held at zero. Sub-project 71 migrated 830 call sites:
    `X.query.get(id)` and `db.session.query(X).get(id)` became
    `db.session.get(X, id)`, `session.query(X).get(id)` became
    `session.get(X, id)`, and all 171 `get_or_404` sites became
    `db.session.get(X, id) or abort(404)` -- which is what Flask-SQLAlchemy's
    own get_or_404 does, minus the legacy call it makes internally.
    """
    hits = []
    for path, text in _sources('app', 'tests'):
        relative = path.relative_to(ROOT).as_posix()
        if relative in LEGACY_EXEMPT:
            continue
        hits += [f'{relative}:{line}' for line in _legacy_calls(path, text)]

    assert len(hits) <= LEGACY_GET_CEILING, (
        f'legacy Query.get() is back at {len(hits)} site(s): {hits[:10]}. '
        f'Use db.session.get(Model, id), or '
        f'db.session.get(Model, id) or abort(404) in place of get_or_404.')


@pytest.mark.parametrize('module, line', [
    ('app/shared/tasks/deletes.py', 'if follower.remote_user_id else None'),
    ('app/shared/tasks/pages.py', 'if follower.remote_user_id else None'),
])
def test_the_null_follower_guards_are_still_there(module, line):
    """SAWarning: "fully NULL primary key identity cannot load any object. This
    condition MAY RAISE AN ERROR in a future release." Both fan-out loops used
    to hand .get() a nullable FK and rely on the None it happened to answer.
    The guard is what makes the skip deliberate rather than accidental.
    """
    assert line in (ROOT / module).read_text()
