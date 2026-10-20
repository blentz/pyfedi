"""The Local filter means the domain the visitor is browsing (per-domain identity, plan B task 10, spec §3.1/§3.5).

hell.cloud's Local lists exactly what it listed before storefronts; the onion's lists only the onion's communities.
`on_domain` also accepts NULL on the default domain, and a remote community's `site_domain_id` is NULL as well, so the
remote community in every scene here is what pins that the old "is local" test is kept beside the domain test."""
import re
from types import SimpleNamespace

import pytest
from flask import current_app, g
from flask_login import login_user
from sqlalchemy import event

import app.storefront as storefront
from app import db
from app.models import Site
from app.main.routes import home_feed_source
from app.utils import get_deduped_post_ids
from tests.conftest import forget_login
from tests.factories import bearer, make_community, make_instance, make_post, make_post_reply, make_user
from tests.test_local_only_invisible import _post, _reply, world  # noqa: F401 -- world is a fixture

pytestmark = pytest.mark.usefixtures('site')

PROXY = '10.89.0.2'
ONION = 'http://abc.onion'
ONION_HEADERS = {'X-PieFed-Via': 'onion', 'X-Forwarded-For': '10.89.0.7'}


@pytest.fixture
def scene(app, world, monkeypatch):  # noqa: F811
    """`world`'s hell.cloud community `clearnet` and onion community `hidden`, plus a remote community `remote`
    (no domain stamped, as every remote row is). Each holds one post and one reply, titled for the community."""
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    db.session.get(Site, 1).private_instance = False   # anonymous visitors reach the pages
    peer = make_instance('peer.example')
    remote_user = make_user(peer, 'rem')
    remote = make_community('remote', host='peer.example')
    remote.ap_id = 'remote@peer.example'
    remote.instance_id = peer.id
    remote.user_id = remote_user.id
    world.alice.rss_token = 'alice-rss-token'
    world.onionite.rss_token = 'onionite-rss-token'
    posts = {}
    for key, community, author in (('clear', world.clearnet, world.alice), ('onion', world.hidden, world.onionite)):
        posts[key] = _post(app, community, author)
    posts['remote'] = make_post(remote, remote_user, 'https://peer.example/post/1')
    for key, title in (('clear', 'CLEARPOST'), ('onion', 'ONIONPOST'), ('remote', 'REMOTEPOST')):
        posts[key].title = title
    replies = {'clear': _reply(app, posts['clear'], world.alice), 'onion': _reply(app, posts['onion'], world.onionite)}
    replies['remote'] = make_post_reply(posts['remote'], remote_user)
    replies['remote'].ap_id = 'https://peer.example/comment/1'
    db.session.commit()
    assert remote.site_domain_id is None and remote.ap_id is not None
    world.remote = remote
    return SimpleNamespace(world=world, posts=SimpleNamespace(**posts), replies=SimpleNamespace(**replies),
                           remote=remote)


def _titles(text):
    return set(re.findall(r'(?:CLEAR|ONION|REMOTE)POST', text))


def _visit(app, path, *, onion=False, user=None, **kwargs):
    forget_login()
    client = app.test_client()
    base_url = ONION if onion else None
    if user is not None:
        with client.session_transaction(base_url=base_url) as session:
            session['_user_id'] = str(user.id)
            session['_fresh'] = True
    if not onion:
        return client.get(path, **kwargs)
    headers = {**ONION_HEADERS, **kwargs.pop('headers', {})}
    response = None
    for _ in range(3):   # a cookieless onion request is sent out and back to pick up its session (app/onion_guard.py)
        response = client.get(path, base_url=base_url, headers=headers, environ_base={'REMOTE_ADDR': PROXY}, **kwargs)
        if response.status_code != 303:
            break
        path = response.headers['Location']
    return response


# The web home page

def test_home_local_on_the_default_domain_lists_its_community_and_neither_the_onion_nor_a_remote_one(app, scene):
    text = _visit(app, '/home/new/local', user=scene.world.alice).get_data(as_text=True)
    assert _titles(text) == {'CLEARPOST'}
    assert _titles(_visit(app, '/home/new/local').get_data(as_text=True)) == {'CLEARPOST'}   # anonymous too


def test_home_local_on_the_onion_lists_only_the_onions_communities(app, scene):
    text = _visit(app, '/home/new/local', onion=True, user=scene.world.onionite).get_data(as_text=True)
    assert _titles(text) == {'ONIONPOST'}


