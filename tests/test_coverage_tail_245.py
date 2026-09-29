"""Round 245: the per-peer and per-viewer gates in `app/utils.py`.

Six helpers, each consulted before this instance talks to a peer or shows somebody a page,
and none of them had rows:

    instance_online / instance_gone_forever   whether a peer is worth delivering to
    banned_ip_addresses                       the admin's IP ban list
    user_cookie_banned                        a cookie that marks a browser as banned
    guess_mime_type                           the type served for a stored file
    user_filters_replies                      a viewer's own keyword filters for replies

`instance_gone_forever` answers **True** for a domain this instance has never seen, which is
the opposite of what its name suggests and the more conservative answer -- it is asserted
here rather than left to be discovered.

`guess_mime_type` is what a stored file is served as, so an answer of `text/html` for an
upload would be a stored-XSS. Its fallbacks build `image/<ext>` from the extension, which is
worth pinning for the same reason.
"""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.models import Filter, Instance, IpBan, Site, utcnow
from app.utils import (banned_ip_addresses, guess_mime_type, instance_gone_forever,
                       instance_online, user_cookie_banned, user_filters_replies)
from tests.factories import make_instance, make_user


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    return SimpleNamespace(app=app, site=g.site, baseline=api_baseline)


# --------------------------------------------------------------------------
# Is this peer worth talking to
# --------------------------------------------------------------------------


class TestWhetherAPeerIsReachable:
    """`instance_online` and `instance_gone_forever` are consulted before delivery. Both
    take a DOMAIN rather than an Instance, look the row up in a task session, and answer
    for a domain they do not hold -- differently.
    """

    @pytest.fixture
    def seeded(self, env):
        live = make_instance('live.example')
        live.dormant = False
        live.gone_forever = False
        dead = make_instance('dead.example')
        dead.dormant = True
        dead.gone_forever = True
        db.session.commit()
        env.live = live
        env.dead = dead
        return env

    def test_a_live_peer_is_online(self, seeded):
        assert instance_online('live.example') is True

    def test_a_dormant_peer_is_not(self, seeded):
        assert instance_online('dead.example') is False

    def test_a_domain_this_instance_has_never_seen_is_not_online(self, seeded):
        """`else: return False`. A peer with no Instance row is one nothing has ever been
        delivered to, and claiming it is online would send an activity into the dark."""
        assert instance_online('stranger.example') is False

    def test_an_empty_domain_is_not_online(self, seeded):
        """The guard at the top. `inbox_domain(None.strip())` would be an
        `AttributeError`, and the domain comes from a URL a peer supplied."""
        assert instance_online('') is False
        assert instance_online(None) is False

    def test_a_peer_marked_gone_is_gone(self, seeded):
        assert instance_gone_forever('dead.example') is True

    def test_a_live_peer_is_not_gone(self, seeded):
        assert instance_gone_forever('live.example') is False

    def test_an_unknown_domain_is_treated_as_gone(self, seeded):
        """`else: return True` -- the opposite default from `instance_online`, and the
        conservative one: a domain with no row has never been reached, so treating it as
        gone stops this instance retrying it forever."""
        assert instance_gone_forever('stranger.example') is True

    def test_an_empty_domain_is_not_gone(self, seeded):
        """And here the empty string answers False rather than True, because '' is not a
        peer at all -- so the two guards in these two functions do NOT agree, which is
        worth stating."""
        assert instance_gone_forever('') is False
        assert instance_gone_forever(None) is False

    def test_a_url_is_reduced_to_its_inbox_domain(self, seeded):
        """`inbox_domain(domain.strip())`. Callers pass whatever they have, including a
        full inbox URL, and surrounding whitespace comes off first."""
        assert instance_online('  live.example  ') is True
        assert instance_online('https://live.example/inbox') is True


# --------------------------------------------------------------------------
# Who is banned
# --------------------------------------------------------------------------


class TestTheIpBanList:

    def test_every_banned_address_is_listed(self, env):
        db.session.add_all([IpBan(ip_address='198.51.100.4', notes='a scraper'),
                            IpBan(ip_address='203.0.113.7', notes='another')])
        db.session.commit()

        listed = banned_ip_addresses()

        assert '198.51.100.4' in listed
        assert '203.0.113.7' in listed

    def test_an_empty_ban_list_is_an_empty_list(self, env):
        """Not None: every caller writes `if ip_address() in banned_ip_addresses()`, and
        `in None` is a `TypeError` on every request."""
        assert banned_ip_addresses() == []


