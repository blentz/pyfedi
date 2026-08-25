import json

import pytest

from app.utils import (block_bots, compaction_level, debug_mode_only,
                       display_back_button, referrer, requestor_domain,
                       show_ban_message, user_cookie_banned)
from tests.factories import make_instance, make_user


# NOTE: ip_address is covered in tests/test_client_ip.py, not here. The tests that
# used to live in this file pinned the pre-fix behaviour -- CF-Connecting-IP, then
# X-Forwarded-For, first entry of the chain -- which is exactly the spoofing defect
# TRUSTED_CLIENT_IP_HEADER fixed. ip_address is now the same object as Flask-Limiter's
# key function, so its tests cover both and belong in one place.


class TestRequestorDomain:
    def test_domain_is_taken_from_a_bot_style_user_agent(self, app):
        ua = 'SomeCrawler/1.0 (+https://crawler.example/info)'
        with app.test_request_context(headers={'User-Agent': ua}):
            assert requestor_domain() == 'crawler.example'

    def test_a_user_agent_without_a_plus_yields_nothing(self, app):
        with app.test_request_context(headers={'User-Agent': 'Mozilla/5.0'}):
            assert requestor_domain() == ''

    def test_an_absent_user_agent_yields_nothing(self, app):
        with app.test_request_context():
            assert requestor_domain() == ''


class TestReferrer:
    def test_the_next_query_parameter_wins(self, app):
        with app.test_request_context('/?next=/somewhere'):
            assert referrer() == '/somewhere'

    def test_a_posted_referrer_field_is_next(self, app):
        with app.test_request_context('/', method='POST', data={'referrer': '/from-form'}):
            assert referrer() == '/from-form'

    def test_an_on_site_referer_header_is_used(self, app):
        with app.test_request_context('/', headers={'Referer': 'https://test.piefed.local/x'}):
            assert referrer() == 'https://test.piefed.local/x'

    def test_an_off_site_referer_header_is_ignored(self, app):
        """Fails if the SERVER_NAME check is dropped -- an open-redirect guard."""
        with app.test_request_context('/', headers={'Referer': 'https://evil.example/x'}):
            assert referrer(default='/fallback') == '/fallback'

    def test_a_referer_that_merely_mentions_the_server_name_is_still_accepted(self, app):
        """Documents a real weakness: `SERVER_NAME in referrer` is a substring
        check, not an origin check. https://evil.example/?x=test.piefed.local
        contains the SERVER_NAME as a query-string value and is accepted here,
        even though it is not this site. Reported, not fixed here."""
        with app.test_request_context(
                '/', headers={'Referer': 'https://evil.example/?x=test.piefed.local'}):
            assert referrer(default='/fallback') == 'https://evil.example/?x=test.piefed.local'

    def test_the_default_is_used_when_nothing_else_matches(self, app):
        with app.test_request_context('/'):
            assert referrer(default='/fallback') == '/fallback'

    def test_the_index_is_the_final_fallback(self, app):
        with app.test_request_context('/'):
            assert referrer() == '/home'


class TestUserCookieBanned:
    def test_the_ban_cookie_is_detected(self, app):
        with app.test_request_context('/', headers={'Cookie': 'sesion=17489047567495'}):
            assert user_cookie_banned() is True

    def test_no_cookie(self, app):
        with app.test_request_context('/'):
            assert user_cookie_banned() is False


class TestCompactionLevel:
    def test_the_cookie_value_is_returned(self, app):
        with app.test_request_context('/', headers={'Cookie': 'compact_level=compact-max'}):
            assert compaction_level() == 'compact-max'

    def test_absent_cookie_returns_none_under_https(self, app):
        with app.test_request_context('/'):
            assert compaction_level() is None

    def test_absent_cookie_returns_the_compact_default_under_mixed_protocol(self, app):
        original = app.config['HTTP_PROTOCOL']
        app.config['HTTP_PROTOCOL'] = 'mixed'
        try:
            with app.test_request_context('/'):
                assert compaction_level() == 'compact-min compact-max'
        finally:
            app.config['HTTP_PROTOCOL'] = original


