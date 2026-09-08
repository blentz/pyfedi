# Sub-project 27: closing `app/shared/tasks/deletes.py` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take `app/shared/tasks/deletes.py` from 11.16% to 100% statement and branch coverage, land three production changes, close D309 at its twelfth and final site, and correct a register entry that currently describes this module wrongly.

**Architecture:** One new test file, `tests/test_shared_tasks_deletes.py`, built on the harness shape sub-projects 24-26 established. The module's engine is `delete_object`, which fans out over four delivery paths with three different signing actors. Tests assert on `ActivityPubLog` row counts (the only oracle that sees a spurious send), nested `@context` absences (the only ones that discriminate), and `keyId` URLs (the only observable separating signers).

**Tech Stack:** pytest, respx, SQLAlchemy 2.0.52, Celery with `task_always_eager`, podman-compose via `./run_tests.sh`.

**Spec:** `docs/superpowers/specs/2026-09-08-coverage-deletes-27-design.md`

## Global Constraints

- **Delete nothing the task did not create.** No `rm`, no file removal of any kind. `git checkout -- app/` is permitted ONLY as a mutation restore step.
- **Only the controller runs the full suite**, one pytest session at a time, in the FOREGROUND. Task implementers run only the test files they change.
- **`pytest` exits 1 on a session timeout and `run_tests.sh` propagates it.** A shell pipeline eats the status — read `${PIPESTATUS[0]}`, or do not pipe.
- **Coverage takes the dotted module form** (`--cov=app.shared.tasks.deletes`). A path form collects nothing, writes no JSON, and exits 0 — a silent green failure.
- **The coverage JSON lands INSIDE the `pyfedi_test-runner` container**, not the host bind mount. Retrieve with `podman cp`.
- **Test counts come from pytest's own collection output**, never from `grep -c '^def test_'`.
- **Before believing any failure, run `./run_tests.sh --down`.** This suite is sensitive to accumulated database state across consecutive runs.
- **Mutations:** one at a time, targeted single-line `sed`, dry-run WITHOUT `-i` and read the produced line first, apply, run, restore with `git checkout -- app/`, then assert both an empty `git diff -- app/` and the expected `wc -l`. **Restore before any point where you might stop and report.** Paste every dry-run line and every failure — a narrated mutation result is worth nothing.
- **Commit with `git commit -F <file>`, never `-m`.** Commit messages are normal English prose with a lowercase `type:` subject prefix (`test:`, `fix:`, `docs:`).
- End every commit message with:
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` then
  `Claude-Session: https://claude.ai/code/session_01BF7kVN2spWD86NYKWyGrVn`
- **No ordered assertions over rows a query planner returned.** `following_instances()` ends in an unordered `.distinct().all()`. Set-based or sorted. Order imposed by straight-line Python is fine.
- **Every line number re-derived against the current tree**, and citations into a file your own diff touches re-derived AFTER the diff is final.
- **Where prose describes a statement, cite that statement's line, not the `if` guarding it.**
- **A top-level `@context` assertion is vacuous.** `app/activitypub/signature.py:100-101` reinjects `@context` into any body lacking it. Only a NESTED absence discriminates.
- **A spurious send is invisible to a delivered-inboxes set and to a route call count.** `signature.py:143`'s `except Exception as e:` swallows respx's unmatched-request assertion into an `ActivityPubLog` failure row. Where you need "nothing else was sent", the oracle is a row count.
- **`post_request` writes its `ActivityPubLog` row unconditionally at `signature.py:105`**, before the transport and before the empty-uri check at `:109-111`. A send to a `None` inbox writes a row and makes no httpx request.

---

## File Structure

**Create:** `tests/test_shared_tasks_deletes.py` — the whole module's tests. One file, matching every sibling in this package.

**Modify:** `tests/factories.py` — gains `make_notification`.

**Modify:** `app/shared/tasks/deletes.py` — three production changes, 320 → 318 lines.

**Modify:** `coverage_floors.ini` — gains `app/shared/tasks/deletes.py = 100`, 18 entries.

**Modify:** `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` and `tests/README.md` — register and facts.

## Line-number sequencing — READ THIS BEFORE TASK 2

Task 2 lands PC3, which DELETES `deletes.py:205-206`. Every line below `:206` shifts by −2:

| Before | After | Statement |
|--------|-------|-----------|
| `:208` | `:206` | `if is_post and followers and not reason:` |
| `:213` | `:211` | `payload['cc'].append(...)` |
| `:214` | `:212` | the `Instance` join |
| `:215` | `:213` | `.filter(Instance.gone_forever == False)` |
| `:218` | `:216` | `send_post_request(instance.inbox, ...)` |
| `:221` | `:219` | `if is_post:` |
| `:222` | `:220` | the `Notification` query |
| `:252` | `:250` | `delete_object(...)` inside the batch task |
| `:293` | `:291` | `def delete_message` |
| `:320` | `:318` | the PM `send_post_request` |

Lines at or above `:204` are unaffected: `:133` (PC1), `:146`, `:155`, `:171`, `:186`, `:196`, `:197`, `:198`, `:201`, `:202`.

**Tasks 3 onward cite POST-SHIFT numbers.** Task 1 and Task 2 cite pre-shift numbers for anything below `:206` and must re-derive at the end of Task 2.

---

## Task 1: Open the test file with the delete harness

**Files:**
- Create: `tests/test_shared_tasks_deletes.py`

**Interfaces:**
- Produces: `_seed(local_community=True, with_keys=False)`, `_make_deliverable(s, online=True)`, `_follower(s, http_mock, inbox=PEER_INBOX, domain='follower.example')` returning `(route, instance)`, `_sent_activity(route, index=-1)`, `_delivered_inboxes(*routes)`, constants `PEER_INBOX`, `OTHER_INBOX`.

- [ ] **Step 1: Write the module docstring and harness**