class TestTheBannedBrowserCookie:
    """`user_cookie_banned` looks for a cookie named `sesion` -- deliberately misspelt, so
    it does not collide with the session cookie. Its presence alone is the ban; the value is
    never read.
    """

    def test_a_browser_carrying_the_cookie_is_banned(self, env):
        with env.app.test_request_context('/', headers={'Cookie': 'sesion=anything'}):
            assert user_cookie_banned() is True

    def test_a_browser_without_it_is_not(self, env):
        with env.app.test_request_context('/'):
            assert user_cookie_banned() is False

    def test_the_value_is_not_read(self, env):
        """Presence, not content: a cookie set to an empty string still counts, which is
        what makes the check impossible to satisfy accidentally in one direction and
        trivial to clear in the other."""
        with env.app.test_request_context('/', headers={'Cookie': 'sesion='}):
            assert user_cookie_banned() is True

    def test_the_session_cookie_is_not_it(self, env):
        """The misspelling is the point -- a correctly spelt `session` cookie must not ban
        the browser that carries it, which is every logged-in browser."""
        with env.app.test_request_context('/', headers={'Cookie': 'session=abc'}):
            assert user_cookie_banned() is False


# --------------------------------------------------------------------------
# What a stored file is served as
# --------------------------------------------------------------------------


class TestGuessingAStoredFilesType:
    """The answer becomes a `Content-Type`, so it is a security answer: a stored upload
    served as `text/html` is a stored-XSS. The function prefers `mimetypes`, and falls back
    to `image/<ext>` twice -- once when `guess_type` answers None outright and once when it
    answers a tuple whose first element is None.
    """

    @pytest.mark.parametrize('path,expected', [
        ('photo.png', 'image/png'),
        ('photo.jpg', 'image/jpeg'),
        ('clip.mp4', 'video/mp4'),
    ])
    def test_a_known_extension_gets_its_real_type(self, env, path, expected):
        assert guess_mime_type(path) == expected

    def test_an_unknown_extension_is_assumed_to_be_an_image(self, env):
        """The fallback. `mimetypes` does not know `.avif` on every platform, and these
        files are uploads -- so the guess is a narrow `image/<ext>` rather than something
        a browser would execute."""
        assert guess_mime_type('photo.notatype') == 'image/notatype'

    def test_a_file_with_no_extension_is_a_byte_stream(self, env):
        """`application/octet-stream`, which a browser downloads rather than renders. That
        is the right answer for a file whose type nothing can determine."""
        assert guess_mime_type('justaname') == 'application/octet-stream'

    def test_a_path_with_a_trailing_dot_is_a_byte_stream_too(self, env):
        """`.lstrip('.')` leaves an empty extension, and `image/` is not a media type."""
        assert guess_mime_type('justaname.') == 'application/octet-stream'


# --------------------------------------------------------------------------
# A viewer's keyword filters for replies
# --------------------------------------------------------------------------


