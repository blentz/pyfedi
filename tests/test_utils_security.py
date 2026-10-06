"""Rejection paths for the XML and SVG input filters in app/utils.py.

These functions exist to reject hostile input, so the expectations here are
derived from the primary sources rather than from the implementation:

* XML 1.0 (Fifth Edition) section 2.2, the ``Char`` production --
  ``#x9 | #xA | #xD | [#x20-#xD7FF] | [#xE000-#xFFFD] | [#x10000-#x10FFFF]``
  (https://www.w3.org/TR/xml/#charsets)
* XML 1.0 section 2.4 (a literal '<' may not appear in character data or in an
  attribute value), section 2.5 (comments), section 2.7 (CDATA), section 2.8
  (EntityValue may contain a quoted '>')
* RFC 3629 section 3, which forbids overlong forms, surrogate encodings,
  sequences above U+10FFFF, and the bytes C0/C1/F5-FF; and section 10, on
  respelling a code point to defeat a filter
* OWASP XML External Entity Prevention Cheat Sheet
* OWASP XSS Filter Evasion Cheat Sheet
* SVG 1.1 / SVG 2 for element and attribute semantics

An earlier revision of this file carried sixteen ``xfail(strict=True)`` marks
recording places where the implementation contradicted those sources. All of
them have been fixed and the marks are gone; the assertions they guarded are
unchanged and now pass. No test in this file is expected to fail.
"""

import os
from io import BytesIO

import httpx
import pytest
from py_svg_hush import filter_svg
from werkzeug.datastructures import FileStorage
from werkzeug.exceptions import BadRequest

from app.community.util import save_banner_file, save_icon_file
from app.shared.upload import process_upload
from app.utils import (MAX_SVG_SIZE, gibberish, is_valid_xml_utf8, refuse_svg_entity_declarations,
                       sanitize_svg, sanitize_svg_bytes, url_to_thumbnail_file)

KEEP_DATA_URL_MIME_TYPES = {"image": ["jpeg", "png", "gif", "webp", "avif"]}

# Every SVG-to-disk path in app/ writes below this, relative to the repo root,
# which is the working directory the app runs from. It is gitignored; the tests
# that write here clean up after themselves.
MEDIA_ROOT = 'app/static'

pytestmark = pytest.mark.usefixtures('private_static_tree')


def files_under(root: str) -> list:
    """Every regular file below `root`, for before/after comparison."""
    found = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            found.append(os.path.join(dirpath, name))
    return found

# A well-formed SVG whose <!DOCTYPE> internal subset contains a '>' inside a
# quoted entity value. XML 1.0 section 2.8 permits that, and lxml parses this
# document happily -- it is only a non-DTD-aware stripper that breaks on it,
# because a non-greedy rb'<\!.*?>' stops at the inner '>'. That is what used to
# make sanitize_svg fail and leave these bytes, <script> and all, on disk.
DOCTYPE_GT_IN_QUOTED_VALUE = (
    b'<!DOCTYPE svg [<!ENTITY greater "x>y">]>'
    b'<svg xmlns="http://www.w3.org/2000/svg">'
    b'<script>alert(1)</script></svg>'
)


class TestIsValidXmlUtf8:
    """Strict UTF-8 decode, then the XML 1.0 Char production, per code point."""

    def test_plain_ascii_is_valid(self):
        assert is_valid_xml_utf8(b'hello world') is True

    def test_a_str_is_accepted_as_well_as_bytes(self):
        """Both production callers (the RSS builders) pass str."""
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

    def test_delete_is_permitted_by_the_char_production(self):
        """XML 1.0 5e section 2.2: Char includes ``[#x20-#xD7FF]``, and #x7F is in it.

        DEL is listed only under the *discouraged* compatibility characters note
        ("[#x7F-#x84] and [#x86-#x9F]"), which is advisory prose, not part of the
        grammar. A conforming parser accepts it: ElementTree parses
        b'<r>\\x7f</r>' without error.

        This is a deliberate behaviour change. The old byte scanner rejected
        #x7F, which silently dropped any post containing a DEL character from the
        RSS feeds built in app/user/routes.py:2234 and :2238. Such posts now
        appear, which is what XML 1.0 requires.
        """
        assert is_valid_xml_utf8(b'\x7f') is True

    def test_a_control_char_early_in_a_long_string_is_rejected(self):
        assert is_valid_xml_utf8(b'aaaaaaaaaa\x00aaaaaaaaaa') is False

    def test_a_control_char_at_the_very_end_of_a_string_is_rejected(self):
        """The predecessor scanned in two loops split at c_end - 2, and a control
        character in the final two bytes was reachable only by the second. The
        position dependence is gone, but the case is kept as a regression guard.
        """
        assert is_valid_xml_utf8(b'aaaaaaaaaa\x00') is False

    def test_forbidden_fffe_is_rejected(self):
        """U+FFFE falls in the gap between Char's [#xE000-#xFFFD] and [#x10000-...]."""
        assert is_valid_xml_utf8(b'\xef\xbf\xbe') is False

    def test_forbidden_ffff_is_rejected(self):
        """U+FFFF is likewise outside every Char alternative."""
        assert is_valid_xml_utf8(b'\xef\xbf\xbf') is False

    def test_fffd_the_replacement_character_is_valid(self):
        """Boundary on the other side: Char's [#xE000-#xFFFD] ends at U+FFFD.

        Narrowing that range to #xFFFC makes this fail.
        """
        assert is_valid_xml_utf8(b'\xef\xbf\xbd') is True

    def test_surrogate_range_is_rejected(self):
        """Char stops at #xD7FF and resumes at #xE000, excluding D800-DFFF.

        As bytes these are also invalid UTF-8 (RFC 3629 forbids encoding a
        surrogate), so they are refused at the decode step.
        """
        assert is_valid_xml_utf8(b'\xed\xa0\x80') is False   # U+D800, low end
        assert is_valid_xml_utf8(b'\xed\xbf\xbf') is False   # U+DFFF, high end

    def test_the_code_points_around_the_surrogate_block_are_valid(self):
        """Boundaries of Char's [#x20-#xD7FF] and [#xE000-#xFFFD] alternatives."""
        assert is_valid_xml_utf8('퟿') is True
        assert is_valid_xml_utf8('') is True

    def test_astral_plane_text_is_valid(self):
        """Char's [#x10000-#x10FFFF] alternative, at both ends."""
        assert is_valid_xml_utf8('\U00010000') is True
        assert is_valid_xml_utf8('\U0010ffff') is True

    def test_legitimate_multibyte_text_is_valid(self):
        assert is_valid_xml_utf8('日本語のテキスト'.encode('utf-8')) is True

    def test_empty_input_is_valid(self):
        assert is_valid_xml_utf8(b'') is True

    def test_short_inputs_do_not_raise(self):
        """1- and 2-byte inputs used to be the edge case for the loop bounds."""
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
        """No offset in the buffer lets a forbidden sequence through.

        The predecessor scanned bytes in two loops with hand-written bounds, and
        whether a forbidden 3-byte window was examined depended on where it sat.
        Checking decoded code points makes position irrelevant, but the sweep is
        kept: it is cheap, and it would catch any future reintroduction of a
        position-sensitive scan.
        """
        for pos in range(total_length - 2):
            raw = b'a' * pos + forbidden + b'a' * (total_length - pos - 3)
            assert len(raw) == total_length
            assert is_valid_xml_utf8(raw) is False, f'escaped at offset {pos}'


