"""Resolving a community from a handle somebody typed.

Sub-project 87 -- `search_for_community` in `app/community/util.py`. This is
the function that turns `!name@server` into a Community row, fetching the
remote one over WebFinger if this instance has never seen it. Every address
it is given comes from outside: a URL segment (`/c/<actor>/subscribe`), a
search box, or an API query parameter.

One defect, measured first:

* `name, server = address[1:].split('@')` unpacked whatever it was given, so
  `!name` was `ValueError: not enough values to unpack` and `!a@b@c` was
  `too many values to unpack` -- a 500 from a crafted URL, where "no such
  community" was the answer (D1258).
"""
import httpx
import pytest
from flask import current_app, g

from app import db
from app.community.util import search_for_community
from app.models import BannedInstances, Instance, Site
from tests.factories import make_community, make_instance


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    return SimpleNamespace(baseline=api_baseline,
                           server=current_app.config['SERVER_NAME'])


class TestAnAddressThatIsNotOne:
    """D1258. Each of these is a handle a URL or a search box can produce."""

    @pytest.mark.parametrize('address', [
        '!nowhere',           # no server
        '!a@b@c',             # two servers
        '!',                  # nothing at all
        '!name@',             # no server, with the marker
        '!@example.test',     # no name
        '!@',                 # neither
    ])
    def test_it_is_answered_with_nothing(self, env, address):
        assert search_for_community(address, allow_fetch=False) is None

    def test_an_address_with_no_marker_is_not_looked_up(self, env):
        """Only `!name@server` is a community handle; anything else is left
        alone, and the function answers None by falling off the end."""
        assert search_for_community('nowhere@example.test',
                                    allow_fetch=False) is None


class TestACommunityThisInstanceHosts:
    def test_it_is_found_by_its_handle(self, env):
        community = make_community('probeland', host=env.server)
        community.ap_profile_id = f"https://{env.server}/c/probeland"
        community.ap_id = None
        db.session.commit()
        assert search_for_community(f'!probeland@{env.server}',
                                    allow_fetch=False) == community

    def test_the_name_is_matched_in_lower_case(self, env):
        community = make_community('probeland', host=env.server)
        community.ap_profile_id = f"https://{env.server}/c/probeland"
        community.ap_id = None
        db.session.commit()
        assert search_for_community(f'!PROBELAND@{env.server}',
                                    allow_fetch=False) == community

    def test_one_nobody_hosts(self, env):
        assert search_for_community(f'!nowhere@{env.server}',
                                    allow_fetch=False) is None

    def test_a_remote_community_is_not_mistaken_for_a_local_one(self, env):
        """The local lookup requires `ap_id is None`: a row for a REMOTE
        community whose profile happens to sit under this server's name is not
        this instance's own."""
        community = make_community('probeland', host=env.server)
        community.ap_profile_id = f"https://{env.server}/c/probeland"
        community.ap_id = f'probeland@{env.server}'
        db.session.commit()
        assert search_for_community(f'!probeland@{env.server}',
                                    allow_fetch=False) is None


class TestACommunityAlreadyKnown:
    def test_it_is_found_without_asking_anyone(self, env):
        community = make_community('faraway', host='remote.test')
        community.ap_id = 'faraway@remote.test'
        db.session.commit()
        assert search_for_community('!faraway@remote.test',
                                    allow_fetch=False) == community

    def test_one_that_is_not_known_and_may_not_be_fetched(self, env):
        assert search_for_community('!unknown@remote.test',
                                    allow_fetch=False) is None


class TestInstancesThisOneWillNotTalkTo:
    def test_a_banned_instance_is_not_asked(self, env):
        db.session.add(BannedInstances(domain='nasty.test'))
        db.session.commit()
        assert search_for_community('!anything@nasty.test') is None

    def test_an_instance_not_on_the_allowlist_is_not_asked(self, env):
        from app.utils import set_setting
        set_setting('use_allowlist', True)
        assert search_for_community('!anything@stranger.test') is None

    def test_an_instance_on_the_allowlist_is(self, env, http_mock):
        from app.models import AllowedInstances
        from app.utils import set_setting
        set_setting('use_allowlist', True)
        db.session.add(AllowedInstances(domain='friend.test'))
        db.session.commit()
        http_mock.get('https://friend.test/.well-known/webfinger').mock(
            return_value=httpx.Response(404))
        assert search_for_community('!anything@friend.test') is None


class TestAskingTheRemoteInstance:
    """What WebFinger says, and what this instance does with it."""

    def test_a_webfinger_that_answers_404(self, env, http_mock):
        http_mock.get('https://remote.test/.well-known/webfinger').mock(
            return_value=httpx.Response(404))
        assert search_for_community('!unknown@remote.test') is None

    def test_a_webfinger_that_does_not_answer_at_all(self, env, http_mock,
                                                     monkeypatch):
        """The lookup is tried twice, with a pause between; both fail here."""
        monkeypatch.setattr('app.community.util.sleep', lambda seconds: None)
        http_mock.get('https://remote.test/.well-known/webfinger').mock(
            side_effect=httpx.ConnectError('no route'))
        assert search_for_community('!unknown@remote.test') is None

    def test_a_webfinger_that_answers_on_the_second_try(self, env, http_mock,
                                                        monkeypatch):
        monkeypatch.setattr('app.community.util.sleep', lambda seconds: None)
        http_mock.get('https://remote.test/.well-known/webfinger').mock(
            side_effect=[httpx.ConnectError('no route'),
                         httpx.Response(404)])
        assert search_for_community('!unknown@remote.test') is None

    def test_a_webfinger_with_no_self_link(self, env, http_mock):
        http_mock.get('https://remote.test/.well-known/webfinger').mock(
            return_value=httpx.Response(200, json={'links': [
                {'rel': 'http://webfinger.net/rel/profile-page',
                 'href': 'https://remote.test/c/unknown'}]}))
        assert search_for_community('!unknown@remote.test') is None

    def test_an_actor_that_is_not_a_group(self, env, http_mock):
        http_mock.get('https://remote.test/.well-known/webfinger').mock(
            return_value=httpx.Response(200, json={'links': [
                {'rel': 'self', 'type': 'application/activity+json',
                 'href': 'https://remote.test/u/someone'}]}))
        http_mock.get('https://remote.test/u/someone').mock(
            return_value=httpx.Response(200, json={
                'type': 'Person', 'id': 'https://remote.test/u/someone',
                'preferredUsername': 'someone'}))
        assert search_for_community('!someone@remote.test') is None

    def test_an_actor_that_is_not_json(self, env, http_mock):
        http_mock.get('https://remote.test/.well-known/webfinger').mock(
            return_value=httpx.Response(200, json={'links': [
                {'rel': 'self', 'type': 'application/activity+json',
                 'href': 'https://remote.test/c/unknown'}]}))
        http_mock.get('https://remote.test/c/unknown').mock(
            return_value=httpx.Response(200, text='<html>not json</html>'))
        assert search_for_community('!unknown@remote.test') is None

    def test_an_actor_that_answers_404(self, env, http_mock):
        http_mock.get('https://remote.test/.well-known/webfinger').mock(
            return_value=httpx.Response(200, json={'links': [
                {'rel': 'self', 'type': 'application/activity+json',
                 'href': 'https://remote.test/c/unknown'}]}))
        http_mock.get('https://remote.test/c/unknown').mock(
            return_value=httpx.Response(404))
        assert search_for_community('!unknown@remote.test') is None
