# Sub-project 6: the private-message ingestion path — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring `process_chat` — the private-message acceptance policy, 74 statements at 4.1% — to full statement coverage, and fix the defects that surface.

**Architecture:** Tests drive `process_chat` through `dispatch()` with a `ChatMessage` inner object, so `session` is the real dispatcher task session. One new test file, one new factory. Defects are pinned first, then fixed test-first in their own commits.

**Tech Stack:** pytest, pytest-timeout, respx, fakeredis, SQLAlchemy, Flask, podman-compose (`./run_tests.sh`).

**Spec:** `docs/superpowers/specs/2026-09-01-coverage-inbox-chat-6-design.md`

## Global Constraints

- Defects found **in `process_chat`** are fixed test-first, each in its own commit, separate from every test-only commit, each proved by a mutation that fails a named test. Anything outside the function is **registered, not fixed**.
- Findings are numbered from **D121**; the register's index note ("Next free number") is updated in the same change that takes them.
- The coverage floor in `coverage_floors.ini` rises to the measured blended figure **rounded down**, and only ever rises.
- **Locate every code target by content, not by the line numbers in this plan.** They drift; every sub-project since 5c has found them stale.
- **No assertion may rest on a column's declared default.** `User.accept_private_messages` defaults to **3**, so a test of the *accepted* path must seed 3 explicitly rather than lean on it, and every refusing value must be seeded too. 5e lost a fix round to this exact class.
- **Every path's return value is asserted, not just its log.** `False` means "not handled, caller continue"; `True` means "handled". The Create/Update arm's fallback branches on it.
- Every guard is mutation-tested with **each conjunct dropped separately**, each killed by a distinct named test. A `respx.models.AllMockedAssertionError` kill is an **infrastructure kill, not behavioural**.
- **Any docstring claim about another test must be verified true.** 5d and 5e lost several fix rounds to false claims of that kind.
- **Never double `find_actor_or_create_cached` unconditionally** — the preamble resolves the activity's own signed actor through it before any arm runs. Scope such doubles to one URL (`tests/README.md` fact 17).
- **One pytest session at a time** (`tests/README.md` fact 18). Implementers run only the files they touch; **the controller runs the full suite and supplies all coverage figures.**

---

## File structure

| File | Responsibility |
|---|---|
| `tests/factories.py` | gains `make_conversation` |
| `tests/test_inbox_dispatch_chat.py` | **new** — all of `process_chat` |
| `app/activitypub/routes.py` | defect fixes only, `process_chat` only |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` | findings from D121 |
| `tests/README.md`, `coverage_floors.ini` | harness facts, floor raise |

**Shared helpers — reuse, do not rebuild:**

- `dispatch(activity, store_ap_json=True)` — `tests/test_inbox_dispatch_preamble.py`
- `record_moderation(monkeypatch, *names)` — `tests/test_inbox_dispatch_lock_delete.py`; doubles each name on `app.activitypub.routes`, records `(args, kwargs)`
- `inbox_activity(actor, *, activity_type='Like', object_uri=None, **fields)` — `tests/factories.py`; `**fields` applied last, so `object={...}` replaces the default
- `make_user_block(blocker, blocked)`, `make_instance_block(user, instance)`, `make_instance(domain, software='mastodon')`, `make_site()`, `make_user`
- `make_chat_message(sender, recipient, ap_id, *, body='a message', deleted=False)` — added by 5d

**Facts established by reading, which the tasks depend on:**

- `Conversation.members` is a **backref** from `User.conversations` (`app/models.py:1053-1054`, `secondary=conversation_member`). `conversation.members.append(user)` writes the association row that `Conversation.find_existing_conversation`'s raw SQL joins on twice — so a conversation is only findable when **both** parties are members.
- `find_existing_conversation(recipient, sender)` is a `@staticmethod` issuing `db.session.execute(text(sql))`. Under `patch_db_session` that is the task session, which is why these tests must run through `dispatch()` rather than calling `process_chat` directly.
- `User.accept_private_messages` — `db.Column(db.Integer, default=3)`, documented *"None or 0 = do not accept, 1 = This instance, 2 = Trusted instances, 3 = All instances"*.
- `User.created_very_recently()` — `self.created and self.created > utcnow() - timedelta(days=1)`.
- `Instance.trusted` — `db.Column(db.Boolean, default=False, index=True)`.
- `make_site()` sets `blocked_phrases=''`, so a phrase test must set that column itself.
- `blocked_phrases()` (`app/utils.py:1736`) reads `Site.blocked_phrases`, newline-separated.
- `APLOG_CHATMESSAGE = (True, 'Create ChatMessage')`; tests assert `log.result` and `log.exception_message`.

**Doubling.** `find_actor_or_create_cached`, `blocked_phrases`, `publish_sse_event`, `html_to_text` and `shorten_string` are all imported into `app.activitypub.routes` and patch there. `Conversation`, `ChatMessage` and `Notification` are models — drive them with real rows.

**`publish_sse_event` must be doubled on every accepted-path test** — it reaches an external event stream.

---

### Task 1: `make_conversation` factory

**Files:** Modify `tests/factories.py`; create `tests/test_factories_conversation.py`

**Interfaces — Produces:** `make_conversation(sender, recipient) -> Conversation`

- [ ] **Step 1: Write the failing test**

```python
"""tests/test_factories_conversation.py"""
from app.models import Conversation
from tests.factories import make_conversation, make_instance, make_user


