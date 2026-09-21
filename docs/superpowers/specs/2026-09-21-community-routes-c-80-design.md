# Sub-project 80 slice C: the community lifecycle — create, edit, move, delete

**Status:** slice C complete. FIVE production defects fixed (D980-D984); the
design anticipated two. See the findings ledger, D980-D986.
**Branch:** `blentz`, at `7aa7607fb`
**Predecessor:** slice B, which fixed D969-D976. **64 floors.**

## Scope

| function | gaps |
|---|---|
| `community_edit` | 97 |
| `add_local` | 63 |
| `add_remote` | 33 |
| `community_move` | 18 |
| `community_delete` | 7 |

Who may create a community, and who may change one after it exists.

## THE PRODUCTION CHANGES

### P1 — the validator and the route disagree about the name (D980)

`AddCommunityForm.validate` checks `self.url.data.strip().lower()` for
uniqueness against `Community.name`, `User.user_name` and `Feed.name`. The
route then does:

```python
form.url.data = slugify(form.url.data.strip(), separator='_').lower()
```

`slugify` is not the identity on strings the validator accepts. Measured:

```
PROBE g1 raw='__test__'    validator sees='__test__'    route creates='test'
PROBE g1 raw='test__name'  validator sees='test__name'  route creates='test_name'
PROBE g1 raw='___'         validator sees='___'         route creates=''
```

So a submission the validator approved can become a name that already exists,
or an empty one. Both reach the INSERT:

```
psycopg2.errors.UniqueViolation: duplicate key value violates unique
constraint "ix_community_ap_profile_id"
DETAIL:  Key (ap_profile_id)=(https://test.piefed.local/c/general) already exists.
```

An unhandled `IntegrityError` — a **500 on community creation**, reachable by
typing `__general__` when `general` exists. The uniqueness error the form was
written to produce never fires, because it was asked about a different string.

**Fix:** normalise in `AddCommunityForm.validate` — slugify, write it back to
`self.url.data`, and run the emptiness and uniqueness checks against the value
that will actually be stored. The route's own `slugify` then becomes idempotent
rather than load-bearing, and the form reports a duplicate as a field error.

### P2 — a refused community edit discards what was typed (D981)

`community_edit`'s POST branch ends with `else:` rather than `elif
request.method == 'GET':`, so a submission the form refuses is overwritten from
the database. Measured:

```
PROBE g3 errors: {'theme': [...], 'topic': [...], 'default_layout': [...]}
PROBE g3 title redisplayed as: 'Stored title'
```

**D907's shape for the fourth time** — slice A of sub-project 79, slice C's
`admin_federation`, slice F's `admin_user_edit`, and now here. Each instance
was found by covering the function, never by reading it.

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `app/community/routes.py:1237`, `:83` | `community_edit` and `add_local` gate on `current_user.is_admin()` while the rest of the blueprint uses `is_admin_or_staff()` — the D963 inconsistency, now at four sites. | Still one decision about what staff are for; worth one pass over the blueprint rather than four partial ones. |
| R2 | `app/community/routes.py:1275`, `:1285` | `community_edit` calls `db.session.delete(old_icon_file)` **before** `old_icon_file.delete_from_disk()`; `admin_user_edit` does the reverse. Both work today, because the instance stays readable until flush, but the pair is order-dependent and inconsistent. | A convention to settle once, across every route that replaces an uploaded file. |
| R3 | `app/community/routes.py:1298` | `community.languages.append(Language.query.filter(Language.code == 'und').first())` appends `None` when the `und` row is missing, and the failure surfaces as `FlushError: Can't flush None value found in collection Community.languages` rather than anything nameable. | Depends on whether `und` is guaranteed by migration; that is an installer question. |

## What the slice found beyond the design

- **D982** -- `form.topic.data > 0` on an `Optional()` field is a `TypeError`
  for any client that omits it. Reverting the guard failed five rows.
- **D983** -- three of `add_remote`'s five address branches are unreachable
  behind `SearchRemoteCommunity.validate`. Removed.
- **D984** -- R-list item promoted: the `try/except` around `g.site` could not
  rescue anything, because `g.site.enable_nsfw` was read unguarded three lines
  below. Fixed rather than deleted, so the fallback means what it says.

R2 and R3 from the list below remain registered.

## Success criteria

- The five functions at `[]` on the **full-suite** run.
- No floor yet on `app/community/routes.py`.
- P1 and P2 land with their pins inverted, including a row per slugify
  divergence.
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D980**; `tests/README.md` facts from **414**.
