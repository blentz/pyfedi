"""Property checks for the ActivityPub JSON extractors (tier T2, app/activitypub/util.py and entitlement.py).

Everything here reads a peer's JSON document, or a string out of one, before anything about it is trusted: the
`type` the inbox dispatches on, the hosts that decide who owns an id, the inbox an actor's mail is delivered to,
the content warning, language variants and posting warning a page renders, the media and episode URLs stored and
fetched, a ban's expiry, an object's audience, the actor ids a signature's keyId may belong to. Each check asserts
the contract the function documents -- output types, length caps, that every URL out is http(s) and storable,
that an id is only ever trusted on its owner's host -- rather than "it did not raise", and a function that raises
on a shape JSON can carry is a finding.

Inputs are JSON. Bytes that do not decode as JSON go in as a str value, so the top level may be any JSON type:
`shared_inbox` checks only `'id' in request_json`, which a list or a str passes as well as a dict, so a non-dict
reaches normalise_activity_types. A function whose every caller has already checked for a dict is only given
dicts; one that can be reached with anything is given anything. Multi-argument inputs use signatures._fields.

Python's json module, like Flask's get_json, accepts NaN and Infinity, so those reach the coercers as floats.

No database and no network. language_variants_from and posting_warning_from_ap call allowlist_html without
test_env, which reaches the DB-backed fediverse_domains() and get_emoji_replacements(); both are swapped for empty
answers for the duration (_no_db_lookups). The inbox target never lets `endpoints` be a URL string (that fetches)
and fails if anything resolves a host. The only target with an app context is the inbox one, because
delivery_inbox_of logs an off-host inbox through current_app.logger.
"""

import ipaddress
import json
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import urlsplit

import app.utils as app_utils
from app.activitypub import util as ap_util
from app.activitypub.entitlement import (SIGNED_FETCH_EXEMPT_PREFIXES, _owner_candidates, _path_matches,
                                         addressees_of, signed_fetch_exempt)
from app.activitypub.util import (AS_PUBLIC, CONTENT_WARNING_MAX_LENGTH, DISPATCHED_AP_TYPES, INBOX_URL_MAX,
                                  MAX_GALLERY_IMAGES, MAX_IMAGE_SOURCE_URL_LENGTH, MAX_LANGUAGE_VARIANTS,
                                  MAX_POST_URL_LENGTH, QUOTE_KEYS, _attributed_ids, _id_of, _is_tombstone,
                                  _language_map, _note_urls, _same_id, _single_type, _type_holders,
                                  activity_dedupe_key, activitypub_visibility, announce_target_uri, authority_of,
                                  castopod_episode_url, content_warning_from, delivery_inbox_of, ensure_domains_match,
                                  extract_domain_and_actor, gallery_attachments, host_of, is_top_level, is_vote,
                                  language_variants_from, media_url_from, names_actor, normalise_activity_types,
                                  normalise_actor_string, object_has_missing_fields, off_host_inbox, on_owner_host,
                                  parse_ban_expiry, posting_warning_from_ap, primary_subtag, quote_id_from,
                                  shared_inbox_of, stable_create_token)
from app.models import language_from_ap
from app.utils import random_chars, url_is_storable
from tests.fuzz.targets._target import Target
from tests.fuzz.targets.sanitizers import assert_allowlisted
from tests.fuzz.targets.signatures import _fields, _json_or_text, _network_forbidden, _text


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _dump(value) -> str | None:
    """A canonical text of a JSON value, NaN included, to compare a value before and after a call. None when the
    value nests too deeply for the encoder, in which case the caller compares nothing."""
    try:
        return json.dumps(value)
    except RecursionError:
        return None


def _as_object(value) -> dict:
    """`value` when it is a dict, else a dict carrying it under every key the extractors read, so a non-object
    input still reaches the code behind each key rather than being discarded."""
    if isinstance(value, dict):
        return value
    return {'type': value, 'url': value, 'id': value, 'content': value, 'summary': value}


def _is_http_url(value) -> bool:
    return isinstance(value, str) and value.lower().startswith(('http://', 'https://'))


