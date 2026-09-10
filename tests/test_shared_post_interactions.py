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

from flask_login import login_user

from app import db
from app.constants import SRC_API, SRC_WEB
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

