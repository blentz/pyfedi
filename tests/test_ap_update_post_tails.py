"""The type-dispatch tails of `update_post_from_activity` -- the blocks that
run after the shared head has finished with title, language, tags and flair.

Entry is a direct call. The function takes an already-loaded `Post` and the
Update's dict, and does not fetch the object it is applying -- sub-project 14's
tests/test_ap_update_pair.py covers the shared head with no HTTP mock at all.
Only the tails that reach out on their own need one; the `Video` block below is
the first.

The function opens with `with redis_client.lock(f"lock:post:{post.id}", ...)`,
which the shared `redis_double` fixture cannot serve: fakeredis without lupa
has no Lua scripting, and redis-py's `Lock.release()` issues an EVALSHA. Every
test here takes `redis_lock_only_double` instead, whose `.lock()` is a
nullcontext.

The function commits on `db.session`, not on a separate `get_task_session()`.
`expire_on_commit` is at SQLAlchemy's default True: `app/__init__.py:81` builds
`SQLAlchemy(session_options={"autoflush": False}, ...)` and sets no
`expire_on_commit`. So a commit inside the function expires the objects a test
is holding and the next attribute access re-loads them. No explicit refresh is
needed; tests read `post.<column>` straight after the call.

`http_mock` is built with `assert_all_called=True` (tests/conftest.py:288-295),
so a test that registers a route its path never reaches FAILS. Every test below
registers exactly the routes its own scenario reaches, which is why the
"only likes" and "neither endpoint" cases register one route and none at all
rather than registering both and leaving one idle.

Every Update built here carries `name`. Without it the head takes the
`microblog_content_to_title` branch, which sets `post.microblog = True` and can
write `post.url` from an anchor in the body -- and `post.url` is the witness the
early-return test below depends on. `name` keeps the head on the titled path so
the tails are what is being measured.

THE IMAGE BOUNDARY, for the tasks that cover the Event and attachment tails.
Under this harness `make_image_sizes` EXECUTES rather than enqueues: Celery is
eager (tests/conftest.py:106-107), and `make_image_sizes` (app/activitypub/util.py:1724)
dispatches inline under `current_app.debug` and via `.apply_async()` otherwise
-- both reach `make_image_sizes_async`, which fetches `file.source_url`. Two
techniques exist in this suite and they are NOT interchangeable:

  (1) register the image's source_url in `http_mock` and serve a 404, so
      `make_image_sizes_async` stops at its `get_request` (whose bare `except:`
      swallows the failure -- tests/README.md:1091). Precedent:
      tests/test_ap_actor_json_person.py:904-905.
  (2) `set_setting('cache_remote_images_locally', False)`. Precedent:
      tests/test_event_post_type_survives_update.py:161-164.

Technique (2) only works for the Event tail: the setting gates the call at
`app/activitypub/util.py:3396-3397` and nothing gates the one in the url-change
arm at `:3504`, which runs whenever that arm builds an image. So this file
standardises on TECHNIQUE (1) -- it works at both call sites, and
`assert_all_called=True` turns the registered 404 into positive evidence that
the image path was entered. The `Video` cluster never reaches either call site:
it returns at `:3308-3309` before the Links section, and the block itself
creates no `File`. Nor does the `Question` cluster: its four returns at
`:3320`, `:3337`, `:3353` and `:3368` all precede the `Event` block at `:3372`.

Technique (2) appears exactly once, in
`TestEventBlock::test_the_banner_is_stored_when_remote_image_caching_is_off`,
and only because exercising `:3396` on its False side is what that test is for:
no route can be registered on a path that fetches nothing. Every other image
this file touches goes through technique (1). No third technique is introduced.
"""
import contextlib
from datetime import datetime

import httpx
import pytest

from app import db
from app.activitypub.util import update_post_from_activity
from app.constants import (POST_TYPE_ARTICLE, POST_TYPE_EVENT, POST_TYPE_IMAGE,
                           POST_TYPE_LINK, POST_TYPE_POLL, POST_TYPE_VIDEO)
from app.models import Event, File, PollChoice, PollChoiceVote
from app.utils import set_setting, utcnow
from tests.factories import (make_community, make_instance, make_poll,
                             make_poll_choice, make_post, make_site, make_user)

PEER = 'peer.example'


class _RedisLockOnlyDouble:
    """`app.redis_client` stand-in covering only `.lock(...)` as a context
    manager. See this module's docstring for why the shared `redis_double`
    cannot serve it.

    The third copy of this shape in the suite --
    tests/test_inbox_dispatch_votes.py:145-159 has the first and
    tests/test_ap_update_pair.py:46-58 the second -- and a local copy
    rather than a promotion to tests/conftest.py on purpose. The campaign's
    duplication family (D242/D262) is about duplicated production GUARDS, where
    one copy can be repaired and the other left wrong; this class has no
    condition to get wrong, and a broken lock double fails every test in its own
    file loudly rather than passing while testing less. Promoting it would also
    have to leave the two existing copies in place, since neither of those files
    is in this slice's scope, so it would not remove the duplication -- three
    definitions plus an import path. It would at least establish a canonical
    home for a fourth file to import, which is the real argument the other way;
    it is outweighed here because the thing being duplicated has no condition to
    get wrong, not because promotion is worthless.
    """

    def lock(self, *args, **kwargs):
        return contextlib.nullcontext()


@pytest.fixture
def redis_lock_only_double(monkeypatch):
    monkeypatch.setattr('app.redis_client', _RedisLockOnlyDouble())


def _seed_post(software='lemmy'):
    """A local community owned by user 1, a remote author on PEER, and one Post.

    `make_community` hardcodes `instance_id=1` and `user_id=1`, so an instance
    and a user are seeded first to occupy those ids -- the same pattern
    tests/test_inbox_dispatch_votes.py documents.

    `software` reaches `post.instance.software`, which the tag block's flair
    guard reads -- `app/activitypub/util.py:3259` is
    `if len(flair_tags) > 0 or (post.instance.software == 'piefed' or
    post.instance.software == 'pylova'):`. It is 'lemmy' here rather than
    `make_instance`'s 'mastodon' default so that this file matches
    sub-project 14's helper; a later task that wants the software half of that
    guard to fire has to pass 'piefed' or 'pylova'.
    """
    make_site()
    instance = make_instance(PEER, software=software)
    make_user(instance, 'community_owner')
    community = make_community(host=PEER)
    community.ap_fetched_at = utcnow()
    author = make_user(instance, 'author')
    post = make_post(community, author, ap_id=f'https://{PEER}/objects/1')
    db.session.commit()
    return post


def _update(**fields):
    """An Update activity's `object`, with only the keys a test names.

    The function reads `request_json['object']` and nothing else, so the
    envelope carries no `type`, `actor` or `id`.
    """
    return {'object': fields}


# ---------------------------------------------------------------------------
# THE `type == 'Video'` CLUSTER STARTS HERE. The shared harness is everything
# above this line and nothing below it; a later cluster opens its own banner.
# The two constants and the helper that follow are module-level only because a
# test method's globals are its module's -- which is equally true of anything
# placed up with the shared harness, so scope cannot tell a reader who owns a
# name and position has to.
# ---------------------------------------------------------------------------

LIKES_URL = f'https://{PEER}/videos/1/likes'
DISLIKES_URL = f'https://{PEER}/videos/1/dislikes'


def _seed_vote_baseline(post):
    """Seed values contrary to every column the Video block writes.

    `Post.up_votes`, `down_votes` and `score` are declared `default=0`, and
    `ranking` / `ranking_scaled` `default=0.0` (app/models.py's Post). Asserting
    the block's output against those defaults would pass on a block that never
    ran, so each is seeded to something the block cannot produce.

    `reply_count` is seeded non-zero for a second reason: the block computes
    `post.post_ranking(post.score + post.reply_count, post.posted_at)`, and with
    `reply_count` at its `default=0` that argument is indistinguishable from
    `post.score` alone.
    """
    post.up_votes = 7
    post.down_votes = 5
    post.score = -3
    post.ranking = 99.5
    post.ranking_scaled = 88.0
    post.reply_count = 4
    db.session.commit()