```python
"""`delete_object` and its eight wrappers -- the AP Delete and Undo senders.

`app/shared/tasks/deletes.py`, 320 lines. Six `@celery.task` wrappers
(`delete_reply:29`, `restore_reply:44`, `delete_post:59`, `restore_post:74`,
`delete_community:89`, `restore_community:104`) delegating to
`delete_object:118`, plus `delete_posts_with_blocked_images:233` and a PM pair
(`delete_pm:262`, `restore_pm:278`) over `delete_message:293`.

FOUR DELIVERY PATHS AND THREE SIGNING ACTORS, which is one more path and one
more signer than any module this campaign has closed:
  `:196-199` local community -- an Announce per `following_instances()` row
      passing the FOUR-conjunct guard at `:197`, signed with the COMMUNITY key.
  `:201-203` remote community -- one direct send to `ap_inbox_url`, signed with
      the USER key.
  `:214-218` the author's own followers -- a raw `Instance` join, signed with
      the USER key, skipping any domain already in `domains_sent_to`.
  `:320` private messages -- one send to `recipient.ap_inbox_url`, signed with
      the SENDER key, after `:294`'s local-recipient early return.

THE GUARD AT `:127-134` IS SPLIT ACROSS THREE STATEMENTS, which is why this
site was misread by the campaign's own register. `:127` returns for a non-post
in a `local_only` community, `:130` returns for a `local_only` community when
the user has no followers, and `:133` returns for an offline instance. Only
`Community.private` is missing, so this is D309's NARROW shape and not the wide
one -- a distinction that survives only because the guard is read as a block.

`:197` CARRIES FOUR CONJUNCTS AND COVERAGE.PY SEES ONE ARC PAIR:
`instance.inbox`, `instance.online()`, `not user.has_blocked_instance(...)` and
`not instance_banned(...)`. Branch coverage reads 100% with three of them
untested. Every one is pinned by its own test and its own mutation.
"""

import json
import socket
from types import SimpleNamespace

import pytest

from app import db
from app.models import ActivityPubLog
from app.shared.tasks.deletes import (
    delete_community, delete_post, delete_reply, restore_community,
    restore_post, restore_reply,
)
from tests.factories import (
    make_community, make_community_member, make_instance, make_post, make_user,
)

PEER_INBOX = 'https://peer.example/inbox'
OTHER_INBOX = 'https://other.example/inbox'

_REAL_GETADDRINFO = socket.getaddrinfo
_EXAMPLE_TLD_ADDRESS = '93.184.216.34'


def _getaddrinfo_without_the_network(host, *args, **kwargs):
    """`socket.getaddrinfo`, answering for `.example` hosts without a resolver.

    Everything else is delegated to the real function unchanged.
    """
    if isinstance(host, str) and (host == 'example' or host.endswith('.example')):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '',
                 (_EXAMPLE_TLD_ADDRESS, 0))]
    return _REAL_GETADDRINFO(host, *args, **kwargs)


@pytest.fixture(autouse=True)
def _peer_example_resolves_without_a_resolver(monkeypatch):
    """Keep delivery off the machine's DNS resolver.

    THIS IS NOT OPTIONAL AND MUST NOT BE DELETED AS UNNECESSARY. Signing runs
    `is_invalid_get_request_uri`, which reaches `socket.getaddrinfo` at
    app/utils.py:5520 for POST as well as GET, and FAILS OPEN at :5521-5522.
    On a machine whose resolver hijacks NXDOMAIN into a wildcard A record,
    these tests start failing at app/utils.py:5530's `is_global` check instead.
    """
    monkeypatch.setattr(socket, 'getaddrinfo', _getaddrinfo_without_the_network)


def _seed(local_community=True, with_keys=False):
    """instance, user, community, post, reply -- committed.

    `user` both authors the content and performs the deletion, which is the
    author-delete shape; moderator deletes differ only by passing `reason`.
    `delete_object:119` loads the user and signs with their key on the remote
    and follower paths (`:202`, `:218`), and with the COMMUNITY's key on the
    local Announce path (`:198`) -- so `with_keys=True` supplies both.
    """
    instance = make_instance('test.piefed.local', software='piefed')
    user = make_user(instance, 'author', local=True, with_keys=with_keys)
    community = make_community('c1')
    if with_keys:
        community.private_key = user.private_key
        community.public_key = user.public_key
    if not local_community:
        community.ap_id = 'c1@peer.example'
        community.ap_profile_id = 'https://peer.example/c/c1'
        community.ap_public_url = 'https://peer.example/c/c1'
        community.ap_inbox_url = PEER_INBOX
        community.ap_domain = 'peer.example'
    db.session.commit()
    post = make_post(community, user, 'https://test.piefed.local/post/1')
    db.session.commit()
    return SimpleNamespace(instance=instance, user=user, community=community,
                           post=post)


def _make_deliverable(s, online=True):
    """Move the community off instance id 1 onto a real peer Instance.

    `:133` dereferences `community.instance` and returns when it is offline, so
    a community left on instance 1 -- which `make_instance` gives no inbox --
    still reaches the delivery paths, but `online=False` here is what exercises
    `:133`'s true arm. `Instance.online()` is exactly
    `not (self.dormant or self.gone_forever)` (app/models.py:118-119).
    Returns the peer.
    """
    peer = make_instance('follower-home.example', software='lemmy')
    if not online:
        peer.dormant = True
        peer.gone_forever = True
    s.community.instance_id = peer.id
    db.session.commit()
    return peer


def _sent_activity(route, index=-1):
    """The JSON body of the request `route` captured, decoded from the bytes
    that were actually sent. Defaults to the most recent call.
    """
    return json.loads(route.calls[index].request.content)


def _delivered_inboxes(*routes):
    """The SET of inboxes that received a request. Never a list, never ordered
    -- `following_instances()` ends in an unordered `.distinct().all()`."""
    return {str(r.calls[i].request.url) for r in routes for i in range(len(r.calls))}


def _follower(s, http_mock, inbox=PEER_INBOX, domain='follower.example'):
    """A remote instance `following_instances()` will actually return.

    THREE parts, and the third is the one that is easy to miss: an Instance
    row, an inbox, and a USER ON THAT INSTANCE WHO IS A MEMBER OF THE
    COMMUNITY. `Community.following_instances()` (app/models.py:842-851) joins
    `CommunityMember`; without the membership the query returns nothing,
    `:196`'s loop never runs, and every delivery assertion passes against zero
    deliveries.

    THIS IS NOT THE FIXTURE PATH 3 NEEDS. The follower fan-out at `:214-218`
    joins `UserFollower`, not `CommunityMember`, so a recipient built here is
    invisible to it and vice versa. Task 9 builds that one separately.

    Returns `(route, instance)`.
    """
    inst = make_instance(domain, software='lemmy')
    inst.inbox = inbox
    member_user = make_user(inst, f'member_{domain.split(".")[0]}')
    make_community_member(member_user, s.community)
    db.session.commit()
    return http_mock.post(inbox).respond(200, json={}), inst
```

- [ ] **Step 2: Write the two smoke tests**

```python
def test_delete_post_announces_to_the_communitys_followers(db_session, http_mock):
    """`delete_post:59` on a LOCAL community: the guard at `:127-134`, the
    Delete envelope at `:147-156`, and the Announce loop at `:196-199`.

    The nested `@context` absence discriminates -- `:181` deletes it from the
    Delete before `:191` nests it, and `signature.py:100-101`'s reinjection
    reaches the top level only.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    delete_post(None, s.user.id, s.post.id)

    announce = _sent_activity(route)
    assert announce['type'] == 'Announce'
    assert announce['object']['type'] == 'Delete'
    assert announce['object']['object'] == s.post.public_url()
    assert '@context' not in announce['object']


def test_a_remote_community_delete_is_sent_direct(db_session, http_mock):
    """`:201-203`. `community.is_local()` is False, so no Announce is built and
    the Delete goes straight to `community.ap_inbox_url`.

    NO `@context` ASSERTION BELONGS ON THIS ACTIVITY. `:181`'s
    `del delete['@context']` sits inside the `is_local()` branch, so the remote
    path ships whatever `:152` set -- and the Delete is the top-level object
    here, exactly where `signature.py:100-101` reinjects. Both a presence and
    an absence assertion would be non-discriminating.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    route = http_mock.post(PEER_INBOX).respond(200, json={})

    delete_post(None, s.user.id, s.post.id)

    delete = _sent_activity(route)
    assert delete['type'] == 'Delete'
    assert delete['actor'] == s.user.public_url()
    assert delete['audience'] == s.community.public_url()
```

- [ ] **Step 3: Run and confirm both pass**

Run: `./run_tests.sh tests/test_shared_tasks_deletes.py -q`
Expected: `2 passed`. If either fails, read the failure before changing anything — a zero-delivery pass is the failure mode this harness exists to prevent, and `http_mock`'s `assert_all_called=True` (tests/conftest.py:342) plus `_sent_activity`'s `route.calls[-1]` both catch it.

- [ ] **Step 4: Commit**

Subject: `test: open tests/test_shared_tasks_deletes.py with the Delete harness`

---

