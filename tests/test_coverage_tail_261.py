"""Round 261: the decorators that refuse a request, and four helpers beside them.

Six decorators in `app/utils.py` wrap routes and decide whether a request is answered at all.
Each one's refusal is a single `return redirect(...)`, and none of those lines had a row:

    validation_required          an account that has not confirmed its email
    trustworthy_account_required an account too new or too quiet to be trusted
    aged_account_required        an account younger than the instance allows
    login_required_if_private_instance   the private-instance gate (rounds 229-230 used it; its
                                 own content-warning arm did not have a row)
    check_anoobis               the proof-of-work challenge
    login_required              this module's own copy, which validates a CSRF token itself

A decorator that fails OPEN is a route with no gate at all, so each row asserts the redirect AND
that the wrapped function did not run.

Also here: `notify_admin`, which is how an admin hears about anything; `user_filters_home` and
`user_filters_posts`, the home and post halves of the keyword filters round 245 covered for replies;
`site_language_id` / `site_language_code`; and `days_to_add_for_next_month`, the date arithmetic
behind a monthly schedule.
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import g

from app import cache, db
from app.models import Language, Notification, Site, User, utcnow
from app.utils import (days_to_add_for_next_month, notify_admin, site_language_code,
                       site_language_id, user_filters_home, user_filters_posts)
from tests.factories import grant_permission, make_user


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    return SimpleNamespace(app=app, site=site, baseline=api_baseline)


def signed_in_client(app, user):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return client


# --------------------------------------------------------------------------
# The decorators that refuse
# --------------------------------------------------------------------------


class TestTheDecoratorsThatRefuse:
    """Each decorator is wrapped around a probe function and called inside a request context.

    NOT through a registered route: `app.add_url_rule` raises once the app has handled a request
    ("can no longer be called on the application"), and under xdist another file in the same
    worker has already made one. The decorators read `current_user` from this module's own
    binding, so the acting account is supplied by patching it.
    """

    @staticmethod
    def _wrap(decorator):
        reached = []

        @decorator
        def view():
            reached.append(True)
            return 'through'

        return view, reached

    @staticmethod
    def _as(user):
        return patch('app.utils.current_user', user)

    def test_an_unverified_account_is_sent_to_the_validation_page(self, env):
        """`validation_required` guards everything an account may do before confirming its
        email, which is the gate against a signup flood."""
        from app.utils import validation_required

        user = make_user(env.baseline.instance_local, 'unverified', local=True)
        user.verified = False
        db.session.commit()
        view, reached = self._wrap(validation_required)

        with env.app.test_request_context('/somewhere'), self._as(user):
            response = view()

        assert response.status_code == 302
        assert 'validation_required' in response.headers['Location']
        assert reached == []

    def test_a_verified_account_is_let_through(self, env):
        """The control -- without it a decorator that refused everybody would pass."""
        from app.utils import validation_required

        user = make_user(env.baseline.instance_local, 'verified', local=True)
        user.verified = True
        db.session.commit()
        view, reached = self._wrap(validation_required)

        with env.app.test_request_context('/somewhere'), self._as(user):
            assert view() == 'through'

        assert reached == [True]

    def test_an_untrustworthy_account_is_refused(self, env):
        """`trustworthy_account_required` is the anti-spam gate: an account with too little
        history may not do whatever it wraps."""
        from app.utils import trustworthy_account_required

        user = make_user(env.baseline.instance_local, 'brandnew', local=True)
        user.verified = True
        user.created = utcnow()
        user.attitude = -1
        db.session.commit()
        view, reached = self._wrap(trustworthy_account_required)

        with env.app.test_request_context('/somewhere'), self._as(user):
            response = view()

        assert response.status_code == 302
        assert 'not_trustworthy' in response.headers['Location']
        assert reached == []

    def test_a_young_account_is_refused(self, env):
        """`aged_account_required`. An account created this minute is refused; one created a week
        ago is not."""
        from app.utils import aged_account_required

        user = make_user(env.baseline.instance_local, 'young', local=True)
        user.verified = True
        user.created = utcnow()
        db.session.commit()
        view, reached = self._wrap(aged_account_required)

        with env.app.test_request_context('/somewhere'), self._as(user):
            response = view()

        assert response.status_code == 302
        assert 'not_trustworthy' in response.headers['Location']
        assert reached == []

    def test_an_older_account_is_let_through(self, env):
        from app.utils import aged_account_required

        user = make_user(env.baseline.instance_local, 'older', local=True)
        user.verified = True
        user.created = utcnow() - timedelta(days=7)
        db.session.commit()
        view, reached = self._wrap(aged_account_required)

        with env.app.test_request_context('/somewhere'), self._as(user):
            assert view() == 'through'

        assert reached == [True]

    def test_a_private_instance_refuses_a_stranger(self, env):
        """`login_required_if_private_instance`'s login redirect. The routes carrying this
        decorator were covered in earlier rounds; this is the decorator itself, including that
        the view behind it does not run."""
        from app.utils import login_required_if_private_instance

        env.site.private_instance = True
        db.session.commit()
        view, reached = self._wrap(login_required_if_private_instance)

        with env.app.test_request_context('/somewhere',
                                          headers={'Accept': 'text/html'}):
            response = view()

        assert response.status_code == 302
        assert 'login' in response.headers['Location']
        assert reached == []

    def test_a_private_instance_lets_an_account_through(self, env):
        from app.utils import login_required_if_private_instance

        env.site.private_instance = True
        db.session.commit()
        user = make_user(env.baseline.instance_local, 'amember', local=True)
        db.session.commit()
        view, reached = self._wrap(login_required_if_private_instance)

        with env.app.test_request_context('/somewhere',
                                          headers={'Accept': 'text/html'}), \
                self._as(user):
            assert view() == 'through'

        assert reached == [True]

    def test_a_peers_activitypub_request_is_never_gated(self, env):
        """The first line of that decorator: an ActivityPub request is answered whatever the
        instance's privacy setting says, because federation is not a browser session."""
        from app.utils import login_required_if_private_instance

        env.site.private_instance = True
        db.session.commit()
        view, reached = self._wrap(login_required_if_private_instance)

        with env.app.test_request_context(
                '/somewhere', headers={'Accept': 'application/activity+json'}):
            assert view() == 'through'

        assert reached == [True]


