"""The Live view of /c/microblogs: new posts arrive without a reload.

Spec: docs/superpowers/specs/2026-10-07-microblog-live-feed-design.md

Fixture notes, from tests/test_community_show.py: `Site.private_instance`
defaults to True, so every test here makes the instance public; and a local
community is reached at /c/<name>.
"""
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app import db
from app.constants import POST_STATUS_REVIEWING
from app.models import Instance, Language, Post, Site
from app.utils import utcnow
from tests.factories import (make_community, make_community_member, make_instance, make_instance_block,
                             make_post, make_user, make_user_block)

pytestmark = pytest.mark.usefixtures('site')


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def local_instance():
    existing = Instance.query.filter_by(domain='test.piefed.local').first()
    return existing if existing is not None else make_instance('test.piefed.local', software='piefed')


@pytest.fixture
def live(app, db_session):
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    local = local_instance()
    make_user(local, 'founder', local=True)
    viewer = make_user(local, 'viewer', local=True)
    remote = make_instance('mastodon.example')
    author = make_user(remote, 'tooter')
    microblogs = make_community('microblogs')
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.commit()
    counter = iter(range(1, 10_000))

    def toot(**columns):
        post = make_post(microblogs, author, f'https://mastodon.example/statuses/{next(counter)}', microblog=True)
        for name, value in columns.items():
            setattr(post, name, value)
        db.session.commit()
        return post

    return SimpleNamespace(viewer=viewer, author=author, remote=remote, microblogs=microblogs, toot=toot)


@pytest.fixture
def client(app):
    return app.test_client()


def teaser_ids(html):
    return [int(found) for found in re.findall(r'id="post_(\d+)"', html)]


class TestCommunityPostQuery:
    """The filter chain show_community applied inline, now shared with the Live fragment."""

    def test_a_logged_in_viewer_does_not_see_a_blocked_authors_post(self, app, live):
        from flask_login import login_user

        from app.community.routes import community_post_query

        kept = live.toot()
        other = make_user(live.remote, 'pest')
        blocked = make_post(live.microblogs, other, 'https://mastodon.example/statuses/pest', microblog=True)
        make_user_block(live.viewer, other)
        with app.test_request_context():
            login_user(live.viewer)
            posts, content_filters = community_post_query(live.microblogs, 'posts')
            ids = {post.id for post in posts}

        assert kept.id in ids and blocked.id not in ids
        assert isinstance(content_filters, dict)

    def test_an_anonymous_viewer_gets_no_content_filters(self, app, live):
        from app.community.routes import community_post_query

        live.toot()
        with app.test_request_context():
            posts, content_filters = community_post_query(live.microblogs, 'posts')
            assert posts.count() == 1

        assert content_filters == {}


class TestLiveRules:

    def test_the_local_microblogs_community_is_live(self, app, live):
        from app.community.live import is_live_community

        with app.test_request_context():
            assert is_live_community(live.microblogs)

    def test_another_local_community_is_not(self, app, live):
        from app.community.live import is_live_community

        with app.test_request_context():
            assert not is_live_community(make_community('general'))

    def test_a_remote_community_named_microblogs_is_not(self, app, live):
        from app.community.live import is_live_community

        remote = make_community('microblogs', host='other.example')
        remote.ap_id = 'microblogs@other.example'
        remote.instance_id = live.remote.id
        db.session.commit()
        with app.test_request_context():
            assert not is_live_community(remote)

    @pytest.mark.parametrize('logged_in, content_type, page, expected', [
        (True, 'posts', 1, True),
        (False, 'posts', 1, False),
        (True, 'comments', 1, False),
        (True, 'posts', 2, False),
    ])
    def test_live_is_available_only_to_a_logged_in_viewer_of_page_one_of_posts(
            self, app, live, logged_in, content_type, page, expected):
        from flask_login import AnonymousUserMixin

        from app.community.live import live_available

        user = live.viewer if logged_in else AnonymousUserMixin()
        with app.test_request_context():
            assert live_available(live.microblogs, user, content_type, page) is expected

    def test_live_posts_are_newer_than_the_cursor_recent_unpinned_and_newest_first(self, app, live):
        from app.community.live import live_posts

        seen = live.toot()
        older = live.toot(posted_at=utcnow() - timedelta(minutes=10))
        newer = live.toot()
        live.toot(posted_at=utcnow() - timedelta(hours=2))      # a backfill: new id, old post
        live.toot(sticky=True)

        result = live_posts(Post.query.filter(Post.community_id == live.microblogs.id), seen.id)

        assert [post.id for post in result] == [newer.id, older.id]

    def test_live_posts_stop_at_the_limit(self, app, live):
        from app.community import live as live_module

        for _ in range(3):
            live.toot()
        query = Post.query.filter(Post.community_id == live.microblogs.id)
        original = live_module.LIVE_LIMIT
        live_module.LIVE_LIMIT = 2
        try:
            assert len(live_module.live_posts(query, 0)) == 2
        finally:
            live_module.LIVE_LIMIT = original

    def test_the_limit_is_forty(self):
        from app.community.live import LIVE_LIMIT

        assert LIVE_LIMIT == 40


