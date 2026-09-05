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
from app.constants import POST_TYPE_VIDEO
from app.utils import utcnow
from tests.factories import (make_community, make_instance, make_post,
                             make_site, make_user)

PEER = 'peer.example'

LIKES_URL = f'https://{PEER}/videos/1/likes'
DISLIKES_URL = f'https://{PEER}/videos/1/dislikes'


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
    is in this slice's scope -- three definitions plus an import path, which is
    worse than three definitions.
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

        The witness is the Links section's no-url `else` arm at `:3542-3557`.
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
