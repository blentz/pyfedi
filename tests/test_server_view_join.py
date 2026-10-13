"""Joining a server view: /c/microblogs@<host> Join puts that server's public microblogs in the
Subscribed feed. Local only; nothing federates.

Spec: docs/superpowers/specs/2026-10-13-server-view-join-design.md
"""
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.models import BannedInstances, CommunityMember, ServerViewMember
from tests.discovery_fixtures import fresh_cache  # noqa: F401
from tests.factories import make_community, make_instance_block, make_post
from tests.test_feed_lifecycle_routes import csrf
from tests.test_microblog_live import client, live, login, teaser_ids  # noqa: F401
from tests.test_microblog_server_views import VIEW, other_toot, real_microblogs

pytestmark = pytest.mark.usefixtures('site', 'fresh_cache')

SUBSCRIBE = '/community/microblogs@mastodon.example/subscribe'
UNSUBSCRIBE = '/community/microblogs@mastodon.example/unsubscribe'
HTMX = {'HX-Request': 'true'}


def send(client, url, headers=None):
    """POST with a real CSRF token: login_required validates it whatever WTF_CSRF_ENABLED says.
    One token per client: the session keeps the raw half it was signed from. flask_wtf caches the
    raw half on `g`, which a page rendered earlier in the test has already set, and then never
    writes it to the session csrf() reads: pop it first."""
    if not hasattr(client, 'csrf_token'):
        g.pop('csrf_token', None)
        client.csrf_token = csrf(client.application, client)
    return client.post(url, data={'csrf_token': client.csrf_token}, headers=headers)


def rows(user):
    return ServerViewMember.query.filter_by(user_id=user.id).all()


@pytest.fixture(autouse=True)
def approved(site, live):
    """approval_required sends an account with no keypair to /auth/please_wait on a closed site."""
    live.viewer.private_key = 'a key'
    db.session.commit()


def join(client, live):
    login(client, live.viewer)
    return send(client, SUBSCRIBE)


def subscribed_feed(client):
    return teaser_ids(client.get('/home/new/subscribed').get_data(as_text=True))


class TestJoinAndLeave:

    def test_join_records_the_server(self, client, live):
        response = join(client, live)

        assert response.status_code == 302
        assert [(r.user_id, r.instance_id) for r in rows(live.viewer)] == [(live.viewer.id, live.remote.id)]

    def test_join_twice_keeps_one_row(self, client, live):
        join(client, live)
        send(client, SUBSCRIBE)

        assert len(rows(live.viewer)) == 1

    def test_join_does_not_join_the_microblogs_community(self, client, live):
        join(client, live)

        assert CommunityMember.query.filter_by(user_id=live.viewer.id).count() == 0
        assert live.microblogs.subscriptions_count in (0, None)

    def test_leave_deletes_the_row(self, client, live):
        join(client, live)

        response = send(client, UNSUBSCRIBE)

        assert response.status_code == 302
        assert rows(live.viewer) == []

    def test_leave_when_not_joined_is_a_no_op(self, client, live):
        login(client, live.viewer)

        assert send(client, UNSUBSCRIBE).status_code == 302

    def test_htmx_join_swaps_in_leave(self, client, live):
        login(client, live.viewer)

        html = send(client, SUBSCRIBE, headers=HTMX).get_data(as_text=True)

        assert UNSUBSCRIBE in html and 'Leave' in html

    def test_htmx_leave_swaps_in_join(self, client, live):
        join(client, live)

        html = send(client, UNSUBSCRIBE, headers=HTMX).get_data(as_text=True)

        assert SUBSCRIBE in html and 'Join' in html

    def test_an_unknown_server_is_404(self, client, live):
        login(client, live.viewer)

        with patch('app.community.routes.search_for_community', return_value=None):   # no webfinger
            assert send(client, '/community/microblogs@nowhere.example/subscribe').status_code == 404
        assert ServerViewMember.query.count() == 0

    def test_a_banned_server_cannot_be_joined(self, client, live):
        db.session.add(BannedInstances(domain='mastodon.example'))
        db.session.commit()
        login(client, live.viewer)

        send(client, SUBSCRIBE)

        assert rows(live.viewer) == []

    def test_a_banned_server_can_still_be_left(self, client, live):
        join(client, live)
        db.session.add(BannedInstances(domain='mastodon.example'))
        db.session.commit()

        send(client, UNSUBSCRIBE)

        assert rows(live.viewer) == []

    def test_a_real_community_takes_the_community_path(self, client, live):
        real_microblogs(live)

        join(client, live)

        assert rows(live.viewer) == []


