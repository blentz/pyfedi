"""The menus, the share sheet, the random-community links, the content
warning, the honeypot and the bot challenge.

Sub-project 86, slice D -- the rest of `app/main/routes.py`. Two defects, both
measured:

* `/share` read `request.args.get('url')` and called `.strip()` on it. A
  request without a `url` -- which anything that follows the route without its
  query string sends -- was `AttributeError: 'NoneType' object has no
  attribute 'strip'`: a 500, a traceback and a Sentry event for a request that
  is merely incomplete (D1256);
* `/anoobis` refused an off-site `next` by RAISING, with the attacker's host
  in the message, leaving the `abort(403)` below it unreachable. The
  open-redirect guard was working; what it did on success was file a 500
  (D1257).
"""
import pytest
from flask import current_app, g

from app import db
from app.models import Community, Instance, Site
from tests.factories import (make_community, make_community_member,
                             make_instance, make_post, make_user)


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = [api_baseline.user1.id]
    g.site = db.session.get(Site, 1)
    g.site.private_instance = False
    community = make_community('probeland')
    community.show_all = True
    community.post_count = 1
    author = api_baseline.user2
    make_community_member(author, community)
    post = make_post(community, author, ap_id='https://test.piefed.local/s/1')
    post.url = 'https://example.test/an-article'
    db.session.commit()
    return SimpleNamespace(client=app.test_client(), site=g.site,
                           community=community, author=author, post=post,
                           reader=api_baseline.user3,
                           baseline=api_baseline)


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


class TestTheMenus:
    @pytest.mark.parametrize('path', ['/communities_menu', '/explore_menu',
                                      '/topics_menu', '/feeds_menu'])
    def test_a_menu_without_an_account(self, env, path):
        assert env.client.get(path).status_code == 200

    @pytest.mark.parametrize('path', ['/communities_menu', '/explore_menu',
                                      '/topics_menu', '/feeds_menu'])
    def test_a_menu_with_an_account(self, env, path):
        login(env.client, env.author)
        assert env.client.get(path).status_code == 200

    def test_the_communities_menu_lists_your_favourites(self, env):
        from app.models import CommunityFavorite
        db.session.add(CommunityFavorite(user_id=env.author.id,
                                         community_id=env.community.id))
        db.session.commit()
        login(env.client, env.author)
        response = env.client.get('/communities_menu')
        assert response.status_code == 200
        assert 'probeland' in response.get_data(as_text=True)

    @pytest.mark.parametrize('path', ['/topics', '/feeds', '/explore'])
    def test_the_directory_pages(self, env, path):
        assert env.client.get(path).status_code == 200


class TestTheShareSheet:
    def test_sharing_a_link(self, env):
        response = env.client.get(
            '/share?url=https://example.test/an-article&title=a+title')
        assert response.status_code == 200

    def test_sharing_a_link_that_is_already_posted(self, env):
        """The sheet lists the communities the link is already in."""
        response = env.client.get(
            '/share?url=https://example.test/an-article')
        assert response.status_code == 200
        assert 'probeland' in response.get_data(as_text=True)

    def test_naming_no_link_at_all(self, env):
        """D1256. This was `AttributeError: 'NoneType' object has no attribute
        'strip'` -- a 500 for a request that is merely incomplete."""
        assert env.client.get('/share').status_code == 400

    def test_naming_an_empty_link(self, env):
        """D1256."""
        assert env.client.get('/share?url=').status_code == 400

    def test_the_community_last_used_is_remembered(self, env):
        env.client.set_cookie('cross_post_community_id',
                              str(env.community.id))
        response = env.client.get(
            '/share?url=https://example.test/an-article')
        assert response.status_code == 200


class TestTheBotChallenge:
    def test_it_answers_without_a_next(self, env):
        response = env.client.get('/anoobis')
        assert response.status_code == 200
        assert response.get_data(as_text=True) == ''

    def test_a_next_on_this_instance(self, env):
        response = env.client.get('/anoobis?next=/home')
        assert response.status_code == 200

    def test_a_next_somewhere_else_is_refused(self, env):
        """D1257. The guard was right; what it did was raise, which made a
        refused open redirect into a 500 with the attacker's host in the
        message."""
        response = env.client.get('/anoobis?next=https://evil.test/x')
        assert response.status_code == 403

    def test_a_next_with_a_scheme_that_is_not_http(self, env):
        response = env.client.get('/anoobis?next=javascript:alert(1)')
        assert response.status_code in (200, 403)

    def test_a_challenge_result_nobody_asked_for(self, env):
        response = env.client.get('/bot_challenge/not-a-real-uuid')
        assert response.status_code in (200, 400, 403, 404)


class TestTheHoneyPot:
    def test_it_answers_something(self, env):
        response = env.client.get('/honey')
        assert response.status_code == 200
        assert len(response.get_data(as_text=True)) > 50

    def test_an_account_gets_nothing(self, env):
        """A signed-in reader who follows the hidden link is not a scraper."""
        login(env.client, env.author)
        response = env.client.get('/honey')
        assert response.get_data(as_text=True) == ''

    @pytest.mark.parametrize('header, value', [
        ('Sec-Fetch-Dest', 'image'),
        ('Sec-Fetch-Dest', 'audio'),
        ('Sec-Fetch-Dest', 'video'),
        ('Accept', 'image/webp,*/*'),
    ])
    def test_a_browser_prefetching_media_is_not_a_scraper(self, env, header,
                                                          value):
        response = env.client.get('/honey', headers={header: value})
        assert response.get_data(as_text=True) == ''

    def test_the_path_takes_anything_after_it(self, env):
        assert env.client.get('/honey/whatever').status_code == 200


class TestRandomCommunities:
    @pytest.mark.parametrize('path', ['/random', '/r/random'])
    def test_a_random_community(self, env, path):
        # Which one is random -- api_baseline's communities qualify too -- so
        # what is pinned is that it lands on a community page at all.
        response = env.client.get(path)
        assert response.status_code == 302
        assert response.headers['Location'].startswith('/c/')

    @pytest.mark.parametrize('path', ['/randomnsfw', '/r/randnsfw',
                                      '/r/randomnsfw'])
    def test_a_random_nsfw_community_when_there_is_none(self, env, path):
        response = env.client.get(path)
        assert response.status_code == 200
        assert 'No communities found' in response.get_data(as_text=True)

    def test_a_random_nsfw_community(self, env):
        env.community.nsfw = True
        db.session.commit()
        response = env.client.get('/randomnsfw')
        assert response.status_code == 302

    def test_an_instance_you_blocked_is_left_out(self, env):
        from tests.factories import make_instance_block
        env.community.instance_id = env.baseline.instance_remote.id
        db.session.commit()
        make_instance_block(env.author, env.baseline.instance_remote)
        login(env.client, env.author)
        response = env.client.get('/random')
        assert response.status_code == 200
        assert 'No communities found' in response.get_data(as_text=True)


class TestTheContentWarning:
    def test_it_answers(self, env):
        response = env.client.get('/content_warning')
        assert response.status_code == 200
        assert 'Vary' in response.headers
        assert 'private' in response.headers['Cache-Control']
