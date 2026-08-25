from app.utils import (domain_from_email, is_bot, is_video_hosting_site,
                       mimetype_from_url, reply_is_just_link_to_gif_reaction,
                       reply_is_low_effort, shorten_string, shorten_url)

# NOTE: inbox_domain is intentionally NOT tested here. A caller check found no
# reference to it anywhere in the codebase: not in any .py file (besides its own
# definition), not registered as a Jinja global/filter in app/request_hooks.py or
# profile_app.py, and not used in templates/. It is dead code. See
# task-2-report.md for the finding.


class TestIsVideoHostingSite:
    """Fails if the None/empty guard, the prefix list, or the PeerTube check is removed."""

    def test_none(self):
        assert is_video_hosting_site(None) is False

    def test_empty_string(self):
        assert is_video_hosting_site('') is False

    def test_each_known_host(self):
        for url in ['https://youtube.com/watch?v=x', 'https://www.youtube.com/watch?v=x',
                    'https://youtu.be/x', 'https://vimeo.com/1', 'https://www.vimeo.com/1',
                    'https://streamable.com/x', 'https://www.redgifs.com/watch/x']:
            assert is_video_hosting_site(url) is True, url

    def test_peertube_is_matched_by_path_not_host(self):
        assert is_video_hosting_site('https://tilvids.com/videos/watch/abc') is True

    def test_unrelated_site(self):
        assert is_video_hosting_site('https://example.com/page') is False

    def test_prefix_match_is_anchored(self):
        """A host that merely contains youtube.com must not match."""
        assert is_video_hosting_site('https://notyoutube.com/watch') is False


class TestDomainFromEmail:
    """RFC 5321 §4.1.2: a Mailbox is Local-part "@" Domain. The Domain itself
    cannot contain an unescaped '@', so taking the segment after the last '@'
    always recovers the domain even when the local-part is a quoted string
    containing '@'. No divergence found for this function.
    """

    def test_extracts_the_domain(self):
        assert domain_from_email('user@example.com') == 'example.com'

    def test_none(self):
        assert domain_from_email(None) == ''

    def test_empty_and_whitespace(self):
        assert domain_from_email('') == ''
        assert domain_from_email('   ') == ''

    def test_no_at_sign(self):
        assert domain_from_email('notanemail') == ''

    def test_multiple_at_signs_takes_the_last_part(self):
        assert domain_from_email('a@b@example.com') == 'example.com'


class TestMimetypeFromUrl:
    def test_png(self):
        assert mimetype_from_url('https://example.com/a.png') == 'image/png'

    def test_query_string_is_stripped_before_guessing(self):
        assert mimetype_from_url('https://example.com/a.png?v=2') == 'image/png'

    def test_unknown_extension_is_none(self):
        assert mimetype_from_url('https://example.com/a.unknownext') is None

    def test_fragment_does_not_leak_into_the_extension(self):
        """RFC 3986 §3.5: '#' introduces the fragment component, which terminates
        the URI just as '?' terminates it for the query. The implementation only
        does `path.split('?')[0]`, with no equivalent split on '#' — but this is
        NOT a bug: `urlparse()` (called first) already separates path, query and
        fragment into distinct components per RFC 3986, so `parsed_url.path` never
        contains a fragment to begin with. Verified directly against CPython's
        urllib.parse for schemed, scheme-less and relative forms. This test is a
        regression guard: it would fail if a future change stopped using
        `urlparse()` (e.g. naive string slicing on the raw URL).
        """
        assert mimetype_from_url('https://example.com/a.png#frag') == 'image/png'


class TestReplyIsJustLinkToGifReaction:
    def test_each_known_gif_host(self):
        for host in ['https://media.tenor.com/', 'https://media1.tenor.com/',
                     'https://media2.tenor.com/', 'https://media3.tenor.com/',
                     'https://i.giphy.com/', 'https://i.imgflip.com',
                     'https://media1.giphy.com/', 'https://media2.giphy.com/',
                     'https://media3.giphy.com/', 'https://media4.giphy.com/']:
            assert reply_is_just_link_to_gif_reaction(host + 'x.gif') is True, host

    def test_surrounding_whitespace_is_stripped_first(self):
        assert reply_is_just_link_to_gif_reaction('  https://i.giphy.com/x.gif  ') is True

    def test_ordinary_reply(self):
        assert reply_is_just_link_to_gif_reaction('a real comment') is False


class TestReplyIsLowEffort:
    def test_the_three_recognised_forms(self):
        for body in ['this', 'this.', 'this!']:
            assert reply_is_low_effort(body) is True, body

    def test_case_and_whitespace_insensitive(self):
        assert reply_is_low_effort('  THIS!  ') is True

    def test_a_real_reply(self):
        assert reply_is_low_effort('this is a real point') is False


class TestShortenString:
    def test_short_input_is_unchanged(self):
        assert shorten_string('abc') == 'abc'

    def test_long_input_is_truncated_with_an_ellipsis(self):
        result = shorten_string('a' * 100, max_length=10)
        assert result == 'aaaaaaa…'
        assert len(result) == 8

    def test_falsy_input(self):
        assert shorten_string('') == ''
        assert shorten_string(None) == ''


class TestShortenUrl:
    def test_scheme_is_stripped(self):
        assert shorten_url('https://example.com') == 'example.com'
        assert shorten_url('http://example.com') == 'example.com'

    def test_falsy_input_returns_empty_string(self):
        """Regression guard for a real bug (ruled and fixed 2026-08-25): `else: ''`
        at app/utils.py:1274 was missing its `return`, so this yielded None. Since
        `shorten_url` is registered as the Jinja filter `shorten_url`
        (app/request_hooks.py:75, profile_app.py:40), the defect was user-visible:
        a template rendering a falsy value interpolated the literal text "None".
        Fixed to `return ''`, per controller ruling, precisely because of that
        Jinja-filter exposure.
        """
        assert shorten_url('') == ''

    def test_none_input_returns_empty_string(self):
        """The realistic production path to the bug above: the Jinja filter can be
        handed a null column value (e.g. a None url/body), not just ''.
        """
        assert shorten_url(None) == ''


class TestIsBot:
    def test_bot_substring(self):
        assert is_bot('Googlebot/2.1') is True

    def test_meta_external_agent(self):
        assert is_bot('meta-externalagent/1.1') is True

    def test_case_insensitive(self):
        assert is_bot('GOOGLEBOT') is True

    def test_ordinary_browser(self):
        assert is_bot('Mozilla/5.0 (X11; Linux x86_64)') is False