@contextmanager
def _no_db_lookups():
    """allowlist_html without test_env reads the instance list and the emoji table; neither may touch a database
    here, and neither changes what the allowlist property is."""
    saved = app_utils.fediverse_domains, app_utils.get_emoji_replacements
    app_utils.fediverse_domains = lambda: []
    app_utils.get_emoji_replacements = lambda: {}
    try:
        yield
    finally:
        app_utils.fediverse_domains, app_utils.get_emoji_replacements = saved


# RFC 3986, read independently of urllib: the characters a URI may hold at all, and the authority grammar.
_URI_CHARS = frozenset('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~:/?#[]@!$&\'()*+,;=%')
_SCHEME = re.compile(r'[A-Za-z][A-Za-z0-9+.\-]*')
_PCT_OK = re.compile(r'%(?![0-9A-Fa-f]{2})')
_USERINFO = re.compile(r"[A-Za-z0-9\-._~%!$&'()*+,;=:]*")
_REG_NAME = re.compile(r"[A-Za-z0-9\-._~%!$&'()*+,;=]*")
_IPV_FUTURE = re.compile(r"v[0-9A-Fa-f]+\.[A-Za-z0-9\-._~!$&'()*+,;=:]+")
_PORT = re.compile(r'[0-9]*')


def rfc3986_authority(url: str):
    """(host, port, literal) of `url` per RFC 3986 section 3.2: the host lowercased with an IP-literal's brackets
    dropped (`literal` says it was one), the port as written or None; ('', None, False) for a reference with no
    authority. None when `url` is not a well-formed URI reference,
    in which case the reading makes no claim and the caller compares nothing.

    Deliberately not urllib: a differential against urlsplit would assert that urlsplit agrees with itself."""
    if not url or not set(url) <= _URI_CHARS or _PCT_OK.search(url):
        return None
    rest = url
    colon = url.find(':')
    first_delim = min((i for i in (url.find(c) for c in '/?#') if i >= 0), default=len(url))
    if 0 < colon < first_delim and _SCHEME.fullmatch(url[:colon]):
        rest = url[colon + 1:]
    if not rest.startswith('//'):
        return '', None, False
    authority = re.split(r'[/?#]', rest[2:], maxsplit=1)[0]
    userinfo, at, hostport = authority.rpartition('@')
    if at and not _USERINFO.fullmatch(userinfo):
        return None
    if hostport.startswith('['):
        literal, closed, after = hostport[1:].partition(']')
        if not closed or (after and not after.startswith(':')):
            return None
        if not _IPV_FUTURE.fullmatch(literal):
            try:
                ipaddress.IPv6Address(literal)
            except ValueError:
                return None
            if '%' in literal:   # RFC 3986 has no zone identifier
                return None
        host, port, is_literal = literal, after[1:] if after else None, True
    else:
        is_literal = False
        host, _, port = hostport.partition(':')
        if not _REG_NAME.fullmatch(host):
            return None
        port = port if ':' in hostport else None
    if port is not None and not _PORT.fullmatch(port):
        return None
    return host.lower(), port or None, is_literal


# ---------------------------------------------------------------------------
# Activity types (interop C1 item 8) and the minimum-fields gates
# ---------------------------------------------------------------------------

def _reference_single_type(value):
    """_single_type's documented rule, written out again: an array's one dispatched type, else its first string,
    else None; None too when it names two dispatched types."""
    if not isinstance(value, list):
        return value
    strings = [entry for entry in value if isinstance(entry, str)]
    dispatched = []
    for entry in strings:
        if entry in DISPATCHED_AP_TYPES and entry not in dispatched:
            dispatched.append(entry)
    if len(dispatched) > 1:
        return None
    return dispatched[0] if dispatched else (strings[0] if strings else None)


def _reference_holders(activity: dict) -> list:
    holders = [activity]
    obj = activity.get('object')
    if isinstance(obj, dict):
        holders.append(obj)
        if _reference_single_type(activity.get('type')) == 'Announce' and isinstance(obj.get('object'), dict):
            holders.append(obj['object'])
    return holders


