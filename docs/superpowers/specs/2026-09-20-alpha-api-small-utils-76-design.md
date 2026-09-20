# Sub-project 76: the small `app/api/alpha/utils` modules

**Status:** design approved, ready to implement
**Branch:** `blentz`, at `acb3ec132`
**Predecessor:** sub-project 75, which closed three small utilities — 56 floors.

## Goal

Close the three smallest modules under `app/api/alpha/utils`, and take three
floors — 59.

| module | before | gaps | what it is |
|---|---|---|---|
| `app/api/alpha/utils/domain.py` | 25.000% | 9 | the domain block/unblock endpoint |
| `app/api/alpha/utils/topic.py` | 18.367% | 40 | the topic tree, nested |
| `app/api/alpha/utils/upload.py` | 25.000% | 33 | four image endpoints |

Nothing in `tests/` references any of the three today.

## No production change expected

None of the three shows a defect on reading, and the two shapes that looked
like one were probed and are **registered** rather than repaired — see R1 and
R2. Stated up front per sub-project 74's precedent, and qualified per 75's: the
scoping is a prediction, and if the rows turn something up it is repaired with
them.

## THE AUTH FALLBACK, MEASURED

Two of `upload.py`'s four endpoints are written:

```python
try:
    user = authorise_api_user(auth, return_type="model")
except Exception:
    if current_user.is_authenticated:
        user = current_user
    else:
        raise Exception('incorrect_login')
```

So a **bad token is silently ignored when a browser session exists**. Probed
across all three states:

```
PROBE g1 bad token + session  -> {'url': 'u.png'}, uploaded as <User user1_1>
PROBE g2 bad token, no session -> Exception incorrect_login
PROBE g3 good token            -> {'url': 'u.png'}, user 1
```

That reads as deliberate — the web UI's upload dialog calls these endpoints with
cookies and may hold a stale token — but it means a client sending a **wrong**
token is never told, so it never learns to refresh. Registered as R1 with the
probe, not changed: altering what an invalid token does is an authentication
decision, not a coverage round's.

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `upload.py:12-18`, `:48-54` | **A bare `except Exception` around `authorise_api_user` falls back to the session user**, so an invalid or expired Bearer token is accepted rather than refused whenever the caller also has a browser session. Measured above. | An authentication decision. The behaviour is pinned in all three states so the decision starts from evidence. |
| R2 | `upload.py:21-29` | **`total_size` is summed from a per-user `SELECT` over `user_file` and never used** — the quota check that consumed it is commented out at `:28-29`. Every image upload therefore runs a query whose result is discarded. | D736's family: a computation whose only consumer is gone. Deleting it would also delete the quota check's scaffolding, which the comment says is coming back. |
| R3 | `upload.py:35-44` | **`post_upload_community_image` and `post_upload_user_image` are the same three lines twice**, differing only in `destination`. | Named; both are covered. |
| R4 | `topic.py:17` | **`data['include_communities'] if 'include_communities' in data else True`** rather than `data.get('include_communities', True)`. | Cosmetic. |

## Success criteria

- All three modules at `[]`/`[]` on the **full-suite** run — the number that
  counts, per fact 339.
- Three floors of 100 — **59 floors**.
- No `Model.query.get(...)` anywhere in the new rows: the deprecated-API ratchet
  caught exactly that twice in sub-project 75, in a file written the same way.
- Suite green; floors checked with `&&`; a mutation pass over all three,
  anchors checked first — D833.
- Findings registered from **D879**; `tests/README.md` facts from **342**.
