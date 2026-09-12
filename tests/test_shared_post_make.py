"""`app/shared/post.py`'s `make_post` -- Group D of five.

SCOPE. One function, `:163-249`, 61 statements and 26 branch arcs, and the
only group in this module that began at ZERO PERCENT: no test had ever called
it. Three collaborators block a first call, and Task 1 settled all three
before any assertion in this file was written. They are recorded here because
each one is the reason a naive first test fails, and the failure modes look
nothing like the cause.

  1. `can_create_post` (app/utils.py:2504-2506) refuses a LOCAL user whose
     `private_key` is None. `make_user(..., local=True)` leaves it None unless
     `with_keys=True`, so every user `seed_post_context` produces is refused at
     `:187` before reaching anything. Keypair generation costs roughly a second
     per user, which is why `seed_make_context` mints exactly one keyed author
     and every test reuses it.

  2. `g.site` is never set for a local author. `:219` is
     `community.last_active = g.site.last_active = utcnow()`.
     `can_create_post` has a `g.site` fallback at app/utils.py:2508-2509, but
     it sits on the REMOTE-user branch, which a local author never reaches. In
     production `g.site` comes from the `before_request` at
     app/request_hooks.py:79; this harness never dispatches a request, so it
     must be supplied explicitly.

  3. `notify_about_post` fires on the ordinary path. `:238` guards on
     `post.status == POST_STATUS_PUBLISHED`, which is the column default, and
     `tests/conftest.py:106` sets `task_always_eager=True`, so
     `notify_about_post_task`'s body runs inline rather than queueing.

HELPERS ARE IMPORTED FROM THE EDIT FILE, NOT DUPLICATED.
`_api_input`, `_web_form`, `_Field` and `_OMIT` come from
`tests.test_shared_post_edit`. `make_post` passes its `input` straight through
to `edit_post` at `:231`, so the two functions want the SAME shapes, and
`_web_form` already carries `link_url` and `video_url`
(tests/test_shared_post_edit.py:152). Cross-module test imports have precedent
here: tests/test_ap_collections.py:6 imports `seed_actors` from
tests/test_actor_profiles.py, and tests/__init__.py makes `tests` a package.
Those names are underscore-prefixed, which normally means module-private;
importing them is a deliberate choice to avoid a second copy drifting from the
first. RULE OF THREE: a third consumer justifies promoting them to
tests/factories.py. Two do not.

`edit_post` REALLY RUNS in nearly every test here. `make_post:231` delegates to
it with `from_scratch=True`, and that is the only way those arms of `edit_post`
are ever reached, so letting the call happen is what proves the two functions
compose. It is monkeypatched ONLY where the rollback at `:233-236` must be
reached. A consequence worth knowing when a test here fails: the failure may
originate below `:231`, in `edit_post`, not in the function this file names.

`g.site` IS SUPPLIED, NOT SUPPRESSED, AND FROM `seed_make_context` -- NOT FROM
INSIDE `web_ctx` (Probe B; corrects an earlier draft of this paragraph, which
assumed the opposite before the probe ran). `g` is bound to Flask's APP
context, not the request context: `tests/conftest.py`'s `app` fixture pushes
one app context for the whole test (`with application.app_context(): yield
application`, `tests/conftest.py:112-113`) and `db_session` clears
`g.__dict__` once per test before the test body runs
(`g.__dict__.clear()`, `tests/conftest.py:156`) -- so anything stashed on `g` during
the test survives across `with web_ctx(...)` blocks entering and leaving,
because those only push/pop a REQUEST context on top of the same already-open
app context. `seed_make_context` therefore sets `g.site = site` directly in
its own body, before `web_ctx` is ever opened, and it is visible inside the
`web_ctx` block later. This also answers Probe B's second question: the
SRC_API arm has no request context at all, but it runs inside the same
per-test app context the `app` fixture pushes, so `g.site` set here is visible
to it too, with no `web_ctx` and no extra mechanism needed.

`notify_about_post` IS MONKEYPATCHED PER TEST, NOT GLOBALLY (Probe C). Every
test in this file that does not care what `notify_about_post` does should
receive a `monkeypatch.setattr(post_module, 'notify_about_post', ...)` of its
own (or use the `stub_notify` fixture below, which does exactly that), rather
than relying on an autouse fixture or a module-level patch. Task 5 needs to
RECORD the call (arguments, call count), and a global suppression would hide
it from a brief that only knows about this file's public surface.
"""

import os
from types import SimpleNamespace

import pytest

from flask import g

from app import db
from app.constants import (
    POST_TYPE_ARTICLE,
    POST_TYPE_LINK,
    POST_TYPE_VIDEO,
    POST_STATUS_PUBLISHED,
    SRC_API,
    SRC_WEB,
)
from app.models import Domain, Post, PostVote
from tests.factories import (
    bearer,
    make_community,
    make_domain,
    make_instance,
    make_site,
    make_user,
    web_ctx,
)
from tests.test_shared_post_edit import _OMIT, _Field, _api_input, _web_form

