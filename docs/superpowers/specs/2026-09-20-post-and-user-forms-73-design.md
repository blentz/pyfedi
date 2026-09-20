# Sub-project 73: `app/post/forms.py` and `app/user/forms.py` — a validator that never ran

**Status:** design approved, ready to implement
**Branch:** `blentz`, at `cf8392f06`
**Predecessor:** sub-project 72, which closed `app/auth/forms.py` — 50 floors.

## Goal

Close both remaining mid-sized form modules, repair the dead validator found
while scoping them, and add a repo-wide guard that no inline validator can go
unwired again.

| module | before | missing |
|---|---|---|
| `app/post/forms.py` | 73.171% | 14 lines, 8 arcs |
| `app/user/forms.py` | 85.311% | 14 lines, 12 arcs |

## THE PRODUCTION CHANGE

### P1 — `validate_matrix_user_id` has never run

```python
matrixuserid = StringField(_l('Matrix User ID'), validators=[Optional(), Length(max=255)], ...)
...
def validate_matrix_user_id(self, matrix_user_id):
    if not matrix_user_id.data.strip().startswith('@'):
        raise ValidationError(_l('Matrix user ids start with @'))
```

WTForms binds an inline validator by name: for a field called `matrixuserid` it
looks for **`validate_matrixuserid`**. The method is called
`validate_matrix_user_id`, so nothing binds it and it has never executed.

**Probe** — a profile form carrying a Matrix ID with no `@` at all:

```
PROBE y1 matrix field name: ['matrixuserid']
PROBE y1 validator sought by wtforms: validate_matrixuserid -> False
PROBE y1 validator actually defined: validate_matrix_user_id -> True
PROBE y1 matrixuserid errors: []
```

No error. This is sub-project 45's class — **a guard that does not guard** —
and it is why the method's lines show as uncovered: no test could reach them,
because no code path does.

**The rename alone is not the repair.** The field is `Optional()`, and the
method as written rejects the empty string: `''.strip().startswith('@')` is
False, so simply wiring it up would make an optional field mandatory for every
profile save. The fix is both halves — bind it, and skip when the field is
blank.

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `app/post/forms.py:47`, `app/user/forms.py:187`, `app/chat/forms.py:33`, `app/community/forms.py:579` | **`reasons_to_string` is copied verbatim into four form classes**, along with the `reason_choices` list it reads. Four identical nested loops and one shared 255-character truncation. | A refactor across four modules, two of which this round does not touch. Both copies in scope are covered here, so the duplication is now pinned rather than merely present. |
| R2 | `app/post/forms.py:106-107` | **`import dateparser` and `import pendulum` inside `validate_remind_at`.** The campaign's standing rule is that imports go at the top of the file; `app/` carried 216 such violations when the rule was written and this is two of them. | Pre-existing and outside this round's remit. Named so it is counted rather than overlooked. |
| R3 | `app/post/forms.py:108-116` | **`validate_remind_at` raises `ValidationError` INSIDE its own `try`, and the bare `except Exception` catches it and raises an identical one.** The message is the same either way, so no behaviour depends on it — but a genuine failure inside `dateparser` is reported to the user as "Invalid." exactly like a badly typed date. | The two paths are observationally identical today. Changing what a parser crash tells the user is a product decision. |

## Success criteria

- Both modules at `[]`/`[]` on the **full-suite** run; two floors of 100 — 52.
- P1 lands with its pin inverted; `git diff --numstat` names exactly
  `app/user/forms.py`.
- **A new guard test asserts that every undecorated `validate_<name>` in `app/`
  names a real field on its own class**, so this cannot recur silently. The scan
  that found P1 reported four hits on its first run and one after the false
  positives were removed — marshmallow's `@validates_schema` methods, and a
  `DateTimeLocalField` the first field-type list did not know about — so the
  guard matches any `*Field` call and ignores decorated methods.
- Suite green; floors checked with `&&`; a mutation pass over both modules'
  methods, anchors checked first — D833.
- Findings registered from **D863**; `tests/README.md` facts from **335**.
