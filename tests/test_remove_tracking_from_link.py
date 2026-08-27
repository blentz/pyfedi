"""remove_tracking_from_link (app/utils.py:3083-3105) rewrites youtu.be share
links to youtube.com/watch, preserving only the timestamp parameter. Every
other host passes through untouched.

This is the only pure function in sub-project 1c -- no DB, no network.
"""
from app.utils import remove_tracking_from_link


class TestYoutubeShortLinks:
    """Mutation that fails these: changing the `netloc == 'youtu.be'` test, or
    deleting the rewrite so the url returns unchanged."""

    def test_a_bare_short_link_becomes_a_watch_url(self, app):
        assert remove_tracking_from_link('https://youtu.be/abc123') == \
            'https://youtube.com/watch?v=abc123'

    def test_tracking_parameters_are_dropped(self, app):
        """si= is the share-tracking parameter; only t= survives."""
        assert remove_tracking_from_link('https://youtu.be/abc123?si=TRACKING') == \
            'https://youtube.com/watch?v=abc123'

    def test_the_timestamp_is_preserved_and_renamed(self, app):
        """t= on youtu.be becomes start= on youtube.com. Mutation that fails
        this: deleting the `.replace('t=', 'start=')`."""
        assert remove_tracking_from_link('https://youtu.be/abc123?t=42') == \
            'https://youtube.com/watch?v=abc123&start=42'

    def test_a_timestamp_alongside_tracking_keeps_only_the_timestamp(self, app):
        assert remove_tracking_from_link('https://youtu.be/abc123?si=X&t=42') == \
            'https://youtube.com/watch?v=abc123&start=42'


class TestEverythingElsePassesThrough:
    """Mutation that fails these: removing the `else: return url` arm, or
    widening the netloc test so it matches every host."""

    def test_a_full_youtube_url_is_untouched(self, app):
        url = 'https://www.youtube.com/watch?v=abc123&si=TRACKING'
        assert remove_tracking_from_link(url) == url

    def test_an_unrelated_host_is_untouched(self, app):
        url = 'https://example.com/a?utm_source=newsletter'
        assert remove_tracking_from_link(url) == url

    def test_a_host_merely_containing_youtu_be_is_untouched(self, app):
        """netloc equality, not substring. Mutation that fails this: changing
        `==` to `in`."""
        url = 'https://notyoutu.be/abc123'
        assert remove_tracking_from_link(url) == url