def check_activity_types(data: bytes) -> None:
    """normalise_activity_types, _single_type, _type_holders, object_has_missing_fields and is_vote.

    normalise_activity_types is given any JSON value. It never changes its input. For a dict it returns the dict
    itself when no type the dispatcher reads is an array; otherwise None when one of those arrays has no single
    usable type, else a different dict equal to the input but for those types, each now _single_type's choice.
    Anything that is not a dict has no type to read and is None. _type_holders and _single_type agree with the
    rule their docstrings state; the minimum-fields gate and is_vote with theirs."""
    value = _json_or_text(data)
    before = _dump(value)

    result = normalise_activity_types(value)

    if before is not None:
        assert _dump(value) == before, 'normalise_activity_types changed its input'
    if not isinstance(value, dict):
        assert result is None, f'a {type(value).__name__} was not refused'
    else:
        holders = _reference_holders(value)
        assert _type_holders(value) == holders and all(
            a is b for a, b in zip(_type_holders(value), holders)), '_type_holders read different objects'
        for holder in holders:
            declared = holder.get('type')
            chosen = _single_type(declared)
            if isinstance(declared, list):
                assert chosen == _reference_single_type(declared), f'_single_type({declared!r}) = {chosen!r}'
            else:
                assert chosen is declared, '_single_type changed a type that is not an array'
        if not any(isinstance(holder.get('type'), list) for holder in holders):
            assert result is value, 'no array type, but the activity was not returned as itself'
        elif any(_reference_single_type(holder.get('type')) is None for holder in holders
                 if isinstance(holder.get('type'), list)):
            assert result is None, 'an array type with no single usable value was not refused'
        else:
            assert isinstance(result, dict) and result is not value, 'an array type was not normalised on a copy'
            # The expected output: the input with each array type the dispatcher reads replaced by its choice.
            if before is not None:
                expected = json.loads(before)
                for holder in _reference_holders(expected):
                    if isinstance(holder.get('type'), list):
                        holder['type'] = _reference_single_type(holder['type'])
                assert _dump(result) == _dump(expected), 'the copy differs from the input by more than its types'

    if isinstance(value, dict):
        missing = object_has_missing_fields(value)
        if value.get('type') == 'OrderedCollection':
            assert missing == ('id' not in value)
        else:
            assert missing == any(key not in value for key in ('id', 'type', 'actor', 'object'))

    vote = is_vote(value)
    expected_vote = False
    if isinstance(value, dict):
        if value.get('type') == 'Announce':
            obj = value.get('object')
            expected_vote = isinstance(obj, dict) and obj.get('type') in ('Like', 'Dislike')
        else:
            expected_vote = value.get('type') in ('Like', 'Dislike')
    assert vote is expected_vote, f'is_vote said {vote!r}'


# ---------------------------------------------------------------------------
# Hosts and actor strings
# ---------------------------------------------------------------------------

def _cased(host: str) -> str:
    return host.partition('%')[0]


def _check_host_of(url: str) -> str:
    host = host_of(url)
    assert isinstance(host, str)
    # Lowercase up to a '%': urllib keeps what follows one in its case, as an IPv6 zone id is case-sensitive, and
    # for a reg-name that only makes two spellings of an odd host compare unequal -- it fails closed.
    assert _cased(host) == _cased(host).lower(), f'host_of({url!r}) = {host!r} is not lowercase'
    assert '@' not in host, f'host_of({url!r}) = {host!r} kept userinfo'
    reading = rfc3986_authority(url)
    if reading is not None:
        expected_host = reading[0]
        assert host.lower() == expected_host, f'host_of({url!r}) = {host!r}, RFC 3986 reads {expected_host!r}'

    authority = authority_of(url)
    assert isinstance(authority, str) and _cased(authority) == _cased(authority).lower() and '@' not in authority
    if authority:
        # An IP-literal comes back bracketed: IPv6, which has a ':', and IPvFuture, which need not.
        bracketed = f'[{host}]' if authority.startswith('[') else host
        assert ':' not in host or authority.startswith('['), f'authority_of({url!r}) = {authority!r}'
        assert host and (authority == bracketed or (
            authority.startswith(bracketed + ':') and authority[len(bracketed) + 1:].isdigit())), (
            f'authority_of({url!r}) = {authority!r} does not name host_of\'s {host!r}')
    if reading is not None and reading[0]:
        expected_host, port, literal = reading
        name = f'[{expected_host}]' if literal else expected_host
        if port and int(port) > 65535:
            expected = ''
        else:
            expected = f'{name}:{int(port)}' if port else name
        assert authority.lower() == expected, f'authority_of({url!r}) = {authority!r}, RFC 3986 reads {expected!r}'
    return host


