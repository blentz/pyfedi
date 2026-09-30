"""The front page and its RSS feed.

Sub-project 86, slice B -- `index`, `home_page` and `index_rss` in
`app/main/routes.py`: the busiest public endpoints this instance has. One
defect, measured first:

* `index_rss` computed its ETag and answered `304 Not Modified` BEFORE
  testing `g.site.private_instance`. A caller holding an ETag from before the
  instance was made private -- or one guessed, since it is
  `home_{hash(last_active)}` -- got a 304 where a fresh request got 404. That
  is an access check a conditional request walks past, and it is the same
  defect this campaign fixed in the community feed (D1252).
"""
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.models import Site, utcnow
from tests.factories import (make_community, make_community_member, make_post,
                             make_user)


@pytest.fixture
def env(app, api_baseline):
    """A public instance with one post on its front page.

    `api_baseline`'s Site is PRIVATE, which redirects every anonymous page to
    the login screen -- so a test of the public front page has to say
    otherwise, and a test of the gate has to put it back.
    """
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    g.site.private_instance = False
    community = make_community('probeland')
    community.show_all = True
    author = api_baseline.user2
    post = make_post(community, author, ap_id='https://test.piefed.local/z/1')
    post.title = 'the first post'
    db.session.commit()
    return SimpleNamespace(client=app.test_client(), site=g.site,
                           community=community, author=author, post=post,
                           reader=api_baseline.user3,
                           baseline=api_baseline)


class TestTheNextPageLink:
    """D781, fixed. `len(post_ids) > page + 1 * page_length` read as
    `page + page_length` because `*` binds tighter, so a page past the last
    full one still offered a next page. It is now `(page + 1) * page_length`,
    as the api/alpha copy (D776) was fixed.

    Five ids at two per page: pages 0-2 hold them, so page 1 has a next page
    and page 2 does not -- where the old test read `5 > 4` and offered one.
    """

    def _next_url(self, env, app, monkeypatch, page):
        monkeypatch.setitem(app.config, 'PAGE_LENGTH', 2)
        captured = {}

        def fake_render(template, **kwargs):
            captured.update(kwargs)
            return 'rendered'

        with patch('app.main.routes.get_deduped_post_ids', return_value=[1, 2, 3, 4, 5]), \
                patch('app.main.routes.render_template', side_effect=fake_render):
            assert env.client.get(f'/home/new/all?page={page}').status_code == 200
        return captured['next_url']

    def test_a_middle_page_offers_the_next(self, env, app, monkeypatch):
        assert self._next_url(env, app, monkeypatch, page=1) is not None

    def test_the_last_page_offers_no_next(self, env, app, monkeypatch):
        assert self._next_url(env, app, monkeypatch, page=2) is None


