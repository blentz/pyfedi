# Sub-project 19 — `send_post` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take `send_post` (`app/shared/tasks/pages.py:88-368`) to zero uncovered statements and branch arms — except two arms proved unreachable — and land the two production fixes registered as D298 and D299.

**Architecture:** One new test file, `tests/test_shared_tasks_send_post.py`, calls `send_post` directly with an explicit `session`. A locally-seeded community with no followers exercises the whole function and reaches the early `return` at `:338` without a single outbound request. Both fixes are guard additions, each test-first in its own commit, each mutation-proved.

**Tech Stack:** pytest, respx (`http_mock`), SQLAlchemy, Flask, Celery (eager), `tests/factories.py`, `./run_tests.sh`.

**Spec:** `docs/superpowers/specs/2026-09-06-coverage-send-post-19-design.md`

---

## Global Constraints

Copied verbatim from the spec. Every task's requirements implicitly include this section.

- **Delete nothing the task did not create. `claude_test` in the repository root is not the campaign's.**
- Only the controller runs the full suite, one pytest session at a time. The controller supplies every coverage figure; no implementer reports one.
- Any run exceeding ~600s is erroneous — that is `session_timeout` in `pytest.ini:28`, and pytest **still exits 0** on a session timeout, so a truncated run reads as green. Check the test count and the coverage report's mtime, not the exit code. `./run_tests.sh --down`, then retry.
- Coverage command: `./run_tests.sh --cov=app --cov-branch --cov-report=json:scratch_full_cov.json -q`.
- **Mutation instrument.** Apply each mutation with a targeted single-line edit (`sed -i 'NNNs/old/new/'` or an Edit matching one unique line). **Never rewrite the whole file.** After applying, `wc -l app/shared/tasks/pages.py` must print `432`. Restore with `git checkout -- app/`. After restoring, **both** `git diff -- app/` empty and the line count unchanged, before the next mutation. One at a time, never batched. Sub-project 18 lost a 1174-line production module to a whole-file rewrite at this exact step.
- Mutation record: the mutation, the single test that killed it, **assertion-kill or crash-kill**, **sole or multi**.
- A fix that changes a guard invalidates every mutation previously recorded for that guard; re-run them all in the same commit (fact 74).
- Every line number copied from anywhere must be re-derived against the current tree before it is written down (fact 99). Citation sweeps run in two passes: `file:line` first, then bare paths against `git ls-files` (fact 100).
- Enumerate conditional expressions by AST walk, not by grep (fact 94) — coverage.py emits no arc for one (fact 87). `sed -n 'A,Bp'` prints no line numbers (fact 95).
- Commit messages containing backticks are committed with `git commit -F <file>`, never `-m`.

---

## Amendment to the spec, binding on every task

**Spec success criterion 2 cannot be met as written, and this plan corrects it.**

The spec asks for zero uncovered statements *and zero uncovered branch arms* in `:88-368`. Two arms are **unreachable**:

```
267:     if not community.local_only:      # missing arc (267, 307)
330:     if not community.local_only:      # missing arc (330, 336)
```

`community` is bound once at `:91` (`community = post.community`) and never reassigned anywhere in the function — verified by scanning `:88-368` for any `community =`. And `:153-154` is:

```
153:     if community.local_only or community.private:
154:         return
```

So `community.local_only` truthy returns at `:154`, and by `:267` it is always falsy. `not community.local_only` is therefore always `True`, and the false arms of `:267` and `:330` can never execute.

**Two more arms are unreachable for different reasons, found while writing this plan.**

```
196:       'name': post.title,          # set unconditionally, in the dict literal
209:     if post.type != POST_TYPE_POLL:
210:         page['name'] = post.title  # re-sets what :196 already set
...
307:     if '@context' not in create:   # :257 always put it there
309:     if 'name' in page:             # :196 always put it there
```

- **`:307`'s TRUE arm (`:308`) is unreachable.** `create` is built at `:250-259` with `'@context': default_context()` at `:257`, and nothing removes it before `:307`. So `'@context' not in create` is never true.
- **`:309`'s FALSE arm, arc `(309, 311)`, is unreachable.** `:196` sets `page['name']` unconditionally inside the dict literal, and nothing deletes it before `:309`. So `'name' in page` is always true.

**Criterion 2 is amended to:** zero uncovered statements, and zero uncovered branch arms **except four**: the false arms of `:267` and `:330`, the true arm of `:307`, and the false arm of `:309`. Task 8 registers all four with a written unreachability argument against `tests/README.md` fact 75's catalogue of causes.

Do not chase these four arms. Do not add a test that appears to reach them.

**Two findings fall out of the second pair, and Task 9 registers both.**

1. **`:209-210` is a redundant statement.** `:196` already assigned `page['name'] = post.title` for every post type; `:210` assigns the same value again for non-polls only. It has no effect. This is fact 75's sixth cause — the statement-scoped one sub-project 16 added — appearing in the wild.
2. **`:306`'s comment says "amend copy of the Create", and no copy is made.** `:254` is `'object': page` — a reference — and `:311` is `note = page`, another reference. So `:310`'s `del`, `:312`'s `content` reset and `:314`'s type change all mutate the very dict `create['object']` points at. The sends at `:295`, `:297`, `:299` and `:303` happen *before* `:306`, and `send_post_request` signs the body inside the call, so delivery order saves this from being a live defect — but the comment describes a copy that does not exist, and a future edit that moved a send below `:306` would silently ship the mutated object. Register it as a latent hazard with that reasoning, not as a crash.

---

## The uncovered map

Measured by the controller on the current tree. 125 total, 64 statements and 61 branch arms.

| Region | Lines | stmt | br | Task |
|---|---|---|---|---|
| Mention extraction | `:96-123` | 21 | 14 | 1 |
| Mention notification and the four early returns | `:125-174` | 18 | 14 | 2 |
| Page builder | `:175-246` | 4 | 5 | 3 |
| Create/Announce and community delivery | `:247-305` | 9 | 9 | 6 |
| Note amendment and follower delivery | `:306-368` | 12 | 19 | 7 |

**Note what this map says about the two fixes.** The builder is nearly covered already — its only uncovered statements are `:217-220`, the image-url fallback chain. So `:181`, `:224`, `:232` and `:233` are **already executed** by existing tests, with non-`None` values. The defects are live in behaviour but invisible to coverage. The failing tests in Tasks 4 and 5 must construct the `None` states specifically; adding coverage will not surface them.

---

## File structure

