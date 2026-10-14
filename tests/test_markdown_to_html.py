import pathlib
import random
import re
import time
import unittest
from unittest import mock

from app.utils import (LINK_PATTERN, MARKDOWN_IN_HTML_LINE_LIMIT, MARKDOWN_MAX_BACKTICK_RUN, MARKDOWN_MAX_QUOTE_DEPTH,
                       MARKDOWN_MAX_SPACE_RUN, MARKDOWN_RENDER_CAP, limit_quote_depth, markdown_to_html,
                       neutralise_quadratic_markdown)

CORPUS = pathlib.Path(__file__).parent / 'fuzz' / 'corpus'

# LINK_PATTERN as it was before it was made linear, kept as the reference the rewrite must agree with.
OLD_LINK_PATTERN = re.compile(
    r"""
        \b
        (
            (?:https?://|(?<!//)www\.)    # prefix - https:// or www.
            \w[\w_\-]*(?:\.\w[\w_\-]*)*   # host
            [^<>\s\"']*                   # rest of url
            (?<![?!.,:*_~;])(?<!\)\))     # exclude trailing punctuation
            (?=[?!.,:*_~);]?(?:[<\s]|$))  # make sure that we're not followed by " or ', i.e. we're outside of href="...".
        )
    """,
    re.X
)


def random_texts(fragments, count, longest, seed):
    """`count` strings of up to `longest` fragments each, drawn from `fragments` with a fixed seed."""
    chooser = random.Random(seed)
    return [''.join(chooser.choice(fragments) for _ in range(chooser.randint(0, longest))) for _ in range(count)]


def corpus_texts(target, limit):
    """Each committed seed of a fuzz target as text, cut to `limit` characters so an old, super-linear regex
    still finishes on it."""
    return [path.read_bytes().decode('utf-8', errors='replace')[:limit] for path in sorted((CORPUS / target).iterdir())]


