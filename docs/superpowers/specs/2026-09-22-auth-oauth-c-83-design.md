# Sub-project 83 slice C: `app/auth/oauth_util.py`

**Status:** slice C complete. SIX production defects fixed (D1138–D1143), one
dead arm removed (D1144).
**Branch:** `blentz`
**Predecessor:** slice B, which closed `app/auth/routes.py`'s registration and
connect routes and fixed D1133, D1135, D1136, D1137. **67 floors, unchanged
this round** — `app/auth/routes.py`'s and `app/auth/util.py`'s floors are
taken when those modules close.

## Scope

`app/auth/oauth_util.py` in full — the shared handler all three providers'
authorize callbacks return, the registration it performs, and the user name it
invents — plus the Mastodon arm of `mastodon_authorize`, which is the only
place a visitor types their own email.

`app/auth/oauth_util.py` was **11.9%** and is now **100%**.

## THE PRODUCTION CHANGES

### Every first-time Google or Discord signup was a 500 (D1138)

`handle_user_verification` ends its new-account path with `return None`, and a
Flask view that returns None is

```
PROBE av4 outcome: TypeError: The view function for 'auth.google_authorize'
did not return a valid response. | user created: True
```

The account had already been created, `finalize_user_setup` had run and
`login_user` had handed out the session — and then the visitor got an error
page. Whether they were signed in was invisible to them. A new account now
goes where the local registration path sends one, `auth.filter_selection`.

### The OAuth signup bypassed every user-name rule (D1139, D1140)

`find_new_username` took the email's **local part, verbatim**, and checked
only that no user held the same string with the same capitalisation:

```python
existing_user = User.query.filter(User.user_name == email_parts[0], User.ap_id == None).first()
```

`RegistrationForm.validate_user_name` asks six questions. This asked a
narrower version of one of them. Measured:

```
PROBE av1 admin exists: True
PROBE av2 names: ['founder', 'Person', 'we.ird+chars!']
PROBE av3 person-ish names: ['Person', 'person']
```

* **`admin`** — the name `process_registration_form` reserves — was one Google
  sign-in away (D1139);
* `we.ird+chars!` is outside `USER_NAME_CHARSET_RE`, which exists because a
  local user name is interpolated into an actor URL, a webfinger answer and a
  feed regex (D1139);
* `person` was created beside `Person`, while `find_user` lowers both sides
  and takes `.first()` — so which of the two answers to a login is decided by
  row order (D1140). Community and feed names were not consulted either.

The three uniqueness queries the form makes are now one function,
`user_name_is_taken` in `app/utils.py`, called from both ends; the reserved
list is `RESERVED_USER_NAMES` beside it, which `process_registration_form`
held as a local list. Fact 478 for the thirteenth time in this campaign.

### Two accounts, one email address (D1141)

Mastodon's `accounts/verify_credentials` returns no email, so that arm renders
a form and asks the visitor for one. Nothing checked it:

```
PROBE av6 status: 302 | accounts holding that email: 2 | names: ['Person', 'person']
```

`RegistrationForm.validate_real_email` refuses a taken address and
`handle_user_verification` refuses one for the providers that supply it. This
third door had no check at all — and login-by-email, the reset-password
request and the resend-verification form all look an account up by address and
take `.first()`, so the duplicate decides which account those answer for.

The check is `email_already_registered`, shared by both arms. It lives in the
route rather than in the form because **the same form is submitted by somebody
whose account already exists**, and their own address is not a duplicate.

The other half: a POST that did not validate fell through to
`handle_oauth_authorize`, which asks the provider for a token it has already
spent, so a visitor whose email was refused was told *"Login failed due to a
problem with the OAuth server."* The form is shown again now, with the reason.

### A banned visitor was given an account, and then a session (D1142)

`handle_oauth_authorize` sends an **existing** banned account to
`handle_banned_user`; the Mastodon arm does the same. A visitor who had no
account yet was never asked. `initialize_new_user` wrote
`banned=user_ip_banned() or user_cookie_banned()` into the row and called
`finalize_user_setup` and `login_user` on it regardless:

```
PROBE av5 outcome: ... | created: True | banned: True
```

One helper, `refuse_banned_visitor`, at both new-account sites.

### A provider with no email crashed the callback (D1143)

```
PROBE av7 outcome: AttributeError: 'NoneType' object has no attribute 'lower'
```

`email = user_info.get('email')` and then `email.lower()`. Reachable whenever
a provider answers without one — which is Mastodon's normal behaviour, and any
provider whose scope was configured without the email claim.

### D1144, an arm that could not be taken

`if can_user_authenticate is False: return redirect(url_for('auth.login'))` —
`can_user_register` answers True, a redirect or a rendered page, never False,
and the arm existed to convert a False into the redirect the function already
returns for itself. Removed.

## The mutation pass

24 mutants, **24 killed** on the measuring pass — after two rows were added
for arms the first draft left unmeasured: that the profile survives in the
session across a refused email, and that a form failing its own validators is
shown again instead of a spent handshake.

## Success criteria

- `app/auth/oauth_util.py` at **100%**, 0 functions with gaps.
- D1138–D1143 land with their pins inverted (12 rows red without them).
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D1138**; `tests/README.md` facts from **532**.
