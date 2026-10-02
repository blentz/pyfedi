"""Final-review fixes for the visibility plan: a visible reply must not carry its hidden parent post, and the
remaining surfaces obey the predicate."""
from unittest.mock import patch

import pytest
from flask import current_app, g

from app import db
from app.models import Language, Site
from tests.factories import bearer, make_post_reply, make_visibility_world
from tests.test_visibility_single_object import MISSING, client_as

SECRET_TITLE = 'Secret Parent Title'
SECRET_BODY = 'secret parent body'
PUBLIC_REPLY = 'zebrafinch public answer'


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
    w.post.title = SECRET_TITLE
    w.post.body = SECRET_BODY
    w.post.body_html = f'<p>{SECRET_BODY}</p>'
    # a PUBLIC reply, by a local follower, under the followers-only post
    w.open_reply = make_post_reply(w.post, w.follower, PUBLIC_REPLY)
    w.open_reply.ap_id = 'https://m.example/r/77'
    db.session.commit()
    return w


def api_get(app, path, user, **params):
    headers = {'Authorization': bearer(user)} if user is not None else {}
    return client_as(app, None).get(path, query_string=params, headers=headers)


def assert_no_secret(text, where):
    assert SECRET_TITLE not in text, where
    assert SECRET_BODY not in text, where


# C1
@pytest.mark.parametrize('who', ['stranger', None])
def test_post_replies_of_a_hidden_post_is_not_found(app, world, who):
    w = world
    user = getattr(w, who) if who else None
    hidden = api_get(app, '/api/alpha/post/replies', user, post_id=w.post.id)
    missing = api_get(app, '/api/alpha/post/replies', user, post_id=MISSING)
    assert hidden.status_code == missing.status_code != 200
    assert hidden.get_data(as_text=True) == missing.get_data(as_text=True)
    via_parent = api_get(app, '/api/alpha/post/replies', user, parent_id=w.open_reply.id)
    assert via_parent.status_code != 200
    assert_no_secret(via_parent.get_data(as_text=True), 'parent_id branch')


def test_post_replies_of_a_hidden_post_open_to_a_follower(app, world):
    w = world
    response = api_get(app, '/api/alpha/post/replies', w.follower, post_id=w.post.id)
    assert response.status_code == 200, response.get_data(as_text=True)
    assert PUBLIC_REPLY in response.get_data(as_text=True)


# R1 / C2: every reply-bearing surface, for an anonymous visitor and a logged-in stranger
def _api_surfaces(w, who):
    surfaces = [('comment', '/api/alpha/comment', {'id': w.open_reply.id}, True),
                ('list community', '/api/alpha/comment/list', {'community_id': w.community.id}, True),
                ('list post', '/api/alpha/comment/list', {'post_id': w.post.id}, False),
                ('list person', '/api/alpha/comment/list', {'person_id': w.follower.id}, True),
                ('list search', '/api/alpha/comment/list', {'q': 'zebrafinch'}, False)]
    if who is not None:
        surfaces += [('list saved', '/api/alpha/comment/list', {'saved_only': 'true'}, True),
                     ('list liked', '/api/alpha/comment/list', {'liked_only': 'true'}, True),
                     ('resolve', '/api/alpha/resolve_object', {'q': w.open_reply.ap_id}, True)]
    return surfaces


def _web_surfaces(w, who):
    surfaces = [('community comments', '/c/microblogs?content_type=comments'),
                ('topic comments', '/topic/news?content_type=comments'),
                ('search', '/search?q=zebrafinch&search_for=comments'),
                ('profile', f'/u/{w.follower.user_name}')]
    if who is not None:
        surfaces.append(('bookmarks', '/bookmarks/comments'))
    return surfaces


@pytest.fixture
def swept(world):
    from app.models import Topic
    from tests.factories import make_post_reply_bookmark, make_post_reply_vote
    w = world
    topic = Topic(name='news', machine_name='news', num_communities=1, show_posts_in_children=False)
    db.session.add(topic)
    db.session.commit()
    w.community.topic_id = topic.id
    w.post.indexable = True
    w.open_reply.indexable = True
    db.session.commit()
    for user in (w.stranger, w.follower):
        make_post_reply_bookmark(user, w.open_reply)
        make_post_reply_vote(user, w.open_reply, 1)
    return w


