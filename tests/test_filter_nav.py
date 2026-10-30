"""The icon filter nav (spec 2026-10-30 G4): each choice an icon link with hover text and a text alternative, the
current one marked, and nothing that needs JavaScript."""
import re

import pytest
from bs4 import BeautifulSoup

pytestmark = pytest.mark.usefixtures('site')


def render(app, choices, label='Filter'):
    with app.test_request_context():
        macro = app.jinja_env.get_template('_filter_nav.html').module.filter_nav
        return BeautifulSoup(str(macro(choices, label)), 'html.parser')


def test_each_choice_is_an_icon_link_with_hover_text_and_a_text_alternative(app):
    nav = render(app, [('/a', 'home', 'Local', 'Posts on this instance', False),
                       ('/b', 'image', 'Galleries', None, True)])

    assert nav.nav['aria-label'] == 'Filter'
    links = nav.find_all('a')
    assert [a['href'] for a in links] == ['/a', '/b']
    assert [a['title'] for a in links] == ['Posts on this instance', 'Galleries']   # the name when no description
    assert [a.find(class_='visually-hidden').text for a in links] == ['Local', 'Galleries']
    assert [a.find('span', class_='fe')['class'][1] for a in links] == ['fe-home', 'fe-image']
    assert all(a.find('span', class_='fe')['aria-hidden'] == 'true' for a in links)


def test_only_the_current_choice_is_marked(app):
    nav = render(app, [('/a', 'home', 'Local', '', False), ('/b', 'all', 'All', '', True)])

    first, second = nav.find_all('a')
    assert 'aria-current' not in first.attrs and 'btn-outline-secondary' in first['class']
    assert second['aria-current'] == 'page' and 'btn-primary' in second['class']


def test_nothing_in_it_needs_javascript(app):
    html = str(render(app, [('/a', 'home', 'Local', '', True)]))
    assert '<select' not in html and 'onclick' not in html and 'data-bs-toggle' not in html
    assert not re.search(r'hx-[a-z]+=', html)


# --- the home page's view filter ---

from tests.test_front_page_view_filters import env  # noqa: E402,F401


def home_nav(client, path):
    page = BeautifulSoup(client.get(path).get_data(as_text=True), 'html.parser')
    return page, page.find('nav', class_='view_filter')


def test_the_home_filter_is_one_icon_nav_for_every_screen(env):
    page, nav = home_nav(env.app.test_client(), '/home/new/media')

    assert [a['href'] for a in nav.find_all('a')] == ['/home/new/local', '/home/new/popular', '/home/new/media',
                                                     '/home/new/all']
    assert [a.find(class_='visually-hidden').text for a in nav.find_all('a')] == ['Local', 'Popular', 'Media', 'All']
    assert nav.find('a', attrs={'aria-current': 'page'})['href'] == '/home/new/media'
    assert 'd-none' not in nav['class'] and 'd-xl-block' not in nav['class']   # shown on phones too
    assert page.find('select', attrs={'name': 'filter_by'}) is None           # the select that needed JavaScript


def test_a_signed_in_reader_also_gets_subscribed(env):
    client = env.app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(env.reader.id)
        session['_fresh'] = True

    _, nav = home_nav(client, '/home/new/subscribed')

    assert nav.find('a')['href'] == '/home/new/subscribed'
    assert nav.find('a', attrs={'aria-current': 'page'}).find(class_='fe')['class'][1] == 'fe-subscription'


# --- the community navs (shared with topics) ---

from tests.test_community_show import env as community_env, url  # noqa: E402,F401


def community_page(app, client, community, query=''):
    html = client.get(url(app, 'activitypub.community_profile', actor=community.name) + query).get_data(as_text=True)
    return BeautifulSoup(html, 'html.parser')


def test_the_community_sort_nav_is_icons_with_top_as_a_details_menu(app, community_env):
    client, community, _author = community_env
    page = community_page(app, client, community, '?sort=top_1w')

    sort = page.find('nav', attrs={'aria-label': 'Sort posts'})
    assert [a.find(class_='visually-hidden').text for a in sort.find_all('a', recursive=False)] == \
        ['Hot', 'New', 'Old', 'Active']
    top = sort.find('details', class_='filter_menu')
    assert top.summary.find(class_='visually-hidden').text == 'Top' and 'btn-primary' in top.summary['class']
    assert [a.text for a in top.find_all('a')] == ['12 hours', 'Day', 'Week', 'Month', 'Year', 'All Time']
    assert top.find('a', attrs={'aria-current': 'page'}).text == 'Week'
    assert sort.find(attrs={'data-bs-toggle': True}) is None   # the Top dropdown that needed JavaScript


def test_the_community_content_nav_is_icons_and_events_only_where_there_are_events(app, community_env):
    client, community, _author = community_env
    page = community_page(app, client, community, '?content_type=comments')

    show = page.find('nav', attrs={'aria-label': 'Show'})
    assert [a.find(class_='visually-hidden').text for a in show.find_all('a')] == ['Posts', 'Comments']
    assert show.find('a', attrs={'aria-current': 'page'})['href'].startswith('?content_type=comments&sort=new')