## Task 2: PC3 — free the notification cleanup, and land the line shift

**Files:**
- Modify: `tests/factories.py`
- Modify: `app/shared/tasks/deletes.py:205-208`
- Modify: `tests/test_shared_tasks_deletes.py`

**Interfaces:**
- Consumes: `_seed`, `_make_deliverable`, `_follower` from Task 1.
- Produces: `make_notification(user, post, notif_type=NOTIF_POST)` in `tests/factories.py`; `deletes.py` at 318 lines with every citation below `:206` shifted by −2.

**Context:** `:205-206` is `if reason: return`. `reason` is set only on moderator paths (`app/shared/post.py:1065`, `:1107`; `app/shared/reply.py:428`, `:464`); the author's own delete passes none. So the early return suppresses the follower fan-out for moderator removals, which looks deliberate. The notification cleanup at `:221-228` is merely downstream of the same `return`, which is not. A moderator-removed post keeps its notifications; an author-deleted one loses them.

- [ ] **Step 1: Add the notification factory**

Append to `tests/factories.py`, after `make_notification_subscription`:

```python
def make_notification(user: User, post: Post, notif_type: int = NOTIF_POST,
                      title: str = 'a notification') -> Notification:
    """A Notification whose `targets` names `post`, the shape
    `delete_object`'s cleanup query reads.

    That query is `Notification.targets.op("->>")("post_id").cast(Integer) ==
    object.id`, so `targets` must be a JSON object carrying `post_id` -- a
    plain integer column would not match, and neither would a nested shape.
    `notif_type` matters because the cleanup SKIPS `NOTIF_REPORT` and
    `NOTIF_REPORT_ESCALATION` rows, so a test pinning the skip passes one of
    those and a test pinning the delete passes anything else.
    """
    notification = Notification(
        title=title,
        user_id=user.id,
        author_id=user.id,
        notif_type=notif_type,
        targets={'post_id': post.id},
    )
    db.session.add(notification)
    db.session.commit()
    return notification
```

Add `Notification` to the `app.models` import list in that file, and `NOTIF_POST`, `NOTIF_REPORT` to the `app.constants` imports. Re-derive both import lines against the file as it stands — do not assume either exists already.

- [ ] **Step 2: Write the three failing tests**

```python
def test_a_moderator_delete_still_clears_notifications(db_session, http_mock):
    """`:221-228`'s cleanup, which `:205-206`'s early return used to skip.

    A moderator delete passes `reason`; an author delete does not. Before this
    commit the `reason` return at `:205` fired first, so a moderated removal
    left its notifications pointing at content that no longer exists -- the
    asymmetry running the wrong way, since moderated removals are exactly where
    a stale notification matters.

    THE ROW COUNT IS OVER `Notification`, NOT `ActivityPubLog`. The delivery
    assertion here would pass either way: `:205` returns after both the
    Announce loop and the direct send have already run.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)
    make_notification(s.user, s.post)

    delete_post(None, s.user.id, s.post.id, reason='spam')

    assert db.session.query(Notification).count() == 0
    assert len(route.calls) == 1


def test_an_author_delete_clears_notifications_too(db_session, http_mock):
    """The control. Without it, the test above cannot distinguish "the reason
    return no longer blocks the cleanup" from "the cleanup runs
    unconditionally and always did"."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)
    make_notification(s.user, s.post)

    delete_post(None, s.user.id, s.post.id)

    assert db.session.query(Notification).count() == 0
    assert len(route.calls) == 1


def test_a_report_notification_survives_the_delete(db_session, http_mock):
    """`:225-226`'s `continue`, the one arm of the cleanup loop that keeps a
    row. Two notifications on the same post, one of them a report: the report
    survives and the other does not, so a mutation removing the `continue`
    fails on the count rather than on which row happens to remain."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)
    make_notification(s.user, s.post)
    make_notification(s.user, s.post, notif_type=NOTIF_REPORT)

    delete_post(None, s.user.id, s.post.id)

    assert db.session.query(Notification).count() == 1
    assert db.session.query(Notification).one().notif_type == NOTIF_REPORT
    assert len(route.calls) == 1
```

Add `Notification` to the test file's `app.models` import and `NOTIF_REPORT` to a new `app.constants` import; add `make_notification` to the `tests.factories` import list.

- [ ] **Step 3: Run and READ THE FAILURE**

Run: `./run_tests.sh tests/test_shared_tasks_deletes.py -q`
Expected: `test_a_moderator_delete_still_clears_notifications` fails with `assert 1 == 0`. The other two pass — they exercise the no-reason path, which already reaches the cleanup. PASTE the failure.

- [ ] **Step 4: Make the production change**

Delete `app/shared/tasks/deletes.py:205-206`:

```python
    if reason:
        return
```

and change what was `:208` to:

```python
    if is_post and followers and not reason:
```

Keep the blank line structure intact. The file goes from 320 to 318 lines.

- [ ] **Step 5: Run** — expect `5 passed`.

- [ ] **Step 6: Verify the shift**

Run and PASTE:
- `git diff --numstat -- app/shared/tasks/deletes.py` — expect `1	3	app/shared/tasks/deletes.py`
- `wc -l app/shared/tasks/deletes.py` — expect `318`
- `sed -n '206p;216p;219p;250p;291p' app/shared/tasks/deletes.py` — confirm these now carry the statements the plan's sequencing table predicts.

- [ ] **Step 7: Re-derive Task 1's citations**

Task 1's module docstring cites `:233`, `:262`, `:278`, `:293`, `:214-218`, `:320`. All are below `:206` and have shifted. Open each and correct it. This is the single most-repeated defect in this campaign — four occurrences, twice inside the commit that caused the shift.

- [ ] **Step 8: Commit**

Subject: `fix: let a moderated delete clear its notifications`

---

## Task 3: PC1 — the `private` disjunct, and D309's last site

**Files:**
- Modify: `app/shared/tasks/deletes.py:133`
- Modify: `tests/test_shared_tasks_deletes.py`

**Interfaces:**
- Consumes: everything from Tasks 1-2. `deletes.py` is 318 lines.

**Context:** `:133` reads `if not community.instance.online():`. The two `local_only` guards above it are conditional on `is_post` and on `followers`, so `private` cannot join them — it goes on `:133`, the unconditional guard.

- [ ] **Step 1: Write the four tests**

```python
def test_a_local_only_community_sends_no_reply_delete(db_session, http_mock):
    """`:127`'s guard, reached only for a NON-post -- `not is_post` is its first
    conjunct, so a post never returns here.

    Zero deliveries, so the oracle is the `ActivityPubLog` count: `post_request`
    writes its row at `signature.py:105` before the transport, and a
    delivered-inboxes assertion cannot see a send that respx never matched
    (`signature.py:143` swallows it).
    """
    s = _seed(with_keys=True)
    peer = _make_deliverable(s)
    reply = make_post_reply(s.post, s.user)
    s.community.local_only = True
    db.session.commit()

    delete_reply(None, s.user.id, reply.id)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_local_only_community_sends_no_post_delete_without_followers(
        db_session, http_mock):
    """`:130`'s guard. A POST passes `:127` (its `not is_post` conjunct is
    False) and is stopped here instead, because the author has no
    `UserFollower` rows and the community is `local_only`.

    The pair `:127`/`:130` is why this function contributes two lines to D309
    and counts once as a site."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    s.community.local_only = True
    db.session.commit()

    delete_post(None, s.user.id, s.post.id)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_private_community_sends_no_delete(db_session, http_mock):
    """D309's TWELFTH AND FINAL SITE.

    `private` is seeded with `local_only` deliberately left False -- with it
    True the test would pass on `:127`/`:130`'s pre-existing conjuncts and
    prove nothing about the new one. That is the same trap every earlier D309
    site's test was built to avoid, and `app/admin/routes.py:1388` makes the
    uncoupled state reachable today: it writes `community.local_only` from the
    admin form without touching `community.private`.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    s.community.private = True
    db.session.commit()

    delete_post(None, s.user.id, s.post.id)

    assert db.session.query(ActivityPubLog).count() == 0


def test_an_offline_community_instance_sends_no_delete(db_session, http_mock):
    """`:133`'s other disjunct, the one that was already there. Separated from
    the `private` test because the two fail independently."""
    s = _seed(with_keys=True)
    _make_deliverable(s, online=False)

    delete_post(None, s.user.id, s.post.id)

    assert db.session.query(ActivityPubLog).count() == 0
```