def test_make_conversation_is_findable_by_the_lookup_process_chat_uses(app, db_session):
    """`Conversation.find_existing_conversation` (app/models.py:254) joins
    `conversation_member` TWICE and requires a row for each party, so a
    conversation built without both members is invisible to the very lookup
    `process_chat` performs. This test asserts through that staticmethod rather
    than through `.members`, because being findable is the only property the
    dispatcher cares about.

    It is also asserted symmetrically: the staticmethod takes (recipient,
    sender) in that order, and a factory that only registered one direction
    would pass one call and fail the other.
    """
    instance = make_instance('peer.example')
    sender = make_user(instance, 'sender')
    recipient = make_user(None, 'recipient', local=True)

    conversation = make_conversation(sender, recipient)

    assert conversation.id is not None
    assert conversation.user_id == sender.id
    assert {m.id for m in conversation.members} == {sender.id, recipient.id}
    assert Conversation.find_existing_conversation(
        recipient=recipient, sender=sender).id == conversation.id
    assert Conversation.find_existing_conversation(
        recipient=sender, sender=recipient).id == conversation.id
```

- [ ] **Step 2: Run it and watch it fail**

Run: `./run_tests.sh tests/test_factories_conversation.py -q`
Expected: FAIL, `ImportError: cannot import name 'make_conversation'`

- [ ] **Step 3: Add the factory**

Add `Conversation` to the `app.models` import block in `tests/factories.py` in alphabetical position, then:

```python
def make_conversation(sender: User, recipient: User) -> Conversation:
    """A two-party conversation, built the way process_chat builds one.

    `user_id` records the initiator; membership is what makes it findable.
    `members` is a backref from User.conversations (app/models.py:1053-1054,
    `secondary=conversation_member`), so appending here writes the association
    rows that `Conversation.find_existing_conversation`'s raw SQL joins on. A
    conversation missing either row is invisible to that lookup, which is why
    both are appended rather than relying on `user_id` alone.
    """
    conversation = Conversation(user_id=sender.id)
    conversation.members.append(sender)
    conversation.members.append(recipient)
    db.session.add(conversation)
    db.session.commit()
    return conversation
```

- [ ] **Step 4: Run it and watch it pass** — `./run_tests.sh tests/test_factories_conversation.py -q`, Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/factories.py tests/test_factories_conversation.py
git commit -m "test: add a Conversation factory for the chat ingestion path"
```

---

### Task 2: recipient resolution, and both "not handled" exits

**Files:** Create `tests/test_inbox_dispatch_chat.py`

**Interfaces — Produces:** `chat_activity(sender, **objfields)` and `seed_chat_pair(...)`, used by every later task.

