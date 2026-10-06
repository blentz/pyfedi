"""Interop D24: post search can be narrowed to videos and podcasts, and the narrowing survives paging."""
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.constants import POST_STATUS_REVIEWING
from app.models import Site
from tests.factories import make_community, make_instance, make_post, make_user

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture(autouse=True)
def founder(db_session):
    make_user(make_instance('test.piefed.local', software='piefed'), 'founder', local=True)


def searchable_post(community, author, ap_id, title):
    """make_post leaves status and indexable at defaults the search filters on."""
    post = make_post(community, author, ap_id, title=title)
    post.status = POST_STATUS_REVIEWING + 1
    post.indexable = True
    return post


@pytest.fixture
def posts(app, db_session):
    g.site = db.session.get(Site, 1)
    g.site.private_instance = False  # anonymous search is login-gated on a private instance
    for software, host in (('peertube', 'tube.example'), ('lemmy', 'lemmy.example')):
        instance = make_instance(host, software=software)
        community = make_community(f'c_{software}', host=host)
        community.instance_id = instance.id
        searchable_post(community, make_user(instance, f'a_{software}'), f'https://{host}/p/1', f'zqword {software}')
    db.session.commit()
    return app


def captured(app, **args):
    seen = {}
    with patch('app.search.routes.render_template', side_effect=lambda t, **k: seen.update(k) or 'ok'):
        assert app.test_client().get('/search', query_string=args).status_code == 200
    return seen


def test_media_keeps_only_peertube_and_castopod_posts(posts):
    seen = captured(posts, q='zqword', media='1')
    assert [p.title for p in seen['posts'].items] == ['zqword peertube']
    assert seen['media'] is True


def test_media_alone_runs_a_search(posts):
    assert [p.title for p in captured(posts, media='1')['posts'].items] == ['zqword peertube']


def test_without_media_both_come_back(posts):
    assert sorted(p.title for p in captured(posts, q='zqword')['posts'].items) == ['zqword lemmy', 'zqword peertube']


def test_the_form_has_the_checkbox(posts):
    html = posts.test_client().get('/search?q=zqword&media=1').get_data(as_text=True)
    assert 'name="media"' in html and 'checked' in html


def test_paging_links_keep_media_and_external(posts, monkeypatch):
    """Anonymous page size is 50 (hardcoded in run_search), so 52 matching posts give a second page."""
    instance = make_instance('tube2.example', software='peertube')
    community = make_community('c_tube2', host='tube2.example')
    community.instance_id = instance.id
    author = make_user(instance, 'a_tube2')
    for i in range(51):
        searchable_post(community, author, f'https://tube2.example/p/{i}', f'zqword {i}')
    db.session.commit()

    monkeypatch.setattr('app.search.routes.search_videos', lambda q, allow_nsfw: [])  # no network in a paging test
    seen = captured(posts, q='zqword', media='1', external='1')

    assert 'media=1' in seen['next_url'] and 'external=1' in seen['next_url']
