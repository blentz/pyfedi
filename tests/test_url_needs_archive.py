"""url_needs_archive (app/post/util.py:271-287) decides whether to offer a
removepaywall.com archive link for a post's link URL. It is lower severity
than domain_from_url's equivalent defect (app/utils.py, fixed at 81a3e40e):
the result feeds a membership test that drives an archive-link affordance,
not a Domain row used for access control.

Before the fix in this commit, line 277 read
`urlparse(url.replace('www.', ''))` -- a blanket replacement over the whole
URL string, before parsing, removing every occurrence rather than a leading
host label. That fails in both directions:

- False negative: `https://awww.nytimes.com/x` mangles to `anytimes.com`, so
  no archive link is offered for a genuinely paywalled host.
- False positive: `https://nywww.times.com/x` mangles to `ny` + `times.com`
  = `nytimes.com`, so an attacker-registered host is treated as paywalled.
"""
from app.post.util import url_needs_archive


class TestHostIsNotMangled:
    """Mutation that fails this: restoring `.replace('www.', '')` on the
    whole URL string at app/post/util.py:277."""

    def test_an_interior_www_does_not_produce_a_false_positive(self):
        """The sharp one. Pre-fix, 'https://nywww.times.com/x' mangles to
        'ny' + 'times.com' == 'nytimes.com', an attacker-registered host
        treated as paywalled."""
        assert url_needs_archive('https://nywww.times.com/x') is False

    def test_an_interior_www_does_not_produce_a_false_negative(self):
        """Does NOT discriminate the fix on its own: pre-fix,
        'https://awww.nytimes.com/x' mangles to 'anytimes.com', which is
        also not in the paywalled list, so this returns False both before
        and after the fix -- for the wrong reason pre-fix. Kept because it
        documents the failure mode and must still hold post-fix."""
        assert url_needs_archive('https://awww.nytimes.com/x') is False


class TestLeadingWwwIsStripped:
    """The intended normalization, which the fix must preserve. This is the
    test that rejects an over-broad fix that simply deletes the replacement
    without adding a prefix-only strip: that would break every
    'www.'-prefixed paywalled link, the common real-world case."""

    def test_www_prefixed_paywalled_host_still_needs_archive(self):
        assert url_needs_archive('https://www.nytimes.com/x') is True


class TestBarePaywalledHost:
    def test_bare_paywalled_host_needs_archive(self):
        assert url_needs_archive('https://nytimes.com/x') is True


class TestExemptions:
    """Mutation that fails these: deleting either unlock-code exemption."""

    def test_nytimes_with_unlocked_article_code_is_exempt(self):
        assert url_needs_archive('https://nytimes.com/x?unlocked_article_code=abc') is False

    def test_theatlantic_with_gift_is_exempt(self):
        assert url_needs_archive('https://theatlantic.com/x?gift=abc') is False


class TestNonPaywalledHost:
    def test_non_paywalled_host_does_not_need_archive(self):
        assert url_needs_archive('https://example.com/x') is False


class TestFalsyInput:
    def test_empty_string(self):
        assert url_needs_archive('') is False

    def test_none(self):
        assert url_needs_archive(None) is False
