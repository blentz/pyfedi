"""Round 240: model helpers with no rows.

`app/models.py` is the lowest-covered module left. This round takes the small helpers whose
absence is easiest to misread -- a one-line accessor looks too simple to test right up until
it returns the wrong thing on every page that calls it.

    RssFeed / RssFeedItem   `delete_dependencies` -- deleting an RSS feed deletes the POSTS
                            it created, under a per-post redis lock
    _large_community_subscribers   an hour-cached percentile query over every community
    Domain.blocked_by / type_to_class
    Filter.keywords_string
    Site.active_now and the four `all_active_*` counts, which are raw SQL
    Feed.display_name / link / local_url for a remote feed

The RSS rows matter most: `RssFeedItem.delete_dependencies` deletes a Post outright, and it
is reached from `RssFeed.delete_dependencies`, so removing a feed removes everything it ever
posted. The Site counts matter second: each is a separate hand-written SQL string, and four
of the five share a shape that is easy to copy with one condition missing.
"""
import contextlib
from datetime import timedelta
from types import SimpleNamespace

import pytest
from flask import g

from app import cache, db
from app.models import (Community, Domain, DomainBlock, Feed, Filter, Post, RssFeed,
                        RssFeedItem, Site, User, utcnow)
from tests.factories import make_community, make_instance, make_post, make_user


class _LockOnlyRedis:
    """`app.redis_client` stands in for only `.lock(...)`. This environment's fakeredis has
    no Lua scripting, which redis-py's real `Lock.release()` needs.
    """

    def lock(self, *args, **kwargs):
        return contextlib.nullcontext()


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    return SimpleNamespace(app=app, site=site, baseline=api_baseline)


# --------------------------------------------------------------------------
# Deleting an RSS feed
# --------------------------------------------------------------------------


class TestDeletingAnRssFeed:
    """A `RssFeed` polls a URL and creates a Post per item. Deleting the feed is therefore
    not a bookkeeping operation: `RssFeedItem.delete_dependencies` deletes the Post.
    """

    @pytest.fixture
    def seeded(self, env, monkeypatch):
        monkeypatch.setattr('app.redis_client', _LockOnlyRedis())
        community = make_community('feedland')
        author = make_user(env.baseline.instance_local, 'feedbot', local=True)
        db.session.commit()
        feed = RssFeed(url='https://news.example/rss', title='A Feed',
                       community_id=community.id, check_frequency=60)
        db.session.add(feed)
        db.session.commit()
        first = make_post(community, author, ap_id='https://test.piefed.local/r/1',
                          title='an imported story')
        second = make_post(community, author, ap_id='https://test.piefed.local/r/2',
                           title='another imported story')
        db.session.add_all([
            RssFeedItem(feed_id=feed.id, guid='guid-1', post_id=first.id),
            RssFeedItem(feed_id=feed.id, guid='guid-2', post_id=second.id),
        ])
        db.session.commit()
        env.feed = feed
        env.posts = [first, second]
        env.community = community
        env.author = author
        return env

    def test_deleting_the_feed_deletes_every_post_it_created(self, seeded):
        post_ids = [post.id for post in seeded.posts]

        seeded.feed.delete_dependencies()

        for post_id in post_ids:
            assert db.session.get(Post, post_id) is None

    def test_an_item_with_no_post_is_harmless(self, seeded):
        """`if post:` -- the importer records an item with `post_id=None` when it decides
        not to create a post (`app/cli.py`, twice), and `None.delete_dependencies()` would
        be an `AttributeError` in the middle of the loop, leaving the feed's remaining posts
        behind.

        `post_id=None` is the ONLY shape that reaches this guard: a post_id naming a row
        that no longer exists violates the foreign key, so "the post was deleted first" is
        not a state the database allows (fact 781). D1422 added the `if self.post_id` test in
        front of the lookup, because `db.session.get(Post, None)` emits an SAWarning about a
        fully NULL primary key.
        """
        orphan = RssFeedItem(feed_id=seeded.feed.id, guid='guid-3', post_id=None)
        db.session.add(orphan)
        db.session.commit()
        post_ids = [post.id for post in seeded.posts]

        seeded.feed.delete_dependencies()

        for existing in post_ids:
            assert db.session.get(Post, existing) is None

    def test_one_item_deletes_only_its_own_post(self, seeded):
        """`RssFeedItem.delete_dependencies` on its own. The item names one post, and a
        helper that deleted by feed rather than by item would take both."""
        item = RssFeedItem.query.filter_by(guid='guid-1').one()
        kept = seeded.posts[1].id

        item.delete_dependencies()

        assert db.session.get(Post, seeded.posts[0].id) is None
        assert db.session.get(Post, kept) is not None


