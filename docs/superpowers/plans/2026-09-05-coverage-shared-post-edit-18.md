# Sub-project 18 — `edit_post` dispatch and suspicious-domain block — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take `edit_post`'s source dispatch (`app/shared/post.py:251-383`) and its suspicious-domain block (`:565-598`) to zero uncovered statements and zero uncovered branch arms, and land the two production fixes the campaign registered there.

**Architecture:** One new test file, `tests/test_shared_post_edit.py`, drives `edit_post` by direct call. Both source branches are entered by passing `user=` explicitly, which skips `authorise_api_user` on the API side and `current_user` on the web side. The `SRC_WEB` shape is served by a small WTForms double in the file's prelude. The two fixes are one-line in-place edits, each test-first in its own commit, each mutation-proved.

**Tech Stack:** pytest, respx (`http_mock`), SQLAlchemy, Flask, `tests/factories.py`, `./run_tests.sh` (podman-compose, tmpfs Postgres).

**Spec:** `docs/superpowers/specs/2026-09-05-coverage-shared-post-edit-18-design.md`

---

## Global Constraints

Copied verbatim from the spec. Every task's requirements implicitly include this section.

- **Delete nothing the task did not create. `claude_test` in the repository root is not the campaign's.**
- Only the controller runs the full suite, and only one pytest session at a time. The controller supplies every coverage figure; no implementer reports one.
- Any test run exceeding ~600s is erroneous and must be addressed directly: `./run_tests.sh --down`, then retry.
- Coverage command: `./run_tests.sh --cov=app --cov-branch --cov-report=json:scratch_full_cov.json -q`.
- Mutation discipline: one mutation at a time, `app/` restored and verified after each, never batched. A fix that changes a guard invalidates every mutation previously recorded for that guard; re-run them all in the same commit (fact 74).
- Every line number copied from anywhere must be re-derived against the current tree before it is written down (fact 99). Citation sweeps run in two passes: `file:line` first, then bare paths against `git ls-files` (fact 100).
- Enumerate conditional expressions by AST walk, not by grep (fact 94). `sed -n 'A,Bp'` prints no line numbers; use `grep -n`, `awk` on `NR`, or `cat -n` (fact 95).
- Commit messages containing backticks are committed with `git commit -F <file>`, never `-m`.
- **No line number in `app/shared/post.py` may move.** Both fixes are in-place single-line edits. Seven test files cite this module by line and the cited lines bracket both fix sites.

---

## Correction to the spec, carried into every task

Spec §4.1 says D287 must land first "so that the D292 test can reach the crash through the path the fix just opened." **That reason is wrong and the plan does not rely on it.** The `targets_data` dict at `:573-579` is built inside `if domain:` at `:567`, *before* both loops. `:577` therefore executes for every domain, and the crash needs only that *some* loop add a `Notification`. The admin loop at `:590-598` does that today, with D287 unfixed.

The ordering is kept for a different and real reason: Task 6 covers the dedup arms at `:592`, which need a user who is both moderator and admin, and that is reachable only once `:582` works. Recording the corrected reason here rather than only deleting the wrong one is harness fact 96.

**SECOND CORRECTION, appended by Task 8's register round and NOT known to any earlier task: the finding number is wrong throughout this document.** The `orig_post_domain` / `TypeError: Object of type Domain is not JSON serializable` defect is **D286**, not D292. Every "D292" in this plan, in the spec it came from, in Task 5's brief and commit `26ad38e7`'s body, and in six locations in `tests/test_shared_post_edit.py` (eight occurrences on seven lines, `grep -o 'D292'` at commit `2df31ca9`), means **D286** — the number was read once from the spec and copied forward without anyone re-opening the register cell it named. **D292 is a different, still-open entry**: `Post.new`'s unguarded `choice_ap['name']` read at `app/models.py:2205-2209`, the create-path sibling of D284. Nothing downstream broke — the fix, the tests and the arbitration are all correct, and only the label was wrong, which is exactly the part a green suite cannot check. The closure is recorded in `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` against D286, and the six sites are enumerated there by symbol for whoever next owns `tests/test_shared_post_edit.py`, since Task 8's authorisation was docs and config only.

---

## Harness facts this plan depends on

Each was re-derived against the current tree while this plan was written.

1. `app/shared/post.py:250` is `def edit_post(input, post: Post, type, src, user=None, auth=None, uploaded_file=None, from_scratch=False, hash=None):`. The next `def` is `delete_post` at `:755`.
2. `:252` `if not user:` and `:316` `if not user:` are the same guard on both branches. Passing `user=<User>` skips `authorise_api_user` **and** `current_user`. No request context, no login, no auth token.
3. `:384` is the marker comment. Note that `:388` and `:390` read `input.sticky.data` and `input.nsfl.data` *below* it, guarded by the conditional expression `False if src == SRC_API else ...`. The `SRC_WEB` double must therefore carry `sticky` and `nsfl`.
4. `:419` is `hash = None`, unconditional. The `hash=` parameter of `edit_post` is dead: `:602` reads the local, never the argument.
5. `:435` `if url != post.url or uploaded_file:` sets `url_changed = True` at `:436`, and `:459` `db.session.commit()` runs only inside `if not from_scratch:` at `:421`. **A test that needs the `:459` commit must pass `from_scratch=False` and change the url.**
6. `:565` is `if url and (from_scratch or url_changed):`.
7. `:601` `if is_image_url(url):` → `:606` `db.session.add(file)` → `:607` `db.session.commit()`. This is the first commit after the notify block.
8. `is_image_url` (`app/utils.py:247`) calls `mime_type_using_head`, which issues an **httpx HEAD**. `mime_type_using_head` catches only `httpx.HTTPError` and `httpx.InvalidURL`; respx's unmatched-request error is neither, so it escapes. Every url-bearing test must register the HEAD route.
9. `:403` `if post.url:` also calls `is_image_url(post.url)` at `:410`. **Seed the post with `url=None`** so only one HEAD is needed.
10. `block_outbound_http` (`tests/conftest.py:214`) is session-scoped, autouse, and registers zero routes; unmatched requests raise. `http_mock` (`tests/conftest.py:288-295`) is `assert_all_called=True`, so a registered route that is never reached fails the test.
11. Under this harness `make_image_sizes` (`app/activitypub/util.py:1724`) **executes**. The technique for stopping it is a **bodiless 404** on `file.source_url`: `make_image_sizes_async:1748` needs `/api/v3/image_proxy` in the url to retry, and `:1759` needs status 200 to proceed. Neither holds, so it returns having done nothing.
12. `domain_from_url` (`app/utils.py:1561`) lowercases the host and strips a leading `www.`, then looks up `Domain.name`, creating the row when absent. A seeded `Domain(name='suspicious.example')` is found by `https://suspicious.example/...`.
13. `Domain.notify_mods` and `Domain.notify_admins` are `db.Column(db.Boolean, default=False)` (`app/models.py:3458-3459`). `make_domain(name)` does not set them; a test sets the attributes and commits.
14. `Community.moderators()` (`app/models.py`) returns `CommunityMember` rows with `is_owner` or `is_moderator`, excluding `is_banned`. `make_community_member(user, community, is_moderator=True)` produces one.
15. `Site.admins()` joins `user_role` with an **INNER** join, so a roleless `User.id == 1` is **not** an admin (D295). It filters on `user_role.c.role_id == ROLE_ADMIN`, so the role's id must *be* `ROLE_ADMIN` (4, `app/constants.py:81`) — `grant_permission` creates a `Role` with an auto id and is therefore not enough. `tests/test_ap_update_post_tails.py:3021` `_make_admin` is the get-or-create shape that works; this file's prelude carries its own copy. `Site.admins()` also short-circuits on `hasattr(g, 'admin_ids')`; `tests/conftest.py` clears `flask.g` before every test and nothing here sets it, so the JOIN arm always runs.
16. `make_community` hardcodes `instance_id=1` and `user_id=1`. `tests/conftest.py:143` truncates with `RESTART IDENTITY`, so the first `make_instance` in a test gets id 1 (fact 89). Seed pairwise-distinct ids and assert `len({...}) == n` wherever an assertion is id-valued.
17. `Post.language_id`, `User.language_id` and `Post.timezone` accept `None`. `Poll.end_poll`, `Event.start` and `Event.end` are `db.DateTime` — `TIMESTAMP WITHOUT TIME ZONE` (`app/models.py:3782`, `:3841-3842`).
18. `app/__init__.py:81` builds `SQLAlchemy(session_options={"autoflush": False}, ...)` with no `expire_on_commit`, so it is at SQLAlchemy's default `True`. A commit inside `edit_post` expires held objects and the next attribute access re-loads. Where no commit intervenes, use `db.session.expire(obj)` before asserting — a read through the writing Session cannot otherwise distinguish committed from pending.

---

## File structure

