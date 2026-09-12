# Coverage sub-project 37 Implementation Plan: `post.py` Group D — `make_post`

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take `make_post` (`app/shared/post.py:163-249`) to zero missing statements and zero missing branch arcs — 61 statements and 26 arcs, currently 0.0%.

**Architecture:** A new `tests/test_shared_post_make.py`, reusing the mature input helpers already in `tests/test_shared_post_edit.py` rather than duplicating them. `make_post` is a thin orchestrator — a source fork, three raise-guards, a block of unconditional state mutations, a delegation to `edit_post` with rollback, and a return fork — so the file follows that structure. `edit_post` really runs in almost every test; it is monkeypatched only where the rollback must be reached.

**Tech Stack:** pytest, Flask, Flask-Login, SQLAlchemy 2.0.52, Celery (eager), real Redis in the compose stack, coverage.py with branch measurement. Everything runs through `./run_tests.sh`.

**Spec:** `docs/superpowers/specs/2026-09-12-coverage-post-d-37-design.md`

## Global Constraints

- **Delete nothing this plan did not create.** `git checkout -- app/` is permitted ONLY as a mutation-restore step.
- **There is NO host Python with flask or pytest.** Everything runs through `./run_tests.sh` = `podman compose exec -T test-runner pytest "$@"` (`run_tests.sh:85`).
- **To run python inside the container**: `podman-compose -f compose.test.yaml exec -T test-runner python ...` (`tests/README.md:403-405`). **`run_tests.sh` has no `--exec` flag** and rejects one with pytest exit 4.
- **Only the controller runs the full suite.** Tasks run `tests/test_shared_post_make.py` alone unless told otherwise.
- **`pytest` exits 1 on a session timeout and `run_tests.sh` propagates it.** A shell PIPELINE eats the status — read `${PIPESTATUS[0]}`, or do not pipe.
- **Coverage takes the dotted form** `--cov=app.shared.post`. A path form collects nothing, writes no JSON, exits 0.
- **Write coverage JSON outside the repository.** `/app` is bind-mounted.
- **Read `summary.percent_covered`**, not `percent_statements_covered`.
- Before believing any failure, run `./run_tests.sh --down` and retry once.
- **Test counts come from pytest's own collection output**, never from a number in this plan.
- **No ordered assertions over rows a query planner returned.** Compare sets.
- **No test may request the `redis_double` fixture.**
- **SRC_API tests must NOT be wrapped in `web_ctx`; SRC_WEB tests that read `current_user` must be.** `make_post:172` is `user = current_user`.
- **Every monkeypatch must restore in a `finally`.**
- **No duplicate test names.** Python rebinds at collection and the earlier test silently stops running. Check with `grep -oE "^def (test_[a-z_]+)" tests/test_shared_post_make.py | sort | uniq -d` before every commit.
- **Every line number re-derived** with numbered output: `awk 'NR>=X && NR<=Y {printf "%d\t%s\n",NR,$0}' FILE`. Cite a statement's own line, not the `if` guarding it.
- **Sweep citations before committing** with `:[0-9]+(/:[0-9]+)?(-:?[0-9]+)?`, no backtick or path anchor.
- **VERIFY EVERY CITATION MECHANICALLY AND PASTE THE PROOF.** Tasks 1, 2 and 3 each shipped exactly one Major finding and all three were citations — a neighbouring line, a neighbouring line, and a wrong FILE. Sweeping for the pattern is not enough, because a well-formed citation can still point at the wrong thing. For every `file:line` you add or change in a commit, run `awk 'NR>=X && NR<=Y {printf "%d\t%s\n",NR,$0}' FILE` and **paste the numbered output into your report next to the claim it supports**. A citation whose proof is not in the report is not verified, and a reviewer will treat it as a finding.
- **Mutations:** one at a time, **line-scoped `sed`**, dry-run and read the produced line first, apply, run, restore, then assert empty `git diff -- app/` and `wc -l app/shared/post.py` = 1193. Restore before any point where you might stop.
- Commit with `git commit -F <file>`, never `-m`. Lowercase `type:` subject prefix, normal English prose.
- **Commit trailer, last two lines, in this order.** `Claude Opus 5` is a LITERAL CONSTANT, not a field describing the agent:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BF7kVN2spWD86NYKWyGrVn
  ```
- **Register numbering:** findings start at **D454**, `tests/README.md` facts at **224**.

### The four false-witness mechanisms (D451)

Before writing any assertion, ask: *would this value be different on the arm I am NOT testing?*

1. **State the function sets unconditionally.** `:206-228` runs on every non-raising path — the `Post` row, the vote, the counters. None of it witnesses anything about the guards above it. **This is the dominant hazard in this round**, because almost every test will create a post.
2. **A fixture coincidence** making two arms produce the same value.
3. **Emptiness with no positive control.** A raise that does not happen, or an empty call list, is what you get from a correct refusal, a broken fixture, and a monkeypatch that never installed — alike.
4. **An input that takes the same path under both arms.** A test of a filter must use an input the filter would reject.

And: **a mutation's non-failures are evidence.** A test whose docstring names a branch and stays green under that branch's mutation does not guard it.

---

## File Structure

**Create:** `tests/test_shared_post_make.py` — this round's whole test surface, Tasks 1 through 6.

**Modify:** `coverage_floors.ini`, `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`, `tests/README.md` (Task 7).

**Not modified:** `app/shared/post.py` (no production change is planned), `tests/factories.py`, `tests/test_shared_post_edit.py`.

**Helper reuse.** The new file imports `_api_input`, `_web_form`, `_Field` and `_OMIT` from `tests.test_shared_post_edit`. Precedent: `tests/test_ap_collections.py:6` imports `seed_actors` from `tests.test_actor_profiles`, and `tests/__init__.py` makes `tests` a package. `_web_form` already carries `link_url` and `video_url` (`tests/test_shared_post_edit.py:152`), so it is a complete `make_post` WEB form unchanged. These are underscore-prefixed, which normally means module-private; importing them anyway is deliberate and must be justified in the new file's docstring. **Rule of three: two consumers justify an import, a third justifies promoting them to `tests/factories.py`.** Do not promote them in this round.

---

## Task 1: Search the register, probe three blockers, open the file

**Files:**
- Create: `tests/test_shared_post_make.py`

**Interfaces:**
- Produces: `seed_make_context(**kw)` returning a namespace with at least `instance`, `site`, `author`, `community`. Every later task consumes it.

This task is a **probe**, and it is larger than a normal opener because nothing has ever called `make_post`. Its deliverable is a working file plus four recorded answers. **A surprising answer is worth more than the prediction it contradicts** — if a probe contradicts this plan, say so plainly rather than working around it.

- [ ] **Step 1: Search the register BEFORE probing anything**

This step is new this round and exists because the last one skipped it. Sub-project 36 spent a probe, a Major review finding, five documentation sites and a final-review correction re-deriving what **D295** had recorded rounds earlier, then registered a worse duplicate as D442.

```bash
for term in can_create_post 'g\.site' notify_about_post generate_ap_id scale_by extra_rate_limit_check can_upload_video domain_from_url; do
  echo "=== $term ==="
  grep -n "$term" docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md | head -5