Add `make_post_reply` to the `tests.factories` import list.

- [ ] **Step 2: Run and READ THE FAILURE**

Expected: `test_a_private_community_sends_no_delete` fails with `assert 1 == 0`. The other three pass. PASTE the failure.

- [ ] **Step 3: Make the production change**

`app/shared/tasks/deletes.py:133`, from `if not community.instance.online():` to:

```python
    if community.private or not community.instance.online():
```

`private` goes FIRST. `Community.instance_id` is a nullable FK (`app/models.py:575`), and `or` short-circuits left to right, so a private community with no instance row returns at the guard rather than raising `AttributeError`. No test asserts that; reordering would reopen it without failing here.

- [ ] **Step 4: Run** — expect `9 passed`.

- [ ] **Step 5: Verify** — BEFORE committing, PASTE `git diff --numstat` (expect `1	1	app/shared/tasks/deletes.py`) and `wc -l app/shared/tasks/deletes.py` (expect `318`). Do not use `git diff --numstat -- app/...` after committing: that reads the working tree against HEAD and is empty once the change is in, which makes it look like nothing changed.

- [ ] **Step 6: Commit**

Subject: `fix: stop delete_object federating a private community`

---

## Task 4: PC2 — the crash in `delete_posts_with_blocked_images`

**Files:**
- Modify: `app/shared/tasks/deletes.py:250`
- Modify: `tests/test_shared_tasks_deletes.py`

**Context:** `delete_object`'s signature is `(..., session=None)` at `:118` and `:119` is `user = session.query(User).get(user_id)`. The batch task at what is now `:250` is the only one of seven call sites that omits `session=`. `deletes.py` never references `db.session`, so `patch_db_session` does not rescue it. Every call raises `AttributeError: 'NoneType' object has no attribute 'query'` — after `:247`'s `file.delete_from_disk()` and `:248`'s commit have already run for the first post.

- [ ] **Step 1: Write the two tests**

```python
def test_the_blocked_image_batch_deletes_every_post(db_session, http_mock):
    """`delete_posts_with_blocked_images:231`, which raised `AttributeError` on
    EVERY call before this commit.

    TWO posts, because the crash was partial rather than total: the loop
    commits the first post's deletion at `:248` and unlinks its file at `:247`
    before `:250` raises, so a one-post batch would have shown a deleted post
    and a raised task -- the same visible state a successful delete of one post
    leaves. The second post is what distinguishes them.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)
    second = make_post(s.community, s.user, 'https://test.piefed.local/post/2')
    db.session.commit()

    delete_posts_with_blocked_images([s.post.id, second.id], s.user.id, False)

    assert s.post.deleted is True
    assert second.deleted is True
    assert _delivered_inboxes(route) == {PEER_INBOX}
    assert len(route.calls) == 2


def test_the_blocked_image_batch_skips_a_missing_post(db_session, http_mock):
    """`:238`'s `if post:` false arm. A post id that no longer exists is
    skipped rather than raising, and the surviving id is still processed."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    delete_posts_with_blocked_images([999999, s.post.id], s.user.id, False)

    assert s.post.deleted is True
    assert len(route.calls) == 1
```

Add `delete_posts_with_blocked_images` to the `app.shared.tasks.deletes` import list.

- [ ] **Step 2: Run and READ THE FAILURE**

Expected: both fail with `AttributeError: 'NoneType' object has no attribute 'query'`, propagated out of the wrapper by `:253-255`'s `except Exception: session.rollback(); raise`. PASTE the traceback's final line for each.

- [ ] **Step 3: Make the production change**

`app/shared/tasks/deletes.py:250`, from

```python
                        delete_object(user_id, post, is_post=True, reason='Contains blocked image')
```

to

```python
                        delete_object(user_id, post, is_post=True, reason='Contains blocked image', session=session)
```

matching all six other call sites in the file.

- [ ] **Step 4: Run** — expect `11 passed`.

- [ ] **Step 5: Verify** — PASTE `git diff --numstat` (expect `1	1	app/shared/tasks/deletes.py`) and `wc -l` (expect `318`).

- [ ] **Step 6: Commit**

Subject: `fix: pass the task session into delete_object from the blocked-image batch`

---

## Task 5: The six wrappers, and their rollback

**Files:**
- Modify: `tests/test_shared_tasks_deletes.py`

**Interfaces:**
- Produces: `_recording_task_session(monkeypatch)` — the TENTH copy in this suite.

**Context:** Sub-project 26 shipped four rollback tests asserting only `pytest.raises`, and a mutation replacing `session.rollback()` with `pass` survived all of them. The assertion that discriminates is on the recorded call ORDER.

- [ ] **Step 1: Add the recording helper**

```python
def _recording_task_session(monkeypatch):
    """Make `get_task_session` hand back a GENUINE Session that records
    `rollback()` and `close()`.

    THE TENTH COPY of a helper that also lives in
    tests/test_shared_tasks_flags.py, test_shared_tasks_likes.py,
    test_shared_tasks_locks.py, test_shared_tasks_send_answer.py,
    test_shared_tasks_add_remove.py, test_shared_tasks_send_reply.py,
    test_shared_tasks_send_post.py, test_shared_tasks_groups.py and
    test_shared_tasks_blocks.py. Duplicated rather than imported: this campaign
    keeps its test modules independent so a helper can be edited for one
    function's needs without silently changing another's assertions. D324
    tracks the count.

    The session is real -- only the observation is added, by wrapping the two
    methods rather than replacing the object. A fake session would prove the
    wrapper calls methods on a mock; this proves it calls them on the session
    the function actually used.

    THE PATCH TARGET IS THE DELETES MODULE, NOT `app.utils`. `deletes.py:5`
    imports `get_task_session` into the deletes namespace and every wrapper
    resolves it there. Patching `app.utils.get_task_session` would apply
    cleanly, observe nothing, and leave the assertion trivially true against an
    empty list.
    """
    import app.shared.tasks.deletes as deletes_module

    calls = []
    real = deletes_module.get_task_session()

    original_rollback = real.rollback
    original_close = real.close

    def recording_rollback():
        calls.append('rollback')
        return original_rollback()

    def recording_close():
        calls.append('close')
        return original_close()

    monkeypatch.setattr(real, 'rollback', recording_rollback, raising=False)
    monkeypatch.setattr(real, 'close', recording_close, raising=False)
    monkeypatch.setattr(deletes_module, 'get_task_session', lambda: real)
    return SimpleNamespace(calls=calls, session=real)
```

