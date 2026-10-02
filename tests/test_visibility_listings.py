"""Listings, search, RSS and the sitemap show public content only (interop D7); the
followed-author feed source obeys the viewer predicate.

Every hidden row is checked for the follower AND the stranger: a follower may open a
followers-only post by its permalink (Task 4), but no listing offers it.
"""
import re
import uuid

import pytest
from flask import current_app, g

from app import db
from app.activitypub.util import record_boost
from app.models import Domain, Language, Post, Site, Tag, post_tag
from tests.factories import (bearer, feed_ids, make_follow, make_instance, make_post, make_post_reply,
                             make_user, make_visibility_world)
from tests.test_visibility_single_object import client_as

pytestmark = pytest.mark.usefixtures('site')

DOMAIN = 'news.example'
TAG = 'solarstorm'
WORD = 'zebrafinch'


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

    domain = Domain(name=DOMAIN, post_count=0, banned=False)
    tag = Tag(name=TAG, display_as=TAG, banned=False, post_count=0)
    db.session.add_all([domain, tag])
    db.session.commit()

    # w.post is the microblog (Post.private) followers-only post. The two below are
    # ordinary, non-microblog posts, so the visibility clause is what excludes them.
    followers_post = make_post(w.community, w.author, 'https://m.example/s/3', title=f'{WORD} followers')
    followers_post.visibility = 'followers'
    unlisted_post = make_post(w.community, w.author, 'https://m.example/s/4', title=f'{WORD} unlisted')
    unlisted_post.visibility = 'unlisted'
    w.public_post.title = f'{WORD} public'
    for post in (w.post, w.public_post, followers_post, unlisted_post):
        post.domain_id = domain.id
        post.url = f'https://{DOMAIN}/{post.id}'
        post.instance_id = 1
        post.sticky = False
        post.status = 1
        db.session.execute(post_tag.insert().values(post_id=post.id, tag_id=tag.id))
    w.public_post.sticky = True
    db.session.commit()
    w.followers_post = followers_post
    w.unlisted_post = unlisted_post
    w.hidden = [w.post, followers_post, unlisted_post]
    w.domain = domain
    return w


def post_links(response):
    return {int(i) for i in re.findall(r'/post/(\d+)', response.get_data(as_text=True))}


WEB_SURFACES = {
    'community_page': lambda w: '/c/microblogs',
    'community_comments': lambda w: '/c/microblogs?content_type=comments',
    'community_rss': lambda w: '/community/microblogs/feed',
    'domain_page': lambda w: f'/d/{w.domain.id}',
    'domain_rss': lambda w: f'/d/{w.domain.id}/feed',
    'instance_posts': lambda w: '/instance/piefed.test/posts',
    'tag_page': lambda w: f'/tag/{TAG}',
    'tag_rss': lambda w: f'/tag/{TAG}/feed',
    'search': lambda w: f'/search?q={WORD}&search_for=posts',
    'sitemap': lambda w: '/sitemap.xml',
}


@pytest.mark.parametrize('surface', sorted(WEB_SURFACES))
@pytest.mark.parametrize('who', ['follower', 'stranger', None])
def test_web_listing_hides_non_public_posts(app, world, surface, who):
    w = world
    viewer = getattr(w, who) if who else None
    response = client_as(app, viewer).get(WEB_SURFACES[surface](w))
    assert response.status_code == 200
    shown = post_links(response)
    hidden_ids = {p.id for p in w.hidden}
    assert not shown & hidden_ids, surface
    if surface != 'community_comments':
        assert w.public_post.id in shown, surface


def test_community_comments_page_omits_hidden_replies(app, world):
    w = world
    for who in (w.follower, w.stranger):
        body = client_as(app, who).get('/c/microblogs?content_type=comments').get_data(as_text=True)
        assert 'secret reply' not in body
        assert 'public child' in body


def test_topic_comments_omit_hidden_replies(app, world):
    from app.models import Topic
    w = world
    topic = Topic(name='news', machine_name='news', num_communities=1, show_posts_in_children=False)
    db.session.add(topic)
    db.session.commit()
    w.community.topic_id = topic.id
    db.session.commit()
    for who in (w.follower, w.stranger):
        body = client_as(app, who).get('/topic/news?content_type=comments').get_data(as_text=True)
        assert 'secret reply' not in body
        assert 'public child' in body


@pytest.mark.parametrize('path', ['/c/microblogs/outbox', '/c/microblogs/featured'])
def test_community_collections_omit_non_public_posts(app, world, path):
    w = world
    w.community.ap_id = None
    for post in (w.public_post, *w.hidden):
        post.sticky = True
    db.session.commit()
    response = app.test_client().get(path, headers={'Accept': 'application/activity+json'})
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'https://m.example/s/2' in body
    for post in w.hidden:
        assert post.ap_id not in body