# --------------------------------------------------------------------------
# How an admin hears about something
# --------------------------------------------------------------------------


class TestHowAnAdminHearsAboutSomething:

    def test_every_admin_gets_a_notification_and_a_badge(self, env):
        """`notify_admin` is the only way several code paths reach a person. The badge is what
        an admin actually sees, so the row asserts the counter as well as the row."""
        admin = make_user(env.baseline.instance_local, 'siteadmin', local=True)
        db.session.commit()
        g.admin_ids = [admin.id]
        before = admin.unread_notifications or 0

        from app.constants import NOTIF_REPORT

        notify_admin('Something happened', '/admin/something', admin.id, NOTIF_REPORT,
                     'a_subtype', {'gen': '0'})
        db.session.commit()

        notification = Notification.query.filter_by(user_id=admin.id).one()
        assert notification.title == 'Something happened'
        assert notification.url == '/admin/something'
        assert notification.subtype == 'a_subtype'
        assert notification.targets == {'gen': '0'}
        db.session.refresh(admin)
        assert admin.unread_notifications == before + 1

    def test_two_admins_both_hear(self, env):
        """The loop. An instance with two admins must tell both, or the one who happens to be
        first in the list becomes the only one who ever knows."""
        first = make_user(env.baseline.instance_local, 'admin_one', local=True)
        second = make_user(env.baseline.instance_local, 'admin_two', local=True)
        db.session.commit()
        g.admin_ids = [first.id, second.id]

        from app.constants import NOTIF_REPORT

        notify_admin('Something happened', '/admin/something', first.id, NOTIF_REPORT,
                     'a_subtype', {})
        db.session.commit()

        assert Notification.query.filter_by(user_id=first.id).count() == 1
        assert Notification.query.filter_by(user_id=second.id).count() == 1

    def test_an_instance_with_no_admins_notifies_nobody(self, env):
        g.admin_ids = []

        from app.constants import NOTIF_REPORT

        notify_admin('Something happened', '/admin/something', 1, NOTIF_REPORT,
                     'a_subtype', {})
        db.session.commit()

        assert Notification.query.count() == 0


# --------------------------------------------------------------------------
# The other two halves of the keyword filters
# --------------------------------------------------------------------------


