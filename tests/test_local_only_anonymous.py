"""Only logged-in users see local-only content (plan B task 9, spec §3.4).

With `private_instance` off, anonymous visitors reach the web pages, RSS, the sitemap, link previews and the alpha
API. Each leaves local-only posts and comments out: one asked for by its own URL is 404 as if it did not exist, and a
list omits it. A logged-in user sees it everywhere, and an RSS reader with a member's `?token=` sees it in feeds.
"""
import glob
import json
import os
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest
from cachelib import SimpleCache
from flask import g

import app.local_only as local_only
from app import cache, db
from app.nntp.nntpserver import NNTPArticleNotFound
from app.nntp.server import CommunityArticleIndex, IndexArticleDict, PieFedNNTPServer, _make_message_id, _tl
from app.constants import POST_TYPE_EVENT
from app.models import Event, ModLog, Post, PostReply, Site, Tag, Topic, utcnow
from app.post.util import convert_archived_replies_to_tree
from app.utils import archive_post
from app.visibility import RestrictedReply, can_view, mark_restricted
from tests.conftest import forget_login
from tests.factories import bearer, make_domain, make_feed_item, make_local_feed
from tests.test_community_lifecycle import login
from tests.test_local_only_invisible import _post, _reply, world  # noqa: F401 -- world is a fixture

pytestmark = pytest.mark.usefixtures('site')

HTML = {'Accept': 'text/html'}


def _content(post_or_reply, text):
    if isinstance(post_or_reply, Post):
        post_or_reply.title = text
    post_or_reply.visibility = 'public'
    post_or_reply.body = f'{text} body'
    post_or_reply.body_html = f'<p>{text} body</p>'


@pytest.fixture
def scene(app, world, monkeypatch):
    """On a public instance: hell.cloud's `clearnet` community, in a topic and a public feed, holding a public post
    and a local-only one (same tag, same link domain), and on the public post an ordinary reply and a local-only one.
    alice has an RSS token."""
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # the post page renders form.csrf_token()
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    site = db.session.get(Site, 1)
    site.private_instance = False
    topic = Topic(name='Things', machine_name='things')
    db.session.add(topic)
    tag = Tag(name='lotag', display_as='lotag')
    shared_tag = Tag(name='xtag', display_as='xtag')
    hidden_tag = Tag(name='hiddentag', display_as='hiddentag')
    db.session.add_all([tag, shared_tag, hidden_tag])
    domain = make_domain('example.com')
    db.session.commit()
    world.clearnet.topic_id = topic.id
    world.clearnet.show_all = world.clearnet.show_popular = True
    feed = make_local_feed('lofeed', public=True)
    feed.user_id = world.alice.id
    make_feed_item(feed, world.clearnet)
    world.alice.rss_token = 'alice-rss-token'
    world.alice.ap_profile_id = f"{app.config['SERVER_URL']}/u/alice"   # what /u/alice/feed looks the account up by
    public = _post(app, world.clearnet, world.alice)
    hidden = _post(app, world.clearnet, world.alice, local_only=True)
    for post, text in ((public, 'PUBLICPOST'), (hidden, 'HIDDENPOST')):
        _content(post, text)
        post.url = f'https://example.com/{text.lower()}'
        post.domain_id = domain.id
        post.tags.extend([tag, shared_tag])
        post.indexable = True
    hidden.tags.append(hidden_tag)
    ordinary = _reply(app, public, world.alice)
    hidden_reply = _reply(app, public, world.alice, local_only=True)
    _content(ordinary, 'PUBLICREPLY')
    _content(hidden_reply, 'HIDDENREPLY')
    for reply in (ordinary, hidden_reply):
        reply.path = [0, reply.id]   # the factory leaves path empty; PostReply.new sets it
    public.reply_count = 2
    db.session.commit()
    g.admin_ids = []
    return SimpleNamespace(world=world, public=public, hidden=hidden, ordinary=ordinary, hidden_reply=hidden_reply,
                           domain=domain, alice=world.alice)


def _client(app):
    forget_login()
    return app.test_client()


def _anon(app, path, **kwargs):
    return _client(app).get(path, headers=HTML, **kwargs)


def _member(app, user, path):
    client = _client(app)
    login(client, user)
    return client.get(path, headers=HTML)


