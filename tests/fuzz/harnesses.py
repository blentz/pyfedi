"""Property checks shared by the fuzz campaign and the corpus replay.

Each check raises AssertionError when the property is violated. "It did not
crash" is a weak oracle -- it would pass a sanitizer that returned its input
unchanged -- so each check asserts a security property instead.

No atheris import lives here on purpose: tests/test_utils_fuzz_corpus.py replays
the committed corpus through these same functions without the fuzzer, so a
finding stays pinned by the ordinary deterministic suite.

Two things every check here has to get right, or the campaign is worthless:

* It must not fire on inert output. Every one of these functions may legally
  emit the *text* "javascript:" or " onload=" as escaped character data -- that
  is a string on a page, not a script. Byte-level substring checks flag those
  within seconds of fuzzing and drown a real finding in noise, so the markup
  properties below are asserted against a PARSED tree, not against raw bytes.
  Raw-byte assertions are used only where the sanitizer is known to escape the
  same characters in text (proved by the corpus cases named in each comment),
  so a raw hit can only mean real markup.
* It must not fire on inputs the function is contracted to refuse. ValueError is
  the refusal channel for the SVG path; a refusal is a success, not a crash.

app.utils.sanitize_svg is deliberately NOT a target here. It opens a path, hands
the bytes to sanitize_svg_bytes, and writes the result back; all of its input
handling is sanitize_svg_bytes, which IS fuzzed. Fuzzing a filesystem path would
exercise open(), not PieFed. Its own logic -- destroying the file on failure --
is behaviour under a fixed input rather than a property over arbitrary bytes,
and tests/test_utils_security.py covers it directly.
"""

import re
import sys
import traceback
import xml.etree.ElementTree as ElementTree

from bs4 import BeautifulSoup

from app.utils import allowlist_html, is_valid_xml_utf8, sanitize_svg_bytes

# WHATWG URL parsing, "basic URL parser": leading and trailing C0 controls and
# space are stripped from the input, and every ASCII tab, LF and CR is removed
# from it, BEFORE the scheme is read. So href="\tjava\nscript:alert(1)" is
# href="javascript:alert(1)" by the time a browser decides what to do with it.
# Any check that looks for the literal prefix "javascript:" misses all of those.
_URL_LEADING_TRAILING_STRIP = ''.join(chr(c) for c in range(0x21))
_URL_REMOVED_ENTIRELY = str.maketrans('', '', '\t\n\r')

# An event-handler content attribute is any attribute whose name begins "on".
# The allowlist deletes every attribute outside a fixed list, so none may ever
# reach the output; a surviving one is an XSS regardless of which handler it is.
_EVENT_HANDLER_ATTR = re.compile(r'^on.', re.IGNORECASE)


def _resolves_to_javascript_url(value) -> bool:
    """True if a browser would treat this attribute value as a javascript: URL.

    Applies the WHATWG stripping described above before looking at the scheme,
    which is exactly what app.utils.allowlist_html's furl(...).scheme test does
    not do.
    """
    if isinstance(value, list):        # BeautifulSoup returns multi-valued attributes as lists
        value = ' '.join(value)
    normalized = value.strip(_URL_LEADING_TRAILING_STRIP).translate(_URL_REMOVED_ENTIRELY)
    return normalized.lower().startswith('javascript:')


def _local_name(name: str) -> str:
    """Strip an ElementTree Clark-notation namespace, so '{ns}href' -> 'href'."""
    return name.rpartition('}')[2]


def check_is_valid_xml_utf8(data: bytes) -> None:
    """Must agree with an independent reading of the XML 1.0 Char production.

    The brief for this task described the pre-Task-4 implementation -- a
    hand-written byte scanner whose s[i+1]/s[i+2] indexing against a `c_end - 2`
    loop bound could raise IndexError -- and hunted that crash. That code is
    gone. app.utils.is_valid_xml_utf8 is now a strict UTF-8 decode followed by
    one negated-character-class regex built from _XML_CHAR_RANGES, so "does not
    raise" is no longer a meaningful oracle: a regex either matches or it does
    not.

    What can still go wrong is the regex being built WRONG -- a range assembled
    from chr(lo)/chr(hi) and joined with '-' is one typo away from admitting the
    surrogate block or from turning a '-' into a range operator, and it would
    fail silently in the accepting direction. So the property asserted is a
    differential one: the regex verdict must equal a verdict computed by plain
    integer comparison against the same table, per code point. The two share the
    table and nothing else.

    Also asserted: the bytes path and the str path agree. app.utils documents
    both as accepted, and callers use both.
    """
    result = is_valid_xml_utf8(data)
    assert isinstance(result, bool), f'returned {type(result).__name__}, not bool'

    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        assert result is False, 'input that is not strict UTF-8 was accepted'
        return

    expected = all(
        code_point in (0x9, 0xA, 0xD)
        or 0x20 <= code_point <= 0xD7FF
        or 0xE000 <= code_point <= 0xFFFD
        or 0x10000 <= code_point <= 0x10FFFF
        for code_point in map(ord, text)
    )
    assert result == expected, (
        f'regex said {result}, per-code-point check said {expected}')

    assert is_valid_xml_utf8(text) == result, (
        'the str path and the bytes path disagree about the same characters')