def check_hosts(data: bytes) -> None:
    """host_of, authority_of, extract_domain_and_actor, normalise_actor_string, on_owner_host, names_actor and
    _same_id over two strings.

    host_of and authority_of are lowercase, carry no userinfo, and agree with an independent RFC 3986 reading on
    every well-formed reference; authority_of names host_of's host. normalise_actor_string gives ('', '') or the
    two lowercased halves of exactly one '@'. on_owner_host is True only for a non-empty owner host the entry
    shares, names_actor only for an id equal to the stored one after strip and lowercase, and _same_id is
    symmetric and reflexive exactly for an id with a host."""
    first, second = _fields(data, 2)
    a, b = _text(first), _text(second)

    host_a, host_b = _check_host_of(a), _check_host_of(b)

    domain, actor = extract_domain_and_actor(a)
    assert isinstance(domain, str) and isinstance(actor, str)
    assert not any(c in domain for c in '/?#'), f'extract_domain_and_actor domain {domain!r}'
    assert '/' not in actor, f'extract_domain_and_actor actor {actor!r}'

    name, server = normalise_actor_string(a)
    if (name, server) != ('', ''):
        assert name and server and '@' not in name + server
        assert name == name.lower() and server == server.lower()
        stripped = a.strip()
        if stripped[:1] in ('@', '!', '~'):
            stripped = stripped[1:]
        assert stripped.lower() == f'{name}@{server}', f'normalise_actor_string({a!r}) = {(name, server)!r}'
    else:
        stripped = a.strip()
        if stripped[:1] in ('@', '!', '~'):
            stripped = stripped[1:]
        parts = stripped.split('@')
        assert len(parts) != 2 or not parts[0] or not parts[1], f'normalise_actor_string refused {a!r}'

    for entry in (a, {'id': a}):
        owned = on_owner_host(entry, b)
        assert owned is (bool(host_b) and host_a == host_b), f'on_owner_host({entry!r}, {b!r}) = {owned!r}'

    assert names_actor(a, SimpleNamespace(ap_profile_id=b)) is (bool(b) and a.strip().lower() == b.lower())
    assert names_actor(a, None) is False

    same = _same_id(a, b)
    assert same is _same_id(b, a), f'_same_id is not symmetric for {a!r}, {b!r}'
    try:
        has_host = bool(urlsplit(a).netloc)
    except ValueError:
        has_host = False
    assert _same_id(a, a) is has_host, f'_same_id({a!r}, itself) = {_same_id(a, a)!r}'
    if same:
        assert host_a == host_b, f'_same_id named two hosts the same: {a!r}, {b!r}'


# ---------------------------------------------------------------------------
# Inboxes
# ---------------------------------------------------------------------------

def check_inboxes(data: bytes) -> None:
    """shared_inbox_of, off_host_inbox and delivery_inbox_of over an actor document with an inline `endpoints`.

    A shared inbox is None or on the host of the document's own `id`; an off-host inbox is None or a string NOT on
    that host; the delivery inbox is None or at most INBOX_URL_MAX characters on the host of the STORED actor id,
    the shared inbox when it is usable, else `inbox`, and None whenever either names another host. An id with no
    host trusts nothing. No call reaches the network: `endpoints` as a URL string (the document form, which is
    fetched) is never given."""
    stored, document = _fields(data, 2)
    actor_id = _text(stored)
    actor_json = _as_object(_json_or_text(document))
    if isinstance(actor_json.get('endpoints'), str):
        actor_json = {**actor_json, 'endpoints': {'sharedInbox': actor_json['endpoints']}}

    with _network_forbidden() as attempts:
        shared = shared_inbox_of(actor_json)
        off_host = off_host_inbox(actor_json)
        delivery = delivery_inbox_of(actor_json, actor_id)
    assert not attempts, f'an inbox helper reached the network: {attempts!r}'

    document_id = actor_json.get('id')
    document_host = host_of(document_id) if isinstance(document_id, str) else ''
    endpoints = actor_json.get('endpoints')
    declared = endpoints.get('sharedInbox') if isinstance(endpoints, dict) else None
    declared = declared if isinstance(declared, str) and declared else None
    inbox = actor_json.get('inbox')
    inbox = inbox if isinstance(inbox, str) and inbox else None

    if shared is not None:
        assert shared == declared and document_host and host_of(shared) == document_host, (
            f'shared inbox {shared!r} is not on the actor\'s host {document_host!r}')
    else:
        assert declared is None or not document_host or host_of(declared) != document_host

    if off_host is not None:
        assert off_host == inbox and host_of(off_host) != document_host, f'off-host inbox {off_host!r}'
    else:
        assert inbox is None or host_of(inbox) == document_host

    stored_host = host_of(actor_id)
    if delivery is not None:
        assert len(delivery) <= INBOX_URL_MAX, f'delivery inbox of {len(delivery)} characters'
        assert stored_host and host_of(delivery) == stored_host, (
            f'delivery inbox {delivery!r} is not on the stored actor\'s host {stored_host!r}')
    candidates = [url for url in (declared, inbox) if url is not None]
    if not stored_host or any(host_of(url) != stored_host for url in candidates):
        assert delivery is None, 'a delivery inbox was chosen beside an off-host one, or for a hostless id'
    else:
        usable = [url for url in candidates if len(url) <= INBOX_URL_MAX]
        assert delivery == (usable[0] if usable else None)


