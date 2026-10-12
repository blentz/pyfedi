"""C3: multi-language objects (docs/superpowers/specs/2026-10-12-ap-interop-c3-multi-language-design.md)."""
import pytest

from app import db
from app.activitypub.util import (MAX_LANGUAGE_VARIANTS, language_variants_from, object_language,
                                  primary_subtag)
from app.models import Language, Post, PostReply

QUOTED = 'https://other.test/notes/9'


@pytest.fixture(autouse=True)
def _app_context(app):
    """The sanitiser is cached, so every reader runs inside the app."""


def _obj(**fields):
    obj = {'id': 'https://remote.test/notes/1', 'type': 'Note', 'content': '<p>hello</p>'}
    obj.update(fields)
    return obj


@pytest.mark.parametrize('value,expected', [
    ('en', 'en'), ('pt-BR', 'pt'), ('ZH-Hant-TW', 'zh'), ('ast', 'ast'),
    ('e', None), ('engl', None), ('en_US', None), ('', None), (5, None), (None, None), ('<b>', None),
])
def test_primary_subtag(value, expected):
    assert primary_subtag(value) == expected


def test_no_map_is_nothing():
    assert language_variants_from(_obj()) == (None, None)


@pytest.mark.parametrize('bad', [None, 'en', [], {}, {'en': 5}, {5: 'x'}, {'not a tag': '<p>x</p>'}])
def test_an_unusable_map_is_nothing(bad):
    assert language_variants_from(_obj(contentMap=bad)) == (None, None)


def test_mastodon_single_key_map_has_no_variants():
    assert language_variants_from(_obj(contentMap={'de': '<p>hello</p>'})) == ('de', None)


def test_the_key_matching_content_is_the_default_whatever_its_position():
    code, variants = language_variants_from(_obj(contentMap={'fr': '<p>bonjour</p>', 'en': '<p>hello</p>'}))
    assert code == 'en' and set(variants) == {'fr'}
    assert variants['fr'] == {'title': None, 'body_html': '<p>bonjour</p>', 'cw': None}


def test_without_a_match_the_language_property_is_the_default():
    obj = _obj(content='<p>other</p>', language={'identifier': 'fr', 'name': 'French'},
               contentMap={'en': '<p>hello</p>', 'fr': '<p>bonjour</p>'})
    code, variants = language_variants_from(obj)
    assert code == 'fr' and set(variants) == {'en'}


def test_without_a_match_or_language_the_first_valid_key_is_the_default():
    code, variants = language_variants_from(_obj(content='<p>other</p>',
                                                 contentMap={'x y': '<p>?</p>', 'es': '<p>hola</p>', 'en': '<p>hi</p>'}))
    assert code == 'es' and set(variants) == {'en'}


def test_region_keys_collapse_and_the_first_wins():
    code, variants = language_variants_from(_obj(contentMap={'en': '<p>hello</p>', 'pt-BR': '<p>olá</p>',
                                                             'pt-PT': '<p>olá PT</p>'}))
    assert code == 'en' and variants['pt']['body_html'] == '<p>olá</p>'


def test_name_and_summary_maps_fill_title_and_warning():
    obj = _obj(contentMap={'en': '<p>hello</p>', 'de': '<p>hallo</p>'},
               nameMap={'en': 'Title', 'de': ' Titel &amp; mehr '}, summaryMap={'de': '<b>Achtung</b>'})
    _, variants = language_variants_from(obj)
    assert variants['de'] == {'title': 'Titel & mehr', 'body_html': '<p>hallo</p>', 'cw': 'Achtung'}


def test_a_page_summary_without_sensitive_is_no_warning():
    obj = _obj(type='Page', contentMap={'en': '<p>hello</p>', 'de': '<p>hallo</p>'}, summaryMap={'de': 'desc'})
    assert language_variants_from(obj)[1]['de']['cw'] is None


def test_variant_html_is_sanitised_and_quote_fallback_stripped():
    obj = _obj(contentMap={'en': '<p>hello</p>',
                           'de': f'<p>hallo<script>x()</script><img src=x onerror=alert(1)></p>'
                                 f'<p>RE: <a href="{QUOTED}">{QUOTED}</a></p>'})
    body = language_variants_from(obj, QUOTED)[1]['de']['body_html']
    assert '<script' not in body and 'onerror' not in body and QUOTED not in body and 'hallo' in body


def test_bare_text_is_wrapped_in_a_paragraph():
    assert language_variants_from(_obj(contentMap={'en': '<p>hello</p>', 'de': 'hallo'}))[1]['de']['body_html'] \
        == '<p>hallo</p>'


def test_an_empty_or_oversized_variant_is_dropped():
    obj = _obj(contentMap={'en': '<p>hello</p>', 'de': '<p> </p>', 'fr': '<p>' + 'a' * 100_000 + '</p>',
                           'es': '<p>hola</p>'})
    assert set(language_variants_from(obj)[1]) == {'es'}


def test_variants_are_capped():
    codes = ['de', 'fr', 'es', 'it', 'nl', 'pl', 'ja', 'zh', 'ko', 'ru', 'sv', 'fi']
    obj = _obj(contentMap={'en': '<p>hello</p>', **{c: f'<p>{c}</p>' for c in codes}})
    assert list(language_variants_from(obj)[1]) == codes[:MAX_LANGUAGE_VARIANTS]


@pytest.fixture
def languages(app, db_session):
    for code, name in (('en', 'English'), ('de', 'Deutsch')):
        if not Language.query.filter_by(code=code).first():
            db.session.add(Language(code=code, name=name))
    db.session.commit()


def test_object_language_uses_the_language_property_when_it_agrees(languages):
    lang = object_language(_obj(language={'identifier': 'eo', 'name': 'Esperanto'}), None)
    assert (lang.code, lang.name) == ('eo', 'Esperanto')


def test_object_language_prefers_the_map_code_over_a_disagreeing_property(languages):
    assert object_language(_obj(language={'identifier': 'en', 'name': 'English'}), 'de').code == 'de'


def test_object_language_for_an_unknown_map_code_is_none(languages):
    assert object_language(_obj(), 'xx') is None


def test_object_language_with_nothing_is_none(languages):
    assert object_language(_obj(), None) is None


def test_columns_exist(app, db_session):
    assert Post.language_variants.type.__class__.__name__ == 'JSONB'
    assert PostReply.language_variants.type.__class__.__name__ == 'JSONB'
