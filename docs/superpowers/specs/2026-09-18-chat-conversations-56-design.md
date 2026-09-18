# Sub-project 56: `app/chat` Group A — the conversation surface

**Status:** design approved, ready for an implementation plan
**Branch:** `blentz`, at `f694a7dd4`
**Predecessor:** sub-project 55, which closed `app/feed/util.py` and
`app/feed/forms.py` at 100.0 and completed the `app/feed` package — 31 floors.

## Goal

Cover the read and write surface of the chat package — the two routes a user
reaches to read a conversation and to start one, the htmx refresh that reloads
a conversation, and the helper that actually sends a message — repair the three
defects probed while scoping them, and set an interim floor on `app/chat/routes.py`
plus a floor on `app/chat/util.py` if Group A's share closes it.

Group B (sub-project 57) takes the rest of the package — `chat_report`,
`update_message`, `block_instance`, `chat_delete`, `chat_leave`,
`ban_from_mod`, `chat_options`, the three template stubs and
`app/chat/forms.py` — and closes `app/chat`.

## Targets

From the full-suite JSON at the delivered tree (`coverage.json`, run of
2026-09-18T19:42, `app/chat/routes.py` 22.3%, `app/chat/util.py` 49.3%):

| function | where | missing statements | missing arcs | total |
|---|---|---|---|---|
| `chat_home` | `routes.py:20-68` | 31 | 18 | 49 |
| `new_message` | `routes.py:74-102` | 21 | 10 | 31 |
| `chat_conversation` | `routes.py:241-252` | 8 | 6 | 14 |
| `send_message` | `util.py:12-76` | 7 | 5 | 12 |
| **total** | | **67** | **39** | **106** |

Inside the campaign's 110-130 band. The package's other 114 are Group B's.

## THE THREE PRODUCTION CHANGES

### P1 — `chat_home`'s POST path writes into ANY conversation

```python
if form.validate_on_submit():                       # app/chat/routes.py:22
    if current_user.banned or not current_user.verified or not current_user.can_send_pm:
        return redirect(url_for('chat.denied'))
    send_message(form.message.data, conversation_id)     # :25
```

The route's GET path checks membership at `:41` —
`if not current_user.is_admin() and not conversation.is_member(current_user): abort(400)`
— and the POST path, which runs first and returns before `:28` is ever reached,
checks nothing but the sender's own standing. A verified user who is not a
member posts into any conversation by id.

**Probe** — carol (id 4) posting into alice↔bob's conversation:

```
PROBE p1 status: 302 /chat/1#message
PROBE p1 messages in foreign conversation: [(4, 'i am not a member')]
```

The message is committed, is given an `ap_id` under this server's `SERVER_URL`,
and `send_message` then notifies every other member and federates to the remote
ones — so the intrusion arrives in both members' notifications and, for a
remote member, on another instance.

**This is D725's shape on the write side.** D725 served a private feed list to
any logged-in caller; this accepts a private message from one.

**Fix: require membership, admins included.** The guard is
`conversation.is_member(current_user)` alone, deliberately stricter than the
read path's `is_admin() or is_member()`. An admin reads a foreign conversation
for moderation, which `:41` allows and this design leaves alone; an admin
writing into one produces a message attributed to the admin inside someone
else's thread, which no moderation flow asks for and which today exists only
because nothing was checked at all. A non-member POST takes the same `abort(400)`
the read path takes, so the two paths refuse the same way.

### P2 — `new_message`'s duplicate-conversation guard never fires

```python
members = db.session.execute(text("SELECT user_id FROM conversation_member ...")).all()
if current_user.id in members and recipient.id in members:      # app/chat/routes.py:86
    return redirect(url_for('chat.chat_home', conversation_id=existing_conversation.id, ...))
```

`.all()` returns a list of `Row` objects, so the membership test compares an
`int` against `(2,)` and is **always False**. `find_existing_conversation`
does its work and the redirect it exists to enable never happens.

**Probe** — alice and bob already hold conversation 1, alice opens
`/chat/<bob>/new` and sends:

