"""R-REPLYVIEW: a followers-only object is visible to the users it was pushed to, as Mastodon does -- the author a
reply answers (its parent reply's author, else its post's author) and locally Mentioned users. can_view,
visible_to_clause and visible_to_sql agree on parent authors; Mentions are can_view only (the SQL twins cannot
resolve them), so a Mentioned user can open the object and is notified of it, but it is not listed for them."""
import contextlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import current_app, g
from sqlalchemy import text

from app import db
from app.activitypub.util import notify_about_post_reply
from app.constants import NOTIF_POST, VISIBILITY_FOLLOWERS
from app.models import Language, Notification, Post, PostReply
from app.user.utils import _get_user_post_replies, _get_user_posts_and_replies
from app.visibility import RestrictedReply, can_view, visible_to_clause, visible_to_sql
from tests.factories import bearer, make_community, make_follow, make_instance, make_notification_subscription, \
    make_post, make_post_reply, make_site, make_user
from tests.test_visibility_profiles import as_viewer
from tests.test_visibility_single_object import client_as


@pytest.fixture
def world(app, db_session, monkeypatch):
    """alice (local) wrote a followers-only post; bob follows her and answers it followers-only; alice follows
    nobody. carol follows bob and answers bob's reply followers-only. sam is a stranger to all of them."""
    local = make_instance(app.config['SERVER_NAME'], software='piefed')
    site = make_site()
    alice = make_user(local, 'alice', local=True)
    bob = make_user(local, 'bob', local=True)
    carol = make_user(local, 'carol', local=True)
    sam = make_user(local, 'sam', local=True)
    make_follow(bob, alice)
    make_follow(carol, bob)
    make_follow(carol, alice)
    community = make_community()
    post = make_post(community, alice, None, title='for my followers')
    post.visibility = VISIBILITY_FOLLOWERS
    db.session.commit()
    reply = make_post_reply(post, bob, 'a reply for my followers')
    reply.visibility = VISIBILITY_FOLLOWERS
    db.session.commit()
    nested = make_post_reply(post, carol, 'answering bob')
    nested.parent_id = reply.id
    nested.depth = 1
    nested.visibility = VISIBILITY_FOLLOWERS
    db.session.commit()
    site.private_instance = False
    g.admin_ids = []
    g.site = site
    monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.add(Language(code='en', name='English'))
    db.session.commit()
    site.language_id = Language.query.filter_by(code='en').one().id
    db.session.commit()
    return SimpleNamespace(alice=alice, bob=bob, carol=carol, sam=sam, post=post, reply=reply, nested=nested)


def _viewers(w):
    return (None, w.alice.id, w.bob.id, w.carol.id, w.sam.id)


# --- the predicate ---

def test_the_post_author_sees_a_followers_only_reply_from_someone_she_does_not_follow(world):
    w = world
    assert can_view(w.reply, w.alice.id)
    assert not can_view(w.reply, w.sam.id)
    assert not can_view(w.reply, None)


def test_a_nested_reply_is_visible_to_the_parent_replys_author_not_the_posts(world):
    w = world
    assert can_view(w.nested, w.bob.id)
    assert can_view(w.nested, w.alice.id) is False   # carol's reply answers bob, not alice
    assert not can_view(w.nested, w.sam.id)


def test_the_orm_twin_agrees_on_parent_authors(world):
    w = world
    for model, obj in ((PostReply, w.reply), (PostReply, w.nested), (Post, w.post)):
        for viewer in _viewers(w):
            ids = {row.id for row in model.query.filter(visible_to_clause(model, viewer))}
            assert (obj.id in ids) == can_view(obj, viewer), (obj.id, viewer)


def test_the_sql_twin_agrees_on_parent_authors(world):
    w = world
    replies = text(f"SELECT r.id FROM post_reply r WHERE {visible_to_sql('r', reply=True)}")
    posts = text(f"SELECT p.id FROM post p WHERE {visible_to_sql('p')}")
    for viewer in _viewers(w):
        reply_ids = {row[0] for row in db.session.execute(replies, {'visibility_viewer_id': viewer})}
        post_ids = {row[0] for row in db.session.execute(posts, {'visibility_viewer_id': viewer})}
        for reply in (w.reply, w.nested):
            assert (reply.id in reply_ids) == can_view(reply, viewer), (reply.id, viewer)
        assert (w.post.id in post_ids) == can_view(w.post, viewer), viewer


def test_a_locally_mentioned_user_sees_a_followers_only_post_and_reply(app, world):
    w = world
    server = app.config['SERVER_NAME']
    w.post.body = f'hello @sam@{server}'
    w.nested.body = f'cc @sam@{server}'
    db.session.commit()

    assert can_view(w.post, w.sam.id)
    assert can_view(w.nested, w.sam.id)


