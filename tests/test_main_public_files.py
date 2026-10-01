"""The small pages every instance serves, and the diagnostics behind debug.

Sub-project 99 -- the rest of `app/main/routes.py`: the files a crawler asks
for (`robots.txt`, `security.txt`, `sitemap.xml`, `rsl.xml`), the two a
browser asks for (`service_worker.js`, `manifest.json`), the pages an
instance can override with a CMS page, the feed and topic listings, the
protocol handler behind `web+ap://`, and the nine diagnostics that answer
only when the instance is running in debug mode.

The diagnostics matter to cover for one reason: each is one decorator away
from being a production endpoint that sends mail, writes to S3, or binds to
the directory, and `debug_mode_only` is the only thing in front of them.
"""
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from flask import current_app, g

from app import db
from app.models import CmsPage, Post, Site
from tests.factories import (grant_permission, make_community,
                             make_community_member, make_post, make_user)


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    g.site.private_instance = False
    community = make_community('probeland')
    member = api_baseline.user2
    db.session.commit()
    make_community_member(member, community)
    return SimpleNamespace(app=app, client=app.test_client(),
                           community=community, member=member,
                           baseline=api_baseline)


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


# --------------------------------------------------------------------------
# the files a crawler asks for
# --------------------------------------------------------------------------

class TestWhatACrawlerIsGiven:
    def test_robots_txt_is_plain_text(self, env):
        response = env.client.get('/robots.txt')
        assert response.status_code == 200
        assert response.mimetype == 'text/plain'

    def test_it_points_at_the_licence_when_ai_crawlers_are_refused(
            self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'ALLOW_AI_CRAWLERS', False)
        assert b'rsl.xml' in env.client.get('/robots.txt').data

    def test_and_does_not_when_they_are_welcome(self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'ALLOW_AI_CRAWLERS', True)
        assert b'rsl.xml' not in env.client.get('/robots.txt').data

    def test_the_licence_itself_is_served_when_they_are_refused(
            self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'ALLOW_AI_CRAWLERS', False)
        response = env.client.get('/rsl.xml')
        assert response.status_code == 200
        assert response.mimetype == 'text/xml'

    def test_and_is_not_there_at_all_when_they_are_welcome(self, env,
                                                           monkeypatch):
        monkeypatch.setitem(current_app.config, 'ALLOW_AI_CRAWLERS', True)
        assert env.client.get('/rsl.xml').status_code == 404

    def test_security_txt(self, env):
        response = env.client.get('/.well-known/security.txt')
        assert response.status_code == 200
        assert response.mimetype == 'text/plain'

    def a_post(self, env, title, **columns):
        """The sitemap is `@cache.cached`, so each of these clears it first
        -- otherwise the second test in the class reads the first one's
        answer and asserts nothing."""
        from app import cache
        cache.clear()
        post = make_post(env.community, env.member,
                         ap_id=f'https://test.piefed.local/p/{title}',
                         title=title)
        post.instance_id = 1
        post.slug = None
        for key, value in columns.items():
            setattr(post, key, value)
        db.session.commit()
        return post

    def test_the_sitemap_lists_this_instance_s_own_posts(self, env):
        post = self.a_post(env, 'listed', indexable=True)
        response = env.client.get('/sitemap.xml')
        assert response.status_code == 200
        assert response.mimetype == 'text/xml'
        assert f'/post/{post.id}<'.encode() in response.data

    def test_a_post_nobody_may_index_is_not_in_it(self, env):
        post = self.a_post(env, 'unindexable', indexable=False)
        assert f'/post/{post.id}<'.encode() not in \
            env.client.get('/sitemap.xml').data

    def test_a_post_from_another_instance_is_not_in_it(self, env):
        post = self.a_post(env, 'remote', indexable=True,
                           instance_id=env.baseline.instance_remote.id)
        assert f'/post/{post.id}<'.encode() not in \
            env.client.get('/sitemap.xml').data

    def test_a_deleted_post_is_not_in_it(self, env):
        post = self.a_post(env, 'deleted', indexable=True, deleted=True)
        assert f'/post/{post.id}<'.encode() not in \
            env.client.get('/sitemap.xml').data

    def test_an_unpublished_post_is_not_in_it(self, env):
        post = self.a_post(env, 'draft', indexable=True, status=-1)
        assert f'/post/{post.id}<'.encode() not in \
            env.client.get('/sitemap.xml').data

    @pytest.mark.parametrize('column', ['private', 'local_only'])
    def test_a_post_in_a_private_or_local_only_community_is_not_in_it(self, env, column):
        """R222, fixed. The sitemap filtered on the post alone, so a post in
        a members-only or local-only community was listed for any crawler
        (owner ruling 2026-09-30)."""
        setattr(env.community, column, True)
        post = self.a_post(env, column, indexable=True)
        assert f'/post/{post.id}<'.encode() not in \
            env.client.get('/sitemap.xml').data