class TestAViewersReplyFilters:
    """`user_filters_replies` returns `{title: {keywords}}` for the filters a viewer has
    marked as applying to replies. A filter set to hide COMPLETELY is collected under the
    key `'-1'`, which is how the caller tells "hide with a warning" from "do not show at
    all".
    """

    @pytest.fixture
    def viewer(self, env):
        user = make_user(env.baseline.instance_local, 'filterer', local=True)
        db.session.commit()
        env.viewer = user
        return env

    def _filter(self, viewer, title, keywords, hide_type=0, filter_replies=True,
                expire_after=None):
        row = Filter(title=title, user_id=viewer.id, keywords=keywords,
                     hide_type=hide_type, filter_replies=filter_replies,
                     expire_after=expire_after)
        db.session.add(row)
        db.session.commit()
        return row

    def test_a_filters_keywords_are_grouped_under_its_title(self, viewer):
        self._filter(viewer.viewer, 'Sport', 'football\ncricket')

        assert user_filters_replies(viewer.viewer.id) == {'Sport': {'football',
                                                                   'cricket'}}

    def test_keywords_are_lower_cased_and_stripped(self, viewer):
        """The comparison downstream is against lower-cased text, so a keyword typed with a
        capital or a trailing space would never match anything."""
        self._filter(viewer.viewer, 'Sport', '  FootBall  \n CRICKET')

        assert user_filters_replies(viewer.viewer.id) == {'Sport': {'football',
                                                                   'cricket'}}

    def test_a_hide_completely_filter_goes_under_minus_one(self, viewer):
        """`hide_type` 1 means hide without a warning, and every such keyword is collected
        under one key -- so the caller does not need to know which filter matched, only
        that the reply must not be rendered at all."""
        self._filter(viewer.viewer, 'Sport', 'football', hide_type=0)
        self._filter(viewer.viewer, 'Spoilers', 'ending', hide_type=1)

        result = user_filters_replies(viewer.viewer.id)

        assert result['Sport'] == {'football'}
        assert result['-1'] == {'ending'}

    def test_a_filter_that_does_not_apply_to_replies_is_left_out(self, viewer):
        """`filter_replies=True`. The same Filter row can apply to the home feed, to posts,
        to replies, or to any combination, and this function answers for replies only."""
        self._filter(viewer.viewer, 'Sport', 'football', filter_replies=False)

        assert user_filters_replies(viewer.viewer.id) == {}

    def test_an_expired_filter_is_left_out(self, viewer):
        """`expire_after > today OR expire_after IS NULL`. A filter with a past date is one
        the viewer asked to stop applying."""
        self._filter(viewer.viewer, 'Sport', 'football',
                     expire_after=date.today() - timedelta(days=1))

        assert user_filters_replies(viewer.viewer.id) == {}

    def test_a_filter_expiring_in_the_future_still_applies(self, viewer):
        self._filter(viewer.viewer, 'Sport', 'football',
                     expire_after=date.today() + timedelta(days=1))

        assert user_filters_replies(viewer.viewer.id) == {'Sport': {'football'}}

    def test_another_viewers_filters_are_not_returned(self, viewer):
        """The filters are per account, and returning somebody else's would hide content
        from a viewer who never asked."""
        other = make_user(viewer.baseline.instance_local, 'otherfilterer', local=True)
        db.session.commit()
        self._filter(other, 'Theirs', 'something')

        assert user_filters_replies(viewer.viewer.id) == {}


class TestWhenTheDatabaseFailsUnderThesePredicates:
    """Each of these helpers opens its own task session and wraps the work in
    `except Exception: session.rollback(); raise` / `finally: session.close()`. They are
    called from request handlers AND from celery tasks, so a failure has to leave the
    session clean rather than half-open -- but it must still propagate, because answering
    "not banned" or "online" after a database error would be a gate failing open.
    """

    @pytest.fixture
    def broken_session(self, monkeypatch):
        class Broken:
            def __init__(self):
                self.rolled_back = False
                self.closed = False

            def query(self, *args, **kwargs):
                raise RuntimeError('the connection went away')

            def rollback(self):
                self.rolled_back = True

            def close(self):
                self.closed = True

        broken = Broken()
        monkeypatch.setattr('app.utils.get_task_session', lambda: broken)
        return broken

    @pytest.mark.parametrize('call', [
        lambda: instance_online('live.example'),
        lambda: instance_gone_forever('live.example'),
        lambda: banned_ip_addresses(),
    ])
    def test_the_failure_is_raised_after_the_session_is_cleaned_up(self, env,
                                                                  broken_session, call):
        with pytest.raises(RuntimeError, match='connection went away'):
            call()

        assert broken_session.rolled_back is True
        assert broken_session.closed is True


class TestTheUnreachableMimeFallback:

    def test_guess_type_always_returns_a_tuple(self, env):
        """`guess_mime_type` opens with `if content_type is None:` and a fallback behind it.
        `mimetypes.guess_type` returns a 2-TUPLE for every input -- `(None, None)` when it
        cannot tell -- so that arm is unreachable and the `content_type[0] is None` branch
        below it is the one that actually runs.

        Recorded rather than deleted: the two fallbacks are identical, so removing the
        first changes nothing, and asserting the library's contract here is what would fail
        if a future Python changed it.
        """
        import mimetypes

        for path in ['photo.png', 'justaname', 'photo.notatype', '']:
            answer = mimetypes.guess_type(path)
            assert isinstance(answer, tuple) and len(answer) == 2