@pytest.mark.parametrize('body', ['hello @sam@elsewhere.example', 'hello @Sam@{server}', 'hello @samuel@{server}',
                                  'hello sam', ''])
def test_only_a_mention_send_post_would_resolve_to_the_viewer_counts(app, world, body):
    w = world
    w.post.body = body.format(server=app.config['SERVER_NAME'])
    db.session.commit()

    assert not can_view(w.post, w.sam.id)


def test_a_mention_of_a_remote_user_with_a_local_users_name_does_not_count(app, world):
    w = world
    remote_sam = make_user(make_instance('peer.example'), 'sam2')
    remote_sam.user_name = 'sam'
    w.post.body = f"hello @sam@{app.config['SERVER_NAME']}"
    db.session.commit()

    assert not can_view(w.post, remote_sam.id)


# --- the read surfaces ---

def _comment_page(app, viewer, url):
    """The /post/<id>/comment/<id> page (a notification's link) as `viewer`: its status and its reply tree."""
    rendered = {}

    def capture(template, **context):
        rendered.update(context)
        return app.response_class('rendered')

    with patch('app.post.routes.render_template', side_effect=capture):
        status = client_as(app, viewer).get(url).status_code
    return status, rendered.get('replies') or []


def _shown(tree, reply_id):
    """Whether `reply_id` is in `tree` as itself, not as a RestrictedReply placeholder."""
    for entry in tree:
        if entry['comment'].id == reply_id:
            return not isinstance(entry['comment'], RestrictedReply)
        found = _shown(entry['replies'], reply_id)
        if found is not None:
            return found
    return None


def test_the_web_comment_view_shows_the_reply_to_the_post_author(app, world):
    w = world
    status, tree = _comment_page(app, w.alice, f'/post/{w.post.id}/comment/{w.reply.id}')

    assert status == 200
    assert _shown(tree, w.reply.id) is True


def test_the_web_comment_view_shows_bob_the_nested_reply_and_alice_a_placeholder(app, world):
    w = world
    url = f'/post/{w.post.id}/comment/{w.reply.id}'

    assert _shown(_comment_page(app, w.bob, url)[1], w.nested.id) is True
    assert _shown(_comment_page(app, w.alice, url)[1], w.nested.id) is False


def test_the_api_comment_is_served_to_the_post_author_and_not_a_stranger(app, world):
    w = world
    client = app.test_client()
    shown = client.get('/api/alpha/comment', query_string={'id': w.reply.id}, headers={'Authorization': bearer(w.alice)})
    hidden = client.get('/api/alpha/comment', query_string={'id': w.reply.id}, headers={'Authorization': bearer(w.sam)})

    assert shown.status_code == 200
    assert shown.json['comment_view']['comment']['body'] == 'a reply for my followers'
    assert hidden.status_code != 200


def test_the_api_post_is_served_to_a_mentioned_user(app, world):
    w = world
    w.post.body = f"hello @sam@{app.config['SERVER_NAME']}"
    db.session.commit()
    client = app.test_client()

    response = client.get('/api/alpha/post', query_string={'id': w.post.id}, headers={'Authorization': bearer(w.sam)})

    assert response.status_code == 200


def test_the_post_author_is_notified_of_the_reply_with_a_link_she_can_open(app, world, redis_double, monkeypatch):
    w = world
    make_notification_subscription(w.alice, w.post.id, NOTIF_POST)
    make_notification_subscription(w.sam, w.post.id, NOTIF_POST)

    with monkeypatch.context() as locks:   # fakeredis cannot release a lock (tests/conftest.py redis_double)
        locks.setattr('app.redis_client', _LockOnly())
        notify_about_post_reply(None, w.reply)

    notification = Notification.query.filter_by(user_id=w.alice.id).one()
    assert notification.url == f'/post/{w.post.id}/comment/{w.reply.id}#comment_{w.reply.id}'
    status, tree = _comment_page(app, w.alice, notification.url.split('#')[0])
    assert status == 200
    assert _shown(tree, w.reply.id) is True
    assert Notification.query.filter_by(user_id=w.sam.id).count() == 0   # a stranger subscribed to it is not told


class _LockOnly:
    def lock(self, *args, **kwargs):
        return contextlib.nullcontext()


@pytest.mark.parametrize('who, shown', [('alice', True), ('sam', False)])
def test_the_repliers_profile_lists_the_reply_to_the_post_author(app, world, who, shown):
    """Both profile listings: the ORM one (_get_user_post_replies) and the raw-SQL overview."""
    w = world
    ctx = as_viewer(app, getattr(w, who))
    try:
        replies = [r.id for r in _get_user_post_replies(w.bob, 1).items]
        overview, _ = _get_user_posts_and_replies(w.bob, 1)
    finally:
        ctx.pop()

    assert (w.reply.id in replies) is shown
    assert (w.reply.id in [o.id for o in overview if isinstance(o, PostReply)]) is shown
