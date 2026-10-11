"""Amendment B, S7 read side: a followers-only post AUTHORED HERE gets the same protection remote followers-only
content already has (tests/test_visibility_listings.py). It is absent from every listing for everyone, present on a
follower's home feed, and its permalink is a 404 for anyone else.

The posts are made through make_post -- the S7.1 producer -- not by setting the column on a factory row.
"""
import re
from types import SimpleNamespace

from unittest.mock import patch

import pytest
from flask import current_app, g

from app import db
from app.constants import POST_TYPE_ARTICLE, SRC_API
from app.models import Language
from app.shared.post import make_post
from tests.factories import bearer, feed_ids, make_community, make_follow, make_instance, make_site, make_user
from tests.test_shared_post_edit import _api_input
from tests.test_visibility_single_object import client_as

WORD = 'quokkabird'


def post_links(response):
    return {int(i) for i in re.findall(r'/(?:post|p)/(\d+)', response.get_data(as_text=True))}


@pytest.fixture
def world(app, db_session, monkeypatch):
    local = make_instance('test.piefed.local', software='piefed')
    site = make_site()
    site.private_instance = False
    g.admin_ids = []
    g.site = site
    monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.add(Language(code='en', name='English'))
    db.session.commit()
    author = make_user(local, 'alice', local=True, with_keys=True)
    follower = make_user(local, 'fran', local=True)
    stranger = make_user(local, 'sam', local=True)
    make_follow(follower, author)
    community = make_community('microblogs')
    with monkeypatch.context() as m:
        m.setattr('app.shared.post.task_selector', lambda *a, **k: None)
        m.setattr('app.shared.post.authorise_api_user', lambda *a, **k: author)
        _, hidden = make_post(_api_input(title=f'{WORD} hidden', visibility='followers'), community,
                              POST_TYPE_ARTICLE, SRC_API, auth='t')
        _, shown = make_post(_api_input(title=f'{WORD} shown'), community, POST_TYPE_ARTICLE, SRC_API, auth='t')
    # search lists only indexable posts, and the search_vector trigger fills the vector on commit
    hidden.indexable = shown.indexable = True
    db.session.commit()
    return SimpleNamespace(author=author, follower=follower, stranger=stranger, community=community,
                           hidden=hidden, shown=shown)


SURFACES = {
    'community_page': '/c/microblogs',
    'community_rss': '/community/microblogs/feed',
    'instance_posts': '/instance/test.piefed.local/posts',
    'search': f'/search?q={WORD}&search_for=posts',
}


@pytest.mark.parametrize('surface', sorted(SURFACES))
@pytest.mark.parametrize('who', ['follower', 'stranger', None])
def test_no_listing_offers_it(app, world, surface, who):
    viewer = getattr(world, who) if who else None
    response = client_as(app, viewer).get(SURFACES[surface])
    assert response.status_code == 200, surface
    shown = post_links(response)
    assert world.hidden.id not in shown, surface
    assert world.shown.id in shown, surface


@pytest.mark.parametrize('who', ['follower', 'stranger'])
def test_the_api_post_list_omits_it(app, world, who):
    viewer = getattr(world, who)
    for params in ({}, {'community_id': world.community.id}, {'type_': 'Local'}):
        response = app.test_client().get('/api/alpha/post/list', query_string=params,
                                         headers={'Authorization': bearer(viewer)})
        assert response.status_code == 200, params
        ids = {p['post']['id'] for p in response.get_json()['posts']}
        assert world.shown.id in ids, params
        assert world.hidden.id not in ids, params


def test_a_followers_home_feed_has_it(app, world):
    assert world.hidden.id in feed_ids(app, world.follower, [world.community.id], include_following=True)


def test_a_strangers_home_feed_does_not(app, world):
    assert world.hidden.id not in feed_ids(app, world.stranger, [world.community.id], include_following=True)


@pytest.mark.parametrize('who, status', [('follower', 200), ('author', 200), ('stranger', 404), (None, 404)])
def test_the_permalink(app, world, who, status):
    viewer = getattr(world, who) if who else None
    with patch('app.post.routes.render_template', return_value=app.response_class('rendered')):
        assert client_as(app, viewer).get(f'/post/{world.hidden.id}').status_code == status