done
```

Also grep `tests/README.md` for the same terms. **Report what you found before running any probe.** If an entry already answers a probe below, say so and cite it rather than re-deriving it — and still run the probe, because an entry can be stale, but frame the probe as confirming a citation rather than discovering a fact.

- [ ] **Step 2: Re-derive the line numbers this task cites**

```bash
awk 'NR>=163 && NR<=249 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
awk 'NR>=2494 && NR<=2512 {printf "%d\t%s\n",NR,$0}' app/utils.py
```

Confirm `:164` is `if src == SRC_API:`, `:187` the permission guard, `:219` the `g.site` write, `:231` the `edit_post` call, `:238` the notify guard. If any differs, the tree has moved — report that rather than adjusting silently.

- [ ] **Step 3: Write the file header and the seed helper**

```python
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
"""

import os
from types import SimpleNamespace

import pytest

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
    return SimpleNamespace(instance=instance, site=site, author=author,
                           community=community)
```

- [ ] **Step 4: Probe A — does `make_post` run at all, and what does it need?**

Write this temporarily and run it:

```python
def test_probe_a_bare_make_post_call(db_session, app):
    from app.shared.post import make_post

    s = seed_make_context()

    with web_ctx(app, s.author):
        post = make_post(_web_form(), s.community, POST_TYPE_ARTICLE, SRC_WEB)

    assert post is None
```

Run: `./run_tests.sh tests/test_shared_post_make.py::test_probe_a_bare_make_post_call -v`

**The assertion is expected to fail; the point is HOW.** Three outcomes are plausible and they mean different things:
- `AttributeError` on `g.site` → blocker 2 is real and unsolved. Record the traceback line.
- `Exception: You are not permitted to make posts in this community` → `with_keys=True` did not fix `can_create_post`; something else in it refuses. Read `app/utils.py:2494-2540` and say which check.
- The call succeeds and the assertion fails on `post is None` → all three blockers are handled by `web_ctx` + the seed, which contradicts this plan's premise. **Say so loudly** — it would mean Tasks 2-5 are simpler than written.

Record the verbatim result.

- [ ] **Step 5: Probe B — how is `g.site` supplied?**

If Probe A died on `g.site`, find the cheapest fix and record which you chose and why. Candidates, in order of preference:

```python
from flask import g
g.site = s.site          # inside the web_ctx block, before the call
```

Check whether `conftest.py`'s `site` fixture (`:205`) already helps, and whether `make_site()` alone is enough. **Whatever you choose becomes a helper or a fixture in this file** — every later task needs it, so it must not be re-invented per test. If the fix belongs inside `seed_make_context`, put it there; if it must happen inside the request context, add a context manager beside `web_ctx` and say why.

Record: the chosen mechanism, and whether the SRC_API arm needs it too (it has no request context at all, so `g` may behave differently).

- [ ] **Step 6: Probe C — what does `notify_about_post` do here?**

`:238-239` fires on the ordinary path. Find out whether it is harmless, slow, or fatal:

```python
def test_probe_c_notify_about_post_runs(db_session, app):
    from app.shared.post import make_post

    s = seed_make_context()

    with web_ctx(app, s.author):
        post = make_post(_web_form(), s.community, POST_TYPE_ARTICLE, SRC_WEB)

    assert post.status != POST_STATUS_PUBLISHED
```

The assertion is expected to fail (status IS published by default). What matters is whether the call at `:239` raised, hung, or attempted an outbound request on the way there. **If it needs suppressing, decide where**: a module-level monkeypatch of `post_module.notify_about_post` in the tests that do not care, or a fixture. Record the decision — several later tests take the `:238` true arm and must not each solve this separately.

- [ ] **Step 7: Probe D — is `extra_rate_limit_check` really a stub?**

`:155-160` reads as an unconditional `return False`. If so, arc `166 -> 167` is unreachable without a monkeypatch, and the `raise Exception('rate_limited')` at `:167` is dead code today.

```bash
awk 'NR>=155 && NR<=161 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
```

Confirm by reading, and check whether any caller elsewhere passes something that changes it (`grep -rn "extra_rate_limit_check" app/`). Record the finding — Task 2 needs to know whether it is monkeypatching around a stub or exercising real logic, and Task 7 registers it either way.

- [ ] **Step 8: Delete every probe, write the first real test**

```python
def test_an_article_post_is_created_through_the_web_arm(db_session, app):
    """`:164`'s false arm through to `:246`'s bare return.

    The first end-to-end call this function has ever had. Asserts on the row
    reaching the database, not merely on the return value, because `:246`
    returns the same object `:206` built and a regression that never committed
    would still return it.
    """
    s = seed_make_context()

    with web_ctx(app, s.author):
        result = make_post(_web_form(), s.community, POST_TYPE_ARTICLE, SRC_WEB)

    assert result is not None
    rows = db.session.query(Post).filter_by(community_id=s.community.id).all()
    assert len(rows) == 1
    assert rows[0].user_id == s.author.id
    assert rows[0].title == 'a title'
```

Apply whatever Probes B and C established — this test must pass.

- [ ] **Step 9: Run the file**

```bash
./run_tests.sh tests/test_shared_post_make.py -v
```

Report the collection line verbatim. No probe tests may remain.

- [ ] **Step 10: Write the probe report**

Record all four answers with verbatim output, the register-search results from Step 1, and state plainly whether any contradicts this plan. Fix the file docstring in this task if so.

- [ ] **Step 11: Commit**

Subject: `test: open the make_post file and settle its three blockers`

---

## Task 2: The source fork, the url arms, and the return fork

**Files:**
- Modify: `tests/test_shared_post_make.py`

**Arcs:** `164->165`, `164->172`, `166->167`, `166->168`, `174->175`, `174->176`, `176->177`, `176->179`, `243->244`, `243->246` — **10 of the 26.**

- [ ] **Step 1: Re-derive**

```bash
awk 'NR>=163 && NR<=181 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
awk 'NR>=243 && NR<=247 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
```

- [ ] **Step 2: Understand the trap in this task**

The API and WEB arms produce the SAME post. Both create a row, both set the same title. **A test that asserts only "a post exists" cannot tell the arms apart** — that is false-witness mechanism 1, and `:206-228` runs on both paths. What differs:

| | SRC_API | SRC_WEB |
|---|---|---|
| user comes from | `authorise_api_user(auth)` `:165` | `current_user` `:172` |
| title | `input['title']` `:168`, NOT stripped | `input.title.data.strip()` `:173`, stripped |
| url | `input['url']` `:169`, no type logic | type-dependent, `:174-179` |
| returns | `(user.id, post)` `:244` | `post` `:246` |

**The return fork is the cleanest discriminator** — a tuple versus a bare object — and the title strip is the second. Use both.

- [ ] **Step 3: Write the source-fork tests**

```python
def test_the_api_arm_returns_the_user_id_and_the_post(db_session):
    """`:164`'s true arm and `:244`'s two-tuple.

    NO `web_ctx`: the API arm takes no request context. The two-tuple is what
    distinguishes this arm from `:246`'s bare return, and a test asserting only
    that a post exists would pass on either arm -- `:206-228` runs on both.
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

    One test covering both arms deliberately, because the asymmetry IS the
    witness: the same input produces different stored titles depending on the
    arm. Catches a regression adding `.strip()` to `:168` or dropping it from
    `:173`, neither of which any single-arm test would see.
    """
    s = seed_make_context()

    api_result = make_post(_api_input(title='  spaced  '), s.community,
                           POST_TYPE_ARTICLE, SRC_API, auth=bearer(s.author))
    with web_ctx(app, s.author):
        web_post = make_post(_web_form(title='  spaced  '), s.community,
                             POST_TYPE_ARTICLE, SRC_WEB)

    assert api_result[1].title == '  spaced  '
    assert web_post.title == 'spaced'
```

- [ ] **Step 4: Write the url-arm tests**

`:174-179` is a three-way selection on `type`. Each arm needs its own witness, and the discriminator is the url that reaches the post:

```python
def test_a_link_post_picks_link_url_not_video_url(db_session, app):
    """`:174`'s true arm and `:175`, witnessed through `:195`'s raise.

    THE ASSERTION IS ON THE RAISE, NOT ON `post.url`, AND THAT IS THE WHOLE
    POINT. `make_post` never puts its local `url` on the Post: `:206-207`
    passes `title` and `language_id` only. `post.url` is set later by
    `edit_post`, which re-derives url from the SAME `input` and `type`
    (`:256`, `:321-327`). So a test asserting `post.url == ...` passes even
    with `:175` deleted -- it witnesses `edit_post`, not `make_post`.

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
```


- [ ] **Step 5: Write the rate-limit test**

Task 1's Probe D establishes whether `extra_rate_limit_check` is a stub. **If it returns `False` unconditionally, arc `166 -> 167` is reachable only by monkeypatch**, and the test must say so:

```python
def test_a_rate_limited_api_user_is_refused(db_session):
    """`:166`'s true arm and `:167`'s raise.

    `extra_rate_limit_check` (`:155-160`) is currently an unconditional
    `return False` -- its docstring says the real limiting is still planned --
    so this arc CANNOT be reached without replacing it. The monkeypatch is not
    a convenience here; it is the only way in, and that fact is registered
    rather than hidden. When the function grows real logic, this test keeps
    working and a fixture-based version would have to be rewritten.
    """
    s = seed_make_context()

    import app.shared.post as post_module
    original = post_module.extra_rate_limit_check
    post_module.extra_rate_limit_check = lambda user: True
    try:
        with pytest.raises(Exception, match='rate_limited'):
            make_post(_api_input(), s.community, POST_TYPE_ARTICLE, SRC_API,
                      auth=bearer(s.author))
    finally:
        post_module.extra_rate_limit_check = original

    assert db.session.query(Post).count() == 0