class TestSubscribedFeed:

    def test_a_joined_servers_microblog_is_in_the_feed(self, client, live):
        mine = live.toot()
        join(client, live)

        assert subscribed_feed(client) == [mine.id]

    def test_it_is_not_there_before_joining_or_after_leaving(self, client, live):
        live.toot()
        login(client, live.viewer)
        assert subscribed_feed(client) == []

        send(client, SUBSCRIBE)
        send(client, UNSUBSCRIBE)

        assert subscribed_feed(client) == []

    @pytest.mark.parametrize('visibility', ['unlisted', 'followers'])
    def test_only_public_posts_arrive(self, client, live, visibility):
        live.toot(visibility=visibility)
        join(client, live)

        assert subscribed_feed(client) == []

    def test_another_servers_microblog_does_not_arrive(self, client, live):
        other_toot(live)
        join(client, live)

        assert subscribed_feed(client) == []

    def test_the_authors_posts_in_another_community_do_not_arrive(self, client, live):
        make_post(make_community('general'), live.author, 'https://mastodon.example/statuses/general')
        join(client, live)

        assert subscribed_feed(client) == []

    def test_a_blocked_instance_stays_hidden(self, client, live):
        live.toot()
        join(client, live)
        make_instance_block(live.viewer, live.remote)

        assert subscribed_feed(client) == []

    def test_the_live_fragment_returns_it(self, client, live):
        join(client, live)
        mine = live.toot()

        response = client.get('/home/live_posts/subscribed?after=0')

        assert response.status_code == 200
        assert teaser_ids(response.get_data(as_text=True)) == [mine.id]


class TestPage:

    def test_a_signed_in_user_sees_join_then_leave(self, client, live):
        login(client, live.viewer)
        assert SUBSCRIBE in client.get(VIEW).get_data(as_text=True)

        send(client, SUBSCRIBE)
        html = client.get(VIEW).get_data(as_text=True)

        assert UNSUBSCRIBE in html and SUBSCRIBE not in html

    def test_the_other_controls_stay_hidden(self, client, live):
        login(client, live.viewer)

        html = client.get(VIEW).get_data(as_text=True)

        assert '/community/microblogs/submit' not in html
        assert '/community/microblogs/subscribe' not in html
        assert '-notification-toggle' not in html
        assert 'Set my flair' not in html

    def test_the_dillo_theme_shows_join_then_leave(self, client, live):
        live.viewer.theme = 'dillo'
        login(client, live.viewer)
        assert SUBSCRIBE in client.get(VIEW).get_data(as_text=True)

        send(client, SUBSCRIBE)
        html = client.get(VIEW).get_data(as_text=True)

        assert UNSUBSCRIBE in html
        assert '/community/microblogs/submit' not in html

    def test_a_visitor_sees_no_join(self, client, live):
        assert SUBSCRIBE not in client.get(VIEW).get_data(as_text=True)


class TestCommunityList:

    def page(self, client):
        return client.get('/communities?home_select=any').get_data(as_text=True)

    def test_a_server_view_row_shows_join_then_leave(self, client, live):
        live.toot()
        login(client, live.viewer)
        assert SUBSCRIBE in self.page(client)

        send(client, SUBSCRIBE)
        html = self.page(client)

        assert UNSUBSCRIBE in html and SUBSCRIBE not in html