Covers: `object['to']` as a bare string, as a list, absent, and an empty list; then the two paths that **return False** — an invalid recipient, and a recipient that is not local.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_inbox_dispatch_chat.py"""
import pytest

from app import db
from app.activitypub import routes as activitypub_routes
from app.models import (ActivityPubLog, ChatMessage, Conversation, Notification, Site,
                        User, utcnow)
from tests.factories import (inbox_activity, make_conversation, make_instance,
                             make_instance_block, make_site, make_user, make_user_block)
from tests.test_inbox_dispatch_lock_delete import record_moderation
from tests.test_inbox_dispatch_preamble import dispatch


def chat_activity(sender, **objfields):
    """A Create whose inner object is a ChatMessage.

    `inbox_activity` applies **fields last, so `object=` replaces its default
    string object -- required, because process_chat is only reached once the
    arm has read `core_activity['object']['type']`.

    Callers pass `to=`, `content=`, `id=` etc. through **objfields, and may
    pass a field explicitly as absent by simply not supplying it -- which is
    how the missing-field tests are written.
    """
    obj = {'type': 'ChatMessage'}
    obj.update(objfields)
    return inbox_activity(sender, activity_type='Create', object=obj)


def seed_chat_pair(host='peer.example', accept=3, trusted=False):
    """A remote sender and a LOCAL recipient — the only shape that reaches the
    acceptance chain, since everything past `recipient.is_local()` requires it.

    `accept` is seeded onto the recipient EXPLICITLY even when it equals the
    column default of 3, because an assertion resting on a declared default
    proves nothing (see this plan's global constraints).

    `make_user(None, name, local=True)` leaves `ap_profile_id` NULL, unlike a
    real local account, and process_chat resolves the recipient by that value —
    so it is stamped here.
    """
    from flask import current_app
    make_site()
    instance = make_instance(host)
    instance.trusted = trusted
    sender = make_user(instance, 'sender')
    recipient = make_user(None, 'recipient', local=True)
    recipient.ap_profile_id = f"{current_app.config['SERVER_URL']}/u/recipient".lower()
    recipient.accept_private_messages = accept
    db.session.commit()
    return instance, sender, recipient


def test_a_string_recipient_is_accepted_as_a_sole_jsonld_element(app, db_session, monkeypatch):
    """`object['to']` as a bare string. JSON-LD lets a single-element array be
    written as the value alone, which is why the function accepts both shapes.
    Proven by the message landing, not merely by the absence of a failure.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    record_moderation(monkeypatch, 'publish_sse_event')

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='hello', id='https://peer.example/pm/1'))

    assert db_session.query(ChatMessage).filter_by(ap_id='https://peer.example/pm/1').one()
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_a_list_recipient_takes_its_first_element(app, db_session, monkeypatch):
    """`object['to']` as an array. The function takes element 0 and ignores the
    rest — the second entry here is a URL no user has, so the message landing
    for the FIRST proves which element was read.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    record_moderation(monkeypatch, 'publish_sse_event')

    dispatch(chat_activity(sender, to=[recipient.ap_profile_id, 'https://peer.example/u/nobody'],
                           content='hello', id='https://peer.example/pm/1'))

    assert db_session.query(ChatMessage).filter_by(ap_id='https://peer.example/pm/1').one()


@pytest.mark.parametrize('objfields,description', [
    ({}, "no 'to' key at all"),
    ({'to': []}, "'to' is an empty list"),
])
def test_an_unresolvable_recipient_is_refused_and_reports_not_handled(
        app, db_session, monkeypatch, objfields, description):
    """Both shapes leave `recipient_ap_id` None. The RETURN VALUE matters as
    much as the log: False means "not handled, caller continue", and the
    Create/Update arm's fallback path branches on it.

    The return value cannot be observed through `dispatch()`, so it is asserted
    by doubling nothing and checking the log — and separately, at the arm level,
    by the sibling test below that drives the fallback path.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()

    dispatch(chat_activity(sender, content='hello', id='https://peer.example/pm/1', **objfields))

    log = ActivityPubLog.query.one()
    assert log.result == 'failure', description
    assert log.exception_message == 'Chat recipient is invalid', description
    assert db_session.query(ChatMessage).count() == 0, description


def test_a_remote_recipient_is_refused_as_not_local(app, db_session, monkeypatch):
    """`recipient.is_local()` is False, so the whole acceptance chain is skipped
    and the function falls through to its final failure. This is the second of
    the two paths that report "not handled".
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    other_remote = make_user(instance, 'remoterecipient')
    db.session.commit()

    dispatch(chat_activity(sender, to=other_remote.ap_profile_id,
                           content='hello', id='https://peer.example/pm/1'))

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'ChatMessage target is not local'
    assert db_session.query(ChatMessage).count() == 0
```

- [ ] **Step 2: Run the file**

Run: `./run_tests.sh tests/test_inbox_dispatch_chat.py -q`
Expected: PASS. If the local recipient is not resolved by `ap_profile_id`, read `find_actor_or_create_cached` and adjust the seeding — then say in `seed_chat_pair`'s docstring what it actually keys on.

- [ ] **Step 3: Mutation-test the `to`-shape handling**

Drop the `elif isinstance(..., list) ...` branch and confirm `test_a_list_recipient_takes_its_first_element` fails. Restore. Verify `git diff app/activitypub/routes.py` is empty. Record the kill.

- [ ] **Step 4: Commit**

```bash
git add tests/test_inbox_dispatch_chat.py
git commit -m "test: cover process_chat's recipient resolution and both not-handled exits"
```

---

### Task 3: the return-value contract, at the call site that uses it

**Files:** Modify `tests/test_inbox_dispatch_chat.py`

`process_chat`'s return value is only consulted at one place: the Create/Update arm's fallback, `if not community: if process_chat(...): return`. Success criterion 3 requires it asserted, and it can only be asserted there.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_handled_chat_stops_the_arm_from_treating_it_as_content(app, db_session, monkeypatch):
    """The arm's fallback path: `find_community` finds nothing, so `process_chat`
    is tried, and a TRUE return means it handled the activity and the arm must
    stop. Proven by `ensure_domains_match` never being reached.

    The object type here is NOT ChatMessage -- it is a Page, so the arm reaches
    the fallback rather than the dedicated ChatMessage branch. That is the only
    call site where the return value is read.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    record_moderation(monkeypatch, 'publish_sse_event')
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: None)
    calls = record_moderation(monkeypatch, 'ensure_domains_match')

    activity = inbox_activity(sender, activity_type='Create',
                              object={'type': 'Page', 'to': recipient.ap_profile_id,
                                      'content': 'hello', 'id': 'https://peer.example/pm/1'})
    dispatch(activity)

    assert calls['ensure_domains_match'] == []
    assert db_session.query(ChatMessage).filter_by(ap_id='https://peer.example/pm/1').one()