def _text(response):
    return response.get_data(as_text=True)


# One item, asked for by its own URL

def test_an_anonymous_visitor_gets_404_for_a_local_only_post_page(app, scene):
    assert _anon(app, f'/post/{scene.hidden.id}').status_code == 404
    response = _member(app, scene.alice, f'/post/{scene.hidden.id}')
    assert response.status_code == 200
    assert 'HIDDENPOST' in _text(response)
    assert _anon(app, f'/post/{scene.public.id}').status_code == 200


@pytest.mark.parametrize('suffix', ['embed', 'embed_code', 'oembed', 'cross_posts', 'options_menu', 'share_mastodon',
                                    'lazy_replies/abc'])
def test_the_embeds_and_fragments_of_a_local_only_post_are_404(app, scene, suffix):
    scene.hidden.cross_posts = [scene.public.id]
    db.session.commit()
    assert _anon(app, f'/post/{scene.hidden.id}/{suffix}').status_code == 404
    assert _member(app, scene.alice, f'/post/{scene.hidden.id}/{suffix}').status_code == 200


def test_oembed_of_a_public_post_still_answers(app, scene):
    response = _anon(app, f'/post/{scene.public.id}/oembed')
    assert response.status_code == 200
    assert response.json['title'] == 'PUBLICPOST'


def test_an_anonymous_visitor_gets_404_for_a_local_only_comment_permalink(app, scene):
    for path in (f'/post/{scene.public.id}/comment/{scene.hidden_reply.id}', f'/comment/{scene.hidden_reply.id}'):
        assert _anon(app, path).status_code == 404, path
        response = _member(app, scene.alice, path)
        assert response.status_code == 200, path
        assert 'HIDDENREPLY' in _text(response), path
    client = _client(app)
    assert client.post(f'/post/{scene.public.id}/comment/{scene.hidden_reply.id}/ajax/abc').status_code == 404
    options = f'/post/{scene.public.id}/comment/{scene.hidden_reply.id}/options_menu'
    assert _anon(app, options).status_code == 404
    assert _member(app, scene.alice, options).status_code == 200
    response = _anon(app, f'/post/{scene.public.id}/comment/{scene.ordinary.id}')
    assert response.status_code == 200
    assert 'PUBLICREPLY' in _text(response)


def test_a_public_posts_thread_omits_local_only_replies_for_an_anonymous_visitor(app, scene):
    page = _text(_anon(app, f'/post/{scene.public.id}'))
    assert 'PUBLICREPLY' in page
    assert 'HIDDENREPLY' not in page
    page = _text(_anon(app, f'/post/{scene.public.id}/lazy_replies/abc'))
    assert 'PUBLICREPLY' in page and 'HIDDENREPLY' not in page
    assert 'HIDDENREPLY' in _text(_member(app, scene.alice, f'/post/{scene.public.id}'))


def test_a_public_posts_cross_posts_omit_a_local_only_one(app, scene):
    scene.public.cross_posts = [scene.hidden.id]
    db.session.commit()
    assert f'/post/{scene.hidden.id}' not in _text(_anon(app, f'/post/{scene.public.id}/cross_posts'))
    assert f'/post/{scene.hidden.id}' in _text(_member(app, scene.alice, f'/post/{scene.public.id}/cross_posts'))
    assert 'HIDDENREPLY' not in _text(_anon(app, f'/post/{scene.public.id}'))


def test_a_public_posts_link_preview_tags_are_unchanged(app, scene):
    page = _text(_anon(app, f'/post/{scene.public.id}'))
    assert '<meta name="description" content="PUBLICPOST body" />' in page
    assert f'/post/{scene.public.id}/oembed' in page
    assert 'rel="alternate" type="application/activity+json"' in page


def test_a_local_only_post_page_offers_no_oembed_or_activitypub_alternate(app, scene):
    page = _text(_member(app, scene.alice, f'/post/{scene.hidden.id}'))
    assert f'/post/{scene.hidden.id}/oembed' not in page
    assert 'rel="alternate" type="application/activity+json"' not in page


# Lists

@pytest.mark.parametrize('path', ['/home/new/all', '/c/clearnet', '/c/clearnet?content_type=comments', '/u/alice',
                                  '/tag/lotag', '/tags/posts/{tag}', '/d/{domain}', '/topic/things',
                                  '/topic/things?content_type=comments', '/f/lofeed', '/instance/{server}/posts'])
