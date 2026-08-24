import unittest

from app.activitypub.util import announce_target_uri, is_top_level


class TestAnnounceTargetUri(unittest.TestCase):

    def test_string_object(self):
        """Mastodon sends the boosted object as a bare URI string"""
        self.assertEqual(
            announce_target_uri({'type': 'Announce', 'object': 'https://m.example/notes/1'}),
            'https://m.example/notes/1')

    def test_dict_object_uses_id(self):
        """Some platforms embed the object; its id is the URI"""
        self.assertEqual(
            announce_target_uri({'type': 'Announce', 'object': {'id': 'https://m.example/notes/2'}}),
            'https://m.example/notes/2')

    def test_missing_object(self):
        """An Announce with no object yields None rather than raising"""
        self.assertIsNone(announce_target_uri({'type': 'Announce'}))

    def test_empty_string_object(self):
        """An empty string is not a usable URI"""
        self.assertIsNone(announce_target_uri({'type': 'Announce', 'object': ''}))

    def test_dict_object_without_id(self):
        """An embedded object with no id yields None"""
        self.assertIsNone(announce_target_uri({'type': 'Announce', 'object': {'type': 'Note'}}))

    def test_dict_object_with_non_string_id(self):
        """A non-string id is rejected rather than returned"""
        self.assertIsNone(announce_target_uri({'type': 'Announce', 'object': {'id': 12345}}))

    def test_non_dict_activity(self):
        """A malformed activity yields None rather than raising"""
        self.assertIsNone(announce_target_uri('not a dict'))


class TestIsTopLevel(unittest.TestCase):

    def test_no_in_reply_to_key(self):
        """An object with no inReplyTo is top-level"""
        self.assertTrue(is_top_level({'type': 'Note', 'content': 'hello'}))

    def test_in_reply_to_none(self):
        """inReplyTo explicitly null is top-level"""
        self.assertTrue(is_top_level({'type': 'Note', 'inReplyTo': None}))

    def test_in_reply_to_empty_string(self):
        """inReplyTo as an empty string is top-level"""
        self.assertTrue(is_top_level({'type': 'Note', 'inReplyTo': ''}))

    def test_in_reply_to_set(self):
        """An object with a real inReplyTo is a reply, not top-level"""
        self.assertFalse(is_top_level({'type': 'Note', 'inReplyTo': 'https://m.example/notes/1'}))

    def test_non_dict(self):
        """Malformed data is not top-level"""
        self.assertFalse(is_top_level(None))