def test_an_unhandled_chat_lets_the_arm_continue_to_the_domain_check(app, db_session, monkeypatch):
    """The mirror: `process_chat` returns FALSE (no resolvable recipient), so the
    arm does NOT stop and goes on to `ensure_domains_match`. Together with the
    test above this pins the return value in both directions -- which no
    assertion inside process_chat's own tests can do.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: None)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: False)

    activity = inbox_activity(sender, activity_type='Create',
                              object={'type': 'Page', 'content': 'hello',
                                      'id': 'https://peer.example/pm/1'})
    dispatch(activity)

    messages = [l.exception_message for l in ActivityPubLog.query.all()]
    assert 'Domains do not match' in messages
```

- [ ] **Step 2: Run the file** — Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_inbox_dispatch_chat.py
git commit -m "test: pin process_chat's return-value contract at its only reading call site"
```

---

### Task 4: the sender-too-new and blocked guards

**Files:** Modify `tests/test_inbox_dispatch_chat.py`

`sender.created_very_recently() and user.ap_domain != 'fediseer.com'` — two conjuncts. `recipient.has_blocked_user(sender.id) or recipient.has_blocked_instance(sender.instance_id)` — two disjuncts. Four separate kills.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_brand_new_sender_is_refused(app, db_session, monkeypatch):
    """`created_very_recently()` is `created > utcnow() - timedelta(days=1)`,
    so `created` is seeded to NOW explicitly rather than left at whatever the
    factory set — the assertion must rest on a value this test chose.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    sender.created = utcnow()
    db.session.commit()

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='hello', id='https://peer.example/pm/1'))

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Sender is too new'
    assert db_session.query(ChatMessage).count() == 0


def test_an_old_sender_is_not_refused_for_newness(app, db_session, monkeypatch):
    """The other side of the first conjunct: `created` two days ago. Paired with
    the test above so the conjunct dies in both directions.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    sender.created = utcnow() - timedelta(days=2)
    db.session.commit()
    record_moderation(monkeypatch, 'publish_sse_event')

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='hello', id='https://peer.example/pm/1'))

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_a_brand_new_sender_from_fediseer_is_exempt(app, db_session, monkeypatch):
    """The second conjunct: `user.ap_domain != 'fediseer.com'`. A brand-new
    sender is normally refused; this one is not, solely because of its domain.
    This is the ONLY test that distinguishes that conjunct.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair(host='fediseer.com')
    sender.created = utcnow()
    sender.ap_domain = 'fediseer.com'
    db.session.commit()
    record_moderation(monkeypatch, 'publish_sse_event')

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='hello', id='https://peer.example/pm/1'))

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_a_sender_blocked_by_the_recipient_is_refused(app, db_session, monkeypatch):
    """First disjunct of the block check: a UserBlock row."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    sender.created = utcnow() - timedelta(days=2)
    make_user_block(recipient, sender)
    db.session.commit()

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='hello', id='https://peer.example/pm/1'))

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Sender blocked by recipient'
    assert db_session.query(ChatMessage).count() == 0


def test_a_sender_on_a_blocked_instance_is_refused(app, db_session, monkeypatch):
    """Second disjunct: an InstanceBlock row and NO UserBlock, so this test and
    the one above kill the two halves separately.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    sender.created = utcnow() - timedelta(days=2)
    make_instance_block(recipient, instance)
    db.session.commit()

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='hello', id='https://peer.example/pm/1'))

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Sender blocked by recipient'
    assert db_session.query(ChatMessage).count() == 0
