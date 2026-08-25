"""Rejection paths for the XML and SVG input filters in app/utils.py.

These three functions exist to reject hostile input, so the expectations here are
derived from the primary sources rather than from the implementation:

* XML 1.0 (Fifth Edition) section 2.2, the ``Char`` production --
  ``#x9 | #xA | #xD | [#x20-#xD7FF] | [#xE000-#xFFFD] | [#x10000-#x10FFFF]``
  (https://www.w3.org/TR/xml/#charsets)
* RFC 3629 section 3, which forbids overlong forms, surrogate encodings,
  sequences above U+10FFFF, and the bytes C0/C1/F5-FF
* OWASP XML External Entity Prevention Cheat Sheet
* OWASP XSS Filter Evasion Cheat Sheet
* SVG 1.1 / SVG 2 for element and attribute semantics

Where the implementation and the standard disagree, the test asserts what the
standard says and is marked ``xfail(strict=True)`` with the clause cited. A
strict xfail turns into a failure the moment the divergence is fixed, so none of
them can rot into a silent pass.
"""

import os

import pytest

from app.utils import is_valid_xml_utf8, sanitize_svg, sanitize_svg_bytes

MAX_SVG_SIZE = 10 * 1024 * 1024

# A well-formed SVG whose <!DOCTYPE> internal subset contains a '>' inside a
# quoted entity value. app/utils.py strips declarations with the non-greedy
# rb'<\!.*?>', which stops at that inner '>' and leaves the tail of the
# declaration behind, so the document handed to filter_svg is no longer
# well-formed. Used by several tests below; see
# test_a_doctype_quoting_a_gt_defeats_the_pre_strip_regex.
DOCTYPE_GT_IN_QUOTED_VALUE = (
    b'<!DOCTYPE svg [<!ENTITY greater "x>y">]>'
    b'<svg xmlns="http://www.w3.org/2000/svg">'
    b'<script>alert(1)</script></svg>'
)