```

The `count() == 0` assertion matters: it proves the raise happened BEFORE `:206` created a row. Without it the test would pass even if the raise moved below the creation.

**Also write the false-arm counterpart** — an ordinary API call reaching `:168` — or state which existing test serves as it. `:166`'s false arm needs a witness that is not merely "some other test happens to pass".

- [ ] **Step 6: Run and report the collection line**

- [ ] **Step 7: State which arc each test witnesses, and name the positive control for the rate-limit test**

- [ ] **Step 8: Commit**

Subject: `test: cover make_post's source fork, url arms and return fork`

---

## Task 3: The permission and domain guards

**Files:**
- Modify: `tests/test_shared_post_make.py`

**Arcs:** `187->188`, `187->190`, `190->191`, `190->197`, `193->194`, `193->197`, `194->195`, `194->197` — **8 of the 26.**

- [ ] **Step 1: Re-derive**

```bash
awk 'NR>=187 && NR<=196 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
```

Confirm `:187` is the two-disjunct permission guard, `:190` `if url:`, `:193` `if domain:`, `:194` the banned/`.pages.dev` compound.

- [ ] **Step 2: Know the compounds before writing**

`:187` is `if not can_create_post(user, community) or user_ip_banned():` — **two disjuncts, one arc pair.** Each needs its own witness.

