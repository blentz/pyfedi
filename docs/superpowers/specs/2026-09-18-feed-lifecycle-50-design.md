# Sub-project 50: feed.py's lifecycle layer — Group B

**Status:** design approved, ready for an implementation plan
**Branch:** `blentz`, at `9b4dcb7f`
**Predecessor:** sub-project 49, which covered Group A of this module and shipped
three production repairs. Its three unmet success criteria were discharged at
**D671** before this round was scoped: suite green at `5219 passed, 3 skipped,
6 subtests passed in 355.25s`, `All 27 module floors met.`, and the six named
no-regression modules at their required figures.

## Goal

Cover Group B of `app/shared/feed.py` — `join_feed`, `leave_feed`, `make_feed`
and `delete_feed`, the lifecycle layer that sits on top of the wiring Group A
covered — and repair the four production defects found while scoping it, each of
which was reproduced by execution before it was written down here.

`app/shared/feed.py` is the last unfloored module in `app/shared`. This is the
second of the three rounds that finish the tier; Group C (`edit_feed`, 105/48)
is the third and the module's floor is taken there, not here.

## THE FACT THAT SHAPES EVERY TASK IN THIS ROUND

**Group B's existing coverage was produced entirely by tests that are not about
it, and two of its four functions have never been executed by any test at all.**

Measured with per-test coverage contexts over the whole suite
(`--cov=app.shared.feed --cov-context=test --cov-config=.coveragerc.ctx`, a
throwaway config; `5219 passed, 3 skipped, 6 subtests passed in 342.05s`):

| function | test contexts that execute it | what those tests are about |
|---|---|---|
| `join_feed` | 8, **all** `tests/test_redirect_back.py::BackSiteContract` | `back()`'s referrer policy |
| `leave_feed` | **0** | — |
| `make_feed` | **0** | — |
| `delete_feed` | 3, **all** `tests/test_redirect_back.py::TestFeedDeleteRedirect` | `back()`'s referrer policy |

`leave_feed` and `make_feed` have exactly one executed line each — `:113` and
`:152`, their own `def` statements, executed at import.

This is the campaign's **seventh consecutive oracle trap**, and it is a harder
one than sub-project 49's: 49's targets had *no* executing tests, so the trap was
visible as a zero. Here two functions carry partial coverage supplied by eleven
tests whose subject is the HTTP redirect that follows the call. A reader who
measured `join_feed` at 23 executed lines and concluded "partly tested" would be
wrong about every one of them. **Measure the oracle before using it, and when a
test executes a target incidentally, name what that test is actually about.**

The eleven redirect tests are also this round's regression tripwire: they are the
only existing tests that will notice a behaviour change in `join_feed` or
`delete_feed`, and they will notice only if it changes the redirect.

## Targets

Per-function, from the full-suite `--cov=app` JSON at `2dcc2e80`, filtered in
Python on the function's line range rather than read off the terminal summary:

| function | lines | missing statements | missing arcs |
|---|---|---|---|
| `join_feed` | `:25-111` | 34 | 20 |
| `leave_feed` | `:113-150` | 24 | 16 |
| `make_feed` | `:152-227` | 52 | 10 |
| `delete_feed` | `:356-384` | 6 | 6 |
| **total** | | **116** | **52** |

116 statements is inside the campaign's 110-130 pace; 52 arcs is just under the
56-72 band, and the round is taken at that size rather than padded, for the same
reason sub-project 49 was: the group has a real boundary (`edit_feed` is Group C
and pulls 105/48 of its own).

The same JSON reads `app/shared/feed.py` at **43.706**, with every Group A
statement executed and `[[487, 493]]` — D669's permanent cause-9 residual — as
the only missing arc above `:384`.

## Callers, verified with `/usr/bin/grep -rn` over `app/`

- `app/api/alpha/utils/feed.py:7` imports all four; `:135` `join_feed(feed.link(),
  user.id, SRC_API)`, `:137` `leave_feed(feed, SRC_API, auth)`, `:166`
  `make_feed(input_data, SRC_API, auth)`, `:219` `delete_feed(feed_id, SRC_API,
  auth)`.
- `app/api/alpha/utils/community.py:17` imports `leave_feed`; `:189` calls it in
  the bulk-leave loop of `post_community_leave_all`.