def test_anonymous_listings_omit_local_only_content(app, scene, path):
    tag_id = db.session.query(Tag.id).filter_by(name='lotag').scalar()
    path = path.format(tag=tag_id, domain=scene.domain.id, server=app.config['SERVER_NAME'])
    anonymous = _anon(app, path)
    assert anonymous.status_code == 200, path
    member = _member(app, scene.alice, path)
    assert member.status_code == 200, path
    hidden, shown = ('HIDDENREPLY', 'PUBLICREPLY') if 'comments' in path or path == '/u/alice' else \
        ('HIDDENPOST', 'PUBLICPOST')
    assert shown in _text(anonymous), path
    assert hidden not in _text(anonymous), path
    assert hidden in _text(member), path
    if path == '/u/alice':
        assert 'HIDDENPOST' not in _text(anonymous) and 'HIDDENPOST' in _text(member)


@pytest.mark.parametrize('path', ['/home/new/all', '/c/clearnet', '/u/alice', '/tag/lotag',
                                  '/search?q=body&search_for=posts'])
def test_an_rss_token_does_not_unlock_an_html_list(app, scene, path):
    response = _anon(app, f"{path}{'&' if '?' in path else '?'}token=alice-rss-token")
    assert response.status_code == 200, path
    assert 'HIDDENPOST' not in _text(response), path


def _cloud(response):
    page = _text(response)
    tags = {tag['text']: tag for tag in json.loads(re.search(r'var tags = (.*?);\n', page).group(1))}
    relationships = json.loads(re.search(r'var tagRelationships = (.*?);\n', page).group(1))
    return tags, relationships


def test_the_tag_cloud_leaves_local_only_posts_out_for_an_anonymous_visitor(app, scene):
    path = f'/tags/cloud/community/{scene.world.clearnet.id}'
    tags, relationships = _cloud(_anon(app, path))
    assert 'hiddentag' not in tags
    assert tags['lotag']['numPosts'] == 1
    assert relationships[str(tags['lotag']['id'])] == {str(tags['xtag']['id']): 1}
    tags, relationships = _cloud(_member(app, scene.alice, path))
    assert 'hiddentag' in tags and tags['lotag']['numPosts'] == 2
    assert relationships[str(tags['lotag']['id'])][str(tags['xtag']['id'])] == 2


@pytest.mark.parametrize('path', ['/home/new/all', '/c/clearnet'])
def test_upcoming_events_name_no_local_only_event_to_an_anonymous_visitor(app, scene, path, monkeypatch):
    monkeypatch.setitem(app.extensions['cache'], cache, SimpleCache())   # the home sidebar is memoized
    soon = utcnow() + timedelta(days=1)
    for post in (scene.public, scene.hidden):
        post.type = POST_TYPE_EVENT
        db.session.add(Event(post_id=post.id, start=soon, end=soon))
    scene.hidden.title = 'HIDDENEVENT'   # the listing shows only HIDDENPOST's teaser, never this title
    db.session.commit()
    assert 'HIDDENEVENT' in _text(_member(app, scene.alice, path))
    assert 'HIDDENEVENT' not in _text(_anon(app, path))


def test_the_share_page_names_no_community_for_a_link_only_a_local_only_post_carries(app, scene):
    path = '/share?url=https://example.com/hiddenpost'
    assert 'already been shared' not in _text(_anon(app, path))
    assert 'already been shared' in _text(_member(app, scene.alice, path))
    assert 'already been shared' in _text(_anon(app, '/share?url=https://example.com/publicpost'))


def test_the_overview_of_a_profile_omits_local_only_content(app, scene):
    anonymous = _text(_anon(app, '/u/alice?overview_page=1'))
    assert 'HIDDENPOST' not in anonymous and 'HIDDENREPLY' not in anonymous
    assert 'PUBLICPOST' in anonymous


def test_search_omits_local_only_content(app, scene):
    for search_for, hidden in (('posts', 'HIDDENPOST'), ('comments', 'HIDDENREPLY')):
        path = f'/search?q=body&search_for={search_for}'
        assert hidden not in _text(_anon(app, path)), search_for
        assert hidden in _text(_member(app, scene.alice, path)), search_for