| File | Responsibility | Task |
|---|---|---|
| `tests/test_shared_post_edit.py` | **Create.** The whole sub-project's tests: prelude (WTForms double, seeding helper, input builders) then six clusters in source order. | 1-7 |
| `app/shared/post.py:582` | **Modify, one line in place.** D287. | 4 |
| `app/shared/post.py:577` | **Modify, one line in place.** D292. | 5 |
| `tests/test_ap_update_post_tails.py:2889`, `:3332` | **Modify prose only.** Both name `:577` as an open D292 site; it is fixed in Task 5. | 5 |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` | **Append.** D297 onward; D287 and D292 marked fixed. | 8 |
| `tests/README.md` | **Append.** New harness facts. | 8 |
| `coverage_floors.ini` | **Add one line.** `app/shared/post.py` has no entry today. | 8 |

---

### Task 1: The file, its prelude, and the SRC_API scalar reads

**Files:**
- Create: `tests/test_shared_post_edit.py`

**Interfaces:**
- Consumes: nothing.
- Produces: the prelude every later task uses —
  - `_Field(data)` — one WTForms field; only `.data` is read.
  - `_OMIT` — sentinel; a key mapped to it is left off the form object entirely.
  - `_web_form(**over)` → object with the `SRC_WEB` shape. Defaults are complete; `over` replaces. A value of `None` sets the *attribute* to `None` (a falsy field), a value of `_OMIT` deletes the attribute, anything else is wrapped in `_Field`.
  - `_api_input(**over)` → `dict` with the `SRC_API` shape. Defaults are complete; `over` replaces. A value of `_OMIT` removes the key.
  - `_seed(url=None, domain_name=None, notify_mods=False, notify_admins=False)` → `SimpleNamespace(instance, user, community, post, domain)`. `domain` is `None` unless `domain_name` is given.

- [ ] **Step 1: Write the module docstring and prelude**

Create `tests/test_shared_post_edit.py` with exactly this content:

```python
"""`edit_post`'s source dispatch and its suspicious-domain block.

`app/shared/post.py:250-754` is the LOCAL post editor. Its federated twin,
`update_post_from_activity`, was taken to zero uncovered statements by
sub-project 17; two of that sub-project's findings (D287 and D292) are
statements about how the two copies disagree, and both were found by reading
this function without ever executing it. This file executes it.

SCOPE. Two clusters, 175 uncovered when this file was started:

  - the source dispatch, :251-383 -- SRC_API 76, SRC_WEB 61. Its lower bound is
    the function's own comment at :384, "beyond this point do not use the input
    variable as it can be either a dict or a form object!". Note that :388 and
    :390 read `input.sticky.data` and `input.nsfl.data` BELOW that marker,
    behind the conditional expression `False if src == SRC_API else ...` -- so
    the marker describes an intent the code does not quite keep, and the SRC_WEB
    double below carries `sticky` and `nsfl` for that reason.
  - the domain and notify block, :565-598 -- 38. This is the local copy of
    `app/activitypub/util.py:3508-3545`.

ENTRY is a direct call. `edit_post` opens with `if not user:` on BOTH branches
(:252 for SRC_API, :316 for SRC_WEB), so passing `user=` skips
`authorise_api_user` and `current_user` alike. No request context, no login, no
auth token is needed anywhere in this file.

THE HEAD REQUEST. `is_image_url` (app/utils.py:247) issues an httpx HEAD through
`mime_type_using_head`, which catches only `httpx.HTTPError` and
`httpx.InvalidURL`. respx's unmatched-request error is neither, so it escapes
into the test. `edit_post` can call `is_image_url` twice -- once at :410 on
`post.url`, once at :601 on the new `url` -- so every seeded post here has
`url=None`, leaving exactly one HEAD to register.

THE IMAGE BOUNDARY. Under this harness `make_image_sizes`
(app/activitypub/util.py:1724) EXECUTES rather than enqueues: Celery is eager
(tests/conftest.py:106). The technique used here is a bodiless 404 on the
source url. `make_image_sizes_async:1748` retries only when
'/api/v3/image_proxy' is in the url, and :1759 proceeds only on status 200;
neither holds, so it returns having done nothing. The OTHER technique in this
suite -- turning off `cache_remote_images_locally` -- does NOT work here: that
setting gates only the Event block's call in app/activitypub/util.py, and
:614/:616 call `make_image_sizes` directly.

`http_mock` is `assert_all_called=True` (tests/conftest.py:288-295), so each
test registers exactly the routes its own path reaches -- the crash tests below
register the HEAD only, because they raise at :607 before the GET.

WHICH COMMIT RAISES. `:459 db.session.commit()` runs only inside
`if not from_scratch:` (:421). Tests that need a durable pre-notify commit
therefore pass `from_scratch=False` and change the url, which is also what sets
`url_changed = True` at :436 and opens the :565 gate.
"""

import pytest
from datetime import datetime

from app import db
from app.constants import (
    NOTIF_REPORT, POST_TYPE_ARTICLE, POST_TYPE_EVENT, POST_TYPE_IMAGE,
    POST_TYPE_LINK, POST_TYPE_POLL, POST_TYPE_VIDEO, SRC_API, SRC_WEB,
)
from app.constants import ROLE_ADMIN
from app.models import Event, File, Notification, Poll, PollChoice, Role
from app.shared.post import edit_post
from tests.factories import (
    make_community, make_community_flair, make_community_member, make_domain,
    make_instance, make_post, make_user,
)

from types import SimpleNamespace


_OMIT = object()
"""Sentinel: a key mapped to this is left OUT of the built input entirely.

Needed because absence and falsity are different branches here. :265
`if 'tags' in input:` and :340 `hasattr(input, 'image_alt_text')` test
presence; :263 `if image_alt_text is None:` and :333 `if input.flair:` test
value. A builder that could only set values could not reach half the arms.
"""


class _Field:
    """One WTForms field. edit_post's SRC_WEB branch reads only `.data`."""

    def __init__(self, data):
        self.data = data


def _web_form(**over):
    """The SRC_WEB shape, as a plain object carrying `_Field` attributes.

    Defaults are a complete POST_TYPE_LINK submission. `over` replaces:
      value        -> wrapped in _Field
      None         -> the ATTRIBUTE is set to None, so `if input.flair:` (:333),
                      `if input.finish_in:` (:354) and `input.image_alt_text`
                      (:340) take their false arms
      _OMIT        -> the attribute is not set at all, so `hasattr` (:340) is
                      False

    `sticky` and `nsfl` are present because :388 and :390 read them below the
    :384 marker.
    """
    fields = {
        'title': 'a title', 'body': 'a body',
        'link_url': 'https://example.com/page', 'video_url': 'https://example.com/v.mp4',
        'nsfw': False, 'ai_generated': False, 'notify_author': True,
        'language_id': None, 'tags': '', 'flair': '',
        'scheduled_for': None, 'repeat': None, 'timezone': 'UTC',
        'image_alt_text': '', 'sticky': False, 'nsfl': False,
        'mode': 'single', 'local_only': False, 'finish_in': '3d',
        'event_timezone': 'UTC',
        'start_datetime': datetime(2030, 1, 1, 9, 0), 'end_datetime': datetime(2030, 1, 1, 10, 0),
        'max_attendees': 10, 'online': False, 'online_link': '',
        'join_mode': 'free', 'irl_address': '1 Road', 'irl_city': 'Town', 'irl_country': 'Nowhere',
    }
    for i in range(1, 16):
        fields[f'choice_{i}'] = ''
    fields.update(over)

    form = SimpleNamespace()
    for name, value in fields.items():
        if value is _OMIT:
            continue
        setattr(form, name, None if value is None else _Field(value))
    return form


def _api_input(**over):
    """The SRC_API shape: a plain dict. `_OMIT` removes a key."""
    data = {
        'title': 'a title', 'body': 'a body', 'url': None,
        'nsfw': False, 'ai_generated': False, 'notify_author': True,
        'language_id': None,
    }
    data.update(over)
    return {k: v for k, v in data.items() if v is not _OMIT}


def _seed(url=None, domain_name=None, notify_mods=False, notify_admins=False):
    """instance/user/community/post, and optionally a Domain with notify flags.

    The post is seeded with url=None on purpose: :403 `if post.url:` guards a
    second `is_image_url` call at :410, and every extra call is another HEAD
    route this file would have to register.

    make_community hardcodes instance_id=1 and user_id=1, and
    tests/conftest.py:143 truncates with RESTART IDENTITY, so the first
    make_instance here is id 1 and the first make_user is id 1.
    """
    instance = make_instance('test.piefed.local', software='piefed')
    user = make_user(instance, 'editor', local=True)
    community = make_community('c1')
    post = make_post(community, user, ap_id='https://test.piefed.local/post/1')
    post.url = url
    db.session.commit()

    domain = None
    if domain_name:
        domain = make_domain(domain_name)
        domain.notify_mods = notify_mods
        domain.notify_admins = notify_admins
        db.session.commit()

    return SimpleNamespace(instance=instance, user=user, community=community,
                           post=post, domain=domain)


def _make_admin(user):
    """Make `user` an admin as `Site.admins()` counts them.

    `Site.admins()` (app/models.py) takes its JOIN arm here:
    tests/conftest.py clears `flask.g` before every test and nothing in this
    file sets `admin_ids`, so `hasattr(g, 'admin_ids')` is False.

    A ROLE ROW IS REQUIRED EVEN FOR USER 1. `.join(user_role)` is an INNER join,
    so a user with no row there is dropped before the `or_` is evaluated --
    `User.id == 1` cannot rescue a user the join already eliminated. That is
    D295, and it is why `grant_permission` (which creates a Role with an AUTO
    id) is not enough: the filter is on `user_role.c.role_id == ROLE_ADMIN`, so
    the role's id must BE ROLE_ADMIN. The get-or-create below is the same shape
    as tests/test_ap_update_post_tails.py:3021.
    """
    role = db.session.get(Role, ROLE_ADMIN)
    if role is None:
        role = Role(id=ROLE_ADMIN, name=f'role-{ROLE_ADMIN}', weight=0)
        db.session.add(role)
        db.session.commit()
    user.roles.append(role)
    db.session.commit()
```

- [ ] **Step 2: Run the file to verify it collects and the prelude imports resolve**

Run: `./run_tests.sh tests/test_shared_post_edit.py -q`
Expected: `no tests ran` — collection succeeds, zero tests. Any `ImportError` here is a real defect in the import list; fix it before proceeding.

- [ ] **Step 3: Commit the prelude**

```bash
git add tests/test_shared_post_edit.py
git commit -F <message-file>
```

Message subject: `test: open tests/test_shared_post_edit.py with the edit_post harness prelude`

Throughout this plan, `<message-file>` means a file you write under the SDD workspace this task was dispatched with — never `-m`, because these messages contain backticks and backticks inside a double-quoted `-m` are command substitution.

- [ ] **Step 4: Write the SRC_API scalar-read tests**

Append a banner and these tests. They cover `:251-264` and the two presence-guarded reads.

```python
# ---------------------------------------------------------------------------
# SRC_API: the scalar reads, :251-264
# ---------------------------------------------------------------------------


def test_api_branch_reads_every_scalar_and_strips_the_title(db_session):
    """:251-260. The dict shape, entered with user= so :253 never runs.

    Title is `.strip()`ed at :254; the leading and trailing spaces here are the
    witness that :254 ran rather than a bare assignment.
    """
    s = _seed()
    edit_post(_api_input(title='  spaced  ', body='new body', nsfw=True,
                         ai_generated=True, notify_author=False),
              s.post, POST_TYPE_ARTICLE, SRC_API, user=s.user)

    assert s.post.title == 'spaced'
    assert s.post.body == 'new body'
    assert s.post.nsfw is True
    assert s.post.ai_generated is True
    assert s.post.notify_author is False


def test_api_branch_falls_back_to_the_users_timezone_when_the_key_is_absent(db_session):
    """:261, false arm of `'timezone' in input`."""
    s = _seed()
    s.user.timezone = 'Pacific/Auckland'
    db.session.commit()

    edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API, user=s.user)

    assert s.post.timezone == 'Pacific/Auckland'


