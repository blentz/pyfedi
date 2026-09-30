"""`app/shared/post.py`'s reader interactions -- Group A of five.

SCOPE. The eight reader-facing functions, 63 uncovered statements and 51
uncovered branch arcs when this file was started:

  vote_for_post :31-74 (29/18), vote_for_poll :1146-1187 (20/18),
  bookmark_post :77-94 (4/4), subscribe_post :115-152 (4/4),
  mark_post_read :1115-1130 (2/3), get_post_flair_list :1133-1143 (2/2),
  remove_bookmark_post :97-112 (1/2), extra_rate_limit_check :155-160 (1/0).

THE HARNESS IS THE POINT OF THIS FILE, and it is not the harness the four
maintenance test files use. Those call Celery task bodies directly: no request,
no user, no session. Nothing here can do that.

NO `user=` ESCAPE HATCH. tests/test_shared_post_edit.py runs without login or
request context because `edit_post` takes `user=` and checks `if not user:` on
both source arms (:252 and :316). No Group A function has that parameter.
vote_for_post:41, bookmark_post:78, remove_bookmark_post:98, subscribe_post:117
and vote_for_poll:1150 read `current_user` or call `authorise_api_user` with no
way around it.

THE SRC_API ARM DOES NOT NEED A REQUEST CONTEXT. Probe D (Task 1) proved this
wrong: vote_for_post:50 and vote_for_poll:1152 both read
`if user.banned or user_ip_banned():` AFTER the source fork, so both arms reach
it, and `user_ip_banned` (app/utils.py:2311-2314) does call `ip_address`, which
app/utils.py:2308 binds to app/__init__.py's `get_ip_address`. But
`get_ip_address` (app/__init__.py:68-77) wraps its `request` read in
`try/except RuntimeError` for exactly this case -- its own comment says "no
application or request context (e.g. a CLI command)" -- and returns `''`
instead of raising. `user_ip_banned` then sees a falsy IP and returns `None`,
so `user.banned or user_ip_banned()` is falsy and the guard lets a
context-free call straight through. A bearer-token call with no request
context does not fail inside `user_ip_banned`; it does not fail there at all.
CONSEQUENTLY, no SRC_API-arm test in this round wraps its call in `web_ctx`
-- `web_ctx` stays reserved for the SRC_WEB arms, which need `flash` and
`request.args` to be available.

WHAT ACTUALLY BLOCKS AN API-ARM TEST IS AN `ap_id`, NOT CONTEXT.
`authorise_api_user`'s condition at app/utils.py:3628,
`if user.ap_id is not None or user.verified is False or user.banned is True or
user.deleted is True:`, raises `Exception('incorrect_login')` at its body,
app/utils.py:3629, for any bearer token whose user has a non-None `ap_id` --
before the source fork ever reaches `user_ip_banned`. Probe D's `seed_post_context()`
originally called `make_user(instance, 'author')` and `make_user(instance,
'voter')` with `local` left at its factory default of `False`
(tests/factories.py:41), so both got a non-None `ap_id` and every bearer-token
call against them failed here, unrelated to request context. Round-1 controller
ruling: `seed_post_context()` now passes `local=True` to both calls (see `seed_post_context()`'s own
docstring), so this file's `author` and `voter` authorise cleanly and this
paragraph is a historical record, not a live hazard in this file. It remains a
hazard for any test elsewhere that builds a bearer-token user without
`local=True`, exactly as tests/test_shared_post_edit.py:202 already avoids it.

`redis_double` REACHES THE FUNCTION-BODY IMPORT BUT BREAKS THE LOCK IT TAKES.
Probe A (Task 1) resolved the question tests/conftest.py:459-463 raises: yes,
`redis_double` DOES reach `mark_post_read:1126`'s `from app import
redis_client`, contrary to what that warning leaves open. The traceback showed
`redis_client.lock(...)` (`:1127`) acquiring its lock against a
`fakeredis._connection.FakeRedisConnection`, proof the fixture's monkeypatch of
`app.redis_client` is visible even to a function-body import. But the fixture
still breaks the call: the lock's `__exit__` releases through a Lua script over
`EVALSHA`, and fakeredis does not implement that command, so any call through
`mark_post_read` -- directly, or transitively through `bookmark_post`,
`vote_for_post`, or `vote_for_poll` -- raises this verbatim error under
`redis_double`:

    redis.exceptions.ResponseError: unknown command 'evalsha', with args
    beginning with:

The remedy is to omit `redis_double` entirely and let the call bind to the real
Redis in the compose stack (`pyfedi_test-redis_1`), which implements `EVALSHA`
and completes the lock's acquire-and-release cycle normally. NO TEST IN THIS
FILE REQUESTS `redis_double` for exactly this reason -- Groups B and C's
`delete_post:765` and `votes_cast_today` (app/models.py:47-52) do the same
function-body redis import and inherit this same finding.

REAL REDIS IS SHARED ACROSS THE WHOLE TEST SESSION -- WATCH FOR KEY COLLISIONS.
Dropping `redis_double` for the reason above means every test in this round
that touches Redis binds to the one Redis instance the whole compose stack
shares for the session, not a fixture-scoped double that resets between tests.
`votes_cast_today` (app/models.py:47-52) reads the key
`votes_cast_{date.today()}_{user_id}`, and tests/conftest.py:131 resets
Postgres id sequences after every test, so user ids are REUSED across tests in
the same run. A test that increments or sets that key and does not clean it up
leaves a value a later test's user id can collide with, silently corrupting
that later test's vote-quota assertion. Task 6, which exercises
`votes_cast_today`'s callers, is expected to wrap its Redis-touching tests in a
context manager that deletes the key on exit; no test anywhere in this round
should call `redis_client.set(...)` on a `votes_cast_*` key without a matching
cleanup.

THE WEB ARMS RETURN A FLASK `Response`, NEVER A `str`. Probe B (Task 1) found
all three WEB templates (`post/_post_voting_buttons.html`,
`post/_post_voting_buttons_masonry.html`, `post/_post_notification_toggle.html`)
render without error against `seed_post_context()`'s objects and a logged-in `web_ctx`
user -- no missing `g` attribute, no missing `Site` row. But `render_template`
as imported at app/shared/post.py:23 is `app.utils.render_template`, not
Flask's own. app/utils.py:74-79 calls `flask.render_template` internally to
get a plain string, then app/utils.py:86-99 wraps that string in
`make_response(content)` and adds `ETag`/`Cache-Control`/`Link` headers before
returning it. THIS RULE APPLIES ONLY TO A FUNCTION WHOSE SRC_WEB ARM RETURNS A
RENDERED TEMPLATE -- not to every function in every round. In Group A that is
`vote_for_post` (app/shared/post.py:47-48, :73-74) and `subscribe_post`
(app/shared/post.py:152): every SRC_WEB-arm assertion against their result
must read `result.status_code` and `result.get_data(as_text=True)`;
`isinstance(result, str)` is always False and is not evidence of a broken
render. Group B's `lock_post`, `move_post`, `sticky_post`, `hide_post`,
`mod_remove_post` and `mod_restore_post` render no template at all -- they
return `user.id, post` or a bare `return` -- so this rule does not apply to
them.

THE FEDERATION LEVER IS `community.private`, NOT `local_only`. task_selector
runs synchronously under eager Celery (tests/conftest.py:105-110), so
vote_for_post:60 and vote_for_poll:1174/:1186 execute real bodies in
app/shared/tasks/likes.py. Both bodies return at their first guard --
`send_vote:60` and the poll task's own equivalent -- when
`community.local_only or community.private or not community.instance.online()`.
`local_only` looks like the obvious lever and is the WRONG one: `can_downvote`
(app/utils.py) returns False for `community.local_only and not user.is_local()`,
so setting it silently disables every downvote test. `private` is checked by the
federation guard and by nothing on the voting path.

NAME COLLISION. tests/factories.py:665 has its own `mark_post_read(user, post)`,
a two-argument factory, and app/shared/post.py:1115 has the three-argument
production function. This file imports the production one and never the factory.
tests/factories.py:679's `hide_post` collides the same way with
app/shared/post.py:1020, which is Group B.
"""

import pytest
from datetime import date

from flask import get_flashed_messages

from app import db
from app.constants import SRC_API, SRC_PLD, SRC_WEB
from app.models import Poll, PollChoice, PollChoiceVote, PostBookmark, \
    NotificationSubscription, PostVote, read_posts
from app.shared.post import (
    bookmark_post,
    extra_rate_limit_check,
    get_post_flair_list,
    mark_post_read,
    remove_bookmark_post,
    subscribe_post,
    vote_for_poll,
    vote_for_post,
)
from tests.factories import bearer, make_community, make_instance, make_post, \
    make_post_flair, make_site, make_user, seed_post_context, web_ctx


def _clear_votes_cast(user_id):
    """Deletes the `votes_cast_{today}_{user_id}` key a completed vote wrote.

    Not one of this file's brief-listed helpers -- added in Task 5 because it
    is the first task in this round whose tests let `vote_for_post` run all
    the way to `post.vote()` (defined at app/models.py:2725; the key writes
    this helper cleans up are app/models.py:2825-2829). That call sets or
    increments this key on the REAL redis instance the whole compose stack
    shares for the session (module docstring, "REAL REDIS IS SHARED ACROSS
    THE WHOLE TEST SESSION"), and tests/conftest.py:131 resets id sequences
    after every test, so a later test whose user reuses this id would
    otherwise inherit a stale count. Every test in this file that completes a
    real vote calls this in a `finally` block. The import is inside the
    function body, not at module level, for the same reason
    `votes_cast_today` (app/models.py:48) does it that way -- `app.redis_client`
    is a module-level name assigned by `create_app`, so a top-level `from app
    import redis_client` here would bind `None`, captured before `create_app`
    ever runs.
    """
    from app import redis_client
    redis_client.delete(f'votes_cast_{date.today()}_{user_id}')


def test_extra_rate_limit_check_returns_false_for_any_user(db_session):
    """`:160`'s `return False`, the function's whole body.

    Catches a regression making the stub return anything truthy. Its only
    production caller is make_post:166, which is Group D, so this round reaches
    it by direct call and says so rather than pretending otherwise.
    """
    s = seed_post_context()

    assert extra_rate_limit_check(s.voter) is False


def test_flair_list_loads_the_post_when_given_an_integer(db_session):
    """`:1134`'s true arm and `:1135`'s lookup.

    Catches a regression dropping the isinstance branch, which would leave an
    int bound to `post` and fail at `:1137`'s attribute read.
    """
    s = seed_post_context()
    make_post_flair(s.post, name='news')

    flair_list = get_post_flair_list(s.post.id)

    assert {f.id for f in flair_list} == {f.id for f in s.post.flair}
    assert len(flair_list) == 1


def test_flair_list_accepts_a_post_object_and_skips_the_lookup(db_session):
    """`:1134`'s false arm.

    Pins that a Post instance is used as given. Catches a regression that made
    the lookup unconditional, which would raise on a Post argument.
    """
    s = seed_post_context()
    make_post_flair(s.post, name='news')

    flair_list = get_post_flair_list(s.post)

    assert len(flair_list) == 1