**Read `tests/test_shared_tasks_groups.py`'s copy before writing this one** and match its construction exactly — the version above sketches the shape, and the sibling is the authority on how the real session is obtained. Delete the `db.create_scoped_session` line if the sibling does not use it.

- [ ] **Step 2: Write the six wrapper tests**

```python
@pytest.mark.parametrize('task, kwarg', [
    (delete_reply, 'reply_id'),
    (restore_reply, 'reply_id'),
    (delete_post, 'post_id'),
    (restore_post, 'post_id'),
    (delete_community, 'community_id'),
    (restore_community, 'community_id'),
])
def test_each_wrapper_rolls_back_and_reraises(db_session, monkeypatch, task, kwarg):
    """All six wrappers' `except Exception: session.rollback(); raise`.

    An id that does not exist makes the wrapper's own query raise -- `.one()`
    raises `NoResultFound` for the reply and community wrappers, and `.get()`
    returns None for the post wrappers, whose `delete_object` then raises
    `AttributeError` on `object.community`. Both propagate through the same
    `except`.

    `record.calls == ['rollback', 'close']` IS THE ASSERTION. A
    `pytest.raises` alone passes just as well when `session.rollback()` is
    replaced by `pass`, which is exactly the defect sub-project 26 shipped in
    four tests and had to fix in a later round.
    """
    record = _recording_task_session(monkeypatch)

    with pytest.raises(Exception):
        task(None, 1, **{kwarg: 999999})

    assert record.calls == ['rollback', 'close']
```

- [ ] **Step 3: Write the happy-path wrapper tests**

```python
def test_restore_post_sends_an_undo(db_session, http_mock):
    """`restore_post:74` -> `is_restore=True`. `:160-172` wraps the Delete in
    an Undo, and on the LOCAL path `:178` strips the Undo's `@context` and
    `:161` has already stripped the Delete's, so the Announce at `:192` is the
    only level that carries one.

    BOTH nested absences are asserted and both fail independently -- this is
    the campaign's first activity with `@context` absences at two depths.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    restore_post(None, s.user.id, s.post.id)

    announce = _sent_activity(route)
    assert announce['object']['type'] == 'Undo'
    assert '@context' not in announce['object']
    assert announce['object']['object']['type'] == 'Delete'
    assert '@context' not in announce['object']['object']


def test_delete_community_addresses_the_community_itself(db_session, http_mock):
    """`delete_community:89`, whose object IS the community -- `:120-121` takes
    the `isinstance(object, Community)` arm rather than reading
    `object.community`."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    delete_community(None, s.user.id, s.community.id)

    announce = _sent_activity(route)
    assert announce['object']['object'] == s.community.public_url()


def test_restore_reply_sends_an_undo_of_a_reply_delete(db_session, http_mock):
    """`restore_reply:44`, the `is_restore` arm on a NON-post. Distinguishes
    `:120`'s else arm (`object.community`) reached from the reply wrappers."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)
    reply = make_post_reply(s.post, s.user)
    db.session.commit()

    restore_reply(None, s.user.id, reply.id)

    announce = _sent_activity(route)
    assert announce['object']['type'] == 'Undo'
    assert announce['object']['object']['object'] == reply.public_url()
```

- [ ] **Step 4: Run** — expect `20 passed` (11 + 6 parametrised + 3).

- [ ] **Step 5: Prove the rollback assertion discriminates**

One mutation, with every step pasted:
1. `git status --porcelain` — confirm clean.
2. Dry-run WITHOUT `-i`: `sed -n "36s/session\.rollback()/pass/p" app/shared/tasks/deletes.py` — paste the produced line and read it. Re-derive `36` first: it is the `session.rollback()` inside `delete_reply`.
3. Apply to all six wrapper rollbacks with one `sed -i` naming each line explicitly.
4. Run the file in the FOREGROUND, unpiped. PASTE the failures — expect the six parametrised cases to fail on `assert [] == ['rollback', 'close']` or `assert ['close'] == ['rollback', 'close']`.
5. Restore with `git checkout -- app/`; PASTE `git diff --stat -- app/` (empty) and `wc -l app/shared/tasks/deletes.py` (318).

- [ ] **Step 6: Commit**

Subject: `test: cover the six delete wrappers and pin their rollback ordering`

---

## Task 6: `:197`'s four conjuncts

**Files:**
- Modify: `tests/test_shared_tasks_deletes.py`

**Context:** `:197` is `if instance.inbox and instance.online() and not user.has_blocked_instance(instance.id) and not instance_banned(instance.domain):`. coverage.py records ONE arc pair for the whole `if`, so branch coverage reads 100% with three conjuncts untested. Each gets its own test and, in Task 11, its own mutation.

- [ ] **Step 1: Write the four tests**

```python
def test_a_following_instance_without_an_inbox_is_skipped(db_session, http_mock):
    """`:197`'s FIRST conjunct. A send to a None inbox takes
    `signature.py:109`'s `empty uri` arm: it writes an `ActivityPubLog` row and
    makes NO httpx request, so respx sees nothing and only the row count can
    tell the two cases apart.

    A second, deliverable follower proves the loop CONTINUES rather than
    aborting on the first skip."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    dud = make_instance('inboxless.example', software='lemmy')
    dud.inbox = None
    make_community_member(make_user(dud, 'member_inboxless'), s.community)
    db.session.commit()
    route, _good = _follower(s, http_mock, inbox=OTHER_INBOX,
                             domain='good.example')

    delete_post(None, s.user.id, s.post.id)

    assert _delivered_inboxes(route) == {OTHER_INBOX}
    assert db.session.query(ActivityPubLog).count() == 1


def test_a_dormant_following_instance_is_skipped(db_session, http_mock):
    """`:197`'s SECOND conjunct, `instance.online()`. NO ROUTE IS REGISTERED
    for the dormant instance -- under `assert_all_called=True` a registered
    route that never fires fails the test for the wrong reason, and an
    unmatched request would NOT fail it at all (`signature.py:143` swallows it
    into a failure row). The row count is the oracle."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    dormant = make_instance('dormant.example', software='lemmy')
    dormant.inbox = OTHER_INBOX
    dormant.dormant = True
    make_community_member(make_user(dormant, 'member_dormant'), s.community)
    db.session.commit()
    route, _good = _follower(s, http_mock)

    delete_post(None, s.user.id, s.post.id)

    assert _delivered_inboxes(route) == {PEER_INBOX}
    assert db.session.query(ActivityPubLog).count() == 1


def test_an_instance_the_user_blocked_is_skipped(db_session, http_mock):
    """`:197`'s THIRD conjunct, `not user.has_blocked_instance(instance.id)`.

    The block belongs to the DELETING user, not to the community and not to the
    instance's own users -- `has_blocked_instance` reads `InstanceBlock` rows
    keyed on `user_id`."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    blocked = make_instance('blocked.example', software='lemmy')
    blocked.inbox = OTHER_INBOX
    make_community_member(make_user(blocked, 'member_blocked'), s.community)
    make_instance_block(s.user, blocked)
    db.session.commit()
    route, _good = _follower(s, http_mock)

    delete_post(None, s.user.id, s.post.id)

    assert _delivered_inboxes(route) == {PEER_INBOX}
    assert db.session.query(ActivityPubLog).count() == 1


def test_a_site_banned_instance_is_skipped(db_session, http_mock):
    """`:197`'s FOURTH conjunct, `not instance_banned(instance.domain)`.

    `instance_banned` reads the `BannedInstances` table by DOMAIN, which is the
    site-wide ban rather than the per-user block the third conjunct reads. The
    two are different tables and different scopes; a test for one does not pin
    the other."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    banned = make_instance('banned.example', software='lemmy')
    banned.inbox = OTHER_INBOX
    make_community_member(make_user(banned, 'member_banned'), s.community)
    make_banned_instance('banned.example')
    db.session.commit()
    route, _good = _follower(s, http_mock)

    delete_post(None, s.user.id, s.post.id)

    assert _delivered_inboxes(route) == {PEER_INBOX}
    assert db.session.query(ActivityPubLog).count() == 1
```