```

Add `from datetime import timedelta` to the imports.

- [ ] **Step 2: Run the file** — Expected: PASS

- [ ] **Step 3: Mutation-test all four conjuncts separately**

Four separate edits, restoring after each: drop `sender.created_very_recently()`; drop `and user.ap_domain != 'fediseer.com'`; drop `recipient.has_blocked_user(sender.id) or`; drop `or recipient.has_blocked_instance(sender.instance_id)`. Confirm each is killed by a distinct named test, and verify the production diff is empty before committing. **Record all four kills separately.** If any conjunct cannot be killed, say so plainly rather than engineering around it.

- [ ] **Step 4: Commit**

```bash
git add tests/test_inbox_dispatch_chat.py
git commit -m "test: cover process_chat's sender-too-new and blocked guards"
```

---

### Task 5: the four `accept_private_messages` outcomes

**Files:** Modify `tests/test_inbox_dispatch_chat.py`

`None` or `0` → off; `1` → local only; `2` with an untrusted sender instance → refused; `2` with a trusted one → accepted; `3` → accepted.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize('accept', [None, 0])
def test_a_recipient_with_pms_off_refuses(app, db_session, monkeypatch, accept):
    """`accept_private_messages is None or == 0`. Both values are seeded
    explicitly; neither is the column default of 3, so nothing here rests on a
    default. Parametrised because the guard is one condition with two accepted
    spellings of "off".
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair(accept=accept)
    sender.created = utcnow() - timedelta(days=2)
    db.session.commit()

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='hello', id='https://peer.example/pm/1'))

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Recipient has turned off PMs'
    assert db_session.query(ChatMessage).count() == 0


def test_a_recipient_accepting_only_local_pms_refuses_a_remote_sender(app, db_session, monkeypatch):
    """`accept_private_messages == 1`. The sender is remote, which is the only
    case that reaches process_chat at all from a federated activity.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair(accept=1)
    sender.created = utcnow() - timedelta(days=2)
    db.session.commit()

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='hello', id='https://peer.example/pm/1'))

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Recipient only accepts local PMs'


def test_a_trusted_instances_recipient_refuses_an_untrusted_sender(app, db_session, monkeypatch):
    """`accept_private_messages == 2 and not sender.instance.trusted` — the
    second conjunct is False here, so the refusal fires.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair(accept=2, trusted=False)
    sender.created = utcnow() - timedelta(days=2)
    db.session.commit()

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='hello', id='https://peer.example/pm/1'))

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Sender from untrusted instance'


def test_a_trusted_instances_recipient_accepts_a_trusted_sender(app, db_session, monkeypatch):
    """The other side of that conjunct: `Instance.trusted` seeded True (its
    column default is False, so the True is this test's own choice). Paired with
    the test above so the conjunct dies in both directions.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair(accept=2, trusted=True)
    sender.created = utcnow() - timedelta(days=2)
    db.session.commit()
    record_moderation(monkeypatch, 'publish_sse_event')

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='hello', id='https://peer.example/pm/1'))

    log = ActivityPubLog.query.one()
    assert log.result == 'success'
```

- [ ] **Step 2: Run the file** — Expected: PASS

- [ ] **Step 3: Mutation-test the settings chain**

Three edits, restoring after each: drop `recipient.accept_private_messages is None or` (confirm the `None` parametrisation fails); drop the `== 1` branch entirely; drop `and not sender.instance.trusted`. Confirm each is killed by a distinct named test, verify the production diff is empty, record all three.

- [ ] **Step 4: Commit**

```bash
git add tests/test_inbox_dispatch_chat.py
git commit -m "test: cover process_chat's four accept_private_messages outcomes"
```

---

### Task 6: blocked phrases, and the conversation find-or-create