class TestVideoVoteCollections:
    """`if request_json['object']['type'] == 'Video':` -- the PeerTube arm that
    recovers vote totals by fetching the object's `likes` and `dislikes`
    collections, then returns before the rest of the function runs.
    """

    def test_both_vote_collections_are_counted(self, app, db_session, http_mock,
                                               redis_lock_only_double):
        """Both endpoints present and both serving `totalItems`.

        `upvotes` starts at 1 ("from OP"), so 5 likes give 6; `downvotes` starts
        at 0, so 2 dislikes give 2. The two totals are deliberately different
        from each other, so an arm that credited the wrong collection could not
        produce this pair.
        """
        post = _seed_post()
        _seed_vote_baseline(post)
        posted_at = post.posted_at
        http_mock.get(LIKES_URL).mock(return_value=httpx.Response(200, json={'totalItems': 5}))
        http_mock.get(DISLIKES_URL).mock(return_value=httpx.Response(200, json={'totalItems': 2}))

        update_post_from_activity(post, _update(type='Video', name='a post',
                                                likes=LIKES_URL, dislikes=DISLIKES_URL))

        # Discard any unflushed attribute state and force a re-SELECT, so the
        # assertions below read the committed row rather than pending values on
        # the object the function just wrote to. This is what pins the block's
        # `db.session.commit()` at `app/activitypub/util.py:3308`:
        # `app/__init__.py:81` builds `SQLAlchemy(session_options={"autoflush":
        # False}, ...)` at module scope -- not in `create_app`, which starts at
        # `app/__init__.py:129` -- so without that commit nothing reaches the
        # row at all, `expire` re-reads the seeded baseline, and every assertion
        # below fails. Without this line the commit can be deleted with the
        # whole file still green.
        db.session.expire(post)

        assert post.up_votes == 6
        assert post.down_votes == 2
        assert post.score == 4
        # score + reply_count, not score: 4 + 4.
        assert post.ranking == post.post_ranking(8, posted_at)
        # community.subscriptions_count is 0 from make_community, and
        # Community.scale_by() returns 3 for subscriptions_count <= 1.
        assert post.ranking_scaled == int(post.ranking + 3)

    def test_only_likes_present_counts_only_upvotes(self, app, db_session, http_mock,
                                                    redis_lock_only_double):
        """`if endpoint in request_json['object']` skipping the absent half.

        Only the `likes` route is registered: `http_mock` asserts every
        registered route is called, so registering a `dislikes` route here would
        fail the test rather than sit idle.
        """
        post = _seed_post()
        _seed_vote_baseline(post)
        http_mock.get(LIKES_URL).mock(return_value=httpx.Response(200, json={'totalItems': 3}))

        update_post_from_activity(post, _update(type='Video', name='a post', likes=LIKES_URL))

        assert post.up_votes == 4
        assert post.down_votes == 0
        assert post.score == 4

    def test_neither_vote_collection_present_records_the_op_upvote(self, app, db_session,
                                                                   redis_lock_only_double):
        """No `likes` and no `dislikes` key: the loop fetches nothing and the
        block still writes the OP's own upvote over whatever was there.

        No HTTP fixture at all -- the session-scoped `block_outbound_http`
        router (tests/conftest.py:214-216) raises on any request that escapes,
        so an unexpected fetch here fails rather than leaving the process.
        """
        post = _seed_post()
        _seed_vote_baseline(post)

        update_post_from_activity(post, _update(type='Video', name='a post'))

        assert post.up_votes == 1
        assert post.down_votes == 0
        assert post.score == 1

    def test_a_non_200_vote_collection_is_ignored(self, app, db_session, http_mock,
                                                  redis_lock_only_double):
        """`if object_request and object_request.status_code == 200`.

        The 404 serves a body that IS valid JSON carrying `totalItems`. A body
        the parser rejects would make the status guard untestable: the bare
        `except:` at `app/activitypub/util.py:3291-3293` would set `object` to
        None and produce this same outcome with the status check removed.
        tests/README.md:1085-1089 records that exact trap.
        """
        post = _seed_post()
        _seed_vote_baseline(post)
        http_mock.get(LIKES_URL).mock(return_value=httpx.Response(404, json={'totalItems': 99}))

        update_post_from_activity(post, _update(type='Video', name='a post', likes=LIKES_URL))

        assert post.up_votes == 1
        assert post.down_votes == 0
        assert post.score == 1

    def test_a_vote_collection_that_is_not_json_is_ignored(self, app, db_session, http_mock,
                                                           redis_lock_only_double):
        """The bare `except:` around `object_request.json()`
        (`app/activitypub/util.py:3289-3293`), which sets `object = None`.

        A 200 with a body httpx's `.json()` refuses.
        """
        post = _seed_post()
        _seed_vote_baseline(post)
        http_mock.get(LIKES_URL).mock(return_value=httpx.Response(200, text='not json at all'))

        update_post_from_activity(post, _update(type='Video', name='a post', likes=LIKES_URL))

        assert post.up_votes == 1
        assert post.down_votes == 0
        assert post.score == 1

    def test_a_vote_collection_without_total_items_is_ignored(self, app, db_session, http_mock,
                                                              redis_lock_only_double):
        """`if object and 'totalItems' in object`.

        The body is a non-empty dict on purpose: `{}` is falsy, so an empty one
        would satisfy a mutant that dropped `'totalItems' in object` and kept
        only `if object`, and this test would pass against it.
        """
        post = _seed_post()
        _seed_vote_baseline(post)
        http_mock.get(LIKES_URL).mock(
            return_value=httpx.Response(200, json={'type': 'OrderedCollection'}))

        update_post_from_activity(post, _update(type='Video', name='a post', likes=LIKES_URL))

        assert post.up_votes == 1
        assert post.down_votes == 0
        assert post.score == 1

    def test_the_video_block_returns_before_the_links_section(self, app, db_session,
                                                              redis_lock_only_double):
        """`db.session.commit()` / `return` at `app/activitypub/util.py:3308-3309`,
        "return now for PeerTube, otherwise rest of this function breaks the post".

        The witness is the Links section's no-url `else` arm at `:3550-3567`.
        With no `attachment` in the Update and `post.type` not `POST_TYPE_EVENT`,
        `new_url` stays None, so `old_url != new_url` is true and that arm sets
        `post.type = POST_TYPE_ARTICLE` and `post.url = None`. It was chosen over
        the other post-return effects because it needs no extra fixture and it
        writes two columns at once, both of which this test seeds to values the
        arm cannot produce: a non-null url, and POST_TYPE_VIDEO against the
        column's `default=POST_TYPE_ARTICLE`.
        """
        post = _seed_post()
        post.url = f'https://{PEER}/w/abc123'
        post.type = POST_TYPE_VIDEO
        db.session.commit()

        update_post_from_activity(post, _update(type='Video', name='a post'))

        assert post.url == f'https://{PEER}/w/abc123'
        assert post.type == POST_TYPE_VIDEO
        # ... and the block did run, so this is not a test of an untaken path.
        assert post.up_votes == 1


class TestVideoVoteCollectionRetry:
    """The `except httpx.HTTPError:` / `time.sleep(3)` / second `get_request`
    pair at `app/activitypub/util.py:3282-3287`.

    `no_real_sleeping` (tests/conftest.py:454-470) neutralises both sleep sites,
    not just the one in this block: `app/activitypub/util.py` does a
    module-level `import time` and calls `time.sleep(3)`, which a patch to
    `time.sleep` covers, while `app/utils.py` binds `from time import sleep` at
    import and needs `app.utils.sleep` patched separately. That second one is
    load-bearing here -- see the call-count arithmetic below.

    `get_request` retries once ITSELF: a transport failure lands in its
    `except httpx.HTTPError as read_timeout` arm (`app/utils.py:173-180`), which
    sleeps and re-issues the request before giving up and raising
    `httpx.HTTPError`. So one failing `get_request` call costs TWO route calls,
    and the counts asserted below are read with that in mind.
    """

    @pytest.mark.usefixtures('no_real_sleeping')
    def test_a_failed_fetch_is_retried_and_the_second_attempt_counts(self, app, db_session,
                                                                     http_mock,
                                                                     redis_lock_only_double):
        """First `get_request` raises, the second succeeds.

        The outcome alone cannot tell a retry from a first-attempt success --
        both end with the collection counted -- so the assertion is on the
        respx route's own call count. Three: two from the first `get_request`'s
        internal retry, then one from the block's own second call.
        """
        post = _seed_post()
        _seed_vote_baseline(post)
        route = http_mock.get(LIKES_URL).mock(side_effect=[
            httpx.ConnectError('boom'),
            httpx.ConnectError('boom'),
            httpx.Response(200, json={'totalItems': 9}),
        ])

        update_post_from_activity(post, _update(type='Video', name='a post', likes=LIKES_URL))

        assert route.call_count == 3
        assert post.up_votes == 10
        assert post.down_votes == 0
        assert post.score == 10

    @pytest.mark.usefixtures('no_real_sleeping')
    def test_a_failed_retry_leaves_the_request_none(self, app, db_session, http_mock,
                                                    redis_lock_only_double):
        """Both `get_request` calls raise, so `object_request = None` and the
        `if object_request and ...` guard short-circuits on it.

        Four route calls: each of the block's two `get_request` calls spends its
        own internal retry before raising.
        """
        post = _seed_post()
        _seed_vote_baseline(post)
        route = http_mock.get(LIKES_URL).mock(side_effect=httpx.ConnectError('boom'))

        update_post_from_activity(post, _update(type='Video', name='a post', likes=LIKES_URL))

        assert route.call_count == 4
        assert post.up_votes == 1
        assert post.down_votes == 0
        assert post.score == 1


# ---------------------------------------------------------------------------
# THE `type == 'Question'` CLUSTER STARTS HERE. Everything from here down to the
# next banner belongs to it; the shared harness is the region above the `Video`
# banner and the names between the two banners belong to `Video` alone.
# ---------------------------------------------------------------------------

# Any ISO 8601 string. `app/activitypub/util.py:3338` is
# `poll.end_poll = request_json['object']['endTime']`, which stores the peer's
# string straight into `Poll.end_poll` (a `db.DateTime`, app/models.py:3782)
# with no `fromisoformat` in between; the year is far enough out that no test
# here can confuse it with a seeded value.
END_TIME = '2027-01-01T12:00:00+00:00'

# What comes back out of `Poll.end_poll` after an Update carrying END_TIME, read
# from a re-SELECT rather than off the in-memory attribute. Naive, because the
# column is `db.DateTime` with no `timezone=True` (app/models.py:3782), so it is
# `timestamp without time zone`: psycopg2 adapts the peer's str to a BARE
# literal (`adapt('...+05:00').getquoted()` is `b"'2027-01-01T12:00:00+05:00'"`,
# with no `::` tag), so the column's own type drives the cast and the offset is
# DISCARDED rather than converted by.
#
# END_TIME_OTHER_OFFSET is the same wall-clock time under a different offset --
# a DIFFERENT instant, five hours earlier. It stores as the SAME
# END_TIME_STORED, which is the whole finding, and it is asserted rather than
# merely described: `test_the_end_time_string_is_cast_by_postgres_to_a_naive_datetime`
# sends both. END_TIME alone could not show it, because `+00:00` cannot
# distinguish "offset discarded" from "converted to UTC".
END_TIME_STORED = datetime(2027, 1, 1, 12, 0)
END_TIME_OTHER_OFFSET = '2027-01-01T12:00:00+05:00'

# A contrary baseline for `end_poll`, far enough from END_TIME_STORED that no
# assertion below can be satisfied by the seeded value.
SEEDED_END_TIME = datetime(2020, 6, 1, 9, 30)


def _poll_update(*choices, end_time=None, mode_key='oneOf'):
    """A `Question` object carrying `choices` as its vote list.

    Each choice is a dict exactly as a peer would send it, so a test can pass a
    malformed one -- `{}`, or a dict with `name` but no `replies` -- without the
    helper repairing it. `mode_key` selects `oneOf` (mode 'single') or `anyOf`
    (mode 'multiple'); the production code reads one or the other and returns
    early when neither is present.

    `name` is not optional and is not part of the poll: this module's docstring
    records that every Update built in this file carries it, and for a Question
    it is load-bearing rather than tidy. Without `name` the head takes the else
    arm at `app/activitypub/util.py:3167-3184`, whose first statement is
    `autogenerated_title, link = microblog_content_to_title(post.body_html)`,
    and `microblog_content_to_title` opens with `if '<h1>' in html.lower():`
    (app/utils.py:1305). `make_post` leaves `body_html` at None unless
    `microblog=True` (tests/factories.py:326-330), so the titleless arm raises
    AttributeError before the Question block is ever reached.
    """
    obj = {'type': 'Question', 'name': 'a poll', mode_key: list(choices)}
    if end_time is not None:
        obj['endTime'] = end_time
    return {'object': obj}


def _choice(name, total=None):
    """One entry in a Question's vote list.

    `total=None` omits `replies` entirely, which is what a peer sending a poll
    with no vote counts looks like; a number produces
    `{'replies': {'totalItems': n}}`.
    """
    vote = {'name': name}
    if total is not None:
        vote['replies'] = {'totalItems': total}
    return vote


def _seed_poll(post, choices, mode='single'):
    """A `Poll` on `post` plus one `PollChoice` per `(choice_text, num_votes)`.

    `num_votes` is set after `make_poll_choice` because that factory
    (tests/factories.py:783) does not take it. Every caller passes a non-zero
    value: `PollChoice.num_votes` is declared `default=0` (app/models.py:3824),
    so a test asserting that the totals path wrote 0 over the default would pass
    against a block that never ran.

    `sort_order` counts from 1 to match what the Edit path writes at
    `app/activitypub/util.py:3345-3351` (`i = 1`, incremented once per vote it
    does not skip).
    """
    poll = make_poll(post, mode=mode)
    rows = []
    for i, (choice_text, num_votes) in enumerate(choices, start=1):
        row = make_poll_choice(post, choice_text, sort_order=i)
        row.num_votes = num_votes
        rows.append(row)
    db.session.commit()
    return poll, rows