def test_api_branch_takes_the_supplied_timezone_when_the_key_is_present(db_session):
    """:261, true arm. The user's timezone differs, so a pass-through of the
    user value would not be distinguishable from the input value."""
    s = _seed()
    s.user.timezone = 'Pacific/Auckland'
    db.session.commit()

    edit_post(_api_input(timezone='Europe/Berlin'), s.post, POST_TYPE_ARTICLE,
              SRC_API, user=s.user)

    assert s.post.timezone == 'Europe/Berlin'


@pytest.mark.parametrize('supplied,expected', [
    (_OMIT, ''),     # :262 false arm -- key absent, never reaches :263
    (None, ''),      # :262 true arm, then :263 true arm -- present but None
    ('alt', 'alt'),  # :262 true arm, :263 false arm
])
def test_api_branch_normalises_absent_and_null_alt_text_to_empty(
        db_session, http_mock, supplied, expected):
    """:262-264. Three arms across two guards, and they are NOT the same arm:
    an absent key never reaches :263, a null one does.

    `image_alt_text` has an observable only on the image path, where :666
    `file.alt_text = image_alt_text` writes it to a real File row. Without a url
    the local is carried and dropped, and the three cases would be
    indistinguishable -- so this drives a .png and reads the File back. That
    also shows what :263 is FOR: without it, `None` would reach :666 and null
    the column rather than clearing it to ''.
    """
    s = _seed()
    http_mock.head('https://example.com/pic.png').respond(
        200, headers={'Content-Type': 'image/png'})
    http_mock.get('https://example.com/pic.png').respond(404)

    edit_post(_api_input(image_alt_text=supplied, url='https://example.com/pic.png'),
              s.post, POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=True)

    db.session.expire(s.post)
    file = File.query.get(s.post.image_id)
    assert file is not None
    assert file.alt_text == expected
```

- [ ] **Step 5: Run them**

Run: `./run_tests.sh tests/test_shared_post_edit.py -q`
Expected: 7 passed.

`test_api_branch_normalises_absent_and_null_alt_text_to_empty` reaches `:603`'s `(uploaded_file and type == POST_TYPE_IMAGE) or type == POST_TYPE_LINK` — the second disjunct is why it drives `POST_TYPE_LINK`. If `file.alt_text` comes back `None` rather than `''` for the `_OMIT` and `None` cases, that is a real result: report it and adjust the parametrize table to what ran, keeping the docstring's explanation of which arm each case takes.

- [ ] **Step 6: Write the tags and flair tests**

```python
# ---------------------------------------------------------------------------
# SRC_API: tags and flair, :265-282
# ---------------------------------------------------------------------------


def test_api_branch_parses_a_tag_string_when_the_key_is_present(db_session):
    """:265-266, true arm."""
    s = _seed()
    edit_post(_api_input(tags='alpha,beta'), s.post, POST_TYPE_ARTICLE, SRC_API,
              user=s.user)

    db.session.expire(s.post)
    assert sorted(t.name for t in s.post.tags) == ['alpha', 'beta']


def test_api_branch_leaves_tags_empty_when_the_key_is_absent(db_session):
    """:267-268, false arm. The post starts with a tag so an empty result is a
    clearing rather than a no-op -- :454 `post.tags.clear()` runs first."""
    s = _seed()
    edit_post(_api_input(tags='pre'), s.post, POST_TYPE_ARTICLE, SRC_API, user=s.user)
    db.session.expire(s.post)
    assert [t.name for t in s.post.tags] == ['pre']

    edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API, user=s.user)
    db.session.expire(s.post)
    assert list(s.post.tags) == []


def test_api_branch_parses_a_flair_string_when_the_flair_key_is_present(db_session):
    """:269-270, first arm of the three-way flair dispatch.

    `flairs_from_string(input['flair'], post.community_id)`
    (app/community/util.py:381) resolves each comma-separated name through
    `find_flair(name, community_id)`, so the flair must belong to THIS post's
    community or the lookup returns nothing and the test passes vacuously.
    """
    s = _seed()
    flair = make_community_flair(s.community, name='news')

    edit_post(_api_input(flair='news'), s.post, POST_TYPE_ARTICLE, SRC_API, user=s.user)

    db.session.expire(s.post)
    assert [f.id for f in s.post.flair] == [flair.id]


def test_api_branch_accepts_a_bare_integer_flair_id(db_session):
    """:271-275, the `isinstance(flair_id, int)` true arm -- the RSS shape."""
    s = _seed()
    flair = make_community_flair(s.community, name='rss')

    edit_post(_api_input(flair_id=flair.id), s.post, POST_TYPE_ARTICLE, SRC_API,
              user=s.user)

    db.session.expire(s.post)
    assert [f.id for f in s.post.flair] == [flair.id]


def test_api_branch_accepts_a_list_of_flair_ids(db_session):
    """:276-277, the isinstance false arm."""
    s = _seed()
    one = make_community_flair(s.community, name='one')
    two = make_community_flair(s.community, name='two')
    assert len({one.id, two.id}) == 2

    edit_post(_api_input(flair_id=[one.id, two.id]), s.post, POST_TYPE_ARTICLE,
              SRC_API, user=s.user)

    db.session.expire(s.post)
    assert sorted(f.id for f in s.post.flair) == sorted([one.id, two.id])


def test_api_branch_drops_flair_ids_that_match_no_row(db_session):
    """:278, the list comprehension's filter. `CommunityFlair.query.get()`
    returns None for a missing id, and :278 is what stops that None reaching
    post.flair."""
    s = _seed()
    real = make_community_flair(s.community, name='real')
    missing = real.id + 1000

    edit_post(_api_input(flair_id=[real.id, missing]), s.post, POST_TYPE_ARTICLE,
              SRC_API, user=s.user)

    db.session.expire(s.post)
    assert [f.id for f in s.post.flair] == [real.id]


def test_api_branch_leaves_flair_empty_when_flair_id_is_falsy(db_session):
    """:271's second conjunct false, so :279-280. An empty list is present but
    falsy -- distinct from the key being absent."""
    s = _seed()
    edit_post(_api_input(flair_id=[]), s.post, POST_TYPE_ARTICLE, SRC_API, user=s.user)

    db.session.expire(s.post)
    assert list(s.post.flair) == []


def test_api_branch_leaves_flair_empty_when_neither_key_is_present(db_session):
    """:279-280 reached with both `'flair' in input` and `'flair_id' in input`
    false."""
    s = _seed()
    edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API, user=s.user)

    db.session.expire(s.post)
    assert list(s.post.flair) == []
```

- [ ] **Step 7: Run them and fix the seeding order**

Run: `./run_tests.sh tests/test_shared_post_edit.py -q`
Expected: 15 passed.

`flairs_from_string` lives at `app/community/util.py:381`, not in `app/utils.py`. It calls `find_flair(name, community_id)`, so every flair a test expects to match must be created on the post's own community — which is what `_seed()` returns as `s.community`.

- [ ] **Step 8: Commit**

```bash
git add tests/test_shared_post_edit.py
git commit -F <message-file>
```

Message subject: `test: cover edit_post's SRC_API scalar, tag and flair reads`

---

### Task 2: SRC_API event and poll parsing, and the D297 measurement

**Files:**
- Modify: `tests/test_shared_post_edit.py` (append one cluster)

**Interfaces:**
- Consumes: `_seed`, `_api_input`, `_OMIT` from Task 1.
- Produces: a measured statement about what `:310`'s aware `datetime` does when written to `Poll.end_poll`, for Task 8 to register as D297.

- [ ] **Step 1: Write the event-parsing tests**

```python
# ---------------------------------------------------------------------------
# SRC_API: event and poll parsing, :284-314
# ---------------------------------------------------------------------------


def test_api_event_start_string_is_parsed_and_stripped_of_its_offset(db_session):
    """:288-290. `.replace(tzinfo=None)` AFTER fromisoformat means a peer offset
    is DISCARDED rather than converted: 09:00+05:00 is stored as 09:00, not as
    04:00 UTC.

    The federated copy does the opposite. `app/activitypub/util.py:3375-3376`
    parses startTime/endTime with a bare `fromisoformat` and keeps the value
    aware, so the two editors disagree about what a peer's event time means.
    """
    s = _seed()
    edit_post(_api_input(event={'start': '2030-06-01T09:00:00+05:00',
                                'end': '2030-06-01T10:00:00+05:00'}),
              s.post, POST_TYPE_EVENT, SRC_API, user=s.user)

    event = Event.query.filter_by(post_id=s.post.id).first()
    assert event is not None
    assert event.start == datetime(2030, 6, 1, 9, 0)
    assert event.start.tzinfo is None


def test_api_event_start_that_is_already_a_datetime_passes_through(db_session):
    """:291-292, the isinstance false arm -- the self-assignment."""
    s = _seed()
    naive = datetime(2030, 6, 1, 9, 0)
    edit_post(_api_input(event={'start': naive, 'end': datetime(2030, 6, 1, 10, 0)}),
              s.post, POST_TYPE_EVENT, SRC_API, user=s.user)

    event = Event.query.filter_by(post_id=s.post.id).first()
    assert event.start == naive


def test_api_event_end_is_skipped_when_the_key_is_absent(db_session):
    """:293, first conjunct false."""
    s = _seed()
    edit_post(_api_input(event={'start': '2030-06-01T09:00:00Z'}),
              s.post, POST_TYPE_EVENT, SRC_API, user=s.user)

    event = Event.query.filter_by(post_id=s.post.id).first()
    assert event.start == datetime(2030, 6, 1, 9, 0)


def test_api_event_end_is_skipped_when_the_value_is_falsy(db_session):
    """:293, second conjunct false. Present-but-None is a different arm from
    absent, and neither reaches :294."""
    s = _seed()
    edit_post(_api_input(event={'start': '2030-06-01T09:00:00Z', 'end': None}),
              s.post, POST_TYPE_EVENT, SRC_API, user=s.user)

    event = Event.query.filter_by(post_id=s.post.id).first()
    assert event.start == datetime(2030, 6, 1, 9, 0)


def test_api_event_end_that_is_already_a_datetime_passes_through(db_session):
    """:296-297, the isinstance false arm for `end`."""
    s = _seed()
    naive = datetime(2030, 6, 1, 10, 0)
    edit_post(_api_input(event={'start': '2030-06-01T09:00:00Z', 'end': naive}),
              s.post, POST_TYPE_EVENT, SRC_API, user=s.user)

    event = Event.query.filter_by(post_id=s.post.id).first()
    assert event.end == naive


def test_api_event_block_is_skipped_when_there_is_no_event_key(db_session):
    """:285-286, false arm. `input.get('event', None)` is the API dict's only
    `.get` -- the surrounding reads all subscript."""
    s = _seed()
    edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API, user=s.user)

    assert Event.query.filter_by(post_id=s.post.id).first() is None
```