| File | Responsibility | Task |
|---|---|---|
| `tests/test_shared_tasks_send_post.py` | **Create.** The whole sub-project's tests: prelude, then five clusters in source order. | 1-3, 6-8 |
| `app/shared/tasks/pages.py:181` | **Modify.** D299 — guard the image dereference. | 4 |
| `app/shared/tasks/pages.py:224`, `:232`, `:233` | **Modify.** D298 — guard three `ap_datetime` calls. | 5 |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` | **Append.** D300 onward; D298/D299 closure. | 9 |
| `tests/README.md` | **Append.** New harness facts. | 9 |
| `coverage_floors.ini` | **Add one line.** `app/shared/tasks/pages.py` has no entry. | 9 |

---

## Harness facts this plan depends on

Each was re-derived against the current tree while this plan was written.

1. `app/shared/tasks/pages.py:88` is `def send_post(post_id, edit=False, session=None):`. The file is **432 lines**. The next `def` is `move_post` at `:370` (its `@celery.task` decorator is `:369`).
2. **`session` has no usable default.** `:89` is `session.query(Post).get(post_id)`, so `session=None` raises `AttributeError`. Every test passes `db.session`.
3. **Four early returns stand between entry and the builder**, and a test must clear all four to reach `:175`:
   - `:149-150` `if not community.instance.online(): return`
   - `:153-154` `if community.local_only or community.private: return`
   - `:156-158` a `CommunityBan` row for `(user, community)` → `return`
   - `:159-161` `if not community.is_local():` then a blocked or banned instance → `return`
4. `Instance.online()` is `return not (self.dormant or self.gone_forever)`. Both columns default `False` (`app/models.py:98`, `:100`) and `make_instance` sets neither, so a factory instance is online. To take `:150`, set `dormant = True` and commit.
5. **`community.local_only` is NOT the lever sub-project 18 thought it was.** It returns at `:154`, long before the delivery block at `:267`. That is why the false arms of `:267` and `:330` are dead — see the amendment above.
6. **The natural stopping point before the network is `:336-338`:**
   ```
   336:     followers = session.query(UserFollower).filter_by(local_user_id=post.user_id, is_inward=True).all()
   337:     if not followers:
   338:         return
   ```
   A post whose author has no inward followers ends the function there. Combined with a local community that has no `following_instances()`, the whole builder runs with zero outbound requests.
7. Delivery is `send_post_request` (`app/activitypub/signature.py:82`). A test that reaches it needs the sender to have keys — `make_user(..., with_keys=True)` — because signing calls `.encode()` on the private key and a keyless sender dies before any request.
8. `search_for_user` (`app/user/utils.py:85`) is called at `:106` and `:112`, each inside a bare `except: pass` (`:107-108`, `:113-114`). A mention that fails to resolve is silently skipped, so a test asserting "not delivered" cannot distinguish "correctly skipped" from "crashed and swallowed" — pin the reason, not the absence.
9. `tests/conftest.py:143` truncates with `RESTART IDENTITY`, so ids restart at 1 (fact 89). Seed pairwise-distinct and assert `len({...}) == n`. **Order matters too**: `make_community` hardcodes `instance_id=1`, so a peer instance built before the local one leaves the community's FK pointing at the peer (fact 89's sub-project 18 extension).
10. `http_mock` is `assert_all_called=True` (`tests/conftest.py:288-295`): a registered route never reached FAILS the test. Register exactly what the path reaches.
11. `app/__init__.py:81` builds `SQLAlchemy(session_options={"autoflush": False}, ...)` with no `expire_on_commit`, so it is at SQLAlchemy's default `True`. Use `db.session.expire(obj)` before asserting where no commit intervenes.

---

### Task 1: The file, its prelude, and mention extraction

**Files:**
- Create: `tests/test_shared_tasks_send_post.py`

**Interfaces:**
- Consumes: nothing.
- Produces: the prelude every later task uses —
  - `_seed(body=None, post_type=POST_TYPE_ARTICLE, url=None, local_community=True)` → `SimpleNamespace(instance, user, community, post)`. Seeds the LOCAL instance first so `make_community`'s hardcoded `instance_id=1` resolves to it.
  - `_peer(domain='peer.example', software='lemmy')` → `Instance`, always called **after** `_seed()`.
  - `_send(post, edit=False)` → calls `send_post(post.id, edit=edit, session=db.session)`.

Target: `:96-123`, 21 statements / 14 branch arms.

- [ ] **Step 1: Create the file with its module docstring and prelude**

```python
"""`send_post` -- the Celery-path builder and deliverer of an ActivityPub Page.

`app/shared/tasks/pages.py:88-368`. This is the second of two Page builders in
the codebase; the other is `post_to_page` (app/activitypub/util.py:132-219),
reached from the outbox collection view at app/activitypub/routes.py:2033. They
are near-twins and their disagreements are findings D298, D299 and D300.

ENTRY is a direct call. `send_post(post_id, edit=False, session=None)` has no
usable default for `session` -- :89 is `session.query(Post).get(post_id)` -- so
every test here passes `db.session` explicitly.

FOUR EARLY RETURNS stand between entry and the builder at :175, and a test that
wants to reach the builder must clear all four:

  :149-150  `if not community.instance.online(): return`
  :153-154  `if community.local_only or community.private: return`
  :156-158  a CommunityBan row for (user, community)
  :159-161  a remote community whose instance the user blocked, or that is banned

`Instance.online()` is `not (self.dormant or self.gone_forever)`, and both
columns default False (app/models.py:98, :100), so a factory instance is online
without help.

NOTE ON :153, because sub-project 18 relied on the opposite. Setting
`community.local_only = True` does NOT merely skip delivery -- it returns at
:154 before the builder runs at all. That is also why the false arms of :267
and :330 (`if not community.local_only:`) are UNREACHABLE: `community` is bound
once at :91 and never reassigned, so by :267 the flag is always falsy. Those two
arms are registered as unreachable rather than chased.

STOPPING BEFORE THE NETWORK. :336-338 is
`followers = ...; if not followers: return`. A post whose author has no inward
UserFollower rows ends the function there. Combined with a local community that
has no following_instances(), the entire builder runs with zero outbound
requests, and no `http_mock` is needed. Tests that DO reach delivery must give
the sender real keys (`make_user(..., with_keys=True)`), because signing calls
`.encode()` on the private key.

MENTIONS ARE SILENTLY SKIPPED. `search_for_user` (app/user/utils.py:85) is
called at :106 and :112, each inside a bare `except: pass` (:107-108, :113-114).
So a test asserting that a mention produced no notification cannot distinguish
"correctly skipped" from "crashed and swallowed" -- pin the reason, not the
absence.
"""

import pytest
from types import SimpleNamespace

from app import db
from app.constants import (
    NOTIF_MENTION, POST_TYPE_ARTICLE, POST_TYPE_EVENT, POST_TYPE_IMAGE,
    POST_TYPE_LINK, POST_TYPE_POLL, POST_TYPE_VIDEO,
)
from app.models import (
    CommunityBan, Event, File, Notification, Poll, PollChoice, User,
    UserFollower,
)
from app.shared.tasks.pages import send_post
from tests.factories import (
    make_community, make_community_member, make_instance, make_post, make_user,
)


def _seed(body=None, post_type=POST_TYPE_ARTICLE, url=None, local_community=True):
    """The local instance, a local author, a community, and a post.

    ORDER IS LOAD-BEARING. `make_community` hardcodes `instance_id=1`
    (tests/factories.py) and tests/conftest.py:143 truncates with
    RESTART IDENTITY, so whichever Instance is inserted first gets id 1. The
    local instance is created first here so the community's FK points at it. A
    peer built before this call would capture id 1 and silently make the
    community's instance the peer -- see `_peer` below.

    `local_community=False` gives the community an `ap_id`, which is what
    `Community.is_local()` tests, so :159's guard opens.
    """
    instance = make_instance('test.piefed.local', software='piefed')
    user = make_user(instance, 'author', local=True)
    community = make_community('c1')
    post = make_post(community, user, ap_id='https://test.piefed.local/post/1')
    post.type = post_type
    post.body = body
    post.url = url
    if not local_community:
        community.ap_id = 'c1@peer.example'
    db.session.commit()
    return SimpleNamespace(instance=instance, user=user, community=community,
                           post=post)


def _peer(domain='peer.example', software='lemmy'):
    """A remote Instance. ALWAYS call this AFTER `_seed()` -- see `_seed`'s
    docstring for why the order matters."""
    return make_instance(domain, software=software)


def _send(post, edit=False):
    """`send_post` takes an explicit session; there is no usable default."""
    return send_post(post.id, edit=edit, session=db.session)
```

- [ ] **Step 2: Verify the file collects**

Run: `./run_tests.sh tests/test_shared_tasks_send_post.py -q`
Expected: `no tests ran`. Any `ImportError` is a real defect in the import list — fix it before proceeding. In particular, confirm every name in the `app.models` and `app.constants` import lists actually exists; drop any that does not and say so in your report.

- [ ] **Step 3: Commit the prelude**

```bash
git add tests/test_shared_tasks_send_post.py
git commit -F <message-file>
```

Subject: `test: open tests/test_shared_tasks_send_post.py with the send_post harness`

- [ ] **Step 4: Write the mention-extraction tests**

```python
# ---------------------------------------------------------------------------
# Mention extraction, :96-123
# ---------------------------------------------------------------------------