def _seed_link_witness(post):
    """Seed `post.url` and `post.type` contrary to what the Links section writes.

    All four of the poll arm's `return`s -- `:3320`, `:3337`, `:3353` and
    `:3368` -- leave `update_post_from_activity` before its Links section, whose
    no-url `else` arm at `:3550-3567` sets `post.type = POST_TYPE_ARTICLE` and
    `post.url = None` for any Update carrying no `attachment` on a post that is
    not an Event. A test whose only other witness is "the seeded poll rows are
    unchanged" needs this pair, or it passes just as happily against a poll arm
    that never ran at all.
    """
    post.url = f'https://{PEER}/post/1'
    post.type = POST_TYPE_POLL
    db.session.commit()


def _stored_choices(post):
    """`(choice_text, num_votes)` for `post`'s choices, in `sort_order`.

    Queried rather than read off the objects `_seed_poll` returned, so a row the
    Edit path deleted and re-created is seen as the new row it is.
    """
    rows = PollChoice.query.filter_by(post_id=post.id).order_by(PollChoice.sort_order).all()
    return [(row.choice_text, row.num_votes) for row in rows]


def _stored_sort_orders(post):
    """`(choice_text, sort_order)` for `post`'s choices, in **id** order.

    Deliberately not `order_by(PollChoice.sort_order)` the way `_stored_choices`
    is: this helper's whole subject is the `sort_order` values, and ordering the
    query by the column under test would let a mutant that mis-numbers the rows
    be sorted back into the expected sequence. `PollChoice.id` is assigned by the
    sequence in insertion order, so id order is the order
    `app/activitypub/util.py:3346-3350` added them in.
    """
    rows = PollChoice.query.filter_by(post_id=post.id).order_by(PollChoice.id).all()
    return [(row.choice_text, row.sort_order) for row in rows]


def _seed_choice_vote(post, choice):
    """One `PollChoiceVote` on `choice`, so the first `DELETE` has something to
    delete.

    Constructed here rather than through a factory because there is none:
    `PollChoiceVote` does not appear anywhere in tests/factories.py.

    The model is at app/models.py:3832-3836. Its primary key is the pair
    `(choice_id, user_id)` (`:3833-3834`) and it carries `post_id` separately
    (`:3835`) -- `post_id` is not optional here, because
    `app/activitypub/util.py:3341-3342` is
    `db.session.execute(text('DELETE FROM "poll_choice_vote" WHERE post_id = :post_id'), ...)`,
    which matches on that column alone. A row seeded without it would survive
    the first DELETE and then make the second one fail on the foreign key
    instead, which is a different test than the one intended.

    The voter is a third user: `_seed_post` seeds 'community_owner' and 'author'
    to occupy ids 1 and 2, so this one is never user 1.
    """
    voter = make_user(post.instance, 'voter')
    vote = PollChoiceVote(choice_id=choice.id, user_id=voter.id, post_id=post.id)
    db.session.add(vote)
    db.session.commit()
    return vote


class TestQuestionRouting:
    """`app/activitypub/util.py:3311-3320` -- the poll arm's opening, which picks
    the vote list out of `oneOf` or `anyOf` and returns when neither is there.

    WHERE `mode` CAN BE ASSERTED AT ALL. `mode` is a local, initialised to
    'single' at `:3313` and re-assigned to 'multiple' at `:3318` in the `anyOf`
    arm. The only statement that persists it is `poll.mode = mode` at `:3339`,
    inside the Edit path, which `:3333` reaches only when `total_vote_count == 0`
    and which then requires both a `Poll` row (`:3335`) and an `endTime`
    (`:3336-3337`) before it gets that far. So the two tests below that assert
    `mode` send all-zero totals and an `endTime`; no test on the totals path can
    assert `mode`, because the totals path (`:3355-3368`) never reads the
    variable.
    """

    def test_one_of_supplies_the_vote_list_and_records_mode_single(self, app, db_session,
                                                                   redis_lock_only_double):
        """`if 'oneOf' in request_json['object']:` at `:3314`, with `mode` left at
        the 'single' `:3313` initialises it to.

        The seeded poll is 'multiple' so that 'single' is a change rather than
        the value that was already there, and the seeded choice is named
        something no vote names, so the recreated set is evidence that `votes`
        was bound from `oneOf` and not left empty.
        """
        post = _seed_post()
        poll, _ = _seed_poll(post, [('Red', 7)], mode='multiple')

        update_post_from_activity(post, _poll_update(_choice('Yes', 0), _choice('No', 0),
                                                     end_time=END_TIME))

        # Every test in this cluster expires before it asserts. `expire_all()`
        # rather than `expire(obj)` because the witnesses are whole rowsets and
        # because `_stored_choices` re-queries: a query returns the objects
        # already in the identity map and discards the SELECT's values for them,
        # so an unexpired row reads stale no matter how fresh the query is. It
        # buys what Ruling E buys -- `app/__init__.py:81` builds
        # `SQLAlchemy(session_options={"autoflush": False}, ...)`, so an
        # arm whose `db.session.commit()` were removed would still leave its
        # writes on the in-memory attributes and every assertion below would
        # pass on a block that persisted nothing.
        db.session.expire_all()

        assert poll.mode == 'single'
        assert _stored_choices(post) == [('Yes', 0), ('No', 0)]

    def test_any_of_supplies_the_vote_list_and_records_mode_multiple(self, app, db_session,
                                                                     redis_lock_only_double):
        """`elif 'anyOf' in request_json['object']:` at `:3316-3318`, the arm that
        also sets `mode = 'multiple'`.

        Seeded 'single' so the assertion is on a change. The vote list is
        identical to the `oneOf` test's apart from the key it arrives under, so
        the key is the only thing that differs between the two.
        """
        post = _seed_post()
        poll, _ = _seed_poll(post, [('Red', 7)], mode='single')

        update_post_from_activity(post, _poll_update(_choice('Yes', 0), _choice('No', 0),
                                                     end_time=END_TIME, mode_key='anyOf'))

        db.session.expire_all()

        assert poll.mode == 'multiple'
        assert _stored_choices(post) == [('Yes', 0), ('No', 0)]

    def test_neither_one_of_nor_any_of_writes_nothing(self, app, db_session,
                                                      redis_lock_only_double):
        """The `else: return` at `:3319-3320`.

        Built with `_update` rather than `_poll_update`, because `_poll_update`
        always writes one of the two keys.

        This test cannot assert `mode`: the return at `:3320` is upstream of
        every statement that persists it, so the seeded 'multiple' below is the
        contrary baseline for "nothing was written" and not a claim about the
        local.
        """
        post = _seed_post()
        _seed_link_witness(post)
        poll, _ = _seed_poll(post, [('Red', 7)], mode='multiple')

        update_post_from_activity(post, _update(type='Question', name='a poll'))

        db.session.expire_all()

        assert poll.mode == 'multiple'
        assert poll.end_poll is None
        assert _stored_choices(post) == [('Red', 7)]
        # ... and the arm was entered and returned from `:3320`, rather than
        # never having matched at `:3311`.
        assert post.url == f'https://{PEER}/post/1'


class TestQuestionVoteCountGuards:
    """The three `continue`s at `app/activitypub/util.py:3323-3331`, which drop a
    vote missing `name`, missing `replies`, or whose `replies` has no
    `totalItems` before it can reach `total_vote_count += ...` at `:3331`.

    A skipped vote is not observable in itself -- nothing persists
    `total_vote_count`. What IS observable is the routing decision it feeds:
    `if total_vote_count == 0:` at `:3333` sends an all-skipped Update down the
    Edit path and a counted one down the totals path. Each test below sends no
    `endTime`, so the Edit path stops at `:3336-3337` having written nothing,
    while the totals path would overwrite the seeded `num_votes`. The witness is
    therefore the seeded rows still being the seeded rows -- paired with
    `_seed_link_witness`, because "nothing was written" is also what a poll arm
    that never ran would produce, and only the surviving `post.url` separates
    the two.

    None of these tests can assert `mode`: `:3339` is downstream of the
    `endTime` return they rely on.
    """

    def test_a_vote_with_no_name_is_not_counted(self, app, db_session,
                                                redis_lock_only_double):
        """`if not 'name' in vote: continue` at `:3324-3325`.

        The nameless vote is the one carrying a total, so dropping the guard
        would make `total_vote_count` 5 and route to the totals path; the
        well-formed vote alongside it carries 0 so that the guard's own arm
        leaves the count at 0.
        """
        post = _seed_post()
        _seed_link_witness(post)
        _seed_poll(post, [('Yes', 7)])

        update_post_from_activity(post, _poll_update({'replies': {'totalItems': 5}},
                                                     _choice('Yes', 0)))

        db.session.expire_all()
        assert _stored_choices(post) == [('Yes', 7)]
        assert post.url == f'https://{PEER}/post/1'

    def test_a_vote_with_no_replies_is_not_counted(self, app, db_session,
                                                   redis_lock_only_double):
        """`if not 'replies' in vote: continue` at `:3326-3327`.

        `_choice('Yes')` with no `total` omits `replies` entirely. The guard is
        what stops `:3328`'s `vote['replies']` from being read on a vote that has
        none.
        """
        post = _seed_post()
        _seed_link_witness(post)
        _seed_poll(post, [('Yes', 7), ('No', 11)])

        update_post_from_activity(post, _poll_update(_choice('Yes'), _choice('No', 0)))

        db.session.expire_all()
        assert _stored_choices(post) == [('Yes', 7), ('No', 11)]
        assert post.url == f'https://{PEER}/post/1'

    def test_replies_without_total_items_is_not_counted(self, app, db_session,
                                                        redis_lock_only_double):
        """`if not 'totalItems' in vote['replies']: continue` at `:3328-3329`.

        Written inline rather than through `_choice`, which builds `replies` only
        together with `totalItems`. `replies` is a non-empty dict so that a
        mutant reading it for truth rather than for the key still sees something
        truthy.
        """
        post = _seed_post()
        _seed_link_witness(post)
        _seed_poll(post, [('Yes', 7), ('No', 11)])

        update_post_from_activity(post, _poll_update(
            {'name': 'Yes', 'replies': {'type': 'Collection'}}, _choice('No', 0)))

        db.session.expire_all()
        assert _stored_choices(post) == [('Yes', 7), ('No', 11)]
        assert post.url == f'https://{PEER}/post/1'