class TestTheFrontPage:
    def test_it_answers(self, env):
        response = env.client.get('/')
        assert response.status_code == 200
        assert 'the first post' in response.get_data(as_text=True)

    @pytest.mark.parametrize('path', ['/', '/home', '/home/new',
                                      '/home/new/popular'])
    def test_every_way_of_asking_for_it(self, env, path):
        assert env.client.get(path).status_code == 200

    def test_an_anonymous_reader_cannot_ask_for_subscribed(self, env):
        """`subscribed` means nothing without an account, so the filter falls
        back to `popular` rather than answering an empty page."""
        response = env.client.get('/home/hot/subscribed')
        assert response.status_code == 200
        assert 'the first post' in response.get_data(as_text=True)

    def test_a_conditional_request_is_answered_with_a_304(self, env):
        first = env.client.get('/')
        assert first.status_code == 200
        second = env.client.get(
            '/', headers={'If-None-Match': first.headers['ETag']})
        assert second.status_code == 304

    def test_an_etag_from_another_sort_is_not_a_match(self, env):
        hot = env.client.get('/home/hot')
        new = env.client.get('/home/new',
                             headers={'If-None-Match': hot.headers['ETag']})
        assert new.status_code == 200

    def test_a_fediverse_peer_gets_the_instance_actor(self, env):
        """D1253. The summary was `g.site.name + ' - ' + g.site.description`,
        and both are nullable -- so an instance whose tagline was never filled
        in answered its own introductions with
        `TypeError: can only concatenate str (not "NoneType") to str`."""
        response = env.client.get(
            '/', headers={'Accept': 'application/activity+json'})
        assert response.status_code == 200
        assert response.get_json()['type'] == 'Application'

    def test_the_instance_actor_when_the_site_has_a_name_and_a_tagline(
            self, env):
        env.site.name = 'Probeland'
        env.site.description = 'a nice place'
        db.session.commit()
        actor = env.client.get(
            '/', headers={'Accept': 'application/activity+json'}).get_json()
        assert actor['summary'] == 'Probeland - a nice place'

    def test_the_instance_actor_when_only_the_name_is_set(self, env):
        env.site.name = 'Probeland'
        env.site.description = None
        db.session.commit()
        actor = env.client.get(
            '/', headers={'Accept': 'application/activity+json'}).get_json()
        assert actor['summary'] == 'Probeland'

    def test_the_instance_actor_when_neither_is_set(self, env):
        env.site.name = None
        env.site.description = None
        db.session.commit()
        actor = env.client.get(
            '/', headers={'Accept': 'application/activity+json'}).get_json()
        assert actor['summary'] == ''

    def test_a_private_instance_sends_an_anonymous_reader_to_the_login(
            self, env):
        env.site.private_instance = True
        db.session.commit()
        response = env.client.get('/')
        assert response.status_code == 302
        assert '/auth/login' in response.headers['Location']

    def test_a_private_instance_still_answers_a_fediverse_peer(self, env):
        env.site.private_instance = True
        db.session.commit()
        response = env.client.get(
            '/', headers={'Accept': 'application/activity+json'})
        assert response.status_code == 200
        assert response.get_json()['type'] == 'Application'

    def test_a_low_bandwidth_reader(self, env):
        env.client.set_cookie('low_bandwidth', '1')
        assert env.client.get('/').status_code == 200

    def test_a_tag_narrows_the_page(self, env):
        assert env.client.get('/?tag=news').status_code == 200

    def test_a_page_past_the_first(self, env):
        assert env.client.get('/?page=2').status_code == 200


class TestTheFrontPageRss:
    def test_it_answers(self, env):
        response = env.client.get('/index/feed')
        assert response.status_code == 200
        assert response.mimetype == 'application/rss+xml'
        assert 'the first post' in response.get_data(as_text=True)

    @pytest.mark.parametrize('feed_type', ['new', 'active', 'hot', 'top',
                                           'nonsense'])
    def test_every_kind_of_feed(self, env, feed_type):
        response = env.client.get(f'/index/feed/{feed_type}')
        assert response.status_code == 200

    def test_a_conditional_request_is_answered_with_a_304(self, env):
        first = env.client.get('/index/feed')
        second = env.client.get(
            '/index/feed', headers={'If-None-Match': first.headers['ETag']})
        assert second.status_code == 304

    def test_a_private_instance_refuses_it(self, env):
        env.site.private_instance = True
        db.session.commit()
        assert env.client.get('/index/feed').status_code == 404

    def test_a_private_instance_refuses_a_conditional_request_too(self, env):
        """D1252. The ETag was computed and answered BEFORE the private check,
        so a caller holding one from before the instance was made private got
        304 where a fresh request got 404."""
        public = env.client.get('/index/feed')
        etag = public.headers['ETag']
        env.site.private_instance = True
        db.session.commit()
        response = env.client.get('/index/feed',
                                  headers={'If-None-Match': etag})
        assert response.status_code == 404

    def test_a_token_reads_the_feed_as_its_owner(self, env):
        env.reader.rss_token = 'a-token'
        db.session.commit()
        response = env.client.get('/index/feed?token=a-token')
        assert response.status_code == 200

    def test_a_token_nobody_holds_reads_it_anonymously(self, env):
        response = env.client.get('/index/feed?token=nobody-holds-this')
        assert response.status_code == 200