class TestAnnounceLivePost:
    """The SSE wake-up. The payload is empty on purpose: each client fetches its own filtered posts."""

    @pytest.fixture
    def published(self, app, monkeypatch):
        calls = []
        monkeypatch.setattr('app.utils.publish_sse_event', lambda key, value: calls.append((key, value)))
        monkeypatch.setitem(app.config, 'NOTIF_SERVER', 'https://notifs.example')
        return calls

    def test_a_new_microblogs_post_wakes_the_live_feed(self, app, live, published):
        from app.community.live import announce_live_post

        with app.test_request_context():
            announce_live_post(live.toot(), live.microblogs, backfill=False)

        assert published == [('live:microblogs', '{}')]

    def test_a_backfilled_post_does_not(self, app, live, published):
        from app.community.live import announce_live_post

        with app.test_request_context():
            announce_live_post(live.toot(), live.microblogs, backfill=True)

        assert published == []

    def test_a_post_in_another_community_does_not(self, app, live, published):
        from app.community.live import announce_live_post

        general = make_community('general')
        post = make_post(general, live.author, 'https://mastodon.example/statuses/g')
        with app.test_request_context():
            announce_live_post(post, general, backfill=False)

        assert published == []

    @pytest.mark.parametrize('columns', [
        {'status': POST_STATUS_REVIEWING},
        {'deleted': True},
        {'visibility': 'unlisted'},
    ])
    def test_a_post_the_feed_would_not_list_does_not(self, app, live, published, columns):
        from app.community.live import announce_live_post

        with app.test_request_context():
            announce_live_post(live.toot(**columns), live.microblogs, backfill=False)

        assert published == []

    def test_without_a_notification_server_nothing_is_published(self, app, live, published, monkeypatch):
        from app.community.live import announce_live_post

        monkeypatch.setitem(app.config, 'NOTIF_SERVER', '')
        with app.test_request_context():
            announce_live_post(live.toot(), live.microblogs, backfill=False)

        assert published == []

    def test_a_redis_failure_is_logged_not_raised(self, app, live, monkeypatch, caplog):
        from app.community.live import announce_live_post

        def broken(key, value):
            raise ConnectionError('redis is down')
        monkeypatch.setattr('app.utils.publish_sse_event', broken)
        monkeypatch.setitem(app.config, 'NOTIF_SERVER', 'https://notifs.example')
        with app.test_request_context():
            announce_live_post(live.toot(), live.microblogs, backfill=False)

        assert 'redis is down' in caplog.text

    def test_the_signal_reaches_the_redis_channel(self, app, live, redis_double, monkeypatch):
        from app.community.live import announce_live_post

        monkeypatch.setitem(app.config, 'NOTIF_SERVER', 'https://notifs.example')
        subscriber = redis_double.pubsub()
        subscriber.subscribe('live:microblogs')
        subscriber.get_message(timeout=1)       # the subscribe confirmation
        with app.test_request_context():
            announce_live_post(live.toot(), live.microblogs, backfill=False)

        message = subscriber.get_message(timeout=1)
        assert message['channel'] == 'live:microblogs' and message['data'] == '{}'


FRAGMENT = '/community/microblogs/live/posts'


