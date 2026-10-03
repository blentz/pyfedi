"""D19 restricts seeing, not enforcement: a moderator may remove a followers-only object they cannot view."""
from unittest.mock import patch

import pytest
from flask import current_app, g

from app import db
from app.models import File, Language, ModLog, Post, PostReply, Site
from tests.factories import (
    bearer,
    grant_permission,
    make_post_reply,
    make_user,
    make_visibility_world,
)
from tests.test_visibility_single_object import client_as


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
    w.admin = make_user(w.stranger.instance, 'ada', local=True)
    w.admin.private_key = 'a local account with keys'
    db.session.commit()
    role = grant_permission(w.admin, 'administer all communities')
    role.name = 'Admin'  # the web removal routes act only for a user the model calls an admin
    db.session.commit()
    w.stranger.private_key = 'a local account with keys'
    db.session.commit()
    return w


def removed(model, id_):
    db.session.expire_all()
    return db.session.get(model, id_).deleted


def web(app, user, url, data):
    from tests.test_visibility_interactions import send
    with patch('app.shared.post.task_selector'), patch('app.shared.reply.task_selector'):
        return send(app, user, 'post', url, data)


def test_admin_removes_a_followers_only_post_on_the_web(app, world):
    w = world
    assert web(app, w.stranger, f'/post/{w.post.id}/delete', {'reason': 'spam'}).status_code == 404
    assert not removed(Post, w.post.id)
    web(app, w.admin, f'/post/{w.post.id}/delete', {'reason': 'spam'})
    assert removed(Post, w.post.id)


def test_admin_removes_a_followers_only_reply_on_the_web(app, world):
    w = world
    url = f'/post/{w.public_post.id}/comment/{w.reply.id}/delete'
    assert web(app, w.stranger, url, {'reason': 'spam'}).status_code == 404
    assert not removed(PostReply, w.reply.id)
    web(app, w.admin, url, {'reason': 'spam'})
    assert removed(PostReply, w.reply.id)


def test_admin_still_cannot_view_the_post(app, world):
    w = world
    assert client_as(app, w.admin).get(f'/post/{w.post.id}').status_code == 404


@pytest.mark.parametrize('path, key, data, model, attr', [
    ('/api/alpha/post/remove', 'post_id', {'removed': True}, Post, 'post'),
    ('/api/alpha/comment/remove', 'comment_id', {'removed': True}, PostReply, 'reply'),
])
def test_api_removal(app, world, path, key, data, model, attr):
    w = world
    target = getattr(w, attr)
    body = {key: target.id, **data}
    with patch('app.shared.post.task_selector'), patch('app.shared.reply.task_selector'):
        stranger = app.test_client().post(path, json=body, headers={'Authorization': bearer(w.stranger)})
        assert stranger.status_code >= 400
        assert not removed(model, target.id)
        admin = app.test_client().post(path, json=body, headers={'Authorization': bearer(w.admin)})
    assert admin.status_code == 200, admin.data
    assert removed(model, target.id)
    assert 'secret' not in admin.get_data(as_text=True)


def test_purge_list_stubs_a_followers_only_post(app, world):
    w = world
    image = File(source_url='https://m.example/i.png', file_path='i.png', hash='0' * 256)
    db.session.add(image)
    db.session.commit()
    w.post.image_id = image.id
    w.post.title = 'Secret Title'
    w.public_post.image_id = image.id
    w.public_post.title = 'Open Title'
    w.public_post.user_id = w.stranger.id
    db.session.commit()
    with patch('app.admin.routes.posts_with_blocked_images', return_value=[w.post.id, w.public_post.id]):
        html = client_as(app, w.admin).get('/admin/block_image_purge_posts').get_data(as_text=True)
    assert f'value="{w.post.id}"' in html  # still selectable
    assert 'Visible to followers only' in html
    assert 'Secret Title' not in html and 'alice' not in html
    assert 'Open Title' in html


def test_remove_ack_for_a_reply_on_a_followers_only_post_leaks_nothing(app, world):
    w = world
    w.post.title = 'Parent Secret Title'
    w.post.body = 'parent secret body'
    reply = make_post_reply(w.post, w.author, 'reply secret body')
    reply.visibility = 'followers'
    db.session.commit()
    path = '/api/alpha/comment/remove'
    with patch('app.shared.reply.task_selector'):
        response = app.test_client().post(path, json={'comment_id': reply.id, 'removed': True},
                                          headers={'Authorization': bearer(w.admin)})
    assert response.status_code == 200, response.data
    text = response.get_data(as_text=True)
    for leaked in ('Parent Secret Title', 'parent secret body', 'reply secret body', 'alice', 'm.example'):
        assert leaked not in text
    creator = response.get_json()['comment_view']['creator']
    assert creator['id'] == 0 and not creator.get('avatar') and not creator.get('published')
    assert response.get_json()['comment_view']['comment']['user_id'] == 0


def test_post_remove_ack_has_no_author_identity(app, world):
    w = world
    with patch('app.shared.post.task_selector'):
        response = app.test_client().post('/api/alpha/post/remove', json={'post_id': w.post.id, 'removed': True},
                                          headers={'Authorization': bearer(w.admin)})
    view = response.get_json()['post_view']
    assert view['creator']['id'] == 0 and view['post']['user_id'] == 0
    assert str(w.author.id) != '0' and 'alice' not in response.get_data(as_text=True)


def test_delete_confirmation_page_does_not_name_the_hidden_post(app, world):
    w = world
    w.post.title = 'Hidden Title'
    db.session.commit()
    html = client_as(app, w.admin).get(f'/post/{w.post.id}/delete').get_data(as_text=True)
    assert 'Hidden Title' not in html
    assert 'Are you sure you want to delete this post?' in html


def test_the_modlog_does_not_name_removed_followers_only_content(app, world):
    w = world
    w.post.title = 'Modlog Secret Title'
    w.reply.body = 'modlog secret reply'
    db.session.commit()
    with patch('app.shared.post.task_selector'), patch('app.shared.reply.task_selector'):
        for path, body in (('/api/alpha/post/remove', {'post_id': w.post.id, 'removed': True}),
                           ('/api/alpha/comment/remove', {'comment_id': w.reply.id, 'removed': True})):
            assert app.test_client().post(path, json=body, headers={'Authorization': bearer(w.admin)}).status_code == 200
    entries = ModLog.query.all()
    assert len(entries) == 2
    for entry in entries:
        assert entry.link_text == 'followers-only content'
        assert entry.target_user_id == w.author.id  # R3: kept; neutralized on read