class TestQuestionEditPath:
    """`app/activitypub/util.py:3333-3353`, the "Edit, not a totals update" arm.

    It is reached when `total_vote_count == 0` -- which is a SUM at `:3331`,
    over only the votes the three `continue`s let through, and NOT "every choice
    is on zero votes". Six shapes in this file reach it. Each clause below was
    written against the `_poll_update(...)` call it names, read at the call:

      - every vote well-formed and on zero -- `_choice('Yes', 0), _choice('No', 0)`,
        which is most of the class below and both `TestQuestionRouting` tests
        that get this far;
      - an EMPTY vote list, where the counting loop never runs at all --
        `_poll_update(end_time=END_TIME)`, sent only by
        `test_an_empty_vote_list_deletes_every_choice_and_recreates_none`. Not a
        case of "every vote on zero": there are no votes;
      - a MIX that stops at `:3337` for want of an `endTime`, one skipped vote
        alongside one well-formed zero. This is what EACH of the three
        `TestQuestionVoteCountGuards` tests sends -- the nameless
        `{'replies': {'totalItems': 5}}` with `_choice('Yes', 0)`,
        `_choice('Yes')` with `_choice('No', 0)`, and
        `{'name': 'Yes', 'replies': {'type': 'Collection'}}` with
        `_choice('No', 0)`. All three, not one of them;
      - a MIX that carries an `endTime` and so runs the whole arm --
        `_choice('Yes', 0), {'replies': {'totalItems': 0}}, _choice('No', 0)`
        with `end_time=END_TIME`, sent only by
        `test_a_nameless_vote_is_skipped_and_the_rest_stay_contiguous` below;
      - EVERY vote skipped by the counting loop but every vote NAMED --
        `_choice('Yes'), _choice('No')` with `end_time=END_TIME`, sent only by
        `test_votes_with_names_but_no_replies_are_still_recreated` below;
      - well-formed non-zero totals that CANCEL, which is how
        `test_the_edit_path_returns_before_the_totals_loop` below sends -3 and 3.

    A seventh shape -- every vote NAMELESS -- is not sent anywhere in this file.
    It is what exposed Task 4's defect: before the fix, an all-nameless list plus
    an `endTime` raised `KeyError: 'name'` in the recreate loop, which re-read
    the unfiltered list without the counting loop's `:3324` guard applied -- and
    raised AFTER the two DELETEs below had run. The guard at `:3347-3348` is that
    fix, and the fourth-listed shape above is the mixed version of it that IS
    sent here.

    WHY THAT GUARD TESTS `name` ALONE and not the counting loop's other two
    conditions: the recreate loop reads nothing but `vote['name']`, so a vote
    with a `name` and no `replies` supplies everything it needs. Copying all
    three `continue`s here would take the fifth-listed shape above -- an entire
    list of such votes, which the counting loop skips in full and so routes to
    this arm -- run both DELETEs, insert nothing, and COMMIT that at `:3352`. Not
    a crash: an Update that silently empties the poll.
    `test_votes_with_names_but_no_replies_are_still_recreated` is that case, and
    its docstring records what is and is not claimed about how often a peer sends
    it.

    It then requires a `Poll` row (`:3335`) and an
    `endTime` (`:3336-3337`), writes `end_poll` and `mode`, and REPLACES the
    choice set outright:

        db.session.execute(text('DELETE FROM "poll_choice_vote" WHERE post_id = :post_id'),
                           {'post_id': post.id})
        db.session.execute(text('DELETE FROM "poll_choice" WHERE post_id = :post_id'), {'post_id': post.id})

    -- two raw statements at `:3341-3343`, followed by a loop at `:3345-3351`
    that inserts one fresh `PollChoice` per vote that carries a `name`.

    WHAT IS NOT HERE, because `TestQuestionRouting` above already has it:
    `poll.mode = mode` at `:3339` is asserted by that class's first two tests,
    which send all-zero totals and an `endTime` precisely so that this arm runs.
    The tests below assert `mode` only where it is a "this arm did NOT run"
    witness.

    Both DELETEs match on `post_id`, not on the poll, so the rows a test seeds
    have to carry `post_id` -- see `_seed_choice_vote`.
    """

    def test_the_seeded_choices_and_their_votes_are_deleted_and_replaced(
            self, app, db_session, redis_lock_only_double):
        """The two DELETEs at `:3341-3343` and the recreate loop at `:3345-3351`.

        THE POINT OF THE 'Old ...' NAMES. A test that only asserts the new rows
        are present does not prove a DELETE ran: the loop at `:3345-3351` would
        produce that same rowset by insertion alone if the seeded rows had never
        existed. So the seeded rows carry `choice_text` values the Update does
        not mention, and the assertion is on the WHOLE stored set -- 'Old A' and
        'Old B' being absent is the DELETE's only witness.

        The ids are checked too, and are the sharper half of the proof: they say
        the two 'Yes'/'No' rows are NEW rows rather than the seeded rows renamed
        in place. `tests/conftest.py:143` truncates with `RESTART IDENTITY`, so
        `PollChoice.id` restarts at 1 in every test and the two sets are small
        enough to collide by accident; the `len(...) == 2` guards are there
        because two equal ids on either side would make `isdisjoint` vacuous.
        """
        post = _seed_post()
        _, rows = _seed_poll(post, [('Old A', 7), ('Old B', 11)])
        _seed_choice_vote(post, rows[0])
        seeded_ids = {row.id for row in rows}
        assert len(seeded_ids) == 2

        update_post_from_activity(post, _poll_update(_choice('Yes', 0), _choice('No', 0),
                                                     end_time=END_TIME))

        db.session.expire_all()

        assert _stored_choices(post) == [('Yes', 0), ('No', 0)]
        assert PollChoiceVote.query.count() == 0
        recreated_ids = {row.id for row in PollChoice.query.filter_by(post_id=post.id).all()}
        assert len(recreated_ids) == 2
        assert seeded_ids.isdisjoint(recreated_ids)

    def test_an_empty_vote_list_deletes_every_choice_and_recreates_none(
            self, app, db_session, redis_lock_only_double):
        """The DELETEs at `:3341-3343` with the loop at `:3345-3351` running zero
        times.

        `'oneOf' in request_json['object']` at `:3314` is satisfied by an empty
        list, so `votes` binds to `[]`, the counting loop at `:3323-3331` never
        runs, `total_vote_count` stays at the 0 `:3322` set, and this arm deletes
        the choice set without putting anything back.

        This is the DELETE proof with nothing to confuse it: there is no
        recreate step whose insertions could be mistaken for survivors, so an
        empty stored set can only be the DELETE at `:3343`.

        `end_poll` is asserted alongside it so the test says WHICH arm emptied
        the table. An empty set on its own is a claim about rows; `end_poll`
        moving off the seeded 2020 date is a claim about `:3338`, and the two
        together place the DELETE inside this arm rather than merely somewhere.
        """
        post = _seed_post()
        poll, rows = _seed_poll(post, [('Old A', 7), ('Old B', 11)])
        poll.end_poll = SEEDED_END_TIME
        db.session.commit()
        _seed_choice_vote(post, rows[1])

        update_post_from_activity(post, _poll_update(end_time=END_TIME))

        db.session.expire_all()

        assert _stored_choices(post) == []
        assert PollChoiceVote.query.count() == 0
        assert poll.end_poll == END_TIME_STORED

    def test_a_post_with_no_poll_row_writes_nothing(self, app, db_session,
                                                    redis_lock_only_double):
        """The false arm of `if poll:` at `:3335`.

        `Poll.query.filter_by(post_id=post.id).first()` at `:3334` returns None,
        so control drops straight to the `return` at `:3353`.

        `PollChoice` has no foreign key to `Poll` (app/models.py:3821 points at
        `post.id`), so choices can be -- and here are -- seeded without one. That
        is what makes this test's "nothing was written" observable at all: the
        two rows the DELETEs would have removed are still there.

        Paired with `_seed_link_witness` because surviving choice rows are also
        what an Update that never entered the poll arm would leave; only
        `post.url` says a `return` INSIDE the arm was reached.
        """
        post = _seed_post()
        _seed_link_witness(post)
        make_poll_choice(post, 'Old A', sort_order=1).num_votes = 7
        make_poll_choice(post, 'Old B', sort_order=2).num_votes = 11
        db.session.commit()

        update_post_from_activity(post, _poll_update(_choice('Yes', 0), _choice('No', 0),
                                                     end_time=END_TIME))

        db.session.expire_all()

        assert _stored_choices(post) == [('Old A', 7), ('Old B', 11)]
        assert post.url == f'https://{PEER}/post/1'

    def test_an_update_with_no_end_time_returns_before_the_writes(
            self, app, db_session, redis_lock_only_double):
        """`if not 'endTime' in request_json['object']: return` at `:3336-3337`.

        `TestQuestionVoteCountGuards` above leans on this return as its "nothing
        was written" mechanism; this test is about the return itself, and so
        asserts what those tests cannot. The poll is seeded 'multiple'
        against an Update arriving under `oneOf`, so `mode` staying 'multiple'
        says `:3339` was not reached; `end_poll` staying at the seeded 2020 date
        says `:3338` was not reached either. The surviving `PollChoiceVote` says
        the return is upstream of the DELETEs at `:3341-3343` and not merely
        upstream of the recreate loop.
        """
        post = _seed_post()
        _seed_link_witness(post)
        poll, rows = _seed_poll(post, [('Old A', 7), ('Old B', 11)], mode='multiple')
        poll.end_poll = SEEDED_END_TIME
        db.session.commit()
        _seed_choice_vote(post, rows[0])

        update_post_from_activity(post, _poll_update(_choice('Yes', 0), _choice('No', 0)))

        db.session.expire_all()

        assert poll.mode == 'multiple'
        assert poll.end_poll == SEEDED_END_TIME
        assert _stored_choices(post) == [('Old A', 7), ('Old B', 11)]
        assert PollChoiceVote.query.count() == 1
        assert post.url == f'https://{PEER}/post/1'

    def test_the_end_time_string_is_cast_by_postgres_to_a_naive_datetime(
            self, app, db_session, redis_lock_only_double):
        """`poll.end_poll = request_json['object']['endTime']` at `:3338`.

        The peer's string is assigned RAW. There is no `datetime.fromisoformat`
        between it and the column, unlike the Event block below, whose `:3375`
        and `:3376` read `startTime`/`endTime` through `datetime.fromisoformat`.
        What makes the raw assignment work at all is psycopg2 plus Postgres:
        the str is sent as a literal and the server casts it to the column's
        type on the way in.

        WHAT ACTUALLY LANDS, measured by re-SELECT rather than reasoned about:
        `datetime.datetime(2027, 1, 1, 12, 0)`, with `tzinfo` None. `Poll.end_poll`
        is `db.Column(db.DateTime)` (app/models.py:3782) with no `timezone=True`,
        i.e. `timestamp without time zone`.

        THE SECOND UPDATE IS THE POINT. `END_TIME`'s `+00:00` cannot distinguish
        "the offset was discarded" from "the value was converted to UTC" -- both
        give 12:00. `END_TIME_OTHER_OFFSET` is the same wall clock at `+05:00`,
        a genuinely different instant five hours earlier, and it stores as the
        SAME `END_TIME_STORED`. So the offset is DISCARDED, and a peer in a
        non-UTC offset silently records a poll deadline wrong by that offset.

        This is NOT what the Event block below does with the same field.
        `:3375-3376` read `startTime`/`endTime` through `datetime.fromisoformat`,
        which yields an AWARE datetime; psycopg2 tags an aware datetime
        `::timestamptz`, and the assignment cast into a naive column then
        CONVERTS by the server's session TimeZone (`Etc/UTC` under this harness)
        instead of truncating. Measured both ways. The two blocks are not twins
        in outcome -- only this one corrupts.

        The `sqlalchemy.exc.DataError` a malformed `endTime` raises out of the
        function at commit time is the other finding on this line. Neither is
        repaired here.

        The seeded 2020 date is the contrary baseline: `end_poll` is nullable and
        `make_poll` leaves it None, so asserting a value over None would be
        weaker. The second Update re-seeds it for the same reason -- otherwise
        the second assertion would be satisfied by what the first Update left.
        """
        post = _seed_post()
        poll, _ = _seed_poll(post, [('Old A', 7)])
        poll.end_poll = SEEDED_END_TIME
        db.session.commit()

        update_post_from_activity(post, _poll_update(_choice('Yes', 0), end_time=END_TIME))

        db.session.expire_all()

        assert poll.end_poll == END_TIME_STORED
        assert poll.end_poll.tzinfo is None

        poll.end_poll = SEEDED_END_TIME
        db.session.commit()

        update_post_from_activity(post, _poll_update(_choice('Yes', 0),
                                                     end_time=END_TIME_OTHER_OFFSET))

        db.session.expire_all()

        assert poll.end_poll == END_TIME_STORED

    def test_choices_are_numbered_from_one_in_the_order_the_update_lists_them(
            self, app, db_session, redis_lock_only_double):
        """`i = 1` at `:3345` and `i += 1` at `:3351`, which between them supply
        `sort_order` to every row the loop inserts.

        Asserted as the full `(choice_text, sort_order)` set rather than as a row
        count, because the count is what a mis-numbering mutant would leave
        untouched. Three choices, so a numbering that started at 0, or that
        failed to increment, or that ran backwards, all produce a different list.

        The seeded 'Old' row is not in the expected list: it is deleted at
        `:3343` and its `sort_order` of 1 is re-used by 'Yes', which is why the
        assertion is on `choice_text` pairs and not on `sort_order` alone.
        """
        post = _seed_post()
        _seed_poll(post, [('Old', 7)])

        update_post_from_activity(post, _poll_update(_choice('Yes', 0), _choice('No', 0),
                                                     _choice('Maybe', 0), end_time=END_TIME))

        db.session.expire_all()

        assert _stored_sort_orders(post) == [('Yes', 1), ('No', 2), ('Maybe', 3)]

    def test_a_nameless_vote_is_skipped_and_the_rest_stay_contiguous(
            self, app, db_session, redis_lock_only_double):
        """The `if not 'name' in vote: continue` guard at `:3347-3348`, and its
        placement ABOVE `i += 1` at `:3351` rather than below it.

        THE SHAPE. A nameless entry between two well-formed ones. The counting
        loop's own `name` guard at `:3324-3325` drops it, and the two well-formed
        votes carry 0, so `total_vote_count` stays 0 and `:3333` routes here.
        Before Task 4's fix the recreate loop read `vote['name']` off the
        unfiltered list and raised `KeyError: 'name'` -- after the two DELETEs at
        `:3341-3343` had already run.

        WHY THE NAMELESS ENTRY CARRIES `totalItems: 0`. So that this test is
        about the recreate loop alone. A non-zero total there would make the
        Update route to the totals path the moment the counting loop's `name`
        guard at `:3324` was removed, and the test would be measuring that guard
        instead of this one.

        WHAT IS ASSERTED, and why it is `_stored_sort_orders` rather than
        `_stored_choices`: a recreated row's `num_votes` is the column's
        `default=0` (app/models.py:3824), so asserting it would be vacuous, while
        `sort_order` is this test's subject. The seeded 'Old' row's absence is the
        DELETE's witness. The guard's is TWO rows: a condition that never fires
        makes `:3349` raise and the call never return, and one that always fires
        leaves the table empty. And 1-2 rather than 1-3 is the
        `continue`-before-`i += 1` placement's -- the ONLY assertion in this file
        that distinguishes the two, since every other Edit-path test sends a list
        with nothing in it to skip.

        This test does NOT separate a `name`-only guard here from the counting
        loop's full three-condition one: both admit the two well-formed votes.
        The test below it sends the shape that does.
        """
        post = _seed_post()
        _seed_poll(post, [('Old', 7)])

        update_post_from_activity(post, _poll_update(_choice('Yes', 0),
                                                     {'replies': {'totalItems': 0}},
                                                     _choice('No', 0), end_time=END_TIME))

        db.session.expire_all()

        assert _stored_sort_orders(post) == [('Yes', 1), ('No', 2)]

    def test_votes_with_names_but_no_replies_are_still_recreated(
            self, app, db_session, redis_lock_only_double):
        """That the guard at `:3347-3348` tests `name` ALONE.

        NOT A DEFECT WITNESS. This shape traversed the unguarded recreate loop
        perfectly well before Task 4's fix -- the loop reads `vote['name']` and
        nothing else -- so this test passes on both sides of it. What it pins is
        the fix's ONE degree of freedom: the counting loop's spelling twenty
        lines up is three `continue`s, and copying all three here (rather than
        only the first) would delete the poll's choice set and put nothing back
        for exactly this Update.

        WHAT THE WIDENED GUARD WOULD DO, and why it is not the conservative
        choice it looks like. Every vote here carries a `name` and none carries
        `replies`, so `:3324-3329` skips all of them, `total_vote_count` stays 0,
        and `:3333` routes to the Edit path -- which runs both DELETEs at
        `:3341-3343`, and would then insert nothing and COMMIT that at `:3352`.
        Not a crash: an Update that silently empties the poll.

        No claim is made here about how often a peer sends this shape. PyFedi
        itself does not: both of its outbound emitters, `app/activitypub/util.py:186-193`
        and `app/shared/tasks/pages.py:228`, always write
        `'replies': {'type': 'Collection', 'totalItems': N}` (with `N` forced to
        0 when not an edit), so PyFedi's own poll Edit passes all three counting
        guards and arrives as the first shape the class docstring lists. What is
        established is only what is asserted below: this shape traverses the
        recreate loop correctly today, and the widened guard would empty its
        choice set. That is enough to settle which `continue` belongs here.

        The seeded 'Old' row is the contrary baseline: an empty expected list
        would be what a widened guard produced, and this expected list is what it
        could not.
        """
        post = _seed_post()
        _seed_poll(post, [('Old', 7)])

        update_post_from_activity(post, _poll_update(_choice('Yes'), _choice('No'),
                                                     end_time=END_TIME))

        db.session.expire_all()

        assert _stored_sort_orders(post) == [('Yes', 1), ('No', 2)]

    def test_the_edit_path_returns_before_the_totals_loop(self, app, db_session,
                                                          redis_lock_only_double):
        """The `return` at `:3353`.

        WHY THE TOTALS ARE -3 AND 3, and not the zeroes every other test here
        sends. `:3353` does not guard the Links section the way the totals arm's
        `:3368` does -- deleting it drops control into the totals loop at
        `:3356-3365`, which commits at `:3366` and returns at `:3368`, still
        short of the Links section. So `post.url` survives either way, and an
        all-zero Update makes that loop a no-op that rewrites the same 0s. This
        was measured, not predicted: with `return` → `pass` applied, the
        all-zero version of this test PASSED and the mutant survived.

        `:3331` accumulates `vote['replies']['totalItems']` with no sign check,
        so -3 and 3 sum to the 0 that `:3333` routes on while still being
        numbers the totals loop would WRITE. The Edit path recreates both rows at
        `PollChoice.num_votes`'s `default=0` (app/models.py:3824); the totals
        loop, if reached, would put -3 and 3 there instead. That is what makes
        the two 0s below a claim about `:3353` rather than about the default --
        and the seeded 7 and 11, on rows of the same names, are the contrary
        baseline for the recreation itself.

        `post.url` is asserted too, as the outer witness the rest of this file
        uses: the Links section's no-url `else` at `:3550-3567` sets
        `post.type = POST_TYPE_ARTICLE` and `post.url = None`.
        """
        post = _seed_post()
        _seed_link_witness(post)
        _seed_poll(post, [('Yes', 7), ('No', 11)])

        update_post_from_activity(post, _poll_update(_choice('Yes', -3), _choice('No', 3),
                                                     end_time=END_TIME))

        db.session.expire_all()

        assert _stored_choices(post) == [('Yes', 0), ('No', 0)]
        assert post.url == f'https://{PEER}/post/1'
        assert post.type == POST_TYPE_POLL


