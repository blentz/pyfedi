import html as html_module
import unittest

import pytest
from bs4 import BeautifulSoup

from app.utils import (allowlist_html, community_link_to_href, feed_link_to_href, markdown_to_html,
                       person_link_to_href, url_host)


class TestAllowlistHtml(unittest.TestCase):

    def test_basic_html(self):
        """Test basic HTML with allowed tags is preserved"""
        html = "<p>Basic paragraph</p>"
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        self.assertEqual(result, html)

    def test_disallowed_tags(self):
        """Test that disallowed tags are removed"""
        html = "<p>Paragraph with <script>alert('xss')</script> script</p>"
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        self.assertEqual(result, "<p>Paragraph with  script</p>")

    def test_nested_tags(self):
        """Test that allowed nested tags are preserved"""
        html = "<blockquote><p>Nested content</p></blockquote>"
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        self.assertEqual(result, html)

    def test_attributes(self):
        """Test that allowed attributes are preserved and others removed"""
        html = '<a href="https://example.com" onclick="alert(\'xss\')" style="color:red">Link</a>'
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        self.assertEqual(result, '<a href="https://example.com" rel="nofollow ugc" target="_blank">Link</a>')

    def test_empty_input(self):
        """Test empty input"""
        self.assertEqual(allowlist_html(""), "")
        self.assertEqual(allowlist_html(None), "")

    def test_plain_text_urls(self):
        """Test that plain text URLs are converted to links"""
        markdown = "Visit https://example.com for more info."
        result = allowlist_html(markdown_to_html(markdown, test_env={'fn_string': 'fn-test'}), test_env={'fn_string': 'fn-test'})
        self.assertEqual(result,
                         '<p>Visit <a href="https://example.com" rel="nofollow ugc" target="_blank">https://example.com</a> for more info.</p>\n')

    def test_angle_brackets_in_text(self):
        """Test that angle brackets in plain text are escaped"""
        html = "<p>Text with <Book Title and Volume> needs escaping</p>"
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        print(f"Original: {html}")
        print(f"Result: {result}")
        self.assertTrue("&lt;Book Title and Volume&gt;" in result)

    def test_angle_brackets_in_blockquote(self):
        """Test that angle brackets in blockquote are escaped"""
        html = "<blockquote><p><Book Title and Volume> Review Goes Here [5/10]</p></blockquote>"
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        print(f"Original: {html}")
        print(f"Result: {result}")
        self.assertTrue("&lt;Book Title and Volume&gt;" in result)
    
    def test_community_link_basic_html(self):
        """Test link creation of !community@instance.tld"""
        text = "!community@instance.tld"
        correct_html = '<a href="https://instance.tld/community/lookup/community/instance.tld">!community@instance.tld</a>'
        result = community_link_to_href(text, server_name_override="instance.tld")
        self.assertEqual(result, correct_html)
    
    def test_community_link_markdown_link(self):
        """
        Ignore link creation inside a markdown-created link

        Test input came from parsing markdown: [Link to !community@instance.tld is not here](https://other_site.tld)
        """
        text = ('<a href="https://other_site.tld" rel="nofollow ugc" target="_blank">'
                'Link to !community@instance.tld is not here</a>')
        correct_html = ('<a href="https://other_site.tld" rel="nofollow ugc" target="_blank">'
                        'Link to !community@instance.tld is not here</a>')
        result = community_link_to_href(text, server_name_override="instance.tld")
        self.assertEqual(result, correct_html)
    
    def test_community_link_code_block(self):
        """Ignore link creation if in a <code> block"""
        text = "<code>!community@instance.tld</code>"
        correct_html = "<code>!community@instance.tld</code>"
        result = community_link_to_href(text, server_name_override="instance.tld")
        self.assertEqual(result, correct_html)

    def test_community_link_masto_link(self):
        """
        Ignore link creation if preceded by a / (mastodon links are sometimes like this, more often for people links)
        """
        text = "https://masto.tld/!community@instance.tld/12345"
        correct_html = "https://masto.tld/!community@instance.tld/12345"
        result = community_link_to_href(text, server_name_override="instance.tld")
        self.assertEqual(result, correct_html)
    
    def test_feed_link_basic_html(self):
        """Test link creation of ~feed@instance.tld"""
        text = "~feed@instance.tld"
        correct_html = '<a href="https://instance.tld/feed/lookup/feed/instance.tld">~feed@instance.tld</a>'
        result = feed_link_to_href(text, server_name_override="instance.tld")
        self.assertEqual(result, correct_html)
    
    def test_feed_link_markdown_link(self):
        """
        Ignore link creation inside a markdown-created link

        Test input came from parsing markdown: [Link to ~feed@instance.tld is not here](https://other_site.tld)
        """
        text = ('<a href="https://other_site.tld" rel="nofollow ugc" target="_blank">'
                'Link to ~feed@instance.tld is not here</a>')
        correct_html = ('<a href="https://other_site.tld" rel="nofollow ugc" target="_blank">'
                        'Link to ~feed@instance.tld is not here</a>')
        result = feed_link_to_href(text, server_name_override="instance.tld")
        self.assertEqual(result, correct_html)
    
    def test_feed_link_code_block(self):
        """Ignore link creation if in a <code> block"""
        text = "<code>~feed@instance.tld</code>"
        correct_html = "<code>~feed@instance.tld</code>"
        result = feed_link_to_href(text, server_name_override="instance.tld")
        self.assertEqual(result, correct_html)

    def test_feed_link_masto_link(self):
        """
        Ignore link creation if preceded by a / (mastodon links are sometimes like this, more often for people links)
        """
        text = "https://masto.tld/~feed@instance.tld/12345"
        correct_html = "https://masto.tld/~feed@instance.tld/12345"
        result = feed_link_to_href(text, server_name_override="instance.tld")
        self.assertEqual(result, correct_html)
    
    def test_person_link_basic_html(self):
        """Test link creation of @person@instance.tld"""
        text = "@person@instance.tld"
        correct_html = ('<a href="https://instance.tld/user/lookup/person/instance.tld" '
                        'rel="nofollow noindex">@person@instance.tld</a>')
        result = person_link_to_href(text, server_name_override="instance.tld")
        self.assertEqual(result, correct_html)
    
    def test_person_link_markdown_link(self):
        """
        Ignore link creation inside a markdown-created link

        Test input came from parsing markdown: [Link to @person@instance.tld is not here](https://other_site.tld)
        """
        text = ('<a href="https://other_site.tld" rel="nofollow ugc" target="_blank">'
                'Link to @person@instance.tld is not here</a>')
        correct_html = ('<a href="https://other_site.tld" rel="nofollow ugc" target="_blank">'
                        'Link to @person@instance.tld is not here</a>')
        result = person_link_to_href(text, server_name_override="instance.tld")
        self.assertEqual(result, correct_html)
    
    def test_person_link_code_block(self):
        """Ignore link creation if in a <code> block"""
        text = "<code>@person@instance.tld</code>"
        correct_html = "<code>@person@instance.tld</code>"
        result = person_link_to_href(text, server_name_override="instance.tld")
        self.assertEqual(result, correct_html)

    def test_person_link_masto_link(self):
        """
        Ignore link creation if preceded by a / (mastodon links are sometimes like this, more often for people links)
        """
        text = "https://masto.tld/@person@instance.tld/12345"
        correct_html = "https://masto.tld/@person@instance.tld/12345"
        result = person_link_to_href(text, server_name_override="instance.tld")
        self.assertEqual(result, correct_html)

    def test_enhanced_image_width_attribute(self):
        """Test that width attribute on img tag is preserved"""
        html = '<img src="cat.jpg" alt="Cat" width="200px"/>'
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        self.assertIn('width="200px"', result)
        self.assertIn('src="cat.jpg"', result)
        self.assertIn('alt="Cat"', result)

    def test_enhanced_image_height_attribute(self):
        """Test that height attribute on img tag is preserved"""
        html = '<img src="cat.jpg" alt="Cat" height="150px"/>'
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        self.assertIn('height="150px"', result)
        self.assertIn('src="cat.jpg"', result)

    def test_enhanced_image_align_attribute(self):
        """Test that align attribute on img tag is preserved"""
        html = '<img src="cat.jpg" alt="Cat" align="left"/>'
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        self.assertIn('align="left"', result)
        self.assertIn('src="cat.jpg"', result)

    def test_enhanced_image_all_attributes(self):
        """Test that all enhanced image attributes are preserved"""
        html = '<img src="cat.jpg" alt="Cat" width="200px" height="150px" align="right" class="thumbnail"/>'
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        self.assertIn('width="200px"', result)
        self.assertIn('height="150px"', result)
        self.assertIn('align="right"', result)
        self.assertIn('class="thumbnail"', result)
        self.assertIn('src="cat.jpg"', result)
        self.assertIn('alt="Cat"', result)

    def test_enhanced_image_title_attribute(self):
        """Test that title attribute on img tag is preserved"""
        html = '<img src="cat.jpg" alt="Cat" title="A cute cat"/>'
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        self.assertIn('title="A cute cat"', result)
        self.assertIn('src="cat.jpg"', result)

    def test_enhanced_image_with_anchor(self):
        """Test that thumbnail wrapped in anchor preserves all attributes"""
        html = '<a href="full.jpg"><img src="thumb.jpg" alt="Cat" width="200px" align="left"/></a>'
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        self.assertIn('<a href="full.jpg"', result)
        self.assertIn('width="200px"', result)
        self.assertIn('align="left"', result)
        self.assertIn('src="thumb.jpg"', result)
        # Anchor should have nofollow and target
        self.assertIn('rel="nofollow ugc"', result)
        self.assertIn('target="_blank"', result)

    def test_enhanced_image_disallowed_attributes_removed(self):
        """Test that disallowed attributes on img are removed"""
        html = '<img src="cat.jpg" alt="Cat" width="200px" onclick="alert(\'xss\')" style="border:1px"/>'
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        self.assertIn('width="200px"', result)
        self.assertIn('src="cat.jpg"', result)
        # Dangerous attributes should be removed
        self.assertNotIn('onclick', result)
        self.assertNotIn('style', result)

    def test_enhanced_image_loading_lazy_added(self):
        """Test that loading=lazy is added to images"""
        html = '<img src="cat.jpg" alt="Cat" width="200px"/>'
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        self.assertIn('loading="lazy"', result)
        self.assertIn('width="200px"', result)


