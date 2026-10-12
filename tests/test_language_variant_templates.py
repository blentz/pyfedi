"""C3: templates render the viewer's language variant (post page, comments, teasers in both themes)."""
import pytest
from bs4 import BeautifulSoup
from flask import g, render_template_string

from app import cache, db
from app.models import Language
from tests.factories import (make_community, make_community_member, make_instance, make_post, make_post_reply,
                             make_user)

PEER = 'remote.test'
FR_POST = {'title': 'Bonjour', 'body_html': '<p>bonjour</p>', 'cw': None}
FR_REPLY = {'title': None, 'body_html': '<p>salut</p>', 'cw': None}


@pytest.fixture
def page_world(app, db_session, monkeypatch):
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


def page(app, world, path):
    return BeautifulSoup(logged_in_client(app, world['viewer']).get(path).get_data(as_text=True), 'html.parser')


def test_post_page_shows_the_requested_variant_and_the_switch(page_world, app):
    soup = page(app, page_world, f"/post/{page_world['post'].id}?lang=fr")
    title = soup.select_one('h1.post_title')
    assert title.get_text(strip=True) == 'Bonjour' and title['lang'] == 'fr'
    assert 'bonjour' in soup.select_one('.post_body').get_text()
    switch = soup.select_one('p.language_switch')
    assert [a['href'] for a in switch.select('a')] == ['?lang=en'] and 'English' in switch.get_text()


def test_post_page_default_has_a_switch_to_the_variant(page_world, app):
    soup = page(app, page_world, f"/post/{page_world['post'].id}")
    assert soup.select_one('h1.post_title').get_text(strip=True) == 'Hello'
    assert soup.select_one('p.language_switch a')['href'] == '?lang=fr'


def test_no_switch_without_variants(page_world, app):
    page_world['post'].language_variants = page_world['reply'].language_variants = None
    db.session.commit()
    assert page(app, page_world, f"/post/{page_world['post'].id}").select('p.language_switch') == []


def test_post_page_variant_body_stays_inside_the_warning(page_world, app):
    page_world['post'].content_warning = 'spoilers'
    db.session.commit()
    details = page(app, page_world, f"/post/{page_world['post'].id}?lang=fr").select_one('details.content_warning')
    assert details.summary.get_text(strip=True) == 'spoilers' and 'bonjour' in details.get_text()


def test_comment_shows_its_variant_and_a_switch_with_its_anchor(page_world, app):
    reply = page_world['reply']
    soup = page(app, page_world, f"/post/{page_world['post'].id}?lang=fr")
    comment = soup.select_one(f'#comment_{reply.id}')
    assert 'salut' in comment.get_text()
    assert [a['href'] for a in comment.select('p.language_switch a')] == [f'?lang=en#comment_{reply.id}']


TEASER = ("{% from '{theme}post/post_teaser/_macros.html' import render_{kind} %}"
          "{{ render_{kind}(post, blur_content=False, current_user=current_user, locale='en', request=request, "
          "user_pronouns={}, reported_posts=[], admin_ids=[], user_flair={}, user_notes={}, "
          "moderated_community_ids=[], joined_communities=[], communities_banned_from_list=[]) }}")


@pytest.mark.parametrize('theme', ['', 'themes/dillo/'])
@pytest.mark.parametrize('kind', ['article', 'event', 'link', 'poll', 'image'])
def test_teaser_shows_the_variant_paragraph_without_a_switch(page_world, app, theme, kind):
    source = TEASER.replace('{theme}', theme).replace('{kind}', kind)
    with app.test_request_context('/?lang=fr'):
        html = render_template_string(source, post=page_world['post'])
    assert 'bonjour' in html and '<p>hello</p>' not in html and 'language_switch' not in html


@pytest.mark.parametrize('theme', ['', 'themes/dillo/'])
def test_teaser_title_is_the_variant_title(page_world, app, theme):
    source = (f"{{% from '{theme}post/post_teaser/_macros.html' import render_title %}}"
              "{{ render_title(post, current_user=current_user, request=request, locale='en', "
              "user_pronouns={}, reported_posts=[], admin_ids=[], user_flair={}, user_notes={}) }}")
    with app.test_request_context('/?lang=fr'):
        soup = BeautifulSoup(render_template_string(source, post=page_world['post']), 'html.parser')
    heading = soup.select('h3')[-1]
    assert 'Bonjour' in heading.get_text() and heading['lang'] == 'fr'


@pytest.mark.parametrize('theme', ['', 'themes/dillo/'])
def test_teaser_warning_hides_the_variant_body(page_world, app, theme):
    page_world['post'].content_warning = 'spoilers'
    source = TEASER.replace('{theme}', theme).replace('{kind}', 'article')
    with app.test_request_context('/?lang=fr'):
        html = render_template_string(source, post=page_world['post'])
    assert 'spoilers' in html and 'bonjour' not in html

