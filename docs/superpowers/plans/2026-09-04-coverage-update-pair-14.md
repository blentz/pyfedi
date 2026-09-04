# Sub-project 14: the update pair's mirrored core — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cover the mirrored core of `update_post_from_activity` and
`update_post_reply_from_activity` — 108 uncovered statements — fix the three
peer-triggerable crashes the pair's asymmetries expose, and close the two
mechanical defects sub-project 13 registered.

**Architecture:** One new test file, `tests/test_ap_update_pair.py`, driving
both functions by direct call with a hand-built `request_json`. Tests first,
across nine tasks; then one task carrying the three in-scope fixes, one
carrying the two carried-forward fixes, and one recording the findings.

**Tech Stack:** pytest, `./run_tests.sh` (podman-compose, tmpfs Postgres),
`tests/factories.py`, a local redis lock double.

**Spec:** `docs/superpowers/specs/2026-09-04-coverage-update-pair-14-design.md`

## Global Constraints

- Defects found in these two functions are fixed test-first, each in its own
  commit, separate from every test-only commit, and each proved by a mutation
  that fails a named test. Anything outside them is registered.
- **A defect is fixed when the correct spelling already exists in the file and
  the change is mechanical; it is registered when the fix would require
  choosing new behaviour for a case the codebase has never handled.**
- The two carried-forward fixes, D232 and D219(c), each get their own commit
  on the same terms.
- Findings are numbered from **D236**, and **both** live "Next free number"
  notes are updated in the same change. The historical notes are frozen
  records — leave them alone.
- The coverage floor for `app/activitypub/util.py` rises to the measured
  blended figure rounded down. It currently reads **61**.
- Locate every code target by content, not by the line numbers in this plan.
- Every test asserts on persisted row state, not merely on the absence of an
  exception.
- No vacuous assertions: never assert a value equal to a column's declared
  default without seeding a contrary baseline first.
- The full suite must pass. **Only the controller runs it**, one session at a
  time, and the controller supplies every coverage figure.
- **Delete nothing** the task did not create. `claude_test` in the repository
  root is not the campaign's.

## A note on test counts

**This plan states a per-task delta, never a running total.** Sub-project 13
stated running totals and corrected them five times, every time because a task
legitimately grew and every later number stayed internally consistent while
being wrong. A delta is robust: a task that grows invalidates nothing
downstream. Each task says how many tests it adds, and the implementer reports
the actual file total it observed.

## File structure

| File | Responsibility |
|---|---|
| `tests/test_ap_update_pair.py` | **Create.** Every test in this slice. Helpers at the top, then the reply function's tests, then the post function's, then the fix inversions. |
| `app/activitypub/util.py` | **Modify, Tasks 10-11 only.** Five fixes, five commits. No other task touches it. |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` | **Modify, Task 12.** Entries from D236. |
| `tests/README.md` | **Modify, Task 12.** Harness facts; currently numbered to 70. |
| `coverage_floors.ini` | **Modify, Task 12.** `app/activitypub/util.py`, 61 → measured. |

---

### Task 1: The harness, and the reply's content/source pair

**Files:**
- Create: `tests/test_ap_update_pair.py`

**Interfaces:**
- Produces: `redis_lock_only_double` fixture; `_seed_reply()`, `_seed_post()`,
  `_update(**fields)` helpers, used by every later task.

**Read first, and report what you find:** the nine existing test files the
spec names. Several of them already drive these functions through the inbox.
**Report in one line each what they pin**, so this slice adds coverage rather
than duplicating it. Three of them (`test_dillo_video_teaser.py`,
`test_federated_event_url_sentinel.py`, `test_event_post_type_survives_update.py`)
sit in the out-of-scope tails — recognise them and move on.

**The harness fact that will cost you a day if you skip it.** Both functions
open with `with redis_client.lock(...)`. The shared `redis_double` fixture
**cannot** back that: this environment's fakeredis has no Lua scripting, so
`Lock.acquire()` succeeds on plain `SET NX PX` but `Lock.release()` issues an
`EVALSHA` and raises `redis.exceptions.ResponseError: unknown command
'evalsha'` on `__exit__`, every time. Read the `redis_double` docstring in
`tests/conftest.py` and "The fakeredis lock limitation" in `tests/README.md`.
Reuse the shape `tests/test_inbox_dispatch_votes.py` already established.

- [ ] **Step 1: Write the file's header, helpers and fixture**

```python
"""The mirrored core of update_post_from_activity and
update_post_reply_from_activity -- the halves of the two functions that do the
same job on a Post and on a PostReply.

Entry is a direct call. Both functions take an already-loaded row and a dict;
neither fetches the object it is applying, so no HTTP mock is needed for
anything in this file.

Both open with `with redis_client.lock(...)`, which the shared `redis_double`
fixture cannot serve: fakeredis without lupa has no Lua scripting, and
redis-py's `Lock.release()` issues an EVALSHA. Every test here takes
`redis_lock_only_double` instead, whose `.lock()` is a nullcontext.

Both functions commit on `db.session`, unlike the refresh tasks of
sub-project 13, which used a separate `get_task_session()`. `expire_on_commit`
is at SQLAlchemy's default True -- the app factory overrides only `autoflush`
-- so a commit inside the function under test expires these objects and the
next attribute access re-loads them. No explicit refresh is needed here, and
tests that would need one elsewhere say so.
"""
import contextlib

import pytest

from app import db
from app.activitypub.util import (update_post_from_activity,
                                  update_post_reply_from_activity)
from app.constants import NOTIF_MENTION
from app.models import Notification, PostReply, User
from app.utils import utcnow
from tests.factories import (make_community, make_instance, make_post,
                             make_post_reply, make_site, make_user)

PEER = 'peer.example'


class _RedisLockOnlyDouble:
    """`app.redis_client` stand-in covering only `.lock(...)` as a context
    manager. Same shape as tests/test_inbox_dispatch_votes.py's, and for the
    same reason -- see this module's docstring.
    """

    def lock(self, *args, **kwargs):
        return contextlib.nullcontext()


@pytest.fixture
def redis_lock_only_double(monkeypatch):
    monkeypatch.setattr('app.redis_client', _RedisLockOnlyDouble())


def _seed_post(software='lemmy'):
    """A local community owned by user 1, a remote author on PEER, and one Post.

    `make_community` hardcodes `instance_id=1` and `user_id=1`, so an instance
    and a user are seeded first to occupy those ids -- the same pattern
    tests/test_inbox_dispatch_votes.py documents.

    `software` reaches `post.instance.software`, which the flair branch reads.
    """
    make_site()
    instance = make_instance(PEER, software=software)
    make_user(instance, 'community_owner')
    community = make_community(host=PEER)
    community.ap_fetched_at = utcnow()
    author = make_user(instance, 'author')
    post = make_post(community, author, ap_id=f'https://{PEER}/objects/1')
    db.session.commit()
    return post


def _seed_reply(software='lemmy'):
    """The same scenario plus one PostReply on the Post.

    `software` reaches `reply.instance.software`, which gates the Mention
    de-duplication block: `make_instance` defaults to 'mastodon', which IS in
    MICROBLOG_APPS, so the default would silently take the de-dup path. This
    helper defaults to 'lemmy' so the simple path is the default and a test
    that wants de-duplication asks for it.
    """
    post = _seed_post(software=software)
    author = db.session.query(User).get(post.user_id)
    reply = make_post_reply(post, author, body='before')
    db.session.commit()
    return reply


def _update(**fields):
    """An Update activity's `object`, with only the keys a test names.

    Both functions read `request_json['object']` and nothing else, so the
    envelope carries no `type`, `actor` or `id`: adding them would suggest
    those are read, and they are not.
    """
    return {'object': fields}
```

- [ ] **Step 2: Write the reply's content/source tests**

```python
def test_a_reply_html_content_is_wrapped_and_converted(app, db_session, redis_lock_only_double):
    """`update_post_reply_from_activity`'s content arm wraps bare content in
    `<p>` before allowlisting, then derives `body` from the html because no
    `source` was supplied.

    The seeded body is 'before', which the document does not contain, so
    "the document was applied" is distinguishable from "the seed survived".
    """
    reply = _seed_reply()

    update_post_reply_from_activity(reply, _update(content='hello there'))

    assert reply.body_html == '<p>hello there</p>'
    assert reply.body == 'hello there'


