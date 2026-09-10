"""`app/shared/post.py`'s reader interactions -- Group A of five.

SCOPE. The eight reader-facing functions, 63 uncovered statements and 51
uncovered branch arcs when this file was started:

  vote_for_post :31-74 (29/18), vote_for_poll :1146-1174 (20/18),
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
CONSEQUENTLY, no SRC_API-arm test in this round wraps its call in `_web_ctx`
-- `_web_ctx` stays reserved for the SRC_WEB arms, which need `flash` and
`request.args` to be available.

WHAT ACTUALLY BLOCKS AN API-ARM TEST IS AN `ap_id`, NOT CONTEXT.
`authorise_api_user`'s condition at app/utils.py:3628,
`if user.ap_id is not None or user.verified is False or user.banned is True or
user.deleted is True:`, raises `Exception('incorrect_login')` at its body,
app/utils.py:3629, for any bearer token whose user has a non-None `ap_id` --
before the source fork ever reaches `user_ip_banned`. Probe D's `_seed()`
originally called `make_user(instance, 'author')` and `make_user(instance,
'voter')` with `local` left at its factory default of `False`
(tests/factories.py:41), so both got a non-None `ap_id` and every bearer-token
call against them failed here, unrelated to request context. Round-1 controller
ruling: `_seed()` now passes `local=True` to both calls (see `_seed()`'s own
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
render without error against `_seed()`'s objects and a logged-in `_web_ctx`
user -- no missing `g` attribute, no missing `Site` row. But `render_template`
as imported at app/shared/post.py:23 is `app.utils.render_template`, not
Flask's own. app/utils.py:74-79 calls `flask.render_template` internally to
get a plain string, then app/utils.py:86-99 wraps that string in
`make_response(content)` and adds `ETag`/`Cache-Control`/`Link` headers before
returning it. Every SRC_WEB-arm assertion in this round must therefore read
`result.status_code` and `result.get_data(as_text=True)`; `isinstance(result,
str)` is always False and is not evidence of a broken render.

THE FEDERATION LEVER IS `community.private`, NOT `local_only`. task_selector
runs synchronously under eager Celery (tests/conftest.py:105-110), so
vote_for_post:60 and vote_for_poll:1165/:1173 execute real bodies in
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
from contextlib import contextmanager
from types import SimpleNamespace

from flask import get_flashed_messages
from flask_login import login_user

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
from tests.factories import make_community, make_instance, make_post, \
    make_post_flair, make_site, make_user


def _seed(*, private=True):
    """One instance, one site, one community, an author, a voter and a post.

    `private=True` is the federation lever described in the module docstring: it
    stops app/shared/tasks/likes.py's eager task bodies at their first guard so
    no test issues an outbound request. Pass private=False only in a test that
    means to exercise the federation path, and expect to arrange for it.

    `make_community` hardcodes `instance_id=1` (tests/factories.py:141) and
    tests/conftest.py:131 resets sequences after every test, so the instance
    seeded first here lands on id 1 and the community resolves to it. Unlike the
    maintenance rounds, this round WANTS id 1 rather than avoiding it.

    author and voter are minted `local=True` (ap_id None) because every SRC_API
    arm in this round authorises through `authorise_api_user`
    (app/utils.py:3628), which rejects any bearer token whose user has a non-None
    `ap_id`. See the module docstring's "SRC_API ARM" section -- this is a
    Round-1 controller ruling, not the original brief. `make_community`
    (tests/factories.py:148) hardcodes `local_only=False` regardless, so
    `can_downvote`'s `community.local_only and not user.is_local()` check is not
    engaged by this change either way.
    """
    instance = make_instance('local.example', software='piefed')
    site = make_site()
    author = make_user(instance, 'author', local=True)
    voter = make_user(instance, 'voter', local=True)
    community = make_community('interactions')
    community.private = private
    db.session.commit()
    post = make_post(community, author, 'https://local.example/p/1')
    return SimpleNamespace(instance=instance, site=site, author=author,
                           voter=voter, community=community, post=post)


@contextmanager
def _web_ctx(app, user, query_string=''):
    """A request context with `user` logged in, for the SRC_WEB arms.

    `query_string` feeds `request.args`, which vote_for_post:45 and :72 read as
    `request.args.get('style', '')` to choose between two templates.
    """
    with app.test_request_context('/?' + query_string):
        login_user(user)
        yield


def _bearer(user):
    """The Authorization header value the SRC_API arms authorise from.

    The precedent is tests/test_shared_post_edit.py:300, which passes
    `auth=f'Bearer {s.user.encode_jwt_token()}'` into edit_post's API branch.
    """
    return f'Bearer {user.encode_jwt_token()}'


def test_extra_rate_limit_check_returns_false_for_any_user(db_session):
    """`:160`'s `return False`, the function's whole body.

    Catches a regression making the stub return anything truthy. Its only
    production caller is make_post:166, which is Group D, so this round reaches
    it by direct call and says so rather than pretending otherwise.
    """
    s = _seed()

    assert extra_rate_limit_check(s.voter) is False


def test_flair_list_loads_the_post_when_given_an_integer(db_session):
    """`:1134`'s true arm and `:1135`'s lookup.

    Catches a regression dropping the isinstance branch, which would leave an
    int bound to `post` and fail at `:1137`'s attribute read.
    """
    s = _seed()
    make_post_flair(s.post, name='news')

    flair_list = get_post_flair_list(s.post.id)

    assert {f.id for f in flair_list} == {f.id for f in s.post.flair}
    assert len(flair_list) == 1


def test_flair_list_accepts_a_post_object_and_skips_the_lookup(db_session):
    """`:1134`'s false arm.

    Pins that a Post instance is used as given. Catches a regression that made
    the lookup unconditional, which would raise on a Post argument.
    """
    s = _seed()
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
    s = _seed()

    flair_list = get_post_flair_list(s.post)

    assert flair_list == []


def test_marking_read_inserts_one_row_per_post_id(db_session):
    """`:1118-1120`'s INSERT, reached through `:1116`'s true arm.

    Catches a regression inverting `:1116`, which would send a read=True call
    down the DELETE branch and leave the table empty.
    """
    s = _seed()
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
    s = _seed()

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
    s = _seed()
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
    s = _seed()
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
    s = _seed()
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
    wrap the call in `_web_ctx`.
    """
    s = _seed()

    result = bookmark_post(s.post.id, SRC_API, auth=_bearer(s.voter))

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
    s = _seed()

    with _web_ctx(app, s.voter):
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
    s = _seed()
    bookmark_post(s.post.id, SRC_API, auth=_bearer(s.voter))

    with pytest.raises(Exception, match='already been bookmarked'):
        bookmark_post(s.post.id, SRC_API, auth=_bearer(s.voter))

    assert db.session.query(PostBookmark).filter_by(
        post_id=s.post.id, user_id=s.voter.id).count() == 1


