"""C3: which language variant a viewer sees."""
from unittest.mock import patch

import pytest
from flask import g
from sqlalchemy import event

from app import db
from app.models import Language
from app.variants import chosen_variant, language_ids, language_names, shown, switch_url
from tests.factories import make_community, make_instance, make_post, make_post_reply, make_user

FR = {'title': 'Bonjour', 'body_html': '<p>bonjour</p>', 'cw': None}
DE = {'title': None, 'body_html': '<p>hallo</p>', 'cw': 'Achtung'}


@pytest.mark.parametrize('requested,viewer,expected', [
    (None, 'fr', ('fr', FR)), (None, 'en', ('en', None)), (None, None, ('en', None)), (None, 'ja', ('en', None)),
    ('de', 'fr', ('de', DE)), ('en', 'fr', ('en', None)), ('xx', 'fr', ('fr', FR)), ('<b>', None, ('en', None)),
])
def test_chosen_variant(requested, viewer, expected):
    assert chosen_variant({'fr': FR, 'de': DE}, 'en', requested, viewer) == expected


@pytest.fixture
def item(app, db_session):
    for code, name in (('en', 'English'), ('fr', 'Français'), ('de', 'Deutsch')):
        if not Language.query.filter_by(code=code).first():
            db.session.add(Language(code=code, name=name))
    instance = make_instance('remote.test')
    user = make_user(instance, 'polyglot')
    community = make_community('variants')
    post = make_post(community, user, 'https://remote.test/post/1')
    post.title, post.body_html, post.content_warning = 'Hello', '<p>hello</p>', None
    post.language = Language.query.filter_by(code='en').first()
    post.language_variants = {'fr': FR, 'de': DE}
    db.session.commit()
    g.pop('language_names', None)
    g.pop('language_ids', None)
    return post


def test_shown_without_variants_is_the_default(item, app):
    item.language_variants = None
    with app.test_request_context('/'):
        assert shown(item) == ('en', 'Hello', '<p>hello</p>', None, [], item.language_id)


def test_shown_follows_the_locale(item, app):
    with app.test_request_context('/'), patch('app.variants.get_locale', return_value='fr_CA'):
        v = shown(item)
    assert (v.code, v.title, v.body_html, v.cw) == ('fr', 'Bonjour', '<p>bonjour</p>', None)
    assert v.others == [('en', 'English'), ('de', 'Deutsch')]
    assert v.language_id == Language.query.filter_by(code='fr').first().id   # the language of the version shown


def test_lang_parameter_wins_and_falls_back_to_the_default_title(item, app):
    with app.test_request_context('/?lang=de'), patch('app.variants.get_locale', return_value='fr'):
        v = shown(item)
    assert (v.code, v.title, v.body_html, v.cw) == ('de', 'Hello', '<p>hallo</p>', 'Achtung')


def test_an_unknown_or_hostile_lang_is_ignored(item, app):
    with app.test_request_context('/?lang=%3Cscript%3E'), patch('app.variants.get_locale', return_value='en'):
        v = shown(item)
    assert v.code == 'en' and all('<' not in code for code, _ in v.others)


def test_the_default_warning_covers_a_variant_without_one(item, app):
    item.content_warning = 'spoilers'
    with app.test_request_context('/?lang=fr'):
        assert shown(item).cw == 'spoilers'


def test_a_comment_has_no_title(item, app):
    reply = make_post_reply(item, item.author, body='hi')
    reply.body_html, reply.language_variants = '<p>hi</p>', {'fr': {**FR, 'title': None}}  # comment variants carry no title
    reply.language = None
    with app.test_request_context('/?lang=fr'):
        v = shown(reply)
    assert v.title is None and v.body_html == '<p>bonjour</p>' and [c for c, _ in v.others] == ['und']


def test_an_unnamed_code_is_listed_by_its_code(item, app):
    item.language_variants = {'eo': FR}
    with app.test_request_context('/'), patch('app.variants.get_locale', return_value='en'):
        assert shown(item).others == [('eo', 'eo')]


def test_shown_outside_a_request_is_the_default(item):
    assert shown(item).code == 'en'


def test_language_names_is_one_query_per_request(item, app):
    with app.test_request_context('/'):
        statements = []
        listener = lambda *args: statements.append(args[2])
        event.listen(db.engine, 'before_cursor_execute', listener)
        try:
            for _ in range(5):
                assert language_names()['fr'] == 'Français'
        finally:
            event.remove(db.engine, 'before_cursor_execute', listener)
    assert len(statements) == 1


def test_language_names_outside_a_request_is_not_cached_in_g(item):
    assert language_names()['de'] == 'Deutsch'


def test_language_ids_is_one_query_per_request_and_uncached_outside_one(item, app):
    expected = Language.query.filter_by(code='fr').first().id
    assert language_ids()['fr'] == expected
    with app.test_request_context('/'):
        statements = []
        listener = lambda *args: statements.append(args[2])
        event.listen(db.engine, 'before_cursor_execute', listener)
        try:
            for _ in range(5):
                assert language_ids()['fr'] == expected
        finally:
            event.remove(db.engine, 'before_cursor_execute', listener)
    assert len(statements) == 1


def test_switch_url_outside_a_request_is_the_bare_parameter():
    assert switch_url('fr', '#comment_4') == '?lang=fr#comment_4'
