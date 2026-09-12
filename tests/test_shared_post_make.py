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
