# Sub-project 81 slice G: files, single notifications, unsubscribes, follows

**Status:** slice G complete. TWO production defects fixed (D1071, D1072).
**Branch:** `blentz`
**Predecessor:** slice F, which fixed D1068. **65 floors.**

## Scope

Thirteen small functions: the file list and its delete page, the four
single-notification actions, the two unsubscribe links, both bookmark lists,
and the follow-request list with its accept and reject.

## THE PRODUCTION CHANGES

### P1 — every uploaded file's URL was enumerable (D1071)

`/user/files/delete/<id>` renders

```html
<img src="{{ file.source_url }}">
```

for whatever id is in the URL, and nothing tied the file to the caller.
Measured from an unrelated account:

```
PROBE aa1 status: 200 url leaked: True
```

Ids are sequential, so walking them disclosed the URL of **every uploaded file
on the instance** — including files an account uploaded and never posted. The
deletion itself was never the hole: `process_file_delete` scopes its DELETE by
user (`app/shared/upload.py:128`). **The page was.**

### P2 — rejecting a follow request accepted it (D1072)

`user_follow_request_reject` is a copy of the accept route, and the copy was
never finished:

```python
if follow_request:
    follow_request.is_accepted = True     # in the REJECT route
    ...
    "type": "Reject",
```

So rejecting a follow request sent the remote side a `Reject` **and recorded
locally that the follow had been accepted**. The rejected person then appeared
in the followers list of somebody who believed they had turned them away —
`show_profile` selects `is_accepted == True` — and the two instances disagreed
about what had happened. The column's own comment gives the value:

```python
is_accepted = db.Column(db.Boolean)   # None = request sent. True = accepted. False = Rejected
```

which is what `app/activitypub/routes.py:1185` writes for an inbound
rejection.

## Success criteria

- The thirteen functions at `[]` on the **full-suite** run.
- P1 and P2 land with their pins inverted.
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D1071**; `tests/README.md` facts from **491**.