def test_a_body_with_no_mentions_skips_the_scanner_entirely(db_session):
    """:97, false arm -- `if post.body:` with a body that is falsy.

    The witness is that no Notification exists: the scanner never runs, so
    :126's loop has nothing to iterate.
    """
    s = _seed(body=None)
    _send(s.post)

    assert Notification.query.count() == 0


def test_a_local_mention_resolves_and_is_notified(db_session):
    """:98-108 and :115-123. The local arm of :102's host comparison.

    `current_app.config['SERVER_NAME']` is 'test.piefed.local'
    (tests/conftest.py:69), which is what `_seed` gives the local instance, so
    `@mentioned@test.piefed.local` takes :103-108.
    """
    s = _seed(body='hello @mentioned@test.piefed.local')
    mentioned = make_user(s.instance, 'mentioned', local=True)
    assert len({s.user.id, mentioned.id}) == 2

    _send(s.post)

    notifications = Notification.query.filter_by(user_id=mentioned.id).all()
    assert len(notifications) == 1
    assert notifications[0].notif_type == NOTIF_MENTION
    assert notifications[0].subtype == 'post_mention'


def test_an_author_mentioning_themselves_is_not_notified(db_session):
    """:104, false arm -- `if user_name != user.user_name:`.

    The author is 'author', so `@author@test.piefed.local` is skipped without
    ever calling `search_for_user`.
    """
    s = _seed(body='hello @author@test.piefed.local')
    _send(s.post)

    assert Notification.query.count() == 0


def test_an_unresolvable_local_mention_is_skipped_silently(db_session):
    """:105-108, the bare `except: pass`. THE REASON IS THE ASSERTION.

    `search_for_user` raises for a name with no matching row, and :107-108
    swallows it. A test that only asserted `Notification.query.count() == 0`
    could not tell that outcome apart from the mention never being scanned at
    all, so this asserts the post itself was still processed -- the function
    ran past the scanner rather than dying in it.
    """
    s = _seed(body='hello @nobodyhere@test.piefed.local')
    _send(s.post)

    assert Notification.query.count() == 0
    db.session.expire(s.post)
    assert s.post.id is not None


def test_a_remote_mention_takes_the_ap_id_branch(db_session):
    """:109-114, the else arm of :102. The host differs from SERVER_NAME, so
    the lookup key is the full `name@host` rather than a bare user name."""
    s = _seed()
    peer = _peer()
    s.post.body = 'hello @remoteuser@peer.example'
    db.session.commit()
    remote = make_user(peer, 'remoteuser', local=False)
    assert remote.ap_id == 'remoteuser@peer.example'

    _send(s.post)

    assert Notification.query.filter_by(user_id=remote.id).count() == 0


def test_the_same_local_user_mentioned_twice_is_added_once(db_session):
    """:116-123, the dedup. :118's first disjunct -- a local recipient has
    `ap_id` None, so the comparison falls to `user_name`."""
    s = _seed(body='@mentioned@test.piefed.local and again @mentioned@test.piefed.local')
    mentioned = make_user(s.instance, 'mentioned', local=True)
    assert mentioned.ap_id is None

    _send(s.post)

    assert Notification.query.filter_by(user_id=mentioned.id).count() == 1


def test_two_different_local_users_are_both_added(db_session):
    """:122-123, the true arm of `if add_recipient:` on the second pass --
    the dedup must NOT suppress a genuinely different recipient."""
    s = _seed(body='@alpha@test.piefed.local and @beta@test.piefed.local')
    alpha = make_user(s.instance, 'alpha', local=True)
    beta = make_user(s.instance, 'beta', local=True)
    assert len({s.user.id, alpha.id, beta.id}) == 3

    _send(s.post)

    assert Notification.query.filter_by(user_id=alpha.id).count() == 1
    assert Notification.query.filter_by(user_id=beta.id).count() == 1
```

- [ ] **Step 5: Run them**

Run: `./run_tests.sh tests/test_shared_tasks_send_post.py -q`
Expected: all pass.

`test_the_same_local_user_mentioned_twice_is_added_once` depends on `:118`'s first disjunct. If `search_for_user` returns the same identity object twice, the dedup may be exercised trivially. Confirm by reading `app/user/utils.py:85` what it returns, and say in your report which disjunct of `:118`-`:119` the test actually takes.

- [ ] **Step 6: Commit**

```bash
git add tests/test_shared_tasks_send_post.py
git commit -F <message-file>
```

Subject: `test: cover send_post's mention extraction and dedup`

---

### Task 2: Mention notification and the four early returns

**Files:**
- Modify: `tests/test_shared_tasks_send_post.py` (append)

**Interfaces:**
- Consumes: `_seed`, `_peer`, `_send` from Task 1.
- Produces: nothing later tasks need.

Target: `:125-174`, 18 statements / 14 branch arms.

- [ ] **Step 1: Write the notification tests**

```python
# ---------------------------------------------------------------------------
# Mention notification, :125-147
# ---------------------------------------------------------------------------


def test_a_remote_recipient_gets_no_local_notification(db_session):
    """:127, false arm -- `if recipient.is_local():`.

    `User.is_local()` tests `ap_id`, which `make_user(local=False)` sets. The
    recipient is still collected into `recipients` (and so still reaches :172's
    tag loop), but no Notification row is written for them.
    """
    s = _seed()
    peer = _peer()
    s.post.body = 'hello @remoteuser@peer.example'
    db.session.commit()
    remote = make_user(peer, 'remoteuser', local=False)

    _send(s.post)

    assert Notification.query.filter_by(user_id=remote.id).count() == 0


def test_an_edit_reuses_the_existing_mention_notification(db_session):
    """:128-129 and :132's false arm. On an edit, an existing Notification with
    the same url suppresses a second one -- so the count stays 1 across a
    create followed by an edit."""
    s = _seed(body='hello @mentioned@test.piefed.local')
    mentioned = make_user(s.instance, 'mentioned', local=True)

    _send(s.post, edit=False)
    assert Notification.query.filter_by(user_id=mentioned.id).count() == 1

    _send(s.post, edit=True)
    assert Notification.query.filter_by(user_id=mentioned.id).count() == 1


def test_an_edit_with_no_prior_notification_creates_one(db_session):
    """:128-129 with the query returning None, so :132's TRUE arm still runs.
    This is the arm that distinguishes "edit suppresses" from "edit never
    notifies"."""
    s = _seed(body='hello @mentioned@test.piefed.local')
    mentioned = make_user(s.instance, 'mentioned', local=True)

    _send(s.post, edit=True)

    assert Notification.query.filter_by(user_id=mentioned.id).count() == 1


def test_the_mention_notification_carries_its_targets_and_bumps_the_unread_count(db_session):
    """:133-147. The targets dict at :133-138 and the counter at :145.

    `author_user_name` at :137 is a conditional expression; coverage.py emits no
    arc for one (tests/README.md fact 87), so both arms need named tests. This
    takes the `user_name` arm -- a local author has `ap_id` None.
    """
    s = _seed(body='hello @mentioned@test.piefed.local')
    mentioned = make_user(s.instance, 'mentioned', local=True)
    before = mentioned.unread_notifications
    assert s.user.ap_id is None

    _send(s.post)

    notification = Notification.query.filter_by(user_id=mentioned.id).one()
    assert notification.targets['gen'] == '0'
    assert notification.targets['post_id'] == s.post.id
    assert notification.targets['author_user_name'] == 'author'
    db.session.expire(mentioned)
    assert mentioned.unread_notifications == before + 1


def test_the_mention_notification_uses_ap_id_when_the_author_has_one(db_session):
    """:137, the OTHER arm of the conditional expression."""
    s = _seed(body='hello @mentioned@test.piefed.local')
    s.user.ap_id = 'author@peer.example'
    db.session.commit()
    mentioned = make_user(s.instance, 'mentioned', local=True)

    _send(s.post)

    notification = Notification.query.filter_by(user_id=mentioned.id).one()
    assert notification.targets['author_user_name'] == 'author@peer.example'
```