class TestLiveFragment:

    def test_new_posts_after_the_cursor_come_back_newest_first_with_the_new_cursor(self, client, live):
        seen = live.toot()
        older = live.toot(posted_at=utcnow() - timedelta(minutes=5))
        newer = live.toot()
        login(client, live.viewer)

        response = client.get(f'{FRAGMENT}?after={seen.id}')

        assert response.status_code == 200
        assert teaser_ids(response.get_data(as_text=True)) == [newer.id, older.id]
        assert response.headers['X-Live-Cursor'] == str(max(newer.id, older.id))

    def test_nothing_new_is_204(self, client, live):
        latest = live.toot()
        login(client, live.viewer)

        response = client.get(f'{FRAGMENT}?after={latest.id}')

        assert response.status_code == 204 and response.get_data() == b''

    def test_a_backfilled_post_is_not_new(self, client, live):
        live.toot(posted_at=utcnow() - timedelta(hours=2))
        login(client, live.viewer)

        assert client.get(f'{FRAGMENT}?after=0').status_code == 204

    def test_at_most_forty_posts(self, client, live):
        for _ in range(41):
            live.toot()
        login(client, live.viewer)

        assert len(teaser_ids(client.get(f'{FRAGMENT}?after=0').get_data(as_text=True))) == 40

    def test_the_viewers_blocks_and_settings_apply(self, client, live):
        kept = live.toot()
        pest = make_user(live.remote, 'pest')
        make_post(live.microblogs, pest, 'https://mastodon.example/statuses/pest', microblog=True)
        make_user_block(live.viewer, pest)
        live.toot(nsfw=True)
        live.toot(status=POST_STATUS_REVIEWING)
        elsewhere = make_instance('blocked.example')
        stranger = make_user(elsewhere, 'stranger')
        make_post(live.microblogs, stranger, 'https://blocked.example/statuses/1', microblog=True)
        make_instance_block(live.viewer, elsewhere)
        live.viewer.hide_nsfw = 1
        db.session.commit()
        login(client, live.viewer)

        assert teaser_ids(client.get(f'{FRAGMENT}?after=0').get_data(as_text=True)) == [kept.id]

    def test_the_cursor_advances_even_when_a_keyword_filter_hides_every_teaser(self, client, live, monkeypatch):
        hidden = live.toot()
        monkeypatch.setattr(Post, 'blocked_by_content_filter', lambda self, filters, user_id: '-1')
        login(client, live.viewer)

        response = client.get(f'{FRAGMENT}?after=0')

        assert response.status_code == 200
        assert teaser_ids(response.get_data(as_text=True)) == []
        assert response.headers['X-Live-Cursor'] == str(hidden.id)

    def test_an_anonymous_visitor_is_sent_to_log_in(self, client, live):
        response = client.get(f'{FRAGMENT}?after=0')

        assert response.status_code == 302 and '/auth/login' in response.headers['Location']

    def test_another_community_has_no_live_fragment(self, client, live):
        make_community('general')
        login(client, live.viewer)

        assert client.get('/community/general/live/posts?after=0').status_code == 404

    def test_a_private_community_refuses_a_viewer_who_is_not_a_member(self, client, live):
        live.toot()
        live.microblogs.private = True
        db.session.commit()
        login(client, live.viewer)

        assert client.get(f'{FRAGMENT}?after=0').status_code == 403

    def test_a_private_community_still_serves_its_members(self, client, live):
        live.toot()
        live.microblogs.private = True
        make_community_member(live.viewer, live.microblogs)
        login(client, live.viewer)

        assert client.get(f'{FRAGMENT}?after=0').status_code == 200

    def test_an_unknown_community_has_none_either(self, client, live):
        login(client, live.viewer)

        assert client.get('/community/nosuch/live/posts?after=0').status_code == 404

    @pytest.mark.parametrize('query', ['', '?after=', '?after=abc'])
    def test_a_missing_or_unreadable_cursor_is_400(self, client, live, query):
        login(client, live.viewer)

        assert client.get(f'{FRAGMENT}{query}').status_code == 400

    def test_the_thirteenth_request_in_a_minute_is_refused(self, live):
        from app import create_app, limiter
        from tests.conftest import TestConfig

        # The suite's app was built with the limiter off, and Flask-Limiter registers its
        # request hook only while building an app with it on, so this builds one.
        class LimitedConfig(TestConfig):
            RATELIMIT_ENABLED = True

        limited = create_app(LimitedConfig)
        try:
            with limited.app_context():
                client = limited.test_client()
                login(client, live.viewer)
                limiter.reset()
                codes = [client.get(f'{FRAGMENT}?after=0').status_code for _ in range(13)]
        finally:
            limiter.reset()
            limiter.enabled = False

        assert codes[:12] == [204] * 12 and codes[12] == 429