`:194` is `if domain.banned or domain.name.endswith('.pages.dev'):` — **two disjuncts, one arc pair.** Each needs its own witness.

Taking each compound true once and false once satisfies coverage while leaving an operand untested. That is how sub-project 36 shipped a test that could not fail.

- [ ] **Step 3: Write the permission tests**

```python
def test_an_unverified_user_cannot_make_a_post(db_session):
    """`:187`'s FIRST disjunct and `:188`'s raise.

    `can_create_post` (app/utils.py:2505) refuses a local user who is
    unverified OR keyless. This seeds the keyed author `seed_make_context`
    builds and then clears `verified`, so the refusal comes from the check
    under test rather than from the missing-key condition every test in this
    file already works around.
    """
    s = seed_make_context()
    s.author.verified = False
    db.session.commit()

    with pytest.raises(Exception, match='not permitted'):
        make_post(_api_input(), s.community, POST_TYPE_ARTICLE, SRC_API,
                  auth=bearer(s.author))

    assert db.session.query(Post).count() == 0


def test_an_ip_banned_user_cannot_make_a_post(db_session):
    """`:187`'s SECOND disjunct, with the first FALSE.

    The author is fully permitted -- keyed, verified, unbanned -- so
    `can_create_post` returns True and only `user_ip_banned()` refuses. That
    separation is the whole test: a fixture failing both disjuncts would pass
    while witnessing only the first.
    """
    s = seed_make_context()

    import app.shared.post as post_module
    original = post_module.user_ip_banned
    post_module.user_ip_banned = lambda: True
    try:
        with pytest.raises(Exception, match='not permitted'):
            make_post(_api_input(), s.community, POST_TYPE_ARTICLE, SRC_API,
                      auth=bearer(s.author))
    finally:
        post_module.user_ip_banned = original

    assert db.session.query(Post).count() == 0
```

**Check the import style first.** `user_ip_banned` must be patched at the binding site `make_post` actually reads. Run `grep -n "user_ip_banned\|can_create_post" app/shared/post.py` and confirm whether they are module-level imports (patch `post_module.<name>`) or imported inside the function body (patch the source module instead). `tests/README.md` records this campaign's binding-site convention — follow it, and say in your report which site you patched and why.

- [ ] **Step 4: Write the domain tests**

