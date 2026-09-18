# Sub-project 56 implementation plan: `app/chat` Group A

**Design:** `docs/superpowers/specs/2026-09-18-chat-conversations-56-design.md`
**Base:** `blentz` at `f80915b15`
**Targets:** 67 missing statements, 39 missing arcs across `chat_home`,
`new_message`, `chat_conversation` and `send_message`.

## Global Constraints

Unchanged. **One interim floor this round** on `app/chat/routes.py`, the
measured blended figure rounded down, with the checker re-run after it lands —
32 floors. `app/chat/util.py` takes a floor only if `update_message` turns out
covered to 100 incidentally; it is Group B's target, so the expected outcome is
no util floor this round.

## The exact residual

From the full-suite JSON at the delivered tree. These are the lines and arcs
the round must close, before P1-P3 add any:

| function | missing lines | missing arcs |
|---|---|---|
| `chat_home` | 21-26, 28, 33-35, 37, 39-47, 49, 51, 54-55, 57, 59-63, 65 | `22→23`, `22→28`, `23→24`, `23→25`, `33→34`, `33→39`, `34→35`, `34→37`, `41→42`, `41→43`, `43→44`, `43→49`, `45→46`, `45→51`, `46→45`, `46→47`, `54→55`, `54→57` |
| `new_message` | 75, 77-78, 80-84, 86-97, 99 | `77→78`, `77→80`, `80→81`, `80→82`, `83→84`, `83→88`, `86→87`, `86→88`, `90→91`, `90→99` |
| `chat_conversation` | 243-248, 250, 252 | `244→245`, `244→252`, `246→247`, `246→250`, `247→246`, `247→248` |
| `send_message` | 40-41, 43, 45, 65-66, 73 | `24→40`, `40→41`, `40→43`, `65→66`, `65→73` |

`send_message`'s local arm is already covered incidentally; **only the remote
arm is missing**, which is why Task 4 is federation-shaped and nothing else.

## Harness facts for these modules

- **`app.chat.routes.render_template`** is patched for every test that reaches a
  render (fact 292). `chat_home`'s template reads `conversation.member_names`
  and the message list, so an unpatched render is a template debugging session,
  not a route test.
- **POSTs need a real CSRF token** (fact 293): mint it in
  `app.test_request_context()` with `generate_csrf()`, copy `session['csrf_token']`
  into the client's session, and send the token in the form data.
- **`app.chat.util.publish_sse_event` must be patched** in every test that sends
  to a local recipient — it reaches Redis.
- **`app.chat.util.send_post_request` must be patched** for the remote arm.
  `respx` is active, so an unpatched call fails loudly rather than reaching the
  network.
- **`new_message` is decorated with `trustworthy_account_required`**
  (`app/utils.py:1903-1911`), which redirects to `/auth/not_trustworthy` unless
  `current_user.trustworthy()` — false for an account created within 7 days with
  reputation under 100 (`app/models.py:1286-1291`). A fixture user is created
  now, so **every `new_message` test must age its sender**:
  `user.created = utcnow() - timedelta(days=30)`. The scoping probe hit this
  exact redirect before it aged the user.
- **`can_send_pm_to` (`app/models.py:1654-1664`)** refuses a sender created
  within a day, with reputation at or below -10, banned, unverified, or with
  `can_send_pm` false — unless either party is admin or staff. `make_user`
  leaves `verified=True` and `banned=False`, so aging the account is usually the
  only change a happy-path test needs.
- **`make_conversation(sender, recipient)`** writes both `conversation_member`
  rows, which is what `find_existing_conversation`'s join needs;
  **`make_chat_message(sender, recipient, ap_id)`** leaves `conversation_id`
  NULL by design, so a test that needs a message INSIDE a conversation sets it.
- **`send_message`'s remote arm branches on `recipient.instance.software`**:
  `lemmy`/`mbin` give `ap_type` `ChatMessage`, anything else `Note`; and
  anything that is neither `lemmy` nor `piefed` also gets a `tag` mention array.
  Three softwares are needed to cover both branches plus the `piefed` exclusion.
- A remote recipient needs `ap_inbox_url` and `ap_public_url`, which `make_user`
  supplies for `local=False`; the **sender** needs `private_key`, so it is built
  with `with_keys=True` or given a string key.