def href_of(result: str) -> str:
    """The href BeautifulSoup reads back out of allowlist_html's own output.

    Asserting on the parsed attribute rather than on a substring of the markup
    matters here: the whole point of these tests is that a value can *look*
    harmless in the raw string and still be a javascript: URL once a browser has
    normalised it, so a substring assertion would be testing the wrong thing.
    """
    anchor = BeautifulSoup(result, 'html.parser').find('a')
    assert anchor is not None, f'no anchor survived: {result!r}'
    return anchor.get('href')


class TestAnchorSchemeIsDecidedTheWayABrowserDecidesIt:
    """Padded and case-varied script URLs must not survive the allowlist.

    allowlist_html is the sanitisation boundary for every remote post, comment,
    profile field and community description, so an href it leaves intact is an
    href a browser will follow on click.

    The expectations come from the WHATWG URL Standard's basic URL parser
    (https://url.spec.whatwg.org/#concept-basic-url-parser), which normalises a
    URL BEFORE reading its scheme:

    * step 1 -- "remove any leading and trailing C0 control or space from
      input"; a C0 control or space is U+0000 to U+001F, or U+0020.
    * step 2 -- "remove all ASCII tab or newline from input", from anywhere in
      it; an ASCII tab or newline is U+0009, U+000A or U+000D.
    * scheme start state / scheme state -- a scheme is an ASCII alpha followed
      by ASCII alphanumerics, '+', '-' and '.', terminated by ':', and it is
      ASCII-lowercased before it is compared to anything.

    Additional spellings are taken from the OWASP XSS Filter Evasion Cheat
    Sheet's "Embedded tab", "Embedded newline", "Embedded carriage return" and
    case-variation entries.

    BUG (fixed here): the previous implementation asked
    furl(tag['href']).scheme == 'javascript'. furl performs none of the
    normalisation above, so every padded spelling below was returned intact,
    with rel/target added as though it were an ordinary safe link -- stored XSS
    reachable from any federating instance.
    """

    # (id, href as it appears in the source HTML). The entity spellings are
    # written as entities on purpose: that is how they arrive over the wire, and
    # the HTML parser is what turns them into the raw control character.
    BLOCKED = [
        ('plain', 'javascript:alert(1)'),
        ('uppercase', 'JAVASCRIPT:alert(1)'),
        ('mixed_case', 'JaVaScRiPt:alert(1)'),
        ('leading_space', ' javascript:alert(1)'),
        ('trailing_space', 'javascript:alert(1) '),
        ('leading_tab_entity', '&#9;javascript:alert(1)'),
        ('leading_lf_entity', '&#10;javascript:alert(1)'),
        ('leading_cr_entity', '&#13;javascript:alert(1)'),
        ('leading_nul', '\x00javascript:alert(1)'),
        ('leading_c0_unit_separator', '\x1fjavascript:alert(1)'),
        ('embedded_tab_in_scheme', 'jav\tascript:alert(1)'),
        ('embedded_lf_in_scheme', 'java\nscript:alert(1)'),
        ('embedded_cr_in_scheme', 'java\rscript:alert(1)'),
        ('embedded_tab_entity_in_scheme', 'jav&#9;ascript:alert(1)'),
        ('embedded_lf_entity_in_scheme', 'java&#10;script:alert(1)'),
        ('every_trick_at_once', ' \t\n\rJaVa\tScRiPt&#10;:alert(1) '),
        ('colon_entity', 'javascript&#58;alert(1)'),
    ]

    @pytest.mark.parametrize('href', [h for _, h in BLOCKED], ids=[i for i, _ in BLOCKED])
    def test_a_javascript_url_is_blanked(self, href):
        result = allowlist_html(f'<a href="{href}">x</a>', test_env={'fn_string': 'fn-test'})
        assert href_of(result) == '', f'javascript: URL survived: {result!r}'

    @pytest.mark.parametrize('href', [
        'vbscript:msgbox(1)',
        'VBScript:msgbox(1)',
        ' vbscript:msgbox(1)',
        'vb&#9;script:msgbox(1)',
    ])
    def test_a_vbscript_url_is_blanked(self, href):
        """A deliberate widening beyond the reported javascript: defect.

        vbscript: executes script in the same way javascript: does wherever it
        is still supported, and there is no legitimate use of it in federated
        content. It was NOT blocked before this change.
        """
        result = allowlist_html(f'<a href="{href}">x</a>', test_env={'fn_string': 'fn-test'})
        assert href_of(result) == '', f'vbscript: URL survived: {result!r}'

    @pytest.mark.parametrize('href', [
        'data:text/html,&lt;script&gt;alert(1)&lt;/script&gt;',
        'data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==',
        'DATA:text/html,x',
        ' data:text/html,x',
        'da&#9;ta:text/html,x',
        'data:image/svg+xml;base64,PHN2Zz48c2NyaXB0Lz48L3N2Zz4=',
    ])
    def test_a_data_url_is_blanked(self, href):
        """A deliberate widening beyond the reported javascript: defect.

        A data:text/html or data:image/svg+xml href navigated to from a link
        executes script in a document the attacker wrote. Browsers now block
        top-level navigation to data: URLs, so this is defence in depth rather
        than a live hole -- but the anchor href is not a place PieFed has any
        reason to carry a data: URL, so it is blanked. Note the scope: this
        applies to <a href> only. img/@src is untouched by this change.
        """
        result = allowlist_html(f'<a href="{href}">x</a>', test_env={'fn_string': 'fn-test'})
        assert href_of(result) == '', f'data: URL survived: {result!r}'

    @pytest.mark.parametrize('href', [
        'https://example.com/',
        'http://example.com/path?a=b&amp;c=d#frag',
        'https://example.com/javascript:alert(1)',
        'https://example.com/?q=data:text/html',
        'https://user:pw@example.com:8443/x',
        '/relative/path',
        '//example.com/protocol-relative',
        'relative.html',
        'mailto:someone@example.com',
        'tel:+15550100',
        'ftp://example.com/file.txt',
        'magnet:?xt=urn:btih:abc',
        'gemini://example.com/',
        'data-sheet.html',
        'javascriptic://example.com/',
    ])
    def test_an_ordinary_href_is_preserved_byte_for_byte(self, href):
        """A fix that blanks legitimate hrefs is worse than the bug it fixes.

        The value is kept as it arrived, not as the WHATWG parser normalises it:
        normalisation is used to DECIDE, not to rewrite. Every browser applies
        the same normalisation itself when it follows the link, so rewriting
        would change stored content for no gain, and 'javascriptic' shows why
        the decision has to be a scheme comparison rather than a prefix test.
        """
        result = allowlist_html(f'<a href="{href}">ok</a>', test_env={'fn_string': 'fn-test'})
        assert href_of(result) == html_module.unescape(href)

    @pytest.mark.parametrize('href', [
        ' https://example.com/ ',
        '\thttps://example.com/',
        'https://example.com/a\tb',
        '\x01https://example.com/',
    ])
    def test_an_accepted_href_is_not_rewritten_to_its_normalised_form(self, href):
        """Normalisation DECIDES the scheme; it must not rewrite what is stored.

        Added after a discrimination run: mutating the fix to store the
        normalised value instead of the original left the whole suite green,
        which meant the "decide, do not rewrite" decision was undocumented by
        any test. It is documented by this one. A browser applies the same
        normalisation itself when it follows the link, so rewriting would edit
        stored federated content for no gain.
        """
        result = allowlist_html(f'<a href="{href}">ok</a>', test_env={'fn_string': 'fn-test'})
        assert href_of(result) == href

    def test_a_same_page_anchor_still_gets_its_footnote_suffix(self):
        result = allowlist_html('<a href="#fn-1">1</a>', test_env={'fn_string': 'fn-test'})
        assert href_of(result) == '#fn-1-fn-test'

    def test_a_blanked_anchor_keeps_its_text_and_its_rel(self):
        """Blanking the href is the pre-existing treatment for a rejected URL,
        and it is kept: the link text stays visible and the anchor goes nowhere."""
        result = allowlist_html('<a href=" javascript:alert(1)">click me</a>',
                                test_env={'fn_string': 'fn-test'})
        assert 'click me' in result
        assert 'rel="nofollow ugc"' in result

    def test_a_markdown_link_still_round_trips(self):
        result = markdown_to_html('[a link](https://example.com/x)', test_env={'fn_string': 'fn-test'})
        assert href_of(result) == 'https://example.com/x'

    def test_a_markdown_javascript_link_is_blanked(self):
        result = markdown_to_html('[click](javascript:alert%281%29)', test_env={'fn_string': 'fn-test'})
        assert href_of(result) == ''


