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


def test_an_unverified_user_cannot_make_a_post(db_session, app):
    """`:187`'s FIRST disjunct and `:188`'s raise.

    SRC_WEB, not SRC_API. `authorise_api_user` (app/utils.py:3628-3629)
    itself raises `'incorrect_login'` for `user.verified is False` before
    `make_post` is ever reached -- verified empirically: the API-arm version
    of this test failed with `'incorrect_login'` rather than `'not
    permitted'`, so the bearer-token layer refused first and `:187` was never
    exercised. `current_user` (`:172`) has no such gate: `login_user`
    (`tests/factories.py:1224`) does not consult `verified`, since `User`
    inherits flask-login's default `is_active = True`. The SRC_WEB arm is
    therefore the only way to reach `:187` with an unverified user and have
    the refusal come from `can_create_post` (app/utils.py:2504-2506) -- the
    check under test -- rather than from the token layer.

    Positive control: `test_an_article_post_is_created_through_the_web_arm`,
    same web arm and a verified author, no raise.
    """
    s = seed_make_context()
    s.author.verified = False
    db.session.commit()

    with web_ctx(app, s.author):
        with pytest.raises(Exception, match='not permitted'):
            make_post(_web_form(), s.community, POST_TYPE_ARTICLE, SRC_WEB)

    assert db.session.query(Post).count() == 0


def test_an_ip_banned_user_cannot_make_a_post(db_session):
    """`:187`'s SECOND disjunct, with the first FALSE.

    The author is fully permitted -- keyed, verified, unbanned -- so
    `can_create_post` returns True and only `user_ip_banned()` refuses. That
    separation is the whole test: a fixture failing both disjuncts would pass
    while witnessing only the first.

    `user_ip_banned` is a module-level import at `app/shared/post.py:28`
    (`from app.utils import ... user_ip_banned, ...`), so `make_post` reads
    the name out of `post_module`'s own namespace -- patching `app.utils`
    directly would leave `post_module.user_ip_banned` pointing at the
    original function. `post_module.user_ip_banned` is therefore the binding
    site patched here, per tests/README.md's convention for this campaign.

    Positive control: `test_the_api_arm_returns_the_user_id_and_the_post`,
    same author, `user_ip_banned` unpatched, no raise.
    """
    s = seed_make_context()

    original = post_module.user_ip_banned
    post_module.user_ip_banned = lambda: True
    try:
        with pytest.raises(Exception, match='not permitted'):
            make_post(_api_input(), s.community, POST_TYPE_ARTICLE, SRC_API,
                      auth=bearer(s.author))
    finally:
        post_module.user_ip_banned = original

    assert db.session.query(Post).count() == 0


def test_a_post_with_no_url_skips_the_domain_check(db_session):
    """`:190`'s false arm, straight to `:197`.

    `_api_input` defaults `url` to None, so this is the ordinary article path.
    Asserts no Domain row was created, which is what distinguishes skipping
    the block from running it against a url that happens to be clean.
    """
    s = seed_make_context()
    before = db.session.query(Domain).count()

    make_post(_api_input(), s.community, POST_TYPE_ARTICLE, SRC_API,
              auth=bearer(s.author))

    assert db.session.query(Domain).count() == before