class TestLivePage:

    def test_the_live_button_shows_for_a_logged_in_viewer_of_microblogs(self, client, live):
        login(client, live.viewer)

        assert '?sort=live' in client.get('/c/microblogs').get_data(as_text=True)

    def test_no_live_button_for_an_anonymous_visitor(self, client, live):
        assert '?sort=live' not in client.get('/c/microblogs').get_data(as_text=True)

    def test_no_live_button_on_another_community(self, client, live):
        make_community('general')
        login(client, live.viewer)

        assert '?sort=live' not in client.get('/c/general').get_data(as_text=True)

    def test_the_live_page_carries_the_client_contract(self, client, live):
        posts = [live.toot(), live.toot()]
        login(client, live.viewer)

        html = client.get('/c/microblogs?sort=live').get_data(as_text=True)

        assert 'id="live_feed"' in html
        assert f'data-cursor="{max(post.id for post in posts)}"' in html
        assert 'data-posts-url="/community/microblogs/live/posts"' in html
        assert 'data-sse-url=""' in html
        assert 'id="live_status"' in html and 'id="live_pill"' in html
        assert 'position-fixed' in re.search(r'<button[^>]*id="live_pill"[^>]*>', html).group(0)
        assert 'position-sticky top-0' not in html
        assert 'js/live_feed.js' in html
        assert set(teaser_ids(html)) == {post.id for post in posts}

    @pytest.mark.parametrize('query', ['flair=x', 'tag=x'])
    def test_a_filtered_view_is_not_live(self, client, live, query):
        login(client, live.viewer)

        html = client.get(f'/c/microblogs?sort=live&{query}').get_data(as_text=True)

        assert 'id="live_feed"' not in html

    def test_the_pill_string_reads_right_for_any_count(self, client, live):
        login(client, live.viewer)

        html = client.get('/c/microblogs?sort=live').get_data(as_text=True)

        assert 'data-str-new-posts="New posts: %d"' in html

    def test_an_empty_community_starts_the_cursor_at_zero(self, client, live):
        login(client, live.viewer)

        assert 'data-cursor="0"' in client.get('/c/microblogs?sort=live').get_data(as_text=True)

    def test_with_a_notification_server_the_page_names_the_live_stream(self, app, client, live, monkeypatch):
        monkeypatch.setitem(app.config, 'NOTIF_SERVER', 'https://notifs.example')
        login(client, live.viewer)

        html = client.get('/c/microblogs?sort=live').get_data(as_text=True)

        assert 'data-sse-url="https://notifs.example/live/stream?feed=microblogs"' in html

    @pytest.mark.parametrize('path, as_viewer', [
        ('/c/microblogs?sort=live', False),
        ('/c/general?sort=live', True),
        ('/c/microblogs?sort=live&content_type=comments', True),
        ('/c/microblogs?sort=live&page=2', True),
    ])
    def test_sort_live_falls_back_to_new_where_live_is_not_available(self, client, live, path, as_viewer):
        make_community('general')
        if as_viewer:
            login(client, live.viewer)

        response = client.get(path)

        assert response.status_code == 200
        assert 'id="live_feed"' not in response.get_data(as_text=True)

    def test_live_leaves_out_sticky_posts(self, client, live):
        pinned = live.toot(sticky=True)
        login(client, live.viewer)

        assert pinned.id not in teaser_ids(client.get('/c/microblogs?sort=live').get_data(as_text=True))

    def test_live_forces_the_list_layout(self, client, live):
        live.microblogs.default_layout = 'masonry'
        db.session.commit()
        live.toot()
        login(client, live.viewer)

        html = client.get('/c/microblogs?sort=live').get_data(as_text=True)

        assert 'id="masonry"' not in html and 'id="live_feed"' in html

    def test_live_works_in_low_bandwidth_mode(self, client, live):
        live.toot()
        login(client, live.viewer)
        client.set_cookie('low_bandwidth', '1')

        assert 'id="live_feed"' in client.get('/c/microblogs?sort=live').get_data(as_text=True)

    def test_older_posts_continue_in_the_new_sort(self, client, live):
        for _ in range(3):
            live.toot()
        live.viewer.page_length = 2
        db.session.commit()
        login(client, live.viewer)

        html = client.get('/c/microblogs?sort=live').get_data(as_text=True)

        assert 'Older posts' in html
        assert re.search(r'href="[^"]*page=2[^"]*sort=new|href="[^"]*sort=new[^"]*page=2', html)