def test_a_reply_already_wrapped_content_is_not_double_wrapped(app, db_session, redis_lock_only_double):
    """The `startswith('<p>')` half of the wrap guard. Its sibling half is
    `startswith('<blockquote>')`, covered by the next test -- two disjuncts,
    two tests, so dropping either one is attributable.
    """
    reply = _seed_reply()

    update_post_reply_from_activity(reply, _update(content='<p>hello</p>'))

    assert reply.body_html == '<p>hello</p>'


def test_a_reply_blockquote_content_is_not_wrapped(app, db_session, redis_lock_only_double):
    """The `startswith('<blockquote>')` disjunct. Without this test that
    disjunct can be deleted with the suite green, since the `<p>` case is
    caught by its own.
    """
    reply = _seed_reply()

    update_post_reply_from_activity(reply, _update(content='<blockquote>q</blockquote>'))

    assert reply.body_html.startswith('<blockquote>')


def test_a_reply_markdown_source_overwrites_the_html_derived_body(app, db_session, redis_lock_only_double):
    """`source` with `mediaType: text/markdown` wins: `body` becomes the
    markdown and `body_html` is re-derived from it, overwriting the value the
    html arm just computed.

    The html and the markdown are deliberately DIFFERENT strings, so the
    assertion distinguishes which arm won. If both said the same thing the
    test would pass either way.
    """
    reply = _seed_reply()

    update_post_reply_from_activity(reply, _update(
        content='<p>from html</p>',
        source={'mediaType': 'text/markdown', 'content': 'from markdown'},
    ))

    assert reply.body == 'from markdown'
    assert 'from markdown' in reply.body_html
    assert 'from html' not in reply.body_html


def test_a_reply_source_that_is_not_markdown_leaves_the_html_body(app, db_session, redis_lock_only_double):
    """The `mediaType == 'text/markdown'` conjunct's False side. A `source`
    that is a dict and has a mediaType, but the wrong one, must fall to the
    `else` and keep the html-derived body.
    """
    reply = _seed_reply()

    update_post_reply_from_activity(reply, _update(
        content='<p>from html</p>',
        source={'mediaType': 'text/plain', 'content': 'from markdown'},
    ))

    assert reply.body == 'from html'


def test_a_reply_source_that_is_not_a_dict_leaves_the_html_body(app, db_session, redis_lock_only_double):
    """The `isinstance(..., dict)` conjunct. A peer sending `source` as a
    string must not reach the `['mediaType']` subscript behind it.
    """
    reply = _seed_reply()

    update_post_reply_from_activity(reply, _update(
        content='<p>from html</p>',
        source='not a dict',
    ))

    assert reply.body == 'from html'
```

- [ ] **Step 3: Mutation-test the content arm**

Drop each conjunct of the wrap guard and of the `source` guard **separately**,
run, confirm exactly one named test fails, restore, confirm
`git diff app/activitypub/util.py` is empty. The `source` guard has three
conjuncts — `'source' in`, `isinstance(..., dict)`, `'mediaType' in`, and the
`== 'text/markdown'` comparison — and the third of those is the one Task 10
will find missing from the sibling.

**Mutating a whole guard to `if True:` is a site-level proof, not a
conjunct-level one.** Delete one conjunct at a time. Record which kills are by
assertion and which by crash, and whether each is sole or multi.

- [ ] **Step 4: Run and commit**

Run: `./run_tests.sh tests/test_ap_update_pair.py -q`
Expected: **6 tests added.**

```bash
git add tests/test_ap_update_pair.py
git commit -m "test: cover the reply update's content and source arms"
```

---

### Task 2: The reply's simple applications

**Files:**
- Modify: `tests/test_ap_update_pair.py`

**Interfaces:**
- Consumes: Task 1's helpers and fixture.

Four independent field applications, each an `if <key> in` gate:
`language`, `distinguished`, `repliesEnabled`, and the `ap_updated`
`try/except ValueError`. Read all four from source before writing; the
`language` one calls `find_language_or_create`, which creates a row.

- [ ] **Step 1: Write the tests**

```python
def test_a_reply_language_is_applied(app, db_session, redis_lock_only_double):
    """`find_language_or_create` is called with the document's identifier and
    name, and the returned row's id lands on the reply.

    The reply's seeded `language_id` is asserted to differ first, so "applied"
    is distinguishable from "already was".
    """
    reply = _seed_reply()
    seeded = reply.language_id

    update_post_reply_from_activity(reply, _update(
        language={'identifier': 'de', 'name': 'German'},
    ))

    assert reply.language_id is not None
    assert reply.language_id != seeded


def test_a_reply_language_that_is_not_a_dict_is_ignored(app, db_session, redis_lock_only_double):
    """The `isinstance(..., dict)` conjunct. A peer sending `language` as a
    bare string must not reach the `['identifier']` subscript behind it.
    """
    reply = _seed_reply()
    seeded = reply.language_id

    update_post_reply_from_activity(reply, _update(language='de'))

    assert reply.language_id == seeded


def test_a_reply_distinguished_flag_is_applied(app, db_session, redis_lock_only_double):
    """`distinguished` is copied verbatim. Seeded False first -- the column's
    own default -- so the document's True is the only thing that could have
    set it.
    """
    reply = _seed_reply()
    reply.distinguished = False
    db.session.commit()

    update_post_reply_from_activity(reply, _update(distinguished=True))

    assert reply.distinguished is True


def test_a_reply_distinguished_flag_is_applied_when_false(app, db_session, redis_lock_only_double):
    """The contrary seed. Without this sibling, the gate could be replaced by
    `reply.distinguished = True` and the suite would stay green.
    """
    reply = _seed_reply()
    reply.distinguished = True
    db.session.commit()

    update_post_reply_from_activity(reply, _update(distinguished=False))

    assert reply.distinguished is False


def test_a_reply_replies_enabled_flag_is_applied(app, db_session, redis_lock_only_double):
    """`repliesEnabled` -> `replies_enabled`, the one renamed key in this
    function. Seeded to the opposite of what the document sends.
    """
    reply = _seed_reply()
    reply.replies_enabled = True
    db.session.commit()

    update_post_reply_from_activity(reply, _update(repliesEnabled=False))

    assert reply.replies_enabled is False


def test_a_reply_updated_timestamp_is_parsed(app, db_session, redis_lock_only_double):
    """`ap_updated` comes from the document's `updated` when it parses.
    Asserting the parsed VALUE, not merely that it is set -- `utcnow()` is
    what the fallback would give, so "is not None" would pass either way.
    """
    reply = _seed_reply()

    update_post_reply_from_activity(reply, _update(
        content='x', updated='2020-01-02T03:04:05+00:00',
    ))

    assert reply.ap_updated.year == 2020
    assert reply.ap_updated.month == 1


def test_a_reply_unparseable_updated_falls_back_to_now(app, db_session, redis_lock_only_double):
    """The `except ValueError` arm. `datetime.fromisoformat` raises on a
    string it cannot read, and the handler substitutes `utcnow()`.

    Asserting the year is the CURRENT year rather than 2020 is what
    distinguishes the fallback from the parse.
    """
    reply = _seed_reply()

    update_post_reply_from_activity(reply, _update(
        content='x', updated='not a timestamp',
    ))

    assert reply.ap_updated.year == utcnow().year
```

- [ ] **Step 2: Mutation-test each gate**

Four `if <key> in` gates and one `isinstance` conjunct. Delete each
separately; confirm a distinct named test dies for each; restore. For the
`except ValueError` arm, narrow the caught type to something that cannot fire
and confirm the fallback test dies.

- [ ] **Step 3: Run and commit**

Run: `./run_tests.sh tests/test_ap_update_pair.py -q`
Expected: **7 tests added.**

```bash
git add tests/test_ap_update_pair.py
git commit -m "test: cover the reply update's simple field applications"
```

---

### Task 3: The reply's attachment loop

**Files:**
- Modify: `tests/test_ap_update_pair.py`

**Interfaces:**
- Consumes: Task 1's helpers and fixture.

Seventeen uncovered statements, the largest single block in the reply
function. **Read it from source first** — it accepts `attachment` as either a
dict or a list, normalises to a list, and for each entry appends a markdown
image to `reply.body`, preferring `url` over `href` (note the order: `href` is
read first and `url` overwrites it), with `name` as alt text. It regenerates
`body_html` only if the list was non-empty.

- [ ] **Step 1: Write the tests**

```python
def test_a_reply_single_attachment_dict_is_appended(app, db_session, redis_lock_only_double):
    """The `isinstance(..., dict)` arm: a lone attachment object is wrapped
    into a one-element list rather than iterated as a dict's keys.
    """
    reply = _seed_reply()

    update_post_reply_from_activity(reply, _update(
        content='body text',
        attachment={'url': 'https://cdn.example/a.png', 'name': 'alt words'},
    ))

    assert '![alt words](https://cdn.example/a.png)' in reply.body


