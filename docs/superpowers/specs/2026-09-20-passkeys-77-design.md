# Sub-project 77: the passkey pair — WebAuthn registration and login

**Status:** design approved, ready to implement
**Branch:** `blentz`, at `342bae12b`
**Predecessor:** sub-project 76, which closed the three smallest alpha API utils — 59 floors.

## Goal

Close both passkey modules and take two floors — 61.

| module | before | gaps |
|---|---|---|
| `app/auth/passkeys.py` | 18.987% | 64 |
| `app/user/passkeys.py` | 42.857% | 36 |

Neither is referenced by any existing test.

## THE ONE PRODUCTION CHANGE

### P1 — `if not user.passkeys:` is always false

```python
if not user.passkeys:
    error_message = f'No passkeys found for {username}'
else:
    ...
```

`User.passkeys` is `db.relationship('Passkey', lazy='dynamic', ...)`
(`app/models.py:1083`), so the attribute is an **`AppenderQuery`**, not a list —
and a query object is truthy whether or not it would return rows.

**Probe**, on a user with no passkeys at all:

```
PROBE h1 type: AppenderQuery
PROBE h1 count: 0
PROBE h1 bool(user.passkeys): True
PROBE h1 "not user.passkeys" -> False
```

So the branch never runs and `'No passkeys found for …'` is a message the
application cannot emit. The `else` arm runs instead, loops over zero passkeys,
and reports `'No valid passkeys found for …'` — the same refusal by a different
route, which is why nothing looked wrong.

The file already knows the right spelling: `allowed_credentials` two functions
above tests `user.passkeys.count()`.

**Fix:** `.count()`, which makes the branch reachable and the message accurate.
This is sub-project 45's class — a guard that does not guard — and the same
`lazy='dynamic'` truthiness trap is worth a fact, because `User` has other
dynamic relationships.

## Registered, not fixed — and one of these is serious

| # | Where | What | Why not this round |
|---|---|---|---|
| **R1** | `app/auth/passkeys.py:89`, `:93` | **THE CLONED-AUTHENTICATOR CHECK IS DISABLED.** `verify_authentication_response(..., credential_current_sign_count=0)` is hardcoded, while the stored counter is maintained beside it: `passkey.counter += 1`. WebAuthn's signature counter exists so a relying party can detect a **cloned authenticator** — the RP stores the last count and rejects a response that does not exceed it. Passing `0` makes every count acceptable. Measured: a passkey stored with `counter=41` is verified with `credential_current_sign_count: 0` and then incremented to `42`. So the value is written on every login and never read. **Repairing it is not a coverage round's call**: the stored counters are also *wrong* — incremented by one rather than set from the authenticator's reported `new_sign_count`, which `verify_registration_response`'s result carries and `app/user/passkeys.py:107` discards — so switching the check on would reject logins from authenticators whose real count has outrun the stored one. It needs a migration decision, not a one-line change. |
| **R2** | `app/auth/passkeys.py:39` | **THE OPTIONS ENDPOINT ENUMERATES USERNAMES.** `/auth/passkeys/login_options` answers `{"error": "Could not find user nobody"}` for an unknown name and a real challenge for a known one, so an unauthenticated caller can test whether an account exists. Measured in both states. **The verification endpoint deliberately does not do this** — `app/auth/passkeys.py:104` uses the same `'No valid passkeys found for …'` for a missing user as for a user with no working passkey — so the asymmetry is between two endpoints in one file. | Changing what the endpoint tells an unknown caller is a product decision about the login UX, and the browser code that consumes this response is not in scope. Registered with the probe so the decision starts from evidence. |
| R3 | `app/user/passkeys.py:38`, `:86` | **`login_required(csrf=False)` on both registration endpoints.** Typical for WebAuthn JSON endpoints, where the challenge is the anti-replay measure, but it is a deliberate exemption and is not commented as one. | Named rather than changed. |
| R4 | `app/user/passkeys.py:107-109` | **The authenticator's initial `sign_count` is discarded at registration**: the new `Passkey` takes the column default of 0 even though `registration_verification.sign_count` is available. R1's other half. | Same decision as R1. |
| R5 | `app/auth/passkeys.py:75-81` | **`public_key` is typed `LargeBinary` but the code handles a `str` as well**, with a nested `try`/`except` falling back to `.encode('utf-8')` when base64 decoding fails. Defensive against data this schema cannot hold. | A data-shape question about rows that may predate the column type. |

## Success criteria

- Both modules at `[]`/`[]` on the **full-suite** run; two floors of 100 — 61.
- P1 lands with its pin inverted; `git diff --numstat` names exactly
  `app/auth/passkeys.py`.
- No `Model.query.get(...)` in the new rows.
- Suite green; floors checked with `&&`; a mutation pass over both modules,
  anchors checked first — D833.
- Findings registered from **D884**; `tests/README.md` facts from **344**.