def test_bookmarking_twice_through_the_web_flashes_instead_of_raising(db_session, app):
    """`:88`'s false arm and `:91`'s flash.

    The WEB duplicate path must NOT raise -- that asymmetry is the arm. Catches
    a regression hoisting the raise out of `:88`, which would give the web route
    an exception it has no handler for. Also asserts the flashed message's
    content (read inside the same `_web_ctx`, since `flash`/`get_flashed_messages`
    both operate on that request's session), so a regression that drops `:91`'s
    `flash(_(msg))` call outright does not survive on `result is None` alone.
    """
    s = _seed()
    with _web_ctx(app, s.voter):
        bookmark_post(s.post.id, SRC_WEB)

    with _web_ctx(app, s.voter):
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
    s = _seed()
    bookmark_post(s.post.id, SRC_API, auth=_bearer(s.voter))

    result = remove_bookmark_post(s.post.id, SRC_API, auth=_bearer(s.voter))

    assert result == s.voter.id
    assert db.session.query(PostBookmark).filter_by(
        post_id=s.post.id, user_id=s.voter.id).count() == 0


def test_removing_a_bookmark_that_does_not_exist_raises_through_the_api(db_session):
    """`:101`'s false arm and `:107`'s raise, through `:106`'s true arm.

    Catches a regression that made the delete unconditional, which would raise
    a different error entirely on a None row.
    """
    s = _seed()

    with pytest.raises(Exception, match='was not bookmarked'):
        remove_bookmark_post(s.post.id, SRC_API, auth=_bearer(s.voter))


def test_removing_a_bookmark_that_does_not_exist_flashes_on_the_web(db_session, app):
    """`:106`'s false arm and `:109`'s flash, plus `:111`'s false arm.

    Catches a regression hoisting `:107`'s raise out of `:106`, and also
    asserts the flashed message's content (read inside the same `_web_ctx`) so
    a regression that drops `:109`'s `flash(_(msg))` call outright does not
    survive on `result is None` alone.
    """
    s = _seed()

    with _web_ctx(app, s.voter):
        result = remove_bookmark_post(s.post.id, SRC_WEB)
        flashed = get_flashed_messages()

    assert result is None
    assert len(flashed) == 1
    assert 'was not bookmarked' in flashed[0]


