# Sub-project 57: `app/chat` Group B — closing the package

**Status:** design approved, ready for an implementation plan
**Branch:** `blentz`, at `847a2fc22`
**Predecessor:** sub-project 56, which closed `app/chat`'s conversation surface
and floored `app/chat/routes.py` at 65 — 32 floors.

## Goal

Cover everything the chat package has left — the report form, the options,
delete and leave routes, the instance block, the moderator ban view, the three
template stubs, `update_message` and `app/chat/forms.py` — repair the four
defects probed while scoping them, and **take a floor on each of the three
modules, which closes `app/chat`.**

## Targets

From the full-suite JSON at the delivered tree (`app/chat/routes.py` 65.957,
`app/chat/util.py` 67.164, `app/chat/forms.py` 55.556):

| function | where | missing statements | missing arcs | total |
|---|---|---|---|---|
| `chat_report` | `routes.py:208-245` | 21 | 12 | 33 |
| `update_message` | `util.py:79-128` | 16 | 6 | 22 |
| `block_instance` | `routes.py:188-202` | 10 | 4 | 14 |
| `ReportConversationForm.reasons_to_string` | `forms.py:33-39` | 6 | 6 | 12 |
| `chat_delete` | `routes.py:161-167` | 7 | 2 | 9 |
| `chat_leave` | `routes.py:173-182` | 6 | 2 | 8 |
| `ban_from_mod` | `routes.py:137-147` | 6 | 2 | 8 |
| `chat_options` | `routes.py:153-155` | 3 | 2 | 5 |
| `denied`, `blocked`, `empty` | `routes.py:119`, `:125`, `:131` | 3 | 0 | 3 |
| **total** | | **78** | **36** | **114** |

Inside the campaign's 110-130 band, and it is what the package has left.

## THE FOUR PRODUCTION CHANGES

### P1 — an edited private message federates as a NEW message

```python
"id": f"{current_app.config['SERVER_URL']}/activities/update/{gibberish(15)}",
...
"type": "Create"                                    # app/chat/util.py:117
```

**Probe:**

```
PROBE b3 activity id: https://test.piefed.local/activities/update/odDGXrwdMmJXBH4
PROBE b3 activity type: Create
PROBE b3 object type: Note
```

The activity id says `update`, the wrapper says `Create`, and a remote instance
receiving it has no way to know the sender edited anything — the peer shows a
second message. **The codebase settles this itself**: `app/shared/tasks/notes.py:187`
and `app/shared/tasks/pages.py:252` both read
`type = 'Create' if not edit else 'Update'`. `update_message` is only ever
called on an edit (`app/api/alpha/utils/private_message.py:193` is its one
caller), so the type is unconditionally wrong.

**Fix:** `"type": "Update"`. The inverted test asserts the wrapper type AND
that the object still carries the software-appropriate inner type, since
changing both would be a different bug.

### P2 — `ban_from_mod` shows the VIEWER's ban history, to anyone

```python
user_link = 'u/' + current_user.user_name          # app/chat/routes.py:139
past_bans = ModLog.query.filter(ModLog.community_id == community_id,
                                ModLog.link == user_link, ...)
```

The route takes `user_id` in its url and then filters the mod log by the
**caller's** name. It also carries no authorization beyond `login_required`.

**Probe** — carol, who moderates nothing, opening the page for bob:

```
PROBE b2 status: 200
PROBE b2 past_bans links: ['u/carol'] (url named bob, viewer is carol)
```

**Fix, both halves.** The link is built from the user the url names, and the
route is gated to that community's moderators, its owner and admins, following
`app/community/routes.py:1063`'s shape:
`if not (community.is_moderator() or community.is_owner() or current_user.is_admin()): abort(401)`.
The community has to be loaded to ask, which it currently never is, so the
route also gains a `get_or_404` and an unknown community stops being a page of
empty results.

The inverted test asserts the page names the RIGHT user's bans — a repair that
returned nothing would pass a test that only checked carol's rows were gone.

### P3 — `chat_options` and `chat_report` are a 500 for a non-member

