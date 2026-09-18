# Sub-project 55: `app/feed/util.py` and `app/feed/forms.py` — closing the package

**Status:** design approved, ready for an implementation plan
**Branch:** `blentz`, at `97a56e713`
**Predecessor:** sub-project 54, which closed `app/feed/routes.py` at 99.017 and
floored it — 29 floors.

## Goal

Cover the two files the feed package has left — the helpers the routes call and
the forms they validate — repair the two defects found while scoping them, and
**take a floor on each, which closes `app/feed` entirely.**

## Targets

From the delivered tree's full-suite JSON at `97a56e713`:

| module | missing statements | missing arcs | percent |
|---|---|---|---|
| `app/feed/util.py` | 44 | 31 | 46.0 |
| `app/feed/forms.py` | 20 | 14 | 70.2 |
| **total** | **64** | **45** | |

Below the campaign's 110-130 band, taken at that size because it is what the
package has left and this round closes it.

## THE TWO PRODUCTION CHANGES

### P1 — `AddCopyFeedForm.validate` crashes when the url field is absent

```python
if self.url.data.strip() == '':      # app/feed/forms.py:30
```

A disabled or omitted input means `url.data` is **None**, not `''` — the fact
sub-project 52 had to learn to test `feed_edit`'s empty-url arm (fact 293). And
`EditFeedForm.validate:82` guards exactly this with `if self.url.data is not
None:`; its twin does not.

**Probe:** `POST /feed/new` without the field →
`PROBE nourl exception: AttributeError 'NoneType' object has no attribute
'strip'`.

Reachable from `/feed/new` and `/feed/<id>/copy`, both of which are ordinary
user-facing forms.

**Fix:** the guard `EditFeedForm` already has, so the twins agree. **This is
D712's shape again** — one of a pair repaired, the other left — except that here
neither copy was repaired and the guard exists only because `feed_edit`'s
disabled-widget case forced it.

### P2 — `search_for_feed` raises `ValueError` on an address with two `@`

```python
name, server = address[1:].split('@')   # app/feed/util.py:39
```

**Probe:** `search_for_feed('~name@host@extra')` →
`PROBE split exception: ValueError too many values to unpack (expected 2)`.

Reachable from `feed_add_remote`, whose first arm is exactly
`address.startswith('~') and '@' in address` — so a user typing `~a@b@c` into
the "add remote feed" box gets a 500.

**Fix:** split once from the right (`rsplit('@', 1)`) or reject the address with
the same "Feed not found" path the rest of the function uses. **The design's
decision: reject.** A two-`@` address is not a typo the server should guess at,
and the caller already has a not-found flash for it.

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `app/feed/util.py:60-67` | **A webfinger failure sleeps 3-10 seconds INSIDE THE REQUEST and then retries**, and on a second failure returns None silently. The sleep is `time.sleep(randint(3, 10))` on the request thread. | Moving the retry off the request is a scheduling change. Registered, and every test patches the sleep so the suite does not pay it. |
| R2 | `app/feed/util.py:70-84` | **The webfinger walk reads `feed_json['type']` and `webfinger_json['links']` without guards**, so a malformed reply is a KeyError rather than a not-found. Same silent-failure family as **D720**. | The error contract for remote replies is one decision across several modules; registered so it is made once. |
| R3 | `app/feed/util.py:108-118` | `initialise_new_communities` **breaks after the first community in debug** and dispatches for all of them otherwise -- deliberate, per its comment, and registered so the asymmetry is not read as a bug. | Not a defect. |

## Success criteria

- Both modules at `[]`/`[]` on the **full-suite** run, except arcs declared
  unreachable with a named cause and a proof.
- **Both take floors** — 31 floors — which closes `app/feed`.
- The two repairs land with pins inverted; `git diff --numstat` names exactly
  `app/feed/util.py` and `app/feed/forms.py`.
- Suite green; floors checked with `&&`; a mutation pass over both modules.
- Findings registered from **D733**; `tests/README.md` facts from **302**.
