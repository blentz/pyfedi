"""Replays every committed fuzz input deterministically.

The campaign itself is nondeterministic and runs on demand (see tests/README.md).
This file is what keeps a finding fixed: an input that once broke a property is
committed here and checked on every run, forever.

Nothing here imports atheris. The property checks in tests/fuzz/harnesses.py are
plain functions, so the corpus replays inside the ordinary suite -- fast,
deterministic, and without the fuzzer's tracing fighting coverage.py's.

The production change that would make these tests fail: any edit to
app.utils.allowlist_html, app.utils.is_valid_xml_utf8 or
app.utils.sanitize_svg_bytes that lets script markup, an event-handler
attribute, a javascript:/vbscript: URL in an anchor's href, or an entity
declaration through, or that makes is_valid_xml_utf8's character table disagree
with XML 1.0.
"""

import pathlib

import pytest

from tests.fuzz.harnesses import (check_allowlist_html, check_is_valid_xml_utf8,
                                  check_sanitize_svg_bytes)

CORPUS_ROOT = pathlib.Path(__file__).parent / 'fuzz' / 'corpus'

CHECKS = {
    'allowlist_html': check_allowlist_html,
    'is_valid_xml_utf8': check_is_valid_xml_utf8,
    'sanitize_svg_bytes': check_sanitize_svg_bytes,
}

# Inputs that break a property because the DEFECT IS REAL AND UNFIXED. A security
# fix in deployed software is the project owner's decision, so these are reported
# and pinned, not patched.
#
# strict=True on purpose: when a defect is fixed its cases XPASS and fail the
# run, which is the signal to delete the entry rather than let a stale xfail hide
# a later regression.
#
# EMPTY, deliberately. It held six entries covering two defects, both of which
# the project owner authorised and which are now fixed in app/utils.py:
#
#   * the javascript:-URL bypass (javascript_url_leading_space,
#     javascript_url_leading_tab_entity, javascript_url_newline_in_scheme).
#     allowlist_html decided the scheme with furl(href).scheme, and furl does not
#     apply the WHATWG normalisation a browser applies before reading a scheme.
#     It now calls app.utils.has_unsafe_url_scheme, which does.
#   * IndexError out of escape_non_html_brackets on '</>' (empty_closing_tag,
#     empty_closing_tag_with_space, empty_closing_tag_indexerror_as_found).
#
# Their corpus files are still here and still replayed on every run -- they are
# now regression pins that PASS, which is exactly what a fixed finding should
# become. The strict xfails XPASSed the moment the fix landed, which is what
# prompted deleting these entries rather than relaxing them.
KNOWN_UNFIXED = {}


def corpus_cases():
    """A list, not a generator: pytest 10 will stop accepting the latter."""
    cases = []
    for name, check in CHECKS.items():
        directory = CORPUS_ROOT / name
        for path in sorted(directory.glob('*')):
            if path.is_file():
                case_id = f'{name}/{path.name}'
                marks = []
                if case_id in KNOWN_UNFIXED:
                    marks.append(pytest.mark.xfail(strict=True, reason=KNOWN_UNFIXED[case_id]))
                cases.append(pytest.param(check, path, id=case_id, marks=marks))
    return cases


@pytest.mark.parametrize('check,path', corpus_cases())
def test_corpus_input_still_satisfies_its_property(check, path):
    check(path.read_bytes())


@pytest.mark.parametrize('name', sorted(CHECKS))
def test_every_target_has_a_seeded_corpus(name):
    """A target whose corpus directory vanished would otherwise silently stop
    being replayed, since parametrize over an empty glob collects nothing."""
    directory = CORPUS_ROOT / name
    assert directory.is_dir(), f'{directory} is missing'
    assert any(p.is_file() for p in directory.iterdir()), f'{directory} is empty'


@pytest.mark.parametrize('case_id', sorted(KNOWN_UNFIXED))
def test_every_known_unfixed_case_is_present(case_id):
    """A strict xfail only reports a fix if the file it names still exists.

    Delete the corpus file and the parametrize id disappears with it, taking the
    xfail and its report of the defect along -- silently. This asserts the
    pairing so the reminder cannot be removed by accident.

    While KNOWN_UNFIXED is empty this collects as a single test skipped with
    "got empty parameter set", which is the honest report: there is nothing
    pinned as unfixed, and the guard is here for the next finding."""
    name, _, filename = case_id.partition('/')
    assert (CORPUS_ROOT / name / filename).is_file(), (
        f'{case_id} is named in KNOWN_UNFIXED but no longer exists')
