"""Interop D24: a logged-in viewer opens a wider-network video here, through the authenticated resolve path."""
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.discovery import views
from app.models import Site
from tests.discovery_fixtures import fresh_cache  # noqa: F401
from tests.factories import make_banned_instance, make_community, make_instance, make_post, make_user
from tests.test_admin_federation import csrf, login

pytestmark = pytest.mark.usefixtures('fresh_cache')
URL = 'https://tube.example/videos/watch/1'


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    user = api_baseline.user1
    user.verified = True
    db.session.commit()
    client = app.test_client()
    login(client, user)
    return SimpleNamespace(client=client, token=csrf(app, client), user=user)


def test_a_video_that_resolves_opens_its_post(env, monkeypatch):
    instance = make_instance('tube.example', software='peertube')
    post = make_post(make_community('chan', host='tube.example'), make_user(instance, 'author'), URL)
    monkeypatch.setattr(views, 'resolve_remote_post_from_search', lambda uri: post if uri == URL else None)

    response = env.client.post('/discovery/video/resolve', data={'csrf_token': env.token, 'url': URL, 'q': 'zq'})

    assert response.status_code == 302 and response.headers['Location'].endswith(f'/post/{post.id}')


def test_a_video_that_does_not_resolve_goes_back_to_the_search(env, monkeypatch):
    monkeypatch.setattr(views, 'resolve_remote_post_from_search', lambda uri: None)

    response = env.client.post('/discovery/video/resolve', data={'csrf_token': env.token, 'url': URL, 'q': 'zq'})

    assert response.status_code == 302
    assert '/search' in response.headers['Location'] and 'q=zq' in response.headers['Location']
    assert 'external=1' in response.headers['Location']


@pytest.mark.parametrize('url', ['', 'http://tube.example/v/1', 'https://not a host/v', 'ftp://x.example/v', None,
                                 'https://tube.example\\@evil.example/v', 'https://user@tube.example/v',
                                 'https://tube.example:8443/v'])
def test_a_bad_url_is_400(env, monkeypatch, url):
    monkeypatch.setattr(views, 'resolve_remote_post_from_search', lambda uri: pytest.fail('fetched'))
    data = {'csrf_token': env.token, 'q': 'zq'}
    if url is not None:
        data['url'] = url
    assert env.client.post('/discovery/video/resolve', data=data).status_code == 400


def test_a_banned_host_is_not_fetched(env, monkeypatch):
    make_banned_instance('tube.example')
    monkeypatch.setattr(views, 'resolve_remote_post_from_search', lambda uri: pytest.fail('fetched'))

    response = env.client.post('/discovery/video/resolve', data={'csrf_token': env.token, 'url': URL})

    assert response.status_code == 302 and '/search' in response.headers['Location']


def test_without_csrf_nothing_is_fetched(env, monkeypatch):
    monkeypatch.setattr(views, 'resolve_remote_post_from_search', lambda uri: pytest.fail('fetched'))
    response = env.client.post('/discovery/video/resolve', data={'url': URL})
    assert response.status_code == 400   # login_required aborts 400 on a missing or bad csrf token


def test_anonymous_viewers_are_sent_to_log_in(app, db_session, monkeypatch):
    monkeypatch.setattr(views, 'resolve_remote_post_from_search', lambda uri: pytest.fail('fetched'))
    response = app.test_client().post('/discovery/video/resolve', data={'url': URL})
    assert response.status_code == 302 and 'login' in response.headers['Location']