```python
def test_a_post_with_no_url_skips_the_domain_check(db_session):
    """`:190`'s false arm, straight to `:197`.

    `_api_input` defaults `url` to None, so this is the ordinary article path.
    Asserts no Domain row was created, which is what distinguishes skipping the
    block from running it against a url that happens to be clean.
    """
    s = seed_make_context()
    before = db.session.query(Domain).count()

    make_post(_api_input(), s.community, POST_TYPE_ARTICLE, SRC_API,
              auth=bearer(s.author))

    assert db.session.query(Domain).count() == before


def test_a_banned_domain_is_refused(db_session):
    """`:194`'s FIRST disjunct and `:195`'s raise.

    `domain_from_url` (app/utils.py:1561) creates the Domain when absent, so
    the row is seeded banned FIRST -- otherwise the call would create a fresh
    unbanned one and the guard would never fire.
    """
    s = seed_make_context()
    domain = make_domain('banned.example')
    domain.banned = True
    db.session.commit()

    with pytest.raises(Exception, match='blocked by admin'):
        make_post(_api_input(url='https://banned.example/thing'), s.community,
                  POST_TYPE_LINK, SRC_API, auth=bearer(s.author))

    assert db.session.query(Post).count() == 0


def test_a_pages_dev_domain_is_refused_even_when_not_banned(db_session):
    """`:194`'s SECOND disjunct, with the first FALSE.

    The domain is created by `domain_from_url` and left UNBANNED, so
    `domain.banned` is False and only the `.pages.dev` suffix test refuses.
    This is the only test that distinguishes the two operands of `:194`, which
    coverage.py scores as a single arc pair.
    """
    s = seed_make_context()

    with pytest.raises(Exception, match='blocked by admin'):
        make_post(_api_input(url='https://someone.pages.dev/thing'),
                  s.community, POST_TYPE_LINK, SRC_API, auth=bearer(s.author))

    assert db.session.query(Post).count() == 0


def test_an_ordinary_domain_is_allowed(db_session):
    """`:194`'s false arm -- BOTH disjuncts false -- and `:197`.

    The positive control for the two refusal tests above: same shape, clean
    domain, no raise. Without it, a `pytest.raises` that passed because the
    call raised for some unrelated reason would look identical.
    """
    s = seed_make_context()

    user_id, post = make_post(_api_input(url='https://clean.example/thing'),
                              s.community, POST_TYPE_LINK, SRC_API,
                              auth=bearer(s.author))

    assert post.url == 'https://clean.example/thing'
```

**`:193`'s false arm** — a truthy url whose `domain_from_url` returns None — needs its own witness. `app/utils.py:1562-1568` returns None for a falsy url and for one whose host cannot be determined. Work out an input that is truthy but hostless, write the test, and if you conclude no such input survives `:191`'s `strip()` say so with the argument rather than leaving the arc silent.

- [ ] **Step 5: Run and report the collection line**

- [ ] **Step 6: State which disjunct of `:187` and of `:194` each test witnesses**

- [ ] **Step 7: Commit**

Subject: `test: cover make_post's permission and domain guards`

---

## Task 4: The uploaded-file guards

**Files:**
- Modify: `tests/test_shared_post_make.py`

**Arcs:** `197->199`, `197->206`, `200->201`, `200->202`, `203->204`, `203->206` — **6 of the 26.**

- [ ] **Step 1: Re-derive**

```bash
awk 'NR>=197 && NR<=205 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
awk 'NR>=2581 && NR<=2597 {printf "%d\t%s\n",NR,$0}' app/utils.py
```

- [ ] **Step 2: Know what you are testing**

`:197` is `if uploaded_file and uploaded_file.filename != '':` — two conditions, one arc pair. An absent file and a file with an empty filename take the same arm for different reasons; witness both.

`:200` is `if type == POST_TYPE_VIDEO and can_upload_video():` — **two conjuncts, one arc pair.** `can_upload_video` (`app/utils.py:2586`) reads `get_setting('allow_video_file_uploads', 'no')` and returns False on the default, so the second conjunct is false unless the setting is changed. That makes the conjuncts genuinely separable: a video post with the setting off takes the false arm on the second conjunct, and a non-video post takes it on the first.

`uploaded_file` is a Werkzeug `FileStorage` in production, but `:197-203` reads only `.filename`, so a `SimpleNamespace(filename=...)` is sufficient. **Verify that by reading `:197-204` before relying on it** — if anything else is read, the stand-in must supply it, and an `AttributeError` is not an assertion.

- [ ] **Step 3: Write the tests**

```python
def test_no_uploaded_file_skips_the_extension_check(db_session):
    """`:197`'s false arm via `uploaded_file=None`, straight to `:206`."""
    s = seed_make_context()

    user_id, post = make_post(_api_input(), s.community, POST_TYPE_ARTICLE,
                              SRC_API, auth=bearer(s.author))

    assert post.title == 'a title'


def test_an_uploaded_file_with_an_empty_filename_is_ignored(db_session):
    """`:197`'s SECOND condition taken false, with the first TRUE.

    A FileStorage with no filename is what a browser sends for an empty file
    input, so this is the ordinary no-upload submission rather than an edge
    case. Distinguishing it from `uploaded_file=None` is the point: both reach
    `:206`, and only this test shows the `.filename != ''` half is load-bearing.
    """
    s = seed_make_context()

    user_id, post = make_post(_api_input(), s.community, POST_TYPE_ARTICLE,
                              SRC_API, auth=bearer(s.author),
                              uploaded_file=SimpleNamespace(filename=''))

    assert post.title == 'a title'


def test_a_disallowed_extension_is_refused(db_session):
    """`:203`'s true arm and `:204`'s raise."""
    s = seed_make_context()

    with pytest.raises(Exception, match='filetype not allowed'):
        make_post(_api_input(), s.community, POST_TYPE_ARTICLE, SRC_API,
                  auth=bearer(s.author),
                  uploaded_file=SimpleNamespace(filename='payload.exe'))

    assert db.session.query(Post).count() == 0


def test_an_allowed_image_extension_passes(db_session):
    """`:203`'s false arm -- the positive control for the test above.

    Same shape, allowed extension, no raise. `:199`'s list is the witness:
    change it and this test fails where the refusal test would not.
    """
    s = seed_make_context()

    user_id, post = make_post(_api_input(), s.community, POST_TYPE_ARTICLE,
                              SRC_API, auth=bearer(s.author),
                              uploaded_file=SimpleNamespace(filename='pic.PNG'))

    assert post.title == 'a title'


def test_a_video_upload_is_refused_when_video_uploads_are_off(db_session):
    """`:200`'s SECOND conjunct taken false, with the first TRUE.

    `can_upload_video` (app/utils.py:2588-2589) returns False when
    `allow_video_file_uploads` is 'no', which is the default, so `:201` never
    runs and '.mp4' stays out of the allowed list. A regression dropping the
    `can_upload_video()` conjunct would let this through.
    """
    s = seed_make_context()

    with pytest.raises(Exception, match='filetype not allowed'):
        make_post(_api_input(), s.community, POST_TYPE_VIDEO, SRC_API,
                  auth=bearer(s.author),
                  uploaded_file=SimpleNamespace(filename='clip.mp4'))
```

