# Sub-project 52: `app/feed/routes.py` Group A — the lifecycle routes

**Status:** design approved, ready for an implementation plan
**Branch:** `blentz`, at `e82cdd98`
**Predecessor:** sub-project 51, which closed `edit_feed`, took
`app/shared/feed.py`'s first floor at 99 and with it **closed `app/shared`** —
28 floors, one per module in the tier.

## Goal

Cover the feed **lifecycle routes** — `feed_new`, `feed_edit`, `feed_delete`,
`feed_notification` and `feed_unsubscribe` — and repair the four production
defects found while scoping them.

This is the layer directly above the module the last three rounds covered, and
it is where three findings already registered against `app/shared/feed.py`
point: **D657** (the add-community route's missing authorization, repaired in
sub-project 49), **D696** (the rename guard that exists only in the browser),
and the `feed_unsubscribe` twin whose guards **D673** and **D674** were repaired
against.

## Targets

From the delivered tree's full-suite `--cov=app` JSON at `e82cdd98`, filtered in
Python per function:

| route | lines | missing statements | missing arcs |
|---|---|---|---|
| `feed_new` | `:41-78` | 28 | 18 |
| `feed_edit` | `:125-185` | 45 | 22 |
| `feed_delete` | `:190-204` | 1 | 1 |
| `feed_notification` | `:316-332` | 9 | 2 |
| `feed_unsubscribe` | `:593-660` | 32 | 20 |
| **total** | | **115** | **63** |

115 statements sits inside the campaign's 110-130 pace and 63 arcs inside the
56-72 band — the first round in a while that lands in both without argument.

The module as a whole reads **32.272** over 485 statements, with 298 missing.
The remaining two thirds are scoped as future groups, not padded into this one:

| round | routes | missing |
|---|---|---|
| **A — this round** | the five above | **115 / 63** |
| **B** | `feed_copy`, `feed_add_remote`, `lookup` | 109 / 61 |
| **C** | `show_feed`, `feed_list`, `feed_create_post`, `show_feed_rss`, `get_all_child_feed_ids` | 74 / 46 |

**No floor is taken this round.** `app/feed/routes.py` is being closed in three,
and a floor on one third ratchets against the other two — the rule sub-projects
49 and 50 followed for `app/shared/feed.py`.

## THE ORACLE — measured, and it is not zero this time

Eleven tests in `tests/test_redirect_back.py` execute these routes today, and
they are the reason `feed_delete` shows **1 missing statement** and `subscribe`
shows none at all. Their subject is `back()`'s referrer policy: they assert
where the user is sent, not what the route did. `TestFeedDeleteRedirect`
asserts the feed row is gone as well, which is why `feed_delete` is all but
closed before this round starts.

**What that means for this round, stated before any test is written:** the
routes with real gaps are the ones whose bodies do work the redirect tests never
look at — the form pre-fill in `feed_edit`, the slug handling in `feed_new`, the
whole community sweep in `feed_unsubscribe`. Treat the eleven as a tripwire and
run that file on its own at the end, exactly as sub-projects 50 and 51 did.

## THE FOUR PRODUCTION CHANGES

Each was reproduced by execution in a throwaway probe before being written here.

### P1 — `feed_edit:178-181` pre-fills NSFL from the NSFW column

```python
if g.site.enable_nsfl is False:
    edit_feed_form.nsfl.render_kw = {'disabled': True}
else:
    edit_feed_form.nsfw.data = feed_to_edit.nsfw      # <- nsfl, twice over
```

The `else` arm of the **NSFL** branch assigns **`nsfw.data`** from
**`feed_to_edit.nsfw`**. `nsfl.data` is therefore never populated, so the edit
form always renders the NSFL box unchecked — and since the form round-trips,
**saving any edit to an NSFL feed clears its NSFL flag.**

**Probe:** a feed row with `nsfw=False, nsfl=True` rendered through the route
gave `PROBE form.nsfw.data: False form.nsfl.data: False`.

**Fix:** `edit_feed_form.nsfl.data = feed_to_edit.nsfl`. The pin asserts the
form's data for both flags, with the row carrying **different** values for them,
because a fixture with both flags equal cannot tell the two columns apart.

### P2 — `feed_new` accepts NSFW/NSFL that the site forbids

`:46-49` disables the two widgets when the site has NSFW or NSFL turned off.
That is a browser-side hint; nothing checks the submitted values, and
`make_feed` writes them unconditionally — unlike `edit_feed:377-380`, which
guards both writes behind `g.site`.

**Probe:** with `enable_nsfw` and `enable_nsfl` both False, a POST carrying
`nsfw=y, nsfl=y` returned `302` and produced `feed nsfw/nsfl: (True, True)`.

**The same shape as D675 and D696**, and the third time the campaign has found
a `render_kw = {'disabled': True}` standing in for a server-side rule.

**Fix:** refuse the flags server-side in the route, where `g.site` is already
being read for the widgets, rather than in `make_feed` — the shared function is
floored and closed, and the API path has its own site policy question that this
round is not scoped to answer. **Registered as R1**: the API create path
(`app/api/alpha/utils/feed.py`) still writes both flags with no site check.

### P3 — `feed_new?topic_id=<missing>` is a 500

`:67-68` does `Topic.query.get(request.args.get('topic_id'))` and then reads
`topic.communities` with no check.

**Probe:** `PROBE topic exception: AttributeError 'NoneType' object has no
attribute 'communities'`.

**Fix:** treat a missing topic the way the rest of the file treats a missing
feed — a 404 rather than a 500. The query string is user-supplied and the link
that carries it comes from a page the user may have had open while the topic was
deleted.

### P4 — `feed_unsubscribe:640-643` unsubscribes from communities without telling anyone