class TestIsValidXmlUtf8EncodingValidity:
    """RFC 3629 section 3: byte sequences that denote no code point at all.

    Every input below raises UnicodeDecodeError in Python and is rejected by
    ElementTree when wrapped in an element. The predecessor searched for specific
    byte *patterns* and never checked that its input decoded as UTF-8, so it
    reported all of them as valid XML content.
    """

    @pytest.mark.parametrize('raw', [
        pytest.param(b'\xc0\x80', id='overlong-2-byte-NUL'),
        pytest.param(b'\xe0\x80\x80', id='overlong-3-byte-NUL'),
        pytest.param(b'\xf0\x80\x80\x80', id='overlong-4-byte-NUL'),
        pytest.param(b'\xc1\xaf', id='overlong-2-byte-solidus'),
    ])
    def test_overlong_forms_are_rejected(self, raw):
        """RFC 3629 section 3: "implementations MUST reject" non-shortest forms.

        These matter beyond pedantry: b'\\xc0\\x80' and b'\\xe0\\x80\\x80' both
        denote U+0000, which test_nul_is_rejected shows is rejected when spelled
        b'\\x00'. The overlong spelling used to walk straight past it.
        """
        assert is_valid_xml_utf8(raw) is False

    @pytest.mark.parametrize('raw', [
        pytest.param(b'\x80', id='lone-continuation'),
        pytest.param(b'ab\x80cd', id='continuation-mid-string'),
    ])
    def test_lone_continuation_bytes_are_rejected(self, raw):
        """RFC 3629 section 3: 80-BF may appear only after a valid lead byte."""
        assert is_valid_xml_utf8(raw) is False

    @pytest.mark.parametrize('raw', [
        pytest.param(b'\xe2\x82', id='truncated-3-byte'),
        pytest.param(b'\xf0\x9f\x98', id='truncated-4-byte'),
    ])
    def test_truncated_sequences_are_rejected(self, raw):
        """RFC 3629 section 3: a lead byte must be followed by its full complement."""
        assert is_valid_xml_utf8(raw) is False

    @pytest.mark.parametrize('raw', [
        pytest.param(b'\xf5\x80\x80\x80', id='above-U+10FFFF'),
        pytest.param(b'\xfe', id='byte-FE'),
        pytest.param(b'\xff', id='byte-FF'),
    ])
    def test_bytes_that_can_never_appear_in_utf8_are_rejected(self, raw):
        """RFC 3629 section 3: F5-FF never occur in a valid UTF-8 sequence."""
        assert is_valid_xml_utf8(raw) is False

    @pytest.mark.parametrize('raw', [
        pytest.param(b'\xf0\x8f\xbf\xbe', id='overlong-U+FFFE'),
        pytest.param(b'\xf0\x8f\xbf\xbf', id='overlong-U+FFFF'),
        pytest.param(b'\xf0\x8d\xa0\x80', id='overlong-U+D800'),
    ])
    def test_forbidden_code_points_are_rejected_in_their_overlong_spelling(self, raw):
        """SECURITY: the same code points test_forbidden_fffe_is_rejected covers.

        A byte-pattern scanner recognises only the *shortest* spelling of each
        forbidden code point. Re-spelling one as a longer sequence -- exactly the
        technique RFC 3629 section 10 warns about, "a different way to represent
        the same character" -- used to defeat the filter outright. Decoding
        before checking closes it: the overlong form no longer decodes at all,
        and if it did it would decode to the same code point its shortest form
        does, and be caught by the same test.
        """
        assert is_valid_xml_utf8(raw) is False

    def test_a_lone_surrogate_str_is_rejected(self):
        """XML 1.0 section 2.2: D800-DFFF is not a Char.

        Deliberate behaviour change. This used to return True: the function's
        first act was ``pystring.encode('utf-8', errors='ignore')``, which
        silently deletes a lone surrogate, so the scanner saw b'' and reported
        valid -- while the caller went on to hand the original str to feedgen,
        which cannot serialise it. A str is now checked directly, with no
        encode/decode round trip to lose the information.
        """
        assert is_valid_xml_utf8('\ud800') is False

    def test_a_lone_surrogate_is_rejected_inside_otherwise_valid_text(self):
        assert is_valid_xml_utf8('before\ud800after') is False


# The DOCTYPE Adobe Illustrator and older Inkscape put at the top of an SVG 1.1
# export. It declares no entities, so it is accepted -- refusing it would break
# uploads from the two most common SVG producers while buying nothing, since
# filter_svg never fetches an external DTD (see
# TestFilterSvgIsSafeWithoutPreStripping).
ILLUSTRATOR_DOCTYPE = (
    b'<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" '
    b'"http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">'
)

# The same amplifying document in both UTF-16 byte orders. XML 1.0 section 4.3.3
# requires a UTF-16 entity to begin with a byte order mark, and py-svg-hush
# refuses UTF-16 without one, so these two are the whole of the reachable
# UTF-16 surface. Neither contains the ASCII bytes b'<!ENTITY', which is what
# made them invisible to the refusal scan; 1000 references to a 1 KB entity
# amplify a ~6 KB input into ~1 MB of output.
_UTF16_ENTITY_SOURCE = ('<!DOCTYPE svg [<!ENTITY a "' + 'A' * 1000 + '">]>'
                        '<svg xmlns="http://www.w3.org/2000/svg"><text>' +
                        '&a;' * 1000 + '</text></svg>')
UTF16_ENTITY_SVG_LE = b'\xff\xfe' + _UTF16_ENTITY_SOURCE.encode('utf-16-le')
UTF16_ENTITY_SVG_BE = b'\xfe\xff' + _UTF16_ENTITY_SOURCE.encode('utf-16-be')


