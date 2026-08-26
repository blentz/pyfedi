"""`app.utils.is_safe_redirect_target` -- the one origin check.

Every place a user-influenced URL becomes a redirect target goes through this
function: `back()` and all three of `referrer()`'s sources. There used to be two
implementations of the control (`back()` had none, `referrer()` and two community
routes had `SERVER_NAME in url`), and the one that existed was a SUBSTRING test:
`https://evil.example/?x=test.piefed.local` contains the server name and passed
it. These tests pin the real check.

Everything here runs under the DEFAULT redirect policy -- same origin only, the
behaviour an install that has never touched the admin setting gets. The other
three policies, and the guarantee that none of them relaxes any of the parsing
pinned below, are in the sibling file tests/test_redirect_policy.py.

RECLASSIFICATION: `is_safe_redirect_target` is no longer a pure function. Now
that the policy is admin-configurable it reads `get_setting`, and under the two
instance-matching policies it queries the Instance table -- so it needs a working
database, not merely an app context. These tests already ran with `site` (and so
with `db_session`), so none of them had to move; but the function itself now
belongs in the DB-backed category rather than the pure one.
"""

import pytest

from app.utils import is_safe_redirect_target

pytestmark = pytest.mark.usefixtures('site')


# SERVER_NAME under test is 'test.piefed.local' (.env.test).

class TestRelativeUrlsAreSameOriginByDefinition:
    @pytest.mark.parametrize('url', [
        '/foo',
        'foo',
        '/foo/bar?x=1#frag',
        'foo?x=1',
        '/',
        '?x=1',
        '#frag',
    ])
    def test_a_relative_url_is_accepted(self, app, url):
        with app.test_request_context('/'):
            assert is_safe_redirect_target(url) is True


class TestSameOriginAbsoluteUrls:
    @pytest.mark.parametrize('url', [
        'https://test.piefed.local/x',
        'http://test.piefed.local/x',
        'https://test.piefed.local',
        'https://TEST.PieFed.Local/x',          # hosts are case-insensitive
        'https://test.piefed.local:5000/x',     # port is not part of the check
    ])
    def test_our_own_host_is_accepted(self, app, url):
        with app.test_request_context('/'):
            assert is_safe_redirect_target(url) is True


class TestCrossOriginIsRejected:
    def test_another_host_is_rejected(self, app):
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://evil.example/x') is False

    def test_the_substring_lookalike_is_rejected(self, app):
        """THE regression guard for the bypass the old substring check had.

        `current_app.config['SERVER_NAME'] in url` is true for this URL, because
        the server name appears in the query string. It is not this site.
        """
        with app.test_request_context('/'):
            assert is_safe_redirect_target(
                'https://evil.example/?x=test.piefed.local') is False

    @pytest.mark.parametrize('url', [
        'https://evil.example/#test.piefed.local',
        'https://evil.example/test.piefed.local',
        'https://test.piefed.local.evil.example/x',
        'https://eviltest.piefed.local/x',
    ])
    def test_other_shapes_that_merely_contain_the_server_name_are_rejected(self, app, url):
        with app.test_request_context('/'):
            assert is_safe_redirect_target(url) is False

    def test_userinfo_cannot_smuggle_our_host_in(self, app):
        """`https://our.host@evil.example/` has host evil.example -- everything
        before the `@` is userinfo, which browsers ignore for navigation."""
        with app.test_request_context('/'):
            assert is_safe_redirect_target(
                'https://test.piefed.local@evil.example/') is False

    @pytest.mark.parametrize('url', [
        '//evil.example/x',
        '//evil.example',
        '//test.piefed.local@evil.example/x',
    ])
    def test_protocol_relative_urls_are_rejected(self, app, url):
        """A classic bypass: no scheme, so it looks relative to a naive check,
        but a browser reads it as another origin."""
        with app.test_request_context('/'):
            assert is_safe_redirect_target(url) is False

    @pytest.mark.parametrize('url', [
        '/\\evil.example/x',
        '\\\\evil.example/x',
        '/\\\\evil.example',
    ])
    def test_backslash_authority_bypasses_are_rejected(self, app, url):
        """WHATWG URL treats a backslash in the authority position as a slash,
        so `/\\evil.example` navigates to another origin even though urlparse
        reports it as a relative path."""
        with app.test_request_context('/'):
            assert is_safe_redirect_target(url) is False


