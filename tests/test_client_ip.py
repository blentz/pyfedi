"""The trusted client-IP source.

`app.get_ip_address` is Flask-Limiter's key function (`app/__init__.py`) and
`app.utils.ip_address` is what IP bans, the honeypot ban, geolocation and the
`user.ip_address` audit column are derived from. Both must resolve the client
address from a source the client cannot write.

The default source is `request.remote_addr`, which `ProxyFix(x_for=1)` has
already resolved from the LAST entry of `X-Forwarded-For` -- the entry appended
by the single trusted reverse proxy PieFed sits behind. `TRUSTED_CLIENT_IP_HEADER`
names a header to read instead, for deployments behind a CDN that publishes the
real client in a header of its own (Cloudflare: `CF-Connecting-IP`).

Every test that mutates config restores it in a `finally`: the `app` fixture is
session-scoped, so a leak corrupts later tests.
"""

import pytest

from app import get_ip_address
from app.utils import ip_address

HEADER_SETTING = 'TRUSTED_CLIENT_IP_HEADER'

# Both names must resolve to the same implementation, so every behavioural test
# below runs against both. If they ever drift apart again, every test doubles up.
BOTH = pytest.mark.parametrize('resolve', [get_ip_address, ip_address],
                               ids=['limiter_key_function', 'app.utils.ip_address'])


@pytest.fixture
def trusted_header(app):
    """Set TRUSTED_CLIENT_IP_HEADER for one test and put the old value back."""
    original = app.config.get(HEADER_SETTING)

    def configure(value):
        app.config[HEADER_SETTING] = value

    yield configure
    app.config[HEADER_SETTING] = original


class TestDefaultIsRemoteAddr:
    """The regression guard for the spoofing vulnerability.

    Before this was fixed both functions read `CF-Connecting-IP` and then
    `X-Forwarded-For` straight off the request and kept the FIRST entry of the
    chain -- the one the client wrote. Any client could therefore pick its own
    rate-limit bucket and step around an IP ban by sending a header.
    """

    @BOTH
    def test_remote_addr_is_used_when_no_header_is_configured(self, app, resolve):
        with app.test_request_context(environ_base={'REMOTE_ADDR': '9.9.9.9'}):
            assert resolve() == '9.9.9.9'

    @BOTH
    def test_a_spoofed_forwarded_for_does_not_change_the_answer(self, app, resolve):
        with app.test_request_context(headers={'X-Forwarded-For': '1.2.3.4'},
                                      environ_base={'REMOTE_ADDR': '9.9.9.9'}):
            assert resolve() == '9.9.9.9'

    @BOTH
    def test_a_spoofed_cloudflare_header_does_not_change_the_answer(self, app, resolve):
        with app.test_request_context(headers={'CF-Connecting-IP': '1.2.3.4'},
                                      environ_base={'REMOTE_ADDR': '9.9.9.9'}):
            assert resolve() == '9.9.9.9'

    @BOTH
    def test_a_header_the_deployment_did_not_configure_is_ignored(self, app, resolve, trusted_header):
        """Configuring one header does not make every header trusted."""
        trusted_header('CF-Connecting-IP')
        with app.test_request_context(headers={'X-Forwarded-For': '1.2.3.4',
                                               'True-Client-IP': '2.3.4.5'},
                                      environ_base={'REMOTE_ADDR': '9.9.9.9'}):
            assert resolve() == '9.9.9.9'


