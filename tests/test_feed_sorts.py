"""Coverage for the three sort chains in app/utils.py's feed functions.

`get_deduped_post_ids` (:3919-3951), `post_ids_to_models` (:3966-3978) and
`instance_sticky_posts` (:3992-4004) each carry their own `elif sort ==` chain
dispatching to a different ORDER BY. Seven sort values apply to all three: '',
'hot', 'scaled', 'top', 'new', 'old', 'active'. (`top_*` windows -- 'top_1h',
'top_6h', etc -- are a separate task's and land in this same file behind their
own, distinctly-prefixed classes.)

Votes alone do not move ordering. Production recalculates Post.score and
Post.ranking SYNCHRONOUSLY inside Post.vote() (app/models.py:2601-2716), not on a
schedule -- but `tests/factories.py`'s `make_post_vote` is a bare PostVote insert
that does not call vote() and so does not touch those columns (see its own
docstring). Every ordering test below therefore sets Post.ranking,
Post.ranking_scaled, Post.score, Post.posted_at, Post.last_active and
Post.up_votes/down_votes DIRECTLY, to values whose correct order differs from
insertion order -- so a passing assertion means the sort branch under test
actually ordered the rows, not that it happened to preserve insertion order.

## The 'top' divergence

`post_ids_to_models` orders `top` by `Post.score` (app/utils.py:3971), and
`get_deduped_post_ids`'s raw SQL agrees (`ORDER BY p.score DESC`, :3928).
`instance_sticky_posts` orders `top` by `Post.up_votes - Post.down_votes`
(:3997) instead. These are two implementations of one concept and they do not
provably agree:

Under this suite's config (SPICY_UNDER_10/30/60 all default to 1.0 --
config.py:81-83, unset in .env.test) they happen to coincide for votes cast
through Post.vote(): an upvote adds `spicy_effect` to score, and `spicy_effect
== effect` (1.0) exactly when the multiplier is 1.0, so score tracks
up_votes - down_votes losslessly through vote()'s own add, remove and reversal
paths (app/models.py:2641-2689).

They stop coinciding the moment an instance sets a non-default SPICY_UNDER_* --
an intentional, admin-facing knob; the comment right there says so: "Make 'hot'
sort more spicy by amplifying the effect of early upvotes" (app/models.py:2669).
From that point on, ADDING a vote moves score by the amplified `spicy_effect`
while up_votes moves by exactly 1, and REMOVING a vote always undoes score by
the vote's stored `effect` -- always +-1, never the amplification
(app/models.py:2642, 2656) -- so the resulting drift between score and
up_votes - down_votes is permanent, surviving even after every vote on the post
is later removed. `Post.score` is also written by two other paths that do not
run through vote() at all: the auto-upvote on post creation
(app/shared/post.py:213) and the PeerTube vote-count sync
(app/activitypub/util.py:2999) -- both happen to set score == up_votes -
down_votes by construction, but neither is vote()'s incremental path, so
neither closes the drift vote() introduces once opened.

`TestTopOrderingDivergesBetweenImplementations` below demonstrates the
consequence directly: with score and up_votes - down_votes disagreeing on which
post leads, `post_ids_to_models('top')` and `instance_sticky_posts('top')`
return the SAME two Post rows in OPPOSITE order. This is reported, not
reconciled -- see task-2-report.md.
"""

import uuid
from datetime import timedelta

import pytest
from flask_login import login_user

from app import db
from app.models import utcnow
from app.utils import get_deduped_post_ids, instance_sticky_posts, post_ids_to_models
from tests.factories import make_community, make_instance, make_post, make_user

SORTS = ['', 'hot', 'scaled', 'top', 'new', 'old', 'active']

# Fields every seeded post gets unless overridden, chosen so no sort branch's own
# WHERE clause filters a post out by accident: reply_count > 0 ('active'),
# ranking_scaled not null and from_bot false ('scaled'), posted_at recent enough
# to clear 'top's 24h cutoff in get_deduped_post_ids's raw SQL.
BASE_FIELDS = dict(reply_count=1, from_bot=False, ranking=0.0, ranking_scaled=0.0,
                   score=0, up_votes=0, down_votes=0)


def _seed_post(community, author, ap_id, **overrides):
    """A post with every feed-ordering field set directly, bypassing vote()'s
    (or any scheduled job's) recalculation entirely, so the seeded value is
    exactly what the calling test names."""
    now = utcnow()
    post = make_post(community, author, ap_id)
    fields = dict(BASE_FIELDS, posted_at=now, last_active=now)
    fields.update(overrides)
    for name, value in fields.items():
        setattr(post, name, value)
    db.session.commit()
    return post


