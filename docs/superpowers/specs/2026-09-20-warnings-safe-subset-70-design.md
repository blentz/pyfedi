# Sub-project 70: the warnings, safe subset — two latent defects and a deprecation

**Status:** design approved, ready to implement
**Branch:** `blentz`, at `0bfb2e9c8`
**Predecessor:** sub-project 69, which closed `app/errors` — 49 floors.

## Why this round exists

The standing goal is "100% test coverage with 0 errors and 0 warnings". Sixty-nine
sub-projects have pursued the first and ignored the third. The suite reports
**8,373 warnings** across **430 distinct sites**, and the count has grown every
round (7,768 at sub-project 64, 7,857 at 65, 8,326 at 68).

Measured breakdown of the 430 sites:

| where | class | sites |
|---|---|---|
| `app/` | `LegacyAPIWarning` (`Query.get()`) | 206 |
| `tests/` | `LegacyAPIWarning` (`Query.get()`) | 206 |
| `tests/` | `DeprecationWarning` | 8 |
| third party | `DeprecationWarning` | 4 |
| `app/` | `DeprecationWarning` | 3 |
| `app/` | **`SAWarning`** | **2** |
| third party | `LegacyAPIWarning` | 1 |

This round takes the part that is **not** a mechanical rename: the two
`SAWarning`s, which are a latent defect SQLAlchemy says may become an error, and
the sixteen `datetime.utcnow()` calls. The 412-site `Query.get()` migration is
sub-project 71 and has its own plan.

## THE CAP ON "ZERO", MEASURED

`get_or_404` is Flask-SQLAlchemy's own method and its implementation calls the
legacy API:

```python
def get_or_404(self, ident, description=None):
    rv = self.get(ident)
```

— `flask_sqlalchemy/query.py:30`, version **3.1.1** against SQLAlchemy
**2.0.52**. Every one of `app/`'s **171** `get_or_404` call sites therefore emits
a `LegacyAPIWarning` from inside a dependency. No rewrite of our own `.get()`
calls reaches zero while those remain. **Decided: sub-project 71 replaces all 171
with `db.session.get(Model, id) or abort(404)`** rather than filtering the
warning, so the zero is real rather than suppressed.

## THE TWO PRODUCTION CHANGES

### P1 — a NULL primary key handed to `.get()`, which SQLAlchemy says may become an error

```python
for follower in followers:
    user_details = session.query(User).get(follower.remote_user_id)
    if user_details:
        payload['cc'].append(user_details.public_url())
```

`app/shared/tasks/deletes.py:209` and `app/shared/tasks/pages.py:345`, the same
four lines in both.

```
SAWarning: fully NULL primary key identity cannot load any object.
This condition may raise an error in a future release.
```

`UserFollower.remote_user_id` is a **nullable** FK (`app/models.py:3568`), and a
row with it unset is reachable — `tests/test_shared_tasks_deletes.py:1043`
already seeds one deliberately and pins that the fan-out skips it. So the code
is correct today **only because `.get(None)` happens to return `None`**, which is
exactly what the warning says will stop being true.

**Fix:** skip the row explicitly, before the query. The behaviour is unchanged
today, the warning goes, and the intent stops depending on a deprecated
accident.

### P2 — `datetime.utcnow()`

Sixteen literal sites: six reachable in `app/`, eight in `tests/`, two in
`app/nntp/` (registered below).

```
DeprecationWarning: datetime.datetime.utcnow() is deprecated and scheduled for
removal in a future version.
```

The repo already has the replacement. `app/models.py:41`:

```python
def utcnow(naive=True):
    if naive:
        return datetime.now(ZoneInfo('UTC')).replace(tzinfo=None)
    return datetime.now(ZoneInfo('UTC'))
```

`utcnow()` returns **naive UTC**, which is byte-for-byte what `datetime.utcnow()`
returns, so the substitution changes no value and no comparison against the
naive datetimes in the database. Three files — `app/shared/tasks/notes.py`,
`groups.py`, `pages.py` — already import and use it beside the deprecated call.

**Fix:** use `utcnow()`. NOT `datetime.now(UTC)`, which returns an **aware**
datetime and would raise `TypeError: can't compare offset-naive and offset-aware
datetimes` against every stored column.

## The ratchet this round adds

Coverage has a floor file that only rises. Warnings have had nothing, which is
how 430 sites accumulated unnoticed. `tests/test_no_deprecated_apis.py` adds the
mirror: a **ceiling** per deprecated pattern, seeded at the count this round
leaves behind, which may only fall. Sub-project 71 drives the `Query.get()`
ceiling to zero; until then the ceiling stops it growing.

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `app/nntp/nntpserver.py:295`, `app/nntp/server.py:722` | Two more `datetime.datetime.utcnow()` calls. | **Neither module is imported by the suite** — both measure 0.0% — so they emit no warning in the run this round is reducing, and the change would be unverifiable. `nntpserver.py` is a generic NNTP implementation with no app imports; coupling it to `app.models` for this is the wrong trade. Both belong to the `app/nntp` sub-project. |
| R2 | third party | 4 `DeprecationWarning` (`ldap3`/`pyasn1` `tagMap`/`typeMap`, `httpx`, `botocore`) and 1 `LegacyAPIWarning` (`flask_sqlalchemy`). | Not ours to fix. The flask-sqlalchemy one is addressed by 71's `get_or_404` decision; the rest would need a dependency bump, which is not a coverage round's business. |

## Success criteria

- Both `SAWarning` sites gone, with the existing skip behaviour still pinned.
- Zero `datetime.utcnow()` in `app/` outside `app/nntp/`, and zero in `tests/`.
- `tests/test_no_deprecated_apis.py` enforces both, plus a ceiling on the
  `Query.get()` count for 71 to drive down.
- Suite green at 6287+; floors checked with `&&`; **the warning count measured
  before and after**, which is this round's actual deliverable.
- Findings registered from **D845**; `tests/README.md` facts from **329**.