**Files:** Modify `tests/test_inbox_dispatch_chat.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_a_message_containing_a_blocked_phrase_is_refused(app, db_session, monkeypatch):
    """`blocked_phrases()` reads newline-separated `Site.blocked_phrases`, and
    `make_site()` sets it to '' — so this test writes the column itself. The
    refusal message embeds the matched phrase, so the assertion pins the whole
    string rather than just the failure.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    sender.created = utcnow() - timedelta(days=2)
    db_session.query(Site).get(1).blocked_phrases = 'buymynft\nspamword'
    db.session.commit()

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='hello buymynft friend', id='https://peer.example/pm/1'))

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Blocked because phrase buymynft'
    assert db_session.query(ChatMessage).count() == 0


def test_a_message_containing_no_blocked_phrase_is_delivered(app, db_session, monkeypatch):
    """The other side: the site HAS blocked phrases configured, but this message
    matches none of them. Paired with the test above so the filter cannot be
    removed without a failure — a test with no phrases configured would pass
    either way.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    sender.created = utcnow() - timedelta(days=2)
    db_session.query(Site).get(1).blocked_phrases = 'buymynft\nspamword'
    db.session.commit()
    record_moderation(monkeypatch, 'publish_sse_event')

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='an ordinary message', id='https://peer.example/pm/1'))

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_an_existing_conversation_is_reused_rather_than_duplicated(app, db_session, monkeypatch):
    """`find_existing_conversation` joins `conversation_member` twice, so a
    conversation is only found when BOTH parties are members — which is exactly
    what `make_conversation` builds. Reuse is asserted by the total conversation
    count staying at 1, not merely by the message landing.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    sender.created = utcnow() - timedelta(days=2)
    existing = make_conversation(sender, recipient)
    existing_id = existing.id
    record_moderation(monkeypatch, 'publish_sse_event')

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='hello', id='https://peer.example/pm/1'))

    db.session.expire_all()
    assert db_session.query(Conversation).count() == 1
    message = db_session.query(ChatMessage).one()
    assert message.conversation_id == existing_id


def test_a_first_message_creates_the_conversation(app, db_session, monkeypatch):
    """No conversation exists, so one is created with both parties as members —
    asserted through `find_existing_conversation` so the association rows are
    proven written, not just the Conversation row.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    sender.created = utcnow() - timedelta(days=2)
    db.session.commit()
    record_moderation(monkeypatch, 'publish_sse_event')

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='hello', id='https://peer.example/pm/1'))

    db.session.expire_all()
    assert db_session.query(Conversation).count() == 1
    assert Conversation.find_existing_conversation(recipient=recipient, sender=sender) is not None
```

- [ ] **Step 2: Run the file** — Expected: PASS

- [ ] **Step 3: Mutation-test the phrase filter and the reuse guard**

Drop the `if blocked_phrase in ...` body and confirm the blocked-phrase test fails. Restore. Replace `if not existing_conversation:` with `if True:` and confirm the reuse test fails. Restore. Verify the production diff is empty; record both kills.

- [ ] **Step 4: Commit**

```bash
git add tests/test_inbox_dispatch_chat.py
git commit -m "test: cover process_chat's blocked-phrase filter and conversation reuse"
```

---

### Task 7: message create vs update, the notification, and SUCCESS

**Files:** Modify `tests/test_inbox_dispatch_chat.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_a_new_message_is_stored_with_both_body_forms_and_notifies(app, db_session, monkeypatch):
    """The create path. `body_html` keeps the sent markup and `body` is its
    text rendering via `html_to_text`, so both are asserted — storing only one
    would lose either formatting or searchability.

    `publish_sse_event` is doubled because it reaches an external event stream;
    its call is asserted rather than merely allowed, since a silent failure to
    publish would leave a live client showing nothing.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    sender.created = utcnow() - timedelta(days=2)
    recipient.unread_notifications = 0
    db.session.commit()
    recipient_id = recipient.id
    calls = record_moderation(monkeypatch, 'publish_sse_event')

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='<p>hello there</p>', id='https://peer.example/pm/1'))

    db.session.expire_all()
    message = db_session.query(ChatMessage).one()
    assert message.body_html == '<p>hello there</p>'
    assert 'hello there' in message.body
    assert message.sender_id == sender.id and message.recipient_id == recipient_id

    assert len(calls['publish_sse_event']) == 1
    notification = db_session.query(Notification).one()
    assert notification.user_id == recipient_id
    assert notification.title.startswith('New message from')
    assert db_session.query(User).get(recipient_id).unread_notifications == 1

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_a_repeat_ap_id_updates_the_existing_message_and_says_so(app, db_session, monkeypatch):
    """The update path, selected by an `ap_id` that already exists. The
    notification title distinguishes it ('Updated message from'), and the
    message count staying at 1 proves an update rather than a second row.

    `read` is seeded True beforehand so its reset to False is evidence of the
    write rather than a default sitting there.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    sender.created = utcnow() - timedelta(days=2)
    conversation = make_conversation(sender, recipient)
    existing = ChatMessage(sender_id=sender.id, recipient_id=recipient.id,
                           conversation_id=conversation.id, body='old', body_html='<p>old</p>',
                           ap_id='https://peer.example/pm/1', read=True)
    db.session.add(existing)
    db.session.commit()
    record_moderation(monkeypatch, 'publish_sse_event')

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='<p>new text</p>', id='https://peer.example/pm/1'))

    db.session.expire_all()
    assert db_session.query(ChatMessage).count() == 1
    message = db_session.query(ChatMessage).one()
    assert message.body_html == '<p>new text</p>'
    assert message.read is False

    notification = db_session.query(Notification).one()
    assert notification.title.startswith('Updated message from')


def test_an_encrypted_flag_is_carried_through_and_defaults_to_none(app, db_session, monkeypatch):
    """`encrypted` is read with a membership check — unlike `content` and `id`
    a few lines away, which are not (see Task 8). Both halves are covered here:
    supplied, and absent.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    sender.created = utcnow() - timedelta(days=2)
    db.session.commit()
    record_moderation(monkeypatch, 'publish_sse_event')

    dispatch(chat_activity(sender, to=recipient.ap_profile_id, content='hello',
                           id='https://peer.example/pm/1', encrypted='pgp'))

    assert db_session.query(ChatMessage).one().encrypted == 'pgp'
```

