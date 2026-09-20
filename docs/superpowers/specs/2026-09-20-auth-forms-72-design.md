# Sub-project 72: `app/auth/forms.py` — registration validation

**Status:** design approved, ready to implement
**Branch:** `blentz`, at `e6afdb22f`
**Predecessor:** sub-project 71, which ended our own warnings — 49 floors.

## Goal

Cover the registration form's four validators, repair the two defects in
`validate_password`, and take a floor. The module is the lowest-covered form
file in the repo at **58.91%**, with 28 missing statements and 25 missing arcs.

## Targets

| member | where | missing |
|---|---|---|
| `RegistrationForm.__init__` | `:39-42` | 1 line, 1 arc |
| `validate_real_email` | `:44-47` | 3 lines, 2 arcs |
| `validate_user_name` | `:49-75` | 6 lines, 6 arcs |
| `validate_password` | `:77-98` | 17 lines, 15 arcs |
| `filter_user_name` | `:100-104` | 1 line, 1 arc |

Everything else in the file is field declarations, executed at import.

## THE TWO PRODUCTION CHANGES

### P1 — the length guard rejects the only length it should allow

```python
if len(password.data) == 128:
    raise ValidationError(_l('Maximum password length is 128 characters.'))
```

`==`, not `>`. Measured across the boundary:

```
PROBE w1 len=127 errors=[]
PROBE w1 len=128 errors=['Maximum password length is 128 characters.']
PROBE w1 len=129 errors=[]
PROBE w1 len=130 errors=[]
```

So a password of exactly 128 characters — **the stated maximum**, per the
field's own `title` of "Minimum length 8, maximum 128" — is refused with a
message saying the maximum is 128. And 129 and 130 sail past the check that
exists to stop them. The guard is wrong in both directions at once: it rejects
the boundary and admits everything beyond it.

The field validator behind it is `Length(min=8, max=129)`, which admits 129 too,
so **a 129-character password is accepted by the whole form today** while a
128-character one is refused.

**Fix:** `>`, which makes the effective maximum 128 and matches what the form
tells the user. The field's `max=129` then never fires for a password this
validator sees; it is left alone rather than tuned in the same breath, because
changing a field validator changes which error message a user gets and that is
a separate decision.

### P2 — the common-password check is written twice, and the second is dead

```python
    if password.data == 'password' or password.data == '12345678' or password.data == '1234567890':
        raise ValidationError(_l('This password is too common.'))        # :81-82
    ...
    if password.data == 'password' or password.data == '12345678' or password.data == '1234567890':
        raise ValidationError(_l('This password is too common.'))        # :97-98
```

Character for character the same condition, on the same unmodified data. `:97`
can only be reached when `:81` was false, so `:98` is **unreachable**: coverage
reports the arc `[97, 98]` missing and no test can close it.

`PROBE w2 the common-password check appears twice: True`, and a common password
raises exactly one error, from the first.

**Fix:** delete `:97-98`. This is the choice sub-project 68 made for the same
shape (D822): dead code comes out rather than earning a pragma, because the
alternative leaves a line that the next reader has to re-derive as unreachable.

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `:80` | **`validate_password` mutates the submitted password**: `password.data = password.data.strip()`, so `'  secretpw  '` is silently stored as `'secretpw'` — `PROBE w4 password after validate: 'secretpw'`. The user who typed the spaces will type them again at login, where `LoginForm` does NOT strip, so the password they set is not the password they can log in with. | A real defect and a bigger one than this round's two, but repairing it changes what is stored for future registrations and needs the login path considered with it. Registered with evidence rather than fixed in a coverage round. |
| R2 | `:16`, `:113` | **`LoginForm.password` carries `Length(min=8, max=129)`** while `ResetPasswordForm.password` carries none. An account whose password predates the rule, or exceeds 129 characters, cannot log in at all — the form refuses before any credential check. | The same family as R1 and the same reason: it is a policy decision about existing accounts. |
| R3 | `:89-94` | **The `all_the_same` loop never breaks early**, scanning all 128 characters after the first mismatch settles it. | Cosmetic on a string this short. |
| R4 | `:81`, `:97` | **The common-password list is three literals inline**, not a list or a file, so extending it means editing a condition. | Noted for whoever adds the fourth. |

## Success criteria

- All five members at `[]`/`[]` on the **full-suite** run.
- `app/auth/forms.py` takes a floor of 100 — 50 floors.
- Both repairs land with pins inverted; `git diff --numstat` names exactly
  `app/auth/forms.py`.
- Suite green; floors checked with `&&`; a mutation pass over the four
  validators, anchors checked for uniqueness first — D833.
- Findings registered from **D857**; `tests/README.md` facts from **333**.
