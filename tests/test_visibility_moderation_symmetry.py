"""D2 (residuals WP-D, ruling D22): enforcement is not seeing. A moderator or admin may restore, lock, sticky, purge
and block the image of a followers-only object they cannot view, exactly as they may remove it; what they are shown
afterwards stays neutral. Anyone else is still answered 404 / not found."""
from unittest.mock import patch

import pytest
from flask import g, session as flask_session
from flask_wtf.csrf import generate_csrf

from app import db
from app.constants import POST_TYPE_IMAGE
from app.models import BlockedImage, Post, PostReply
from tests.factories import bearer, grant_permission
from tests.test_visibility_moderation import world  # noqa: F401 -- the fixture
from tests.test_visibility_single_object import client_as

SECRET = 'SECRET-TITLE'


def _send(app, user, url, data=None):
    """As tests/test_visibility_interactions.send, but hands back the client so a test can read the flashes."""
    client = client_as(app, user)
    g.pop('csrf_token', None)
    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as session:
        session['csrf_token'] = raw
    with patch('app.shared.post.task_selector'), patch('app.shared.reply.task_selector'), \
            patch('app.post.routes.task_selector'):
        response = client.post(url, data={**(data or {}), 'csrf_token': token})
    with client.session_transaction() as session:
        flashes = ' '.join(message for _category, message in session.get('_flashes', []))
    return response, flashes


@pytest.fixture
def hidden(world):
    w = world
    w.post.title = SECRET
    w.post.body = 'secret body'
    db.session.commit()
    return w


def _fresh(model, id_):
    db.session.expire_all()
    return db.session.get(model, id_)


def _deleted_by_admin(obj, admin):
    obj.deleted = True
    obj.deleted_by = admin.id
    db.session.commit()


# --- the web ---------------------------------------------------------------

def test_an_admin_restores_a_followers_only_post_they_cannot_view(app, hidden):
    w = hidden
    _deleted_by_admin(w.post, w.admin)
    assert _send(app, w.stranger, f'/post/{w.post.id}/restore')[0].status_code == 404
    assert _fresh(Post, w.post.id).deleted

    response, flashes = _send(app, w.admin, f'/post/{w.post.id}/restore')

    assert response.status_code == 302
    assert not _fresh(Post, w.post.id).deleted
    assert SECRET not in flashes


def test_an_admin_restores_a_followers_only_reply_they_cannot_view(app, hidden):
    w = hidden
    _deleted_by_admin(w.reply, w.admin)
    url = f'/post/{w.public_post.id}/comment/{w.reply.id}/restore'
    assert _send(app, w.stranger, url)[0].status_code == 404

    _send(app, w.admin, url)

    assert not _fresh(PostReply, w.reply.id).deleted


def test_an_admin_locks_and_unlocks_a_followers_only_post(app, hidden):
    w = hidden
    assert _send(app, w.stranger, f'/post/{w.post.id}/lock/yes')[0].status_code == 404

    _response, flashes = _send(app, w.admin, f'/post/{w.post.id}/lock/yes')
    assert _fresh(Post, w.post.id).comments_enabled is False
    assert SECRET not in flashes
    _send(app, w.admin, f'/post/{w.post.id}/lock/no')
    assert _fresh(Post, w.post.id).comments_enabled is True


def test_an_admin_locks_a_followers_only_reply(app, hidden):
    w = hidden
    url = f'/post/{w.public_post.id}/{w.reply.id}/lock/yes'
    assert _send(app, w.stranger, url)[0].status_code == 404

    _send(app, w.admin, url)

    assert _fresh(PostReply, w.reply.id).replies_enabled is False


def test_an_admin_stickies_a_followers_only_post_without_being_told_its_title(app, hidden):
    w = hidden
    assert _send(app, w.stranger, f'/post/{w.post.id}/sticky/yes')[0].status_code == 404

    _response, flashes = _send(app, w.admin, f'/post/{w.post.id}/sticky/yes')

    assert _fresh(Post, w.post.id).sticky is True
    assert flashes and SECRET not in flashes


def test_an_admin_instance_stickies_a_followers_only_post_without_being_told_its_title(app, hidden):
    w = hidden

    _response, flashes = _send(app, w.admin, f'/post/{w.post.id}/instance_sticky/yes')

    assert _fresh(Post, w.post.id).instance_sticky is True
    assert flashes and SECRET not in flashes