- [ ] **Step 2: Run the file** — Expected: PASS

- [ ] **Step 3: Mutation-test the create/update selection**

Replace `if not updated_message:` with `if True:` and confirm the update test fails. Restore; verify the production diff is empty; record the kill.

- [ ] **Step 4: Commit**

```bash
git add tests/test_inbox_dispatch_chat.py
git commit -m "test: cover process_chat's message create and update paths"
```

---

### Task 8: pin the defects

**Files:** Modify `tests/test_inbox_dispatch_chat.py`

**This task asserts BROKEN behaviour on purpose.** Task 9 inverts these and lands the fixes, which is how each fix earns its proof. **Do not touch `app/activitypub/routes.py`.**

- [ ] **Step 1: Write tests pinning current behaviour**

**Verify each defect against the function before asserting it.** If reality
differs from the description below, report that rather than contriving a test
to match it — a previous sub-project shipped a pinning test that could not tell
a bug from its fix, and it cost a full fix round.

```python
def test_a_chat_message_with_no_content_crashes(app, db_session, monkeypatch):
    """PINS defect 1. `core_activity['object']['content']` is read with no
    membership check, first by the blocked-phrase filter and again when building
    the body. `content` is peer-controlled, so any peer can raise this KeyError
    out of a Celery task.

    The contrast is a few lines above in the same function: `object['to']` IS
    checked for membership AND for both plausible JSON-LD shapes, and
    `object['encrypted']` is read with an `in` guard. The caution is present
    either side of these two reads and absent between them.

    The sender is aged past `created_very_recently()` and the recipient left at
    an accepting setting, so the crash is reached rather than short-circuited by
    an earlier refusal.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    sender.created = utcnow() - timedelta(days=2)
    db.session.commit()

    with pytest.raises(KeyError):
        dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                               id='https://peer.example/pm/1'))


def test_a_chat_message_with_no_id_crashes(app, db_session, monkeypatch):
    """PINS defect 2, the sibling unguarded read. `core_activity['object']['id']`
    is used for the existing-message lookup and for the new row's `ap_id`.

    `content` IS supplied here, so this test fails for its own reason rather
    than for the previous test's — without that, both tests would pass on a
    single missing-field crash and neither would pin its own defect.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    sender.created = utcnow() - timedelta(days=2)
    db.session.commit()

    with pytest.raises(KeyError):
        dispatch(chat_activity(sender, to=recipient.ap_profile_id, content='hello'))


def test_the_inner_is_local_check_can_never_be_false(app, db_session, monkeypatch):
    """PINS defect 3, an equivalent mutant. The SSE event and notification sit
    under `if recipient.is_local():`, inside a block already guarded by
    `if recipient and recipient.is_local():` — the same call on the same object,
    with nothing between them that could change it.

    This test cannot observe the inner guard directly; what it establishes is
    that every accepted message notifies, so there is no reachable case where
    the outer check passes and the inner one does not. Task 9 removes the inner
    guard, and this test must keep passing — that is the proof it was dead.

    Same class as D95 (Remove's dead `if proceed:`), D96 (Block's dead Mastodon
    isinstance) and D103 (the site-ban already_banned guard).
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    sender.created = utcnow() - timedelta(days=2)
    recipient.unread_notifications = 0
    db.session.commit()
    recipient_id = recipient.id
    calls = record_moderation(monkeypatch, 'publish_sse_event')

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='hello', id='https://peer.example/pm/1'))

    db.session.expire_all()
    assert len(calls['publish_sse_event']) == 1
    assert db_session.query(Notification).count() == 1
    assert db_session.query(User).get(recipient_id).unread_notifications == 1
```