class TestIsValidXmlUtf8:
    """A hand-rolled UTF-8 byte scanner. Each test names the check it exercises."""

    def test_plain_ascii_is_valid(self):
        assert is_valid_xml_utf8(b'hello world') is True

    def test_a_str_is_encoded_before_scanning(self):
        assert is_valid_xml_utf8('hello world') is True

    def test_permitted_whitespace_is_valid(self):
        """XML 1.0 section 2.2 Char admits #x9, #xA and #xD."""
        assert is_valid_xml_utf8(b'a\tb\nc\rd') is True

    def test_nul_is_rejected(self):
        """#x0 is outside every alternative of the Char production."""
        assert is_valid_xml_utf8(b'\x00') is False

    def test_vertical_tab_and_form_feed_are_rejected(self):
        """#xB and #xC sit in the C0 gap the Char production leaves out."""
        assert is_valid_xml_utf8(b'\x0b') is False
        assert is_valid_xml_utf8(b'\x0c') is False

    def test_control_range_14_to_31_is_rejected(self):
        """[#xE-#x1F] is below Char's [#x20-#xD7FF] alternative."""
        assert is_valid_xml_utf8(b'\x0e') is False
        assert is_valid_xml_utf8(b'\x1f') is False

    def test_delete_is_rejected(self):
        """Current behaviour. This CONTRADICTS XML 1.0 -- see the xfail below.

        Kept so the divergence is visible from both sides: this test records what
        the code does today, and
        test_delete_is_permitted_by_the_char_production records what the standard
        requires. Fixing the divergence must flip both.
        """
        assert is_valid_xml_utf8(b'\x7f') is False

    @pytest.mark.xfail(strict=True, reason='SPEC DIVERGENCE: XML 1.0 permits #x7F')
    def test_delete_is_permitted_by_the_char_production(self):
        """XML 1.0 5e section 2.2: Char includes ``[#x20-#xD7FF]``, and #x7F is in it.

        DEL is listed only under the *discouraged* compatibility characters note
        ("[#x7F-#x84] and [#x86-#x9F]"), which is advisory prose, not part of the
        grammar. A conforming parser accepts it: ElementTree parses
        b'<r>\\x7f</r>' without error. Rejecting it here silently drops legitimate
        posts from the RSS feeds built in app/user/routes.py.
        """
        assert is_valid_xml_utf8(b'\x7f') is True

    def test_control_char_in_a_long_string_is_rejected_by_the_first_loop(self):
        """Long enough that the control byte is found before the tail loop."""
        assert is_valid_xml_utf8(b'aaaaaaaaaa\x00aaaaaaaaaa') is False

    def test_control_char_in_the_last_two_bytes_is_rejected_by_the_tail_loop(self):
        """The first loop stops at c_end - 2, so the tail loop must catch this."""
        assert is_valid_xml_utf8(b'aaaaaaaaaa\x00') is False

    def test_forbidden_fffe_is_rejected(self):
        """U+FFFE falls in the gap between Char's [#xE000-#xFFFD] and [#x10000-...]."""
        assert is_valid_xml_utf8(b'\xef\xbf\xbe') is False

    def test_forbidden_ffff_is_rejected(self):
        """U+FFFF is likewise outside every Char alternative."""
        assert is_valid_xml_utf8(b'\xef\xbf\xbf') is False

    def test_surrogate_range_is_rejected(self):
        """Char stops at #xD7FF and resumes at #xE000, excluding D800-DFFF."""
        assert is_valid_xml_utf8(b'\xed\xa0\x80') is False   # \ud800, low end
        assert is_valid_xml_utf8(b'\xed\xbf\xbf') is False   # \udfff, high end

    def test_legitimate_multibyte_text_is_valid(self):
        assert is_valid_xml_utf8('日本語のテキスト'.encode('utf-8')) is True

    def test_empty_input_is_valid(self):
        assert is_valid_xml_utf8(b'') is True

    def test_short_inputs_do_not_raise(self):
        """The loop bounds are hand-written; 1- and 2-byte inputs are the edge."""
        for raw in [b'', b'a', b'ab', b'\xef', b'\xef\xbf']:
            assert isinstance(is_valid_xml_utf8(raw), bool)

    @pytest.mark.parametrize('forbidden', [
        pytest.param(b'\xef\xbf\xbe', id='U+FFFE'),
        pytest.param(b'\xef\xbf\xbf', id='U+FFFF'),
        pytest.param(b'\xed\xa0\x80', id='U+D800'),
        pytest.param(b'\xed\xbf\xbf', id='U+DFFF'),
    ])
    @pytest.mark.parametrize('total_length', range(3, 13))
    def test_a_forbidden_sequence_is_caught_at_every_position(self, forbidden, total_length):
        """No position lets a forbidden 3-byte sequence slip between the two loops.

        The first loop runs ``while i < c_end - 2`` and the tail loop checks only
        ASCII controls, which looks like a gap. It is not one: a complete 3-byte
        window can only start at index <= c_end - 3, and c_end - 3 < c_end - 2, so
        every window the buffer actually contains is examined by the first loop.
        This sweeps every start offset for every buffer length 3..12 to prove it.

        Widening the first loop's bound to ``while i < c_end`` would make this
        raise IndexError; narrowing it to ``while i < c_end - 3`` would make the
        last-position cases return True.
        """
        for pos in range(total_length - 2):
            raw = b'a' * pos + forbidden + b'a' * (total_length - pos - 3)
            assert len(raw) == total_length
            assert is_valid_xml_utf8(raw) is False, f'escaped at offset {pos}'


