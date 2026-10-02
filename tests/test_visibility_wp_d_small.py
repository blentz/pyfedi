"""Small visibility follow-ups from the residuals wave (WP-D items D4 onward)."""
from unittest.mock import patch

import pytest

from app import db
from tests.factories import make_post_reply, make_visibility_world
from tests.test_visibility_interactions import send


@pytest.fixture
def world(app, db_session):
    return make_visibility_world()


# --- D4: post_reply_block_instance gates the post as well as the reply -------

def test_blocking_the_instance_of_a_reply_on_a_hidden_post_is_404(app, world):
    w = world
    reply = make_post_reply(w.post, w.author, 'a public reply on a followers-only post')

    with patch('app.post.routes.block_remote_instance') as block:
        response = send(app, w.stranger, 'post', f'/post/{w.post.id}/comment/{reply.id}/block_instance')

    assert response.status_code == 404
    assert not block.called


def test_blocking_the_instance_of_a_reply_through_another_post_is_404(app, world):
    w = world
    reply = make_post_reply(w.post, w.author, 'a public reply on a followers-only post')

    with patch('app.post.routes.block_remote_instance') as block:
        response = send(app, w.stranger, 'post', f'/post/{w.public_post.id}/comment/{reply.id}/block_instance')

    assert response.status_code == 404
    assert not block.called


def test_blocking_the_instance_of_a_visible_reply_still_works(app, world):
    w = world

    with patch('app.post.routes.block_remote_instance') as block:
        response = send(app, w.stranger, 'post', f'/post/{w.public_post.id}/comment/{w.public_child.id}/block_instance')

    assert response.status_code == 404  # public_child is local (instance 1): refused as before, by the instance check
    w.public_child.instance_id = w.author.instance_id
    db.session.commit()
    with patch('app.post.routes.block_remote_instance') as block:
        response = send(app, w.stranger, 'post', f'/post/{w.public_post.id}/comment/{w.public_child.id}/block_instance')
    assert response.status_code == 302
    assert block.called