def test_an_instance_sticky_local_only_post_is_not_on_the_anonymous_home_page(app, scene):
    scene.hidden.instance_sticky = True
    db.session.commit()
    assert 'HIDDENPOST' not in _text(_anon(app, '/home/hot/all'))
    assert 'HIDDENPOST' in _text(_member(app, scene.alice, '/home/hot/all'))


def test_the_modlog_names_no_local_only_post_to_an_anonymous_visitor(app, scene):
    db.session.add_all([ModLog(user_id=scene.alice.id, community_id=scene.world.clearnet.id, type='mod',
                               action='lock_post', post_id=post.id, link_text=post.title, public=True,
                               link=f'post/{post.id}') for post in (scene.public, scene.hidden)])
    db.session.add(ModLog(user_id=scene.alice.id, community_id=scene.world.clearnet.id, type='mod',
                          action='delete_post_reply', reply_id=scene.hidden_reply.id, link_text='HIDDENREPLY',
                          public=True))
    db.session.commit()
    anonymous = _text(_anon(app, '/modlog'))
    assert 'PUBLICPOST' in anonymous
    assert 'HIDDENPOST' not in anonymous and 'HIDDENREPLY' not in anonymous
    member = _text(_member(app, scene.alice, '/modlog'))
    assert 'HIDDENPOST' in member and 'HIDDENREPLY' in member


def _ids(value, keys=('post_id', 'comment_id', 'id')):
    if isinstance(value, dict):
        return {item for key, inner in value.items()
                for item in ([(key, inner)] if key in keys else []) + list(_ids(inner, keys))}
    if isinstance(value, list):
        return {item for inner in value for item in _ids(inner, keys)}
    return set()


def test_the_api_modlog_leaves_entries_about_local_only_content_out_for_an_anonymous_caller(app, scene):
    db.session.add_all([ModLog(user_id=scene.alice.id, community_id=scene.world.clearnet.id, type='mod',
                               action='lock_post', post_id=post.id, link_text=post.title, public=True)
                        for post in (scene.public, scene.hidden)])
    db.session.add(ModLog(user_id=scene.alice.id, community_id=scene.world.clearnet.id, type='mod',
                          action='delete_post_reply', reply_id=scene.hidden_reply.id, reason='HIDDENREASON',
                          public=True))
    db.session.commit()
    anonymous = _api(app, 'modlog')
    assert anonymous.status_code == 200
    ids = _ids(anonymous.json)
    assert ('post_id', scene.public.id) in ids
    assert ('post_id', scene.hidden.id) not in ids and ('comment_id', scene.hidden_reply.id) not in ids
    assert 'HIDDENREASON' not in anonymous.get_data(as_text=True)
    member = _api(app, 'modlog', scene.alice)
    assert {('post_id', scene.hidden.id), ('comment_id', scene.hidden_reply.id)} <= _ids(member.json)


# RSS: anonymous readers never, a member's token always

RSS_PATHS = ['/index/feed/all', '/community/clearnet/feed', '/u/alice/feed', '/tag/lotag/feed', '/d/{domain}/feed',
             '/topic/things.rss', '/f/lofeed.rss']


@pytest.mark.parametrize('path', RSS_PATHS)
def test_rss_without_a_token_omits_local_only_posts_and_with_one_includes_them(app, scene, path):
    path = path.format(domain=scene.domain.id)
    anonymous = _client(app).get(path)
    assert anonymous.status_code == 200, path
    assert 'PUBLICPOST' in _text(anonymous), path
    assert 'HIDDENPOST' not in _text(anonymous), path
    with_token = _client(app).get(path, query_string={'token': 'alice-rss-token'})
    assert with_token.status_code == 200, path
    assert 'HIDDENPOST' in _text(with_token), path
    wrong_token = _client(app).get(path, query_string={'token': 'not-a-token'})
    assert 'HIDDENPOST' not in _text(wrong_token), path


@pytest.mark.parametrize('path', RSS_PATHS)
def test_a_logged_in_session_does_not_unlock_rss(app, scene, path):
    path = path.format(domain=scene.domain.id)
    response = _member(app, scene.alice, path)
    assert response.status_code == 200, path
    assert 'PUBLICPOST' in _text(response) and 'HIDDENPOST' not in _text(response), path


