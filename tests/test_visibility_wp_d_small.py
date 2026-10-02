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


# --- D6: the D18 placeholder honours THREAD_CUTOFF_DEPTH and is hidable like the teaser ------------

def _placeholder_html(app, depth, cutoff=5):
    from flask import render_template
    from app.visibility import RestrictedReply
    parent = RestrictedReply(id=10, depth=depth, parent_id=None, post_id=3)
    child = RestrictedReply(id=11, depth=depth + 1, parent_id=10, post_id=3)
    with app.test_request_context('/'):
        return render_template('post/_post_reply_restricted.html', post_reply=parent, THREAD_CUTOFF_DEPTH=cutoff, nonce='n0nce',
                               children=[{'comment': child, 'replies': [], 'restricted': True}])


def test_a_placeholder_below_the_cutoff_renders_its_children(app, db_session):
    html = _placeholder_html(app, depth=2)
    assert 'id="comment_11"' in html
    assert 'Continue thread' not in html


def test_a_placeholder_past_the_cutoff_links_to_the_rest_of_the_thread(app, db_session):
    html = _placeholder_html(app, depth=6)
    assert 'id="comment_11"' not in html
    assert 'Continue thread' in html
    assert '/post/3/comment/10' in html


def test_a_placeholder_with_no_cutoff_renders_every_level(app, db_session):
    assert 'id="comment_11"' in _placeholder_html(app, depth=60, cutoff=0)


def test_a_placeholder_hides_with_its_collapsed_parent(app, db_session):
    html = _placeholder_html(app, depth=2)
    assert 'comment_body hidable' in html
    assert 'replies hidable depth_2' in html


# --- D9: an archived stub with no id is skipped, not a KeyError ---------------------------------------

def test_a_malformed_archived_stub_is_skipped(app, world):
    from app.post.util import convert_archived_replies_to_tree
    w = world
    archived = [
        {'visibility': 'followers', 'depth': 0, 'replies': []},  # no id: written by nothing this code knows
        {'id': 7, 'visibility': 'followers', 'depth': 0, 'path': [0, 7], 'replies': [
            {'visibility': 'followers', 'depth': 1, 'replies': []}]},
    ]

    tree = convert_archived_replies_to_tree(archived, w.public_post)

    assert [entry['comment'].id for entry in tree] == [7]
    assert tree[0]['restricted'] and tree[0]['replies'] == []