class TestMarkdownToHtml(unittest.TestCase):

    def test_basic_markdown(self):
        """Test basic markdown formatting"""
        markdown = "**Bold** and *italic* text"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual(result, "<p><strong>Bold</strong> and <em>italic</em> text</p>\n")

        markdown = "**Bold**, *italics*, __underscore bold__, and _underscore italics_, each next to a punctuation mark."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p><strong>Bold</strong>, <em>italics</em>, <strong>underscore bold</strong>, and <em>underscore italics</em>, each next to a punctuation mark.</p>\n'
        self.assertEqual(target_html, result)

    def test_paragraphs(self):
        """Test paragraph formatting"""
        markdown = "First paragraph\n\nSecond paragraph"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual(result, "<p>First paragraph</p>\n<p>Second paragraph</p>\n")

    def test_links(self):
        """Test links formatting"""
        markdown = "[Link text](https://example.com)"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual(result,
                         '<p><a href="https://example.com" rel="nofollow ugc" target="_blank">Link text</a></p>\n')

    def test_links_w_periods(self):
        """Test links formatting with a period on the end"""
        markdown = "This is a test link https://pizza.com. Will it work?"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual(result,
                         '<p>This is a test link <a href="https://pizza.com" rel="nofollow ugc" target="_blank">https://pizza.com</a>. Will it work?</p>\n')
    
    def test_links_w_tilde(self):
        """Test links that have a tilde in them."""
        markdown = "This link has a tilde: https://site.tld/~user"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<p>This link has a tilde: <a href="https://site.tld/~user" rel="nofollow ugc" target="_blank">https://site.tld/~user</a></p>\n'
        self.assertEqual(result, correct_html)

    def test_links_no_http(self):
        """Test links that have no https:// on the front"""
        markdown = "My website www.nihilore.com"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<p>My website <a href="https://www.nihilore.com" rel="nofollow ugc" target="_blank">www.nihilore.com</a></p>\n'
        self.assertEqual(result, correct_html)
    
    def test_links_w_parens(self):
        """Test links with parentheses."""
        markdown = "https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<p><a href="https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)" rel="nofollow ugc" target="_blank">https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)</a></p>\n'
        self.assertEqual(result, correct_html)

        markdown = "(https://en.wikipedia.org/wiki/Venison_Creek_(Ontario))"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<p>(<a href="https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)" rel="nofollow ugc" target="_blank">https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)</a>)</p>\n'
        self.assertEqual(result, correct_html)

        markdown = "(here is a link: https://en.wikipedia.org/wiki/Drosophila)"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<p>(here is a link: <a href="https://en.wikipedia.org/wiki/Drosophila" rel="nofollow ugc" target="_blank">https://en.wikipedia.org/wiki/Drosophila</a>)</p>\n'
        self.assertEqual(result, correct_html)

        markdown = "(here is a link: https://en.wikipedia.org/wiki/Venison_Creek_(Ontario))"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<p>(here is a link: <a href="https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)" rel="nofollow ugc" target="_blank">https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)</a>)</p>\n'
        self.assertEqual(result, correct_html)

        # Link embedded in a sentence with trailing period
        markdown = "Check out https://en.wikipedia.org/wiki/Venison_Creek_(Ontario) for more info."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<p>Check out <a href="https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)" rel="nofollow ugc" target="_blank">https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)</a> for more info.</p>\n'
        self.assertEqual(result, correct_html)

        # Link followed by bold text — next_sibling is a Tag, not a NavigableString (caused a crash before fix)
        markdown = "https://en.wikipedia.org/wiki/Venison_Creek_(Ontario) **bold text**"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<p><a href="https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)" rel="nofollow ugc" target="_blank">https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)</a> <strong>bold text</strong></p>\n'
        self.assertEqual(result, correct_html)

        # Parenthetical sentence containing a link with parens in path
        markdown = "(See https://en.wikipedia.org/wiki/Venison_Creek_(Ontario) for details)"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<p>(See <a href="https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)" rel="nofollow ugc" target="_blank">https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)</a> for details)</p>\n'
        self.assertEqual(result, correct_html)

        # Markdown-style named link with parens in URL
        markdown = "[Ontario Creek](https://en.wikipedia.org/wiki/Venison_Creek_(Ontario))"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<p><a href="https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)" rel="nofollow ugc" target="_blank">Ontario Creek</a></p>\n'
        self.assertEqual(result, correct_html)

        # Multiple links with parens in one sentence
        markdown = "Compare https://en.wikipedia.org/wiki/Venison_Creek_(Ontario) and https://en.wikipedia.org/wiki/Python_(programming_language) side by side."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<p>Compare <a href="https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)" rel="nofollow ugc" target="_blank">https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)</a> and <a href="https://en.wikipedia.org/wiki/Python_(programming_language)" rel="nofollow ugc" target="_blank">https://en.wikipedia.org/wiki/Python_(programming_language)</a> side by side.</p>\n'
        self.assertEqual(result, correct_html)

        # Link with parens followed by a comma
        markdown = "Visit https://en.wikipedia.org/wiki/Venison_Creek_(Ontario), then read more."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<p>Visit <a href="https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)" rel="nofollow ugc" target="_blank">https://en.wikipedia.org/wiki/Venison_Creek_(Ontario)</a>, then read more.</p>\n'
        self.assertEqual(result, correct_html)

    def test_code_blocks(self):
        """Test code blocks formatting"""
        markdown = "```\ncode block\n```"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertTrue("<pre><code>code block" in result)

    def test_blockquote(self):
        """Test blockquote formatting"""
        markdown = "> This is a quote"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertTrue("<blockquote>\n<p>This is a quote</p>\n</blockquote>" in result)
    
    def test_nested_blockquotes(self):
        """Test the handling of nesting blockquotes"""
        markdown = "> This is a quote\n> > This is a nested quote *with* __formatting__.\n> \n> Ending the quote."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<blockquote>\n<p>This is a quote</p>\n<blockquote>\n<p>This is a nested quote <em>with</em> <strong>formatting</strong>.</p>\n</blockquote>\n<p>Ending the quote.</p>\n</blockquote>\n'
        self.assertEqual(correct_html, result)
        
        markdown = "> This is a quote\n> \n> ::: spoiler Summary\n> > This is a **formatted** blockquote in a spoiler in a blockquote.\n> :::\n> \n> Ending the quote."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<blockquote>\n<p>This is a quote</p>\n<details><summary>Summary</summary><div class="spoiler_block symmetric">\n<blockquote>\n<p>This is a <strong>formatted</strong> blockquote in a spoiler in a blockquote.</p>\n</blockquote>\n</div></details>\n<p>Ending the quote.</p>\n</blockquote>\n'
        self.assertEqual(correct_html, result)

    def test_lists(self):
        """Test unordered and ordered lists"""
        markdown = "* Item 1\n* Item 2\n\n1. First\n2. Second"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertTrue("<ul>\n<li>Item 1</li>\n<li>Item 2</li>\n</ul>" in result)
        self.assertTrue("<ol>\n<li>First</li>\n<li>Second</li>\n</ol>" in result)

        markdown = "Line of text:\n- first bullet\n- second bullet"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<p>Line of text:</p>\n<ul>\n<li>first bullet</li>\n<li>second bullet</li>\n</ul>\n'
        self.assertEqual(correct_html, result)

        markdown = "Line of text:\n1. first item\n2. second item"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<p>Line of text:</p>\n<ol>\n<li>first item</li>\n<li>second item</li>\n</ol>\n'
        self.assertEqual(correct_html, result)

    def test_javascript_links(self):
        """Test that bad links are nuked"""
        markdown = "here is some text [click](javascript:alert(1)) here is some more text"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertTrue("javascript" not in result)

    def test_angle_brackets(self):
        """Test that angle brackets are properly escaped"""
        markdown = "Text with <tags> should be escaped"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertTrue("&lt;tags&gt;" in result)

    def test_angle_brackets_in_blockquote(self):
        """Test that angle brackets in blockquotes are properly escaped"""
        markdown = "> <Book Title and Volume> Review Goes Here [5/10]"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertTrue("&lt;Book Title and Volume&gt;" in result)

        markdown = "This is an emoticon with an angled bracket :<\n\nThis is a paragraph\n\n> This is a block quote."
        correct_html = '<p>This is an emoticon with an angled bracket :&lt;</p>\n<p>This is a paragraph</p>\n<blockquote>\n<p>This is a block quote.</p>\n</blockquote>\n'
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual(correct_html, result)

    def test_gt_lt_in_code(self):
        """Test usage of angle brackets in code block"""
        markdown = "Normal text `code block > something else` normal text again"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual(result, "<p>Normal text <code>code block &gt; something else</code> normal text again</p>\n")

    def test_gt_lt_in_code_block(self):
        """Test usage of angle brackets in large code block"""
        markdown = "Normal text\n\n```\n<html>\n```\n\nnormal text again"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual(result,
                         "<p>Normal text</p>\n<pre><code>&lt;html&gt;\n</code></pre>\n<p>normal text again</p>\n")

    def test_complex_markdown_with_angle_brackets(self):
        """Test a more complex markdown sample with angle brackets"""
        markdown = """What light novels have you read in the past week? Something good? Bad? Let us know about it.

And if you want to add your score to the database to help your fellow Bookworms find new reading materials you can use the following template:

><Book Title and Volume> Review Goes Here [5/10]
"""
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertTrue("&lt;Book Title and Volume&gt;" in result)
        self.assertTrue("<blockquote>" in result)

    def test_disallowed_tags(self):
        """Test that disallowed tags are removed"""
        markdown = "Paragraph with <script>alert('xss')</script> script."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual(result, "<p>Paragraph with &lt;script&gt;alert(’xss’)&lt;/script&gt; script.</p>\n")
    
    def test_double_bold(self):
        """Test a variety of cases where bold markdown has caused problems in the past"""
        markdown = "Two **bold** words in one **bold** sentence."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = "<p>Two <strong>bold</strong> words in one <strong>bold</strong> sentence.</p>\n"
        self.assertEqual(result, correct_html)

        markdown = "Links with underscores still work: https://en.wikipedia.org/wiki/Rick_Astley"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = (
            '<p>Links with underscores still work: <a href="https://en.wikipedia.org/wiki/Rick_Astley" '
            'rel="nofollow ugc" target="_blank">https://en.wikipedia.org/wiki/Rick_Astley</a></p>\n')
        self.assertEqual(result, correct_html)

        markdown = "Double **bold** and ***italics* words** in *one* sentence."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = ('<p>Double <strong>bold</strong> and <strong><em>italics</em> words</strong> in '
            '<em>one</em> sentence.</p>\n')
        self.assertEqual(result, correct_html)

        markdown = "Ignore `**bold**` words in code block with **bold** markdown."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = '<p>Ignore <code>**bold**</code> words in code block with <strong>bold</strong> markdown.</p>\n'
        self.assertEqual(result, correct_html)

        markdown = "What about ignoring **bold** words inside a\n\n```\nfenced **code** block?\n```"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = (
            '<p>What about ignoring <strong>bold</strong> words inside a</p>\n<pre><code>fenced **code** block?\n'
            '</code></pre>\n')
        self.assertEqual(result, correct_html)

        markdown = "[Bold in **part of** a link](https://en.wikipedia.org/wiki/Rick_Astley)"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        correct_html = (
            '<p><a href="https://en.wikipedia.org/wiki/Rick_Astley" rel="nofollow ugc" target="_blank">Bold in '
            '<strong>part of</strong> a link</a></p>\n')
        self.assertEqual(result, correct_html)

    def test_strikethrough_in_inline_code(self):
        """Don't strikethrough text in inline code."""
        markdown = "`don't ~~strikethrough~~`"
        correct_html = "<p><code>don't ~~strikethrough~~</code></p>\n"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual(result, correct_html)
    
    def test_strikethrough_in_fenced_code(self):
        """Don't strikethrough text in fenced code block."""
        markdown = "```\ndon't ~~strikethrough~~\n```"
        correct_html = "<pre><code>don't ~~strikethrough~~\n</code></pre>\n"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual(result, correct_html)
    
    def test_spoiler_in_fenced_code(self):
        """Don't format spoiler block in fenced code block."""
        markdown = "```\n::: spoiler Spoiler Title\ndon't ~~strikethrough~~\n:::\n```"
        correct_html = "<pre><code>::: spoiler Spoiler Title\ndon't ~~strikethrough~~\n:::\n</code></pre>\n"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual(result, correct_html)

    def test_code_block_link(self):
        """Test code blocks formatting containing a link"""

        markdown = "```\ncode block with link: https://example.com/ \n```"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual("<pre><code>code block with link: https://example.com/ \n</code></pre>\n", result)
    
    def test_en_dash(self):
        """Test converting -- to an en dash"""

        markdown = "Using--an en dash."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual("<p>Using–an en dash.</p>\n", result)
    
    def test_em_dash(self):
        """Test converting --- to an em dash"""

        markdown = "Em-dashes are fairly idiosyncratic---strange, really---to use regularly."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual("<p>Em-dashes are fairly idiosyncratic—strange, really—to use regularly.</p>\n", result)
    
    def test_em_dash_hr(self):
        """Test converting --- to an em dash while also having a horizontal rule"""

        markdown = "Writing em---dashes is\n\n---\n\nkind of annoying"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual('<p>Writing em—dashes is</p>\n<hr/>\n<p>kind of annoying</p>\n', result)
    
    def test_ellipsis(self):
        """Test converting ... to an ellipsis character"""

        markdown = "Thinking about an ellipsis..."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual("<p>Thinking about an ellipsis…</p>\n", result)
    
    def test_ignore_smartypants_inline_code(self):
        """Test that checks en- and em-dashes as well as ellipses are not in inline code"""

        markdown = "No `en--dash`, nor `em---dash`, nor `ellipsis...` here."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual(
            '<p>No <code>en--dash</code>, nor <code>em---dash</code>, nor <code>ellipsis...</code> here.</p>\n', result)
    
    def test_ignore_smartypants_code_block(self):
        """Test that checks en- and em-dashes as well as ellipses are not in code blocks"""

        markdown = "```\nNo en--dash, nor em---dash, nor ellipsis... here.\n```"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual("<pre><code>No en--dash, nor em---dash, nor ellipsis... here.\n</code></pre>\n", result)
    
    def test_bracketed_links(self):
        """Test that urls in angle brackets are turned into links"""

        markdown = "<https://piefed.social>"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual(
            '<p><a href="https://piefed.social" rel="nofollow ugc" target="_blank">https://piefed.social</a></p>\n',
            result)
    
    def test_bracketed_links_inline_code(self):
        """Test that bracketed links are ignored in inline code"""

        markdown = "`<https://piefed.social>`"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual('<p><code>&lt;https://piefed.social&gt;</code></p>\n', result)
    
    def test_bracketed_links_code_block(self):
        """Test that bracketed links are ignored in code blocks"""

        markdown = "```\n<https://piefed.social>\n```"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual('<pre><code>&lt;https://piefed.social&gt;\n</code></pre>\n', result)
    
    def test_lemmy_autocomplete_community(self):
        """Test that lemmy-formatted autocomplete community names drop the markdown link formatting"""

        markdown = "[!community@instance.tld](https://instance.tld/c/community)"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual('<p>!community@instance.tld</p>\n', result)
    
    def test_lemmy_autocomplete_person(self):
        """Test that lemmy-formatted autocomplete person names drop the markdown link formatting"""

        markdown = "[@user@instance.tld](https://instance.tld/u/user)"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual('<p>@user@instance.tld</p>\n', result)
    
    def test_lemmy_autocomplete_feed(self):
        """
        Test that lemmy-formatted autocomplete feed names drop the markdown link formatting. Note that lemmy does not
        currently have autocomplete for feeds, this is really just a means to keep formatting consistent with people
        and communities.
        """

        markdown = "[~feed@instance.tld](https://instance.tld/f/feed)"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual('<p>~feed@instance.tld</p>\n', result)

    def test_lemmy_autocomplete_multiple_links(self):
        """Test that multiple lemmy autocomplete links are all handled correctly"""

        markdown = "Check out [!community@instance.tld](https://instance.tld/c/community) and [@user@instance.tld](https://instance.tld/u/user)"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertEqual('<p>Check out !community@instance.tld and @user@instance.tld</p>\n', result)
    
    def test_footnotes(self):
        """Test the footnotes extra"""

        # Testing basic functionality
        markdown = "This is a paragraph with a footnote[^1].\n\n[^1]: This is the footnote."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p>This is a paragraph with a footnote<sup class="footnote-ref" id="fnref-1-fn-test"><a href="#fn-1-fn-test">1</a></sup>.</p>\n<div class="footnotes">\n<hr/>\n<ol>\n<li id="fn-1-fn-test">\n<p>This is the footnote.\xa0<a class="footnoteBackLink" href="#fnref-1-fn-test">↩</a></p>\n</li>\n</ol>\n</div>\n'
        self.assertEqual(target_html, result)

        # Testing multiple footnotes with different names
        markdown = "Here is a footnote ref[^1]. Here is another[^note].\n\n[^1]: First footnote\n\n[^note]: Second footnote"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p>Here is a footnote ref<sup class="footnote-ref" id="fnref-1-fn-test"><a href="#fn-1-fn-test">1</a></sup>. Here is another<sup class="footnote-ref" id="fnref-note-fn-test"><a href="#fn-note-fn-test">2</a></sup>.</p>\n<div class="footnotes">\n<hr/>\n<ol>\n<li id="fn-1-fn-test">\n<p>First footnote\xa0<a class="footnoteBackLink" href="#fnref-1-fn-test">↩</a></p>\n</li>\n<li id="fn-note-fn-test">\n<p>Second footnote\xa0<a class="footnoteBackLink" href="#fnref-note-fn-test">↩</a></p>\n</li>\n</ol>\n</div>\n'
        self.assertEqual(target_html, result)

        # Testing multiline footnote with formatting
        markdown = "Here is a footnote ref[^1].\n\n[^1]:\n    indented *line*\n    **formatted** line with `code` and || spoilers ||"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p>Here is a footnote ref<sup class="footnote-ref" id="fnref-1-fn-test"><a href="#fn-1-fn-test">1</a></sup>.</p>\n<div class="footnotes">\n<hr/>\n<ol>\n<li id="fn-1-fn-test">\n<p>indented <em>line</em>\n<strong>formatted</strong> line with <code>code</code> and <tg-spoiler>spoilers</tg-spoiler>\xa0<a class="footnoteBackLink" href="#fnref-1-fn-test">↩</a></p>\n</li>\n</ol>\n</div>\n'
        self.assertEqual(target_html, result)
    
    def test_double_underscore_bold(self):
        """Test using double underscores to signify bold"""

        # Basic functionality
        markdown = "Here is a __bold__ word."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p>Here is a <strong>bold</strong> word.</p>\n'
        self.assertEqual(target_html, result)

        # Multiple places in a sentence
        markdown = "Here are __two__ bold __words__."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p>Here are <strong>two</strong> bold <strong>words</strong>.</p>\n'
        self.assertEqual(target_html, result)

        # Bold and italics
        markdown = "***This*** is ___bold and italics___."
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p><em><strong>This</strong></em> is <em><strong>bold and italics</strong></em>.</p>\n'
        self.assertEqual(target_html, result)
    
    def test_spoiler_blocks(self):
        """Test various functionality with spoiler blocks."""

        # Basic functionality
        markdown = "::: spoiler Summary\nThis is a spoiler.\n:::"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<details><summary>Summary</summary><div class="spoiler_block symmetric">\n<p>This is a spoiler.</p>\n</div></details>\n'
        self.assertEqual(target_html, result)

        # Naked spoiler
        markdown = "::: spoiler\nThis is a spoiler.\n:::"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<details><summary>Spoiler</summary><div class="spoiler_block symmetric">\n<p>This is a spoiler.</p>\n</div></details>\n'
        self.assertEqual(target_html, result)

        # Bulleted list immediately after spoiler opening
        markdown = "::: spoiler Summary\n- one\n- two\n:::"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<details><summary>Summary</summary><div class="spoiler_block symmetric">\n<ul>\n<li>one</li>\n<li>two</li>\n</ul>\n</div></details>\n'
        self.assertEqual(target_html, result)

        # Nested spoilers
        markdown = "::: spoiler First Summary\n::: spoiler Second Summary\nSpoiler content\n:::\n:::"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<details><summary>First Summary</summary><div class="spoiler_block symmetric">\n<details><summary>Second Summary</summary><div class="spoiler_block symmetric">\n<p>Spoiler content</p>\n</div></details>\n</div></details>\n'
        self.assertEqual(target_html, result)

        # Asymmetric spoiler formatting, fallback to old spoiler block behavior
        markdown = "::: spoiler Summary 1\n::: spoiler Summary 2\nThis is a spoiler with no closing\n:::\n"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p><details><summary>Summary 1</summary><div class="spoiler_block"><p>\n</p></div></details> spoiler Summary 2</p>\n<p>This is a spoiler with no closing</p>\n<p>:::</p>\n'
        self.assertEqual(target_html, result)
    
    def test_video_embeds(self):
        """Tests embedded video markdown."""

        # mp4 video
        markdown = "test ![alt text here](https://site.tld/video.mp4)"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p>test <video class="responsive-video" controls="" loop="" muted="" playsinline="" preload="metadata"><source src="https://site.tld/video.mp4" type="video/mp4"/> Your browser does not support playing HTML5 video. <a href="https://site.tld/video.mp4" rel="nofollow ugc" target="_blank">You can download a copy of the file instead.</a> Here is a description of the content: alt text here</video></p>\n'
        self.assertEqual(target_html, result)

        # webm video
        markdown = "test ![alt text here](https://site.tld/video.webm)"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p>test <video class="responsive-video" controls="" loop="" muted="" playsinline="" preload="metadata"><source src="https://site.tld/video.webm" type="video/webm"/> Your browser does not support playing HTML5 video. <a href="https://site.tld/video.webm" rel="nofollow ugc" target="_blank">You can download a copy of the file instead.</a> Here is a description of the content: alt text here</video></p>\n'
        self.assertEqual(target_html, result)

        # mov video (D476, fixed: a supported video format, played like mp4)
        markdown = "test ![alt text here](https://site.tld/video.mov)"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p>test <video class="responsive-video" controls="" loop="" muted="" playsinline="" preload="metadata"><source src="https://site.tld/video.mov" type="video/mp4"/> Your browser does not support playing HTML5 video. <a href="https://site.tld/video.mov" rel="nofollow ugc" target="_blank">You can download a copy of the file instead.</a> Here is a description of the content: alt text here</video></p>\n'
        self.assertEqual(target_html, result)

        # other, unsupported video, just treat it like any other image markdown
        markdown = "test ![alt text here](https://site.tld/video.avi)"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p>test <img alt="alt text here" loading="lazy" src="https://site.tld/video.avi"/></p>\n'
        self.assertEqual(target_html, result)

        # make sure images still work right
        markdown = "test ![alt text here](https://site.tld/image.png)"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p>test <img alt="alt text here" loading="lazy" src="https://site.tld/image.png"/></p>\n'
        self.assertEqual(target_html, result)
    
    def test_inline_spoilers(self):
        """Tests inline spoiler functionality."""

        # Basic functionality
        # telegram/discord format: || like this ||
        markdown = "|| spoiler here ||"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p><tg-spoiler>spoiler here</tg-spoiler></p>\n'
        self.assertEqual(target_html, result)

        # reddit format: >! like this !<
        markdown = ">! spoiler here !<"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p><tg-spoiler>spoiler here</tg-spoiler></p>\n'
        self.assertEqual(target_html, result)

        # with no space after spoiler delineator
        # telegram/discord:
        markdown = "||spoiler here||"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p><tg-spoiler>spoiler here</tg-spoiler></p>\n'
        self.assertEqual(target_html, result)

        # reddit:
        markdown = ">!spoiler here!<"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p><tg-spoiler>spoiler here</tg-spoiler></p>\n'
        self.assertEqual(target_html, result)

        # Multiple occurrences
        # telegram/discord:
        markdown = "|| spoiler here || and || another ||"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p><tg-spoiler>spoiler here</tg-spoiler> and <tg-spoiler>another</tg-spoiler></p>\n'
        self.assertEqual(target_html, result)

        # reddit:
        markdown = ">! spoiler here !< and >! another !<"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p><tg-spoiler>spoiler here</tg-spoiler> and <tg-spoiler>another</tg-spoiler></p>\n'
        self.assertEqual(target_html, result)

        # Mixed formats
        markdown = "|| spoiler here || and >! another !<"
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        target_html = '<p><tg-spoiler>spoiler here</tg-spoiler> and <tg-spoiler>another</tg-spoiler></p>\n'
        self.assertEqual(target_html, result)


    def test_quotes_nested_past_the_depth_limit_render_that_deep(self):
        """markdown2 recurses once per blockquote level, so 200 nested '>' (402 bytes) raised RecursionError out of
        markdown_to_html (fuzz: markdown_to_html/blockquote_200_levels), and well before that its cost grows faster
        than the square of the depth: one line of 128 levels took 0.9 s. A '>' past MARKDOWN_MAX_QUOTE_DEPTH levels
        is now text (owner ruling 2026-10-13), so such a body renders that many levels deep."""
        markdown = ('>' * 200 + 'a\n') * 2
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        depth = deepest = 0
        for tag in re.findall(r'</?blockquote>', result):
            depth += -1 if tag.startswith('</') else 1
            deepest = max(deepest, depth)
        self.assertEqual(deepest, MARKDOWN_MAX_QUOTE_DEPTH)
        self.assertIn('&gt;' * (200 - MARKDOWN_MAX_QUOTE_DEPTH) + 'a', result)

    def test_quotes_nested_well_inside_the_limit_still_render(self):
        markdown = ('>' * 20 + 'a\n') * 2
        result = markdown_to_html(markdown, test_env={'fn_string': 'fn-test'})
        self.assertGreaterEqual(result.count('<blockquote>'), 20)
        self.assertIn('<p>a</p>', result)