**`:200`'s FIRST conjunct taken false with the second true** needs its own witness — a non-video post while `can_upload_video()` would return True. That requires flipping the setting; find how `get_setting` is written in this suite (`grep -rn "set_setting\|get_setting" tests/ | head`) and use the established mechanism rather than inventing one. If the setting cannot be changed cleanly in a test, monkeypatch `post_module.can_upload_video` and say in the docstring that the monkeypatch stands in for a setting.

**Also write the `:200 -> :201` true-arm witness**: a video post with uploads enabled and a `.mp4` filename, which must NOT raise.

- [ ] **Step 4: Run and report the collection line**

- [ ] **Step 5: State which conjunct of `:200` each test witnesses**

- [ ] **Step 6: Commit**

Subject: `test: cover make_post's uploaded-file and video-extension guards`

---

## Task 5: Creation, state mutations, delegation, rollback and notify

**Files:**
- Modify: `tests/test_shared_post_make.py`

**Arcs:** `238->239`, `238->241` — only 2. **This task carries the least arc weight and the most risk**, because the rollback at `:233-236` has NO arc at all: coverage.py does not model exception handlers as branches, so nothing in the arc count reveals whether those four statements are tested.

- [ ] **Step 1: Re-derive**

```bash
awk 'NR>=206 && NR<=247 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
```

- [ ] **Step 2: Write the state-mutation tests**

`:211-222` sets eight things. Assert them where a regression would be invisible otherwise:

```python
def test_creating_a_post_seeds_its_author_upvote(db_session):
    """`:211`'s up_votes, `:213`'s score, and `:226-227`'s PostVote row.

    A new post starts with its author's own upvote. Catches a regression
    dropping `:226`, which would leave the score claiming a vote that no
    PostVote row backs -- a discrepancy no single-field assertion would show.
    """
    s = seed_make_context()

    user_id, post = make_post(_api_input(), s.community, POST_TYPE_ARTICLE,
                              SRC_API, auth=bearer(s.author))

    assert post.up_votes == 1
    assert post.score == 1
    votes = db.session.query(PostVote).filter_by(post_id=post.id).all()
    assert len(votes) == 1
    assert votes[0].user_id == s.author.id
    assert votes[0].effect == 1


def test_creating_a_post_increments_both_counters(db_session):
    """`:218`'s community.post_count and `:220`'s user.post_count.

    Both start at a known non-zero value so a regression that ASSIGNS rather
    than increments fails here. Starting from zero would let `= 1` pass.
    """
    s = seed_make_context()
    s.community.post_count = 5
    s.author.post_count = 7
    db.session.commit()

    make_post(_api_input(), s.community, POST_TYPE_ARTICLE, SRC_API,
              auth=bearer(s.author))

    db.session.refresh(s.community)
    db.session.refresh(s.author)
    assert s.community.post_count == 6
    assert s.author.post_count == 8
```

- [ ] **Step 3: Write the rollback tests — the part no arc will tell you about**

```python
def test_a_failing_edit_post_rolls_back_the_post_and_the_vote(db_session):
    """`:233`, `:234` and `:235` -- the rollback, which has NO branch arc.

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

    import app.shared.post as post_module
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


def test_a_failing_edit_post_re_raises_the_original_exception(db_session):
    """`:236`'s `raise e`, distinct from the deletions above it.

    The rollback must not swallow the cause. Catches a regression replacing
    `raise e` with a bare `return` or a generic error -- both of which would
    leave the two count assertions above passing while the caller lost the
    reason.
    """
    s = seed_make_context()

    import app.shared.post as post_module
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
```

**Confirm the patch target before writing these.** `make_post:231` calls `edit_post` as a module-global; verify with `grep -n "^from\|^import\|def edit_post" app/shared/post.py` that patching `post_module.edit_post` is what `make_post` actually resolves. If `edit_post` is resolved some other way, patch the site that works and say so.

- [ ] **Step 4: Write the notify tests**

```python
def test_a_published_post_notifies(db_session):
    """`:238`'s true arm and `:239`.

    Status defaults to published, so this is the ordinary path. Records the
    call rather than asserting on its effects, because `notify_about_post`
    dispatches a Celery task whose body is out of scope here.
    """
    calls = []
    s = seed_make_context()

    import app.shared.post as post_module
    original = post_module.notify_about_post
    post_module.notify_about_post = lambda post: calls.append(post.id)
    try:
        user_id, post = make_post(_api_input(), s.community, POST_TYPE_ARTICLE,
                                  SRC_API, auth=bearer(s.author))
    finally:
        post_module.notify_about_post = original

    assert calls == [post.id]


def test_an_unpublished_post_does_not_notify(db_session):
    """`:238`'s false arm.

    Needs a post whose status is NOT published when `:238` runs. `edit_post`
    sets status, so the status must be forced through the input rather than
    after the fact. Work out how -- a scheduled post is the natural case -- and
    if no input produces a non-published status, say so with the argument
    rather than deleting the test.

    Its positive control is the test above: same shape, same recorder, one call
    instead of none. An empty `calls` list is what a broken monkeypatch
    produces too.
    """
```

