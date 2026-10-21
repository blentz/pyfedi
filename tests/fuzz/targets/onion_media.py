"""Property checks for the onion remote-media proxy (app/storefront.py rewrite_links and its helpers, app/onion_media.py).

On an onion storefront every remote media URL a page carries is pointed at the signed proxy, and the server fetches
only URLs it signed. Four checks hold that to account:

* onion_media_rewrite: random HTML through rewrite_links on an onion DomainInfo. No exception; no iframe, embed or
  object with a remote src or data survives; every src, poster, data-* and srcset candidate of a media element, and
  every url(...) in a style, is local or the proxy's shape, and remote_for reads each proxied value back as an
  absolute http(s) URL whose proxy URL is that same value.
* onion_media_signature: random (sig, key) gives remote_for a str or None without raising, and a str only when the
  signature is the one proxy_url makes for it. A genuine proxy URL of random text round-trips.
* onion_media_fetchable: _fetchable never raises and returns None or an absolute http(s) URL with a host, in a
  spelling a browser and the proxy's client read alike.
* onion_media_style: _proxied_style never raises and returns None or a style whose every url(...) is local or
  proxy-shaped.

No database and no network. The domain registry is swapped for one with SERVER_NAME alone (_no_domain_rows) and S3 is
unset for the duration, so the rewrite reads neither the database nor the S3 host.
"""

import re
from contextlib import contextmanager
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from flask import current_app

import app.storefront as storefront
from app.onion_media import proxy_url, remote_for
from app.storefront import DomainInfo
from tests.fuzz.targets._target import Target
from tests.fuzz.targets.signatures import _fields, _text
from tests.fuzz.targets.urls import _no_domain_rows

MAX_INPUT = 4096
ONION = DomainInfo('fuzzfuzzfuzzfuzz.onion', 'http', False, 'admin', False, True, None, 2)
PROXY_PATH = re.compile(r'/onion_media/([0-9a-f]{64})/([A-Za-z0-9_-]*)')
_MEDIA_URL_ATTRS = ('src', 'poster')


@contextmanager
def _s3_unset():
    config = current_app.config
    had, saved = 'S3_PUBLIC_URL' in config, config.get('S3_PUBLIC_URL')
    config['S3_PUBLIC_URL'] = ''
    try:
        yield
    finally:
        if had:
            config['S3_PUBLIC_URL'] = saved
        else:
            del config['S3_PUBLIC_URL']


def _assert_proxied_or_local(value: str, where: str) -> None:
    match = PROXY_PATH.fullmatch(value)
    if match:
        remote = remote_for(*match.groups())
        assert remote is not None, f'{where}: {value!r} is proxy-shaped but not signed'
        assert proxy_url(remote) == value, f'{where}: {value!r} does not round-trip'
        parts = urlsplit(remote)
        assert parts.scheme in ('http', 'https') and parts.netloc, f'{where}: proxied {remote!r} is not absolute http(s)'
        return
    assert not storefront._is_remote(value), f'{where}: remote {value!r} left as written'


def check_onion_media_rewrite(data: bytes) -> None:
    body = _text(data[:MAX_INPUT])
    with _no_domain_rows(), _s3_unset():
        out = storefront.rewrite_links(body, ONION)
    assert isinstance(out, str)
    soup = BeautifulSoup(out, 'html.parser', on_duplicate_attribute='ignore')
    for tag in soup.find_all(True):
        for name in ('src', 'data'):
            if tag.name in storefront._EMBED_TAGS and isinstance(tag.get(name), str):
                assert not storefront._is_remote(tag[name]), f'{tag.name} {name}={tag[name]!r} survived'
        if tag.name in storefront._PROXIED_TAGS:
            for name, value in tag.attrs.items():
                if not isinstance(value, str):
                    continue
                if name in _MEDIA_URL_ATTRS or name.startswith('data-'):
                    _assert_proxied_or_local(value, f'<{tag.name} {name}>')
                elif name == 'srcset':
                    for start, end in storefront._srcset_urls(value):
                        _assert_proxied_or_local(value[start:end], f'<{tag.name} srcset>')
        style = tag.get('style')
        if isinstance(style, str):
            for url in storefront._css_urls(style):
                _assert_proxied_or_local(url.strip('\'" '), f'<{tag.name} style>')


def check_onion_media_signature(data: bytes) -> None:
    sig, key = _fields(data[:MAX_INPUT], 2)
    sig, key = _text(sig), _text(key)
    remote = remote_for(sig, key)
    assert remote is None or isinstance(remote, str), f'remote_for gave {remote!r}'
    if remote is not None:
        assert PROXY_PATH.fullmatch(proxy_url(remote)).group(1) == sig, f'{sig!r}/{key!r} verified as {remote!r}'
    genuine = proxy_url(_text(data[:MAX_INPUT]))
    assert remote_for(*PROXY_PATH.fullmatch(genuine).groups()) == _text(data[:MAX_INPUT])


def check_onion_media_fetchable(data: bytes) -> None:
    url = _text(data[:MAX_INPUT])
    fetched = storefront._fetchable(url)
    if fetched is None:
        return
    parts = urlsplit(fetched)
    assert parts.scheme in ('http', 'https') and parts.netloc, f'_fetchable({url!r}) = {fetched!r}'
    assert fetched.startswith(f'{parts.scheme}://'), f'_fetchable({url!r}) = {fetched!r}'
    assert '\\' not in fetched and fetched == fetched.strip(), f'_fetchable({url!r}) = {fetched!r}'


def check_onion_media_style(data: bytes) -> None:
    style = _text(data[:MAX_INPUT])
    with _no_domain_rows(), _s3_unset():
        out = storefront._proxied_style(style)
        assert out is None or isinstance(out, str)
        if out is not None:
            for url in storefront._css_urls(out):
                _assert_proxied_or_local(url.strip('\'" '), 'style')


TARGETS = [
    Target(name='onion_media_rewrite', check=check_onion_media_rewrite, surface='urls', budget_ms=None, needs_app=True),
    Target(name='onion_media_signature', check=check_onion_media_signature, surface='urls', budget_ms=None,
           needs_app=True),
    Target(name='onion_media_fetchable', check=check_onion_media_fetchable, surface='urls', budget_ms=None,
           needs_app=False),
    Target(name='onion_media_style', check=check_onion_media_style, surface='urls', budget_ms=None, needs_app=True),
]