def test_flair_list_returns_an_empty_list_when_the_post_has_no_flair(db_session):
    """`:1139`'s `flair_list = []` executes, reached through `:1137`'s true arm.

    This does NOT catch a regression at `:1137` -- `Post.flair`
    (app/models.py:1754) is a many-to-many relationship with no
    `lazy='dynamic'`, so it is never None; an unflaired post's `post.flair` is
    an empty `InstrumentedList`, which is itself a `list` subclass and compares
    equal to `[]`. That makes `:1137`'s two arms behaviourally EQUIVALENT on
    this path: the true arm assigns a literal `[]`; the false arm's
    `post.flair` is `== []` too, and no equality, length, iteration, or
    `isinstance(..., list)` check a caller could write distinguishes the two.
    Inverting or deleting `:1137`'s condition does not change this test's
    result -- it is an equivalent mutant here, not an escaped one. What this
    test actually pins is `:1139`'s statement executing on the no-flair path;
    the other two tests in this file pin the flair-present branch's behaviour.
    """
    s = seed_post_context()

    flair_list = get_post_flair_list(s.post)

    assert flair_list == []


def test_marking_read_inserts_one_row_per_post_id(db_session):
    """`:1118-1120`'s INSERT, reached through `:1116`'s true arm.

    Catches a regression inverting `:1116`, which would send a read=True call
    down the DELETE branch and leave the table empty.
    """
    s = seed_post_context()
    second = make_post(s.community, s.author, 'https://local.example/p/2')

    mark_post_read([s.post.id, second.id], True, s.voter.id)

    rows = db.session.execute(
        read_posts.select().where(read_posts.c.user_id == s.voter.id)).fetchall()
    assert {row.read_post_id for row in rows} == {s.post.id, second.id}


def test_marking_read_twice_updates_rather_than_duplicating(db_session):
    """`:1119`'s `ON CONFLICT (user_id, read_post_id) DO UPDATE`.

    Catches a regression dropping the conflict clause, which would raise a
    unique-violation on the second call instead of refreshing `interacted_at`.
    """
    s = seed_post_context()

    mark_post_read([s.post.id], True, s.voter.id)
    first = db.session.execute(
        read_posts.select().where(read_posts.c.user_id == s.voter.id)).fetchone()
    mark_post_read([s.post.id], True, s.voter.id)

    rows = db.session.execute(
        read_posts.select().where(read_posts.c.user_id == s.voter.id)).fetchall()
    assert len(rows) == 1
    assert rows[0].interacted_at >= first.interacted_at


def test_marking_unread_deletes_the_row(db_session):
    """`:1123-1125`'s DELETE, reached through `:1116`'s false arm.

    Catches a regression inverting `:1116`, which would re-insert on a
    read=False call instead of removing.
    """
    s = seed_post_context()
    mark_post_read([s.post.id], True, s.voter.id)

    mark_post_read([s.post.id], False, s.voter.id)

    rows = db.session.execute(
        read_posts.select().where(read_posts.c.user_id == s.voter.id)).fetchall()
    assert rows == []


def test_an_empty_post_id_list_still_bumps_last_seen(db_session):
    """Both loops' zero-iteration exit arcs, plus `:1128-1130`.

    `:1117` and `:1122` each need a zero-length list to record their exit arc,
    and `:1128`'s UPDATE runs regardless of how many posts were named -- so an
    empty call is not a no-op. Catches a regression moving the last_seen update
    inside either loop, which would make it depend on the list being non-empty.
    """
    s = seed_post_context()
    s.voter.last_seen = None
    db.session.commit()

    mark_post_read([], True, s.voter.id)

    db.session.refresh(s.voter)
    assert s.voter.last_seen is not None
    rows = db.session.execute(
        read_posts.select().where(read_posts.c.user_id == s.voter.id)).fetchall()
    assert rows == []


def test_an_empty_post_id_list_on_the_unread_branch_also_bumps_last_seen(db_session):
    """`:1122`'s zero-iteration exit arc specifically.

    The read=True twin above records `:1117`'s exit arc; this records `:1122`'s.
    Coverage treats them as separate arcs and one test cannot take both.
    """
    s = seed_post_context()
    s.voter.last_seen = None
    db.session.commit()

    mark_post_read([], False, s.voter.id)

    db.session.refresh(s.voter)
    assert s.voter.last_seen is not None


def test_bookmarking_through_the_api_creates_the_row_and_marks_read(db_session):
    """`:78`'s API arm, `:84`'s add, `:80`'s mark_post_read call, `:94`'s return.

    Pins all four at once because they lie on one straight path. Catches a
    regression dropping `:80`, which would leave the read_posts table empty
    while the bookmark still appeared. No request context here -- the module
    docstring's "SRC_API ARM DOES NOT NEED A REQUEST CONTEXT" finding applies
    equally to `bookmark_post`'s identical ternary at `:78`, so this does not
    wrap the call in `web_ctx`.
    """
    s = seed_post_context()

    result = bookmark_post(s.post.id, SRC_API, auth=bearer(s.voter))

    assert result == s.voter.id
    assert db.session.query(PostBookmark).filter_by(
        post_id=s.post.id, user_id=s.voter.id).count() == 1
    rows = db.session.execute(
        read_posts.select().where(read_posts.c.user_id == s.voter.id)).fetchall()
    assert {row.read_post_id for row in rows} == {s.post.id}


def test_bookmarking_through_the_web_reads_current_user_and_returns_none(db_session, app):
    """`:78`'s WEB arm and `:93`'s false arm.

    `:93`'s `if src == SRC_API:` has no else, so the WEB call returns None. That
    is the arm this pins. Catches a regression making `:78` read the bearer
    token unconditionally, which would raise with auth=None.
    """
    s = seed_post_context()

    with web_ctx(app, s.voter):
        result = bookmark_post(s.post.id, SRC_WEB)

    assert result is None
    assert db.session.query(PostBookmark).filter_by(
        post_id=s.post.id, user_id=s.voter.id).count() == 1


def test_bookmarking_twice_through_the_api_raises(db_session):
    """`:83`'s false arm and `:89`'s raise, reached through `:88`'s true arm.

    Catches a regression inverting `:83`, which would add a second row rather
    than reject. Asserts the row count stayed at one so the raise is not
    reached through an unrelated crash.
    """
    s = seed_post_context()
    bookmark_post(s.post.id, SRC_API, auth=bearer(s.voter))

    with pytest.raises(Exception, match='already been bookmarked'):
        bookmark_post(s.post.id, SRC_API, auth=bearer(s.voter))

    assert db.session.query(PostBookmark).filter_by(
        post_id=s.post.id, user_id=s.voter.id).count() == 1


def test_bookmarking_twice_through_the_web_flashes_instead_of_raising(db_session, app):
    """`:88`'s false arm and `:91`'s flash.

    The WEB duplicate path must NOT raise -- that asymmetry is the arm. Catches
    a regression hoisting the raise out of `:88`, which would give the web route
    an exception it has no handler for. Also asserts the flashed message's
    content (read inside the same `web_ctx`, since `flash`/`get_flashed_messages`
    both operate on that request's session), so a regression that drops `:91`'s
    `flash(_(msg))` call outright does not survive on `result is None` alone.
    """
    s = seed_post_context()
    with web_ctx(app, s.voter):
        bookmark_post(s.post.id, SRC_WEB)

    with web_ctx(app, s.voter):
        result = bookmark_post(s.post.id, SRC_WEB)
        flashed = get_flashed_messages()

    assert result is None
    assert len(flashed) == 1
    assert 'already been bookmarked' in flashed[0]
    assert db.session.query(PostBookmark).filter_by(
        post_id=s.post.id, user_id=s.voter.id).count() == 1


def test_removing_a_bookmark_through_the_api_deletes_it(db_session):
    """`:101`'s true arm, `:102`'s delete, `:112`'s return.

    Catches a regression inverting `:101`, which would leave the row and flash
    or raise instead.
    """
    s = seed_post_context()
    bookmark_post(s.post.id, SRC_API, auth=bearer(s.voter))

    result = remove_bookmark_post(s.post.id, SRC_API, auth=bearer(s.voter))

    assert result == s.voter.id
    assert db.session.query(PostBookmark).filter_by(
        post_id=s.post.id, user_id=s.voter.id).count() == 0


def test_removing_a_bookmark_that_does_not_exist_raises_through_the_api(db_session):
    """`:101`'s false arm and `:107`'s raise, through `:106`'s true arm.

    Catches a regression that made the delete unconditional, which would raise
    a different error entirely on a None row.
    """
    s = seed_post_context()

    with pytest.raises(Exception, match='was not bookmarked'):
        remove_bookmark_post(s.post.id, SRC_API, auth=bearer(s.voter))


def test_removing_a_bookmark_that_does_not_exist_flashes_on_the_web(db_session, app):
    """`:106`'s false arm and `:109`'s flash, plus `:111`'s false arm.

    Catches a regression hoisting `:107`'s raise out of `:106`, and also
    asserts the flashed message's content (read inside the same `web_ctx`) so
    a regression that drops `:109`'s `flash(_(msg))` call outright does not
    survive on `result is None` alone.
    """
    s = seed_post_context()

    with web_ctx(app, s.voter):
        result = remove_bookmark_post(s.post.id, SRC_WEB)
        flashed = get_flashed_messages()

    assert result is None
    assert len(flashed) == 1
    assert 'was not bookmarked' in flashed[0]


def test_subscribing_through_the_api_creates_the_subscription(db_session):
    """`:143-147`'s creation, reached through `:124`'s false arm and `:136`'s
    false arm, and `:150`'s return.

    No `web_ctx` here -- the module docstring's "SRC_API ARM DOES NOT NEED A
    REQUEST CONTEXT" finding applies: `:117`'s ternary reads
    `authorise_api_user(auth)` for SRC_API, never `current_user`, so a bearer
    call authorises with no request context at all.

    Catches a regression inverting `:136`, which would reject a first
    subscription as already existing.
    """
    s = seed_post_context()

    result = subscribe_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    assert result == s.voter.id
    assert db.session.query(NotificationSubscription).filter_by(
        entity_id=s.post.id, user_id=s.voter.id).count() == 1


def test_subscribing_twice_through_the_api_raises(db_session):
    """`:136`'s true arm and `:139`'s raise, through `:138`'s true arm.

    Asserts the count stayed at one, so the raise is not reached through an
    unrelated crash that would satisfy pytest.raises just as well.
    """
    s = seed_post_context()
    subscribe_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    with pytest.raises(Exception, match='already existed'):
        subscribe_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    assert db.session.query(NotificationSubscription).filter_by(
        entity_id=s.post.id, user_id=s.voter.id).count() == 1


def test_unsubscribing_through_the_api_deletes_the_subscription(db_session):
    """`:124`'s true arm, `:125`'s true arm, `:126`'s delete.

    Catches a regression changing `:124` to a truthiness test, which would send
    `subscribe=False` down the creation branch.
    """
    s = seed_post_context()
    subscribe_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    result = subscribe_post(s.post.id, False, SRC_API, auth=bearer(s.voter))

    assert result == s.voter.id
    assert db.session.query(NotificationSubscription).filter_by(
        entity_id=s.post.id, user_id=s.voter.id).count() == 0