class TestQuestionTotalsUpdate:
    """`app/activitypub/util.py:3355-3368`, the "totals Update" arm: for each
    vote, find the `PollChoice` with that `choice_text` and write the vote's
    `totalItems` onto it.

    Its `continue`s at `:3357-3362` are a verbatim copy of the counting loop's at
    `:3324-3329`, which is what they should be: this loop reads BOTH `name` and
    `replies['totalItems']`, so it needs all three of the counting loop's
    conditions -- unlike the Edit path's recreate loop, which reads only `name`
    and takes only the first of them.

    Every seeded `num_votes` here is non-zero and differs from the total the
    Update carries for it, so no assertion below can be satisfied by the
    `default=0` the column declares (app/models.py:3824) nor by the row being
    left alone.

    None of these tests can assert `mode`: this arm never reads the variable, and
    `:3339` is on the other side of `:3333`.
    """

    def test_a_matching_choice_takes_the_new_total(self, app, db_session,
                                                   redis_lock_only_double):
        """`choice.num_votes = vote['replies']['totalItems']` at `:3365`.

        Ruling E: the arm's `db.session.commit()` at `:3366` is the statement
        this test pins, so the session is expired before the assertion. Without
        that, `autoflush=False` means a commit mutated to `pass` still leaves
        the new value on the in-memory attribute and the assertion passes.
        """
        post = _seed_post()
        _, (yes,) = _seed_poll(post, [('Yes', 7)])

        update_post_from_activity(post, _poll_update(_choice('Yes', 3)))

        db.session.expire_all()

        assert yes.num_votes == 3
        # The totals arm updates in place; it does not delete and re-create the
        # way the Edit path at `:3341-3351` does.
        assert _stored_choices(post) == [('Yes', 3)]

    def test_a_vote_naming_an_unknown_choice_is_ignored(self, app, db_session,
                                                        redis_lock_only_double):
        """The false arm of `if choice:` at `:3364`.

        'Maybe' has no row, so `.first()` returns None and the vote is dropped;
        the 'Yes' vote in the same Update still lands, which is what separates
        "the unknown choice was skipped" from "the loop gave up".
        """
        post = _seed_post()
        _seed_poll(post, [('Yes', 7)])

        update_post_from_activity(post, _poll_update(_choice('Yes', 3), _choice('Maybe', 9)))

        db.session.expire_all()

        assert _stored_choices(post) == [('Yes', 3)]

    def test_two_choices_are_updated_in_one_update(self, app, db_session,
                                                   redis_lock_only_double):
        """`for vote in votes:` at `:3356` running more than once.

        The four numbers in play -- seeds 7 and 11, targets 3 and 5 -- are
        pairwise distinct, so an arm that credited the wrong choice, or wrote
        one vote's total to both rows, cannot produce this pair.
        """
        post = _seed_post()
        _seed_poll(post, [('Yes', 7), ('No', 11)])

        update_post_from_activity(post, _poll_update(_choice('Yes', 3), _choice('No', 5)))

        db.session.expire_all()

        assert _stored_choices(post) == [('Yes', 3), ('No', 5)]

    def test_the_totals_path_returns_before_the_links_section(self, app, db_session,
                                                              redis_lock_only_double):
        """The `return` at `:3368`, under the comment "no URLs in Polls to worry
        about, so return now".

        Same witness the `Video` cluster uses, here behind `_seed_link_witness`:
        the Links section's no-url `else` arm at `:3550-3567`. This test differs
        from the ones above that also call it in that the poll rows change too,
        so it pins `:3368` specifically -- the totals arm's own `return` -- and
        not merely "some `return` in the poll arm was reached".
        """
        post = _seed_post()
        _seed_link_witness(post)
        _seed_poll(post, [('Yes', 7)])

        update_post_from_activity(post, _poll_update(_choice('Yes', 3)))

        db.session.expire_all()

        assert post.url == f'https://{PEER}/post/1'
        assert post.type == POST_TYPE_POLL
        # ... and the arm did run, so this is not a test of an untaken path.
        assert _stored_choices(post) == [('Yes', 3)]

    def test_malformed_votes_are_skipped_and_their_siblings_still_land(
            self, app, db_session, redis_lock_only_double):
        """The three `continue`s the totals loop gained at `:3357-3362`, matching
        the counting loop's at `:3324-3329`.

        The loop reads `vote['name']` at `:3363` and
        `vote['replies']['totalItems']` at `:3365`; before Task 4's fix it read
        both unguarded, and the first malformed sibling raised out of the
        function.

        ONE TEST, THREE GUARDS, on purpose: each malformed entry below is the
        sole thing that reverting one of the three `continue`s trips over, so
        reverting any single one of them fails this test.

          - `{'replies': {'totalItems': 0}}` -- no `name`, so without the first
            guard `:3363` raises `KeyError: 'name'`;
          - `_choice('No')` -- `name` but no `replies`, so without the second
            guard the THIRD guard's own `vote['replies']` at `:3361` raises
            `KeyError: 'replies'`. That line, not `:3365`: measured by reverting
            the guard and reading the traceback, not reasoned about. Execution
            never reaches `:3363`, so unlike 'Maybe' below this entry does NOT
            need a matching row to be observable -- it is seeded for the
            `('No', 11)` unchanged-row witness in the assertion instead;
          - `{'name': 'Maybe', 'replies': {'type': 'Collection'}}` -- `replies`
            without `totalItems`, so without the third guard `:3365` raises
            `KeyError: 'totalItems'`. `replies` is a non-empty dict so that a
            mutant reading it for truth rather than for the key still sees
            something truthy. THIS one names a SEEDED choice deliberately:
            `:3365` sits inside `if choice:` at `:3364`, so with no matching row
            it would never be reached and the guard would be unobservable.

        Written inline rather than through `_choice` where the shape needs it:
        `_choice` builds `replies` only together with `totalItems`, and always
        writes `name`.

        The two well-formed votes bracket the malformed ones, so the loop is
        shown to run past each of them rather than to stop at the last one it
        could reach. Their totals of 3 and 5 sum to the 8 that sends `:3333`
        down this arm; the seeded 7, 11, 13 and 17 are pairwise distinct from
        each other and from both targets, so the two rows that must NOT move are
        asserted against values neither the default nor any arm here could have
        written.
        """
        post = _seed_post()
        _seed_poll(post, [('Yes', 7), ('No', 11), ('Maybe', 13), ('Later', 17)])

        update_post_from_activity(post, _poll_update(
            _choice('Yes', 3),
            {'replies': {'totalItems': 0}},
            _choice('No'),
            {'name': 'Maybe', 'replies': {'type': 'Collection'}},
            _choice('Later', 5)))

        db.session.expire_all()

        assert _stored_choices(post) == [('Yes', 3), ('No', 11), ('Maybe', 13), ('Later', 5)]


