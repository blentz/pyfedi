from app.utils import (allowlist_html, community_link_to_href, escape_img, feed_link_to_href,
                       handle_lemmy_autocomplete, links_with_parens,
                       mastodon_extra_field_link, person_link_to_href,
                       piefed_markdown_to_lemmy_markdown, remove_images)


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

    def test_no_anchor_returns_none(self):
        """BUG (see plan): falls off the end rather than returning a documented value.

        Both live callers (app/activitypub/util.py:648, :1157) only invoke this
        function when '<a ' already appears in the raw string, so this exact path
        is guarded in practice. But that guard is a naive substring check, not real
        HTML validation, so malformed remote input (e.g. an unclosed '<a href...'
        with no '>') can still reach here. When it does, the caller immediately
        does `field_data['value'].strip()` on the result with no None-check, so a
        None return here becomes an unhandled AttributeError while processing a
        remote actor's profile fields.

        This return value is UNGUARDED at both call sites, and it is not the only
        unguarded failure mode: an anchor with no `href` attribute (e.g.
        '<a class="x">link</a>' or '<a name="anchor">y</a>') raises KeyError('href')
        on `tag['href']` above, for the same reason. Both `activity_json['attachment']`
        entries are entirely remote-instance-controlled, making this a
        remote-triggerable crash in deployed software. A controller ruling
        confirmed this by execution and escalated it to the project owner directly
        rather than fixing it here — the fix belongs in app/activitypub/util.py,
        outside this sub-project's file scope. See task-3-report.md, "Finding 2",
        for the full analysis. Do not add the KeyError case as a test in this
        file; it belongs with the eventual fix.
        """
        assert mastodon_extra_field_link('<p>no link</p>') is None


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

    def test_balanced_link_is_untouched(self):
        html = '<a href="https://example.com/(x)">(x)</a>'
        assert links_with_parens(html) == html


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
