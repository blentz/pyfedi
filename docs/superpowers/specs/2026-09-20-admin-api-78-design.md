# Sub-project 78: the admin API and the error handler that answers for it

**Status:** design approved, ready to implement
**Branch:** `blentz`, at `50df198e3`
**Predecessor:** sub-project 77, which closed the passkey pair — 61 floors.

## Why this target

The standing priority changed: **hosted-site security is now highest, and
`app/cli.py` and `app/nntp/*` are lowest.** Ranking every unfloored module by
the density of authorization constructs it contains — `is_admin(`, `is_staff(`,
`permission_required`, `user_access(`, `authorise_api_user`, `abort(401|403)`,
`banned` — puts `app/admin/routes.py` first (160 constructs, 1400 missed
statements) and `app/community/routes.py` second (143, 1441).

Both are ten-round jobs. This round takes the same surface where it is **small
and complete**: the admin API, which is what a remote caller can reach, plus the
handler that turns every failure in it into a response.

| module | before | gaps | what it is |
|---|---|---|---|
| `app/api/alpha/utils/admin.py` | 11.429% | 62 | registration approval and denial |
| `app/api/alpha/__init__.py` | 50.000% | 38 | the API's shared error handler |

`app/admin/routes.py` is the next target and will be split like
`app/tag/routes.py` was.

## What this code does, and why it matters

`put_registration_approve` **deletes a user account** on the denial path —
`new_user.delete_dependencies()` then `db.session.delete(new_user)` — and
`get_registration_list` exposes every pending registration, which carries the
applicant's answer to the signup question. Both are behind
`user_access("approve registrations", user.id)`.

The rows here are therefore about **who can reach them**, not only what they do
when reached: an authorization guard that does not guard is this campaign's most
repeated finding (sub-projects 45, 73, 77), and the two most recent instances
were both guards that could never fire.

## No production change expected

Both modules read correctly. Three shapes are **registered** below, one of them
an information-disclosure question that belongs to whoever owns the API's
contract.

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| **R1** | `app/api/alpha/__init__.py:108-114` | **EVERY UNHANDLED EXCEPTION'S MESSAGE IS ECHOED TO THE CALLER.** The final `else` answers `{"code": 400, "message": str(e), "status": "Bad Request"}` for anything not matched above, so an internal failure's text — which may name a table, a column, a constraint or a file path — reaches an unauthenticated client. The branch is deliberate for the application's own raised strings (`'incorrect_login'`, `"Insufficient permissions to manage registrations"`), and those two plus `'No object found.'` are specifically excluded from logging, which shows the author was thinking about the distinction. | Changing what the API tells a caller about an internal error is a contract decision, and the clients consuming these messages are out of scope. Registered with the behaviour pinned in both directions — an application message and an internal one. |
| R2 | `app/api/alpha/__init__.py:108-114` | **AN AUTHORIZATION FAILURE ANSWERS 400, NOT 403.** `user_access(...)` failing raises a plain `Exception`, which lands in the same `else` as a malformed request, so a client cannot distinguish "you may not" from "you asked wrongly", and neither can monitoring. | The status codes are the API's published contract. |
| R3 | `app/api/alpha/utils/admin.py:16`, `:30` | **`limit` is taken from the caller unchecked** and passed straight to `paginate(per_page=limit)`, so a request may ask for an arbitrarily large page of registrations. | A rate-limiting and pagination-policy decision that applies across the whole alpha API, not just here. |

## Success criteria

- Both modules at `[]`/`[]` on the **full-suite** run; two floors — 63.
- Every authorization guard in scope exercised **from both sides**: the
  permitted caller and the refused one, with the refusal asserted on the side
  effect as well as the response, since a guard that returns the right error
  after doing the work is the shape sub-project 45 was named for.
- No `Model.query.get(...)` in the new rows.
- Suite green; floors checked with `&&`; a mutation pass over both modules,
  anchors checked first — D833.
- Findings registered from **D891**; `tests/README.md` facts from **347**.
