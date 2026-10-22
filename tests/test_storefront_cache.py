"""Cached pages are cached per storefront (onion storefront spec §3.4a, Task 5).

A page renders its own storefront's URLs, so a cache entry keyed without the storefront would hand the onion's
URLs to a clearnet visitor (naming the onion they never asked for) or the clearnet's to an onion visitor (links
that leave Tor). Every presentation cache key names the storefront. The ActivityPub documents name only the
canonical origin, so they keep their path keys and are shared by every host.

The suite runs on NullCache, under which nothing is ever cached and no test of a cache key can fail. Each test here
swaps in a real SimpleCache for its own duration.
"""
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from cachelib import SimpleCache
from flask import g
from flask_login import login_user

from app import cache, db
from app.api.alpha.views import cached_modlist_for_community, cached_modlist_for_user, user_view
from app.models import CommunityMember, File, Site, SiteDomain, Topic
from app.storefront import (clear_domain_cache, delete_memoized_everywhere, storefront_cache_key,
                            storefront_memo_name, storefront_query_cache_key)
from app.utils import make_cache_key
from tests.factories import make_community, make_community_member, make_instance, make_post, make_user

ONION = 'abc.onion'
CANONICAL = 'test.piefed.local'
ORIGINS = {ONION: f'http://{ONION}', CANONICAL: f'https://{CANONICAL}'}
MEDIA = 'app/static/media/posts/ab/cd/abcdefghijklmno.jpg'

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def real_cache(app, monkeypatch):
    monkeypatch.setitem(app.extensions['cache'], cache, SimpleCache())
    clear_domain_cache()
    yield
    clear_domain_cache()


@pytest.fixture
def onion(db_session, real_cache):
    if SiteDomain.query.filter_by(domain=CANONICAL).first() is None:   # the canonical host stays served
        db.session.add(SiteDomain(domain=CANONICAL, scheme='https', is_default=True))
    db.session.add(SiteDomain(domain=ONION, scheme='http', federates=False))
    db.session.get(Site, 1).private_instance = False
    db.session.commit()
    clear_domain_cache()
    yield ONION
    clear_domain_cache()


@pytest.fixture
def world(app, onion):
    local = make_instance(CANONICAL, software='piefed')
    author = make_user(local, 'author', local=True)
    author.ap_profile_id = f'https://{CANONICAL}/u/author'
    author.ap_public_url = f'https://{CANONICAL}/u/author'
    avatar = File(file_path=MEDIA)
    db.session.add(avatar)
    db.session.commit()
    author.avatar_id = avatar.id
    topic = Topic(name='Pictures', machine_name='pictures', num_communities=1)
    db.session.add(topic)
    db.session.commit()
    community = make_community('general')
    community.topic_id = topic.id
    make_community_member(author, community, is_moderator=True)
    post = make_post(community, author, f'https://{CANONICAL}/post/1', title='a picture')
    post.instance_id = 1
    post.indexable = True
    author.post_count = 1
    db.session.commit()
    return author, community, post


def get(app, path, host):
    g.pop('_login_user', None)   # the suite's requests share the fixture's app context, and flask.g with it
    response = app.test_client().get(path, base_url=ORIGINS[host])
    assert response.status_code == 200, (path, host, response.status_code)
    return response.get_data(as_text=True)


# hell's rows by their handle, which names them on either host (plan C1: a bare name is the storefront's own)
PRESENTATION_VIEWS = ['/sitemap.xml', f'/community/general@{CANONICAL}/feed', f'/community/general@{CANONICAL}/feed?limit=5',
                      f'/u/author@{CANONICAL}/feed', '/topic/pictures.rss']


@pytest.mark.parametrize('first, second', [(CANONICAL, ONION), (ONION, CANONICAL)])
@pytest.mark.parametrize('path', PRESENTATION_VIEWS)
def test_cached_view_per_host(app, world, path, first, second):
    get(app, path, first)
    body = get(app, path, second)
    # a sitemap lists its own domain's posts (plan C2 task 3), and every onion post is local-only, so the onion's is
    # empty: that it is not hell's cached entry is the `<loc>` check below
    if not (path == '/sitemap.xml' and second == ONION):
        assert f'{ORIGINS[second]}/' in body
    if second == CANONICAL:
        assert ONION not in body                        # the onion is never named to a clearnet visitor
    else:
        assert f'https://{CANONICAL}/static' not in body   # nor a link out of Tor (the RSS guid stays the AP id)
        assert f'<loc>https://{CANONICAL}' not in body
        assert f'<link>https://{CANONICAL}' not in body


