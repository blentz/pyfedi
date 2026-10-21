"""C3: the translate buttons and routes act on the language variant the viewer sees."""
from unittest.mock import patch

import pytest
from flask import g, render_template_string, session as flask_session
from flask_login import login_user
from flask_wtf.csrf import generate_csrf

from app import cache, db
from app.models import Language
from tests.factories import (make_community, make_community_member, make_instance, make_post, make_post_reply,
                             make_site, make_user)

PEER = 'remote.test'
FR_POST = {'title': 'Bonjour', 'body_html': '<p>bonjour</p>', 'cw': None}
FR_REPLY = {'title': None, 'body_html': '<p>bonjour</p>', 'cw': None}


@pytest.fixture
def world(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)  # the post page's reply form needs csrf_token
    local = make_instance(app.config['SERVER_NAME'], software='piefed')
    cache.clear()
    for code, name in (('en', 'English'), ('fr', 'Français')):
        if not Language.query.filter_by(code=code).first():
            db.session.add(Language(code=code, name=name))
    db.session.commit()
    english = Language.query.filter_by(code='en').first()
    author = make_user(make_instance(PEER), 'polyglot')
    viewer = make_user(local, 'viewer', local=True)
    community = make_community('variants')  # after the users: it is owned by user 1
    make_community_member(author, community)
    post = make_post(community, author, ap_id=f'https://{PEER}/post/1')
    post.title, post.body, post.body_html = 'Hello', 'hello', '<p>hello</p>'
    post.language, post.language_variants = english, {'fr': FR_POST}
    reply = make_post_reply(post, author, body='hi')
    reply.body_html, reply.language, reply.language_variants = '<p>hi</p>', english, {'fr': FR_REPLY}
    db.session.commit()
    g.pop('language_names', None)
    return locals()


def logged_in_client(app, user):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True
    return client


@pytest.fixture
def client(app, world, monkeypatch):
    monkeypatch.setitem(app.config, 'TRANSLATE_ENDPOINT', 'http://lt.test')
    client = logged_in_client(app, world['viewer'])
    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as sess:
        sess['csrf_token'] = raw
    client.environ_base['HTTP_X_CSRFTOKEN'] = token
    return client


def test_post_translate_sends_the_chosen_variant_with_its_language(world, client):
    with patch('app.post.routes.libretranslate_string', side_effect=lambda text, source, target: f'[{source}]{text}') as lt:
        html = client.post(f"/post/{world['post'].id}/translate?lang=fr").get_data(as_text=True)
    assert lt.call_args_list[0].args[0] == '<p>bonjour</p>'
    assert all(call.kwargs['source'] == 'auto' for call in lt.call_args_list)  # a variant's language may not be loaded
    assert 'Bonjour' in html


def test_teaser_translate_sends_the_variant_title(world, client):
    with patch('app.post.routes.libretranslate_string', return_value='Hi') as lt:
        client.post(f"/post_teaser/{world['post'].id}/translate?lang=fr")
    assert lt.call_args.args[0] == 'Bonjour' and lt.call_args.kwargs['source'] == 'auto'


def test_reply_translate_sends_the_variant(world, client):
    with patch('app.post.routes.libretranslate_string', return_value='<p>hi</p>') as lt:
        client.post(f"/post_reply/{world['reply'].id}/translate?lang=fr")
    assert lt.call_args.args[0] == '<p>bonjour</p>' and lt.call_args.kwargs['source'] == 'auto'


def test_without_lang_the_default_is_sent_as_before(world, client):
    with patch('app.post.routes.libretranslate_string', return_value='x') as lt:
        client.post(f"/post/{world['post'].id}/translate")
    assert lt.call_args_list[0].args[0] == world['post'].body_html
    assert lt.call_args_list[0].kwargs['source'] == 'auto'  # English is the site language


def test_a_default_language_other_than_the_sites_is_still_the_source(world, client):
    world['post'].language = Language.query.filter_by(code='fr').first()
    world['post'].language_variants = None
    db.session.commit()
    with patch('app.post.routes.libretranslate_string', return_value='x') as lt:
        client.post(f"/post/{world['post'].id}/translate")
    assert lt.call_args_list[0].kwargs['source'] == 'fr'


def test_an_item_without_a_language_translates_from_auto(world, client):
    world['post'].language = None
    world['post'].language_variants = None
    db.session.commit()
    with patch('app.post.routes.libretranslate_string', return_value='x') as lt:
        client.post(f"/post/{world['post'].id}/translate")
    assert lt.call_args_list[0].kwargs['source'] == 'auto'


def speaks(user, code):
    user.language_id = Language.query.filter_by(code=code).first().id
    db.session.commit()


def french(user):
    speaks(user, 'fr')


def test_the_button_carries_the_shown_language(world, client):
    speaks(world['viewer'], 'en')
    html = client.get(f"/post/{world['post'].id}?lang=fr").get_data(as_text=True)
    assert f"/post/{world['post'].id}/translate?lang=fr" in html
    assert f"/post_reply/{world['reply'].id}/translate?lang=fr" in html


