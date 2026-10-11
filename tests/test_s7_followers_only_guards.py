"""Amendment B, S7.4: the guards around a local followers-only microblog post.

pyfedi has no local boost or quote feature (plan, contract additions 3), so the guards are: a followers-only post
cannot be moved; an inbound QuoteRequest for one is not authorized (its canQuote names only the author); it is never
cross-referenced with other posts of the same link nor offered for cross-posting; and the existing E9 gates keep every
moderation and vote activity about it away from the community's followers -- including a remote community's inbox,
which would relay a local author's followers-only reply to that community's followers.
"""
from types import SimpleNamespace

import pytest
from flask import get_flashed_messages, render_template_string

from app import db
from app.activitypub.util import process_quote_boost
from app.constants import (POST_TYPE_LINK, POST_TYPE_VIDEO, SRC_API, SRC_WEB, VISIBILITY_FOLLOWERS,
                           VISIBILITY_PUBLIC)
from app.models import ActivityBatch, Post, QuoteAuthorization, Site
from app.shared.post import move_post
from app.shared.tasks import adds, likes, locks, removes
from app.shared.tasks.pages import move_object
from tests.factories import (make_community, make_community_member, make_instance, make_post, make_post_reply,
                             make_site, make_user, web_ctx)


class Sent(list):
    def __call__(self, inbox, *args, **kwargs):
        self.append(inbox)


def _world(visibility):
    local = make_instance('test.piefed.local', software='piefed')
    make_site()
    make_user(local, 'founder', local=True)     # user 1: make_community hardcodes it as the owner, so alice is not
    author = make_user(local, 'alice', local=True)
    community = make_community('microblogs')
    other = make_community('general')
    peer = make_instance('peer3.example', software='lemmy')
    peer.inbox = 'https://peer3.example/inbox'
    make_community_member(make_user(peer, 'cm'), community)     # the community has a follower host
    post = make_post(community, author, ap_id='https://test.piefed.local/post/1', title='hello')
    post.visibility = visibility
    db.session.commit()
    return SimpleNamespace(local=local, author=author, community=community, other=other, post=post)


# --------------------------------------------------------------------------
# Moving
# --------------------------------------------------------------------------


def test_moving_a_followers_only_post_is_refused_before_the_permission_check(db_session, monkeypatch):
    w = _world(VISIBILITY_FOLLOWERS)
    monkeypatch.setattr('app.shared.post.authorise_api_user', lambda *a, **k: w.author)
    with pytest.raises(Exception, match='A followers-only post cannot be moved.'):
        move_post(w.post.id, w.other.id, SRC_API, auth='t')
    assert w.post.community_id == w.community.id


def test_moving_a_public_post_reaches_the_permission_check(db_session, monkeypatch):
    """Control: the author is no moderator, so a public post is refused for permission -- proving the guard above
    runs first and is not the permission check misreported."""
    w = _world(VISIBILITY_PUBLIC)
    monkeypatch.setattr('app.shared.post.authorise_api_user', lambda *a, **k: w.author)
    with pytest.raises(Exception, match='Does not have permission'):
        move_post(w.post.id, w.other.id, SRC_API, auth='t')


@pytest.mark.parametrize('visibility, moved', [(VISIBILITY_FOLLOWERS, False), (VISIBILITY_PUBLIC, True)],
                         ids=['followers-only', 'public'])
def test_a_moderator_moving_on_the_web_is_refused_only_for_a_followers_only_post(db_session, app, monkeypatch,
                                                                                visibility, moved):
    """P7: the web arm flashes the refusal and leaves the post where it is. The mover moderates both communities,
    so the public control really moves."""
    w = _world(visibility)
    make_community_member(w.author, w.community, is_moderator=True)
    make_community_member(w.author, w.other, is_moderator=True)
    tasks = []
    monkeypatch.setattr('app.shared.post.task_selector', lambda *a, **k: tasks.append(a))
    with web_ctx(app, w.author):
        result = move_post(w.post.id, w.other.id, SRC_WEB)
        flashed = get_flashed_messages()
    assert result is None
    assert (db.session.get(Post, w.post.id).community_id == w.other.id) is moved
    assert bool(tasks) is moved
    assert ('A followers-only post cannot be moved.' in flashed) is not moved


