"""app/tag/routes.py -- the tag page's sorts (the front page's Hot, Top, New, Live, Active, Scaled and Old, taken as
?sort=), and its Live fragment.

One request per test: flask_login answers a second one as the first one's user.
"""
import re
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.models import Site, Tag, post_tag
from app.utils import utcnow
from tests.factories import make_community, make_community_member, make_post, make_user_block


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def teaser_ids(html):
    return [int(found) for found in re.findall(r'id="post_(\d+)"', html)]


@pytest.fixture
def tagged(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    g.site.private_instance = False
    community = make_community('tagland')
    tag = Tag(name='solarstorm', display_as='SolarStorm', banned=False, post_count=0)
    db.session.add(tag)
    db.session.commit()
    reader = api_baseline.user3
    author = api_baseline.user2
    make_community_member(reader, community)
    counter = iter(range(1, 10_000))

    def post(tagged=True, **columns):
        created = make_post(community, author, f'https://test.piefed.local/t/{next(counter)}')
        for name, value in columns.items():
            setattr(created, name, value)
        if tagged:
            db.session.execute(post_tag.insert().values(post_id=created.id, tag_id=tag.id))
        db.session.commit()
        return created

    return SimpleNamespace(client=app.test_client(), community=community, tag=tag, reader=reader, author=author,
                           post=post)


def rendered(tagged, query=''):
    """The kwargs show_tag hands its template for /tag/solarstorm<query>."""
    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        response = tagged.client.get(f'/tag/solarstorm{query}')
    assert response.status_code == 200
    return render.call_args.kwargs


def listed(tagged, query=''):
    return [post.id for post in rendered(tagged, query)['posts'].items]


class TestTagSort:

    def test_a_known_sort_is_taken(self, app):
        from app.tag.routes import tag_sort
        with app.test_request_context():
            assert tag_sort('top_1w') == 'top_1w'

    def test_an_anonymous_reader_gets_hot(self, app):
        from app.tag.routes import tag_sort
        with app.test_request_context():
            assert tag_sort(None) == 'hot'
            assert tag_sort('sideways') == 'hot'

    def test_a_reader_gets_their_default(self, app, tagged):
        from flask_login import login_user

        from app.tag.routes import tag_sort
        tagged.reader.default_sort = 'active'
        db.session.commit()
        with app.test_request_context():
            login_user(tagged.reader)
            assert tag_sort(None) == 'active'
            assert tag_sort('new') == 'new'

    def test_a_default_that_is_no_sort_is_hot(self, app, tagged):
        from flask_login import login_user

        from app.tag.routes import tag_sort
        tagged.reader.default_sort = None
        db.session.commit()
        with app.test_request_context():
            login_user(tagged.reader)
            assert tag_sort(None) == 'hot'


class TestOrders:

    def test_hot_orders_by_ranking(self, tagged):
        low = tagged.post(ranking=1)
        high = tagged.post(ranking=50)

        assert listed(tagged, '?sort=hot') == [high.id, low.id]

    def test_hot_is_the_anonymous_default(self, tagged):
        low = tagged.post(ranking=1)
        high = tagged.post(ranking=50)

        assert listed(tagged) == [high.id, low.id]
        assert rendered(tagged)['sort'] == 'hot'

    def test_new_orders_by_posted_at(self, tagged):
        older = tagged.post(posted_at=utcnow() - timedelta(hours=3), ranking=50)
        newer = tagged.post(posted_at=utcnow() - timedelta(hours=1), ranking=1)

        assert listed(tagged, '?sort=new') == [newer.id, older.id]

    def test_old_orders_oldest_first(self, tagged):
        older = tagged.post(posted_at=utcnow() - timedelta(hours=3))
        newer = tagged.post(posted_at=utcnow() - timedelta(hours=1))

        assert listed(tagged, '?sort=old') == [older.id, newer.id]

    def test_active_keeps_commented_posts_most_recent_first(self, tagged):
        quiet = tagged.post(reply_count=0, last_active=utcnow())
        earlier = tagged.post(reply_count=2, last_active=utcnow() - timedelta(hours=2))
        later = tagged.post(reply_count=1, last_active=utcnow() - timedelta(minutes=5))

        ids = listed(tagged, '?sort=active')
        assert ids == [later.id, earlier.id]
        assert quiet.id not in ids

    def test_scaled_orders_by_scaled_ranking_and_drops_bots(self, tagged):
        bot = tagged.post(ranking_scaled=90, from_bot=True)
        low = tagged.post(ranking_scaled=5)
        high = tagged.post(ranking_scaled=80)

        ids = listed(tagged, '?sort=scaled')
        assert ids == [high.id, low.id]
        assert bot.id not in ids

    @pytest.mark.parametrize('sort, inside, outside', [
        ('top_1h', timedelta(minutes=30), timedelta(hours=2)),
        ('top_6h', timedelta(hours=5), timedelta(hours=7)),
        ('top_12h', timedelta(hours=11), timedelta(hours=13)),
        ('top', timedelta(hours=23), timedelta(hours=25)),
        ('top_1w', timedelta(days=6), timedelta(days=8)),
        ('top_1m', timedelta(days=27), timedelta(days=29)),
        ('top_1y', timedelta(days=364), timedelta(days=366)),
    ])
    def test_top_keeps_its_window_by_score(self, tagged, sort, inside, outside):
        low = tagged.post(posted_at=utcnow() - inside, score=1)
        high = tagged.post(posted_at=utcnow() - inside, score=9)
        gone = tagged.post(posted_at=utcnow() - outside, score=99)

        ids = listed(tagged, f'?sort={sort}')
        assert ids == [high.id, low.id]
        assert gone.id not in ids

    def test_top_all_keeps_every_post(self, tagged):
        ancient = tagged.post(posted_at=utcnow() - timedelta(days=4000), score=99)
        recent = tagged.post(score=1)

        assert listed(tagged, '?sort=top_all') == [ancient.id, recent.id]

    def test_an_unknown_sort_is_hot(self, tagged):
        assert rendered(tagged, '?sort=sideways')['sort'] == 'hot'

    def test_the_sort_keeps_the_tag_filter(self, tagged):
        tagged.post(tagged=False)
        kept = tagged.post()

        assert listed(tagged, '?sort=new') == [kept.id]

    def test_the_viewers_blocks_still_apply(self, tagged):
        tagged.post()
        make_user_block(tagged.reader, tagged.author)
        login(tagged.client, tagged.reader)

        assert listed(tagged, '?sort=new') == []


class TestPagination:

    def test_the_links_carry_the_sort(self, tagged):
        for _ in range(51):
            tagged.post()

        assert 'sort=active' in rendered(tagged, '?sort=active&page=2')['prev_url']

    def test_the_next_link_carries_the_sort(self, tagged):
        for _ in range(51):
            tagged.post()

        assert 'sort=top_1w' in rendered(tagged, '?sort=top_1w')['next_url']

    def test_live_continues_in_new(self, tagged):
        for _ in range(51):
            tagged.post()
        login(tagged.client, tagged.reader)

        kwargs = rendered(tagged, '?sort=live')
        assert kwargs['live'] is True
        assert 'sort=new' in kwargs['next_url'] and 'page=2' in kwargs['next_url']


class TestLivePage:

    def test_live_carries_the_client_contract(self, tagged):
        older = tagged.post(posted_at=utcnow() - timedelta(minutes=5))
        newest = tagged.post()
        login(tagged.client, tagged.reader)

        html = tagged.client.get('/tag/solarstorm?sort=live').get_data(as_text=True)

        assert 'id="live_feed"' in html
        assert 'data-posts-url="/tag/solarstorm/live_posts"' in html
        assert 'data-sse-url=""' in html
        assert f'data-cursor="{newest.id}"' in html
        assert 'id="live_status"' in html and 'id="live_pill"' in html and 'js/live_feed.js' in html
        assert teaser_ids(html) == [newest.id, older.id]

    def test_an_empty_live_page_has_cursor_zero(self, tagged):
        login(tagged.client, tagged.reader)

        assert 'data-cursor="0"' in tagged.client.get('/tag/solarstorm?sort=live').get_data(as_text=True)

    @pytest.mark.parametrize('query, as_reader', [
        ('?sort=live', False),
        ('?sort=live&page=2', True),
        ('?sort=live&category=community&category_id=1', True),
    ])
    def test_live_falls_back_to_new_where_unavailable(self, tagged, query, as_reader):
        if as_reader:
            login(tagged.client, tagged.reader)

        kwargs = rendered(tagged, query)
        assert kwargs['sort'] == 'new' and kwargs['live'] is False and kwargs['live_cursor'] == 0

    def test_a_reader_whose_default_is_live_gets_live(self, tagged):
        tagged.reader.default_sort = 'live'
        db.session.commit()
        login(tagged.client, tagged.reader)

        assert rendered(tagged)['live'] is True


class TestNav:

    def test_every_sort_is_offered_to_a_reader(self, tagged):
        login(tagged.client, tagged.reader)

        html = tagged.client.get('/tag/solarstorm?sort=new').get_data(as_text=True)

        for sort in ('hot', 'new', 'live', 'old', 'active', 'scaled', 'top_1h', 'top_6h', 'top_12h', 'top',
                     'top_1w', 'top_1m', 'top_1y', 'top_all'):
            assert f'/tag/solarstorm?sort={sort}"' in html
        assert 'id="live_feed"' not in html

    def test_the_current_sort_is_marked(self, tagged):
        html = tagged.client.get('/tag/solarstorm?sort=active').get_data(as_text=True)

        assert re.search(r'href="/tag/solarstorm\?sort=active" class="btn btn-primary"', html)
        assert re.search(r'href="/tag/solarstorm\?sort=hot" class="btn btn-outline-secondary"', html)
        assert re.search(r'value="/tag/solarstorm\?sort=active" [^>]*selected', html)

    def test_a_top_sort_marks_the_top_button(self, tagged):
        html = tagged.client.get('/tag/solarstorm?sort=top_1w').get_data(as_text=True)

        assert re.search(r'class="btn btn-primary dropdown-toggle"', html)
        assert re.search(r'class="dropdown-item active" href="/tag/solarstorm\?sort=top_1w"', html)

    def test_no_live_for_anonymous(self, tagged):
        html = tagged.client.get('/tag/solarstorm').get_data(as_text=True)

        assert 'sort=live' not in html
        assert '/tag/solarstorm?sort=hot"' in html

    def test_a_category_filter_is_kept_and_live_is_not_offered(self, tagged):
        login(tagged.client, tagged.reader)
        community_id = tagged.community.id

        html = tagged.client.get(f'/tag/solarstorm?category=community&category_id={community_id}').get_data(as_text=True)

        assert f'/tag/solarstorm?sort=new&amp;category=community&amp;category_id={community_id}"' in html
        assert 'sort=live' not in html


class TestLiveFragment:

    def url(self, after=0, tag='solarstorm'):
        return f'/tag/{tag}/live_posts?after={after}'

    def test_new_posts_come_back_with_the_cursor(self, tagged):
        post = tagged.post()
        login(tagged.client, tagged.reader)

        response = tagged.client.get(self.url())

        assert response.status_code == 200
        assert teaser_ids(response.get_data(as_text=True)) == [post.id]
        assert response.headers['X-Live-Cursor'] == str(post.id)

    def test_only_posts_after_the_cursor_inside_the_window_and_tagged(self, tagged):
        seen = tagged.post()
        tagged.post(tagged=False)
        tagged.post(posted_at=utcnow() - timedelta(hours=2))
        newer = tagged.post()
        login(tagged.client, tagged.reader)

        response = tagged.client.get(self.url(seen.id))

        assert teaser_ids(response.get_data(as_text=True)) == [newer.id]

    def test_nothing_new_is_204(self, tagged):
        seen = tagged.post()
        login(tagged.client, tagged.reader)

        assert tagged.client.get(self.url(seen.id)).status_code == 204

    def test_the_viewers_blocks_still_apply(self, tagged):
        tagged.post()
        make_user_block(tagged.reader, tagged.author)
        login(tagged.client, tagged.reader)

        assert tagged.client.get(self.url()).status_code == 204

    def test_an_unknown_tag_is_404(self, tagged):
        login(tagged.client, tagged.reader)

        assert tagged.client.get(self.url(tag='nosuchtag')).status_code == 404

    @pytest.mark.parametrize('query', ['', '?after=abc'])
    def test_a_bad_cursor_is_400(self, tagged, query):
        login(tagged.client, tagged.reader)

        assert tagged.client.get(f'/tag/solarstorm/live_posts{query}').status_code == 400

    def test_anonymous_is_sent_to_log_in(self, tagged):
        response = tagged.client.get(self.url())

        assert response.status_code == 302 and '/auth/login' in response.headers['Location']

    def test_the_tag_name_is_case_insensitive(self, tagged):
        post = tagged.post()
        login(tagged.client, tagged.reader)

        assert teaser_ids(tagged.client.get(self.url(tag='SolarStorm')).get_data(as_text=True)) == [post.id]
