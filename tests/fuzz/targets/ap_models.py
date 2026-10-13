"""Property checks for the peer-document coercers in app/models.py (tier T2).

Every column PieFed fills from a peer's actor or object document goes through one of these: the `_as_*` readers
that turn a JSON value into something its column can hold, the icon/image, 'More info', name, key, language and
markdown readers, the PropertyValue fields, and the timestamp readers in front of every DateTime column. Each
was written after a peer's value crashed a refresh or lost a post (the D13xx comments), so each check asserts the
documented contract -- the type out, the column width, the http(s) scheme -- for any JSON value at all, and a
raise is a finding.

Python's json module, like Flask's get_json, accepts NaN and Infinity, so those reach the readers as floats.

No database and no network. is_local_url reads SERVER_URL from the app config, so its target alone runs in an app
context; nothing in any check touches the database.
"""

import math
import re
from datetime import datetime

from flask import current_app

from app.activitypub.util import host_of
from app.models import (_as_dict, _as_float, _as_int, _as_text, _as_url, actor_name_from_ap, image_url_from,
                        is_local_url, language_from_ap, markdown_source, more_info_url_from, parse_ap_published,
                        parse_ap_timestamp, property_value_fields, public_key_pem, utcnow)
from tests.fuzz.targets._target import Target
from tests.fuzz.targets.ap_json import _as_object, _is_http_url
from tests.fuzz.targets.signatures import _fields, _json_or_text, _text

# The keys a document is read under, so a fuzzed value reaches each reader whatever its top-level type.
_SLOTS = ('icon', 'image', 'attachment', 'preferredUsername', 'name', 'publicKey', 'language', 'source', 'url')


def _values(value):
    """The value itself and, when it is a list or dict, each of its members: one input exercises a reader on the
    whole value and on every element a peer could have put in it."""
    yield value
    if isinstance(value, list):
        yield from value
    elif isinstance(value, dict):
        yield from value.values()