- [ ] **Step 2: Write the four early-return tests**

```python
# ---------------------------------------------------------------------------
# The four early returns, :149-161
# ---------------------------------------------------------------------------


def test_a_dormant_community_instance_stops_before_the_builder(db_session):
    """:149-150. `Instance.online()` is `not (self.dormant or self.gone_forever)`
    (app/models.py), and both columns default False, so this must be set.

    The witness is that the mention notification from :125-147 DID land while
    nothing after :150 ran -- which is what distinguishes an early return here
    from the function never being called.
    """
    s = _seed(body='hello @mentioned@test.piefed.local')
    mentioned = make_user(s.instance, 'mentioned', local=True)
    s.instance.dormant = True
    db.session.commit()

    _send(s.post)

    assert Notification.query.filter_by(user_id=mentioned.id).count() == 1


def test_a_gone_forever_instance_also_stops(db_session):
    """:149-150 via the second disjunct of `online()`. The two columns are
    separately load-bearing."""
    s = _seed(body='hello @mentioned@test.piefed.local')
    mentioned = make_user(s.instance, 'mentioned', local=True)
    s.instance.gone_forever = True
    db.session.commit()

    _send(s.post)

    assert Notification.query.filter_by(user_id=mentioned.id).count() == 1


@pytest.mark.parametrize('flag', ['local_only', 'private'])
def test_a_local_only_or_private_community_stops_before_the_builder(db_session, flag):
    """:153-154, both disjuncts.

    THIS IS THE RETURN SUB-PROJECT 18 WAS ACTUALLY USING. Its tests set
    `community.local_only = True` believing it skipped delivery at :267; it in
    fact returns here, before the builder. That is also why :267's and :330's
    false arms are unreachable -- see this file's module docstring.
    """
    s = _seed(body='hello @mentioned@test.piefed.local')
    mentioned = make_user(s.instance, 'mentioned', local=True)
    setattr(s.community, flag, True)
    db.session.commit()

    _send(s.post)

    assert Notification.query.filter_by(user_id=mentioned.id).count() == 1


def test_a_banned_author_stops_before_the_builder(db_session):
    """:156-158. A CommunityBan row for (author, community)."""
    s = _seed(body='hello @mentioned@test.piefed.local')
    mentioned = make_user(s.instance, 'mentioned', local=True)
    db.session.add(CommunityBan(user_id=s.user.id, community_id=s.community.id))
    db.session.commit()

    _send(s.post)

    assert Notification.query.filter_by(user_id=mentioned.id).count() == 1


def test_a_remote_community_on_a_blocked_instance_stops(db_session):
    """:159-161, first disjunct. :159 opens only for a community that is NOT
    local -- `Community.is_local()` tests `ap_id`, which `_seed` sets under
    `local_community=False`."""
    s = _seed(body='hello @mentioned@test.piefed.local', local_community=False)
    mentioned = make_user(s.instance, 'mentioned', local=True)
    peer = _peer()
    s.community.instance_id = peer.id
    db.session.commit()
    from app.models import InstanceBlock
    db.session.add(InstanceBlock(user_id=s.user.id, instance_id=peer.id))
    db.session.commit()

    _send(s.post)

    assert Notification.query.filter_by(user_id=mentioned.id).count() == 1
```

- [ ] **Step 3: Write the type-dispatch and tag tests**

```python
@pytest.mark.parametrize('post_type,expected', [
    (POST_TYPE_POLL, 'Question'),
    (POST_TYPE_EVENT, 'Event'),
    (POST_TYPE_ARTICLE, 'Page'),
])
def test_the_activity_type_follows_the_post_type(db_session, post_type, expected):
    """:163-168. The three-way dispatch that names the object.

    There is no direct observable for `type` -- it is a local consumed at :185.
    Assert instead that the function ran to completion for each type, and say so
    in the docstring rather than implying the value was checked. Task 3 pins the
    value through the builder.
    """
    s = _seed(post_type=post_type)
    if post_type == POST_TYPE_POLL:
        poll = Poll(post_id=s.post.id, end_poll=None, mode='single')
        db.session.add(poll)
        db.session.commit()
    _send(s.post)


def test_a_mentioned_recipient_lands_in_both_tag_and_cc(db_session):
    """:172-174. Every recipient is appended to `tag` as a Mention and to `cc`.

    Both are locals consumed by the page dict at :188-189, so the observable is
    indirect. Task 6 asserts the delivered Create's `cc`; here the assertion is
    that the notification path and the tag path both ran for one recipient.
    """
    s = _seed(body='hello @mentioned@test.piefed.local')
    mentioned = make_user(s.instance, 'mentioned', local=True)

    _send(s.post)

    assert Notification.query.filter_by(user_id=mentioned.id).count() == 1
```

- [ ] **Step 4: Run and reconcile**

Run: `./run_tests.sh tests/test_shared_tasks_send_post.py -q`
Expected: all pass.

`test_a_remote_community_on_a_blocked_instance_stops` imports `InstanceBlock` locally. Check whether `tests/factories.py` already has `make_instance_block(user, instance)` — it does — and use the factory instead, moving the import to the module's factory import list. Report which you used.

- [ ] **Step 5: Commit**

```bash
git add tests/test_shared_tasks_send_post.py
git commit -F <message-file>
```

Subject: `test: cover send_post's mention notifications and its four early returns`

---

### Task 3: The Page builder's image-url fallback chain

**Files:**
- Modify: `tests/test_shared_tasks_send_post.py` (append)

**Interfaces:**
- Consumes: `_seed`, `_send` from Task 1.
- Produces: nothing later tasks need.

Target: `:175-246`, 4 statements / 5 branch arms — all of it in `:213-221`.

The rest of the builder is already covered by existing tests elsewhere in the suite. The uncovered part is exactly the image-url fallback:

```
213:     if post.image_id:
214:         image_url = ''
215:         if post.image.source_url:
216:             image_url = post.image.source_url
217:         elif post.image.file_path:
218:             image_url = post.image.file_path.replace('app/static/', f"{current_app.config['SERVER_URL']}/static/")
219:         elif post.image.thumbnail_path:
220:             image_url = post.image.thumbnail_path.replace('app/static/', f"{current_app.config['SERVER_URL']}/static/")
221:         page['image'] = {'type': 'Image', 'url': image_url}
```

- [ ] **Step 1: Write the fallback tests**

```python
# ---------------------------------------------------------------------------
# The Page builder's image-url fallback, :213-221
# ---------------------------------------------------------------------------


def _attach_image(post, **columns):
    """A File on `post`, with only the columns the caller names set.

    :215-220 is a three-step fallback over source_url, file_path and
    thumbnail_path, so each test must leave the earlier columns unset for its
    own arm to be reached.
    """
    f = File(**columns)
    db.session.add(f)
    db.session.commit()
    post.image_id = f.id
    db.session.commit()
    return f


def test_the_image_url_prefers_the_source_url(db_session):
    """:215-216, the first arm. `source_url` wins over the other two."""
    s = _seed(post_type=POST_TYPE_LINK, url='https://example.com/a')
    _attach_image(s.post, source_url='https://example.com/pic.png',
                  file_path='app/static/posts/x.png',
                  thumbnail_path='app/static/posts/x_thumb.png')

    _send(s.post)


def test_the_image_url_falls_back_to_the_file_path(db_session):
    """:217-218, reached only when `source_url` is falsy. The stored path is
    rewritten from `app/static/` to the server's static URL."""
    s = _seed(post_type=POST_TYPE_LINK, url='https://example.com/a')
    _attach_image(s.post, source_url=None, file_path='app/static/posts/x.png')

    _send(s.post)


def test_the_image_url_falls_back_to_the_thumbnail_path(db_session):
    """:219-220, reached only when both `source_url` and `file_path` are
    falsy."""
    s = _seed(post_type=POST_TYPE_LINK, url='https://example.com/a')
    _attach_image(s.post, source_url=None, file_path=None,
                  thumbnail_path='app/static/posts/x_thumb.png')

    _send(s.post)


def test_the_image_url_stays_empty_when_the_file_has_no_paths(db_session):
    """:219's false arm -- all three columns falsy, so `image_url` keeps the
    `''` assigned at :214 and :221 emits it."""
    s = _seed(post_type=POST_TYPE_LINK, url='https://example.com/a')
    _attach_image(s.post, source_url=None, file_path=None, thumbnail_path=None)

    _send(s.post)
```

