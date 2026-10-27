"""A feed list costs a fixed number of queries, however many feeds it lists (plan F4, final review M9).

On a site with an onion every storefront hides something, so the community count of each listed feed and the owner of
each menu feed must not be read one feed at a time."""
from contextlib import contextmanager

import pytest
from cachelib import SimpleCache
from sqlalchemy import event

from app import cache, db
from app.feed_domains import shown_community_count
from app.local_only import hides_anything_here, shown_here
from app.models import Feed, User
from tests.factories import make_feed_item
from tests.three_domains import FIRST, ONION, SHOP, request_on, three_domains, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture(autouse=True)
def fresh_caches(app, monkeypatch):
    """The suite's cache is a NullCache, which would read the domain rows again on every call: use one that keeps."""
    monkeypatch.setitem(app.extensions['cache'], cache, SimpleCache())


@contextmanager
def counted():
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db.engine, 'before_cursor_execute', record)
    try:
        yield statements
    finally:
        event.remove(db.engine, 'before_cursor_execute', record)


def feeds_of(d, how_many):
    """`how_many` public first-domain feeds, each with its own owner, holding a first-domain and an onion community."""
    shown, hidden = d.local_community('shown', d.first), d.local_community('hidden', d.onion)
    feeds = []
    for number in range(how_many):
        feed = d.local_feed(f'feed{number}', d.first, d.account(f'owner{number}', d.first))
        for community in (shown, hidden):
            make_feed_item(feed, community)
        feed.num_communities = 2
        feeds.append(feed)
    db.session.commit()
    return feeds


def listed(app, d, how_many):
    """The feeds as a list page holds them: loaded in one query, their owners not loaded."""
    feeds_of(d, how_many)
    db.session.expire_all()
    feeds = Feed.query.order_by(Feed.id).all()
    for row in list(db.session.identity_map.values()):
        if isinstance(row, User):
            db.session.expire(row)
    return feeds


def listed_again(feeds):
    """The same feeds with their owners unloaded again."""
    for row in list(db.session.identity_map.values()):
        if isinstance(row, User):
            db.session.expire(row)
    return feeds


def test_the_shown_community_count_of_a_list_is_one_query(app, three_domains):  # noqa: F811
    feeds = listed(app, three_domains, 5)
    with request_on(app, three_domains.first):
        hides_anything_here()   # warm: the domain rows are cached
        with counted() as statements:
            assert [shown_community_count(feed) for feed in feeds] == [1] * 5   # the onion one is not counted
    assert len(statements) <= 1


def test_shown_here_over_a_menu_does_not_load_each_owner_alone(app, three_domains):  # noqa: F811
    feeds = listed(app, three_domains, 5)
    with request_on(app, three_domains.first):
        shown_here(feeds)   # warm: the domain rows are cached
        feeds = listed_again(feeds)
        with counted() as few:
            assert shown_here(feeds[:1]) == feeds[:1]
        feeds = listed_again(feeds)
        with counted() as many:
            assert shown_here(feeds) == feeds
    assert len(many) == len(few) and len(many) <= 2, [s[:60] + s[-60:] for s in many]
