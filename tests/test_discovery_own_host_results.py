"""Follow-up F2 fix round, item 2: a wider-network search hit on one of our own domains is listed when the viewer may
open the local post it names, and not otherwise. Its link goes through the resolve path to the local post."""
import pytest
from cachelib import SimpleCache
from flask_login import login_user

from app import cache, db
from app.constants import VISIBILITY_FOLLOWERS
from app.discovery import external_search
from app.discovery.external_search import search_videos
from tests.discovery_fixtures import fresh_cache  # noqa: F401
from tests.factories import make_user
from tests.test_local_only_invisible import _post, world  # noqa: F401

pytestmark = pytest.mark.usefixtures('site', 'fresh_cache')


@pytest.fixture(autouse=True)
def real_cache(app, monkeypatch):
    monkeypatch.setitem(app.extensions['cache'], cache, SimpleCache())


def hit(host, path, n=1):
    return {'url': f'https://{host}{path}', 'name': f'Video {n}', 'nsfw': False,
            'channel': {'displayName': 'Chan', 'host': host}}


@pytest.fixture
def answer(monkeypatch):
    def set_payload(*hits):
        monkeypatch.setattr(external_search.sources, 'fetch_json',
                            lambda *args, **kwargs: {'data': list(hits)})
    return set_payload


def listed(app, answer, hits, viewer=True):
    answer(*hits)
    with app.test_request_context():
        if viewer:
            login_user(make_user(None, 'carol', local=True))
        return [v['title'] for v in search_videos('cats', allow_nsfw=False)]


def test_an_own_host_hit_naming_a_post_the_viewer_may_open_is_listed(app, world, answer):
    post = _post(app, world.clearnet, world.bob)
    server = app.config['SERVER_NAME']

    assert listed(app, answer, [hit(server, f'/post/{post.id}'), hit('tube.example', '/videos/watch/2', 2)]) == \
        ['Video 1', 'Video 2']


def test_it_is_listed_even_when_its_url_is_the_posts_stored_one(app, world, answer):
    post = _post(app, world.clearnet, world.bob)   # the helper stores ap_id as {SERVER_URL}/post/{id}
    assert post.ap_id.endswith(f'/post/{post.id}')

    assert listed(app, answer, [hit(app.config['SERVER_NAME'], f'/post/{post.id}')]) == ['Video 1']


def test_an_own_host_hit_the_viewer_may_not_open_is_not_listed(app, world, answer):
    post = _post(app, world.clearnet, world.bob)
    post.visibility = VISIBILITY_FOLLOWERS
    db.session.commit()

    assert listed(app, answer, [hit(app.config['SERVER_NAME'], f'/post/{post.id}')]) == []


def test_an_own_host_hit_naming_no_post_is_not_listed(app, world, answer):
    assert listed(app, answer, [hit(app.config['SERVER_NAME'], '/post/99999')]) == []


def test_only_the_first_results_are_checked_so_the_service_cannot_make_us_run_a_query_per_hit(app, world, answer,
                                                                                               monkeypatch):
    checked = []
    real = external_search.hosted_post_for_viewer
    monkeypatch.setattr(external_search, 'hosted_post_for_viewer',
                        lambda url, *args: checked.append(url) or real(url, *args))
    server = app.config['SERVER_NAME']

    listed(app, answer, [hit(server, f'/post/{n}', n) for n in range(1, 40)])

    assert len(checked) == external_search.RESULT_LIMIT


def test_the_same_post_under_two_urls_is_listed_once(app, world, answer):
    post = _post(app, world.clearnet, world.bob)
    server = app.config['SERVER_NAME']

    assert listed(app, answer, [hit(server, f'/post/{post.id}', 1),
                                hit(server, f'/c/clearnet/p/{post.id}/a-post', 2)]) == ['Video 1']