def test_cached_view_is_still_cached(app, world):
    """The per-host key is a key, not a bypass: a second request on the same host is the first one's body."""
    first = get(app, '/topic/pictures.rss', ONION)
    Topic.query.filter_by(machine_name='pictures').one().name = 'Renamed'
    db.session.commit()
    assert get(app, '/topic/pictures.rss', ONION) == first


def test_rss_query_string_still_keys_the_cache(app, world):
    """show_community_rss reads `limit` and friends from the query string: each query string is its own entry."""
    make_post(world[1], world[0], f'https://{CANONICAL}/post/2', title='a second picture').instance_id = 1
    db.session.commit()
    one = get(app, f'/community/general@{CANONICAL}/feed?limit=1', ONION)
    two = get(app, f'/community/general@{CANONICAL}/feed?limit=2', ONION)
    assert one.count('<item>') == 1
    assert two.count('<item>') == 2


def test_storefront_cache_key_names_host_and_path(app, onion):
    with app.test_request_context('/u/author/feed', base_url=ORIGINS[ONION]):
        app.preprocess_request()
        assert storefront_cache_key() == f'view/{ONION}/u/author/feed'


def test_storefront_query_cache_key(app, onion):
    def key(host, query):
        with app.test_request_context(f'/c/general/feed?{query}', base_url=ORIGINS[host]):
            app.preprocess_request()
            return storefront_query_cache_key(actor='general')

    assert key(ONION, 'a=1&b=2') == key(ONION, 'b=2&a=1')   # argument order does not matter
    assert key(ONION, 'a=1').startswith(f'view/{ONION}/c/general/feed?')
    assert key(ONION, 'a=1') != key(ONION, 'a=2')
    assert key(ONION, 'a=1') != key(CANONICAL, 'a=1')


def test_storefront_memo_name(app, onion):
    with app.test_request_context('/', base_url=ORIGINS[ONION]):
        app.preprocess_request()
        assert storefront_memo_name('app.api.alpha.views.user_view') == f'app.api.alpha.views.user_view@{ONION}'


def test_make_cache_key_includes_host(app, onion):
    """app/utils.py make_cache_key keys on request.url, which carries the host. Pinned so it stays so."""
    user = make_user(make_instance(CANONICAL, software='piefed'), 'keyed', local=True)
    for logged_in in (False, True):
        keys = {}
        for host in (ONION, CANONICAL):
            with app.test_request_context('/home', base_url=ORIGINS[host]):
                app.preprocess_request()
                if logged_in:
                    login_user(user)
                keys[host] = make_cache_key(sort='hot')
        assert ONION in keys[ONION] and ONION not in keys[CANONICAL]
        assert keys[ONION] != keys[CANONICAL]


AP_CACHED_ROUTES = ['/.well-known/nodeinfo', '/.well-known/host-meta', '/.well-known/host-meta.json',
                    '/nodeinfo/2.0', '/nodeinfo/2.0.json', '/nodeinfo/2.1', '/nodeinfo/2.1.json',
                    '/api/v1/instance', '/api/v3/site']


@pytest.mark.parametrize('path', AP_CACHED_ROUTES)
def test_ap_discovery_documents_are_cached_per_host(app, onion, path):
    """Each federating domain describes itself (plan C3), so these are cached per host. The onion federates with no
    one and describes no server: a 404, in either order of first request, and the canonical host's copy never
    answers it."""
    def onion_status():
        g.pop('_login_user', None)
        return app.test_client().get(path, base_url=ORIGINS[ONION]).status_code

    clearnet = get(app, path, CANONICAL)
    assert onion_status() == 404
    assert ONION not in clearnet
    cache.clear()
    assert onion_status() == 404
    assert get(app, path, CANONICAL) == clearnet