# --------------------------------------------------------------------------
# the files a browser asks for
# --------------------------------------------------------------------------

class TestWhatABrowserIsGiven:
    def test_the_service_worker(self, env):
        response = env.client.get('/service_worker.js')
        assert response.status_code == 200
        assert 'max-age=86400' in response.headers['Cache-Control']

    def test_the_manifest_names_this_instance(self, env):
        g.site.name = 'Probeland'
        g.site.description = 'a place for things'
        db.session.commit()
        response = env.client.get('/manifest.json')
        assert response.status_code == 200
        manifest = response.get_json()
        assert manifest['name'] == 'Probeland'
        assert manifest['description'] == 'a place for things'
        assert manifest['id'] == current_app.config['SERVER_URL']

    def test_an_instance_that_has_named_itself_nothing(self, env):
        g.site.name = None
        g.site.description = None
        db.session.commit()
        manifest = env.client.get('/manifest.json').get_json()
        assert manifest['name'] == 'PieFed'
        assert manifest['description'] == ''

    def test_it_is_not_cached_by_anything_shared(self, env):
        """The user agent decides which one is served, so a shared cache
        would hand an iPhone the Android one."""
        response = env.client.get('/manifest.json')
        assert response.headers['Cache-Control'].startswith('private')

    @pytest.mark.parametrize('agent', [
        'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)',
        'Mozilla/5.0 (Linux; Android 14)',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'curl/8.0',
        '',
    ])
    def test_every_platform_gets_one(self, env, agent):
        response = env.client.get('/manifest.json',
                                  headers={'User-Agent': agent})
        assert response.status_code == 200
        assert response.get_json()['id']

    def test_a_user_agent_that_cannot_be_parsed_at_all(self, env):
        with patch('app.main.routes.uaparse',
                   side_effect=Exception('not a user agent')):
            response = env.client.get('/manifest.json')
        assert response.status_code == 200

    def test_the_icons_the_instance_has_set(self, env):
        from app.utils import set_setting
        set_setting('logo_192', 'https://cdn.test/small.png')
        set_setting('logo_512', 'https://cdn.test/large.png')
        icons = env.client.get('/manifest.json').get_json()['icons']
        sources = {icon['sizes']: icon['src'] for icon in icons}
        assert sources['192x192'] == 'https://cdn.test/small.png'
        assert sources['512x512'] == 'https://cdn.test/large.png'

    def test_the_ones_it_has_not(self, env):
        icons = env.client.get('/manifest.json').get_json()['icons']
        sources = {icon['sizes']: icon['src'] for icon in icons}
        assert sources['192x192'].endswith('piefed_logo_icon_t_192.png')
        assert sources['512x512'].endswith('piefed_logo_icon_t_512.png')

    def test_the_same_file_under_its_static_path(self, env):
        assert env.client.get('/static/manifest.json').status_code == 200


# --------------------------------------------------------------------------
# pages an instance can write for itself
# --------------------------------------------------------------------------