class TestDegenerateAngleBrackets:
    """Angle-bracket content that names no tag must be treated as text.

    BUG (fixed here): app.utils.escape_non_html_brackets did
    tag_content[1:].split()[0] once it saw a leading slash. For '</>' that is
    ''.split(), which is [], so a three-character string raised IndexError --
    an unhandled 500 on the ingest path for every federated post, comment and
    profile, and for any local user who typed it into a comment box.
    escape_non_html_angle_brackets had the same bug plus one more case: it had
    no empty-content guard at all, so markdown_to_html('< >') raised too.

    The IndexError was found by the fuzz campaign in tests/fuzz/ at execution
    236; the minimised artifact is tests/fuzz/corpus/allowlist_html/empty_closing_tag.
    """

    DEGENERATE = ['</>', '<>', '< >', '</ >', '< / >', '<//>', '< />', '<\t>', '<  />', '</\t>',
                  '</  >', '<\x0b>']

    @pytest.mark.parametrize('text', DEGENERATE)
    def test_allowlist_html_treats_it_as_text(self, text):
        result = allowlist_html(text, test_env={'fn_string': 'fn-test'})
        assert '&lt;' in result and '&gt;' in result, (
            f'{text!r} was not escaped as text: {result!r}')

    @pytest.mark.parametrize('text', DEGENERATE)
    def test_markdown_to_html_treats_it_as_text(self, text):
        result = markdown_to_html(text, test_env={'fn_string': 'fn-test'})
        assert '&lt;' in result and '&gt;' in result, (
            f'{text!r} was not escaped as text: {result!r}')

    @pytest.mark.parametrize('text', ['<p>a</>b</p>', 'x </> y', '```\n</>\n```'])
    def test_degenerate_brackets_do_not_destroy_their_surroundings(self, text):
        assert allowlist_html(text, test_env={'fn_string': 'fn-test'})
        assert markdown_to_html(text, test_env={'fn_string': 'fn-test'})

    @pytest.mark.parametrize('text,tag', [('<p>ok</p>', 'p'), ('<strong>ok</strong>', 'strong'),
                                          ('<br/>', 'br'), ('<em>ok</em>', 'em')])
    def test_real_tags_are_still_recognised(self, text, tag):
        """The guard must not turn well-formed markup into escaped text."""
        result = allowlist_html(text, test_env={'fn_string': 'fn-test'})
        assert BeautifulSoup(result, 'html.parser').find(tag) is not None, result