def test_unsubscribing_when_none_exists_raises_through_the_api(db_session):
    """`:125`'s false arm and `:131`'s raise, through `:130`'s true arm."""
    s = seed_post_context()

    with pytest.raises(Exception, match='did not exist'):
        subscribe_post(s.post.id, False, SRC_API, auth=bearer(s.voter))

    assert db.session.query(NotificationSubscription).filter_by(
        entity_id=s.post.id, user_id=s.voter.id).count() == 0


def test_the_web_arm_ignores_the_subscribe_argument_it_was_given(db_session, app):
    """`:119-120`'s override, the function's least obvious behaviour.

    `:120` recomputes `subscribe` from `post.notify_new_replies(user_id)`, so a
    SRC_WEB caller passing subscribe=False when no subscription exists still
    CREATES one -- the argument is discarded. `app/post/routes.py:2089`, the
    only production caller of this arm, passes `subscribe=None` for exactly
    this reason: the value is never read.

    Verified: no NotificationSubscription row exists for this post/user before
    the call (`seed_post_context()` creates none), so `post.notify_new_replies(user_id)`
    at `:120` is False and `subscribe` becomes True -- the create arm below.

    Catches a regression deleting `:119`, which would make the web arm honour
    the argument and silently change the toggle route's behaviour.
    """
    s = seed_post_context()
    assert db.session.query(NotificationSubscription).filter_by(
        entity_id=s.post.id, user_id=s.voter.id).count() == 0

    with web_ctx(app, s.voter):
        result = subscribe_post(s.post.id, False, SRC_WEB)

    assert result.status_code == 200
    assert db.session.query(NotificationSubscription).filter_by(
        entity_id=s.post.id, user_id=s.voter.id).count() == 1


def test_the_web_arm_removes_an_existing_subscription_when_toggled(db_session, app):
    """`:120`'s other arm: with a subscription present, `notify_new_replies`
    returns truthy and `subscribe` becomes False, so the toggle removes.

    Together with the test above this exercises both values `:120` can
    produce. Verified: after the first call a row exists (asserted below,
    count == 1) before the second call is made, the mirror of the zero-row
    state the test above starts from -- so the two tests provably take
    different arms of `:120`'s ternary rather than coincidentally landing on
    the same one.

    Catches a regression inverting `:120`'s ternary, which would make the
    toggle one-way.
    """
    s = seed_post_context()
    with web_ctx(app, s.voter):
        subscribe_post(s.post.id, True, SRC_WEB)

    assert db.session.query(NotificationSubscription).filter_by(
        entity_id=s.post.id, user_id=s.voter.id).count() == 1

    with web_ctx(app, s.voter):
        result = subscribe_post(s.post.id, True, SRC_WEB)

    assert result.status_code == 200
    assert db.session.query(NotificationSubscription).filter_by(
        entity_id=s.post.id, user_id=s.voter.id).count() == 0


def test_a_third_source_reaches_the_flash_branches_the_web_arm_cannot(db_session, app):
    """`:130`'s false arm and `:133`'s flash, then `:136`'s true arm, `:138`'s
    false arm and `:141`'s flash.

    Neither SRC_WEB nor SRC_API can reach `:133`/`:141`. SRC_WEB cannot:
    `:119-120`'s override sets `subscribe` from `post.notify_new_replies`,
    which runs the SAME query as `:122-123`'s `existing_notification` lookup
    (entity_id, user_id, type=NOTIF_POST, identical on both), so under SRC_WEB
    `subscribe == False` if and only if `existing_notification` is truthy --
    `:124`/`:125`/`:136` can never land on the "mismatched" arms
    (`:125`'s false arm or `:136`'s true arm) that lead to `:129-133` or
    `:137-141`. SRC_API cannot either: `:130`/`:138`'s `if src == SRC_API`
    always takes the raise branch (`:131`/`:139`) instead of the else.
    `app/post/routes.py:2089`, the only WEB caller, passes `subscribe=None`,
    consistent with the argument never mattering there.

    This was verified empirically, not just reasoned: a diagnostic call of
    `subscribe_post(post_id, False, SRC_WEB)` against a post with no existing
    subscription (the exact inputs the original brief's web-flash test
    specified) was run and observed to CREATE a subscription and flash
    nothing (`before=0 after=1 flashed=[]`), not reach `:133` -- see the task
    report for the transcript. `:133`/`:141` are only reachable through a
    third source value that (a) is not SRC_WEB, so skips `:119`'s override,
    and (b) is not SRC_API, so `:130`/`:138` take the else. SRC_PLD
    (app/constants.py:94, the admin preload path) is used here purely as such
    a value to reach these two statements; it is not how the code is used in
    production, and this docstring says so rather than implying otherwise.

    Uses `web_ctx` (not an SRC_API test) because `:117`'s else-arm reads
    `current_user.id`, which needs a logged-in request context regardless of
    the source constant.

    Catches a regression deleting either `flash(_(msg))` call outright. Under
    SRC_PLD, `:149`'s `if src == SRC_API:` is always False, so control flow
    reaches `:151`'s else and `:150`'s return is never taken either way --
    deleting a flash call does not change which branch runs or what `:152`
    returns, only what got flashed. That is exactly why this test asserts on
    `flashed`'s content rather than on `result` alone: a deleted flash call
    would otherwise still return a normal 200 render and pass silently.
    """
    s = seed_post_context()

    with web_ctx(app, s.voter):
        result = subscribe_post(s.post.id, False, SRC_PLD)
        flashed = get_flashed_messages()

    assert result.status_code == 200
    assert len(flashed) == 1
    assert 'did not exist' in flashed[0]
    assert db.session.query(NotificationSubscription).filter_by(
        entity_id=s.post.id, user_id=s.voter.id).count() == 0

    subscribe_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    with web_ctx(app, s.voter):
        result = subscribe_post(s.post.id, True, SRC_PLD)
        flashed = get_flashed_messages()

    assert result.status_code == 200
    assert len(flashed) == 1
    assert 'already existed' in flashed[0]
    assert db.session.query(NotificationSubscription).filter_by(
        entity_id=s.post.id, user_id=s.voter.id).count() == 1


# --- Task 5: vote_for_post, :31-48's source fork and permission gates ---

def test_an_api_upvote_from_a_bot_returns_early_without_voting(db_session):
    """`:35`'s true arm and `:36`'s early return.

    `can_upvote` rejects a bot, so `:36` returns the user id having voted for
    nothing. Asserts no PostVote row, because `:36` and `:63` return the same
    value and the return alone cannot tell them apart. No `web_ctx` -- the
    module docstring's "SRC_API ARM DOES NOT NEED A REQUEST CONTEXT" finding
    applies to this arm too. No `redis_double` requested anywhere in this
    file, per the module docstring, and this call does not even reach
    `mark_post_read` -- it returns at `:36`, before `:56`.
    """
    s = seed_post_context()
    s.voter.bot = True
    db.session.commit()

    result = vote_for_post(s.post.id, 'upvote', True, None, SRC_API,
                            auth=bearer(s.voter))

    assert result == s.voter.id
    assert db.session.query(PostVote).filter_by(
        user_id=s.voter.id, post_id=s.post.id).count() == 0


def test_an_api_downvote_returns_early_when_downvotes_are_disabled(db_session):
    """`:35`'s false arm, `:37`'s true arm and `:38`'s early return.

    Catches a regression collapsing `:37` into `:35`, which would let a
    downvote through while downvotes are off site-wide.
    """
    s = seed_post_context()
    s.site.enable_downvotes = False
    db.session.commit()

    result = vote_for_post(s.post.id, 'downvote', True, None, SRC_API,
                            auth=bearer(s.voter))

    assert result == s.voter.id
    assert db.session.query(PostVote).filter_by(
        user_id=s.voter.id, post_id=s.post.id).count() == 0


def test_an_api_upvote_passes_both_gates_and_records_the_vote(db_session):
    """`:35`'s and `:37`'s false arms together, and `:63`'s return.

    The false-arm counterpart of the two tests above. Catches a regression
    inverting either gate, which would reject a permitted vote. This is the
    first test in this file to let `vote_for_post` run all the way through
    `post.vote()`, so it writes a real `votes_cast_*` key to the shared redis
    instance (see `_clear_votes_cast`'s docstring) and cleans it up in a
    `finally` block regardless of assertion outcome.
    """
    s = seed_post_context()

    try:
        result = vote_for_post(s.post.id, 'upvote', True, None, SRC_API,
                                auth=bearer(s.voter))

        assert result == s.voter.id
        assert db.session.query(PostVote).filter_by(
            user_id=s.voter.id, post_id=s.post.id).count() == 1
    finally:
        _clear_votes_cast(s.voter.id)


def test_an_api_reversal_is_refused_when_the_permission_that_cast_the_vote_is_gone(db_session):
    """`vote_for_post`'s reversal gate -- the twin of `vote_for_reply`'s.

    `app/api/alpha/utils/post.py` maps any score that is not +/-1 to
    'reversal', exactly as the reply API does. Before the fix, `:35` and `:37`
    were each gated on a direction LITERAL, so both were false for a reversal
    and the call reached `post.vote()` with no permission check at all.

    THE TWIN IS TESTED HERE AND NOT ONLY IN THE REPLY FILE BECAUSE AN UNTESTED
    TWIN IS HOW THE MIRRORED PAIR DIVERGES. `app/shared/post.py` and
    `app/shared/reply.py` carry the same gate; a later change that repairs or
    breaks one and not the other is only visible if both are pinned.

    The gate resolves the EXISTING vote rather than the direction literal,
    because 'reversal' names no permission of its own. The seeded vote is an
    upvote with `effect` 1, so `can_upvote` is the function consulted, and the
    bot flag set between the calls is what makes it refuse.

    THE WITNESS IS THE SURVIVING ROW, NOT THE RETURNED ID -- the refusal and
    the success return the same value. Under the unfixed code the row count
    below reads 0.
    """
    s = seed_post_context()
    try:
        vote_for_post(s.post.id, 'upvote', True, None, SRC_API,
                      auth=bearer(s.voter))
        assert db.session.query(PostVote).filter_by(
            user_id=s.voter.id, post_id=s.post.id).count() == 1

        s.voter.bot = True
        db.session.commit()

        result = vote_for_post(s.post.id, 'reversal', True, None, SRC_API,
                               auth=bearer(s.voter))

        assert result == s.voter.id
        assert db.session.query(PostVote).filter_by(
            user_id=s.voter.id, post_id=s.post.id).count() == 1
    finally:
        _clear_votes_cast(s.voter.id)


def test_an_api_downvote_reversal_is_refused_when_downvotes_are_disabled(db_session):
    """`vote_for_post`'s reversal gate, `effect < 0` half -- negative witness.

    `site.enable_downvotes` IS THE RIGHT LEVER AND THE BOT FLAG IS NOT.
    `enable_downvotes` False makes `can_downvote` return at app/utils.py:2445
    while leaving `can_upvote` (app/utils.py:2480, which never reads the Site
    row) TRUE. A mutant that consulted `can_upvote` for a negative effect --
    swapping the two functions, or collapsing the second check into the first
    -- would permit this reversal and the row below would be gone. The bot
    flag refuses both and could not tell them apart.
    """
    s = seed_post_context()
    try:
        vote_for_post(s.post.id, 'downvote', True, None, SRC_API,
                      auth=bearer(s.voter))
        assert db.session.query(PostVote).filter_by(
            user_id=s.voter.id, post_id=s.post.id).count() == 1

        s.site.enable_downvotes = False
        db.session.commit()

        result = vote_for_post(s.post.id, 'reversal', True, None, SRC_API,
                               auth=bearer(s.voter))

        assert result == s.voter.id
        assert db.session.query(PostVote).filter_by(
            user_id=s.voter.id, post_id=s.post.id).count() == 1
    finally:
        _clear_votes_cast(s.voter.id)