- [ ] **Step 2: Give these tests a real observable**

As written, all four tests assert nothing — they prove only that the function did not crash. **That is a defect by this project's rubric.** Before running them, add an observable.

`page` is a local, so it cannot be read directly. Two options; pick one and say which in your report:

- **Preferred:** make the community remote (`_seed(local_community=False)`) and give it an `ap_inbox_url`, so `:303`'s `send_post_request` fires; register that route with `http_mock` and assert on the captured request body's `object.image.url`. The sender needs `make_user(..., with_keys=True)`.
- **Fallback:** if wiring delivery proves too heavy for this task, `monkeypatch` `app.shared.tasks.pages.send_post_request` with a recorder and assert on the dict it receives.

Whichever you choose, every one of the four tests must assert the exact `image.url` string its arm produces, including the `app/static/` rewrite for the two path arms.

- [ ] **Step 3: Run**

Run: `./run_tests.sh tests/test_shared_tasks_send_post.py -q`
Expected: all pass, with each of the four asserting a distinct `image.url`.

- [ ] **Step 4: Commit**

```bash
git add tests/test_shared_tasks_send_post.py
git commit -F <message-file>
```

Subject: `test: cover send_post's image-url fallback chain`

---

### Task 4: Fix D299 — the unguarded image dereference at `:181`

**Files:**
- Modify: `app/shared/tasks/pages.py:178-181`
- Modify: `tests/test_shared_tasks_send_post.py` (append)

**The defect.**

```
178:     if post.type == POST_TYPE_LINK or post.type == POST_TYPE_VIDEO:
179:         attachment.append({'href': post.url, 'type': 'Link'})
180:     elif post.type == POST_TYPE_IMAGE:
181:         attachment.append({'type': 'Image', 'url': post.image.source_url, 'name': post.image.alt_text})
```

`:181` dereferences `post.image` with no `image_id` check. Two places disagree with it:

- `app/activitypub/util.py:172-177` wraps the identical dereference in `if post.image_id is not None:`.
- **`app/shared/tasks/pages.py:213`, thirty-two lines below, is `if post.image_id:`** — the same file, the same function, already using the guarded idiom.

The internal contradiction is the stronger argument: a fix here does not need the other file's permission, only consistency with the line below it.

- [ ] **Step 1: Write the failing test**

```python
# ---------------------------------------------------------------------------
# D299 -- the image attachment, :180-181
# ---------------------------------------------------------------------------


def test_an_image_post_with_no_image_row_does_not_crash(db_session):
    """D299. :181 dereferences `post.image` under `elif post.type ==
    POST_TYPE_IMAGE` with NO image_id check.

    Before the fix this raises `AttributeError: 'NoneType' object has no
    attribute 'source_url'`. The state is ordinary, not contrived:
    `edit_post` produces a POST_TYPE_IMAGE post with `image_id` None whenever
    the image path at app/shared/post.py:601-608 is not taken.

    :213 in this same function, thirty-two lines below, already guards the same
    dereference with `if post.image_id:`, and
    app/activitypub/util.py:172 guards it in the sibling builder.
    """
    s = _seed(post_type=POST_TYPE_IMAGE)
    assert s.post.image_id is None

    _send(s.post)
```

- [ ] **Step 2: Run it and record the exact failure**

Run: `./run_tests.sh "tests/test_shared_tasks_send_post.py::test_an_image_post_with_no_image_row_does_not_crash" -q`
Expected: FAIL with `AttributeError: 'NoneType' object has no attribute 'source_url'`.

Quote the exact line in your report. **If it fails with anything else, stop and report** — a different failure means the test does not reach `:181`, and a fix landed against a test that never exercised the defect proves nothing.

- [ ] **Step 3: Apply the fix**

Change `:180` to require an image row, matching `:213`'s idiom:

```python
    elif post.type == POST_TYPE_IMAGE and post.image_id:
```

**Argue the choice in your report.** The alternative is to keep `:180` and guard inside, emitting an empty attachment list. The nearest precedent, `app/activitypub/util.py:172-177`, **omits** the attachment entirely when there is no image, which is what the one-line `and post.image_id` above also does. State whether you followed that precedent and why.

Verify no line moved:

```bash
git diff --stat app/shared/tasks/pages.py
wc -l app/shared/tasks/pages.py
```

Expected: `1 file changed, 1 insertion(+), 1 deletion(-)` and `432`.

- [ ] **Step 4: Run the test again**

Expected: PASS.

- [ ] **Step 5: Add the test that proves the guard still admits a real image**

```python
def test_an_image_post_with_an_image_row_still_gets_its_attachment(db_session):
    """:180-181, the true arm -- the guard must not suppress a real image.

    Without this, `elif False:` would pass the test above and lose the feature.
    """
    s = _seed(post_type=POST_TYPE_IMAGE)
    _attach_image(s.post, source_url='https://example.com/pic.png',
                  alt_text='a picture')

    _send(s.post)
```

Give this test the same real observable Task 3 settled on, and assert the attachment's `url` and `name`.

- [ ] **Step 6: Mutation-prove the fix**

One at a time. After each: `git checkout -- app/`, then confirm **both** `git diff -- app/` empty and `wc -l app/shared/tasks/pages.py` = `432`.

| # | Mutation at `:180` | Expected |
|---|---|---|
| 1 | `elif post.type == POST_TYPE_IMAGE:` (revert) | crash-killed by `test_an_image_post_with_no_image_row_does_not_crash` |
| 2 | `elif False:` | assertion-killed by `test_an_image_post_with_an_image_row_still_gets_its_attachment` |
| 3 | `elif post.image_id:` (drop the type test) | see below |

For mutation 3, a LINK post takes `:178` first, so it may not be distinguishable. If no existing test kills it, add one whose post is neither LINK, VIDEO nor IMAGE but has an `image_id`, and assert no Image attachment is emitted. Record the result either way.

- [ ] **Step 7: Commit**

```bash
git add app/shared/tasks/pages.py tests/test_shared_tasks_send_post.py
git commit -F <message-file>
```

Subject: `fix: do not build an Image attachment for a post with no image`

The body must name `:213` as the in-function precedent, `app/activitypub/util.py:172` as the sibling-builder precedent, and state that this closes D299.

---

### Task 5: Fix D298 — three unguarded `ap_datetime` calls

**Files:**
- Modify: `app/shared/tasks/pages.py:222-233`
- Modify: `tests/test_shared_tasks_send_post.py` (append)

**The defect.** `ap_datetime` (`app/utils.py:2293-2294`) is `return date_time.isoformat() + '+00:00'` with no `None` guard. Three callers pass nullable fields:

```
224:         page['endTime'] = ap_datetime(poll.end_poll)
232:         page['startTime'] = ap_datetime(event.start)
233:         page['endTime'] = ap_datetime(event.end)
```

`Poll.end_poll` (`app/models.py:3782`), `Event.start` (`:3841`) and `Event.end` (`:3842`) are all nullable, and `edit_post` leaves them `None` whenever the input omits them (`app/shared/post.py:687`, `:705`).

