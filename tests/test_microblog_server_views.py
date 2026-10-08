"""Per-server views of the microblogs community: /c/microblogs@<host>.

Spec: docs/superpowers/specs/2026-10-08-microblog-instance-views-design.md
"""
import re
from datetime import timedelta

import pytest
from flask import render_template_string

from app import db
from app.community.live import is_local_microblogs, microblog_server_view, server_view_actor
from app.models import BannedInstances, Tag
from app.utils import utcnow
from tests.discovery_fixtures import fresh_cache  # noqa: F401
from tests.factories import make_community, make_instance, make_post, make_user
from tests.test_microblog_live import client, live, login, teaser_ids  # noqa: F401

pytestmark = pytest.mark.usefixtures('site', 'fresh_cache')

VIEW = '/c/microblogs@mastodon.example'


def other_toot(live, n=1):
    """A microblog in the same community by an author on another server."""
    other = make_instance(f'other{n}.example')
    author = make_user(other, f'other{n}')
    return make_post(live.microblogs, author, f'https://other{n}.example/statuses/1', microblog=True)


def real_microblogs(live):
    """A real remote community named microblogs on the test server's host."""
    community = make_community('microblogs', host='mastodon.example')
    community.ap_id = 'microblogs@mastodon.example'
    community.instance_id = live.remote.id
    db.session.commit()
    return community


class TestResolution:

    def view(self, app, actor):
        with app.test_request_context():
            found = microblog_server_view(actor)
            return found.id if found is not None else None

    def test_a_known_server_resolves(self, app, live):
        assert self.view(app, 'microblogs@mastodon.example') == live.remote.id

    def test_case_is_ignored(self, app, live):
        assert self.view(app, ' Microblogs@Mastodon.Example ') == live.remote.id

    @pytest.mark.parametrize('actor', ['microblogs@nowhere.example', 'general@mastodon.example',
                                       'microblogs', 'microblogs@', '@mastodon.example'])
    def test_anything_else_does_not(self, app, live, actor):
        assert self.view(app, actor) is None

    def test_this_server_does_not(self, app, live):
        assert self.view(app, 'microblogs@test.piefed.local') is None

    def test_a_banned_server_does_not(self, app, live):
        db.session.add(BannedInstances(domain='mastodon.example'))
        db.session.commit()

        assert self.view(app, 'microblogs@mastodon.example') is None

    def test_a_real_community_wins(self, app, live):
        real_microblogs(live)

        assert self.view(app, 'microblogs@mastodon.example') is None

    def test_server_view_actor_names_the_view(self, app, live):
        with app.test_request_context():
            assert server_view_actor(live.remote.id) == 'microblogs@mastodon.example'

    def test_server_view_actor_is_none_where_there_is_no_view(self, app, live):
        real_microblogs(live)
        with app.test_request_context():
            assert server_view_actor(live.remote.id) is None
            assert server_view_actor(999_999) is None

    def test_only_the_local_microblogs_community_is_local_microblogs(self, app, live):
        with app.test_request_context():
            assert is_local_microblogs(live.microblogs)
            assert not is_local_microblogs(make_community('general'))
            assert not is_local_microblogs(real_microblogs(live))


class TestServerViewPage:

    def test_it_lists_only_that_servers_posts(self, client, live):
        mine = live.toot()
        other_toot(live)

        html = client.get(f'{VIEW}?sort=new').get_data(as_text=True)

        assert set(teaser_ids(html)) == {mine.id}

    def test_the_title_and_heading_name_the_view(self, client, live):
        html = client.get(VIEW).get_data(as_text=True)

        assert 'microblogs@mastodon.example' in re.search(r'<title>([^<]*)</title>', html).group(1)
        assert re.search(r'<h1[^>]*>\s*microblogs@mastodon.example', html)

    def test_case_is_ignored(self, client, live):
        assert client.get('/c/Microblogs@Mastodon.Example').status_code == 200

    @pytest.mark.parametrize('path', ['/c/microblogs@nowhere.example', '/c/microblogs@test.piefed.local'])
    def test_no_view_is_404_for_a_visitor(self, client, live, path):
        assert client.get(path).status_code == 404

    def test_a_signed_in_user_with_no_view_gets_the_existing_lookup(self, client, live):
        login(client, live.viewer)

        response = client.get('/c/microblogs@nowhere.example')

        assert response.status_code == 302 and 'lookup' in response.headers['Location']

    def test_a_banned_server_is_404(self, client, live):
        live.toot()
        db.session.add(BannedInstances(domain='mastodon.example'))
        db.session.commit()

        assert client.get(VIEW).status_code == 404

    def test_a_real_community_wins(self, client, live):
        real = real_microblogs(live)
        theirs = make_post(real, live.author, 'https://mastodon.example/statuses/real', microblog=True)
        live.toot()

        html = client.get(f'{VIEW}?sort=new').get_data(as_text=True)

        assert set(teaser_ids(html)) == {theirs.id}

    def test_an_activitypub_request_is_400(self, client, live):
        response = client.get(VIEW, headers={'Accept': 'application/activity+json'})

        assert response.status_code == 400

    def test_a_server_with_no_posts_is_an_empty_page(self, client, live):
        response = client.get(VIEW)

        assert response.status_code == 200 and teaser_ids(response.get_data(as_text=True)) == []

    def test_pagination_stays_on_the_view(self, client, live):
        for _ in range(3):
            live.toot()
        live.viewer.page_length = 2
        db.session.commit()
        login(client, live.viewer)

        html = client.get(f'{VIEW}?sort=new').get_data(as_text=True)

        assert re.search(r'href="/c/microblogs(@|%40)mastodon.example\?[^"]*page=2', html)

    def test_join_and_post_controls_are_hidden(self, client, live):
        login(client, live.viewer)
        plain = client.get('/c/microblogs').get_data(as_text=True)
        assert '/community/microblogs/submit' in plain
        assert '-notification-toggle' in plain

        html = client.get(VIEW).get_data(as_text=True)

        assert '/community/microblogs/submit' not in html
        assert '/community/microblogs/subscribe' not in html
        assert '-notification-toggle' not in html

    def test_a_tag_filter_is_not_applied(self, client, live):
        mine = live.toot()
        db.session.add(Tag(name='x'))
        db.session.commit()

        html = client.get(f'{VIEW}?sort=new&tag=x').get_data(as_text=True)

        assert set(teaser_ids(html)) == {mine.id}

    def test_the_etag_differs_from_the_community(self, client, live):
        live.toot()

        view = client.get(f'{VIEW}?sort=new').headers['ETag']
        plain = client.get('/c/microblogs?sort=new').headers['ETag']

        assert view != plain
