"""Signed onion media proxy URLs and their cache file names (onion media proxy spec 2026-10-19, section 3.2)."""
import pytest

from app.onion_media import EXTENSIONS, MEDIA_SUFFIXES, cache_name, cached_file, kind, proxy_url, remote_for

URLS = ['https://lemmy.zip/pictrs/image/a.webp', 'https://example.org/p/é中.png?w=1&x=ü']


def _split(url):
    return url.split('/')[2:4]


def _flip(text):
    return text[:-1] + ('0' if text[-1] != '0' else '1')


@pytest.mark.parametrize('remote', URLS)
def test_round_trip(app, remote):
    url = proxy_url(remote)
    assert url.startswith('/onion_media/')
    assert '=' not in url
    assert remote_for(*_split(url)) == remote


def test_tampered_signature_or_key_is_none(app):
    sig, key = _split(proxy_url(URLS[0]))
    assert remote_for(_flip(sig), key) is None
    assert remote_for(sig, _flip(key)) is None
    assert remote_for(sig, 'a') is None            # bad base64 length
    assert remote_for(sig, '_w') is None           # decodes to non-UTF-8 byte 0xff
    assert remote_for('', '') is None
    assert remote_for(sig[:-2], key) is None       # wrong length


def test_signature_depends_on_secret_key(app, monkeypatch):
    sig, key = _split(proxy_url(URLS[0]))
    monkeypatch.setitem(app.config, 'SECRET_KEY', 'another-secret')
    assert remote_for(sig, key) is None


def test_cache_name_uses_the_type_extension(app):
    assert cache_name(URLS[0], 'image/avif').endswith('.avif')
    assert len(cache_name(URLS[0], 'image/avif')) == 64 + len('.avif')
    assert cache_name(URLS[0], 'image/avif') != cache_name(URLS[1], 'image/avif')
    with pytest.raises(KeyError):
        cache_name(URLS[0], 'image/svg+xml')


def test_cached_file_finds_any_allowed_extension(app, tmp_path, monkeypatch):
    monkeypatch.setitem(app.config, 'MEDIA_PROXY_DIR', str(tmp_path))
    assert cached_file(URLS[0]) is None
    path = tmp_path / cache_name(URLS[0], 'video/webm')
    path.write_bytes(b'x')
    assert cached_file(URLS[0]) == path
    assert cached_file(URLS[1]) is None
    assert set(EXTENSIONS) >= {'image/jpeg', 'audio/flac'}


def test_every_stored_extension_is_a_media_suffix_of_its_kind():
    """A URL that names a file the proxy would store is proxied and guessed as the same kind."""
    for content_type, extension in EXTENSIONS.items():
        assert MEDIA_SUFFIXES[extension] == kind(content_type)
    assert EXTENSIONS['video/quicktime'] == '.mov' and EXTENSIONS['audio/aac'] == '.aac'
    assert {'.jpeg', '.jpe', '.jfif', '.m4v', '.oga', '.opus'} <= set(MEDIA_SUFFIXES)
