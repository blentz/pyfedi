from unittest.mock import patch

import pytest
from flask import current_app, g
from flask_login import login_user

from app import db
from app.models import Language, PostBookmark, PostVote, PostReplyBookmark, Site, UserFollower
from app.user.utils import _get_user_posts, _get_user_post_replies, _get_user_posts_and_replies
from tests.factories import make_visibility_world, make_post_reply, bearer
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
    return w


def as_viewer(app, viewer):
    """Request context with `viewer` logged in (None = anonymous)."""
    g.pop('_login_user', None)
    ctx = app.test_request_context('/')
    ctx.push()
    if viewer is not None:
        login_user(viewer)
    return ctx


def profile(app, w, viewer):
    ctx = as_viewer(app, viewer)
    try:
        posts = [p.id for p in _get_user_posts(w.author, 1).items]
        replies = [r.id for r in _get_user_post_replies(w.author, 1).items]
        overview, _ = _get_user_posts_and_replies(w.author, 1)
        overview_posts = [o.id for o in overview if o.__class__.__name__ == 'Post']
        overview_replies = [o.id for o in overview if o.__class__.__name__ == 'PostReply']
    finally:
        ctx.pop()
    return posts, replies, overview_posts, overview_replies


@pytest.mark.parametrize('who', ['stranger', 'pending', None])
def test_profile_hides_followers_only_from_non_followers(app, world, who):
    w = world
    posts, replies, o_posts, o_replies = profile(app, w, getattr(w, who) if who else None)
    assert w.post.id not in posts and w.post.id not in o_posts
    assert w.reply.id not in replies and w.reply.id not in o_replies
    assert w.public_post.id in posts and w.public_post.id in o_posts


@pytest.mark.parametrize('who', ['follower', 'author'])
def test_profile_shows_followers_only_to_follower_and_author(app, world, who):
    w = world
    posts, replies, o_posts, o_replies = profile(app, w, getattr(w, who))
    assert w.post.id in posts and w.post.id in o_posts
    assert w.reply.id in replies and w.reply.id in o_replies


def test_profile_rss_shows_neither(app, world):
    w = world
    client = client_as(app, None)
    with patch('app.user.routes.RSSFeed') as feed:
        feed.return_value.create_feed.return_value = '<rss/>'
        resp = client.get('/u/alice@m.example/feed')
    assert resp.status_code == 200
    posts = feed.return_value.create_feed.call_args[0][0]
    ids = [p.id for p in posts]
    assert w.post.id not in ids
    assert w.public_post.id in ids


def captured_posts(app, viewer, url):
    client = client_as(app, viewer)
    with patch('app.user.routes.render_template', return_value=app.response_class('rendered')) as render:
        assert client.get(url).status_code == 200
    return [p.id for p in render.call_args.kwargs['posts']]


def test_bookmark_vanishes_when_the_follow_ends(app, world):
    w = world
    db.session.add(PostBookmark(user_id=w.follower.id, post_id=w.post.id))
    db.session.commit()
    assert w.post.id in captured_posts(app, w.follower, '/bookmarks')
    UserFollower.query.filter_by(local_user_id=w.follower.id).delete()
    db.session.commit()
    assert w.post.id not in captured_posts(app, w.follower, '/bookmarks')


def test_reply_bookmark_vanishes_when_the_follow_ends(app, world):
    w = world
    db.session.add(PostReplyBookmark(user_id=w.follower.id, post_reply_id=w.reply.id))
    db.session.commit()

    def shown():
        client = client_as(app, w.follower)
        with patch('app.user.routes.render_template', return_value=app.response_class('rendered')) as render:
            assert client.get('/bookmarks/comments').status_code == 200
        return [r.id for r in render.call_args.kwargs['post_replies']]
    assert w.reply.id in shown()
    UserFollower.query.filter_by(local_user_id=w.follower.id).delete()
    db.session.commit()
    assert w.reply.id not in shown()


def test_read_and_hidden_posts_obey_the_predicate(app, world):
    w = world
    db.session.execute(db.text('INSERT INTO read_posts (user_id, read_post_id, interacted_at) VALUES (:u, :p, now())'),
                       {'u': w.stranger.id, 'p': w.post.id})
    db.session.execute(db.text('INSERT INTO hidden_posts (user_id, hidden_post_id) VALUES (:u, :p)'),
                       {'u': w.stranger.id, 'p': w.post.id})
    db.session.commit()
    assert captured_posts(app, w.stranger, '/read-posts') == []
    assert captured_posts(app, w.stranger, '/hidden_posts') == []


def get_user(app, viewer, **extra):
    client = client_as(app, None)
    resp = client.get('/api/alpha/user', query_string={'person_id': w_id(viewer.author), 'include_content': 'true', **extra},
                      headers={'Authorization': bearer(viewer.who)})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()