def test_an_api_downvote_reversal_lands_when_downvotes_are_enabled(db_session):
    """The same-mechanism positive control for the test above.

    Identical except that `enable_downvotes` is left alone, so `can_downvote`
    permits and the reversal must LAND. Without it, a fixture in which no
    downvote reversal could ever succeed would produce the same surviving row.
    """
    s = seed_post_context()
    try:
        vote_for_post(s.post.id, 'downvote', True, None, SRC_API,
                      auth=bearer(s.voter))
        assert db.session.query(PostVote).filter_by(
            user_id=s.voter.id, post_id=s.post.id).count() == 1

        result = vote_for_post(s.post.id, 'reversal', True, None, SRC_API,
                               auth=bearer(s.voter))

        assert result == s.voter.id
        assert db.session.query(PostVote).filter_by(
            user_id=s.voter.id, post_id=s.post.id).count() == 0
    finally:
        _clear_votes_cast(s.voter.id)


def test_an_api_reversal_with_no_existing_vote_is_not_refused_by_the_gate(db_session):
    """The gate's `if existing_vote:` false arm.

    With no vote to resolve a permission from, the gate falls through rather
    than refusing. Control reaches `post.vote()`, where app/models.py:2732-
    2741 returns None for a reversal with no existing vote -- so this is a
    quiet no-op, NOT the raise its `PostReply.vote` counterpart produces. That
    divergence between the two models is registered, not repaired here.

    THE WITNESS IS THE ABSENT ROW: no `PostVote` is written, which is what
    distinguishes falling through the gate from a mutant that let the
    else-branch cast a new downvote.
    """
    s = seed_post_context()
    try:
        result = vote_for_post(s.post.id, 'reversal', True, None, SRC_API,
                               auth=bearer(s.voter))

        assert result == s.voter.id
        assert db.session.query(PostVote).filter_by(
            user_id=s.voter.id, post_id=s.post.id).count() == 0
    finally:
        _clear_votes_cast(s.voter.id)


def test_an_api_reversal_still_works_for_a_permitted_voter(db_session):
    """The positive control for the gate above, and it is mandatory.

    A refusal test alone cannot tell a gate that refuses the right voter from
    one that refuses every voter. Same two-call sequence, bot flag never set,
    so `can_upvote` permits and the reversal must LAND -- the `PostVote` row
    is gone. If the new gate refused unconditionally, the test above would
    still pass and this one would fail.
    """
    s = seed_post_context()
    try:
        vote_for_post(s.post.id, 'upvote', True, None, SRC_API,
                      auth=bearer(s.voter))
        assert db.session.query(PostVote).filter_by(
            user_id=s.voter.id, post_id=s.post.id).count() == 1

        result = vote_for_post(s.post.id, 'reversal', True, None, SRC_API,
                               auth=bearer(s.voter))

        assert result == s.voter.id
        assert db.session.query(PostVote).filter_by(
            user_id=s.voter.id, post_id=s.post.id).count() == 0
    finally:
        _clear_votes_cast(s.voter.id)


def test_a_web_upvote_from_a_bot_renders_empty_voting_buttons(db_session, app):
    """`:43`'s FIRST conjunction taken true -- upvote and not can_upvote.

    `:43-44` is one arc pair to coverage.py, so this and the three tests below
    are the only way to distinguish its parts, and Task 11's mutation pass is
    the only instrument that proves they are distinguished. Catches a
    regression dropping the first disjunct.

    Asserts `result.status_code` rather than `isinstance(result, str)`:
    `render_template` here is `app.utils.render_template`
    (app/shared/post.py:23), which wraps the rendered string in
    `make_response` (app/utils.py:86-99) before returning it, so the value is
    a Flask `Response`, never a plain string.
    """
    s = seed_post_context()
    s.voter.bot = True
    db.session.commit()

    with web_ctx(app, s.voter):
        result = vote_for_post(s.post.id, 'upvote', True, None, SRC_WEB)

    assert result.status_code == 200
    assert db.session.query(PostVote).filter_by(
        user_id=s.voter.id, post_id=s.post.id).count() == 0


def test_a_web_downvote_is_refused_when_downvotes_are_disabled(db_session, app):
    """`:44`'s SECOND conjunction taken true -- downvote and not can_downvote,
    with the first disjunct false because the direction is not 'upvote'.

    Catches a regression dropping the second disjunct, which would let the
    downvote through to `:58`.
    """
    s = seed_post_context()
    s.site.enable_downvotes = False
    db.session.commit()

    with web_ctx(app, s.voter):
        result = vote_for_post(s.post.id, 'downvote', True, None, SRC_WEB)

    assert result.status_code == 200
    assert db.session.query(PostVote).filter_by(
        user_id=s.voter.id, post_id=s.post.id).count() == 0


def test_a_web_upvote_is_allowed_when_only_downvotes_are_disabled(db_session, app):
    """Both disjuncts false with `can_downvote` False -- the input that proves
    `:44`'s direction check is load-bearing.

    Without this test, a regression replacing `:44`'s
    `vote_direction == 'downvote'` with True would still pass everything
    above. Completes a real vote (see `test_an_api_upvote_passes_both_gates...`
    above for why the redis cleanup is needed), so it is wrapped in the same
    `finally`.
    """
    s = seed_post_context()
    s.site.enable_downvotes = False
    db.session.commit()

    try:
        with web_ctx(app, s.voter):
            result = vote_for_post(s.post.id, 'upvote', True, None, SRC_WEB)

        assert result.status_code == 200
        assert db.session.query(PostVote).filter_by(
            user_id=s.voter.id, post_id=s.post.id).count() == 1
    finally:
        _clear_votes_cast(s.voter.id)


def test_a_web_downvote_is_allowed_when_nothing_blocks_it(db_session, app):
    """Both disjuncts false with both helpers True -- the fourth input.

    Completes the four-way exercise of `:43-44`. Verified live (see task
    report) to catch a regression dropping `:44`'s `not` on `can_downvote`
    (which would then block this permitted downvote).

    NOT a witness for `:43`'s equality check the way the brief claimed --
    scoped correctly, not in general. Live mutation (replacing `:43`'s
    `vote_direction == 'upvote'` with `True`) left every test in this file
    passing, including this one: `can_upvote` has no restriction
    `can_downvote` lacks (both start from the same `user is None or
    community is None or user.banned or user.bot` check, and share the same
    banned-community-list check; `can_downvote` only ever adds MORE
    restrictions on top), so `not can_upvote(user, post.community)` is False
    whenever this test's downvote is actually unblocked. The mutated first
    disjunct (`True and not can_upvote(...)`) therefore evaluates the same
    as the original's (`False and ...`) for every reachable arrangement of
    these two helpers WHEN `vote_direction == 'downvote'`. That scope
    matters: it is a conditional equivalence, true only across calls whose
    direction is 'downvote', not an unconditional one -- unlike `:1137`'s
    equivalent mutant (module docstring), which no input distinguishes at
    all. Here, a `vote_direction` outside {'upvote', 'downvote'} (e.g.
    'reversal', reachable via app/api/alpha/utils/post.py:1397 and
    unvalidated via app/post/routes.py:539) DOES distinguish the mutant --
    see `test_a_web_reversal_bypasses_the_upvote_gate_for_a_blocked_user`
    below, which is the actual killer for this line.

    Completes a real vote, so the redis cleanup applies here too.
    """
    s = seed_post_context()

    try:
        with web_ctx(app, s.voter):
            result = vote_for_post(s.post.id, 'downvote', True, None, SRC_WEB)

        assert result.status_code == 200
        vote = db.session.query(PostVote).filter_by(
            user_id=s.voter.id, post_id=s.post.id).one()
        assert vote.effect < 0
    finally:
        _clear_votes_cast(s.voter.id)


def test_the_masonry_template_is_chosen_when_a_style_is_requested(db_session, app):
    """`:45`'s true arm, the refusal path's masonry template.

    `:45` and `:72` are two separate ternaries with identical text; this pins
    `:45`'s. Catches a regression hardcoding either template name.

    Renders BOTH styles from the same gated input (`bot=True`, 'upvote', so
    `:43-44`'s disjunction is true on both calls and both return through
    `:47`'s early render) and asserts the two Response bodies differ. A
    status-only assertion cannot tell `_post_voting_buttons.html` and
    `_post_voting_buttons_masonry.html` apart -- both return 200 -- so this
    compares `result.get_data(as_text=True)` between the two renders, per the
    module docstring's "THE WEB ARMS RETURN A FLASK RESPONSE" finding.

    Verified live (see task report): with `bot=True`, both of this post's
    community's `can_upvote`/`can_downvote` checks are False inside the
    template itself, so `_post_voting_buttons.html`'s unconditional `<span
    class="score" ...>` (the only element not gated by either check) still
    renders, while `_post_voting_buttons_masonry.html`'s authenticated branch
    contains no unconditional element -- both its upvote and downvote blocks
    are individually gated -- so it renders empty. The two bodies are not
    merely different strings; one is non-empty markup and the other is
    effectively blank, which is the strongest form of "not equal" available.
    """
    s = seed_post_context()
    s.voter.bot = True
    db.session.commit()

    with web_ctx(app, s.voter):
        plain = vote_for_post(s.post.id, 'upvote', True, None, SRC_WEB)
    with web_ctx(app, s.voter, query_string='style=masonry'):
        masonry = vote_for_post(s.post.id, 'upvote', True, None, SRC_WEB)

    assert plain.status_code == 200
    assert masonry.status_code == 200
    plain_body = plain.get_data(as_text=True)
    masonry_body = masonry.get_data(as_text=True)
    assert plain_body != masonry_body
    assert 'class="score"' in plain_body


