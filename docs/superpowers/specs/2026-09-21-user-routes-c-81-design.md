# Sub-project 81 slice C: the settings page and the block forms

**Status:** slice C complete. THREE production defects fixed (D1051–D1053).
**Branch:** `blentz`
**Predecessor:** slice B, which fixed D1041–D1048 and emptied D988's inventory.
**65 floors.**

## Scope

| function | gaps |
|---|---|
| `user_settings` | 103 |
| `user_settings_block_community` | 25 |
| `user_settings_block_user` | 24 |
| `user_settings_block_domain` | 19 |
| `user_settings_block_instance` | 16 |
| `_calculate_future_date` | 15 |

## THE PRODUCTION CHANGES

### P1 — two cookie parses that answered 500 (D1051, D1052)

```
ValueError: Invalid isoformat string: 'not-a-date'
ValueError: invalid literal for int() with base 10: 'abc'
```

Both from cookies, both unguarded, and **both cookies are set to expire in
2099** — so one corrupt value answers 500 on the settings page permanently, and
the settings page is where the account would go to clear it. The feature is a
self-imposed daily usage limit, which makes the accounts most likely to hit it
the ones least equipped to work around it. An unreadable restriction is now
treated as no restriction.

**The first probe measured nothing.** With only one of the two cookies set the
`and` chain short-circuits before the parse, so it answered 302 and looked
clean. Recorded as fact 475.

### P2 — the block box would block this instance (D1053)

Typing this instance's own domain into the settings page's *block instance*
box blocked it: every local post, comment and community hidden from the caller,
with no obvious way back. **D1035's shape at the other end of the same
feature** — that one was the button on a local profile, this one is the form —
and fixing the first did not touch the second.

## Success criteria

- The six functions at `[]` on the **full-suite** run.
- P1 and P2 land with their pins inverted.
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D1051**; `tests/README.md` facts from **475**.