@pytest.mark.parametrize('visibility, expected', [(VISIBILITY_FOLLOWERS, []),
                                                  (VISIBILITY_PUBLIC, ['https://peer3.example/inbox'])],
                         ids=['followers-only', 'public'])
def test_move_object_announces_only_an_open_post(db_session, monkeypatch, visibility, expected):
    w = _world(visibility)
    sent = Sent()
    monkeypatch.setattr('app.shared.tasks.pages.send_post_request', sent)
    move_object(db.session, w.author.id, w.post, origin=w.community, target=w.other)
    assert sent == expected


# --------------------------------------------------------------------------
# Quote requests
# --------------------------------------------------------------------------


def _quote_request(post):
    return {'id': 'https://peer4.example/activities/q1', 'type': 'QuoteRequest',
            'actor': 'https://peer4.example/users/carol', 'object': post.ap_id,
            'instrument': 'https://peer4.example/notes/1'}


def test_a_quote_request_for_a_followers_only_post_is_not_authorized(db_session, monkeypatch):
    w = _world(VISIBILITY_FOLLOWERS)
    sent = Sent()
    monkeypatch.setattr('app.activitypub.util.send_post_request', sent)
    process_quote_boost(_quote_request(w.post), w.post.ap_id, 'https://peer4.example/notes/1')
    assert QuoteAuthorization.query.count() == 0
    assert sent == []


def test_a_quote_request_for_a_public_post_is_still_authorized(db_session, monkeypatch):
    w = _world(VISIBILITY_PUBLIC)
    peer4 = make_instance('peer4.example')
    peer4.inbox = 'https://peer4.example/inbox'
    carol = make_user(peer4, 'carol')
    db.session.commit()
    sent = Sent()
    monkeypatch.setattr('app.activitypub.util.send_post_request', sent)
    monkeypatch.setattr('app.activitypub.util.find_actor_or_create_cached', lambda *a, **k: carol)
    process_quote_boost(_quote_request(w.post), w.post.ap_id, 'https://peer4.example/notes/1')
    assert QuoteAuthorization.query.count() == 1
    assert sent == ['https://peer4.example/inbox']


# --------------------------------------------------------------------------
# E9 regression: moderation and votes about a local followers-only post stay home
# --------------------------------------------------------------------------

E9_TASKS = [
    ('lock_post', lambda w: locks.lock_post(None, w.author.id, w.post.id), 'app.shared.tasks.locks'),
    ('unlock_post', lambda w: locks.unlock_post(None, w.author.id, w.post.id), 'app.shared.tasks.locks'),
    ('sticky_post', lambda w: adds.sticky_post(None, w.author.id, w.post.id), 'app.shared.tasks.adds'),
    ('vote', lambda w: likes.vote_for_post(None, w.author.id, w.post.id, None, 'upvote'), 'app.shared.tasks.likes'),
]


@pytest.mark.parametrize('name, run, module', E9_TASKS, ids=[t[0] for t in E9_TASKS])
@pytest.mark.parametrize('visibility, federated', [(VISIBILITY_FOLLOWERS, False), (VISIBILITY_PUBLIC, True)],
                         ids=['followers-only', 'public'])
def test_e9_keeps_a_local_followers_only_post_away_from_community_followers(
        db_session, monkeypatch, name, run, module, visibility, federated):
    w = _world(visibility)
    sent = Sent()
    monkeypatch.setattr(f'{module}.send_post_request', sent)
    run(w)
    announced = bool(sent) or ActivityBatch.query.count() > 0
    assert announced is federated, name


# --------------------------------------------------------------------------
# E9 in a remote community: a followers-only reply there is the community's object only when its author is remote
# --------------------------------------------------------------------------


def _remote_world(reply_author_local, visibility):
    """A reply under dave's post in a lemmy community on peer5. The reply is by bob (local) or by erin (peer7)."""
    local = make_instance('test.piefed.local', software='piefed')
    make_site()
    actor = make_user(local, 'bob', local=True, with_keys=True)
    peer5 = make_instance('peer5.example', software='lemmy')
    remote = make_community('elsewhere', host='peer5.example')
    remote.ap_id = 'https://peer5.example/c/elsewhere'
    remote.ap_profile_id = 'https://peer5.example/c/elsewhere'
    remote.instance_id = peer5.id
    remote.ap_inbox_url = 'https://peer5.example/c/elsewhere/inbox'
    remote.ap_featured_url = 'https://peer5.example/c/elsewhere/featured'
    db.session.commit()
    post = make_post(remote, make_user(make_instance('peer6.example'), 'dave'),
                     ap_id='https://peer6.example/post/1', title='hello')
    post.visibility = visibility
    author = actor if reply_author_local else make_user(make_instance('peer7.example'), 'erin')
    reply = make_post_reply(post, author, 'agreed')
    reply.visibility = visibility
    reply.community_id = remote.id
    db.session.commit()
    assert not remote.is_local()
    return SimpleNamespace(actor=actor, community=remote, post=post, reply=reply)