def _setup(prefix, with_viewer=False):
    """make_instance before make_user(None, ...) for the instance_id=1 foreign
    key, then a user before make_community for the user_id=1 foreign key --
    same ordering tests/test_subscribed_feed_microblogs.py relies on."""
    make_instance(f'{prefix}.example')
    author = make_user(None, f'{prefix}author', local=True)
    viewer = make_user(None, f'{prefix}viewer', local=True) if with_viewer else None
    community = make_community(f'{prefix}community')
    return (author, viewer, community) if with_viewer else (author, community)


def _order_values(sort):
    """(field, value_for_post_a, value_for_post_b) such that, with post_a seeded
    before post_b (so ascending-id / insertion order is [a, b]), the correct
    order for `sort` is always [b, a] -- the opposite of insertion order. That
    makes a single assertion shape (`ids == [b.id, a.id]`) prove the branch
    really sorted, for every sort alike."""
    now = utcnow()
    earlier = now - timedelta(hours=2)
    return {
        '': ('ranking', 1.0, 2.0),          # DESC
        'hot': ('ranking', 1.0, 2.0),       # DESC
        'scaled': ('ranking_scaled', 1.0, 2.0),  # DESC
        'top': ('score', 1, 2),             # DESC
        'new': ('posted_at', earlier, now),      # DESC: more recent first
        'old': ('posted_at', now, earlier),      # ASC: earlier first
        'active': ('last_active', earlier, now), # DESC: more recently active first
    }[sort]


# instance_sticky_posts' 'top' branch sorts by up_votes - down_votes, not score
# (see the module docstring) -- down_votes stays 0 (from BASE_FIELDS) on both
# posts so the difference tracks up_votes alone.
STICKY_TOP_FIELD = ('up_votes', 1, 2)


def feed_ids(app, viewer, sort, community):
    with app.test_request_context('/'):
        login_user(viewer)
        return get_deduped_post_ids(uuid.uuid4().hex, [community.id], sort)


class TestGetDedupedPostIdsSorts:
    """Ordering for get_deduped_post_ids' seven `sort` branches (app/utils.py:3919-3951)."""

    @pytest.mark.parametrize('sort', SORTS)
    def test_returns_every_requested_post(self, app, db_session, redis_double, sort):
        """No post may be dropped, whatever the ordering -- fails if a sort
        branch's WHERE clause (e.g. scaled's ranking_scaled/from_bot filter)
        leaked onto a branch that should only order."""
        author, viewer, community = _setup('getall', with_viewer=True)
        posts = [_seed_post(community, author, f'https://getall.example/{i}') for i in range(3)]

        ids = feed_ids(app, viewer, sort, community)

        assert set(ids) == {p.id for p in posts}

    @pytest.mark.parametrize('sort', SORTS)
    def test_orders_by_the_sort_specific_field(self, app, db_session, redis_double, sort):
        author, viewer, community = _setup('getorder', with_viewer=True)
        field, value_a, value_b = _order_values(sort)
        post_a = _seed_post(community, author, 'https://getorder.example/a', **{field: value_a})
        post_b = _seed_post(community, author, 'https://getorder.example/b', **{field: value_b})

        ids = feed_ids(app, viewer, sort, community)

        assert ids == [post_b.id, post_a.id]


class TestGetDedupedPostIdsScaledAlsoFilters:
    """get_deduped_post_ids' 'scaled' branch appends its own WHERE clause
    (app/utils.py:3924): `p.ranking_scaled is not null AND p.from_bot is false`.
    That is a filter, not just an ordering -- both directions, on both columns.
    """

    def test_a_post_with_null_ranking_scaled_is_absent(self, app, db_session, redis_double):
        author, viewer, community = _setup('scalednull', with_viewer=True)
        excluded = _seed_post(community, author, 'https://scalednull.example/1', ranking_scaled=None)
        included = _seed_post(community, author, 'https://scalednull.example/2', ranking_scaled=1.0)

        ids = feed_ids(app, viewer, 'scaled', community)

        assert excluded.id not in ids
        assert included.id in ids

    def test_a_post_from_a_bot_is_absent(self, app, db_session, redis_double):
        author, viewer, community = _setup('scaledbot', with_viewer=True)
        excluded = _seed_post(community, author, 'https://scaledbot.example/1', from_bot=True)
        included = _seed_post(community, author, 'https://scaledbot.example/2', from_bot=False)

        ids = feed_ids(app, viewer, 'scaled', community)

        assert excluded.id not in ids
        assert included.id in ids