## File Structure

| file | responsibility |
|---|---|
| `tests/test_chat_routes.py` | **Create.** `chat_home`, `new_message`, `chat_conversation`. |
| `tests/test_chat_util.py` | **Create.** `send_message`'s remote arm. |
| `app/chat/routes.py` | **Modify** three times: P1's guard, P2's id comparison, P3's redirect. |
| `coverage_floors.ini` | **Modify.** One new line, `app/chat/routes.py`. |
| `tests/README.md`, the register | Facts from **305**, findings from **D741**. |
| `tests/test_zz_chat_probe.py` | **Delete** in Task 1; it is scoping scaffolding. |

---

## Task 1: P1, P2 and P3

All three repairs are in `app/chat/routes.py`, and each lands with its pin
inverted — the test written first, run against the unrepaired tree, and seen to
fail for the stated reason.

**Pins** (the probe already established each; the tests restate them as
assertions):

- P1: a verified non-member POSTs to `/chat/<id>` and the message lands —
  assert the `ChatMessage` row exists with the outsider's `sender_id`, not
  merely that the response is a 302.
- P2: two users already holding a conversation go through `/chat/<to>/new`, and
  a SECOND `Conversation` row appears; assert the count AND that the GET
  rendered the form rather than redirecting.
- P3: a POST to `/chat` with no conversation id raises
  `AttributeError: 'NoneType' object has no attribute 'id'`.

**Fixes:**

- P1, before `send_message` at `:25`: refuse a non-member with the read path's
  `abort(400)`. The guard is `conversation.is_member(current_user)` **alone** —
  no admin arm, per the design. The conversation has to be loaded to ask, so the
  POST path gains a `Conversation.query.get_or_404(conversation_id)` above it,
  which also means an unknown id is a 404 on both methods rather than a 404 on
  GET and a crash on POST.
- P2 at `:83-86`: take ids from the rows —
  `member_ids = {row.user_id for row in members}` — and test against that set.
  `chat_home:51`'s use of the same query is `len(members)` and stays as it is.
- P3, at the top of the POST path: `conversation_id is None` redirects to
  `chat.empty`, the same destination `:37` uses for a user with no conversations.

**Inversions:**

- P1: the outsider's POST is a 400 AND no `ChatMessage` row exists for that
  conversation; a member's POST in the same test file still succeeds, so a
  repair that refused everyone would fail. Assert both halves.
- P2: the GET redirects to the EXISTING conversation id, and
  `Conversation.query.count()` is still 1 afterwards. Both halves, since a
  repair that stopped creating rows without redirecting would leave the user on
  a dead form and pass a count-only test.
- P3: the POST is a 302 to `/chat/empty`, and no `ChatMessage` row was written.

**Close the task by deleting `tests/test_zz_chat_probe.py`** and committing the
three repairs with their tests.

## Task 2: `chat_home`

- The POST arm, all four ways it can go: the member who sends (the happy path,
  through to the redirect with `_anchor='message'`); the banned sender, the
  unverified sender and the `can_send_pm=False` sender, each redirected to
  `chat.denied` — three separate rows, because the guard at `:23` is a
  three-way `or` and one row would leave two operands load-bearing for nothing.
- The GET arm with `conversation_id is None`: with conversations, redirecting to
  the most recently updated one (two conversations with different `updated_at`,
  so the `order_by(desc(...))` is load-bearing); and with none, redirecting to
  `chat.empty`.
- The GET arm with an id: the member's read, asserting that the caller's own
  incoming messages are marked read and the other party's are NOT — the `:46`
  `if message.recipient_id == current_user.id` is exactly that discrimination;
  the admin non-member read, which `:41` allows; and the non-member, non-admin
  read, which is the `abort(400)`.
- The `alone` flag: a conversation with one joined member gives `alone` True and
  a two-member conversation gives False. Assert it out of the render call's
  kwargs, which is what the patched `render_template` makes visible.
- `:43`'s `if conversations:` false arm — a user whose only membership row has
  `joined = False` reaches the id branch with an empty `conversations` list and
  gets `messages = []`. This is the `43→49` arc and it needs a conversation the
  user is a member of (so `:41` passes) while `joined` is false (so the query at
  `:28` returns nothing).