- [ ] **Step 2: Run them**

Run: `./run_tests.sh tests/test_shared_post_edit.py -q -k event`
Expected: 6 passed.

- [ ] **Step 3: Write the poll-parsing tests, including the D297 probe**

```python
def test_api_poll_defaults_every_field_when_only_choices_are_given(db_session):
    """:303-307. All three `.get` defaults at once."""
    s = _seed()
    edit_post(_api_input(poll={'choices': [{'choice_text': 'yes', 'sort_order': 1},
                                           {'choice_text': 'no', 'sort_order': 2}]}),
              s.post, POST_TYPE_POLL, SRC_API, user=s.user)

    poll = Poll.query.filter_by(post_id=s.post.id).first()
    assert poll is not None
    assert poll.mode == 'single'
    assert poll.local_only is False
    assert sorted(c.choice_text for c in
                  PollChoice.query.filter_by(post_id=s.post.id).all()) == ['no', 'yes']


def test_api_poll_takes_supplied_mode_and_local_only(db_session):
    """:304-305, the non-default arms."""
    s = _seed()
    edit_post(_api_input(poll={'mode': 'multiple', 'local_only': True,
                               'choices': [{'choice_text': 'a', 'sort_order': 1}]}),
              s.post, POST_TYPE_POLL, SRC_API, user=s.user)

    poll = Poll.query.filter_by(post_id=s.post.id).first()
    assert poll.mode == 'multiple'
    assert poll.local_only is True


def test_api_poll_end_is_skipped_when_the_key_is_absent(db_session):
    """:308, first conjunct false."""
    s = _seed()
    edit_post(_api_input(poll={'choices': [{'choice_text': 'a', 'sort_order': 1}]}),
              s.post, POST_TYPE_POLL, SRC_API, user=s.user)

    assert Poll.query.filter_by(post_id=s.post.id).first().end_poll is None


def test_api_poll_end_is_skipped_when_the_value_is_falsy(db_session):
    """:308, second conjunct false."""
    s = _seed()
    edit_post(_api_input(poll={'end_poll': None,
                               'choices': [{'choice_text': 'a', 'sort_order': 1}]}),
              s.post, POST_TYPE_POLL, SRC_API, user=s.user)

    assert Poll.query.filter_by(post_id=s.post.id).first().end_poll is None


def test_api_poll_end_that_is_already_a_datetime_passes_through(db_session):
    """:311-312, the isinstance false arm."""
    s = _seed()
    naive = datetime(2030, 6, 1, 12, 0)
    edit_post(_api_input(poll={'end_poll': naive,
                               'choices': [{'choice_text': 'a', 'sort_order': 1}]}),
              s.post, POST_TYPE_POLL, SRC_API, user=s.user)

    assert Poll.query.filter_by(post_id=s.post.id).first().end_poll == naive


def test_api_poll_end_string_is_parsed_and_KEEPS_its_offset(db_session):
    """:309-310. THE D297 PROBE, and the reason this cluster exists.

    :310 is `datetime.fromisoformat(...replace('Z', '+00:00'))` with NO
    `.replace(tzinfo=None)` -- unlike its two siblings at :290 and :295. So it
    yields an AWARE datetime, and writes it into `Poll.end_poll`, which is
    `db.Column(db.DateTime)` -- TIMESTAMP WITHOUT TIME ZONE (app/models.py:3782).

    The federated copy does not parse at all: `app/activitypub/util.py:3338` is
    `poll.end_poll = request_json['object']['endTime']`, the raw string (D290).

    This test records what the write ACTUALLY does, measured, not argued.
    """
    s = _seed()
    edit_post(_api_input(poll={'end_poll': '2030-06-01T12:00:00+05:00',
                               'choices': [{'choice_text': 'a', 'sort_order': 1}]}),
              s.post, POST_TYPE_POLL, SRC_API, user=s.user)

    stored = Poll.query.filter_by(post_id=s.post.id).first().end_poll
    assert stored.tzinfo is None, 'psycopg2 strips tzinfo on the way into a naive column'
    assert stored == datetime(2030, 6, 1, 12, 0)


def test_api_poll_block_is_skipped_when_there_is_no_poll_key(db_session):
    """:300-301, false arm."""
    s = _seed()
    edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API, user=s.user)

    assert Poll.query.filter_by(post_id=s.post.id).first() is None
```

- [ ] **Step 4: Run the poll tests and MEASURE D297**

Run: `./run_tests.sh tests/test_shared_post_edit.py -q -k poll`
Expected: 7 passed **or** `test_api_poll_end_string_is_parsed_and_KEEPS_its_offset` fails.

**If it fails, that is the measurement, not a bug in the test.** Record the observed value and rewrite the two assertions to match reality, keeping the docstring's explanation. Three outcomes are possible and all three are findings:

| Observed | Meaning |
|---|---|
| `tzinfo is None` and hour `12` | psycopg2 drops the offset without converting — same lossy outcome as `:290`/`:295`, reached by a different route |
| `tzinfo is None` and hour `7` | the driver converts to UTC — the local editor and the federated editor now disagree about the *instant*, not just the representation |
| the call raises | writing an aware value into a naive column is an error the API branch can trigger from peer input |

Write the observed row into the task report as **the D297 measurement**, quoted exactly. Do not describe it as expected or unsurprising; report what ran.

- [ ] **Step 5: Commit**

```bash
git add tests/test_shared_post_edit.py
git commit -F <message-file>
```

Message subject: `test: cover edit_post's SRC_API event and poll parsing, and measure the end_poll write`

---

### Task 3: The SRC_WEB branch

**Files:**
- Modify: `tests/test_shared_post_edit.py` (append one cluster)

**Interfaces:**
- Consumes: `_seed`, `_web_form`, `_Field`, `_OMIT` from Task 1.
- Produces: nothing later tasks need.

- [ ] **Step 1: Write the url-type dispatch tests**

```python
# ---------------------------------------------------------------------------
# SRC_WEB: the form shape, :315-383
# ---------------------------------------------------------------------------


def test_web_branch_takes_the_link_url_for_a_link_post(db_session, http_mock):
    """:320-321. `.strip()` at :321 is proved by the padding.

    A url means :601 `is_image_url(url)` fires one HEAD. The seeded post has
    url=None so :410's second call never happens.
    """
    s = _seed()
    http_mock.head('https://example.com/doc').respond(200, headers={'Content-Type': 'text/html'})
    http_mock.get('https://example.com/doc').respond(200, html='<html></html>')

    edit_post(_web_form(link_url='  https://example.com/doc  '), s.post,
              POST_TYPE_LINK, SRC_WEB, user=s.user)

    assert s.post.url == 'https://example.com/doc'


def test_web_branch_takes_the_video_url_for_a_video_post(db_session, http_mock):
    """:322-323."""
    s = _seed()
    http_mock.head('https://example.com/clip.mp4').respond(200, headers={'Content-Type': 'video/mp4'})
    http_mock.get('https://example.com/clip.mp4').respond(200, html='')

    edit_post(_web_form(video_url='  https://example.com/clip.mp4  '), s.post,
              POST_TYPE_VIDEO, SRC_WEB, user=s.user)

    assert s.post.url == 'https://example.com/clip.mp4'
    assert s.post.type == POST_TYPE_VIDEO


def test_web_branch_keeps_the_existing_url_for_an_image_edit(db_session, http_mock):
    """:324-325, BOTH conjuncts true: POST_TYPE_IMAGE and not from_scratch.

    This is the one arm that reads `post.url` rather than the form, so the post
    is seeded WITH a url here -- which means two HEADs, one at :410 and one at
    :601, both to the same address. respx matches a route repeatedly, so one
    registration covers both.
    """
    s = _seed(url='https://example.com/pic.png')
    http_mock.head('https://example.com/pic.png').respond(200, headers={'Content-Type': 'image/png'})
    http_mock.get('https://example.com/pic.png').respond(404)

    edit_post(_web_form(), s.post, POST_TYPE_IMAGE, SRC_WEB, user=s.user,
              from_scratch=False)

    assert s.post.url == 'https://example.com/pic.png'


def test_web_branch_clears_the_url_for_an_image_created_from_scratch(db_session):
    """:324's second conjunct false, so :326-327 sets url=None. No HEAD, because
    :565 `if url and ...` and :601 both short-circuit on a falsy url."""
    s = _seed()
    edit_post(_web_form(), s.post, POST_TYPE_IMAGE, SRC_WEB, user=s.user,
              from_scratch=True)

    assert s.post.url is None


def test_web_branch_clears_the_url_for_an_article(db_session):
    """:326-327 via the type dispatch falling all the way through."""
    s = _seed()
    edit_post(_web_form(), s.post, POST_TYPE_ARTICLE, SRC_WEB, user=s.user)

    assert s.post.url is None
```

- [ ] **Step 2: Write the remaining scalar, flair and alt-text tests**