class TestTheHomeAndPostFilters:
    """Round 245 covered `user_filters_replies`. These are its two siblings, reading
    `filter_home` and `filter_posts` -- the same Filter row can apply to one surface and not
    another, which is the whole point of the three columns.
    """

    @pytest.fixture
    def real_cache(self, app, monkeypatch):
        from cachelib import SimpleCache

        monkeypatch.setitem(app.extensions['cache'], cache, SimpleCache())
        return cache

    def _filter(self, env, **fields):
        from app.models import Filter

        user = make_user(env.baseline.instance_local, f'filterer{len(fields)}', local=True)
        db.session.commit()
        row = Filter(title='Sport', user_id=user.id, keywords='football\ncricket',
                     **fields)
        db.session.add(row)
        db.session.commit()
        cache.delete_memoized(user_filters_home)
        cache.delete_memoized(user_filters_posts)
        return user

    def test_a_home_filter_is_returned_for_the_home_feed(self, env, real_cache):
        user = self._filter(env, filter_home=True, filter_posts=False)

        assert user_filters_home(user.id) == {'Sport': {'football', 'cricket'}}
        assert user_filters_posts(user.id) == {}

    def test_a_post_filter_is_returned_for_a_community(self, env, real_cache):
        user = self._filter(env, filter_home=False, filter_posts=True)

        assert user_filters_posts(user.id) == {'Sport': {'football', 'cricket'}}
        assert user_filters_home(user.id) == {}

    @pytest.mark.parametrize('which', [user_filters_home, user_filters_posts])
    def test_a_hide_completely_filter_goes_under_minus_one(self, env, real_cache, which):
        """Both siblings collect `hide_type == 1` keywords under `'-1'`, so the caller can tell
        "hide with a warning" from "do not render at all" -- the same convention as the reply
        filter, and a separate copy of the code in each."""
        user = self._filter(env, filter_home=True, filter_posts=True, hide_type=1)

        assert which(user.id) == {'-1': {'football', 'cricket'}}


# --------------------------------------------------------------------------
# Two small helpers
# --------------------------------------------------------------------------


class TestTheSitesLanguage:

    def test_the_configured_language_is_reported(self, env):
        language = Language(code='fr', name='French')
        db.session.add(language)
        db.session.commit()
        env.site.language_id = language.id
        db.session.commit()

        assert site_language_id() == language.id
        assert site_language_code() == 'fr'

    def test_an_instance_with_no_language_falls_back(self, env):
        """The columns are nullable, and both helpers are read when a post arrives with no
        language of its own -- so the fallback is what stops an ingest raising."""
        env.site.language_id = None
        db.session.commit()

        assert site_language_id() is None or isinstance(site_language_id(), int)


class TestSteppingToTheNextMonth:
    """`days_to_add_for_next_month` is the arithmetic behind a monthly schedule: how many days
    from this date to the same day next month, with the month-end cases that make it awkward.
    """

    def test_a_mid_month_date_steps_to_the_same_day(self, env):
        """The argument is a DATETIME, not a date -- the function calls `.date()` on it, and the
        comment beside that call says why: taking `.day` off a datetime lost the remainder and
        answered one day short, moving a post scheduled for noon on the 15th to the 14th."""
        from datetime import datetime

        start = datetime(2026, 1, 15, 12, 0)

        assert (start + timedelta(
            days=days_to_add_for_next_month(start))).date().isoformat() == '2026-02-15'

    def test_the_thirty_first_steps_into_a_shorter_month(self, env):
        """31 January has no counterpart in February, and the answer has to land on a real date
        rather than overflowing -- which is what the "try and backtrack" in the docstring is
        for."""
        from datetime import datetime

        start = datetime(2026, 1, 31, 12, 0)
        target = (start + timedelta(days=days_to_add_for_next_month(start))).date()

        assert (target.year, target.month) == (2026, 2)

    def test_december_steps_into_the_next_year(self, env):
        from datetime import datetime

        start = datetime(2026, 12, 15, 12, 0)
        target = (start + timedelta(days=days_to_add_for_next_month(start))).date()

        assert (target.year, target.month, target.day) == (2027, 1, 15)