class TestAMalformedHrefDoesNotDestroyTheDocument:
    """furl refuses to parse some URLs, and it refuses by raising ValueError.

    BUG (fixed here): allowlist_html called furl(tag['href']) unguarded, purely
    to read .host for the instance_domains comparison. A remote instance
    controls the content, so a single anchor with an unparseable authority made
    the WHOLE document raise out of the sanitisation boundary -- an unhandled
    500 on the federated ingest path, and on any local user who typed such a
    link into a comment box.

    A URL whose host furl cannot parse simply has no host, so it cannot be one
    of our instances. It is treated as remote and left alone, which is what this
    function already does with every other href it has no reason to rewrite.
    Blanking it would be wrong: an unparseable authority is not a script URL,
    and the scheme test has already run and passed by this point.

    Found by probing rather than by the fuzz campaign: reaching it needs the
    literal '<a href=' structure wrapped around a specific malformed authority.
    The two reported vectors were 'http://[' and 'http://[::1'; probing furl
    directly turned up four distinct refusal classes, all ValueError, listed in
    MALFORMED below.
    """

    # Every one of these raised ValueError out of allowlist_html before the fix.
    # Grouped by the refusal furl reports, because they are four different code
    # paths inside furl and only the first is about IPv6 at all.
    MALFORMED = [
        # "Invalid IPv6 URL" -- an unterminated or misplaced bracket
        'http://[',
        'http://[::1',
        'http://[:80',
        'https://[',
        '//[',
        'http://user@[/',
        'http://[::1]x/',
        'http://a[b]c/',
        'http://[[]]/',
        'ftp://[',
        # "... does not appear to be an IPv4 or IPv6 address" -- bracketed, but
        # not an address
        'http://[]',
        'http://[]:80',
        'http://[zzz]',
        'http://[1:2:3:4:5:6:7:8:9]',
        # "Invalid port ..." -- nothing to do with the host
        'http://example.com:notaport/',
        'http://example.com:99999999999/',
        # "Invalid host ..." -- characters furl will not accept in a host
        'http://%zz/',
        # UnicodeEncodeError out of the idna codec -- a lone surrogate in the
        # host. Found by probing furl with 200,000 random short strings and then
        # targeted ones; it is the only non-"ValueError" spelling that turned up,
        # and it is caught anyway because UnicodeEncodeError IS a ValueError.
        # Reachable: json.loads turns a "\\ud800" escape in remote actor or
        # object JSON into exactly this string.
        'http://\ud800/',
        'http://\udfff.example.com/',
    ]

    @pytest.mark.parametrize('href', MALFORMED)
    def test_it_does_not_raise(self, href):
        allowlist_html(f'<a href="{href}">x</a>', test_env={'fn_string': 'fn-test'})

    @pytest.mark.parametrize('href', MALFORMED)
    def test_the_href_is_left_alone(self, href):
        """Left alone, not blanked. An unparseable authority is not a script
        URL, and the scheme test has already passed by the time furl is asked."""
        result = allowlist_html(f'<a href="{href}">x</a>', test_env={'fn_string': 'fn-test'})
        assert href_of(result) == href

    @pytest.mark.parametrize('href', MALFORMED)
    def test_the_rest_of_the_document_still_renders(self, href):
        """The point of the defect: one bad anchor took the whole post with it."""
        html = f'<p>before</p><a href="{href}">x</a><p>after</p>'
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        assert 'before' in result and 'after' in result

    @pytest.mark.parametrize('href', MALFORMED)
    def test_markdown_survives_it_too(self, href):
        result = markdown_to_html(f'text <a href="{href}">x</a> more',
                                  test_env={'fn_string': 'fn-test'})
        assert 'text' in result and 'more' in result

    @pytest.mark.parametrize('href', [
        'javascript://[',
        ' javascript://[::1',
        'vbscript://[',
        'data://[',
        'jav\tascript://[',
    ])
    def test_an_unsafe_scheme_is_still_blanked_when_the_host_is_unparseable(self, href):
        """A hostile scheme wins over an unparseable host: these are blanked,
        not left alone.

        The scheme test also runs BEFORE furl is asked for a host, so a hostile
        href never reaches furl or rewrite_href. That ORDERING is defence in
        depth and this suite cannot observe it: allowlist_html sets
        instance_domains to [] under test_env, so the host comparison is always
        False and swapping the two tests changes no outcome here. Confirmed by
        mutation, not assumed -- see the f5_scheme_test_after_host row in the
        task report. What this test does pin is the outcome: unsafe scheme plus
        unparseable host is blanked."""
        result = allowlist_html(f'<a href="{href}">x</a>', test_env={'fn_string': 'fn-test'})
        assert href_of(result) == ''

    @pytest.mark.parametrize('href', [
        'http://[::1]:80/',
        'http://[::1]/',
        'http://[fe80::1%25eth0]/',
        'http://[::ffff:1.2.3.4]/',
        'https://example.com:8443/',
    ])
    def test_a_well_formed_authority_is_still_parsed_and_kept(self, href):
        """The guard must swallow furl's refusal, not furl's answer: a URL furl
        parses fine must still reach the instance_domains comparison."""
        result = allowlist_html(f'<a href="{href}">ok</a>', test_env={'fn_string': 'fn-test'})
        assert href_of(result) == href