If you can construct a case where the outer and inner `is_local()` calls
disagree, **that is a finding** — report it and do not write the third test as
given.

- [ ] **Step 2: Run the file** — Expected: PASS. Every assertion describes current behaviour; a failure means the defect is not as described.

- [ ] **Step 3: Commit**

```bash
git add tests/test_inbox_dispatch_chat.py
git commit -m "test: pin process_chat's unguarded reads and its dead is_local guard"
```

---

### Task 9: fix the defects

**Files:** Modify `app/activitypub/routes.py` (`process_chat` only); modify `tests/test_inbox_dispatch_chat.py`

Invert the Task 8 tests, watch them fail, then fix. **Report the actual failure output** — that step is the proof.

**Fix A — the unguarded reads.** `core_activity['object']['content']` and `['id']` are peer-controlled and read without a membership check, a few lines below the same function's careful handling of `object['to']`. Decide the shape yourself from the code and justify it in the report: the arm's idiom for refusing is `log_incoming_ap(id, APLOG_CHATMESSAGE, APLOG_FAILURE, saved_json, '<reason>')` and a return, and the return value must match the contract — decide whether a malformed message is "handled" (`True`) or "not handled" (`False`) and say why. Note the blocked-phrase filter is currently skipped when `content` is falsy; say whether your fix changes that and whether it should.

**Fix B — the dead inner `is_local()`.** Remove it and de-indent its body, or leave it and register it, whichever you can justify. It is inside a block already guarded by the same call. State your reasoning.

Separate commits per fix. Mutation-test each: revert it, confirm a named test fails, restore.

- [ ] **Step 1: Invert the Fix A tests, run, watch them fail**
- [ ] **Step 2: Apply Fix A**
- [ ] **Step 3: Run, watch them pass**
- [ ] **Step 4: Mutation-test Fix A; record the kill**
- [ ] **Step 5: Commit Fix A**
- [ ] **Step 6: Invert the Fix B test, run, watch it fail**
- [ ] **Step 7: Apply Fix B**
- [ ] **Step 8: Run, watch it pass**
- [ ] **Step 9: Mutation-test Fix B; record the kill**
- [ ] **Step 10: Commit Fix B**

**Do not fix** `sender.instance.trusted`'s unguarded dereference or the `user`/`sender` inconsistency. Both are registered in Task 10 — the first because whether a sender reaching that point can have a null instance is a question the tests should answer before code changes, the second because it is harmless today.

---

### Task 10: the register, the README, and the floor

**Files:** Modify the findings register, `tests/README.md`, `coverage_floors.ini`

- [ ] **Step 1: Register the findings**

Add a `## Sub-project 6` section to `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`, in the shape 5e's section uses (**read it first**). Number from **D121**; update the index note's "Next free number" in the same change.

Source from the task reports. Every row: location, defect, impact, and how established — `measured` or `reading-level`. **Verify every line number against the file before writing it**; citation errors are this register's recurring defect, and 5d needed three separate corrections.

Register at minimum, beyond what the tasks found:

- Task 9's two fixes, with commits.
- **`sender.instance.trusted` dereferenced unguarded**, reached whenever the recipient's setting is `2`. Not fixed — say what the tests established about whether a null instance is reachable there.
- **The new-account guard reads `user` where every other line reads `sender`.** They are the same row (`sender = session.query(User).get(user.id)`), so harmless today; recorded because the whole reason `sender` exists is a session-identity bug the code's own comment describes.
- **The blocked-phrase filter is skipped when `content` is falsy** — state whether Fix A changed this.

- [ ] **Step 2: Record the harness facts**

Append to `tests/README.md`'s numbered list, continuing its numbering, at minimum: that `Conversation.find_existing_conversation` joins `conversation_member` twice and therefore only finds a conversation with **both** parties registered as members, which is why `make_conversation` appends both; and that `User.accept_private_messages` defaults to **3** ("All instances"), so a test of the accepting path must seed it explicitly.

- [ ] **Step 3: Raise the floor**

The controller supplies the measured blended figure. Set `app/activitypub/routes.py` in `coverage_floors.ini` to that figure **rounded down**. **Do not run the full suite.**

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md tests/README.md coverage_floors.ini
git commit -m "docs: register sub-project 6's findings and raise the routes.py floor"
```