**The arbitration, from spec §4.2.** Guard the callers and **omit the key**. Do NOT change `ap_datetime`: it has **29 call sites**, most passing non-nullable columns, and returning `None` would put `"endTime": null` into the activity — worse than an absent key, because a peer cannot distinguish a null from a missing value without knowing our schema. The precedent is `app/activitypub/util.py:168`, a sibling statement inside `post_to_page` that guards its own `updated` key by omitting it.

**Only `app/shared/tasks/pages.py` is fixed.** The three copies at `app/activitypub/util.py:195`, `:200` and `:201` are outside this sub-project's file and stay registered, with this arbitration attached.

- [ ] **Step 1: Write the three failing tests**

```python
# ---------------------------------------------------------------------------
# D298 -- ap_datetime on nullable fields, :224, :232, :233
# ---------------------------------------------------------------------------


def test_a_poll_with_no_end_time_does_not_crash(db_session):
    """D298 at :224. `ap_datetime` (app/utils.py:2294) is
    `return date_time.isoformat() + '+00:00'` with no None guard, and
    `Poll.end_poll` (app/models.py:3782) is nullable -- `edit_post` leaves it
    None whenever the input omits it (app/shared/post.py:687).

    Before the fix this raises
    `AttributeError: 'NoneType' object has no attribute 'isoformat'`.
    """
    s = _seed(post_type=POST_TYPE_POLL)
    db.session.add(Poll(post_id=s.post.id, end_poll=None, mode='single'))
    db.session.commit()

    _send(s.post)


def test_an_event_with_no_start_does_not_crash(db_session):
    """D298 at :232. `Event.start` (app/models.py:3841) is nullable."""
    s = _seed(post_type=POST_TYPE_EVENT)
    db.session.add(Event(post_id=s.post.id, start=None, end=None))
    db.session.commit()

    _send(s.post)


def test_an_event_with_a_start_but_no_end_does_not_crash(db_session):
    """D298 at :233, reached only once :232 is guarded. `Event.end`
    (app/models.py:3842) is nullable and `edit_post` leaves it None whenever
    the input omits it (app/shared/post.py:705).

    This is a separate test from the one above because the two guards are
    separately load-bearing: fixing :232 alone leaves this crashing.
    """
    from datetime import datetime
    s = _seed(post_type=POST_TYPE_EVENT)
    db.session.add(Event(post_id=s.post.id, start=datetime(2030, 6, 1, 9, 0),
                         end=None))
    db.session.commit()

    _send(s.post)
```

- [ ] **Step 2: Run them and record each exact failure**

Run: `./run_tests.sh tests/test_shared_tasks_send_post.py -q -k does_not_crash`
Expected: each of the three FAILS with `AttributeError: 'NoneType' object has no attribute 'isoformat'`.

Quote all three. If any fails differently, stop and report.

- [ ] **Step 3: Apply the fix**

Three guards, each omitting its key:

```python
        if poll.end_poll is not None:
            page['endTime'] = ap_datetime(poll.end_poll)
```

```python
        if event.start is not None:
            page['startTime'] = ap_datetime(event.start)
        if event.end is not None:
            page['endTime'] = ap_datetime(event.end)
```

This adds three lines, so the file becomes **435 lines**. That is expected here — unlike sub-project 18, no test file cites `app/shared/tasks/pages.py` by line number. **Verify that claim before relying on it:**

```bash
grep -rn 'tasks/pages.py:[0-9]' tests/ docs/
```

If anything cites a line at or below `:233`, re-derive and update it in this same commit, and say so in your report.

- [ ] **Step 4: Run the three tests again**

Expected: all three PASS.

- [ ] **Step 5: Add the tests that prove the keys are still emitted when the values exist**

```python
def test_a_poll_with_an_end_time_still_emits_endTime(db_session):
    """:224's true arm. Without this, `if False:` would pass the crash test and
    silently drop the key for every poll."""
    from datetime import datetime
    s = _seed(post_type=POST_TYPE_POLL)
    db.session.add(Poll(post_id=s.post.id, end_poll=datetime(2030, 6, 1, 12, 0),
                        mode='single'))
    db.session.commit()

    _send(s.post)


def test_an_event_with_both_times_still_emits_both_keys(db_session):
    """:232 and :233, both true arms."""
    from datetime import datetime
    s = _seed(post_type=POST_TYPE_EVENT)
    db.session.add(Event(post_id=s.post.id, start=datetime(2030, 6, 1, 9, 0),
                         end=datetime(2030, 6, 1, 10, 0)))
    db.session.commit()

    _send(s.post)
```

Give both the real observable Task 3 settled on, and assert the exact `startTime`/`endTime` strings. Also assert, in the three crash tests from Step 1, that the key is **absent** — `assert 'endTime' not in page` — because absence is the whole point of the arbitration and nothing else defends it.

- [ ] **Step 6: Mutation-prove the fix**

One at a time, with the tree verified between each.

| # | Mutation | Expected |
|---|---|---|
| 1 | `:224`'s guard → `if True:` | crash-killed by `test_a_poll_with_no_end_time_does_not_crash` |
| 2 | `:224`'s guard → `if False:` | assertion-killed by `test_a_poll_with_an_end_time_still_emits_endTime` |
| 3 | `:232`'s guard → `if True:` | crash-killed by `test_an_event_with_no_start_does_not_crash` |
| 4 | `:232`'s guard → `if False:` | assertion-killed by `test_an_event_with_both_times_still_emits_both_keys` |
| 5 | `:233`'s guard → `if True:` | crash-killed by `test_an_event_with_a_start_but_no_end_does_not_crash` |
| 6 | `:233`'s guard → `if False:` | assertion-killed by `test_an_event_with_both_times_still_emits_both_keys` |

Then re-run Task 4's three `:180` mutations, because `:181` sits in the same builder block and fact 74 requires it. Nine results total.

- [ ] **Step 7: Commit**

```bash
git add app/shared/tasks/pages.py tests/test_shared_tasks_send_post.py
git commit -F <message-file>
```

Subject: `fix: omit endTime and startTime rather than crash on a null timestamp`

The body must carry the arbitration: why the callers and not `ap_datetime` (29 call sites; a JSON null is worse than an absent key), the `app/activitypub/util.py:168` precedent, and that the three copies at `app/activitypub/util.py:195`, `:200`, `:201` stay open.

---

### Task 6: Create/Announce and community delivery

**Files:**
- Modify: `tests/test_shared_tasks_send_post.py` (append)

Target: `:247-305`, 9 statements / 9 branch arms. Note that `(267, 307)` is one of the two unreachable arms — do not chase it.

The uncovered statements are `:293-304`, the per-instance fan-out inside `:291`'s loop:

```
291:             for instance in set(community.following_instances() + user.following_instances(software='piefed')):
292:                 if instance.inbox and instance.online() and not user.has_blocked_instance(instance.id) and not instance_banned(instance.domain):
293:                     if instance.software in MICROBLOG_APPS:
294:                         if activity == 'create':
295:                             send_post_request(instance.inbox, microblog_announce, ...)
297:                             send_post_request(instance.inbox, create, ...)
299:                         send_post_request(instance.inbox, group_announce, ...)
300:                     if post.type != POST_TYPE_POLL:
301:                         domains_sent_to.append(instance.domain)
303:             send_post_request(community.ap_inbox_url, create, ...)
```

- [ ] **Step 1: Establish the delivery harness**

Every test in this task reaches `send_post_request`. Use the observable Task 3 settled on. The sender must be built with `make_user(..., with_keys=True)` — signing calls `.encode()` on the private key and a keyless sender dies before any request is attempted.

`_seed` does not take a `with_keys` parameter. Add one — `_seed(..., with_keys=False)` — passing it through to `make_user`, and say in your report that you extended the prelude. Do not create a second seeding helper.

- [ ] **Step 2: Write the delivery tests**

