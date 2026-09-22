# Sub-project 83 slice B: registration and the three OAuth providers

**Status:** slice B complete. FIVE production defects fixed (D1133, D1135,
D1136, D1137), ONE registered and pinned (D1134).
**Branch:** `blentz`
**Predecessor:** slice A, which closed the credential flows and fixed
D1129–D1131. **67 floors, unchanged** — `app/auth/routes.py`'s floor is taken
when the module closes.

## Scope

`register` and its helpers in `app/auth/util.py`, and the twelve OAuth routes:
`{google,mastodon,discord}_{login,connect,authorize,connect_callback}` plus
`mastodon_authorize`'s registration form.

## THE PRODUCTION CHANGES

### The honeypot was a bypass (D1133)

`is_invalid_email_or_username` opened with

```python
if form.email.data.strip():
    return False
```

`email` is the **honeypot** — a `HiddenField` beside the real `real_email`,
which a human never fills. `False` means "not invalid", so filling it skipped
**both checks below**: the reserved-user-name list and the role-address
refusal. Measured:

```
PROBE at3 honeypot + reserved name "admin": users created=1 | admin exists=True
PROBE at4 honeypot + role address: users created=1
PROBE at5 reserved name, NO honeypot: users created=0
PROBE at6 role address, NO honeypot: users created=0
```

The field meant to catch bots was the way around the gates.

The first attempt at this measurement, in slice A, could not reach the code:
`RegistrationForm` adds a `CaptchaField` with `DataRequired` unless
`captcha_enabled` is off, so the form refused every probe submission and the
route answered 200 with the form re-rendered — which reads exactly like the
route rejecting the registration. Fact 529.

### The door that never shut (D1137)

`handle_abandoned_open_instance` closes registrations when no admin has
logged in recently. It wrote to `g.site` — which `before_request` builds as
`Site(**get_site_as_dict())`, a **transient object from a plain dict, never
added to the session**. The write reached the current request and nothing
else, so the next request rebuilt `g.site` from the database and the instance
was Open again. Fact 530.

### Two of six routes missing a decorator (D1135)

`google_connect`, `discord_connect` and both their callbacks carry
`@login_required`. `mastodon_connect` and `mastodon_connect_callback` did not:

```
PROBE au1 anonymous /auth/mastodon_connect: 200
PROBE au1 anonymous /auth/google_connect: 302 -> /auth/login?next=...
```

An anonymous caller was redirected to the remote instance for a flow that can
only fail — a real authorization burned.

### What a failure told the visitor (D1136)

All three callbacks ended `flash(... error=str(e))`, and the exception comes
from the OAuth client:

```
Failed to connect Mastodon account: token secret=abc123 leaked
```

The detail goes to `current_app.logger.warning` now, where the operator can
see it and the visitor cannot.

## Registered and pinned

**D1134** — with D1133 fixed the honeypot no longer bypasses anything, but
nothing acts on it either: filling it registers an account exactly like an
honest submission. Making it refuse is a product decision, not a coverage
fix — the field is named `email`, which is what an autofiller looks for, so
refusing would lock out anyone whose password manager fills hidden fields.
Pinned in a row carrying "update this test (D1134)".

## Success criteria

- `register` and the OAuth routes at `[]` on the **full-suite** run.
- D1133, D1135, D1136 and D1137 land with their pins inverted (13 rows red
  without them); D1134 pinned as it stands.
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D1133**; `tests/README.md` facts from **529**.
- 17 mutants, 15 killed on the measuring pass; both survivors were arms with
  no row — a whitespace-only `tos_url`, and `RequireApplication` with no
  question.