Add `make_instance_block` and `make_banned_instance` to the `tests.factories` import list.

- [ ] **Step 2: Run** — expect `24 passed`.

- [ ] **Step 3: Commit**

Subject: `test: pin each of the four conjuncts guarding the Announce loop`

---

## Task 7: Signer identity across all four paths

**Files:**
- Modify: `tests/test_shared_tasks_deletes.py`

**Interfaces:**
- Produces: `_key_id_of(route, index=-1)` — the SEVENTH copy in this suite.

**Context:** Sub-project 26's final whole-branch review found six signer sites asserted in prose and pinned by none, because both `_seed` helpers copied the sender's keypair onto the community and no test read the `keyId`. The key MATERIAL being identical does not matter: the declared `keyId` is a URL, and `user.public_url()` and `community.public_url()` differ.

- [ ] **Step 1: Add the helper**

```python
def _key_id_of(route, index=-1):
    """The `keyId` the captured request was signed under.

    The only observable separating `:198`'s signer (the COMMUNITY) from
    `:202`'s and `:216`'s (the USER) and `:318`'s (the message SENDER). respx
    never verifies a signature, so the key material leaves no trace on the
    wire -- only the declared keyId does, which is why `_seed` copying the
    user's keypair onto the community does not make these assertions vacuous.
    """
    return route.calls[index].request.headers['signature'].split('"')[1]
```

- [ ] **Step 2: Write the three signer tests**

```python
def test_the_announce_is_signed_by_the_community(db_session, http_mock):
    """`:198`. The local path signs as the COMMUNITY, because the Announce is
    the community's activity even though the Delete inside it is the user's."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    delete_post(None, s.user.id, s.post.id)

    assert _key_id_of(route) == s.community.public_url() + '#main-key'


def test_the_remote_delete_is_signed_by_the_user(db_session, http_mock):
    """`:202`. No Announce is built, so the USER signs their own Delete."""
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    route = http_mock.post(PEER_INBOX).respond(200, json={})

    delete_post(None, s.user.id, s.post.id)

    assert _key_id_of(route) == s.user.public_url() + '#main-key'


def test_the_two_paths_sign_differently(db_session, http_mock):
    """The discriminator for the pair above. Without it, a mutation swapping
    BOTH signers at once would leave each test above passing against the other
    site's actor if the two URLs ever converged."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    delete_post(None, s.user.id, s.post.id)

    assert _key_id_of(route) != s.user.public_url() + '#main-key'
```

- [ ] **Step 3: Run** — expect `27 passed`.

- [ ] **Step 4: Commit**

Subject: `test: pin the signing actor on the Announce and remote delete paths`

---

## Task 8: The four `@context` nesting shapes

**Files:**
- Modify: `tests/test_shared_tasks_deletes.py`

**Context:** `:181`'s `del delete['@context']` and `:178`'s `del undo['@context']` both sit INSIDE the `is_local()` branch, so the remote path ships whatever `:152` and `:168` set. `signature.py:100-101` reinjects at the top level only.

| Case | Top level | Nested |
|------|-----------|--------|
| Remote, delete | `delete`, has one (`:152`) | — |
| Remote, restore | `undo`, has one (`:168`) | `delete` absent (`:161`) |
| Local, delete | `announce` (`:192`) | `delete` absent (`:181`) |
| Local, restore | `announce` (`:192`) | `undo` absent (`:178`), `delete` absent (`:161`) |

- [ ] **Step 1: Write the remote-restore test**

The other three cases are already covered by Task 1's smoke tests and Task 5's `test_restore_post_sends_an_undo`. Only the remote-restore shape has no test.

```python
def test_a_remote_restore_nests_a_context_free_delete(db_session, http_mock):
    """The fourth shape. `:161` strips the Delete's `@context` before `:167`
    nests it, and `:178`'s strip of the Undo's own never runs because it sits
    inside the `is_local()` branch -- so the Undo keeps `:168`'s.

    ONLY THE NESTED ABSENCE IS ASSERTED. The Undo is the top-level object here,
    exactly where `signature.py:100-101` reinjects, so any assertion about its
    own `@context` would hold whether or not `:168` existed.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    route = http_mock.post(PEER_INBOX).respond(200, json={})

    restore_post(None, s.user.id, s.post.id)

    undo = _sent_activity(route)
    assert undo['type'] == 'Undo'
    assert undo['object']['type'] == 'Delete'
    assert '@context' not in undo['object']
```

- [ ] **Step 2: Run** — expect `28 passed`.

- [ ] **Step 3: Commit**

Subject: `test: cover the remote restore's nested Delete envelope`

---

## Task 9: The follower fan-out — path 3

**Files:**
- Modify: `tests/test_shared_tasks_deletes.py`

**Context:** This is the sub-project's most likely source of a silently-zero-recipient test. Path 3 at `:206-216` requires `is_post`, at least one `UserFollower` row with `local_user_id == user.id`, and — after Task 2 — no `reason`. It joins `UserFollower`, NOT `CommunityMember`, so `_follower` from Task 1 is invisible to it.

- [ ] **Step 1: Write the fan-out helper**

```python
def _personal_follower(s, http_mock, inbox=OTHER_INBOX, domain='fan.example'):
    """A remote instance the FOLLOWER fan-out at `:212-216` will return.

    A DIFFERENT SHAPE FROM `_follower`. That one needs a `CommunityMember`
    because `following_instances()` joins it; this one needs a `UserFollower`
    row whose `local_user_id` is the deleting user, because `:212` joins
    `Instance -> User -> UserFollower` and filters on
    `UserFollower.local_user_id == user.id`. A recipient built by one helper
    produces zero deliveries on the other's path, under assertions that still
    pass.

    `is_inward=True` is the honest shape: these are people who follow US, which
    is what `local_user_id == user.id` with `remote_user_id` as the recipient
    means in production.

    Returns `(route, instance, follower_user)`.
    """
    inst = make_instance(domain, software='lemmy')
    inst.inbox = inbox
    fan = make_user(inst, f'fan_{domain.split(".")[0]}')
    make_follow(s.user, fan, is_inward=True)
    db.session.commit()
    return http_mock.post(inbox).respond(200, json={}), inst, fan
```

Add `make_follow` to the `tests.factories` import list.

- [ ] **Step 2: Write the four tests**

