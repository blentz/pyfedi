"""The bot-challenge interstitial: `anoobis` in `app/main/routes.py`.

`check_anoobis` sends an anonymous visitor here with `?next=<the path they asked
for>`, and `anoobis.html` puts that value straight into

    location.href = '{{ next }}';

so whatever this route accepts is where an anonymous visitor's browser goes. It had
no tests.

D1359. The guard was a second implementation of the origin check, and a weaker one:

    f = furl(next)
    if next and (f.host is None or f.host == SERVER_NAME) and \\
            (f.scheme is None or f.scheme.startswith('http')):

`f.host is None` accepts everything furl reads as having no authority, and a browser
does not agree with furl about what that means. Measured against furl, each of these
was served -- an open redirect on the one page whose job is to bounce a visitor
onward:

    \\\\evil.test/x      host=None            browsers fold \\ to /, so this is
                                            //evil.test/x -- protocol-relative
    /\\evil.test        host=None            the same, as /\\ -> //
    https:/\\evil.test  host=None, https     -> https://evil.test
    http:evil.test     host=None, http      scheme-relative; Chrome resolves it
                                            as http://evil.test/

`is_safe_redirect_target` is THE origin check: `back()` and all three of
`referrer()`'s sources already go through it, and its docstring names the
back()/referrer() divergence that having two implementations produced. This route
uses it now, which is why this file asserts the four bypasses AND the agreement
between the route and the function -- a third implementation appearing is the thing
to catch.
"""
import pytest
from flask import current_app, g

from app import db
from app.models import Site
from app.utils import is_safe_redirect_target


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    db.session.commit()
    return SimpleNamespace(client=app.test_client(), site=site,
                           host=current_app.config['SERVER_NAME'])


def served(env, next_value):
    return env.client.get('/anoobis', query_string={'next': next_value})


class TestTheFourBypasses:
    """D1359. Each of these is a value furl reads as hostless and a browser reads
    as somebody else's origin."""

    @pytest.mark.parametrize('next_value', [
        '\\\\evil.test/x',
        '/\\evil.test',
        'https:/\\evil.test',
        'http:evil.test',
    ])
    def test_it_is_refused(self, env, next_value):
        assert served(env, next_value).status_code == 403

    @pytest.mark.parametrize('next_value', [
        '\\\\evil.test/x',
        '/\\evil.test',
        'https:/\\evil.test',
        'http:evil.test',
    ])
    def test_the_value_never_reaches_the_page(self, env, next_value):
        """The status is not the whole assertion: what matters is that the string
        does not end up in `location.href`."""
        response = served(env, next_value)

        assert b'evil.test' not in response.data


class TestWhatWasAlreadyRefused:
    @pytest.mark.parametrize('next_value', [
        'https://evil.test/',
        '//evil.test/x',
        '////evil.test',
        'javascript:alert(1)',
        'data:text/html,<script>alert(1)</script>',
        'https://test.piefed.local.evil.test/',
        'https://evil.test/?x=test.piefed.local',
    ])
    def test_it_is_still_refused(self, env, next_value):
        assert served(env, next_value).status_code == 403


class TestWhatMustStillWork:
    def test_the_path_check_anoobis_sends(self, env):
        """`check_anoobis` redirects here with `next=request.path`, so a root
        relative path is the only value the site itself ever produces."""
        response = served(env, '/c/news')

        assert response.status_code == 200
        assert b'/c/news' in response.data

    @pytest.mark.parametrize('next_value', ['/', '/post/1', '/c/news?page=2',
                                            '/search?q=x'])
    def test_a_relative_path(self, env, next_value):
        assert served(env, next_value).status_code == 200

    def test_this_servers_own_absolute_url(self, env):
        assert served(env, f'https://{env.host}/c/news').status_code == 200

    def test_no_next_at_all_is_an_empty_answer(self, env):
        """`if next is None: return ''` -- not a 403, and not a page. Asserted
        because it is the one path that returns neither."""
        response = env.client.get('/anoobis')

        assert response.status_code == 200
        assert response.data == b''

    def test_an_empty_next_is_refused(self, env):
        """`next` is present but falsy, so it reaches the guard rather than the
        early return, and `if next and ...` refuses it."""
        assert served(env, '').status_code == 403


class TestTheRouteAgreesWithTheCanonicalCheck:
    """The property that keeps a third implementation from appearing: for every
    value, the route's answer and `is_safe_redirect_target`'s answer match.
    """

    VALUES = [
        '/', '/c/news', '/post/1?x=1', '/search?q=%23tag', '#frag', '?x=1',
        'relative/path',
        'https://test.piefed.local/x', 'http://test.piefed.local/x',
        'https://evil.test/', '//evil.test/x', '////evil.test',
        '\\\\evil.test/x', '/\\evil.test', 'https:/\\evil.test', 'http:evil.test',
        'javascript:alert(1)', 'data:text/plain,x', 'ftp://evil.test/',
        'https://test.piefed.local@evil.test/', 'https://evil.test\\@test.piefed.local/',
        'http://[', 'http://[zzz]', 'https://test.piefed.local:8443/x',
        'https://test.piefed.local\t/x',
    ]

    @pytest.mark.parametrize('next_value', VALUES)
    def test_the_two_answers_match(self, env, next_value):
        route_allows = served(env, next_value).status_code == 200

        with current_app.test_request_context():
            canonical = is_safe_redirect_target(next_value)

        assert route_allows == canonical, next_value


class TestThePageItself:
    def test_the_difficulties_are_rendered(self, env, monkeypatch):
        """The two config values the challenge needs; a page served without them
        cannot be solved, so the visitor would be stuck at the interstitial."""
        monkeypatch.setitem(current_app.config, 'ANOOBIS_DIFFICULTY_DESKTOP', 17)
        monkeypatch.setitem(current_app.config, 'ANOOBIS_DIFFICULTY_MOBILE', 13)

        response = served(env, '/c/news')

        assert b'17' in response.data and b'13' in response.data