def _check_scalars(value) -> None:
    for default in (None, 7):
        number = _as_int(value, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            assert number is default, f'_as_int({value!r}) = {number!r}, not the default'
        elif isinstance(value, float) and not math.isfinite(value):
            assert number is default, f'_as_int({value!r}) = {number!r}: not a number a column can hold'
        else:
            assert type(number) is int and number == int(value)

        real = _as_float(value, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            assert real is default, f'_as_float({value!r}) = {real!r}, not the default'
        elif isinstance(value, int) and abs(value) >= 2 ** 1024 - 2 ** 970:   # rounds past the largest float
            assert real is default, f'_as_float of a {len(str(value))}-digit integer = {real!r}'
        else:
            assert type(real) is float

    for limit in (None, 5):
        text = _as_text(value, limit)
        if isinstance(value, str) and value:
            assert text == (value[:limit] if limit else value)
        else:
            assert text is None, f'_as_text({value!r}) = {text!r}'
        url = _as_url(value, limit)
        assert url is None or (url == text and _is_http_url(url)), f'_as_url({value!r}) = {url!r}'
        if text is not None and _is_http_url(text):
            assert url == text

    mapping = _as_dict(value)
    assert mapping is value if isinstance(value, dict) else mapping == {}


def check_coercers(data: bytes) -> None:
    """_as_int, _as_float, _as_text, _as_url and _as_dict, on the value and on each of its members.

    _as_int and _as_float give the default for anything that is not a number, and for a bool -- True is an int in
    Python, and never a count. _as_int gives an int only for a finite number. _as_text gives a non-empty string
    cut to the limit, or None; _as_url that string only when it starts http:// or https:// (any case)."""
    value = _json_or_text(data)
    for member in _values(value):
        _check_scalars(member)


def _assert_url(url, what: str) -> None:
    assert url is None or (_is_http_url(url) and len(url) <= 1024), f'{what} gave {url!r}'


def check_actor_fields(data: bytes) -> None:
    """image_url_from, more_info_url_from, actor_name_from_ap, property_value_fields, public_key_pem,
    language_from_ap and markdown_source over any JSON value.

    Every URL out is http(s) and fits File.source_url (1024). A name is non-empty, unpadded at the front and at
    most its limit. PropertyValue pairs are strings of at most 1024. A PEM is a non-empty string; a language is a
    code of at most 5 and a name of at most 50, both non-empty; markdown is a string, and only from a `source`
    whose mediaType is text/markdown unless that is waived."""
    value = _json_or_text(data)
    document = _as_object(value)
    for slot in _SLOTS:
        document.setdefault(slot, value)

    for member in _values(value):
        for prefer_last in (False, True):
            _assert_url(image_url_from(member, prefer_last), 'image_url_from')
        _assert_url(more_info_url_from(member), 'more_info_url_from')
        language = language_from_ap(member)
        if language is not None:
            code, name = language
            assert isinstance(code, str) and 0 < len(code) <= 5, f'language code {code!r}'
            assert isinstance(name, str) and 0 < len(name) <= 50, f'language name {name!r}'
    _assert_url(image_url_from(document.get('icon'), True), 'image_url_from(icon)')
    _assert_url(more_info_url_from(document.get('attachment')), 'more_info_url_from(attachment)')

    for key, limit in (('preferredUsername', 255), ('name', 255), ('name', 3)):
        name = actor_name_from_ap(document, key, limit)
        if name is not None:
            assert isinstance(name, str) and 0 < len(name) <= limit and name == name.lstrip(), f'name {name!r}'
    assert actor_name_from_ap(value) is None or isinstance(value, dict)

    for limit in (1024, 4):
        for label, text in property_value_fields(document.get('attachment'), limit):
            assert isinstance(label, str) and isinstance(text, str), f'field ({label!r}, {text!r})'
            assert len(label) <= limit and len(text) <= limit

    pem = public_key_pem(document)
    assert pem is None or (isinstance(pem, str) and pem), f'public_key_pem gave {pem!r}'

    for require in (True, False):
        markdown = markdown_source(document, require)
        if markdown is not None:
            source = document['source']
            assert isinstance(markdown, str) and markdown is source['content']
            assert source.get('mediaType') == 'text/markdown' or (not require and source.get('mediaType') is None)


def check_timestamps(data: bytes) -> None:
    """parse_ap_timestamp and parse_ap_published, for any JSON value and any ISO-shaped string.

    Each is None or a NAIVE datetime (the columns are timestamp without time zone, and an offset is converted to
    UTC first), and a published time is never later than now. Neither raises: a raise loses the post."""
    value = _json_or_text(data)
    for member in _values(value):
        timestamp = parse_ap_timestamp(member)
        if timestamp is not None:
            assert isinstance(member, str) and type(timestamp) is datetime and timestamp.tzinfo is None, (
                f'parse_ap_timestamp({member!r}) = {timestamp!r}')
        published = parse_ap_published(member)
        if published is not None:
            assert type(published) is datetime and published.tzinfo is None
            assert published <= utcnow(), f'parse_ap_published({member!r}) = {published} is in the future'
            assert published == min(timestamp, published)


_LOOKALIKE_SUFFIXES = ('', '/', '.', '.evil.example', '@evil.example', ':8443', '\\@evil.example', '%2e')


def check_local_url(data: bytes) -> None:
    """is_local_url: True exactly for SERVER_URL itself or a URL that continues it with '/', and never for a URL
    whose host is another than SERVER_URL's -- a look-alike host (SERVER_URL plus a suffix, a userinfo trick) is
    never this instance. The fuzzed text is tried alone and appended to SERVER_URL."""
    base = current_app.config['SERVER_URL']
    base_host = host_of(base)
    first, second = _fields(data, 2)
    text, tail = _text(first), _text(second)
    for url in (text, base + text, base + tail + text, *(base + suffix + text for suffix in _LOOKALIKE_SUFFIXES)):
        local = is_local_url(url)
        assert local is (url == base or url.startswith(base + '/')), f'is_local_url({url!r}) = {local!r}'
        if local:
            assert host_of(url) == base_host, f'is_local_url accepted {url!r}, whose host is {host_of(url)!r}'
            assert re.match(re.escape(base) + r'(/|$)', url)
    value = _json_or_text(first)
    expected = isinstance(value, str) and (value == base or value.startswith(base + '/'))
    assert is_local_url(value) is expected, f'is_local_url({value!r})'


TARGETS = [
    Target(name='models_coercers', check=check_coercers, surface='ap_models', budget_ms=None, needs_app=False),
    Target(name='models_actor_fields', check=check_actor_fields, surface='ap_models', budget_ms=None,
           needs_app=False),
    Target(name='models_timestamps', check=check_timestamps, surface='ap_models', budget_ms=None, needs_app=False),
    # The only one with an app: SERVER_URL is config. No database is touched.
    Target(name='models_local_url', check=check_local_url, surface='ap_models', budget_ms=None, needs_app=True),
]