REMOTE_TASKS = [
    ('lock', lambda w: locks.lock_object(db.session, w.actor.id, w.reply), 'app.shared.tasks.locks'),
    ('unlock', lambda w: locks.lock_object(db.session, w.actor.id, w.reply, is_undo=True), 'app.shared.tasks.locks'),
    ('add', lambda w: adds.add_object(db.session, w.actor.id, w.reply), 'app.shared.tasks.adds'),
    ('remove', lambda w: removes.remove_object(db.session, w.actor.id, w.reply), 'app.shared.tasks.removes'),
    ('vote', lambda w: likes.send_vote(w.actor.id, w.reply, None, 'upvote', None), 'app.shared.tasks.likes'),
]


@pytest.mark.parametrize('name, run, module', REMOTE_TASKS, ids=[t[0] for t in REMOTE_TASKS])
@pytest.mark.parametrize('reply_author_local, visibility, delivered', [
    (True, VISIBILITY_FOLLOWERS, False),        # the local author's audience is their followers, not the community
    (True, VISIBILITY_PUBLIC, True),
    (False, VISIBILITY_FOLLOWERS, True),        # R-a: the remote author sent it to the community, which holds it
], ids=['local-followers-only', 'local-public', 'remote-followers-only'])
def test_a_remote_community_is_sent_activities_only_about_objects_it_holds(
        db_session, monkeypatch, name, run, module, reply_author_local, visibility, delivered):
    w = _remote_world(reply_author_local, visibility)
    sent = Sent()
    monkeypatch.setattr(f'{module}.send_post_request', sent)
    run(w)
    assert sent == (['https://peer5.example/c/elsewhere/inbox'] if delivered else []), name


# --------------------------------------------------------------------------
# Cross-posts
# --------------------------------------------------------------------------

LINK = 'https://news.example/story/1'


def _cross_world():
    """Two posts of the same link: alice's followers-only one in microblogs, bob's public one in general."""
    w = _world(VISIBILITY_FOLLOWERS)
    w.post.type = POST_TYPE_LINK
    w.post.url = LINK
    bob = make_user(w.local, 'bob', local=True)
    w.public = make_post(w.other, bob, ap_id='https://test.piefed.local/post/2', title='same link')
    w.public.type = POST_TYPE_LINK
    w.public.url = LINK
    db.session.commit()
    return w


def test_a_followers_only_post_gets_no_cross_posts(db_session):
    w = _cross_world()
    w.post.calculate_cross_posts()
    assert not w.post.cross_posts
    assert not w.public.cross_posts


def test_a_public_post_is_not_cross_referenced_with_a_followers_only_one(db_session):
    w = _cross_world()
    w.public.calculate_cross_posts()
    assert not w.public.cross_posts
    assert not w.post.cross_posts


def test_two_public_posts_of_one_link_are_still_cross_referenced(db_session):
    """Control for the two above."""
    w = _cross_world()
    w.post.visibility = VISIBILITY_PUBLIC
    db.session.commit()
    w.public.calculate_cross_posts()
    assert w.public.cross_posts == [w.post.id]
    assert w.post.cross_posts == [w.public.id]


def test_the_cross_posts_list_leaves_out_a_post_the_viewer_may_not_see(db_session, app, monkeypatch):
    """Rows written before the guard above can still name a followers-only post; the list filters them."""
    w = _cross_world()
    _site_open()
    third = make_post(make_community('third'), w.author, ap_id='https://test.piefed.local/post/3', title='again')
    third.url = LINK
    w.public.cross_posts = [w.post.id, third.id]
    db.session.commit()
    rendered = {}
    monkeypatch.setattr('app.post.routes.render_template',
                        lambda template, **kw: rendered.update(kw) or 'rendered')
    response = app.test_client().get(f'/post/{w.public.id}/cross_posts')
    assert response.status_code == 200
    assert [p.id for p in rendered['cross_posts']] == [third.id]


