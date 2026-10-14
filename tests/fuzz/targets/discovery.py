"""Property checks for discovery (tier T4, app/discovery/): the URL filters, the directory entry parsers, the
SepiaSearch video rows and podcast feed credits.

Discovery reads third-party directories -- joinmastodon.org, FediDB, SepiaSearch, index.castopod.org -- and
podcast RSS feeds, and turns their rows into links this server shows and actors it later fetches. Each check
asserts the contract the code documents: a row cannot point an entry at a host other than the one it names (the
actor URL is https and a browser reads the entry's host from it), names come out as plain capped text, a feed
that declares a DTD gives nothing, credits are capped and their URLs https. Hosts are compared against
urls.whatwg_reading, the independent WHATWG reading, so a URL urllib and a browser split differently is caught.

No database and no network: every function here is pure but for filters.url_is_excluded and the fetchers,
which are not called. Inputs are JSON (signatures._json_or_text) or length-prefixed fields (signatures._fields).
"""

import json
from urllib.parse import urlsplit
from xml.parsers import expat
from xml.sax.saxutils import escape, quoteattr

import httpx

from app.discovery import castopod, mastodon, peertube, pixelfed, sources
from app.discovery.credits import (MAX_CREDITS, _declares_a_dtd, episode_credits, parse_feed,
                                   parse_feed_credits)
from app.discovery.external_search import clean_video_url, video_from
from app.discovery.filters import NAME_LIMIT, URL_LIMIT, ascii_host, clean_https_url, clean_name, looks_nsfw, \
    request_host, strict_ascii_host
from app.utils import url_is_storable
from tests.fuzz.targets._target import Target
from tests.fuzz.targets.markdown import _reference_hostname
from tests.fuzz.targets.signatures import _fields, _json_or_text, _text
from tests.fuzz.targets.urls import INVALID, whatwg_reading

MAX_INPUT = 64 * 1024


def _strings(value) -> list:
    """Every str in a JSON value, one level into a dict or list, so one input feeds several calls."""
    found = [value] if isinstance(value, str) else []
    if isinstance(value, dict):
        found += [item for item in value.values() if isinstance(item, str)] + [key for key in value]
    elif isinstance(value, list):
        found += [item for item in value if isinstance(item, str)]
    return found


def https_on(url, host: str) -> bool:
    """True when a browser reads `url` as an https URL on exactly `host`, or refuses it outright (a host ending in a
    label like '0xa1' is an IPv4 address to a browser, and an invalid one): a refused URL sends nobody anywhere."""
    if not isinstance(url, str):
        return False
    scheme, browser_host = whatwg_reading(url)
    return scheme == 'https' and (browser_host == host or browser_host is INVALID)


# ---------------------------------------------------------------------------
# filters: hosts, names, https URLs
# ---------------------------------------------------------------------------

_CONTROL = frozenset(chr(c) for c in list(range(0x20)) + list(range(0x7F, 0xA0)))


def _authority(url: str) -> str:
    rest = url.partition('://')[2] if '://' in url else ''
    for delimiter in '/?#':
        rest = rest.partition(delimiter)[0]
    return rest


def check_discovery_filters(data: bytes) -> None:
    """request_host, ascii_host, strict_ascii_host, clean_https_url, clean_name and looks_nsfw, none of which raises.

    request_host gives None or a lower-case ASCII host with no trailing dot, and only for a URL with no backslash
    and no userinfo whose host httpx (the client that fetches) and urllib both read as that host. ascii_host and
    strict_ascii_host give None or such a host. clean_https_url gives None or its input unchanged, and only for an
    https URL within URL_LIMIT that url_is_storable accepts. clean_name gives None or stripped text within its limit
    with no tag, no control character and no run of whitespace. looks_nsfw is a bool, False for anything without
    'nsfw', '18+' or 'porn' in it."""
    value = _json_or_text(data[:MAX_INPUT])
    for text in _strings(value) or [value]:
        host = request_host(text)
        if host is not None:
            assert isinstance(text, str) and '\\' not in text, f'request_host accepted {text!r}'
            assert host.isascii() and host == host.lower() and host and not host.endswith('.'), f'host {host!r}'
            assert httpx.URL(text).raw_host.decode('ascii').lower().rstrip('.') == host
            assert not httpx.URL(text).userinfo and not _authority(text).rpartition('@')[0], (
                f'request_host accepted userinfo in {text!r}')
            if text.isascii():
                assert urlsplit(text).hostname.rstrip('.').lower() == host, f'urllib reads another host in {text!r}'

        other = ascii_host(text)
        assert other is None or (other.isascii() and other == other.lower() and not other.endswith('.')), (
            f'ascii_host({text!r}) = {other!r}')
        if isinstance(text, str):
            strict = strict_ascii_host(text)
            assert strict is None or (strict.isascii() and strict == strict.lower() and not strict.endswith('.')), (
                f'strict_ascii_host({text!r}) = {strict!r}')

        cleaned = clean_https_url(text)
        if cleaned is not None:
            assert cleaned is text and len(text) <= URL_LIMIT, f'clean_https_url changed {text!r}'
            assert whatwg_reading(text)[0] == 'https', f'clean_https_url accepted {text!r}'
            assert url_is_storable(text)

        name = clean_name(text)
        if name is not None:
            assert isinstance(name, str) and name and len(name) <= NAME_LIMIT and name == name.strip()
            assert not set(name) & _CONTROL, f'clean_name({text!r}) kept a control character: {name!r}'
            assert '  ' not in name and not ('<' in name and '>' in name.partition('<')[2]), f'clean_name {name!r}'
        short = clean_name(text, 5)
        assert short is None or len(short) <= 5

        nsfw = looks_nsfw(text)
        assert isinstance(nsfw, bool)
        if not isinstance(text, str):
            assert nsfw is False
        elif 'nsfw' not in text.lower() and '18+' not in text and 'porn' not in text.lower():
            assert nsfw is False, f'looks_nsfw({text!r})'