# ---------------------------------------------------------------------------
# Content warnings, language maps and variants, posting warnings, quotes
# ---------------------------------------------------------------------------

def check_content_warning(data: bytes) -> None:
    """content_warning_from, primary_subtag and _language_map.

    A content warning is None or a non-empty string of at most CONTENT_WARNING_MAX_LENGTH characters, and None
    unless `summary` is a string on a Note or on an object flagged `sensitive: true`. primary_subtag is None or
    the lowercased first subtag of a BCP 47 tag. _language_map keeps, per primary subtag, the first string a
    peer gave for it."""
    obj = _as_object(_json_or_text(data))

    warning = content_warning_from(obj)
    if warning is not None:
        assert isinstance(warning, str) and warning and len(warning) <= CONTENT_WARNING_MAX_LENGTH
        assert isinstance(obj.get('summary'), str)
        assert obj.get('type') == 'Note' or obj.get('sensitive') is True

    for key in ('contentMap', 'nameMap', 'summaryMap'):
        mapping = obj.get(key)
        result = _language_map(obj, key)
        expected = {}
        if isinstance(mapping, dict):
            for tag, text in mapping.items():
                code = primary_subtag(tag)
                if code is not None:
                    assert re.fullmatch(r'[a-z]{2,3}', code) and tag.lower().split('-')[0] == code, (
                        f'primary_subtag({tag!r}) = {code!r}')
                    if isinstance(text, str):
                        expected.setdefault(code, text)
                else:
                    assert not (isinstance(tag, str) and re.fullmatch(r'[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8})*', tag))
        assert result == expected, f'_language_map({key!r}) = {result!r}'


def check_language_variants(data: bytes) -> None:
    """language_variants_from, posting_warning_from_ap and quote_id_from.

    The quote id is None or the first valid QUOTE_KEYS value (an http(s) URL with a host, at most 255 ASCII
    characters, no space or character RFC 3986 forbids), never the object's own id. Variants are (None, None)
    without a usable contentMap; otherwise the default is one of its codes and variants are None or at most
    MAX_LANGUAGE_VARIANTS other codes, each with a body that satisfies the allowlist property, a title of at most
    255 characters and a content warning content_warning_from would give. The posting warning is None or at most
    512 characters satisfying the allowlist property."""
    obj = _as_object(_json_or_text(data))

    quote = quote_id_from(obj)
    if quote is not None:
        assert isinstance(quote, str) and len(quote) <= 255 and quote.isascii() and quote != obj.get('id')
        assert not any(c.isspace() or ord(c) < 0x21 or ord(c) == 0x7f or c in '"<>\\^`{|}' for c in quote)
        assert _is_http_url(quote) and host_of(quote), f'quote id {quote!r} is not an http(s) URL with a host'
        firsts = [obj.get(key).get('id') if isinstance(obj.get(key), dict) else obj.get(key) for key in QUOTE_KEYS]
        assert quote in firsts

    with _no_db_lookups():
        default, variants = language_variants_from(obj, quote)
        warning = posting_warning_from_ap(obj)

    contents = _language_map(obj, 'contentMap')
    if not contents:
        assert (default, variants) == (None, None)
    else:
        # The docstring's order: the contentMap code whose text is `content`, else the `language` property's code
        # (which need not be a contentMap key), else the first usable key.
        ap_language = language_from_ap(obj.get('language'))
        assert default in contents or (ap_language and default == primary_subtag(ap_language[0])), (
            f'default {default!r} is neither a contentMap code nor the language property\'s')
        if variants is not None:
            assert isinstance(variants, dict) and 0 < len(variants) <= MAX_LANGUAGE_VARIANTS
            for code, variant in variants.items():
                assert code in contents and code != default
                assert set(variant) == {'title', 'body_html', 'cw'}
                assert_allowlisted(variant['body_html'])
                title = variant['title']
                assert title is None or (isinstance(title, str) and 0 < len(title) <= 255)
                cw = variant['cw']
                assert cw is None or (isinstance(cw, str) and 0 < len(cw) <= CONTENT_WARNING_MAX_LENGTH)

    if warning is not None:
        assert isinstance(warning, str) and 0 < len(warning) <= 512
        assert isinstance(obj.get('postingWarning'), str), 'a posting warning out of a value that is not a string'
        assert_allowlisted(warning)