class TestThePagesAnInstanceWrites:
    def test_the_privacy_page_as_it_ships(self, env):
        assert env.client.get('/privacy').status_code == 200

    def test_one_the_instance_has_replaced(self, env):
        db.session.add(CmsPage(url='/privacy', title='Our privacy policy',
                               body='we keep nothing',
                               body_html='<p>we keep nothing</p>'))
        db.session.commit()
        response = env.client.get('/privacy')
        assert b'we keep nothing' in response.data

    def test_the_about_page(self, env):
        assert env.client.get('/about').status_code == 200

    def test_the_keyboard_shortcuts(self, env):
        assert env.client.get('/keyboard_shortcuts').status_code == 200

    def test_login_is_only_a_signpost(self, env):
        response = env.client.get('/login')
        assert response.status_code == 302
        assert '/auth/login' in response.headers['Location']


# --------------------------------------------------------------------------
# the listings
# --------------------------------------------------------------------------

class TestTheListings:
    def test_the_feeds_page(self, env):
        assert env.client.get('/feeds').status_code == 200

    def test_it_can_be_searched(self, env):
        assert env.client.get('/feeds?search=anything').status_code == 200

    def test_a_federating_caller_is_given_json(self, env):
        response = env.client.get('/feeds', headers={
            'Accept': 'application/activity+json'})
        assert response.status_code == 200
        assert response.content_type.startswith('application/activity+json')
        assert 'public_feeds' in response.get_json()['site_view']

    def test_a_public_feed_is_listed_for_them(self, env):
        from tests.factories import make_local_feed
        feed = make_local_feed('news', public=True)
        feed.user_id = env.member.id
        feed.machine_name = 'news'
        db.session.commit()
        response = env.client.get('/feeds', headers={
            'Accept': 'application/activity+json'})
        listed = response.get_json()['site_view']['public_feeds']
        assert any(url.endswith('/f/news') for url in listed)

    def test_a_feed_on_another_instance_is_not_listed_as_ours(self, env):
        """The list is of feeds THIS instance hosts; a remote one belongs on
        its own server's list, under its own name."""
        from tests.factories import make_feed
        remote = make_feed(env.baseline.instance_remote, 'faraway',
                           public=True)
        remote.machine_name = 'faraway'
        db.session.commit()
        response = env.client.get('/feeds', headers={
            'Accept': 'application/activity+json'})
        listed = response.get_json()['site_view']['public_feeds']
        assert not any(url.endswith('/f/faraway') for url in listed)

    def test_the_explore_page(self, env):
        assert env.client.get('/explore').status_code == 200

    def test_the_explore_page_signed_in(self, env):
        login(env.client, env.member)
        assert env.client.get('/explore').status_code == 200

    def test_a_private_instance_refuses_both_to_strangers(self, env):
        g.site.private_instance = True
        db.session.commit()
        for path in ('/feeds', '/explore'):
            response = env.client.get(path)
            assert response.status_code == 302
            assert 'login' in response.headers['Location']


# --------------------------------------------------------------------------
# web+ap:// links
# --------------------------------------------------------------------------

