"""Task 4b: acting on a followers-only object is refused before it takes effect.

Every case runs the same attempt as a stranger and as a follower. The stranger gets the answer a nonexistent id
gets and the database does not change; the follower's identical attempt goes through.
"""
from unittest.mock import patch

import pytest
from flask import current_app, g

from app import db
from app.models import Language, Notification, NotificationSubscription, Post, PostBookmark, PostReply, \
    PostReplyBookmark, PostReplyVote, PostVote, Report, Site
from tests.factories import make_visibility_world, bearer
from tests.test_visibility_single_object import client_as, MISSING


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
    for user in (w.stranger, w.follower):
        user.private_key = 'a local account with keys: it may act, which is what this file tests'
    db.session.commit()
    return w


TABLES = (PostVote, PostReplyVote, PostBookmark, PostReplyBookmark, NotificationSubscription, PostReply, Report,
          Notification)


def snapshot(w):
    """Row counts of every table an interaction writes to, and the columns of the two hidden objects."""
    db.session.expire_all()
    counts = {t.__name__: db.session.query(t).count() for t in TABLES}
    objects = {}
    for obj in (db.session.get(Post, w.post.id), db.session.get(PostReply, w.reply.id)):
        objects[type(obj).__name__] = {c.name: getattr(obj, c.name) for c in obj.__table__.columns}
    return counts, objects


# (name, method, url, form data). {p} is the hidden followers-only post, {r} the hidden reply under the public
# post {pp}.
WEB_ACTIONS = [
    ('post_vote', 'post', '/post/{p}/upvote/public', None),
    ('comment_vote', 'post', '/comment/{r}/upvote/public', None),
    ('post_bookmark', 'post', '/post/{p}/bookmark', None),
    ('reply_bookmark', 'post', '/post/{pp}/comment/{r}/bookmark', None),
    ('post_notification', 'post', '/post/{p}/notification', None),
    ('reply_notification', 'post', '/post_reply/{r}/notification', None),
    ('post_report', 'post', '/post/{p}/report', {'reasons': ['1'], 'description': 'x'}),
    ('reply_report', 'post', '/post/{pp}/comment/{r}/report', {'reasons': ['1'], 'description': 'x'}),
    ('add_reply', 'post', '/post/{pp}/comment/{r}/reply', {'body': 'hello there', 'language_id': '{lang}'}),
    ('add_reply_inline', 'post', '/post/{pp}/comment/{r}/reply_inline/n', {'body': 'hello there'}),
]


def send(app, user, method, url, data=None):
    """The request as `user`, with a real CSRF token: every POST needs one or it is a 400 before the route runs."""
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf

    client = client_as(app, user)
    g.pop('csrf_token', None)  # generate_csrf caches in g; a cached token writes nothing to this request's session
    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as session:
        session['csrf_token'] = raw
    return getattr(client, method)(url, data={**(data or {}), 'csrf_token': token})


def fill(template, w):
    return template.format(p=w.post.id, r=w.reply.id, pp=w.public_post.id,
                           lang=Language.query.filter_by(code='en').one().id)


@pytest.mark.parametrize('name, method, url, data', WEB_ACTIONS, ids=[a[0] for a in WEB_ACTIONS])
def test_web_action_refused_for_stranger_and_goes_through_for_follower(app, world, name, method, url, data):
    w = world
    url = fill(url, w)
    data = {k: fill(v, w) if isinstance(v, str) else v for k, v in (data or {}).items()}
    before = snapshot(w)
    response = send(app, w.stranger, method, url, data)
    assert response.status_code == 404
    assert snapshot(w) == before

    response = send(app, w.follower, method, url, data)
    assert response.status_code in (200, 302), response.status_code
    assert snapshot(w) != before


