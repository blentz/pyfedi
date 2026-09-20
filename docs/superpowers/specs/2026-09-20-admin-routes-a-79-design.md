# Sub-project 79: `app/admin/routes.py` slice A — the admin landing page, and a guard nobody had

**Status:** slice A complete. Three production defects fixed (D898, D899,
D900); the design anticipated one. See the findings ledger, D898-D911.
**Branch:** `blentz`, at `ec192c784`
**Predecessor:** sub-project 78, which closed the admin API — 63 floors.

## Goal

Begin `app/admin/routes.py` — the highest-authorization-density module in the
repository (160 constructs, 1400 missed statements, 15.93%) — with the slice
that contains a real access-control defect, and add the blueprint-wide test
that would have caught it.

`app/admin/routes.py` is 1760 statements and will take several rounds. The split
follows its own seams; this round takes the entry points:

| function | gaps |
|---|---|
| `admin_home` | 46 |
| `admin_misc` | 16 |
| `admin_instance_chooser` | 19 |

`admin_site` (151 gaps) is slice B, the federation group (~430) slices C and D,
the community and topic group (~180) slice E, and the user/content group (~130)
slice F.

## THE PRODUCTION CHANGE

### P1 — the admin landing page has no permission check

```python
@bp.route('/', methods=['GET', 'POST'])
@login_required
def admin_home():
```

Every other route on this blueprint carries a `@permission_required(...)`.
`admin_home` carries none, and it renders:

- `os.getloadavg()` — the host's load averages
- `os.cpu_count()` or `NUM_CPU` — the host's core count
- `shutil.disk_usage(path)` as a percentage — **the host's disk usage**
- the loaded plugin list and every registered hook
- the LibreTranslate endpoint's language list
- every `CronJobLog` row that is overdue, by name and last-run time

**Probe**, as a verified account with no roles and no permissions:

```
PROBE n1 has change instance settings: False
PROBE n1 GET /admin/ status: 200
PROBE n1 disk_usage leaked: Storage used: 51.18%
PROBE n1 num_cores leaked: 32
PROBE n1 load averages leaked: True
PROBE n1 plugins leaked: True
PROBE n2 anonymous status: 302 /auth/login?next=%2Fadmin%2F
```

So anonymous visitors are correctly sent to the login page and **every
registered account** gets the instance's infrastructure telemetry. On a site
with open registration that is anyone who signs up.

**The intended audience is not in doubt.** `app/templates/base.html:268` shows
the entire Admin menu — including the link to this page — behind
`{% if current_user.is_admin_or_staff() %}`. The UI already states the rule; the
route does not enforce it.

**Fix:** `if not current_user.is_admin_or_staff(): abort(403)`, which is the
check the template already uses, so nobody who can currently see the link loses
anything. Not `@permission_required('change instance settings')`: that would
exclude staff who hold only `manage users` or `administer all communities`, who
legitimately reach this page today.

## The durable artefact

A single test enumerates **every rule on the admin blueprint** from
`app.url_map`, logs in as an ordinary verified account with no roles, requests
each one, and asserts none answers 200. `admin_home` is the route it would have
caught; the point is the ones added later.

Parameterised rules get a plausible id substituted. Rules that accept only POST
are exercised with POST. The row lists any route that answers 200 rather than
asserting a count, so a failure names the hole.

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 (now D909) | `app/admin/routes.py:95-101` | **The admin home contacts the configured LibreTranslate endpoint on every page load**, inside a bare `except Exception: pass`. A slow or hostile endpoint delays the page for as long as the HTTP client's timeout allows, and a failure is invisible. | A caching and timeout decision about an outbound dependency. |
| R2 (now D910) | `app/admin/routes.py:115` | **`flash(_(message))` interpolates the overdue-task list into the string BEFORE gettext sees it**, so the catalogue is asked for a string containing this instance's task names and never matches — D815's shape, in a different file. | Pre-existing; the same fix as D815 but in a slice this round does not otherwise touch. Named so it is counted. |

## What the slice actually found, beyond the design

Two further production defects that the design did not anticipate, both in
`admin_misc`:

- **D899** -- `SiteMiscForm` and `CloseInstanceForm` both named their button
  `submit`, and the view binds both forms to one request body, so a
  misc-settings Save with any text in the closing-announcement box took the
  close-the-instance branch: ten-year federation pause, registration closed,
  Danger Zone confirmation skipped. Fixed by renaming to `close_submit`.
- **D900** -- `read_posts_cutoff` was an `IntegerField` with no validators and
  the route casts it with `int()`, so a submission omitting the key raised
  `TypeError`. Fixed with `InputRequired()` + `NumberRange(min=0)`.

R3 from the design is registered as **D911**: `get_site_as_dict()` dereferences
`site.__table__` with no nil check, so an instance with no `Site` row cannot
serve any request -- which is why `admin_misc`'s fresh-instance path is covered
by calling the view directly.

## Success criteria

- The three functions at `[]`/`[]` on the **full-suite** run.
- **No floor yet**: `app/admin/routes.py` stays unfloored until the last slice,
  as `app/tag/routes.py` did between 66 and 68 — an interim floor is taken only
  where the measured figure is meaningful, and here it would rise every round.
- P1 lands with its pin inverted; the blueprint-wide guard test passes.
- Suite green; floors checked with `&&`; a mutation pass over the three
  functions, anchors checked first — D833.
- Findings registered from **D898**; `tests/README.md` facts from **349**.
