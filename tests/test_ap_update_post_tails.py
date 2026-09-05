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
`app/activitypub/util.py:3388-3389` and nothing gates the one in the url-change
arm at `:3496`, which runs whenever that arm builds an image. So this file
standardises on TECHNIQUE (1) -- it works at both call sites, and
`assert_all_called=True` turns the registered 404 into positive evidence that
the image path was entered. The `Video` cluster in this file never reaches
either call site: it returns at `:3308-3309` before the Links section, and the
block itself creates no `File`. So no test here registers an image route; the
guidance above is for the later tails.
"""
import contextlib

import httpx
import pytest

from app import db
from app.activitypub.util import update_post_from_activity
from app.constants import POST_TYPE_POLL, POST_TYPE_VIDEO
from app.models import PollChoice
from app.utils import utcnow
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

        The witness is the Links section's no-url `else` arm at `:3542-3559`.
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
    `app/activitypub/util.py:3345-3349` (`i = 1`, incremented per vote).
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

    All four of the poll arm's `return`s -- `:3320`, `:3337`, `:3351` and
    `:3360` -- leave `update_post_from_activity` before its Links section, whose
    no-url `else` arm at `:3542-3559` sets `post.type = POST_TYPE_ARTICLE` and
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
    assert `mode`, because the totals path (`:3353-3360`) never reads the
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


class TestQuestionTotalsUpdate:
    """`app/activitypub/util.py:3353-3360`, the "totals Update" arm: for each
    vote, find the `PollChoice` with that `choice_text` and write the vote's
    `totalItems` onto it.

    Every seeded `num_votes` here is non-zero and differs from the total the
    Update carries for it, so no assertion below can be satisfied by the
    `default=0` the column declares (app/models.py:3824) nor by the row being
    left alone.

    None of these tests can assert `mode`: this arm never reads the variable, and
    `:3339` is on the other side of `:3333`.
    """

    def test_a_matching_choice_takes_the_new_total(self, app, db_session,
                                                   redis_lock_only_double):
        """`choice.num_votes = vote['replies']['totalItems']` at `:3357`.

        Ruling E: the arm's `db.session.commit()` at `:3358` is the statement
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
        # way the Edit path at `:3341-3349` does.
        assert _stored_choices(post) == [('Yes', 3)]

    def test_a_vote_naming_an_unknown_choice_is_ignored(self, app, db_session,
                                                        redis_lock_only_double):
        """The false arm of `if choice:` at `:3356`.

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
        """`for vote in votes:` at `:3354` running more than once.

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
        """The `return` at `:3360`, under the comment "no URLs in Polls to worry
        about, so return now".

        Same witness the `Video` cluster uses, here behind `_seed_link_witness`:
        the Links section's no-url `else` arm at `:3542-3559`. This test differs
        from the ones above that also call it in that the poll rows change too,
        so it pins `:3360` specifically -- the totals arm's own `return` -- and
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
