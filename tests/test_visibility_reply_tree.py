import pytest

from app.post.util import post_replies, get_comment_branch
from app.visibility import RestrictedReply
from tests.factories import make_visibility_world, bearer
from tests.test_visibility_single_object import client_as, world  # noqa: F401  (world is a fixture)


def test_stranger_sees_placeholder_with_public_child(app, db_session):
    w = make_visibility_world()
    tree = post_replies(w.public_post, 'new', w.stranger)
    entry = next(e for e in tree if e['comment'].id == w.reply.id)
    assert isinstance(entry['comment'], RestrictedReply)
    assert entry['restricted'] is True
    assert entry['replies'][0]['comment'].id == w.public_child.id


def test_follower_sees_the_reply(app, db_session):
    w = make_visibility_world()
    tree = post_replies(w.public_post, 'new', w.follower)
    entry = next(e for e in tree if e['comment'].id == w.reply.id)
    assert entry['comment'].body == 'secret reply'


def test_anonymous_sees_placeholder(app, db_session):
    w = make_visibility_world()
    tree = post_replies(w.public_post, 'new', None)
    entry = next(e for e in tree if e['comment'].id == w.reply.id)
    assert isinstance(entry['comment'], RestrictedReply)


def test_comment_branch_marks_hidden_root(app, db_session):
    w = make_visibility_world()
    branch = get_comment_branch(w.public_post, w.reply.id, 'top', w.stranger)
    assert isinstance(branch[0]['comment'], RestrictedReply)
    assert branch[0]['replies'][0]['comment'].body == 'public child'


@pytest.fixture
def csrf_on(app, monkeypatch):
    # the post page renders form.csrf_token(), which does not exist while CSRF is off
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)


def test_post_page_html_never_contains_the_body(app, world, csrf_on):
    w = world
    html = client_as(app, w.stranger).get(f'/post/{w.public_post.id}').get_data(as_text=True)
    assert 'secret reply' not in html
    assert 'Visible to followers only' in html
    assert 'public child' in html


def test_permalink_to_public_child_of_hidden_parent(app, world, csrf_on):
    """Review focus 5."""
    w = world
    response = client_as(app, w.stranger).get(f'/post/{w.public_post.id}/comment/{w.public_child.id}')
    assert response.status_code == 200
    assert 'public child' in response.get_data(as_text=True)
    assert 'secret reply' not in response.get_data(as_text=True)


def test_permalink_to_hidden_comment_is_a_placeholder_with_children(app, world, csrf_on):
    w = world
    response = client_as(app, w.stranger).get(f'/post/{w.public_post.id}/comment/{w.reply.id}')
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'secret reply' not in html
    assert 'Visible to followers only' in html
    assert 'public child' in html


def test_ap_replies_collection_omits_hidden_reply(app, world):
    w = world
    response = app.test_client().get(f'/post/{w.public_post.id}/replies', headers={'Accept': 'application/activity+json'})
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert w.reply.profile_id() not in body
    assert f'/comment/{w.reply.id}' not in body
    assert response.json['totalItems'] == 1  # the public child only
    assert 'public child' in body


def test_ap_context_collection_omits_hidden_reply(app, world):
    w = world
    response = app.test_client().get(f'/post/{w.public_post.id}/context', headers={'Accept': 'application/activity+json'})
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert w.reply.profile_id() not in body
    assert response.json['totalItems'] == 2  # the post and the public child; the factory leaves reply ap_ids None


def _find(comments, reply_id):
    """Client-style walk: every entry, stub or not, is read through entry['comment']['id'] and ['path']."""
    for c in comments:
        assert c['comment']['path']
        if c['comment']['id'] == reply_id:
            return c
        found = _find(c.get('replies', []), reply_id)
        if found:
            return found
    return None


def test_api_post_replies_returns_stub_for_stranger(app, world):
    w = world
    response = app.test_client().get('/api/alpha/post/replies', query_string={'post_id': w.public_post.id},
                                     headers={'Authorization': bearer(w.stranger)})
    assert response.status_code == 200
    stub = _find(response.json['comments'], w.reply.id)
    assert stub['comment']['body'] is None and stub['creator']['id'] == 0 and not stub['creator']['user_name']
    assert stub['visibility'] == 'followers'
    assert stub['comment']['post_id'] == w.public_post.id
    assert 'secret reply' not in response.get_data(as_text=True)
    child = stub['replies'][0]
    assert child['comment']['body'] == 'public child'


def test_api_post_replies_shows_follower_the_reply(app, world):
    w = world
    response = app.test_client().get('/api/alpha/post/replies', query_string={'post_id': w.public_post.id},
                                     headers={'Authorization': bearer(w.follower)})
    assert _find(response.json['comments'], w.reply.id)['comment']['body'] == 'secret reply'


def test_api_comment_list_returns_stub_for_stranger(app, world):
    w = world
    response = app.test_client().get('/api/alpha/comment/list', query_string={'post_id': w.public_post.id},
                                     headers={'Authorization': bearer(w.stranger)})
    assert response.status_code == 200
    stub = _find(response.json['comments'], w.reply.id)
    assert stub['comment']['body'] is None and stub['visibility'] == 'followers'
    assert 'secret reply' not in response.get_data(as_text=True)


def test_api_comment_list_tree_modes_return_stub(app, world):
    w = world
    for extra in ({'post_id': w.public_post.id, 'max_depth': 5},):
        response = app.test_client().get('/api/alpha/comment/list', query_string=extra,
                                         headers={'Authorization': bearer(w.stranger)})
        assert response.status_code == 200
        assert 'secret reply' not in response.get_data(as_text=True)
        assert _find(response.json['comments'], w.reply.id)['comment']['body'] is None


def test_restricted_reply_carries_its_path(app, db_session):
    w = make_visibility_world()
    entry = next(e for e in post_replies(w.public_post, 'new', w.stranger) if e['comment'].id == w.reply.id)
    assert entry['comment'].path[-1] == w.reply.id
    assert entry['comment'].path[0] == 0


@pytest.mark.parametrize('path', ['/api/alpha/post/replies', '/api/alpha/comment/list'])
def test_api_builds_the_neutral_post_and_community_once_per_thread(app, world, path):
    """D7 (residuals WP-D): every stub in one post's thread shares one neutral community and post."""
    from unittest.mock import patch
    from app import db
    from app.api.alpha import views
    from tests.factories import make_post_reply
    w = world
    for n in range(3):
        hidden = make_post_reply(w.public_post, w.author, f'secret {n}')
        hidden.visibility = 'followers'
    db.session.commit()
    real = views._neutral_post
    with patch('app.api.alpha.views._neutral_post', side_effect=real) as neutral:
        response = app.test_client().get(path, query_string={'post_id': w.public_post.id},
                                         headers={'Authorization': bearer(w.stranger)})
    assert response.status_code == 200

    def stubs(comments):
        return sum((c['comment']['body'] is None) + stubs(c.get('replies', [])) for c in comments)

    assert stubs(response.json['comments']) == 4
    assert neutral.call_count == 1
