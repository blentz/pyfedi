# Sub-project 6: the private-message ingestion path

**Date:** 2026-09-01
**Module:** `app/activitypub/routes.py`, the function `process_chat`
**Predecessors:** 5a-5e, which brought `process_inbox_request` from 14% to all
but one statement. This slice is the first of its delegates.
**Findings register:** `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`,
continuing at **D121**

## The unit

`process_chat(user, store_ap_json, core_activity, session)` — at the time of
writing `routes.py:2527-2631`, **74 statements, 3 executed (4.1%)**.

It is reached from the Create/Update arm in two ways: directly, when the inner
object's type is `ChatMessage`; and as a fallback, when no community can be
resolved for an ordinary object, where its **return value decides whether the
arm stops**. Sub-project 5e covered both call sites by doubling this function;
this slice tests what it actually does.

It is also the private-message *acceptance policy*: who may message whom, under
which of the recipient's settings, and what content is refused. That makes it a
moderation and abuse surface, not merely an ingestion path.

### Citations drift

Every sub-project from 5c onward found its brief's line numbers stale, and
5e's own fixes moved everything after `routes.py:1200`. **Locate every target
by code content.** Quoted lines are navigational aids, never identifiers.

## What it does

| Step | Behaviour | Result |
|---|---|---|
| Recipient resolution | `object['to']` may be a bare string, or a JSON-LD array whose first element is taken. Absent or empty leaves it `None`. | `None` → FAILURE `'Chat recipient is invalid'`, **returns False** |
| Actor lookup | `find_actor_or_create_cached(recipient_ap_id)`, then a re-query through the task session | non-local or unfound → FAILURE `'ChatMessage target is not local'`, **returns False** |
| Sender too new | `sender.created_very_recently() and user.ap_domain != 'fediseer.com'` | FAILURE `'Sender is too new'`, returns True |
| Blocked | `recipient.has_blocked_user(sender.id) or recipient.has_blocked_instance(sender.instance_id)` | FAILURE `'Sender blocked by recipient'`, returns True |
| PMs off | `accept_private_messages is None or == 0` | FAILURE `'Recipient has turned off PMs'`, returns True |
| Local only | `accept_private_messages == 1` | FAILURE `'Recipient only accepts local PMs'`, returns True |
| Untrusted | `accept_private_messages == 2 and not sender.instance.trusted` | FAILURE `'Sender from untrusted instance'`, returns True |
| Accepted | otherwise | blocked-phrase filter, then store and notify |

`User.accept_private_messages` is `db.Column(db.Integer, default=3)`, documented
in the model as *"None or 0 = do not accept, 1 = This instance, 2 = Trusted
instances, 3 = All instances"*. **The `else` branch is therefore the intended
default, not an accidental fall-through** — an earlier reading of this function
mistook it for one, and the spec records the correction so nobody repeats it.

On acceptance: `blocked_phrases()` (which reads newline-separated
`Site.blocked_phrases`) refuses on any substring match; then
`Conversation.find_existing_conversation(recipient=, sender=)` finds or creates
the conversation; then a `ChatMessage` is created, or an existing one with the
same `ap_id` is updated; then an SSE event and a `Notification`, and SUCCESS.

**The return value is a contract, not a detail.** `False` means "not handled —
caller should continue"; `True` means "handled". The Create/Update arm's
fallback path branches on it. Every path's return value is therefore in scope.

## Goal

Full statement coverage of `process_chat`, and branch coverage sufficient that
no guard survives having any one of its conjuncts dropped. Expected effect:
`app/activitypub/routes.py` moves from its measured **62.5873%** blended toward
**66%**, and the floor rises to the measured figure rounded down.

## Out of scope

`process_new_content` — the other large delegate, 87 statements at 1.1%, and the
natural next slice. The outbound ActivityPub representation endpoints
(`community_profile`, `user_profile`, `feed_profile`, the outboxes and follower
collections) keep their own future spec. The delegates `process_chat` itself
calls — `find_actor_or_create_cached`, `blocked_phrases`,
`Conversation.find_existing_conversation`, `publish_sse_event`, `html_to_text` —
are doubled or driven through real data here, not tested here.

## The defects this slice must confront

Five are visible on reading. This slice carries the same bounded fix
authorisation 5c, 5d and 5e had: **defects found are fixed test-first, each in
its own commit, separate from every test-only commit, and each proved by a
mutation that fails a named test.** Anything larger than this function is
registered, as 5d did with the sibling `Lock` arm and 5e with the preamble.

### Two unguarded reads of peer-controlled fields

`core_activity['object']['content']` is read without a membership check, in the
blocked-phrase filter and again when building the message body.
`core_activity['object']['id']` is read the same way, for the `ap_id` lookup and
for the new row. A `ChatMessage` lacking either field therefore raises
`KeyError` out of a Celery task. Both fields come straight from the peer.

Contrast the same function's careful handling of `object['to']`, which checks
membership and both plausible JSON-LD shapes — the caution is present a few
lines above and absent here.

### `recipient.is_local()` is tested twice

