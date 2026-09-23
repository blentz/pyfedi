# Sub-project 84 slice D: the reading half of `app/api/alpha/utils/user.py`

**Status:** slice D complete. ONE production defect fixed (D1184), one
equivalent mutant recorded (D1185).
**Branch:** `blentz`
**Predecessor:** slice C, which covered the module's actions and fixed
D1175–D1181. **74 floors, unchanged** — this module's floor is taken when
slice E closes it.

## Scope

`get_user`, `get_user_details`, `get_user_list`, `get_user_replies` (which is
also the mentions feed), `get_user_media`, `get_user_unread_count` and
`post_user_mark_all_as_read`.

The module is at **39%** after slices C and D. What remains is
`put_user_save_user_settings` (300 lines) and the notification group — slice
E, which closes it.

## THE PRODUCTION CHANGE

### One notification broke the whole replies feed (D1184)

```python
if 'comment_id' in result[0]:
    all_comment_ids.append(result[0]['comment_id'])
if result[1] == True:
    read_comment_ids.append(result[0]['comment_id'])   # no test
```

The second append had none of the membership test the first one makes.
`Notification.targets` is a free-form JSON dict and several subtypes write
different keys into it, so one notification of the right subtype carrying a
`post_id` instead broke the endpoint for that account entirely:

```
PROBE bg3 outcome: KeyError: 'comment_id'
PROBE bg4 outcome: KeyError: 'comment_id'
```

— the replies feed and the mentions feed both. The read collection now sits
inside the test that was already there.

## What the coverage found without a defect

* `get_user` looks a local account up by bare name with
  `func.lower(User.ap_domain) == None`, which reads like a comparison that
  can never match. SQLAlchemy renders it `IS NULL` and the lookup resolves —
  measured before assuming anything: `PROBE bg1 outcome: found 3 | author
  ap_domain: None`.
* `get_user_media` falls back to the **web session** when the bearer token
  does not authorise. Inside a request an unauthenticated caller reaches
  `incorrect_login`; the fallback only ever answers with the caller's own
  media. Left alone, with a row for each half.

## D1185, an equivalent mutant

`if unread_notifications > 0:` guards the counting queries. With the counter
at zero every query it guards answers zero anyway, and `other` is
`0 - 0 - 0 - 0`. It is a performance short-circuit.

## The mutation pass

31 mutants, 22 killed on the measuring pass — the worst ratio of this
sub-project, and **every one of the nine survivors was a row of mine**:

* four sort rows asserted only the **count**, which every sort satisfies, so
  all four passed with the sort clause deleted (fact 554);
* the media row gave its file a name identical to the last segment of its
  url, so the `file_name or <derive from url>` fallback could not be told
  apart (fact 555);
* that row also had only one account's file in the database, so dropping the
  `user_id` filter changed nothing;
* the `Local` list row asserted that a local account was present rather than
  that a remote one was absent;
* `saved_only` and the deleted-account-by-name lookup had no row at all.

All rewritten or added; 30/31, the survivor being D1185.

That nine-of-nine result is the argument for the mutation pass in one round:
the coverage was already at `[]` for these functions, and nine of the
assertions holding it up were not testing what they named.

## Success criteria

- Every function in scope at `[]`.
- D1184 lands with its pin inverted (two rows red without it).
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D1184**; `tests/README.md` facts from **554**.