# ---------------------------------------------------------------------------
# Directory entry parsers
# ---------------------------------------------------------------------------

def _check_entry(entry, platform: str, host: str | None = None) -> None:
    if entry is None:
        return
    assert isinstance(entry, dict) and tuple(entry) == sources.ENTRY_KEYS, f'entry keys {list(entry)!r}'
    assert entry['platform'] == platform
    assert _reference_hostname(entry['host']), f'entry host {entry["host"]!r}'
    if host is not None:
        assert entry['host'] == host
    assert https_on(entry['actor_url'], entry['host']), (
        f'{platform} entry actor_url {entry["actor_url"]!r} is not https on {entry["host"]!r}')
    assert isinstance(entry['name'], str)
    assert entry['avatar'] is None or isinstance(entry['avatar'], str)
    assert type(entry['followers']) is int and entry['followers'] >= 0
    assert isinstance(entry['nsfw'], bool)


def check_discovery_entries(data: bytes) -> None:
    """The mastodon, pixelfed, peertube and castopod entry parsers and sources' helpers, over any JSON row.

    Every entry is None or exactly ENTRY_KEYS, its host a lower-case DNS name and its actor_url an https URL a
    browser reads as on that host -- a row cannot send the entry to a third party. The server lists give only
    such host names. uri_is_on is True only for an https URI a browser reads as on the host; actor_url_on gives
    one on the host; as_count gives a non-negative int, 0 for a bool."""
    value = _json_or_text(data[:MAX_INPUT])
    rows = value if isinstance(value, list) else [value]

    servers = mastodon.top_mastodon_servers(value)
    hosts = pixelfed.pixelfed_hosts(value if isinstance(value, dict) else {'data': rows})
    for name in servers + hosts:
        assert _reference_hostname(name), f'server list gave {name!r}'
    host = (servers or hosts or ['directory.example'])[0]

    for row in rows[:16]:
        _check_entry(mastodon.account_to_entry(row, host), 'mastodon', host)
        _check_entry(pixelfed.profile_to_entry(row, host), 'pixelfed', host)
        _check_entry(peertube.channel_to_entry(row), 'peertube')
        _check_entry(castopod.podcast_to_entry(row), 'castopod')

        count = sources.as_count(row)
        assert type(count) is int and count >= 0 and (count == 0 or count is row)
        for text in _strings(row):
            on = sources.uri_is_on(text, host)
            assert isinstance(on, bool)
            if on:
                assert https_on(text, host), f'uri_is_on({text!r}, {host!r}) but a browser reads {whatwg_reading(text)!r}'
            assert https_on(sources.actor_url_on(text, host, 'someone'), host)
            assert sources.is_username(text) is (isinstance(text, str) and 1 <= len(text) <= 64 and set(text) <= set(
                'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-'))


def check_video_rows(data: bytes) -> None:
    """external_search.video_from and clean_video_url over any JSON row: a video is None or https on exactly the
    host its channel names (as a browser reads its rebuilt URL), with a non-empty stripped title;
    clean_video_url's (url, host) has a URL a browser reads as https on that host, with no userinfo and no port."""
    value = _json_or_text(data[:MAX_INPUT])
    for row in (value if isinstance(value, list) else [value])[:16]:
        video = video_from(row)
        if video is not None:
            channel_host = row['channel']['host'].lower()
            assert video['host'] == channel_host, f'video host {video["host"]!r}, channel {channel_host!r}'
            assert https_on(video['url'], channel_host), f'video url {video["url"]!r} not on {channel_host!r}'
            assert video['title'] and video['title'] == video['title'].strip()
            assert isinstance(video['channel'], str) and isinstance(video['nsfw'], bool)
        for text in _strings(row) + [row.get('url') if isinstance(row, dict) else None]:
            cleaned = clean_video_url(text)
            if cleaned is not None:
                url, host = cleaned
                assert https_on(url, host) and _reference_hostname(host), f'clean_video_url({text!r}) = {cleaned!r}'
                assert url.startswith(f'https://{host}') and url[len(f'https://{host}'):][:1] in ('', '/', '?')


