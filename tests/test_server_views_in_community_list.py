"""Server views in the All Communities list (/communities).

Spec: docs/superpowers/specs/2026-10-08-server-views-in-community-list-design.md
"""
from datetime import timedelta

import pytest

from app import db
from app.models import BannedInstances, Community, InstanceBlock
from app.utils import utcnow
from tests.discovery_fixtures import fresh_cache  # noqa: F401
from tests.factories import make_community, make_instance, make_post, make_user
from tests.test_microblog_live import client, live, login  # noqa: F401

pytestmark = pytest.mark.usefixtures('site', 'fresh_cache')


def server(live, host, posts=1):
    """`posts` microblogs in the microblogs community by an author on `host`."""
    instance = make_instance(host)
    author = make_user(instance, f'u_{host}')
    for n in range(posts):
        make_post(live.microblogs, author, f'https://{host}/statuses/{n}', microblog=True)
    return instance


def community(name, post_count):
    row = make_community(name)
    row.post_count = post_count
    db.session.commit()
    return row


from app.community.server_view_list import (ServerView, like_pattern, paginate_union, parse_sort,
                                            server_view_select, view_side_allowed)


def view_rows(app, live, **kwargs):
    with app.test_request_context():
        return {row.title: row for row in db.session.execute(server_view_select(live.microblogs.id, **kwargs)).all()}


def page(app, live, sort_by='post_count desc', n=1, per_page=50, view=True, query=None, **kwargs):
    with app.test_request_context():
        communities = query if query is not None else Community.query.filter_by(banned=False)
        views = server_view_select(live.microblogs.id, **kwargs) if view else None
        result = paginate_union(communities, views, sort_by, n, per_page, live.microblogs)
        return result, [item.link() for item in result.items]


class TestServerViewSelect:

    def test_one_row_per_server_with_its_counts(self, app, live):
        live.toot()
        live.toot(posted_at=utcnow() - timedelta(days=10), reply_count=3)

        row = view_rows(app, live)['microblogs@mastodon.example']

        assert row.community_id is None and row.instance_id == live.remote.id
        assert (row.subscriptions_count, row.post_count, row.post_reply_count, row.active_weekly) == (0, 2, 3, 1)
        assert row.last_active > row.created_at

    def test_local_deleted_banned_and_real_community_servers_are_left_out(self, app, live):
        make_post(live.microblogs, live.viewer, 'https://test.piefed.local/post/1', microblog=True)
        live.toot(deleted=True)
        server(live, 'banned.example')
        db.session.add(BannedInstances(domain='banned.example'))
        server(live, 'piefed.example')
        real = make_community('microblogs', host='piefed.example')
        real.ap_id = 'microblogs@piefed.example'
        db.session.commit()

        assert view_rows(app, live) == {}

    def test_a_ban_with_no_domain_does_not_hide_every_server(self, app, live):
        live.toot()
        db.session.add(BannedInstances(domain=None))
        db.session.commit()

        assert list(view_rows(app, live)) == ['microblogs@mastodon.example']

    def test_a_viewers_blocked_servers_are_left_out(self, app, live):
        live.toot()
        server(live, 'other.example')

        assert list(view_rows(app, live, blocked_instance_ids=[live.remote.id])) == ['microblogs@other.example']

    @pytest.mark.parametrize('search, expected', [
        ('microblogs', {'microblogs@mastodon.example', 'microblogs@infosec.exchange'}),
        ('InfoSec', {'microblogs@infosec.exchange'}),
        ('%', set()),
        ('_', set()),
    ])
    def test_search_matches_the_title_literally(self, app, live, search, expected):
        live.toot()
        server(live, 'infosec.exchange')

        assert set(view_rows(app, live, search=search)) == expected

    def test_the_instance_filter_picks_one_host(self, app, live):
        live.toot()
        server(live, 'infosec.exchange')

        assert list(view_rows(app, live, host='InfoSec.Exchange')) == ['microblogs@infosec.exchange']