@pytest.mark.parametrize('path', ['/community/clearnet/feed', '/u/alice/feed', '/topic/things.rss'])
def test_a_cached_feed_read_by_a_logged_in_reader_is_not_replayed_to_an_anonymous_one(app, scene, path,
                                                                                     monkeypatch):
    monkeypatch.setitem(app.extensions['cache'], cache, SimpleCache())
    assert _member(app, scene.alice, path).status_code == 200
    assert 'HIDDENPOST' not in _text(_client(app).get(path))


@pytest.mark.parametrize('path', ['/community/clearnet/feed', '/u/alice/feed', '/topic/things.rss'])
def test_a_cached_feed_read_with_a_token_is_not_replayed_to_an_anonymous_reader(app, scene, path, monkeypatch):
    monkeypatch.setitem(app.extensions['cache'], cache, SimpleCache())
    assert 'HIDDENPOST' in _text(_client(app).get(path, query_string={'token': 'alice-rss-token'}))
    assert 'HIDDENPOST' not in _text(_client(app).get(path))


def test_the_ical_feed_omits_a_local_only_event(app, scene):
    for post in (scene.public, scene.hidden):
        post.type = POST_TYPE_EVENT
        db.session.add(Event(post_id=post.id, start=post.posted_at, end=post.posted_at))
    db.session.commit()
    assert 'HIDDENPOST' not in _text(_anon(app, '/community/clearnet/ical'))
    assert 'HIDDENPOST' in _text(_member(app, scene.alice, '/community/clearnet/ical'))
    assert _anon(app, f'/post/{scene.hidden.id}/ical').status_code == 404


def test_the_sitemap_omits_local_only_posts_even_for_a_logged_in_crawler(app, scene):
    for response in (_anon(app, '/sitemap.xml'), _member(app, scene.alice, '/sitemap.xml')):
        assert response.status_code == 200
        assert f'/post/{scene.public.id}' in _text(response)
        assert f'/post/{scene.hidden.id}' not in _text(response)


# The alpha API: anonymous calls never, a token user always

def _api(app, path, user=None, **query):
    headers = {'Authorization': bearer(user)} if user else {}
    return _client(app).get(f'/api/alpha/{path}', query_string=query, headers=headers)


def _post_ids(response):
    return {item['post']['id'] for item in response.json['posts']}


def _comment_ids(response):
    return {item['comment']['id'] for item in response.json['comments']}


@pytest.mark.parametrize('path', ['post/list', 'post/list2'])
def test_an_anonymous_api_post_list_omits_local_only_posts(app, scene, path):
    anonymous = _post_ids(_api(app, path, type_='All', sort='New'))
    assert scene.public.id in anonymous and scene.hidden.id not in anonymous
    assert scene.hidden.id in _post_ids(_api(app, path, scene.alice, type_='All', sort='New'))


def test_an_anonymous_api_person_listing_omits_local_only_content(app, scene):
    response = _api(app, 'user', person_id=scene.alice.id, include_content='true')
    assert response.status_code == 200
    assert {item['post']['id'] for item in response.json['posts']} == {scene.public.id}
    assert {item['comment']['id'] for item in response.json['comments']} == {scene.ordinary.id}


@pytest.mark.parametrize('query', [{'post_id': None}, {'post_id': None, 'depth_first': 'true'}, {}])
def test_an_anonymous_api_comment_list_omits_local_only_comments(app, scene, query):
    query = {key: scene.public.id if value is None else value for key, value in query.items()}
    anonymous = _comment_ids(_api(app, 'comment/list', **query))
    assert scene.ordinary.id in anonymous and scene.hidden_reply.id not in anonymous
    assert scene.hidden_reply.id in _comment_ids(_api(app, 'comment/list', scene.alice, **query))


def test_anonymous_api_post_replies_omit_local_only_comments(app, scene):
    anonymous = _comment_ids(_api(app, 'post/replies', post_id=scene.public.id))
    assert anonymous == {scene.ordinary.id}
    member = _comment_ids(_api(app, 'post/replies', scene.alice, post_id=scene.public.id))
    assert member == {scene.ordinary.id, scene.hidden_reply.id}