def test_a_reply_attachment_list_is_appended_in_order(app, db_session, redis_lock_only_double):
    """The `isinstance(..., list)` arm, with two entries so the loop runs more
    than once and order is observable.
    """
    reply = _seed_reply()

    update_post_reply_from_activity(reply, _update(
        content='body text',
        attachment=[{'url': 'https://cdn.example/1.png'},
                    {'url': 'https://cdn.example/2.png'}],
    ))

    assert reply.body.index('1.png') < reply.body.index('2.png')


def test_a_reply_attachment_url_wins_over_href(app, db_session, redis_lock_only_double):
    """Both keys are read and `url` is read second, so it overwrites `href`.
    The two values differ, which is what makes the precedence observable --
    equal values would pass whichever won.
    """
    reply = _seed_reply()

    update_post_reply_from_activity(reply, _update(
        content='body text',
        attachment=[{'href': 'https://cdn.example/href.png',
                     'url': 'https://cdn.example/url.png'}],
    ))

    assert 'url.png' in reply.body
    assert 'href.png' not in reply.body


def test_a_reply_attachment_href_is_used_when_there_is_no_url(app, db_session, redis_lock_only_double):
    """The `href` half on its own. Without this test the `'href' in
    attachment` branch can be deleted with the suite green, because the
    precedence test above supplies both keys.
    """
    reply = _seed_reply()

    update_post_reply_from_activity(reply, _update(
        content='body text',
        attachment=[{'href': 'https://cdn.example/href.png'}],
    ))

    assert 'href.png' in reply.body


def test_a_reply_attachment_with_no_url_appends_nothing(app, db_session, redis_lock_only_double):
    """The `if url:` gate. An attachment carrying only a `name` contributes no
    markdown at all.

    The body is asserted to equal exactly what the content arm produced, so an
    empty `![alt]()` would fail rather than pass unnoticed.
    """
    reply = _seed_reply()

    update_post_reply_from_activity(reply, _update(
        content='body text',
        attachment=[{'name': 'alt only'}],
    ))

    assert reply.body == 'body text'


def test_an_empty_attachment_list_does_not_regenerate_the_html(app, db_session, redis_lock_only_double):
    """`if attachment_list:` guards the `body_html` regeneration. With an
    empty list the html must remain what the content arm allowlisted.

    Seeded through the html arm rather than the markdown arm so the two
    spellings differ and the assertion can tell them apart.
    """
    reply = _seed_reply()

    update_post_reply_from_activity(reply, _update(
        content='<p>body text</p>', attachment=[],
    ))

    assert reply.body_html == '<p>body text</p>'
```

- [ ] **Step 2: Mutation-test the loop's guards**

The `isinstance(dict)` / `isinstance(list)` pair, the `'href' in` and `'url'
in` gates, the `'name' in` gate, the `if url:` gate and the
`if attachment_list:` gate. One at a time, each killed by a distinct named
test.

**Watch for a shielded gate:** `'name' in attachment` has no test of its own
above — decide whether the alt-text assertion in the first test kills it, and
if not, add the sibling that does.

- [ ] **Step 3: Run and commit**

Run: `./run_tests.sh tests/test_ap_update_pair.py -q`
Expected: **6 tests added**, plus any sibling Step 2 showed was needed.

```bash
git add tests/test_ap_update_pair.py
git commit -m "test: cover the reply update's attachment loop"
```

---

### Task 4: The reply's Mention walk, the simple path

**Files:**
- Modify: `tests/test_ap_update_pair.py`

**Interfaces:**
- Consumes: Task 1's helpers and fixture.
- Produces: `_seed_local_recipient(name)`, used by Task 5.

**Read the whole Mention block from source before writing.** This task covers
reaching it and the notification it creates; Task 5 covers the four
suppression rules inside it. The block is gated on the instance's software:
`reply.instance.software == 'mbin' or in MICROBLOG_APPS`. `_seed_reply`
defaults to `'lemmy'`, which is neither, so this task's tests take the simple
path.

A Mention's `href` must start with `https://` + `SERVER_NAME`, which
`tests/conftest.py` sets to `test.piefed.local`. The recipient must be a LOCAL
user — `ap_id=None` — and its `ap_profile_id` must match the href lowercased.

- [ ] **Step 1: Write the helper and the tests**

```python
def _seed_local_recipient(name='localuser'):
    """A local user the Mention block can resolve.

    `User.query.filter_by(ap_profile_id=..., ap_id=None)` is the lookup, so
    both columns matter. `make_user(None, name, local=True)` already leaves
    `ap_id` None -- and leaves `ap_profile_id` None too, which is why this
    helper sets it: the lookup needs it to equal the lowercased href the
    document sends. Passing `instance=None` is supported; `make_user` falls
    back to `instance_id=1`.
    """
    recipient = make_user(None, name, local=True)
    recipient.ap_profile_id = f'https://test.piefed.local/u/{name}'
    db.session.commit()
    return recipient


def _mention(name='localuser'):
    return {'type': 'Mention', 'href': f'https://test.piefed.local/u/{name}'}


def test_a_reply_mention_of_a_local_user_notifies_them(app, db_session, redis_lock_only_double):
    """The simple path: two tags so the `len(...) > 1` gate is satisfied, a
    Mention naming a local user, a non-microblog instance so the
    de-duplication block is skipped.

    Asserts the Notification row exists with the right recipient, type and
    url -- not merely that some notification was created.
    """
    reply = _seed_reply()
    recipient = _seed_local_recipient()

    update_post_reply_from_activity(reply, _update(
        content='hello',
        tag=[{'type': 'Hashtag', 'name': '#x'}, _mention()],
    ))

    notification = db.session.query(Notification).filter_by(
        user_id=recipient.id, notif_type=NOTIF_MENTION).one()
    assert notification.url.endswith(f'/comment/{reply.id}')
    assert notification.subtype == 'comment_mention'


def test_a_reply_mention_increments_the_recipients_unread_count(app, db_session, redis_lock_only_double):
    """`recipient.unread_notifications += 1` sits beside the `db.session.add`
    and is a separate statement. Seeded to 3 rather than left at the column
    default, so "incremented" is distinguishable from "set to 1".
    """
    reply = _seed_reply()
    recipient = _seed_local_recipient()
    recipient.unread_notifications = 3
    db.session.commit()

    update_post_reply_from_activity(reply, _update(
        content='hello',
        tag=[{'type': 'Hashtag', 'name': '#x'}, _mention()],
    ))

    assert recipient.unread_notifications == 4


def test_a_lone_reply_mention_is_ignored(app, db_session, redis_lock_only_double):
    """THE ASYMMETRY. The reply function's tag gate requires
    `len(request_json['object']['tag']) > 1`, so a document carrying exactly
    one tag -- a single Mention -- is skipped entirely. The post function's
    gate has no length condition.

    This test PINS the current behaviour rather than asserting it is correct.
    The spec registers it rather than fixing it, because changing the gate
    changes which notifications this instance generates.
    """
    reply = _seed_reply()
    recipient = _seed_local_recipient()

    update_post_reply_from_activity(reply, _update(content='hello', tag=[_mention()]))

    assert db.session.query(Notification).filter_by(user_id=recipient.id).count() == 0


def test_a_reply_mention_of_a_remote_user_notifies_nobody(app, db_session, redis_lock_only_double):
    """The `startswith('https://' + SERVER_NAME)` guard. A Mention naming a
    user on another host is not ours to notify.
    """
    reply = _seed_reply()
    _seed_local_recipient()

    update_post_reply_from_activity(reply, _update(
        content='hello',
        tag=[{'type': 'Hashtag', 'name': '#x'},
             {'type': 'Mention', 'href': f'https://{PEER}/u/someone'}],
    ))

    assert db.session.query(Notification).count() == 0


def test_a_reply_mention_with_no_href_notifies_nobody(app, db_session, redis_lock_only_double):
    """`profile_id = json_tag['href'] if 'href' in json_tag else None`, then
    `if profile_id and ...`. A Mention with no href yields None and stops.
    """
    reply = _seed_reply()
    _seed_local_recipient()

    update_post_reply_from_activity(reply, _update(
        content='hello',
        tag=[{'type': 'Hashtag', 'name': '#x'}, {'type': 'Mention'}],
    ))

    assert db.session.query(Notification).count() == 0


def test_a_reply_mention_of_a_blocked_sender_is_suppressed(app, db_session, redis_lock_only_double):
    """`blocked_users(recipient.id)` -- a recipient who has blocked the
    reply's author gets no notification.

    Use `make_user_block` from tests/factories.py; read its signature before
    calling it.
    """
    reply = _seed_reply()
    recipient = _seed_local_recipient()
    # block the reply's author, so reply.user_id lands in blocked_senders
    ...  # see Step 2

    update_post_reply_from_activity(reply, _update(
        content='hello',
        tag=[{'type': 'Hashtag', 'name': '#x'}, _mention()],
    ))

    assert db.session.query(Notification).filter_by(user_id=recipient.id).count() == 0


def test_a_second_reply_mention_does_not_duplicate_the_notification(app, db_session, redis_lock_only_double):
    """`existing_notification` -- the same comment mentioning the same user
    twice produces one row, not two.

    Two Updates rather than two tags in one document, because the block breaks
    out per tag and the second Update is the realistic shape.
    """
    reply = _seed_reply()
    recipient = _seed_local_recipient()
    document = _update(content='hello',
                       tag=[{'type': 'Hashtag', 'name': '#x'}, _mention()])

    update_post_reply_from_activity(reply, document)
    update_post_reply_from_activity(reply, document)

    assert db.session.query(Notification).filter_by(user_id=recipient.id).count() == 1
```

