import re

from app import utils
from app.utils import (allowlist_html, community_link_to_href, escape_img, feed_link_to_href,
                       handle_lemmy_autocomplete, links_with_parens,
                       mastodon_extra_field_link, person_link_to_href,
                       piefed_markdown_to_lemmy_markdown, pop_code, pop_link, remove_images, sub_skipping, stash_code_html, stash_code_md,
                       stash_link_html, escape_non_html_angle_brackets, handle_bold_em, handle_reddit_spoilers)
from tests.test_allowlist_html import assert_linear, pump
from tests.test_markdown_to_html import corpus_texts, random_texts


def old_stash(pattern, text, placeholder):
    """A stash as it was before its regex was made linear: re.sub over `pattern`, numbering each snippet."""
    snippets = []

    def store(match):
        snippets.append(match.group(0))
        return f'{placeholder}{len(snippets) - 1}$'

    return snippets, re.sub(pattern, store, text)


class TestRemoveImages:
    """Fails if either decompose() loop is removed."""

    def test_img_tags_are_removed(self):
        assert '<img' not in remove_images('<p>text</p><img src="x.png">')

    def test_video_tags_are_removed(self):
        assert '<video' not in remove_images('<p>text</p><video src="x.mp4"></video>')

    def test_surrounding_text_survives(self):
        assert 'keep me' in remove_images('<p>keep me</p><img src="x.png">')

    def test_html_without_media_is_unchanged(self):
        assert remove_images('<p>plain</p>') == '<p>plain</p>'


class TestEscapeImg:
    def test_img_becomes_a_placeholder(self):
        result = escape_img('before <img src="x.png"> after')
        assert '<img' not in result
        assert 'image placeholder' in result

    def test_text_without_images_is_unchanged(self):
        assert escape_img('no images here') == 'no images here'


class TestPiefedMarkdownToLemmyMarkdown:
    def test_soft_break_gains_two_spaces(self):
        assert piefed_markdown_to_lemmy_markdown('a\r\nb') == 'a  \r\nb'

    def test_text_without_breaks_is_unchanged(self):
        assert piefed_markdown_to_lemmy_markdown('one line') == 'one line'


class TestMastodonExtraFieldLink:
    def test_returns_the_first_href(self):
        html = '<a href="https://example.com/">example</a>'
        assert mastodon_extra_field_link(html) == 'https://example.com/'

    def test_no_anchor_returns_the_input_unchanged(self):
        """WAS A BUG, now fixed: this used to fall off the end and return None.

        Both live callers (app/activitypub/util.py:648, :1157) only invoke this
        function when '<a ' already appears in the raw string, so this exact path
        looked guarded. But that guard is a naive substring check, not real HTML
        validation, so malformed remote input (e.g. an unclosed '<a href...' with
        no '>') reached here anyway. The caller then does
        `field_data['value'].strip()` on the result with no None-check, so the
        None return became an unhandled AttributeError while processing a remote
        actor's profile fields -- and `activity_json['attachment']` is entirely
        remote-instance-controlled, so that was a remote-triggerable crash in
        deployed software.

        The contract now: a str in every path, so both call sites' .strip() is
        safe without either of them being touched. When there is no anchor
        carrying an href, the extra field is returned unchanged -- the caller
        stores the field's own text, which is the closest thing to what the
        remote instance meant.
        """
        assert mastodon_extra_field_link('<p>no link</p>') == '<p>no link</p>'

    def test_unclosed_anchor_returns_the_input_unchanged(self):
        """The exact input that used to become AttributeError at the call site."""
        assert mastodon_extra_field_link('text <a ') == 'text <a '

    def test_anchor_without_an_href_returns_the_input_unchanged(self):
        """WAS A BUG, now fixed: `tag['href']` raised KeyError('href') here.

        Same reachability as above -- '<a ' appears in the raw string, so the
        call sites' substring guard lets it through.
        """
        assert mastodon_extra_field_link('<a class="x">link</a>') == '<a class="x">link</a>'
        assert mastodon_extra_field_link('<a name="anchor">y</a>') == '<a name="anchor">y</a>'

    def test_an_href_less_anchor_does_not_hide_a_later_link(self):
        html = '<a class="x">label</a> <a href="https://example.com/">example</a>'
        assert mastodon_extra_field_link(html) == 'https://example.com/'

    def test_an_empty_href_is_returned_as_the_empty_string(self):
        """Pre-existing behaviour, pinned: an href that is present but empty is
        still an href, and returning '' keeps the return type a str."""
        assert mastodon_extra_field_link('<a href="">e</a>') == ''

    def test_the_return_is_always_a_str(self):
        for value in ['<p>no link</p>', 'text <a ', '<a class="x">l</a>', '',
                      '<a href="https://example.com/">e</a>', '<a href="">e</a>']:
            assert isinstance(mastodon_extra_field_link(value), str), value