class TestPostIdsToModelsSorts:
    """Ordering for post_ids_to_models' seven `sort` branches (app/utils.py:3966-3978)."""

    @pytest.mark.parametrize('sort', SORTS)
    def test_returns_every_requested_post(self, db_session, sort):
        author, community = _setup('modelsall')
        posts = [_seed_post(community, author, f'https://modelsall.example/{i}') for i in range(3)]

        result = post_ids_to_models([p.id for p in posts], sort).all()

        assert {p.id for p in result} == {p.id for p in posts}

    @pytest.mark.parametrize('sort', SORTS)
    def test_orders_by_the_sort_specific_field(self, db_session, sort):
        author, community = _setup('modelsorder')
        field, value_a, value_b = _order_values(sort)
        post_a = _seed_post(community, author, 'https://modelsorder.example/a', **{field: value_a})
        post_b = _seed_post(community, author, 'https://modelsorder.example/b', **{field: value_b})

        result = post_ids_to_models([post_a.id, post_b.id], sort).all()

        assert [p.id for p in result] == [post_b.id, post_a.id]


class TestInstanceStickyPostsSorts:
    """Ordering for instance_sticky_posts' seven `sort` branches (app/utils.py:3992-4004).
    'top' here orders by Post.up_votes - Post.down_votes, NOT Post.score -- see
    the module docstring on why that diverges from the other two functions' 'top'.
    """

    @pytest.mark.parametrize('sort', SORTS)
    def test_returns_every_requested_post(self, db_session, sort):
        author, community = _setup('stickyall')
        posts = [_seed_post(community, author, f'https://stickyall.example/{i}', instance_sticky=True)
                for i in range(3)]

        result = instance_sticky_posts(sort)

        assert {p.id for p in result} == {p.id for p in posts}

    @pytest.mark.parametrize('sort', SORTS)
    def test_orders_by_the_sort_specific_field(self, db_session, sort):
        author, community = _setup('stickyorder')
        field, value_a, value_b = STICKY_TOP_FIELD if sort == 'top' else _order_values(sort)
        post_a = _seed_post(community, author, 'https://stickyorder.example/a', instance_sticky=True,
                            **{field: value_a})
        post_b = _seed_post(community, author, 'https://stickyorder.example/b', instance_sticky=True,
                            **{field: value_b})

        result = instance_sticky_posts(sort)

        assert [p.id for p in result] == [post_b.id, post_a.id]


class TestTopOrderingDivergesBetweenImplementations:
    """post_ids_to_models orders 'top' by Post.score; instance_sticky_posts orders
    it by Post.up_votes - Post.down_votes (app/utils.py:3971 vs :3997). See the
    module docstring for why and when those disagree in a real deployment. This
    test does not reproduce the SPICY_UNDER_* mechanism that opens the drift; it
    fixes score and up_votes/down_votes independently on the same two Post rows
    to demonstrate the reportable fact -- that the two 'top' orderings are not
    interchangeable -- directly.
    """

    def test_the_two_top_orderings_disagree_for_the_same_posts(self, db_session):
        author, community = _setup('diverge')
        # score ranks x above y; the raw vote difference ranks y above x.
        post_x = _seed_post(community, author, 'https://diverge.example/x', instance_sticky=True,
                            score=10, up_votes=1, down_votes=1)
        post_y = _seed_post(community, author, 'https://diverge.example/y', instance_sticky=True,
                            score=1, up_votes=5, down_votes=0)

        by_score = post_ids_to_models([post_x.id, post_y.id], 'top').all()
        by_votes = instance_sticky_posts('top')

        assert [p.id for p in by_score] == [post_x.id, post_y.id]
        assert [p.id for p in by_votes] == [post_y.id, post_x.id]


# The eight top_* cutoff windows get_deduped_post_ids applies (app/utils.py:3925-3944).
# Each recognised value sets params['top_cutoff'] to utcnow() minus its own window and
# appends 'p.posted_at > :top_cutoff ' to the WHERE clause (:3926-3927); top_all is the
# exception and appends no cutoff clause at all. TOP_WINDOWS pairs each recognised,
# time-bounded value with its production window so a single parametrized test proves
# every boundary rather than one broad test that would pass whether or not any
# individual window's number were wrong.
TOP_WINDOWS = [
    ('top_1h', timedelta(hours=1)),
    ('top_6h', timedelta(hours=6)),
    ('top_12h', timedelta(hours=12)),
    ('top', timedelta(hours=24)),
    ('top_1w', timedelta(days=7)),
    ('top_1m', timedelta(days=28)),
    ('top_1y', timedelta(days=365)),
]

# utcnow() is evaluated a second time inside get_deduped_post_ids, after this module's
# utcnow() call below has already fixed each post's posted_at -- so the production
# cutoff is never exactly "window ago" from the moment a test computed it, only close
# to it. A post seeded exactly at the cutoff is therefore not reproducible: whether it
# lands a hair inside or outside the strict '>' depends on scheduler timing between the
# two calls. BOUNDARY_MARGIN seeds a few seconds either side instead, which is what the
# task brief asks for -- close enough to the boundary that an off-by-one in any window's
# timedelta (hours vs days, or the wrong number of either) would flip the result, while
# leaving a margin no test process is slow enough to consume between seeding a post and
# the query that reads it back.
BOUNDARY_MARGIN = timedelta(seconds=30)