def test_an_anonymous_api_get_of_a_local_only_post_or_comment_is_not_found(app, scene):
    response = _api(app, 'post', id=scene.hidden.id)
    assert response.status_code == 400
    assert _api(app, 'post', id=scene.hidden.id + 1000).json == response.json   # as for a missing post
    assert _api(app, 'post', scene.alice, id=scene.hidden.id).status_code == 200
    assert _api(app, 'comment', id=scene.hidden_reply.id).status_code == 400
    assert _api(app, 'comment', scene.alice, id=scene.hidden_reply.id).status_code == 200


def test_an_anonymous_api_search_omits_local_only_content(app, scene):
    posts = _post_ids(_api(app, 'search', q='body', type_='Posts'))
    assert scene.hidden.id not in posts
    comments = _comment_ids(_api(app, 'search', q='body', type_='Comments'))
    assert scene.hidden_reply.id not in comments


def test_resolve_object_finds_local_only_content_for_a_token_user_only(app, scene):
    """A guard, not a change: resolve_object refuses every anonymous call (PERM-1)."""
    for obj in (scene.hidden, scene.hidden_reply):
        query = {'q': obj.ap_id}
        assert _api(app, 'resolve_object', **query).status_code == 400, obj
        assert _api(app, 'resolve_object', scene.alice, **query).status_code == 200, obj


def test_an_api_post_view_names_no_local_only_cross_post_anonymously(app, scene):
    scene.public.cross_posts = [scene.hidden.id]
    db.session.commit()
    anonymous = _api(app, 'post', id=scene.public.id).json
    assert anonymous['post_view']['post']['cross_posts'] == []
    assert anonymous['cross_posts'] == []
    member = _api(app, 'post', scene.alice, id=scene.public.id).json
    assert [item['post_id'] for item in member['post_view']['post']['cross_posts']] == [scene.hidden.id]
    assert [item['post']['id'] for item in member['cross_posts']] == [scene.hidden.id]


# NNTP: GROUP and ARTICLE answer before AUTHINFO, so an unauthenticated reader is anonymous

@pytest.fixture
def nntp_reader():
    """Sets the authenticated account of this thread's NNTP connection, as _authenticate does."""
    def authenticate(user):
        _tl.user_id = user.id
    yield authenticate
    _tl.__dict__.pop('user_id', None)


def test_an_anonymous_nntp_reader_is_not_given_a_local_only_article(app, scene, nntp_reader):
    domain = app.config['SERVER_NAME']
    server = SimpleNamespace(_app=app, _domain=domain)
    articles = IndexArticleDict(None, app, domain)
    hidden_post, hidden_reply = _make_message_id('post', scene.hidden.id, domain), \
        _make_message_id('reply', scene.hidden_reply.id, domain)
    for message_id in (hidden_post, hidden_reply):
        with pytest.raises(NNTPArticleNotFound):
            PieFedNNTPServer._fetch_body_and_headers(server, message_id)
        with pytest.raises(NNTPArticleNotFound):
            articles[message_id]
    index = CommunityArticleIndex(scene.world.clearnet.id, app, domain)
    assert index.count == 4   # a count leaks nothing
    numbers = {index._id_map[('post', scene.hidden.id)], index._id_map[('reply', scene.hidden_reply.id)]}
    nntp_reader(scene.alice)
    assert 'HIDDENPOST body' in PieFedNNTPServer._fetch_body_and_headers(server, hidden_post)[0]
    assert PieFedNNTPServer._fetch_body_and_headers(server, hidden_reply)[0] == 'HIDDENREPLY body'
    assert articles[hidden_post].subject == 'HIDDENPOST'
    assert articles[hidden_reply].subject == 'Re: PUBLICPOST'
    for number in numbers:   # read by the authenticated reader, so cached in the index every reader shares
        assert index.get_info(number)
    _tl.__dict__.pop('user_id')
    for number in numbers:
        with pytest.raises(NNTPArticleNotFound):
            index.get_info(number)
    ordinary = index.get_info(index._id_map[('reply', scene.ordinary.id)])
    assert ordinary.subject == 'Re: PUBLICPOST'


# The helpers

def test_can_view_is_false_for_local_only_content_without_a_viewer(app, scene):
    assert not can_view(scene.hidden, None)
    assert not can_view(scene.hidden_reply, None)
    assert can_view(scene.hidden, scene.world.bob.id)
    assert can_view(scene.public, None)


