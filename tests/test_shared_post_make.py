"""`app/shared/post.py`'s `make_post` -- Group D of five.

SCOPE. One function, `:163-246` (final-review MINOR 5 corrects this from
`:163-249`, which overshot by three and swallowed a line belonging to
`edit_post`; re-derived via `ast`: `lineno=163`, `end_lineno=246` -- `:247`
and `:248` are blank and `:249` is `edit_post`'s leading comment), 61
statements and 26 branch arcs, and the
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
from datetime import datetime
from types import SimpleNamespace

import pytest

from flask import g

from app import db
from app.constants import (
    POST_TYPE_ARTICLE,
    POST_TYPE_LINK,
    POST_TYPE_VIDEO,
    POST_STATUS_PUBLISHED,
    POST_STATUS_SCHEDULED,
    SRC_API,
    SRC_WEB,
)
from app.models import Community, Domain, Post, PostVote, User, utcnow
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
from app.utils import get_setting, set_setting

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

    `assert result is rows[0]` is load-bearing, not decorative: `result is not
    None` alone is true of a two-tuple too, so a mutation of `:243`
    (`if src == SRC_API:` -> `if src == SRC_WEB:`) that sends this SRC_WEB
    call down `:244`'s `return user.id, post` arm instead of `:246`'s bare
    return survived with only that check -- verified empirically. Identity
    against the queried row additionally pins the docstring's "same object"
    claim, which nothing here checked before.

    `assert rows[0].slug.endswith('/a-title')` closes final-review MAJOR 3:
    `:224`'s `post.generate_ap_id(community)` was, before this assertion,
    both unmutated (absent from Task 6's mutation table) and unasserted
    (`ap_id`/`slug` had zero hits anywhere in this file) -- and it is NOT
    dead code the way D455(a) originally claimed `:173`'s title duplicate
    was. `generate_ap_id` (app/models.py:2556-2568) reads `self.title` and
    calls `slugify` on it at `:224`, which runs BEFORE `:231` delegates to
    `edit_post` -- `edit_post` never regenerates `ap_id` or `slug` (grepped
    `:250-751` for both names, zero hits), so whatever `:224` computes from
    `make_post`'s own title (`:173`'s stripped value, via `:207`) is
    permanent, unlike `post.title` itself, which `edit_post:395` overwrites
    afterward. `_web_form()`'s default title is `'a title'`, `slugify('a
    title')` is `'a-title'`, and `community.post_url_type` is unset (`None`)
    on `make_community`'s row, so `generate_ap_id` takes the "friendly" arm
    (app/models.py:2557) and sets `self.slug` to
    `f'/c/{community.name}@.../p/{post.id}/a-title'`. Deleting `:224` leaves
    `post.slug` at its column default (`None`) and `post.ap_id` at `:207`'s
    10-character `gibberish()` -- verified empirically as a real hole before
    this assertion existed.
    """
    s = seed_make_context()

    with web_ctx(app, s.author):
        result = make_post(_web_form(), s.community, POST_TYPE_ARTICLE, SRC_WEB)

    assert result is not None
    rows = db.session.query(Post).filter_by(community_id=s.community.id).all()
    assert len(rows) == 1
    assert rows[0].user_id == s.author.id
    assert rows[0].title == 'a title'
    assert result is rows[0]
    assert rows[0].slug.endswith('/a-title')