# --------------------------------------------------------------------------
# _large_community_subscribers
# --------------------------------------------------------------------------


class TestTheLargeCommunityThreshold:
    """The average subscriber count of the top 15% of communities, cached for an hour. It
    is what decides whether a community counts as 'large' anywhere it is used, so the two
    things worth pinning are which communities it counts and that the cache is consulted.
    """

    @pytest.fixture
    def real_cache(self, app, monkeypatch):
        """`CACHE_TYPE = 'NullCache'` makes every read miss and every write a no-op, so a
        "served from cache" assertion passes whether or not anything was cached. Flask-Caching
        resolves its backend through `app.extensions['cache'][cache]` on each access, so
        replacing that entry is enough and monkeypatch restores it.
        """
        from cachelib import SimpleCache

        monkeypatch.setitem(app.extensions['cache'], cache, SimpleCache())
        return cache

    @pytest.fixture
    def seeded(self, env, real_cache):
        cache.delete('large_community_subscribers')
        for n, count in enumerate([1000, 500, 100, 50, 10, 5, 1]):
            community = make_community(f'sized{n}')
            community.subscriptions_count = count
        db.session.commit()
        yield env
        cache.delete('large_community_subscribers')

    def _value(self):
        from app.models import _large_community_subscribers

        return _large_community_subscribers()

    def test_it_averages_only_the_biggest_communities(self, seeded):
        """15% of seven communities is one, so the answer is the largest one's own count --
        which is also the assertion that says the percentile filter is applied at all: the
        average over all seven is far lower."""
        assert float(self._value()) == pytest.approx(1000.0)

    def test_a_banned_community_is_not_counted(self, seeded):
        """`WHERE banned IS false`. A banned community's subscriber count is still in the
        table, and counting it would move a site-wide threshold using a community nobody
        can reach."""
        biggest = Community.query.filter_by(name='sized0').one()
        biggest.banned = True
        db.session.commit()
        cache.delete('large_community_subscribers')

        assert float(self._value()) == pytest.approx(500.0)

    def test_a_community_with_no_subscribers_is_not_counted(self, seeded):
        """`AND subscriptions_count > 0`. Empty communities would otherwise drag the
        average of a percentile computed over them."""
        for n in range(20):
            empty = make_community(f'empty{n}')
            empty.subscriptions_count = 0
        db.session.commit()
        cache.delete('large_community_subscribers')

        assert float(self._value()) == pytest.approx(1000.0)

    def test_the_answer_is_cached(self, seeded):
        """`cache.get` / `cache.set`. The query is a window function over every community,
        so it is not one to run per request -- and the row proves the cache is READ, not
        merely written, by changing the data behind it."""
        first = float(self._value())

        biggest = Community.query.filter_by(name='sized0').one()
        biggest.subscriptions_count = 999999
        db.session.commit()

        assert float(self._value()) == pytest.approx(first)
        cache.delete('large_community_subscribers')
        assert float(self._value()) != pytest.approx(first)


# --------------------------------------------------------------------------
# Domain and Filter helpers
# --------------------------------------------------------------------------


