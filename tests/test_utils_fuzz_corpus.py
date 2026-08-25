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
attribute, a javascript: URL or an entity declaration through, or that makes
is_valid_xml_utf8's character table disagree with XML 1.0.
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
_JAVASCRIPT_URL_BYPASS = (
    'reported, unfixed: allowlist_html decides whether an href is a javascript: URL with '
    "furl(href).scheme == 'javascript'. The WHATWG URL parser strips leading and trailing C0 "
    'controls and spaces and removes every ASCII tab, LF and CR BEFORE reading the scheme, so a '
    'browser resolves href=" javascript:alert(1)" and href="java\\nscript:alert(1)" to '
    'javascript:alert(1) and runs them, while furl sees no javascript scheme and the attribute '
    'is left intact. allowlist_html is the XSS boundary for all remote content: stored XSS.')

_EMPTY_CLOSING_TAG_INDEX_ERROR = (
    'reported, unfixed: allowlist_html raises IndexError on "</>". '
    'app/utils.py:410 escape_non_html_brackets does tag_content[1:].split()[0] after seeing a '
    'leading slash; for "/" that is "".split(), which is []. The empty-string guard above it '
    'only catches "<>". escape_non_html_angle_brackets has the same bug, so markdown_to_html("</>") '
    'raises too. Every piece of remote content goes through here, so a three-character string in '
    'any federated post, comment or profile is an unhandled 500.')

KNOWN_UNFIXED = {
    'allowlist_html/javascript_url_leading_space': _JAVASCRIPT_URL_BYPASS,
    'allowlist_html/javascript_url_leading_tab_entity': _JAVASCRIPT_URL_BYPASS,
    'allowlist_html/javascript_url_newline_in_scheme': _JAVASCRIPT_URL_BYPASS,
    'allowlist_html/empty_closing_tag': _EMPTY_CLOSING_TAG_INDEX_ERROR,
    'allowlist_html/empty_closing_tag_with_space': _EMPTY_CLOSING_TAG_INDEX_ERROR,
    'allowlist_html/empty_closing_tag_indexerror_as_found': _EMPTY_CLOSING_TAG_INDEX_ERROR,
}


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
    pairing so the reminder cannot be removed by accident."""
    name, _, filename = case_id.partition('/')
    assert (CORPUS_ROOT / name / filename).is_file(), (
        f'{case_id} is named in KNOWN_UNFIXED but no longer exists')