class TestLinksWithParens:
    """Fails if either paren-balancing arm is removed."""

    def test_trailing_paren_outside_the_link_is_pulled_in(self):
        html = '<a href="https://en.wikipedia.org/wiki/Foo_(bar">Foo_(bar</a>)'
        result = links_with_parens(html)
        assert 'Foo_(bar)"' in result or 'Foo_(bar)' in result

    def test_trailing_paren_inside_the_link_is_pushed_out(self):
        html = '<a href="https://example.com/x)">x)</a>'
        result = links_with_parens(html)
        assert 'href="https://example.com/x"' in result

    def test_an_anchor_without_an_href_is_left_alone(self):
        """An <a name=...> has no URL to balance. links_with_parens read href.count on None, so one such anchor in a
        post's markdown raised AttributeError out of markdown_to_html (fuzz: markdown_preprocessors/
        links_with_parens_no_href, markdown_to_html/anchor_without_href)."""
        html = '<a name="x">y)</a>)'
        assert links_with_parens(html) == html

    def test_balanced_link_is_untouched(self):
        html = '<a href="https://example.com/(x)">(x)</a>'
        assert links_with_parens(html) == html

    def test_a_pushed_out_paren_absorbs_a_following_comment_into_text(self):
        """Covers app/utils.py line 953 / branch [950, 953], AND discriminates.

        The final whole-branch review found that line uncovered with no recorded
        reason. It is the second arm of the push-out branch -- reached when a
        node FOLLOWS the link, so the `)` is prepended to that node instead of
        inserted as a new sibling.

        A test using ordinary following text covers the line and catches
        NOTHING: prepending `)` to a text node and inserting `)` as a sibling
        render identically, so collapsing both arms onto insert_after passes it.
        That test was written, measured against exactly that break, found to be
        worthless, and deleted. This one is its replacement.

        PINS A PRE-EXISTING QUIRK, reported rather than fixed.

        bs4's Comment subclasses NavigableString, so an HTML comment following
        the link takes the prepend arm. `")" + comment` is a plain str, and
        replace_with then swaps the Comment node for a NavigableString -- the
        comment's TEXT becomes visible content. The insert_after arm would have
        left the comment intact.

        Consequences checked before deciding not to fix: this is markdown output
        heading for allowlist_html, the absorbed text is escaped as character
        data like any other text, and it needs an author to write both an
        unbalanced trailing `)` in a link and an adjacent HTML comment. Cosmetic,
        not a security property. Fixing it means choosing what a comment beside a
        link should do, which is a behaviour decision and its own change; this
        test says what happens today so that change is visible when someone makes
        it.
        """
        html = '<a href="https://example.com/x)">x)</a><!-- note -->'
        result = links_with_parens(html)
        assert result == '<a href="https://example.com/x">x</a>) note '


class TestActorLinkHelpers:
    """The uncovered line in each is the `current_app.config` fallback branch,
    reached only when server_name_override is not passed."""

    def test_community_link_uses_the_configured_server_name(self, app):
        result = community_link_to_href('!news@lemmy.world')
        assert 'test.piefed.local/community/lookup/news/lemmy.world' in result

    def test_community_link_honours_the_override(self, app):
        result = community_link_to_href('!news@lemmy.world', server_name_override='other.example')
        assert 'other.example/community/lookup/' in result

    def test_feed_link_uses_the_configured_server_name(self, app):
        result = feed_link_to_href('~tech@lemmy.world')
        assert 'test.piefed.local/feed/lookup/tech/lemmy.world' in result

    def test_person_link_uses_the_configured_server_name(self, app):
        result = person_link_to_href('@bob@lemmy.world')
        assert 'test.piefed.local/user/lookup/bob/lemmy.world' in result


