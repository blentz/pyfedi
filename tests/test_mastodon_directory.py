"""Filtering Mastodon's /api/v1/directory into a list of handles to follow.

The admin federation page has two community importers (preload from lemmyverse,
and a remote instance scan). Neither can work against Mastodon: they call Lemmy's
/api/v3/community/list and equivalents, and Mastodon has no communities. This is
the user-shaped analogue, reading the opt-in public profile directory.

The filtering is the part worth testing. Following is already handled by
bulk_follow, which resolves each handle through search_for_user.
"""

import unittest

from app.admin.util import directory_candidates


def account(acct='someone', statuses=100, followers=50, bot=False):
    """One entry shaped like Mastodon's /api/v1/directory returns it.

    Note `acct` is a BARE username for accounts local to the queried instance --
    the domain has to be appended by us.
    """
    return {
        'acct': acct,
        'statuses_count': statuses,
        'followers_count': followers,
        'bot': bot,
    }


class TestDirectoryCandidates(unittest.TestCase):

    def test_bare_acct_gains_the_queried_domain(self):
        """Mastodon returns a bare username for local accounts; we must qualify it"""
        handles, _ = directory_candidates([account(acct='wakko')], 'mastodon.cloud',
                                          minimum_statuses=1, minimum_followers=1,
                                          exclude_bots=True, limit=10)
        self.assertEqual(handles, ['wakko@mastodon.cloud'])

    def test_already_qualified_acct_is_left_alone(self):
        """A directory can return an already-qualified acct; do not double-append"""
        handles, _ = directory_candidates([account(acct='wakko@elsewhere.example')],
                                          'mastodon.cloud', minimum_statuses=1,
                                          minimum_followers=1, exclude_bots=True, limit=10)
        self.assertEqual(handles, ['wakko@elsewhere.example'])

    def test_below_minimum_statuses_excluded(self):
        handles, stats = directory_candidates([account(statuses=5)], 'mastodon.cloud',
                                              minimum_statuses=50, minimum_followers=1,
                                              exclude_bots=True, limit=10)
        self.assertEqual(handles, [])
        self.assertEqual(stats['below_minimum_statuses'], 1)

    def test_below_minimum_followers_excluded(self):
        handles, stats = directory_candidates([account(followers=2)], 'mastodon.cloud',
                                              minimum_statuses=1, minimum_followers=10,
                                              exclude_bots=True, limit=10)
        self.assertEqual(handles, [])
        self.assertEqual(stats['below_minimum_followers'], 1)

    def test_bots_excluded_when_flag_set(self):
        handles, stats = directory_candidates([account(bot=True)], 'mastodon.cloud',
                                              minimum_statuses=1, minimum_followers=1,
                                              exclude_bots=True, limit=10)
        self.assertEqual(handles, [])
        self.assertEqual(stats['bots'], 1)

    def test_bots_included_when_flag_clear(self):
        handles, _ = directory_candidates([account(acct='botty', bot=True)], 'mastodon.cloud',
                                          minimum_statuses=1, minimum_followers=1,
                                          exclude_bots=False, limit=10)
        self.assertEqual(handles, ['botty@mastodon.cloud'])

    def test_limit_caps_the_result(self):
        """The admin asked for N accounts; return no more than N"""
        accounts = [account(acct=f'user{i}') for i in range(10)]
        handles, stats = directory_candidates(accounts, 'mastodon.cloud',
                                              minimum_statuses=1, minimum_followers=1,
                                              exclude_bots=True, limit=3)
        self.assertEqual(len(handles), 3)
        self.assertEqual(stats['candidates'], 10, 'stats report everything that passed the filters')

    def test_missing_counts_are_treated_as_zero(self):
        """A directory entry missing a count must not raise"""
        handles, _ = directory_candidates([{'acct': 'sparse'}], 'mastodon.cloud',
                                          minimum_statuses=1, minimum_followers=1,
                                          exclude_bots=True, limit=10)
        self.assertEqual(handles, [])

    def test_entry_without_acct_is_skipped(self):
        """Malformed entries are skipped rather than producing an unusable handle"""
        handles, _ = directory_candidates([{'statuses_count': 100, 'followers_count': 100}],
                                          'mastodon.cloud', minimum_statuses=1,
                                          minimum_followers=1, exclude_bots=True, limit=10)
        self.assertEqual(handles, [])

    def test_stats_total_counts_everything_seen(self):
        accounts = [account(statuses=5), account(followers=1), account(bot=True), account()]
        _, stats = directory_candidates(accounts, 'mastodon.cloud', minimum_statuses=50,
                                        minimum_followers=10, exclude_bots=True, limit=10)
        self.assertEqual(stats['seen'], 4)
        self.assertEqual(stats['candidates'], 1)