**Why this task specifies tests by name, arm and setup rather than by full code.** Every test here asserts on a captured outbound request, and the capture mechanism is whichever of the two options Task 3 settled on — respx routes or a `send_post_request` recorder. Writing the bodies out here would hard-code a choice Task 3 has not yet made when this plan is written. Read Task 3's committed tests first and follow their shape exactly; do not introduce a second capture mechanism alongside it.

Write one named test for each of these, each asserting on the captured request bodies:

| Test | Arm | Setup |
|---|---|---|
| `test_a_microblog_instance_gets_the_announce_on_create` | `:293` true, `:294` true → `:295` | a follower instance whose `software` is in `MICROBLOG_APPS`, `edit=False` |
| `test_a_microblog_instance_gets_the_create_directly_on_edit` | `:293` true, `:294` false → `:297` | same instance, `edit=True` |
| `test_a_lemmy_instance_gets_the_group_announce` | `:293` false → `:299` | a follower instance whose `software` is `lemmy` |
| `test_a_poll_does_not_mark_the_domain_as_sent_to` | `:300` false | `POST_TYPE_POLL`, and assert the mention fan-out at `:331-333` still reaches that domain |
| `test_a_non_poll_marks_the_domain_as_sent_to` | `:300` true → `:301` | `POST_TYPE_ARTICLE`, and assert `:332`'s guard suppresses a second send to the same domain |
| `test_a_remote_community_receives_the_create_at_its_inbox` | `:268` false → `:303` | `_seed(local_community=False)` with an `ap_inbox_url` |
| `test_an_offline_follower_instance_is_skipped` | `:292` second conjunct false | follower instance with `dormant = True` |
| `test_an_inboxless_follower_instance_is_skipped` | `:292` first conjunct false | follower instance with `inbox = None` |

Read `MICROBLOG_APPS` in `app/constants.py` and use a value from it verbatim rather than guessing. Read `Community.following_instances` (`app/models.py:1667`) and `User.following_instances` (`:842`) to learn what rows make an instance a follower, and seed accordingly — do not assume.

Each test's docstring must name the line and arm it takes.

- [ ] **Step 3: Run**

Run: `./run_tests.sh tests/test_shared_tasks_send_post.py -q`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_shared_tasks_send_post.py
git commit -F <message-file>
```

Subject: `test: cover send_post's Announce construction and per-instance delivery`

---

### Task 7: The Note amendment and follower delivery

**Files:**
- Modify: `tests/test_shared_tasks_send_post.py` (append)

Target: `:306-368`, 12 statements / 19 branch arms. `(330, 336)` is the second unreachable arm — do not chase it.

- [ ] **Step 1: Write the Note-amendment tests**

**Same note as Task 6:** the assertions here all read a captured outbound request, so follow the capture mechanism Task 3 settled on and Task 6 used. Do not introduce a third.

The amendment turns the Page into a Mastodon-friendly Note. Cover each conditional:

| Line | Arm to take |
|---|---|
| `:307` | **False arm only.** The true arm at `:308` is unreachable — `:257` always puts `@context` in `create`. |
| `:309` | **True arm only.** The false arm, arc `(309, 311)`, is unreachable — `:196` always puts `name` in `page`. Do not try to build a post without one; `:209-210` re-assigns the same key rather than being its only writer. |
| `:313` | `note['type'] == 'Page' or 'Event'` — both arms |
| `:315` | LINK or VIDEO |
| `:317` | not POLL |
| `:319` | EVENT |
| `:324` | `post_body_html` truthy and falsy |
| `:326` | `post.language_id` set and unset |

Read `:306-329` before writing, and write one named test per arm, each asserting on the amended body actually sent to a mentioned recipient at `:333`.

- [ ] **Step 2: Write the follower-delivery tests**

```
336:     followers = session.query(UserFollower).filter_by(local_user_id=post.user_id, is_inward=True).all()
337:     if not followers:
338:         return
341:     for follower in followers:
342:         user_details = session.query(User).get(follower.remote_user_id)
343:         if user_details:
344:             create['cc'].append(user_details.public_url())
346:     for instance in user.following_instances():
347:         if instance.domain not in domains_sent_to and instance.id != 1 and instance.software != 'piefed':
348:             if instance.inbox and instance.online() and not user.has_blocked_instance(instance.id) and not instance_banned(instance.domain):
349:                 send_post_request(instance.inbox, create, ...)
```

Cover: the early return at `:337-338` (already exercised by every earlier test — name which one and say so rather than adding a redundant test); `:343`'s true and false arms (a `UserFollower` row whose `remote_user_id` matches no `User` takes the false arm); and each of `:347`'s three conjuncts and `:348`'s four, one test per conjunct, each leaving the others satisfied.

`tests/factories.py` has `make_follow(local_user, remote_user, is_accepted=True, is_inward=False)`. Read it and pass `is_inward=True`, since `:336` filters on that column.

- [ ] **Step 3: Run**

Run: `./run_tests.sh tests/test_shared_tasks_send_post.py -q`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_shared_tasks_send_post.py
git commit -F <message-file>
```

Subject: `test: cover send_post's Note amendment and follower fan-out`

---

### Task 8: Residuals, the AST walk, and the unreachability argument

**Files:**
- Modify: `tests/test_shared_tasks_send_post.py` (append whatever the measurement demands)

- [ ] **Step 1: Ask the controller for the scoped measurement**

Do NOT run the full suite. Report to the controller that Task 8 is ready and needs the missing statements and missing branch arms within `app/shared/tasks/pages.py:88-368`. Starting point was 64 statements and 61 branch arms.

- [ ] **Step 2: Write a test for each remaining arm**

For each one the controller reports, write one named test whose docstring names the line and the arm. Follow the shapes already in the file.

**Four arms are excluded and must NOT be chased:** the false arms of `:267` and `:330` (arcs `(267, 307)` and `(330, 336)`), the true arm of `:307`, and the false arm of `:309` (arc `(309, 311)`). See Step 4.

If any other arm is genuinely unreachable, do not fake it — write the argument instead, and consult `tests/README.md` fact 75's catalogue of causes for an unkillable clause, naming which cause applies or describing a new one.

- [ ] **Step 3: Enumerate conditional expressions by AST walk**

coverage.py emits no arc for a conditional expression (fact 87), so the controller's numbers cannot see them.

```bash
python3 - <<'PY'
import ast
src = open('app/shared/tasks/pages.py').read()
tree = ast.parse(src)
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == 'send_post':
        for sub in ast.walk(node):
            if isinstance(sub, ast.IfExp):
                print(sub.lineno, ast.get_source_segment(src, sub))
PY
```

Reconcile every result against a named test taking each arm. `:137`'s `user.ap_id if user.ap_id else user.user_name` is covered by two tests from Task 2. `:247` and `:249` are `'create' if not edit else 'update'` and `'Create' if not edit else 'Update'` — both arms of each need a named test. Report the walk's raw output and your reconciliation.

- [ ] **Step 4: Write the unreachability argument for `:267` and `:330`**

Add this as a comment block in the test file, immediately above the delivery cluster, so the next reader finds it where the arms live:

