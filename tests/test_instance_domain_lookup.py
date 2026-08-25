"""`app.utils.inbox_domain` and the four lookups that had its body copied inline.

`inbox_domain(value)` lower-cases an ActivityPub URL or bare domain and reduces
it to a hostname. `instance_allowed`, `instance_banned`, `instance_online` and
`instance_gone_forever` each carried the same two-line copy before looking the
result up in the database.

The tests are pins: every assertion here holds against the inline copies as
well, so they hold the refactor to "no behaviour change" rather than merely
exercising the new call.
"""

import pytest

from app import db
from app.models import AllowedInstances, BannedInstances, Instance
from app.utils import (inbox_domain, instance_allowed, instance_banned,
                       instance_gone_forever, instance_online)


class TestInboxDomain:
    def test_a_url_is_reduced_to_its_hostname(self):
        assert inbox_domain('https://example.com/u/alice/inbox') == 'example.com'

    def test_the_result_is_lower_case(self):
        assert inbox_domain('https://Example.COM/inbox') == 'example.com'

    def test_plain_http_is_recognised_too(self):
        assert inbox_domain('http://example.com/inbox') == 'example.com'

    def test_a_bare_domain_is_passed_through_lower_cased(self):
        assert inbox_domain('Example.COM') == 'example.com'

    def test_a_port_is_dropped(self):
        """`.hostname`, not `.netloc` -- so an instance on a non-default port
        normalises to the bare host. Longstanding behaviour, pinned here."""
        assert inbox_domain('https://example.com:8443/inbox') == 'example.com'

    def test_the_empty_string_survives(self):
        assert inbox_domain('') == ''


class TestInstanceAllowed:
    def test_a_bare_domain_matches_its_row(self, app, db_session):
        db.session.add(AllowedInstances(domain='allowed.example'))
        db.session.commit()
        assert instance_allowed('allowed.example') is True

    def test_an_inbox_url_matches_the_same_row(self, app, db_session):
        db.session.add(AllowedInstances(domain='allowed.example'))
        db.session.commit()
        assert instance_allowed('https://Allowed.Example/u/bob/inbox') is True

    def test_surrounding_whitespace_is_ignored(self, app, db_session):
        db.session.add(AllowedInstances(domain='allowed.example'))
        db.session.commit()
        assert instance_allowed('  allowed.example  ') is True

    def test_an_unlisted_domain_is_not_allowed(self, app, db_session):
        db.session.add(AllowedInstances(domain='allowed.example'))
        db.session.commit()
        assert instance_allowed('https://other.example/inbox') is False

    def test_an_empty_value_is_allowed_by_default(self, app, db_session):
        assert instance_allowed('') is True
        assert instance_allowed(None) is True


class TestInstanceBanned:
    def test_a_bare_domain_matches_its_row(self, app, db_session):
        db.session.add(BannedInstances(domain='banned.example'))
        db.session.commit()
        assert instance_banned('banned.example') is True

    def test_an_inbox_url_matches_the_same_row(self, app, db_session):
        db.session.add(BannedInstances(domain='banned.example'))
        db.session.commit()
        assert instance_banned('https://Banned.Example/u/bob/inbox') is True

    def test_surrounding_whitespace_is_ignored(self, app, db_session):
        db.session.add(BannedInstances(domain='banned.example'))
        db.session.commit()
        assert instance_banned('  BANNED.example  ') is True

    def test_an_unlisted_domain_is_not_banned(self, app, db_session):
        db.session.add(BannedInstances(domain='banned.example'))
        db.session.commit()
        assert instance_banned('https://other.example/inbox') is False

    def test_an_empty_value_is_not_banned(self, app, db_session):
        assert instance_banned('') is False
        assert instance_banned(None) is False

    def test_a_wildcard_ban_still_matches(self, app, db_session):
        """Mastodon-style '*' bans are matched by regex after the lookup misses;
        the value fed to that regex is the normalised domain."""
        db.session.add(BannedInstances(domain='cum.**mp'))
        db.session.commit()
        assert instance_banned('https://cum.camp/inbox') is True


class TestInstanceOnline:
    def _instance(self, domain, dormant=False, gone_forever=False):
        instance = Instance(domain=domain, software='piefed', dormant=dormant,
                            gone_forever=gone_forever)
        db.session.add(instance)
        db.session.commit()
        return instance

    def test_a_bare_domain_matches_its_row(self, app, db_session):
        self._instance('peer.example')
        assert instance_online('peer.example') is True

    def test_an_inbox_url_matches_the_same_row(self, app, db_session):
        self._instance('peer.example')
        assert instance_online('https://Peer.Example/u/bob/inbox') is True

    def test_surrounding_whitespace_is_ignored(self, app, db_session):
        self._instance('peer.example')
        assert instance_online('  peer.example  ') is True

    def test_a_dormant_instance_is_not_online(self, app, db_session):
        self._instance('peer.example', dormant=True)
        assert instance_online('https://peer.example/inbox') is False

    def test_an_unknown_domain_is_not_online(self, app, db_session):
        assert instance_online('https://nobody.example/inbox') is False

    def test_an_empty_value_is_not_online(self, app, db_session):
        assert instance_online('') is False
        assert instance_online(None) is False


class TestInstanceGoneForever:
    def _instance(self, domain, gone_forever=False):
        instance = Instance(domain=domain, software='piefed', gone_forever=gone_forever)
        db.session.add(instance)
        db.session.commit()
        return instance

    def test_a_bare_domain_matches_its_row(self, app, db_session):
        self._instance('peer.example', gone_forever=True)
        assert instance_gone_forever('peer.example') is True

    def test_an_inbox_url_matches_the_same_row(self, app, db_session):
        self._instance('peer.example', gone_forever=True)
        assert instance_gone_forever('https://Peer.Example/u/bob/inbox') is True

    def test_surrounding_whitespace_is_ignored(self, app, db_session):
        self._instance('peer.example', gone_forever=True)
        assert instance_gone_forever('  peer.example  ') is True

    def test_a_live_instance_is_not_gone(self, app, db_session):
        self._instance('peer.example')
        assert instance_gone_forever('https://peer.example/inbox') is False

    def test_an_unknown_domain_counts_as_gone(self, app, db_session):
        assert instance_gone_forever('https://nobody.example/inbox') is True

    def test_an_empty_value_is_not_gone(self, app, db_session):
        assert instance_gone_forever('') is False
        assert instance_gone_forever(None) is False