class TestLinkPattern(unittest.TestCase):
    """LINK_PATTERN is matched over every rendered body (markdown2's link-patterns extra) and inside every <...>
    (escape_non_html_angle_brackets). Its host and rest-of-url parts overlapped, so a run of url characters with
    nowhere to end backtracked cubically (fuzz: redos_link_pattern/*_then_quote,
    markdown_preprocessors/escape_brackets_link_pump)."""

    FRAGMENTS = ['https://', 'http://', 'www.', 'w', 'a', '_', '-', '.', '/', '"', "'", ' ', '\n', '<', '>', '(',
                 ')', '))', '?', '!', ',', ':', '*', '~', ';', 'é', '//']

    def assert_same_links(self, text):
        self.assertEqual([m.span(1) for m in LINK_PATTERN.finditer(text)],
                         [m.span(1) for m in OLD_LINK_PATTERN.finditer(text)], text)
        old, new = OLD_LINK_PATTERN.match(text), LINK_PATTERN.match(text)
        self.assertEqual(new and new.span(1), old and old.span(1), text)

    def test_finds_the_links_the_old_pattern_found(self):
        for text in random_texts(self.FRAGMENTS, 4000, 24, seed=1):
            self.assert_same_links(text)

    def test_finds_the_links_the_old_pattern_found_in_the_fuzz_corpus(self):
        for text in corpus_texts('redos_link_pattern', 400) + corpus_texts('markdown_to_html', 400):
            self.assert_same_links(text)

    def test_a_run_with_nowhere_to_end_is_read_in_linear_time(self):
        size = 64 * 1024
        for text in ('https://' + 'a' * size + '"', 'www.' * (size // 4) + '"', 'https://' + 'a.' * (size // 2) + '"',
                     'https://' * (size // 8) + '"'):
            started = time.perf_counter()
            self.assertEqual(list(LINK_PATTERN.finditer(text)), [])
            self.assertIsNone(LINK_PATTERN.match(text))
            self.assertLess(time.perf_counter() - started, 1.0)

    def test_markdown_still_autolinks(self):
        result = markdown_to_html('see https://pizza.com. and www.x.example/a_(b) ok', test_env={'fn_string': 'fn-test'})
        self.assertIn('<a href="https://pizza.com" rel="nofollow ugc" target="_blank">https://pizza.com</a>.', result)
        self.assertIn('href="https://www.x.example/a_(b)"', result)

E = {'fn_string': 'fn-test'}


def pump(unit: str, size: int = 64 * 1024) -> str:
    return (unit * (size // len(unit) + 1))[:size]


class TestTheRenderCap(unittest.TestCase):
    """Owner ruling 2026-10-13: remote markdown has no length cap on ingest, so markdown_to_html renders at most
    MARKDOWN_RENDER_CAP characters, then '…'. The slowest shape known renders in about a second at that length (see
    app/utils.py); the quadratic constructs are rewritten first (TestQuadraticConstructs)."""

    def test_a_text_past_the_cap_renders_its_first_characters_then_an_ellipsis(self):
        text = 'word ' * (MARKDOWN_RENDER_CAP // 5 + 100)
        result = markdown_to_html(text, test_env=E)
        self.assertEqual(result, markdown_to_html(text[:MARKDOWN_RENDER_CAP] + '…', test_env=E))
        self.assertTrue(result.endswith('…</p>\n'))

    def test_a_text_at_the_cap_is_rendered_whole(self):
        text = 'a' * MARKDOWN_RENDER_CAP
        self.assertEqual(markdown_to_html(text, test_env=E), f'<p>{text}</p>\n')

    def test_the_cut_never_splits_a_stash_placeholder(self):
        """The preprocessors stash code and links behind placeholders and restore them; the cut is made on the text
        as given, before any of them runs, so a cut through code or a link renders exactly as the shortened text
        would, and no placeholder is left in the output."""
        for text, cap in (('`code span` tail', 5), ('```\nfenced\n```\ntail', 8),
                          ('[link](https://x.example/a) tail', 12),
                          ('<code>x</code> <a href="https://x.example/">y</a>', 22), ('> quoted `code` here', 12)):
            with mock.patch('app.utils.MARKDOWN_RENDER_CAP', cap):
                result = markdown_to_html(text, test_env=E)
            self.assertEqual(result, markdown_to_html(text[:cap] + '…', test_env=E), text)


class TestQuadraticConstructs(unittest.TestCase):
    """markdown2 2.5.5 is quadratic (or worse) in several constructs; 64 KiB of '![a::b](' took 32 s, runs of '!['
    7 s and of '<img ' 8 s. Owner ruling 2026-10-13: neutralise them before markdown2 sees them, leaving ordinary
    markdown byte-identical (TestOrdinaryMarkdownIsUnchanged). Each pump is also a markdown_to_html fuzz seed,
    replayed against the target's budget (fuzz: markdown_to_html/pump_*)."""

    def test_unmatched_link_openers_are_text(self):
        for unit in ('![', '![a::b](', '[a]('):
            result = neutralise_quadratic_markdown(pump(unit))
            self.assertNotIn('[', result)
            self.assertEqual(result.replace('&#91;', '['), pump(unit))
        ordinary = 'x] y) see ![a](b) and [c](d) and [e'
        self.assertEqual(neutralise_quadratic_markdown(ordinary), ordinary)

    def test_links_past_the_copy_limit_stay_text(self):
        result = neutralise_quadratic_markdown(pump('[a](b)', 25_000))
        kept = result.count('[a](b)')
        self.assertGreater(kept, 1000)
        self.assertEqual(result.count('&#91;a](b)'), 25_000 // 6 - kept)
        self.assertEqual(result[:kept * 6], '[a](b)' * kept)

    def test_unended_tag_openers_are_text(self):
        for unit in ('<img ', '<div ', '<a ', '<http://', '<!--', '<?x'):
            result = neutralise_quadratic_markdown(pump(unit, 25_000))
            self.assertEqual(result.replace('&lt;', '<'), pump(unit, 25_000))
            self.assertLessEqual(result.count('<'), 1, unit)   # the last scans only to the end, once
        ordinary = '<!DOCTYPE html><img src="x"> a < b <b>bold</b> <!-- note --> <?php ?> <div\nclass="x">'
        self.assertEqual(neutralise_quadratic_markdown(ordinary), ordinary)

    def test_long_backtick_runs_are_text(self):
        run = '`' * (MARKDOWN_MAX_BACKTICK_RUN + 1)
        self.assertEqual(neutralise_quadratic_markdown(f'a {run} b'), 'a ' + '&#96;' * len(run) + ' b')
        fence = '`' * MARKDOWN_MAX_BACKTICK_RUN
        self.assertEqual(neutralise_quadratic_markdown(f'{fence}\ncode\n{fence}'), f'{fence}\ncode\n{fence}')

    def test_quotes_deeper_than_the_limit_are_text(self):
        depth = MARKDOWN_MAX_QUOTE_DEPTH
        self.assertEqual(limit_quote_depth('> ' * (depth + 3) + 'a\nb'),
                         '> ' * depth + '&gt; ' + '> ' * 2 + 'a\nb')
        self.assertEqual(limit_quote_depth('>' * depth + ' a\n  > > b'), '>' * depth + ' a\n  > > b')

    def test_lines_heavy_with_pipes_lose_their_table_markup(self):
        result = neutralise_quadratic_markdown(pump('|a', 25_000))
        self.assertEqual(result, pump('|a', 25_000).replace('|', '&#124;'))
        table = '| a | b |\n|---|---|\n| ' + 'c' * 20_000 + ' | d |\n'
        self.assertEqual(neutralise_quadratic_markdown(table), table)

    def test_a_long_line_after_a_markdown_block_tag_moves_to_the_next_line(self):
        long_line = 'x' * (MARKDOWN_IN_HTML_LINE_LIMIT + 1)
        self.assertEqual(neutralise_quadratic_markdown(f'<blockquote markdown="1">{long_line}'),
                         f'<blockquote markdown="1">\n{long_line}')
        for text in (f'<blockquote markdown="1">{long_line[1:]}', f'<blockquote>{long_line}</blockquote>',
                     f'<span markdown="1">{long_line}</span>', f'<div markdown="1">\n{long_line}'):
            self.assertEqual(neutralise_quadratic_markdown(text), text)
        self.assertEqual(markdown_to_html(f'> {long_line}', test_env=E),
                         f'<blockquote>\n<p>{long_line}</p>\n</blockquote>\n')

    def test_long_space_runs_keep_only_their_first_spaces(self):
        run = ' ' * (MARKDOWN_MAX_SPACE_RUN + 2)
        self.assertEqual(neutralise_quadratic_markdown(f'a{run}b'), f'a{" " * MARKDOWN_MAX_SPACE_RUN}&#32;&#32;b')
        for text in (f'a{run}\nb', f'a{run}', f'a{run}\\\nb', 'a' + ' ' * MARKDOWN_MAX_SPACE_RUN + 'b'):
            self.assertEqual(neutralise_quadratic_markdown(text), text)

    def test_bare_urls_past_the_autolink_limit_stay_text(self):
        result = markdown_to_html(pump('http://a.b ', 25_000), test_env=E)
        self.assertLess(0, result.count('<a '), 25_000 // 11)
        few = 'see http://a.example and www.b.example and [c](https://c.example/x) and http://d.example.'
        self.assertEqual(markdown_to_html(few, test_env=E).count('<a '), 4)


class TestOrdinaryMarkdownIsUnchanged(unittest.TestCase):
    """The differential behind 'leaves ordinary markdown byte-identical': over every committed markdown seed but
    those built to be slow, and this repository's README and developer docs, markdown_to_html gives exactly what it
    gives with both rewrites switched off. (The markdown tests above, unchanged, are the rest of it.)"""

    ROOT = pathlib.Path(__file__).parent.parent

    def texts(self):
        for target in ('markdown_to_html', 'markdown_preprocessors'):
            for path in sorted((CORPUS / target).iterdir()):
                text = path.read_bytes()[1:].decode('utf-8', errors='replace')
                # Not the pumps (64 KiB of one construct, built to be slow) or the 200- and 250-level quotes.
                if len(text) <= 4096 and '>' * 100 not in text.replace(' ', ''):
                    yield path.name, text
        for path in [self.ROOT / 'README.md', *sorted((self.ROOT / 'docs' / 'developer_docs').glob('*.md'))]:
            text = path.read_text()
            for start in range(0, len(text), MARKDOWN_RENDER_CAP):
                yield f'{path.name}:{start}', text[start:start + MARKDOWN_RENDER_CAP]

    def test_the_rewrites_change_no_ordinary_output(self):
        for name, text in self.texts():
            with mock.patch('app.utils.limit_quote_depth', lambda value: value), \
                    mock.patch('app.utils.neutralise_quadratic_markdown', lambda value: value):
                plain = markdown_to_html(text, test_env=E)
            self.assertEqual(markdown_to_html(text, test_env=E), plain, name)

    def test_the_rewrites_leave_ordinary_text_alone(self):
        for name, text in self.texts():
            self.assertEqual(limit_quote_depth(text), text, name)
            self.assertEqual(neutralise_quadratic_markdown(text), text, name)


if __name__ == '__main__':
    unittest.main()