def test_a_hostless_url_skips_the_domain_check(db_session):
    """`:193`'s false arm: `url` truthy and surviving `:191`'s `.strip()`,
    but `domain_from_url` returns None.

    `domain_from_url` (app/utils.py:1561-1596) returns None in three
    situations: a falsy url (`:1562-1568`, excluded here because `:190`
    already requires url to be truthy before this function is even called), a
    `ValueError` escaping `urlparse` (`:1569-1582`, e.g. an unbalanced IPv6
    bracket), and -- the case this test exercises -- a url that `urlparse`
    parses WITHOUT error but whose `.hostname` comes back None, which takes
    the `else: return None` arm at `:1595-1596`.

    `'not-a-url'` is such a url: it has no `://` and no netloc, so
    `urlparse('not-a-url'.lower())` succeeds (no exception) and yields
    `hostname=None`. It is truthy going into `:190`, and `.strip()` at `:191`
    leaves it unchanged (no surrounding whitespace to strip), so it reaches
    `:192`'s `domain_from_url` call exactly as written -- domain comes back
    None, `:193` is false, and control falls straight to `:197` with no raise.

    `edit_post` IS STUBBED TO IDENTITY HERE, following
    `test_the_web_arm_strips_the_title_and_the_api_arm_does_not`'s precedent
    above. `edit_post` re-derives its OWN url from the same `input` (`:256`)
    and re-runs its OWN `domain_from_url` call (`:566`) against it, and for a
    truthy, non-empty url it goes on to call `is_image_url`/`opengraph_parse`
    (call sites app/shared/post.py:601, :642 -- the functions are DEFINED at
    app/utils.py:247 and :2998), which reach real httpx machinery. Verified
    empirically, and worth recording because it very nearly produced a
    silently-broken test: `httpx.Client.build_request` MERGES a url containing
    no `//authority` against its (empty) `base_url`, stripping even an
    explicit scheme prefix like `'https:nohost'` down to a bare relative path
    before the request ever leaves the client -- so a route registered for
    the url as written never matches the request respx actually sees. And a
    fully schemeless string such as `'not-a-url'`, once respx (installed by
    the session-scoped `block_outbound_http` fixture, tests/conftest.py:263)
    returns a mocked response for it, crashes with a `ValueError` deep in
    httpx's OWN cookie-jar bookkeeping (`urllib.request.Request._splittype`
    demands a `scheme:rest` prefix) -- a failure in httpx's plumbing, not in
    `:193`. Stubbing `edit_post` sidesteps all of it: this test's target line
    is `make_post:193`, not `edit_post`'s independent re-derivation of the
    same url.

    Asserts no Domain row was created, which is what proves `domain_from_url`
    took its hostless `else` arm (`:1595-1596`) rather than the branch that
    constructs one (`app/utils.py:1591`, reached only when
    `parsed_url.hostname` is truthy) -- the same observable
    `test_a_post_with_no_url_skips_the_domain_check` uses for `:190`'s false
    arm, but reached here with `:190` TRUE and `:193` FALSE instead of `:190`
    FALSE.
    """
    s = seed_make_context()
    before = db.session.query(Domain).count()

    original_edit_post = post_module.edit_post
    post_module.edit_post = lambda *args, **kwargs: args[1]
    try:
        result = make_post(_api_input(url='not-a-url'), s.community,
                            POST_TYPE_LINK, SRC_API, auth=bearer(s.author))
    finally:
        post_module.edit_post = original_edit_post

    assert result is not None
    assert db.session.query(Domain).count() == before


def test_a_banned_domain_is_refused(db_session):
    """`:194`'s FIRST disjunct and `:195`'s raise.

    `domain_from_url` (app/utils.py:1590-1593) creates the Domain when absent,
    so the row is seeded banned FIRST -- otherwise the call would create a
    fresh unbanned one and the guard would never fire.

    THE ASSERTION IS ON THE RAISE, NOT ON `post.url` OR A `Domain` ROW: both
    are confounded, per `test_a_link_post_picks_link_url_not_video_url`'s
    docstring above. `make_post` never puts its local `url` on the Post
    (`:206-207` passes `title` and `language_id` only), and `edit_post:566`
    calls `domain_from_url` again on its own re-derived url, so a Domain row
    existing proves nothing about `:192` specifically.

    `edit_post` IS STUBBED TO IDENTITY, and this is load-bearing for the
    raise assertion too, not just for the Domain-row point above --
    discovered by running the mutation this test is meant to catch.
    `edit_post:568` reimplements `:194`'s EXACT compound
    (`if domain.banned or domain.name.endswith('.pages.dev'):`) against its
    own re-derived url, and raises the SAME message
    (`domain.name + ' is blocked by admin'`, `edit_post:569`). `make_post`'s
    `:230-236` catches ANY exception `edit_post` raises, deletes the vote and
    the post, and re-raises it unchanged -- so with `edit_post` left to run
    for real, deleting `:194`'s first disjunct in `make_post` (leaving bare
    `if domain.banned:`) does not fail this test: `make_post`'s own check goes
    quiet, but `edit_post`'s untouched copy of the SAME check fires one call
    later and produces an indistinguishable raise. `Post` count stays 0
    either way too, because the except block at `:233-236` deletes the row
    even when `edit_post` is the one that raised. Stubbing `edit_post` to
    identity removes its copy of the check entirely, so the only way this
    test can still raise is `make_post`'s own `:194`/`:195`.
    """
    s = seed_make_context()
    domain = make_domain('banned.example')
    domain.banned = True
    db.session.commit()

    original_edit_post = post_module.edit_post
    post_module.edit_post = lambda *args, **kwargs: args[1]
    try:
        with pytest.raises(Exception, match='blocked by admin'):
            make_post(_api_input(url='https://banned.example/thing'), s.community,
                      POST_TYPE_LINK, SRC_API, auth=bearer(s.author))
    finally:
        post_module.edit_post = original_edit_post

    assert db.session.query(Post).count() == 0