The route deletes the `CommunityMember` row directly:

```python
if membership and membership.joined_via_feed:
    db.session.query(CommunityMember).filter_by(...).delete()
```

The shared `leave_community` (`app/shared/community.py:57-84`) does two more
things: it dispatches `task_selector('leave_community', ...)`, which federates
the Undo Follow, and the counter work that goes with leaving. The route does
neither, so:

**Probe** — user in a remote community joined via the feed, community
`subscriptions_count` seeded to 5:

```
PROBE unsub status: 302 send calls: 0
PROBE unsub community members left: 0
PROBE unsub community subscriptions_count: 5
```

The membership is gone locally, **the remote community was never told**, and its
subscriber count is left one too high. The user goes on receiving nothing while
the remote side goes on believing they follow it.

**Fix:** call `leave_community(...)` per community instead of deleting the row
by hand, which is what `leave_feed` does after **D673**'s repair — the two
unsubscribe paths then agree, and the guard **D673** added protects this caller
too.

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `app/api/alpha/utils/feed.py:145-166` | **The API create path has no site NSFW/NSFL policy either.** P2's repair lands in the web route, where `g.site` is already read; the API arm reaches `make_feed` without consulting the site at all. | Putting the check in `make_feed` would settle it for both, but `app/shared/feed.py` is floored and closed, and the API's error contract is the same open question D681 registered for duplicate urls. Registered together so one round can answer both. |
| R2 | `app/feed/routes.py:55-56` | **The `/f/` prefix strip is DEAD CODE.** `form.url.data[3:]` runs only when the stripped, lower-cased url starts with `/f/` -- and `apply_feed_url_rules` (`app/utils.py:4750-4762`) rejects any url containing a slash before the route is reached, on both the public regex (`^[a-zA-Z0-9_]+$`) and the private one (`^[a-zA-Z0-9_]+(?:/<username>)?$`, which allows a slash only before the owner's own name). Probed on both arms: `{'url': [Feed urls can only contain letters, numbers, and underscores.]}`. | Deleting production code is not this round's remit, and the lines are harmless. Declared as **fact 75 cause 5, unreachable data**, with the two statements and their arc left in the round's residual list and the proof recorded, rather than covered by a test that fakes the precondition. |
| R3 | `feed_notification:314-331` | **A GET route with a side effect and no ownership check**: any logged-in user toggles their own notification subscription for any feed, through a GET, with no CSRF protection. The subscription is the caller's own, so this is not a privilege bug; it is a CSRF-able state change. | Changing a route's method is a template-and-URL change across the app, not a coverage change. Pinned as current behaviour. |
| R4 | `feed_unsubscribe:631` | **`feed.subscriptions_count -= 1` has no floor**, so a count that has drifted below 1 goes negative. The same shape as `leave_feed`'s, which this campaign has already covered without repairing. | Consistent with the existing treatment; registered so the eventual repair fixes both sites at once. |

## Shapes the tests must handle

- **These are ROUTE tests.** `tests/test_redirect_back.py:32-56` has the idiom:
  `login(client, user)` writes `_user_id` into the session, and `csrf(app,
  client)` mints a token/session pair, which POST routes need because
  `app.utils.login_required` calls `validate_csrf` directly and ignores
  `WTF_CSRF_ENABLED`.
- **`pytestmark = pytest.mark.usefixtures('site')`.** These routes read `g.site`,
  which `before_request` populates for a real request — unlike the bare
  `test_request_context` that fact 291 is about.
- **Rendering must be patched for any GET that re-renders a form.** The feed
  templates raise `jinja2.exceptions.UndefinedError: ... has no attribute
  'csrf_token'` under the test config, which disables CSRF for forms. Patch
  `app.feed.routes.render_template` and assert on the `form` it was handed —
  which is also the only way to see P1 at all.
- **`AddCopyFeedForm` requires `communities`**, and a real value sends
  `form_communities_to_ids` down `search_for_community`, which attempts a live
  webfinger and trips respx. Patch `app.shared.feed.form_communities_to_ids`.
- **`feed_unsubscribe` is reached by feed NAME, not id** (`/feed/<actor>/
  unsubscribe`), through `actor_to_feed`.
- **The federation arm** sends an Undo Follow through
  `app.feed.routes.send_post_request`; patch it there, and assert the signing
  credentials — `args[2]` and `args[3]` — as sub-projects 49 and 50 had to
  (**D663**).

## Success criteria

- The five routes at `missing_lines []` / `missing_branches []` measured on the
  **full-suite** `--cov=app` run, except R2's two unreachable statements and
  their arc, which carry a named cause and a proof (fact 252).
- **No floor for `app/feed/routes.py`.** Group C closes the module. 28 floors,
  `coverage_floors.ini` unmodified.
- The four production changes land with pins inverted, `git diff --numstat
  <base> HEAD -- app/` naming exactly `app/feed/routes.py`.
- `tests/test_redirect_back.py` still passes, run on its own.
- No regression: the six `app/shared` modules at their floors, and
  `app/shared/feed.py` still `99.659` with `[]` / `[[372, 377], [549, 555]]`.
- Full suite green; floors check chained with `&&`.
- A mutation pass over the five routes, scoped as D602 requires, with an
  exhaustive conjoined sweep, and zero survivors or each survivor carrying a
  proof.
- Findings registered from **D700**; `tests/README.md` facts from **292**.

## Environment

Unchanged. Everything through `./run_tests.sh`; dotted `--cov=app.feed.routes`;
one pytest session at a time; `pytest.ini` not edited; per-test contexts via an
untracked `.coveragerc.ctx` (fact 284). The suite now runs ~360s at 5334 tests.