class TestTheProtocolHandler:
    def test_it_asks_to_be_signed_in(self, env):
        response = env.client.get('/protocol_handler?to=web+ap://x')
        assert response.status_code == 302
        assert 'login' in response.headers['Location']

    def test_with_nothing_to_look_up_it_shows_the_page(self, env):
        login(env.client, env.member)
        assert env.client.get('/protocol_handler').status_code == 200

    def test_a_post(self, env):
        post = make_post(env.community, env.member,
                         ap_id='https://test.piefed.local/p/1')
        db.session.commit()
        login(env.client, env.member)
        with patch('app.main.routes.get_resolve_object',
                   return_value={'post': {'post': {'id': post.id}}}):
            response = env.client.get('/protocol_handler?to=web+ap://x')
        assert response.status_code == 302

    def test_a_comment(self, env):
        from tests.factories import make_post_reply
        post = make_post(env.community, env.member,
                         ap_id='https://test.piefed.local/p/1')
        db.session.commit()
        reply = make_post_reply(post, env.member)
        db.session.commit()
        login(env.client, env.member)
        with patch('app.main.routes.get_resolve_object',
                   return_value={'comment': {'comment': {'id': reply.id}}}):
            response = env.client.get('/protocol_handler?to=web+ap://x')
        assert response.status_code == 302
        assert f'/comment/{reply.id}' in response.headers['Location']

    def test_a_community(self, env):
        login(env.client, env.member)
        with patch('app.main.routes.get_resolve_object',
                   return_value={'community': {'community': {
                       'id': env.community.id}}}):
            response = env.client.get('/protocol_handler?to=web+ap://x')
        assert response.status_code == 302

    def test_a_person(self, env):
        login(env.client, env.member)
        with patch('app.main.routes.get_resolve_object',
                   return_value={'person': {'person': {
                       'id': env.member.id}}}):
            response = env.client.get('/protocol_handler?to=web+ap://x')
        assert response.status_code == 302

    def test_something_that_resolves_to_nothing_this_handler_knows(self, env):
        """D1289. A resolver answer the handler has no branch for -- a feed,
        say, or an empty one -- fell off the end of the view, and a view that
        returns None is a 500 rather than "could not find that"."""
        login(env.client, env.member)
        with patch('app.main.routes.get_resolve_object', return_value={}):
            response = env.client.get('/protocol_handler?to=web+ap://x')
        assert response.status_code == 302
        assert response.headers['Location'] in ('/', '/home')

    def test_what_could_not_be_found_is_named_in_the_message(self, env):
        """The flash carried the literal `%(url)s`: the placeholder was
        never given a value."""
        login(env.client, env.member)
        with patch('app.main.routes.get_resolve_object',
                   side_effect=Exception('no such thing')):
            response = env.client.get(
                '/protocol_handler?to=web%2Bap%3A//remote.test/c/faraway',
                follow_redirects=True)
        assert b'%(url)s' not in response.data
        assert b'remote.test/c/faraway' in response.data

    def test_something_that_cannot_be_looked_up_at_all(self, env):
        login(env.client, env.member)
        with patch('app.main.routes.get_resolve_object',
                   side_effect=Exception('no such thing')):
            response = env.client.get('/protocol_handler?to=web+ap://x')
        assert response.status_code == 302
        assert response.headers['Location'] in ('/', '/home')

    def test_the_scheme_is_rewritten_before_it_is_resolved(self, env):
        login(env.client, env.member)
        with patch('app.main.routes.get_resolve_object',
                   return_value={}) as resolve:
            env.client.get(
                '/protocol_handler?to=web%2Bap%3A//remote.test/c/faraway')
        assert resolve.call_args.args[1]['q'] == \
            'https://remote.test/c/faraway'


# --------------------------------------------------------------------------
# the diagnostics
# --------------------------------------------------------------------------

DIAGNOSTICS = ['/test', '/test_email', '/test_redis', '/test_ip', '/test_s3',
               '/test_hashing', '/test_ldap', '/test_ldap_login',
               '/test_libretranslate']


class TestTheDiagnosticsAreNotThereInProduction:
    @pytest.mark.parametrize('path', DIAGNOSTICS)
    def test_each_one_is_refused(self, env, path, monkeypatch):
        monkeypatch.setattr(current_app, 'debug', False)
        assert env.client.get(path).status_code == 403

    @pytest.mark.parametrize('path', DIAGNOSTICS)
    def test_and_to_somebody_signed_in_too(self, env, path, monkeypatch):
        monkeypatch.setattr(current_app, 'debug', False)
        login(env.client, env.member)
        assert env.client.get(path).status_code == 403