def test_a_pages_dev_domain_is_refused_even_when_not_banned(db_session):
    """`:194`'s SECOND disjunct, with the first FALSE.

    The domain is created by `domain_from_url` and left UNBANNED, so
    `domain.banned` is False and only the `.pages.dev` suffix test refuses.
    This is the only test that distinguishes the two operands of `:194`, which
    coverage.py scores as a single arc pair: a fixture that made both
    disjuncts true at once would pass under a mutant that deleted this one.

    `edit_post` IS STUBBED TO IDENTITY -- required, not optional, and found
    the hard way: `edit_post:568` reimplements `:194`'s exact compound against
    its own re-derived url and raises the identical message, so without this
    stub a mutant deleting `:194`'s SECOND disjunct (leaving bare
    `if domain.banned:`) sails through. Confirmed empirically: with the stub
    absent, dropping `:194`'s `.pages.dev` operand left every test in this
    file green, because `make_post`'s own check went quiet while
    `edit_post`'s untouched copy fired one call later at `:231` and produced
    the same 'blocked by admin' message, caught and re-raised unchanged by
    `make_post`'s `:230-236`. See `test_a_banned_domain_is_refused`'s
    docstring above for the full mechanism. With the stub in place, the only
    source of a raise is `make_post`'s own `:194`/`:195`.
    """
    s = seed_make_context()

    original_edit_post = post_module.edit_post
    post_module.edit_post = lambda *args, **kwargs: args[1]
    try:
        with pytest.raises(Exception, match='blocked by admin'):
            make_post(_api_input(url='https://someone.pages.dev/thing'),
                      s.community, POST_TYPE_LINK, SRC_API, auth=bearer(s.author))
    finally:
        post_module.edit_post = original_edit_post

    assert db.session.query(Post).count() == 0


def test_an_ordinary_domain_is_allowed(db_session, http_mock):
    """`:194`'s false arm -- BOTH disjuncts false -- and `:197`.

    The positive control for the two refusal tests above: same shape, clean
    domain, no raise. Without it, a `pytest.raises` that passed because the
    call raised for some unrelated reason would look identical.

    Needs `http_mock`: unlike the refusal tests, nothing raises before `:231`
    delegates to `edit_post`, and `edit_post` reaches a real url this time
    (`'https://clean.example/thing'` has a resolvable host, unlike
    `test_a_hostless_url_skips_the_domain_check`'s input), so `is_image_url`
    fires one HEAD and, since the response is not an image, `opengraph_parse`
    fires one GET -- both registered here, matching
    `test_web_branch_takes_the_link_url_for_a_link_post`
    (tests/test_shared_post_edit.py) for the same shape.
    """
    s = seed_make_context()
    http_mock.head('https://clean.example/thing').respond(200, headers={'Content-Type': 'text/html'})
    http_mock.get('https://clean.example/thing').respond(200, html='<html></html>')

    user_id, post = make_post(_api_input(url='https://clean.example/thing'),
                              s.community, POST_TYPE_LINK, SRC_API,
                              auth=bearer(s.author))

    assert post.url == 'https://clean.example/thing'