# State-changing routes whose only visible effect is a column or a row somewhere else. For these the follower
# control is "not 404": the follower is not a moderator, so they get a permission refusal, not a not-found.
WEB_REFUSED_ONLY = [
    ('post', '/post/{p}/delete'), ('post', '/post/{p}/restore'), ('post', '/post/{p}/purge'),
    ('post', '/post/{p}/edit'), ('post', '/post/{p}/sticky/yes'), ('post', '/post/{p}/instance_sticky/yes'),
    ('post', '/post/{p}/hide/yes'), ('post', '/post/{p}/lock/yes'), ('post', '/post/{p}/mea_culpa'),
    ('post', '/post/{p}/block_user'), ('post', '/post/{p}/block_community'), ('post', '/post/{p}/set_ai'),
    ('post', '/post/{p}/set_read'), ('post', '/post/{p}/reminder'), ('post', '/post/{p}/translate'),
    ('post', '/post/{p}/emoji_set'), ('post', '/post/{p}/move'), ('post', '/post/{p}/set_flair'),
    ('post', '/post/{p}/remove_bookmark'),
    ('post', '/poll/{p}/vote'),
    ('post', '/comment/{r}/emoji_set'), ('post', '/comment/{r}/upvote/public/emoji'),
    ('post', '/post/{pp}/comment/{r}/edit'), ('post', '/post/{pp}/comment/{r}/delete'),
    ('post', '/post/{pp}/comment/{r}/restore'), ('post', '/post/{pp}/comment/{r}/purge'),
    ('post', '/post/{pp}/comment/{r}/block_user'), ('post', '/post/{pp}/comment/{r}/block_instance'),
    ('post', '/post/{pp}/comment/{r}/distinguish'), ('post', '/post/{pp}/comment/{r}/remove_bookmark'),
    ('post', '/post/{pp}/{r}/lock/yes'), ('post', '/post/{pp}/{r}/collapse/yes'),
    ('post', '/post_reply/{r}/choose_answer'), ('post', '/post_reply/{r}/unchoose_answer'),
    ('post', '/post_reply/{r}/reminder'), ('post', '/post_reply/{r}/translate'),
]


@pytest.mark.parametrize('method, url', WEB_REFUSED_ONLY, ids=[u[1] for u in WEB_REFUSED_ONLY])
def test_other_state_changing_routes_are_not_found_for_stranger(app, world, method, url):
    w = world
    url = fill(url, w)
    before = snapshot(w)
    stranger = send(app, w.stranger, method, url)
    assert stranger.status_code == 404
    assert snapshot(w) == before
    # Not hidden from the follower: whatever the route then says, it is not the hidden-object 404 at the gate.
    # (A route may 404 for a reason of its own, so the control is the call that reaches past the gate.)
    from app.post import routes
    gate_calls = []

    def spy(real):
        def wrapper(*args, **kwargs):
            real(*args, **kwargs)  # raises NotFound for a hidden object, so a recorded call is a call that passed
            gate_calls.append(args or kwargs)
        return wrapper

    with patch.object(routes, 'refuse_invisible', spy(routes.refuse_invisible)), \
            patch.object(routes, 'refuse_invisible_ids', spy(routes.refuse_invisible_ids)):
        try:
            send(app, w.follower, method, url)
        except Exception:
            pass  # a route handed a bare form may fail further on; the gate was already passed
    assert gate_calls


def test_anonymous_hidden_nsfw_post_is_404_not_a_login_redirect(app, world):
    w = world
    w.post.nsfw = True
    db.session.commit()
    anon = client_as(app, None)
    assert anon.get(f'/post/{w.post.id}').status_code == 404
    # control: a public NSFW post still sends an anonymous visitor to log in
    w.public_post.nsfw = True
    db.session.commit()
    assert anon.get(f'/post/{w.public_post.id}').status_code == 302


API_ACTIONS = [
    # name, method, path, json, key the id goes under
    ('post_like', 'post', '/api/alpha/post/like', {'score': 1}, 'post_id'),
    ('post_save', 'put', '/api/alpha/post/save', {'save': True}, 'post_id'),
    ('post_subscribe', 'put', '/api/alpha/post/subscribe', {'subscribe': True}, 'post_id'),
    ('post_report', 'post', '/api/alpha/post/report', {'reason': 'spam'}, 'post_id'),
    ('comment_like', 'post', '/api/alpha/comment/like', {'score': 1}, 'comment_id'),
    ('comment_save', 'put', '/api/alpha/comment/save', {'save': True}, 'comment_id'),
    ('comment_subscribe', 'put', '/api/alpha/comment/subscribe', {'subscribe': True}, 'comment_id'),
    ('comment_report', 'post', '/api/alpha/comment/report', {'reason': 'spam'}, 'comment_id'),
    ('comment_create', 'post', '/api/alpha/comment', {'body': 'hello there', 'parent_id': '{r}'}, 'post_id'),
    ('comment_create_under_hidden_parent', 'post', '/api/alpha/comment', {'body': 'hello there', 'post_id': '{pp}'},
     'parent_id'),
]