def check_sanitize_svg_bytes(data: bytes) -> None:
    """Sanitized output must carry no script, no event handler and no DTD.

    ValueError is the documented refusal channel and covers every deliberate
    rejection: oversize input, a UTF-16 encoding and an entity declaration
    (app.utils.refuse_svg_entity_declarations), plus py-svg-hush's own parse and
    encoding errors. A refusal is a success. Any OTHER exception escaping is a
    finding, and is left to propagate so the traceback names its own cause.

    The element/attribute properties are asserted against the parsed output
    rather than its bytes because character data survives sanitization: the
    corpus cases javascript_text and onload_text are well-formed SVGs whose
    <text> content is "javascript:x" and " onload=1", and both are legal, inert
    output that a substring check would report as a break-in.

    Parsing the output with the stdlib ElementTree is safe HERE, and only here,
    because it is py-svg-hush's output being parsed, not the fuzzer's input:
    the no-DTD assertion runs first and fails the check before any parse if a
    doctype or entity declaration ever reached the output, which is what an
    entity-expansion or XXE attack on the parser would need.
    """
    try:
        result = sanitize_svg_bytes(data)
    except ValueError:
        return

    assert isinstance(result, bytes), f'returned {type(result).__name__}, not bytes'

    # Safe as raw-byte checks: py-svg-hush escapes '<' and '&' in character data
    # and in attribute values (corpus case cdata_script proves it -- a CDATA
    # section holding "<script>" comes back as "&lt;script&gt;"), so a literal
    # '<!' or '<?' in the output can only be markup.
    lowered = result.lower()
    assert b'<!doctype' not in lowered, 'a doctype survived sanitization'
    assert b'<!entity' not in lowered, 'entity declaration survived sanitization'
    assert b'<?xml-stylesheet' not in lowered, 'stylesheet processing instruction survived sanitization'

    root = ElementTree.fromstring(result)
    for element in root.iter():
        if isinstance(element.tag, str):
            assert _local_name(element.tag) != 'script', 'script element survived sanitization'
        for name, value in element.attrib.items():
            local = _local_name(name)
            assert not _EVENT_HANDLER_ATTR.match(local), (
                f'event handler attribute {local!r} survived sanitization')
            if local in ('href', 'src'):
                assert not _resolves_to_javascript_url(value), (
                    f'javascript: URL survived sanitization in {local!r}')


def _allowlist_html_properties(data: bytes, *, check_javascript_urls: bool) -> None:
    """The body of check_allowlist_html. See both wrappers below.

    test_env is passed so the function does not reach get_emoji_replacements()
    or fediverse_domains(), both of which are DB-backed. Fuzzing must not need a
    database.

    Asserted against the parsed output, for the reason given in the module
    docstring: allowlist_html passes plain text through untouched, so the
    inputs "javascript:alert(1)" and "x onload=y" come back verbatim as
    character data. They are inert text; a substring check calls them XSS.
    BeautifulSoup('html.parser') is the same parser allowlist_html itself uses
    to decide what to keep, so the tree checked here is the tree it built.
    """
    text = data.decode('utf-8', errors='replace')
    result = allowlist_html(text, test_env={'fn_string': 'fn-test'})
    assert isinstance(result, str), f'returned {type(result).__name__}, not str'

    soup = BeautifulSoup(result, 'html.parser')
    assert not soup.find_all('script'), 'script element survived the allowlist'
    for tag in soup.find_all(True):
        for name, value in tag.attrs.items():
            assert not _EVENT_HANDLER_ATTR.match(name), (
                f'event handler attribute {name!r} survived the allowlist')
            if check_javascript_urls and name == 'href':
                assert not _resolves_to_javascript_url(value), (
                    'javascript: URL survived the allowlist')


def check_allowlist_html(data: bytes) -> None:
    """The XSS boundary for all remote content: no script, no handlers, no
    javascript: URLs may survive, for any input.

    This is the check the corpus replay runs, and the one a campaign should use.
    """
    _allowlist_html_properties(data, check_javascript_urls=True)


# The frames that raise app.utils's reported-but-unfixed IndexError. Both do
# `tag_content[1:].split()[0]` (or [:-1]) on markup like "</>", where stripping
# the slash leaves an empty string and split() therefore returns [].
_KNOWN_INDEX_ERROR_FRAMES = ('escape_non_html_brackets', 'escape_non_html_angle_brackets')


def check_allowlist_html_past_known_defects(data: bytes) -> None:
    """check_allowlist_html with its two REPORTED, UNFIXED defects suppressed.
    Campaigns only -- never the corpus replay.

    A fuzz campaign stops at its first crash. allowlist_html has two known
    defects that a campaign reaches immediately, so a run against
    check_allowlist_html spends its whole budget rediscovering them and never
    looks for a third:

    1. the javascript:-URL bypass -- tests/fuzz/corpus/allowlist_html/
       javascript_url_leading_space and its two siblings. Reached at execution 0,
       because it is in the seed corpus.
    2. IndexError out of escape_non_html_brackets on "</>" --
       tests/fuzz/corpus/allowlist_html/empty_closing_tag. This campaign found
       it at execution 236 of its first 60-second run.

    Both are pinned by strict xfails in tests/test_utils_fuzz_corpus.py, which is
    where they stay visible. Suppressing them HERE, and only here, is what lets a
    campaign keep hunting past them; it is not a fix and it does not hide them.

    Both suppressions are narrow. Dropping the javascript:-URL assertion costs
    nothing production already handles -- an unpadded "javascript:" href is
    emptied by allowlist_html itself, so that assertion can only ever fire on the
    known bypass. The IndexError is swallowed only when it was raised by one of
    the two frames named in _KNOWN_INDEX_ERROR_FRAMES; an IndexError from
    anywhere else is a new finding and propagates.
    """
    try:
        _allowlist_html_properties(data, check_javascript_urls=False)
    except IndexError:
        raising_frame = traceback.extract_tb(sys.exc_info()[2])[-1].name
        if raising_frame in _KNOWN_INDEX_ERROR_FRAMES:
            return
        raise
