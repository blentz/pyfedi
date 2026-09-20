# Sub-project 79 slice D: the two remote scans

**Status:** slice D complete. FOUR production defects fixed (D931-D934), one
registered (D935), one repaired on its third sighting (D936).
**Branch:** `blentz`, at `d8da26bbd`
**Predecessor:** slice C, which fixed D920-D924.

**A note on order:** slices A, B and C each had a design written before the
work. This one did not — the defects were found by reading the two functions
straight through, and this document is the record rather than the plan. Naming
that is cheaper than implying a process that did not happen.

## Goal

Cover the last two functions in the federation group:

| function | gaps |
|---|---|
| `admin_federation_remote_scan` | 173 |
| `admin_federation_mastodon_scan` | 27 |

Both read a REMOTE server's API and then subscribe or follow on the strength of
what it said, so the interesting rows are all about what happens when that
server answers something the code did not expect.

## The production changes

### P1 — D924's membership test, in a second place (D931)

`server_domain in banned_urls`, identical to the one slice C replaced in
`admin_federation_preload`. A wildcard ban is a pattern, not a domain, so
`'evil.example' in ['ev*l.example']` is False and a defederated instance could
be scanned and subscribed to wholesale. Replaced with `instance_banned()`.

### P2 — an unbound name the remote server controls (D932)

`remote_instanceinfo_url` was assigned only inside the `for e in
nodeinfo_dict['links']` loop. A document matching neither schema 2.0 nor 2.1 —
an empty `links` list is enough — left it unbound, and the next line raised
`UnboundLocalError`. Initialised to `None` and checked.

### P3 — three loops a remote server could run forever (D933)

The lemmy, piefed and mbin scans each page until a page comes back with fewer
than 50 entries, and the remote server decides how long every page is. Capped
at 200 pages: 10,000 communities, past any real instance.

### P4 — the Mastodon scan had no banned-instance check (D934)

The two community scans on the same page refuse a banned instance; this one
never checked. `bulk_follow` resolves each handle through `search_for_user`,
which fetches the actor and creates local rows, so following accounts on a
defederated instance is federating with it.

### P5 — D815's shape, third sighting (D936)

Four `flash(_(f'...'))` sites and three `flash(_(message))` summaries
interpolated before gettext saw the string. The four URL messages now use named
parameters; the three summaries are flashed untranslated, with a comment, since
they are diagnostics built from seven runtime counts.

## Result

- Both functions at `[]` on the full-suite run.
- Mutation pass: **41 of 41 killed on the measuring pass**, the campaign's
  first clean sweep at this size. Facts 371 and 377 are why.
- `ANCHOR FAILURES: [('m20', 2), ('m26', 2)]` became D935: the lemmy and piefed
  branches are byte-identical for ~30 lines.
- Full suite `6750 passed, 3 skipped, 258 warnings`, `All 63 module floors met.`
  6702 + 48 = 6750, exactly.
- Still no floor on `app/admin/routes.py`: slices E and F remain.
