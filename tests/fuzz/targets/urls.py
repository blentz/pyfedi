"""Property checks for URL readers, redirects and request-derived hosts (tier T4, app/utils.py, app/cli.py and the
webfinger resource parse in app/activitypub/routes.py).

Every function here decides something about a URL a peer, a feed or a visitor chose: whether it may be stored as a
link, which host it names, whether a redirect to it stays on this site, whether a fetch of it would reach a private
address, which domain a User-Agent claims, whether a webfinger resource is ours. Each check asserts that decision
against an independent reading of the URL -- whatwg_reading() below, written from the WHATWG URL Standard and
sharing no code with app/ -- rather than "it did not raise", and a raise on a string a request can carry is a
finding.

The security direction is what is asserted. A function that refuses a URL a browser would also accept fails
closed and is not a finding; one that accepts a URL whose host a browser or httpx reads differently from the
host the function judged is.

No database and no network. is_safe_redirect_target's policy read (get_setting) and its instance lookups
(instance_redirect_allowed) are swapped for fuzz-chosen answers; is_invalid_get_request_uri gets a fake
socket.getaddrinfo whose addresses the input chooses, and every other way to the network fails the check; the
webfinger parse runs with User, Community and Feed swapped for stubs that record a lookup and find nothing; the
nodeinfo readers get a fake _nodeinfo_json that serves documents out of the input. Multi-argument inputs use
signatures._fields.
"""

import ipaddress
import json
import re
import socket
import unicodedata
from contextlib import contextmanager
from urllib.parse import parse_qsl, unquote_to_bytes, urlparse, urlsplit

import httpx
from flask import current_app
from furl import furl
from werkzeug.exceptions import HTTPException

import app.activitypub.routes as ap_routes
import app.storefront as storefront
import app.utils as app_utils
from app.cli import strip_tracking
from app.pinned_http import is_refused_address
from app.utils import (MAX_URL_HOST_LENGTH, REDIRECT_POLICY_ALL_REFERRERS, REDIRECT_POLICY_FEDERATED_SERVERS,
                       REDIRECT_POLICY_SAME_ORIGIN, REDIRECT_POLICY_TRUSTED_SERVERS, UNSAFE_SRC_SCHEMES,
                       UNSAFE_URL_SCHEMES, _host_reads_alike, _nodeinfo2_software, _nodeinfo_href_on_host,
                       domain_from_email, domain_name_from_url, guess_mime_type, has_unsafe_url_scheme, inbox_domain,
                       is_bot, is_invalid_get_request_uri, is_safe_redirect_target, is_video_hosting_site,
                       is_video_url, mimetype_from_url, remote_instance_software, requestor_domain,
                       safe_redirect_target, url_host, url_is_parseable, url_is_storable, url_scheme)
from tests.fuzz.targets._target import Target
from tests.fuzz.targets.ap_json import rfc3986_authority
from tests.fuzz.targets.signatures import _fields, _json_or_text, _network_forbidden, _text

# The largest input a check reads. A URL past this is refused long before any of these functions by every caller.
MAX_INPUT = 8 * 1024


# ---------------------------------------------------------------------------
# An independent reading of a URL, after the WHATWG URL Standard
# ---------------------------------------------------------------------------

# Leading and trailing C0 controls and space are stripped, and every tab, LF and CR removed, before anything is read.
_C0_OR_SPACE = ''.join(chr(c) for c in range(0x21))
_TAB_OR_NEWLINE = str.maketrans('', '', '\t\n\r')
_SCHEME_START = re.compile(r'([A-Za-z][A-Za-z0-9+.\-]*):')
# The special schemes whose authority a browser reads with a backslash as a slash and every leading slash skipped.
# 'file' is special too, but its host is nothing a ban or an origin check is about.
SPECIAL_SCHEMES = frozenset({'http', 'https', 'ws', 'wss', 'ftp'})
# Forbidden domain code points: a host holding one after percent-decoding is a URL a browser refuses outright.
_FORBIDDEN_DOMAIN = frozenset(''.join(chr(c) for c in range(0x20)) + ' #%/:<>?@[\\]^|\x7f')
# Ideographic and fullwidth full stops, which UTS46 maps to '.'.
_FULL_STOPS = str.maketrans({'。': '.', '．': '.', '｡': '.'})

INVALID = object()   # the browser refuses the URL: nothing is navigated to or fetched


def _ipv4_number(part: str):
    """One WHATWG IPv4 part as an int, or None when it is not a number."""
    radix = 10
    if part[:2] in ('0x', '0X'):
        part, radix = part[2:], 16
    elif len(part) > 1 and part[0] == '0':
        part, radix = part[1:], 8
    if part == '':
        return 0
    digits = {10: '0123456789', 16: '0123456789abcdefABCDEF', 8: '01234567'}[radix]
    if not set(part) <= set(digits):
        return None
    return int(part, radix)


def _ends_in_a_number(host: str) -> bool:
    parts = host.split('.')
    if parts[-1] == '' and len(parts) > 1:
        parts.pop()
    last = parts[-1]
    if last and set(last) <= set('0123456789'):
        return True
    return last[:2] in ('0x', '0X') and set(last[2:]) <= set('0123456789abcdefABCDEF')