class TestRefuseSvgEntityDeclarations:
    """PieFed layer: uploaded SVGs that declare an XML entity are refused.

    Refusal replaced a non-greedy ``rb'<\\!.*?>'`` substitution that tried to
    strip declarations out. That substitution was both bypassable (XML 1.0
    section 2.8 lets a quoted EntityValue contain '>', which ends the match
    early) and destructive to valid documents (a comment or CDATA section
    containing '>' was cut in half). Refusing can be neither.

    The predicate is the entity declaration, not the DOCTYPE: entity declarations
    are the actual threat, and a DTD that declares none is inert.
    """

    def test_a_plain_svg_is_accepted(self):
        assert refuse_svg_entity_declarations(
            b'<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>') is None

    def test_a_doctype_that_declares_no_entities_is_accepted(self):
        """REGRESSION GUARD. Refusing every DOCTYPE would reject SVG 1.1 exports
        from Illustrator and Inkscape, which is a pure availability cost: it buys
        no security, because filter_svg never fetches the DTD this names."""
        assert refuse_svg_entity_declarations(
            ILLUSTRATOR_DOCTYPE + b'<svg xmlns="http://www.w3.org/2000/svg"/>') is None

    def test_an_entity_declaration_is_refused(self):
        with pytest.raises(ValueError, match='entity declarations'):
            refuse_svg_entity_declarations(
                b'<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
                b'<svg xmlns="http://www.w3.org/2000/svg"/>')

    @pytest.mark.parametrize('spelling', [
        pytest.param(b'<!ENTITY', id='canonical-uppercase'),
        pytest.param(b'<!entity', id='lowercase'),
        pytest.param(b'<!EnTiTy', id='mixed-case'),
    ])
    def test_the_entity_match_is_case_insensitive(self, spelling):
        """Only uppercase `<!ENTITY` is a real XML declaration, so the other
        spellings would be refused by the parser anyway. Matching them here is
        deliberate slack in the refusing direction; the '-->' and ']]>'
        terminators are matched exactly, because slack there would mean skipping
        bytes unexamined."""
        with pytest.raises(ValueError, match='entity declarations'):
            refuse_svg_entity_declarations(
                b'<!DOCTYPE svg [' + spelling + b' e "x">]>'
                b'<svg xmlns="http://www.w3.org/2000/svg"/>')

    @pytest.mark.parametrize('declaration', [
        pytest.param(b'<!ELEMENT svg ANY>', id='ELEMENT'),
        pytest.param(b'<!ATTLIST svg x CDATA #IMPLIED>', id='ATTLIST'),
        pytest.param(b'<!ATTLIST svg onload CDATA "alert(1)">', id='ATTLIST-with-default'),
        pytest.param(b'<!NOTATION gif SYSTEM "gif">', id='NOTATION'),
    ])
    def test_a_non_entity_declaration_is_accepted(self, declaration):
        """These declare no entity and are inert: filter_svg does not apply
        attribute defaults declared in an internal subset, which the ATTLIST case
        above is the interesting one for. Verified by execution; the
        end-to-end consequence is pinned by
        test_an_attlist_default_does_not_inject_an_attribute below."""
        assert refuse_svg_entity_declarations(
            b'<!DOCTYPE svg [' + declaration + b']>'
            b'<svg xmlns="http://www.w3.org/2000/svg"/>') is None

    def test_an_entity_inside_a_conditional_section_is_still_refused(self):
        """`<![INCLUDE[` is neither a comment nor a CDATA section, so the scan
        advances two bytes and keeps looking rather than skipping its body."""
        with pytest.raises(ValueError, match='entity declarations'):
            refuse_svg_entity_declarations(
                b'<![INCLUDE[<!ENTITY e "x">]]>'
                b'<svg xmlns="http://www.w3.org/2000/svg"/>')

    def test_an_entity_declared_alongside_other_declarations_is_still_found(self):
        """A non-entity declaration must not consume the entity behind it."""
        with pytest.raises(ValueError, match='entity declarations'):
            refuse_svg_entity_declarations(
                b'<!DOCTYPE svg [<!ELEMENT svg ANY><!ENTITY e "x">]>'
                b'<svg xmlns="http://www.w3.org/2000/svg"/>')

    def test_an_entity_declaration_after_the_root_element_is_refused(self):
        """The scan is not limited to the prolog, so a declaration cannot be
        hidden by putting it somewhere a prolog-only check would not look."""
        with pytest.raises(ValueError, match='entity declarations'):
            refuse_svg_entity_declarations(
                b'<svg xmlns="http://www.w3.org/2000/svg">'
                b'<!ENTITY e "x"></svg>')

    def test_a_comment_is_not_mistaken_for_a_declaration(self):
        """XML 1.0 section 2.5. Comments begin '<!' and must be skipped, not
        refused -- this is what makes the check exact rather than a grep."""
        assert refuse_svg_entity_declarations(
            b'<svg xmlns="http://www.w3.org/2000/svg"><!-- hello --><rect/></svg>') is None

    def test_a_comment_containing_an_entity_declaration_is_not_refused(self):
        """A substring search for '<!ENTITY' would refuse this valid document.
        Skipping the comment by its '-->' terminator does not."""
        assert refuse_svg_entity_declarations(
            b'<svg xmlns="http://www.w3.org/2000/svg">'
            b'<!-- <!ENTITY e "x"> --><rect/></svg>') is None

    def test_a_cdata_section_containing_an_entity_declaration_is_not_refused(self):
        """XML 1.0 section 2.7: inside CDATA this is ordinary character data."""
        assert refuse_svg_entity_declarations(
            b'<svg xmlns="http://www.w3.org/2000/svg">'
            b'<text><![CDATA[<!ENTITY e "x">]]></text></svg>') is None

    def test_a_comment_containing_gt_is_not_refused(self):
        """The exact case the old stripper corrupted. '>' is legal in a comment
        and must not terminate anything."""
        assert refuse_svg_entity_declarations(
            b'<svg xmlns="http://www.w3.org/2000/svg"><!-- a > b --><rect/></svg>') is None

    def test_an_unterminated_comment_is_refused(self):
        """Not well-formed XML, so there is no valid document to preserve, and
        an unterminated comment could otherwise hide the rest of the file."""
        with pytest.raises(ValueError, match='unterminated comment'):
            refuse_svg_entity_declarations(
                b'<svg xmlns="http://www.w3.org/2000/svg"><!-- <rect/></svg>')

    def test_an_unterminated_cdata_section_is_refused(self):
        with pytest.raises(ValueError, match='unterminated CDATA'):
            refuse_svg_entity_declarations(
                b'<svg xmlns="http://www.w3.org/2000/svg"><text><![CDATA[x</text></svg>')

    def test_an_entity_declaration_after_a_comment_is_still_found(self):
        """Skipping a comment must resume scanning, not stop."""
        with pytest.raises(ValueError, match='entity declarations'):
            refuse_svg_entity_declarations(
                b'<svg xmlns="http://www.w3.org/2000/svg"><!-- x -->'
                b'<!ENTITY e "y"><rect/></svg>')

    def test_an_entity_declaration_after_a_cdata_section_is_still_found(self):
        with pytest.raises(ValueError, match='entity declarations'):
            refuse_svg_entity_declarations(
                b'<svg xmlns="http://www.w3.org/2000/svg"><text><![CDATA[x]]></text>'
                b'<!ENTITY e "y"><rect/></svg>')

    def test_an_entity_declaration_quoting_a_gt_is_refused(self):
        """XML 1.0 section 2.8 lets an EntityValue contain '>' when quoted, so
        `<!ENTITY greater "x>y">` is a legal declaration whose closing '>' is not
        the first one. That ambiguity is exactly why this refuses rather than
        strips: there is nothing to get wrong."""
        with pytest.raises(ValueError, match='entity declarations'):
            refuse_svg_entity_declarations(DOCTYPE_GT_IN_QUOTED_VALUE)

    @pytest.mark.parametrize('encoded', [
        pytest.param(UTF16_ENTITY_SVG_LE, id='utf-16-le-bom'),
        pytest.param(UTF16_ENTITY_SVG_BE, id='utf-16-be-bom'),
    ])
    def test_a_utf16_svg_is_refused(self, encoded):
        """SECURITY. The scan reads ASCII bytes, so `<!ENTITY` encoded as UTF-16
        is invisible to it -- the literal byte string is simply not present:

            b'<!ENTITY' in svg.encode('utf-16')  ->  False

        and the declaration sailed through untouched. filter_svg DOES accept
        UTF-16 (it transcodes to UTF-8 on output), so this was a real bypass of
        the entity refusal, and combined with the flat-expansion amplification
        pinned in TestFilterSvgIsSafeWithoutPreStripping it is reachable
        amplification, not a theoretical gap.

        The fix refuses UTF-16 outright rather than decoding it first. Nothing
        in PieFed produces or expects a UTF-16 SVG, and filter_svg's output is
        UTF-8 regardless, so a UTF-16 upload was never stored as UTF-16 anyway.
        """
        assert b'<!ENTITY' not in encoded, 'the ASCII scan cannot see this declaration'
        with pytest.raises(ValueError, match='UTF-16'):
            refuse_svg_entity_declarations(encoded)

    def test_a_clean_utf16_svg_is_refused_too(self):
        """The refusal is on the encoding, not on what the document contains --
        that is what makes it sound. A UTF-16 document with nothing hostile in
        it is refused as well, and that availability cost is the price of not
        having to decode attacker-chosen bytes before scanning them."""
        clean = ('<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>').encode('utf-16')
        with pytest.raises(ValueError, match='UTF-16'):
            refuse_svg_entity_declarations(clean)

    def test_a_utf8_bom_is_not_mistaken_for_a_utf16_one(self):
        """A UTF-8 BOM (EF BB BF) is ASCII-compatible from byte three onwards,
        so the scan works on it and it must not be caught by the UTF-16 guard.
        filter_svg accepts such a document."""
        assert refuse_svg_entity_declarations(
            b'\xef\xbb\xbf<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>') is None

    def test_a_legacy_eight_bit_encoding_is_still_accepted(self):
        """REGRESSION GUARD for the narrowness of the UTF-16 refusal.

        Refusing everything that does not decode as UTF-8 would also refuse a
        declared ISO-8859-1 document, which filter_svg accepts today. It needs
        no special handling: every ASCII-compatible encoding spells `<!ENTITY`
        with exactly those bytes, so the scan is already sound over it.
        """
        latin1 = ('<?xml version="1.0" encoding="ISO-8859-1"?>'
                  '<svg xmlns="http://www.w3.org/2000/svg"><text>caf\xe9</text></svg>'
                  ).encode('latin-1')
        assert refuse_svg_entity_declarations(latin1) is None

    def test_an_entity_in_a_legacy_eight_bit_encoding_is_still_refused(self):
        latin1 = ('<!DOCTYPE svg [<!ENTITY e "caf\xe9">]>'
                  '<svg xmlns="http://www.w3.org/2000/svg"><text>&e;</text></svg>'
                  ).encode('latin-1')
        with pytest.raises(ValueError, match='entity declarations'):
            refuse_svg_entity_declarations(latin1)