class TestNonHttpSchemesAreRejected:
    @pytest.mark.parametrize('url', [
        'javascript:alert(1)',
        'data:text/html,<script>alert(1)</script>',
        'file:///etc/passwd',
        'ftp://test.piefed.local/x',
        'JavaScript:alert(1)',
        'jAvAsCrIpT:alert(1)',
    ])
    def test_only_http_and_https_are_accepted(self, app, url):
        with app.test_request_context('/'):
            assert is_safe_redirect_target(url) is False


class TestMalformedAndUnparseable:
    def test_a_malformed_ipv6_host_is_rejected_and_does_not_raise(self, app):
        """urlparse raises ValueError('Invalid IPv6 URL') here. An earlier task
        fixed exactly this class of crash in allowlist_html; the guard must
        answer False, not propagate."""
        with app.test_request_context('/'):
            assert is_safe_redirect_target('http://[::1') is False

    @pytest.mark.parametrize('url', [
        'https://[::1]:notaport/x',
        'http://test.piefed.local:99999999/x',
        'https://[fe80::1%25eth0/x',
    ])
    def test_anything_that_will_not_parse_is_rejected(self, app, url):
        with app.test_request_context('/'):
            assert is_safe_redirect_target(url) is False

    @pytest.mark.parametrize('url', ['', '   ', None, 'https://', '//'])
    def test_nothing_to_redirect_to_is_rejected(self, app, url):
        with app.test_request_context('/'):
            assert is_safe_redirect_target(url) is False

    @pytest.mark.parametrize('url', [
        'https://evil.exam\tple/x',
        'https://evil.exam\nple/x',
        'ht\ttps://evil.example/x',
        '/foo\x00bar',
    ])
    def test_control_characters_are_rejected(self, app, url):
        """Browsers strip tab/CR/LF from a URL before parsing, so a check that
        parses the raw string can disagree with the browser about the host."""
        with app.test_request_context('/'):
            assert is_safe_redirect_target(url) is False

    def test_a_non_string_is_rejected(self, app):
        with app.test_request_context('/'):
            assert is_safe_redirect_target(object()) is False


class TestServerNameWithAPort:
    """env.sample ships SERVER_NAME=127.0.0.1:5000, so the configured value may
    carry a port. The port is stripped before the host comparison; without that
    every absolute URL would be rejected on such a deployment."""

    @pytest.fixture
    def server_name(self, app):
        original = app.config['SERVER_NAME']
        app.config['SERVER_NAME'] = '127.0.0.1:5000'
        yield
        app.config['SERVER_NAME'] = original

    def test_our_host_is_accepted_when_server_name_carries_a_port(self, app, server_name):
        with app.test_request_context('/'):
            assert is_safe_redirect_target('http://127.0.0.1:5000/x') is True

    def test_the_same_host_on_another_port_is_accepted(self, app, server_name):
        with app.test_request_context('/'):
            assert is_safe_redirect_target('http://127.0.0.1/x') is True

    def test_another_host_is_still_rejected(self, app, server_name):
        with app.test_request_context('/'):
            assert is_safe_redirect_target('http://evil.example:5000/x') is False


class TestNoServerNameConfigured:
    @pytest.fixture
    def blank_server_name(self, app):
        original = app.config['SERVER_NAME']
        app.config['SERVER_NAME'] = ''
        yield
        app.config['SERVER_NAME'] = original

    def test_relative_urls_still_work(self, app, blank_server_name):
        with app.test_request_context('/'):
            assert is_safe_redirect_target('/foo') is True

    def test_no_absolute_url_can_be_matched(self, app, blank_server_name):
        """With nothing to compare against, nothing absolute is our own site."""
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://test.piefed.local/x') is False
