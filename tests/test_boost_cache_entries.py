import unittest
from datetime import datetime

from app.utils import boost_cache_entries


class TestBoostCacheEntries(unittest.TestCase):

    def test_single_remote_booster(self):
        """A remote user's ap_id and ISO timestamp are carried into the cache"""
        rows = [(7, 'alice@m.example', 'alice', datetime(2026, 8, 23, 12, 30, 0))]
        self.assertEqual(boost_cache_entries(rows), [{
            'user_id': 7,
            'ap_id': 'alice@m.example',
            'display_name': 'alice',
            'created_at': '2026-08-23T12:30:00',
        }])

    def test_local_booster_has_no_ap_id(self):
        """A local user has ap_id None; the cache stores an empty string"""
        rows = [(3, None, 'bob', datetime(2026, 8, 23, 9, 0, 0))]
        self.assertEqual(boost_cache_entries(rows)[0]['ap_id'], '')

    def test_missing_timestamp(self):
        """A null created_at becomes an empty string rather than raising"""
        rows = [(3, None, 'bob', None)]
        self.assertEqual(boost_cache_entries(rows)[0]['created_at'], '')

    def test_order_preserved(self):
        """Input order is preserved; the caller decides the ordering"""
        rows = [
            (1, None, 'first', datetime(2026, 8, 23, 10, 0, 0)),
            (2, None, 'second', datetime(2026, 8, 23, 11, 0, 0)),
        ]
        names = [entry['display_name'] for entry in boost_cache_entries(rows)]
        self.assertEqual(names, ['first', 'second'])

    def test_empty(self):
        """No boosts yields an empty list, not None"""
        self.assertEqual(boost_cache_entries([]), [])