class TestUrlHost:
    """url_host must swallow furl's REFUSAL without swallowing its ANSWER.

    Tested directly because allowlist_html cannot reach the difference: it sets
    instance_domains to [] under test_env, so `host in instance_domains` is
    False whatever the host is, and a url_host that always returned None would
    leave the whole suite green. That is not hypothetical -- mutating url_host
    to `return None` unconditionally passed 829 tests before this class existed.
    """

    @pytest.mark.parametrize('url,host', [
        ('https://example.com/path', 'example.com'),
        ('http://sub.example.com:8443/x', 'sub.example.com'),
        ('https://user:pw@example.com/', 'example.com'),
        ('http://[::1]:80/', '[::1]'),
        ('http://[::ffff:1.2.3.4]/', '[::ffff:1.2.3.4]'),
    ])
    def test_a_parseable_host_is_returned(self, url, host):
        assert url_host(url) == host

    @pytest.mark.parametrize('url', TestAMalformedHrefDoesNotDestroyTheDocument.MALFORMED)
    def test_a_host_furl_refuses_becomes_none(self, url):
        assert url_host(url) is None

    @pytest.mark.parametrize('url', ['mailto:someone@example.com', '/relative/path',
                                     'relative.html', '#fragment', ''])
    def test_a_url_with_no_authority_has_no_host(self, url):
        """None here is furl's own answer, not the guard's -- the caller has
        always handled it, which is why None is the right thing to return for a
        refusal too."""
        assert url_host(url) is None


if __name__ == '__main__':
    unittest.main()