@pytest.mark.parametrize('who', ['follower', 'stranger'])
def test_api_post_list_omits_non_public_posts(app, world, who):
    w = world
    viewer = getattr(w, who)
    for params in ({}, {'community_id': w.community.id}, {'type_': 'Local'}):
        response = app.test_client().get('/api/alpha/post/list', query_string=params,
                                         headers={'Authorization': bearer(viewer)})
        assert response.status_code == 200, params
        ids = {p['post']['id'] for p in response.get_json()['posts']}
        assert w.public_post.id in ids, params
        assert not ids & {p.id for p in w.hidden}, params


def test_api_post_list_omits_non_public_posts_anonymously(app, world):
    w = world
    response = app.test_client().get('/api/alpha/post/list', query_string={'community_id': w.community.id})
    ids = {p['post']['id'] for p in response.get_json()['posts']}
    assert w.public_post.id in ids
    assert not ids & {p.id for p in w.hidden}


@pytest.mark.parametrize('who', ['follower', 'stranger'])
def test_api_reply_list_omits_hidden_replies_in_listing_modes(app, world, who):
    w = world
    viewer = getattr(w, who)
    for params in ({}, {'community_id': w.community.id}, {'q': 'secret'}):
        response = app.test_client().get('/api/alpha/comment/list', query_string=params,
                                         headers={'Authorization': bearer(viewer)})
        assert response.status_code == 200, params
        ids = [c['comment']['id'] for c in response.get_json()['comments']]
        assert w.reply.id not in ids, params
    listed = app.test_client().get('/api/alpha/comment/list', query_string={'community_id': w.community.id},
                                   headers={'Authorization': bearer(viewer)})
    assert w.public_child.id in [c['comment']['id'] for c in listed.get_json()['comments']]


def test_api_reply_list_keeps_a_stub_inside_a_thread(app, world):
    w = world
    response = app.test_client().get('/api/alpha/comment/list', query_string={'post_id': w.public_post.id},
                                     headers={'Authorization': bearer(w.stranger)})
    assert response.status_code == 200
    comments = {c['comment']['id']: c['comment'] for c in response.get_json()['comments']}
    assert w.reply.id in comments and 'secret reply' not in str(comments[w.reply.id])
    assert w.public_child.id in comments


# ---- the aggregate feed -------------------------------------------------------------

@pytest.fixture
def feed_world(world):
    return world


def test_community_source_lists_public_posts_only(app, feed_world):
    w = feed_world
    for who in (w.follower, w.stranger):
        ids = feed_ids(app, who, [w.community.id])
        assert w.public_post.id in ids
        assert not set(ids) & {p.id for p in w.hidden}


def test_followed_author_source_gives_the_follower_the_hidden_posts(app, feed_world):
    w = feed_world
    ids = feed_ids(app, w.follower, [w.community.id], include_following=True)
    assert {w.post.id, w.followers_post.id, w.unlisted_post.id, w.public_post.id} <= set(ids)


def test_followed_author_source_hides_followers_only_from_a_pending_follower(app, feed_world):
    w = feed_world
    pending_ids = feed_ids(app, w.pending, [w.community.id], include_following=True)
    assert not set(pending_ids) & {w.post.id, w.followers_post.id}


def test_followed_author_source_gives_the_stranger_nothing_hidden(app, feed_world):
    w = feed_world
    other = make_user(make_instance('o.example'), 'olive')
    make_follow(w.stranger, other)
    ids = feed_ids(app, w.stranger, [w.community.id], include_following=True)
    assert not set(ids) & {p.id for p in w.hidden}


def test_following_a_booster_does_not_grant_the_authors_audience(app, feed_world):
    w = feed_world
    booster = make_user(make_instance('b.example'), 'booker')
    make_follow(w.stranger, booster)
    record_boost(w.followers_post, booster)
    record_boost(w.post, booster)
    record_boost(w.public_post, booster)
    ids = feed_ids(app, w.stranger, [w.community.id], include_following=True)
    assert not set(ids) & {w.post.id, w.followers_post.id}
    assert w.public_post.id in ids


def test_a_follower_of_the_booster_who_also_follows_the_author_sees_the_boost(app, feed_world):
    w = feed_world
    booster = make_user(make_instance('b.example'), 'booker')
    make_follow(w.follower, booster)
    record_boost(w.followers_post, booster)
    ids = feed_ids(app, w.follower, [w.community.id], include_following=True)
    assert w.followers_post.id in ids


def test_community_ical_omits_non_public_events(app, world):
    from datetime import timedelta
    from app.constants import POST_TYPE_EVENT
    from app.models import Event, utcnow
    w = world
    for post in (w.public_post, w.followers_post, w.unlisted_post):
        post.type = POST_TYPE_EVENT
        post.title = f'event {post.id}'
        db.session.add(Event(post_id=post.id, start=utcnow(), end=utcnow() + timedelta(hours=1)))
    db.session.commit()
    body = client_as(app, w.follower).get('/community/microblogs/ical').get_data(as_text=True)
    assert f'event {w.public_post.id}' in body
    assert f'event {w.followers_post.id}' not in body
    assert f'event {w.unlisted_post.id}' not in body