import app.shared.post as post_module
from app.shared.post import make_post


def seed_make_context(community_name='making'):
    """One instance, one site, one KEYED local author, one community.

    `with_keys=True` is load-bearing, not incidental: `can_create_post`
    (app/utils.py:2505) refuses a local user whose `private_key` is None, so
    without it every test in this file dies at `:187` with 'You are not
    permitted to make posts in this community' -- a message that names a
    permission problem and gives no hint that the real cause is a missing key.

    It costs roughly a second (tests/factories.py:44-49), so ONE author is
    minted here and shared. Do not add a second keyed user without a reason.
    """
    instance = make_instance('local.example', software='piefed')
    site = make_site()
    author = make_user(instance, 'maker', local=True, with_keys=True)
    community = make_community(community_name)
    db.session.commit()
    g.site = site
    return SimpleNamespace(instance=instance, site=site, author=author,
                            community=community)


@pytest.fixture
def stub_notify(monkeypatch):
    """Per-test suppression of `notify_about_post`, recording each call.

    Probe C decision (see file docstring): several tests take `:238`'s true
    arm on the ordinary published-post path and must not each solve this
    separately, but the patch is applied HERE, per test that requests this
    fixture, not as an autouse fixture or a module-level patch -- Task 5 needs
    to see and record the call, which a global suppression would hide from it.
    """
    calls = []

    def _fake_notify_about_post(post):
        calls.append(post)

    monkeypatch.setattr(post_module, 'notify_about_post', _fake_notify_about_post)
    return calls


def test_an_article_post_is_created_through_the_web_arm(db_session, app):
    """`:164`'s false arm through to `:246`'s bare return.

    The first end-to-end call this function has ever had. Asserts on the row
    reaching the database, not merely on the return value, because `:246`
    returns the same object `:206` built and a regression that never committed
    would still return it. `notify_about_post` is left to run for real here
    (Probe C found it harmless against a community/author with no followers)
    rather than stubbed, so this test also witnesses `:238`'s true arm.
    """
    s = seed_make_context()

    with web_ctx(app, s.author):
        result = make_post(_web_form(), s.community, POST_TYPE_ARTICLE, SRC_WEB)

    assert result is not None
    rows = db.session.query(Post).filter_by(community_id=s.community.id).all()
    assert len(rows) == 1
    assert rows[0].user_id == s.author.id
    assert rows[0].title == 'a title'


def test_the_api_arm_returns_the_user_id_and_the_post(db_session):
    """`:164`'s true arm and `:244`'s two-tuple.

    NO `web_ctx`: the API arm takes no request context. The two-tuple is what
    distinguishes this arm from `:246`'s bare return, and a test asserting only
    that a post exists would pass on either arm -- `:206-228` runs on both.

    This is also the false-arm witness for `:166`: `extra_rate_limit_check` is
    left unpatched, so it takes its ordinary `return False`, `:166` is taken
    false, and the call proceeds to `:168` and beyond.
    """
    s = seed_make_context()

    result = make_post(_api_input(), s.community, POST_TYPE_ARTICLE, SRC_API,
                        auth=bearer(s.author))

    assert isinstance(result, tuple)
    user_id, post = result
    assert user_id == s.author.id
    assert post.title == 'a title'


def test_the_web_arm_strips_the_title_and_the_api_arm_does_not(db_session, app):
    """`:173`'s `.strip()` against `:168`'s bare read.

    `edit_post` (called from `:231`) independently RE-DERIVES and strips the
    title on BOTH arms (`:254` for SRC_API, `:318` for SRC_WEB) and overwrites
    `post.title` with it at `:395` -- so by the time `make_post` returns
    normally, the value `:168`/`:173` produced has already been clobbered on
    every path. Verified empirically: an unpatched version of this test failed
    with `'spaced' == '  spaced  '` on the API arm, because `:254`'s
    `.strip()` ran downstream of `:168`'s bare read regardless. `edit_post` is
    therefore stubbed to identity here -- the one test in this file that does
    not let it run for real -- so the value `:168`/`:173` actually set on the
    Post at `:206-207` survives to the assertion. The asymmetry between the
    two returned titles is still produced entirely by `:168` vs `:173`.
    """
    s = seed_make_context()

    original_edit_post = post_module.edit_post
    post_module.edit_post = lambda *args, **kwargs: args[1]
    try:
        api_result = make_post(_api_input(title='  spaced  '), s.community,
                                POST_TYPE_ARTICLE, SRC_API, auth=bearer(s.author))
        with web_ctx(app, s.author):
            web_post = make_post(_web_form(title='  spaced  '), s.community,
                                  POST_TYPE_ARTICLE, SRC_WEB)
    finally:
        post_module.edit_post = original_edit_post

    assert api_result[1].title == '  spaced  '
    assert web_post.title == 'spaced'