- [ ] **Step 2: Fill in the block test's setup**

The blocked-sender test above has a `...` placeholder for one reason: the
exact call depends on `make_user_block`'s signature and on which id
`blocked_users()` returns. **Read both**, then write the two or three lines
that make the reply's author blocked by the recipient, and delete the
placeholder. If `blocked_users` turns out to key the other way round, say so
in your report — that is itself worth knowing.

- [ ] **Step 3: Mutation-test the walk's guards**

The `len(...) > 1` gate, the `isinstance(list)` conjunct, the `'type' in
json_tag` conjunct, the `== 'Mention'` comparison, the `'href' in` gate, the
`profile_id and isinstance(..., str)` pair, the `startswith` guard, the
`blocked_senders` check and the `existing_notification` check. One at a time,
each killed by a distinct named test.

- [ ] **Step 4: Run and commit**

Run: `./run_tests.sh tests/test_ap_update_pair.py -q`
Expected: **7 tests added.**

```bash
git add tests/test_ap_update_pair.py
git commit -m "test: cover the reply update's mention walk"
```

---

### Task 5: The reply's Mention de-duplication

**Files:**
- Modify: `tests/test_ap_update_pair.py`

**Interfaces:**
- Consumes: Task 1's helpers, Task 4's `_seed_local_recipient` and `_mention`.

The densest untested logic in either function, and it exists only in the reply
path. It runs only when `reply.instance.software == 'mbin'` or is in
`MICROBLOG_APPS` (`app/constants.py` — `mastodon`, `misskey`, `akkoma`,
`iceshrimp`, `pleroma`, `fedibird`). `_seed_reply(software='mastodon')` opts
in.

**Read all four suppression rules from source and write one test per rule,
plus one that reaches the notification with the block active** — without that
last one, every rule's test could be passing for the wrong reason.

The four rules, in source order: the recipient is the post's author; a
`post_mention` notification already exists for this post; a `comment_mention`
notification already exists anywhere in the comment chain (`reply.path`); the
recipient authored a comment in that chain.

- [ ] **Step 1: Write the opt-in test first, and watch it notify**

```python
def test_a_microblog_reply_mention_still_notifies_when_no_rule_applies(app, db_session, redis_lock_only_double):
    """The de-duplication block runs -- the instance is 'mastodon', which is in
    MICROBLOG_APPS -- and none of its four rules matches, so the notification
    is still created.

    This test exists so the four suppression tests below cannot pass for the
    wrong reason: it proves the path reaches the notification at all under the
    same fixture they use.
    """
    reply = _seed_reply(software='mastodon')
    recipient = _seed_local_recipient()

    update_post_reply_from_activity(reply, _update(
        content='hello',
        tag=[{'type': 'Hashtag', 'name': '#x'}, _mention()],
    ))

    assert db.session.query(Notification).filter_by(
        user_id=recipient.id, notif_type=NOTIF_MENTION).count() == 1
```

- [ ] **Step 2: Write one test per suppression rule**

Four tests, each seeding exactly the state its rule keys on and asserting
**zero** notifications for the recipient, paired against Step 1's control.
Derive each from source rather than from this description:

1. **The post's author.** Make the mentioned local user the post's author, so
   `recipient.id == reply.post.user_id`.
2. **A `post_mention` already notified.** Seed a `Notification` with
   `notif_type=NOTIF_MENTION`, `subtype='post_mention'`, and
   `targets={'post_id': <the post's id>}`. Note the query casts
   `targets->>'post_id'` to `Integer`, so the stored value's type matters —
   read the query and match it.
3. **A `comment_mention` already notified in the chain.** This one needs a
   parent reply so `reply.path` has more than the reply's own id in it. Read
   how `PostReply.path` is populated before seeding; if `make_post_reply` does
   not populate it, set it explicitly and say so in your report.
4. **The recipient authored a comment in the chain.** A raw
   `SELECT user_id FROM "post_reply" WHERE id IN :ids` over the path.

**Report the shape of `reply.path` you found**, because rules 3 and 4 both
depend on it and this plan is not asserting what it contains.

- [ ] **Step 3: Mutation-test each rule separately**

Delete each `continue` (or the condition guarding it) one at a time and
confirm exactly that rule's test fails while Step 1's control still passes.
Four rules, four kills.

**If any rule proves unkillable, do not weaken the assertion.** Say which,
and which of the campaign's catalogued causes applies: a fixture that never
creates the excluded row; a clause subsumed by an earlier one; production
exception handling swallowing the effect; or a genuinely crash-only kill.

- [ ] **Step 4: Run and commit**

Run: `./run_tests.sh tests/test_ap_update_pair.py -q`
Expected: **5 tests added** — the control plus one per rule.

```bash
git add tests/test_ap_update_pair.py
git commit -m "test: cover the reply update's mention de-duplication"
```

---

### Task 6: The post's content, source and mediaType chain

**Files:**
- Modify: `tests/test_ap_update_pair.py`

**Interfaces:**
- Consumes: Task 1's helpers and fixture.

The post function's content arm has **four** branches where the reply's has
two: a `source` markdown arm, an object-level `mediaType == 'text/html'` arm,
an object-level `mediaType == 'text/markdown'` arm, and an `else` that wraps
and allowlists. The reply function has no object-level `mediaType` handling at
all — that asymmetry is registered in Task 12.

**Do not write a test for a `source` with no `mediaType` in this task.** That
is the crash Task 10 fixes, and its pin belongs there.

- [ ] **Step 1: Write the tests**

