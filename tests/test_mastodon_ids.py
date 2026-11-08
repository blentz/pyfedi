"""Status ids (plan W1.2): a post and a comment are both statuses, from two tables with their own id sequences, so a
status id is a snowflake: the second it was posted, above one bit for the table and the whole row id. Ids grow with
time, as Tusky assumes (it orders its home timeline by id), and decode to exactly one row, whatever time a remote
server claims for its posts."""
from datetime import datetime, timedelta
from itertools import count

import pytest

from app import db
from app.mastodon import ids
from tests.factories import make_community, make_post, make_post_reply, make_user

_SERIAL = count()


@pytest.fixture
def world(app, api_baseline):
    author = make_user(None, 'snowflaker', local=True)
    community = make_community('snowflakes')
    return {'author': author, 'community': community}


def _post(world, when):
    post = make_post(world['community'], world['author'], ap_id=f'https://test.piefed.local/post/{next(_SERIAL)}')
    post.posted_at = when
    db.session.commit()
    return post


def _reply(world, post, when):
    reply = make_post_reply(post, world['author'])
    reply.posted_at = when
    db.session.commit()
    return reply


def test_a_post_and_a_comment_differ_and_decode_back(world):
    when = datetime(2026, 11, 8, 12, 0, 0, 123456)
    post = _post(world, when)
    reply = _reply(world, post, when)
    assert ids.status_id(post) != ids.status_id(reply)
    assert ids.find_status(ids.status_id(post)) is post
    assert ids.find_status(ids.status_id(reply)) is reply


def test_rows_claiming_the_same_moment_still_have_their_own_ids(world):
    """A remote server chooses `published`: many posts claiming one instant must not share an id."""
    when = datetime(2026, 11, 8, 12, 0, 0)
    posts = [_post(world, when) for _ in range(3)]
    values = [ids.status_id(post) for post in posts]
    assert len(set(values)) == 3
    assert [ids.find_status(value) for value in values] == posts


def test_ids_grow_with_the_time_posted_whatever_the_table(world):
    start = datetime(2026, 11, 8, 12, 0, 0)
    older_post = _post(world, start)
    newer_reply = _reply(world, older_post, start + timedelta(seconds=1))
    newest_post = _post(world, start + timedelta(seconds=5))
    values = [int(ids.status_id(row)) for row in (older_post, newer_reply, newest_post)]
    assert values == sorted(values) and len(set(values)) == 3


@pytest.mark.parametrize('when', [datetime(1990, 1, 1), datetime(2026, 11, 8), datetime(2200, 1, 1)],
                         ids=['before-2000', 'now', 'far-future'])
def test_an_id_is_a_numeric_string_that_fits_63_bits_whatever_the_claimed_time(world, when):
    post = _post(world, when)
    status = ids.status_id(post)
    assert status.isdigit() and 0 < int(status) < 2 ** 63
    assert ids.find_status(status) is post


@pytest.mark.parametrize('raw', ['', 'abc', '-5', '0', '1' * 40, '12345', str(1 << 40)],
                         ids=['empty', 'letters', 'negative', 'zero', 'huge', 'nothing-there', 'no-row-id'])
def test_an_id_naming_nothing_finds_nothing(world, raw):
    assert ids.find_status(raw) is None


def test_a_deleted_row_is_still_found_for_the_caller_to_judge(world):
    post = _post(world, datetime(2026, 11, 8))
    post.deleted = True
    db.session.commit()
    assert ids.find_status(ids.status_id(post)) is post


def test_a_bound_for_paging_is_the_second_an_id_names(world):
    """max_id and since_id need not name a row we have: an app may hold one from a page since deleted."""
    status = ids.status_id(_post(world, datetime(2026, 11, 8, 12, 0, 0, 5000)))
    assert ids.moment(status) == datetime(2026, 11, 8, 12, 0, 0)
    assert ids.moment('abc') is None