class TestDomainHelpers:

    @pytest.fixture
    def seeded(self, env):
        domain = Domain(name='news.example', post_count=0)
        db.session.add(domain)
        db.session.commit()
        env.domain = domain
        env.reader = make_user(env.baseline.instance_local, 'domainreader', local=True)
        db.session.commit()
        return env

    def test_a_domain_is_blocked_only_for_the_user_who_blocked_it(self, seeded):
        """`blocked_by` is per user, so the row needs a second account: a helper that
        ignored `user_id` would answer True for everybody once anybody blocked it."""
        other = make_user(seeded.baseline.instance_local, 'otherreader', local=True)
        db.session.commit()
        db.session.add(DomainBlock(domain_id=seeded.domain.id, user_id=seeded.reader.id))
        db.session.commit()

        assert seeded.domain.blocked_by(seeded.reader) is True
        assert seeded.domain.blocked_by(other) is False

    @pytest.mark.parametrize('warning_type,expected', [
        (None, 'fe-warning red'),
        (0, 'fe-warning red'),
        (1, 'fe-context green'),
        (2, 'fe-recommended red'),
    ])
    def test_the_warning_icon_matches_the_warning_type(self, seeded, warning_type,
                                                       expected):
        """A domain's admin note can be a warning, context or a recommendation, and the
        class decides both the icon and the COLOUR -- green for context, red for the other
        two. `None` is grouped with 0 deliberately: the column is nullable and a domain
        created before the field existed must not render as a recommendation."""
        seeded.domain.warning_type = warning_type
        db.session.commit()

        assert seeded.domain.type_to_class() == expected

    def test_an_unknown_warning_type_renders_no_icon(self, seeded):
        """The trailing `return ''`. A value outside 0-2 -- from a future migration, or a
        hand-edited row -- gets no class rather than a broken one."""
        seeded.domain.warning_type = 99
        db.session.commit()

        assert seeded.domain.type_to_class() == ''


class TestAFiltersKeywords:
    """`Filter.keywords` is a newline-separated column; `keywords_string` is what the edit
    form shows. The round trip is newline-separated in the database, comma-separated on the
    page.
    """

    def _filter(self, env, keywords):
        user = make_user(env.baseline.instance_local, f'filterer{abs(hash(keywords))%1000}',
                         local=True)
        db.session.commit()
        row = Filter(title='a filter', user_id=user.id, keywords=keywords)
        db.session.add(row)
        db.session.commit()
        return row

    def test_newlines_become_commas(self, env):
        assert self._filter(env, 'spam\nscam\nnonsense').keywords_string() == \
            'spam, scam, nonsense'

    def test_surrounding_whitespace_is_stripped(self, env):
        """`kw.strip()`. A keyword with a trailing space would not match anything the
        filter is compared against, and the form is where somebody would notice."""
        assert self._filter(env, '  spam  \n scam ').keywords_string() == 'spam, scam'

    @pytest.mark.parametrize('stored', [None, ''])
    def test_no_keywords_is_an_empty_string(self, env, stored):
        """`None.split` is an `AttributeError`, so the first half of the guard is
        load-bearing.

        The second half, `or self.keywords == ''`, is an EQUIVALENT MUTANT: without it an
        empty string falls through to `''.split('\\n')` == `['']`, which strips to `['']`
        and joins to `''` -- the same answer. Removing it changes nothing observable. Both
        values are driven anyway, because the two reach the answer by different routes and
        a future edit to the join would separate them.
        """
        assert self._filter(env, stored).keywords_string() == ''


# --------------------------------------------------------------------------
# Site's activity counts
# --------------------------------------------------------------------------


