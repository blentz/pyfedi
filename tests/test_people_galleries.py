"""Galleries on the People pages (spec 2026-10-30 G3): `?filter=galleries` lists the Pixelfed accounts known here,
the search still applying, and the page offers it beside the search box."""
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup

from tests.test_instance_routes import _instance, _person, _seed

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def people(app, db_session):
    _seed()
    pixels = _instance('pixelfed.example', software='Pixelfed')
    _person(pixels, 'photographer')
    _person(pixels, 'painter')
    _person(_instance('mastodon.example', software='mastodon'), 'tooter')
    return app.test_client()


def names(client, path):
    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        assert client.get(path).status_code == 200
    return sorted(user.user_name for user in render.call_args.kwargs['people'].items), render.call_args.kwargs


def test_galleries_lists_pixelfed_accounts_only(people):
    listed, context = names(people, '/instance/all/people?filter=galleries')

    assert listed == ['painter', 'photographer'] and context['filter'] == 'galleries'


def test_the_search_still_applies(people):
    listed, _context = names(people, '/instance/all/people?filter=galleries&q=painter')

    assert listed == ['painter']


def test_an_instance_page_narrows_it_further(people):
    assert names(people, '/instance/mastodon.example/people?filter=galleries')[0] == []


def test_an_unknown_filter_lists_everyone(people):
    listed, context = names(people, '/instance/all/people?filter=nonsense')

    assert 'tooter' in listed and context['filter'] == ''


def test_the_page_offers_galleries_beside_the_search(people):
    html = BeautifulSoup(people.get('/instance/all/people?filter=galleries&q=p').get_data(as_text=True), 'html.parser')

    nav = html.find('nav', attrs={'aria-label': 'Show people'})
    links = nav.find_all('a')
    assert [a.find(class_='visually-hidden').text for a in links] == ['Everyone', 'Galleries']
    assert [a['href'] for a in links] == ['/instance/all/people?q=p', '/instance/all/people?filter=galleries&q=p']
    assert links[1]['aria-current'] == 'page'
    search = html.find('input', attrs={'name': 'q'}).find_parent('form')
    assert search.find('input', attrs={'name': 'filter', 'type': 'hidden'})['value'] == 'galleries'


def test_paging_keeps_the_filter(people):
    for index in range(51):
        _person(_instance(f'pixel{index}.example', software='pixelfed'), f'shooter{index:03d}')

    _listed, context = names(people, '/instance/all/people?filter=galleries')

    assert 'filter=galleries' in context['next_url']