# ---------------------------------------------------------------------------
# THE `type == 'Event'` CLUSTER STARTS HERE. Everything from here down to the
# next banner belongs to it; the `Question` cluster's names are the region
# between the previous banner and this one and are not reached into here.
# ---------------------------------------------------------------------------

# The thirteen keys `app/activitypub/util.py:3375-3387` reads off
# `request_json['object']`, one per line and every one of them UNGUARDED: there
# is no `if ... in request_json['object']` anywhere between `:3374`'s `if event:`
# and `:3388`, so a peer document that omits any single one raises `KeyError`
# out of `update_post_from_activity`. Counted off the source rather than taken
# from the plan, which is why the count is stated here: thirteen assignments,
# thirteen subscripts, `:3375` through `:3387` inclusive.
#
# This module SUPPLIES all thirteen rather than repairing the block. Repair
# would mean choosing, per key, what an absent key should leave behind -- the
# column's current value, its declared default, or None -- and the codebase has
# never handled the case, so it is registered rather than fixed.
#
# The values are pairwise distinct from `_seed_event_post`'s baseline below, so
# no assertion on any one of them can be satisfied by the row that was already
# there.
EVENT_FIELDS = {
    'startTime': '2031-03-04T18:30:00',
    'endTime': '2031-03-04T21:45:00',
    'timezone': 'Europe/Berlin',
    'maximumAttendeeCapacity': 250,
    'participantCount': 42,
    'onlineLink': f'https://{PEER}/events/1/stream',
    'joinMode': 'external',
    'externalParticipationUrl': f'https://{PEER}/events/1/rsvp',
    'anonymousParticipation': True,
    'isOnline': True,
    'buyTicketsLink': f'https://{PEER}/events/1/tickets',
    'feeCurrency': 'EUR',
    'feeAmount': 12.5,
}

# `:3375-3376` are `datetime.fromisoformat(...)`, so `Event.start` and
# `Event.end` (`app/models.py:3841-3842`, both naive `db.DateTime`) receive
# datetime objects rather than the peer's string. These strings carry no offset,
# so what `fromisoformat` builds is already naive and Postgres stores it
# unchanged. The offset-bearing case is `Poll.end_poll`'s territory and is
# settled by `END_TIME_STORED` above; nothing here re-derives it.
EVENT_START = datetime(2031, 3, 4, 18, 30)
EVENT_END = datetime(2031, 3, 4, 21, 45)

OLD_BANNER = f'https://{PEER}/banners/old.png'
NEW_BANNER = f'https://{PEER}/banners/new.png'


def _seed_event_post():
    """An Event-typed `Post` with an `Event` row, returned as `(post, event)`.

    There is no `Event` factory in tests/factories.py -- `make_post` is the only
    Post-shaped one and it writes no related rows -- so the row is constructed
    here. `Event.post_id` is the primary key (app/models.py:3840); there is no
    surrogate id.

    Every column `:3375-3387` writes is seeded to a value the Update cannot
    produce, and to one the column's own declaration does not already hold:
    `max_attendees` and `participant_count` are `default=0`
    (app/models.py:3844-3845), `join_mode` is `default='free'` (`:3848`) so the
    baseline is 'restricted' rather than 'free', and `event_fee_amount` is
    `default=0` (`:3854`). `anonymous_participation` and `online` are
    `default=False` (`:3850-3851`) and are seeded False on purpose: the Update
    sets both True, so the assertion is against a value neither the default nor
    the baseline holds, and seeding True would instead make the Update's own
    write unobservable.

    `post.type` is POST_TYPE_EVENT and `post.url` is None, which together make
    `:3418`'s `new_url = old_url if post.type == POST_TYPE_EVENT else None`
    initialise `new_url` to the None already in `post.url`. With no `attachment`
    in the Update, `:3472`'s `old_url != new_url` is then False and the whole
    url-change arm is skipped -- so an Event test measures the Event block alone
    and reaches no network beyond what it registers itself.
    """
    post = _seed_post()
    post.type = POST_TYPE_EVENT
    post.url = None
    event = Event(post_id=post.id,
                  start=datetime(2020, 1, 2, 9, 0),
                  end=datetime(2020, 1, 2, 10, 0),
                  timezone='UTC',
                  max_attendees=5,
                  participant_count=3,
                  online_link=f'https://{PEER}/events/0/stream',
                  join_mode='restricted',
                  external_participation_url=f'https://{PEER}/events/0/rsvp',
                  anonymous_participation=False,
                  online=False,
                  buy_tickets_link=f'https://{PEER}/events/0/tickets',
                  event_fee_currency='USD',
                  event_fee_amount=3.0)
    db.session.add(event)
    db.session.commit()
    return post, event


def _event_update(**overrides):
    """An `Event` object carrying all thirteen keys, plus anything `overrides`
    adds (`image`, in the tests that have one).

    `name` is here for the reason this module's docstring gives: without it the
    head takes the `microblog_content_to_title` arm, which `make_post` leaves
    `body_html=None` for and which would raise before the Event block is
    reached.
    """
    fields = {'type': 'Event', 'name': 'an event'}
    fields.update(EVENT_FIELDS)
    fields.update(overrides)
    return _update(**fields)


def _attach_banner(post, source_url):
    """Give `post` an existing banner and return the `File` row's id.

    `source_url` is the caller's, never a default: the seeded banner and the one
    an Update supplies must be distinguishable by a column, because their ids
    cannot be relied on to differ across runs -- tests/conftest.py:143 truncates
    with RESTART IDENTITY, so `File.id` restarts at 1 in every test.
    """
    banner = File(source_url=source_url)
    db.session.add(banner)
    db.session.commit()
    post.image_id = banner.id
    db.session.commit()
    return banner.id


def _stored_event(post):
    """`post`'s `Event` row, re-SELECTed rather than read off the object
    `_seed_event_post` returned."""
    db.session.expire_all()
    return Event.query.filter_by(post_id=post.id).one()


