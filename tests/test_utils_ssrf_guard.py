"""`is_invalid_get_request_uri` in `app/utils.py` -- the SSRF guard.

Every outbound fetch this instance makes on a peer's behalf goes through it:
`get_request`, `url_to_thumbnail_file`, the actor refreshes, the object resolvers.
The URIs it judges come from peer documents and from search boxes, so what it lets
through is what an outsider can make this server connect to. It had no tests of its
own -- other files monkeypatch it or set DEBUG to get past it.

D1358 has two halves, and the first is the one that mattered.

**Every IPv6 literal was allowed.** `furl(...).host` keeps the brackets, so for
`https://[::1]/x` the host is the string `'[::1]'`:
`ipaddress.ip_address('[::1]')` raised ValueError, `getaddrinfo('[::1]', None)`
raised `gaierror [Errno -2] Name or service not known`, and the DNS handler FAILS
OPEN by design. So `https://[::1]/`, `https://[fd00::1]/` and
`https://[::ffff:127.0.0.1]/` all passed the guard, with no DNS control needed to
reach that branch. Measured for all three.

**And `is_global` alone is not "routable on the public internet".** Measured with
Python 3.13.15's own `ipaddress`:

    ::7f00:1             is_global=True   IPv4-compatible IPv6 (RFC 4291), with
                                          127.0.0.1 in the low 32 bits
    ::ffff:0:127.0.0.1   is_global=True   the same address, other spelling
    64:ff9b::7f00:1      is_global=True   NAT64 well-known prefix (RFC 6052)
                                          embedding 127.0.0.1 -- behind a NAT64
                                          gateway that is a route to loopback
    64:ff9b::a00:1       is_global=True   the same, embedding 10.0.0.1
    ff02::1              is_global=True   IPv6 all-nodes multicast
    224.0.0.1            is_global=True   IPv4 all-hosts multicast

`is_reserved` covers the first four and `is_multicast` the last two.

Not fixed, and stated here so nobody reads these tests as a promise: the address is
resolved in this function and resolved AGAIN by whoever performs the request, so a
name that answers differently the second time still gets through. Closing DNS
rebinding means pinning the request to the address checked here, which is a change
to the HTTP client.
"""
import pytest
from flask import current_app

from app.utils import is_invalid_get_request_uri


@pytest.fixture(autouse=True)
def not_debugging(app, monkeypatch):
    """`is_invalid_get_request_uri` returns False immediately under DEBUG, so the
    whole function is unreachable while it is on -- which is why other test files
    set DEBUG to get PAST this guard rather than to exercise it."""
    monkeypatch.setattr(current_app, 'debug', False, raising=False)


@pytest.fixture
def resolves(monkeypatch):
    """Point `getaddrinfo` at chosen addresses, so a hostname's verdict can be
    asserted without depending on real DNS."""
    def install(*addresses):
        def getaddrinfo(host, port, *arguments, **keywords):
            import socket as socket_module

            return [(socket_module.AF_INET, socket_module.SOCK_STREAM, 6, '',
                     (address, 0)) for address in addresses]

        monkeypatch.setattr('app.utils.socket.getaddrinfo', getaddrinfo)
    return install


class TestEveryIpv6LiteralUsedToBeAllowed:
    """The first half of D1358, and the reason it was reachable: none of these
    needs a hostname, a DNS answer or anything the attacker does not already
    control. `https://[::1]/` is the whole exploit."""

    @pytest.mark.parametrize('host', [
        '[::1]',
        '[fd00::1]',
        '[fe80::1]',
        '[::ffff:127.0.0.1]',
        '[::ffff:10.0.0.1]',
        '[::ffff:169.254.169.254]',
        '[::]',
    ])
    def test_a_bracketed_literal_is_read_as_an_address(self, app, host):
        assert is_invalid_get_request_uri(f'https://{host}/actor') is True

    def test_a_bracketed_literal_with_a_port(self, app):
        """A port is how this reaches something interesting -- a database, a
        metrics endpoint, an admin socket."""
        assert is_invalid_get_request_uri('https://[::1]:5432/actor') is True

    def test_a_public_bracketed_literal_still_works(self, app):
        """The brackets are stripped, not rejected."""
        assert is_invalid_get_request_uri(
            'https://[2606:4700:4700::1111]/actor') is False


class TestAddressesThatUsedToGetThrough:
    """The second half: addresses `is_global` calls global that are not."""

    @pytest.mark.parametrize('host', [
        '[::7f00:1]',
        '[::ffff:0:127.0.0.1]',
        '[64:ff9b::7f00:1]',
        '[64:ff9b::a00:1]',
        '[ff02::1]',
        '224.0.0.1',
    ])
    def test_it_is_refused(self, app, host):
        assert is_invalid_get_request_uri(f'https://{host}/actor') is True


class TestAddressesThatWereAlreadyRefused:
    @pytest.mark.parametrize('host', [
        '127.0.0.1',
        '10.0.0.1',
        '192.168.1.1',
        '172.16.0.1',
        '169.254.169.254',  # the cloud metadata service
        '0.0.0.0',
        '[::1]',
        '[fd00::1]',
        '[fe80::1]',
        '[::ffff:127.0.0.1]',
        '[::ffff:169.254.169.254]',
        '100.64.0.1',
    ])
    def test_it_is_still_refused(self, app, host):
        assert is_invalid_get_request_uri(f'https://{host}/actor') is True


