# Sub-project 83 slice A: `app/auth/routes.py` — the credential flows

**Status:** slice A complete. THREE production defects fixed (D1129, D1130,
D1131).
**Branch:** `blentz`
**Predecessor:** sub-project 82, which closed `app/post/routes.py` and took its
floor. **67 floors, unchanged this round** — `app/auth/routes.py`'s floor is
taken when the module closes.

## Why this module next

The standing instruction puts hosted-site security first. After the post
routes, the lowest-covered security-bearing code is authentication:

| module | statements | covered |
|---|---|---|
| `app/auth/routes.py` | 256 | 25.7% |
| `app/auth/util.py` | 309 | 20.4% |
| `app/auth/oauth_util.py` | 118 | 11.9% |
| `app/auth/onboarding.py` | 110 | 15.1% |
| `app/ldap_utils.py` | 109 | 10.8% |

Everything an attacker reaches **before** holding an account lives here:
login, registration, the email-verification link, the password-reset link, and
three OAuth providers.

## Scope of slice A

`login`, `logout`, `register`, `resend_email`, `reset_password_request`,
`reset_password`, `verify_email`, and the small pages they redirect to
(`please_wait`, `check_email`, `not_trustworthy`, `validation_required`,
`permission_denied`). The OAuth callbacks are slice B.

## THE PRODUCTION CHANGES

### The resend-verification form told you which addresses are registered (D1129)

`resend_email` answered **"No user found with that email address."** for an
address it does not know, and "If an account exists, a link has been sent" for
one it does. The second message is carefully non-committal; the first gives
the answer away, one guess at a time. Measured:

```
PROBE ar1 known address says: True
PROBE ar2 unknown address says: True
```

`reset_password_request`, one screen away, already answers the same way in
both cases. Fact 478 again: the same question asked at two ends, guarded at
one.

### A password-reset link worked more than once (D1130)

`get_reset_password_token` mints a JWT carrying the user id and a ten-minute
expiry, and `verify_reset_password_token` decodes it and returns the user.
Nothing about **using** the token changed anything, so the same link reset the
password again, and again, until it expired. Measured:

```
PROBE ar3 first reset worked: True | same token reused: True
```

The window is ten minutes, but the link outlives the reset in browser history,
in a forwarded message, on a shared device — and whoever finds it there takes
the account from the person who has just secured it.

The token now carries a **digest of the password hash it was issued against**,
and verification refuses a token whose digest no longer matches. That makes it
single-use with no storage at all: the reset changes the hash, so the token is
spent. A digest rather than the hash itself, because **a JWT is signed, not
encrypted** — whoever holds the token can read its payload.

Tokens minted before this field existed carry no `pw` claim and are refused.
That costs their holders one more click on "forgot password", and closes the
window for everybody else.

### A lead for slice B, not yet measured

`is_invalid_email_or_username` (`app/auth/util.py`) opens with

```python
if form.email.data.strip():
    return False
```

`email` is the **honeypot** — a `HiddenField` a human never fills, beside the
real `real_email`. Returning False means "not invalid", so a caller who fills
the honeypot **skips the reserved-name and role-address checks below it** and
carries on into registration. Either the early return is inverted or the
honeypot is consumed somewhere this reading has not found.

A first probe could not reach the code: every registration POST it sent came
back as a re-rendered form, so the form itself refused them and nothing was
measured. That is slice B's first job, and it is recorded here rather than
guessed at.

## Success criteria

- The credential flows in scope at `[]`; `register` and the OAuth callbacks
  are slice B, and `app/auth/routes.py` rises from 25.7% to 60.8%.
- D1129, D1130 and D1131 land with their pins inverted.
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D1129**; `tests/README.md` facts from **526**.

### D1131, the third defect

`process_login` said **"No account exists with that user name."** for a name it
does not know and "Invalid password" for one it does — username enumeration on
the most-probed form on the site. Three sites carried the pair
(`app/auth/util.py` twice, `app/shared/auth.py` once) while the API arm of the
same flow already raised a single `incorrect_login` for both. One message now,
carrying the reset link for everybody: it helps whoever has genuinely
forgotten a password — including the OAuth-created accounts that have none
(D1073) — and, shown every time, it distinguishes nothing.

Four existing rows in `tests/test_shared_auth_login.py` pinned the old wording
and were updated: they are pins of behaviour this round deliberately changed,
which is what a pin turning red is for.
