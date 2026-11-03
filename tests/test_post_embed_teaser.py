"""The embed page's teaser is the embed's: `/post/<id>/embed` renders the post teaser with `embed=True`.

The teaser macros (app/templates/post/post_teaser/_macros.html) do what an embed needs when `embed` is true: the
instance's logo, absolute links that open in a new tab (the embed is a frame on another site), and no options menu or
cross-posts. Upstream's macro refactor (8e328c6ed) stopped passing `embed` from the teaser to the macros, so none of
it happened: an embed showed the teaser as a page of this site shows it. The Dillo theme's copy had the same gap.

Pages of this site still show the teaser as they did: none of it applies when `embed` is not set."""
import re

import pytest

from app import db
from app.constants import POST_TYPE_LINK
from app.models import Community, Site
from app.storefront import clear_domain_cache
from tests.factories import make_community_member, make_post
from tests.test_branding_pages import FIRST, branded, page  # noqa: F401  (fixtures)
from tests.two_domains import two_domains  # noqa: F401  (a fixture)

pytestmark = pytest.mark.usefixtures('site')

EXTERNAL = f'https://{FIRST}'


@pytest.fixture
def embedded(branded):
    author = branded.local_user('embedauthor', branded.domains[0])
    community = db.session.query(Community).filter_by(name='goldart').one()
    make_community_member(author, community)
    post = make_post(community, author, f'{EXTERNAL}/post/embedded', title='The embedded post')
    post.body_html = '<p>embedded</p>'
    db.session.commit()
    return post


def embed_logo(body: str) -> list[str]:
    logo = re.search(r'<div class="embed_logo">.*?</div>', body, re.S)
    return re.findall(r'<img [^>]*>', logo.group(0)) if logo else []


def title_link(body: str) -> str:
    return re.search(r'<h3 lang="[^"]*"><a [^>]*class="post_teaser_title_a"[^>]*>', body).group(0)


def test_an_embed_shows_the_instances_logo(app, embedded):
    assert embed_logo(page(app, f'/post/{embedded.id}/embed')) == [
        '<img src="/static/media/golden_100.png" alt="Logo" width="40" height="40">']


def test_an_embeds_links_are_absolute_and_open_in_a_new_tab(app, embedded):
    body = page(app, f'/post/{embedded.id}/embed')
    link = title_link(body)
    assert f'href="{EXTERNAL}/post/{embedded.id}' in link and 'target="_blank"' in link
    assert f'href="{EXTERNAL}/c/goldart" target="_blank"' in body


def test_an_embed_has_no_options_menu(app, embedded):
    assert 'post_options_link' not in page(app, f'/post/{embedded.id}/embed')


@pytest.mark.parametrize('post_type', ['article', 'link'])
def test_a_page_of_this_site_shows_the_teaser_as_before(app, embedded, post_type):
    if post_type == 'link':
        embedded.type = POST_TYPE_LINK
        embedded.url = 'https://example.org/linked'
        db.session.commit()
    body = page(app, '/c/goldart')
    assert embed_logo(body) == [] and 'class="embed_logo"' not in body
    link = title_link(body)
    assert 'target="_blank"' not in link and f'href="{EXTERNAL}' not in link
    assert 'post_options_link' in body


def test_a_dillo_embed_shows_the_logo(app, embedded, monkeypatch):
    site = db.session.get(Site, 1)
    site.default_theme = 'dillo'
    db.session.commit()
    clear_domain_cache()
    body = page(app, f'/post/{embedded.id}/embed')
    assert embed_logo(body) == ['<img src="/static/media/golden_100.png" alt="Logo" width="40" height="40">']
    assert 'target="_blank"' in title_link(body)