class TestDisplayBackButton:
    def test_ios_with_an_on_site_referrer_shows_the_button(self, app):
        ua = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)'
        with app.test_request_context('/', headers={
                'User-Agent': ua, 'Referer': app.config['SERVER_URL'] + '/x'}):
            assert display_back_button() == 'display_back_button'

    def test_ios_with_an_off_site_referrer_does_not(self, app):
        ua = 'Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X)'
        with app.test_request_context('/', headers={
                'User-Agent': ua, 'Referer': 'https://elsewhere.example/x'}):
            assert display_back_button() == ''

    def test_a_desktop_browser_never_shows_it(self, app):
        with app.test_request_context('/', headers={'User-Agent': 'Mozilla/5.0 (X11)'}):
            assert display_back_button() == ''


class TestBlockBots:
    def test_a_human_reaches_the_view(self, app):
        decorated = block_bots(lambda: 'view ran')
        with app.test_request_context('/', headers={'User-Agent': 'Mozilla/5.0'}):
            assert decorated() == 'view ran'

    def test_a_bot_is_refused(self, app):
        decorated = block_bots(lambda: 'view ran')
        with app.test_request_context('/', headers={'User-Agent': 'Googlebot/2.1'}):
            with pytest.raises(Exception) as excinfo:
                decorated()
            assert '403' in str(excinfo.value)


class TestDebugModeOnly:
    def test_the_view_runs_in_debug_mode(self, app):
        decorated = debug_mode_only(lambda: 'view ran')
        original = app.debug
        app.debug = True
        try:
            with app.test_request_context('/'):
                assert decorated() == 'view ran'
        finally:
            app.debug = original

    def test_the_view_is_refused_in_production_mode(self, app):
        decorated = debug_mode_only(lambda: 'view ran')
        original = app.debug
        app.debug = False
        try:
            with app.test_request_context('/'):
                with pytest.raises(Exception) as excinfo:
                    decorated()
                assert '403' in str(excinfo.value)
        finally:
            app.debug = original


class TestShowBanMessage:
    def test_redirects_to_the_index_and_sets_the_ban_cookie(self, app, db_session):
        """Fails if the cookie stops being set -- it is how the ban survives logout."""
        with app.test_request_context('/'):
            response = show_ban_message()

        assert response.status_code == 302
        assert 'sesion=' in response.headers.get('Set-Cookie', '')


class TestUnreadNotificationsListener:
    def test_setting_the_count_publishes_an_sse_event(self, app, db_session, redis_double):
        """Fails if the event listener is unregistered, or stops publishing.

        NOTIF_SERVER must be truthy or the listener returns early by design.
        """
        original = app.config['NOTIF_SERVER']
        app.config['NOTIF_SERVER'] = 'https://notif.example'
        try:
            instance = make_instance('test.instance.example')
            user = make_user(instance, 'notified', local=True)
            pubsub = redis_double.pubsub()
            pubsub.subscribe(f'notifications:{user.id}')
            pubsub.get_message(timeout=1)          # the subscribe confirmation

            user.unread_notifications = 5

            message = pubsub.get_message(timeout=1)
            assert message is not None, 'no SSE event was published'
            assert json.loads(message['data']) == {'num_notifs': 5}
        finally:
            app.config['NOTIF_SERVER'] = original

    def test_setting_the_same_value_publishes_nothing(self, app, db_session, redis_double):
        """The `value != oldvalue` guard: re-saving an unchanged count must not
        wake every connected client."""
        original = app.config['NOTIF_SERVER']
        app.config['NOTIF_SERVER'] = 'https://notif.example'
        try:
            instance = make_instance('test.instance.example')
            user = make_user(instance, 'unchanged', local=True)
            user.unread_notifications = 5
            pubsub = redis_double.pubsub()
            pubsub.subscribe(f'notifications:{user.id}')
            pubsub.get_message(timeout=1)

            user.unread_notifications = 5

            assert pubsub.get_message(timeout=1) is None
        finally:
            app.config['NOTIF_SERVER'] = original