def test_a_web_reversal_bypasses_the_upvote_gate_for_a_blocked_user(db_session, app):
    """Round-1 fix. `:43`'s equality check IS load-bearing, but only across
    calls whose `vote_direction` is outside {'upvote', 'downvote'} -- the
    scope `test_a_web_downvote_is_allowed_when_nothing_blocks_it`'s docstring
    now states. `vote_direction='reversal'` is such a value, reachable in
    production via app/api/alpha/utils/post.py:1397 (`score` neither 1 nor
    -1) and, completely unvalidated, via app/post/routes.py:539's URL path
    segment.

    On the CURRENT code, with a bot voter (`can_upvote` and `can_downvote`
    both False) and `vote_direction='reversal'`: `:43`'s first conjunct is
    `'reversal' == 'upvote' and ...` = False; `:44`'s second conjunct is
    `'reversal' == 'downvote' and ...` = False. Neither disjunct fires, so
    the guard does not trigger and execution proceeds PAST `:48` to `:56`'s
    `mark_post_read` call -- the one thing in this function that inserts
    into `read_posts` before anything else does. Its presence afterward is
    therefore proof execution passed the guard; its absence would prove the
    guard fired. This is a stronger assertion than "nothing raised" or
    "status_code == 200", both of which are identical whichever branch runs
    (`:47` and `:72-74` are separate templates, but for this direction and
    this seed they are called with byte-identical arguments -- both pass
    `recently_upvoted=[]`/`recently_downvoted=[]` -- so comparing rendered
    bodies cannot distinguish the branches here the way it did for the
    masonry-style test above).

    NO EXISTING VOTE IS SEEDED, deliberately. Post.vote's reversal handling
    (app/models.py:2732-2741) does one of two things when an existing vote
    IS found: it relabels `vote_direction` to match that vote's OWN sign and
    falls through to `:2744`. From there, WITH `emoji` NULL -- as here, and
    as in every `vote_for_post` call in this file -- it reaches the ordinary
    "remove it" arms at app/models.py:2762/2776 (never the sign-flip arms at
    :2768/:2782, since the relabelled direction can never disagree with the
    vote it was just read from), so a reversal carrying a null emoji can only
    DELETE an existing vote, never create one or change its sign. THAT
    SCOPING IS LOAD-BEARING: with a NON-null emoji, app/models.py:2746-2752
    overwrites `existing_vote.emoji` IN PLACE, refreshes the reaction cache
    and returns None WITHOUT deleting the vote -- and because `:2734-2737`
    has already forced `vote_direction` to agree with the existing vote's
    sign, `:2746`'s condition is ALWAYS satisfied for a reversal carrying an
    emoji. Both production callers can supply one
    (app/api/alpha/utils/post.py:1390 and app/post/routes.py:540). See D408
    in docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md; an
    earlier form of this docstring stated the delete-only claim unscoped,
    which is D412's own failure mode -- true for the one input the test uses,
    false in general, stated in general. With none seeded, `existing_vote` is
    None and `:2741` returns None immediately, before writing anything --
    so this call cannot accidentally create or remove a PostVote row either
    way, and the `read_posts` row is the only signal in this test that
    depends on the guard rather than on Post.vote's own branching.

    Catches a regression replacing `:43`'s `vote_direction == 'upvote'` with
    `True`. Verified by LIVE MUTATION (see task report): under that
    mutation, `not can_upvote(user, post.community)` is True for this bot,
    the first disjunct becomes True, the guard fires, and `:47`'s early
    return runs before `:56` -- no `read_posts` row is written, and this
    test's `rows == {s.post.id}` assertion fails.
    """
    s = seed_post_context()
    s.voter.bot = True
    db.session.commit()

    with web_ctx(app, s.voter):
        result = vote_for_post(s.post.id, 'reversal', True, None, SRC_WEB)

    assert result.status_code == 200
    rows = db.session.execute(
        read_posts.select().where(read_posts.c.user_id == s.voter.id)).fetchall()
    assert {row.read_post_id for row in rows} == {s.post.id}
    assert db.session.query(PostVote).filter_by(
        user_id=s.voter.id, post_id=s.post.id).count() == 0


def test_a_web_reversal_is_refused_when_the_permission_that_cast_the_vote_is_gone(db_session, app):
    """D408, fixed. The web arm gated only the 'upvote' and 'downvote'
    literals, so a 'reversal' taken from the URL path segment reached
    `post.vote()` with no permission check and a voter who had lost
    `can_upvote` could still withdraw their upvote. It now takes the API
    arm's gate: the permission that would have cast the existing vote.

    The witness is the surviving row; the refusal renders the buttons."""
    s = seed_post_context()
    try:
        vote_for_post(s.post.id, 'upvote', True, None, SRC_API,
                      auth=bearer(s.voter))
        s.voter.bot = True
        db.session.commit()

        with web_ctx(app, s.voter):
            result = vote_for_post(s.post.id, 'reversal', True, None, SRC_WEB)

        assert result.status_code == 200
        assert db.session.query(PostVote).filter_by(
            user_id=s.voter.id, post_id=s.post.id).count() == 1
    finally:
        _clear_votes_cast(s.voter.id)


def test_a_web_reversal_still_works_for_a_permitted_voter(db_session, app):
    """D408's positive control: with the permission intact the web reversal
    lands and the vote is gone, so the gate does not refuse every voter."""
    s = seed_post_context()
    try:
        vote_for_post(s.post.id, 'upvote', True, None, SRC_API,
                      auth=bearer(s.voter))

        with web_ctx(app, s.voter):
            vote_for_post(s.post.id, 'reversal', True, None, SRC_WEB)

        assert db.session.query(PostVote).filter_by(
            user_id=s.voter.id, post_id=s.post.id).count() == 0
    finally:
        _clear_votes_cast(s.voter.id)


# --- Task 6: vote_for_post, the ban check through the return arms ---

def test_a_banned_user_is_aborted_with_403(db_session, app):
    """`:50`'s FIRST conjunct -- `user.banned` -- and `:51`'s abort.

    DEVIATES FROM THE BRIEF, which asked for SRC_API with `vote_direction=
    'upvote'`. That combination never reaches `:50` at all: `authorise_api_user`
    (app/utils.py:3628) raises a bare `Exception('incorrect_login')` for ANY
    bearer token whose user has `user.banned is True`, before vote_for_post's
    body runs a single line, so a banned SRC_API caller never even reaches
    `:34`'s return. And a banned SRC_WEB caller with `vote_direction='upvote'`
    fares no better via a different route: `can_upvote` (app/utils.py:2481)
    also returns False whenever `user.banned` is true, so `:43`'s `not
    can_upvote(...)` is True and the function returns through `:47`'s early
    render before `:50` ever runs.

    `vote_direction='reversal'` sidesteps both: `:43` and `:44` compare
    `vote_direction` against 'upvote'/'downvote' by equality, so neither
    conjunct's first half is ever True for 'reversal' regardless of what
    `can_upvote`/`can_downvote` return, and the disjunction is False --
    exactly the mechanism `test_a_web_reversal_bypasses_the_upvote_gate_for_a_
    blocked_user` above already established for a bot voter. Execution falls
    through to `:50`, where `user.banned` is now the ONLY thing that can raise,
    and it is True. SRC_WEB is required (not SRC_API) because SRC_WEB reads
    `current_user` directly with no `authorise_api_user` gate to intercept it.

    Asserts the status code rather than merely that something raised, so an
    abort reached through an unrelated crash cannot satisfy it, and asserts no
    vote was recorded.
    """
    from werkzeug.exceptions import Forbidden

    s = seed_post_context()
    s.voter.banned = True
    db.session.commit()

    with web_ctx(app, s.voter):
        with pytest.raises(Forbidden) as excinfo:
            vote_for_post(s.post.id, 'reversal', True, None, SRC_WEB)

    assert excinfo.value.code == 403
    assert db.session.query(PostVote).filter_by(
        user_id=s.voter.id, post_id=s.post.id).count() == 0


def test_a_vote_over_the_daily_quota_is_aborted_with_429(db_session, app):
    """`:53`'s true arm and `:54`'s abort.

    Sets the counter one above VOTE_QUOTA, the tight side of `>`. Asserts 429
    specifically, because `:51` aborts 403 on the same function and a bare
    `pytest.raises(Exception)` cannot tell them apart. Writes the redis key
    `votes_cast_today` (app/models.py:47-52) reads directly in this test body
    rather than through a second helper -- `_clear_votes_cast` (Task 5,
    above) already exists for the matching cleanup half of this job, and the
    module docstring's binding rule is that no test may leave a
    `votes_cast_*` key behind, not that every test must share one write
    helper. The abort happens before `post.vote()` runs, so this key is never
    incremented by production code either -- only this test's own `set` and
    the `finally`'s `_clear_votes_cast` ever touch it.
    """
    from app import redis_client
    from werkzeug.exceptions import TooManyRequests

    s = seed_post_context()
    redis_client.set(f'votes_cast_{date.today()}_{s.voter.id}',
                     str(app.config['VOTE_QUOTA'] + 1))

    try:
        with pytest.raises(TooManyRequests) as excinfo:
            vote_for_post(s.post.id, 'upvote', True, None, SRC_API,
                          auth=bearer(s.voter))

        assert excinfo.value.code == 429
        assert db.session.query(PostVote).filter_by(
            user_id=s.voter.id, post_id=s.post.id).count() == 0
    finally:
        _clear_votes_cast(s.voter.id)


def test_a_vote_exactly_at_the_daily_quota_is_allowed(db_session, app):
    """`:53`'s false arm at the boundary itself.

    `:53` is `>`, so a count EQUAL to VOTE_QUOTA passes. This is the
    direction sub-project 32's mutation pass failed to probe, and the reason
    this plan asks for both. Catches a regression changing `>` to `>=`. The
    vote completes, so `post.vote()` (app/models.py:2825-2829) itself
    increments this same key afterward; the `finally` clears it regardless of
    what value it ends up holding.
    """
    from app import redis_client

    s = seed_post_context()
    redis_client.set(f'votes_cast_{date.today()}_{s.voter.id}',
                     str(app.config['VOTE_QUOTA']))

    try:
        result = vote_for_post(s.post.id, 'upvote', True, None, SRC_API,
                               auth=bearer(s.voter))

        assert result == s.voter.id
        assert db.session.query(PostVote).filter_by(
            user_id=s.voter.id, post_id=s.post.id).count() == 1
    finally:
        _clear_votes_cast(s.voter.id)


def test_a_first_web_upvote_reports_the_post_as_recently_upvoted(db_session, app):
    """`:67`'s true arm and `:68`'s assignment, plus `:62`'s false arm.

    `Post.vote` returns None for a fresh vote, so `undo is None` holds and the
    post id lands in `recently_upvoted`, which flows into the template as the
    `voted_up` CSS class (app/templates/post/_post_voting_buttons.html:3) on
    the upvote button div. Asserting on that literal marker in the rendered
    body -- not just `status_code` -- is what actually pins `:68`'s
    assignment: a regression that skipped it would still return 200 with an
    unrelated body, but would never render `voted_up`. Completes a real vote,
    so the redis cleanup applies (see `_clear_votes_cast`'s docstring).
    """
    s = seed_post_context()

    try:
        with web_ctx(app, s.voter):
            result = vote_for_post(s.post.id, 'upvote', True, None, SRC_WEB)

        assert result.status_code == 200
        body = result.get_data(as_text=True)
        assert 'voted_up' in body
        assert 'voted_down' not in body
        assert db.session.query(PostVote).filter_by(
            user_id=s.voter.id, post_id=s.post.id).count() == 1
    finally:
        _clear_votes_cast(s.voter.id)


def test_a_first_web_downvote_reports_the_post_as_recently_downvoted(db_session, app):
    """`:67`'s false arm, `:69`'s true arm and `:70`'s assignment.

    Catches a regression collapsing `:69` into `:67`, which would report a
    downvote as an upvote to the template. Same `voted_down`/`voted_up`
    marker technique as the upvote test above, plus the `PostVote.effect`
    check the brief asked for.
    """
    s = seed_post_context()

    try:
        with web_ctx(app, s.voter):
            result = vote_for_post(s.post.id, 'downvote', True, None, SRC_WEB)

        assert result.status_code == 200
        body = result.get_data(as_text=True)
        assert 'voted_down' in body
        assert 'voted_up' not in body
        vote = db.session.query(PostVote).filter_by(
            user_id=s.voter.id, post_id=s.post.id).one()
        assert vote.effect < 0
    finally:
        _clear_votes_cast(s.voter.id)