```python
def test_a_post_markdown_source_sets_body_and_html(app, db_session, redis_lock_only_double):
    """The `source` arm. Unlike the reply function, this one does NOT compute
    an html body first -- `source` is checked before anything else, so the
    document's `content` is never allowlisted when a markdown source is
    present.
    """
    post = _seed_post()

    update_post_from_activity(post, _update(
        content='<p>from html</p>',
        source={'mediaType': 'text/markdown', 'content': 'from markdown'},
    ))

    assert post.body == 'from markdown'
    assert 'from markdown' in post.body_html


def test_a_post_html_media_type_allowlists_the_content(app, db_session, redis_lock_only_double):
    """The object-level `mediaType: text/html` arm, which the reply function
    has no counterpart for. `body` is derived from the allowlisted html.
    """
    post = _seed_post()

    update_post_from_activity(post, _update(
        content='<p>plain html</p>', mediaType='text/html',
    ))

    assert post.body_html == '<p>plain html</p>'
    assert post.body == 'plain html'


def test_a_post_markdown_media_type_renders_the_content(app, db_session, redis_lock_only_double):
    """The object-level `mediaType: text/markdown` arm. `content` IS the
    markdown here, so `body` keeps it verbatim and `body_html` is rendered.
    """
    post = _seed_post()

    update_post_from_activity(post, _update(
        content='**bold**', mediaType='text/markdown',
    ))

    assert post.body == '**bold**'
    assert '<strong>' in post.body_html or '<b>' in post.body_html


def test_a_post_bare_content_is_wrapped_and_allowlisted(app, db_session, redis_lock_only_double):
    """The `else` arm's wrap. No `source`, no `mediaType`, and content that
    starts with neither `<p>` nor `<blockquote>`.
    """
    post = _seed_post()

    update_post_from_activity(post, _update(content='bare words'))

    assert post.body_html == '<p>bare words</p>'


def test_a_post_blockquote_content_is_not_wrapped(app, db_session, redis_lock_only_double):
    """The `<blockquote>` disjunct of the wrap guard, which needs its own test
    or it can be deleted with the `<p>` case still passing.
    """
    post = _seed_post()

    update_post_from_activity(post, _update(content='<blockquote>q</blockquote>'))

    assert post.body_html.startswith('<blockquote>')


def test_a_post_with_null_content_keeps_its_body(app, db_session, redis_lock_only_double):
    """THE ASYMMETRY, on the side that gets it right. This function guards
    `request_json['object']['content'] is not None`; the reply function does
    not, and Task 10 fixes that.

    The seeded body is asserted unchanged, which is what distinguishes "the
    guard skipped the arm" from "the arm ran and wrote None".
    """
    post = _seed_post()
    post.body = 'seeded body'
    db.session.commit()

    update_post_from_activity(post, _update(content=None, name='a title'))

    assert post.body == 'seeded body'
```

- [ ] **Step 2: Mutation-test the chain**

Each arm of the elif chain, the `is not None` conjunct, both wrap disjuncts,
and the `source` guard's conjuncts other than the missing `'mediaType' in`.
One at a time.

**Note the elif chain's ordering is itself testable:** deleting the `source`
arm should make the markdown test fall through to a different arm, not merely
crash. Record whether each kill is by assertion or by crash.

- [ ] **Step 3: Run and commit**

Run: `./run_tests.sh tests/test_ap_update_pair.py -q`
Expected: **6 tests added.**

```bash
git add tests/test_ap_update_pair.py
git commit -m "test: cover the post update's content and media type arms"
```

---

### Task 7: The post's title, nsfw and nsfl

**Files:**
- Modify: `tests/test_ap_update_pair.py`

**Interfaces:**
- Consumes: Task 1's helpers and fixture.

The title block has two arms — a document `name`, or an autogenerated
microblog title from `microblog_content_to_title(post.body_html)` — and then a
`if old_title != new_title:` gate behind which sit the NSFL and NSFW keyword
scans. After that, two independent `sensitive` / `nsfl` key applications that
can override what the keyword scan just set.

**The `else` arm's `url_is_parseable` block carries a campaign-authored
comment explaining why it stores `None` rather than `''`.** Read it. Do not
restate it, contradict it, or delete it. `tests/test_unparseable_url_ingress.py`
already pins that behaviour — check what it covers before adding anything
there.

- [ ] **Step 1: Write the tests**

```python
def test_a_post_name_sets_the_title_and_clears_microblog(app, db_session, redis_lock_only_double):
    """The `'name' in` arm. `microblog` is seeded True so the `= False` is
    observable rather than matching the column's own default.
    """
    post = _seed_post()
    post.microblog = True
    db.session.commit()

    update_post_from_activity(post, _update(name='A New Title', content='x'))

    assert post.title == 'A New Title'
    assert post.microblog is False


def test_a_post_with_no_name_autogenerates_a_microblog_title(app, db_session, redis_lock_only_double):
    """The `else` arm. With no `name`, the title comes from
    `microblog_content_to_title(post.body_html)` and `microblog` is set True.

    The content is short enough that the `len(...) < 20` branch prefixes
    '[Microblog] '; the long-title branch is the next test.
    """
    post = _seed_post()
    post.microblog = False
    db.session.commit()

    update_post_from_activity(post, _update(content='short'))

    assert post.title.startswith('[Microblog] ')
    assert post.microblog is True


def test_a_long_autogenerated_title_is_not_prefixed(app, db_session, redis_lock_only_double):
    """The `len(autogenerated_title) < 20` gate's False side. Twenty
    characters or more and the prefix is dropped.

    Read `microblog_content_to_title` before choosing the content string --
    it truncates, so the input must be long enough that its OUTPUT clears 20.
    """
    post = _seed_post()

    update_post_from_activity(post, _update(
        content='a considerably longer sentence than the other one',
    ))

    assert not post.title.startswith('[Microblog] ')


def test_an_nsfl_keyword_in_a_new_title_sets_the_flag(app, db_session, redis_lock_only_double):
    """The keyword scan behind `if old_title != new_title:`. Seeded False --
    the column default -- but the title is also seeded DIFFERENT from the
    document's, which is the part that makes the gate open.
    """
    post = _seed_post()
    post.nsfl = False
    db.session.commit()

    update_post_from_activity(post, _update(name='[NSFL] a title', content='x'))

    assert post.nsfl is True


def test_an_nsfw_keyword_in_a_new_title_sets_the_flag(app, db_session, redis_lock_only_double):
    """The NSFW half of the same scan. Separate from NSFL so each keyword
    branch has its own kill.
    """
    post = _seed_post()
    post.nsfw = False
    db.session.commit()

    update_post_from_activity(post, _update(name='(NSFW) a title', content='x'))

    assert post.nsfw is True


def test_an_unchanged_title_does_not_rescan_for_keywords(app, db_session, redis_lock_only_double):
    """The `old_title != new_title` gate itself. A document repeating the
    title the post already has must not reach the scan -- proved by seeding a
    title that CONTAINS a keyword and a flag that is False, and asserting the
    flag stayed False.

    This is the only test that distinguishes the gate from an unconditional
    scan.
    """
    post = _seed_post()
    post.title = '[NSFL] already'
    post.nsfl = False
    db.session.commit()

    update_post_from_activity(post, _update(name='[NSFL] already', content='x'))

    assert post.nsfl is False


def test_a_sensitive_flag_overrides_the_title_scan(app, db_session, redis_lock_only_double):
    """`sensitive` is applied AFTER the keyword scan, so a document with an
    NSFW title and `sensitive: false` ends up False.

    That ordering is the whole point of the test -- asserting False against a
    title that would have set True is what proves which ran last.
    """
    post = _seed_post()
    post.nsfw = False
    db.session.commit()

    update_post_from_activity(post, _update(
        name='[NSFW] a title', content='x', sensitive=False,
    ))

    assert post.nsfw is False


def test_an_nsfl_key_is_applied(app, db_session, redis_lock_only_double):
    """The `'nsfl' in` application, seeded to the opposite value."""
    post = _seed_post()
    post.nsfl = True
    db.session.commit()

    update_post_from_activity(post, _update(name='a title', content='x', nsfl=False))

    assert post.nsfl is False
```

- [ ] **Step 2: Check the third NSFL keyword**

The scan tests three strings, not two: `[NSFL]`, `(NSFL)` and `[COMBAT]`.
The tests above cover one spelling each for NSFL and NSFW. **Decide whether
each remaining disjunct is separately killable**, and add a test for any that
is not. Report which you added and why.

- [ ] **Step 3: Mutation-test**

The `'name' in` gate, the `len(...) < 20` gate, the `link != ''` gate, the
`old_title != new_title` gate, each keyword disjunct, and the `sensitive` and
`nsfl` gates. One at a time.

- [ ] **Step 4: Run and commit**

Run: `./run_tests.sh tests/test_ap_update_pair.py -q`
Expected: **8 tests added**, plus any Step 2 showed were needed.

```bash
git add tests/test_ap_update_pair.py
git commit -m "test: cover the post update's title and sensitivity flags"
```

---

### Task 8: The post's language, including the contentMap fallback

**Files:**
- Modify: `tests/test_ap_update_pair.py`

**Interfaces:**
- Consumes: Task 1's helpers and fixture.

Three uncovered statements and two asymmetries against the reply function: the
post path has a `contentMap` fallback the reply path lacks, and it assigns
only when the resolved language differs from the one already stored, where the
reply path assigns unconditionally.

