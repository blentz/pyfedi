"""Small helpers in app/utils.py that v1.8.10 added or changed."""
from types import SimpleNamespace

import pytest

from app import cache, db
from app.models import Emoji, utcnow
from app.utils import (calculate_rss_cache_timeout, get_emoji_replacements, joined_server_views, set_setting,
                       sql_file)


def test_a_sql_file_name_climbing_out_of_its_folder_reads_nothing(app):
    assert sql_file('../config') == ''
    assert 'SELECT' in sql_file('user_votes_cast').upper()


@pytest.mark.parametrize('emoji_filter, expected', [
    ('', {':ok:', ':bad:', ':worse:'}),
    ('bad.example\n  \nworse.example', {':ok:'}),
    ('\n   \n', {':ok:', ':bad:', ':worse:'}),   # blank lines filter nothing
])
def test_the_emoji_filter_leaves_out_emoji_from_the_urls_it_names(app, db_session, emoji_filter, expected):
    db.session.add_all([Emoji(token=':OK:', url='https://cdn.example/ok.png'),
                        Emoji(token=':bad:', url='https://bad.example/bad.png'),
                        Emoji(token=':worse:', url='https://worse.example/w.png'),
                        Emoji(token=None, url='https://cdn.example/none.png')])
    db.session.commit()
    set_setting('emoji_filter', emoji_filter)
    cache.delete_memoized(get_emoji_replacements)

    assert set(get_emoji_replacements()) == expected


def test_nobody_has_joined_no_server_views(app):
    cache.delete_memoized(joined_server_views)
    assert joined_server_views(None) == set()


def test_a_feed_with_one_dated_post_is_cached_an_hour(app):
    posts = [SimpleNamespace(created_at=utcnow()), SimpleNamespace(created_at=None)]
    assert calculate_rss_cache_timeout(posts) == 3600
