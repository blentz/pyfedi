# Sub-project 57 implementation plan: `app/chat` Group B

**Design:** `docs/superpowers/specs/2026-09-18-chat-package-57-design.md`
**Base:** `blentz` at `94aa45858`
**Targets:** 78 missing statements, 36 missing arcs across nine functions in
three modules.

## Global Constraints

Unchanged. **Three floors this round** — `app/chat/routes.py` raised from 65,
`app/chat/util.py` and `app/chat/forms.py` new, each the measured blended
figure rounded down — with the checker re-run after they land: 34 floors, and
`app/chat` closed.

## Harness facts carried from sub-project 56

- `app.chat.routes.render_template` is patched for anything that renders;
  `app.chat.util.publish_sse_event` and `app.chat.util.send_post_request` for
  anything that sends.
- POSTs need a real CSRF token, and here that is not optional: `login_required`
  validates it itself (`app/utils.py:1979`), and a POST without one raises
  rather than returning a 400.
- `_seed`, `login`, `csrf`, `_make_admin`, `_message` and `_aged` already exist
  in `tests/test_chat_routes.py`; Group B's route tests go in the same file and
  reuse them rather than growing a second set.
- `make_community(name, host)` takes NO instance argument — the scoping probe
  lost a run to `can't adapt type 'Instance'`.
- `Site.admins()` is what `chat_report` notifies; an admin in this suite is a
  role literally named `Admin` (or user id 1, which `_seed` burns).

## File Structure

| file | responsibility |
|---|---|
| `tests/test_chat_routes.py` | **Modify.** Group B's route tests, appended. |
| `tests/test_chat_util.py` | **Modify.** `update_message`, both arms. |
| `tests/test_chat_forms.py` | **Create.** `ReportConversationForm.reasons_to_string`. |
| `app/chat/routes.py` | **Modify** three times: P2, P3, P4. |
| `app/chat/util.py` | **Modify** once: P1's activity type. |
| `coverage_floors.ini` | **Modify.** One line raised, two added. |
| `tests/README.md`, the register | Facts from **308**, findings from **D753**. |
| `tests/test_zz_chatb_probe.py` | **Delete** in Task 1. |

---

## Task 1: P1, P2, P3 and P4

**Pins** (each already probed; the tests restate them):

- P1: `update_message` delivers a payload whose `type` is `Create` while its
  `id` is an `/activities/update/` url.
- P2: `ban_from_mod` for bob, viewed by carol, renders carol's own ban rows —
  and carol, who moderates nothing, gets a 200.
- P3: `chat_options` and `chat_report` raise
  `TypeError ... did not return a valid response` for a non-member.
- P4: `block_instance` with `HX-Request` and no `HX-Current-Url` raises
  `TypeError argument of type 'NoneType' is not iterable`.

**Fixes:**

- P1 at `util.py:117`: `"type": "Update"`.
- P2 at `routes.py:136-140`: load the community with `get_or_404`, gate on
  `community.is_moderator() or community.is_owner() or current_user.is_admin()`
  with `abort(401)` otherwise (the shape at `app/community/routes.py:1063`),
  and build `user_link` from the user the url names, which needs that user
  loaded with `get_or_404` too.
- P3 at `routes.py:154` and `:210`: `abort(400)` in the else, matching
  `chat_home`.
- P4 at `routes.py:193-198`: an absent `HX-Current-Url` takes the chat-url
  branch — `HX-Redirect` to `main.index`.

**Inversions:**

- P1: the wrapper is `Update` AND the object still carries the
  software-appropriate inner type (`ChatMessage` for lemmy, `Note` for piefed),
  since changing both would be a different bug.
- P2: three halves. A moderator sees the rows belonging to the user the URL
  NAMES; a non-moderator gets 401; and the moderator's own rows for a different
  user do NOT appear, which is what proves the filter moved rather than broke.
- P3: both routes return 400 for a stranger, and a member still gets the render
  — otherwise a repair that refused everyone would pass.
- P4: the header-less request gets `HX-Redirect: /` and a 200, and the block is
  still applied; a request WITH a non-chat current url still gets that url back,
  which keeps the true arm load-bearing.