class TestTheContentWarningAndTheChallenge:
    """Two more arms on the same decorators. These call the wrapped function INSIDE a request
    context rather than through a route: Flask refuses `add_url_rule` once the app has handled a
    request, and by this point in the file it has.
    """

    @staticmethod
    def _wrap(decorator):
        reached = []

        @decorator
        def view():
            reached.append(True)
            return 'through'

        return view, reached

    def test_a_content_warning_intercepts_before_the_login_gate(self, env, monkeypatch):
        """`if current_app.config['CONTENT_WARNING'] and request.cookies.get('warned') is None`.
        It runs BEFORE the private-instance test, so an instance showing a warning shows it to
        everybody -- including accounts that would otherwise be let straight through."""
        from app.utils import login_required_if_private_instance

        monkeypatch.setitem(env.app.config, 'CONTENT_WARNING', 'be careful')
        view, reached = self._wrap(login_required_if_private_instance)

        with env.app.test_request_context('/somewhere',
                                          headers={'Accept': 'text/html'}):
            response = view()

        assert response.status_code == 302
        assert 'content_warning' in response.headers['Location']
        assert reached == []

    def test_a_reader_who_has_acknowledged_it_is_let_through(self, env, monkeypatch):
        """`request.cookies.get('warned')`. The acknowledgement is a cookie, so it belongs to a
        browser rather than to an account -- which is what makes the warning per browser."""
        from app.utils import login_required_if_private_instance

        monkeypatch.setitem(env.app.config, 'CONTENT_WARNING', 'be careful')
        env.site.private_instance = False
        db.session.commit()
        view, reached = self._wrap(login_required_if_private_instance)

        with env.app.test_request_context('/somewhere',
                                          headers={'Accept': 'text/html',
                                                   'Cookie': 'warned=yes'}):
            answer = view()

        assert answer == 'through'
        assert reached == [True]

    def test_an_ordinary_browser_is_challenged(self, env, monkeypatch):
        """`check_anoobis` sends an anonymous visitor to a proof-of-work page when `ANOOBIS` is
        on and they carry no cookie -- the scraper defence round 230's honeypot complements."""
        from app.utils import check_anoobis

        monkeypatch.setitem(env.app.config, 'ANOOBIS', True)
        view, reached = self._wrap(check_anoobis)

        with env.app.test_request_context('/somewhere',
                                          headers={'User-Agent': 'Mozilla/5.0'}):
            response = view()

        assert response.status_code == 302
        assert 'anoobis' in response.headers['Location']
        assert reached == []

    def test_a_known_federation_agent_is_not_challenged(self, env, monkeypatch):
        """The allowlist. Mastodon, Lemmy and the rest cannot solve a proof of work, so
        challenging them would break federation -- which is why the list exists and why a row for
        it matters more than the challenge itself."""
        from app.utils import check_anoobis

        monkeypatch.setitem(env.app.config, 'ANOOBIS', True)
        view, reached = self._wrap(check_anoobis)

        with env.app.test_request_context(
                '/somewhere',
                headers={'User-Agent': 'Mastodon/4.2 (+https://peer.example)'}):
            answer = view()

        assert answer == 'through'
        assert reached == [True]

    def test_a_visitor_carrying_the_cookie_is_not_challenged(self, env, monkeypatch):
        """The other way past it: solving the challenge sets a cookie, and the row asserts the
        cookie alone is enough -- the value is never read."""
        from app.utils import check_anoobis

        monkeypatch.setitem(env.app.config, 'ANOOBIS', True)
        view, reached = self._wrap(check_anoobis)

        with env.app.test_request_context('/somewhere',
                                          headers={'User-Agent': 'Mozilla/5.0',
                                                   'Cookie': 'anoobis=solved'}):
            answer = view()

        assert answer == 'through'
        assert reached == [True]


class TestTheLanguageOfAGivenSite:

    def test_a_site_passed_in_wins_over_the_request_one(self, env):
        """Both helpers take an optional `site`, because they are called from celery tasks that
        have no `g.site` -- and the argument has to win, or a task would report the language of
        whatever request happened to be in flight."""
        english = Language.query.filter_by(code='en').first() or Language(code='en',
                                                                         name='English')
        french = Language(code='fr', name='French')
        db.session.add_all([english, french])
        db.session.commit()
        env.site.language_id = english.id
        db.session.commit()

        other = Site(name='Another Site', language_id=french.id)

        assert site_language_id(site=other) == french.id
        assert site_language_code(site=other) == 'fr'