def test_local_only_visible_and_without_local_only(app, scene):
    with app.test_request_context('/'):
        assert not local_only.local_only_visible()
        assert local_only.without_local_only(Post.query, Post).filter(Post.id == scene.hidden.id).first() is None
        assert local_only.without_local_only(Post.query, Post, visible=True).filter(Post.id == scene.hidden.id).first()
        assert not local_only.local_only_visible_rss()
    with app.test_request_context('/?token=alice-rss-token'):
        assert not local_only.local_only_visible()   # a token is for RSS views only
        assert local_only.without_local_only(PostReply.query, PostReply).filter(
            PostReply.id == scene.hidden_reply.id).first() is None
        assert local_only.local_only_visible_rss()


ARCHIVED_DIR = 'app/static/media/archived'


@pytest.fixture
def archive_dir():
    """The archive files a test writes, removed afterwards."""
    written = set(glob.glob(f'{ARCHIVED_DIR}/*'))
    yield lambda: set(glob.glob(f'{ARCHIVED_DIR}/*')) - written
    for path in set(glob.glob(f'{ARCHIVED_DIR}/*')) - written:
        if os.path.isfile(path):
            os.unlink(path)


def test_a_thread_holding_a_local_only_reply_is_not_archived(app, scene, archive_dir):
    """An archive is a public file (S3_PUBLIC_URL or app/static/media/archived): it would publish the reply."""
    archive_post(scene.public.id, None)
    db.session.expire_all()
    assert db.session.get(Post, scene.public.id).archived is None
    assert archive_dir() == set()
    assert db.session.get(PostReply, scene.hidden_reply.id).body == 'HIDDENREPLY body'
    assert db.session.get(PostReply, scene.ordinary.id).body == 'PUBLICREPLY body'


def test_a_local_only_post_is_not_archived(app, scene, archive_dir):
    scene.hidden.body = 'HIDDENPOST ' * 30   # long enough to be archived
    db.session.commit()
    archive_post(scene.hidden.id, None)
    db.session.expire_all()
    assert db.session.get(Post, scene.hidden.id).archived is None
    assert archive_dir() == set()
    assert db.session.get(Post, scene.hidden.id).body.startswith('HIDDENPOST')


def test_a_thread_with_no_local_only_content_is_still_archived(app, scene, archive_dir):
    db.session.delete(scene.hidden_reply)
    scene.public.reply_count = 1
    db.session.commit()
    archive_post(scene.public.id, None)
    db.session.expire_all()
    assert db.session.get(Post, scene.public.id).archived
    assert archive_dir()
    assert 'PUBLICREPLY' in _text(_anon(app, f'/post/{scene.public.id}'))


def test_an_archive_from_before_local_only_content_works_out_which_replies_are(app, scene):
    """An archive written before 923e66349 has no 'local_only' key. Each reply is worked out as PostReply.new would,
    and one that cannot be is treated as local-only."""
    def entry(reply_id, author, replies=(), **extra):
        return {'id': reply_id, 'body': f'body {reply_id}', 'author_id': author, 'replies': list(replies), **extra}
    world = scene.world
    archived = [
        entry(901, world.alice.id, [entry(902, world.alice.id)]),           # hell.cloud: open
        entry(903, world.onionite.id, [entry(904, world.alice.id)]),        # local-only author; a reply under it
        entry(905, 999_999),                                                 # no such author
        entry(906, None),                                                    # no author at all
        entry(907, world.alice.id, [entry(908, world.alice.id)], visibility='followers'),   # a stub's child
        entry(909, world.onionite.id, local_only=False),                     # the key, when present, is the answer
    ]
    tree = mark_restricted(convert_archived_replies_to_tree(archived, scene.public), None)

    def shown(entries):
        found = {}
        for item in entries:
            found[item['comment'].id] = not isinstance(item['comment'], RestrictedReply)
            found.update(shown(item['replies']))
        return found
    assert shown(tree) == {901: True, 902: True, 903: False, 904: False, 905: False, 906: False, 907: False,
                           908: False, 909: True}
    member = mark_restricted(convert_archived_replies_to_tree(archived, scene.public), scene.alice.id)
    assert shown(member)[903] and shown(member)[904]