```python
def test_a_post_delete_reaches_the_authors_own_followers(db_session, http_mock):
    """`:216`, the follower fan-out, on a REMOTE community so the Announce loop
    does not also run and the two deliveries stay distinguishable.

    `:211` appends each follower's actor URL to the payload's `cc`, so the
    delivered activity carries the follower -- that is the assertion that
    proves the loop at `:210` ran, rather than merely that a request arrived.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    community_route = http_mock.post(PEER_INBOX).respond(200, json={})
    fan_route, _inst, fan = _personal_follower(s, http_mock)

    delete_post(None, s.user.id, s.post.id)

    assert _delivered_inboxes(fan_route) == {OTHER_INBOX}
    assert fan.public_url() in _sent_activity(fan_route)['cc']
    assert len(community_route.calls) == 1


def test_a_reply_delete_does_not_reach_the_authors_followers(db_session, http_mock):
    """`:206`'s `is_post` conjunct. The fan-out is for posts only; a reply
    delete goes to the community and stops."""
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    community_route = http_mock.post(PEER_INBOX).respond(200, json={})
    _fan_route, _inst, _fan = _personal_follower(s, http_mock)
    reply = make_post_reply(s.post, s.user)
    db.session.commit()

    delete_reply(None, s.user.id, reply.id)

    assert len(community_route.calls) == 1
    assert db.session.query(ActivityPubLog).count() == 1


def test_a_moderated_post_delete_skips_the_follower_fanout(db_session, http_mock):
    """`:206`'s `not reason` conjunct, added by this sub-project's PC3.

    The moderator's Delete still reaches the community; it is the AUTHOR'S
    personal followers who are not told. Task 2 proved the same change frees
    the notification cleanup; this proves it did not also free the fan-out.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    community_route = http_mock.post(PEER_INBOX).respond(200, json={})
    _fan_route, _inst, _fan = _personal_follower(s, http_mock)

    delete_post(None, s.user.id, s.post.id, reason='spam')

    assert len(community_route.calls) == 1
    assert db.session.query(ActivityPubLog).count() == 1


def test_a_follower_on_an_already_notified_domain_is_not_sent_to_twice(
        db_session, http_mock):
    """`:214`'s `if instance.domain not in domains_sent_to`. The follower lives
    on the SAME instance as the remote community, which `:203` already added to
    `domains_sent_to`, so the fan-out skips it.

    ONE delivery, not two, and the row count is what says so -- a second send
    to the same registered route would leave `_delivered_inboxes` unchanged."""
    s = _seed(local_community=False, with_keys=True)
    peer = _make_deliverable(s)
    s.community.ap_domain = peer.domain
    db.session.commit()
    route = http_mock.post(PEER_INBOX).respond(200, json={})
    fan = make_user(peer, 'fan_same_domain')
    peer.inbox = PEER_INBOX
    make_follow(s.user, fan, is_inward=True)
    db.session.commit()

    delete_post(None, s.user.id, s.post.id)

    assert db.session.query(ActivityPubLog).count() == 1
```

**The last test's `domains_sent_to` arithmetic must be verified, not assumed.** `:203` appends `community.instance.domain`, which is the peer Instance's domain, while `:214` compares against the FOLLOWER instance's domain. Confirm they are the same row before relying on the skip; if the fixture cannot make them match, say so in the report rather than weakening the assertion.

- [ ] **Step 3: Run** — expect `32 passed`.

- [ ] **Step 4: Commit**

Subject: `test: cover the author's follower fan-out and its three guards`

---

## Task 10: Private messages

**Files:**
- Modify: `tests/test_shared_tasks_deletes.py`

**Context:** `delete_message:291` returns early at `:292-293` for a local recipient and otherwise sends one request to `recipient.ap_inbox_url` at `:318`, signed with the message SENDER's key.

- [ ] **Step 1: Write the three tests**

```python
def test_delete_pm_sends_a_delete_to_the_remote_recipient(db_session, http_mock):
    """`delete_pm:260` -> `delete_message:291` -> `:318`. Signed by the message
    SENDER, which is a third distinct signer in this module."""
    s = _seed(with_keys=True)
    peer = make_instance('pm.example', software='lemmy')
    recipient = make_user(peer, 'recipient')
    recipient.ap_inbox_url = OTHER_INBOX
    db.session.commit()
    message = make_chat_message(s.user, recipient, 'https://test.piefed.local/pm/1')
    db.session.commit()
    route = http_mock.post(OTHER_INBOX).respond(200, json={})

    delete_pm(None, message.id)

    delete = _sent_activity(route)
    assert delete['type'] == 'Delete'
    assert delete['object'] == message.ap_id
    assert _key_id_of(route) == s.user.public_url() + '#main-key'


def test_restore_pm_wraps_the_delete_in_an_undo(db_session, http_mock):
    """`restore_pm:276` -> `is_restore=True`. `:306` strips the Delete's
    `@context` before `:312` nests it; the Undo keeps `:313`'s, and the Undo is
    top-level, so only the NESTED absence discriminates."""
    s = _seed(with_keys=True)
    peer = make_instance('pm.example', software='lemmy')
    recipient = make_user(peer, 'recipient')
    recipient.ap_inbox_url = OTHER_INBOX
    db.session.commit()
    message = make_chat_message(s.user, recipient, 'https://test.piefed.local/pm/1')
    db.session.commit()
    route = http_mock.post(OTHER_INBOX).respond(200, json={})

    restore_pm(None, message.id)

    undo = _sent_activity(route)
    assert undo['type'] == 'Undo'
    assert undo['object']['type'] == 'Delete'
    assert '@context' not in undo['object']


def test_a_pm_to_a_local_recipient_sends_nothing(db_session, http_mock):
    """`:292`'s early return. Both parties local, so there is nobody to tell.

    The row count is the oracle: no route is registered, and an unmatched
    request would be swallowed into a failure row rather than failing here."""
    s = _seed(with_keys=True)
    recipient = make_user(s.instance, 'local_recipient', local=True)
    db.session.commit()
    message = make_chat_message(s.user, recipient, 'https://test.piefed.local/pm/1')
    db.session.commit()

    delete_pm(None, message.id)

    assert db.session.query(ActivityPubLog).count() == 0
```

Add `delete_pm`, `restore_pm` to the deletes import list and `make_chat_message` to the factories import list. **Re-derive every line number in these three docstrings** — they all sit below Task 2's shift.

- [ ] **Step 2: Run** — expect `35 passed`.

- [ ] **Step 3: Commit**

Subject: `test: cover the private-message delete and restore path`

---

## Task 11: Close the module, mutate, set the floor

**Files:**
- Modify: `tests/test_shared_tasks_deletes.py`
- Modify: `coverage_floors.ini`

- [ ] **Step 1: Measure**

```
./run_tests.sh tests/test_shared_tasks_deletes.py -q --cov=app.shared.tasks.deletes --cov-branch --cov-report=json --cov-report=term-missing
podman cp pyfedi_test-runner_1:/app/coverage.json ./coverage-deletes.json
```

Use the DOTTED module form. Report statements, branches, partials, and the exact uncovered lines.

- [ ] **Step 2: Close the residual**

Write a test per uncovered line or arc. For any line you believe unreachable, give a per-line proof rather than a floor below 100 — and if a line genuinely cannot be reached, say so in the report with the argument, and do not lower the floor to accommodate it without ruling.

- [ ] **Step 3: Mutate**