class TestTheDiagnosticsInDebug:
    @pytest.fixture(autouse=True)
    def in_debug(self, env, monkeypatch):
        monkeypatch.setattr(current_app, 'debug', True)

    def test_the_markdown_one(self, env):
        assert env.client.get('/test').status_code == 200

    def test_the_email_one_sends_to_the_address_it_is_given(self, env):
        with patch('app.main.routes.send_email') as send:
            response = env.client.get('/test_email?email=x@example.test')
        assert response.status_code == 200
        assert send.call_args.kwargs['recipients'] == ['x@example.test']

    def test_and_to_the_caller_when_there_is_one(self, env):
        login(env.client, env.member)
        with patch('app.main.routes.send_email') as send:
            env.client.get('/test_email')
        assert send.call_args.kwargs['recipients'] == [env.member.email]

    def test_the_redis_one(self, env):
        assert b'ok' in env.client.get('/test_redis').data.lower()

    def test_the_redis_one_when_there_is_no_redis(self, env, monkeypatch):
        monkeypatch.setattr('app.redis_client', None)
        assert b'error' in env.client.get('/test_redis').data.lower()

    def test_the_ip_one(self, env):
        response = env.client.get('/test_ip')
        assert response.status_code == 200
        assert b'CF-Connecting-IP is empty' in response.data

    def test_the_ip_one_behind_a_proxy(self, env):
        response = env.client.get('/test_ip',
                                  headers={'CF-Connecting-IP': '10.0.0.9'})
        assert b'10.0.0.9' in response.data

    def test_the_s3_one(self, env, s3_bucket, monkeypatch):
        for key, value in (('S3_REGION', 'us-east-1'),
                           ('S3_ENDPOINT',
                            'https://s3.us-east-1.amazonaws.com'),
                           ('S3_ACCESS_KEY', 'key'),
                           ('S3_ACCESS_SECRET', 'secret'),
                           ('S3_BUCKET', s3_bucket)):
            monkeypatch.setitem(current_app.config, key, value)
        assert env.client.get('/test_s3').data == b'Ok'

    def test_the_hashing_one(self, env):
        with patch('app.main.routes.retrieve_image_hash',
                   return_value='1' * 256):
            assert env.client.get('/test_hashing').data == b'Ok'

    def test_the_hashing_one_when_the_image_cannot_be_read(self, env):
        with patch('app.main.routes.retrieve_image_hash', return_value=None):
            assert env.client.get('/test_hashing').data == b'Error'

    def test_the_directory_one(self, env):
        with patch('app.main.routes.test_ldap_connection', return_value=True), \
                patch('app.main.routes.sync_user_to_ldap', return_value=True):
            response = env.client.get('/test_ldap')
        assert b'successful' in response.data

    def test_the_directory_one_when_it_cannot_connect(self, env):
        with patch('app.main.routes.test_ldap_connection', return_value=False):
            response = env.client.get('/test_ldap')
        assert b'Could not connect' in response.data

    def test_the_directory_one_when_it_raises(self, env):
        with patch('app.main.routes.test_ldap_connection',
                   side_effect=Exception('the directory is down')):
            response = env.client.get('/test_ldap')
        assert b'the directory is down' in response.data

    def test_the_directory_login_one(self, env):
        with patch('app.main.routes.login_with_ldap', return_value=object()):
            response = env.client.get(
                '/test_ldap_login?user_name=someone&password=x')
        assert b'True' in response.data

    def test_the_directory_login_one_when_it_refuses(self, env):
        with patch('app.main.routes.login_with_ldap', return_value=False):
            response = env.client.get(
                '/test_ldap_login?user_name=someone&password=x')
        assert b'False' in response.data

    def test_the_directory_login_one_when_it_raises(self, env):
        with patch('app.main.routes.login_with_ldap',
                   side_effect=Exception('the directory is down')):
            response = env.client.get('/test_ldap_login')
        assert b'the directory is down' in response.data

    def test_the_translation_one(self, env):
        translator = MagicMock()
        translator.translate.return_value = 'it worked'
        with patch('app.main.routes.LibreTranslateAPI',
                   return_value=translator):
            assert env.client.get('/test_libretranslate').data == b'it worked'


class TestReplayingAnInboxRequest:
    def test_it_asks_to_be_signed_in(self, env):
        response = env.client.get('/replay_inbox')
        assert response.status_code == 302
        assert 'login' in response.headers['Location']

    def test_it_replays_what_it_is_given(self, env):
        login(env.client, env.member)
        with patch('app.activitypub.routes.replay_inbox_request') as replay:
            response = env.client.get('/replay_inbox')
        assert response.data == b'ok'
        replay.assert_called_once_with({})
