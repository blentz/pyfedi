import json

import pytest
from sqlalchemy import text

from app.utils import (block_bots, compaction_level, debug_mode_only,
                       display_back_button, referrer, requestor_domain,
                       show_ban_message, user_cookie_banned)
import app.utils as app_utils
from app import db
from app.models import User
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

    @pytest.mark.parametrize('ua', ['x (+https://[x)', 'x (+https://a.example:port/)', 'x (+https://%zz/)'])
    def test_a_claimed_url_furl_refuses_names_no_domain(self, app, ua):
        """The User-Agent is the visitor's to write, and furl raises ValueError on an unterminated IPv6 bracket, a
        port that is not a number or a host it will not accept: /.well-known/webfinger and every signed-fetch
        route answered 500. A claim with no readable host names no domain, as one with no host already does."""
        with app.test_request_context(headers={'User-Agent': ua}):
            assert requestor_domain() is None


class TestReferrer:
    """referrer() reads three user-controlled sources -- ?next=, a posted
    `referrer` form field, and the Referer header -- and ~29 call sites hand the
    result straight to redirect(). Every source goes through
    is_safe_redirect_target; a source that fails falls through to the next one
    and ultimately to the default."""

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
        """Fails if the origin check is dropped -- an open-redirect guard."""
        with app.test_request_context('/', headers={'Referer': 'https://evil.example/x'}):
            assert referrer(default='/fallback') == '/fallback'

    # ---- the open-redirect / phishing vector -------------------------------
    # ?next= and the posted `referrer` field used to be returned VERBATIM, with
    # no check at all, and the Referer header was guarded only by the substring
    # test `SERVER_NAME in referrer`. These four are the regression guards.

    def test_a_cross_origin_next_parameter_is_rejected(self, app):
        """THE phishing vector. `/?next=https://evil.example` used to be returned
        unchecked and redirected to."""
        with app.test_request_context('/?next=https://evil.example'):
            assert referrer(default='/fallback') == '/fallback'

    def test_a_same_origin_next_parameter_is_honoured(self, app):
        with app.test_request_context('/?next=https://test.piefed.local/x'):
            assert referrer(default='/fallback') == 'https://test.piefed.local/x'

    def test_a_cross_origin_posted_referrer_field_is_rejected(self, app):
        with app.test_request_context('/', method='POST',
                                      data={'referrer': 'https://evil.example/x'}):
            assert referrer(default='/fallback') == '/fallback'

    def test_a_same_origin_posted_referrer_field_is_honoured(self, app):
        with app.test_request_context('/', method='POST',
                                      data={'referrer': 'https://test.piefed.local/x'}):
            assert referrer(default='/fallback') == 'https://test.piefed.local/x'

    def test_a_referer_that_merely_mentions_the_server_name_is_rejected(self, app):
        """`SERVER_NAME in referrer` was a substring check, not an origin check:
        https://evil.example/?x=test.piefed.local contains the SERVER_NAME as a
        query-string value and used to be accepted. It is not this site."""
        with app.test_request_context(
                '/', headers={'Referer': 'https://evil.example/?x=test.piefed.local'}):
            assert referrer(default='/fallback') == '/fallback'

    def test_a_protocol_relative_next_parameter_is_rejected(self, app):
        with app.test_request_context('/?next=//evil.example/x'):
            assert referrer(default='/fallback') == '/fallback'

    # ---- fall-through ------------------------------------------------------

    def test_a_rejected_next_falls_through_to_the_form_field(self, app):
        with app.test_request_context('/?next=https://evil.example',
                                      method='POST', data={'referrer': '/from-form'}):
            assert referrer(default='/fallback') == '/from-form'

    def test_a_rejected_next_and_form_field_fall_through_to_the_header(self, app):
        with app.test_request_context('/?next=https://evil.example',
                                      method='POST',
                                      data={'referrer': 'https://also-evil.example/x'},
                                      headers={'Referer': 'https://test.piefed.local/x'}):
            assert referrer(default='/fallback') == 'https://test.piefed.local/x'

    def test_all_three_rejected_falls_through_to_the_default(self, app):
        with app.test_request_context('/?next=https://evil.example',
                                      method='POST',
                                      data={'referrer': '//evil.example/x'},
                                      headers={'Referer': 'https://evil.example/?x=test.piefed.local'}):
            assert referrer(default='/fallback') == '/fallback'

    def test_all_three_rejected_and_no_default_falls_through_to_the_index(self, app):
        with app.test_request_context('/?next=javascript:alert(1)'):
            assert referrer() == '/home'

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
            db.session.commit()

            message = pubsub.get_message(timeout=1)
            assert message is not None, 'no SSE event was published'
            # a change only: the page asks its own storefront for the count it may show (follow-up F6, N1)
            assert json.loads(message['data']) == {'notifs_changed': True}
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
            db.session.commit()   # its event goes out here, before the subscription
            pubsub = redis_double.pubsub()
            pubsub.subscribe(f'notifications:{user.id}')
            pubsub.get_message(timeout=1)

            user.unread_notifications = 5
            db.session.commit()

            assert pubsub.get_message(timeout=1) is None
        finally:
            app.config['NOTIF_SERVER'] = original

    @pytest.fixture
    def published(self, app, monkeypatch):
        """The events published, each with the count another connection reads at that moment: what the page's
        fetch of /notifications/unread_count would find."""
        monkeypatch.setitem(app.config, 'NOTIF_SERVER', 'https://notif.example')
        events = []

        def publish(key, value):
            with db.engine.connect() as connection:
                count = connection.execute(text('SELECT unread_notifications FROM "user" WHERE id::text = :id'),
                                           {'id': key.split(':')[1]}).scalar()
            events.append((key, json.loads(value), count))
        monkeypatch.setattr(app_utils, 'publish_sse_event', publish)
        return events

    def test_the_event_is_published_after_the_commit(self, app, db_session, published):
        """Re-review 4, M4: the event says only that the count changed, and the page then asks the server for it. Sent
        before the commit, that fetch could read the old count, and the badge stayed stale until the next event."""
        user = make_user(make_instance('test.instance.example'), 'notified', local=True)
        user.unread_notifications = 5
        user.unread_notifications = 6   # one event per account and commit
        db.session.flush()
        assert published == []
        db.session.commit()
        assert published == [(f'notifications:{user.id}', {'notifs_changed': True}, 6)]

    def test_a_rolled_back_change_is_not_published(self, app, db_session, published):
        user = make_user(make_instance('test.instance.example'), 'notified', local=True)
        user.unread_notifications = 5
        db.session.rollback()
        db.session.commit()
        assert published == []

    def test_an_account_in_no_session_is_published_at_once(self, app, db_session, published):
        user = User(user_name='loose')
        user.unread_notifications = 5
        assert published == [('notifications:None', {'notifs_changed': True}, None)]

    def test_an_account_taken_out_of_the_session_before_the_commit_is_not_published(self, app, db_session,
                                                                                      published):
        user = User(user_name='pending')
        db.session.add(user)
        user.unread_notifications = 5
        db.session.expunge(user)
        db.session.commit()
        assert published == []

    def test_a_redis_failure_while_publishing_is_logged_and_swallowed(self, app, db_session, monkeypatch, caplog):
        """Re-review 5, m1: the data is committed, so the caller gets no error and a retry cannot write it twice."""
        monkeypatch.setitem(app.config, 'NOTIF_SERVER', 'https://notif.example')

        def broken(key, value):
            raise ConnectionError('redis is down')
        monkeypatch.setattr(app_utils, 'publish_sse_event', broken)
        user = make_user(make_instance('test.instance.example'), 'notified', local=True)
        user.unread_notifications = 5
        with caplog.at_level('WARNING', logger=app_utils.logger.name):
            db.session.commit()
        assert [record.levelname for record in caplog.records if 'redis is down' in record.getMessage()] == ['WARNING']
        assert not db.session().in_transaction()
        assert db.session.execute(text('SELECT unread_notifications FROM "user" WHERE id = :id'),
                                  {'id': user.id}).scalar() == 5

    def test_a_savepoint_released_publishes_nothing_until_the_outer_commit(self, app, db_session, published):
        user = make_user(make_instance('test.instance.example'), 'notified', local=True)
        db.session.commit()
        published.clear()
        user.unread_notifications = 5
        with db.session.begin_nested():
            user.unread_notifications = 6
        assert published == []
        db.session.commit()
        assert published == [(f'notifications:{user.id}', {'notifs_changed': True}, 6)]

    def test_a_savepoint_rolled_back_keeps_the_outer_transactions_events(self, app, db_session, published):
        user = make_user(make_instance('test.instance.example'), 'notified', local=True)
        db.session.commit()
        published.clear()
        user.unread_notifications = 5
        savepoint = db.session.begin_nested()
        savepoint.rollback()
        db.session.commit()
        assert published == [(f'notifications:{user.id}', {'notifs_changed': True}, 5)]