def test_subscribing_through_the_api_creates_the_subscription(db_session):
    """`:143-147`'s creation, reached through `:124`'s false arm and `:136`'s
    false arm, and `:150`'s return.

    No `_web_ctx` here -- the module docstring's "SRC_API ARM DOES NOT NEED A
    REQUEST CONTEXT" finding applies: `:117`'s ternary reads
    `authorise_api_user(auth)` for SRC_API, never `current_user`, so a bearer
    call authorises with no request context at all.

    Catches a regression inverting `:136`, which would reject a first
    subscription as already existing.
    """
    s = _seed()

    result = subscribe_post(s.post.id, True, SRC_API, auth=_bearer(s.voter))

    assert result == s.voter.id
    assert db.session.query(NotificationSubscription).filter_by(
        entity_id=s.post.id, user_id=s.voter.id).count() == 1


def test_subscribing_twice_through_the_api_raises(db_session):
    """`:136`'s true arm and `:139`'s raise, through `:138`'s true arm.

    Asserts the count stayed at one, so the raise is not reached through an
    unrelated crash that would satisfy pytest.raises just as well.
    """
    s = _seed()
    subscribe_post(s.post.id, True, SRC_API, auth=_bearer(s.voter))

    with pytest.raises(Exception, match='already existed'):
        subscribe_post(s.post.id, True, SRC_API, auth=_bearer(s.voter))

    assert db.session.query(NotificationSubscription).filter_by(
        entity_id=s.post.id, user_id=s.voter.id).count() == 1


def test_unsubscribing_through_the_api_deletes_the_subscription(db_session):
    """`:124`'s true arm, `:125`'s true arm, `:126`'s delete.

    Catches a regression changing `:124` to a truthiness test, which would send
    `subscribe=False` down the creation branch.
    """
    s = _seed()
    subscribe_post(s.post.id, True, SRC_API, auth=_bearer(s.voter))

    result = subscribe_post(s.post.id, False, SRC_API, auth=_bearer(s.voter))

    assert result == s.voter.id
    assert db.session.query(NotificationSubscription).filter_by(
        entity_id=s.post.id, user_id=s.voter.id).count() == 0


def test_unsubscribing_when_none_exists_raises_through_the_api(db_session):
    """`:125`'s false arm and `:131`'s raise, through `:130`'s true arm."""
    s = _seed()

    with pytest.raises(Exception, match='did not exist'):
        subscribe_post(s.post.id, False, SRC_API, auth=_bearer(s.voter))

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
    the call (`_seed()` creates none), so `post.notify_new_replies(user_id)`
    at `:120` is False and `subscribe` becomes True -- the create arm below.

    Catches a regression deleting `:119`, which would make the web arm honour
    the argument and silently change the toggle route's behaviour.
    """
    s = _seed()
    assert db.session.query(NotificationSubscription).filter_by(
        entity_id=s.post.id, user_id=s.voter.id).count() == 0

    with _web_ctx(app, s.voter):
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
    s = _seed()
    with _web_ctx(app, s.voter):
        subscribe_post(s.post.id, True, SRC_WEB)

    assert db.session.query(NotificationSubscription).filter_by(
        entity_id=s.post.id, user_id=s.voter.id).count() == 1

    with _web_ctx(app, s.voter):
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

    Uses `_web_ctx` (not an SRC_API test) because `:117`'s else-arm reads
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
    s = _seed()

    with _web_ctx(app, s.voter):
        result = subscribe_post(s.post.id, False, SRC_PLD)
        flashed = get_flashed_messages()

    assert result.status_code == 200
    assert len(flashed) == 1
    assert 'did not exist' in flashed[0]
    assert db.session.query(NotificationSubscription).filter_by(
        entity_id=s.post.id, user_id=s.voter.id).count() == 0

    subscribe_post(s.post.id, True, SRC_API, auth=_bearer(s.voter))

    with _web_ctx(app, s.voter):
        result = subscribe_post(s.post.id, True, SRC_PLD)
        flashed = get_flashed_messages()

    assert result.status_code == 200
    assert len(flashed) == 1
    assert 'already existed' in flashed[0]
    assert db.session.query(NotificationSubscription).filter_by(
        entity_id=s.post.id, user_id=s.voter.id).count() == 1

