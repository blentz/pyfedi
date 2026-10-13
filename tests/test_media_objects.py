"""C4: Create of Audio, Image, Document and Video as posts
(docs/superpowers/specs/2026-10-12-ap-interop-c4-media-objects-design.md)."""
import pytest

from app.activitypub.util import (DISPATCHED_AP_TYPES, MAX_MEDIA_URL_ENTRIES, MEDIA_OBJECT_TYPES, MEDIA_URL_KIND,
                                  _single_type, media_url_from)

AUDIO = 'https://tunes.example/api/v1/listen/abc/?upload=1'
PAGE = 'https://tunes.example/library/tracks/1'
IMAGE = 'https://pics.example/media/1'


@pytest.fixture(autouse=True)
def _app_context(app):
    """url_is_storable needs no app, but keep parity with the C3 tests' harness."""
    yield


def test_constants():
    assert MEDIA_OBJECT_TYPES == ('Audio', 'Document', 'Image', 'Video')
    assert MEDIA_URL_KIND == {'Image': 'image', 'Audio': 'audio', 'Document': None}
    assert MAX_MEDIA_URL_ENTRIES == 20
    assert {'Audio', 'Document', 'Image', 'Video'} <= DISPATCHED_AP_TYPES


def test_an_array_type_resolves_to_the_media_type():
    assert _single_type(['Audio', 'schema:AudioObject']) == 'Audio'


def test_a_bare_string_url():
    assert media_url_from({'url': IMAGE}, 'image') == IMAGE


def test_a_link_dict_with_href():
    assert media_url_from({'url': {'type': 'Link', 'href': AUDIO, 'mediaType': 'audio/ogg'}}, 'audio') == AUDIO


def test_a_dict_with_url():
    assert media_url_from({'url': {'url': IMAGE}}, 'image') == IMAGE


def test_a_matching_media_type_beats_an_earlier_entry():
    obj = {'url': [{'type': 'Link', 'href': PAGE, 'mediaType': 'text/html'},
                   {'type': 'Link', 'href': AUDIO, 'mediaType': 'audio/mpeg'}]}
    assert media_url_from(obj, 'audio') == AUDIO


def test_an_entry_of_another_kind_is_skipped_but_one_without_a_media_type_is_the_fallback():
    obj = {'url': [{'href': PAGE, 'mediaType': 'text/html'}, {'href': AUDIO}]}
    assert media_url_from(obj, 'audio') == AUDIO


def test_only_another_kind_is_none():
    assert media_url_from({'url': [{'href': PAGE, 'mediaType': 'text/html'}]}, 'audio') is None


def test_no_kind_takes_the_first_usable_entry():
    obj = {'url': [{'href': 'javascript:x()'}, {'href': PAGE, 'mediaType': 'text/html'}, {'href': AUDIO}]}
    assert media_url_from(obj, None) == PAGE


@pytest.mark.parametrize('value', [
    'javascript:alert(1)', 'data:image/png;base64,AAAA', '/relative/path.png', 'ftp://pics.example/1.png',
    '', None, 5, ['x'], {'href': 5}, [{'href': None}], {}, [],
])
def test_hostile_urls_are_refused(value):
    assert media_url_from({'url': value}, 'image') is None


def test_no_url_key_is_none():
    assert media_url_from({}, None) is None


def test_only_the_first_20_entries_are_read():
    junk = [{'href': PAGE, 'mediaType': 'text/html'}] * MAX_MEDIA_URL_ENTRIES
    assert media_url_from({'url': junk + [{'href': AUDIO, 'mediaType': 'audio/ogg'}]}, 'audio') is None


def test_a_non_string_media_type_counts_as_none():
    assert media_url_from({'url': [{'href': AUDIO, 'mediaType': 7}]}, 'audio') == AUDIO


def _url_of_length(n):
    base = 'https://pics.example/'
    return base + 'a' * (n - len(base))


def test_a_url_over_the_post_url_width_is_skipped_for_a_later_shorter_entry():
    assert media_url_from({'url': _url_of_length(2048)}, 'audio') == _url_of_length(2048)
    assert media_url_from({'url': _url_of_length(2049)}, 'audio') is None
    assert media_url_from({'url': [_url_of_length(2049), AUDIO]}, 'audio') == AUDIO


def test_an_image_url_over_the_file_source_url_width_is_skipped_for_a_later_shorter_entry():
    assert media_url_from({'url': _url_of_length(1024)}, 'image') == _url_of_length(1024)
    assert media_url_from({'url': _url_of_length(1025)}, 'image') is None
    assert media_url_from({'url': [_url_of_length(1025), IMAGE]}, 'image') == IMAGE
    assert media_url_from({'url': _url_of_length(1025)}, None) == _url_of_length(1025)  # a Document is no File row


@pytest.mark.parametrize('kind, length', [(None, 2049), ('audio', 2049), ('image', 1025)])
def test_a_url_longer_than_its_column_is_refused_before_its_host_is_read(monkeypatch, kind, length):
    """url_is_storable IDNA-maps the host, about 10 microseconds a character, and was asked before the length
    cap, so a peer could make each of 20 entries a megabytes-long host (MAX_CONTENT_LENGTH is 100 MB) and hold a
    worker for minutes per object. Measured: a 100,000-character host took one second. Found as the slowest unit
    of the ap_media_urls fuzz target."""
    from app.activitypub import util

    asked = []
    monkeypatch.setattr(util, 'url_is_storable', lambda url: asked.append(url) or True)
    url = 'https://' + 'a' * (length - len('https://'))

    assert media_url_from({'url': [url] * 3}, kind) is None
    assert asked == []