**Close the task by deleting `tests/test_zz_chatb_probe.py`.**

## Task 2: `chat_report`

- The GET as a member: `report_remote` is pre-ticked (`routes.py:243-244`), and
  the render names the conversation.
- The POST as a member: a `Report` row with `type` `REPORT_TYPE_MESSAGE`, the
  reporter, the conversation, and `reasons` as the form's joined string; a
  `Notification` for EACH admin, with each admin's `unread_notifications`
  incremented; and the redirect back to the conversation.
- Two admins, so the notification loop runs more than once and the per-admin
  counter is visibly per-admin.
- A form that fails validation (a description over 256 characters) re-renders
  rather than writing a report — the `validate_on_submit` false arm on a POST,
  which is a different arc from the GET.
- `report_remote` ticked: the branch runs and writes nothing extra, which is
  what R5 says it does. Assert the report count is unchanged rather than
  asserting a federation that does not exist.
- The stranger's 400 (P3's inversion, restated here as the covering row).

## Task 3: `chat_options`, `chat_delete`, `chat_leave`, `ban_from_mod`, the stubs

- `chat_options`: the member's render, the admin's render, the stranger's 400,
  the unknown id's 404.
- `chat_delete`: a member deletes, and the conversation AND its messages AND
  any `Report` rows naming it are gone — the `Report.query.filter(...).delete()`
  at `:163` needs a report to delete or it proves nothing. An admin may delete.
  A stranger's POST leaves the conversation standing (R3's behaviour), and the
  flash is asserted only on the path that deletes.
- `chat_leave`: the member's row flips to `joined = False` while the OTHER
  member's row is untouched; a conversation whose last local member leaves is
  deleted by `delete_if_abandoned`; one that still holds a local member is not;
  and a stranger's POST changes nothing.
- `ban_from_mod`: a moderator's view with an active ban, where `past_bans` is
  offset by one and the most recent row is therefore skipped; the same without
  an active ban, where it is not; a past-bans list that contains an `unban_user`
  action as well as a `ban_user` one, which is what keeps the `or_` at `:141`
  load-bearing; and P2's two authorization rows.
- `denied`, `blocked`, `empty`: one row each, asserting the template name.

## Task 4: `update_message` and `app/chat/forms.py`

`update_message`, both arms — the local arm is entirely uncovered, unlike
`send_message`'s:

- Local recipient: a `Notification` titled `Updated message from ...`, the
  recipient's `unread_notifications` incremented, and the url naming the
  conversation and the message.
- Remote recipient, the same four-software matrix `send_message`'s tests use:
  lemmy `ChatMessage` no tag, mbin `ChatMessage` tag, piefed `Note` no tag,
  mastodon `Note` tag.
- The delivery arguments, and `published` and `updated` taken from
  `created_at` and `edited_at` rather than from now.
- P1's inversion lives here.

`ReportConversationForm.reasons_to_string`:

- One reason, several reasons in the order the form lists them, an id that
  matches nothing (skipped), and the 255-character truncation — the last needs
  enough reasons selected to exceed it, which the form's own choices can do.
- The nested loop at `forms.py:35-38` is the whole function; a single-reason
  test leaves the inner loop's continue untested.

## Task 5: floors, suite, mutation, register

- The full suite in one `&&` chain, coverage measured with `--cov=app`.
- Three floors: `app/chat/routes.py` raised, `app/chat/util.py` and
  `app/chat/forms.py` added, each the measured figure rounded down; the checker
  re-run with all three in place.
- A mutation pass scoped to the nine functions, per D602, using the harness at
  `scratchpad/mutate_56.py` with a new mutant table.
- Register from **D753**: P1-P4, R1-R5, and the CSRF finding that was checked
  and refuted. Facts from **308**.

## Self-review

1. `git diff --numstat <base> HEAD -- app/` names exactly `app/chat/routes.py`
   and `app/chat/util.py`.
2. All four pins inverted and seen to fail on the unrepaired tree.
3. Citations checked at the commit that writes them.
4. All three floors are measured figures rounded down, and the checker passes.
5. No probe file, no `# MUT` left behind, `git status` clean but for the
   intended files.
