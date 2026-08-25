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


def _resolves_to_scheme(value, schemes) -> bool:
    """True if a browser would read one of `schemes` off this attribute value.

    Applies the WHATWG stripping described above before looking at the scheme,
    which is exactly what app.utils.allowlist_html's furl(...).scheme test used
    to fail to do. `schemes` is a tuple of names WITHOUT the colon.

    Deliberately a separate reading of the rule from the production one in
    app.utils.url_scheme: a property check that called the code under test would
    assert nothing.
    """
    if isinstance(value, list):        # BeautifulSoup returns multi-valued attributes as lists
        value = ' '.join(value)
    normalized = value.strip(_URL_LEADING_TRAILING_STRIP).translate(_URL_REMOVED_ENTIRELY).lower()
    return any(normalized.startswith(f'{scheme}:') for scheme in schemes)


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
                # javascript: only. data: is NOT asserted against here:
                # sanitize_svg_bytes legitimately keeps data: URLs for the image
                # MIME types in its keep list, so asserting on it would report
                # correct output as a break-in.
                assert not _resolves_to_scheme(value, ('javascript',)), (
                    f'javascript: URL survived sanitization in {local!r}')


def check_allowlist_html(data: bytes) -> None:
    """The XSS boundary for all remote content: no script element, no event
    handler attribute and no script-executing URL may survive, for any input.

    test_env is passed so the function does not reach get_emoji_replacements()
    or fediverse_domains(), both of which are DB-backed. Fuzzing must not need a
    database.

    Asserted against the parsed output, for the reason given in the module
    docstring: allowlist_html passes plain text through untouched, so the
    inputs "javascript:alert(1)" and "x onload=y" come back verbatim as
    character data. They are inert text; a substring check calls them XSS.
    BeautifulSoup('html.parser') is the same parser allowlist_html itself uses
    to decide what to keep, so the tree checked here is the tree it built.

    This function used to have a second form, check_allowlist_html_past_known_defects,
    which suppressed two reported-but-unfixed defects so a campaign could hunt
    past them: the javascript:-URL bypass, and an IndexError out of
    escape_non_html_brackets on "</>". Both are now fixed in app/utils.py, so
    the suppression has been deleted rather than left to swallow a regression.
    There is one target again, and it is the honest one.

    javascript: and vbscript: are asserted against; data: is not. A data: href
    is blanked by app.utils.allowlist_html as defence in depth, but that is a
    policy choice rather than a property of the XSS boundary, and asserting it
    here would make a campaign report a policy change as a break-in.

    The href assertion applies to ANCHORS ONLY, for the same reason the module
    docstring gives for asserting against a parsed tree. 'href' is on
    allowlist_html's allowed_attrs for every element, so malformed input can
    leave <img href="javascript:x"> standing -- and that is inert, because HTML
    defines no href on img and no browser navigates it. Of the elements in
    app.utils.allowed_tags, 'a' is the only one for which href is a defined,
    navigable attribute: area, link and base are not allowed through at all, so
    nothing is lost by scoping this to anchors. Measured, not assumed: the first
    campaign this target could run flagged exactly that inert <img href> at seed
    load, and tests/fuzz/corpus/allowlist_html/javascript_href_on_img_is_inert
    is the minimised case pinning it.
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
            if name == 'href' and tag.name == 'a':
                assert not _resolves_to_scheme(value, ('javascript', 'vbscript')), (
                    'script-executing URL survived the allowlist')