# ---------------------------------------------------------------------------
# Media, gallery and Castopod episode URLs
# ---------------------------------------------------------------------------

_EPISODE_PATH = re.compile(r'/@[^/]+/episodes/[^/]+/?')


def _assert_storable(url, limit: int, what: str) -> None:
    assert isinstance(url, str) and _is_http_url(url), f'{what} {url!r} is not http(s)'
    assert url_is_storable(url), f'{what} {url!r} is not storable'
    assert len(url) <= limit, f'{what} of {len(url)} characters, over {limit}'


def check_media_urls(data: bytes) -> None:
    """media_url_from, gallery_attachments, castopod_episode_url and _note_urls.

    Every URL out is http(s), passes url_is_storable and fits its column: MAX_POST_URL_LENGTH for a media URL,
    MAX_IMAGE_SOURCE_URL_LENGTH for an image's and every gallery image's. A gallery is at most MAX_GALLERY_IMAGES
    images, none repeated or among the primary URLs, each with alt text of at most 1500 characters and an int or
    None size. An episode URL is on the author's host, with an episode path and no query."""
    obj = _as_object(_json_or_text(data))

    for kind in ('image', 'audio', None):
        url = media_url_from(obj, kind)
        if url is not None:
            _assert_storable(url, MAX_IMAGE_SOURCE_URL_LENGTH if kind == 'image' else MAX_POST_URL_LENGTH,
                             f'media_url_from({kind!r})')

    primary = media_url_from(obj, 'image')
    gallery = gallery_attachments({'object': obj}, [primary, None])
    assert isinstance(gallery, list) and len(gallery) <= MAX_GALLERY_IMAGES
    seen = set()
    for image in gallery:
        assert set(image) == {'url', 'alt_text', 'width', 'height'}
        _assert_storable(image['url'], MAX_IMAGE_SOURCE_URL_LENGTH, 'gallery url')
        assert image['url'] not in seen and image['url'] != primary, f'gallery repeats {image["url"]!r}'
        seen.add(image['url'])
        alt = image['alt_text']
        assert alt is None or (isinstance(alt, str) and 0 < len(alt) <= 1500)
        for size in (image['width'], image['height']):
            assert size is None or (isinstance(size, int) and not isinstance(size, bool))

    author = obj.get('attributedTo') if isinstance(obj.get('attributedTo'), str) else 'https://pod.example/@show'
    episode = castopod_episode_url(obj, author)
    if episode is not None:
        assert _is_http_url(episode) and len(episode) <= 1024
        parts = urlsplit(episode)
        assert parts.hostname and parts.hostname == host_of(author), (
            f'episode {episode!r} is not on the author\'s host {host_of(author)!r}')
        assert _EPISODE_PATH.fullmatch(parts.path) and not parts.query

    urls = _note_urls(obj)
    assert isinstance(urls, set) and all(isinstance(url, str) and url for url in urls)


# ---------------------------------------------------------------------------
# Ban expiry
# ---------------------------------------------------------------------------