def w_id(user):
    return user.id


class V:
    def __init__(self, author, who):
        self.author, self.who = author, who


@pytest.mark.parametrize('who, shown', [('stranger', False), ('pending', False), ('follower', True)])
def test_api_get_user(app, world, who, shown):
    w = world
    j = get_user(app, V(w.author, getattr(w, who)))
    post_ids = [p['post']['id'] for p in j['posts']]
    comment_ids = [c['comment']['id'] for c in j['comments']]
    assert (w.post.id in post_ids) is shown
    assert w.public_post.id in post_ids
    assert (w.reply.id in comment_ids) is shown


def test_api_comment_list_modes_omit_rather_than_stub(app, world):
    w = world
    for extra in ({'person_id': w.author.id}, {'person_id': w.author.id, 'community_id': w.community.id}):
        client = client_as(app, None)
        resp = client.get('/api/alpha/comment/list', query_string=extra, headers={'Authorization': bearer(w.stranger)})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        ids = [c['comment']['id'] for c in resp.get_json()['comments']]
        assert w.reply.id not in ids
        assert all(c['comment'].get('content') is not None for c in resp.get_json()['comments'])


@pytest.mark.parametrize('path', ['/api/alpha/post/list', '/api/alpha/post/list2'])
@pytest.mark.parametrize('mode', ['saved_only', 'liked_only'])
def test_api_post_list_saved_and_liked_obey_the_predicate(app, world, path, mode):
    w = world
    db.session.add(PostBookmark(user_id=w.stranger.id, post_id=w.post.id))
    db.session.add(PostVote(user_id=w.stranger.id, author_id=w.author.id, post_id=w.post.id, effect=1.0))
    db.session.commit()
    resp = client_as(app, None).get(path, query_string={mode: 'true'}, headers={'Authorization': bearer(w.stranger)})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert w.post.id not in [p['post']['id'] for p in resp.get_json()['posts']]


@pytest.mark.parametrize('path', ['/api/alpha/post/list', '/api/alpha/post/list2'])
def test_api_post_list_by_person_obeys_the_predicate(app, world, path):
    w = world
    for who, shown in ((w.stranger, False), (w.follower, True)):
        resp = client_as(app, None).get(path, query_string={'person_id': w.author.id},
                                        headers={'Authorization': bearer(who)})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        ids = [p['post']['id'] for p in resp.get_json()['posts']]
        assert (w.post.id in ids) is shown


# --- D5 / D10 (residuals WP-D): private communities and community-less rows on a profile ---------

def _private_community_with_public_post(w):
    from tests.factories import make_community, make_community_member
    private = make_community('hush')
    private.private = True
    w.public_post.community_id = private.id
    w.public_child.community_id = private.id
    db.session.commit()
    make_community_member(w.author, private)
    return private


def test_a_member_of_a_private_community_sees_its_content_on_a_profile(app, world):
    from tests.factories import make_community_member
    w = world
    private = _private_community_with_public_post(w)
    make_community_member(w.stranger, private)

    posts, _replies, o_posts, _o_replies = profile(app, w, w.stranger)

    assert w.public_post.id in posts and w.public_post.id in o_posts


@pytest.mark.parametrize('who', ['pending', None])
def test_a_non_member_still_does_not_see_private_community_content_on_a_profile(app, world, who):
    w = world
    _private_community_with_public_post(w)

    posts, _replies, o_posts, _o_replies = profile(app, w, getattr(w, who) if who else None)

    assert w.public_post.id not in posts and w.public_post.id not in o_posts


def test_a_banned_member_does_not_see_private_community_content_on_a_profile(app, world):
    from tests.factories import make_community_member
    w = world
    private = _private_community_with_public_post(w)
    make_community_member(w.stranger, private).is_banned = True
    db.session.commit()

    posts, _replies, o_posts, _o_replies = profile(app, w, w.stranger)

    assert w.public_post.id not in posts and w.public_post.id not in o_posts


@pytest.mark.parametrize('who', ['stranger', None, 'author'])
def test_rows_with_no_community_are_not_dropped_from_a_profile(app, world, who):
    from tests.factories import make_community, make_community_member
    w = world
    private = make_community('hush')  # NULL NOT IN (a non-empty set) is NULL, which drops the row
    private.private = True
    db.session.commit()
    make_community_member(w.author, private)
    reply = make_post_reply(w.public_post, w.author, 'a community-less reply')
    reply.community_id = None
    w.public_post.community_id = None
    db.session.commit()

    posts, replies, o_posts, o_replies = profile(app, w, getattr(w, who) if who else None)

    assert w.public_post.id in posts and w.public_post.id in o_posts
    assert reply.id in replies and reply.id in o_replies