class TestIsValidXmlUtf8EncodingValidity:
    """RFC 3629 section 3 sequences the scanner never looks at.

    ``is_valid_xml_utf8`` searches for specific byte *patterns*; it never checks
    that the input decodes as UTF-8 at all. Every input below raises
    UnicodeDecodeError in Python and is rejected by ElementTree when wrapped in
    an element, yet the function reports it as valid XML content.

    Reachability: the two production callers (app/user/routes.py, RSS feed
    generation) pass ``str``, and ``str.encode('utf-8', errors='ignore')`` can
    only produce well-formed UTF-8 -- so these are latent today rather than
    remotely triggerable. The signature accepts bytes and the docstring promises
    a UTF-8 check, so any future bytes caller inherits the hole.
    """

    @pytest.mark.xfail(strict=True, reason='SPEC DIVERGENCE: RFC 3629 s3 forbids overlong forms')
    @pytest.mark.parametrize('raw', [
        pytest.param(b'\xc0\x80', id='overlong-2-byte-NUL'),
        pytest.param(b'\xe0\x80\x80', id='overlong-3-byte-NUL'),
        pytest.param(b'\xf0\x80\x80\x80', id='overlong-4-byte-NUL'),
        pytest.param(b'\xc1\xaf', id='overlong-2-byte-solidus'),
    ])
    def test_overlong_forms_are_rejected(self, raw):
        """RFC 3629 section 3: "implementations MUST reject" non-shortest forms.

        These matter beyond pedantry: b'\\xc0\\x80' and b'\\xe0\\x80\\x80' both
        denote U+0000, which test_nul_is_rejected shows the function does reject
        when spelled b'\\x00'. The overlong spelling walks straight past it.
        """
        assert is_valid_xml_utf8(raw) is False

    @pytest.mark.xfail(strict=True, reason='SPEC DIVERGENCE: RFC 3629 s3 forbids stray continuation bytes')
    @pytest.mark.parametrize('raw', [
        pytest.param(b'\x80', id='lone-continuation'),
        pytest.param(b'ab\x80cd', id='continuation-mid-string'),
    ])
    def test_lone_continuation_bytes_are_rejected(self, raw):
        """RFC 3629 section 3: 80-BF may appear only after a valid lead byte."""
        assert is_valid_xml_utf8(raw) is False

    @pytest.mark.xfail(strict=True, reason='SPEC DIVERGENCE: RFC 3629 s3 forbids truncated sequences')
    @pytest.mark.parametrize('raw', [
        pytest.param(b'\xe2\x82', id='truncated-3-byte'),
        pytest.param(b'\xf0\x9f\x98', id='truncated-4-byte'),
    ])
    def test_truncated_sequences_are_rejected(self, raw):
        """RFC 3629 section 3: a lead byte must be followed by its full complement."""
        assert is_valid_xml_utf8(raw) is False

    @pytest.mark.xfail(strict=True, reason='SPEC DIVERGENCE: RFC 3629 s3 restricts UTF-8 to U+10FFFF')
    @pytest.mark.parametrize('raw', [
        pytest.param(b'\xf5\x80\x80\x80', id='above-U+10FFFF'),
        pytest.param(b'\xfe', id='byte-FE'),
        pytest.param(b'\xff', id='byte-FF'),
    ])
    def test_bytes_that_can_never_appear_in_utf8_are_rejected(self, raw):
        """RFC 3629 section 3: F5-FF never occur in a valid UTF-8 sequence."""
        assert is_valid_xml_utf8(raw) is False

    @pytest.mark.xfail(strict=True, reason='SECURITY: the U+FFFE/U+FFFF/surrogate filter is bypassable by overlong encoding')
    @pytest.mark.parametrize('raw', [
        pytest.param(b'\xf0\x8f\xbf\xbe', id='overlong-U+FFFE'),
        pytest.param(b'\xf0\x8f\xbf\xbf', id='overlong-U+FFFF'),
        pytest.param(b'\xf0\x8d\xa0\x80', id='overlong-U+D800'),
    ])
    def test_forbidden_code_points_are_rejected_in_their_overlong_spelling(self, raw):
        """The same code points test_forbidden_fffe_is_rejected et al. reject.

        The scanner only recognises the *shortest* 3-byte spellings (EF BF BE,
        EF BF BF, ED A0 80..ED BF BF). Re-spelling the identical code point as a
        4-byte overlong sequence -- exactly the technique in RFC 3629 section 10's
        security considerations, "a different way to represent the same
        character" -- walks past the filter. This is a positional-independent
        bypass of the very check the function exists to perform, and is the real
        answer to "can a forbidden sequence get through": not by moving it, by
        respelling it.
        """
        assert is_valid_xml_utf8(raw) is False

    @pytest.mark.xfail(strict=True, reason='SPEC DIVERGENCE: errors=ignore hides a lone surrogate in the str path')
    def test_a_lone_surrogate_str_is_rejected(self):
        """XML 1.0 section 2.2: D800-DFFF is not a Char.

        ``pystring.encode('utf-8', errors='ignore')`` silently deletes a lone
        surrogate, so the scanner sees b'' and reports valid. The caller then
        hands the original str to feedgen, which cannot serialise it. Discarding
        the encoding error is what loses the information; encoding with
        ``errors='strict'`` and treating the exception as invalid would fix it.
        """
        assert is_valid_xml_utf8('\ud800') is False