```python
def test_web_branch_reads_every_remaining_scalar_from_form_data(db_session):
    """:318-319, :328-331, :337-339, and :388/:390 below the :384 marker.

    :388 needs the user to be a moderator, owner or admin of the community for
    `post.sticky` to be written at all; without that the sticky read never runs.
    """
    s = _seed()
    make_community_member(s.user, s.community, is_moderator=True)

    edit_post(_web_form(title='  web title  ', body='web body', nsfw=True,
                        ai_generated=True, notify_author=False, timezone='Europe/Berlin',
                        sticky=True, nsfl=True),
              s.post, POST_TYPE_ARTICLE, SRC_WEB, user=s.user)

    assert s.post.title == 'web title'
    assert s.post.body == 'web body'
    assert s.post.nsfw is True
    assert s.post.ai_generated is True
    assert s.post.notify_author is False
    assert s.post.timezone == 'Europe/Berlin'
    assert s.post.sticky is True
    assert s.post.nsfl is True


def test_web_branch_parses_tags_from_the_form_string(db_session):
    """:332. Unconditional here -- the API branch guards the same call at :265."""
    s = _seed()
    edit_post(_web_form(tags='alpha,beta'), s.post, POST_TYPE_ARTICLE, SRC_WEB,
              user=s.user)

    db.session.expire(s.post)
    assert sorted(t.name for t in s.post.tags) == ['alpha', 'beta']


def test_web_branch_reads_flair_when_the_field_object_is_truthy(db_session):
    """:333-334, true arm."""
    s = _seed()
    flair = make_community_flair(s.community, name='news')

    edit_post(_web_form(flair=[flair.id]), s.post, POST_TYPE_ARTICLE,
              SRC_WEB, user=s.user)

    db.session.expire(s.post)
    assert [f.id for f in s.post.flair] == [flair.id]


def test_web_branch_leaves_flair_empty_when_the_field_object_is_falsy(db_session):
    """:335-336. The guard at :333 tests the FIELD, not `.data`, so a form
    without a flair field at all takes this arm -- `_web_form(flair=None)` sets
    the attribute to None rather than to a _Field."""
    s = _seed()
    edit_post(_web_form(flair=None), s.post, POST_TYPE_ARTICLE, SRC_WEB, user=s.user)

    db.session.expire(s.post)
    assert list(s.post.flair) == []


@pytest.mark.parametrize('image_alt_text,expected_reached', [
    ('alt words', True),   # :340 both conjuncts true
    (None, False),         # hasattr true, field falsy -- second conjunct false
    (_OMIT, False),        # hasattr false -- first conjunct false
])
def test_web_branch_alt_text_needs_both_hasattr_and_a_truthy_field(
        db_session, http_mock, image_alt_text, expected_reached):
    """:340. Two conjuncts, three arms, and the witness is a real File.

    `post.image` is set only on the image path, so this drives a .png url and
    reads back `File.alt_text` written at :666 `if url and post.image:`.
    """
    s = _seed()
    http_mock.head('https://example.com/pic.png').respond(200, headers={'Content-Type': 'image/png'})
    http_mock.get('https://example.com/pic.png').respond(404)

    edit_post(_web_form(link_url='https://example.com/pic.png',
                        image_alt_text=image_alt_text),
              s.post, POST_TYPE_LINK, SRC_WEB, user=s.user)

    db.session.expire(s.post)
    file = File.query.get(s.post.image_id)
    assert file is not None
    assert (file.alt_text == 'alt words') is expected_reached
```

- [ ] **Step 3: Write the form poll and event tests**

```python
def test_web_branch_collects_non_empty_poll_choices_in_form_order(db_session):
    """:343-353. The loop runs 1..15 and :347 drops the empty slots, so a poll
    filled at 1, 3 and 15 proves both arms of the guard and the loop's extent."""
    s = _seed()
    edit_post(_web_form(choice_1='first', choice_3='third', choice_15='last'),
              s.post, POST_TYPE_POLL, SRC_WEB, user=s.user)

    rows = PollChoice.query.filter_by(post_id=s.post.id).order_by(PollChoice.sort_order).all()
    assert [(r.choice_text, r.sort_order) for r in rows] == [
        ('first', 1), ('third', 3), ('last', 15)]


def test_web_branch_strips_poll_choice_text(db_session):
    """:348's `.strip()`."""
    s = _seed()
    edit_post(_web_form(choice_1='  padded  '), s.post, POST_TYPE_POLL, SRC_WEB,
              user=s.user)

    rows = PollChoice.query.filter_by(post_id=s.post.id).all()
    assert [r.choice_text for r in rows] == ['padded']


def test_web_branch_sets_the_poll_end_when_finish_in_is_present(db_session):
    """:354-355, true arm."""
    s = _seed()
    edit_post(_web_form(choice_1='a', finish_in='3d'), s.post, POST_TYPE_POLL,
              SRC_WEB, user=s.user)

    assert Poll.query.filter_by(post_id=s.post.id).first().end_poll is not None


def test_web_branch_leaves_the_poll_end_unset_when_finish_in_is_absent(db_session):
    """:354, false arm. The guard tests the FIELD, so finish_in=None takes it."""
    s = _seed()
    edit_post(_web_form(choice_1='a', finish_in=None), s.post, POST_TYPE_POLL,
              SRC_WEB, user=s.user)

    assert Poll.query.filter_by(post_id=s.post.id).first().end_poll is None


def test_web_branch_leaves_poll_data_none_for_a_non_poll_type(db_session):
    """:356-357."""
    s = _seed()
    edit_post(_web_form(), s.post, POST_TYPE_ARTICLE, SRC_WEB, user=s.user)

    assert Poll.query.filter_by(post_id=s.post.id).first() is None


def test_web_branch_converts_event_times_from_the_forms_timezone_to_utc(db_session):
    """:360-380. THE CONTRAST WITH :290.

    The web branch attaches the form's timezone at :362-363 and CONVERTS with
    `.astimezone(ZoneInfo('UTC'))` at :364-365 before stripping tzinfo at
    :368-369. The API branch at :290 strips WITHOUT converting. So the same wall
    clock submitted through the two branches lands on two different instants.

    Europe/Berlin is UTC+2 on 2030-06-01, so 09:00 local is 07:00 UTC.
    """
    s = _seed()
    edit_post(_web_form(event_timezone='Europe/Berlin',
                        start_datetime=datetime(2030, 6, 1, 9, 0),
                        end_datetime=datetime(2030, 6, 1, 10, 0)),
              s.post, POST_TYPE_EVENT, SRC_WEB, user=s.user)

    event = Event.query.filter_by(post_id=s.post.id).first()
    assert event.start == datetime(2030, 6, 1, 7, 0)
    assert event.end == datetime(2030, 6, 1, 8, 0)
    assert event.start.tzinfo is None


def test_web_branch_carries_every_remaining_event_field(db_session):
    """:370-379, including the nested location dict."""
    s = _seed()
    edit_post(_web_form(max_attendees=42, online=True,
                        online_link='https://meet.example/x', join_mode='request',
                        irl_address='9 Lane', irl_city='Ville', irl_country='Elsewhere'),
              s.post, POST_TYPE_EVENT, SRC_WEB, user=s.user)

    event = Event.query.filter_by(post_id=s.post.id).first()
    assert event.max_attendees == 42
    assert event.online is True
    assert event.online_link == 'https://meet.example/x'
    assert event.join_mode == 'request'


def test_web_branch_leaves_event_data_none_for_a_non_event_type(db_session):
    """:381-382."""
    s = _seed()
    edit_post(_web_form(), s.post, POST_TYPE_ARTICLE, SRC_WEB, user=s.user)

    assert Event.query.filter_by(post_id=s.post.id).first() is None
```

- [ ] **Step 4: Run the whole file**

Run: `./run_tests.sh tests/test_shared_post_edit.py -q`
Expected: all pass.

`test_web_branch_carries_every_remaining_event_field` asserts on `Event` columns whose names the plan took from the `event_data` dict keys at `:370-374`. Re-derive them against the `Event` model (`app/models.py:3839`) before running; if a column is named differently, assert the real column and say so in the report.

- [ ] **Step 5: Commit**

```bash
git add tests/test_shared_post_edit.py
git commit -F <message-file>
```

Message subject: `test: cover edit_post's SRC_WEB form branch`

---

### Task 4: Fix D287 — `community_member.is_local()`

**Files:**
- Modify: `app/shared/post.py:582` (one line, in place)
- Modify: `tests/test_shared_post_edit.py` (append)

**Interfaces:**
- Consumes: `_seed`, `_api_input` from Task 1.
- Produces: a working moderator path into `:583-589`, which Task 6 needs for the `:592` dedup arms.

**The defect.** `app/shared/post.py:582` is `if community_member.is_local():`. `CommunityMember` (`app/models.py:3499-3513`) defines no `is_local` method. It has a `user` relationship at `:3509` (`lazy='joined'`), and `User.is_local` is a method at `app/models.py:1251`. So the call raises `AttributeError` for **any** moderator, and the whole `notify_mods` path is dead.

- [ ] **Step 1: Write the failing test**

```python
# ---------------------------------------------------------------------------
# The suspicious-domain block, :565-598
# ---------------------------------------------------------------------------


def test_a_moderator_of_a_notify_mods_domain_is_notified(db_session, http_mock):
    """D287. :580-589.

    Before the fix this raises `AttributeError: 'CommunityMember' object has no
    attribute 'is_local'` at :582 -- CommunityMember (app/models.py:3499-3513)
    has no such method. It has a `user` relationship at :3509, and User.is_local
    is at :1251.

    The failure is a CRASH, not a wrong value, and that is what makes D287 live
    rather than latent: every notify_mods domain took down the whole edit.
    """
    s = _seed(domain_name='suspicious.example', notify_mods=True)
    moderator = make_user(s.instance, 'mod', local=True)
    make_community_member(moderator, s.community, is_moderator=True)
    assert len({s.user.id, moderator.id}) == 2

    http_mock.head('https://suspicious.example/pic.png').respond(
        200, headers={'Content-Type': 'image/png'})
    http_mock.get('https://suspicious.example/pic.png').respond(404)

    edit_post(_api_input(url='https://suspicious.example/pic.png'), s.post,
              POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=False)

    notifications = Notification.query.filter_by(user_id=moderator.id).all()
    assert len(notifications) == 1
    assert notifications[0].notif_type == NOTIF_REPORT
    assert notifications[0].subtype == 'post_from_suspicious_domain'
```

- [ ] **Step 2: Run it and record the exact failure**

Run: `./run_tests.sh tests/test_shared_post_edit.py::test_a_moderator_of_a_notify_mods_domain_is_notified -q`
Expected: FAIL with `AttributeError: 'CommunityMember' object has no attribute 'is_local'`.

Quote the exact error line in the task report. **If it fails with anything else — an assertion, a `TypeError`, a missing route — stop and report.** A different failure means the test does not reach `:582`, and a fix landed against a test that never exercised the defect proves nothing.

Note: this test also passes through `:577`, so if it fails with `TypeError: Object of type Domain is not JSON serializable` instead, that means the moderator loop was somehow skipped and the admin loop ran. Check the seeding before touching production code.

- [ ] **Step 3: Apply the fix**

Edit `app/shared/post.py:582`, in place, one line:

```python
                    if community_member.user.is_local():
```

Verify no line moved:

```bash
git diff --stat app/shared/post.py
```

Expected: `1 file changed, 1 insertion(+), 1 deletion(-)`.

- [ ] **Step 4: Run the test again**

Run: `./run_tests.sh tests/test_shared_post_edit.py::test_a_moderator_of_a_notify_mods_domain_is_notified -q`
Expected: PASS.

- [ ] **Step 5: Add the remote-moderator test that proves the gate still gates**

