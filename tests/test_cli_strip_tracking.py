"""app.cli.strip_tracking, which every item link `flask rss-feeds` imports goes through before make_post.

A feed item's link is the feed's to write. furl raises ValueError on an unterminated IPv6 bracket, a port that is
not a number or a host it will not accept, and strip_tracking runs outside the per-item try in the import loop, so
one such link aborted the whole feed's run -- every run, since the item was never recorded. A link furl cannot read
has no query furl can strip, so it comes back unchanged and make_post refuses it inside the per-item handling.
"""
import pytest

from app.cli import strip_tracking


def test_tracking_parameters_are_removed_and_the_rest_kept():
    assert strip_tracking('https://e.example/a?utm_source=x&id=2&at_medium=y&traffic_source=z') == (
        'https://e.example/a?id=2')


@pytest.mark.parametrize('url', [None, ''])
def test_no_link_is_the_empty_string(url):
    assert strip_tracking(url) == ''


@pytest.mark.parametrize('url', ['https://[x/?utm_a=1', 'https://e.example:port/?utm_a=1', 'https://%zz/?utm_a=1'])
def test_a_link_furl_cannot_read_comes_back_unchanged(url):
    assert strip_tracking(url) == url
