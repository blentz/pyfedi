"""C2: inbound quote posts (docs/superpowers/specs/2026-10-12-ap-interop-c2-quote-posts-design.md)."""
import pytest

from app.activitypub.util import quote_id_from

OWN = 'https://remote.test/notes/1'
QUOTED = 'https://other.test/notes/9'


@pytest.mark.parametrize('key', ['quote', 'quoteUrl', 'quoteUri', '_misskey_quote'])
def test_each_key_is_read(key):
    assert quote_id_from({'id': OWN, key: QUOTED}) == QUOTED


def test_quote_wins_over_the_legacy_keys():
    obj = {'id': OWN, '_misskey_quote': 'https://a.test/1', 'quoteUri': 'https://b.test/1',
           'quoteUrl': 'https://c.test/1', 'quote': QUOTED}
    assert quote_id_from(obj) == QUOTED


def test_an_invalid_first_key_falls_through():
    assert quote_id_from({'id': OWN, 'quote': 42, 'quoteUrl': QUOTED}) == QUOTED


def test_dict_form_uses_its_id():
    assert quote_id_from({'id': OWN, 'quote': {'id': QUOTED, 'type': 'Note'}}) == QUOTED


@pytest.mark.parametrize('value', [
    None, '', 42, [QUOTED], {'id': 7}, {}, 'ftp://other.test/1', 'https:///nohost', 'javascript:alert(1)',
    'https://other.test/' + 'a' * 240, 'https://ötherr.test/1', 'https://other.test/a b',
    'https://other.test/\x00', 'https://[::1/', 'other.test/1',
])
def test_hostile_values_are_refused(value):
    assert quote_id_from({'id': OWN, 'quote': value}) is None


def test_exactly_255_characters_is_accepted():
    url = 'https://other.test/' + 'a' * (255 - len('https://other.test/'))
    assert quote_id_from({'id': OWN, 'quote': url}) == url


def test_a_self_quote_is_none():
    assert quote_id_from({'id': OWN, 'quote': OWN}) is None


def test_no_quote_is_none():
    assert quote_id_from({'id': OWN}) is None


def test_columns_exist(app, db_session):
    from app.models import Post, PostReply
    assert Post.quote_ap_id.type.length == 255
    assert PostReply.quote_ap_id.type.length == 255