```python
def test_a_remote_moderator_of_a_notify_mods_domain_is_not_notified(db_session, http_mock):
    """:582, false arm -- and the reason D287's fix is `user.is_local()` rather
    than deleting the guard.

    D288 records that the federated copy of this loop
    (app/activitypub/util.py:3520-3527) has NO locality gate at all, so the two
    editors disagree about remote moderators. This test pins THIS editor's
    answer so that disagreement stays visible rather than being quietly
    resolved by a later edit.
    """
    peer = make_instance('peer.example', software='lemmy')
    s = _seed(domain_name='suspicious.example', notify_mods=True)
    remote_mod = make_user(peer, 'remotemod', local=False)
    make_community_member(remote_mod, s.community, is_moderator=True)
    assert remote_mod.ap_id is not None

    http_mock.head('https://suspicious.example/pic.png').respond(
        200, headers={'Content-Type': 'image/png'})
    http_mock.get('https://suspicious.example/pic.png').respond(404)

    edit_post(_api_input(url='https://suspicious.example/pic.png'), s.post,
              POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=False)

    assert Notification.query.filter_by(user_id=remote_mod.id).count() == 0
```

- [ ] **Step 6: Run both**

Run: `./run_tests.sh tests/test_shared_post_edit.py -q -k notify_mods`
Expected: 2 passed.

- [ ] **Step 7: Mutation-prove the fix**

Run these one at a time. After **each** one: restore with `git checkout -- app/shared/post.py`, then run `git diff -- app/` and confirm it is empty before starting the next. Never batch.

| # | Mutation at `:582` | Expected |
|---|---|---|
| 1 | `if True:` | killed by `test_a_remote_moderator_of_a_notify_mods_domain_is_not_notified` |
| 2 | `if False:` | killed by `test_a_moderator_of_a_notify_mods_domain_is_notified` |
| 3 | `if not community_member.user.is_local():` | killed by **both** |

And at `:580`:

| # | Mutation at `:580` | Expected |
|---|---|---|
| 4 | `if True:` | see below |
| 5 | `if False:` | killed by `test_a_moderator_of_a_notify_mods_domain_is_notified` |

For mutation 4, note that both tests above already set `notify_mods=True`, so nothing distinguishes it yet. Add this test in the same commit and re-run mutation 4 against it:

```python
def test_a_moderator_is_not_notified_when_the_domain_does_not_ask(db_session, http_mock):
    """:580, false arm. notify_mods defaults to False
    (app/models.py:3458), which is the shape almost every Domain row has."""
    s = _seed(domain_name='quiet.example', notify_mods=False)
    moderator = make_user(s.instance, 'mod', local=True)
    make_community_member(moderator, s.community, is_moderator=True)

    http_mock.head('https://quiet.example/pic.png').respond(
        200, headers={'Content-Type': 'image/png'})
    http_mock.get('https://quiet.example/pic.png').respond(404)

    edit_post(_api_input(url='https://quiet.example/pic.png'), s.post,
              POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=False)

    assert Notification.query.filter_by(user_id=moderator.id).count() == 0
```

Record each result in the task report as: mutation, the single test that killed it, whether the kill was an **assertion-kill** or a **crash-kill**, and whether it was a **sole** kill or a **multi** kill.

- [ ] **Step 8: Verify the tree is clean and commit**

```bash
git diff -- app/ | head
```

Expected: only the single-line `:582` change. Then:

```bash
git add app/shared/post.py tests/test_shared_post_edit.py
git commit -F <message-file>
```

Message subject: `fix: notify local moderators of suspicious domains, not AttributeError`

The message body must state that `CommunityMember` has no `is_local`, that the call raised for every moderator, that `CommunityMember.user` is at `app/models.py:3509` and `User.is_local` at `:1251`, and that this closes D287.

---

### Task 5: Fix D292 — an ORM object in a `db.JSON` column

**Files:**
- Modify: `app/shared/post.py:577` (one line, in place)
- Modify: `tests/test_shared_post_edit.py` (append)
- Modify: `tests/test_ap_update_post_tails.py:2889`, `:3332` (prose only)

**Interfaces:**
- Consumes: `_seed`, `_api_input`, and the working moderator path from Task 4.
- Produces: nothing later tasks need.

**The defect.** `Notification.targets` is a `db.JSON` column. `app/shared/post.py:577` puts `post.domain` — a `Domain` ORM object — into the dict that becomes it. The flush raises `TypeError: Object of type Domain is not JSON serializable`.

**The arbitration, from spec §4.2.** The fix is `post.domain.name`, not `post.domain_id` and not dropping the key:

1. The three sibling keys — `orig_post_title` (`:575`), `orig_post_body` (`:576`), `author_user_name` (`:578`) — are all human-readable display values. `domain_id` would be the only opaque integer in a dict of display strings.
2. The key has four writers and zero readers. Dropping it changes the dict's **shape**, and shape is what a future reader compares the four writers against.
3. `post.domain` is non-`None` at `:577`: `:566` computes `domain`, `:567` guards `if domain:`, `:570` assigns `post.domain = domain`. `.name` cannot raise here.

**Only `:577` is fixed.** `app/activitypub/util.py:3517` and `:3535` and `app/models.py:2075` are outside this sub-project's scoped file and stay registered, with the argument above now travelling with them.

- [ ] **Step 1: Write the failing test**

```python
def test_the_notify_dict_is_json_serialisable_and_the_edit_is_not_half_applied(
        db_session, http_mock):
    """D292, at the site the register calls the worst of its four.

    Before the fix, :577 puts the Domain ORM object into `targets_data`, which
    becomes `Notification.targets`, a db.JSON column. The flush raises
    `TypeError: Object of type Domain is not JSON serializable`.

    THE CONSEQUENCE IS WORSE THAN A CRASH, and the session boundaries inside
    edit_post are what make it so:

      :459  db.session.commit()      -- title/body/type are already durable
      :588  db.session.add(notify)   -- the poisoned row is pending, nothing raises
      :606  db.session.add(file)
      :607  db.session.commit()      -- HERE, and the File at :606 is orphaned

    :459 runs only inside `if not from_scratch:` (:421), which is why this test
    passes from_scratch=False and changes the url -- the same change that sets
    url_changed at :436 and opens the :565 gate.

    Only the HEAD route is registered: the run dies at :607, before
    make_image_sizes at :616 would fetch the source url.
    """
    s = _seed(domain_name='suspicious.example', notify_mods=True)
    moderator = make_user(s.instance, 'mod', local=True)
    make_community_member(moderator, s.community, is_moderator=True)

    http_mock.head('https://suspicious.example/pic.png').respond(
        200, headers={'Content-Type': 'image/png'})
    http_mock.get('https://suspicious.example/pic.png').respond(404)

    edit_post(_api_input(title='the new title',
                         url='https://suspicious.example/pic.png'),
              s.post, POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=False)

    db.session.expire(s.post)
    assert s.post.title == 'the new title'

    notification = Notification.query.filter_by(user_id=moderator.id).one()
    assert notification.targets['orig_post_domain'] == 'suspicious.example'
    assert isinstance(notification.targets['orig_post_domain'], str)
```

- [ ] **Step 2: Run it and record the exact failure**

Run: `./run_tests.sh "tests/test_shared_post_edit.py::test_the_notify_dict_is_json_serialisable_and_the_edit_is_not_half_applied" -q`
Expected: FAIL with `TypeError: Object of type Domain is not JSON serializable`.

Quote the exact line. If it fails with anything else, stop and report.

- [ ] **Step 3: Write the half-applied-state test, and run it BEFORE the fix**

```python
def test_the_failing_flush_leaves_the_title_committed_and_the_file_orphaned(
        db_session, http_mock):
    """The shape of D292's damage, pinned before the fix removes it.

    This test asserts what SURVIVES the TypeError, which is the part a test that
    only catches the exception cannot see: :459 has already committed the new
    title, so the edit is half-applied rather than cleanly aborted.

    It is xfail-after-fix by construction. Once :577 writes a string the flush
    succeeds and there is nothing half-applied to observe, so this test is
    DELETED in the same commit as the fix -- its content lives on in the
    register entry for D292 and in the docstring above.
    """
```

Run it before the fix, confirm it demonstrates the half-applied state, quote the observation into the task report, and then delete the test body — do **not** ship a test that cannot pass on the fixed tree. The report is where this observation is preserved. If measuring it needs more than ten minutes, skip the measurement and say so; the register already carries the claim from sub-project 17.

- [ ] **Step 4: Apply the fix**

Edit `app/shared/post.py:577`, in place, one line:

```python
                            'orig_post_domain': post.domain.name,
```

Verify no line moved:

```bash
git diff --stat app/shared/post.py
```

Expected: `1 file changed, 1 insertion(+), 1 deletion(-)`.

- [ ] **Step 5: Run the test again**

Run: `./run_tests.sh "tests/test_shared_post_edit.py::test_the_notify_dict_is_json_serialisable_and_the_edit_is_not_half_applied" -q`
Expected: PASS.

- [ ] **Step 6: Assert the whole dict, not just the fixed key**

```python
def test_the_notify_dict_carries_every_key_the_four_writers_share(db_session, http_mock):
    """:573-579. The dict's SHAPE is the thing D292's arbitration protects:
    `orig_post_domain` has four writers and zero readers, so the argument for
    keeping the key is that the four stay comparable.

    author_user_name at :578 is a conditional expression. coverage.py emits no
    arc for one (tests/README.md fact 87), so 100% statements and 100% branches
    can both hold while one arm has never run -- exactly D293's shape. This test
    takes the ap_id arm; the next takes the user_name arm.
    """
    s = _seed(domain_name='suspicious.example', notify_mods=True)
    s.user.ap_id = 'editor@peer.example'
    db.session.commit()
    moderator = make_user(s.instance, 'mod', local=True)
    make_community_member(moderator, s.community, is_moderator=True)

    http_mock.head('https://suspicious.example/pic.png').respond(
        200, headers={'Content-Type': 'image/png'})
    http_mock.get('https://suspicious.example/pic.png').respond(404)

    edit_post(_api_input(title='shaped', url='https://suspicious.example/pic.png'),
              s.post, POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=False)

    targets = Notification.query.filter_by(user_id=moderator.id).one().targets
    assert set(targets) == {'gen', 'post_id', 'orig_post_title', 'orig_post_body',
                            'orig_post_domain', 'author_user_name'}
    assert targets['gen'] == '0'
    assert targets['post_id'] == s.post.id
    assert targets['orig_post_title'] == 'shaped'
    assert targets['orig_post_domain'] == 'suspicious.example'
    assert targets['author_user_name'] == 'editor@peer.example'


def test_the_notify_dict_falls_back_to_user_name_when_there_is_no_ap_id(
        db_session, http_mock):
    """:578, the OTHER arm of the conditional expression. A local editor has
    ap_id None (tests/factories.py make_user), which is the ordinary case."""
    s = _seed(domain_name='suspicious.example', notify_mods=True)
    assert s.user.ap_id is None
    moderator = make_user(s.instance, 'mod', local=True)
    make_community_member(moderator, s.community, is_moderator=True)

    http_mock.head('https://suspicious.example/pic.png').respond(
        200, headers={'Content-Type': 'image/png'})
    http_mock.get('https://suspicious.example/pic.png').respond(404)

    edit_post(_api_input(url='https://suspicious.example/pic.png'), s.post,
              POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=False)

    targets = Notification.query.filter_by(user_id=moderator.id).one().targets
    assert targets['author_user_name'] == 'editor'
```

