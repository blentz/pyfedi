"""What a feed reader and an oEmbed consumer read names the domain they asked, not the first domain (project 4; the
owner's ruling "name can default to domain_name instead").

The oEmbed `provider_name` and every RSS feed title say `<thing> on <name>`. On a sister domain that name is its own
display name where it set one, else the bare domain: `Site.name` is the first domain's. The first domain's output is
`Site.name`, byte for byte as before. Pages people read keep the display-name branding (tests/test_branding_pages.py)."""
import xml.etree.ElementTree as ET

import pytest

from app import cache, db
from app.models import Domain, DomainBranding, Feed, Site, Tag, Topic
from app.storefront import clear_domain_cache
from tests.factories import make_post
from tests.test_c3_golden_documents import golden_site  # noqa: F401  (fixtures)
from tests.test_per_domain_actor_documents import SHOP_BASE, shop_rows, site_named_first  # noqa: F401
from tests.two_domains import FIRST, two_domains  # noqa: F401  (a fixture)

SHOP = 'shop.example'
SITE_NAME = 'First Domain Site'

pytestmark = pytest.mark.usefixtures('golden_site', 'shop_rows', 'site_named_first')


@pytest.fixture
def rows(app, shop_rows):
    """One of everything a feed or an embed is made of, and Site row 1 named differently from the first domain's host."""
    shop, user, community = shop_rows
    site = db.session.get(Site, 1)
    site.name, site.private_instance = SITE_NAME, False
    db.session.add_all([Topic(machine_name='crafts', name='Crafts', show_posts_in_children=False),
                        Tag(name='knit', display_as='Knit', post_count=1),
                        Domain(name='yarn.example'),
                        Feed(title='Makers', machine_name='makers', user_id=user.id, public=True,
                             nsfw=False, nsfl=False, instance_id=shop.instance_id, show_posts_in_children=False)])
    db.session.commit()
    post = make_post(community, user, f'{SHOP_BASE}/post/1', title='A post')
    db.session.commit()
    cache.clear()
    return post


def brand(domain, **values):
    db.session.add(DomainBranding(site_domain_id=domain.id, **values))
    db.session.commit()
    clear_domain_cache()
    cache.clear()


FEEDS = [pytest.param('/index/feed', 'Local', id='front page'),
         pytest.param('/topic/crafts.rss', 'Crafts on {}', id='topic'),
         pytest.param('/tag/knit/feed', '#Knit on {}', id='tag'),
         pytest.param('/d/yarn.example/feed', 'yarn.example on {}', id='domain'),
         pytest.param('/f/makers.rss', 'Makers on {}', id='feed'),
         pytest.param('/community/goldart/feed', 'Shop art on {}', id='community'),
         pytest.param('/u/goldie/feed', 'Shop goldie on {}', id='person')]


def feed_title(app, path, host):
    response = app.test_client().get(path, base_url=f'https://{host}')
    assert response.status_code == 200, (host, path, response.status_code)
    return ET.fromstring(response.data).findtext('channel/title')


def expected(template, name, host=SHOP):
    """The title `template` makes with the site name `name`; the first domain's community and account have other names."""
    if host == FIRST:
        template = template.replace('Shop art', 'Golden art').replace('Shop goldie', 'Goldie')
    return f'{name} - {template}' if template == 'Local' else template.format(name)


@pytest.mark.parametrize('path, template', FEEDS)
def test_a_sister_with_no_own_name_titles_its_feeds_with_the_domain(app, rows, path, template):
    assert feed_title(app, path, SHOP) == expected(template, SHOP)


@pytest.mark.parametrize('path, template', FEEDS)
def test_a_sister_with_its_own_name_titles_its_feeds_with_it(app, rows, shop_rows, path, template):
    brand(shop_rows[0], name='Shop Branded')
    assert feed_title(app, path, SHOP) == expected(template, 'Shop Branded')


@pytest.mark.parametrize('path, template', FEEDS)
def test_the_first_domains_feed_titles_are_still_the_site_name(app, rows, path, template):
    assert feed_title(app, path, FIRST) == expected(template, SITE_NAME, FIRST)


def provider_name(app, post, host):
    response = app.test_client().get(f'/post/{post.id}/oembed', base_url=f'https://{host}')
    assert response.status_code == 200, (host, response.status_code)
    return response.get_json()['provider_name']


def test_oembed_on_a_sister_with_no_own_name_names_the_domain(app, rows):
    assert provider_name(app, rows, SHOP) == SHOP


def test_oembed_on_a_sister_with_its_own_name_names_it(app, rows, shop_rows):
    brand(shop_rows[0], name='Shop Branded')
    assert provider_name(app, rows, SHOP) == 'Shop Branded'


def test_oembed_on_the_first_domain_is_the_site_name(app, rows):
    assert provider_name(app, rows, FIRST) == SITE_NAME