def test_repeating_a_web_upvote_undoes_it_and_reports_neither_list(db_session, app):
    """`:67`'s and `:69`'s false arms together, via a non-None `undo`.

    `Post.vote` returns 'Like' when it removes an existing upvote
    (app/models.py:2762-2767), so `undo is None` is False on both
    conditionals and both lists stay empty. This is the only input that takes
    both false arms. Catches a regression dropping the `undo is None` clause
    from either, which would report an undone vote as a live one -- pinned
    here by asserting NEITHER marker is in the body, not merely that the vote
    row is gone. Only the first call writes a `votes_cast_*` key
    (app/models.py:2762-2767's undo branch never touches it, confirmed by
    reading it), but the `finally` clears it unconditionally regardless.
    """
    s = seed_post_context()

    try:
        with web_ctx(app, s.voter):
            vote_for_post(s.post.id, 'upvote', True, None, SRC_WEB)

        with web_ctx(app, s.voter):
            result = vote_for_post(s.post.id, 'upvote', True, None, SRC_WEB)

        assert result.status_code == 200
        body = result.get_data(as_text=True)
        assert 'voted_up' not in body
        assert 'voted_down' not in body
        assert db.session.query(PostVote).filter_by(
            user_id=s.voter.id, post_id=s.post.id).count() == 0
    finally:
        _clear_votes_cast(s.voter.id)


def test_the_masonry_template_is_chosen_on_the_success_path_too(db_session, app):
    """`:72`'s true arm, distinct from `:45`'s.

    Coverage records two separate ternaries at `:45` and `:72`; Task 5 pinned
    `:45`'s on the early-refusal path (`:47`). This pins `:72`'s on the
    success path -- the one `:45`'s test structurally cannot reach, since
    `:45` only runs when `:43-44`'s guard fires and returns before `:56`.

    DEVIATES FROM THE BRIEF, which called this twice with `vote_direction=
    'upvote'` (masonry then plain) and asserted `masonry != plain`. Two
    problems with that: first, `Response.__eq__` is identity comparison, so
    two distinct Response objects are ALWAYS `!=` regardless of body content
    -- that assertion cannot fail no matter what `:72` does, which is exactly
    the "test that cannot fail" defect this round is watching for. Second,
    even fixed to compare `.get_data(as_text=True)`, a same-post 'upvote'/
    'upvote' pair is a fresh-vote-then-undo pair (see the test above): the
    second call's `recently_upvoted` becomes `[]` where the first's was
    `[post_id]`, so the two bodies would still differ by the `voted_up`
    marker even if `:72` were mutated to hardcode a single template name --
    the test would pass for the wrong reason and never catch that mutation.

    This calls `vote_for_post` TWICE with `vote_direction='reversal'` and no
    existing vote instead. As `test_a_banned_user_is_aborted_with_403` above
    establishes, `:43-44`'s guard never fires for 'reversal', so both calls
    reach `:56` onward. `Post.vote`'s reversal handling (app/models.py:2732-
    2741) returns None immediately with NO existing vote to reverse, before
    touching a single row, a lock, or the votes_cast key -- so `undo` is None
    on both calls but `vote_direction` is 'reversal', not 'upvote' or
    'downvote', so `:67` and `:69` are both False on EVERY call, identically.
    `recently_upvoted=[]` and `recently_downvoted=[]` on both renders, so the
    only difference `:72` can introduce between the masonry and plain calls
    is the template file itself -- no vote-state confound, no PostVote row,
    no redis write, no cleanup needed.

    Verified live (see task report): mutating `:72` to always select
    `post/_post_voting_buttons.html` made the two bodies equal and this
    test's inequality assertion fail, then restored.
    """
    s = seed_post_context()

    with web_ctx(app, s.voter, query_string='style=masonry'):
        masonry = vote_for_post(s.post.id, 'reversal', True, None, SRC_WEB)

    with web_ctx(app, s.voter):
        plain = vote_for_post(s.post.id, 'reversal', True, None, SRC_WEB)

    assert masonry.status_code == 200
    assert plain.status_code == 200
    masonry_body = masonry.get_data(as_text=True)
    plain_body = plain.get_data(as_text=True)
    assert masonry_body != plain_body
    assert 'style=masonry' in masonry_body
    assert 'style=masonry' not in plain_body
    assert db.session.query(PostVote).filter_by(
        user_id=s.voter.id, post_id=s.post.id).count() == 0


def test_voting_marks_the_post_read(db_session, app):
    """`:56`'s `mark_post_read` call on the success path.

    PC3 MOVED this call below `:53-54`'s quota check -- the post-PC3 layout
    is `:50-51` ban, `:53-54` quota, `:56` mark_post_read, and it is the tree
    this test runs against. That move would have passed every other test in
    this file; this test pins that the call still happens on the path where
    the vote succeeds, so the relocation could not silently delete it.
    Completes a real vote, so the redis cleanup applies.
    """
    s = seed_post_context()

    try:
        with web_ctx(app, s.voter):
            vote_for_post(s.post.id, 'upvote', True, None, SRC_WEB)

        rows = db.session.execute(
            read_posts.select().where(read_posts.c.user_id == s.voter.id)).fetchall()
        assert {row.read_post_id for row in rows} == {s.post.id}
    finally:
        _clear_votes_cast(s.voter.id)


def test_an_over_quota_vote_does_not_mark_the_post_read(db_session, app):
    """PC3: mark_post_read USED TO run before the quota check. It no longer
    does -- the post-PC3 layout is `:50-51` ban, `:53-54` quota, `:56`
    mark_post_read, and `:56` is now BELOW the check, not above it.

    Pre-PC3, a user over quota had the post written to read_posts and
    last_seen bumped and only then received a 429 -- the side effect survived
    the rejection. This test failed against the pre-PC3 tree and passes now
    that the call sits below the check. Same over-quota setup as
    `test_a_vote_over_the_daily_quota_is_aborted_with_429`, but asserts the
    `read_posts` table instead of `PostVote`, since the pre-move call this
    test targeted ran unconditionally on this path regardless of the vote
    quota outcome.

    Asserts the 429 as well as the empty table, so a change that stopped the
    abort entirely could not make it pass.
    """
    from app import redis_client
    from werkzeug.exceptions import TooManyRequests

    s = seed_post_context()
    redis_client.set(f'votes_cast_{date.today()}_{s.voter.id}',
                     str(app.config['VOTE_QUOTA'] + 1))

    try:
        with pytest.raises(TooManyRequests) as excinfo:
            vote_for_post(s.post.id, 'upvote', True, None, SRC_API,
                          auth=bearer(s.voter))

        assert excinfo.value.code == 429
        rows = db.session.execute(
            read_posts.select().where(read_posts.c.user_id == s.voter.id)).fetchall()
        assert rows == []
    finally:
        _clear_votes_cast(s.voter.id)



def _seed_poll(s, mode='single', choices=('a', 'b')):
    """A Poll on `s.post` with the named choices.

    `Poll.post_id` is the primary key (app/models.py:3781) -- a poll IS its
    post, so this takes the seeded post rather than making a new id. Returns
    the Poll; read its choices by querying PollChoice with post_id=s.post.id
    (order_by(PollChoice.sort_order) when choice identity/order matters --
    e.g. picking "the first choice" -- since nothing else fixes row order).

    LIMIT: this ties to `s.post` alone, and since `Poll.post_id` is the
    primary key that means exactly one poll per seed. A test needing a SECOND
    poll (e.g. a choice that belongs to a different poll than the one under
    test) needs its own arrangement -- this helper does not support it.
    """
    from datetime import timedelta
    from app.models import utcnow

    poll = Poll(post_id=s.post.id, mode=mode, local_only=False,
               end_poll=utcnow() + timedelta(days=1))
    db.session.add(poll)
    for order, text in enumerate(choices):
        db.session.add(PollChoice(post_id=s.post.id, choice_text=text,
                                  sort_order=order, num_votes=0))
    db.session.commit()
    return poll


def test_a_single_mode_api_vote_records_one_choice(db_session):
    """`:1147`'s true arm, `:1155`'s true arm, `:1168`'s true arm, `:1172`'s
    true arm, `:1173`'s vote and `:1174`'s federation call.

    No `web_ctx` -- the module docstring's "SRC_API ARM DOES NOT NEED A
    REQUEST CONTEXT" finding applies here too, and the real API caller
    (app/api/alpha/utils/post.py:1791-1797) hands `vote_for_poll` a bare int
    `data['choice_id']`, unwrapped, so `:1155` must wrap it into a list.
    Catches a regression dropping `:1155`, which would leave `votes` as the
    bare int `choice.id`: `:1161`'s `for choice_id in votes` comprehension
    would raise `TypeError: 'int' object is not iterable` immediately,
    before `:1168`'s `poll.mode == 'single'` branch or `:1169`'s `len(votes)`
    is ever reached.
    `community.private=True` (this file's `seed_post_context()` default) stops
    `app/shared/tasks/likes.py`'s `vote_for_poll` task at its own first guard,
    so `:1174`'s federation call runs but issues no outbound request and
    touches no redis key -- unlike `vote_for_post`'s `post.vote()`, nothing
    here writes a `votes_cast_*` key, so no `_clear_votes_cast` is needed.

    Asserts the vote set rather than a single row, and refreshes `choice`
    before reading `num_votes`, since `:1173`'s `vote_for_choice` commits
    inside the ORM session this test shares.
    """
    s = seed_post_context()
    _seed_poll(s, mode='single')
    choice = db.session.query(PollChoice).filter_by(post_id=s.post.id).order_by(
        PollChoice.sort_order).first()

    vote_for_poll(s.post.id, choice.id, SRC_API, auth=bearer(s.voter))

    votes = db.session.query(PollChoiceVote).filter_by(user_id=s.voter.id).all()
    assert {v.choice_id for v in votes} == {choice.id}
    db.session.refresh(choice)
    assert choice.num_votes == 1


def test_a_single_mode_web_vote_accepts_a_list(db_session, app):
    """`:1147`'s false arm and `:1155`'s false arm.

    The real web caller (app/post/routes.py:642) also sends a bare int
    for single mode (`int(request.form.get('poll_choice'))`), so nothing in
    this file's real callers exercises `:1155`'s false arm with SRC_WEB. The
    function itself places no such restriction on its `votes` argument
    though -- `isinstance(votes, int)` is the only gate -- so a list is a
    legitimate input on this arm regardless of which caller happens to send
    it today. Catches a regression making `:1155`'s wrap unconditional, which
    would nest an already-list `votes` into `[[choice.id]]`: `:1161`'s
    `int(choice_id)` inside the foreign-filter comprehension would then be
    called on the inner list itself and raise `TypeError: int() argument
    must be a string, a bytes-like object or a real number, not 'list'`,
    well before `:1175`'s `PollChoice.query.get(votes[0])` is ever reached.
    """
    s = seed_post_context()
    _seed_poll(s, mode='single')
    choice = db.session.query(PollChoice).filter_by(post_id=s.post.id).order_by(
        PollChoice.sort_order).first()

    with web_ctx(app, s.voter):
        vote_for_poll(s.post.id, [choice.id], SRC_WEB)

    votes = db.session.query(PollChoiceVote).filter_by(user_id=s.voter.id).all()
    assert {v.choice_id for v in votes} == {choice.id}