class TestEventBlock:
    """`if request_json['object']['type'] == 'Event':` at
    `app/activitypub/util.py:3372` -- the block that copies a Mobilizon-shaped
    Event's scheduling fields onto the `Event` row and replaces the post's
    banner, then falls through to the Links section rather than returning the
    way the `Video` and `Question` arms do.

    THE IMAGE BOUNDARY IN THIS CLUSTER. `:3396-3397` is
    `if get_setting('cache_remote_images_locally', True): make_image_sizes(...)`,
    and this file's docstring records that under this harness `make_image_sizes`
    executes rather than enqueues. Verified against current source, the Event
    block's call is the GATED one and the url-change arm's call at `:3504` --
    `make_image_sizes(image.id, 170, 512, 'posts')`, at the same indentation as
    the `post.image = image` above it and under no condition of its own -- is
    not. So technique (2), turning the setting off, is available HERE and
    nowhere else in this function.

    Both techniques appear below, each where it is the only one that can do the
    job:

      - `test_an_update_carrying_an_image_replaces_the_banner` uses technique
        (1), the registered 404. It is the only test in this file that can
        falsify `:3396` at all: the guard's body has no effect a row assertion
        can see, so `http_mock`'s `assert_all_called=True` turning "the
        registered route was never fetched" into a red run is the whole signal
        (tests/README.md:1090-1099).
      - `test_the_banner_is_stored_when_remote_image_caching_is_off` uses
        technique (2), because exercising the guard's False side is precisely
        what it is for and no route can be registered on a path that fetches
        nothing.

    No third technique is introduced, and the two are not mixed inside one test.
    """

    def test_every_event_field_takes_the_update_s_value(self, app, db_session,
                                                        redis_lock_only_double):
        """The thirteen assignments at `:3375-3387`, one assertion each.

        No HTTP fixture: the Update carries no `image`, so `:3391` is False and
        the block reaches neither `make_image_sizes` nor anything else that
        fetches. The session-scoped `block_outbound_http` router
        (tests/conftest.py:214-216) raises on any request that escapes anyway.
        """
        post, _ = _seed_event_post()

        update_post_from_activity(post, _event_update())

        event = _stored_event(post)
        assert event.start == EVENT_START
        assert event.end == EVENT_END
        assert event.timezone == 'Europe/Berlin'
        assert event.max_attendees == 250
        assert event.participant_count == 42
        assert event.online_link == f'https://{PEER}/events/1/stream'
        assert event.join_mode == 'external'
        assert event.external_participation_url == f'https://{PEER}/events/1/rsvp'
        assert event.anonymous_participation is True
        assert event.online is True
        assert event.buy_tickets_link == f'https://{PEER}/events/1/tickets'
        assert event.event_fee_currency == 'EUR'
        assert event.event_fee_amount == 12.5

    def test_an_update_carrying_an_image_replaces_the_banner(self, app, db_session,
                                                             http_mock,
                                                             redis_lock_only_double):
        """`:3388-3397`: the existing banner is dropped, the Update's becomes the
        post's, and the old `File` row is deleted by `:3570-3572`.

        The two `File` rows are told apart by `source_url`, not by id: with
        RESTART IDENTITY the seeded row is id 1 in every run and the new one is
        id 2, and an assertion that read only the id would be measuring the
        sequence. `assert len({old_id, new_id}) == 2` is the explicit guard that
        the pair really is distinct before anything is concluded from it.

        The registered 404 is technique (1) -- see this class's docstring. It is
        what carries `:3396`: `make_image_sizes_async` wraps its `get_request`
        in a bare `except:` (`app/activitypub/util.py:1743-1746`), so the fetch
        leaves no trace a row assertion could read, and only
        `assert_all_called=True` makes "the guard was skipped" visible.
        """
        post, _ = _seed_event_post()
        old_id = _attach_banner(post, OLD_BANNER)
        http_mock.get(NEW_BANNER).respond(404)

        update_post_from_activity(post, _event_update(image={'url': NEW_BANNER}))

        db.session.expire_all()
        new_id = post.image_id
        assert new_id is not None
        assert len({old_id, new_id}) == 2
        assert db.session.get(File, new_id).source_url == NEW_BANNER
        # `:3390` recorded the old id in old_db_entry_to_delete and `:3571`
        # deleted it. Asserting on the row rather than on the local.
        assert File.query.filter_by(id=old_id).count() == 0
        assert File.query.count() == 1

    def test_an_update_with_no_image_clears_the_banner(self, app, db_session,
                                                       redis_lock_only_double):
        """`:3398-3399`'s `else: post.image_id = None`, and `:3388-3390` running
        on the way there.

        `post.image_id` is seeded non-None, so the assertion is not the column's
        own NULL. No HTTP fixture: with no `image` key nothing is fetched.
        """
        post, _ = _seed_event_post()
        old_id = _attach_banner(post, OLD_BANNER)

        update_post_from_activity(post, _event_update())

        db.session.expire_all()
        assert post.image_id is None
        assert File.query.filter_by(id=old_id).count() == 0
        assert File.query.count() == 0

    def test_the_banner_is_stored_when_remote_image_caching_is_off(
            self, app, db_session, redis_lock_only_double):
        """`:3396`'s False side: the `File` is created and attached, and
        `make_image_sizes` is not called.

        Technique (2), and the only test here that uses it -- see this class's
        docstring. tests/test_event_post_type_survives_update.py:161-164 is the
        precedent for the setting; tests/test_ap_actor_json_person.py:932-941 is
        the precedent for the shape of what such a test may honestly claim.

        What it does NOT assert is that no fetch happened. That absence is not
        observable from here: `make_image_sizes_async`'s bare `except:` would
        swallow the harness's own outbound block, so a mutant that ignored the
        setting and fetched anyway would still leave these rows exactly as they
        are. The assertions are on row state, which is the honest limit.
        """
        set_setting('cache_remote_images_locally', False)
        post, _ = _seed_event_post()

        update_post_from_activity(post, _event_update(image={'url': NEW_BANNER}))

        db.session.expire_all()
        assert post.image_id is not None
        assert db.session.get(File, post.image_id).source_url == NEW_BANNER

    def test_a_post_with_no_event_row_is_left_alone(self, app, db_session,
                                                    redis_lock_only_double):
        """`:3374`'s `if event:` taking its False side.

        `Event.query.filter_by(post_id=post.id).first()` at `:3373` returns None
        for a post that has no row, and everything from the thirteen assignments
        through `:3400`'s commit is inside the guard. The witness is the banner:
        the Update supplies an `image`, so a block that ran would have replaced
        `post.image_id` and left two `File` rows behind. Neither happens.

        No HTTP fixture, and that is itself part of the assertion under
        `assert_all_called=True`: registering the banner route here would fail
        the test, because nothing fetches it.
        """
        post = _seed_post()
        post.type = POST_TYPE_EVENT
        post.url = None
        old_id = _attach_banner(post, OLD_BANNER)

        update_post_from_activity(post, _event_update(image={'url': NEW_BANNER}))

        db.session.expire_all()
        assert Event.query.count() == 0
        assert post.image_id == old_id
        assert File.query.count() == 1
        assert db.session.get(File, old_id).source_url == OLD_BANNER

    def test_an_update_whose_type_is_not_event_leaves_the_event_row_alone(
            self, app, db_session, redis_lock_only_double):
        """`:3372`'s own condition taking its False side, on a post that DOES
        have an `Event` row.

        Distinct from the test above: there the row is missing, here the row is
        present and the document's `type` is what keeps the block out. The
        object deliberately carries none of the thirteen keys, which is what a
        peer's non-Event Update actually looks like -- and it means forcing
        `:3372` true raises `KeyError: 'startTime'` at `:3375` rather than
        quietly writing, which is the unguarded-subscript finding this cluster
        registers, executed.

        `post.type` is still POST_TYPE_EVENT, so `:3418` initialises `new_url`
        from `post.url` and the url-change arm stays out of it.
        """
        post, _ = _seed_event_post()

        update_post_from_activity(post, _update(type='Page', name='an event'))

        event = _stored_event(post)
        assert event.start == datetime(2020, 1, 2, 9, 0)
        assert event.timezone == 'UTC'
        assert event.max_attendees == 5
        assert event.join_mode == 'restricted'
        assert event.event_fee_currency == 'USD'


# ---------------------------------------------------------------------------
# THE ATTACHMENT DISPATCH CLUSTER STARTS HERE. Everything from here down
# belongs to it; the `Event` cluster's names are the region between the previous
# banner and this one.
# ---------------------------------------------------------------------------

# A url the dispatch is asked to TAKE. Each arm gets its own, so an assertion on
# `post.url` names which arm wrote it and no two arms can be confused.
LINK_HREF_URL = f'https://{PEER}/attachments/link-href'
LINK_URL_URL = f'https://{PEER}/attachments/link-url'
DOCUMENT_URL = f'https://{PEER}/attachments/document'
AUDIO_URL = f'https://{PEER}/attachments/audio'
IMAGE_URL = f'https://{PEER}/attachments/image'

# A url present in an attachment list that the dispatch must NOT take -- the
# entry after a `break`, or the image a `Link` beats. No route is ever
# registered for it, so a mutation that reached it fails on the unmocked request
# as well as on the assertion.
UNTAKEN_URL = f'https://{PEER}/attachments/never-taken'

# What `post.url` holds before the Update. On PEER on purpose: `:3468` and
# `:3509` both call `domain_from_url`, and equal domains make `:3510`'s
# `old_domain != new_domain` False, which keeps the banned-domain notification
# block (`:3511-3544`, `Site.admins()` and `post.community.moderators()`) out of
# every test here. It is also not any of the urls above, so `:3472`'s
# `old_url != new_url` is True whenever an arm takes one.
SEEDED_URL = f'https://{PEER}/attachments/seeded'

# The Update's `name`. Distinct from `make_post`'s 'a post' default and from
# AUDIO_NAME, so the Audio arm's `post.title = attachment['name']` at `:3439`
# and the head's own title write at `:3187` cannot be confused for each other.
UPDATE_NAME = 'the updated title'
AUDIO_NAME = 'the podcast episode'


def _seed_link_post():
    """A `Post` seeded contrary to everything the Links section writes.

    `post.type` is POST_TYPE_LINK because `Post.type` is
    `default=constants.POST_TYPE_ARTICLE` (app/models.py:1715): the two tests
    below whose expected outcome IS POST_TYPE_ARTICLE would otherwise assert the
    column's own default and pass against a function that never ran.

    POST_TYPE_LINK rather than POST_TYPE_EVENT matters a second time: `:3418`
    initialises `new_url` to `post.url` only for events, so a non-event seed is
    what puts `new_url` at None and lets the dispatch below be the thing that
    changes it.
    """
    post = _seed_post()
    post.type = POST_TYPE_LINK
    post.url = SEEDED_URL
    db.session.commit()
    return post


def _attachment_update(*attachment):
    """An Update whose `object` carries `attachment` as a list.

    `type` is 'Page', which is what a Lemmy post Update actually carries and
    which keeps the `Video`, `Question` and `Event` arms above out of the way.
    """
    return _update(type='Page', name=UPDATE_NAME, attachment=list(attachment))


def _taken(http_mock, url):
    """Register the two routes the url-change arm hits for a url the dispatch
    took, and nothing else.

    HEAD first: `:3480`'s `is_image_url(new_url)` calls `mime_type_using_head`
    (app/utils.py:270, 333), which issues `httpx_client.head(url)`. Answering
    `image/jpeg` sends `is_image_url` down its Content-Type branch
    (app/utils.py:271-273) and makes the answer independent of the url's path,
    so none of the constants above needs a file extension it would not really
    have.

    That True lands the arm on `:3481-3482`, which sets POST_TYPE_IMAGE and
    builds `File(source_url=new_url)` -- so `post.type` is a second witness for
    "this url was taken", and no `opengraph_parse` is reached (`:3491` is in the
    else). `:3504`'s ungated `make_image_sizes` then fetches the File's
    source_url, which is the GET.

    404 for the GET, technique (1) of this module's docstring: it stops
    `make_image_sizes_async` at `:1759`'s status check rather than at a parse,
    so no body is needed and none is served -- tests/README.md:1085-1089's trap
    is about a bare `except:` around a `.json()`, and this path has no parse to
    feed. Precedent: tests/test_ap_actor_json_person.py:918.
    """
    http_mock.head(url).respond(200, headers={'Content-Type': 'image/jpeg'})
    http_mock.get(url).respond(404)