def check_ban_expiry(data: bytes) -> None:
    """parse_ban_expiry: None, or a timezone-aware datetime that has not passed yet, for any value of `expires`
    or `endTime`. It is documented never to raise: a raise would discard the whole ban."""
    value = _json_or_text(data)
    for activity in ({'expires': value}, {'endTime': value}, _as_object(value)):
        expires = parse_ban_expiry(activity)
        if expires is not None:
            assert isinstance(expires, datetime), f'parse_ban_expiry gave a {type(expires).__name__}'
            assert expires.utcoffset() is not None, 'parse_ban_expiry gave a naive datetime'
            # The function compared with its own now; a few seconds of slack for the time since.
            assert expires > datetime.now(timezone.utc) - timedelta(seconds=5), f'{expires} has passed'


# ---------------------------------------------------------------------------
# Addressing and ids
# ---------------------------------------------------------------------------

VISIBILITY_LABELS = frozenset({'public', 'unlisted', 'followers', 'direct'})


def _addressing(obj, field) -> list:
    value = obj.get(field) if isinstance(obj, dict) else None
    if isinstance(value, str):
        return [value]
    return [entry for entry in value if isinstance(entry, str)] if isinstance(value, list) else []


def check_addressing(data: bytes) -> None:
    """activitypub_visibility, addressees_of, announce_target_uri and is_top_level, given any JSON value.

    Visibility is always one of its four labels, and the one the object's own to/cc imply. addressees_of is
    every non-empty string (or object's string id) in to, cc, audience, bto and bcc, in that order.
    announce_target_uri is None or a non-empty string the activity's object names; is_top_level is a bool, False
    for a reply."""
    value = _json_or_text(data)

    label = activitypub_visibility(value)
    assert label in VISIBILITY_LABELS, f'visibility {label!r}'
    to, cc = _addressing(value, 'to'), _addressing(value, 'cc')
    if any(address in AS_PUBLIC for address in to):
        expected = 'public'
    elif any(address in AS_PUBLIC for address in cc):
        expected = 'unlisted'
    elif any(address.endswith('/followers') for address in to + cc):
        expected = 'followers'
    else:
        expected = 'direct'
    assert label == expected, f'visibility {label!r}, to/cc say {expected!r}'

    addressees = addressees_of(value)
    expected_addressees = []
    if isinstance(value, dict):
        for field in ('to', 'cc', 'audience', 'bto', 'bcc'):
            field_value = value.get(field)
            for item in field_value if isinstance(field_value, list) else [field_value]:
                item = item.get('id') if isinstance(item, dict) else item
                if isinstance(item, str) and item:
                    expected_addressees.append(item)
    assert addressees == expected_addressees, f'addressees_of = {addressees!r}'

    target = announce_target_uri(value)
    if target is not None:
        obj = value.get('object')
        assert isinstance(target, str) and target and target in (obj, _id_of(obj))
    top = is_top_level(value)
    assert isinstance(top, bool)
    if top:
        assert isinstance(value, dict) and not value.get('inReplyTo')


_TOKEN_CHARS = frozenset(random_chars)


def check_ids(data: bytes) -> None:
    """_id_of, _attributed_ids, ensure_domains_match, _is_tombstone, activity_dedupe_key and stable_create_token.

    _id_of is a string the value names or ''; _attributed_ids only non-empty strings. ensure_domains_match is True
    only when the object's id and its actor (or attributedTo) are strings whose hosts are non-empty and equal.
    _is_tombstone is a bool for any body, however deeply nested, and True only for a Tombstone document whose id
    is _same_id as the one asked for. The dedupe key is namespaced and carries the actor's host; the create token
    is 15 characters of gibberish()'s alphabet and depends only on the object id."""
    first, body = _fields(data, 2)
    object_id = _text(first)
    value = _json_or_text(body)

    identifier = _id_of(value)
    assert isinstance(identifier, str) and identifier in ('', value, value.get('id') if isinstance(value, dict) else '')
    attributed = _attributed_ids(value)
    assert isinstance(attributed, list) and all(isinstance(entry, str) and entry for entry in attributed)

    if isinstance(value, dict):
        note_id = value.get('id') if 'id' in value else None
        note_actor = None
        if 'actor' in value:
            note_actor = value['actor']
        elif 'attributedTo' in value:
            attributed_to = value['attributedTo']
            if isinstance(attributed_to, str):
                note_actor = attributed_to
            elif isinstance(attributed_to, list):
                for entry in attributed_to:
                    if isinstance(entry, dict) and entry.get('type') in ('Person', 'Podcast'):
                        note_actor = entry.get('id')
                        break
                    if isinstance(entry, str):
                        note_actor = entry
                        break
        matches = ensure_domains_match(value)
        expected = (isinstance(note_id, str) and isinstance(note_actor, str) and bool(note_id) and
                    bool(note_actor) and bool(host_of(note_id)) and host_of(note_id) == host_of(note_actor))
        assert matches is expected, f'ensure_domains_match = {matches!r}, hosts say {expected!r}'

    tombstone = _is_tombstone(body, object_id)
    assert isinstance(tombstone, bool)
    if tombstone:
        document = json.loads(body)
        assert isinstance(document, dict) and document.get('type') == 'Tombstone'
        assert _same_id(_id_of(document.get('id')), object_id)

    key = activity_dedupe_key(value, object_id)
    assert key == f'ap-dedupe:{host_of(object_id)}:{value}', f'dedupe key {key!r}'

    token = stable_create_token(object_id)
    assert len(token) == 15 and set(token) <= _TOKEN_CHARS and token == stable_create_token(object_id)