def test_a_banned_user_cannot_vote_in_a_poll(db_session, app):
    """`:1152`'s first disjunct (`user.banned`) and `:1153`'s abort.

    DEVIATES FROM THE BRIEF, which asked for SRC_API. SRC_API never reaches
    `:1152` for a banned user: `authorise_api_user` (app/utils.py:3628)
    raises a bare `Exception('incorrect_login')` for any bearer token whose
    user has `user.banned is True`, before `vote_for_poll`'s body runs past
    `:1148` -- the same finding `test_a_banned_user_is_aborted_with_403`
    already established for `vote_for_post`'s own SRC_API arm. SRC_WEB reads
    `current_user` directly at `:1150` with no `authorise_api_user` gate in
    front of it, so it is required to reach `:1152` at all. Unlike
    `vote_for_post`, there is no intervening `can_upvote`/`can_downvote` gate
    to route around, so a plain SRC_WEB call is enough -- no 'reversal'-style
    trick is needed here.

    Asserts 403 specifically and asserts no vote row, so an abort reached
    through an unrelated crash cannot satisfy it.
    """
    from werkzeug.exceptions import Forbidden

    s = seed_post_context()
    _seed_poll(s, mode='single')
    choice = db.session.query(PollChoice).filter_by(post_id=s.post.id).first()
    s.voter.banned = True
    db.session.commit()

    with web_ctx(app, s.voter):
        with pytest.raises(Forbidden) as excinfo:
            vote_for_poll(s.post.id, choice.id, SRC_WEB)

    assert excinfo.value.code == 403
    assert db.session.query(PollChoiceVote).filter_by(user_id=s.voter.id).count() == 0


def test_a_second_single_mode_vote_raises_through_the_api(db_session):
    """`:1172`'s false arm and `:1178`'s raise, through `:1177`'s true arm.

    `has_voted` is poll-scoped (app/models.py:3789-3792, filters on
    `post_id`), so a second vote in the SAME poll is rejected even for a
    DIFFERENT choice -- this is the `has_voted`/`vote_for_choice` asymmetry
    Step 5 reports and Tasks 9/10 act on. No `web_ctx` for either call, per
    the module docstring's SRC_API finding. `match=` pins the specific
    message, and the row-count assertion pins that the second call recorded
    nothing, so an unrelated crash under the same bare `Exception` type
    cannot satisfy this test.
    """
    s = seed_post_context()
    _seed_poll(s, mode='single')
    first, second = db.session.query(PollChoice).filter_by(
        post_id=s.post.id).order_by(PollChoice.sort_order).all()

    vote_for_poll(s.post.id, first.id, SRC_API, auth=bearer(s.voter))

    with pytest.raises(Exception, match='already voted'):
        vote_for_poll(s.post.id, second.id, SRC_API, auth=bearer(s.voter))

    assert db.session.query(PollChoiceVote).filter_by(user_id=s.voter.id).count() == 1


def test_a_second_single_mode_vote_is_silently_ignored_on_the_web(db_session, app):
    """`:1177`'s false arm.

    The web arm must not raise -- app/post/routes.py:643 has no handler and
    flashes 'Vote has been cast.' unconditionally afterwards, so a raise here
    would surface as an unhandled 500 rather than the flash the real caller
    expects. Catches a regression hoisting `:1178` out of `:1177`'s `if src
    == SRC_API:` guard, which would raise for the web arm too.
    """
    s = seed_post_context()
    _seed_poll(s, mode='single')
    first, second = db.session.query(PollChoice).filter_by(
        post_id=s.post.id).order_by(PollChoice.sort_order).all()

    with web_ctx(app, s.voter):
        vote_for_poll(s.post.id, [first.id], SRC_WEB)
        vote_for_poll(s.post.id, [second.id], SRC_WEB)

    assert db.session.query(PollChoiceVote).filter_by(user_id=s.voter.id).count() == 1


def test_too_many_choices_in_single_mode_raises_through_the_api(db_session):
    """`:1169`'s true arm and `:1171`'s raise, through `:1170`'s true arm.

    No `web_ctx`, per the module docstring's SRC_API finding. `match=` pins
    the specific message. Asserts no vote was recorded, because `:1171`
    raises before `:1172` is ever reached, and a regression that raised AFTER
    voting would otherwise look identical under a bare `pytest.raises
    (Exception)`.
    """
    s = seed_post_context()
    _seed_poll(s, mode='single')
    first, second = db.session.query(PollChoice).filter_by(
        post_id=s.post.id).order_by(PollChoice.sort_order).all()

    with pytest.raises(Exception, match='single'):
        vote_for_poll(s.post.id, [first.id, second.id], SRC_API,
                      auth=bearer(s.voter))

    assert db.session.query(PollChoiceVote).filter_by(user_id=s.voter.id).count() == 0


def test_too_many_choices_in_single_mode_falls_through_on_the_web(db_session, app):
    """`:1170`'s false arm -- the fall-through this round registers as a
    known equivalent-mutant-adjacent quirk, not a bug to fix.

    `:1169`'s length check raises only for SRC_API (`:1170`). The web arm
    falls through to `:1172`-`:1175`, which votes for `votes[0]` alone and
    silently discards the rest. This exact input is unreachable from the real
    web caller (app/post/routes.py:642), which normalizes single mode to one
    bare int before calling `vote_for_poll` -- so this test registers the
    fall-through's current behaviour (pinning it so it cannot change
    unnoticed) rather than asserting it is correct.
    """
    s = seed_post_context()
    _seed_poll(s, mode='single')
    first, second = db.session.query(PollChoice).filter_by(
        post_id=s.post.id).order_by(PollChoice.sort_order).all()

    with web_ctx(app, s.voter):
        vote_for_poll(s.post.id, [first.id, second.id], SRC_WEB)

    votes = db.session.query(PollChoiceVote).filter_by(user_id=s.voter.id).all()
    assert {v.choice_id for v in votes} == {first.id}


def test_no_choices_in_single_mode_raises_through_the_api(db_session):
    """`:1169`'s true arm at the OTHER end of the arity boundary, and the
    reason PC1's empty-votes guard sits INSIDE `:1162`'s block.

    `test_too_many_choices_in_single_mode_raises_through_the_api` pins
    `len(votes) != 1` from above (two choices); nothing pinned it from below.
    An empty list is reachable from the real API caller: `PollVoteRequest`
    (app/api/alpha/schema.py:1745) declares `choice_id` as
    `fields.List(fields.Integer(), required=True)`, and `required=True` only
    demands the key be present -- `[]` deserializes cleanly and reaches
    `vote_for_poll` via app/api/alpha/utils/post.py:1797. That same schema line
    states the contract pinned here: "Must have a length of 1 for a poll in
    single vote mode."

    The path matters. `votes` is empty but nothing in it was foreign, so
    `:1161`'s `foreign` is empty, `:1162` is false, and `:1166`'s
    `if not votes: return` never runs -- control reaches `:1169` and `:1171`
    raises. Task 11's mutation pass found that dedenting `:1166`-`:1167` out of
    `:1162`'s block, making the guard unconditional, was invisible to the whole
    suite: it turns this documented API error into a silent success. This test
    is the only thing distinguishing the two placements. Asserts no vote was
    recorded as well, since `:1171` raises before `:1172`, so a regression that
    raised AFTER voting cannot pass under a bare `pytest.raises(Exception)`.
    """
    s = seed_post_context()
    _seed_poll(s, mode='single')

    with pytest.raises(Exception, match='single'):
        vote_for_poll(s.post.id, [], SRC_API, auth=bearer(s.voter))

    assert db.session.query(PollChoiceVote).filter_by(user_id=s.voter.id).count() == 0


def test_multiple_mode_records_every_choice(db_session, app):
    """`:1168`'s false arm and `:1180`'s loop over several choices.

    The real multiple-mode web caller (app/post/routes.py:642) sends
    `request.form.getlist('poll_choice[]')`, a list of strings -- `:1184`'s
    explicit `int(choice_id)` exists for that reason, so this test passes
    plain ints rather than strings to keep the choice-identity assertion
    simple. The casts are EXECUTED either way but not observable either way:
    Task 11's mutation pass found that removing any of the five
    `int(choice_id)` casts survived the whole file while every call site
    passed ints. Two tests below carry the real caller's type, and they are not
    interchangeable: `test_multiple_mode_accepts_the_string_ids_the_web_caller_sends`
    covers this ordinary path but kills no single-site cast mutation, while
    `test_the_membership_filter_keeps_string_ids_when_one_is_foreign` is the
    one that kills `:1165`'s. Keep both when editing this test. Catches a
    regression sending a
    multiple-mode poll down the single-mode branch, which would record only
    `votes[0]` and silently drop the rest -- comparing sets rather than a
    single id, since a query-planner ordering assumption would let that
    regression pass by accident.
    """
    s = seed_post_context()
    _seed_poll(s, mode='multiple', choices=('a', 'b', 'c'))
    all_choices = db.session.query(PollChoice).filter_by(post_id=s.post.id).all()
    ids = {c.id for c in all_choices}

    with web_ctx(app, s.voter):
        vote_for_poll(s.post.id, sorted(ids), SRC_WEB)

    votes = db.session.query(PollChoiceVote).filter_by(user_id=s.voter.id).all()
    assert {v.choice_id for v in votes} == ids


def test_multiple_mode_accepts_the_string_ids_the_web_caller_sends(db_session, app):
    """The type the real multiple-mode caller sends, on the ordinary path.

    app/post/routes.py:642 builds `votes` from
    `request.form.getlist('poll_choice[]')` for multiple mode, which yields a
    list of STRINGS -- only the single-mode half of that ternary applies
    `int(...)`. Every `vote_for_poll` call in this file passed ints before this
    test, so the suite exercised a type the real multiple-mode caller never
    sends. This is the ordinary case: every choice legitimate, none foreign.

    This test kills no SINGLE-site mutation of the five `int(choice_id)` casts,
    and that is itself the thing to carry forward. Dropping `:1161`'s cast
    alone changes nothing observable: `'5' not in {5}` is true, so every choice
    is misclassified foreign and `:1162` opens, but `:1165`'s cast is intact
    and re-filters the strings back into the IDENTICAL list, so the vote
    proceeds normally. `:1161`'s cast is redundant given `:1165`'s. The silent
    total loss -- `votes` emptied, `:1167` returning with nothing recorded and
    no error at all, since `:1163` raises only for SRC_API while
    app/post/routes.py:644 flashes 'Vote has been cast.' -- needs `:1165`'s
    cast gone, which `test_the_membership_filter_keeps_string_ids_when_one_is_foreign`
    pins, or both casts gone together, which no single-site mutation reaches.

    What this test is for: it is the only string-typed call on the non-foreign
    path, and its `num_votes` assertion is the only measurement anywhere of
    what `:1183`, `:1184` and `:1187` do with a string. Those three survive
    their own mutations because psycopg2/PostgreSQL coerce a numeric string at
    the parameter-binding boundary -- a property of the driver and dialect, NOT
    of this code, so it is a survivor to re-check on any DB change rather than
    an equivalence. This test is what makes that a measured claim instead of an
    assumed one, and it is what fails first if the coercion goes away. Asserts
    `num_votes` as well as the recorded set because `vote_for_choice`
    (app/models.py:3794) moves the tally through a separate
    `PollChoice.query.get`.
    """
    s = seed_post_context()
    _seed_poll(s, mode='multiple', choices=('a', 'b'))
    choices = db.session.query(PollChoice).filter_by(post_id=s.post.id).all()
    ids = {c.id for c in choices}

    with web_ctx(app, s.voter):
        vote_for_poll(s.post.id, [str(i) for i in sorted(ids)], SRC_WEB)

    votes = db.session.query(PollChoiceVote).filter_by(user_id=s.voter.id).all()
    assert {v.choice_id for v in votes} == ids
    for c in choices:
        db.session.refresh(c)
    assert {c.num_votes for c in choices} == {1}