- [ ] **Step 7: Mutation-prove the fix**

One at a time; `git checkout -- app/shared/post.py` and confirm `git diff -- app/` is empty after each.

| # | Mutation at `:577` | Expected |
|---|---|---|
| 1 | `'orig_post_domain': post.domain,` (revert) | crash-killed by `test_the_notify_dict_is_json_serialisable_and_the_edit_is_not_half_applied` |
| 2 | `'orig_post_domain': post.domain_id,` | assertion-killed by the same test (`isinstance(..., str)`) |
| 3 | delete the line | assertion-killed by `test_the_notify_dict_carries_every_key_the_four_writers_share` (the `set(targets)` assertion) |

Mutation 3 is the one that proves the arbitration's third reason — shape — is actually defended by a test rather than only argued in prose.

Also re-run the `:580` and `:582` mutations from Task 4, all five, because `:577` is inside the same block and fact 74 requires it. Record all eight results.

- [ ] **Step 8: Update `tests/test_ap_update_post_tails.py`**

Both `:2889` and `:3332` name `app/shared/post.py:577` as an open D292 site. Re-derive both line numbers first (they may have moved if that file changed), then edit the prose so it says the site is **fixed**, names this sub-project, and keeps the reachability argument that made it the worst of the four. Do not delete the sentences — a correction that only deletes a claim gets re-derived (fact 96).

Verify afterwards that `app/shared/post.py:577` is still the `orig_post_domain` line, so the citation stays true:

```bash
awk 'NR==577 {print}' app/shared/post.py
```

- [ ] **Step 9: Run the two affected files and commit**

Run: `./run_tests.sh tests/test_shared_post_edit.py tests/test_ap_update_post_tails.py -q`
Expected: all pass.

```bash
git add app/shared/post.py tests/test_shared_post_edit.py tests/test_ap_update_post_tails.py
git commit -F <message-file>
```

Message subject: `fix: store the domain name, not the Domain object, in a JSON notification target`

The message body must carry the three-reason arbitration verbatim from spec §4.2, state that only `:577` is fixed and that the other three sites stay registered, and name the half-applied consequence with its session boundaries.

---

### Task 6: The rest of the suspicious-domain block

**Files:**
- Modify: `tests/test_shared_post_edit.py` (append)

**Interfaces:**
- Consumes: `_seed`, `_api_input`, and both fixes from Tasks 4 and 5.
- Produces: nothing later tasks need.

- [ ] **Step 1: Write the gate tests for `:565` and `:567`**

```python
def test_the_domain_block_is_reached_from_scratch_without_a_url_change(db_session, http_mock):
    """:565, `from_scratch` true arm. `url_changed` is False here because :435's
    block runs only inside `if not from_scratch:` (:421) -- so this is the one
    route into :566 that does not depend on the url having changed."""
    s = _seed(domain_name='suspicious.example', notify_mods=True)
    moderator = make_user(s.instance, 'mod', local=True)
    make_community_member(moderator, s.community, is_moderator=True)

    http_mock.head('https://suspicious.example/pic.png').respond(
        200, headers={'Content-Type': 'image/png'})
    http_mock.get('https://suspicious.example/pic.png').respond(404)

    edit_post(_api_input(url='https://suspicious.example/pic.png'), s.post,
              POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=True)

    assert Notification.query.filter_by(user_id=moderator.id).count() == 1


def test_the_domain_block_is_skipped_when_the_url_is_unchanged(db_session, http_mock):
    """:565, both `from_scratch` and `url_changed` false. The post already HAS
    the url, so :435 `url != post.url` is false and :436 never runs.

    Seeding the post with a url means :410 calls is_image_url too, so the HEAD
    route is matched twice; respx serves a route repeatedly.
    """
    s = _seed(url='https://suspicious.example/pic.png',
              domain_name='suspicious.example', notify_mods=True)
    moderator = make_user(s.instance, 'mod', local=True)
    make_community_member(moderator, s.community, is_moderator=True)

    http_mock.head('https://suspicious.example/pic.png').respond(
        200, headers={'Content-Type': 'image/png'})
    http_mock.get('https://suspicious.example/pic.png').respond(404)

    edit_post(_api_input(url='https://suspicious.example/pic.png'), s.post,
              POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=False)

    assert Notification.query.filter_by(user_id=moderator.id).count() == 0


def test_the_domain_block_is_skipped_for_a_hostless_url(db_session, http_mock):
    """:567, false arm. domain_from_url (app/utils.py:1561) returns None when
    urlparse finds no hostname, and every caller writes `if domain:` first."""
    s = _seed()
    http_mock.head('file:///etc/passwd').respond(200, headers={'Content-Type': 'text/plain'})

    edit_post(_api_input(url='file:///etc/passwd'), s.post, POST_TYPE_LINK,
              SRC_API, user=s.user, from_scratch=True)

    assert Notification.query.count() == 0
```

The `file:///` url may not be reachable through httpx at all. Before running, check what `mime_type_using_head` does with a non-http scheme: if httpx raises `InvalidURL`, `mime_type_using_head` catches it and returns `''`, so **no route should be registered** and `http_mock` must not be requested (its `assert_all_called=True` would fail on an unused route). Adjust the test to whichever is true and say which in the report.

- [ ] **Step 2: Write the banned-domain tests**

```python
def test_a_banned_domain_raises_before_anything_is_notified(db_session, http_mock):
    """:568-569, first conjunct. The message is the domain name plus a fixed
    suffix, and it is what the web route surfaces to the person editing."""
    s = _seed(domain_name='banned.example')
    s.domain.banned = True
    db.session.commit()

    http_mock.head('https://banned.example/pic.png').respond(
        200, headers={'Content-Type': 'image/png'})

    with pytest.raises(Exception) as excinfo:
        edit_post(_api_input(url='https://banned.example/pic.png'), s.post,
                  POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=True)

    assert 'banned.example is blocked by admin' in str(excinfo.value)


def test_a_pages_dev_domain_raises_even_when_it_is_not_banned(db_session, http_mock):
    """:568, second conjunct. `.pages.dev` is hardcoded, so a domain nobody has
    banned still raises -- the two conjuncts are separately load-bearing."""
    s = _seed(domain_name='thing.pages.dev')
    assert s.domain.banned is False

    http_mock.head('https://thing.pages.dev/pic.png').respond(
        200, headers={'Content-Type': 'image/png'})

    with pytest.raises(Exception) as excinfo:
        edit_post(_api_input(url='https://thing.pages.dev/pic.png'), s.post,
                  POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=True)

    assert 'thing.pages.dev is blocked by admin' in str(excinfo.value)
```

- [ ] **Step 3: Write the post_count and admin-loop tests**

```python
def test_the_new_domain_gains_a_post_and_the_old_one_loses_one(db_session, http_mock):
    """:570-571, against :443-445's decrement on the old url. The two are a
    pair: a url change moves the count from one Domain to the other."""
    s = _seed(url='https://old.example/a.png')
    old = make_domain('old.example')
    old.post_count = 5
    new = make_domain('new.example')
    new.post_count = 2
    db.session.commit()
    assert len({old.id, new.id}) == 2

    http_mock.head('https://old.example/a.png').respond(200, headers={'Content-Type': 'image/png'})
    http_mock.head('https://new.example/b.png').respond(200, headers={'Content-Type': 'image/png'})
    http_mock.get('https://new.example/b.png').respond(404)

    edit_post(_api_input(url='https://new.example/b.png'), s.post, POST_TYPE_LINK,
              SRC_API, user=s.user, from_scratch=False)

    db.session.expire(old)
    db.session.expire(new)
    assert old.post_count == 4
    assert new.post_count == 3
    assert s.post.domain_id == new.id


def test_an_admin_of_a_notify_admins_domain_is_notified(db_session, http_mock):
    """:590-598. Site.admins() joins user_role with an INNER join (D295), so a
    roleless User.id == 1 is NOT an admin -- the `or_(..., User.id == 1)` never
    sees a roleless user, and the filter is on role_id == ROLE_ADMIN, so the
    role's id must BE ROLE_ADMIN -- which is what _make_admin arranges."""
    s = _seed(domain_name='suspicious.example', notify_admins=True)
    admin = make_user(s.instance, 'admin', local=True)
    _make_admin(admin)

    http_mock.head('https://suspicious.example/pic.png').respond(
        200, headers={'Content-Type': 'image/png'})
    http_mock.get('https://suspicious.example/pic.png').respond(404)

    edit_post(_api_input(url='https://suspicious.example/pic.png'), s.post,
              POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=True)

    assert Notification.query.filter_by(user_id=admin.id).count() == 1


def test_no_admin_is_notified_when_the_domain_does_not_ask(db_session, http_mock):
    """:590, false arm."""
    s = _seed(domain_name='quiet.example', notify_admins=False)

    http_mock.head('https://quiet.example/pic.png').respond(
        200, headers={'Content-Type': 'image/png'})
    http_mock.get('https://quiet.example/pic.png').respond(404)

    edit_post(_api_input(url='https://quiet.example/pic.png'), s.post,
              POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=True)

    assert Notification.query.count() == 0
```

`_make_admin` from the prelude is what makes the role's id **be** `ROLE_ADMIN`. `grant_permission` alone is not enough: it creates a `Role` with an auto id, and `Site.admins()` filters on `user_role.c.role_id == ROLE_ADMIN`. That is why `grant_permission` is not in this file's import list.

- [ ] **Step 4: Write the dedup tests — both arms of `:592`**