def _ipv4(host: str):
    """The WHATWG IPv4 parser: the dotted-decimal address a host that ends in a number names, or INVALID."""
    parts = host.split('.')
    if parts[-1] == '' and len(parts) > 1:
        parts.pop()
    if len(parts) > 4 or any(part == '' for part in parts):
        return INVALID
    numbers = [_ipv4_number(part) for part in parts]
    if any(number is None for number in numbers) or any(number > 255 for number in numbers[:-1]):
        return INVALID
    if numbers[-1] >= 256 ** (5 - len(numbers)):
        return INVALID
    value = numbers[-1]
    for index, number in enumerate(numbers[:-1]):
        value += number * 256 ** (3 - index)
    return '.'.join(str((value >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def _ipv6_serialised(literal: str):
    """The WHATWG serialisation of an IPv6 literal (lower-case hex, the longest run of two or more zero pieces
    compressed, no embedded dotted quad), or INVALID. The address is read by ipaddress; the serialisation is written
    out here, because str(IPv6Address) prints an IPv4-mapped address with a dotted quad and a browser does not."""
    if '%' in literal:
        return INVALID
    try:
        number = int(ipaddress.IPv6Address(literal))
    except ValueError:
        return INVALID
    pieces = [(number >> (16 * (7 - index))) & 0xFFFF for index in range(8)]
    best_start, best_length, start = -1, 1, None
    for index in range(9):
        if index < 8 and pieces[index] == 0:
            start = index if start is None else start
        elif start is not None:
            if index - start > best_length:
                best_start, best_length = start, index - start
            start = None
    if best_start < 0:
        return ':'.join(f'{piece:x}' for piece in pieces)
    head = ':'.join(f'{piece:x}' for piece in pieces[:best_start])
    tail = ':'.join(f'{piece:x}' for piece in pieces[best_start + best_length:])
    return f'{head}::{tail}'


def _domain(raw: str):
    """A special URL's host as a browser serialises it before IDNA: percent-decoded, ASCII lower-cased, an IPv4
    address when it ends in a number; non-ASCII text is folded by NFKC and casefold as an approximation of UTS46.
    INVALID when the browser refuses it."""
    try:
        decoded = unquote_to_bytes(raw).decode('utf-8')
    except UnicodeDecodeError:
        return INVALID
    if not decoded.isascii():
        decoded = unicodedata.normalize('NFKC', decoded.translate(_FULL_STOPS)).casefold()
    decoded = decoded.lower()
    if not decoded or set(decoded) & _FORBIDDEN_DOMAIN:
        return INVALID
    if _ends_in_a_number(decoded):
        return _ipv4(decoded)
    return decoded


def whatwg_reading(url: str):
    """(scheme, host) a browser reads from `url`, by the WHATWG URL Standard, independently of app/ and urllib.

    scheme is lower-case, or None for a reference with no scheme. host is None when the URL has no authority,
    INVALID when the browser refuses the URL, else the host as serialised: an IPv6 literal without its brackets in
    canonical form, an IPv4 address dotted-decimal, a domain lower-cased (non-ASCII approximated, see _domain).
    A non-special scheme's host is returned as written, lower-cased."""
    text = url.strip(_C0_OR_SPACE).translate(_TAB_OR_NEWLINE)
    match = _SCHEME_START.match(text)
    if match is None:
        return None, None
    scheme, rest = match.group(1).lower(), text[match.end():]
    if scheme in SPECIAL_SCHEMES:
        authority = re.split(r'[/\\?#]', rest.lstrip('/\\'), maxsplit=1)[0]
    elif rest.startswith('//'):
        authority = re.split(r'[/?#]', rest[2:], maxsplit=1)[0]
    else:
        return scheme, None
    hostport = authority.rpartition('@')[2]
    if hostport.startswith('['):
        literal, closed, after = hostport[1:].partition(']')
        if not closed or (after and not after.startswith(':')):
            return scheme, INVALID
        return scheme, _ipv6_serialised(literal)
    host, _, port = hostport.partition(':')
    if port and not port.isdigit():
        return scheme, INVALID
    if scheme not in SPECIAL_SCHEMES:
        return scheme, host.lower()
    return scheme, _domain(host)


def whatwg_host(url: str):
    """whatwg_reading's host for a special scheme, else None. INVALID passes through."""
    scheme, host = whatwg_reading(url)
    return host if scheme in SPECIAL_SCHEMES else None


def _ip_text(host: str) -> str:
    """A host as compared with a WHATWG reading: an IPv6 literal (urllib drops its brackets) in canonical form."""
    if ':' in host:
        serialised = _ipv6_serialised(host)
        return host if serialised is INVALID else serialised
    return host


# ---------------------------------------------------------------------------
# url_scheme, has_unsafe_url_scheme, url_is_storable, url_host, url_is_parseable
# ---------------------------------------------------------------------------

_UNSAFE = frozenset({'javascript', 'vbscript', 'livescript', 'mocha', 'data'})


def check_url_predicates(data: bytes) -> None:
    """url_scheme reads the scheme a browser reads; has_unsafe_url_scheme is that scheme's membership in the set
    it is given; url_is_parseable is True exactly when urlparse reads the URL and its lower-cased form; url_host is
    None or a str and never raises; url_is_storable never accepts an unsafe scheme, an unparseable URL or a host
    longer than MAX_URL_HOST_LENGTH, and for a special scheme only accepts a URL whose host a browser reads
    exactly as domain_from_url's urlparse(url.lower()).hostname does -- the host a domain ban is checked against --
    and whose Domain name (domain_name_from_url) is the one the browser's host is filed under."""
    value = _json_or_text(data[:MAX_INPUT])
    storable = url_is_storable(value)
    parseable = url_is_parseable(value)
    assert isinstance(storable, bool) and isinstance(parseable, bool)
    if not isinstance(value, str):
        assert storable is False and parseable is False, f'a {type(value).__name__} was accepted'
        return
    url = value

    scheme = url_scheme(url)
    expected_scheme, browser_host = whatwg_reading(url)
    assert scheme == (expected_scheme or ''), f'url_scheme({url!r}) = {scheme!r}, a browser reads {expected_scheme!r}'
    assert has_unsafe_url_scheme(url) is (scheme in _UNSAFE)
    assert has_unsafe_url_scheme(url, UNSAFE_SRC_SCHEMES) is (scheme in _UNSAFE - {'data'})
    assert UNSAFE_URL_SCHEMES == _UNSAFE

    try:
        urlparse(url)
        urlparse(url.lower())
        expected_parseable = True
    except ValueError:
        expected_parseable = False
    assert parseable is expected_parseable, f'url_is_parseable({url!r}) = {parseable}'

    host = url_host(url)
    assert host is None or isinstance(host, str)

    if not storable:
        return
    assert parseable, f'{url!r} is storable but not parseable'
    assert scheme not in _UNSAFE, f'{url!r} is storable with the unsafe scheme {scheme!r}'
    ban_host = urlparse(url.lower()).hostname
    assert ban_host is None or len(ban_host) <= MAX_URL_HOST_LENGTH, f'{url!r} is storable with a {len(ban_host)} host'
    assert _host_reads_alike(url)
    if scheme in SPECIAL_SCHEMES and browser_host is not INVALID:
        assert ban_host, f'{url!r} is storable with no host'
        assert browser_host == _ip_text(ban_host), (
            f'{url!r} is storable: a domain ban reads {ban_host!r}, a browser visits {browser_host!r}')
        # The Domain row a ban is on depends only on the host a browser visits: every spelling of one IPv6 address
        # ([0:0::1], [::1]) is filed under the same name.
        visited = f'[{browser_host}]' if ':' in browser_host else browser_host
        assert domain_name_from_url(url) == domain_name_from_url(f'{scheme}://{visited}/'), (
            f'{url!r} is filed under {domain_name_from_url(url)!r}, its browser host {browser_host!r} elsewhere')


# ---------------------------------------------------------------------------
# Small host helpers: inbox_domain, domain_from_email, is_bot and the two discovery _host readers
# ---------------------------------------------------------------------------

def _one_spelling(host: str) -> str:
    """An instance ban's spelling of `host`: an IPv6 literal, bracketed or not, as ipaddress compresses it and
    without brackets (owner ruling 2026-10-13); anything else as it is."""
    literal = host[1:-1] if host.startswith('[') and host.endswith(']') else host
    try:
        return ipaddress.IPv6Address(literal).compressed
    except ValueError:
        return host


def check_host_helpers(data: bytes) -> None:
    """inbox_domain gives a lower-case str with no trailing dot and never raises; for a well-formed http(s) URL it
    is the RFC 3986 host, and for a bare value the value, an IPv6 literal in either case in its one spelling. domain_from_email gives '' or the text after the last '@'. is_bot is a bool, True exactly
    when 'bot' or 'meta-externalagent' appears in any case. The two discovery `_host` readers never raise and give
    the lower-case urlsplit host or ''."""
    from app.discovery.instance_answers import _host as answers_host
    from app.interop.workarounds import _host as workarounds_host

    text = _text(data[:MAX_INPUT])

    domain = inbox_domain(text)
    assert isinstance(domain, str), f'inbox_domain gave {domain!r}'
    assert domain == domain.lower(), f'inbox_domain({text!r}) = {domain!r} is not lower-case'
    assert not domain.endswith('.'), f'inbox_domain({text!r}) = {domain!r} kept a trailing dot'
    if text.lower().startswith(('http://', 'https://')):
        reading = rfc3986_authority(text.lower())
        if reading is not None:
            assert domain == _one_spelling(reading[0].rstrip('.')), (
                f'inbox_domain({text!r}) = {domain!r}, RFC 3986 reads {reading[0]!r}')
    elif 'http://' not in text.lower() and 'https://' not in text.lower():
        assert domain == _one_spelling(text.lower().rstrip('.')), f'inbox_domain({text!r}) = {domain!r}'

    email_domain = domain_from_email(text)
    if not text.strip() or '@' not in text:
        assert email_domain == ''
    else:
        assert email_domain == text.rpartition('@')[2], f'domain_from_email({text!r}) = {email_domain!r}'

    lowered = text.lower()
    assert is_bot(text) is ('bot' in lowered or 'meta-externalagent' in lowered)

    for reader in (answers_host, workarounds_host):
        host = reader(text)
        assert isinstance(host, str) and host == host.lower() and '@' not in host, f'{reader.__module__}._host {host!r}'
        reading = rfc3986_authority(text)
        if reading is not None and host:
            assert host == reading[0], f'{reader.__module__}._host({text!r}) = {host!r}, RFC 3986 reads {reading[0]!r}'
    assert workarounds_host(data) == ''


# ---------------------------------------------------------------------------
# MIME types and video URLs
# ---------------------------------------------------------------------------

_VIDEO_EXTENSIONS = ('.mp4', '.webm', '.mov')
_VIDEO_SITES = ('https://youtube.com', 'https://www.youtube.com', 'https://youtu.be', 'https://www.vimeo.com',
                'https://vimeo.com', 'https://streamable.com', 'https://www.redgifs.com/watch/')


def check_media_urls(data: bytes) -> None:
    """guess_mime_type gives a type/subtype str for any path; mimetype_from_url gives None or such a str and never
    raises; is_video_url is True only for a URL with no unsafe scheme whose path ends in a video extension;
    is_video_hosting_site is True only for one of its https prefixes or an http(s) URL holding 'videos/watch'."""
    text = _text(data[:MAX_INPUT])

    guessed = guess_mime_type(text)
    assert isinstance(guessed, str) and '/' in guessed, f'guess_mime_type({text!r}) = {guessed!r}'

    mime = mimetype_from_url(text)
    assert mime is None or (isinstance(mime, str) and '/' in mime), f'mimetype_from_url({text!r}) = {mime!r}'
    assert mimetype_from_url('') is None and mimetype_from_url(None) is None

    video = is_video_url(text)
    assert isinstance(video, bool)
    if video:
        assert whatwg_reading(text)[0] not in _UNSAFE, f'is_video_url accepted {text!r}'
        assert urlparse(text).path.lower().endswith(_VIDEO_EXTENSIONS), f'is_video_url accepted {text!r}'

    hosting = is_video_hosting_site(text)
    assert isinstance(hosting, bool)
    if hosting:
        assert text.startswith(_VIDEO_SITES) or (
            text.lower().startswith(('http://', 'https://')) and 'videos/watch' in text), (
            f'is_video_hosting_site accepted {text!r}')


# ---------------------------------------------------------------------------
# Redirect targets
# ---------------------------------------------------------------------------

_POLICIES = (REDIRECT_POLICY_SAME_ORIGIN, REDIRECT_POLICY_TRUSTED_SERVERS, REDIRECT_POLICY_FEDERATED_SERVERS,
             REDIRECT_POLICY_ALL_REFERRERS, 'not-a-policy', None)


@contextmanager
def _redirect_policy(policy, allowed: bool):
    """get_setting answers `policy` for the redirect setting; instance_redirect_allowed answers `allowed` and
    records each (host, trusted_only) it is asked about. Neither touches the database."""
    asked = []

    def setting(name, default=None):
        assert name == app_utils.REDIRECT_POLICY_SETTING, f'get_setting({name!r}) read by the redirect check'
        return default if policy is None else policy

    def instance_allowed(host, trusted_only):
        asked.append((host, trusted_only))
        # An instance's domain is a plain DNS name it federates from: none ends in a number (a browser reads that
        # as an IPv4 address) or holds a '%' or other character no hostname holds, so the stub allows none either.
        return allowed and not _ends_in_a_number(host) and host.isascii() and all(
            char.isalnum() or char in '-._' for char in host)

    saved = app_utils.get_setting, app_utils.instance_redirect_allowed
    app_utils.get_setting, app_utils.instance_redirect_allowed = setting, instance_allowed
    try:
        yield asked
    finally:
        app_utils.get_setting, app_utils.instance_redirect_allowed = saved


def check_redirect_targets(data: bytes) -> None:
    """is_safe_redirect_target and safe_redirect_target, under each redirect policy.

    Accepted is always a bool. Never accepted, under any policy: a URL with a control character, a scheme a browser
    reads as other than http(s), or a reference a browser reads as having an authority but no scheme (//host,
    /\\host, ///host). An accepted relative reference has no scheme and no authority. An accepted absolute URL's
    browser host is this server's under same_origin (and an unrecognised policy); under trusted/federated it is
    this server's or one instance_redirect_allowed was asked about, with the right trusted flag, and allowed.
    safe_redirect_target gives the candidate exactly when it is accepted, else the default. The domain registry is
    stubbed to its no-rows reading, so SERVER_NAME is the one host served."""
    head, rest = data[:1], data[1:MAX_INPUT]
    selector = head[0] if head else 0
    policy, allowed = _POLICIES[selector % len(_POLICIES)], bool(selector & 0x80)
    url = _json_or_text(rest)
    server = current_app.config['SERVER_NAME'].lower()

    with _redirect_policy(policy, allowed) as asked, _no_domain_rows():
        accepted = is_safe_redirect_target(url)
        assert safe_redirect_target(url, '/fallback') == (url if accepted else '/fallback')
    assert isinstance(accepted, bool)
    if not isinstance(url, str):
        assert accepted is False, f'a {type(url).__name__} was accepted'
        return
    if not accepted:
        return

    # Python's str.strip() is wider than WHATWG's, and the check strips first; what it then holds is what decides.
    candidate = url.strip().translate(_TAB_OR_NEWLINE)
    assert not any(ord(char) < 0x20 or ord(char) == 0x7F for char in candidate), f'control character in {url!r}'
    scheme, host = whatwg_reading(candidate.replace('\\', '/'))
    if scheme is None:
        assert not candidate.replace('\\', '/').startswith('//'), f'{url!r} has an authority and was accepted'
        return
    assert scheme in ('http', 'https'), f'{url!r} has the scheme {scheme!r} and was accepted'
    if host is INVALID:
        return   # a browser refuses it: no navigation happens
    assert host, f'{url!r} has no host and was accepted'
    own = host.rstrip('.') == server.partition(':')[0]   # is_local_host ignores a trailing dot
    if policy == REDIRECT_POLICY_ALL_REFERRERS:
        return
    if policy in (REDIRECT_POLICY_TRUSTED_SERVERS, REDIRECT_POLICY_FEDERATED_SERVERS) and not own:
        trusted = policy == REDIRECT_POLICY_TRUSTED_SERVERS
        assert allowed and asked and all(flag is trusted for _host, flag in asked), (
            f'{url!r} accepted under {policy} without an allowed instance')
        assert host in {_ip_text(asked_host) for asked_host, _flag in asked}, (
            f'{url!r} accepted under {policy}: instance {asked!r} asked, a browser visits {host!r}')
        return
    assert own, f'{url!r} accepted under {policy!r}: a browser visits {host!r}, not {server!r}'


# ---------------------------------------------------------------------------
# is_invalid_get_request_uri and is_refused_address
# ---------------------------------------------------------------------------

# Address blocks that are not publicly routable, from the IANA special-purpose registries, written out here rather
# than read from ipaddress's own is_global: a differential against the predicate under test would assert nothing.
_NOT_PUBLIC_V4 = [ipaddress.IPv4Network(block) for block in (
    '0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16', '172.16.0.0/12', '192.0.0.0/24',
    '192.0.2.0/24', '192.88.99.0/24', '192.168.0.0/16', '198.18.0.0/15', '198.51.100.0/24', '203.0.113.0/24',
    '224.0.0.0/4', '240.0.0.0/4')]
# The registry's "globally reachable" exceptions inside those blocks.
_PUBLIC_V4 = [ipaddress.IPv4Network(block) for block in ('192.0.0.9/32', '192.0.0.10/32')]
# IPv6: only 2000::/3 is global unicast, and an IPv4-mapped address is as public as the IPv4 address it maps.
_GLOBAL_UNICAST = ipaddress.IPv6Network('2000::/3')
_MAPPED = ipaddress.IPv6Network('::ffff:0:0/96')
_NOT_PUBLIC_V6 = [ipaddress.IPv6Network(block) for block in (
    '2001::/23', '2001:db8::/32', '2002::/16', '3fff::/20')]
_PUBLIC_V6 = [ipaddress.IPv6Network(block) for block in (
    '2001:1::1/128', '2001:1::2/128', '2001:1::3/128', '2001:3::/32', '2001:4:112::/48', '2001:20::/28',
    '2001:30::/28')]


def is_public(address) -> bool:
    if address.version == 4:
        return (any(address in block for block in _PUBLIC_V4)
                or not any(address in block for block in _NOT_PUBLIC_V4))
    if address in _MAPPED:
        return is_public(address.ipv4_mapped)
    if any(address in block for block in _PUBLIC_V6):
        return True
    return address in _GLOBAL_UNICAST and not any(address in block for block in _NOT_PUBLIC_V6)


def _addresses(data: bytes) -> list:
    """Fuzz-chosen addresses: a tag byte (even: IPv4, odd: IPv6) then 4 or 16 bytes, repeated, at most 8."""
    addresses = []
    while data and len(addresses) < 8:
        size = 16 if data[0] & 1 else 4
        chunk, data = data[1:1 + size], data[1 + size:]
        if len(chunk) < size:
            break
        addresses.append(ipaddress.ip_address(chunk))
    return addresses


def _ascii_name(host: str) -> str:
    host = host.lower().rstrip('.')
    try:
        return host.encode('idna').decode('ascii')
    except UnicodeError:
        return host


def check_invalid_get_uri(data: bytes) -> None:
    """is_invalid_get_request_uri, outside debug, with getaddrinfo answering fuzz-chosen addresses.

    A False ("go ahead and fetch") means: the URL httpx would fetch is http(s) on a host that is neither empty nor
    under .local; that host is the one checked -- the literal itself, or the name handed to getaddrinfo -- and
    every address checked is public by an independent reading of the IANA registries. The one documented
    exception is a resolution failure, which fails open. is_refused_address refuses every non-public address."""
    uri_bytes, resolver = _fields(data[:MAX_INPUT], 2)
    uri = _text(uri_bytes)
    fail_resolution = resolver[:1] == b'\xff'
    addresses = _addresses(resolver[1:])
    asked = []

    def getaddrinfo(host, port, *args, **kwargs):
        asked.append(host)
        if fail_resolution:
            raise socket.gaierror(socket.EAI_NONAME, 'fuzz: no such name')
        return [(socket.AF_INET6 if address.version == 6 else socket.AF_INET, socket.SOCK_STREAM, 6, '',
                 (str(address), 0, 0, 0) if address.version == 6 else (str(address), 0)) for address in addresses]

    for address in addresses:
        if not is_public(address):
            assert is_refused_address(address), f'is_refused_address let {address} through'

    with _network_forbidden() as attempts:
        socket.getaddrinfo = getaddrinfo
        assert current_app.debug is False
        invalid = is_invalid_get_request_uri.uncached(uri)
    assert attempts == [], f'is_invalid_get_request_uri reached the network: {attempts!r}'
    assert isinstance(invalid, bool)
    if invalid:
        return
    if fail_resolution and asked:
        return   # documented: a DNS failure fails open, and the pinned transport refuses the connection instead
    try:
        target = httpx.URL(uri)
        fetched_host = target.host
    except (httpx.InvalidURL, ValueError, TypeError):
        return   # httpx cannot fetch it at all
    assert target.scheme in ('http', 'https'), f'{uri!r} allowed with scheme {target.scheme!r}'
    assert fetched_host and not fetched_host.lower().rstrip('.').endswith('.local'), f'{uri!r} allowed'
    try:
        literal = ipaddress.ip_address(fetched_host)
    except ValueError:
        literal = None
    if literal is not None:
        assert not asked, f'{uri!r}: the literal {fetched_host} was resolved as a name'
        assert is_public(literal), f'{uri!r} allowed: the literal {literal} is not public'
        return
    assert asked, f'{uri!r} allowed without resolving {fetched_host!r}'
    assert {_ascii_name(host) for host in asked} == {_ascii_name(fetched_host)}, (
        f'{uri!r}: {asked!r} resolved, httpx fetches {fetched_host!r}')
    for address in addresses:
        assert is_public(address), f'{uri!r} allowed: {fetched_host!r} resolves to {address}'


# ---------------------------------------------------------------------------
# requestor_domain and the webfinger resource parse
# ---------------------------------------------------------------------------

def _header_text(data: bytes) -> str:
    """What a WSGI header value can be: latin-1 text with no CR, LF or NUL."""
    return data.decode('latin-1').translate(str.maketrans('', '', '\r\n\x00'))


def check_requestor_domain(data: bytes) -> None:
    """requestor_domain, with the User-Agent taken from the input: never raises, gives a str or None, and a non-empty
    answer is the host of the text after the User-Agent's last '+' with ')' removed -- for a well-formed URL, its
    RFC 3986 host."""
    user_agent = _header_text(data[:MAX_INPUT])
    with current_app.test_request_context('/.well-known/webfinger', headers={'User-Agent': user_agent}):
        domain = requestor_domain()
    assert domain is None or isinstance(domain, str), f'requestor_domain gave {domain!r}'
    if not domain:
        return
    assert '+' in user_agent
    claimed = user_agent.split('+')[-1].replace(')', '')
    # Not a hostname check: the claim is the visitor's own, and a host furl reads with a space in it names no
    # instance, so it can neither be banned nor allowed.
    assert not set(domain) & set('@/?#'), f'requestor_domain({user_agent!r}) = {domain!r}'
    reading = rfc3986_authority(claimed)
    if reading is not None and reading[0]:
        assert domain.lower() == reading[0], f'requestor_domain({user_agent!r}) = {domain!r}, RFC 3986 reads {reading[0]!r}'


class _Lookups:
    """Stands in for User, Community and Feed in app.activitypub.routes: records a query and finds nothing."""

    user_name = 'user_name'

    def __init__(self, record):
        self._record = record

    @property
    def query(self):
        return self

    def filter(self, *args, **kwargs):
        return self

    filter_by = filter

    def first(self):
        self._record.append('lookup')
        return None


@contextmanager
def _no_actor_lookups():
    record = []
    stub = _Lookups(record)
    saved = ap_routes.User, ap_routes.Community, ap_routes.Feed
    ap_routes.User = ap_routes.Community = ap_routes.Feed = stub
    try:
        yield record
    finally:
        ap_routes.User, ap_routes.Community, ap_routes.Feed = saved


@contextmanager
def _no_domain_rows():
    """The domain registry with no rows, which serves SERVER_NAME alone, without the database read behind it."""
    saved = storefront.domain_rows
    storefront.domain_rows = lambda: (storefront._synthesised_default(),)
    try:
        yield
    finally:
        storefront.domain_rows = saved


def check_webfinger_resource(data: bytes) -> None:
    """process_webfinger_request's resource parse and domain check, with the actor lookups and domain rows stubbed.

    Only an HTTPException with a 4xx code or a response escapes -- never another exception, which is a 500. A
    response is the instance actor's, and only for a resource naming it on this server. A lookup happens only for
    a resource whose domain is this server: for acct:, the text after the first '@'; for a well-formed http(s) URL,
    its RFC 3986 host."""
    resource = _text(data[:MAX_INPUT])
    server = current_app.config['SERVER_NAME'].lower()
    with _no_actor_lookups() as lookups, _no_domain_rows(), \
            current_app.test_request_context('/.well-known/webfinger'):
        try:
            response = ap_routes.process_webfinger_request.uncached(resource)
        except HTTPException as refusal:
            assert 400 <= refusal.code < 500, f'{resource!r} gave {refusal.code}'
            response = None

    if resource.startswith('acct:'):
        domain = resource.split(':')[1].partition('@')[2].lower()
    elif 'https:' in resource or 'http:' in resource:
        reading = rfc3986_authority(resource)
        # None also when RFC 3986 reads no authority at all ('acc,http://x@host'): a malformed resource may be
        # looked up by its last path segment, which names no local actor, and gets the same 404.
        domain = None if reading is None or not reading[0] else reading[0]
    else:
        assert response is None and not lookups, f'{resource!r} is neither acct: nor a URL and was answered'
        return
    if lookups:
        assert domain in (None, server), f'{resource!r} names {domain!r} and was looked up'
    if response is not None:
        assert response.status_code == 200 and not lookups
        assert domain in (None, server), f'{resource!r} names {domain!r} and was answered'
        assert response.get_json()['subject'] == f'acct:{server}@{server}'


# ---------------------------------------------------------------------------
# cli.strip_tracking
# ---------------------------------------------------------------------------

_TRACKING = ('utm_', 'at_', 'traffic_source')


def check_strip_tracking(data: bytes) -> None:
    """strip_tracking, which every RSS-imported item's link goes through: never raises, always gives a str, and the
    query of what it gives has no parameter named with a tracking prefix -- except for a link furl cannot read,
    which has no query furl can strip and comes back unchanged."""
    text = _text(data[:MAX_INPUT])
    stripped = strip_tracking(text)
    assert isinstance(stripped, str), f'strip_tracking gave {stripped!r}'
    assert strip_tracking(None) == '' and strip_tracking('') == ''
    try:
        furl(text)
    except ValueError:
        assert stripped == text, f'strip_tracking changed {text!r}, which furl cannot read'
        return
    try:
        query = urlsplit(stripped).query
    except ValueError:
        return
    for key, _value in parse_qsl(query, keep_blank_values=True):
        assert not key.startswith(_TRACKING), f'strip_tracking({text!r}) kept {key!r}'


# ---------------------------------------------------------------------------
# Remote nodeinfo
# ---------------------------------------------------------------------------

def _fetch_host(url: str):
    """The host httpx connects to for `url`, or None when it cannot fetch it (no host, or a URL it refuses)."""
    try:
        target = httpx.URL(url)
        host = target.raw_host.decode('ascii').lower().rstrip('.')
    except (httpx.InvalidURL, ValueError, TypeError):
        return None
    return host if host and target.scheme in ('http', 'https') else None


def check_nodeinfo_hrefs(data: bytes) -> None:
    """remote_instance_software and _nodeinfo2_software, with _nodeinfo_json serving the input's three documents.

    Every URL read is one of the two well-known paths under remote_url or an https href whose host -- as httpx, the
    client that fetches it, reads it -- is the host this server already asked for /.well-known/nodeinfo. What it
    returns is a non-empty lower-case str; anything else it raises (both callers catch Exception).
    _nodeinfo_href_on_host never accepts an href that is not https or names another host."""
    remote_bytes, first, second, third = _fields(data[:MAX_INPUT], 4)
    remote_url = _text(remote_bytes)
    documents = {'nodeinfo': first, 'nodeinfo2': third, 'other': second}
    fetched = []
    well_known = f'{remote_url}/.well-known/nodeinfo'
    well_known2 = f'{remote_url}/.well-known/x-nodeinfo2'

    def nodeinfo_json(uri, deadline):
        fetched.append(uri)
        if _fetch_host(uri) is None:   # httpx refuses a URL it cannot read before any connection
            raise httpx.UnsupportedProtocol(f'fuzz: httpx cannot fetch {uri!r}')
        body = documents['nodeinfo' if uri == well_known else 'nodeinfo2' if uri == well_known2 else 'other']
        return json.loads(body)   # ValueError for a body that is not JSON, as the real one

    saved = app_utils._nodeinfo_json
    app_utils._nodeinfo_json = nodeinfo_json
    try:
        with _network_forbidden() as attempts:
            try:
                software = remote_instance_software(remote_url, timeout=5)
            except Exception:
                software = None
            try:
                software2 = _nodeinfo2_software(remote_url)
            except Exception:
                software2 = None
    finally:
        app_utils._nodeinfo_json = saved
    assert attempts == []

    own_host = _fetch_host(well_known)
    for uri in fetched:
        if uri in (well_known, well_known2):
            continue
        assert uri.lower().startswith('https://'), f'{uri!r} read for {remote_url!r}'
        href_host = _fetch_host(uri)
        if href_host is None:
            continue   # httpx cannot fetch it
        assert own_host and href_host == own_host, f'{uri!r} (httpx host {href_host!r}) read for {remote_url!r}'
    for name in (software, software2):
        assert name is None or (isinstance(name, str) and name and name == name.lower()), f'software {name!r}'

    for document in (_json_or_text(first), _json_or_text(second)):
        links = document.get('links') if isinstance(document, dict) else None
        for link in links if isinstance(links, list) else [document]:
            href = link.get('href') if isinstance(link, dict) else link
            if _nodeinfo_href_on_host(href, remote_url):
                assert href.lower().startswith('https://')
                assert url_host(href) and (url_host(href) or '').lower().rstrip('.') == (
                    url_host(remote_url) or '').lower().rstrip('.')
                href_host = _fetch_host(href)
                if href_host is not None and own_host is not None:   # both fetchable: one host
                    assert href_host == own_host, f'{href!r} accepted for {remote_url!r}'


_IGNORED_BY_UTS46 = ('\u00ad', '\u034f', '\u180b', '\u200b', '\u2060', '\u3164', '\ufe00', '\ufe0f', '\U000e0100')
_CSS_PREFIXES = ('', "'x' ", '"url(" ', '/* url( \' */ ', '\u0130' * 3, "url('a') ", "url(b'c) ", 'URL( d ) ')


def check_browser_host(data: bytes) -> None:
    """storefront.browser_host reads the host a browser loads a URL from, as whatwg_reading does.

    The oracle is compared on PURE ASCII hosts only: an input that is ASCII even after percent-decoding, whose host
    is an ASCII domain, an IPv4 address (an IPv4-mapped IPv6 literal is that address) or an IPv6 literal. whatwg_reading
    approximates UTS-46 for anything else (NFKC and casefold, where a browser maps non-transitionally: it keeps
    the sharp s), so a non-ASCII host is covered by the metamorphic checks instead: spelling an ASCII domain with a
    character UTS-46 deletes, or with its fullwidth forms, does not change the host. For a URL the browser refuses,
    browser_host may still name a host (hiding too much is safe) but never raises.
    The S3 media scanners finish inside the target's budget on any input; _srcset_urls returns slices of it, and
    _css_urls finds the URL of a url(...) behind any prefix of balanced quotes, comments and earlier tokens."""
    url = _text(data[:MAX_INPUT])
    for candidate in (url, 'https:' + url, '//' + url):
        host = storefront.browser_host(candidate)
        assert isinstance(host, str), f'browser_host({candidate!r}) = {host!r}'
        scheme, expected = whatwg_reading(candidate if not candidate.startswith('//') else 'https:' + candidate)
        if scheme not in SPECIAL_SCHEMES or expected is INVALID or expected is None:
            continue
        if host == storefront.UNMAPPABLE or not unquote_to_bytes(candidate.encode('utf-8', 'surrogatepass')).isascii():
            continue
        if ':' in expected:
            address = ipaddress.ip_address(expected)
            address = getattr(address, 'ipv4_mapped', None) or address
            assert host and ipaddress.ip_address(host) == address, (
                f'browser_host({candidate!r}) = {host!r}, a browser reads [{expected}]')
        else:
            assert host == expected.rstrip('.'), f'browser_host({candidate!r}) = {host!r}, a browser reads {expected!r}'

    host = storefront.browser_host('https:' + url)
    if host.isascii() and host.isprintable() and host and ':' not in host and host != storefront.UNMAPPABLE \
            and not storefront._ends_in_a_number(host):   # a dropped trailing dot ('0..') made it a domain, not an address
        cut = len(data) % len(host)
        spellings = [f'https://{host[:cut]}{ignored}{host[cut:]}/x' for ignored in _IGNORED_BY_UTS46]
        spellings.append('https://' + ''.join(chr(ord(c) + 0xfee0) if '!' <= c <= '~' else c for c in host) + '/x')
        for spelling in spellings:
            assert storefront.browser_host(spelling) == host, f'{spelling!r} is not read as {host!r}'

    for token in storefront._css_urls(url):
        assert isinstance(token, str)
    for start, end in storefront._srcset_urls(url):
        assert 0 <= start < end <= len(url) and url[start] not in '\t\n\f\r ,'
    bare = ''.join(c for c in url if not c.isspace() and c not in ')\'"\\/*(')
    if bare:
        for prefix in _CSS_PREFIXES:
            assert bare in storefront._css_urls(f'{prefix}url({bare})'), f'{prefix!r} hides url({bare!r})'


# None is budgeted: no regex here has a backtracking shape. The redirect, request-host, webfinger and resolver
# targets need an app: they read current_app.config or push a request context.
TARGETS = [
    Target(name='url_predicates', check=check_url_predicates, surface='urls', budget_ms=None, needs_app=False),
    Target(name='url_host_helpers', check=check_host_helpers, surface='urls', budget_ms=None, needs_app=False),
    Target(name='media_urls', check=check_media_urls, surface='urls', budget_ms=None, needs_app=False),
    Target(name='redirect_targets', check=check_redirect_targets, surface='urls', budget_ms=None, needs_app=True),
    Target(name='invalid_get_uri', check=check_invalid_get_uri, surface='urls', budget_ms=None, needs_app=True),
    Target(name='requestor_domain', check=check_requestor_domain, surface='urls', budget_ms=None, needs_app=True),
    Target(name='webfinger_resource', check=check_webfinger_resource, surface='urls', budget_ms=None,
           needs_app=True),
    Target(name='strip_tracking', check=check_strip_tracking, surface='urls', budget_ms=None, needs_app=False),
    Target(name='storefront_browser_host', check=check_browser_host, surface='urls', budget_ms=200, needs_app=False),
    Target(name='nodeinfo_hrefs', check=check_nodeinfo_hrefs, surface='urls', budget_ms=None, needs_app=False),
]
