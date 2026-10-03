"""Listings, search, RSS and the sitemap show public content only (interop D7); the
followed-author feed source obeys the viewer predicate.

Every hidden row is checked for the follower AND the stranger: a follower may open a
followers-only post by its permalink (Task 4), but no listing offers it.
"""
import json
import re
from datetime import timedelta

import pytest
from flask import current_app, g

from app import db
from app.activitypub.util import record_boost
from app.constants import POST_TYPE_EVENT
from app.models import Domain, Event, Language, Site, Tag, Topic, post_tag, utcnow
from tests.factories import (
    bearer,
    feed_ids,
    make_follow,
    make_instance,
    make_post,
    make_post_reply,
    make_user,
    make_visibility_world,
)
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
    assert not set(pending_ids) & {w.post.id, w.followers_post.id, w.unlisted_post.id}


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


def test_community_comments_judge_a_reply_by_its_own_visibility(app, world):
    w = world
    w.public_post.visibility = 'followers'
    db.session.commit()
    body = client_as(app, w.stranger).get('/c/microblogs?content_type=comments').get_data(as_text=True)
    assert 'public child' in body
    assert 'secret reply' not in body


# ---- sites the sweep above does not reach -----------------------------------------------

def add_search_reply(w, body, visibility):
    reply = make_post_reply(w.public_post, w.author, body)
    reply.visibility = visibility
    reply.indexable = True
    db.session.commit()
    return reply


@pytest.mark.parametrize('who', ['follower', 'stranger', None])
def test_comment_search_omits_hidden_replies_and_finds_the_public_one(app, world, who):
    w = world
    for reply in (w.reply, w.public_child):
        reply.indexable = True
    w.public_post.indexable = True
    db.session.commit()
    viewer = getattr(w, who) if who else None
    body = client_as(app, viewer).get('/search?q=secret&search_for=comments').get_data(as_text=True)
    assert 'secret reply' not in body
    visible = add_search_reply(w, f'{WORD} openreply', 'public')
    body = client_as(app, viewer).get(f'/search?q={WORD}&search_for=comments').get_data(as_text=True)
    assert 'openreply' in body, visible.id


def test_comment_search_omits_a_hidden_reply_that_matches(app, world):
    w = world
    w.public_post.indexable = True
    add_search_reply(w, f'{WORD} hiddenreply', 'followers')
    add_search_reply(w, f'{WORD} openreply', 'public')
    for who in (w.follower, w.stranger, None):
        body = client_as(app, who).get(f'/search?q={WORD}&search_for=comments').get_data(as_text=True)
        assert 'openreply' in body
        assert 'hiddenreply' not in body


@pytest.fixture
def two_tags(world):
    """A second tag on the public post and on every hidden one: it must co-occur with the first once, not four times."""
    w = world
    other = Tag(name='aurora', display_as='aurora', banned=False, post_count=0)
    db.session.add(other)
    db.session.commit()
    for post in (w.public_post, *w.hidden):
        db.session.execute(post_tag.insert().values(post_id=post.id, tag_id=other.id))
    db.session.commit()
    w.tag = Tag.query.filter_by(name=TAG).one()
    w.other_tag = other
    return w


def page_var(response, name):
    return json.loads(re.search(rf'var {name} = (.*?);\s*\n', response.get_data(as_text=True)).group(1))


@pytest.mark.parametrize('who', ['follower', 'stranger', None])
def test_tag_cloud_counts_and_relates_public_posts_only(app, two_tags, who):
    w = two_tags
    viewer = getattr(w, who) if who else None
    response = client_as(app, viewer).get(f'/tags/cloud/community/{w.community.id}')
    assert response.status_code == 200
    counts = {t['text']: t['numPosts'] for t in page_var(response, 'tags')}
    assert counts == {TAG: 1, 'aurora': 1}
    relationships = page_var(response, 'tagRelationships')
    assert relationships == {str(w.tag.id): {str(w.other_tag.id): 1}, str(w.other_tag.id): {str(w.tag.id): 1}}


@pytest.mark.parametrize('who', ['follower', 'stranger', None])
def test_tag_cloud_of_a_topic_counts_public_posts_only(app, two_tags, who):
    w = two_tags
    topic = Topic(name='news', machine_name='news', num_communities=1, show_posts_in_children=False)
    db.session.add(topic)
    db.session.commit()
    w.community.topic_id = topic.id
    db.session.commit()
    viewer = getattr(w, who) if who else None
    response = client_as(app, viewer).get(f'/tags/cloud/topic/{topic.id}')
    assert {t['text']: t['numPosts'] for t in page_var(response, 'tags')} == {TAG: 1, 'aurora': 1}


@pytest.mark.parametrize('who', ['follower', 'stranger', None])
def test_tag_posts_lists_public_posts_only(app, world, who):
    w = world
    viewer = getattr(w, who) if who else None
    tag = Tag.query.filter_by(name=TAG).one()
    response = client_as(app, viewer).get(f'/tags/posts/{tag.id}')
    assert response.status_code == 200
    shown = post_links(response)
    assert w.public_post.id in shown
    assert not shown & {p.id for p in w.hidden}


@pytest.mark.parametrize('who', ['follower', 'stranger'])
def test_api_post_list2_omits_non_public_posts(app, world, who):
    w = world
    viewer = getattr(w, who)
    for params in ({}, {'community_id': w.community.id}):
        response = app.test_client().get('/api/alpha/post/list2', query_string=params,
                                         headers={'Authorization': bearer(viewer)})
        assert response.status_code == 200, (params, response.get_data(as_text=True))
        ids = {p['post']['id'] for p in response.get_json()['posts']}
        assert w.public_post.id in ids, params
        assert not ids & {p.id for p in w.hidden}, params


def test_api_post_list2_omits_non_public_posts_anonymously(app, world):
    w = world
    response = app.test_client().get('/api/alpha/post/list2')
    ids = {p['post']['id'] for p in response.get_json()['posts']}
    assert w.public_post.id in ids
    assert not ids & {p.id for p in w.hidden}


@pytest.mark.parametrize('who', ['follower', 'stranger'])
def test_instance_posts_for_a_logged_in_reader_with_content_filters_on(app, world, who):
    """The signed-in branch of /instance/<domain>/posts applies the viewer's own filters on top of the audience."""
    w = world
    viewer = getattr(w, who)
    viewer.ignore_bots, viewer.hide_nsfw, viewer.hide_nsfl, viewer.hide_read_posts = 1, 1, 1, True
    db.session.commit()
    response = client_as(app, viewer).get('/instance/piefed.test/posts')
    assert response.status_code == 200
    shown = post_links(response)
    assert w.public_post.id in shown
    assert not shown & {p.id for p in w.hidden}