def test_the_live_wake_up_is_registered_as_a_post_stored_hook():
    from app.community.live import announce_live_post
    from app.models import post_stored_hooks

    assert announce_live_post in post_stored_hooks


class TestPostNewAnnounces:
    """Post.new is where an inbound Create becomes a Post; the wake-up goes out after its commit."""

    PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'
    AUTHOR = 'https://remote.test/u/tooter'

    @pytest.fixture
    def env(self, app, api_baseline, monkeypatch):
        from flask import g

        from tests.factories import make_community_member

        g.admin_ids = []
        g.site = db.session.get(Site, 1)
        community = make_community('livenew')
        author = make_user(api_baseline.instance_remote, 'tooter')
        author.ap_id = 'tooter@remote.test'
        author.ap_profile_id = self.AUTHOR
        author.ap_public_url = self.AUTHOR
        db.session.commit()
        make_community_member(author, community)
        calls = []
        monkeypatch.setattr('app.models.post_stored_hooks',
                            [lambda post, community, backfill: calls.append((post.id, community.id, backfill))])
        return SimpleNamespace(community=community, author=author, calls=calls)

    def create(self, env, number, backfill=False):
        document = {'id': f'https://remote.test/p/{number}', 'type': 'Page', 'name': 'a post',
                    'attributedTo': self.AUTHOR, 'to': [self.PUBLIC],
                    'published': '2026-01-01T00:00:00Z', 'content': '<p>body</p>'}
        return Post.new(env.author, env.community,
                        {'id': f'https://remote.test/c/{number}', 'type': 'Create', 'to': [self.PUBLIC],
                         'object': document}, backfill=backfill)

    def test_a_new_post_is_announced_once_after_it_is_stored(self, app, env):
        post = self.create(env, 1)

        assert env.calls == [(post.id, env.community.id, False)]

    def test_a_backfilled_post_is_passed_as_one(self, app, env):
        post = self.create(env, 2, backfill=True)

        assert env.calls == [(post.id, env.community.id, True)]

    def test_hooks_run_after_the_ai_detection_block(self, app, env, monkeypatch):
        order = []
        monkeypatch.setitem(app.config, 'DETECT_AI_ENDPOINT', 'https://ai.example/detect')
        monkeypatch.setattr('app.models.post_stored_hooks', [lambda post, community, backfill: order.append('hook')])
        monkeypatch.setattr(type(env.author), 'created_very_recently', lambda self: True)

        def fake_get(url):
            order.append('ai')
            return None

        monkeypatch.setattr('app.utils.get_request', fake_get)
        document = {'id': 'https://remote.test/p/20', 'type': 'Page', 'name': 'a post',
                    'attributedTo': self.AUTHOR, 'to': [self.PUBLIC], 'published': '2026-01-01T00:00:00Z',
                    'content': '<p>' + 'long ' * 100 + '</p>'}
        Post.new(env.author, env.community, {'id': 'https://remote.test/c/20', 'type': 'Create',
                                             'to': [self.PUBLIC], 'object': document})

        assert order == ['ai', 'hook']

    def test_a_failing_hook_does_not_fail_the_ingest_or_stop_the_next_hook(self, app, env, monkeypatch, caplog):
        def broken(post, community, backfill):
            raise RuntimeError('boom')

        monkeypatch.setattr('app.models.post_stored_hooks', [broken, lambda *args: env.calls.append(args)])

        post = self.create(env, 3)

        assert post is not None and len(env.calls) == 1
        assert sum('boom' in record.getMessage() or record.exc_info is not None
                   for record in caplog.records if record.levelname == 'ERROR') == 1