class TestFilterSvgIsSafeWithoutPreStripping:
    """py-svg-hush's own behaviour, pinned because sanitize_svg_bytes relies on it.

    Removing PieFed's byte-level pre-stripping is only sound if filter_svg is
    itself safe against XXE and entity expansion. These call filter_svg directly,
    bypassing PieFed's declaration refusal entirely, so they measure the library
    and nothing else. If a future py-svg-hush starts resolving external entities
    or amplifying internal ones, these fail and the refusal above stops being
    defence in depth and becomes load-bearing.
    """

    def test_an_external_entity_naming_a_local_file_is_never_dereferenced(self):
        """OWASP XXE Prevention. The entity expands to nothing; the file is not read."""
        raw = (b'<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
               b'<svg xmlns="http://www.w3.org/2000/svg"><text>&xxe;</text></svg>')
        result = filter_svg(raw, KEEP_DATA_URL_MIME_TYPES)
        assert b'root:' not in result
        assert b'/bin/' not in result
        assert b'<text/>' in result

    def test_an_external_dtd_is_never_fetched(self):
        """A SYSTEM identifier on the DOCTYPE itself. Port 9 (discard) would hang
        or refuse if it were dereferenced; the document sanitises instead."""
        raw = (b'<!DOCTYPE svg SYSTEM "http://127.0.0.1:9/evil.dtd">'
               b'<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>')
        assert b'<rect/>' in filter_svg(raw, KEEP_DATA_URL_MIME_TYPES)

    def test_a_parameter_entity_naming_a_local_file_is_never_dereferenced(self):
        raw = (b'<!DOCTYPE svg [<!ENTITY % pe SYSTEM "file:///etc/passwd">%pe;]>'
               b'<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>')
        result = filter_svg(raw, KEEP_DATA_URL_MIME_TYPES)
        assert b'root:' not in result

    def test_a_nested_entity_reference_expands_within_a_budget(self):
        """Nesting is NOT refused outright. An expansion budget is what stops
        billion laughs, and this test names the budget rather than a property
        py-svg-hush does not have.

        Measured against the installed py-svg-hush: expanding ONE entity's value
        may itself trigger at most TWO further expansions, counted
        transitively. So an entity value holding two references expands, and one
        holding three does not; a chain of three entities expands and a chain of
        four does not. The budget is per top-level expansion, not global -- ten
        independent depth-2 entities all expand in the same document.

        An earlier revision of this test asserted that a nested reference was
        "refused outright". It passed, but for the wrong reason: the fanout-ten
        payload it used blows the budget on its first entity, so the test would
        have kept passing even if nesting had been permitted to arbitrary depth.
        """
        expands = (b'<!DOCTYPE svg [<!ENTITY a "AAAA"><!ENTITY b "&a;&a;">]>'
                   b'<svg xmlns="http://www.w3.org/2000/svg"><text>&b;</text></svg>')
        assert b'AAAAAAAA' in filter_svg(expands, KEEP_DATA_URL_MIME_TYPES)

        chain_of_three = (b'<!DOCTYPE svg [<!ENTITY a "AAAA"><!ENTITY b "&a;">'
                          b'<!ENTITY c "&b;">]>'
                          b'<svg xmlns="http://www.w3.org/2000/svg"><text>&c;</text></svg>')
        assert b'AAAA' in filter_svg(chain_of_three, KEEP_DATA_URL_MIME_TYPES)

        over_budget = (b'<!DOCTYPE svg [<!ENTITY a "AAAA"><!ENTITY b "&a;&a;&a;">]>'
                       b'<svg xmlns="http://www.w3.org/2000/svg"><text>&b;</text></svg>')
        with pytest.raises(ValueError, match='XML parsing error'):
            filter_svg(over_budget, KEEP_DATA_URL_MIME_TYPES)

        chain_of_four = (b'<!DOCTYPE svg [<!ENTITY a "AAAA"><!ENTITY b "&a;">'
                         b'<!ENTITY c "&b;"><!ENTITY d "&c;">]>'
                         b'<svg xmlns="http://www.w3.org/2000/svg"><text>&d;</text></svg>')
        with pytest.raises(ValueError, match='XML parsing error'):
            filter_svg(chain_of_four, KEEP_DATA_URL_MIME_TYPES)

    def test_billion_laughs_exceeds_the_budget_rather_than_being_banned(self):
        """Why the classic payload never gets off the ground: a fanout of ten
        exceeds the two-expansion budget on the very first nested entity, so it
        is refused at depth 2 just as at depth 9. That is the budget doing the
        work, not a ban on nesting."""
        depth2 = (b'<!DOCTYPE lolz [<!ENTITY lol "lol">'
                  b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>'
                  b'<svg xmlns="http://www.w3.org/2000/svg"><text>&lol2;</text></svg>')
        with pytest.raises(ValueError, match='XML parsing error'):
            filter_svg(depth2, KEEP_DATA_URL_MIME_TYPES)

    def test_a_flat_expansion_amplifies_and_is_bounded_only_by_a_total_budget(self):
        """The amplification that IS real, recorded honestly.

        The per-entity-value budget above does not apply to references in
        ELEMENT CONTENT: each one is expanded independently, so a single
        non-nested entity referenced many times amplifies without limit until a
        TOTAL expansion budget of 256 MiB is hit. That budget was measured
        exactly against the installed py-svg-hush -- 255.990 MiB of expansion
        succeeds and 256.010 MiB is refused -- and a 787 KB input reaches it, so
        PieFed's 10 MB MAX_SVG_SIZE input cap does not bound the output.

        This test proves the amplification exists using a SMALL document: about
        4 KB in, about 1 MB out, a factor of roughly 250. The 256 MiB figure
        itself is exercised only by
        TestFilterSvgExpansionBudgetIsExpensive::test_the_total_expansion_budget_is_256_mib,
        which is skipped unless PIEFED_EXPENSIVE_SVG_TESTS is set, because
        reaching it costs about half a gigabyte of RSS.

        None of this is reachable through PieFed's own SVG entry points --
        refuse_svg_entity_declarations rejects the declaration before filter_svg
        sees it -- which is exactly why that refusal must not have an encoding
        blind spot. See TestRefuseSvgEntityDeclarations' UTF-16 cases.
        """
        raw = (b'<!DOCTYPE svg [<!ENTITY a "' + b'A' * 1000 + b'">]>'
               b'<svg xmlns="http://www.w3.org/2000/svg"><text>' +
               b'&a;' * 1000 + b'</text></svg>')
        result = filter_svg(raw, KEEP_DATA_URL_MIME_TYPES)

        assert len(raw) < 5 * 1024, 'the input for this test must stay small'
        assert len(result) > 1000 * 1000
        assert len(result) > 200 * len(raw)

    def test_an_entity_cannot_smuggle_a_script_element(self):
        """Entity expansion happens before filtering, so an expanded <script>
        would still be seen as a script element and removed. It is not smuggled
        past the filter as text either."""
        raw = (b'<!DOCTYPE svg [<!ENTITY e "<script>alert(1)</script>">]>'
               b'<svg xmlns="http://www.w3.org/2000/svg">&e;</svg>')
        result = filter_svg(raw, KEEP_DATA_URL_MIME_TYPES).lower()
        assert b'script' not in result
        assert b'alert' not in result

    def test_an_entity_cannot_smuggle_a_javascript_url(self):
        raw = (b'<!DOCTYPE svg [<!ENTITY js "javascript:alert(1)">]>'
               b'<svg xmlns="http://www.w3.org/2000/svg" '
               b'xmlns:xlink="http://www.w3.org/1999/xlink">'
               b'<a xlink:href="&js;"><rect/></a></svg>')
        assert b'javascript' not in filter_svg(raw, KEEP_DATA_URL_MIME_TYPES).lower()

    def test_an_attlist_default_does_not_inject_an_attribute(self):
        """The reason a non-entity internal subset can be let through.

        XML lets an ATTLIST declare a DEFAULT value for an attribute, which a
        conforming parser applies to every matching element. If filter_svg's
        parser did that, a DTD with no entity in it could still inject onload.
        It does not: the attribute never appears.
        """
        raw = (b'<!DOCTYPE svg [<!ATTLIST svg onload CDATA "alert(1)">]>'
               b'<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>')
        result = filter_svg(raw, KEEP_DATA_URL_MIME_TYPES).lower()
        assert b'onload' not in result
        assert b'alert' not in result

    def test_a_doctype_alone_does_not_stop_filter_svg_sanitising(self):
        """The document PieFed used to mangle into unparseable garbage. Handed to
        filter_svg intact, the <script> is removed and the document survives."""
        result = filter_svg(DOCTYPE_GT_IN_QUOTED_VALUE, KEEP_DATA_URL_MIME_TYPES)
        assert b'script' not in result.lower()
        assert b'<svg' in result