def test_multiple_mode_with_no_choices_records_nothing(db_session, app):
    """`:1180`'s zero-iteration exit arc.

    app/post/routes.py:642 uses `request.form.getlist`, which returns `[]`
    when the voter submits nothing for multiple mode, so this input is
    reachable from the real caller. Catches a regression giving the loop a
    default choice or otherwise recording a vote when `votes` is empty.
    """
    s = seed_post_context()
    _seed_poll(s, mode='multiple')

    with web_ctx(app, s.voter):
        vote_for_poll(s.post.id, [], SRC_WEB)

    assert db.session.query(PollChoiceVote).filter_by(user_id=s.voter.id).count() == 0


def test_an_api_vote_for_another_polls_choice_is_rejected(db_session):
    """PC1: `vote_for_poll` USED TO accept a choice belonging to another poll.
    The membership filter that closed it is now at `:1159-1167`.

    app/api/alpha/utils/post.py:1793 hands `data['choice_id']` straight through.
    Against the pre-PC1 tree this recorded a PollChoiceVote whose post_id was
    the target poll and whose choice_id belonged to another, and incremented the
    FOREIGN choice's num_votes. It failed then and passes now that the filter
    has landed: `:1161` collects the foreign ids and `:1163-1164` raises on the
    SRC_API arm, which is what the body below asserts.

    No `web_ctx` -- this is the SRC_API arm, and the module docstring's
    "SRC_API ARM DOES NOT NEED A REQUEST CONTEXT" finding applies here as it
    does to every other SRC_API test in this file.
    """
    s = seed_post_context()
    _seed_poll(s, mode='single')
    other_post = make_post(s.community, s.author, 'https://local.example/p/other')
    from datetime import timedelta
    from app.models import utcnow
    other_poll = Poll(post_id=other_post.id, mode='single', local_only=False,
                      end_poll=utcnow() + timedelta(days=1))
    db.session.add(other_poll)
    db.session.add(PollChoice(post_id=other_post.id, choice_text='foreign',
                              sort_order=0, num_votes=0))
    db.session.commit()
    foreign = db.session.query(PollChoice).filter_by(post_id=other_post.id).one()

    with pytest.raises(Exception, match='does not belong to this poll'):
        vote_for_poll(s.post.id, foreign.id, SRC_API, auth=bearer(s.voter))

    assert db.session.query(PollChoiceVote).filter_by(user_id=s.voter.id).count() == 0
    db.session.refresh(foreign)
    assert foreign.num_votes == 0


def test_a_nonexistent_choice_id_does_not_dereference_none(db_session):
    """PC1's second effect. The brief expected the None dereference at `:1175`'s
    `PollChoice.query.get(votes[0]).choice_text`, but the OBSERVED failure
    happens earlier and elsewhere: `poll.vote_for_choice` (app/models.py:3800-
    3801) does `choice = PollChoice.query.get(choice_id); choice.num_votes +=
    1`, and that `choice.num_votes` read is what raises AttributeError on
    `None` for a nonexistent id, before `:1175` is ever reached. Either
    dereference would fail the same way; this test observed the
    `vote_for_choice` one. A choice id matching no row makes that an
    AttributeError on None. The membership filter now at `:1159-1167` closed
    it, because a nonexistent choice is not a member. It failed with that
    AttributeError against the pre-PC1 tree and passes now that the filter has
    landed -- `:1164` raises on the SRC_API arm instead, which is the
    behaviour the body below asserts.

    No `web_ctx` -- SRC_API arm, same reasoning as the test above.
    """
    s = seed_post_context()
    _seed_poll(s, mode='single')
    highest = db.session.query(PollChoice).order_by(PollChoice.id.desc()).first()

    with pytest.raises(Exception, match='does not belong to this poll'):
        vote_for_poll(s.post.id, highest.id + 1000, SRC_API,
                      auth=bearer(s.voter))

    assert db.session.query(PollChoiceVote).filter_by(user_id=s.voter.id).count() == 0


def test_the_membership_filter_keeps_every_legitimate_choice(db_session, app):
    """PC1's safety demonstration, required by the spec.

    A multiple-mode vote naming two of this poll's choices and one foreign
    choice must record both legitimate votes and reject only the foreign one.
    Catches a filter that rejects the whole request, or one that matches on the
    wrong column and drops everything.
    """
    s = seed_post_context()
    _seed_poll(s, mode='multiple', choices=('a', 'b'))
    other_post = make_post(s.community, s.author, 'https://local.example/p/other')
    from datetime import timedelta
    from app.models import utcnow
    db.session.add(Poll(post_id=other_post.id, mode='single', local_only=False,
                        end_poll=utcnow() + timedelta(days=1)))
    db.session.add(PollChoice(post_id=other_post.id, choice_text='foreign',
                              sort_order=0, num_votes=0))
    db.session.commit()
    mine = {c.id for c in db.session.query(PollChoice).filter_by(post_id=s.post.id)}
    foreign = db.session.query(PollChoice).filter_by(post_id=other_post.id).one()

    with web_ctx(app, s.voter):
        vote_for_poll(s.post.id, sorted(mine) + [foreign.id], SRC_WEB)

    votes = db.session.query(PollChoiceVote).filter_by(user_id=s.voter.id).all()
    assert {v.choice_id for v in votes} == mine
    db.session.refresh(foreign)
    assert foreign.num_votes == 0


def test_the_membership_filter_keeps_string_ids_when_one_is_foreign(db_session, app):
    """`:1165`'s `int(choice_id)` -- the one cast the all-legitimate string
    test above cannot reach.

    `:1165` runs only inside `:1162`, so a request whose choices are all
    legitimate leaves `foreign` empty and never executes it. This sends the
    real web caller's string list (app/post/routes.py:642) WITH one foreign
    choice, so `:1161`'s intact cast marks that one, `:1162` opens, and
    `:1165` re-filters the strings.

    Dropping `:1165`'s cast makes `'5' in {5}` false for the legitimate
    choices too, so `votes` empties and `:1167` returns -- the entire vote
    silently discarded rather than the foreign entry alone, and with no error
    on the web arm. That mutation survived all 56 tests before this one
    existed. Compares sets, never row order.
    """
    s = seed_post_context()
    _seed_poll(s, mode='multiple', choices=('a', 'b'))
    other_post = make_post(s.community, s.author, 'https://local.example/p/other')
    from datetime import timedelta
    from app.models import utcnow
    db.session.add(Poll(post_id=other_post.id, mode='single', local_only=False,
                        end_poll=utcnow() + timedelta(days=1)))
    db.session.add(PollChoice(post_id=other_post.id, choice_text='foreign',
                              sort_order=0, num_votes=0))
    db.session.commit()
    mine = {c.id for c in db.session.query(PollChoice).filter_by(post_id=s.post.id)}
    foreign = db.session.query(PollChoice).filter_by(post_id=other_post.id).one()

    with web_ctx(app, s.voter):
        vote_for_poll(s.post.id, [str(i) for i in sorted(mine) + [foreign.id]],
                      SRC_WEB)

    votes = db.session.query(PollChoiceVote).filter_by(user_id=s.voter.id).all()
    assert {v.choice_id for v in votes} == mine
    db.session.refresh(foreign)
    assert foreign.num_votes == 0


def test_a_web_single_mode_vote_for_only_a_foreign_choice_does_not_crash(db_session, app):
    """Pins the SRC_WEB single-mode arm that the raise-for-API/skip-for-web
    asymmetry exists for -- fix round 1 of PC1 left this arm broken.

    A single-mode SRC_WEB vote whose only choice is foreign gets filtered by
    the membership check down to an empty `votes` list. The skip is normally
    communicated back by `if src == SRC_API: raise`, which never fires for
    SRC_WEB, so control fell through to `poll.vote_for_choice(votes[0], ...)`
    and indexed an empty list. That is an unhandled 500 on
    app/post/routes.py:643, a route with no error handling that flashes
    'Vote has been cast.' unconditionally -- exactly the failure mode the
    raise-for-API/skip-for-web semantics exists to keep that route from ever
    having. It failed with that IndexError against the fix-round-1 tree and
    passes now that the membership branch returns early at `:1166-1167` when
    nothing survives the filter.
    """
    s = seed_post_context()
    _seed_poll(s, mode='single')
    other_post = make_post(s.community, s.author, 'https://local.example/p/other')
    from datetime import timedelta
    from app.models import utcnow
    db.session.add(Poll(post_id=other_post.id, mode='single', local_only=False,
                        end_poll=utcnow() + timedelta(days=1)))
    db.session.add(PollChoice(post_id=other_post.id, choice_text='foreign',
                              sort_order=0, num_votes=0))
    db.session.commit()
    foreign = db.session.query(PollChoice).filter_by(post_id=other_post.id).one()

    with web_ctx(app, s.voter):
        vote_for_poll(s.post.id, foreign.id, SRC_WEB)

    assert db.session.query(PollChoiceVote).filter_by(user_id=s.voter.id).count() == 0


def test_resubmitting_multiple_mode_choices_does_not_federate_again(db_session, app):
    """PC2: the multiple-mode loop's task_selector call used to fire even when
    `vote_for_choice` no-ops.

    `Poll.vote_for_choice` guards its whole body with `if not existing_vote:`
    (app/models.py:3797), so a repeat submission records nothing -- but the
    loop's `task_selector` call (now guarded at `:1185`-`:1187`) used to
    federate one phantom vote per choice regardless, and remote instances
    would increment totals the origin does not have.

    Counts task_selector calls rather than inspecting network traffic, because
    task_selector runs synchronously under eager Celery and the poll task
    returns at its own first guard with `community.private` set (this file's
    `seed_post_context()` default) -- no outbound request, no redis key, so no
    `redis_double` and no `_clear_votes_cast` are needed here.
    """
    calls = []
    s = seed_post_context()
    _seed_poll(s, mode='multiple', choices=('a', 'b'))
    ids = sorted(c.id for c in
                 db.session.query(PollChoice).filter_by(post_id=s.post.id))

    import app.shared.post as post_module
    original = post_module.task_selector

    def counting_task_selector(task_key, **kwargs):
        calls.append(task_key)
        return original(task_key, **kwargs)

    post_module.task_selector = counting_task_selector
    try:
        with web_ctx(app, s.voter):
            vote_for_poll(s.post.id, ids, SRC_WEB)
        first_round = len(calls)

        with web_ctx(app, s.voter):
            vote_for_poll(s.post.id, ids, SRC_WEB)
    finally:
        post_module.task_selector = original

    assert first_round == 2
    assert len(calls) == 2, \
        'the repeat submission federated votes that were never recorded'
    assert db.session.query(PollChoiceVote).filter_by(user_id=s.voter.id).count() == 2