- [ ] **Step 1: Write the tests**

```python
def test_a_post_language_dict_is_applied(app, db_session, redis_lock_only_double):
    """The `language` arm, the one both functions share."""
    post = _seed_post()
    seeded = post.language_id

    update_post_from_activity(post, _update(
        name='t', content='x', language={'identifier': 'de', 'name': 'German'},
    ))

    assert post.language_id is not None
    assert post.language_id != seeded


def test_a_post_content_map_supplies_the_language(app, db_session, redis_lock_only_double):
    """THE ASYMMETRY. `contentMap`'s first key is read as a language code
    through `find_language`, a fallback the reply function does not have.

    `find_language` LOOKS UP rather than creating, so the code must be one the
    database already carries -- read the seeding in tests/conftest.py or the
    languages migration before choosing it, and report which you used.
    """
    post = _seed_post()
    seeded = post.language_id

    update_post_from_activity(post, _update(
        name='t', content='x', contentMap={'de': '<p>hallo</p>'},
    ))

    assert post.language_id != seeded


def test_a_post_language_dict_wins_over_content_map(app, db_session, redis_lock_only_double):
    """`elif` -- the two are alternatives, not both. A document carrying both
    must resolve through `language` and never consult `contentMap`.

    The two name DIFFERENT languages, which is what makes the winner
    observable.
    """
    post = _seed_post()

    update_post_from_activity(post, _update(
        name='t', content='x',
        language={'identifier': 'de', 'name': 'German'},
        contentMap={'fr': '<p>bonjour</p>'},
    ))

    assert post.language_id is not None
    # assert it is the German row, not the French one -- read find_language's
    # return and assert on the code, not merely on inequality


def test_a_post_language_that_is_not_a_dict_is_ignored(app, db_session, redis_lock_only_double):
    """The `isinstance(..., dict)` conjunct on the `language` arm. A bare
    string must not reach `['identifier']`.

    With `contentMap` absent, the elif is not taken either, so the seeded
    language must survive untouched.
    """
    post = _seed_post()
    seeded = post.language_id

    update_post_from_activity(post, _update(name='t', content='x', language='de'))

    assert post.language_id == seeded


def test_an_unchanged_post_language_is_not_reassigned(app, db_session, redis_lock_only_double):
    """THE SECOND ASYMMETRY. `if new_language and (new_language.id !=
    old_language_id)` -- the post path assigns only on a change; the reply
    path assigns unconditionally.

    Proving "did not reassign" needs an observable other than the value, since
    reassigning the same id is invisible. Seed the post's language to the one
    the document names, then assert the id is unchanged AND that no second
    language row was created -- `find_language_or_create` is what would
    create one.
    """
    post = _seed_post()

    update_post_from_activity(post, _update(
        name='t', content='x', language={'identifier': 'de', 'name': 'German'},
    ))
    first = post.language_id

    update_post_from_activity(post, _update(
        name='t2', content='x', language={'identifier': 'de', 'name': 'German'},
    ))

    assert post.language_id == first
```

- [ ] **Step 2: Finish the two tests this plan left open**

`test_a_post_language_dict_wins_over_content_map` carries a comment rather
than a final assertion, and `test_an_unchanged_post_language_is_not_reassigned`
asserts a weaker thing than its docstring claims. **Read `find_language` and
`find_language_or_create`**, then make both assertions say exactly what the
docstrings promise. If the "did not reassign" case turns out to have no
observable at all, say so in your report and mark the conjunct's kill as
crash-only or unkillable with the reason — do not leave a test whose docstring
overstates it.

- [ ] **Step 3: Mutation-test**

The `language` gate, its `isinstance` conjunct, the `contentMap` elif and its
`isinstance` conjunct, and both halves of `new_language and (new_language.id
!= old_language_id)`. One at a time.

- [ ] **Step 4: Run and commit**

Run: `./run_tests.sh tests/test_ap_update_pair.py -q`
Expected: **5 tests added.**

```bash
git add tests/test_ap_update_pair.py
git commit -m "test: cover the post update's language resolution"
```

---

### Task 9: The post's tag walk and updated timestamp

**Files:**
- Modify: `tests/test_ap_update_pair.py`

**Interfaces:**
- Consumes: Task 1's helpers, Task 4's `_seed_local_recipient` and `_mention`.

The largest block in the post function's scoped region. Three tag types are
handled in one loop — `Hashtag`, `lemmy:CommunityTag` (collected as flair),
and `Mention` — followed by a flair-application gate that fires when there are
flair tags **or** the posting instance is `piefed` or `pylova`. Then
`comments_enabled` and the `ap_updated` `try/except ValueError`.

`_seed_post(software=...)` sets `post.instance.software`, which that gate
reads. The default is `'lemmy'`, which is neither.

**Do not write a test for a tag object with no `type` key in this task.** That
is the crash Task 10 fixes, and its pin belongs there.

- [ ] **Step 1: Write the tests**

```python
def test_a_post_hashtag_is_attached(app, db_session, redis_lock_only_double):
    """The `Hashtag` arm through `find_hashtag_or_create`. The tag list is
    cleared first, so asserting the new tag is present also proves the clear
    did not remove it afterwards.
    """
    post = _seed_post()

    update_post_from_activity(post, _update(
        name='t', content='x', tag=[{'type': 'Hashtag', 'name': '#topic'}],
    ))

    assert any('topic' in t.name.lower() for t in post.tags)


def test_a_hashtag_matching_the_community_name_is_ignored(app, db_session, redis_lock_only_double):
    """Lemmy adds the community slug as a hashtag on every post, and the
    comparison against `post.community.name` drops it.

    The community `_seed_post` builds is named by `make_community`'s default
    -- read it and use that name, lowercased, as the hashtag.
    """
    post = _seed_post()

    update_post_from_activity(post, _update(
        name='t', content='x',
        tag=[{'type': 'Hashtag', 'name': '#' + post.community.name}],
    ))

    assert len(list(post.tags)) == 0


def test_a_post_mention_of_a_local_user_notifies_them(app, db_session, redis_lock_only_double):
    """The post path's Mention arm. Unlike the reply path there is NO
    de-duplication block and no `len(tag) > 1` gate, so a lone Mention
    notifies -- which is the pair's asymmetry, pinned here from the side that
    allows it.
    """
    post = _seed_post()
    recipient = _seed_local_recipient()

    update_post_from_activity(post, _update(name='t', content='x', tag=[_mention()]))

    notification = db.session.query(Notification).filter_by(
        user_id=recipient.id, notif_type=NOTIF_MENTION).one()
    assert notification.subtype == 'post_mention'
    assert notification.url.endswith(f'/post/{post.id}')


def test_a_post_mention_increments_the_recipients_unread_count(app, db_session, redis_lock_only_double):
    """Seeded to 3, asserted 4 -- "incremented", not "set to 1"."""
    post = _seed_post()
    recipient = _seed_local_recipient()
    recipient.unread_notifications = 3
    db.session.commit()

    update_post_from_activity(post, _update(name='t', content='x', tag=[_mention()]))

    assert recipient.unread_notifications == 4


def test_a_second_post_mention_does_not_duplicate_the_notification(app, db_session, redis_lock_only_double):
    """`existing_notification` on the post side."""
    post = _seed_post()
    recipient = _seed_local_recipient()
    document = _update(name='t', content='x', tag=[_mention()])

    update_post_from_activity(post, document)
    update_post_from_activity(post, document)

    assert db.session.query(Notification).filter_by(user_id=recipient.id).count() == 1


def test_a_community_tag_becomes_flair(app, db_session, redis_lock_only_double):
    """`lemmy:CommunityTag` entries are collected and applied through
    `find_flair_or_create` once the loop ends.

    Read `find_flair_or_create`'s signature -- it takes the whole tag object
    and the community id -- and shape the tag to match what it reads.
    """
    post = _seed_post()

    update_post_from_activity(post, _update(
        name='t', content='x',
        tag=[{'type': 'lemmy:CommunityTag', 'name': 'News',
              'id': f'https://{PEER}/flair/news'}],
    ))

    assert len(list(post.flair)) == 1


def test_a_piefed_instance_clears_flair_with_no_flair_tags(app, db_session, redis_lock_only_double):
    """The gate's second disjunct: with no `lemmy:CommunityTag` entries at
    all, flair is still cleared when the posting instance is piefed or pylova.

    Seeded WITH a flair first -- `make_post_flair` in tests/factories.py --
    so "cleared" is distinguishable from "there was never any".
    """
    post = _seed_post(software='piefed')
    # seed an existing flair here; read make_post_flair's signature first

    update_post_from_activity(post, _update(name='t', content='x', tag=[]))

    assert len(list(post.flair)) == 0


def test_a_lemmy_instance_keeps_flair_when_no_flair_tags_arrive(app, db_session, redis_lock_only_double):
    """The gate's False side, and the reason it exists: a Lemmy Update
    carrying no flair tags must NOT clear flair this instance already holds,
    because Lemmy does not send flair at all.

    Same seeding as the test above, opposite software, opposite assertion --
    the pair is what makes the disjunct killable.
    """
    post = _seed_post(software='lemmy')
    # seed the same existing flair here

    update_post_from_activity(post, _update(name='t', content='x', tag=[]))

    assert len(list(post.flair)) == 1


def test_comments_enabled_defaults_to_true_when_absent(app, db_session, redis_lock_only_double):
    """The ternary's else. Seeded False so the default's True is observable
    -- the column's own default would make this vacuous.
    """
    post = _seed_post()
    post.comments_enabled = False
    db.session.commit()

    update_post_from_activity(post, _update(name='t', content='x'))

    assert post.comments_enabled is True


def test_a_post_unparseable_updated_falls_back_to_now(app, db_session, redis_lock_only_double):
    """The post side's `except ValueError`, mirroring the reply's."""
    post = _seed_post()

    update_post_from_activity(post, _update(
        name='t', content='x', updated='not a timestamp',
    ))

    assert post.ap_updated.year == utcnow().year
```