# ---------------------------------------------------------------------------
# Podcast feed credits
# ---------------------------------------------------------------------------

_CREDIT_KEYS = ('name', 'role', 'image', 'profile_url', 'user_id')
_NAMESPACE = 'https://podcastindex.org/namespace/1.0'


def _templated_feed(fields: list[bytes]) -> bytes:
    """A podcast feed built around fuzz text, so the parse reaches the person tags rather than failing on XML: the
    first byte is how many channel persons (up to 31); each later field is role|href|img|name, escaped."""
    count = fields[0][0] % 32 if fields[0] else 1
    parts = [_text(field).split('|', 3) + ['', '', '', ''] for field in fields[1:]] or [['host', '', '', 'x'] * 1]
    people = []
    for index in range(count):
        role, href, img, name = parts[index % len(parts)][:4]
        people.append(f'<podcast:person role={quoteattr(role)} href={quoteattr(href)} img={quoteattr(img)}>'
                      f'{escape(name)}</podcast:person>')
    guests = ''.join(person.replace(f'role=', 'role="guest" data-role=', 1) for person in people)
    link = escape(parts[0][1])
    return (f'<?xml version="1.0"?><rss xmlns:podcast="{_NAMESPACE}"><channel>{"".join(people)}'
            f'<item><link>{link}</link><guid>{escape(parts[0][2])}</guid>{guests}</item></channel></rss>').encode()


def _check_credit(credit, role: str) -> None:
    assert isinstance(credit, dict) and tuple(credit) == _CREDIT_KEYS, f'credit {credit!r}'
    assert credit['role'] == role and credit['user_id'] is None
    name = credit['name']
    assert isinstance(name, str) and name and len(name) <= 100 and not set(name) & _CONTROL, f'credit name {name!r}'
    assert not ('<' in name and '>' in name.partition('<')[2]), f'credit name {name!r}'
    for key in ('image', 'profile_url'):
        url = credit[key]
        if url is not None:
            assert whatwg_reading(url)[0] == 'https' and url_is_storable(url), f'credit {key} {url!r}'


def _declarations(feed: bytes) -> list:
    """The DOCTYPE and ENTITY declarations expat reports in `feed`, read with a parser of this check's own."""
    seen = []
    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = lambda *args: seen.append('doctype')
    parser.EntityDeclHandler = lambda *args: seen.append('entity')
    try:
        parser.Parse(feed, True)
    except (expat.ExpatError, LookupError, ValueError):
        pass
    return seen


def check_feed_credits(data: bytes) -> None:
    """parse_feed, _declares_a_dtd, episode_credits and parse_feed_credits over raw or templated RSS. Never raises.
    A feed declaring a DOCTYPE or an ENTITY gives nothing. At most MAX_CREDITS hosts, MAX_CREDITS guests per
    episode and MAX_CREDITS credits for one episode; every credit is plain capped text with https storable URLs,
    and episode_credits gives new dicts."""
    mode, episode, rest = _fields(data[:MAX_INPUT], 3)
    episode_url = _text(episode)
    if mode[:1] and mode[0] & 1:
        feed = _templated_feed(rest.split(b'\x00'))
    else:
        feed = rest

    declares = _declares_a_dtd(feed)
    assert isinstance(declares, bool)
    parsed = parse_feed(feed)
    assert set(parsed) == {'hosts', 'guests'}
    if declares:
        assert parsed == {'hosts': [], 'guests': {}}, 'a feed declaring a DTD gave credits'
    elif parsed['hosts'] or parsed['guests']:
        assert not _declarations(feed), 'a feed declaring a DTD gave credits'
    assert len(parsed['hosts']) <= MAX_CREDITS
    for credit in parsed['hosts']:
        _check_credit(credit, 'host')
    for key, guests in parsed['guests'].items():
        assert isinstance(key, str) and not key.endswith('/')
        assert len(guests) <= MAX_CREDITS
        for credit in guests:
            _check_credit(credit, 'guest')

    credits = episode_credits(parsed, episode_url)
    assert len(credits) <= MAX_CREDITS
    before = json.dumps(parsed, sort_keys=True)
    for credit in credits:
        assert credit['role'] in ('host', 'guest')
        credit['name'] = 'changed'
    assert json.dumps(parsed, sort_keys=True) == before, 'episode_credits gave dicts shared with the parse'
    assert parse_feed_credits(feed, episode_url) == episode_credits(parse_feed(feed), episode_url)


# None is budgeted: expat and the entry parsers are linear, and is_hostname's _HOSTNAME is redos_hostnames' timed
# target. None needs an app.
TARGETS = [
    Target(name='discovery_filters', check=check_discovery_filters, surface='discovery', budget_ms=None,
           needs_app=False),
    Target(name='discovery_entries', check=check_discovery_entries, surface='discovery', budget_ms=None,
           needs_app=False),
    Target(name='discovery_video_rows', check=check_video_rows, surface='discovery', budget_ms=None, needs_app=False),
    Target(name='podcast_feed_credits', check=check_feed_credits, surface='discovery', budget_ms=None,
           needs_app=False),
]