- The notification sweep at `:59-62`: a notification whose url matches
  `/chat/<id>%` for this user is marked read and `unread_notifications` is
  recomputed; a notification for ANOTHER conversation is left alone, which is
  what keeps the `LIKE` pattern load-bearing.

## Task 3: `new_message` and `chat_conversation`

`new_message` — every test ages the sender past `trustworthy()`:

- `can_send_pm_to` false (a sender created within the day) redirects to
  `chat.denied`; the `77→80` arm is the sender who may.
- The recipient who has blocked the sender, and the sender who has blocked the
  recipient, each redirect to `chat.blocked` — two rows, since `:80` is an `or`.
- No existing conversation: the GET renders the form; the POST creates the
  conversation, appends both members, sends and redirects to the new id.
- An existing conversation: the GET redirects to it (P2's inversion, restated
  here as the covering test for `86→87`).
- An existing conversation the recipient has LEFT — `find_existing_conversation`
  still finds the row because it joins on membership regardless of `joined`, and
  the members set still contains both ids, so this redirects too. Record what it
  does rather than asserting what it should do; if it proves to be a defect,
  register it for Group B rather than repairing it here.
- A `to` that is not a user: 404 from `get_or_404`.

`chat_conversation`:

- The member's refresh renders `chat/_messages.html` and marks the caller's
  incoming messages read, leaving the other party's alone.
- The admin non-member gets the same render (`244→245` via the `is_admin` arm).
- The non-member gets `''` — assert the body is empty and the status is 200,
  which is the `244→252` arc and the thing R1 says the other two routes fail to
  do.
- An unknown id is a 404.

## Task 4: `send_message`'s remote arm

Only the remote arm is missing. Every row builds a conversation with one local
sender and one REMOTE recipient, patches `app.chat.util.send_post_request`, and
reads the call's arguments:

- A `lemmy` recipient: `ap_type` is `ChatMessage` and there is **no** `tag`
  (the `!= lemmy and != piefed` guard excludes it).
- An `mbin` recipient: `ap_type` is `ChatMessage` and there **is** a `tag`.
- A `piefed` recipient: `ap_type` is `Note` and there is **no** `tag`.
- A `mastodon` recipient: `ap_type` is `Note` and there **is** a `tag`.

Four rows, which is what it takes to make both `:40` and `:65` load-bearing
independently — two rows would leave one operand of each guard untested.

Assert on the delivery arguments too: the inbox url is the recipient's, the key
is the sender's private key, and the key id is the sender's public url with
`#main-key`. Also assert `inReplyTo` comes from `conversation.last_ap_id(
recipient.id)` — seed one earlier message from the recipient with an `ap_id`
and assert it appears, since an empty string is what an unseeded conversation
gives and that would pass a weaker assertion.

## Task 5: floors, suite, mutation, register

- The full suite in one `&&` chain, coverage measured with `--cov=app`.
- `app/chat/routes.py`'s floor added at the measured figure rounded down, and
  the checker re-run with it in place.
- A mutation pass scoped to the four target functions, per D602: every mutant
  applied singly, restored, and the restoration proved.
- Register from **D741**: P1, P2, P3, the `read = True` ordering that is not a
  defect, and R1-R4 from the design.
- Facts from **305**. At least these three, which this round paid for:
  - **`.all()` on a raw `db.session.execute` returns `Row` tuples**, so
    `some_id in rows` is always False — a guard that runs every request and can
    never fire.
  - **A Flask view that falls off the end returns `None`**, which is a
    `TypeError` and a 500, not a refusal. `chat_conversation` returns `''` in
    the same position; the other two do not.
  - **`trustworthy_account_required` fails a freshly built fixture user**, so
    any test of a route carrying it must age the account past 7 days or give it
    reputation 100.

## Self-review

1. `git diff --numstat <base> HEAD -- app/` names exactly `app/chat/routes.py`.
2. All three pins inverted, each run against the unrepaired tree and seen to
   fail for the stated reason.
3. Citations checked at the commit that writes them.
4. The floor is the measured figure rounded down, and the checker passes.
5. No probe file, no `# MUT` left behind, `git status` clean but for the
   intended files.