- [ ] **Step 2: Finish the two flair tests' seeding**

Both flair tests carry a comment where the seeding line belongs. Read
`make_post_flair` and `make_community_flair` in `tests/factories.py`, seed an
existing flair on the post in each, and delete the comments. If a post's flair
cannot be seeded without also seeding a community flair, do both and say so.

- [ ] **Step 3: Mutation-test**

The `'tag' in` gate, the `isinstance(list)` conjunct, each of the three tag
type comparisons, the community-name comparison, the Mention guards, both
disjuncts of the flair-application gate, the `comments_enabled` ternary and
the `except ValueError`. One at a time.

**The flair gate's `or` has three operands** — `len(flair_tags) > 0`,
`software == 'piefed'`, `software == 'pylova'`. Three operands, three kills;
if `pylova` proves unkillable because no test uses it, add the test rather
than accepting the gap.

- [ ] **Step 4: Run and commit**

Run: `./run_tests.sh tests/test_ap_update_pair.py -q`
Expected: **10 tests added**, plus any Step 3 showed were needed.

```bash
git add tests/test_ap_update_pair.py
git commit -m "test: cover the post update's tag walk and timestamp"
```

---

### Task 10: The three in-scope crash fixes

**Files:**
- Modify: `app/activitypub/util.py`
- Modify: `tests/test_ap_update_pair.py`

**Interfaces:**
- Consumes: Task 1's helpers and fixture, Task 4's `_mention`.
- Produces: nothing later tasks need.

**The first task in this sub-project that changes production code. THREE
FIXES, THREE SEPARATE COMMITS.** Each is a crash a peer triggers by choosing
what to send, and each has its correct spelling already in the file.

**Fix A — `update_post_from_activity` subscripts `source['mediaType']` without
checking the key is there.** The reply function checks `'mediaType' in
request_json['object']['source']` first. Add the same conjunct, in the same
position.

**Fix B — `update_post_reply_from_activity` calls `.startswith` on a `content`
that may be `null`.** The post function guards
`request_json['object']['content'] is not None`. Add the same conjunct.

**Fix C — `update_post_from_activity` subscripts `json_tag['type']` unguarded
twice and guards it once, in the same loop.** The `Hashtag` and
`lemmy:CommunityTag` comparisons read the key directly; the `Mention`
comparison eight lines below checks `'type' in json_tag` first. Add that same
check to the two that lack it. **This is one defect at two call sites — two
separate mutation kills**, the one-clause-several-sites gap this campaign has
hit four times.

For each fix, four steps.

- [ ] **Fix A — Step 1: Write the pin**

```python
def test_a_post_source_with_no_media_type_is_skipped(app, db_session, redis_lock_only_double):
    """A peer sending `source` as an object with content but no `mediaType`
    used to raise KeyError out of this function. The reply function checked
    membership first; this one now does too.

    Asserts the content arm still ran and fell to a later branch, not merely
    that nothing raised -- a guard that abandoned the whole Update would pass
    a bare "no exception" test.
    """
    post = _seed_post()
    post.body = 'seeded body'
    db.session.commit()

    update_post_from_activity(post, _update(
        name='t',
        content='<p>from html</p>',
        source={'content': 'from markdown'},
    ))

    assert post.body == 'from html'
```

- [ ] **Fix A — Step 2:** Run it and watch it FAIL with the `KeyError`.
  **Quote the failure verbatim in your report.**
- [ ] **Fix A — Step 3:** Add the conjunct; run; expect green.
- [ ] **Fix A — Step 4:** Mutation-test: remove the new conjunct alone,
  confirm this test fails and the Task 6 tests still pass, restore, confirm
  `git diff app/activitypub/util.py` shows only the intended change. Commit:

```bash
git add app/activitypub/util.py tests/test_ap_update_pair.py
git commit -m "fix: check a post source object has a mediaType before reading it"
```

- [ ] **Fix B — Steps 5-8:** the same four steps for the reply's `None`
  content. The pin serves `content=None` and asserts the reply's seeded body
  survived; the pre-fix failure is an `AttributeError`. Commit:

```bash
git commit -m "fix: skip a reply update whose content is null"
```

- [ ] **Fix C — Steps 9-12:** the same four steps for the two unguarded
  `json_tag['type']` subscripts. **The pin must reach both sites**: a document
  whose tag list contains one object with no `type` key, asserted to leave the
  post's tags and flair untouched while the rest of the Update applied.
  Mutation-test **each site separately** — remove the guard from the `Hashtag`
  comparison alone and confirm the pin fails, restore, then the
  `lemmy:CommunityTag` comparison alone, restore. Commit:

```bash
git commit -m "fix: check a tag object has a type before reading it"
```

- [ ] **Step 13: Check for a vacated branch.** Each fix may have removed the
  only path some branch was reached by. For each, name the branch it used to
  cover and the test that covers it now. If none does, add one and
  mutation-prove it.

- [ ] **Step 14: Audit docstrings falsified by the fixes.** Every claim in the
  test file that one of these functions crashes, or that a sibling lacks a
  guard, is now suspect. **Grep the identifier, not only the prose** — the
  campaign's most recent lesson is that a docstring describing production
  structure in words has no identifier to grep for and must be re-read. Check
  every docstring in the file that makes a claim about the other function.

---

### Task 11: The two fixes carried forward from sub-project 13

**Files:**
- Modify: `app/activitypub/util.py`
- Modify: `tests/test_ap_refresh_profiles.py`

**Interfaces:**
- Consumes: nothing from this slice's earlier tasks — these live in the
  refresh trio, not the update pair.
- Produces: nothing.

**TWO FIXES, TWO SEPARATE COMMITS.** Both were found and registered by
sub-project 13, both meet the fix rule, and both were left because 13's fix
budget had been ruled closed. They are fixed here on the same terms as if
found here.

Work in `tests/test_ap_refresh_profiles.py`, the file sub-project 13 built —
these tests belong beside the ones that already cover those functions.

**Fix D — D232: the following-collection fetch sends no `Accept` header.**
`refresh_feed_profile_task`'s `get_request(feed.ap_following_url)` is the only
`get_request` in the refresh trio that omits
`headers={'Accept': 'application/activity+json'}`. Every sibling has it.

This one is **more urgent than it looks**: sub-project 13 added a decode guard
to that same fetch, so a peer that content-negotiates and serves HTML no
longer crashes — it returns silently, and the feed's `FeedItem` set stops
syncing with nothing in the logs. 13's crash fix converted a loud failure into
a quiet one.

- [ ] **Fix D — Step 1:** Write a pin that asserts the **request carried the
  header**. `http_mock` is respx; read how the existing tests in that file
  register routes and assert on recorded requests. A test that only checks the
  response was handled cannot see a missing header.