class TestAttachmentDispatchGuard:
    """The four conjuncts of `:3419-3422`, the gate on the attachment walk.

    Both tests here land on the no-url `else` at `:3550-3565`, so both assert
    `post.url is None` and `post.type == POST_TYPE_ARTICLE` -- the pair
    `_seed_link_post` seeds contrary values for.
    """

    def test_an_empty_attachment_list_is_not_walked(self, app, db_session,
                                                    redis_lock_only_double):
        """`len(request_json['object']['attachment']) > 0` at `:3421`.

        An empty list satisfies the first two conjuncts and fails this one.
        Forcing it true does not merely walk an empty list -- `:3422` then
        evaluates `request_json['object']['attachment'][0]` and raises
        IndexError, which is the whole reason the length test precedes the
        subscript.
        """
        post = _seed_link_post()

        update_post_from_activity(post, _attachment_update())

        db.session.expire_all()
        assert post.url is None
        assert post.type == POST_TYPE_ARTICLE

    def test_an_attachment_whose_first_entry_has_no_type_is_not_walked(
            self, app, db_session, redis_lock_only_double):
        """`'type' in request_json['object']['attachment'][0]` at `:3422`.

        The entry carries a `url`, so the list is non-empty and well-formed
        enough to pass the first three conjuncts; only the missing `type` stops
        it. That the walk is skipped rather than entered is what keeps `:3425`'s
        `attachment['type']` from raising -- the guard is checked on entry [0]
        and relied on for every entry.
        """
        post = _seed_link_post()

        update_post_from_activity(post, _attachment_update({'url': UNTAKEN_URL}))

        db.session.expire_all()
        assert post.url is None
        assert post.type == POST_TYPE_ARTICLE


class TestAttachmentDispatchArms:
    """The walk at `:3424-3441` and the conditional second pass at `:3442-3446`.

    Every test here changes `post.url`, which routes on into the url-change arm
    at `:3472-3567`. The assertions stay on what the dispatch itself decided --
    `post.url`, `post.type`, and for the Audio arm `post.title` -- and the image
    and opengraph machinery that arm reaches is left to the task that owns it;
    `_taken` above is only what keeps that machinery off the network.

    Where a list has more than one entry, each extra entry is load-bearing:
    a leading entry with a falsy or absent url falsifies that arm's
    `if new_url: break`, and a trailing entry the walk must never reach
    falsifies the `break` itself.
    """

    def test_a_link_attachment_takes_href_and_stops_the_walk(
            self, app, db_session, http_mock, redis_lock_only_double):
        """`:3425-3431`, the `Link` arm's `href` branch.

        Three entries, and none is decoration:

          - `{'type': 'Link'}` has neither `href` nor `url`, so `:3426` and
            `:3428` both fall through and `new_url` is still None at `:3430`.
            Without that entry, `if new_url:` could be forced true with nothing
            to show for it;
          - the second entry is the one taken, by `href` (Lemmy < 0.19.4);
          - the trailing `Document` is what the `break` at `:3431` prevents
            being reached. Its url is registered nowhere, so a lost `break`
            fails on the unmocked request as well as on the assertion.
        """
        post = _seed_link_post()
        _taken(http_mock, LINK_HREF_URL)

        update_post_from_activity(post, _attachment_update(
            {'type': 'Link'},
            {'type': 'Link', 'href': LINK_HREF_URL},
            {'type': 'Document', 'url': UNTAKEN_URL}))

        db.session.expire_all()
        assert post.url == LINK_HREF_URL
        assert post.type == POST_TYPE_IMAGE

    def test_a_link_attachment_with_no_href_falls_back_to_url(
            self, app, db_session, http_mock, redis_lock_only_double):
        """`:3428-3429`, the `Link` arm's `elif 'url' in attachment` (NodeBB).

        The only test here that reaches that branch: every other `Link` entry in
        this cluster carries `href`, which `:3426` takes first.
        """
        post = _seed_link_post()
        _taken(http_mock, LINK_URL_URL)

        update_post_from_activity(post, _attachment_update(
            {'type': 'Link', 'url': LINK_URL_URL}))

        db.session.expire_all()
        assert post.url == LINK_URL_URL
        assert post.type == POST_TYPE_IMAGE

    def test_a_document_attachment_supplies_the_url_and_stops_the_walk(
            self, app, db_session, http_mock, redis_lock_only_double):
        """`:3432-3435`, the `Document` arm (Mastodon).

        The leading entry's `url` is `''` -- a peer-supplied value, not an
        invented one, and the only shape that can falsify `:3434`'s
        `if new_url:` for this arm, since `:3433` assigns unconditionally. The
        trailing `Link` is what the `break` prevents being reached.
        """
        post = _seed_link_post()
        _taken(http_mock, DOCUMENT_URL)

        update_post_from_activity(post, _attachment_update(
            {'type': 'Document', 'url': ''},
            {'type': 'Document', 'url': DOCUMENT_URL},
            {'type': 'Link', 'href': UNTAKEN_URL}))

        db.session.expire_all()
        assert post.url == DOCUMENT_URL
        assert post.type == POST_TYPE_IMAGE

    def test_an_audio_attachment_supplies_the_url_and_its_name_becomes_the_title(
            self, app, db_session, http_mock, redis_lock_only_double):
        """`:3436-3441`, the `Audio` arm (WordPress podcast), including
        `:3438-3439`'s `post.title = attachment['name']`.

        The title is asserted against AUDIO_NAME, which is neither `make_post`'s
        seeded 'a post' nor the UPDATE_NAME the head wrote at `:3187` a hundred
        lines earlier -- so the assertion can only be satisfied by `:3439`.

        Same three-entry shape as the `Document` test above and for the same two
        reasons. The leading Audio also carries no `name`, so `:3438` is
        exercised on its False side within this test as well.
        """
        post = _seed_link_post()
        _taken(http_mock, AUDIO_URL)

        update_post_from_activity(post, _attachment_update(
            {'type': 'Audio', 'url': ''},
            {'type': 'Audio', 'url': AUDIO_URL, 'name': AUDIO_NAME},
            {'type': 'Link', 'href': UNTAKEN_URL}))

        db.session.expire_all()
        assert post.url == AUDIO_URL
        assert post.title == AUDIO_NAME
        assert post.type == POST_TYPE_IMAGE

    def test_an_audio_attachment_with_no_name_leaves_the_title_alone(
            self, app, db_session, http_mock, redis_lock_only_double):
        """`:3438`'s `if 'name' in attachment:` on a real WordPress-shaped entry
        that has none.

        The title asserted is UPDATE_NAME, what the head's `:3186-3187` wrote
        over `make_post`'s 'a post'. Asserting the seeded 'a post' instead would
        be asserting that the head did not run either.
        """
        post = _seed_link_post()
        _taken(http_mock, AUDIO_URL)

        update_post_from_activity(post, _attachment_update(
            {'type': 'Audio', 'url': AUDIO_URL}))

        db.session.expire_all()
        assert post.url == AUDIO_URL
        assert post.title == UPDATE_NAME

    def test_an_image_only_attachment_list_reaches_the_second_pass(
            self, app, db_session, http_mock, redis_lock_only_double):
        """`:3442-3446`, the second pass, and `:3443`'s `if not new_url:`
        admitting it.

        `Image` matches no arm of the first walk, so the walk completes with
        `new_url` still None and the second pass is what supplies the url
        (PixelFed, PieFed, Lemmy >= 0.19.4).

        The leading `{'type': 'Link'}` is there for the second pass's own
        `attachment['type'] == 'Image'` test at `:3445`: it is an entry that
        reaches the second pass and is not an Image, and it carries no `url`, so
        a mutation that stopped discriminating raises rather than passing. It
        also leaves the first walk empty-handed without adding a second way for
        `new_url` to be set.
        """
        post = _seed_link_post()
        _taken(http_mock, IMAGE_URL)

        update_post_from_activity(post, _attachment_update(
            {'type': 'Link'},
            {'type': 'Image', 'url': IMAGE_URL}))

        db.session.expire_all()
        assert post.url == IMAGE_URL
        assert post.type == POST_TYPE_IMAGE

    def test_a_list_with_both_an_image_and_a_link_takes_the_link(
            self, app, db_session, http_mock, redis_lock_only_double):
        """The point of the second pass being conditional -- `:3442`'s comment
        says Mbin sends link posts with both, and the image is to be ignored.

        The `Image` is first in the list, so the second pass would reach it if
        `:3443` let the pass run at all; `:3430-3431` breaks out with the Link's
        href before that.

        `File.source_url` is asserted as well as `post.url`, because those are
        two separate consequences of the same decision: `:3482` builds the File
        from `new_url`, so an image that had been taken would be the row on disk
        as well as the url on the post.
        """
        post = _seed_link_post()
        _taken(http_mock, LINK_HREF_URL)

        update_post_from_activity(post, _attachment_update(
            {'type': 'Image', 'url': IMAGE_URL},
            {'type': 'Link', 'href': LINK_HREF_URL}))

        db.session.expire_all()
        assert post.url == LINK_HREF_URL
        assert File.query.filter_by(source_url=IMAGE_URL).count() == 0
        assert db.session.get(File, post.image_id).source_url == LINK_HREF_URL


class TestAttachmentDispatchIsNotFedNonLists:
    """`isinstance(request_json['object']['attachment'], list)` at `:3420`.

    Its own cluster because the falsifying document is the one shape the walk
    was never written for, and because the assertion has to be borrowed: with
    the list walk correctly skipped, the only thing left that touches
    `post.url` is the dict arm at `:3448-3450`, so that is what the assertion
    reads. Coverage of `:3448-3450` is a side effect here, not a claim -- the
    test exists so that `:3420` has something to be wrong about.
    """

    def test_a_dict_attachment_is_not_walked_as_a_list(self, app, db_session,
                                                       http_mock,
                                                       redis_lock_only_double):
        """A single dict, which is what Mastodon and a.gup.pe send and what
        `:3448-3449`'s own comment names.

        `isinstance(..., list)` is False, so the four-conjunct guard rejects it
        before `:3421`'s `len(...)` -- which a dict would satisfy -- and before
        `:3422`'s `[0]`, which on a dict is a lookup of the KEY `0` and raises
        `KeyError: 0`. That crash is what makes this the only document in the
        file that can falsify `:3420`; every other attachment here is a list,
        for which the conjunct is true and forcing it true changes nothing.
        """
        post = _seed_link_post()
        _taken(http_mock, DOCUMENT_URL)

        update_post_from_activity(post, _update(
            type='Page', name=UPDATE_NAME,
            attachment={'type': 'Document', 'url': DOCUMENT_URL}))

        db.session.expire_all()
        assert post.url == DOCUMENT_URL
        assert post.type == POST_TYPE_IMAGE
