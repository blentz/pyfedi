import unittest

from app.activitypub.util import activitypub_visibility

PUBLIC_SPELLINGS = [
    'https://www.w3.org/ns/activitystreams#Public',
    'as:Public',
    'Public',
]
FOLLOWERS = 'https://m.example/users/alice/followers'


class TestActivityPubVisibility(unittest.TestCase):

    def test_public_in_to_all_spellings(self):
        """Every legal spelling of as:Public in `to` means public"""
        for spelling in PUBLIC_SPELLINGS:
            with self.subTest(spelling=spelling):
                self.assertEqual(
                    activitypub_visibility({'to': [spelling], 'cc': [FOLLOWERS]}), 'public')

    def test_unlisted_all_spellings(self):
        """Public in `cc` but not `to` means unlisted"""
        for spelling in PUBLIC_SPELLINGS:
            with self.subTest(spelling=spelling):
                self.assertEqual(
                    activitypub_visibility({'to': [FOLLOWERS], 'cc': [spelling]}), 'unlisted')

    def test_followers_only(self):
        """A followers URL addressed with no Public anywhere means followers-only"""
        self.assertEqual(activitypub_visibility({'to': [FOLLOWERS], 'cc': []}), 'followers')

    def test_followers_url_in_cc_only(self):
        """A followers URL in cc still counts as followers-only"""
        self.assertEqual(activitypub_visibility({'to': [], 'cc': [FOLLOWERS]}), 'followers')

    def test_direct(self):
        """Addressed only to named actors means direct"""
        self.assertEqual(
            activitypub_visibility({'to': ['https://m.example/users/bob'], 'cc': []}), 'direct')

    def test_to_as_bare_string(self):
        """`to` may be a string rather than a list"""
        self.assertEqual(
            activitypub_visibility({'to': 'https://www.w3.org/ns/activitystreams#Public'}), 'public')

    def test_cc_as_bare_string(self):
        """`cc` may be a string rather than a list"""
        self.assertEqual(
            activitypub_visibility({'to': FOLLOWERS, 'cc': 'as:Public'}), 'unlisted')

    def test_both_absent(self):
        """An object with no addressing at all is direct, not public"""
        self.assertEqual(activitypub_visibility({'type': 'Note'}), 'direct')

    def test_non_dict(self):
        """Malformed input is direct, the safe default"""
        self.assertEqual(activitypub_visibility(None), 'direct')

    def test_non_string_entries_ignored(self):
        """Non-string entries in an addressing list do not raise"""
        self.assertEqual(
            activitypub_visibility({'to': [None, 123, 'as:Public']}), 'public')

    def test_public_wins_over_followers_in_to(self):
        """Public and a followers URL together in `to` is public"""
        self.assertEqual(
            activitypub_visibility({'to': ['as:Public', FOLLOWERS]}), 'public')
