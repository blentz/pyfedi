"""Home Live (spec Amendment A): the home feed source, the newer-than cursor, the fragment and the page."""
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.models import Site
from app.utils import utcnow
from tests.factories import (make_community, make_community_member, make_instance, make_post,
                             make_user_block)


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def teaser_ids(html):
    return [int(found) for found in re.findall(r'id="post_(\d+)"', html)]


@pytest.fixture
def home(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    g.site.private_instance = False
    community = make_community('homeland')
    community.show_all = True
    community.show_popular = True
    db.session.commit()
    reader = api_baseline.user3
    author = api_baseline.user2
    make_community_member(reader, community)
    counter = iter(range(1, 10_000))

    def post(**columns):
        created = make_post(community, author, f'https://test.piefed.local/h/{next(counter)}')
        for name, value in columns.items():
            setattr(created, name, value)
        db.session.commit()
        return created

    def media_post():
        instance = make_instance('video.example', software='peertube')
        videos = make_community('videos')
        videos.show_all = True
        videos.ap_id = 'videos@video.example'
        videos.instance_id = instance.id
        db.session.commit()
        return make_post(videos, author, f'https://video.example/p/{next(counter)}')

    return SimpleNamespace(media_post=media_post, client=app.test_client(), community=community, reader=reader, author=author,
                           post=post, baseline=api_baseline)


class TestNewerThan:

    def ids(self, app, home, after, view_filter='all'):
        from flask_login import login_user

        from app.main.routes import home_feed_source
        from app.utils import get_deduped_post_ids

        with app.test_request_context():
            login_user(home.reader)
            community_ids, community_sql = home_feed_source(view_filter)
            return get_deduped_post_ids('', community_ids, 'new', include_following=view_filter == 'subscribed',
                                        community_sql=community_sql,
                                        newer_than=(after, utcnow() - timedelta(hours=1)))

    def test_only_posts_after_the_cursor_and_inside_the_window(self, app, home):
        seen = home.post()
        newer = home.post()
        home.post(posted_at=utcnow() - timedelta(hours=2))

        assert self.ids(app, home, seen.id) == [newer.id]

    def test_the_viewers_blocks_still_apply(self, app, home):
        kept = home.post()
        make_user_block(home.reader, home.author)

        assert kept.id not in self.ids(app, home, 0)

    @pytest.mark.parametrize('view_filter', ['subscribed', 'local', 'popular', 'all'])
    def test_each_tab_sees_its_own_post(self, app, home, view_filter):
        post = home.post()

        assert post.id in self.ids(app, home, 0, view_filter)

    def test_media_does_not_see_a_local_discussion_post(self, app, home):
        post = home.post()

        assert post.id not in self.ids(app, home, 0, 'media')

    def test_media_sees_a_post_from_a_media_instance(self, app, home):
        post = home.media_post()

        assert post.id in self.ids(app, home, 0, 'media')



class TestHomeLiveFragment:

    def url(self, view_filter='all', after=0):
        return f'/home/live_posts/{view_filter}?after={after}'

    @pytest.mark.parametrize('view_filter', ['subscribed', 'local', 'popular', 'all'])
    def test_new_posts_come_back_with_the_cursor(self, home, view_filter):
        post = home.post()
        login(home.client, home.reader)

        response = home.client.get(self.url(view_filter))

        assert response.status_code == 200
        assert post.id in teaser_ids(response.get_data(as_text=True))
        assert response.headers['X-Live-Cursor'] == str(post.id)

    def test_media_returns_a_post_from_a_media_instance(self, home):
        post = home.media_post()
        login(home.client, home.reader)

        response = home.client.get(self.url('media'))

        assert response.status_code == 200
        assert post.id in teaser_ids(response.get_data(as_text=True))
        assert response.headers['X-Live-Cursor'] == str(post.id)

    def test_media_with_nothing_new_is_204(self, home):
        home.post()
        login(home.client, home.reader)

        assert home.client.get(self.url('media')).status_code == 204

    def test_nothing_new_is_204(self, home):
        latest = home.post()
        login(home.client, home.reader)

        response = home.client.get(self.url(after=latest.id))

        assert response.status_code == 204 and response.get_data() == b''

    def test_at_most_forty(self, home):
        for _ in range(41):
            home.post()
        login(home.client, home.reader)

        assert len(teaser_ids(home.client.get(self.url()).get_data(as_text=True))) == 40

    @pytest.mark.parametrize('view_filter', ['moderating', 'nosuch'])
    def test_other_filters_are_404(self, home, view_filter):
        login(home.client, home.reader)

        assert home.client.get(self.url(view_filter)).status_code == 404

    @pytest.mark.parametrize('query', ['', '?after=', '?after=abc'])
    def test_a_bad_cursor_is_400(self, home, query):
        login(home.client, home.reader)

        assert home.client.get(f'/home/live_posts/all{query}').status_code == 400

    def test_anonymous_is_sent_to_log_in(self, home):
        response = home.client.get(self.url())

        assert response.status_code == 302 and '/auth/login' in response.headers['Location']

    def test_subscribed_includes_a_followed_authors_post_outside_show_all(self, home):
        from tests.factories import make_follow

        hidden = make_community('quietplace')
        hidden.show_all = False
        db.session.commit()
        post = make_post(hidden, home.author, 'https://test.piefed.local/q/1')
        make_follow(home.reader, home.author)
        login(home.client, home.reader)

        assert post.id in teaser_ids(home.client.get(self.url('subscribed')).get_data(as_text=True))

    def test_each_tab_has_its_own_rate_budget(self, home):
        from app import create_app, limiter
        from tests.conftest import TestConfig

        class LimitedConfig(TestConfig):
            RATELIMIT_ENABLED = True

        limited = create_app(LimitedConfig)
        try:
            with limited.app_context():
                client = limited.test_client()
                login(client, home.reader)
                limiter.reset()
                every = [client.get(self.url(after=0)).status_code for _ in range(13)]
                local = client.get(self.url('local')).status_code
        finally:
            limiter.reset()
            limiter.enabled = False

        assert 429 not in every[:12] and every[12] == 429
        assert local != 429


class TestHomeLivePage:

    def test_the_live_entry_shows_for_a_logged_in_reader(self, home):
        login(home.client, home.reader)

        assert '/home/live/all' in home.client.get('/home/new/all').get_data(as_text=True)

    def test_no_live_entry_for_anonymous(self, home):
        assert '/home/live/' not in home.client.get('/home/new/all').get_data(as_text=True)

    @pytest.mark.parametrize('view_filter, key, has_cursor', [
        ('subscribed', 'any', True), ('local', 'local', True), ('popular', 'popular', True),
        ('media', 'media', False), ('all', 'any', True)])
    def test_the_live_page_carries_the_client_contract(self, app, home, monkeypatch, view_filter, key, has_cursor):
        monkeypatch.setitem(app.config, 'NOTIF_SERVER', 'https://notifs.example')
        post = home.post()
        login(home.client, home.reader)

        html = home.client.get(f'/home/live/{view_filter}').get_data(as_text=True)

        assert 'id="live_feed"' in html
        assert f'data-posts-url="/home/live_posts/{view_filter}"' in html
        assert f'data-sse-url="https://notifs.example/live/stream?feed={key}"' in html
        assert 'id="live_status"' in html and 'id="live_pill"' in html and 'js/live_feed.js' in html
        assert 'id="auto-reload"' not in html
        assert f'data-cursor="{post.id if has_cursor else 0}"' in html  # the fixture post is not a media post

    def test_no_instance_stickies_in_live(self, home):
        sticky = home.post(instance_sticky=True)
        login(home.client, home.reader)

        assert sticky.id not in teaser_ids(home.client.get('/home/live/all').get_data(as_text=True))

    @pytest.mark.parametrize('path, as_reader', [
        ('/home/live/all', False),
        ('/home/live/moderating', True),
        ('/home/live/all?page=1', True),
        ('/home/live/all?tag=news', True),
    ])
    def test_live_falls_back_to_new_where_unavailable(self, home, path, as_reader):
        if as_reader:
            login(home.client, home.reader)

        response = home.client.get(path)

        assert response.status_code in (200, 302)
        assert 'id="live_feed"' not in response.get_data(as_text=True)

    def test_older_posts_continue_in_new(self, app, home, monkeypatch):
        monkeypatch.setitem(app.config, 'PAGE_LENGTH', 2)
        for _ in range(3):
            home.post()
        login(home.client, home.reader)

        html = home.client.get('/home/live/all').get_data(as_text=True)

        assert 'Older posts' in html and '/home/new/all?page=1' in html

    def test_the_nav_highlights_live_on_a_live_page(self, home):
        login(home.client, home.reader)

        live = home.client.get('/home/live/all').get_data(as_text=True)
        new = home.client.get('/home/new/all').get_data(as_text=True)

        assert re.search(r'href="/home/live/all" class="btn btn-primary"', live)
        assert re.search(r'href="/home/live/all" class="btn btn-outline-secondary"', new)

    def test_a_live_page_caches_no_result_list(self, home, redis_double):
        home.post()
        login(home.client, home.reader)

        home.client.get('/home/new/all')
        assert redis_double.keys('feed:*'), 'control: a plain page caches its result list'
        redis_double.flushall()
        home.client.get('/home/live/all')

        assert redis_double.keys('feed:*') == []

    def test_a_live_page_has_no_auto_reload_url(self, home):
        login(home.client, home.reader)

        live = home.client.get('/home/live/all').get_data(as_text=True)
        new = home.client.get('/home/new/all').get_data(as_text=True)

        assert "var reloadUrl = '';" in live
        assert "var reloadUrl = 'None'" not in live
        assert "var reloadUrl = '/home/new/all?fragment=1';" in new