class TestHandleLemmyAutocomplete:
    def test_autocompleted_community_link_becomes_bare_handle(self):
        text = '[!news@lemmy.world](https://lemmy.world/c/news)'
        assert handle_lemmy_autocomplete(text) == '!news@lemmy.world'

    def test_autocompleted_person_link_becomes_bare_handle(self):
        text = '[@bob@lemmy.world](https://lemmy.world/u/bob)'
        assert handle_lemmy_autocomplete(text) == '@bob@lemmy.world'

    def test_an_ordinary_markdown_link_is_left_alone(self):
        text = '[example](https://example.com)'
        assert handle_lemmy_autocomplete(text) == text


class TestLemmyAutocompleteIsLinear:
    """A line of '[!' (or '[@', '[~') scanned to the end of the line from every one, and from every '](' when no
    ')' followed (fuzz: markdown_preprocessors/lemmy_autocomplete_pump)."""

    def test_matches_what_it_matched(self):
        old = re.compile(r"\[((!|@|~).*?)\]\(.*?\)")
        fragments = ['[!a@b](x)', '[', '!', '@', '~', ']', '(', ')', '](', 'a', ' ', '\n', '`c`']
        for text in random_texts(fragments, 4000, 12, seed=7) + corpus_texts('markdown_preprocessors', 300):
            expected = old.sub(lambda m: '<' + m.group(1) + '>', text)
            assert utils._LEMMY_AUTOCOMPLETE_RE.sub(
                lambda m: m.group(0) if m.lastindex is None else '<' + m.group(1) + '>', text) == expected, text

    def test_an_unclosed_line_is_read_in_linear_time(self):
        assert_linear(self, [pump('[!'), pump('[!a](')], handle_lemmy_autocomplete, seconds=0.5)


class TestRedditSpoilersAreLinear:
    """A line of '>!' scanned to the end of the line from every one (fuzz: markdown_preprocessors/
    reddit_spoilers_pump)."""

    def test_matches_what_it_matched(self):
        old = re.compile(r'>!\s?(.+?)\s?!<', re.M)
        fragments = ['>!', '!<', '>!a!<', 'a', ' ', '\n', '\t', '!', '<', '>']
        for text in random_texts(fragments, 6000, 12, seed=8) + corpus_texts('markdown_preprocessors', 300):
            assert sub_skipping(utils._REDDIT_SPOILER_RE, r'[\1]', text) == old.sub(r'[\1]', text), repr(text)

    def test_a_spoiler_opened_at_the_end_of_a_line_can_close_on_the_next(self):
        assert handle_reddit_spoilers('x >!y >!\nz!<') == 'x >!y <tg-spoiler>z</tg-spoiler>'

    def test_an_unclosed_line_is_read_in_linear_time(self):
        assert_linear(self, [pump('>!'), pump('>! a')], handle_reddit_spoilers, seconds=0.5)


class TestAllowlistHtmlResidue:
    """Closes the remaining uncovered lines in allowlist_html (387, 392, 407, 471,
    479, 494) that survive after tests/test_allowlist_html.py. Always pass
    test_env={'fn_string': 'fn-test'} to avoid the DB-backed
    get_emoji_replacements()/fediverse_domains() calls.

    Lines 387 and 392 (the :emoji: substitution body) and line 479 (the
    instance-domain href rewrite) cannot be reached this way: passing any
    test_env value that is not the literal singleton `False` forces
    emoji_replacements to None (line 385: `test_env is False`) and forces
    instance_domains to [] (line 442: `if not test_env`), so those branches
    are structurally dead without a real database. See task-3-report.md.
    """

    def test_empty_angle_brackets_are_escaped(self):
        """Covers line 407: tag_content == '' after stripping whitespace-only
        bracket contents, e.g. a stray '< >' that isn't a real HTML tag."""
        html = '<p>a < > b</p>'
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        assert '&lt; &gt;' in result

    def test_mastodon_invisible_span_is_stripped(self):
        """Covers line 471: spans with class="invisible" (mastodon's link-shortening
        markup) are extracted entirely, not just stripped of attributes."""
        html = '<a href="https://example.com/"><span class="invisible">https://</span>example.com</a>'
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        assert '<span' not in result
        assert 'invisible' not in result

    def test_table_gets_table_class(self):
        """Covers line 494: any surviving <table> tag is given class="table"
        for Bootstrap styling."""
        html = '<table><tr><td>x</td></tr></table>'
        result = allowlist_html(html, test_env={'fn_string': 'fn-test'})
        assert '<table class="table">' in result