API_MODERATOR_ACTIONS = [
    ('post_edit', 'put', '/api/alpha/post', {'title': 'x'}, 'post_id'),
    ('post_delete', 'post', '/api/alpha/post/delete', {'deleted': True}, 'post_id'),
    ('post_lock', 'post', '/api/alpha/post/lock', {'locked': True}, 'post_id'),
    ('post_hide', 'post', '/api/alpha/post/hide', {'hidden': True}, 'post_id'),
    ('post_feature', 'post', '/api/alpha/post/feature', {'featured': True}, 'post_id'),
    ('post_remove', 'post', '/api/alpha/post/remove', {'removed': True}, 'post_id'),
    ('post_mark_as_read', 'post', '/api/alpha/post/mark_as_read', {'read': True}, 'post_id'),
    ('post_poll_vote', 'post', '/api/alpha/post/poll_vote', {'choice_id': [1]}, 'post_id'),
    ('post_assign_flair', 'post', '/api/alpha/post/assign_flair', {'flair_id_list': []}, 'post_id'),
    ('comment_edit', 'put', '/api/alpha/comment', {'body': 'x'}, 'comment_id'),
    ('comment_delete', 'post', '/api/alpha/comment/delete', {'deleted': True}, 'comment_id'),
    ('comment_remove', 'post', '/api/alpha/comment/remove', {'removed': True}, 'comment_id'),
    ('comment_lock', 'post', '/api/alpha/comment/lock', {'locked': True}, 'comment_id'),
    ('comment_distinguish', 'post', '/api/alpha/comment/distinguish', {'distinguished': True}, 'comment_reply_id'),
    ('comment_mark_as_answer', 'post', '/api/alpha/comment/mark_as_answer', {'answer': True}, 'comment_reply_id'),
    ('comment_mark_as_read', 'post', '/api/alpha/comment/mark_as_read', {'read': True}, 'comment_reply_id'),
    ('moderate_post_nsfw', 'post', '/api/alpha/community/moderate/post/nsfw', {'nsfw_status': True}, 'post_id'),
]


def call_api(app, w, user, method, path, body, key, target):
    payload = {k: (fill(v, w) if isinstance(v, str) else v) for k, v in body.items()}
    for k, v in list(payload.items()):
        if isinstance(v, str) and v.isdigit():
            payload[k] = int(v)
    payload[key] = target
    if 'comment_id' not in payload and key == 'post_id' and path.endswith('/api/alpha/comment'):
        pass
    return getattr(app.test_client(), method)(path, json=payload, headers={'Authorization': bearer(user)})


def target_for(w, key, name):
    if key == 'post_id':
        return w.post.id if 'create' not in name else w.public_post.id
    if key == 'parent_id':
        return w.reply.id
    return w.reply.id


@pytest.mark.parametrize('name, method, path, body, key', API_ACTIONS + API_MODERATOR_ACTIONS,
                         ids=[a[0] for a in API_ACTIONS + API_MODERATOR_ACTIONS])
def test_api_action_on_hidden_object_is_a_not_found_and_changes_nothing(app, world, name, method, path, body, key):
    w = world
    before = snapshot(w)
    hidden_target = target_for(w, key, name)
    if name == 'comment_create':
        # a reply to the hidden post: the post is the hidden thing
        body = {'body': 'hello there'}
        key, hidden_target = 'post_id', w.post.id
    if name == 'comment_create_under_hidden_parent':
        body = {'body': 'hello there', 'post_id': w.public_post.id}
    hidden = call_api(app, w, w.stranger, method, path, body, key, hidden_target)
    missing_target = MISSING
    missing = call_api(app, w, w.stranger, method, path, body, key, missing_target)
    assert hidden.get_data(as_text=True) == missing.get_data(as_text=True)
    assert hidden.status_code == missing.status_code != 200
    assert snapshot(w) == before


@pytest.mark.parametrize('name, method, path, body, key', API_ACTIONS,
                         ids=[a[0] for a in API_ACTIONS])
def test_api_action_goes_through_for_follower(app, world, name, method, path, body, key):
    w = world
    before = snapshot(w)
    target = target_for(w, key, name)
    if name == 'comment_create':
        body = {'body': 'hello there'}
        key, target = 'post_id', w.post.id
    if name == 'comment_create_under_hidden_parent':
        body = {'body': 'hello there', 'post_id': w.public_post.id}
    response = call_api(app, w, w.follower, method, path, body, key, target)
    assert response.status_code == 200, response.get_data(as_text=True)
    assert snapshot(w) != before


@pytest.mark.parametrize('name, method, path, body, key', API_MODERATOR_ACTIONS,
                         ids=[a[0] for a in API_MODERATOR_ACTIONS])
def test_api_moderator_action_is_not_the_not_found_for_follower(app, world, name, method, path, body, key):
    """The follower may be refused for lack of permission, but not as if the object did not exist."""
    w = world
    target = target_for(w, key, name)
    hidden = call_api(app, w, w.follower, method, path, body, key, target)
    missing = call_api(app, w, w.follower, method, path, body, key, MISSING)
    assert hidden.get_data(as_text=True) != missing.get_data(as_text=True)