def test_the_all_view_is_not_narrowed_by_the_domain(app, scene):
    text = _visit(app, '/home/new/all', onion=True, user=scene.world.onionite).get_data(as_text=True)
    assert {'CLEARPOST', 'ONIONPOST', 'REMOTEPOST'} <= _titles(text)


def _local_post_ids(storefront_info):
    """Local, as the home page's source for an anonymous reader on `storefront_info` asks for it."""
    with current_app.test_request_context('/'):
        g.storefront = storefront_info
        ids, sql = home_feed_source('local')
        return set(get_deduped_post_ids('', ids, 'new', community_sql=sql))


def test_the_local_source_filters_by_the_storefront_it_is_given(app, scene):
    default, onion = scene.world.default, scene.world.onion
    on_default = storefront.domain_by_id(default.id)
    on_onion = storefront.domain_by_id(onion.id)
    assert _local_post_ids(on_default) == {scene.posts.clear.id}
    assert _local_post_ids(on_onion) == {scene.posts.onion.id}


def test_the_synthesised_default_still_lists_the_unstamped_local_community(app, scene):
    scene.world.clearnet.site_domain_id = None   # a legacy row the migration has not stamped
    db.session.commit()
    synthesised = storefront._synthesised_default()
    assert synthesised.id is None
    assert _local_post_ids(synthesised) == {scene.posts.clear.id}


# The community list

def test_the_communities_list_local_filter_follows_the_domain(app, scene):
    clear = _visit(app, '/communities?home_select=local', user=scene.world.alice).get_data(as_text=True)
    assert '/c/clearnet' in clear and '/c/hidden' not in clear and '/c/remote' not in clear
    onion = _visit(app, '/communities?home_select=local', onion=True,
                   user=scene.world.onionite).get_data(as_text=True)
    assert '/c/hidden' in onion and '/c/clearnet' not in onion and '/c/remote' not in onion


# RSS

def test_the_local_rss_feed_follows_the_domain(app, scene):
    clear = _visit(app, '/index/feed/local?token=alice-rss-token').get_data(as_text=True)
    assert _titles(clear) == {'CLEARPOST'}
    onion = _visit(app, '/index/feed/local?token=onionite-rss-token', onion=True).get_data(as_text=True)
    assert _titles(onion) == {'ONIONPOST'}


def test_the_local_rss_feed_needs_a_token_for_local_only_content_still(app, scene):
    scene.posts.clear.local_only = True
    db.session.commit()
    assert _titles(_visit(app, '/index/feed/local').get_data(as_text=True)) == set()
    assert _titles(_visit(app, '/index/feed/local?token=alice-rss-token').get_data(as_text=True)) == {'CLEARPOST'}


# The alpha API

def _api(app, path, user=None, onion=False, **query):
    headers = {'Authorization': bearer(user)} if user else {}
    return _visit(app, f'/api/alpha/{path}', onion=onion, query_string=query, headers=headers)


def test_the_api_local_community_list_follows_the_domain(app, scene):
    def names(response):
        return {entry['community']['name'] for entry in response.json['communities']}
    assert names(_api(app, 'community/list', scene.world.alice, type_='Local')) == {'clearnet'}
    assert names(_api(app, 'community/list', scene.world.onionite, onion=True, type_='Local')) == {'hidden'}
    assert {'clearnet', 'hidden', 'remote'} <= names(_api(app, 'community/list', scene.world.alice, type_='All'))


@pytest.mark.parametrize('path', ['post/list', 'post/list2'])
def test_the_api_local_post_list_follows_the_domain(app, scene, path):
    def titles(response):
        return {item['post']['title'] for item in response.json['posts']}
    assert titles(_api(app, path, scene.world.alice, type_='Local', sort='New')) == {'CLEARPOST'}
    assert titles(_api(app, path, scene.world.onionite, onion=True, type_='Local', sort='New')) == {'ONIONPOST'}


def test_the_api_local_comment_list_follows_the_domain(app, scene):
    def ids(response):
        return {item['comment']['id'] for item in response.json['comments']}
    assert ids(_api(app, 'comment/list', scene.world.alice, type_='Local')) == {scene.replies.clear.id}
    assert ids(_api(app, 'comment/list', scene.world.onionite, onion=True, type_='Local')) == {scene.replies.onion.id}


def test_a_local_comment_belongs_to_its_authors_domain_wherever_it_is_posted(app, scene):
    def local_ids(user, onion=False):
        response = _api(app, 'comment/list', user, onion=onion, type_='Local')
        return {item['comment']['id'] for item in response.json['comments']}
    mine = make_post_reply(scene.posts.remote, scene.world.alice)
    theirs = make_post_reply(scene.posts.remote, scene.world.onionite)
    for reply in (mine, theirs):
        reply.ap_id = f"{app.config['SERVER_URL']}/comment/{reply.id}"
    db.session.commit()
    assert local_ids(scene.world.alice) == {scene.replies.clear.id, mine.id}   # as before storefronts
    assert local_ids(scene.world.onionite, onion=True) == {scene.replies.onion.id, theirs.id}