- `app/community/routes.py:66` imports `leave_feed`; `:2578` calls it with
  `SRC_WEB, bulk_leave=True`.
- `app/feed/routes.py:27-28` imports `join_feed`, `make_feed`, `delete_feed`;
  `:61` `make_feed(form, SRC_WEB, None, form.icon_file.data,
  form.banner_file.data)`, `:194` `delete_feed(feed_id, SRC_WEB)`, `:586`
  `join_feed(actor, current_user.id)`.

**`leave_feed` has a web twin that does not call it.** `app/feed/routes.py:593-659`
(`feed_unsubscribe`) reimplements the whole unsubscribe by hand. Three of this
round's four repairs are differences between that twin and the shared function,
and in every one of them the twin is the correct one.

## THE FOUR PRODUCTION CHANGES

Every one was reproduced by execution in a throwaway probe file before being
written here, and the probe's output is quoted. None is speculative.

### P1 — `leave_feed:135-139` unsubscribes from communities the user never joined

`leave_feed` ends by auto-leaving the feed's communities when the user has
`feed_auto_leave` set (the column's default is **True**, `app/models.py:1043`):

```python
feed_items = db.session.query(FeedItem).filter_by(feed_id=feed_id).all()
for feed_item in feed_items:
    leave_community(community_id=feed_item.community_id, src=src, auth=auth, bulk_leave=bulk_leave)
```

`leave_community` opens with
`db.session.query(CommunityMember).filter_by(...).one()`
(`app/shared/community.py:59`). `.one()` raises when there is no row, and there
is no row whenever the user is a feed member who never joined that community —
the ordinary case for anyone who joined the feed with `feed_auto_follow` off, or
who left one of its communities by hand.

**Probe:** feed member, one `FeedItem`, no `CommunityMember`, `feed_auto_leave =
True` →
`PROBE C2 exception: NoResultFound No row was found when one was required`.

**The web twin guards exactly this** (`app/feed/routes.py:637-639`):
`membership = CommunityMember.query.filter_by(...).first()` then `if membership
and membership.joined_via_feed:`.

**Fix:** look the membership up with `.first()` and skip when it is absent.
Whether the `joined_via_feed` half of the twin's guard is also adopted is a
behaviour question the plan must answer explicitly rather than by copying:
adopting it means `leave_feed` stops unsubscribing from communities the user
joined on their own, which is what the twin does and what the column exists for.
**Decision: adopt both halves**, so the two paths agree; the round pins the
current behaviour first, then inverts.

### P2 — `leave_feed` leaves the `FeedJoinRequest` row behind, and the user can never rejoin

`Feed.subscribed()` (`app/models.py:4224-4239`) returns `SUBSCRIPTION_PENDING`
when there is no `FeedMember` row **but** a `FeedJoinRequest` row exists.
`leave_feed` deletes the `FeedMember` row and nothing else. `join_feed:37` only
acts when `feed_membership(...) == SUBSCRIPTION_NONMEMBER`.

So: join a remote feed (which is the only path that mints a `FeedJoinRequest`,
`app/shared/feed.py:62`), leave it through the API, and the stale request row
pins the user at `SUBSCRIPTION_PENDING` **for that feed permanently** — every
later join attempt takes `join_feed`'s else arm and flashes "Already subscribed,
or subscription pending".

**Probe:** `PROBE C2b join requests left behind: 1`.

**The web twin deletes it** (`app/feed/routes.py:630`, the
`FeedJoinRequest ... .delete()` next to the `FeedMember` delete at `:629`).

**Fix:** delete the `FeedJoinRequest` rows for that user and feed alongside the
`FeedMember` delete.

### P3 — `make_feed` takes `is_instance_feed` from the caller, with no admin check

`make_feed:163` (API arm) reads `is_instance_feed = input['is_instance_feed']`
and `:177` (web arm) `input.is_instance_feed.data`, and passes it straight into
the `Feed` row. The only gate anywhere is cosmetic:
`app/feed/routes.py:51-52` sets `form.is_instance_feed.render_kw = {'disabled':
True}` for non-admins, which disables the widget in the browser and constrains
nothing on the server; `app/api/alpha/utils/feed.py:157` simply forwards
`data['is_instance_feed']`.

**Probe:** a non-admin user (`assert not s.member.is_admin()`) posting
`is_instance_feed: True` →
`PROBE C6 is_instance_feed: True user_id: 3 ...`.

An instance feed is surfaced in the instance-wide menu (`menu_instance_feeds`),
so this is a privilege escalation into shared UI, the same shape as sub-project
49's P3 (D657), which was also an authorization check that existed nowhere.

**Fix:** refuse `is_instance_feed` from a caller who is not an admin. The plan
decides between hard-refusing (raise/abort) and silently coercing to False;
**the design's decision is to refuse loudly on the API arm and coerce on the web
arm**, because the web form genuinely does not offer the field to non-admins and
a form POST carrying it is a forgery, while an API client can be told it asked
for something it may not have. The plan must pin the current permissive
behaviour on **both** arms before changing either.

### P4 — `post_community_leave_all` raises `UnboundLocalError`, and `leave_feed` is why it can

`app/api/alpha/utils/community.py:159-191` binds `user_id` **only** inside its two
loops:

```python
for community in communities.all():
    ...
        user_id = leave_community(..., bulk_leave=True)
for feed_id in joined_feed_ids:
    ...
        user_id = leave_feed(..., bulk_leave=True)
return user_view(user=user, variant=6, user_id=user_id)
```

A user who has joined nothing reaches the return with `user_id` never bound.

**Probe:** `PROBE C7 exception: UnboundLocalError cannot access local variable
'user_id' where it is not associated with a value`.

The second loop is also a silent downgrade of the first: `leave_community`
returns `user_id` on the API path (`app/shared/community.py:79-80`) and
`leave_feed` returns `None` on every path, so any account that owns or subscribes
to a feed has its `user_id` overwritten with `None` by the feed loop.

**Fix, in two parts.** `leave_feed` returns `user_id` for `SRC_API`, matching its
twin `leave_community`; and the caller initialises `user_id` from the
already-authorised user rather than from a loop's last return value. Either half
alone leaves a live bug: the return value does not help an account with no
memberships, and initialising alone leaves the two functions' contracts
asymmetric.

## Registered, not fixed — with the reason for each

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `make_feed:183-217`; `app/feed/forms.py:27-48` | **A duplicate feed url is a 500.** `Feed.name` is `unique=True` (`app/models.py:4093`) and nothing checks it: the form's `validate()` checks `Community` and `User` name collisions and never `Feed`. Probe: `PROBE dup exception: IntegrityError (psycopg2.errors.UniqueViolation) duplicate key value violates unique constraint "ix_feed_name"`. | The fix is a user-facing error contract in two places (a form validator and an API error code), not a coverage change. Registered with the probe so the round that takes it starts from a reproduction. |
| R2 | `join_feed:82` | **`following_collection['items']` has no fallback.** An `OrderedCollection` reply (`orderedItems`) raises. Probe: `PROBE items exception: KeyError 'items'`. The membership and the join request are already committed when it raises, so the user is left subscribed to a feed whose communities were never imported. | Choosing the accepted shapes is a federation-compatibility decision, and the round already carries four repairs. The test for the current behaviour is written this round, so the defect is pinned even though it is not fixed. |
| R3 | `join_feed:88`, against `:54-57` | **The remote arm calls `do_subscribe` synchronously**, where the local arm sixty lines above honours `current_app.debug` and otherwise calls `.delay`. A remote join with many communities runs every subscribe inline in the request. | A divergence between twin arms, registered as such; changing it changes production scheduling, not coverage. |
| R4 | `join_feed:39`, `:94` | **`success = True` is never reassigned**, so `if success is True and src == SRC_WEB` has a False arm nothing can reach: a fact 75 **cause 9** tautology, the same shape as D669's `if proceed:`. | Declared as this round's permanent residual rather than repaired, so the `[]`/`[]` claim stays honest. The plan states the arc it strands. |
| R5 | `delete_feed:382-383` | **Deleting a feed that has `FeedJoinRequest` rows is a FK violation.** Probe: `PROBE C8a exception: IntegrityError ... violates foreign key constraint "feed_join_request_feed_id_fkey"`. | **Latent, with a reachability proof**: only `join_feed:62` mints those rows and only for remote feeds, while `delete_feed:364-365` aborts 404 unless the caller owns the feed, and a local user does not own a remote feed. P2's repair narrows it further. Registered with the proof so nobody re-derives it. |
| R6 | `make_feed:191-195` | **A mixed-case url splits the actor's identity**: `ap_profile_id` lowercases the url, `ap_public_url`/`ap_followers_url`/`ap_following_url`/`ap_outbox_url` do not. Probe: `PROBE C5 ap_profile_id: .../f/mixedcase` vs `PROBE C5 ap_public_url: .../f/MixedCase`. | Latent: both current callers slugify and `.lower()` before calling (`app/feed/routes.py:57-59`, `app/api/alpha/utils/feed.py:147-149`). Group C must re-check it, because `edit_feed(from_scratch=True)` rebuilds these fields. |
| R7 | `leave_feed`, against `app/feed/routes.py:649-651` | **`leave_feed` busts no caches.** The web twin deletes the memoized `feed_membership`, `menu_subscribed_feeds` and `joined_communities` entries; the shared function deletes none, so an API unsubscribe leaves the feed in the user's menus until the entries expire. | `tests/conftest.py`'s `CACHE_TYPE = 'NullCache'` means no test can observe cache state (D602/D589), so a repair here would ship unasserted. Registered with that reason stated. |

**Candidates that scoping KILLED, recorded so they are not re-raised.** A
stale `num_communities` does **not** orphan `FeedItem` rows — `PROBE C8c ...
orphan FeedItem rows: 0`, the ORM relationship removes them regardless of the
counter the `if` reads. Deleting a parent feed does **not** fail on its children
— `PROBE C8b ... child parent_feed_id is now None`. And `make_feed`'s apparent
double application of `piefed_markdown_to_lemmy_markdown` (`:171` then `:184`)
is inert: the substitution is `(\S)(\r\n)` → `\1  \2`, so the second pass sees a
space before the newline and matches nothing. The plan verifies that
idempotence by execution rather than by reading the regex.

## Shapes the tests must handle

- **`join_feed`'s `finally: db.session.remove()`** (`:109-110`) detaches every
  instance the caller holds. A probe that read a factory row after the call got
  `DetachedInstanceError`. Tests must re-query after calling `join_feed` instead
  of reusing seed objects, and the plan says so at every call site.
- **`join_feed` swallows nothing** — it re-raises after `db.session.rollback()`.
  The rollback arm needs its own test, and the raise must be asserted as the
  same exception type.
- **`join_feed` rebinds its own `actor` parameter** inside both community loops
  (`:53`, `:86`). Nothing reads `actor` afterwards, so it is inert today; a test
  asserting on the value passed to `do_subscribe` must therefore not assume the
  parameter still holds the caller's argument.
- **Both `.delay`-versus-`current_app.debug` forks** (`:54-57` in `join_feed`,
  `:371-374` in `delete_feed`) need both arms, with the `.delay` arm asserted as
  *dispatched*, not executed — the shape sub-project 45 met at
  `join_community:36-40` and 49 met three times.
- **`leave_feed`'s `isinstance` preamble** (`:114-118`) has a third path: an
  argument that is neither `Feed` nor `int` leaves `feed_id` unbound. No caller
  does that; the plan states whether it is tested as a contract or left as an
  unreachable arc with a named cause.
- **`make_feed` calls `RsaKeys.generate_keypair()`** (`:182`) — seconds per call
  if it is real. Patch it on `app.shared.feed` and assert the keys land on the
  row; a test that lets it run is paying for entropy it never asserts.
- **`make_feed`'s two `is_image_url` blocks** (`:203-214`) call
  `make_image_sizes`; patch it, and cover url-present-but-not-an-image
  separately from url-absent, because they are different arcs.
- **`delete_feed`'s `abort(404)`** needs a request context and must be asserted
  as `werkzeug.exceptions.NotFound`, not as "an exception".
- **The `SRC_API` arms call `authorise_api_user`**, imported into
  `app.shared.feed`'s globals at `:20`. Patch it there. `leave_feed:120` calls it
  for `src == SRC_API` and reads `current_user.id` otherwise.
- **`form_communities_to_ids` and `_feed_add_community` are Group A and covered**;
  `make_feed:223-226` calls both. Patch them at `app.shared.feed` so this round's
  `make_feed` tests assert dispatch rather than re-testing Group A.
- **Four id parameters, still.** `feed_id`, `user_id`, `community_id` and
  `current_feed_id` travel together through these functions; sub-project 48
  shipped three blind assertions to lockstep ids and 49's `_seed` fixed it by
  minting decoys. Keep that discipline: mint bystanders, assert the separation
  live.

## The method this round inherits

Unchanged from sub-project 49, which was the campaign's first clean conjoined
sweep (31 of 31 killed, zero lockstep gaps), and the reason it was clean was that
the decoupling discipline was written into the briefs rather than restated:

1. Key the decoupling table by **branch site**, not condition name.
2. Record **observability**, not reached-with-value. A blank means unreached.
3. Build the table over **every** test in the file.
4. Sweep `{site} × {and, or} × {values}` **exhaustively** at every conjunction.

**And the one this round adds, from its own scoping:** when an existing test
executes a target incidentally, the round names what that test is *about* before
relying on it. Eleven redirect tests execute two of these four functions and
none of them asserts anything about feeds.

## D661's eight carried-forward findings

Sub-project 49 registered twenty surviving mutants across eight findings "carried
forward to the module's rounds B and C", each with a closing test already
written out. **Seven of those eight recipes were derived from the mutant and
never executed**; the eighth, item (K), was executed at 49's final review and
found to be wrong as written.

They are Group A findings. This round does not own them, but it is the first
round with the module's fixtures in hand, so the plan schedules one task that
**runs each recipe before trusting it**, closes the ones that hold, and registers
the ones that do not with the failure quoted. A recipe nobody executed is exactly
what produced D661's own correction.

## Success criteria

- All four functions at `missing_lines []` / `missing_branches []` measured on
  the **full-suite** `--cov=app` run, except the arcs explicitly declared
  unreachable with a **named** fact 75 cause and a proof — R4's tautology is the
  one this design already knows about, per **fact 252**.
- **No floor for `app/shared/feed.py` this round.** Group C closes the module and
  takes the floor. **27 floors, unchanged**, asserted by `git diff --numstat`.
- The four production changes land with pins inverted and every control still
  passing. `git diff --numstat <base> HEAD -- app/` names exactly
  `app/shared/feed.py` and `app/api/alpha/utils/community.py`.
- The eleven incidental redirect tests in `tests/test_redirect_back.py` still
  pass, and the plan's regression task runs that file explicitly rather than
  trusting the full suite to surface it.
- No regression: `post.py`, `reply.py`, `user.py`, `domain.py`, `site.py` all
  still `100.0` with `[]`/`[]`; `app/shared/community.py` still `99.714` with
  `[699]`/`[[698, 699]]`; Group A of `app/shared/feed.py` still `[]` with
  `[[487, 493]]` as its only arc.
- Full suite green; floors check chained with `&&` against a `--cov=app` JSON
  whose printed mtime matches the run.
- A mutation pass over the four functions, reported **scoped to what was
  mutated** as D602 requires, with an exhaustive conjoined sweep, and zero
  survivors or each survivor carrying a proof or a named closing test.
- D661's eight recipes each **executed**, with the outcome registered either way.
- Findings registered from **D672**; `tests/README.md` facts from the next free
  number.

## Environment

Unchanged and binding, and two additions from this round's scoping:

Everything runs through `./run_tests.sh`; there is no host Python with flask or
pytest. Coverage takes the **dotted** module form. One pytest session at a time
per worktree — the contexts run and the full-suite run in this round's scoping
were serialised for that reason. `pytest.ini` must not be edited; the suite's
`session_timeout` is 600 and both of this round's full runs finished inside it
(355.25s and 342.05s), so the `-o session_timeout=1800` override sub-project 49
needed was not required and is not assumed.

**New: per-test contexts need a throwaway coverage config.** `.coveragerc` is
tracked and must not gain `dynamic_context`; write `.coveragerc.ctx` (untracked),
pass `--cov-config=.coveragerc.ctx`, and delete it when the measurement is taken.
Its `[json] output` must not be `coverage.json`, or it overwrites the report the
floors check reads.

**New: after a killed run, clear orphaned pytest processes before retrying**
(D666), and apply D610's asymmetric reflex — `--down` after a mid-test kill,
leave the warm stack alone after a before-collection kill.