**This second test is deliberately left unfinished.** `:238`'s false arm requires a post that is not published after `edit_post` runs, and the mechanism is `edit_post`'s scheduling logic — which this round does not otherwise touch. Find it, or argue it is unreachable from `make_post`'s inputs and report that. **Do not delete the arc silently**; if it cannot be reached, Task 7 registers it and the round does not claim zero missing arcs.

- [ ] **Step 5: Witness that `make_post` refuses BEFORE it creates anything**

`make_post:190-195` is redundant in OUTCOME — `edit_post:565-569` reimplements
the identical compound, four lines including the exception string, and
`:231` passes `from_scratch=True` so that guard always fires. What
`make_post`'s copy buys is TIMING: it raises at `:195` before `:206` creates
the Post, where `edit_post` raises at `:569` after the Post and vote are
committed at `:209` and `:228`, which is the whole reason `:230-236` exists.

No test so far distinguishes those two, because both leave `Post.count() == 0`
— one by never creating a row, the other by rolling one back. This one does:

```python
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
    """
    calls = []
    s = seed_make_context()
    d = make_domain('banned.example')
    d.banned = True
    db.session.commit()

    import app.shared.post as post_module
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
```

Verify it: move `:190-195` to below `:231` — or simply delete `:194`'s raise —
and confirm this test fails because `calls` is no longer empty. Restore, and
confirm `git diff -- app/` is empty and `wc -l app/shared/post.py` reads 1193.

- [ ] **Step 6: Run and report the collection line**

- [ ] **Step 7: Report whether `:238`'s false arm was reached**, and if not, the argument

- [ ] **Step 8: Commit**

Subject: `test: cover make_post's state mutations, rollback and notify guard`

---

## Task 6: Mutation pass

**Files:**
- Modify: `tests/test_shared_post_make.py` (only to close a hole)
- Modify: `app/shared/post.py` TEMPORARILY, always restored

- [ ] **Step 1: Record the baseline**

```bash
git diff --stat -- app/
wc -l app/shared/post.py
```

Empty diff, 1193 lines.

- [ ] **Step 2: Build the site table BEFORE mutating anything**

Re-derive every line number first. Enumerate at minimum one mutation per arc in the spec's table of 26, plus:

| Site | Mutation |
|---|---|
| `:164` | `SRC_API` → `SRC_WEB` |
| `:166` | invert |
| `:174`, `:176` | swap the constants |
| `:187` | drop each disjunct separately |
| `:190`, `:193` | invert |
| `:194` | drop each disjunct separately |
| `:197` | drop each condition separately |
| `:200` | drop each conjunct separately |
| `:203` | invert the `not in` |
| `:199` | remove an extension from the list |
| `:211`, `:213`, `:218`, `:220` | change each assignment |
| `:226` | delete the PostVote |
| `:233`, `:234` | delete each |
| `:236` | replace `raise e` with `pass` |
| `:238` | invert |
| `:243` | invert |

**Enumerate every site before mutating any.** A site named in prose and never mutated is a hole that looks like coverage, and an earlier sub-project shipped exactly that.

- [ ] **Step 3: The loop, per mutation**

```bash
sed -n '<LINE>p' app/shared/post.py          # dry-run: READ the line first
sed -i '<LINE>s/<old>/<new>/' app/shared/post.py
git diff -- app/shared/post.py               # confirm it landed where intended
./run_tests.sh tests/test_shared_post_make.py -q
git checkout -- app/shared/post.py
git diff -- app/; wc -l app/shared/post.py   # assert clean, 1193
```

**Always scope `sed` to the line number.** An unanchored pattern can match a second site — sub-project 36 found `:785`'s pattern also at `:1077`, and mutating both would have produced a result nobody was measuring.

**Restore before any point where you might stop.**

- [ ] **Step 4: Read results in both directions**

- **A crash kill is not a kill.** For any failure that is not an assertion, ask whether a viable non-crashing variant of the same fault survives. Record the answer.
- **An operator can be structurally void.** Check the mutation has a signature-compatible target before reading a crash as a kill.
- **Non-failures are evidence.** For each mutation, compare the tests that failed against the tests whose docstrings name that line. Report any mismatch in either direction. Sub-project 36's worst finding was a test that stayed green under the mutation of the branch it claimed to guard, visible in its own mutation output before any reviewer read it.
- **An arc being equivalent does not make every mutation of its line equivalent.** State which claim you are making.

- [ ] **Step 5: Two mandatory mutations beyond the table**

Sub-project 36 found a security-relevant hole that only a non-crashing variant revealed: stripping `restore_post`'s authorisation call passed all 46 tests, because every test used the author's own token.

1. Replace `:165`'s `user = authorise_api_user(auth, return_type='model')` with something that skips authorisation and yields a user — no crash.
2. Neutralise `:187`'s guard so it never refuses.

**If either survives, that is a real hole and a security-relevant one.** Close it with a test that passes a token for a user who is not permitted and asserts the refusal — asserting the specific exception, not merely that no post was created, since a broken fixture produces that too.