@pytest.mark.skipif(
    not os.environ.get('PIEFED_EXPENSIVE_SVG_TESTS'),
    reason='allocates ~500 MB of RSS; set PIEFED_EXPENSIVE_SVG_TESTS=1 to run',
)
class TestFilterSvgExpansionBudgetIsExpensive:
    """The exact 256 MiB total expansion budget, measured rather than asserted.

    Deliberately NOT part of the normal suite: proving this figure means
    actually materialising a quarter of a gigabyte of expanded text, which
    peaked at 505 MB RSS when measured. The cheap consequence of the same
    budget -- that a small document amplifies by a factor of hundreds -- is
    covered unconditionally by
    TestFilterSvgIsSafeWithoutPreStripping::test_a_flat_expansion_amplifies_and_is_bounded_only_by_a_total_budget.

    Run with:

        PIEFED_EXPENSIVE_SVG_TESTS=1 ./run_tests.sh tests/test_utils_security.py -q
    """

    EXPANSION_BUDGET = 256 * 1024 * 1024

    @staticmethod
    def _document(expanded_bytes: int, unit: int = 1024) -> bytes:
        return (b'<!DOCTYPE svg [<!ENTITY a "' + b'A' * unit + b'">]>'
                b'<svg xmlns="http://www.w3.org/2000/svg"><text>' +
                b'&a;' * (expanded_bytes // unit) + b'</text></svg>')

    def test_the_total_expansion_budget_is_256_mib(self):
        under = self._document(self.EXPANSION_BUDGET - 10 * 1024)
        assert len(under) < 1024 * 1024, 'the INPUT is under 1 MB; only the output is huge'
        assert len(filter_svg(under, KEEP_DATA_URL_MIME_TYPES)) > 255 * 1024 * 1024

        over = self._document(self.EXPANSION_BUDGET + 10 * 1024)
        with pytest.raises(ValueError, match='XML parsing error'):
            filter_svg(over, KEEP_DATA_URL_MIME_TYPES)


class TestSanitizeSvgBytes:
    """The XXE and script-injection boundary for uploaded SVGs.

    ``sanitize_svg_bytes`` is a size guard and a markup-declaration refusal of
    PieFed's own, then ``filter_svg`` from py-svg-hush. Nothing rewrites the
    bytes before filter_svg sees them. Tests below say which layer they pin.
    """

    def test_a_plain_svg_survives(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'
        assert b'<rect' in sanitize_svg_bytes(svg)

    def test_a_doctype_entity_declaration_is_refused(self):
        """OWASP XXE Prevention. PieFed layer: refused, not stripped, so there is
        no partly-processed document to get wrong."""
        svg = (b'<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
               b'<svg xmlns="http://www.w3.org/2000/svg"/>')
        with pytest.raises(ValueError, match='entity declarations'):
            sanitize_svg_bytes(svg)

    def test_a_reference_to_a_declared_entity_is_refused(self):
        svg = (b'<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
               b'<svg xmlns="http://www.w3.org/2000/svg"><text>&xxe;</text></svg>')
        with pytest.raises(ValueError, match='entity declarations'):
            sanitize_svg_bytes(svg)

    def test_billion_laughs_is_refused(self):
        """OWASP XXE Prevention, entity expansion. Refused at PieFed's layer for
        carrying a declaration; TestFilterSvgIsSafeWithoutPreStripping shows
        filter_svg refuses it independently."""
        svg = (b'<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>'
               b'<svg xmlns="http://www.w3.org/2000/svg"><text>&lol2;</text></svg>')
        with pytest.raises(ValueError, match='entity declarations'):
            sanitize_svg_bytes(svg)

    def test_a_processing_instruction_does_not_survive(self):
        """An xml-stylesheet PI can load remote XSLT. PieFed no longer strips PIs
        by regex -- filter_svg drops them, which this pins."""
        svg = b'<?xml-stylesheet href="evil.xsl"?><svg xmlns="http://www.w3.org/2000/svg"/>'
        assert b'<?xml-stylesheet' not in sanitize_svg_bytes(svg)

    def test_an_illustrator_doctype_is_sanitized_rather_than_refused(self):
        """REGRESSION GUARD for the DOCTYPE over-refusal we nearly shipped.

        The document is accepted and sanitised: the script goes, the drawing
        stays. Refusing every DOCTYPE would have turned every Illustrator and
        Inkscape SVG 1.1 export into a failed upload.
        """
        svg = (ILLUSTRATOR_DOCTYPE +
               b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
               b'<script>alert(1)</script><rect width="1" height="1"/></svg>')
        result = sanitize_svg_bytes(svg)
        assert b'script' not in result.lower()
        assert b'<rect' in result
        assert b'width="10"' in result

    def test_an_xml_declaration_is_accepted(self):
        """The other side of that: essentially every real SVG starts with one."""
        svg = (b'<?xml version="1.0" encoding="UTF-8"?>'
               b'<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>')
        assert b'<rect/>' in sanitize_svg_bytes(svg)

    @pytest.mark.parametrize('encoded', [
        pytest.param(UTF16_ENTITY_SVG_LE, id='utf-16-le-bom'),
        pytest.param(UTF16_ENTITY_SVG_BE, id='utf-16-be-bom'),
    ])
    def test_a_utf16_entity_svg_is_refused_rather_than_expanded(self, encoded):
        """SECURITY, at the layer PieFed's callers actually use.

        Without the UTF-16 guard this reached filter_svg with its declaration
        intact and came back roughly 1 MB of expanded text -- a ~166x
        amplification of a 6 KB input, and the entity refusal never saw a thing.
        """
        with pytest.raises(ValueError, match='UTF-16'):
            sanitize_svg_bytes(encoded)

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

    def test_a_doctype_quoting_a_gt_is_refused(self):
        """REGRESSION GUARD. XML 1.0 section 2.8 lets an EntityValue contain '>'
        when quoted, and a non-greedy `rb'<\\!.*?>'` strip stopped at that inner
        '>', leaving `y">]>` in front of the root element. filter_svg then
        refused the mangled document, sanitize_svg returned False, and the
        original bytes -- <script> included -- stayed on disk. The file is now
        refused for carrying a declaration at all, which no quoting can affect.
        """
        with pytest.raises(ValueError, match='entity declarations'):
            sanitize_svg_bytes(DOCTYPE_GT_IN_QUOTED_VALUE)

    def test_a_comment_containing_gt_does_not_corrupt_the_document(self):
        """REGRESSION GUARD. XML 1.0 section 2.5 allows '>' inside a comment.
        The old `rb'<\\!.*?>'` strip stopped at it and left ` b -->` behind as
        character data, which filter_svg escaped and emitted as *visible text* in
        the sanitised image. Nothing rewrites the bytes now, so the comment is
        simply dropped by filter_svg and the drawing is unchanged.
        """
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><!-- a > b --><rect/></svg>'
        result = sanitize_svg_bytes(svg)
        assert b'b --&gt;' not in result, 'comment tail leaked into the document as text'
        assert b'&gt;' not in result
        assert b'<rect/>' in result

    def test_a_cdata_section_containing_gt_survives_intact(self):
        """REGRESSION GUARD. XML 1.0 section 2.7: '>' is ordinary data inside
        CDATA. The old strip removed `<![CDATA[a >` and the `rb'\\]>'` cleanup ate
        the section terminator, silently changing the text from 'a > b' to ' b]'.
        """
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><text><![CDATA[a > b]]></text></svg>'
        result = sanitize_svg_bytes(svg)
        assert b'<text>a &gt; b</text>' in result
        assert b'<text> b]</text>' not in result

    def test_a_close_bracket_gt_in_text_is_not_eaten(self):
        """REGRESSION GUARD for the `rb'\\]>'` cleanup that existed only to tidy
        up after DOCTYPE stripping. It matched anywhere, including in ordinary
        character data.
        """
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><text>a]&gt;b</text></svg>'
        assert b'<text>a]&gt;b</text>' in sanitize_svg_bytes(svg)


class TestSanitizeSvgFile:
    def test_a_clean_file_is_left_alone_and_reports_success(self, app, tmp_path):
        path = tmp_path / 'clean.svg'
        original = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'
        path.write_bytes(original)

        assert sanitize_svg(str(path)) is True
        assert b'<rect' in path.read_bytes()

    def test_a_hostile_file_that_can_be_sanitized_is_rewritten_in_place(self, app, tmp_path):
        path = tmp_path / 'hostile.svg'
        path.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)">'
                         b'<script>alert(2)</script><rect/></svg>')

        assert sanitize_svg(str(path)) is True
        result = path.read_bytes()
        assert b'script' not in result.lower()
        assert b'onload' not in result.lower()
        assert b'<rect/>' in result

    def test_an_already_sanitized_file_is_not_rewritten(self, app, tmp_path):
        """The `if sanitized_svg != svg_bytes` false arm: no write when nothing changed.

        `sanitize_svg_bytes` is idempotent -- feeding it its own output is a fixed
        point -- so mtime is the observable: it is pinned to the epoch first, and
        an unnecessary rewrite would move it.

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
        """The except arm: callers rely on False, not on an exception.

        The failure path must also not create the file it was asked to clean.
        """
        missing = tmp_path / 'does-not-exist.svg'
        assert sanitize_svg(str(missing)) is False
        assert not missing.exists()

    def test_a_directory_path_reports_failure_without_removing_it(self, app, tmp_path):
        """IsADirectoryError has to come back as False, not propagate -- and the
        directory must survive: only a regular file is ever destroyed."""
        assert sanitize_svg(str(tmp_path)) is False
        assert tmp_path.is_dir()

    def test_a_file_the_sanitizer_rejects_does_not_survive_on_disk(self, app, tmp_path):
        """SECURITY, and the property the whole fix turns on.

        sanitize_svg writes only when sanitising succeeded, so a failure used to
        leave the attacker's original bytes untouched on disk -- <script> and
        all -- for any caller that ignored the False return. Both callers did.
        The file is now destroyed before False is returned, so the failure is
        safe whatever the caller does with it.

        The input is well-formed XML that lxml parses happily, so a browser
        served these bytes would render them and run the script.
        """
        path = tmp_path / 'quoted-gt.svg'
        path.write_bytes(DOCTYPE_GT_IN_QUOTED_VALUE)

        assert sanitize_svg(str(path)) is False
        assert not path.exists()

    def test_an_oversize_file_does_not_survive_on_disk(self, app, tmp_path):
        """SECURITY, same shape. An SVG over 10 MB is never sanitised -- the
        ValueError is raised before filter_svg is reached -- and used to be left
        on disk with its script intact. Padding here is a comment, so the
        document stays well-formed and renderable.
        """
        path = tmp_path / 'huge-hostile.svg'
        payload = (b'<svg xmlns="http://www.w3.org/2000/svg">'
                   b'<script>alert(1)</script>'
                   b'<!--' + b'A' * MAX_SVG_SIZE + b'--></svg>')
        path.write_bytes(payload)

        assert sanitize_svg(str(path)) is False
        assert not path.exists()

    def test_a_file_that_is_not_xml_at_all_does_not_survive(self, app, tmp_path):
        """An HTML file renamed to .svg: filter_svg refuses it, so it must go."""
        path = tmp_path / 'not-really.svg'
        path.write_bytes(b'<html><body><script>alert(1)</script></body></html>')

        assert sanitize_svg(str(path)) is False
        assert not path.exists()

    def test_a_utf16_file_does_not_survive_on_disk(self, app, tmp_path):
        """SECURITY. The UTF-16 refusal has to reach the file layer too: this
        used to be sanitised "successfully" -- filter_svg transcoded it and
        expanded its entity -- with the declaration never examined."""
        path = tmp_path / 'utf16.svg'
        path.write_bytes(UTF16_ENTITY_SVG_LE)

        assert sanitize_svg(str(path)) is False
        assert not path.exists()


class TestProcessUploadRejectsUnsanitizableSvg:
    """app/shared/upload.py's call site, driven for real.

    process_upload used to call sanitize_svg and discard the result, then carry
    on to register the file and serve it from its URL. The '.svg' branch skips
    the Pillow re-encode, so nothing downstream re-checked it.

    user=None keeps this out of the database: the File/user_file rows are only
    written when a user is passed. Uploads land under app/static/media/, which
    is gitignored; each test cleans up after itself.
    """

    @staticmethod
    def _upload(payload: bytes, filename: str = 'x.svg') -> FileStorage:
        return FileStorage(stream=BytesIO(payload), filename=filename)

    @staticmethod
    def _stored_path(url: str) -> str:
        """process_upload returns SERVER_URL + '/' + final_place minus 'app/'."""
        return 'app/static/' + url.split('/static/', 1)[1]

    def test_an_svg_that_cannot_be_sanitized_is_rejected(self, app):
        with app.app_context():
            with pytest.raises(Exception, match='could not be sanitized'):
                process_upload(self._upload(DOCTYPE_GT_IN_QUOTED_VALUE))

    def test_the_rejected_svg_is_not_left_anywhere_under_the_media_root(self, app):
        media_root = 'app/static'
        before = self._snapshot(media_root)
        with app.app_context():
            with pytest.raises(Exception, match='could not be sanitized'):
                process_upload(self._upload(DOCTYPE_GT_IN_QUOTED_VALUE))
        after = self._snapshot(media_root)

        new_files = sorted(set(after) - set(before))
        try:
            for path in new_files:
                assert b'<script' not in open(path, 'rb').read().lower(), \
                    f'hostile payload survived at {path}'
            assert new_files == [], f'rejected upload left files behind: {new_files}'
        finally:
            for path in new_files:
                os.remove(path)

    def test_an_illustrator_doctype_svg_uploads_and_is_sanitized(self, app):
        """REGRESSION GUARD, end to end, for the DOCTYPE over-refusal.

        An SVG 1.1 export carrying `<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG
        1.1//EN" ...>` -- what Illustrator and Inkscape emit, and the single most
        common shape of real-world SVG upload -- must reach the media root with
        its drawing intact and its script gone. Refusing on the DOCTYPE rather
        than on the entity declaration made this raise instead.
        """
        payload = (ILLUSTRATOR_DOCTYPE +
                   b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
                   b'<script>alert(1)</script><rect width="1" height="1"/></svg>')
        with app.app_context():
            url = process_upload(self._upload(payload, filename='logo.svg'))
            stored = self._stored_path(url)
            try:
                content = open(stored, 'rb').read()
                assert b'script' not in content.lower()
                assert b'<rect' in content
                assert b'width="10"' in content
            finally:
                os.remove(stored)

    def test_a_clean_svg_still_uploads_and_is_sanitized(self, app):
        """The happy path is not collateral damage: a legitimate SVG carrying a
        script is accepted, with the script removed."""
        payload = (b'<svg xmlns="http://www.w3.org/2000/svg">'
                   b'<script>alert(1)</script><rect width="1" height="1"/></svg>')
        with app.app_context():
            url = process_upload(self._upload(payload))
            stored = self._stored_path(url)
            try:
                content = open(stored, 'rb').read()
                assert b'script' not in content.lower()
                assert b'<rect' in content
            finally:
                os.remove(stored)

    @staticmethod
    def _snapshot(root: str) -> list:
        return files_under(root)


class TestSaveIconFileRejectsUnsanitizableSvg:
    """app/community/util.py's save_icon_file, driven for real.

    SECURITY. This is the avatar upload: app/user/routes.py:267 reaches it for
    any logged-in user, and '.svg' is in its allowed_extensions. It saved the
    uploaded bytes straight to the media root and then took the '.svg' branch
    commented "svgs don't need to be resized", which skips the Pillow re-encode
    -- so a hostile SVG was stored verbatim and served from this site's own
    origin. Stored XSS, reachable by any account.

    The rejection is abort(400), which is this function's own convention for an
    upload it will not accept: it is what the disallowed-extension check at the
    top and the trailing else both do, and it is the only way out of the
    function that is not a File, since save_icon_file never returns None. A
    caller's `if file:` cannot swallow it.
    """

    @staticmethod
    def _upload(payload: bytes, filename: str = 'avatar.svg') -> FileStorage:
        return FileStorage(stream=BytesIO(payload), filename=filename)

    def test_a_hostile_svg_avatar_is_rejected(self, app):
        with app.app_context():
            with pytest.raises(BadRequest):
                save_icon_file(self._upload(DOCTYPE_GT_IN_QUOTED_VALUE), 'users')

    def test_a_utf16_svg_avatar_is_rejected(self, app):
        with app.app_context():
            with pytest.raises(BadRequest):
                save_icon_file(self._upload(UTF16_ENTITY_SVG_LE), 'users')

    def test_the_rejected_avatar_is_not_left_anywhere_under_the_media_root(self, app):
        """The property that matters. Before the fix these bytes -- well-formed
        XML with a live <script>, which a browser renders and runs -- were still
        sitting under app/static/media/users/ when the request finished."""
        before = files_under(MEDIA_ROOT)
        with app.app_context():
            with pytest.raises(BadRequest):
                save_icon_file(self._upload(DOCTYPE_GT_IN_QUOTED_VALUE), 'users')
        new_files = sorted(set(files_under(MEDIA_ROOT)) - set(before))

        try:
            for path in new_files:
                with open(path, 'rb') as f:
                    assert b'<script' not in f.read().lower(), \
                        f'hostile payload survived at {path}'
            assert new_files == [], f'rejected upload left files behind: {new_files}'
        finally:
            for path in new_files:
                os.remove(path)

    def test_a_clean_svg_avatar_is_stored_and_sanitized(self, app, db_session):
        """The happy path is not collateral damage. db_session is required
        because save_icon_file adds the File row to the session on success."""
        payload = (b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
                   b'<script>alert(1)</script><rect width="1" height="1"/></svg>')
        with app.app_context():
            file = save_icon_file(self._upload(payload), 'users')
            try:
                with open(file.file_path, 'rb') as f:
                    content = f.read()
                assert b'script' not in content.lower()
                assert b'<rect' in content
            finally:
                os.remove(file.file_path)


class TestSaveBannerFileRejectsUnsanitizableSvg:
    """app/community/util.py's save_banner_file, driven for real.

    Same shape as save_icon_file and reachable the same way
    (app/user/routes.py:283 is the profile cover image), but the pre-existing
    behaviour was worse rather than better: save_banner_file has no '.svg'
    branch at all, so after writing the upload to the media root it called
    Image.open on it, Pillow raised UnidentifiedImageError, and the request
    500ed -- leaving the hostile bytes on disk with nothing to clean them up.

    Sanitising before that point means the payload is destroyed and the upload
    refused with a 400. A CLEAN SVG banner still fails, because Image.open still
    cannot read it; that is a pre-existing bug in listing '.svg' as an allowed
    banner extension and is deliberately not changed here. Only the security
    property is fixed.
    """

    @staticmethod
    def _upload(payload: bytes, filename: str = 'cover.svg') -> FileStorage:
        return FileStorage(stream=BytesIO(payload), filename=filename)

    def test_a_hostile_svg_banner_is_rejected(self, app):
        with app.app_context():
            with pytest.raises(BadRequest):
                save_banner_file(self._upload(DOCTYPE_GT_IN_QUOTED_VALUE), 'users')

    def test_the_rejected_banner_is_not_left_anywhere_under_the_media_root(self, app):
        before = files_under(MEDIA_ROOT)
        with app.app_context():
            with pytest.raises(BadRequest):
                save_banner_file(self._upload(DOCTYPE_GT_IN_QUOTED_VALUE), 'users')
        new_files = sorted(set(files_under(MEDIA_ROOT)) - set(before))

        try:
            for path in new_files:
                with open(path, 'rb') as f:
                    assert b'<script' not in f.read().lower(), \
                        f'hostile payload survived at {path}'
            assert new_files == [], f'rejected upload left files behind: {new_files}'
        finally:
            for path in new_files:
                os.remove(path)


class TestUrlToThumbnailFileDropsUnsanitizableSvg:
    """app/utils.py's remote-thumbnail fetch.

    REGRESSION GUARD for a crash path the entity refusal introduced.
    url_to_thumbnail_file calls sanitize_svg_bytes directly, and that function
    now raises ValueError on entity-bearing input where the old pre-strip
    silently mangled the bytes instead. The exception propagated out of
    url_to_thumbnail_file into edit_post uncaught, so a remote SVG carrying an
    entity declaration crashed post editing.

    A remote thumbnail that cannot be sanitised is dropped, which is what this
    function already does with a thumbnail it cannot fetch and with a URL it
    will not fetch: return None.
    """

    URL = 'https://thumbnails.example/hostile.svg'

    @staticmethod
    def _svg_response(payload: bytes) -> httpx.Response:
        return httpx.Response(200, content=payload,
                              headers={'content-type': 'image/svg+xml'})

    def test_an_entity_bearing_remote_svg_is_dropped_rather_than_raising(self, app, http_mock):
        http_mock.get(self.URL).mock(return_value=self._svg_response(DOCTYPE_GT_IN_QUOTED_VALUE))
        with app.app_context():
            assert url_to_thumbnail_file(self.URL) is None

    def test_a_utf16_remote_svg_is_dropped_rather_than_raising(self, app, http_mock):
        http_mock.get(self.URL).mock(return_value=self._svg_response(UTF16_ENTITY_SVG_LE))
        with app.app_context():
            assert url_to_thumbnail_file(self.URL) is None

    def test_an_oversize_remote_svg_is_dropped_rather_than_raising(self, app, http_mock):
        """The other ValueError sanitize_svg_bytes raises. Padding is a comment,
        so the document stays well-formed."""
        payload = (b'<svg xmlns="http://www.w3.org/2000/svg">'
                   b'<!--' + b'A' * MAX_SVG_SIZE + b'--></svg>')
        http_mock.get(self.URL).mock(return_value=self._svg_response(payload))
        with app.app_context():
            assert url_to_thumbnail_file(self.URL) is None

    def test_the_dropped_svg_is_not_left_anywhere_under_the_media_root(self, app, http_mock):
        http_mock.get(self.URL).mock(return_value=self._svg_response(DOCTYPE_GT_IN_QUOTED_VALUE))
        before = files_under(MEDIA_ROOT)
        with app.app_context():
            assert url_to_thumbnail_file(self.URL) is None
        new_files = sorted(set(files_under(MEDIA_ROOT)) - set(before))

        try:
            assert new_files == [], f'dropped thumbnail left files behind: {new_files}'
        finally:
            for path in new_files:
                os.remove(path)

    def test_a_clean_remote_svg_still_becomes_a_thumbnail(self, app, http_mock):
        """The happy path is not collateral damage: a fetchable SVG is still
        stored, with its script removed."""
        payload = (b'<svg xmlns="http://www.w3.org/2000/svg">'
                   b'<script>alert(1)</script><rect width="1" height="1"/></svg>')
        http_mock.get(self.URL).mock(return_value=self._svg_response(payload))
        with app.app_context():
            file = url_to_thumbnail_file(self.URL)
            assert file is not None
            try:
                with open(file.thumbnail_path, 'rb') as f:
                    content = f.read()
                assert b'script' not in content.lower()
                assert b'<rect' in content
            finally:
                os.remove(file.thumbnail_path)


class TestARemoteServerDoesNotChooseTheFileExtension:
    """D1327/D1328. `url_to_thumbnail_file` took the extension of the file it
    wrote from the REMOTE `Content-Type`:

        file_extension = '.' + content_type.split('/')[-1]

    behind a gate of `content_type.startswith('image')`. So
    `Content-Type: image/html` wrote the peer's body to
    `app/static/media/posts/xx/yy/<name>.html` -- a directory this instance
    serves -- and Pillow's refusal of it then propagated out of the function,
    LEAVING THE FILE THERE. Measured before the repair:

        PROBE html dressed as an image: UnidentifiedImageError: cannot identify
            image file 'app/static/media/posts/Ez/Qj/EzQjV1ayKXOudrX.html'
        PROBE html dressed as an image: content=b'<html><script>alert(document.domain)</sc'

    which is stored XSS on the instance's own origin, reachable by being
    federated a post whose image url points at the attacker.

    An extension PieFed itself accepts is kept; anything else becomes `.img`,
    which no web server serves as script. Pillow sniffs content rather than
    names, so a format it supports under an unlisted content type still works.
    """

    HOSTILE = b'<html><script>alert(document.domain)</script></html>'

    @staticmethod
    def _response(content: bytes, content_type: str) -> httpx.Response:
        return httpx.Response(200, content=content,
                              headers={'content-type': content_type})

    def _fetch(self, app, http_mock, content_type, content=None, url=None):
        """Fetch one thumbnail and report what it returned and what it left."""
        url = url or f'https://thumbnails.example/{gibberish(8)}.img'
        http_mock.get(url).mock(return_value=self._response(
            content if content is not None else self.HOSTILE, content_type))
        before = files_under(MEDIA_ROOT)
        with app.app_context():
            result = url_to_thumbnail_file(url)
        left = sorted(set(files_under(MEDIA_ROOT)) - set(before))
        return result, left

    @pytest.mark.parametrize('content_type', [
        'image/html',
        'image/xhtml+xml',
        'image/php',
        'image/javascript',
        'image/svg',            # not svg+xml, so the sanitiser is not reached
        'image/png x',
        'image/',
        'image/' + 'a' * 200,
    ])
    def test_a_content_type_that_is_not_an_image_is_dropped(self, app, http_mock,
                                                            content_type):
        result, left = self._fetch(app, http_mock, content_type)
        try:
            assert result is None
            assert left == [], f'left behind: {left}'
        finally:
            for path in left:
                os.remove(path)

    def test_nothing_is_written_with_a_scriptable_extension(self, app, http_mock):
        """The property. Whatever the peer says, the name it gets cannot be one
        a web server executes or renders."""
        forbidden = ('.html', '.htm', '.xhtml', '.xhtml+xml', '.php', '.js',
                     '.javascript', '.phtml')
        for content_type in ('image/html', 'image/php', 'image/js',
                             'image/javascript', 'image/xhtml+xml'):
            result, left = self._fetch(app, http_mock, content_type)
            try:
                assert result is None
                for path in left:
                    assert not path.lower().endswith(forbidden), path
            finally:
                for path in left:
                    os.remove(path)

    def test_a_body_pillow_refuses_leaves_nothing_behind(self, app, http_mock):
        """D1328 on its own: the content type is one PieFed accepts, and the
        body is not an image at all."""
        result, left = self._fetch(app, http_mock, 'image/png',
                                   content=b'not a png at all')
        try:
            assert result is None
            assert left == [], f'left behind: {left}'
        finally:
            for path in left:
                os.remove(path)

    def test_it_returns_none_rather_than_raising(self, app, http_mock):
        """`edit_post` calls this and does not catch anything, so the refusal has
        to be a return value."""
        url = 'https://thumbnails.example/notanimage.png'
        http_mock.get(url).mock(return_value=self._response(b'nope', 'image/png'))
        with app.app_context():
            assert url_to_thumbnail_file(url) is None

    def test_a_real_png_is_still_thumbnailed(self, app, http_mock):
        """The happy path is not collateral damage."""
        import io

        from PIL import Image as PILImage

        buffer = io.BytesIO()
        PILImage.new('RGB', (400, 300), (10, 20, 30)).save(buffer, format='PNG')
        result, left = self._fetch(app, http_mock, 'image/png',
                                   content=buffer.getvalue())
        try:
            assert result is not None
            assert result.thumbnail_path
            assert result.thumbnail_width <= 170
        finally:
            for path in left:
                if os.path.isfile(path):
                    os.remove(path)

    def test_a_jpeg_keeps_the_jpg_spelling(self, app, http_mock):
        """`image/jpeg` maps to `.jpg`, which the code below the write compares
        against explicitly when it decides the colour mode."""
        import io

        from PIL import Image as PILImage

        buffer = io.BytesIO()
        PILImage.new('RGB', (200, 200), (1, 2, 3)).save(buffer, format='JPEG')
        result, left = self._fetch(app, http_mock, 'image/jpeg',
                                   content=buffer.getvalue())
        try:
            assert result is not None
        finally:
            for path in left:
                if os.path.isfile(path):
                    os.remove(path)

    def test_the_allowlist_holds_only_image_extensions(self):
        """A property over the list itself, so nothing scriptable is ever added
        to it by hand."""
        from app.utils import allowed_thumbnail_extensions

        for extension in allowed_thumbnail_extensions:
            assert extension.startswith('.')
            assert extension == extension.lower()
            assert extension not in ('.html', '.htm', '.php', '.js', '.xhtml')

    def test_the_extension_is_never_taken_from_the_content_type_verbatim(self):
        """The source-level guard: the expression that caused this is gone."""
        from pathlib import Path

        import app.utils
        source = Path(app.utils.__file__).read_text(encoding='utf8')
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith('#'):
                continue
            assert "file_extension = '.' + content_type_parts[-1]" not in stripped

    def test_the_name_the_bytes_are_written_under(self, app, http_mock, monkeypatch):
        """The extension itself, observed rather than inferred.

        Both repairs close the hole -- the allowlist stops `.html` being chosen,
        and the cleanup removes the file either way -- so a test that looks only
        at what survives cannot tell which one is working, and a mutant dropping
        the allowlist passed against them. What pins it is the path handed to
        `Image.open`, which is the file as it exists on disk before anything
        tidies up.
        """
        recorded = []

        class _Image:
            MAX_IMAGE_PIXELS = None
            LANCZOS = 1

            @staticmethod
            def open(path, *args, **keywords):
                recorded.append(path)
                raise OSError('cannot identify image file')

        monkeypatch.setattr('app.utils.Image', _Image)

        for content_type, expected in (('image/html', '.img'),
                                       ('image/php', '.img'),
                                       ('image/' + 'a' * 200, '.img'),
                                       ('image/jpeg', '.jpg'),
                                       ('image/png', '.png'),
                                       ('image/webp', '.webp')):
            recorded.clear()
            result, left = self._fetch(app, http_mock, content_type)
            try:
                assert result is None
                assert recorded, f'Image.open was never reached for {content_type}'
                assert recorded[0].endswith(expected), \
                    f'{content_type} was written as {recorded[0]}'
            finally:
                for path in left:
                    if os.path.isfile(path):
                        os.remove(path)


class TestTheOriginalDownloadIsNotLeftBehind:
    """D1345. `url_to_thumbnail_file` writes the peer's body under one extension
    and then, when the configured medium format differs, resizes it into another:

        posts/Rq/Mk/RqMkzFBw22MVyW1.png       <- nothing names this
        posts/Rq/Mk/RqMkzFBw22MVyW1.webp      <- File.thumbnail_path
        posts/Rq/Mk/RqMkzFBw22MVyW1_512.webp  <- File.file_path

    Measured, with the default MEDIA_IMAGE_MEDIUM_FORMAT=WEBP: the first file was
    orphaned AT WRITE TIME. No column names it, so `File.delete_from_disk` cannot
    remove it when the post goes, and no sweep can find it either -- it is not a
    leftover that outlives its row, it is a file no row ever mentioned. Every
    remote thumbnail this instance fetched left one, for ever, under a directory
    it serves.

    `tests/test_utils_thumbnail_s3.py` asserts the same thing for the S3 arm,
    where the leftovers land in `app/static/tmp` and `clean_up_tmp` sweeps only
    eight extensions after a day: `.img` -- D1327's fallback -- and `.avif`,
    `.bmp`, `.tiff` and `.mpo` were never swept at all.
    """

    URL = 'https://thumbnails.example/original.png'

    @staticmethod
    def a_png(size=(600, 400)):
        import io

        from PIL import Image

        buffer = io.BytesIO()
        Image.new('RGB', size, (10, 120, 200)).save(buffer, format='PNG')
        return buffer.getvalue()

    def fetch(self, app, http_mock, content_type='image/png', content=None):
        http_mock.get(self.URL).mock(return_value=httpx.Response(
            200, content=content if content is not None else self.a_png(),
            headers={'content-type': content_type}))
        before = files_under(MEDIA_ROOT)
        with app.app_context():
            result = url_to_thumbnail_file(self.URL)
        written = sorted(set(files_under(MEDIA_ROOT)) - set(before))
        return result, written

    def test_only_the_two_files_the_row_names_are_left(self, app, http_mock):
        result, written = self.fetch(app, http_mock)
        try:
            assert written == sorted([result.thumbnail_path, result.file_path])
        finally:
            for path in written:
                if os.path.isfile(path):
                    os.remove(path)

    def test_no_file_keeps_the_fetched_extension(self, app, http_mock):
        """The .png was the whole of the leak, and it is the file nothing could
        ever have deleted."""
        result, written = self.fetch(app, http_mock)
        try:
            assert [path for path in written if path.endswith('.png')] == []
        finally:
            for path in written:
                if os.path.isfile(path):
                    os.remove(path)

    def test_a_format_that_matches_the_fetch_keeps_its_one_file(self, app, http_mock,
                                                               monkeypatch):
        """The other side: when the medium format IS the fetched format, the
        resize writes back over the same path, and that one file is the
        thumbnail. Deleting 'the original' there would delete the thumbnail."""
        monkeypatch.setitem(app.config, 'MEDIA_IMAGE_MEDIUM_FORMAT', 'PNG')

        result, written = self.fetch(app, http_mock)
        try:
            assert result is not None
            assert os.path.isfile(result.thumbnail_path)
            assert os.path.isfile(result.file_path)
            assert written == sorted([result.thumbnail_path, result.file_path])
        finally:
            for path in written:
                if os.path.isfile(path):
                    os.remove(path)

    def test_a_body_pillow_refuses_leaves_neither_name(self, app, http_mock):
        """D1328's drop has to take the original with it. Before this round the
        cleanup listed `temp_file_path`, which by then is the RESIZED name, so a
        failure after the format swap left the peer's bytes behind."""
        result, written = self.fetch(app, http_mock, content=b'<html>not an image')
        try:
            assert result is None
            assert written == []
        finally:
            for path in written:
                if os.path.isfile(path):
                    os.remove(path)

    def failing_save(self, monkeypatch, fail_on):
        """Make Pillow's save raise on the Nth call, so the discard path runs at a
        point the fetched-vs-resized names have already diverged.

        A body Pillow refuses fails at `Image.open`, which is BEFORE the format
        swap moves `temp_file_path`, so a test using one cannot tell whether the
        discard removes the original or merely the name that happens to equal it.
        """
        from PIL import Image

        calls = []
        real_save = Image.Image.save

        def save(self, fp, *arguments, **keywords):
            calls.append(fp)
            if len(calls) == fail_on:
                raise OSError('encoder blew up')
            return real_save(self, fp, *arguments, **keywords)

        monkeypatch.setattr(Image.Image, 'save', save)
        return calls

    def test_the_170_save_failing_still_removes_the_fetched_file(self, app, http_mock,
                                                                monkeypatch):
        """The first save is the one that runs after `temp_file_path` has moved to
        the medium format's extension. The `.png` the peer's bytes went into has
        to go with the discard."""
        payload = self.a_png()  # built BEFORE the patch: a_png saves too
        self.failing_save(monkeypatch, fail_on=1)

        result, written = self.fetch(app, http_mock, content=payload)
        try:
            assert result is None
            assert written == []
        finally:
            for path in written:
                if os.path.isfile(path):
                    os.remove(path)

    def test_the_512_save_failing_removes_the_170_it_had_already_written(
            self, app, http_mock, monkeypatch):
        """By the second save the 170px thumbnail is on disk under the new
        extension, so the discard has three names to clear, not one."""
        payload = self.a_png()
        self.failing_save(monkeypatch, fail_on=2)

        result, written = self.fetch(app, http_mock, content=payload)
        try:
            assert result is None
            assert written == []
        finally:
            for path in written:
                if os.path.isfile(path):
                    os.remove(path)

    def test_an_svg_has_no_original_to_remove(self, app, http_mock):
        """An SVG skips Pillow, so its one file is both the download and the
        thumbnail; the removal must not touch it."""
        http_mock.get(self.URL).mock(return_value=httpx.Response(
            200, content=b'<svg xmlns="http://www.w3.org/2000/svg">'
                        b'<rect width="1" height="1"/></svg>',
            headers={'content-type': 'image/svg+xml'}))
        before = files_under(MEDIA_ROOT)
        with app.app_context():
            result = url_to_thumbnail_file(self.URL)
        written = sorted(set(files_under(MEDIA_ROOT)) - set(before))

        try:
            assert result is not None
            assert written == [result.thumbnail_path]
            assert os.path.isfile(result.thumbnail_path)
        finally:
            for path in written:
                if os.path.isfile(path):
                    os.remove(path)
