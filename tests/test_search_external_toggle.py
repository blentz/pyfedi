"""Interop D24: the search page's opt-in "Include results from the wider network"."""
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.models import Site
from app.search import routes
from app.utils import set_setting
from tests.discovery_fixtures import fresh_cache  # noqa: F401

pytestmark = pytest.mark.usefixtures('site', 'fresh_cache')
VIDEO = {'url': 'https://tube.example/videos/watch/1', 'title': 'Zq video', 'channel': 'Chan',
         'host': 'tube.example', 'nsfw': False}


@pytest.fixture
def asked(app, db_session, monkeypatch):
    g.site = db.session.get(Site, 1)
    g.site.private_instance = False  # anonymous search is login-gated on a private instance
    calls = []
    monkeypatch.setattr(routes, 'search_videos', lambda q, allow_nsfw: calls.append((q, allow_nsfw)) or [VIDEO])
    return calls


def page(app, **args):
    return app.test_client().get('/search', query_string=args).get_data(as_text=True)


def test_off_by_default_nothing_leaves_the_server(app, asked):
    html = page(app, q='zq')
    assert asked == [] and 'From the wider network' not in html
    assert 'name="external"' in html and 'id="external" name="external" value="1" checked' not in html


def test_on_shows_the_block_below_local_results(app, asked):
    html = page(app, q='zq', external='1')
    assert asked == [('zq', False)]
    assert 'From the wider network' in html and 'Zq video' in html
    assert html.index('From the wider network') > html.index('id="search_term"')


def test_anonymous_viewers_get_a_plain_link(app, asked):
    html = page(app, q='zq', external='1')
    assert 'href="https://tube.example/videos/watch/1"' in html and 'video/resolve' not in html
    assert 'rel="nofollow noopener"' in html


def test_the_admin_kill_switch_hides_the_toggle_and_ignores_the_flag(app, asked):
    set_setting('discovery_external_search', False)
    html = page(app, q='zq', external='1')
    assert asked == [] and 'name="external"' not in html and 'From the wider network' not in html


def test_the_toggle_shows_on_the_empty_search_page(app, asked):
    assert 'name="external"' in page(app)
    set_setting('discovery_external_search', False)
    assert 'name="external"' not in page(app)


def test_only_page_one_of_a_post_search_asks(app, asked):
    page(app, q='zq', external='1', page=2)
    page(app, q='zq', external='1', search_for='comments')
    assert asked == []


def test_no_query_asks_nothing(app, asked):
    page(app, external='1', media='1')
    assert asked == []


def test_nothing_found_shows_no_block(app, monkeypatch, db_session):
    g.site = db.session.get(Site, 1)
    g.site.private_instance = False
    monkeypatch.setattr(routes, 'search_videos', lambda q, allow_nsfw: [])
    assert 'From the wider network' not in page(app, q='zq', external='1')


def test_a_logged_in_viewer_gets_a_resolve_button_and_their_nsfw_choice(app, asked):
    from tests.factories import make_instance, make_user
    from tests.test_admin_federation import login
    site = db.session.get(Site, 1)
    site.enable_nsfw = True
    user = make_user(make_instance('test.piefed.local', software='piefed'), 'zqviewer', local=True)
    user.verified, user.hide_nsfw = True, 0
    db.session.commit()
    client = app.test_client()
    login(client, user)

    html = client.get('/search', query_string={'q': 'zq', 'external': '1'}).get_data(as_text=True)

    assert asked == [('zq', True)]
    assert 'action="/discovery/video/resolve"' in html and 'name="csrf_token"' in html