class TestSanitizeSvgBytes:
    """The XXE and script-injection boundary for uploaded SVGs.

    ``sanitize_svg_bytes`` is two layers: PieFed's own size guard and regex
    pre-stripping, then ``filter_svg`` from py-svg-hush. Tests below say which
    layer they pin.
    """

    def test_a_plain_svg_survives(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'
        assert b'<rect' in sanitize_svg_bytes(svg)

    def test_doctype_entity_declaration_is_stripped(self):
        """OWASP XXE Prevention: the entity must not survive to be resolved.

        PieFed layer. The declaration is removed by the rb'<\\!.*?>' substitution
        and the orphaned ']>' by the rb'\\]>' substitution, leaving a document
        with no DTD at all. Nothing in the output can reference file:///etc/passwd.
        """
        svg = (b'<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
               b'<svg xmlns="http://www.w3.org/2000/svg"/>')
        result = sanitize_svg_bytes(svg)
        assert b'<!ENTITY' not in result
        assert b'DOCTYPE' not in result
        assert b'passwd' not in result

    def test_a_reference_to_a_stripped_entity_fails_closed(self):
        """OWASP XXE Prevention: fail closed rather than emit a partly-resolved doc.

        Once the DTD is stripped, '&xxe;' is an undefined entity reference and
        filter_svg refuses the document. Raising is the safe outcome *for this
        function*; see TestSanitizeSvgFile for what the file-level wrapper then
        does with the failure.
        """
        svg = (b'<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
               b'<svg xmlns="http://www.w3.org/2000/svg"><text>&xxe;</text></svg>')
        with pytest.raises(ValueError, match='XML parsing error'):
            sanitize_svg_bytes(svg)

    def test_billion_laughs_fails_closed(self):
        """OWASP XXE Prevention, entity expansion. Same mechanism, DoS payload."""
        svg = (b'<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>'
               b'<svg xmlns="http://www.w3.org/2000/svg"><text>&lol2;</text></svg>')
        with pytest.raises(ValueError, match='XML parsing error'):
            sanitize_svg_bytes(svg)

    def test_processing_instruction_is_stripped(self):
        """PieFed layer: rb'<\\?.*?\\?>'. An xml-stylesheet PI can load remote XSLT."""
        svg = b'<?xml-stylesheet href="evil.xsl"?><svg xmlns="http://www.w3.org/2000/svg"/>'
        assert b'<?xml-stylesheet' not in sanitize_svg_bytes(svg)

    def test_script_element_does_not_survive(self):
        svg = (b'<svg xmlns="http://www.w3.org/2000/svg">'
               b'<script>alert(1)</script></svg>')
        assert b'<script' not in sanitize_svg_bytes(svg).lower()

    def test_uppercase_script_element_does_not_survive(self):
        """OWASP XSS Filter Evasion: case variation of a blocked tag name."""
        svg = (b'<svg xmlns="http://www.w3.org/2000/svg">'
               b'<SCRIPT>alert(1)</SCRIPT></svg>')
        assert b'script' not in sanitize_svg_bytes(svg).lower()

    def test_script_body_wrapped_in_cdata_does_not_survive(self):
        """OWASP XSS Filter Evasion: CDATA-wrapped payload."""
        svg = (b'<svg xmlns="http://www.w3.org/2000/svg">'
               b'<script><![CDATA[alert(1)]]></script></svg>')
        result = sanitize_svg_bytes(svg).lower()
        assert b'script' not in result
        assert b'alert' not in result

    def test_event_handler_attribute_does_not_survive(self):
        svg = (b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)">'
               b'<rect onclick="alert(2)"/></svg>')
        result = sanitize_svg_bytes(svg).lower()
        assert b'onload' not in result
        assert b'onclick' not in result

    def test_smil_animation_event_handler_does_not_survive(self):
        """SVG 1.1 animation elements carry their own event attributes (onbegin)."""
        svg = (b'<svg xmlns="http://www.w3.org/2000/svg"><rect>'
               b'<animate attributeName="x" onbegin="alert(1)"/></rect></svg>')
        assert b'onbegin' not in sanitize_svg_bytes(svg).lower()

    def test_javascript_url_in_an_xlink_href_does_not_survive(self):
        """OWASP XSS Filter Evasion: javascript: URI in a link target."""
        svg = (b'<svg xmlns="http://www.w3.org/2000/svg" '
               b'xmlns:xlink="http://www.w3.org/1999/xlink">'
               b'<a xlink:href="javascript:alert(1)"><rect width="1" height="1"/></a>'
               b'</svg>')
        assert b'javascript' not in sanitize_svg_bytes(svg).lower()

    def test_a_remote_url_is_not_left_pointing_off_site(self):
        """SVG <image href> would otherwise be a load-time request to the attacker."""
        svg = (b'<svg xmlns="http://www.w3.org/2000/svg">'
               b'<image href="http://evil.example/t.png"/></svg>')
        assert b'evil.example' not in sanitize_svg_bytes(svg)

    def test_a_data_url_with_a_disallowed_mime_type_is_dropped(self):
        """PieFed layer: keep_data_url_mime_types omits svg+xml, which can nest script."""
        svg = (b'<svg xmlns="http://www.w3.org/2000/svg">'
               b'<image href="data:image/svg+xml;base64,YQ=="/></svg>')
        assert b'data:image/svg' not in sanitize_svg_bytes(svg)

    def test_a_data_url_with_an_allowed_mime_type_is_kept(self):
        """The other side of that list: image/png is in keep_data_url_mime_types."""
        svg = (b'<svg xmlns="http://www.w3.org/2000/svg">'
               b'<image href="data:image/png;base64,YQ=="/></svg>')
        assert b'data:image/png;base64,YQ==' in sanitize_svg_bytes(svg)

    def test_input_that_is_not_xml_fails_closed(self):
        with pytest.raises(ValueError):
            sanitize_svg_bytes(b'hello')

    def test_empty_input_fails_closed(self):
        with pytest.raises(ValueError):
            sanitize_svg_bytes(b'')

    def test_oversize_input_is_refused(self):
        with pytest.raises(ValueError, match='SVG file too large'):
            sanitize_svg_bytes(b'a' * (MAX_SVG_SIZE + 1))

    def test_exactly_the_limit_is_allowed(self):
        """Boundary: the guard is `>`, so the limit itself must pass it.

        Padded with trailing whitespace, which XML allows after the root element.
        Changing the guard to `>=` makes this raise.
        """
        root = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
        payload = root + b' ' * (MAX_SVG_SIZE - len(root))
        assert len(payload) == MAX_SVG_SIZE
        assert b'<svg' in sanitize_svg_bytes(payload)

    def test_a_doctype_quoting_a_gt_defeats_the_pre_strip_regex(self):
        """PieFed layer defect: rb'<\\!.*?>' is not DTD-aware.

        XML 1.0 section 2.8 lets an EntityValue contain '>' when quoted, so
        `<!ENTITY greater "x>y">` is a legal declaration. The non-greedy regex
        stops at that inner '>' and leaves `y">]>` in front of the root element,
        so a document that WAS well-formed (lxml parses it and sees the <script>
        child) is handed to filter_svg as garbage and rejected outright.

        Sanitisation therefore never runs on an attacker-chosen input, purely
        because of PieFed's own pre-processing. That is safe here -- the raise is
        fail-closed -- but see
        test_a_file_the_sanitizer_rejects_is_left_hostile_on_disk for what the
        wrapper does with it.

        The production change that makes this test fail is making the stripping
        DTD-aware -- i.e. fixing the defect -- at which point filter_svg receives
        a well-formed document and sanitises it instead of raising. (Deleting the
        rb'<\\!.*?>' substitution alone does NOT flip it: the later rb'\\]>'
        substitution then removes the internal subset's terminator and the
        document is still unparseable. Verified by executing both edits.)
        """
        with pytest.raises(ValueError, match='XML parsing error'):
            sanitize_svg_bytes(DOCTYPE_GT_IN_QUOTED_VALUE)

    def test_a_comment_containing_gt_leaks_its_tail_as_rendered_text(self):
        """PieFed layer defect: the same non-DTD-aware regex corrupts comments.

        XML 1.0 section 2.5 allows '>' inside a comment. rb'<\\!.*?>' stops at it
        and leaves the rest of the comment as character data, which filter_svg
        then escapes and emits as *visible text* in the sanitised SVG. Not an
        injection -- the serialiser escapes it -- but silent content corruption
        of any legitimate SVG whose comments contain '>'.

        This asserts the divergence from XML 1.0's comment rule directly: a
        DTD-aware stripper would leave no comment residue at all.
        """
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><!-- a > b --><rect/></svg>'
        result = sanitize_svg_bytes(svg)
        assert b'b --&gt;' in result, 'comment tail leaks into the document as text'

    def test_a_cdata_section_containing_gt_is_truncated(self):
        """PieFed layer defect: rb'<\\!.*?>' also eats into `<![CDATA[ ... ]]>`.

        XML 1.0 section 2.7: '>' is ordinary data inside CDATA. The regex removes
        `<![CDATA[a >` and the rb'\\]>' cleanup removes the section terminator, so
        the text content silently changes from 'a > b' to ' b]'.
        """
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><text><![CDATA[a > b]]></text></svg>'
        result = sanitize_svg_bytes(svg)
        assert b'a &gt; b' not in result
        assert b'<text> b]</text>' in result


