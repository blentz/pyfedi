from unittest.mock import patch

import pytest
from flask import current_app, g

from app import db
from app.models import Language, Site
from tests.factories import make_visibility_world, bearer


def client_as(app, user):
    client = app.test_client()
    if user is not None:
        with client.session_transaction() as session:
            session['_user_id'] = str(user.id)
            session['_fresh'] = True
    return client


MISSING = 999999


@pytest.fixture
def world(app, db_session, monkeypatch):
    w = make_visibility_world()
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.admin_ids = []
    g.site = site
    monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.add(Language(code='en', name='English'))
    db.session.commit()
    site.language_id = Language.query.filter_by(code='en').one().id
    return w


@pytest.mark.parametrize('who, status', [('stranger', 404), ('pending', 404), ('follower', 200), (None, 404)])
def test_post_page(app, world, who, status):
    w = world
    client = client_as(app, getattr(w, who) if who else None)
    with patch('app.post.routes.render_template', return_value=app.response_class('rendered')):
        assert client.get(f'/post/{w.post.id}').status_code == status


@pytest.mark.parametrize('path', ['/post/{pid}/options_menu', '/post/{pid}/embed', '/post/{pid}/cross_posts'])
def test_other_post_views_404_for_stranger(app, world, path):
    w = world
    w.post.cross_posts = [w.public_post.id]  # otherwise cross_posts is a 404 for any post
    db.session.commit()
    url = path.format(pid=w.post.id)
    assert client_as(app, w.stranger).get(url).status_code == 404


@pytest.mark.parametrize('who, status', [('stranger', 404), ('follower', 200)])
def test_reply_options(app, world, who, status):
    w = world
    url = f'/post/{w.public_post.id}/comment/{w.reply.id}/options_menu'
    assert client_as(app, getattr(w, who)).get(url).status_code == status


@pytest.mark.parametrize('who, shown', [('stranger', False), ('follower', True)])
def test_reply_source(app, world, who, shown):
    w = world
    url = f'/post/{w.public_post.id}/comment/{w.reply.id}/source/show'
    body = client_as(app, getattr(w, who)).get(url, headers={'HX-Request': 'true'}).get_data(as_text=True)
    assert ('secret reply' in body) is shown


def test_continue_discussion_gates_the_post_only(app, world):
    w = world
    assert client_as(app, w.stranger).get(f'/post/{w.post.id}/comment/{w.reply.id}').status_code == 404


def test_activitypub_post_is_404_even_for_follower_servers(app, world):
    w = world
    w.post.ap_id = None  # treat as local so post_ap serves rather than redirects
    db.session.commit()
    response = app.test_client().get(f'/post/{w.post.id}', headers={'Accept': 'application/activity+json'})
    assert response.status_code == 404


def test_activitypub_comment_is_404(app, world):
    w = world
    w.reply.ap_id = None
    db.session.commit()
    response = app.test_client().get(f'/comment/{w.reply.id}', headers={'Accept': 'application/activity+json'})
    assert response.status_code == 404


def test_api_post_is_a_not_found_for_stranger(app, world):
    w = world
    client = app.test_client()
    ok = client.get('/api/alpha/post', query_string={'id': w.post.id}, headers={'Authorization': bearer(w.follower)})
    assert ok.status_code == 200
    hidden = client.get('/api/alpha/post', query_string={'id': w.post.id}, headers={'Authorization': bearer(w.stranger)})
    missing = client.get('/api/alpha/post', query_string={'id': MISSING}, headers={'Authorization': bearer(w.stranger)})
    assert hidden.status_code == missing.status_code
    assert hidden.get_data() == missing.get_data()
    anon = client.get('/api/alpha/post', query_string={'id': w.post.id})
    assert anon.get_data() == missing.get_data()


def test_api_comment_is_a_not_found_for_stranger(app, world):
    w = world
    client = app.test_client()
    ok = client.get('/api/alpha/comment', query_string={'id': w.reply.id}, headers={'Authorization': bearer(w.follower)})
    assert ok.status_code == 200
    hidden = client.get('/api/alpha/comment', query_string={'id': w.reply.id}, headers={'Authorization': bearer(w.stranger)})
    missing = client.get('/api/alpha/comment', query_string={'id': MISSING}, headers={'Authorization': bearer(w.stranger)})
    assert hidden.status_code == missing.status_code
    assert hidden.get_data() == missing.get_data()