def test_the_post_page_shows_no_comments_from_a_cross_posted_post_the_viewer_may_not_see(db_session, app,
                                                                                         monkeypatch):
    """"Comments from other communities" walks cross_posts too: a stale id naming a followers-only post must not
    pull in its replies (nor its community)."""
    w = _cross_world()
    _site_open()
    third = make_post(make_community('third'), w.author, ap_id='https://test.piefed.local/post/3', title='again')
    third.url = LINK
    for p in (w.post, third):
        reply = make_post_reply(p, w.author, f'a reply on {p.id}')
        reply.body_html = f'<p>a reply on {p.id}</p>'
        reply.visibility = p.visibility
    w.public.cross_posts = [w.post.id, third.id]
    db.session.commit()
    rendered = {}
    monkeypatch.setattr('app.post.routes.render_template',
                        lambda template, **kw: rendered.update(kw) or app.response_class('rendered'))
    response = app.test_client().get(f'/post/{w.public.id}')
    assert response.status_code == 200
    assert [c.id for c in rendered['more_replies']] == [third.community_id]


@pytest.mark.parametrize('visibility, offered', [(VISIBILITY_PUBLIC, True), (VISIBILITY_FOLLOWERS, False)],
                         ids=['public', 'followers-only'])
def test_the_options_menu_offers_moving_only_an_open_post(db_session, app, monkeypatch, visibility, offered):
    w = _world(visibility)
    _site_open()
    response = _client(app, w.author).get(f'/post/{w.post.id}/options_menu')
    assert response.status_code == 200
    assert (f'/post/{w.post.id}/move'.encode() in response.data) is offered


def _site_open():
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()


def _client(app, user=None):
    client = app.test_client()
    if user is not None:
        with client.session_transaction() as session:
            session['_user_id'] = str(user.id)
            session['_fresh'] = True
    return client


@pytest.mark.parametrize('post_type, visibility, logged_in, offered', [
    (POST_TYPE_LINK, VISIBILITY_PUBLIC, True, True),
    (POST_TYPE_VIDEO, VISIBILITY_PUBLIC, True, True),
    (POST_TYPE_VIDEO, VISIBILITY_PUBLIC, False, False),      # the `and ... or` precedence slip offered it to anyone
    (POST_TYPE_LINK, VISIBILITY_FOLLOWERS, True, False),
    (POST_TYPE_VIDEO, VISIBILITY_FOLLOWERS, True, False),
], ids=['link', 'video', 'video-anonymous', 'followers-only-link', 'followers-only-video'])
def test_the_post_page_offers_cross_posting_only_for_an_open_post_to_a_signed_in_user(
        db_session, app, monkeypatch, post_type, visibility, logged_in, offered):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # the signed-in page's reply form renders csrf_token
    w = _world(visibility)
    _site_open()
    w.post.type = post_type
    w.post.url = 'https://news.example/clip.mp4' if post_type == POST_TYPE_VIDEO else LINK
    db.session.commit()
    response = _client(app, w.author if logged_in else None).get(f'/post/{w.post.id}')
    assert response.status_code == 200
    assert (b'post_cross_post_link' in response.data) is offered


@pytest.mark.parametrize('macros', ['post/post_teaser/_macros.html',
                                    'themes/dillo/post/post_teaser/_macros.html'], ids=['default', 'dillo'])
@pytest.mark.parametrize('visibility, offered', [(VISIBILITY_PUBLIC, True), (VISIBILITY_FOLLOWERS, False)],
                         ids=['public', 'followers-only'])
def test_a_teaser_offers_cross_posting_only_for_an_open_post(db_session, app, macros, visibility, offered):
    w = _world(visibility)
    w.post.type = POST_TYPE_LINK
    w.post.url = LINK
    w.author.bot = True
    db.session.commit()
    with web_ctx(app, w.author):
        html = render_template_string(
            '{% from "' + macros + '" import render_utilities_bar %}'
            '{{ render_utilities_bar(post, current_user=current_user, communities_banned_from_list=[]) }}',
            post=w.post)
    assert ('post_cross_post_link' in html) is offered