def test_the_api_local_user_list_follows_the_domain(app, scene):
    def names(response):
        return {entry['person']['user_name'] for entry in response.json['users']}
    peer_user = scene.remote.user_id
    assert peer_user is not None
    clear = names(_api(app, 'search', scene.world.alice, type_='Users', listing_type='Local', q=''))
    assert 'alice' in clear and 'onionite' not in clear and 'rem' not in clear
    onion = names(_api(app, 'search', scene.world.onionite, onion=True, type_='Users', listing_type='Local', q=''))
    assert 'onionite' in onion and 'alice' not in onion and 'rem' not in onion


# The helper

def test_the_domain_sql_binds_the_id_as_a_parameter(app, db_session):
    default = SimpleNamespace(id=7, is_default=True)
    onion = SimpleNamespace(id=9, is_default=False)
    synthesised = SimpleNamespace(id=None, is_default=True)
    bound = ':' + storefront.ON_DOMAIN_PARAM
    assert storefront.on_domain_sql(default) == f'(c.site_domain_id = {bound} OR c.site_domain_id IS NULL)'
    assert storefront.on_domain_sql(synthesised) == f'(c.site_domain_id = {bound} OR c.site_domain_id IS NULL)'
    assert storefront.on_domain_sql(onion, 'x') == f'x.site_domain_id = {bound}'


# The Live feed cache and validators name the domain

def test_a_result_id_from_one_domain_is_not_served_on_another(app, scene, redis_double):
    viewer = scene.world.alice

    def ids_on(info):
        with app.test_request_context('/'):
            login_user(viewer)
            g.storefront = info
            return get_deduped_post_ids('shared-result', [scene.world.clearnet.id, scene.world.hidden.id], 'new')
    default = storefront.domain_by_id(scene.world.default.id)
    onion = storefront.domain_by_id(scene.world.onion.id)
    first = ids_on(default)
    assert first   # cached under the default domain
    redis_double.set(f'feed:{default.domain}:{viewer.id}:shared-result', '[-999999]', ex=60)
    assert ids_on(default) == [-999999]
    assert ids_on(onion) != [-999999]


def test_the_etags_name_the_domain(app, scene):
    clear = _visit(app, '/index/feed/local?token=alice-rss-token')
    onion = _visit(app, '/index/feed/local?token=onionite-rss-token', onion=True)
    assert clear.headers['ETag'] != onion.headers['ETag']
    replay = _visit(app, '/index/feed/local?token=onionite-rss-token', onion=True,
                    headers={'If-None-Match': clear.headers['ETag']})
    assert replay.status_code == 200
    again = _visit(app, '/index/feed/local?token=onionite-rss-token', onion=True,
                   headers={'If-None-Match': onion.headers['ETag']})
    assert again.status_code == 304
    home = _visit(app, '/home/new/local').headers['ETag']
    assert storefront.current_storefront().domain in home or app.config['SERVER_NAME'] in home
    assert _visit(app, '/home/new/local', headers={'If-None-Match': home}).status_code == 304
    assert _visit(app, '/home/new/local', onion=True, headers={'If-None-Match': home}).status_code != 304


# Round 2

@pytest.mark.parametrize('segment', ['%0A', 'nonsense', 'FOO', '%3Cb%3E'])
def test_an_unknown_feed_type_is_a_404_not_a_500(app, scene, segment):
    assert _visit(app, f'/index/feed/{segment}').status_code == 404
    assert _visit(app, f'/index/feed/{segment}?token=alice-rss-token', onion=False).status_code == 404


def test_the_local_comment_authors_are_local_users_only(app, scene):
    statements = []

    def record(conn, cursor, statement, *rest):
        statements.append(' '.join(statement.split()))
    event.listen(db.engine, 'before_cursor_execute', record)
    try:
        response = _api(app, 'comment/list', scene.world.alice, type_='Local')
    finally:
        event.remove(db.engine, 'before_cursor_execute', record)
    assert {item['comment']['id'] for item in response.json['comments']} == {scene.replies.clear.id}
    author_filters = [s for s in statements if 'post_reply' in s and '"user".id FROM "user"' in s]
    assert author_filters and all('"user".ap_id IS NULL' in s for s in author_filters)