def test_the_api_arm_returns_the_user_id_and_the_post(db_session):
    """`:164`'s true arm and `:244`'s two-tuple.

    NO `web_ctx`: the API arm takes no request context. The two-tuple is what
    distinguishes this arm from `:246`'s bare return, and a test asserting only
    that a post exists would pass on either arm -- `:206-228` runs on both.

    This is also the false-arm witness for `:166`: `extra_rate_limit_check` is
    left unpatched, so it takes its ordinary `return False`, `:166` is taken
    false, and the call proceeds to `:168` and beyond.

    `assert post.slug.endswith('/a-title')` pins `:168` specifically
    (final-review MAJOR 4/3): `post.title` above is NOT a witness for `:168`
    -- `edit_post` is unstubbed here and its own SRC_API branch re-reads the
    SAME `input['title']` and overwrites `post.title` at `:395`, so `:168`
    could read the wrong dict key entirely and `post.title` would still come
    out right via `edit_post`'s independent copy. `post.slug`, by contrast,
    is set once at `:224`, BEFORE `:231` delegates, from whatever `:168`
    handed to `:207` -- `edit_post` never touches it (see the sibling
    assertion's docstring in `test_an_article_post_is_created_through_the_web_arm`
    above for the full `generate_ap_id` mechanism). A mutated `:168` changes
    what `:224` slugifies without changing `post.title`'s final value, so
    this assertion is the one thing in this test that actually depends on
    `:168`'s own read rather than `edit_post`'s.

    A throwaway post is seeded first so `:244`'s returned id cannot coincide
    with `s.author.id` (final-review MAJOR 4): verified empirically as a
    real hole -- mutating `:244` from `return user.id, post` to `return
    post.id, post` survived, because `user` and `post` both reset to
    sequence id 1 per test (tests/conftest.py), and this was the only post
    in the test, so `post.id == s.author.id == 1` by pure fixture
    coincidence (D451 mechanism 2). With a first post already occupying id
    1, this call's `post.id` is 2 while `s.author.id` stays 1, so the two
    can no longer agree by accident.
    """
    s = seed_make_context()

    make_post(_api_input(title='seed post'), s.community, POST_TYPE_ARTICLE,
              SRC_API, auth=bearer(s.author))

    result = make_post(_api_input(), s.community, POST_TYPE_ARTICLE, SRC_API,
                        auth=bearer(s.author))

    assert isinstance(result, tuple)
    user_id, post = result
    assert user_id == s.author.id
    assert user_id != post.id
    assert post.title == 'a title'
    assert post.slug.endswith('/a-title')


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
    `domain_from_url` as well. `:195`'s raise is NOT the only effect of
    `make_post`'s local url that survives downstream, and asserting on the
    raise is therefore not proof of `:174`/`:175` alone: `edit_post`'s own
    copy of this exact check (`:565-569`) reaches the identical exception,
    with the identical message, for this same LINK-typed input if
    `make_post`'s own url selection is broken and `edit_post` runs unstubbed
    (as it does here) -- verified empirically. A live mutation of `:174`
    (swapping its constant so a LINK post's local `url` falls through to
    `:179`'s `url = None`) leaves THIS test green, because `edit_post`
    re-derives the same linkhost url from `type`/`input.link_url` at `:321`
    and reaches `:569`'s raise on its own, one call later. What `make_post`'s
    own check controls -- and what nothing downstream can reproduce -- is
    WHEN the refusal happens: before `:231` ever delegates, so no Post or
    Vote row is created first; that timing is what
    `test_a_banned_domain_is_refused_before_any_row_is_created`'s recorder
    pins, not this test. What THIS test actually witnesses is narrower: WHICH
    field `:174`/`:175` read, via the raised message naming the right host.
    A `:174` mutation is killed by its companion,
    `test_a_video_post_picks_video_url_not_link_url`, not reliably by this
    test alone -- a VIDEO-typed call under that same mutation raises
    immediately with the WRONG host's name, before `edit_post` is ever
    reached, which this test's LINK-typed shape cannot show.

    Both hosts are banned, so the arm that ran is named in the message:
    `:195` raises `domain.name + ' is blocked by admin'`. Delete `:175` (url
    becomes `None`, `:190` is false in `make_post`) and this test does NOT
    fail (final-review MINOR 2, correcting an earlier draft of this
    paragraph that contradicted the finding two paragraphs up): `edit_post`
    reaches the identical raise on its own, for the reason given above. Swap
    `:175` to read `video_url` and the message names the other host -- this
    test fails, because that fault survives into what `edit_post` re-derives
    too.
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

    This test is masked by `edit_post` in exactly the same way its LINK
    companion above is, for the mirror-image fault direction (final-review
    MINOR 6, restoring the symmetry that companion's docstring carries):
    a `:176` mutation that makes a VIDEO post's local `url` fall through to
    `:179`'s `url = None` leaves THIS test green too, because `edit_post`
    re-derives the same videohost url from `type`/`input.video_url` at
    `:323` and reaches `:569`'s raise on its own. That mutation is killed by
    `test_a_video_posts_own_url_is_checked_before_edit_post_runs`'s
    recorder, not by this test.
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

    `'https:///not-a-url'` is such a url: it carries a scheme and `//`, but the
    authority between `//` and the next `/` is empty, so
    `urlparse('https:///not-a-url')` succeeds (no exception) and yields
    `hostname=None`. It used to be the schemeless `'not-a-url'`; D1407 added an
    http(s) check on the API's `url` ABOVE this line, so a schemeless string is
    now refused before the domain block is reached and no longer exercises it.
    The hostless-but-parseable shape is the one that still gets there -- the same
    shape `update_post_from_activity`'s comment names ('https:///x' parses,
    .hostname is None). It is truthy going into `:190`, and `.strip()` at `:191`
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
        result = make_post(_api_input(url='https:///not-a-url'), s.community,
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

    THE ARC'S WITNESS IS THE ABSENCE OF A RAISE, NOT `post.url`
    (final-review MINOR 4, corrected): `post.url` is set by `edit_post`
    (`:565`-ish onward), not by `make_post` -- D456(1)'s point, which three
    other tests in this file were rewritten to stop relying on. The
    assertion below is something `make_post` itself owns (the row exists,
    meaning `:195` did not fire and `:206-228` ran to completion); neither
    `:194` nor `:197` is observed directly by any single field, only by the
    call having returned normally at all.

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

    assert db.session.query(Post).count() == 1
    assert post.up_votes == 1


def test_no_uploaded_file_skips_the_extension_check(db_session):
    """`:197`'s FIRST condition taken false via `uploaded_file=None`,
    straight to `:206` -- arc `197->206`.

    `edit_post` is left to run for real: `uploaded_file=None` makes its own
    identical guard (`:461`, verbatim copy of `:197`) false too, so there is
    nothing downstream for the two copies to disagree about.
    """
    s = seed_make_context()

    user_id, post = make_post(_api_input(), s.community, POST_TYPE_ARTICLE,
                              SRC_API, auth=bearer(s.author))

    assert post.title == 'a title'


def test_an_uploaded_file_with_an_empty_filename_is_ignored(db_session):
    """`:197`'s SECOND condition taken false, with the first TRUE -- the
    other route to arc `197->206`.

    A `FileStorage` with no filename is what a browser sends for an empty
    file input, so this is the ordinary no-upload submission rather than an
    edge case. Distinguishing it from `uploaded_file=None` is the point: both
    reach `:206`, and only this test shows the `.filename != ''` half is
    load-bearing -- a mutant that drops it (leaving bare `if uploaded_file:`)
    IS caught by this test, not merely by the one above (final-review
    MINOR 7 corrects an earlier draft's reasoning here, which talked itself
    out of its own kill): with the guard weakened, this test's
    `SimpleNamespace(filename='')` enters the block, `:202` gives
    `os.path.splitext('')[1] == ''`, `''` is not in `allowed_extensions`, and
    `:204` raises `'filetype not allowed'` -- an unhandled exception this
    test does not expect, so it fails. Task 6's row 12 independently
    confirms the kill by running the mutation.

    `edit_post` runs for real: its own `:461` reads the same empty-filename
    object and takes the same false arm, so nothing downstream disagrees.
    """
    s = seed_make_context()

    user_id, post = make_post(_api_input(), s.community, POST_TYPE_ARTICLE,
                              SRC_API, auth=bearer(s.author),
                              uploaded_file=SimpleNamespace(filename=''))

    assert post.title == 'a title'


def test_a_disallowed_extension_is_refused(db_session):
    """`:203`'s true arm and `:204`'s raise -- arc `203->204`.

    `edit_post` IS STUBBED TO IDENTITY. `edit_post:461-468` reimplements
    `:197-204` VERBATIM, including the exact exception string, so with
    `edit_post` left to run for real a mutation that silences `make_post`'s
    OWN `:203` check (e.g. inverting `not in` to `in`) would not fail this
    test: `make_post`'s check would go quiet, but a Post and Vote would then
    be created and `edit_post` would independently re-run the SAME check on
    the SAME `uploaded_file`/`type`, raise the SAME 'filetype not allowed'
    message, and `:230-236` would catch and re-raise it unchanged -- an
    indistinguishable false pass. Stubbing `edit_post` removes its copy of
    the check, so the only source of a raise here is `make_post`'s own
    `:203`/`:204`.
    """
    s = seed_make_context()

    original_edit_post = post_module.edit_post
    post_module.edit_post = lambda *args, **kwargs: args[1]
    try:
        with pytest.raises(Exception, match='filetype not allowed'):
            make_post(_api_input(), s.community, POST_TYPE_ARTICLE, SRC_API,
                      auth=bearer(s.author),
                      uploaded_file=SimpleNamespace(filename='payload.exe'))
    finally:
        post_module.edit_post = original_edit_post

    assert db.session.query(Post).count() == 0


def test_an_allowed_image_extension_passes(db_session):
    """`:203`'s false arm -- arc `203->206` -- the positive control for the
    test above.

    Same shape, an allowed extension (case-mixed, exercising `:203`'s
    `.lower()` -- final-review MINOR 3 corrects this citation from an
    earlier draft's `:202`, which is `file_ext = os.path.splitext(...)[1]`,
    the line before the one with `.lower()` on it), no raise. `:199`'s list
    is the witness: change it and this test fails where the refusal test
    would not.

    `edit_post` IS STUBBED TO IDENTITY, for a different reason than the raise
    test above: with `uploaded_file` a bare `SimpleNamespace(filename=...)`,
    letting `edit_post` run for real past its own `:461` guard would reach
    `:479`'s `uploaded_file.seek(0)`, which `SimpleNamespace` does not
    support -- an `AttributeError`, not the assertion under test. Stubbing
    sidesteps that entirely; the value asserted below (`post.title`) is set
    by `make_post` itself at `:206-207`, before `edit_post` is ever called.
    """
    s = seed_make_context()

    original_edit_post = post_module.edit_post
    post_module.edit_post = lambda *args, **kwargs: args[1]
    try:
        user_id, post = make_post(_api_input(), s.community, POST_TYPE_ARTICLE,
                                  SRC_API, auth=bearer(s.author),
                                  uploaded_file=SimpleNamespace(filename='pic.PNG'))
    finally:
        post_module.edit_post = original_edit_post

    assert post.title == 'a title'


def test_a_video_upload_is_refused_when_video_uploads_are_off(db_session):
    """`:200`'s SECOND conjunct taken false, with the first TRUE -- arc
    `200->202`.

    `can_upload_video` (app/utils.py:2586-2589) returns False when
    `allow_video_file_uploads` is 'no', which is the default the test suite
    leaves in place, so `:201` never runs and '.mp4' stays out of the
    allowed list. A regression dropping the `can_upload_video()` conjunct
    (leaving bare `if type == POST_TYPE_VIDEO:`) would let '.mp4' through
    for a POST_TYPE_VIDEO post regardless of the setting.

    `edit_post` IS STUBBED TO IDENTITY, load-bearing for the same reason as
    `test_a_disallowed_extension_is_refused`: `edit_post:464` reimplements
    `:200`'s exact compound against its own re-derived `type`, so with
    `edit_post` left to run for real, a mutant dropping `:200`'s second
    conjunct would go quiet in `make_post` but `edit_post`'s untouched copy
    would independently raise the same message one call later -- verified
    empirically as the required verification step below.
    """
    s = seed_make_context()

    original_edit_post = post_module.edit_post
    post_module.edit_post = lambda *args, **kwargs: args[1]
    try:
        with pytest.raises(Exception, match='filetype not allowed'):
            make_post(_api_input(), s.community, POST_TYPE_VIDEO, SRC_API,
                      auth=bearer(s.author),
                      uploaded_file=SimpleNamespace(filename='clip.mp4'))
    finally:
        post_module.edit_post = original_edit_post

    assert db.session.query(Post).count() == 0


def test_a_video_upload_is_accepted_when_video_uploads_are_enabled(db_session):
    """`:200`'s true arm -- arc `200->201` -- both conjuncts TRUE.

    `allow_video_file_uploads` is set to `'yes'` via `set_setting`, the
    mechanism this suite already uses (e.g. tests/test_utils_upload_video.py,
    tests/test_ap_actor_json_group.py:812). `can_upload_video`
    (app/utils.py:2586-2596) reads that setting through `get_setting`, finds
    it neither `'no'`, `'user 1'`, `'admins'` nor `'users'`, and falls through
    to `return True` at `:2596` without ever touching `current_user` -- so
    this needs no `web_ctx` even though the call happens through the SRC_API
    arm with no request context. `:201` then extends the allowed list with
    `.mp4`, so `:203` is false and no exception is raised: this is the
    positive control for the test above, and together the pair separates
    `:200`'s two conjuncts, which coverage.py otherwise scores as a single
    arc pair.

    `edit_post` IS STUBBED TO IDENTITY, for the same `uploaded_file.seek(0)`
    `AttributeError` reason as `test_an_allowed_image_extension_passes` --
    the setting is genuinely 'yes' here, so `edit_post`'s own copy of the
    check (`:464`) would also take the true arm and attempt to actually save
    and process the fake file.

    The setting IS restored to its actual default, not merely to whatever
    `get_setting` happened to return before this test ran (final-review
    MINOR 1, corrected): `get_setting(name, default=None)`
    (app/utils.py:203-212) returns `default` when no `Settings` row exists,
    and no such row exists here -- `make_site()` (tests/factories.py) creates
    only the `Site` row, and `db_session` truncates `Settings` before every
    test -- so an earlier draft's bare `get_setting('allow_video_file_uploads')`
    captured `None`, and the `finally` then did
    `set_setting('allow_video_file_uploads', None)`, which CREATES a row
    holding the string `"null"`. `can_upload_video` (app/utils.py:2586) reads
    `get_setting('allow_video_file_uploads', 'no')` -- with that row present
    its own default is never used, `upload_access` is `None`, none of the
    four `elif` branches match, and it falls through to `return True`: the
    "restore" flipped video uploads from off to permanently ON. `original_setting`
    is therefore captured with the SAME default the call site under test
    uses (`'no'`), so the `finally` restores the real default rather than
    inverting it. This was harmless only by coincidence: nothing runs after
    the `finally` inside this test, and `db_session`'s teardown -- a
    `DELETE FROM` every table in `db.metadata.sorted_tables`
    (tests/conftest.py:126-127) including `Settings`, executed via
    `exec_driver_sql` (tests/conftest.py:191) -- deletes the wrong row before
    the next test could observe it. `cache.delete_memoized(get_setting)` at
    `app/utils.py:222` cannot cause staleness either, for a different reason
    than teardown: the test config sets `CACHE_TYPE = 'NullCache'`
    (tests/conftest.py:68), so `get_setting`'s `@cache.memoize` is inert and
    every call already reads the database directly.
    """
    s = seed_make_context()

    original_setting = get_setting('allow_video_file_uploads', 'no')
    set_setting('allow_video_file_uploads', 'yes')
    original_edit_post = post_module.edit_post
    post_module.edit_post = lambda *args, **kwargs: args[1]
    try:
        user_id, post = make_post(_api_input(), s.community, POST_TYPE_VIDEO,
                                  SRC_API, auth=bearer(s.author),
                                  uploaded_file=SimpleNamespace(filename='clip.mp4'))
    finally:
        post_module.edit_post = original_edit_post
        set_setting('allow_video_file_uploads', original_setting)

    assert post.title == 'a title'
    assert db.session.query(Post).count() == 1


def test_a_video_upload_is_judged_against_the_api_caller(db_session):
    """F12, fixed. `make_post` called `can_upload_video()` with no user, so
    under the 'users' policy the token's owner was judged as the ambient
    `current_user` (anonymous, or absent outside a request) and refused. It
    now passes the real user. `edit_post` is stubbed as in the tests above."""
    s = seed_make_context()

    original_setting = get_setting('allow_video_file_uploads', 'no')
    set_setting('allow_video_file_uploads', 'users')
    original_edit_post = post_module.edit_post
    post_module.edit_post = lambda *args, **kwargs: args[1]
    try:
        user_id, post = make_post(_api_input(), s.community, POST_TYPE_VIDEO,
                                  SRC_API, auth=bearer(s.author),
                                  uploaded_file=SimpleNamespace(filename='clip.mp4'))
    finally:
        post_module.edit_post = original_edit_post
        set_setting('allow_video_file_uploads', original_setting)

    assert db.session.query(Post).count() == 1


def test_a_webm_upload_is_accepted_when_video_uploads_are_enabled(db_session):
    """`:201`'s full extension list, not just `.mp4`.

    Verified empirically as a real hole (final-review MAJOR 4): mutating
    `:201` from `allowed_extensions.extend(['.mp4', '.webm', '.mov'])` to
    `allowed_extensions.extend(['.mp4'])` -- dropping `.webm` and `.mov` --
    survived all 33 other tests, because none of them uploads anything but a
    `.mp4` file. This test is a near-copy of
    `test_a_video_upload_is_accepted_when_video_uploads_are_enabled` above
    with a `.webm` filename instead, closing that gap; `.mov` is left
    unwitnessed since one additional extension already distinguishes
    "the whole list" from "just `.mp4`" and a third near-identical copy would
    add nothing `.webm` doesn't already prove.
    """
    s = seed_make_context()

    original_setting = get_setting('allow_video_file_uploads', 'no')
    set_setting('allow_video_file_uploads', 'yes')
    original_edit_post = post_module.edit_post
    post_module.edit_post = lambda *args, **kwargs: args[1]
    try:
        user_id, post = make_post(_api_input(), s.community, POST_TYPE_VIDEO,
                                  SRC_API, auth=bearer(s.author),
                                  uploaded_file=SimpleNamespace(filename='clip.webm'))
    finally:
        post_module.edit_post = original_edit_post
        set_setting('allow_video_file_uploads', original_setting)

    assert post.title == 'a title'
    assert db.session.query(Post).count() == 1


def test_a_non_video_post_ignores_can_upload_video_even_when_enabled(db_session):
    """`:200`'s FIRST conjunct taken false, with the SECOND conjunct TRUE --
    the short-circuit witness `and` requires but `or` would not.

    `type` is POST_TYPE_ARTICLE, not POST_TYPE_VIDEO, so `:200`'s first
    conjunct is false and Python's `and` never evaluates `can_upload_video()`
    at all -- yet the setting is forced to `'yes'` anyway, so a mutant that
    weakened `:200` to `or` (`if type == POST_TYPE_VIDEO or
    can_upload_video():`) would flip this arm: `.mp4` would join the allowed
    list for an ARTICLE post and the raise below would not happen. Neither
    of the two tests above can catch that particular mutation on its own --
    `test_a_video_upload_is_accepted_when_video_uploads_are_enabled` has
    `type == POST_TYPE_VIDEO` already true, and
    `test_a_video_upload_is_refused_when_video_uploads_are_off` has the
    setting already 'no' -- only holding `type` false and the setting true
    at once separates `and` from `or`.

    `edit_post` IS STUBBED TO IDENTITY for the same reason as the other
    raise tests in this group: `edit_post:464` reimplements the identical
    compound against its own re-derived `type`, so leaving it live would let
    its copy mask a mutation to `make_post`'s own `:200`.
    """
    s = seed_make_context()

    original_setting = get_setting('allow_video_file_uploads', 'no')
    set_setting('allow_video_file_uploads', 'yes')
    original_edit_post = post_module.edit_post
    post_module.edit_post = lambda *args, **kwargs: args[1]
    try:
        with pytest.raises(Exception, match='filetype not allowed'):
            make_post(_api_input(), s.community, POST_TYPE_ARTICLE, SRC_API,
                      auth=bearer(s.author),
                      uploaded_file=SimpleNamespace(filename='clip.mp4'))
    finally:
        post_module.edit_post = original_edit_post
        set_setting('allow_video_file_uploads', original_setting)

    assert db.session.query(Post).count() == 0


# ---------------------------------------------------------------------------
# Group D, Task 5: state mutations (:211-228), the rollback (:233-236, which
# has NO branch arc at all), and the notify guard (:238-239 / :238->241).
# ---------------------------------------------------------------------------


def test_a_bot_authors_post_is_flagged_from_bot(db_session):
    """`:206`'s `from_bot=user.bot or user.bot_override`, unmutated by
    Task 6 and unasserted anywhere in this file (final-review MAJOR 4) --
    verified empirically as a real hole: hardcoding `from_bot=False`
    survived all 34 other tests, because `seed_make_context`'s shared
    author is never a bot and no other test sets `user.bot` or
    `user.bot_override`. `can_create_post` (app/utils.py:2494-2540) has no
    bot-related check, so flipping `bot` does not change whether the post
    is permitted.
    """
    s = seed_make_context()
    s.author.bot = True
    db.session.commit()

    user_id, post = make_post(_api_input(), s.community, POST_TYPE_ARTICLE,
                              SRC_API, auth=bearer(s.author))

    assert post.from_bot is True


def test_creating_a_post_seeds_its_author_upvote(db_session):
    """`:211`'s up_votes, `:213`'s score, `:214`/`:215`'s ranking fields, and
    `:226-227`'s PostVote row.

    A new post starts with its author's own upvote. Catches a regression
    dropping `:226`, which would leave the score claiming a vote that no
    PostVote row backs -- a discrepancy no single-field assertion would show.
    `edit_post` runs for real: it never touches `up_votes`, `score`,
    `ranking`, `ranking_scaled` or `PostVote` (confirmed by grepping
    app/shared/post.py -- the only hits for those names outside this
    function are in delete_post/restore_post/mod_delete_post/
    mod_restore_post, none of which `edit_post` calls), so nothing
    downstream could produce this result in `make_post`'s place.

    `:214`/`:215` were, before these two assertions, unmutated by Task 6 and
    unasserted anywhere in this file (final-review MAJOR 4) -- verified
    empirically as a real hole: replacing either assignment's RHS with a
    hardcoded `0` survived all 34 other tests. `Post.post_ranking` is
    time-dependent (app/models.py:2715-2723, keyed off `post_date`), so the
    expected value cannot be a literal; instead these assertions recompute
    it from the SAME method against the persisted `post.score`/`post.posted_at`
    and `community.scale_by()`, which is what pins `make_post` actually
    STORING the call's result rather than some other value -- `post_ranking`'s
    own correctness is out of scope here.
    """
    s = seed_make_context()

    user_id, post = make_post(_api_input(), s.community, POST_TYPE_ARTICLE,
                              SRC_API, auth=bearer(s.author))

    assert post.up_votes == 1
    assert post.score == 1
    assert post.ranking == post.post_ranking(post.score, post.posted_at)
    assert post.ranking_scaled == int(post.ranking + s.community.scale_by())
    votes = db.session.query(PostVote).filter_by(post_id=post.id).all()
    assert len(votes) == 1
    assert votes[0].user_id == s.author.id
    assert votes[0].effect == 1


def test_creating_a_post_increments_both_counters(db_session):
    """`:218`'s community.post_count and `:220`'s user.post_count, plus
    `:219`'s double-assignment timestamp and `:221`/`:222`'s author
    activity fields.

    Both counters start at a known non-zero value so a regression that
    ASSIGNS rather than increments fails here. Starting from zero would let
    `= 1` pass. `edit_post` never touches `post_count` on the community or
    the author it is passed here (see the grep summary in the test above);
    the `post_count` writes it does contain live only in delete_post/
    restore_post and friends, which `make_post` does not call.

    `:219`/`:221`/`:222` were, before these assertions, unmutated by Task 6
    and unasserted anywhere in this file (final-review MAJOR 4) -- verified
    empirically as real holes: dropping `:219`'s `g.site.last_active` target
    (keeping only `community.last_active`), or deleting `:221` or `:222`
    outright, each survived all 34 other tests. `before`/`after` bound the
    call rather than asserting an exact timestamp, since `utcnow()` is
    called inside `make_post` and cannot be predicted from the test. `:219`
    is a double assignment (`community.last_active = g.site.last_active =
    utcnow()`) and both targets are checked separately so dropping either
    one is caught; `g.site` is the SAME object `seed_make_context` stored as
    `s.site` (both are the literal Python object `g.site` was assigned, not
    a separate row), so no `db.session.refresh` is needed to see the write.
    `:222`'s `ip_address()` (`app/utils.py:2308`, `app/__init__.py:43-78`)
    catches `RuntimeError` for "no request context" and returns `''` -- this
    call has none, being SRC_API -- so `''` is the SPECIFIC value only
    `:222` actually running can produce; `make_user` never sets
    `ip_address` (tests/factories.py), so its column default (`None`) is
    what a dropped `:222` would leave instead, and `''` vs `None` are
    cleanly distinguishable.
    """
    s = seed_make_context()
    s.community.post_count = 5
    s.author.post_count = 7
    db.session.commit()

    before = utcnow()
    make_post(_api_input(), s.community, POST_TYPE_ARTICLE, SRC_API,
              auth=bearer(s.author))
    after = utcnow()

    db.session.refresh(s.community)
    db.session.refresh(s.author)
    assert s.community.post_count == 6
    assert s.author.post_count == 8
    assert before <= s.community.last_active <= after
    assert before <= s.site.last_active <= after
    assert before <= s.author.last_seen <= after
    assert s.author.ip_address == ''


def test_a_failing_edit_post_rolls_back_the_post_and_the_vote(db_session):
    """`:233`, `:234`, `:235` and `:236` -- the rollback, which has NO branch
    arc (final-review MINOR 8 corrects the heading, which named three).

    coverage.py does not model `except` handlers as branches, so these four
    statements are invisible to the arc count: full arc coverage of this
    function says nothing about whether the rollback works. That is why this
    test exists rather than riding along with the delegation tests.

    `edit_post` is monkeypatched to raise because nothing else reaches `:232`
    deterministically -- the whole point of the try is that `edit_post`
    normally succeeds. The patch restores in a `finally`, since
    `post_module.edit_post` is the name `make_post` resolves at call time and
    a leaked patch would break every test after this one.
    """
    s = seed_make_context()

    original = post_module.edit_post

    def exploding_edit_post(*args, **kwargs):
        raise Exception('edit blew up')

    post_module.edit_post = exploding_edit_post
    try:
        with pytest.raises(Exception, match='edit blew up'):
            make_post(_api_input(), s.community, POST_TYPE_ARTICLE, SRC_API,
                      auth=bearer(s.author))
    finally:
        post_module.edit_post = original

    assert db.session.query(Post).count() == 0
    assert db.session.query(PostVote).count() == 0


def test_a_failing_edit_post_restores_the_post_counts(db_session):
    """D463, fixed. The rollback deleted the post and the vote but left
    `community.post_count` and `user.post_count` incremented, so every failed
    upload (D473 counts four ways) inflated both by one for a post that does
    not exist. The handler now puts them back."""
    s = seed_make_context()
    community_before = s.community.post_count
    author_before = s.author.post_count

    original = post_module.edit_post

    def exploding_edit_post(*args, **kwargs):
        raise Exception('edit blew up')

    post_module.edit_post = exploding_edit_post
    try:
        with pytest.raises(Exception, match='edit blew up'):
            make_post(_api_input(), s.community, POST_TYPE_ARTICLE, SRC_API,
                      auth=bearer(s.author))
    finally:
        post_module.edit_post = original

    db.session.expire_all()
    assert db.session.get(Community, s.community.id).post_count == community_before
    assert db.session.get(User, s.author.id).post_count == author_before


def test_a_failing_edit_post_re_raises_the_original_exception(db_session):
    """`:236`'s `raise e`, distinct from the deletions above it.

    The rollback must not swallow the cause. Catches a regression replacing
    `raise e` with a bare `return` or a generic error -- both of which would
    leave the two count assertions above passing while the caller lost the
    reason.
    """
    s = seed_make_context()

    original = post_module.edit_post

    def exploding_edit_post(*args, **kwargs):
        raise ValueError('the specific cause')

    post_module.edit_post = exploding_edit_post
    try:
        with pytest.raises(ValueError, match='the specific cause'):
            make_post(_api_input(), s.community, POST_TYPE_ARTICLE, SRC_API,
                      auth=bearer(s.author))
    finally:
        post_module.edit_post = original


def test_a_published_post_notifies(db_session):
    """`:238`'s true arm and `:239`.

    Status defaults to published, so this is the ordinary path. Records the
    call rather than asserting on its effects, because `notify_about_post`
    dispatches a Celery task whose body is out of scope here.

    Records the OBJECT passed, and asserts identity (`calls == [post]`,
    i.e. `calls[0] is post`), not `calls == [post.id]` as an earlier draft
    did (final-review MAJOR 4): verified empirically as a real hole --
    mutating `:239` from `notify_about_post(post)` to
    `notify_about_post(user)` survived, because `s.author` is the first row
    `seed_make_context` inserts into `user` and this call's `post` is the
    first row inserted into `post` in this test, and `db_session` resets
    both tables' sequences to 1 (tests/conftest.py) -- `user.id == post.id
    == 1` by fixture coincidence (D451 mechanism 2), so recording `.id`
    could not tell the two objects apart. Recording the object itself and
    comparing identity cannot coincide this way regardless of which table's
    sequence produced which integer.
    """
    calls = []
    s = seed_make_context()

    original = post_module.notify_about_post
    post_module.notify_about_post = lambda post: calls.append(post)
    try:
        user_id, post = make_post(_api_input(), s.community, POST_TYPE_ARTICLE,
                                  SRC_API, auth=bearer(s.author))
    finally:
        post_module.notify_about_post = original

    assert calls == [post]


def test_an_unpublished_post_does_not_notify(db_session, app, stub_notify):
    """`:238`'s false arm.

    `edit_post`'s SRC_API branch hardcodes `scheduled_for = None` at `:281`
    regardless of what `input` carries, so no API call can ever reach this
    arm -- there is no key `_api_input` could add that `edit_post` would even
    look at. The mechanism has to be SRC_WEB: `:337` reads
    `input.scheduled_for.data`, and `:413-416` sets
    `post.status = POST_STATUS_SCHEDULED` once that (timezone-aware) date is
    still in the future relative to `utcnow`. `_web_form`'s `scheduled_for`
    default is `None`; overriding it with a far-future datetime and leaving
    `timezone` at its default `'UTC'` reaches `:416` and leaves
    `post.status != POST_STATUS_PUBLISHED` by the time `make_post:238` runs.

    Its positive control is `test_a_published_post_notifies` above: same
    recorder shape (here, the `stub_notify` fixture), one call there and none
    here -- an empty `calls` list on its own is indistinguishable from a
    broken monkeypatch; the contrast with the positive control is what makes
    it mean something.
    """
    s = seed_make_context()
    form = _web_form(scheduled_for=datetime(2030, 1, 1, 9, 0))

    with web_ctx(app, s.author):
        post = make_post(form, s.community, POST_TYPE_ARTICLE, SRC_WEB)

    assert post.status == POST_STATUS_SCHEDULED
    assert stub_notify == []


def test_a_banned_domain_is_refused_before_any_row_is_created(db_session, app):
    """`:195` fires before `:231`, which is the only thing `make_post`'s
    domain check buys.

    `edit_post:565-569` reimplements `:190-195` verbatim -- same compound, same
    exception string -- and `:231` passes `from_scratch=True`, so that copy
    always runs. Refusing a banned domain is therefore NOT what `make_post`'s
    check is for; refusing it CHEAPLY is. `edit_post` would raise the same
    message at `:569`, after `:209` and `:228` committed a Post and a PostVote
    that `:233-235` then has to delete.

    Asserting `Post.count() == 0` cannot tell those apart -- the rollback
    produces it too. Recording whether `edit_post` was ENTERED can.

    `calls == []` is an emptiness assertion, and an empty recorder is what a
    correct early refusal produces, but it is ALSO what a recorder that never
    installed, or a patch that leaked from an earlier test, would produce
    (D451 mechanism 3) -- on its own it proves nothing. Its positive control
    is `test_an_unbanned_domain_lets_edit_post_run` below: identical fixture
    shape, an unbanned domain, and `calls == [True]` -- so the empty case here
    is contrasted against a case where the same recorder is known to work.
    """
    calls = []
    s = seed_make_context()
    d = make_domain('banned.example')
    d.banned = True
    db.session.commit()

    original = post_module.edit_post

    def recorder(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    post_module.edit_post = recorder
    try:
        with pytest.raises(Exception, match='banned.example is blocked by admin'):
            make_post(_api_input(url='https://banned.example/x'), s.community,
                      POST_TYPE_LINK, SRC_API, auth=bearer(s.author))
    finally:
        post_module.edit_post = original

    assert calls == []
    assert db.session.query(Post).count() == 0


def test_an_unbanned_domain_lets_edit_post_run(db_session, http_mock):
    """Positive control for `test_a_banned_domain_is_refused_before_any_row_is_created`.

    That test's `calls == []` is an emptiness assertion, and an empty list is
    also what a recorder that never installed, or a patch that leaked from
    an earlier test, would produce (D451 mechanism 3) -- nothing about an
    empty `calls` on its own proves `:195` refused early. This test uses the
    IDENTICAL recorder shape against an unbanned domain: `:195` does not fire,
    `:231` is reached, and the recorder records exactly one call on its way to
    delegating to the real `edit_post`. `calls == [True]` here is the contrast
    that makes `calls == []` there mean something.

    Needs `http_mock`, same reason as `test_an_ordinary_domain_is_allowed`
    above: nothing raises before `:231` delegates, so `edit_post` reaches a
    real url, `is_image_url` fires one HEAD, and (the response not being an
    image) `opengraph_parse` fires one GET.
    """
    calls = []
    s = seed_make_context()
    http_mock.head('https://ok-unbanned.example/x').respond(200, headers={'Content-Type': 'text/html'})
    http_mock.get('https://ok-unbanned.example/x').respond(200, html='<html></html>')

    original = post_module.edit_post

    def recorder(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    post_module.edit_post = recorder
    try:
        user_id, post = make_post(_api_input(url='https://ok-unbanned.example/x'),
                                  s.community, POST_TYPE_LINK, SRC_API,
                                  auth=bearer(s.author))
    finally:
        post_module.edit_post = original

    assert calls == [True]
    assert db.session.query(Post).count() == 1


def test_an_api_call_is_authorised_against_its_own_bearer_token(db_session):
    """`:165`'s `authorise_api_user` call actually determines WHICH user
    posts, not merely that `can_create_post` (`:187`) refuses someone.

    Security-relevant mutation, verified empirically: replacing `:165` with
    `user = User.query.order_by(User.id).first()` -- skipping authorisation
    entirely but still yielding a real user, no crash -- passed all 29 other
    tests in this file. Every one of them passes `bearer(s.author)`, and
    `s.author` is also the FIRST (and in most of them the only) user
    `seed_make_context` creates, so a mutant that ignores the bearer token
    and substitutes "the first user in the table" is indistinguishable from
    correct code against every existing fixture -- exactly the shape of hole
    sub-project 36 found in `restore_post`.

    `intruder` is created AFTER `s.author`, so `.order_by(User.id).first()`
    still returns the author under the mutation, and is left UNKEYED
    (`with_keys` defaults to False) rather than banned or unverified:
    `authorise_api_user` (app/utils.py:3628-3629) itself rejects a banned or
    unverified user's token with 'incorrect_login' before `make_post` is ever
    reached -- the same confound `test_an_unverified_user_cannot_make_a_post`
    documents above -- so this test would fail against CORRECT code too if it
    used either. A missing `private_key` is refused only by `can_create_post`
    (app/utils.py:2505-2506, the same check `seed_make_context`'s own
    docstring names as the reason the shared author is minted WITH keys),
    which the bearer-token layer never inspects. So under real
    `authorise_api_user`, the token identifies `intruder`, and `:187` refuses
    with the assertion below; under the mutation, `intruder`'s token is
    ignored, the keyed (permitted) author is substituted instead, and the
    post is created -- this `pytest.raises` would then fail to raise at all,
    which is the mutation's actual failure mode, not merely "no post was
    created" (a broken fixture, e.g. a typo'd fixture name, produces that
    too, which is why the message is asserted specifically rather than only
    the row count).
    """
    s = seed_make_context()
    intruder = make_user(s.instance, 'intruder', local=True)
    db.session.commit()

    with pytest.raises(Exception, match='not permitted'):
        make_post(_api_input(), s.community, POST_TYPE_ARTICLE, SRC_API,
                  auth=bearer(intruder))

    assert db.session.query(Post).count() == 0


def test_a_video_posts_own_url_is_checked_before_edit_post_runs(db_session, app):
    """`:176`'s video-url assignment, isolated from `edit_post`'s identical
    re-derivation (`:321-327`/`:565-569`) by the same recorder technique as
    `test_a_banned_domain_is_refused_before_any_row_is_created`.

    Verified empirically as a real hole: swapping `:176`'s constant to
    `elif type == POST_TYPE_LINK:` (so the VIDEO arm falls through to `:179`'s
    `url = None`) passed all 29 other tests. `make_post`'s own local `url` at
    `:176` never reaches the Post -- `edit_post` re-derives it independently
    at `:323` and re-runs the identical domain check at `:565-569` -- so with
    `edit_post` left to run for real (as it is in every other web-arm url
    test), a `None` url from a broken `:176` merely means `edit_post` raises
    the SAME message one call later, an indistinguishable false pass by
    message alone. `link_url` is set to an UNBANNED host and `video_url` to a
    BANNED one, so a `:176` that (correctly or via this fault) reads the wrong
    field is also distinguishable by WHICH host's name appears -- but the
    recorder is what proves WHEN the raise happened: `calls == []` only if
    `make_post`'s own `:176`/`:190`/`:194`/`:195` refused before `:231` ever
    delegated, which is what the mutation defeats even though the final
    exception message it produces is identical.
    """
    calls = []
    s = seed_make_context()
    for host, banned in (('okvideohost.example', False), ('badvideohost.example', True)):
        d = make_domain(host)
        d.banned = banned
    db.session.commit()
    form = _web_form(link_url='https://okvideohost.example/page',
                      video_url='https://badvideohost.example/v.mp4')

    original = post_module.edit_post

    def recorder(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    post_module.edit_post = recorder
    try:
        with web_ctx(app, s.author):
            with pytest.raises(Exception, match='badvideohost.example is blocked by admin'):
                make_post(form, s.community, POST_TYPE_VIDEO, SRC_WEB)
    finally:
        post_module.edit_post = original

    assert calls == []
    assert db.session.query(Post).count() == 0


def test_an_unbanned_video_url_lets_edit_post_run(db_session, app, http_mock):
    """Positive control for
    `test_a_video_posts_own_url_is_checked_before_edit_post_runs`.

    That test's `calls == []` is an emptiness assertion, and an empty list is
    also what a recorder that never installed would produce (D451 mechanism
    3) -- nothing about an empty `calls` on its own proves `:176`/`:195`
    refused early. This test uses the IDENTICAL recorder shape against an
    unbanned video host: `:195` does not fire, `:231` is reached, and the
    recorder records exactly one call on its way to delegating to the real
    `edit_post`. `calls == [True]` here is the contrast that makes
    `calls == []` there mean something.

    Needs `http_mock`: nothing raises before `:231` delegates, so `edit_post`
    reaches a real url and `is_image_url` fires one HEAD via
    `mime_type_using_head` (app/utils.py:270); the response is not an image
    and the host is neither pixelfed nor loops.video, so `opengraph_parse`
    fires one GET (`:642`).

    NO `post.url` ASSERTION (final-review MINOR 4, removed): an earlier
    draft asserted `post.url == 'https://ok-video.example/v.mp4'` here, but
    `post.url` is set by `edit_post`, not by `make_post` -- the same
    confound D456(1) already removed from three other tests. `calls ==
    [True]` and the row count above are both things `make_post` itself
    owns; `post`'s url is left to `edit_post`'s own test file to verify.
    """
    calls = []
    s = seed_make_context()
    http_mock.head('https://ok-video.example/v.mp4').respond(200, headers={'Content-Type': 'video/mp4'})
    http_mock.get('https://ok-video.example/v.mp4').respond(200, html='<html></html>')
    form = _web_form(link_url='https://unused.example/page',
                      video_url='https://ok-video.example/v.mp4')

    original = post_module.edit_post

    def recorder(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    post_module.edit_post = recorder
    try:
        with web_ctx(app, s.author):
            post = make_post(form, s.community, POST_TYPE_VIDEO, SRC_WEB)
    finally:
        post_module.edit_post = original

    assert calls == [True]
    assert db.session.query(Post).count() == 1


def test_a_link_posts_own_url_is_checked_before_edit_post_runs(db_session, app):
    """`:174`/`:175`'s link-url assignment, isolated from `edit_post`'s
    identical re-derivation (`:320-321`/`:565-569`) by the same recorder
    technique as `test_a_video_posts_own_url_is_checked_before_edit_post_runs`
    above -- the LINK arm's mirror of that hole (final-review MAJOR 2).

    Verified empirically as a real hole, exactly like the VIDEO arm's: EITHER
    mutating `:174` to `if type == POST_TYPE_IMAGE:` (a third constant, not
    `POST_TYPE_VIDEO` -- swapping to VIDEO is Task 6's row 3, which a
    different test already kills for an unrelated reason) OR mutating `:175`
    to `url = None` makes a LINK-typed post's local `url` fall through
    `:174`/`:176` to `:179`'s `url = None`, and BOTH survived all 32 tests
    before this one was added. `make_post` then skips `:190-195` entirely;
    `edit_post` runs unstubbed here (as in every other web-arm url test),
    re-derives `url` from `input.link_url` at `:320-321`, and its own domain
    check at `:565-569` raises the identical message one call later -- an
    indistinguishable false pass by message alone, the same mechanism `:176`'s
    hole used. The recorder distinguishes them exactly as the video test
    does: `calls == []` only if `make_post`'s own
    `:174`/`:175`/`:190`/`:194`/`:195` refused before `:231` ever delegated.

    `link_url` is set to a BANNED host and `video_url` to an UNBANNED one, the
    mirror image of the video test's hosts, so a `:174`/`:176` mix-up would
    also be visible by which host's name is raised.
    """
    calls = []
    s = seed_make_context()
    for host, banned in (('badlinkhost.example', True), ('oklinkvideohost.example', False)):
        d = make_domain(host)
        d.banned = banned
    db.session.commit()
    form = _web_form(link_url='https://badlinkhost.example/page',
                      video_url='https://oklinkvideohost.example/v.mp4')

    original = post_module.edit_post

    def recorder(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    post_module.edit_post = recorder
    try:
        with web_ctx(app, s.author):
            with pytest.raises(Exception, match='badlinkhost.example is blocked by admin'):
                make_post(form, s.community, POST_TYPE_LINK, SRC_WEB)
    finally:
        post_module.edit_post = original

    assert calls == []
    assert db.session.query(Post).count() == 0
