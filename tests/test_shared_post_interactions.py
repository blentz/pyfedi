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

WHAT ACTUALLY BLOCKS AN API-ARM TEST IS `_seed()`'s USERS NOT BEING LOCAL.
`_seed()` calls `make_user(instance, 'author')` and `make_user(instance,
'voter')` with `local` left at its factory default of `False`
(tests/factories.py:41), so both get a non-None `ap_id`. `authorise_api_user`
(app/utils.py:3628) rejects any bearer token whose user has
`ap_id is not None` with `raise Exception('incorrect_login')`, and it does
this before the source fork ever reaches `user_ip_banned`. This is what Probe
D actually observed with `_seed()`'s default voter, and it is unrelated to
request context: tests/test_shared_post_edit.py:202's `_seed()` avoids it by
passing `local=True` to its one user. Any test in this round that authorises
a Group A SRC_API arm from a bearer token must mint its user the same way.

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
    """
    instance = make_instance('local.example', software='piefed')
    site = make_site()
    author = make_user(instance, 'author')
    voter = make_user(instance, 'voter')
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
    """`:1139`'s `flair_list = []`, reached through `:1137`'s true arm.

    Catches a regression returning `post.flair` unguarded, which would hand the
    caller None instead of a list.
    """
    s = _seed()

    flair_list = get_post_flair_list(s.post)

    assert flair_list == []