def test_there_is_no_button_when_the_version_shown_is_in_the_viewers_language(world, client):
    french(world['viewer'])
    html = client.get(f"/post/{world['post'].id}?lang=fr").get_data(as_text=True)
    assert f"/post/{world['post'].id}/translate" not in html
    assert f"/post_reply/{world['reply'].id}/translate" not in html


def test_the_button_returns_when_the_version_shown_is_not_in_the_viewers_language(world, client):
    french(world['viewer'])
    html = client.get(f"/post/{world['post'].id}?lang=en").get_data(as_text=True)
    assert f"/post/{world['post'].id}/translate?lang=en" in html
    assert f"/post_reply/{world['reply'].id}/translate?lang=en" in html


def test_the_button_has_no_lang_without_variants(world, client):
    world['post'].language_variants = None
    french(world['viewer'])
    html = client.get(f"/post/{world['post'].id}").get_data(as_text=True)
    assert f"/post/{world['post'].id}/translate\"" in html


@pytest.mark.parametrize('theme', ['', 'themes/dillo/'])
@pytest.mark.parametrize('lang, suffix', [('fr', '?lang=fr'), (None, '')])
def test_teaser_button_carries_the_shown_language(world, app, theme, lang, suffix):
    make_site()
    if lang is None:
        world['post'].language_variants = None
    speaks(world['viewer'], 'fr' if lang is None else 'en')   # the post is English, its French version is shown
    source = (f"{{% from '{theme}post/post_teaser/_macros.html' import render_utilities_bar %}}"
              "{{ render_utilities_bar(post, current_user=current_user, joined_communities=[], "
              "communities_banned_from_list=[], reported_posts=[], moderated_community_ids=[], recently_upvoted=[], recently_downvoted=[]) }}")
    with app.test_request_context('/?lang=fr'):
        login_user(world['viewer'])
        html = render_template_string(source, post=world['post'])
    assert f"/post_teaser/{world['post'].id}/translate{suffix}\"" in html


def test_reply_translation_is_sanitised(world, client):
    with patch('app.post.routes.libretranslate_string', return_value='<p>ok</p><script>x()</script>'):
        html = client.post(f"/post_reply/{world['reply'].id}/translate").get_data(as_text=True)
    assert 'ok' in html and '<script' not in html


def test_teaser_translate_also_translates_the_shown_preview(world, client):
    """The teaser shows the body's first paragraph under the title; translating only the title left it untranslated.
    The preview comes back as an out-of-band swap into the teaser's preview element."""
    with patch('app.post.routes.libretranslate_string', side_effect=lambda text, source, target: f'[{target}]{text}') as lt:
        html = client.post(f"/post_teaser/{world['post'].id}/translate").get_data(as_text=True)
    assert '<p>hello</p>' in [call.args[0] for call in lt.call_args_list]
    assert f'<div id="post_preview_{world["post"].id}" hx-swap-oob="innerHTML">' in html
    assert '[en]' in html.split('hx-swap-oob')[1]


def test_teaser_translate_previews_the_content_warning_when_one_is_shown(world, client):
    world['post'].content_warning = 'spoilers'
    world['post'].language_variants = None
    db.session.commit()
    with patch('app.post.routes.libretranslate_string', side_effect=lambda text, source, target: f'<b>{text}</b>') as lt:
        html = client.post(f"/post_teaser/{world['post'].id}/translate").get_data(as_text=True)
    assert 'spoilers' in [call.args[0] for call in lt.call_args_list]
    assert '<p>&lt;b&gt;spoilers&lt;/b&gt;</p>' in html  # a warning is text, so it is escaped


def test_teaser_translated_preview_is_sanitised(world, client):
    with patch('app.post.routes.libretranslate_string', return_value='<p>ok</p><script>x()</script>'):
        html = client.post(f"/post_teaser/{world['post'].id}/translate").get_data(as_text=True)
    assert 'ok' in html and '<script' not in html


def test_teaser_translate_keeps_a_microblogs_title_hidden(world, client):
    """A microblog teaser's title is visually hidden: its body is the content. The swapped-in title stays hidden."""
    world['community'].name = 'microblogs'
    world['post'].microblog = True
    db.session.commit()
    with patch('app.post.routes.libretranslate_string', return_value='x'):
        html = client.post(f"/post_teaser/{world['post'].id}/translate").get_data(as_text=True)
    assert html.startswith('<h3 class="visually-hidden">')


@pytest.mark.parametrize('theme', ['', 'themes/dillo/'])
@pytest.mark.parametrize('macro', ['render_article', 'render_link', 'render_poll', 'render_image'])
def test_teaser_preview_is_addressable_for_the_translation(world, app, theme, macro):
    make_site()
    source = (f"{{% from '{theme}post/post_teaser/_macros.html' import {macro} %}}"
              f"{{{{ {macro}(post, current_user=current_user, request=request, user_pronouns={{}}, joined_communities=[], "
              "communities_banned_from_list=[], reported_posts=[], moderated_community_ids=[], recently_upvoted=[], recently_downvoted=[]) }}")
    with app.test_request_context('/'):
        login_user(world['viewer'])
        html = render_template_string(source, post=world['post'])
    assert f'id="post_preview_{world["post"].id}"' in html