def test_an_admin_purges_a_followers_only_post_and_is_not_sent_to_its_author(app, hidden):
    w = hidden
    _deleted_by_admin(w.post, w.admin)
    assert _send(app, w.stranger, f'/post/{w.post.id}/purge')[0].status_code == 404

    response, _flashes = _send(app, w.admin, f'/post/{w.post.id}/purge')

    assert response.status_code == 302
    assert _fresh(Post, w.post.id) is None
    assert str(w.author.id) not in response.headers['Location'].rsplit('/', 1)[-1]
    assert '/u/' not in response.headers['Location'] and '/user/' not in response.headers['Location']


def test_an_admin_purges_a_followers_only_reply(app, hidden):
    w = hidden
    _deleted_by_admin(w.reply, w.admin)
    w.public_child.parent_id = None  # a reply with replies is never purged
    db.session.commit()
    url = f'/post/{w.public_post.id}/comment/{w.reply.id}/purge'
    assert _send(app, w.stranger, url)[0].status_code == 404

    _send(app, w.admin, url)

    assert _fresh(PostReply, w.reply.id) is None


def test_an_admin_blocks_the_image_of_a_followers_only_post_without_recording_its_title(app, hidden):
    w = hidden
    grant_permission(w.admin, 'change instance settings')
    w.post.type = POST_TYPE_IMAGE
    w.post.url = 'https://m.example/secret.png'
    db.session.commit()

    page = client_as(app, w.admin).get(f'/post/{w.post.id}/block_image')
    assert page.status_code == 200 and SECRET not in page.get_data(as_text=True)
    with patch('app.post.routes.retrieve_image_hash', return_value='1' * 256):
        _send(app, w.admin, f'/post/{w.post.id}/block_image')

    note = BlockedImage.query.one().note
    assert SECRET not in note
    assert client_as(app, w.admin).get(f'/post/{w.post.id}/block_image_purge_posts').status_code == 200


# --- the API ---------------------------------------------------------------

def _api(app, user, path, body):
    with patch('app.shared.post.task_selector'), patch('app.shared.reply.task_selector'):
        return app.test_client().post(path, json=body, headers={'Authorization': bearer(user)})


@pytest.mark.parametrize('path, body, field', [
    ('/api/alpha/post/lock', {'locked': True}, ('comments_enabled', False)),
    ('/api/alpha/post/feature', {'featured': True, 'feature_type': 'Community'}, ('sticky', True)),
    ('/api/alpha/post/feature', {'featured': True, 'feature_type': 'Local'}, ('instance_sticky', True)),
])
def test_an_admin_moderates_a_followers_only_post_through_the_api(app, hidden, path, body, field):
    w = hidden
    request = {'post_id': w.post.id, **body}
    assert _api(app, w.stranger, path, request).status_code >= 400
    attr, value = field
    assert getattr(_fresh(Post, w.post.id), attr) != value

    response = _api(app, w.admin, path, request)

    assert response.status_code == 200, response.data
    assert getattr(_fresh(Post, w.post.id), attr) == value
    text = response.get_data(as_text=True)
    for leaked in (SECRET, 'secret body', 'alice'):
        assert leaked not in text
    assert response.get_json()['post_view']['creator']['id'] == 0


def test_an_admin_locks_a_followers_only_reply_through_the_api(app, hidden):
    w = hidden
    request = {'comment_id': w.reply.id, 'locked': True}
    assert _api(app, w.stranger, '/api/alpha/comment/lock', request).status_code >= 400

    response = _api(app, w.admin, '/api/alpha/comment/lock', request)

    assert response.status_code == 200, response.data
    assert _fresh(PostReply, w.reply.id).replies_enabled is False
    assert 'secret reply' not in response.get_data(as_text=True)
    assert response.get_json()['comment_view']['creator']['id'] == 0


def test_an_admin_restores_a_followers_only_post_through_api_remove(app, hidden):
    w = hidden
    _deleted_by_admin(w.post, w.admin)

    response = _api(app, w.admin, '/api/alpha/post/remove', {'post_id': w.post.id, 'removed': False})

    assert response.status_code == 200, response.data
    assert not _fresh(Post, w.post.id).deleted
    assert SECRET not in response.get_data(as_text=True)


def test_a_viewer_still_gets_the_full_view_after_locking(app, hidden):
    w = hidden
    response = _api(app, w.admin, '/api/alpha/post/lock', {'post_id': w.public_post.id, 'locked': True})
    assert response.status_code == 200, response.data
    assert response.get_json()['post_view']['creator']['id'] == w.author.id