class TestCodeAndLinkStashesAreLinear:
    """The stashes run on every rendered body, several times each. A tag with no closing tag after it scanned to
    the end of the text from every later opener too (fuzz: markdown_code_stash/unclosed_*_pump,
    the link stash in redos_allowlist_html/empty_anchor_pump)."""

    def test_the_code_stash_stashes_what_it_stashed(self):
        fragments = ['<code>', '</code>', '<code>x</code>', 'a', ' ', '\n', '<', '>']
        for text in random_texts(fragments, 3000, 12, seed=5) + corpus_texts('markdown_code_stash', 300):
            assert stash_code_html(text, 'PH') == old_stash(r'<code>[\s\S]*?<\/code>', text, 'PH'), text

    def test_unclosed_code_is_stashed_in_linear_time(self):
        assert_linear(self, [pump('<code>')], lambda text: stash_code_html(text, 'PH'), seconds=0.5)

    def test_the_link_stash_stashes_what_it_stashed(self):
        fragments = ['<a href=', '</a>', '<a href=x>y</a>', 'a', ' ', '\n', '<', '>']
        for text in random_texts(fragments, 3000, 12, seed=6) + corpus_texts('markdown_code_stash', 300):
            assert stash_link_html(text, 'PH') == old_stash(r'<a href=[\s\S]*?<\/a>', text, 'PH'), text

    def test_unclosed_links_are_stashed_in_linear_time(self):
        assert_linear(self, [pump('<a href=')], lambda text: stash_link_html(text, 'PH'), seconds=0.5)


class TestCodeStashRoundTrip:
    """stash_code_md lifts fenced blocks first, then inline code from what is left, so an inline span can hold a
    fenced block's placeholder. pop_code restored snippets in stash order, so the fenced block's placeholder was
    put back after it had been looked for, and the random placeholder text reached the rendered post in place of
    the code (fuzz: markdown_code_stash/inline_code_around_fenced_block)."""

    def test_inline_code_around_a_fenced_block_round_trips(self):
        text = '`x ```y``` z`'
        snippets, stashed = stash_code_md(text, 'PH')
        assert pop_code(snippets, stashed, 'PH') == text

    def test_a_preprocessor_gives_the_code_back(self):
        assert escape_non_html_angle_brackets('`x ```<y>``` z`') == '`x ```<y>``` z`'
        assert handle_bold_em('`a ```**b**``` c`') == '`a ```**b**``` c`'

    def test_thousands_of_snippets_are_restored_in_linear_time(self):
        """One str.replace over the whole text per snippet: 2 s for one preprocessor over 64 KiB of `a` spans
        (fuzz: markdown_preprocessors/many_code_spans, many_html_code_spans, markdown_code_stash/fence_pump)."""
        assert_linear(self, [pump('`a` ')], handle_bold_em, seconds=0.5)
        text = pump('<a href=x>y</a> ')
        snippets, stashed = stash_link_html(text, 'PH')
        assert_linear(self, [stashed], lambda stashed: pop_link(snippets, stashed, 'PH'), seconds=0.5)
        assert pop_link(snippets, stashed, 'PH') == text

    def test_an_index_no_snippet_has_is_left_as_it_is(self):
        assert pop_code(['a'], 'PH0$ PH1$ PH01$', 'PH') == 'a PH1$ PH01$'