class TestConfiguredHeader:
    @BOTH
    def test_the_configured_header_is_used(self, app, resolve, trusted_header):
        trusted_header('CF-Connecting-IP')
        with app.test_request_context(headers={'CF-Connecting-IP': '1.2.3.4'},
                                      environ_base={'REMOTE_ADDR': '9.9.9.9'}):
            assert resolve() == '1.2.3.4'

    @BOTH
    def test_an_absent_configured_header_falls_back_to_remote_addr(self, app, resolve, trusted_header):
        trusted_header('CF-Connecting-IP')
        with app.test_request_context(environ_base={'REMOTE_ADDR': '9.9.9.9'}):
            assert resolve() == '9.9.9.9'

    @BOTH
    def test_an_empty_configured_header_falls_back_to_remote_addr(self, app, resolve, trusted_header):
        trusted_header('CF-Connecting-IP')
        with app.test_request_context(headers={'CF-Connecting-IP': ''},
                                      environ_base={'REMOTE_ADDR': '9.9.9.9'}):
            assert resolve() == '9.9.9.9'

    @BOTH
    def test_a_comma_separated_header_yields_the_last_entry(self, app, resolve, trusted_header):
        """The LAST entry, not the first.

        Counter-intuitive but deliberate: each hop APPENDS the address it saw,
        so the last entry was written by the proxy closest to PieFed -- the one
        entry a client cannot forge. Everything before it is whatever the client
        sent.
        """
        trusted_header('X-Forwarded-For')
        with app.test_request_context(headers={'X-Forwarded-For': '1.2.3.4, 5.6.7.8, 9.9.9.9'},
                                      environ_base={'REMOTE_ADDR': '10.0.0.1'}):
            assert resolve() == '9.9.9.9'

    @BOTH
    def test_surrounding_whitespace_is_stripped(self, app, resolve, trusted_header):
        trusted_header('CF-Connecting-IP')
        with app.test_request_context(headers={'CF-Connecting-IP': '  1.2.3.4  '},
                                      environ_base={'REMOTE_ADDR': '9.9.9.9'}):
            assert resolve() == '1.2.3.4'

    @BOTH
    def test_a_header_name_is_matched_case_insensitively(self, app, resolve, trusted_header):
        """HTTP header names are case-insensitive; an operator writing
        `cf-connecting-ip` must not silently get the default."""
        trusted_header('cf-connecting-ip')
        with app.test_request_context(headers={'CF-Connecting-IP': '1.2.3.4'},
                                      environ_base={'REMOTE_ADDR': '9.9.9.9'}):
            assert resolve() == '1.2.3.4'


class TestNoAddressAtAll:
    @BOTH
    def test_a_request_without_a_remote_addr_yields_the_empty_string(self, app, resolve):
        """Callers index Redis keys and DB columns with this; None would break
        them in ways an empty string does not."""
        with app.test_request_context(environ_base={'REMOTE_ADDR': None}):
            assert resolve() == ''

    def test_outside_a_request_context_yields_the_empty_string(self, app):
        with app.app_context():
            assert ip_address() == ''


class TestTheTwoFunctionsAgree:
    """`app.utils.ip_address` and the limiter key function must never drift
    apart again: rate limiting and IP bans have to bucket a request identically."""

    def test_they_are_the_same_implementation(self):
        assert ip_address is get_ip_address

    @pytest.mark.parametrize('configured,headers,remote_addr', [
        (None, {}, '9.9.9.9'),
        (None, {'X-Forwarded-For': '1.2.3.4'}, '9.9.9.9'),
        (None, {'CF-Connecting-IP': '1.2.3.4'}, '9.9.9.9'),
        ('CF-Connecting-IP', {'CF-Connecting-IP': '1.2.3.4'}, '9.9.9.9'),
        ('CF-Connecting-IP', {}, '9.9.9.9'),
        ('X-Forwarded-For', {'X-Forwarded-For': '1.2.3.4, 9.9.9.9'}, '10.0.0.1'),
    ])
    def test_same_request_same_answer(self, app, trusted_header, configured, headers, remote_addr):
        if configured is not None:
            trusted_header(configured)
        with app.test_request_context(headers=headers,
                                      environ_base={'REMOTE_ADDR': remote_addr}):
            assert get_ip_address() == ip_address()


class TestThroughTheRealWsgiStack:
    """End to end, through `ProxyFix(x_for=1)` and a real route.

    `app.test_request_context` bypasses WSGI middleware, so the tests above
    cannot show that ProxyFix and the default setting fit together. This one
    drives `/test_ip` through `app.wsgi_app`, which is what a deployed request
    goes through.
    """

    def test_the_proxys_appended_entry_wins_over_the_clients(self, app, site):
        original_debug = app.config['DEBUG']
        try:
            app.config['DEBUG'] = True  # /test_ip is @debug_mode_only
            with app.test_client() as client:
                response = client.get('/test_ip',
                                      headers={'X-Forwarded-For': '1.2.3.4, 203.0.113.7'},
                                      environ_base={'REMOTE_ADDR': '10.0.0.1'})
            assert response.status_code == 200
            # 203.0.113.7 is what the trusted proxy appended; 1.2.3.4 is what the
            # client claimed. ProxyFix(x_for=1) puts the former in remote_addr.
            assert response.text.split(' ')[0] == '203.0.113.7'
        finally:
            app.config['DEBUG'] = original_debug