| # | Target | Mutation | Expected kill |
|---|--------|----------|---------------|
| M1 | `:127` | drop `not is_post and ` | `test_a_local_only_community_sends_no_post_delete_without_followers` |
| M2 | `:130` | drop `not followers and ` | `test_delete_post_announces_to_the_communitys_followers` |
| M3 | `:133` | drop `community.private or ` | `test_a_private_community_sends_no_delete` |
| M4 | `:133` | drop ` or not community.instance.online()` | `test_an_offline_community_instance_sends_no_delete` |
| M5 | `:197` | `instance.inbox and` → `True and` | `test_a_following_instance_without_an_inbox_is_skipped` |
| M6 | `:197` | drop `instance.online() and ` | `test_a_dormant_following_instance_is_skipped` |
| M7 | `:197` | drop `not user.has_blocked_instance(instance.id) and ` | `test_an_instance_the_user_blocked_is_skipped` |
| M8 | `:197` | drop ` and not instance_banned(instance.domain)` | `test_a_site_banned_instance_is_skipped` |
| M9 | `:198` | sign with `user` instead of `community` | `test_the_announce_is_signed_by_the_community` |
| M10 | `:206` | drop ` and not reason` | `test_a_moderated_post_delete_skips_the_follower_fanout` |
| M11 | `:214` | replace the condition with `True` | `test_a_follower_on_an_already_notified_domain_is_not_sent_to_twice` |
| M12 | `:219` | `if is_post:` → `if False:` | `test_an_author_delete_clears_notifications_too` |
| M13 | `:223` | drop the `NOTIF_REPORT` disjunct | `test_a_report_notification_survives_the_delete` |
| M14 | `:250` | drop `, session=session` | `test_the_blocked_image_batch_deletes_every_post` |

Re-derive every line number in this table against the post-Task-2 tree before running anything. One mutation at a time; dry-run without `-i` and paste the produced line; apply; run unpiped in the foreground; paste the real failure; restore with `git checkout -- app/` and paste both `git diff --stat -- app/` (empty) and `wc -l app/shared/tasks/deletes.py` (318).

M5 through M8 are the load-bearing four: they are the conjuncts branch coverage cannot see. If any survives, the test named beside it is not doing its job — report the survivor rather than adjusting the test until it dies.

- [ ] **Step 4: Add the floor**

`coverage_floors.ini`, after `app/shared/tasks/blocks.py = 100`:

```
app/shared/tasks/deletes.py = 100
```

- [ ] **Step 5: Prove the floor bites**

An isolated `--cov=app.shared.tasks.deletes` run names ONE module, so every other floored entry reads 0.0 through `check_coverage_floors.py:69`'s `files.get(module)` default and appears to violate. **Build a SYNTHETIC multi-module report**: merge the real `deletes.py` entry into a report carrying passing entries for the other 17, so the only thing your inversion changes is this one. Then set the floor above the measured value, show `check_coverage_floors.py` failing for `app/shared/tasks/deletes.py` ALONE, restore, and show it passing. State exactly how you built the report.

- [ ] **Step 6: Commit**

Subject: `test: close app/shared/tasks/deletes.py and set its floor`

---

## Task 12: Register the findings

**Files:**
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`
- Modify: `tests/README.md`

**Context:** Next free number is **D333** — confirm against the register. Facts end at **148**; append from **149**.

- [ ] **Step 1: Correct D309, and close it**

The cell currently claims `delete_object` omits both `private` and `online()` and that fixing it means "adding two disjuncts at two lines, not one disjunct at one." That is false: `online()` is checked at `:133`, and only `private` was missing.

The correction must carry the MECHANISM, not just the fact:
- The claim entered through sub-project 26's Task 11 dispatch, asserted from a `sed -n '125,132p'` window that stopped one line short of its own disproof.
- It survived review because the reviewer opened `:127` and `:130`, both quoted correctly. **The sentence is true about the lines it cites and false about the guard they belong to.**
- This is the second Critical register error in two consecutive sub-projects with that shape — D329 was the first. **A citation-level check cannot falsify a block-level claim.** Every "checked directly at this commit" in this register that describes a multi-line construct is exposed to it.

Then close D309: twelve of twelve gates fixed, across ten files. Record that this site's shape differs from the family's — `private` sits on `:133` rather than joining the `local_only` expression, because the two `local_only` guards are conditional on `is_post` and on `followers` — while its effect does not.

- [ ] **Step 2: Add the new entries**

- **`delete_posts_with_blocked_images`'s missing `session`** — fixed this round. Record that the failure was DESTRUCTIVE rather than clean: `:247`'s `file.delete_from_disk()` and `:248`'s commit run before `:250` raises, so a batch deleted exactly one post, unlinked its file, skipped the rest silently, and federated nothing. Two live callers, `app/admin/routes.py:2320` and `app/post/routes.py:2153`. Record its kinship with D319 — a feature that had never worked, found by reading a module coverage was about to touch.
- **The notification asymmetry** — fixed this round. The `reason` early return suppressed the follower fan-out deliberately and the notification cleanup incidentally.
- **The unguarded follower fan-out** — registered, NOT fixed. `:213` filters `Instance.gone_forever == False` only, not `dormant`, and `:216` sends to `instance.inbox` with no `None` check. It is D321's shape in a third file, and unlike `blocks.py:159` it carries no inline guard at all.
- **The `cc` list aliased between `delete` and `undo`** — `:146` binds one list, `:155` and `:171` both reference it, `:211` appends to it. `:186` rebinds the NAME for the announce, so `announce['cc']` is separate. No behavioural difference today, since exactly one of the two is ever sent.
- **`:211` mutates after `:198` has already sent** — on the local path the Announce is serialised at `:198`, before `:211` appends follower URLs to the nested object's `cc`, so those URLs never reach the wire on that path. Correct as it stands; recorded so a future reader does not "fix" the ordering.

- [ ] **Step 3: Recount D324**

Run `grep -rn "def _recording_task_session" tests/` and `grep -rn "def _key_id_of" tests/` against the FINAL tree and take the numbers they print. One raw grep hit for the first is a stale docstring mention at `tests/test_shared_tasks_flags.py:312`, not a definition — verify that is still true rather than assuming it. This round adds the tenth and seventh copies.

- [ ] **Step 4: Append facts from 149**

- **149** — a guard split across several statements is invisible to a citation-level check. `deletes.py:127-134` reads as three separate `if` blocks; two carry `local_only` and the third carries `online()`. A register entry quoting `:127` and `:130` correctly still concluded the wrong thing about the function, because the conjunct it looked for was on `:133`. When a claim is about a GUARD, the unit to open is the block, not the line the claim cites.
- **150** — the follower fan-out and the community fan-out need different fixtures, and neither is `following_instances()`. `deletes.py:212` joins `Instance -> User -> UserFollower` filtered on `local_user_id`; `following_instances()` joins `CommunityMember`. A recipient built for one produces zero deliveries on the other, under assertions that still pass. This is fact 147's third distinct fixture shape in the same package.
- **151** — a `session=None` default on an internal helper turns a missing keyword into an `AttributeError` at the first query rather than a `TypeError` at the call. `delete_object:118` carried one, and the single call site that forgot `session=` had been raising on every invocation without any test noticing, because no test reached the function at all.

- [ ] **Step 5: Verify every citation**

Open every line you cite and confirm it carries the claimed statement. Re-derive citations into files your own diff touches AFTER the diff is final. This register has previously shipped an entry that committed the very citation rot it documented.

- [ ] **Step 6: Commit**

Subject: `docs: close D309 and register sub-project 27's findings`

---

## Final verification (controller only)

- [ ] Full suite, foreground and unpiped, on a stack reset with `./run_tests.sh --down` first.
- [ ] Collected count from pytest's own output for `tests/test_shared_tasks_deletes.py`.
- [ ] `git diff --numstat <base>..HEAD -- app/` — expect `3 5` for `deletes.py` (PC1 one line, PC2 one line, PC3 one line changed and two removed) and no other production file.
- [ ] `wc -l app/shared/tasks/deletes.py` — 318.
- [ ] Floors: 18 entries, all met, against a report whose mtime postdates the run.
- [ ] `git status --short` — clean.