Both guard with `if current_user.is_admin() or conversation.is_member(current_user):`
and have no `else`, so a refused caller falls off the end and Flask raises
`TypeError: The view function ... did not return a valid response`.
`chat_conversation`, three routes below, returns `''` in the same position — the
file disagrees with itself about what a refusal is. Registered as **D747** and
fact 306 in sub-project 56; repaired here, where both functions are targets.

**Fix:** `abort(400)`, matching `chat_home`, so the blueprint has one refusal
shape.

### P4 — `block_instance` is a 500 when htmx sends no current url

```python
if request.headers.get('HX-Request'):
    curr_url = request.headers.get('HX-Current-Url')
    if "/chat/" in curr_url:                        # app/chat/routes.py:195
```

**Probe:**

```
PROBE b1 exception: TypeError argument of type 'NoneType' is not iterable
```

`HX-Current-Url` is optional — htmx omits it when the page has no url to report
— and the block itself has already happened by then, so the user's instance
block is applied and the response is a 500.

**Fix:** an absent header takes the same branch as a chat url, `HX-Redirect` to
`main.index`. That is the branch that exists for "you cannot stay where you
are", and a caller who did not say where they are cannot be sent back there.

## Checked while scoping and NOT a defect

**The form-less POST routes are not CSRF-exposed.** `chat_delete`, `chat_leave`
and `block_instance` carry no form, and there is no `CSRFProtect` anywhere in
`app/` — but `login_required` (`app/utils.py:1968-1980`) validates the token
itself on every POST. A probe without a token raises
`wtforms.validators.ValidationError: The CSRF token is missing.` Recorded
because the absence of `CSRFProtect` invites exactly the wrong conclusion.

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `app/utils.py:1979` | **A missing or expired CSRF token raises `wtforms ValidationError`, which nothing handles — a 500 where a 400 belongs.** Every POST route in the application is behind this, not only chat's. | Cross-cutting: one error handler, one decision, and it belongs to whoever owns the app factory. |
| R2 | `app/chat/util.py:81` | **`update_message` raises `NoResultFound` when `recipient_id` is NULL** — `User.query.filter_by(id=reply.recipient_id).one()`. Probed: `PROBE b5 exception: NoResultFound No row was found when one was required`. ChatMessage rows with a NULL recipient do exist in this schema (the column is nullable and the Undo/Delete restore path reads such rows). | Whether an orphaned message can reach the edit path needs the API's own scoping, which is a later sub-project. Registered with the probe attached. |
| R3 | `app/chat/routes.py:161-167`, `:173-182` | **`chat_delete` and `chat_leave` are a silent no-op for a stranger** — `PROBE b6 delete status: 302 conversation survives: True`, with no flash. The user is told the same thing whether the delete happened or not. | The refusal's user-visible text is a product choice, and unlike P3 nothing crashes. Covered as behaviour. |
| R4 | `app/chat/routes.py:226-234` | **`chat_report`'s `already_notified` set is created, tested and never added to**, so the guard it forms can never be false. Harmless while `Site.admins()` returns distinct rows; it is D742's shape once more — a guard that cannot fire. | A dead guard, not a defect. Registered so the next reader does not assume it does work. |
| R5 | `app/chat/routes.py:213-221` | **`chat_report` hardcodes `source_instance_id=1` and its `report_remote` branch is a bare `...`**, so the checkbox the form offers does nothing. | The federation of reports is unbuilt, not broken; the form's promise is the thing to fix and that is a feature. |

## Success criteria

- Every function above at `[]`/`[]` on the **full-suite** run, except arcs
  declared unreachable with a named cause and a proof.
- **Three floors** — `app/chat/routes.py` raised from 65, `app/chat/util.py`
  and `app/chat/forms.py` new — which closes `app/chat`. 34 floors.
- The four repairs land with pins inverted; `git diff --numstat` names exactly
  `app/chat/routes.py` and `app/chat/util.py`.
- Suite green; floors checked with `&&`; a mutation pass over the nine
  functions, per D602.
- Findings registered from **D753**; `tests/README.md` facts from **308**.
- `tests/test_zz_chatb_probe.py` deleted before delivery.
