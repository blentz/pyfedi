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
from app.constants import (NOTIF_REPORT, POST_TYPE_ARTICLE, POST_TYPE_EVENT,
                           POST_TYPE_IMAGE, POST_TYPE_LINK, POST_TYPE_POLL,
                           POST_TYPE_VIDEO, ROLE_ADMIN)
from app.models import (Event, File, Notification, PollChoice, PollChoiceVote,
                        Post, Role, User)
from app.utils import set_setting, utcnow
from tests.factories import (make_community, make_community_member,
                             make_domain, make_instance, make_poll,
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

    def test_an_image_with_no_url_is_treated_as_no_image_at_all(
            self, app, db_session, redis_lock_only_double):
        """THE DEFECT `:3391` was fixed for. `:3391` tested `'image'` and `:3392`
        then read the nested `['url']` unguarded, so a peer sending
        `{"image": {}}` or `{"image": {"type": "Image"}}` -- an Event whose
        banner the peer has removed, and a shape ActivityPub permits -- raised
        `KeyError: 'url'` out of `update_post_from_activity` and abandoned the
        Update, the thirteen Event fields `:3375-3387` had already written
        included.

        FIXED rather than registered, by this campaign's own test: the correct
        spelling already existed 95 lines below, at `:3487`
        (`'image' in request_json['object'] and 'url' in
        request_json['object']['image']`), whose right-hand side `:3488` is
        otherwise byte-identical to `:3392`. The repair is that second conjunct,
        copied. For a DICT `image` no new behaviour is chosen: a urlless dict
        falls to `:3398`'s `else` and is treated as "no image", which is exactly
        how `:3489`'s `else` treats the same shape.

        BUT THE FIX WIDENS BEHAVIOUR FOR TWO OF THE NON-DICT SHAPES, and saying
        it does not would be false. For a `str` whose url lacks the substring
        `url`, and for a list, the PRE-fix code reached `:3392` and raised
        `TypeError`; POST-fix the added conjunct is False, control falls to
        `:3398`'s `else`, and the banner is SILENTLY CLEARED. That is a
        crash-to-silent-skip widening and tests/README.md:2731-2734 names this
        exact second-order effect of adding a membership check in front of a
        subscript. It is recorded, not denied. It remains defensible on the
        uniformity ground this docstring argues -- `:3487` has always behaved
        this way for those shapes, and README's own precedent accepted the same
        justification for making a tag loop uniform -- but "no new behaviour"
        is only true of the dict case.

        THE RESIDUE, WHICH IS REGISTER MATERIAL AT BOTH SITES AND NOT AT `:3392`
        ALONE. `:3487`'s guard is only correct when `image` is a dict, so
        copying it closes the missing-key hole and leaves the non-dict hole open
        at `:3391` and `:3487` together. The table below is the POST-fix state
        of both sites; where a row says "silently dropped" at `:3391` that
        outcome is now CAUSED by this fix rather than pre-existing at that site,
        which is the widening the paragraph above records. Measured, not assumed
        -- `in` on a str is a SUBSTRING test and does not raise
        (tests/README.md:2717-2735, fact 71, which already states this and at
        :2731-2734 already predicts the widening), so the shapes differ from
        each other:

          - `"image": "https://p.example/pic.png"` -- the guard is a substring
            test that answers False, so the banner is silently dropped;
          - `"image": "https://p.example/url.png"` -- the same substring test
            answers True, and the read raises
            `TypeError: string indices must be integers, not 'str'`;
          - `"image": ["https://p.example/pic.png"]` -- False, banner silently
            dropped;
          - `"image": null` or a number -- the GUARD itself raises
            `TypeError: argument of type 'NoneType' is not a container or
            iterable`, before either read.

        Closing that needs an `isinstance(..., dict)` at two sites and a
        decision about the list form (ActivityPub allows `image` to be an
        array), which is new behaviour this codebase has never had. Registered
        -- and registered as an EXTENSION of tests/README.md's fact 71
        (:2717-2735), which already states the substring rule and already
        predicts this widening, not as a new discovery.
        """
        post, _ = _seed_event_post()
        old_id = _attach_banner(post, OLD_BANNER)

        update_post_from_activity(post, _event_update(image={'type': 'Image'}))

        db.session.expire_all()
        assert post.image_id is None
        assert File.query.filter_by(id=old_id).count() == 0
        assert File.query.count() == 0
        # The Update was applied rather than abandoned: pre-fix the KeyError
        # raised after `:3375-3387` had written but before `:3400` committed.
        assert _stored_event(post).timezone == 'Europe/Berlin'

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

    404 for the GET, with NO body, technique (1) of this module's docstring.
    `make_image_sizes_async` stops at `:1759`'s
    `source_image_response.status_code == 200` and never parses anything, so
    there is no body for a body to matter to. The bare `except:` on that path
    (`app/activitypub/util.py:1743-1746`) wraps the FETCH, and the passage that
    describes a bare `except: pass` around a fetch -- naming this very function
    -- is tests/README.md:1090-1095. It is NOT :1071-1073: that passage says the
    opposite of what a fetch-wrapping handler does ("the bare handlers wrap only
    the `.json()` parse, not the fetch"), about `remote_object_to_json` and
    `verify_object_from_source` rather than about this one.

    An empty body is also the side of tests/README.md:1085-1089 to be on rather
    than an exception to it. That passage is the Feed task's mirror image: two
    status-code guards mutated to `True` SURVIVED precisely because the helpers
    served JSON on non-200 responses, and serving a plain-text body on any
    non-200 killed both. Serving JSON here would be repeating the mistake, not
    avoiding it. (`:272` above cites the same passage for the opposite need --
    that is the `Video` cluster's `.json()`-inside-a-bare-`except:` case, where
    a parseable body is what makes the status guard killable. The two cases are
    not in tension: what the body must be depends on whether a parse follows the
    status check, and here none does.) Precedent for the bare 404:
    tests/test_ap_actor_json_person.py:918.
    """
    http_mock.head(url).respond(200, headers={'Content-Type': 'image/jpeg'})
    http_mock.get(url).respond(404)


class TestAttachmentDispatchGuard:
    """The four conjuncts of `:3419-3422`, the gate on the attachment walk.

    Both tests here land on the no-url `else` at `:3550`, whose body runs
    `:3551-3567`, so both assert
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


# ---------------------------------------------------------------------------
# THE URL-CHANGE CLUSTER STARTS HERE, and it is the one cluster in this file
# that deliberately reaches BACKWARDS for names. `app/activitypub/util.py:3472`
# is `if old_url != new_url:` and the arm under it (`:3472-3506`) is what the
# attachment dispatch above FEEDS: the dispatch decides `new_url`, this arm
# decides what the post becomes. Its documents therefore have to be attachment
# documents, so `_seed_link_post`, `_taken`, `SEEDED_URL`, `UNTAKEN_URL` and
# `UPDATE_NAME` are used here as they stand rather than copied, and
# `_attach_banner` is reached back for from the `Event` cluster -- the only name
# taken from further than the previous banner, and taken because the
# alternative was a byte-for-byte duplicate of a helper whose whole body is two
# INSERTs and a commit. Names INTRODUCED below belong to this cluster alone.
# ---------------------------------------------------------------------------

# The four url shapes this arm classifies. All on PEER, and for the reason the
# attachment cluster's SEEDED_URL comment gives: `:3509`'s `old_domain` and
# `:3468`'s `new_domain` then resolve to the same `Domain` row, `:3510`'s
# `new_domain and old_domain != new_domain` is False, and the banned-domain
# notification block at `:3511-3544` stays out. The YOUTUBE_* constants further
# down are the deliberate exception -- they have to leave PEER, and
# `TestUrlChangeYoutubeFixup`'s docstring says what that admits and why it is
# harmless.
CHANGED_IMAGE_URL = f'https://{PEER}/changed/photo'
CHANGED_LINK_URL = f'https://{PEER}/changed/article'

# `is_video_hosting_site` (app/utils.py:316-329) answers True for any url
# containing 'videos/watch' -- its PeerTube rule -- so this one is a video
# hosting site WITHOUT leaving PEER for youtube.com, which would have made
# `old_domain != new_domain` true and dragged in the notification block above.
# `is_video_url` is False for it: that helper reads only the path's extension
# (app/utils.py:294-313). So `:3496`'s FIRST disjunct is the only one true.
CHANGED_VIDEO_SITE_URL = f'https://{PEER}/videos/watch/9f2'

# The mirror image: '.mp4' is one of `is_video_url`'s two extensions and
# 'videos/watch' is absent, so `:3496`'s SECOND disjunct is the only one true.
CHANGED_VIDEO_FILE_URL = f'https://{PEER}/changed/clip.mp4'

# The three thumbnail sources, one per route into `image`: `:3488`'s
# `object['image']['url']`, `:3493`'s `og:image`, and `:3493`'s `og:image:url`.
# Pairwise distinct, so a `File.source_url` assertion names which line built the
# row and no two routes can be confused for each other.
OBJECT_IMAGE_URL = f'https://{PEER}/changed/from-object-image.png'
OG_IMAGE_URL = f'https://{PEER}/changed/from-og-image.png'
OG_IMAGE_URL_TAG_URL = f'https://{PEER}/changed/from-og-image-url.png'

# `:3494` rejects a filename starting with '/'. A site-relative og:image is the
# shape that line exists for, and it is what a real page most often carries.
OG_RELATIVE_IMAGE = '/changed/relative.png'

# `:3495` passes `opengraph.get('og:title')` through `shorten_string(..., 295)`.
# Short enough to come back unchanged (app/utils.py:1611-1613 returns the input
# when `len(input_str) <= max_length`), so the assertion is on this string and
# not on a truncation this cluster does not own.
OG_TITLE = 'the page the peer linked to'

# `:3485`'s alt text, and the caption on the entry the walk actually took --
# which `:3484-3485` must NOT read, because they subscript `[0]`.
ATTACHMENT_ALT = 'a caption the peer supplied'
UNTAKEN_ALT = 'the caption on the entry the url came from'

# THE ONE SHAPE FOR WHICH `fixup_url` DOES NOT RETURN `(url, url)`, and
# therefore the only one that can catch `:3478`, `:3482` or `:3491` using the
# wrong one of the three strings `:3477` leaves in scope -- `new_url`, which is
# what the dispatch chose, and the `thumbnail_url` and `embed_url` it unpacks.
# `fixup_url` (app/utils.py:3363-3399) rewrites a youtube url into a
# `https://youtu.be/<id>`
# THUMBNAIL url and a `https://www.youtube.com/watch?v=<id>` EMBED url, and
# appends `&start=<t>` to the embed when the shared url carried `t`. A peer
# sharing a timestamped youtube link is what that rewrite is for.
YOUTUBE_WATCH_URL = 'https://www.youtube.com/watch?v=abc123&t=90'
YOUTUBE_THUMBNAIL_URL = 'https://youtu.be/abc123'
YOUTUBE_EMBED_URL = 'https://www.youtube.com/watch?v=abc123&start=90'

# The same rewrite reached from the IMAGE branch instead. The path is not
# '/watch', so `fixup_url` falls to `video_id = path[1:]` (app/utils.py:3384)
# and treats the filename as the id -- which leaves `new_url`, `thumbnail_url`
# ('https://youtu.be/photo.png') and `embed_url` all different from each other.
YOUTUBE_IMAGE_URL = 'https://www.youtube.com/photo.png'
YOUTUBE_IMAGE_EMBED_URL = 'https://www.youtube.com/watch?v=photo.png'

# The `File` a post already has before the Update. Its source_url differs from
# every url above because ids cannot tell two rows apart here:
# tests/conftest.py:143 truncates with RESTART IDENTITY, so `File.id` restarts
# at 1 in every test (harness fact 89, the same reason `_attach_banner` takes
# its source_url from the caller).
EXISTING_IMAGE = f'https://{PEER}/changed/already-here.png'


def _seed_image_typed_post():
    """`_seed_link_post`'s post, re-typed POST_TYPE_IMAGE.

    Every test below that asserts `post.type == POST_TYPE_LINK` -- `:3499`'s
    write -- needs a baseline that is none of three things: not POST_TYPE_LINK,
    which `_seed_link_post` leaves and which would make the assertion read back
    its own seed; not POST_TYPE_ARTICLE, which is `Post.type`'s declared default
    (app/models.py:1715); and not POST_TYPE_EVENT, which `:3418` would take as a
    reason to initialise `new_url` from `post.url` instead of None.
    POST_TYPE_IMAGE is what is left, and it is also a value `:3499` can be
    caught writing over.
    """
    post = _seed_link_post()
    post.type = POST_TYPE_IMAGE
    db.session.commit()
    return post


def _linked_update(url, **extra):
    """An Update whose attachment is a single `Link` pointing at `url`.

    The `Link`/`href` arm at `:3425-3431` is the shortest route from a document
    to a chosen `new_url`, and which arm supplied it is settled by the
    attachment cluster above; these tests need only that `new_url` arrives.
    `extra` carries the `image` key the `object['image']['url']` tests add.
    """
    fields = {'type': 'Page', 'name': UPDATE_NAME,
              'attachment': [{'type': 'Link', 'href': url}]}
    fields.update(extra)
    return _update(**fields)


def _not_an_image(http_mock, url, content_type='text/html'):
    """Answer `url`'s HEAD with a Content-Type that is not an image's, putting
    the arm on `:3486`'s `else`.

    `:3480`'s `is_image_url(new_url)` calls `mime_type_using_head`
    (app/utils.py:270, 333), which issues `httpx_client.head(url)`; a
    Content-Type it can parse sends `is_image_url` down its header branch
    (app/utils.py:271-273), which never looks at the path. That is what lets
    CHANGED_VIDEO_FILE_URL keep its real '.mp4' without the extension branch at
    app/utils.py:283-284 having to be trusted not to mistake it for an image.

    Registering the route is not optional. respx raises
    `AllMockedAssertionError` for a request no route matched, and that is an
    `AssertionError`, not an `httpx.HTTPError` -- so `mime_type_using_head`'s
    `except (httpx.HTTPError, httpx.InvalidURL)` would not swallow it and the
    test would die on the escape rather than on its own assertion.
    """
    http_mock.head(url).respond(200, headers={'Content-Type': content_type})


def _thumbnail_is_fetched(http_mock, url):
    """The GET that `:3504`'s `make_image_sizes` walks into for the `File` this
    arm just built, answered with a bodiless 404.

    Technique (1) of this module's docstring, and this cluster is the one the
    docstring says technique (2) cannot serve: `:3504` carries no
    `get_setting('cache_remote_images_locally', True)` of its own -- verified
    against current source, where the only gated call INSIDE
    `update_post_from_activity` is the Event block's at `:3396-3397` -- so
    turning that setting off leaves this call running. Scoped to this function
    deliberately: file-wide the same setting also gates `:764`, `:766`, `:1303`
    and `:1305`, while `:937`, `:939`, `:1111`, `:1113`, `:1497`, `:1499`,
    `:1689` and `:1691` reach `make_image_sizes` without consulting it at all.
    So "the only gated call" is true of this function and false of the file.

    `_taken` above is this same 404 bundled with an IMAGE HEAD, which is what an
    `is_image_url`-true url needs. This is the half a non-image url needs alone,
    because its `File.source_url` is the thumbnail rather than `new_url`.
    """
    http_mock.get(url).respond(404)


def _opengraph_page(http_mock, url, **tags):
    """Serve `url` as an HTML page carrying `tags` as opengraph <meta> elements.

    `:3491`'s `opengraph_parse` (app/utils.py:2997-3007) delegates to
    `parse_page` (app/utils.py:3165-3225), which GETs the page and requires BOTH
    a 200 (app/utils.py:3195-3196) and 'text/html' in the Content-Type
    (app/utils.py:3198-3199) before it parses; either missing makes it return
    False. So the header here is load-bearing rather than decoration, and
    `_unreadable_page` below is the same helper with the 200 withheld.

    A keyword cannot carry a ':', so each tag is named with '_' and translated:
    `og_image_url=...` becomes `<meta property="og:image:url" ...>`.
    """
    meta = ''.join(f'<meta property="{name.replace("_", ":")}" content="{value}">'
                   for name, value in tags.items())
    http_mock.get(url).respond(200, headers={'Content-Type': 'text/html'},
                               text=f'<html><head>{meta}</head><body></body></html>')


def _unreadable_page(http_mock, url):
    """Serve `url` as a 404, so `parse_page` returns False at
    app/utils.py:3195-3196 and `:3492`'s leading `if opengraph` is False.

    False rather than None, and the difference matters to the mutation: forcing
    `:3492`'s first conjunct true reaches `False.get('og:image', '')` and raises
    `AttributeError`, which is a crash-kill and not an assertion-kill.
    """
    http_mock.get(url).respond(404)


class TestUrlChangeGate:
    """`:3472`'s `if old_url != new_url:` -- the gate on the whole arm."""

    def test_an_update_that_repeats_the_current_url_leaves_the_image_alone(
            self, app, db_session, redis_lock_only_double):
        """The dispatch hands back the url the post already has, so the arm is
        skipped entirely.

        The witness is the `File`, not `post.url`: `post.url` is the one thing
        both sides of this gate agree on and would read SEEDED_URL either way.
        The seeded row can only survive if `:3473-3475` never ran -- forcing
        `:3472` true deletes it from disk, records its id in
        `old_db_entry_to_delete`, and then reaches `:3480`'s
        `is_image_url(SEEDED_URL)`, whose HEAD no route here serves.

        No HTTP fixture for exactly that reason: with the arm skipped nothing is
        fetched, and the session-scoped `block_outbound_http` router
        (tests/conftest.py:214-216) raises on anything that escapes.
        """
        post = _seed_link_post()
        old_id = _attach_banner(post, EXISTING_IMAGE)

        update_post_from_activity(post, _attachment_update(
            {'type': 'Document', 'url': SEEDED_URL}))

        db.session.expire_all()
        assert post.url == SEEDED_URL
        assert post.image_id == old_id
        assert File.query.count() == 1
        assert db.session.get(File, old_id).source_url == EXISTING_IMAGE


class TestUrlChangeImageUrl:
    """`:3480-3485` -- `is_image_url(new_url)` true: POST_TYPE_IMAGE, a `File`
    built from the url itself, and alt text taken from the FIRST attachment.

    `_taken` (attachment cluster, above) registers both routes these tests need:
    the image HEAD `:3480` issues, and the 404 `:3504`'s `make_image_sizes`
    walks into.
    """

    def test_an_image_url_takes_the_first_attachment_s_name_as_alt_text(
            self, app, db_session, http_mock, redis_lock_only_double):
        """`:3483-3485` with all three conjuncts true.

        Two attachments, and the second is what makes the `[0]` in `:3484-3485`
        provable rather than merely present. The second pass at `:3443-3446` has
        no `break`, so the LAST `Image` in the list is the one that supplies
        `new_url` -- which is why the url taken is entry [1]'s while the alt text
        asserted is entry [0]'s. A mutant that read the walked attachment
        instead of `[0]` would write UNTAKEN_ALT and fail here.

        UNTAKEN_URL, on entry [0], is registered nowhere: it is overwritten by
        entry [1] before `:3477` ever sees it, so a run that took it instead
        fails on the unmatched HEAD as well as on the two assertions.
        """
        post = _seed_link_post()
        _taken(http_mock, CHANGED_IMAGE_URL)

        update_post_from_activity(post, _attachment_update(
            {'type': 'Image', 'url': UNTAKEN_URL, 'name': ATTACHMENT_ALT},
            {'type': 'Image', 'url': CHANGED_IMAGE_URL, 'name': UNTAKEN_ALT}))

        db.session.expire_all()
        assert post.url == CHANGED_IMAGE_URL
        assert post.type == POST_TYPE_IMAGE
        image = db.session.get(File, post.image_id)
        assert image.source_url == CHANGED_IMAGE_URL
        assert image.alt_text == ATTACHMENT_ALT

    def test_an_attachment_whose_name_is_null_leaves_the_alt_text_unset(
            self, app, db_session, http_mock, redis_lock_only_double):
        """`:3484`'s third conjunct, `...[0]['name'] is not None`, on the peer
        document it exists for.

        WHAT THIS TEST CANNOT DO, stated rather than papered over. The assertion
        `image.alt_text is None` is not one a contrary baseline can be seeded
        for: the row is CREATED by the code under test at `:3482` and
        `File.alt_text` is a plain nullable column with no default
        (app/models.py:372). Dropping the conjunct is an EQUIVALENT mutation for
        the same reason -- `image.alt_text = None` writes back the None the
        fresh `File` already holds, which is tests/README.md fact 75's cause
        4(a), "the body writes what the guard's own False condition asserts is
        already there". So this test is NOT claimed as that conjunct's killer;
        the mutation table records it unkillable and no test is invented to fake
        one. What it does establish on row state is that a null `name` leaves
        the rest of the arm intact: the type and the `File` are still what
        `:3481-3482` wrote.
        """
        post = _seed_link_post()
        _taken(http_mock, CHANGED_IMAGE_URL)

        update_post_from_activity(post, _attachment_update(
            {'type': 'Image', 'url': CHANGED_IMAGE_URL, 'name': None}))

        db.session.expire_all()
        assert post.type == POST_TYPE_IMAGE
        image = db.session.get(File, post.image_id)
        assert image.source_url == CHANGED_IMAGE_URL
        assert image.alt_text is None


class TestUrlChangeObjectImage:
    """`:3487-3488` -- the non-image url whose thumbnail the peer supplied in
    `object['image']['url']` -- and the `else` at `:3489` that falls through to
    opengraph when it did not.
    """

    def test_the_object_s_own_image_url_becomes_the_thumbnail(
            self, app, db_session, http_mock, redis_lock_only_double):
        """`:3487` with both conjuncts true.

        Three assertions for three separate decisions: `post.url` is `:3478`'s,
        `post.type` is `:3499`'s POST_TYPE_LINK, and `File.source_url` is
        `:3488`'s -- which is NOT `post.url`, so a mutant that built the `File`
        from `new_url` the way `:3482` does fails on the third.

        Two routes, and the absent third is half the assertion: no GET is served
        for CHANGED_LINK_URL, so a run that fell through to `:3491`'s
        `opengraph_parse` would find nothing to parse. It would not crash there
        -- `opengraph_parse` catches Exception and returns None
        (app/utils.py:3006-3007), which is why the kill is the `File` assertion
        plus the OBJECT_IMAGE_URL 404 going uncalled, and not a raise.
        """
        post = _seed_image_typed_post()
        _not_an_image(http_mock, CHANGED_LINK_URL)
        _thumbnail_is_fetched(http_mock, OBJECT_IMAGE_URL)

        update_post_from_activity(post, _linked_update(
            CHANGED_LINK_URL, image={'url': OBJECT_IMAGE_URL}))

        db.session.expire_all()
        assert post.url == CHANGED_LINK_URL
        assert post.type == POST_TYPE_LINK
        assert db.session.get(File, post.image_id).source_url == OBJECT_IMAGE_URL

    def test_an_object_image_with_no_url_falls_through_to_opengraph(
            self, app, db_session, http_mock, redis_lock_only_double):
        """`:3487`'s SECOND conjunct alone, on `{"image": {"type": "Image"}}` --
        the same urlless-dict shape `:3391` was fixed to tolerate.

        Forcing that conjunct true here raises `KeyError: 'url'` at `:3488`,
        which is the crash the guard prevents and the reason this shape gets a
        test of its own rather than being folded into the opengraph cluster
        below. The `File` that does get built comes from `og:image`, so the
        assertion names the line that won.

        THE RESIDUE AT `:3487` IS NOT CLOSED HERE and is not this cluster's to
        close: `'url' in request_json['object']['image']` is a SUBSTRING test
        when `image` is a str and raises when it is None, so the guard is only
        correct for a dict. That hole is open at `:3391` and `:3487` jointly and
        is registered as such; the full shape table is in
        `TestEventBlock::test_an_image_with_no_url_is_treated_as_no_image_at_all`
        and the substring mechanism is tests/README.md fact 71.
        """
        post = _seed_image_typed_post()
        _not_an_image(http_mock, CHANGED_LINK_URL)
        _opengraph_page(http_mock, CHANGED_LINK_URL,
                        og_title=OG_TITLE, og_image=OG_IMAGE_URL)
        _thumbnail_is_fetched(http_mock, OG_IMAGE_URL)

        update_post_from_activity(post, _linked_update(
            CHANGED_LINK_URL, image={'type': 'Image'}))

        db.session.expire_all()
        assert post.type == POST_TYPE_LINK
        assert db.session.get(File, post.image_id).source_url == OG_IMAGE_URL


class TestUrlChangeOpengraphFallback:
    """`:3491-3495` -- "Let's see if we can do better than the source instance
    did!", the block that fetches the linked page itself and reads its
    opengraph tags.

    THE PRECEDENCE, read off `:3493` rather than assumed. That line is
    `filename = opengraph.get('og:image') or opengraph.get('og:image:url')`:
    `og:image` WINS, and `og:image:url` is reached only when `og:image` is
    absent or empty. `test_og_image_wins_over_og_image_url` serves both and
    asserts which one the `File` was built from, so the order is pinned by a
    test and not only by this paragraph.

    `opengraph_parse` is a network call -- app/utils.py:3005 delegates to
    `parse_page`, which GETs the page at app/utils.py:3193 -- so every test here
    serves it through `http_mock`. The page fetched is CHANGED_LINK_URL itself:
    `thumbnail_url` is `fixup_url`'s FIRST return value (`:3477`), and for a
    non-youtube url `fixup_url` returns `(url, url)` unchanged
    (app/utils.py:3312, 3365-3366).

    THE THREE TESTS THAT BUILD NO IMAGE ALL SEED ONE FIRST. `:3505-3506`'s
    `else: old_db_entry_to_delete = None` is the only observable difference
    between "no thumbnail was found" and "a thumbnail was found and lost", and
    it is observable only on a post that HAD a `File`: `:3473-3475` records that
    row's id for deletion, `:3506` un-records it, and `:3570-3572` therefore
    leaves it alone. On a post with no image there is nothing for those lines to
    disagree about and `post.image_id is None` would merely be the column's own
    NULL.
    """

    def test_og_image_wins_over_og_image_url(self, app, db_session, http_mock,
                                             redis_lock_only_double):
        """`:3493`'s `or`, with BOTH operands present and different.

        OG_IMAGE_URL_TAG_URL is registered nowhere, so a mutant that swapped the
        operands fails twice over: on the `File.source_url` assertion, and on
        the OG_IMAGE_URL 404 route going uncalled under
        `assert_all_called=True`.

        `alt_text` is asserted as well, because `:3495` builds it in the same
        expression that consumes `filename` -- it is `og:title` and not
        `og:image:alt`, which the page does not carry and which `parse_page`
        would have collected had it been asked for.
        """
        post = _seed_image_typed_post()
        _not_an_image(http_mock, CHANGED_LINK_URL)
        _opengraph_page(http_mock, CHANGED_LINK_URL, og_title=OG_TITLE,
                        og_image=OG_IMAGE_URL,
                        og_image_url=OG_IMAGE_URL_TAG_URL)
        _thumbnail_is_fetched(http_mock, OG_IMAGE_URL)

        update_post_from_activity(post, _linked_update(CHANGED_LINK_URL))

        db.session.expire_all()
        assert post.url == CHANGED_LINK_URL
        assert post.type == POST_TYPE_LINK
        image = db.session.get(File, post.image_id)
        assert image.source_url == OG_IMAGE_URL
        assert image.alt_text == OG_TITLE

    def test_og_image_url_is_taken_when_og_image_is_absent(
            self, app, db_session, http_mock, redis_lock_only_double):
        """`:3492`'s second disjunct and `:3493`'s right-hand operand.

        The page carries `og:image:url` and no `og:image`, so
        `opengraph.get('og:image', '') != ''` is False and the block is admitted
        by the second test alone. Deleting that second disjunct leaves `:3492`
        False and no `File` is built at all, which the assertion below catches.
        """
        post = _seed_image_typed_post()
        _not_an_image(http_mock, CHANGED_LINK_URL)
        _opengraph_page(http_mock, CHANGED_LINK_URL, og_title=OG_TITLE,
                        og_image_url=OG_IMAGE_URL_TAG_URL)
        _thumbnail_is_fetched(http_mock, OG_IMAGE_URL_TAG_URL)

        update_post_from_activity(post, _linked_update(CHANGED_LINK_URL))

        db.session.expire_all()
        assert post.type == POST_TYPE_LINK
        assert db.session.get(File, post.image_id).source_url == OG_IMAGE_URL_TAG_URL

    def test_an_og_image_that_is_a_bare_path_is_rejected(
            self, app, db_session, http_mock, redis_lock_only_double):
        """`:3494`'s `if not filename.startswith('/')`.

        A site-relative `og:image` is admitted by `:3492` -- it is a non-empty
        string -- and rejected one line later, so this is the only route through
        `:3491-3495` that reaches `:3493` and still builds nothing. Forcing
        `:3494` true creates `File(source_url='/changed/relative.png')` and
        attaches it, which both assertions below catch; the fetch that follows
        it does not, because `get_request` refuses a hostless uri
        (app/utils.py:5499-5501) by raising `httpx.HTTPError` straight into
        `make_image_sizes_async`'s bare `except:` (`:1743-1746`).

        No 404 route is registered here, and under `assert_all_called=True` that
        is deliberate: on the unmutated path nothing is fetched for the image.
        """
        post = _seed_image_typed_post()
        old_id = _attach_banner(post, EXISTING_IMAGE)
        _not_an_image(http_mock, CHANGED_LINK_URL)
        _opengraph_page(http_mock, CHANGED_LINK_URL, og_title=OG_TITLE,
                        og_image=OG_RELATIVE_IMAGE)

        update_post_from_activity(post, _linked_update(CHANGED_LINK_URL))

        db.session.expire_all()
        assert post.type == POST_TYPE_LINK
        assert post.image_id == old_id
        assert File.query.count() == 1
        assert db.session.get(File, old_id).source_url == EXISTING_IMAGE

    def test_a_page_carrying_no_og_image_leaves_the_existing_image_row_alone(
            self, app, db_session, http_mock, redis_lock_only_double):
        """`:3492`'s two `!= ''` tests, both False, on a page that parsed fine.

        `parse_page` returns a NON-empty dict here -- it found `og:title` --
        so `:3492`'s leading `if opengraph` is True and the pair of emptiness
        tests is what rejects the page. That is what separates this test from
        `test_a_page_that_cannot_be_read_...` below, where the leading conjunct
        is the one that does the rejecting.

        Forcing either `!= ''` test true reaches `:3493`, where
        `opengraph.get('og:image') or opengraph.get('og:image:url')` is None and
        `:3494`'s `filename.startswith` raises `AttributeError`.
        """
        post = _seed_image_typed_post()
        old_id = _attach_banner(post, EXISTING_IMAGE)
        _not_an_image(http_mock, CHANGED_LINK_URL)
        _opengraph_page(http_mock, CHANGED_LINK_URL, og_title=OG_TITLE)

        update_post_from_activity(post, _linked_update(CHANGED_LINK_URL))

        db.session.expire_all()
        assert post.type == POST_TYPE_LINK
        assert post.image_id == old_id
        assert File.query.count() == 1
        assert db.session.get(File, old_id).source_url == EXISTING_IMAGE

    def test_a_page_that_cannot_be_read_leaves_the_existing_image_row_alone(
            self, app, db_session, http_mock, redis_lock_only_double):
        """`:3492`'s leading `if opengraph`, on the falsy value the real helper
        returns.

        `parse_page` returns the literal `False` for a non-200
        (app/utils.py:3195-3196) and `opengraph_parse` passes it straight back,
        so forcing this conjunct true reaches `False.get('og:image', '')` and
        raises `AttributeError: 'bool' object has no attribute 'get'`. A
        crash-kill, and one the `except Exception` in `opengraph_parse` cannot
        absorb -- that handler wraps the `parse_page` CALL at
        app/utils.py:3005-3007 and has already returned by the time `:3492`
        runs.
        """
        post = _seed_image_typed_post()
        old_id = _attach_banner(post, EXISTING_IMAGE)
        _not_an_image(http_mock, CHANGED_LINK_URL)
        _unreadable_page(http_mock, CHANGED_LINK_URL)

        update_post_from_activity(post, _linked_update(CHANGED_LINK_URL))

        db.session.expire_all()
        assert post.type == POST_TYPE_LINK
        assert post.image_id == old_id
        assert File.query.count() == 1
        assert db.session.get(File, old_id).source_url == EXISTING_IMAGE


class TestUrlChangeTypeClassification:
    """`:3496-3499` -- POST_TYPE_VIDEO for a video hosting site or a video url,
    POST_TYPE_LINK otherwise.

    Both tests carry an `object['image']['url']` so the arm takes `:3487-3488`
    and never reaches opengraph: the classification at `:3496` is downstream of
    which thumbnail was found and independent of it, so serving a page here
    would add a fetch that measures nothing. The POST_TYPE_LINK side of `:3498`
    is asserted by every test in the two clusters above.
    """

    def test_a_video_hosting_site_url_becomes_a_video_post(
            self, app, db_session, http_mock, redis_lock_only_double):
        """`:3496`'s FIRST disjunct, `is_video_hosting_site(embed_url)`.

        Note the argument: `:3496` passes `embed_url`, `fixup_url`'s SECOND
        return value, and `new_url` to `is_video_url` beside it. For a
        non-youtube url those are the same string (app/utils.py:3312,
        3365-3366), so this test cannot tell the two apart and does not claim
        to; what it pins is that a 'videos/watch' url reaches POST_TYPE_VIDEO.
        The seeded type is POST_TYPE_LINK, which is exactly what `:3499` would
        have written, so the assertion cannot be satisfied by the wrong arm.
        """
        post = _seed_link_post()
        _not_an_image(http_mock, CHANGED_VIDEO_SITE_URL)
        _thumbnail_is_fetched(http_mock, OBJECT_IMAGE_URL)

        update_post_from_activity(post, _linked_update(
            CHANGED_VIDEO_SITE_URL, image={'url': OBJECT_IMAGE_URL}))

        db.session.expire_all()
        assert post.url == CHANGED_VIDEO_SITE_URL
        assert post.type == POST_TYPE_VIDEO

    def test_a_video_file_url_becomes_a_video_post(
            self, app, db_session, http_mock, redis_lock_only_double):
        """`:3496`'s SECOND disjunct, `is_video_url(new_url)`.

        '.mp4' with no 'videos/watch' anywhere in it, so
        `is_video_hosting_site` is False and only the right-hand call can admit
        this. The HEAD is answered 'video/mp4' rather than 'text/html': it is
        what a real server sends for this url, and it keeps `is_image_url` on
        its header branch so the '.mp4' never reaches the extension sniffing at
        app/utils.py:283-284.
        """
        post = _seed_link_post()
        _not_an_image(http_mock, CHANGED_VIDEO_FILE_URL, content_type='video/mp4')
        _thumbnail_is_fetched(http_mock, OBJECT_IMAGE_URL)

        update_post_from_activity(post, _linked_update(
            CHANGED_VIDEO_FILE_URL, image={'url': OBJECT_IMAGE_URL}))

        db.session.expire_all()
        assert post.url == CHANGED_VIDEO_FILE_URL
        assert post.type == POST_TYPE_VIDEO


class TestUrlChangeOldImage:
    """`:3473-3475` and `:3500-3504` -- the old `File` is dropped and the new
    one takes its place.

    The `else` at `:3505-3506` is covered by the three seeded-image tests in
    `TestUrlChangeOpengraphFallback`; this is the arm where
    `old_db_entry_to_delete` survives to `:3570-3572` and the row really goes.
    """

    def test_the_old_image_row_is_deleted_when_a_new_thumbnail_replaces_it(
            self, app, db_session, http_mock, redis_lock_only_double):
        """`:3475` recording the old id and `:3571` spending it.

        The two rows are told apart by `source_url`, never by id:
        tests/conftest.py:143 truncates with RESTART IDENTITY, so the seeded row
        is id 1 in every run and the new one id 2, and an assertion reading ids
        alone would be measuring the sequence. `assert len({old_id, new_id})
        == 2` is the explicit guard that the pair is distinct before anything is
        concluded from it (harness fact 89).

        Deleting `:3473-3475` leaves both rows behind and `File.query.count()`
        reads 2. Forcing `:3473` true on a post with no image is a separate
        mutation, killed by `None.delete_from_disk()` in the tests above that
        seed none.
        """
        post = _seed_image_typed_post()
        old_id = _attach_banner(post, EXISTING_IMAGE)
        _not_an_image(http_mock, CHANGED_LINK_URL)
        _thumbnail_is_fetched(http_mock, OBJECT_IMAGE_URL)

        update_post_from_activity(post, _linked_update(
            CHANGED_LINK_URL, image={'url': OBJECT_IMAGE_URL}))

        db.session.expire_all()
        new_id = post.image_id
        assert new_id is not None
        assert len({old_id, new_id}) == 2
        assert db.session.get(File, new_id).source_url == OBJECT_IMAGE_URL
        assert File.query.filter_by(id=old_id).count() == 0
        assert File.query.count() == 1


class TestUrlChangeYoutubeFixup:
    """`:3477`'s `thumbnail_url, embed_url = fixup_url(new_url)`, and the three
    later lines that each pick one of the three strings it leaves behind.

    WHY THIS CLASS EXISTS AT ALL. Everywhere else in this file `fixup_url`
    returns `(url, url)` -- app/utils.py:3312 initialises both to `url` and
    app/utils.py:3365-3366 returns them untouched for any host outside
    `youtube_domains` -- so `new_url`, `thumbnail_url` and `embed_url` are the
    same string and `:3478`'s `post.url = embed_url`, `:3482`'s
    `File(source_url=new_url)` and `:3491`'s `opengraph_parse(thumbnail_url)`
    cannot be caught reading each other's variable. A youtube url is the only
    shape that separates them; without these two tests three real mutations
    survive with nothing but a shrug to explain them.

    These are also the only two tests in this file whose url leaves PEER, so
    they are the only two where `:3510`'s `new_domain and old_domain !=
    new_domain` is True and the block at `:3511-3544` runs. It runs harmlessly:
    `Domain.notify_mods` and `Domain.notify_admins` are both `default=False`
    (app/models.py:3458-3459) on the row `domain_from_url` creates
    (app/utils.py:1590-1593), so neither notification loop is entered and only
    `:3543-3544`'s `post_count += 1` and `post.domain = new_domain` happen.
    Covering that block is Task 7's, not a claim made here.
    """

    def test_a_timestamped_youtube_link_is_stored_as_its_embed_url(
            self, app, db_session, http_mock, redis_lock_only_double):
        """`:3478` storing `embed_url`, and `:3491` fetching `thumbnail_url`.

        Three different strings, and each assertion names a different one:

          - `post.url` is YOUTUBE_EMBED_URL, which is neither what the peer sent
            nor what the opengraph page was fetched from, so `post.url =
            new_url` and `post.url = thumbnail_url` both fail here;
          - the opengraph page is registered at YOUTUBE_THUMBNAIL_URL and
            nowhere else, so `opengraph_parse(new_url)` or
            `opengraph_parse(embed_url)` finds no route. That failure does not
            raise -- `opengraph_parse` catches Exception and returns None
            (app/utils.py:3006-3007) -- so the kill is the `File` assertion plus
            the youtu.be route going uncalled under `assert_all_called=True`;
          - `post.type` is POST_TYPE_VIDEO, from `:3496`'s
            `is_video_hosting_site(embed_url)`.

        The HEAD is on YOUTUBE_WATCH_URL, not on either rewrite: `:3480` passes
        `new_url` to `is_image_url`, which is the peer's own string.
        """
        post = _seed_link_post()
        _not_an_image(http_mock, YOUTUBE_WATCH_URL)
        _opengraph_page(http_mock, YOUTUBE_THUMBNAIL_URL,
                        og_title=OG_TITLE, og_image=OG_IMAGE_URL)
        _thumbnail_is_fetched(http_mock, OG_IMAGE_URL)

        update_post_from_activity(post, _linked_update(YOUTUBE_WATCH_URL))

        db.session.expire_all()
        assert post.url == YOUTUBE_EMBED_URL
        assert post.type == POST_TYPE_VIDEO
        assert db.session.get(File, post.image_id).source_url == OG_IMAGE_URL

    def test_an_image_on_a_youtube_domain_is_stored_from_the_url_the_peer_sent(
            self, app, db_session, http_mock, redis_lock_only_double):
        """`:3482`'s `File(source_url=new_url)`, on the only document that can
        tell `new_url` from `thumbnail_url` inside the image branch.

        `fixup_url` rewrites any youtube-domain url, `/photo.png` included, so
        `post.url` becomes YOUTUBE_IMAGE_EMBED_URL while the `File` keeps the
        url the peer actually sent. `File(source_url=thumbnail_url)` would store
        'https://youtu.be/photo.png' and fail twice: on the assertion, and on
        `_taken`'s 404 -- registered against YOUTUBE_IMAGE_URL because that is
        what `:3504`'s `make_image_sizes` fetches -- going uncalled.

        Contrived as a peer document and said so plainly: an image served from
        youtube.com is not what youtube.com serves. But nothing rejects it on
        the way in (a `Link` attachment's `href` is whatever the peer wrote),
        the mutation it kills is real, and the alternative was recording a
        killable survivor as if it were unkillable.
        """
        post = _seed_link_post()
        _taken(http_mock, YOUTUBE_IMAGE_URL)

        update_post_from_activity(post, _linked_update(YOUTUBE_IMAGE_URL))

        db.session.expire_all()
        assert post.url == YOUTUBE_IMAGE_EMBED_URL
        assert post.type == POST_TYPE_IMAGE
        assert db.session.get(File, post.image_id).source_url == YOUTUBE_IMAGE_URL


# ---------------------------------------------------------------------------
# THE SUSPICIOUS-DOMAIN CLUSTER STARTS HERE, and it is the last one in this
# file. It picks up exactly where the url-change cluster above stops: that one
# owns `:3472-3506`, this one owns `:3508-3567` -- the notification block a
# changed url's DOMAIN can trigger, the two lines that reassign `post.domain`,
# and the no-url `else` arm the whole Links section falls to when an Update
# carries no attachment.
#
# It reuses `_seed_link_post`, `_taken`, `_linked_update`, `_attach_banner`,
# SEEDED_URL, CHANGED_IMAGE_URL, EXISTING_IMAGE and UPDATE_NAME as they stand
# rather than copying them. `_attach_banner` is reached back for from the
# `Event` cluster, which the url-change cluster above already does and says so.
# Names INTRODUCED below belong to this cluster alone.
#
# WHAT THE `else` ARM ALREADY HAD, and what is left. Two tests in
# `TestAttachmentDispatchGuard` (above) already land on `:3550` incidentally and
# assert `post.url is None` and `post.type == POST_TYPE_ARTICLE`. Neither
# touches `:3565`'s `post.image_id = None` or either of the two
# `calculate_cross_posts` calls, which is what `TestUrlClearedToArticle` below
# is for. The comment at `:3552-3563` is a prior sub-project's fix carrying its
# own reasoning; nothing here re-litigates it.
#
# `Site.admins()` NEEDS A ROLE ROW -- see `_make_admin` below for the proof and
# for which of its two arms every fixture in this file takes.
#
# WHAT NO FIXTURE HERE MAY DO: seed `post.domain` on a post that will produce a
# Notification. `:3517` and `:3535` both store `post.domain` -- the `Domain`
# RELATIONSHIP OBJECT, not its name or its id -- into `Notification.targets`,
# which is `db.Column(db.JSON)` (app/models.py:3738). For a post that already
# has a domain the flush raises
# `StatementError: (builtins.TypeError) Object of type Domain is not JSON
# serializable`, measured directly against this harness. So the tests that
# produce notifications leave `post.domain_id` NULL (`orig_post_domain` is then
# a JSON null, which is what they assert), and the test that seeds a contrary
# `post.domain` baseline -- TestSuspiciousDomainReassignment's first -- has both
# notify flags off so no `Notification` is ever built. That is a production
# defect, not a harness quirk, and it is reported rather than repaired: it is
# not a mechanical fix, because no correct spelling of it exists in the tree.
# All four sites carry the same expression (app/models.py:2075,
# app/activitypub/util.py:3517 and :3535, app/shared/post.py:577), so there is
# nothing to copy from and the choice between `post.domain.name` and
# `post.domain_id` is a behaviour decision this slice does not own.
# ---------------------------------------------------------------------------

# A url on a domain that is NOT PEER, which is what makes `:3510`'s
# `old_domain != new_domain` true. Every other url-change constant above is
# deliberately on PEER to keep this block OUT of those tests; this cluster is
# the one that wants it in.
SUSPICIOUS_DOMAIN = 'suspicious.example'
SUSPICIOUS_URL = f'https://{SUSPICIOUS_DOMAIN}/changed/article'

# A peer-supplied `href` `urlparse` accepts and whose `.hostname` is None: the
# only shape that reaches `:3510` with `new_domain` None, because
# `domain_from_url` returns a row only under `if parsed_url and
# parsed_url.hostname:` (app/utils.py:1583-1594) and None otherwise (`:1596`).
# It is exactly the shape `:3463-3467`'s comment names as the case its own
# parse guard does not cover -- "'https:///x' parses, .hostname is None" -- with
# a path on it. HOSTLESS_PATH is what respx has to match on; see
# `_hostless_head_fails`.
HOSTLESS_URL = 'https:///changed/hostless'
HOSTLESS_PATH = '/changed/hostless'

# `targets_data['orig_post_body']` (`:3516`). Seeded because `make_post` sets no
# body at all, so without it that key would assert None against None. The head
# leaves it alone: `:3143` writes `post.body` only when the Update carries
# `content`, and no Update built in this file does.
SEEDED_BODY = 'the body the peer posted before this Update'

# `Domain.post_count` is `default=0` (app/models.py:3456), so `:3543`'s
# `+= 1` measured from an unseeded row would read back 1 -- a value a
# never-incremented row could not produce, but only by one. Seeding it well
# clear of both 0 and 1 makes the assertion name the increment rather than the
# column's history.
SEEDED_POST_COUNT = 7

# `targets_data['post_id']` (`:3514`, and again at `:3532`) is `post.id`, and
# every value a mutation could put there instead is a small id this fixture
# also creates: `post.community_id` (1), `post.user_id` (2), `post.image_id`
# (1), and the two `Domain` ids. tests/conftest.py:143 truncates with
# `RESTART IDENTITY`, so those ids are the same in every test (harness fact 89)
# and a post seeded in the obvious order would take id 1 and make the assertion
# satisfiable by three of them. DECOY_POSTS is how many `Post` rows precede the
# one under test -- `_seed_link_post`'s own, plus DECOY_POSTS - 1 more -- so it
# takes id 5. Measured on the moderator test below, the fixture's ids come out
# as post 5, community 1, author 2, `File` 1, `Domain` 1, moderator 3. Each test
# that asserts on `targets` still guards explicitly rather than trusting that
# arithmetic.
DECOY_POSTS = 4

# A `Role` id that is neither ROLE_ADMIN (4, app/constants.py:81) nor
# ROLE_STAFF (3, app/constants.py:80). The role granted to user 1 in the
# `User.id == 1` test exists only to satisfy `Site.admins()`'s INNER join, and
# a privileged id would have let `Site.staff()` -- the same join narrowed to
# `user_role.c.role_id == ROLE_STAFF`, with no id disjunct at all
# (app/models.py:4002-4005) -- explain that test's row just as well as
# `Site.admins()` does, so swapping one call for the other would have gone
# unnoticed there. Measured: with ROLE_STAFF granted, `Site.admins()` ->
# `Site.staff()` survived that test.
ORDINARY_ROLE = 9


def _seed_suspicious_post():
    """`_seed_link_post`'s post, with a body and an id nothing else shares.

    See DECOY_POSTS above for why the throwaway rows exist. They are inert to
    everything this cluster measures: their `url` is NULL, so
    `calculate_cross_posts`'s `Post.url == self.url` (app/models.py:2371) can
    never match one, and they are in no one's `cross_posts` list.

    `post.domain_id` is deliberately left NULL -- see the banner.
    """
    seed = _seed_link_post()
    for n in range(DECOY_POSTS - 1):
        make_post(seed.community, seed.author,
                  ap_id=f'https://{PEER}/objects/decoy-{n}')
    post = make_post(seed.community, seed.author,
                     ap_id=f'https://{PEER}/objects/measured')
    post.type = POST_TYPE_LINK
    post.url = SEEDED_URL
    post.body = SEEDED_BODY
    db.session.commit()
    return post


def _suspicious_domain(notify_mods=False, notify_admins=False):
    """The `Domain` row `:3468`'s `domain_from_url(new_url)` will find for
    SUSPICIOUS_URL, with every column this cluster reads seeded explicitly.

    `notify_mods`, `notify_admins` and `banned` are all `default=False` and
    `post_count` is `default=0` (app/models.py:3456-3459). Both notify flags are
    passed by every caller rather than defaulted, so the row states what its
    test is about instead of inheriting it, and `post_count` is seeded contrary
    per SEEDED_POST_COUNT.

    `banned` is left False, which is the default: `:3469` returns early for a
    banned domain and would keep every test here out of its own cluster. That
    guard belongs to the block above this one, not to this one.
    """
    domain = make_domain(SUSPICIOUS_DOMAIN)
    domain.notify_mods = notify_mods
    domain.notify_admins = notify_admins
    domain.post_count = SEEDED_POST_COUNT
    db.session.commit()
    return domain


def _make_admin(user, role_id=ROLE_ADMIN):
    """Make `user` an admin as `Site.admins()` (`:3529`) counts them.

    WHICH ARM. `Site.admins()` (app/models.py:3995-4000) returns
    `query(User).filter(User.id.in_(g.admin_ids))` when `g` carries
    `admin_ids`, and otherwise
    `query(User).filter_by(deleted=False, banned=False).join(user_role)
     .filter(or_(user_role.c.role_id == ROLE_ADMIN, User.id == 1))`.
    tests/conftest.py:137 clears `flask.g` before every test and nothing in this
    file sets `admin_ids`, so **every test here takes the JOIN arm**. That is
    the point of saying so: a fixture that stashed `g.admin_ids` would never
    reach the query, and a test claiming to exercise the role path would be
    proving nothing.

    WHY A ROLE ROW IS REQUIRED EVEN FOR USER 1. `.join(user_role)` is an INNER
    join, so a user with no row in that table is dropped before the `or_` is
    evaluated -- `User.id == 1` cannot rescue a user the join has already
    excluded. `make_user` (tests/factories.py:39-65) creates no roles, so
    `_seed_post`'s user 1 is NOT an admin as seeded: `Site.admins()` returns
    `[]` for that fixture. Measured directly against this harness, and it is
    why every admin below is given a role explicitly.

    WHICH DISJUNCT the row then satisfies is `role_id`'s job. ROLE_ADMIN (4,
    app/constants.py:81) satisfies `user_role.c.role_id == ROLE_ADMIN` for any
    user; any other id leaves `User.id == 1` as the only thing that can match,
    which is what the ORDINARY_ROLE caller below is for. `Role` rows are shared
    across calls so two admins can hold the same role without colliding on its
    primary key. The shape mirrors tests/test_request_hooks.py:140.
    """
    role = db.session.get(Role, role_id)
    if role is None:
        role = Role(id=role_id, name=f'role-{role_id}', weight=0)
        db.session.add(role)
        db.session.commit()
    user.roles.append(role)
    db.session.commit()
    return role


def _make_moderator(post, name):
    """A new `User` who moderates `post`'s community, as `:3520`'s
    `post.community.moderators()` counts them.

    `moderators()` (app/models.py:716-722) selects `CommunityMember` rows with
    `is_owner` or `is_moderator` and `is_banned == False`; `make_community`
    creates no membership rows at all, so a community has no moderators until
    one is made here. It is `@cache.memoize`d, which is inert under
    `CACHE_TYPE = 'NullCache'` (tests/conftest.py:68).
    """
    moderator = make_user(post.author.instance, name)
    make_community_member(moderator, post.community, is_moderator=True)
    return moderator


def _hostless_head_fails(http_mock):
    """Fail the HEAD `:3480` issues for HOSTLESS_URL the way a real transport
    fails a url with no host.

    Registering it is not optional and neither is failing it, and both halves
    were measured against this harness:

      - unrouted, respx raises `AllMockedAssertionError`, an `AssertionError`
        that escapes `mime_type_using_head`'s
        `except (httpx.HTTPError, httpx.InvalidURL)` (app/utils.py:345) and
        kills the test on the escape rather than on its own assertion;
      - answered with a RESPONSE, httpx's cookie handling then calls
        `urllib.request.Request(str(response.request.url))`
        (httpx/_models.py:1106, 1250) -- and httpx normalises a hostless url
        down to its bare path, so that is
        `urllib.request.Request('/changed/hostless')`, which raises
        `ValueError: unknown url type`. Not an `httpx.HTTPError` and not an
        `httpx.InvalidURL`, so the same handler misses it.

    A transport-level failure is the third option and the faithful one.
    Unmocked, httpx refuses a hostless url before any response exists -- also
    measured: `httpx.Client().head('https:///changed/hostless')` raises
    `httpx.UnsupportedProtocol`, and both it and the `httpx.ConnectError` used
    here descend TransportError -> RequestError -> HTTPError, so
    `mime_type_using_head` handles them identically. app/utils.py:345 catches
    it, returns '', and `is_image_url` falls to extension sniffing
    (app/utils.py:275-284), which answers False for this path.

    The route matches on `path` alone because respx resolves a url pattern
    against scheme, host and path together and there is no host to resolve.
    """
    http_mock.route(method='HEAD', path=HOSTLESS_PATH).mock(
        side_effect=httpx.ConnectError('no host to connect to'))


def _seed_cross_post(post, name, url, cross_posts=None):
    """Another `Post` in `post`'s community linking to `url`.

    `Post.cross_posts` is `MutableList.as_mutable(ARRAY(db.Integer))` with no
    default (app/models.py:1745), so NULL is what a post that has never been
    cross-post-scanned holds -- which is exactly the False side of `:3547` and
    `:3566`. A caller passing `cross_posts` is seeding the True side.
    """
    partner = make_post(post.community, post.author,
                        ap_id=f'https://{PEER}/objects/{name}')
    partner.url = url
    partner.cross_posts = cross_posts
    db.session.commit()
    return partner


class TestSuspiciousDomainNotifications:
    """`:3519-3542` -- the two notification loops under `:3510`.

    Every test here changes the url from SEEDED_URL (on PEER) to SUSPICIOUS_URL,
    which is the only thing that makes `:3510`'s `old_domain != new_domain`
    true. `_taken` serves both routes that costs: the `image/jpeg` HEAD `:3480`
    issues, and the 404 `:3504`'s ungated `make_image_sizes` walks into. That
    puts the arm on `:3481-3482` rather than through `opengraph_parse`, which is
    the shortest path to `:3508` and keeps these tests off machinery the cluster
    above already owns.

    `notify.author_id` is `1` at both `:3523` and `:3539` -- a literal, not
    anything derived from the Update -- so every assertion on it is on that
    literal. `Notification.author_id` has no column default (app/models.py:3734),
    so 1 is not a value an unwritten row could hold.
    """

    def test_a_moderator_is_notified_when_the_new_domain_notifies_mods(
            self, app, db_session, http_mock, redis_lock_only_double):
        """`:3519-3527`, with `:3528` False -- every column of the row `:3521`
        builds, and the whole `targets` dict `:3513-3518` assembles.

        `orig_post_title` is UPDATE_NAME and not `make_post`'s 'a post': `:3187`
        writes the Update's title several hundred lines before `:3515` reads it
        back, so "orig" here means "before the domain moved", not "before the
        Update". Asserting the seeded title instead would have failed, and
        asserting the dict without saying which value is which would have hidden
        that.

        A moderator exists AND is notified, so the `notify_admins` half is
        provably out on its own merits rather than for want of an admin: user 1
        is not one (see `_make_admin`), and `Site.admins()` returns `[]` here.
        """
        post = _seed_suspicious_post()
        moderator = _make_moderator(post, 'the_moderator')
        domain = _suspicious_domain(notify_mods=True)
        _taken(http_mock, SUSPICIOUS_URL)

        update_post_from_activity(post, _linked_update(SUSPICIOUS_URL))

        db.session.expire_all()
        # harness fact 89: `targets['post_id']` must not be satisfiable by any
        # other id this fixture created.
        others = {post.community_id, post.user_id, post.image_id, domain.id,
                  moderator.id}
        assert len(others | {post.id}) == len(others) + 1

        rows = Notification.query.all()
        assert len(rows) == 1
        assert rows[0].user_id == moderator.id
        assert rows[0].author_id == 1
        assert rows[0].title == 'Suspicious content'
        assert rows[0].url == post.ap_id
        assert rows[0].notif_type == NOTIF_REPORT
        assert rows[0].subtype == 'post_from_suspicious_domain'
        assert rows[0].targets == {'gen': '0',
                                   'post_id': post.id,
                                   'orig_post_title': UPDATE_NAME,
                                   'orig_post_body': SEEDED_BODY,
                                   'orig_post_domain': None}

    def test_an_admin_holding_the_admin_role_is_notified(
            self, app, db_session, http_mock, redis_lock_only_double):
        """`:3528-3542`, with `:3519` False -- the second `Notification` and the
        second `targets_data`, which `:3531-3536` rebuilds identically inside
        the loop.

        The admin is NOT user 1 and NOT a moderator, so `Site.admins()` can only
        have found it through `user_role.c.role_id == ROLE_ADMIN` -- the
        disjunct the test below does not exercise. A moderator is seeded and
        gets nothing, which is what makes `:3519`'s False side observable rather
        than merely uncontradicted.
        """
        post = _seed_suspicious_post()
        moderator = _make_moderator(post, 'the_moderator')
        admin = make_user(post.author.instance, 'the_admin')
        _make_admin(admin)
        domain = _suspicious_domain(notify_admins=True)
        _taken(http_mock, SUSPICIOUS_URL)

        update_post_from_activity(post, _linked_update(SUSPICIOUS_URL))

        db.session.expire_all()
        others = {post.community_id, post.user_id, post.image_id, domain.id,
                  moderator.id, admin.id}
        assert len(others | {post.id}) == len(others) + 1

        rows = Notification.query.all()
        assert len(rows) == 1
        assert rows[0].user_id == admin.id
        assert rows[0].author_id == 1
        assert rows[0].title == 'Suspicious content'
        assert rows[0].url == post.ap_id
        assert rows[0].notif_type == NOTIF_REPORT
        assert rows[0].subtype == 'post_from_suspicious_domain'
        assert rows[0].targets == {'gen': '0',
                                   'post_id': post.id,
                                   'orig_post_title': UPDATE_NAME,
                                   'orig_post_body': SEEDED_BODY,
                                   'orig_post_domain': None}

    def test_user_one_is_notified_as_an_admin_without_holding_the_admin_role(
            self, app, db_session, http_mock, redis_lock_only_double):
        """`Site.admins()`'s `User.id == 1` disjunct, reached from `:3529`.

        `_seed_post`'s user 1 is the community owner, and the role granted here
        is ORDINARY_ROLE -- neither ROLE_ADMIN nor ROLE_STAFF -- so the row
        exists only to satisfy the INNER join and `User.id == 1` is the only
        thing that can match it. Granting ROLE_ADMIN instead would have made
        this test a duplicate of the one above while looking like a different
        one; see ORDINARY_ROLE for why ROLE_STAFF was no good either.

        A second user holding no role at all is seeded and gets nothing, which
        is the other half of the same claim: admin-ness here is the role row
        plus the id, not the id alone.
        """
        post = _seed_suspicious_post()
        owner = db.session.get(User, 1)
        _make_admin(owner, role_id=ORDINARY_ROLE)
        bystander = make_user(post.author.instance, 'no_role_at_all')
        _suspicious_domain(notify_admins=True)
        _taken(http_mock, SUSPICIOUS_URL)

        update_post_from_activity(post, _linked_update(SUSPICIOUS_URL))

        db.session.expire_all()
        assert len({owner.id, bystander.id}) == 2
        rows = Notification.query.all()
        assert len(rows) == 1
        assert rows[0].user_id == owner.id
        assert rows[0].subtype == 'post_from_suspicious_domain'
        assert Notification.query.filter_by(user_id=bystander.id).count() == 0

    def test_an_admin_who_also_moderates_the_community_is_notified_once(
            self, app, db_session, http_mock, redis_lock_only_double):
        """`:3530`'s `if admin.id not in already_notified:`, which is the whole
        reason `already_notified` (`:3512`, `:3527`) exists.

        KEY SPACE, established from the models rather than assumed:
        `already_notified` collects `community_member.user_id` (`:3527`) and is
        tested against `admin.id` (`:3530`). `CommunityMember.user_id` is
        `db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)`
        (app/models.py:3500) and `admin` is a `User` row returned by
        `Site.admins()` (app/models.py:3995-4000), so both are `user.id` values.
        Same key space, and the de-duplication is sound.

        Two admins, and the second is what makes the de-duplication provable
        rather than merely consistent with one row: without `:3530` the
        moderator-admin would hold TWO notifications and the total would be
        three, so a test with only the moderator-admin could not tell "skipped"
        from "the admin loop never ran".
        """
        post = _seed_suspicious_post()
        mod_admin = _make_moderator(post, 'moderator_and_admin')
        _make_admin(mod_admin)
        plain_admin = make_user(post.author.instance, 'admin_only')
        _make_admin(plain_admin)
        _suspicious_domain(notify_mods=True, notify_admins=True)
        _taken(http_mock, SUSPICIOUS_URL)

        update_post_from_activity(post, _linked_update(SUSPICIOUS_URL))

        db.session.expire_all()
        assert len({mod_admin.id, plain_admin.id}) == 2
        assert Notification.query.filter_by(user_id=mod_admin.id).count() == 1
        assert Notification.query.filter_by(user_id=plain_admin.id).count() == 1
        assert Notification.query.count() == 2

    def test_a_domain_that_notifies_nobody_still_takes_the_post(
            self, app, db_session, http_mock, redis_lock_only_double):
        """`:3519` and `:3528` both False, with a moderator and an admin present
        to be skipped.

        `post.domain_id` moving from NULL to the new row is what makes the zero
        notification count a finding rather than an absence: `:3544` is inside
        the same block, so it proves `:3510` was true and both loops were
        reached and declined. Forcing either guard true produces a row here.
        """
        post = _seed_suspicious_post()
        moderator = _make_moderator(post, 'the_moderator')
        admin = make_user(post.author.instance, 'the_admin')
        _make_admin(admin)
        domain = _suspicious_domain()
        _taken(http_mock, SUSPICIOUS_URL)

        update_post_from_activity(post, _linked_update(SUSPICIOUS_URL))

        db.session.expire_all()
        assert len({moderator.id, admin.id}) == 2
        assert Notification.query.count() == 0
        assert post.domain_id == domain.id


class TestSuspiciousDomainReassignment:
    """`:3543-3544`, and the two conjuncts of `:3510` that gate them."""

    def test_the_new_domain_takes_the_post_and_counts_it(
            self, app, db_session, http_mock, redis_lock_only_double):
        """`:3543`'s `new_domain.post_count += 1` and `:3544`'s
        `post.domain = new_domain`, both against contrary baselines.

        The post starts on the domain its OWN url is on, which is what a post
        created through `Post.new` holds, so `post.domain_id` is not merely
        NULL-to-something here. Both notify flags are off, and that is
        load-bearing rather than incidental: `:3517` would put this seeded
        `Domain` OBJECT into `Notification.targets`, a JSON column, and the
        flush would raise. See the cluster banner.

        The old domain's own `post_count` is asserted unchanged: `:3543`
        increments the NEW row, and nothing in this block decrements the old
        one -- a post moving domains leaves the count it left behind too high.
        That is this function's behaviour as written and the assertion records
        it rather than endorsing it.
        """
        post = _seed_suspicious_post()
        new_domain = _suspicious_domain()
        old_domain = make_domain(PEER)
        old_domain.post_count = SEEDED_POST_COUNT
        post.domain_id = old_domain.id
        db.session.commit()
        _taken(http_mock, SUSPICIOUS_URL)

        update_post_from_activity(post, _linked_update(SUSPICIOUS_URL))

        db.session.expire_all()
        assert len({old_domain.id, new_domain.id}) == 2
        assert post.domain_id == new_domain.id
        assert new_domain.post_count == SEEDED_POST_COUNT + 1
        assert old_domain.post_count == SEEDED_POST_COUNT

    def test_a_url_change_inside_the_same_domain_leaves_the_block_out(
            self, app, db_session, http_mock, redis_lock_only_double):
        """`:3510`'s `old_domain != new_domain`, False.

        SEEDED_URL and CHANGED_IMAGE_URL are both on PEER, so `:3468` and
        `:3509` resolve to the same `Domain` row and the comparison is between
        an object and itself. `post.url` changes, so `:3472` is still true and
        the whole arm above still runs -- this is the block's own gate being
        measured, not the arm's.

        The domain notifies mods and a moderator exists, so forcing this
        conjunct true produces a `Notification` as well as moving `post_count`
        off SEEDED_POST_COUNT. `post.domain_id` is left NULL for the reason the
        banner gives, and that it STAYS NULL is the third witness.
        """
        post = _seed_suspicious_post()
        moderator = _make_moderator(post, 'the_moderator')
        domain = make_domain(PEER)
        domain.notify_mods = True
        domain.post_count = SEEDED_POST_COUNT
        db.session.commit()
        _taken(http_mock, CHANGED_IMAGE_URL)

        update_post_from_activity(post, _linked_update(CHANGED_IMAGE_URL))

        db.session.expire_all()
        assert post.url == CHANGED_IMAGE_URL
        assert Notification.query.filter_by(user_id=moderator.id).count() == 0
        assert Notification.query.count() == 0
        assert domain.post_count == SEEDED_POST_COUNT
        assert post.domain_id is None

    def test_a_hostless_new_url_leaves_the_block_out(
            self, app, db_session, http_mock, redis_lock_only_double):
        """`:3510`'s leading `new_domain`, False -- the conjunct that stops
        `:3519` dereferencing None.

        HOSTLESS_URL has no host, so `domain_from_url` returns None
        (app/utils.py:1583-1596) and `:3469`'s `if new_domain and` lets it
        through. `old_domain` is a real `Domain` row, so the second conjunct is
        TRUE and this one is the only thing keeping the block out -- deleting it
        reaches `new_domain.notify_mods` at `:3519` and raises AttributeError,
        which is a crash-kill and not an assertion-kill.

        ONE ROUTE, and it fails rather than answers -- `_hostless_head_fails`
        gives the two measurements behind that. Its '' sends `is_image_url` to
        extension sniffing, which answers False for this path, so the arm takes
        `:3486`'s `else`. `:3491`'s `opengraph_parse` then reaches
        `get_request`, which refuses a hostless uri at app/utils.py:132-134 by
        raising `httpx.HTTPError` BEFORE issuing anything, and
        `opengraph_parse`'s `except Exception` (app/utils.py:3004-3007) turns
        that into None -- so `:3492` is False, no `File` is built, `:3504` is
        never reached, and no GET route is registered under
        `assert_all_called=True`.

        `post.type` cannot be the witness that the arm really ran: `:3499`
        writes POST_TYPE_LINK, which is what `_seed_suspicious_post` already
        holds. `post.url` carries that claim instead.
        """
        post = _seed_suspicious_post()
        _make_moderator(post, 'the_moderator')
        domain = make_domain(PEER)
        domain.notify_mods = True
        domain.notify_admins = True
        domain.post_count = SEEDED_POST_COUNT
        db.session.commit()
        _hostless_head_fails(http_mock)

        update_post_from_activity(post, _linked_update(HOSTLESS_URL))

        db.session.expire_all()
        assert post.url == HOSTLESS_URL
        assert post.image_id is None
        assert Notification.query.count() == 0
        assert domain.post_count == SEEDED_POST_COUNT
        assert post.domain_id is None


class TestUrlChangeCrossPosts:
    """`:3547-3548` -- `calculate_cross_posts(url_changed=True)` after the url
    moved, and the `is not None` guard on it.

    Both tests here stay on PEER (CHANGED_IMAGE_URL), which keeps the
    suspicious-domain block above out and leaves the cross-post arm as the only
    thing the assertions can be reading.
    """

    def test_a_changed_url_is_re_matched_against_the_other_posts(
            self, app, db_session, http_mock, redis_lock_only_double):
        """`:3548` with `:3547` true, measured on three rows at once.

        `calculate_cross_posts(url_changed=True)` (app/models.py:2346-2389) does
        two separable things, and one partner per side is what separates them:
        `:2350-2358` unlinks the post from the partners it was matched to under
        the OLD url, and `:2371-2388` links it to the partners sharing the NEW
        one. A single partner could be explained by either.

        `old_partner` keeps SEEDED_URL and `new_partner` holds
        CHANGED_IMAGE_URL, so which list each ends up in names which half ran.
        """
        post = _seed_suspicious_post()
        old_partner = _seed_cross_post(post, 'old-partner', SEEDED_URL,
                                       [post.id])
        new_partner = _seed_cross_post(post, 'new-partner', CHANGED_IMAGE_URL)
        post.cross_posts = [old_partner.id]
        db.session.commit()
        _taken(http_mock, CHANGED_IMAGE_URL)

        update_post_from_activity(post, _linked_update(CHANGED_IMAGE_URL))

        db.session.expire_all()
        assert len({post.id, old_partner.id, new_partner.id}) == 3
        assert post.cross_posts == [new_partner.id]
        assert db.session.get(Post, old_partner.id).cross_posts == []
        assert db.session.get(Post, new_partner.id).cross_posts == [post.id]

    def test_a_post_that_has_never_been_matched_is_left_unmatched(
            self, app, db_session, http_mock, redis_lock_only_double):
        """`:3547` False -- `post.cross_posts` NULL, which is what a post that
        has never been scanned holds.

        The kill is on `new_partner`, not on `post`: forcing `:3547` true calls
        `calculate_cross_posts(url_changed=True)` with `self.cross_posts` None,
        which skips the unlink at app/models.py:2350 (None is falsy) but still
        runs the match at `:2371-2388` -- so `new_partner` would acquire
        `[post.id]` and `post` would acquire `[new_partner.id]`. Both are
        asserted, so this guard is killable in both directions.
        """
        post = _seed_suspicious_post()
        new_partner = _seed_cross_post(post, 'new-partner', CHANGED_IMAGE_URL)
        _taken(http_mock, CHANGED_IMAGE_URL)

        update_post_from_activity(post, _linked_update(CHANGED_IMAGE_URL))

        db.session.expire_all()
        assert len({post.id, new_partner.id}) == 2
        assert post.cross_posts is None
        assert db.session.get(Post, new_partner.id).cross_posts is None


class TestUrlClearedToArticle:
    """`:3550-3567` -- the `else` the arm falls to when the Update carried no
    url at all.

    Every Update here is `_update(type='Page', name=UPDATE_NAME)` with no
    `attachment` key, which fails `:3419`'s first conjunct and `:3448`'s, so
    `new_url` is still the None `:3418` initialised it to. `:3472` is then true
    because the post has a url and the Update does not.

    No HTTP fixture in any of them: nothing on this side of the branch fetches
    anything, and the session-scoped `block_outbound_http` router
    (tests/conftest.py:214-216) raises on anything that escapes.
    """

    def test_an_update_with_no_url_clears_the_post_s_image(
            self, app, db_session, redis_lock_only_double):
        """`:3565`'s `post.image_id = None`, and the `File` row `:3570-3572`
        then deletes.

        `:3551`'s POST_TYPE_ARTICLE and `:3564`'s `post.url = None` are asserted
        alongside it because they are the same statement group, but they are not
        what this test adds: `TestAttachmentDispatchGuard` above already reaches
        both. The image is the uncovered half. `_attach_banner` gives the post a
        `File` to lose, which is also what puts `:3473-3475` on its true side,
        and `old_db_entry_to_delete` is what carries the row's id to the DELETE
        after the commit.
        """
        post = _seed_suspicious_post()
        old_id = _attach_banner(post, EXISTING_IMAGE)

        update_post_from_activity(post, _update(type='Page', name=UPDATE_NAME))

        db.session.expire_all()
        assert post.type == POST_TYPE_ARTICLE
        assert post.url is None
        assert post.image_id is None
        assert File.query.filter_by(id=old_id).count() == 0
        assert File.query.count() == 0

    def test_a_post_that_loses_its_url_is_unmatched_from_its_cross_posts(
            self, app, db_session, redis_lock_only_double):
        """`:3567`'s `calculate_cross_posts(delete_only=True)`, with `:3566`
        true.

        `delete_only` is what makes this reachable at all: `:3564` has already
        set `post.url` to None one line earlier, and
        `calculate_cross_posts`'s own first line returns immediately for a
        url-less post UNLESS `delete_only` is set (app/models.py:2347-2348).
        The call then unlinks both directions at app/models.py:2350-2358 and
        returns at `:2359-2360` before any re-matching.

        Both sides are asserted. `post.cross_posts` becomes `[]` rather than
        None -- `:2353` clears the list in place -- and the partner loses
        `post.id` from its own.
        """
        post = _seed_suspicious_post()
        partner = _seed_cross_post(post, 'partner', SEEDED_URL, [post.id])
        post.cross_posts = [partner.id]
        db.session.commit()

        update_post_from_activity(post, _update(type='Page', name=UPDATE_NAME))

        db.session.expire_all()
        assert len({post.id, partner.id}) == 2
        assert post.url is None
        assert post.cross_posts == []
        assert db.session.get(Post, partner.id).cross_posts == []