The whole accepted path already sits inside `if recipient and recipient.is_local():`.
The inner `if recipient.is_local():` guarding the SSE event and notification can
never be False. Dead guard, same equivalent-mutant class as D95, D96 and D103.

### Blocked-phrase filtering is skipped when content is falsy

The filter runs under `if core_activity['object']['content']:`, so an empty
string bypasses it. Harmless on its own — an empty message carries no phrase —
but it means the filter's guard and the crash above share a cause, and a fix to
one should consider the other.

### `sender.instance.trusted` is dereferenced unguarded

Reached whenever the recipient's setting is `2`. If a sender's `instance` is
`None` this raises `AttributeError`. Whether a sender reaching this point can
have a null instance is a question the tests must answer rather than assume.

### The new-account guard reads `user` where every other line reads `sender`

`sender.created_very_recently() and user.ap_domain != 'fediseer.com'` mixes the
two names. They are the same row — `sender = session.query(User).get(user.id)` —
so this is harmless today. It is recorded because the whole reason `sender`
exists is a session-identity bug the code's own comment describes, and a later
edit that made the two diverge would reintroduce it silently.

## Testing approach

Unchanged from 5a-5e, restated so no task brief must rediscover it.

**Entry.** `dispatch(activity, store_ap_json=True)` from
`tests/test_inbox_dispatch_preamble.py`, with a `ChatMessage` inner object —
the route 5e proved reaches this function. Driving it through the dispatcher
rather than calling it directly means `session` is the real task session, which
matters: `Conversation.find_existing_conversation` issues raw SQL through
`db.session`, and the function's own comment explains that a wrong-session
`recipient` caused a live bug.

**Doubling at the binding site.** `record_moderation(monkeypatch, *names)` from
`tests/test_inbox_dispatch_lock_delete.py` patches names on
`app.activitypub.routes`. Verified imported there:
`find_actor_or_create_cached`, `blocked_phrases`, `publish_sse_event`,
`html_to_text`, `shorten_string`. `Conversation`, `ChatMessage` and
`Notification` are imported as models and are better driven with real rows than
doubled.

**Never double `find_actor_or_create_cached` unconditionally.** The preamble
resolves the activity's own signed actor through it before any arm runs, so a
blanket double fails the preamble instead of reaching this function. Scope any
such double to one URL. Recorded as harness fact 17 in `tests/README.md`; two
sub-projects hit it.

**`publish_sse_event` must be doubled** on the accepted path — it reaches an
external event stream.

**Seeding.** `make_user_block(blocker, blocked)`, `make_instance_block(user, instance)`,
`make_chat_message(sender, recipient, ap_id, *, body, deleted)` and
`make_instance(domain, software)` all exist. `blocked_phrases()` reads
`Site.blocked_phrases`, so the phrase test seeds that column on the site row
`make_site()` builds. `make_community` hardcodes `user_id=1`/`instance_id=1`
against real foreign keys, so a `User` must exist before it is called — two
tasks in 5e hit that.

**Mutation discipline.** Every guard is mutation-tested with each conjunct
dropped *separately*, each killed by a distinct named test. The blocked-check
is a two-way `or`; the new-account and untrusted-instance guards are two-way
`and`s. A kill by `respx.models.AllMockedAssertionError` is an infrastructure
kill, not a behavioural one.

**No vacuous assertions.** An assertion on a column that already equals its
declared default proves nothing. `accept_private_messages` defaults to **3**,
so every test of a *refusing* setting must seed it explicitly — and a test of
the accepted path must seed 3 explicitly rather than leaning on the default.
Sub-project 5e lost a fix round to exactly this class of mistake.

**Docstrings must be true.** A claim about what another test proves is verified
before it is written; several fix rounds across 5d and 5e were spent on this.

**One pytest session at a time.** The suite assumes exclusive access to the
stack. Harness fact 18.

## New test file and factories

One new file, `tests/test_inbox_dispatch_chat.py`. The function is 74
statements with a single dominant control flow, so splitting it would
duplicate fixtures without giving a reviewer a cleaner surface.

`tests/factories.py` likely gains `make_conversation(sender, recipient)` for the
existing-conversation path — `Conversation` has a `members` relationship the
function appends both parties to, and no factory builds one. The plan confirms
the exact shape against the model before specifying it.

## Global constraints

- Defects found in `process_chat` are fixed test-first, each in its own commit,
  each proved by mutation. Anything outside the function is registered.
- Findings are numbered from **D121**, and the register's index note is updated
  in the same change that takes the numbers.
- The coverage floor rises to the measured blended figure rounded down.
- Locate every code target by content, not by the line numbers in this spec.
- The full suite must pass. Only the controller runs it, one session at a time.

## Success criteria

1. `process_chat` reaches full statement coverage.
2. No guard in it survives any one conjunct being dropped.
3. Every path's **return value** is asserted, not just its log — the Create/Update
   arm's fallback branches on it.
4. Defects found are fixed with a mutation-proved test, or registered with a
   stated reason for not fixing.
5. `coverage_floors.ini` raised for `app/activitypub/routes.py`.
6. The findings register carries every defect found, from D121.
7. Full suite green.