class TestPaginateUnion:

    def test_views_sort_among_communities(self, app, live):
        community('big', 5)
        community('small', 1)
        server(live, 'mid.example', posts=3)

        _, links = page(app, live, search='', sort_by='post_count desc')

        assert links.index('big') < links.index('microblogs@mid.example') < links.index('small')

    def test_items_are_communities_or_server_views(self, app, live):
        community('big', 5)
        live.toot()

        result, _ = page(app, live)

        kinds = {type(item) for item in result.items}
        assert kinds == {Community, ServerView}
        view = next(item for item in result.items if isinstance(item, ServerView))
        assert view.link() == view.display_name() == 'microblogs@mastodon.example'
        assert view.id is None and view.subscriptions_count == 0 and not view.nsfw and not view.nsfl
        assert view.is_server_view and view.instance.id == live.remote.id
        assert view.icon_image('tiny') == live.microblogs.icon_image('tiny')

    def test_pages_continue_without_gaps_or_repeats_when_keys_tie(self, app, live):
        for n in range(3):
            community(f'c{n}', 1)
        for n in range(3):
            server(live, f's{n}.example', posts=1)

        first, one = page(app, live, per_page=4)
        second, two = page(app, live, per_page=4, n=2)

        assert first.total == 7   # 3 communities + 3 views + the microblogs community itself
        assert len(one) == 4 and len(two) == 3 and not set(one) & set(two)
        assert first.has_next and not first.has_prev and first.next_num == 2 and first.prev_num is None
        assert second.has_prev and not second.has_next and second.prev_num == 1 and second.next_num is None

    def test_without_a_view_side_only_communities_are_listed(self, app, live):
        live.toot()

        _, links = page(app, live, view=False)

        assert not any(link.startswith('microblogs@') for link in links)

    def test_a_wildcard_ban_hides_the_view(self, app, live):
        server(live, 'evil.example')
        db.session.add(BannedInstances(domain='ev*l.example'))
        db.session.commit()

        _, links = page(app, live)

        assert 'microblogs@evil.example' not in links

    def test_a_page_below_one_is_page_one(self, app, live):
        result, _ = page(app, live, n=0)

        assert result.page == 1


class TestHelpers:

    @pytest.mark.parametrize('sort_by, expected', [
        ('post_count desc', ('post_count', 'desc')),
        ('title', ('title', 'asc')),
        ('title ASC', ('title', 'asc')),
        ('', ('active_weekly', 'desc')),
        (None, ('active_weekly', 'desc')),
        ('nonsense desc', ('active_weekly', 'desc')),
        ('id desc', ('active_weekly', 'desc')),
    ])
    def test_parse_sort(self, sort_by, expected):
        assert parse_sort(sort_by) == expected

    def test_like_pattern_escapes_wildcards(self):
        assert like_pattern('a%b_c\\') == '%a\\%b\\_c\\\\%'

    base = dict(home_select='any', subscribe_select='any', topic_id=0, language_id=0, feed_id=0, platform='',
                nsfw='all')

    def test_the_view_side_is_allowed_by_default(self):
        assert view_side_allowed(**self.base)

    @pytest.mark.parametrize('change', [
        {'home_select': 'local'}, {'subscribe_select': 'subscribed'}, {'subscribe_select': 'not_subscribed'},
        {'topic_id': 3}, {'topic_id': -1}, {'language_id': 2}, {'feed_id': 1}, {'platform': 'peertube'},
        {'nsfw': 'yes'},
    ])
    def test_filters_a_view_cannot_match_drop_it(self, change):
        assert not view_side_allowed(**{**self.base, **change})

    @pytest.mark.parametrize('change', [{'home_select': 'remote'}, {'nsfw': 'no'}])
    def test_filters_a_view_can_match_keep_it(self, change):
        assert view_side_allowed(**{**self.base, **change})