class TestGetDedupedPostIdsTopWindows:
    """Boundary coverage for get_deduped_post_ids' seven time-bounded top_* cutoffs.
    For each window, one post is seeded a few seconds inside it (posted_at more recent
    than the cutoff) and one a few seconds outside (posted_at older than the cutoff).
    Only the inside post should come back -- the filter is strictly '>', so a post
    exactly at the cutoff would be excluded, but that exact boundary is not
    reproducible (see BOUNDARY_MARGIN above) and is not what these tests probe.

    Each of these tests fails if app/utils.py's timedelta for its own `sort` value
    changes to any other duration -- shorter OR longer -- since that would move the
    cutoff away from where the two seeded posts straddle it. Step 3 below (run as part
    of this task, not committed as a test) confirmed this directly for top_1w: swapping
    its timedelta(days=7) for timedelta(days=1) failed only this window's two tests.
    """

    @pytest.mark.parametrize('sort,window', TOP_WINDOWS)
    def test_post_just_inside_the_window_is_returned(self, app, db_session, redis_double, sort, window):
        author, viewer, community = _setup(f'topin{sort.replace("_", "")}', with_viewer=True)
        inside = _seed_post(community, author, f'https://topin{sort}.example/inside',
                            posted_at=utcnow() - window + BOUNDARY_MARGIN)

        ids = feed_ids(app, viewer, sort, community)

        assert inside.id in ids

    @pytest.mark.parametrize('sort,window', TOP_WINDOWS)
    def test_post_just_outside_the_window_is_absent(self, app, db_session, redis_double, sort, window):
        author, viewer, community = _setup(f'topout{sort.replace("_", "")}', with_viewer=True)
        outside = _seed_post(community, author, f'https://topout{sort}.example/outside',
                             posted_at=utcnow() - window - BOUNDARY_MARGIN)

        ids = feed_ids(app, viewer, sort, community)

        assert outside.id not in ids


class TestGetDedupedPostIdsTopAll:
    """top_all appends no cutoff clause at all (app/utils.py:3926), rather than a very
    large one -- a single window value could never tell those two apart, since any
    window long enough to be indistinguishable from "no cutoff" in a fast-running test
    would itself be suspicious. The only test that actually discriminates "no cutoff"
    from "a large cutoff" is a post old enough to fall outside every real window,
    checked against both top_all (must be present) and top_1y (must be absent) in the
    same test. This fails if top_all is ever changed to append its own top_cutoff
    clause, however large.
    """

    def test_a_very_old_post_is_returned_by_top_all_but_not_top_1y(self, app, db_session, redis_double):
        author, viewer, community = _setup('topallold', with_viewer=True)
        very_old = _seed_post(community, author, 'https://topall.example/ancient',
                              posted_at=utcnow() - timedelta(days=1000))

        all_ids = feed_ids(app, viewer, 'top_all', community)
        year_ids = feed_ids(app, viewer, 'top_1y', community)

        assert very_old.id in all_ids
        assert very_old.id not in year_ids


class TestGetDedupedPostIdsTopFallthrough:
    """Any sort value that starts with 'top' but matches none of the seven named
    branches nor 'top_all' falls through to `elif sort != 'top_all':` (app/utils.py:
    3943-3944), which silently applies the same 24-hour cutoff as 'top' -- it neither
    raises nor falls back to top_all's "no cutoff" behaviour. A caller that mistypes a
    sort value (e.g. 'top_1d' instead of 'top' or 'top_1w' instead of the real name)
    gets a day's worth of posts with no error and no indication anything is wrong.

    REPORTED, NOT FIXED, per this task's brief. This test pins the current fallthrough
    behaviour (24-hour cutoff) rather than the presumably-intended behaviour, so it
    would need to be rewritten -- not just left failing -- if that fallthrough is ever
    changed. It fails if the fallthrough's timedelta stops being 24 hours, or if
    'top_nonsense' starts being rejected or treated as top_all's unbounded case.
    """

    def test_an_unrecognised_top_value_gets_the_24_hour_fallback_cutoff(self, app, db_session, redis_double):
        author, viewer, community = _setup('topfall', with_viewer=True)
        now = utcnow()
        inside = _seed_post(community, author, 'https://topfall.example/inside',
                            posted_at=now - timedelta(hours=24) + BOUNDARY_MARGIN)
        outside = _seed_post(community, author, 'https://topfall.example/outside',
                             posted_at=now - timedelta(hours=24) - BOUNDARY_MARGIN)

        ids = feed_ids(app, viewer, 'top_nonsense', community)

        assert inside.id in ids
        assert outside.id not in ids