class TestSanitizeSvgFile:
    def test_a_clean_file_is_left_alone_and_reports_success(self, app, tmp_path):
        path = tmp_path / 'clean.svg'
        original = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'
        path.write_bytes(original)

        assert sanitize_svg(str(path)) is True
        assert b'<rect' in path.read_bytes()

    def test_a_hostile_file_is_rewritten_in_place(self, app, tmp_path):
        path = tmp_path / 'hostile.svg'
        path.write_bytes(b'<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
                         b'<svg xmlns="http://www.w3.org/2000/svg"/>')

        assert sanitize_svg(str(path)) is True
        assert b'<!ENTITY' not in path.read_bytes()

    def test_an_already_sanitized_file_is_not_rewritten(self, app, tmp_path):
        """The `if sanitized_svg != svg_bytes` false arm: no write when nothing changed.

        `sanitize_svg_bytes` is idempotent -- the rb'<\\?.*?\\?>' strip removes the
        XML declaration and filter_svg puts an identical one back -- so feeding it
        its own output is a fixed point. mtime is the observable: it is pinned to
        the epoch first, and an unnecessary rewrite would move it.

        Removing the `if` and writing unconditionally makes this fail.
        """
        path = tmp_path / 'already-clean.svg'
        canonical = sanitize_svg_bytes(
            b'<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>')
        path.write_bytes(canonical)
        os.utime(path, (0, 0))

        assert sanitize_svg(str(path)) is True
        assert path.read_bytes() == canonical
        assert os.stat(path).st_mtime == 0, 'file was rewritten despite being unchanged'

    def test_a_missing_file_reports_failure_rather_than_raising(self, app, tmp_path):
        """The except arm: callers rely on False, not on an exception."""
        assert sanitize_svg(str(tmp_path / 'does-not-exist.svg')) is False

    def test_an_oversize_file_reports_failure(self, app, tmp_path):
        path = tmp_path / 'huge.svg'
        path.write_bytes(b'a' * (MAX_SVG_SIZE + 1))
        assert sanitize_svg(str(path)) is False

    def test_a_file_the_sanitizer_rejects_is_left_hostile_on_disk(self, app, tmp_path):
        """SECURITY: False means "not sanitised", and the file keeps its payload.

        `sanitize_svg` writes only when sanitisation succeeded, so when it returns
        False the original bytes are still on disk -- including the <script>. The
        input used here is well-formed XML that lxml parses happily (it is only
        PieFed's own pre-strip regex that breaks it), so a browser served this
        file renders it and runs the script.

        This asserts `sanitize_svg`'s observable contract, which is all this
        module can see. Whether that is exploitable depends on the callers:
        app/shared/upload.py:46 and app/shared/post.py:496 both discard the
        return value, and app/shared/upload.py:63 skips Pillow re-encoding for
        '.svg', so nothing downstream re-checks the file. Reported, not fixed.
        """
        path = tmp_path / 'quoted-gt.svg'
        path.write_bytes(DOCTYPE_GT_IN_QUOTED_VALUE)

        assert sanitize_svg(str(path)) is False
        assert path.read_bytes() == DOCTYPE_GT_IN_QUOTED_VALUE
        assert b'<script>alert(1)</script>' in path.read_bytes()

    def test_an_oversize_file_also_keeps_its_payload(self, app, tmp_path):
        """SECURITY, same shape: the size guard skips sanitisation entirely.

        An SVG over 10 MB is never sanitised -- the ValueError is raised before
        filter_svg is reached -- yet the file is left on disk unchanged with its
        script intact. Padding here is a comment, so the document stays
        well-formed and renderable.
        """
        path = tmp_path / 'huge-hostile.svg'
        payload = (b'<svg xmlns="http://www.w3.org/2000/svg">'
                   b'<script>alert(1)</script>'
                   b'<!--' + b'A' * MAX_SVG_SIZE + b'--></svg>')
        path.write_bytes(payload)

        assert sanitize_svg(str(path)) is False
        assert path.read_bytes() == payload
        assert b'<script>alert(1)</script>' in path.read_bytes()

    def test_a_directory_path_reports_failure(self, app, tmp_path):
        """IsADirectoryError also has to come back as False, not propagate."""
        assert sanitize_svg(str(tmp_path)) is False
