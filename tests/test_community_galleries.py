"""Galleries in microblogs (spec 2026-10-30 §2.1, G1): `?content_type=galleries` lists a community's image posts, from
any platform, and microblogs offers the filter beside Posts, Comments and Events."""
import pytest
from bs4 import BeautifulSoup

from app import db
from app.constants import COMMUNITY_KIND_MICROBLOGS, POST_TYPE_ARTICLE, POST_TYPE_IMAGE
from tests.test_community_show import a_post, env, show, titles, url  # noqa: F401 (env)

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def microblogs(env):
    client, community, author = env
    community.kind = COMMUNITY_KIND_MICROBLOGS
    db.session.commit()
    return client, community, author


def page(app, client, community, query=''):
    html = client.get(url(app, 'activitypub.community_profile', actor=community.name) + query).get_data(as_text=True)
    return BeautifulSoup(html, 'html.parser')


def test_galleries_lists_image_posts_only(app, microblogs):
    client, community, author = microblogs
    a_post(community, author, 1, type=POST_TYPE_ARTICLE)
    a_post(community, author, 2, type=POST_TYPE_IMAGE)

    _response, render = show(app, client, community, '?content_type=galleries')

    assert titles(render) == ['post 2']


def test_microblogs_offers_galleries_in_both_navs(app, microblogs):
    client, community, _author = microblogs

    html = page(app, client, community, '?content_type=galleries')

    nav = html.find('nav', attrs={'aria-label': 'Show'})
    galleries = nav.find_all('a')[-1]
    assert galleries.find(class_='visually-hidden').text == 'Galleries'
    assert galleries.find(class_='fe')['class'][1] == 'fe-image' and galleries['aria-current'] == 'page'
    option = html.find('select', attrs={'name': 'content_type'}).find('option', attrs={'value': 'galleries'})
    assert option is not None and option.has_attr('selected')   # the phone's filter form, which needs no JavaScript


def test_another_community_does_not(app, env):
    client, community, _author = env

    html = page(app, client, community)

    assert 'Galleries' not in [a.find(class_='visually-hidden').text
                               for a in html.find('nav', attrs={'aria-label': 'Show'}).find_all('a')]
    assert html.find('option', attrs={'value': 'galleries'}) is None


def test_an_unknown_content_type_still_lists_comments(app, microblogs):
    client, community, author = microblogs
    a_post(community, author, 1, type=POST_TYPE_IMAGE)

    _response, render = show(app, client, community, '?content_type=nonsense')

    assert render.call_args.kwargs['content_type'] == 'nonsense'
    assert render.call_args.kwargs['posts'] is None
