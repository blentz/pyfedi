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


FRAGMENT = '/community/microblogs@mastodon.example/live/posts'


class TestServerViewLive:

    def keys(self, app, post, community):
        from app.community.live import live_feed_keys

        with app.test_request_context():
            return live_feed_keys(post, community, False)

    def test_a_remote_authors_post_wakes_its_server_view(self, app, live):
        post = live.toot()

        assert f'instance:{live.remote.id}' in self.keys(app, post, live.microblogs)

    def test_a_local_authors_post_does_not(self, app, live):
        post = make_post(live.microblogs, live.viewer, 'https://test.piefed.local/post/1', microblog=True)

        assert not any(key.startswith('instance:') for key in self.keys(app, post, live.microblogs))

    def test_a_post_in_another_community_does_not(self, app, live):
        general = make_community('general')
        post = make_post(general, live.author, 'https://mastodon.example/statuses/g')

        assert not any(key.startswith('instance:') for key in self.keys(app, post, general))

    def test_the_fragment_serves_only_that_servers_new_posts(self, client, live):
        mine = live.toot()
        other_toot(live)
        login(client, live.viewer)

        response = client.get(f'{FRAGMENT}?after=0')

        assert response.status_code == 200
        assert teaser_ids(response.get_data(as_text=True)) == [mine.id]

    def test_the_fragment_is_204_when_only_other_servers_posted(self, client, live):
        other_toot(live)
        login(client, live.viewer)

        assert client.get(f'{FRAGMENT}?after=0').status_code == 204

    def test_no_view_has_no_fragment(self, client, live):
        login(client, live.viewer)

        assert client.get('/community/microblogs@nowhere.example/live/posts?after=0').status_code == 404

    def test_the_live_page_wires_the_view(self, app, client, live, monkeypatch):
        monkeypatch.setitem(app.config, 'NOTIF_SERVER', 'https://notifs.example')
        live.toot()
        login(client, live.viewer)

        html = client.get(f'{VIEW}?sort=live').get_data(as_text=True)

        assert 'id="live_feed"' in html
        assert re.search(r'data-posts-url="/community/microblogs(@|%40)mastodon.example/live/posts"', html)
        assert f'data-sse-url="https://notifs.example/live/stream?feed=instance:{live.remote.id}"' in html


class TestPostLinks:

    def link(self, app, post):
        from app.community.live import post_community_link

        with app.test_request_context():
            return post_community_link(post)

    def test_a_remote_microblog_links_to_its_server_view(self, app, live):
        assert self.link(app, live.toot()) == 'microblogs@mastodon.example'

    def test_a_local_authors_microblog_links_to_the_community(self, app, live):
        post = make_post(live.microblogs, live.viewer, 'https://test.piefed.local/post/2', microblog=True)

        assert self.link(app, post) == 'microblogs'

    def test_a_server_with_a_real_community_links_to_the_community(self, app, live):
        real_microblogs(live)

        assert self.link(app, live.toot()) == 'microblogs'

    def test_a_post_in_another_community_keeps_its_link(self, app, live):
        general = make_community('general')
        post = make_post(general, live.author, 'https://mastodon.example/statuses/h')

        assert self.link(app, post) == 'general'

    def test_the_lookup_is_memoized_per_server(self):
        # Tests run with NullCache, so the cache itself is not exercised here: pin the decorator.
        from app.community.live import server_view_actor

        assert server_view_actor.cache_timeout == 300

    def test_the_post_page_breadcrumb_names_the_view(self, client, live):
        post = live.toot()

        html = client.get(f'/post/{post.id}').get_data(as_text=True)

        assert re.search(r'<a href="/c/microblogs@mastodon.example">microblogs@mastodon.example</a>', html)

    def test_a_teaser_byline_links_to_the_view(self, app, live):
        # The byline is the non-microblog layout; a titled post by a remote author reaches it.
        post = live.toot(microblog=False)

        with app.test_request_context():
            rendered = render_template_string(
                "{% from 'post/post_teaser/_macros.html' import render_title %}"
                "{{ render_title(post, show_post_community=True, request=request, user_pronouns={}, user_flair={}, reported_posts=[]) }}", post=post)

        assert 'href="/c/microblogs@mastodon.example"' in rendered
        assert '>@mastodon.example</span>' in rendered