- [ ] **Fix D — Step 2:** Run it and watch it FAIL. Quote the failure verbatim.
- [ ] **Fix D — Step 3:** Add the header; run; expect green.
- [ ] **Fix D — Step 4:** Mutation-test: remove the header alone, confirm this
  test fails, restore. Commit:

```bash
git add app/activitypub/util.py tests/test_ap_refresh_profiles.py
git commit -m "fix: send an Accept header with a feed's following collection fetch"
```

- [ ] **Fix E — Steps 5-8: D219(c), the feed owners removal loop does not
  unwrap dict entries.** The community moderators removal loop does
  `if isinstance(actor, dict): actor = actor['id']` before comparing; the feed
  owners removal loop compares directly, so a Lemmy-shaped owners collection
  raises `AttributeError: 'dict' object has no attribute 'lower'`. Two lines,
  copied from the loop in the same file.

  The pin serves an owners collection whose `orderedItems` entries are objects
  rather than strings, and asserts the removal pass completed — read the
  existing feed owners tests in that file for the fixture shape, and note that
  a removal pass needs an existing `FeedMember` to consider removing.

  Witnessed failure, fix, mutation, then commit:

```bash
git commit -m "fix: unwrap dict entries in the feed owners removal loop"
```

- [ ] **Step 9: Update both register entries.** D232 and D219(c) currently
  read as registered-not-fixed. Add a line to each saying it was fixed in this
  sub-project, with the commit. Do not renumber them and do not move them.

- [ ] **Step 10: Run the refresh file and report its count.**

Run: `./run_tests.sh tests/test_ap_refresh_profiles.py -q`
Expected: **2 tests added** to that file. Report the total you observed.

---

### Task 12: Register the findings, record the harness facts, raise the floor

**Files:**
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`
- Modify: `tests/README.md`
- Modify: `coverage_floors.ini`

**Interfaces:**
- Consumes: every earlier task's findings, via the SDD ledger.
- Produces: nothing.

**Write no tests and change no production code in this task.**

- [ ] **Step 1: Read the sources**

The SDD ledger for this plan in full, then the per-task reports, then the
spec. **Register what was found, not what was predicted** — the list below was
written before the slice ran.

- [ ] **Step 2: Write the register entries, numbered from D236**

Read several existing entries first and match the house style. Each states the
defect, where it lives **by content, with a line citation correct at your
commit**, how it was found, whether it was fixed or registered, and if
registered, why not.

At minimum, and the ledger will have more:

1. **`update_post_from_activity` crashed on a `source` with no `mediaType`** —
   FIXED, with its commit. One of two right; the correct spelling was 120
   lines away in the same file.
2. **`update_post_reply_from_activity` crashed on `"content": null`** — FIXED,
   with its commit. The mirror of the first: the other function of the pair
   gets this one right and the first one wrong.
3. **`update_post_from_activity` read `json_tag['type']` unguarded at two
   sites and guarded it at a third, in the same loop** — FIXED, with its
   commit. Stronger than a pair disagreement: the loop disagreed with itself.
4. **D232 and D219(c) fixed** — cross-reference the original entries rather
   than restating them.
5. **`update_post_reply_from_activity` ignores a lone Mention**, because its
   tag gate requires `len(tag) > 1` where the post function's has no length
   condition.
6. **Only the reply path localises its notification to the recipient**, with
   `force_locale(get_recipient_language(...))`; the post path uses `_()` under
   whatever locale is current.
7. **Only the reply path de-duplicates Mentions**, with four suppression
   rules; the post path has none.
8. **Only the post path reads `contentMap`**, and only the post path compares
   against the stored `language_id` before assigning.
9. **Only the post path handles an object-level `mediaType`.**
10. **The two locks differ by 6x and 10x** — 60/60 against 10/6 — with no
    stated reason.

- [ ] **Step 3: Update BOTH live "Next free number" notes.** Find every
occurrence; the two carrying the campaign's current value must both change.
The historical ones are frozen — leave them alone. **Say in your report how
you told them apart**: a frozen note sits inside a heading naming an old
D-range.

- [ ] **Step 4: Add the harness facts to `tests/README.md`.** Read the file
first and match its style; the numbered facts currently run to **70**. Add
only what is not already there. Candidates:

- **`redis_double` cannot back a `with redis_client.lock(...)` block**, and
  the narrow lock-only double is the remedy — this is already fact 4's
  subject, so check whether it needs extending rather than repeating.
- **`make_instance` defaults `software='mastodon'`, which is in
  `MICROBLOG_APPS`** — so a test that means to take a non-microblog path must
  say so explicitly.
- **`make_community` hardcodes `instance_id=1` and `user_id=1`**, so an
  instance and a user must be seeded first to occupy those ids.
- **These two functions commit on `db.session`, whose `expire_on_commit` is at
  its default `True`** — unlike the refresh tasks' `get_task_session()`, so no
  explicit refresh is needed after calling them.
- Anything else in the ledger's rulings that generalises.

Say which candidates you dropped and why. **A redundant fact in a 70-fact file
costs a reader more than a missing one.**

- [ ] **Step 5: Raise the floor.** `coverage_floors.ini`,
`app/activitypub/util.py`: raise from **61** to the measured blended figure
rounded down. **The controller supplies that figure — do not run a coverage
run.**

- [ ] **Step 6: Verify every citation.** Tasks 10 and 11 moved lines in
`app/activitypub/util.py`. Verify every `app/*.py` citation you wrote against
current source, and **verify function attributions separately from line
ranges** — sub-project 13 shipped two false attributions whose line numbers
were correct, because the verification pass checked only the range. A citation
of the form "*code* inside *function* (`:N`)" carries two claims and needs two
checks.

- [ ] **Step 7: Commit**

```bash
git add docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md tests/README.md coverage_floors.ini
git commit -m "docs: register sub-project 14's findings and raise the util.py floor"
```

---

## Self-review

**Spec coverage.** Every scoped region has tasks: the reply function's content
and source (Task 1), simple applications (2), attachment loop (3), Mention
walk (4) and de-duplication (5); the post function's content chain (6), title
and flags (7), language (8), tag walk and timestamp (9). The spec's three
authorised crash fixes are Task 10; its two carried-forward fixes are Task 11;
its register, harness facts and floor are Task 12. Every row of the spec's
asymmetry table is reached: `source['mediaType']` (Tasks 1, 6, 10), `content`
null (6, 10), object `mediaType` (6), `contentMap` and the change comparison
(8), the tag list gate (4, 9), `json_tag['type']` (9, 10), Mention locale and
de-duplication (5, 12), lock timeouts (12). The spec's nine success criteria
map to Tasks 1-9 (1-3), Task 10 (4), Task 11 (5), Task 12 (6-8) and the
controller's final run (9).

**Placeholder scan.** No "TBD", no "add appropriate error handling". Every
code step carries real code. **Five steps deliberately say "read X and finish
this"** — Task 4's Step 2 (the user-block setup), Task 5's Step 2 (the four
suppression rules), Task 8's Step 2 (two assertions weaker than their
docstrings), Task 9's Step 2 (flair seeding) and Task 7's Step 2 (the third
NSFL keyword). Each names what to read, what to produce, and what to report if
the answer differs from the plan's guess. Task 8's is the sharpest: it
explicitly says a test whose docstring overstates it must not ship.

**Type consistency.** `_seed_post(software=...)` and `_seed_reply(software=...)`
take the same parameter and `_seed_reply` delegates to `_seed_post`.
`_update(**fields)` is called with keyword arguments in every task.
`_seed_local_recipient(name)` and `_mention(name)` are produced by Task 4 and
consumed by Tasks 5 and 9, with matching defaults. `update_post_from_activity`
takes `(post, request_json)` and `update_post_reply_from_activity` takes
`(reply, request_json)` — both used with that arity throughout.

**Two deliberate departures from sub-project 13's plan shape**, both from
lessons it paid for:

1. **Per-task deltas instead of running totals.** Thirteen stated running
   totals and corrected them five times, always because a task grew and every
   later number stayed internally consistent while being wrong.
2. **Task 10 states its site count in the task text** — "one defect at two
   call sites, two separate mutation kills" — rather than leaving the
   implementer to infer it. The one-clause-several-sites gap has been hit four
   times, and once it was the controller's own brief that caused it by naming
   three sites when there were four.