class TestFetchDirectory(unittest.TestCase):
    """Paging /api/v1/directory. The network is stubbed; what matters is that we
    stop, and that we cannot be made to page forever by a large or hostile host."""

    def _stub(self, pages):
        """Return a fake get_request yielding each page in turn."""
        calls = []

        class FakeResponse:
            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

            def close(self):
                pass

        def fake_get(url, params=None, **kwargs):
            calls.append(params or {})
            index = len(calls) - 1
            return FakeResponse(pages[index] if index < len(pages) else [])

        return fake_get, calls

    def test_stops_on_a_short_page(self):
        from app.admin import util as admin_util
        fake_get, calls = self._stub([[account(acct=f'u{i}') for i in range(80)], [account()]])
        original = admin_util.get_request
        admin_util.get_request = fake_get
        try:
            accounts = admin_util.fetch_mastodon_directory('https://mastodon.cloud', max_pages=10)
        finally:
            admin_util.get_request = original

        self.assertEqual(len(accounts), 81)
        self.assertEqual(len(calls), 2, 'a short page ends the paging')

    def test_max_pages_caps_a_host_that_never_returns_a_short_page(self):
        from app.admin import util as admin_util
        full_page = [account(acct=f'u{i}') for i in range(80)]
        fake_get, calls = self._stub([full_page] * 50)
        original = admin_util.get_request
        admin_util.get_request = fake_get
        try:
            accounts = admin_util.fetch_mastodon_directory('https://mastodon.cloud', max_pages=3)
        finally:
            admin_util.get_request = original

        self.assertEqual(len(calls), 3, 'paging must stop at max_pages')
        self.assertEqual(len(accounts), 240)

    def test_offset_advances_between_pages(self):
        from app.admin import util as admin_util
        full_page = [account(acct=f'u{i}') for i in range(80)]
        fake_get, calls = self._stub([full_page, full_page, []])
        original = admin_util.get_request
        admin_util.get_request = fake_get
        try:
            admin_util.fetch_mastodon_directory('https://mastodon.cloud', max_pages=5)
        finally:
            admin_util.get_request = original

        offsets = [int(c.get('offset', 0)) for c in calls]
        self.assertEqual(offsets[:3], [0, 80, 160])


class TestRemoteInstanceSoftware(unittest.TestCase):
    """Reading software.name out of nodeinfo, extracted so it can be tested.

    The existing remote scan inlines this and indexes nodeinfo blindly; a host
    returning an unexpected shape raises rather than reporting a clear message.
    """

    def _stub(self, payloads):
        class FakeResponse:
            def __init__(self, payload):
                self._payload = payload
                self.text = ''

            def json(self):
                return self._payload

            def close(self):
                pass

        calls = []

        def fake_get(url, params=None, **kwargs):
            calls.append(url)
            return FakeResponse(payloads[len(calls) - 1])

        return fake_get, calls

    def _run(self, payloads):
        from app.admin import util as admin_util
        fake_get, calls = self._stub(payloads)
        original = admin_util.get_request
        admin_util.get_request = fake_get
        try:
            return admin_util.remote_instance_software('https://mastodon.cloud'), calls
        finally:
            admin_util.get_request = original

    def test_reads_software_name_from_schema_2_0(self):
        nodeinfo = {'links': [{'rel': 'http://nodeinfo.diaspora.software/ns/schema/2.0',
                               'href': 'https://mastodon.cloud/nodeinfo/2.0'}]}
        software, calls = self._run([nodeinfo, {'software': {'name': 'mastodon'}}])
        self.assertEqual(software, 'mastodon')
        self.assertEqual(calls[0], 'https://mastodon.cloud/.well-known/nodeinfo')

    def test_reads_software_name_from_schema_2_1(self):
        nodeinfo = {'links': [{'rel': 'http://nodeinfo.diaspora.software/ns/schema/2.1',
                               'href': 'https://mastodon.cloud/nodeinfo/2.1'}]}
        software, _ = self._run([nodeinfo, {'software': {'name': 'mastodon'}}])
        self.assertEqual(software, 'mastodon')

    def test_name_is_lowercased(self):
        """Software names are compared against a literal, so casing must not matter"""
        nodeinfo = {'links': [{'rel': 'http://nodeinfo.diaspora.software/ns/schema/2.0',
                               'href': 'https://mastodon.cloud/nodeinfo/2.0'}]}
        software, _ = self._run([nodeinfo, {'software': {'name': 'Mastodon'}}])
        self.assertEqual(software, 'mastodon')

    def test_no_recognised_schema_link_raises(self):
        nodeinfo = {'links': [{'rel': 'http://example.com/other', 'href': 'https://x/y'}]}
        with self.assertRaises(Exception):
            self._run([nodeinfo])

    def test_missing_links_raises_rather_than_keyerror_escaping(self):
        with self.assertRaises(Exception):
            self._run([{'nope': True}])
