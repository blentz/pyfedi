# Sub-project 81 slice F: the profile page, notifications and alerts

**Status:** slice F complete. ONE production defect fixed (D1068).
**Branch:** `blentz`
**Predecessor:** slice E, which fixed D1064–D1066. **65 floors.**

## Scope

| function | gaps |
|---|---|
| `show_profile` | 47 |
| `user_alerts` | 35 |
| `notifications` | 25 |

## THE PRODUCTION CHANGE

### D1043's shape at its second site (D1068)

```
ValueError: invalid literal for int() with base 10: 'abc'
```

The same expression, the same fault and the same fix as D1043 — at the route
that **lists** notifications rather than the one that marks them read. Fixing
one a slice earlier did not touch the other.

This is **fact 478 repeating within two slices** ("the same feature has two
ends"), which is why it is a numbered finding rather than a footnote: the rule
was written down and still did not get applied. The habit has to be a step —
grep the module for the same expression — not a memory.

## What the coverage found without a defect

`show_profile`'s deleted/banned handling is unreachable through `/u/<name>`:
`activitypub.user_profile` filters `deleted=False, banned=False` before calling
it. The reachable path is `show_profile_by_id` (`/user/<id>`), which is what a
notification link uses — so those rows go through the id route and say why.

`user_feeds` was read while disambiguating a mutation anchor and is correct:
`filter_by(public=True).filter_by(user_id=user.id)`. It is left for a later
slice with its own 11 gaps.

## Success criteria

- The three functions at `[]` on the **full-suite** run.
- D1068 lands with its pin inverted.
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D1068**; `tests/README.md` facts from **488**.