class TestTheSiteActivityCounts:
    """Five hand-written SQL strings counting recently-seen accounts over five windows.
    `active_now` counts only local, verified, unbanned, undeleted accounts; the four
    `all_active_*` counts drop the local and verified conditions but keep banned and
    deleted. Each is its own string, so each can be wrong on its own.
    """

    @pytest.fixture
    def seeded(self, env):
        def account(name, **fields):
            user = make_user(env.baseline.instance_local, name, local=True)
            user.verified = True
            for key, value in fields.items():
                setattr(user, key, value)
            db.session.commit()
            return user

        # Every pre-existing account is pushed out of every window, so the counts below
        # are about the accounts this fixture names and nothing else.
        for user in User.query.all():
            user.last_seen = utcnow() - timedelta(days=400)
        db.session.commit()

        env.now = account('seenjustnow', last_seen=utcnow())
        env.yesterday = account('seenyesterday',
                                last_seen=utcnow() - timedelta(days=2))
        env.banned = account('seenbutbanned', last_seen=utcnow(), banned=True)
        env.deleted = account('seenbutdeleted', last_seen=utcnow(), deleted=True)
        env.unverified = account('seenbutunverified', last_seen=utcnow(),
                                 verified=False)
        peer = make_instance('countpeer.example')
        remote = make_user(peer, 'seenbutremote')
        remote.last_seen = utcnow()
        remote.verified = True
        db.session.commit()
        env.remote = remote
        return env

    def test_active_now_counts_only_local_verified_accounts(self, seeded):
        """One account qualifies: the banned, deleted, unverified and remote ones are each
        excluded by their own condition in the same WHERE clause."""
        assert seeded.site.active_now() == 1

    @pytest.mark.parametrize('method,expected', [
        ('all_active_daily', 3),
        ('all_active_weekly', 4),
        ('all_active_monthly', 4),
        ('all_active_6monthly', 4),
    ])
    def test_the_all_active_counts_widen_with_their_window(self, seeded, method,
                                                           expected):
        """The four share a shape and differ only in the interval, so they are asserted
        together against one set of accounts. Three were seen today -- the local verified
        one, the REMOTE one and the UNVERIFIED one, because neither `ap_id` nor `verified`
        is among these four queries' conditions, unlike `active_now` -- and the wider
        windows add the account seen two days ago. The banned and deleted accounts are in
        none of them."""
        assert getattr(seeded.site, method)() == expected

    def test_a_banned_or_deleted_account_is_in_none_of_them(self, seeded):
        """The two conditions all five strings share. Unbanning one moves every count by
        one, which is what says the condition is present in each."""
        before = [seeded.site.all_active_daily(), seeded.site.all_active_weekly()]
        seeded.banned.banned = False
        db.session.commit()
        after = [seeded.site.all_active_daily(), seeded.site.all_active_weekly()]

        assert after == [before[0] + 1, before[1] + 1]


# --------------------------------------------------------------------------
# Feed naming
# --------------------------------------------------------------------------


class TestHowAFeedNamesItself:

    @pytest.fixture
    def seeded(self, env):
        peer = make_instance('feedpeer.example')
        remote = Feed(name='remotefeed', title='Remote Feed', instance_id=peer.id,
                      ap_id='remotefeed@feedpeer.example',
                      ap_domain='feedpeer.example',
                      ap_profile_id='https://feedpeer.example/f/remotefeed',
                      ap_public_url='https://feedpeer.example/f/remotefeed')
        local = Feed(name='localfeed', title='Local Feed', instance_id=1,
                     ap_profile_id='https://test.piefed.local/f/localfeed',
                     ap_public_url='https://test.piefed.local/f/localfeed')
        db.session.add_all([remote, local])
        db.session.commit()
        env.remote = remote
        env.local = local
        return env

    def test_a_remote_feed_shows_its_host(self, seeded):
        """Two feeds on different instances can share a title, so a remote one is named
        `Title@host` -- and a local one is not, because the host would be noise."""
        assert seeded.remote.display_name() == 'Remote Feed@feedpeer.example'
        assert seeded.local.display_name() == 'Local Feed'

    def test_a_remote_feeds_link_is_its_handle_in_lower_case(self, seeded):
        """`link()` is what goes into a URL. Lower-casing it is what makes
        `/f/RemoteFeed@Host` and `/f/remotefeed@host` the same feed."""
        seeded.remote.ap_id = 'RemoteFeed@FeedPeer.Example'
        db.session.commit()

        assert seeded.remote.link() == 'remotefeed@feedpeer.example'
        assert seeded.local.link() == 'localfeed'

    def test_a_remote_feed_has_a_local_url_on_this_instance(self, seeded):
        """`local_url` is where a reader here browses a remote feed: this instance's own
        `/f/<handle>`, not the feed's home URL. A local feed's is its `ap_profile_id`,
        which already points here."""
        assert seeded.remote.local_url().startswith(
            seeded.app.config['SERVER_URL'] + '/f/')
        assert seeded.remote.local_url().endswith('remotefeed@feedpeer.example')
        assert seeded.local.local_url() == 'https://test.piefed.local/f/localfeed'