# ---------------------------------------------------------------------------
# Entitlement: keyId owners and exempt paths
# ---------------------------------------------------------------------------

def _reference_path_matches(path: str, prefixes) -> bool:
    return any(path.startswith(prefix) if prefix.endswith('/') else (path == prefix or path.startswith(prefix + '/'))
               for prefix in prefixes)


def check_entitlement(data: bytes) -> None:
    """_owner_candidates, _path_matches and signed_fetch_exempt.

    A keyId gives at most four candidate actor ids, distinct, none with a fragment, every one on the keyId's own
    host -- a candidate on another host would let a signature be checked against an actor the keyId does not
    name -- and none at all when the keyId has no host. _path_matches matches a prefix ending in '/' by prefix and
    any other exactly or as a whole leading path segment; signed_fetch_exempt is that over its own prefixes."""
    first, second = _fields(data, 2)
    key_id, path = _text(first), _text(second)

    candidates = _owner_candidates(key_id)
    assert isinstance(candidates, list) and len(candidates) <= 4, f'{len(candidates)} candidates'
    assert len(set(candidates)) == len(candidates), f'repeated candidates {candidates!r}'
    owner_host = host_of(key_id.split('#', 1)[0].strip())
    if not owner_host:
        assert candidates == []
    for candidate in candidates:
        assert isinstance(candidate, str) and '#' not in candidate.split('?', 1)[0]
        assert host_of(candidate) == owner_host, (
            f'candidate {candidate!r} is not on the keyId host {owner_host!r}')

    assert signed_fetch_exempt(path) is _reference_path_matches(path, SIGNED_FETCH_EXEMPT_PREFIXES)
    prefixes = [part for part in key_id.split(' ') if part][:4]
    assert _path_matches(path, prefixes) is _reference_path_matches(path, prefixes)


# None is budgeted: no regex here has a backtracking shape (the one to watch, _CASTOPOD_EPISODE_LINK, is T3's
# timed target). Only the inbox target needs an app: delivery_inbox_of logs through current_app.logger.
TARGETS = [
    Target(name='ap_activity_types', check=check_activity_types, surface='ap_json', budget_ms=None, needs_app=False),
    Target(name='ap_hosts', check=check_hosts, surface='ap_json', budget_ms=None, needs_app=False),
    Target(name='ap_inboxes', check=check_inboxes, surface='ap_json', budget_ms=None, needs_app=True),
    Target(name='ap_content_warning', check=check_content_warning, surface='ap_json', budget_ms=None,
           needs_app=False),
    Target(name='ap_language_variants', check=check_language_variants, surface='ap_json', budget_ms=None,
           needs_app=False),
    Target(name='ap_media_urls', check=check_media_urls, surface='ap_json', budget_ms=None, needs_app=False),
    Target(name='ap_ban_expiry', check=check_ban_expiry, surface='ap_json', budget_ms=None, needs_app=False),
    Target(name='ap_addressing', check=check_addressing, surface='ap_json', budget_ms=None, needs_app=False),
    Target(name='ap_ids', check=check_ids, surface='ap_json', budget_ms=None, needs_app=False),
    Target(name='ap_entitlement', check=check_entitlement, surface='ap_json', budget_ms=None, needs_app=False),
]