```python
# ---------------------------------------------------------------------------
# FOUR UNREACHABLE ARMS, and why no test here chases them.
#
# `:267` and `:330` are both `if not community.local_only:`. Coverage reports
# the arcs (267, 307) and (330, 336) -- their false arms -- as missing, and
# they will stay missing.
#
# `community` is bound once at `:91` (`community = post.community`) and is
# never reassigned anywhere in `:88-368`. And `:153-154` is
# `if community.local_only or community.private: return`. So a community with
# `local_only` set returns at `:154`, long before `:267`; by the time control
# reaches `:267`, `community.local_only` is necessarily falsy, and
# `not community.local_only` is necessarily True.
#
# This is a guard whose condition an earlier return has already decided --
# see tests/README.md fact 75 for the catalogue of causes, and this
# sub-project's register entry for where it is recorded.
#
# Sub-project 18 set `community.local_only = True` in ten of its tests
# believing it skipped delivery at `:267`. It did not; it returned at `:154`.
# The workaround worked, for a different reason than the one written down.
#
# The other two are decided by an unconditional assignment rather than by an
# earlier return:
#
# `:307` is `if '@context' not in create:`. `create` is built at `:250-259`
# with `'@context': default_context()` at `:257`, and nothing removes it before
# `:307`, so the TRUE arm at `:308` can never run.
#
# `:309` is `if 'name' in page:`. `:196` sets `page['name']` unconditionally
# inside the dict literal, and nothing deletes it before `:309`, so the FALSE
# arm -- arc (309, 311) -- can never run. Note that `:209-210` re-assigns the
# same key for non-polls, which is a redundant statement, not a second writer:
# `:196` has already set it for every type.
# ---------------------------------------------------------------------------
```

Check `tests/README.md` fact 75 before writing, and name the cause it matches, or state that this is a new cause and describe it for Task 9 to add.

- [ ] **Step 5: Ask the controller to re-measure, then commit**

```bash
git add tests/test_shared_tasks_send_post.py
git commit -F <message-file>
```

Subject: `test: close send_post's residual arms and record two unreachable ones`

---

### Task 9: Register the findings, raise the floor, verify

**Files:**
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`
- Modify: `tests/README.md`
- Modify: `coverage_floors.ini`

- [ ] **Step 1: Establish the next free finding number**

```bash
grep -o 'D[0-9]\{3\}' docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md | sort -u | tail -5
```

Sub-project 18 ended at **D299**, so the first new number is **D300**. **Confirm against the output rather than trusting this sentence** — sub-project 18 used the wrong number throughout for want of exactly this check, and the register carries a note about it at the `D292`/`D286` distinction.

- [ ] **Step 2: Append the sub-project 19 section to the register**

Read sub-project 18's section first and match its structure. Write:

1. **Two defects fixed, test-first with mutation-proved tests — D298, D299.** For D298 state explicitly that only `app/shared/tasks/pages.py` is fixed and that `app/activitypub/util.py:195`, `:200` and `:201` remain open with the arbitration attached. Carry the arbitration verbatim: guard the callers not `ap_datetime` (29 call sites), omit the key rather than emit a null, precedent at `app/activitypub/util.py:168`.
2. **D300 — the `href`/`EVENT` divergence.** `app/shared/tasks/pages.py:178-179` emits `{'href': post.url}` for LINK and VIDEO with no `None` check and without `POST_TYPE_EVENT`; `app/activitypub/util.py:170` requires `post.url is not None` and includes EVENT. `Post.url` is nullable (`app/models.py:1712`) and `edit_post` writes `None` at `app/shared/post.py:326-327` and `:613`. **Registered, not fixed:** the `None` half and the `EVENT` half need separate arguments, and adding EVENT changes what peers receive for every event post on a live install.
3. **The two unreachable arms**, with the argument from Task 8 Step 4 and the correction it contains: sub-project 18's ten `local_only = True` workarounds worked by returning at `:154`, not by skipping delivery at `:267`. Record that those workarounds are now deletable and why this sub-project did not delete them (`tests/test_shared_post_edit.py` is not this sub-project's file, and removing them would require re-running that file's mutation tables).
4. **Two findings from the `name`/`@context` pair**, numbered after D300:
   - `app/shared/tasks/pages.py:209-210` is a **redundant statement**. `:196` already assigns `page['name'] = post.title` inside the dict literal for every post type; `:210` assigns the same value again for non-polls only, with no effect. Record it against fact 75's sixth cause — the statement-scoped one sub-project 16 added — as an instance found in production code rather than in a test.
   - `app/shared/tasks/pages.py:306`'s comment says "amend **copy** of the Create" and **no copy is made**. `:254` is `'object': page` and `:311` is `note = page`; both are references to one dict, so `:310`'s `del`, `:312`'s content reset and `:314`'s type change all mutate `create['object']` in place. Not a live defect: every send at `:295`, `:297`, `:299` and `:303` happens before `:306`, and `send_post_request` signs the body inside the call. Register it as a **latent hazard** with exactly that reasoning — a future edit that moved a send below `:306` would ship the mutated object silently.
5. **Anything else Task 8 found**, numbered after those.

Do a two-pass citation sweep over everything written (fact 100).

- [ ] **Step 3: Add the new harness facts to `tests/README.md`**

```bash
grep -o '^1[0-9][0-9]\.' tests/README.md | sort -u | tail -3
```

Sub-project 18 ended at fact 106 plus an extension to fact 89. Append from 107. Candidates, each written only if it actually bit:

- `send_post` has no usable default for `session`; `:89` dereferences it immediately.
- The four early returns at `:150`, `:154`, `:158`, `:161`, and that `community.local_only` returns at `:154` rather than skipping delivery at `:267`.
- `:336-338` is the natural stopping point before the network — no followers, no outbound request, no `http_mock` needed.
- A sender without `with_keys=True` dies at signing before any request is attempted.
- `search_for_user`'s two bare `except:` clauses at `:107-108` and `:113-114`, and what that means for a test asserting absence.
- Whether fact 75 gained a new cause from Task 8 Step 4.

- [ ] **Step 4: Ask the controller for the blended figure and add the floor**

`app/shared/tasks/pages.py` has **no entry** in `coverage_floors.ini`. It was 51.38% before this work. Ask the controller for the post-work blended `summary['percent_covered']`, then add one line in the file's existing ordering at the measured value **rounded down to a whole percent**.

- [ ] **Step 5: Verify the floor check passes**

```bash
python3 tests/check_coverage_floors.py scratch_full_cov.json coverage_floors.ini
```

Expected: exit 0, with `app/shared/tasks/pages.py` now among the modules listed. **Read the printed report mtime** — the script prints it because `scratch_full_cov.json` is gitignored and persists, so a run that failed to write it leaves the previous run's file in place and the ratchet would pass against stale data.

- [ ] **Step 6: Ask the controller for the full-suite run**

Report ready. The controller runs the suite once and reports pass/skip/subtest counts, wall time, and the coverage report's mtime. A run at or over 600s is a `session_timeout` truncation that still exits 0 — the controller checks the test count, not the exit code.

- [ ] **Step 7: Commit**

```bash
git add docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md tests/README.md coverage_floors.ini
git commit -F <message-file>
```

Subject: `docs: register sub-project 19's findings and set app/shared/tasks/pages.py's first floor`

---

## Success criteria

From spec §9, with criterion 2 amended by this plan's amendment section:

1. `tests/test_shared_tasks_send_post.py` exists and calls `send_post` directly.
2. `send_post` (`:88-368`) is at zero uncovered statements, and zero uncovered branch arms **except four**: the false arms of `:267` and `:330`, the true arm of `:307`, and the false arm of `:309`. All four are registered with a written unreachability argument.
3. Both `edit=False` and `edit=True` are exercised throughout the builder.
4. Every conditional expression in `:88-368` has both arms exercised by named tests, reconciled by an AST walk rather than by the coverage number.
5. D299 is fixed at `:180-181` and proved by a test that fails with `AttributeError: 'NoneType' object has no attribute 'source_url'` before the fix.
6. D298 is fixed at `:224`, `:232` and `:233`, each proved by a test that fails with `AttributeError: 'NoneType' object has no attribute 'isoformat'` before the fix, and the fix omits the key rather than emitting a null.
7. `ap_datetime` (`app/utils.py:2293-2294`) is unchanged.
8. `coverage_floors.ini` gains an `app/shared/tasks/pages.py` entry at the measured floor.
9. The register carries D300 onward; D298 and D299 are marked fixed, with D298's three `app/activitypub/util.py` copies recorded as still open.
10. The full suite is green, with the pass/skip counts and the coverage report's mtime recorded.