```python
def test_a_user_who_is_both_moderator_and_admin_is_notified_once(db_session, http_mock):
    """:589 and :592, the dedup. THIS TEST IS ONLY REACHABLE BECAUSE D287 IS
    FIXED: before that fix, :582 raised for every moderator, so :589 never ran
    and already_notified was always empty when :592 read it.

    That is the real reason Task 4 lands before Task 6 -- not, as the spec's
    section 4.1 said, because D292's test needed it. D292's test reaches :577
    through the admin loop with D287 unfixed, because the dict at :573-579 is
    built before both loops.
    """
    s = _seed(domain_name='suspicious.example', notify_mods=True, notify_admins=True)
    both = make_user(s.instance, 'both', local=True)
    make_community_member(both, s.community, is_moderator=True)
    _make_admin(both)

    http_mock.head('https://suspicious.example/pic.png').respond(
        200, headers={'Content-Type': 'image/png'})
    http_mock.get('https://suspicious.example/pic.png').respond(404)

    edit_post(_api_input(url='https://suspicious.example/pic.png'), s.post,
              POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=True)

    assert Notification.query.filter_by(user_id=both.id).count() == 1


def test_a_moderator_and_a_separate_admin_are_both_notified(db_session, http_mock):
    """:592, the true arm -- `admin.id not in already_notified`. Two distinct
    people, so the dedup must NOT suppress the second."""
    s = _seed(domain_name='suspicious.example', notify_mods=True, notify_admins=True)
    moderator = make_user(s.instance, 'mod', local=True)
    make_community_member(moderator, s.community, is_moderator=True)
    admin = make_user(s.instance, 'admin', local=True)
    _make_admin(admin)
    assert len({s.user.id, moderator.id, admin.id}) == 3

    http_mock.head('https://suspicious.example/pic.png').respond(
        200, headers={'Content-Type': 'image/png'})
    http_mock.get('https://suspicious.example/pic.png').respond(404)

    edit_post(_api_input(url='https://suspicious.example/pic.png'), s.post,
              POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=True)

    assert Notification.query.filter_by(user_id=moderator.id).count() == 1
    assert Notification.query.filter_by(user_id=admin.id).count() == 1
```

- [ ] **Step 5: Run the whole file**

Run: `./run_tests.sh tests/test_shared_post_edit.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add tests/test_shared_post_edit.py
git commit -F <message-file>
```

Message subject: `test: cover edit_post's suspicious-domain gates, counts and notify dedup`

---

### Task 7: Close the residuals to zero on both metrics

**Files:**
- Modify: `tests/test_shared_post_edit.py` (append whatever the measurement demands)

**Interfaces:**
- Consumes: everything from Tasks 1-6.
- Produces: a written statement of any arm that remains uncovered and why, for Task 8 to register.

- [ ] **Step 1: Ask the controller for the scoped measurement**

The implementer does **not** run the full suite. Report to the controller that Task 7 is ready and needs the two scoped counts. The controller runs the coverage command from the Global Constraints and reports back, for `app/shared/post.py`:

- missing statements and missing branch arms within `:251-383`
- missing statements and missing branch arms within `:565-598`

Starting point was 137 and 38.

- [ ] **Step 2: Enumerate the conditional expressions by AST walk**

coverage.py emits no arc for a conditional expression (fact 87), so the numbers above cannot see them. Run this and reconcile every result against a named test:

```bash
python3 - <<'PY'
import ast
src = open('app/shared/post.py').read()
tree = ast.parse(src)
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == 'edit_post':
        for sub in ast.walk(node):
            if isinstance(sub, ast.IfExp):
                print(sub.lineno, ast.get_source_segment(src, sub))
PY
```

Every `IfExp` reported inside `:251-383` or `:565-598` needs **both** arms exercised by named tests. `:578` is covered by Task 5's two tests. `:388` and `:390` are outside both clusters but sit immediately below the marker; note them, do not chase them.

- [ ] **Step 3: Write a test for each remaining arm**

For each uncovered statement or branch arm the controller reported, write one named test that exercises it, following the shapes already in the file. Each test's docstring names the line and says which arm it takes.

If an arm is genuinely unreachable, do **not** write a test that pretends to reach it. Write the argument down instead: what the arm is, what would have to be true to reach it, and why nothing can make it true. Consult `tests/README.md` fact 75's catalogue of causes for an unkillable clause — there are seven — and say which cause applies, or say that it is an eighth and describe it. Report it for Task 8 to register.

- [ ] **Step 4: Ask the controller to re-measure**

Report back. The controller re-runs the coverage command and confirms both clusters are at zero on both metrics, or names what remains.

- [ ] **Step 5: Commit**

```bash
git add tests/test_shared_post_edit.py
git commit -F <message-file>
```

Message subject: `test: close edit_post's dispatch and domain block to zero on both metrics`

---

### Task 8: Register the findings, raise the floor, verify the suite

**Files:**
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`
- Modify: `tests/README.md`
- Modify: `coverage_floors.ini`

**Interfaces:**
- Consumes: the D297 measurement from Task 2, the mutation tables from Tasks 4 and 5, any residual argument from Task 7.
- Produces: nothing.

- [ ] **Step 1: Establish the next free finding number**

```bash
grep -o 'D[0-9]\{3\}' docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md | sort -u | tail -5
```

Sub-project 17 ended at D296, so the first new number is **D297**. Confirm against the output rather than trusting this sentence.

- [ ] **Step 2: Append the sub-project 18 section to the register**

Follow the existing structure — sub-project 17's section uses numbered subsections with an `### N. <heading> -- D<a>-D<b>` shape. Write:

1. **Two defects fixed, test-first with mutation-proved tests — D287, D292.** Both were already registered; this section records that they are closed, at which sites, and with which argument. For D292 state explicitly that only `app/shared/post.py:577` is fixed and that `app/activitypub/util.py:3517`, `:3535` and `app/models.py:2075` remain open, now with the arbitration attached.
2. **D297 — the `end_poll` write.** The measured outcome from Task 2 Step 4, quoted, with the three-way comparison: `:290`/`:295` strip without converting, `:310` does not strip, `app/activitypub/util.py:3375-3376` keeps aware, `:3338` stores the raw string. Say which of the three outcomes was observed. Registered, not fixed — a timestamp semantics change is a behaviour change for existing installs.
3. **Anything Task 7 found**, numbered from D298.
4. **The reachability correction**, as a note rather than a finding: spec §4.1's stated reason for ordering D287 before D292 was wrong, the dict at `:573-579` precedes both loops, and the real reason is `:592`'s dedup arms. Land it here as well as in the plan, because a correction that lives in only one place gets re-derived (fact 96).

Do a two-pass citation sweep over everything written (fact 100): pass one resolves every `file:line`, pass two resolves every bare path against `git ls-files`.

- [ ] **Step 3: Add the new harness facts to `tests/README.md`**

```bash
grep -c '^' tests/README.md
grep -o '^1[0-9][0-9]\.' tests/README.md | sort -u | tail -3
```

Sub-project 17 ended at fact 100. Append from 101. Candidates, each written only if it actually bit during this sub-project:

- `is_image_url` issues an httpx HEAD and catches only `httpx.HTTPError`/`httpx.InvalidURL`, so respx's unmatched-request error escapes; a url-bearing test must register the HEAD route, and `edit_post` can call it twice (`:410` and `:601`).
- The `.pages.dev` and banned-domain guard raises a bare `Exception`, so `pytest.raises(Exception)` is the only available assertion and the message is the discriminator.
- Whether `_make_admin`'s get-or-create needed anything beyond the `tests/test_ap_update_post_tails.py:3021` shape when a second admin is made in the same test — sub-project 17's version notes that the role is reused across calls, and this file makes two admins in one test at `test_a_moderator_and_a_separate_admin_are_both_notified`.
- Whatever Task 2 Step 4 measured about writing an aware `datetime` into a naive `db.DateTime` column.
- Any technique Task 7 needed for an arm the earlier tasks could not reach.

Write each as a numbered fact in the file's existing voice. Do not restate a fact the file already has; check first.

- [ ] **Step 4: Ask the controller for the blended figure and add the floor**

`app/shared/post.py` has **no entry** in `coverage_floors.ini`, so nothing defends it today. It was 9.03% blended before this work.

Report to the controller and ask for the post-work blended `summary['percent_covered']` for `app/shared/post.py`. Then add one line to `coverage_floors.ini`, in the existing ordering, at the measured value **rounded down to a whole percent**:

```ini
app/shared/post.py = <measured, floored>
```

- [ ] **Step 5: Verify the floor check passes**

Run: `python3 tests/check_coverage_floors.py` (or whatever invocation the file documents — read its header first).
Expected: pass, with `app/shared/post.py` now listed.

- [ ] **Step 6: Ask the controller for the full-suite run**

Report ready. The controller runs the full suite once and reports pass/skip/subtest counts and wall time. A run over ~600s is erroneous: `./run_tests.sh --down`, then retry.

- [ ] **Step 7: Verify no line moved in `app/shared/post.py`**

```bash
git diff --stat main...HEAD -- app/shared/post.py
```

Expected: `2 insertions(+), 2 deletions(-)` and nothing else. Then confirm all twelve citations still resolve:

```bash
for n in 192 211 213 214 410 443 566 577 582 601 613 654; do printf "%s: " "$n"; awk -v n=$n 'NR==n' app/shared/post.py; done
```

Compare each against the citing file's claim. Every one must still say what its citation says it says.

- [ ] **Step 8: Commit**

```bash
git add docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md tests/README.md coverage_floors.ini
git commit -F <message-file>
```

Message subject: `docs: register sub-project 18's findings and set app/shared/post.py's first floor`

---

## Success criteria

From spec §9, unchanged:

1. `tests/test_shared_post_edit.py` exists and drives `edit_post` directly.
2. Cluster A (`:251-383`) and cluster B (`:565-598`) are at zero uncovered statements and zero uncovered branch arms.
3. Both arms of the conditional expression at `:578` are exercised by distinct named tests.
4. D287 is fixed at `:582` and proved by a test that fails with `AttributeError` before the fix.
5. D292 is fixed at `:577` with the arbitration argued in the spec, and proved by a test that fails with `TypeError: Object of type Domain is not JSON serializable` before the fix.
6. No line number in `app/shared/post.py` moves. Every existing citation still resolves.
7. `tests/test_ap_update_post_tails.py`'s prose about `:577` is updated in the same commit as the fix.
8. `coverage_floors.ini` gains an `app/shared/post.py` entry at the measured floor.
9. The findings register carries D297 onward, and D287/D292 are marked fixed.
10. The full suite is green, with the pass/skip counts recorded.