class TestAddressesThatMustStillWork:
    """The other direction: a guard that refuses everything is not a guard, it is
    an outage. These six are the ones the repair was checked against."""

    @pytest.mark.parametrize('host', [
        '8.8.8.8',
        '1.1.1.1',
        '93.184.216.34',
        '[2606:4700:4700::1111]',
        '[2001:4860:4860::8888]',
        '[2a00:1450:4001:827::200e]',
    ])
    def test_it_is_allowed(self, app, host):
        assert is_invalid_get_request_uri(f'https://{host}/actor') is False


class TestWhatTheUriItselfDecides:
    def test_a_uri_with_no_host(self, app):
        assert is_invalid_get_request_uri('/relative/path') is True

    def test_a_uri_with_a_scheme_but_no_host(self, app):
        """`http:///path` parses, and its scheme passes the scheme test, so
        `if not f.host` is the only line that refuses it -- the relative path above
        is stopped by the scheme test instead and cannot tell whether the host test
        is doing anything."""
        assert is_invalid_get_request_uri('http:///path') is True
        assert is_invalid_get_request_uri('https:///path') is True

    def test_an_empty_uri(self, app):
        assert is_invalid_get_request_uri('') is True

    @pytest.mark.parametrize('uri', [
        'ftp://example.com/x',
        'file:///etc/passwd',
        'gopher://example.com/x',
        'data:text/plain,hello',
    ])
    def test_a_scheme_that_is_not_http(self, app, uri):
        assert is_invalid_get_request_uri(uri) is True

    def test_a_mdns_host(self, app):
        """`.local` is refused by name rather than by address, because mDNS is not
        resolvable from here and the answer would depend on the network."""
        assert is_invalid_get_request_uri('https://printer.local/actor') is True

    def test_a_mdns_host_with_a_port(self, app):
        assert is_invalid_get_request_uri('https://printer.local:8443/x') is True

    def test_something_that_is_not_a_uri_at_all(self, app):
        """The bare `except Exception: return True` -- the safe direction, since a
        URI this function cannot parse is one it cannot vouch for."""
        assert is_invalid_get_request_uri('http://[') is True

    @pytest.mark.parametrize('uri', [None, 5, [], {}])
    def test_something_that_is_not_a_string(self, app, uri):
        assert is_invalid_get_request_uri(uri) is True


class TestWhenAHostnameHasToBeResolved:
    def test_a_name_that_resolves_to_a_public_address(self, app, resolves):
        resolves('93.184.216.34')

        assert is_invalid_get_request_uri('https://peer.example/actor') is False

    def test_a_name_that_resolves_to_loopback(self, app, resolves):
        resolves('127.0.0.1')

        assert is_invalid_get_request_uri('https://peer.example/actor') is True

    def test_a_name_that_resolves_to_the_metadata_service(self, app, resolves):
        resolves('169.254.169.254')

        assert is_invalid_get_request_uri('https://peer.example/actor') is True

    def test_a_name_that_resolves_to_the_nat64_prefix(self, app, resolves):
        """The bypass, reached the way an attacker would: through a name they
        control rather than as a literal in the document."""
        resolves('64:ff9b::7f00:1')

        assert is_invalid_get_request_uri('https://peer.example/actor') is True

    def test_one_bad_address_among_good_ones_is_enough(self, app, resolves):
        """`any(...)`: a name answering with both a public and a private address
        must be refused, or a round-robin record is a way through."""
        resolves('93.184.216.34', '127.0.0.1')

        assert is_invalid_get_request_uri('https://peer.example/actor') is True

    def test_all_good_addresses_are_allowed(self, app, resolves):
        resolves('93.184.216.34', '8.8.8.8')

        assert is_invalid_get_request_uri('https://peer.example/actor') is False

    def test_a_resolution_failure_fails_open(self, app, monkeypatch):
        """Deliberate, and documented in the function: a flaky nameserver must not
        make a valid peer unreachable. It is the one place this guard chooses
        availability over safety, so it is asserted rather than left implicit."""
        import socket as socket_module

        def failing(*arguments, **keywords):
            raise socket_module.gaierror('temporary failure')

        monkeypatch.setattr('app.utils.socket.getaddrinfo', failing)

        assert is_invalid_get_request_uri('https://peer.example/actor') is False

    def test_a_resolution_timeout_fails_open_too(self, app, monkeypatch):
        import socket as socket_module

        def timing_out(*arguments, **keywords):
            raise socket_module.timeout('timed out')

        monkeypatch.setattr('app.utils.socket.getaddrinfo', timing_out)

        assert is_invalid_get_request_uri('https://peer.example/actor') is False

    def test_a_resolution_error_that_is_not_dns_is_refused(self, app, monkeypatch):
        """Only `gaierror` and `timeout` fail open; anything else falls to the
        outer `except Exception`, which refuses."""
        def exploding(*arguments, **keywords):
            raise RuntimeError('something else')

        monkeypatch.setattr('app.utils.socket.getaddrinfo', exploding)

        assert is_invalid_get_request_uri('https://peer.example/actor') is True


class TestDebugTurnsTheGuardOff:
    def test_nothing_is_refused_under_debug(self, app, monkeypatch):
        """Asserted so the `not_debugging` fixture above is known to be doing
        something: in a development instance this function refuses nothing, which
        is what lets a developer point their instance at localhost."""
        monkeypatch.setattr(current_app, 'debug', True, raising=False)

        assert is_invalid_get_request_uri('https://127.0.0.1/actor') is False
        assert is_invalid_get_request_uri('file:///etc/passwd') is False