def call_on(app, host, fn, *args, **kwargs):
    with app.test_request_context('/', base_url=ORIGINS[host]):
        app.preprocess_request()
        return fn(*args, **kwargs)


@pytest.mark.parametrize('first, second', [(CANONICAL, ONION), (ONION, CANONICAL)])
def test_user_view_per_storefront(app, world, first, second):
    author = world[0]
    call_on(app, first, user_view, author.id, variant=1)
    view = call_on(app, second, user_view, author.id, variant=1)
    assert urlsplit(view['avatar']).hostname == second
    assert view['actor_id'] == f'https://{CANONICAL}/u/author'   # the identifier stays canonical


@pytest.mark.parametrize('first, second', [(CANONICAL, ONION), (ONION, CANONICAL)])
def test_cached_modlists_per_storefront(app, world, first, second):
    author, community, post = world
    call_on(app, first, cached_modlist_for_community, community.id)
    call_on(app, first, cached_modlist_for_user, author)
    for modlist in (call_on(app, second, cached_modlist_for_community, community.id),
                    call_on(app, second, cached_modlist_for_user, author)):
        assert urlsplit(modlist[0]['moderator']['avatar']).hostname == second


def test_user_view_delete_memoized_clears_every_storefront(app, world):
    """`cache.delete_memoized(user_view)` (a profile edit) must drop the entry on every host, not only the one the
    edit was made on."""
    author = world[0]
    for host in (ONION, CANONICAL):
        call_on(app, host, user_view, author.id, variant=1)
    author.title = 'Renamed'
    db.session.commit()
    call_on(app, ONION, cache.delete_memoized, user_view)
    for host in (ONION, CANONICAL):
        assert call_on(app, host, user_view, author.id, variant=1)['title'] == 'Renamed'


def demote(author, community):
    CommunityMember.query.filter_by(user_id=author.id, community_id=community.id).update({'is_moderator': False})
    db.session.commit()


@pytest.mark.parametrize('cleared_on', [ONION, CANONICAL, None])   # None: a background job, outside any request
def test_delete_memoized_everywhere_clears_one_argument_on_every_storefront(app, world, cleared_on):
    """`cache.delete_memoized(f, arg)` on a per-storefront function deletes the key the current storefront makes,
    so the other storefronts kept serving the stale entry. The helper deletes that argument's entry on every
    registered storefront and on the default."""
    author, community, post = world
    for host in (ONION, CANONICAL):
        assert call_on(app, host, cached_modlist_for_user, author) != []
    demote(author, community)
    if cleared_on is None:
        delete_memoized_everywhere(cached_modlist_for_user, author)
    else:
        call_on(app, cleared_on, delete_memoized_everywhere, cached_modlist_for_user, author)
    for host in (ONION, CANONICAL):
        assert call_on(app, host, cached_modlist_for_user, author) == [], host


def test_delete_memoized_everywhere_keeps_other_arguments(app, world):
    """Per-argument precision: another argument's entry is not cleared."""
    author, community, post = world
    other = make_user(author.instance, 'other', local=True)
    make_community_member(other, community, is_moderator=True)
    for host in (ONION, CANONICAL):
        call_on(app, host, cached_modlist_for_user, other)
    demote(other, community)
    call_on(app, ONION, delete_memoized_everywhere, cached_modlist_for_user, author)
    for host in (ONION, CANONICAL):
        assert call_on(app, host, cached_modlist_for_user, other) != [], host


PER_STOREFRONT = r'(?:alpha_views\.)?(?:user_view|cached_modlist_for_community|cached_modlist_for_user)'


def test_no_single_storefront_delete_with_arguments():
    """Every argument-taking delete of a per-storefront memoized function goes through delete_memoized_everywhere."""
    pattern = re.compile(r'cache\.delete_memoized\(\s*' + PER_STOREFRONT + r'\s*,')
    found = [f'{path}:{number}' for path in Path('app').rglob('*.py')
             for number, line in enumerate(path.read_text().splitlines(), 1) if pattern.search(line)]
    assert found == []