```
PROBE p2 rows: [(2,), (3,)] alice.id in rows: False
PROBE p2 status: 200 loc: None
PROBE p2 conversations now: 1
PROBE p2 post status: 302 loc: /chat/2#message
PROBE p2 conversations after post: 2
```

A second conversation between the same pair, with the first thread's history
stranded behind it.

**This is sub-project 45's "guard that does not guard" shape** — a condition
that is syntactically fine, runs on every request, and can never be true.

**Fix: compare ids to ids** — `scalars().all()`, or a set comprehension over
`row.user_id`. The inverted test asserts the redirect names the EXISTING
conversation id and that no second `Conversation` row appears, since a repair
that merely stopped creating rows would pass a count-only assertion while
leaving the user on a dead form.

`chat_home:51` runs the same query and takes `len(members)` from it, which is
correct as written; this fix does not touch it.

### P3 — a POST to `/chat` with no conversation id is a 500

`chat_home` is registered on both `/chat` and `/chat/<int:conversation_id>`, and
the POST path passes `conversation_id` — `None` on the first route — straight
into `send_message`, where `Conversation.query.get(None)` returns `None` and
`conversation.id` raises.

**Probe:**

```
PROBE p3 exception: AttributeError 'NoneType' object has no attribute 'id'
```

**Fix in the route**, not in the helper: `chat_home` redirects to `chat.empty`
when the POST arrives without a conversation id. `send_message`'s contract is
shared with callers this round is not scoping, and the route already knows what
a conversation-less chat page means — it redirects there at `:37` on the GET
path for exactly this state.

## Checked while scoping and NOT a defect

`chat_home:40` assigns `conversation.read = True` before the membership check at
`:41`, so an outsider's request mutates the object before it is refused. Nothing
commits on that path and the session is discarded:

```
PROBE p5 status: 400 conversation.read now: False
```

Recorded so the ordering is not re-litigated, and so a future commit added
between those two lines is known to be a defect.

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `routes.py:141-144`, `:196-236` | **`chat_options` and `chat_report` fall off the end returning `None` when the guard fails**, which Flask turns into `TypeError ... did not return a valid response` — a 500 where a 403 belongs. `chat_conversation:252` returns `''` at the same place, so the file disagrees with itself. Probed: both raise. | Both functions are Group B's targets; repairing them there keeps the fix and its coverage in one round. |
| R2 | `routes.py:58` | **The notification-read update is built by f-string interpolation** of `conversation_id` and `current_user.id` into SQL, while the query four lines above it binds parameters. Both values arrive through `<int:>` converters or the session, so nothing is injectable today. | The shape, not a live defect; changing it is a one-line edit that belongs with whoever next touches the query. |
| R3 | `routes.py:125-136` | **`ban_from_mod` builds `user_link` from `current_user.user_name` rather than from the `user_id` it was given**, so the past-bans list is filtered by the viewer, and the route carries no moderator check at all. | Group B's target. Registered now so 57 starts with it probed. |
| R4 | `util.py:12` | **`send_message`'s `user: User = current_user` default is bound at import**, so the parameter is a `LocalProxy` rather than a `User` for every web caller. It works, and the type annotation is a lie. | Not a defect. Registered so the annotation is not trusted by a future caller outside a request context. |

## Success criteria

- `chat_home`, `new_message`, `chat_conversation` and `send_message` at `[]`/`[]`
  on the **full-suite** run, except arcs declared unreachable with a named cause
  and a proof.
- `app/chat/routes.py` takes an **interim floor** at the measured figure rounded
  down; `app/chat/util.py` takes a floor only if `update_message` is already
  covered incidentally to 100, otherwise it waits for 57. Floors only rise.
- The three repairs land with their pins inverted — each test run against the
  unrepaired tree and seen to fail — and `git diff --numstat` names exactly
  `app/chat/routes.py`.
- Suite green; floors checked with `&&` in one chain; a mutation pass scoped to
  the four functions, per D602.
- Findings registered from **D741**; `tests/README.md` facts from **305**.
- `tests/test_zz_chat_probe.py` deleted before delivery; it is scoping scaffolding.