- [ ] **Step 6: Close every hole**

Either add a test, or prove the mutation equivalent with an argument that quantifies over the inputs that REACH the site. Re-run the whole file after each new test, and re-run the mutation to confirm the new test actually kills it.

- [ ] **Step 7: Final checks**

```bash
git diff -- app/
wc -l app/shared/post.py
grep -oE "^def (test_[a-z_]+)" tests/test_shared_post_make.py | sort | uniq -d
./run_tests.sh tests/test_shared_post_make.py -q
```

Empty diff, 1193, empty duplicate check, all passing.

- [ ] **Step 8: Commit any tests added**

Subject: `test: close the holes the make_post mutation pass found`

If the pass found no holes, make no commit and say so.

---

## Task 7: Measure, raise the floor, and register

**Files:**
- Modify: `coverage_floors.ini`
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`
- Modify: `tests/README.md`

- [ ] **Step 1: Measure**

```bash
./run_tests.sh tests/test_shared_post_make.py tests/test_shared_post_lifecycle.py \
  tests/test_shared_post_moderation.py tests/test_shared_post_interactions.py \
  tests/test_shared_post_edit.py \
  --cov=app.shared.post --cov-branch --cov-report=json:/tmp/post37.json -q
echo "exit=$?"
```

**Five files now** — the new one plus the four that existed. Dotted form, not a path.

- [ ] **Step 2: Read the number**

```bash
podman-compose -f compose.test.yaml exec -T test-runner python -c \
  "import json;d=json.load(open('/tmp/post37.json'));print(d['files']['app/shared/post.py']['summary'])"
```

Read `percent_covered`.

- [ ] **Step 3: Confirm Group D is closed**

Re-derive `make_post`'s boundaries; do not trust `:163-249`. Confirm zero missing statements and zero missing arcs in that range. **Test BOTH endpoints of every missing arc.** If any remain, report before touching the floor.

- [ ] **Step 4: Report Group E's new figures**

This round's `from_scratch=True` calls reach `edit_post`'s `:324`, `:421`, `:565`, `:739`, `:746`, `:750` for the first time. Record `edit_post`'s missing statements and arcs now, so sub-project 38 inherits a measurement rather than a stale 110/77.

- [ ] **Step 5: Raise the floor**

`coverage_floors.ini`'s `app/shared/post.py` entry is 77. Raise it to the measured `percent_covered` rounded DOWN. The module is not closed — Group E remains.

- [ ] **Step 6: Register from D454**

Update the "Next free number" marker. Required entries:

1. **Why `make_post` was at 0.0%** — the three blockers, each with its mechanism, since together they are the reason a whole function went untested through 36 sub-projects.
2. **`extra_rate_limit_check` is a stub** (`:155-160`, unconditional `return False`) whose docstring says the real limiting is planned. Arc `166 -> 167` is reachable only by monkeypatch, and `:167`'s raise is dead code today. Register it as dead-but-intended, not as a defect.
3. **The `try`/`except` at `:230-236` has no branch arc.** Four statements invisible to the arc count. Record it as a general lesson: full arc coverage of a function says nothing about its exception handlers.
4. **Group E's arcs closed as a side effect**, with the before and after figures.
5. Whatever the mutation pass found, including every survivor with its argument.
6. **Whether `:238`'s false arm was reached**, and if not, the argument — this is the one arc this plan flags as possibly unreachable.

- [ ] **Step 7: `tests/README.md` facts from 224**

At minimum: the `with_keys=True` requirement, its cost, and the misleading error it prevents; how `g.site` is supplied and why the fallback does not help a local author; what `notify_about_post` does under eager Celery; and the cross-module helper import with its rule-of-three note.

- [ ] **Step 8: Commit**

Subject: `docs: raise post.py's floor and register sub-project 37's findings`

**The controller runs the full suite and the floors check** — not this task.

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: the three blockers to Task 1's probes; the register search to Task 1 Step 1; the 26 arcs to Tasks 2-5, which account for 10 + 8 + 6 + 2 = 26; the two compound conditions to Tasks 3 and 4 with an explicit per-operand witness; the arcless `try`/`except` to Task 5 Step 3; the two mandatory authorisation mutations to Task 6 Step 5; the register list to Task 7 Step 6.

**Placeholder scan.** Three steps ask the implementer to derive rather than transcribe — Task 3's `:193` false arm, Task 4's `:200` first conjunct, and Task 5's `:238` false arm — and each says explicitly that a reasoned impossibility is an acceptable, reportable outcome. Task 5's notify test is deliberately left unfinished with that instruction attached. Those are bounded questions, not placeholders. Every other step contains the code it asks for.

**Type consistency.** `seed_make_context(community_name='making')` returns `instance`, `site`, `author`, `community` and keeps that shape through Task 6. The `original`/patch/`finally` monkeypatch shape is identical in Tasks 2, 3, 5 and 6. `_api_input`, `_web_form`, `_Field` and `_OMIT` are imported once in Task 1 and used unchanged thereafter.

**Known risk, stated.** `:238`'s false arm may not be reachable from `make_post`'s inputs, because the status is set inside `edit_post`. If it is not, the round cannot claim zero missing arcs, and Task 7 registers the gap rather than the floor being raised past it. That is the same shape as sub-project 36's `:905` arc, which was registered open and then closed by a follow-up — and it is why Task 7 checks before raising.