def test_a_link_post_picks_link_url_not_video_url(db_session, app):
    """`:174`'s true arm and `:175`, witnessed through `:195`'s raise.

    THE ASSERTION IS ON THE RAISE, NOT ON `post.url`, AND THAT IS THE WHOLE
    POINT. `make_post` never puts its local `url` on the Post: `:206-207`
    passes `title` and `language_id` only. `post.url` is set later by
    `edit_post`, which re-derives url from the SAME `input` and `type`
    (`:256`, `:321-327`). So a test asserting `post.url == ...` passes even
    with `:175` deleted -- it witnesses `edit_post`, not `make_post`. (An
    earlier draft of this file did exactly that, and a fix-round caught it.)

    A Domain-row assertion is confounded too, because `edit_post:566` calls
    `domain_from_url` as well. The ONLY effect of `make_post`'s local url that
    nothing downstream can reproduce is `:195`'s raise, which happens before
    `:231` delegates at all.

    Both hosts are banned, so the arm that ran is named in the message:
    `:195` raises `domain.name + ' is blocked by admin'`. Delete `:175` and url
    is None, `:190` is false, and no raise happens -- this test fails. Swap
    `:175` to read `video_url` and the message names the other host -- this
    test fails.
    """
    s = seed_make_context()
    for host in ('linkhost.example', 'videohost.example'):
        d = make_domain(host)
        d.banned = True
    db.session.commit()
    form = _web_form(link_url='https://linkhost.example/page',
                      video_url='https://videohost.example/v.mp4')

    with web_ctx(app, s.author):
        with pytest.raises(Exception, match='linkhost.example is blocked by admin'):
            make_post(form, s.community, POST_TYPE_LINK, SRC_WEB)

    assert db.session.query(Post).count() == 0


def test_a_video_post_picks_video_url_not_link_url(db_session, app):
    """`:176`'s true arm and `:177`, with `:174` taken false.

    The mirror of the test above: same form, same two banned hosts, different
    `type`, and the message names the OTHER host. Together the pair pins which
    field each arm reads -- neither test alone could, because a single banned
    host cannot distinguish "read the right field" from "read any field".
    """
    s = seed_make_context()
    for host in ('linkhost.example', 'videohost.example'):
        d = make_domain(host)
        d.banned = True
    db.session.commit()
    form = _web_form(link_url='https://linkhost.example/page',
                      video_url='https://videohost.example/v.mp4')

    with web_ctx(app, s.author):
        with pytest.raises(Exception, match='videohost.example is blocked by admin'):
            make_post(form, s.community, POST_TYPE_VIDEO, SRC_WEB)

    assert db.session.query(Post).count() == 0


def test_an_article_post_reads_neither_url_field(db_session, app, stub_notify):
    """`:178`'s else arm and `:179`'s `url = None`, with `:174` and `:176`
    both taken false.

    BOTH hosts are banned and the form carries both, so if `:179` were changed
    to read either field this call would raise. It must not: an article takes
    no url, `:190` is false, and the domain check never runs. The positive
    controls are the two tests above -- same fixture, same banned hosts, and
    they DO raise.
    """
    s = seed_make_context()
    for host in ('linkhost.example', 'videohost.example'):
        d = make_domain(host)
        d.banned = True
    db.session.commit()
    form = _web_form(link_url='https://linkhost.example/page',
                      video_url='https://videohost.example/v.mp4')

    with web_ctx(app, s.author):
        post = make_post(form, s.community, POST_TYPE_ARTICLE, SRC_WEB)

    assert post is not None
    assert db.session.query(Post).count() == 1


def test_a_rate_limited_api_user_is_refused(db_session):
    """`:166`'s true arm and `:167`'s raise.

    `extra_rate_limit_check` (`:155-160`) is currently an unconditional
    `return False` -- finding D406 records that its docstring says the real
    limiting is still planned, and that the same stub is duplicated verbatim in
    `app/shared/reply.py:134-139` -- so this arc CANNOT be reached without
    replacing it. The monkeypatch is not a convenience here; it is the only way
    in, and that fact is registered rather than hidden. When the function grows
    real logic, this test keeps working and a fixture-based version would have
    to be rewritten.

    `:166`'s FALSE arm is witnessed by
    `test_the_api_arm_returns_the_user_id_and_the_post`, an ordinary API call
    that reaches `:168` with `extra_rate_limit_check` left unpatched.
    """
    s = seed_make_context()

    original = post_module.extra_rate_limit_check
    post_module.extra_rate_limit_check = lambda user: True
    try:
        with pytest.raises(Exception, match='rate_limited'):
            make_post(_api_input(), s.community, POST_TYPE_ARTICLE, SRC_API,
                      auth=bearer(s.author))
    finally:
        post_module.extra_rate_limit_check = original

    # Proves the raise happened BEFORE :206 created a row -- without this the
    # test would pass even if the raise moved below the creation.
    assert db.session.query(Post).count() == 0
