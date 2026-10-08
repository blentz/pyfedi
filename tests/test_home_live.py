"""Home Live (spec Amendment A): the home feed source, the newer-than cursor, the fragment and the page."""
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.models import Site
from app.utils import utcnow
from tests.factories import (make_community, make_community_member, make_post,
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

    return SimpleNamespace(client=app.test_client(), community=community, reader=reader, author=author,
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
