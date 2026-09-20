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
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


# This file quotes both patterns in its own regex literals, so it has to be
# excluded from its own scan or it counts itself.
SELF = 'tests/test_no_deprecated_apis.py'


def _sources(*roots):
    for root in roots:
        for path in sorted((ROOT / root).rglob('*.py')):
            if '__pycache__' in path.parts:
                continue
            if path.relative_to(ROOT).as_posix() == SELF:
                continue
            yield path, path.read_text()


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

# SQLAlchemy's legacy Query.get(). Ceiling rather than zero because the
# migration is sub-project 71; this stops the count GROWING in the meantime.
# Note get_or_404 is deliberately not matched here: it is Flask-SQLAlchemy's
# own method, and 71 replaces its call sites too.
LEGACY_GET = re.compile(r'\.query\.get\(|\bsession\.query\([A-Za-z_]+\)\.get\(')

LEGACY_GET_CEILING = 752


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


def test_legacy_query_get_does_not_grow():
    """A ceiling, not a floor. Sub-project 71 drives this to zero by replacing
    Query.get() with db.session.get() and get_or_404 with
    db.session.get(...) or abort(404).
    """
    hits = _count(LEGACY_GET, 'app', 'tests')

    assert len(hits) <= LEGACY_GET_CEILING, (
        f'legacy Query.get() count rose to {len(hits)}, above the ceiling of '
        f'{LEGACY_GET_CEILING}. Use db.session.get(Model, id) instead.')


def test_the_legacy_ceiling_is_not_slack():
    """A ceiling far above the real count would let the number grow silently.
    This pins it within one of the truth, so lowering the count means lowering
    the ceiling in the same commit.
    """
    hits = _count(LEGACY_GET, 'app', 'tests')

    assert len(hits) == LEGACY_GET_CEILING, (
        f'count is {len(hits)} but the ceiling is {LEGACY_GET_CEILING}; move '
        f'the ceiling down to match in the same commit that lowered the count.')


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