@pytest.mark.parametrize('who', ['stranger', None])
def test_a_visible_reply_never_carries_its_hidden_parent_through_the_api(app, swept, who):
    w = swept
    user = getattr(w, who) if who else None
    for name, path, params, listed in _api_surfaces(w, who):
        response = api_get(app, path, user, **params)
        text = response.get_data(as_text=True)
        assert_no_secret(text, name)
        assert 'alice' not in text, name
        if listed:
            assert response.status_code == 200, (name, text)
            assert PUBLIC_REPLY in text, name


@pytest.mark.parametrize('who', ['stranger', None])
def test_a_visible_reply_never_carries_its_hidden_parent_on_the_web(app, swept, who):
    w = swept
    for name, path in _web_surfaces(w, who):
        response = client_as(app, getattr(w, who) if who else None).get(path)
        text = response.get_data(as_text=True)
        assert response.status_code == 200, name
        assert_no_secret(text, name)
        if name != 'search':  # search needs the full-text index, which the test rows may not carry
            assert PUBLIC_REPLY in text, name


def test_the_nntp_subject_of_a_reply_does_not_name_a_hidden_parent(app, swept):
    from app.nntp.server import _reply_to_info
    w = swept
    info = _reply_to_info(w.open_reply, 'piefed.test', 1)
    assert SECRET_TITLE not in info.subject
    w.post.visibility = 'public'
    db.session.commit()
    assert SECRET_TITLE in _reply_to_info(w.open_reply, 'piefed.test', 1).subject


def test_a_follower_sees_the_parent_title(app, swept):
    w = swept
    api = api_get(app, '/api/alpha/comment', w.follower, id=w.open_reply.id)
    assert api.status_code == 200
    assert api.get_json()['comment_view']['post']['title'] == SECRET_TITLE
    web = client_as(app, w.follower).get('/c/microblogs?content_type=comments').get_data(as_text=True)
    assert SECRET_TITLE in web


def test_the_stub_parent_names_nothing_but_ids(app, swept):
    w = swept
    post = api_get(app, '/api/alpha/comment', w.stranger, id=w.open_reply.id).get_json()['comment_view']['post']
    assert post['id'] == w.post.id and post['visibility'] == 'followers'
    assert post['title'] == '' and post['user_id'] == 0 and not post.get('body') and not post.get('url')


# C3
def test_cross_posts_do_not_carry_a_hidden_post(app, world):
    w = world
    w.public_post.cross_posts = [w.post.id]
    w.post.cross_posts = [w.public_post.id]
    db.session.commit()
    for user in (w.stranger, None):
        response = api_get(app, '/api/alpha/post', user, id=w.public_post.id)
        assert response.status_code == 200, response.get_data(as_text=True)
        assert_no_secret(response.get_data(as_text=True), 'cross_posts')
        assert 'alice' not in str(response.get_json()['cross_posts'])
    follower = api_get(app, '/api/alpha/post', w.follower, id=w.public_post.id).get_json()
    assert follower['cross_posts'][0]['post']['title'] == SECRET_TITLE


# R2
def _stub_of(comments, reply_id):
    for c in comments:
        if c['comment']['id'] == reply_id:
            return c
        found = _stub_of(c.get('replies') or [], reply_id)
        if found:
            return found


@pytest.mark.parametrize('path, params', [('/api/alpha/post/replies', 'post_id'),
                                          ('/api/alpha/comment/list', 'post_id')])
def test_a_reply_stub_carries_neutral_objects_not_nulls(app, world, path, params):
    w = world
    w.reply.body = 'secret reply'
    db.session.commit()
    response = api_get(app, path, w.stranger, **{params: w.public_post.id})
    assert response.status_code == 200, response.get_data(as_text=True)
    stub = _stub_of(response.get_json()['comments'], w.reply.id)
    assert stub['visibility'] == 'followers' and stub['comment']['body'] is None
    for key in ('creator', 'counts', 'post', 'community'):
        assert isinstance(stub[key], dict), key
    assert stub['creator']['id'] == 0 and stub['creator']['user_name'] == ''
    assert stub['counts']['score'] == 0 and stub['counts']['comment_id'] == w.reply.id
    assert stub['post']['id'] == w.public_post.id and stub['post']['title'] == '' and stub['post']['user_id'] == 0
    text = response.get_data(as_text=True)
    assert 'secret reply' not in text and '"alice"' not in text
