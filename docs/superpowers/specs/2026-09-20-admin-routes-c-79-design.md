# Sub-project 79 slice C: the federation allow/block surface, and two ways past a ban

**Status:** slice C complete. FIVE production defects fixed (D920-D924); the
design anticipated two. See the findings ledger, D920-D930.
**Branch:** `blentz`, at `4c9c30667`
**Predecessor:** slice B, which fixed D912-D915. Module now at **25.6%**, 1230
missing.

## Goal

Cover the four functions that maintain the instance's federation allow and
block lists:

| function | gaps |
|---|---|
| `import_bans_task` | 71 |
| `admin_federation_ban_lists` | 60 |
| `admin_federation_preload` | 57 |
| `admin_federation` | 46 |

This is the instance's core moderation control: what `admin_federation` writes
is what `instance_banned()` and `instance_allowed()` read on **every inbound
activity and every outbound delivery**. The two remote-scan functions
(`admin_federation_remote_scan`, 173, and `admin_federation_mastodon_scan`, 27)
are slice D.

Probing the read side of what this form writes found two defects, both of them
security.

## THE PRODUCTION CHANGES

### P1 — a wildcard ban is compiled as an unescaped regex

`app/utils.py:2356-2358`:

```python
regex_patterns = [re.compile(f"^{cond.domain.replace('*', '[a-zA-Z0-9]')}$")
                  for cond in session.query(BannedInstances)
                                     .filter(BannedInstances.domain.like('%*%')).all()]
```

The domain is interpolated into a pattern with no escaping. Two consequences,
both measured:

```
PROBE f3 instance_banned('evil.com')  -> True     # ban pattern 'ev*l.com'
PROBE f3 instance_banned('evXl.com')  -> True     # intended
PROBE f3 instance_banned('evilXcom')  -> True     # NOT intended: '.' is a metacharacter
PROBE f4 pattern='ev*l.co(m' RAISED: PatternError missing ), unterminated subpattern at position 18
```

So a wildcard ban bans **more** than it says — every `.` in the pattern matches
any character — and a wildcard entry that also contains a regex metacharacter
raises out of `instance_banned`, which re-raises. `instance_banned` gates
inbound activity processing and outbound delivery, so **one malformed blocklist
entry takes federation down instance-wide**, from a text box an admin types
into. `re.PatternError` is not caught anywhere on that path.

Only entries containing `*` are affected: the `.like('%*%')` filter is what
selects them, which is why `'evil.com['` alone is harmless.

**Fix:** `re.escape` the domain and then reinstate the wildcard:

```python
'^' + re.escape(cond.domain).replace(r'\*', '[a-zA-Z0-9]') + '$'
```

`ev*l.com` becomes `^ev[a-zA-Z0-9]l\.com$` — the `*` still means "any letter or
digit", the `.` is now literal, and `ev*l.co(m` compiles.

### P2 — a banned instance re-federates by appending a dot

`inbox_domain` is documented as "the single implementation of a normalisation".
It lower-cases and drops the port, and it does not strip the root label's
trailing dot. `admin_federation` stores `evil.com`; a peer that presents itself
as `evil.com.` is a different string and misses the row.

**Probe**, with `evil.com` banned:

```
PROBE f5 actor='https://evil.com/users/x'    furl host='evil.com'  inbox_domain='evil.com'  banned=True
PROBE f5 actor='https://evil.com./users/x'   furl host='evil.com.' inbox_domain='evil.com.' banned=False
PROBE f5 actor='https://EVIL.COM./users/x'   furl host='evil.com.' inbox_domain='evil.com.' banned=False
```

`evil.com.` is the fully-qualified form of `evil.com`: DNS resolves it
identically and TLS works, so the actor fetch succeeds and the activity is
processed. **A defederated instance gets back in by adding one character to its
actor ids.**

The allowlist direction fails the safe way — `good.com.` is not on the
allowlist, so it is refused — which is why this shows up only as a ban bypass.

**Fix:** strip the trailing dot in `inbox_domain`, where the rest of the
normalisation already lives, so every caller gets it.

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `app/admin/routes.py:448`, `:454` | `cache.delete_memoized(instance_allowed, allow.strip())` invalidates the key equal to **what the admin typed**, while the row is stored `.lower()`ed and real lookups are memoized under whatever string the PEER sent. The invalidation therefore almost never hits the entry it means to. The 150-second `@cache.memoize` timeout bounds the staleness, which is why this is registered rather than fixed. | A caching-key decision that spans every caller of these two functions, not just the admin form. |

## What the slice found beyond the design

- **D922** -- `import_bans_task` guarded the allowlist import with
  `isinstance(instance_allowed, list)`, the FUNCTION imported from `app.utils`,
  not its local `instances_allowed`. The whole allowlist import had never done
  anything, on any instance, with no error.
- **D923** -- each import section commits separately and read its key with
  `contents_json[...]`, so a file missing one section applied the earlier ones
  and then raised.
- **D924** -- `admin_federation_preload` re-implemented "is this instance
  banned" as a membership test against the raw rows, missing every wildcard ban
  and doing no normalisation.

R1 became **D929**. A second registration, **D930**, was added: the uploaded ban
list is written into the served media root and never deleted.

## Success criteria

- The four functions at `[]` on the **full-suite** run.
- No floor on `app/admin/routes.py` yet — slices D, E and F remain.
- `app/utils.py`'s floor (73) must not fall.
- P1 and P2 land with their pins inverted, including a row for the crash.
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D920**; `tests/README.md` facts from **364**.

All met: `6702 passed, 3 skipped, 258 warnings`, `All 63 module floors met.`
