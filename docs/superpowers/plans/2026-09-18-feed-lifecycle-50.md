# Sub-project 50 implementation plan: `app/shared/feed.py` Group B

**Design:** `docs/superpowers/specs/2026-09-18-feed-lifecycle-50-design.md`
**Base:** `blentz` at `584d7525`
**Targets:** `join_feed` (`:25-111`), `leave_feed` (`:113-150`), `make_feed`
(`:152-227`), `delete_feed` (`:356-384`) — **116 missing statements, 52 missing
arcs** on the full-suite `--cov=app` JSON at `2dcc2e80`.

## Global Constraints

- **Everything runs through `./run_tests.sh`.** There is no host Python with
  flask or pytest, and there is no `--exec` flag. `compose.test.yaml:67`
  bind-mounts `./:/app:z`, so repo files are shared with the container.
- **One pytest session at a time per worktree.** A single-file run by a subagent
  is still a pytest session (sub-project 48). The controller schedules.
- **Never kill a running session.** Teardown will not run and the test database
  is left corrupt. After a mid-test kill, `./run_tests.sh --down`; after a
  before-collection kill, leave the warm stack alone (D610, D666). After any
  killed run, check for orphaned pytest processes before retrying.
- **Coverage takes the dotted module form.** `--cov=app.shared.feed` measures;
  `--cov=app/shared/feed.py` silently measures nothing and exits 0.
- **Never read a coverage figure off the terminal summary.** Filter the JSON in
  Python on the function's line range, the method D654 used.
- **`pytest.ini` must not be edited.** Both full runs in scoping finished inside
  its `session_timeout = 600` (355.25s, 342.05s), so no override is assumed.
- **`.coveragerc` is tracked and must not gain `dynamic_context`.** Per-test
  contexts go through an untracked `.coveragerc.ctx` whose `[json] output` is
  NOT `coverage.json`. Delete it when the measurement is taken.
- **Every mutant is applied singly, marked `# MUT`, and hand-reverted**, with
  `git show HEAD:app/shared/feed.py | diff - app/shared/feed.py` proved empty
  before the next one.
- **Never run an unanchored replace over the register.** Anchor every edit to a
  unique long string, asserted to occur exactly once.

## THE NAME COLLISION — read this before writing a single import

`tests/factories.py:156` defines **`make_feed`**. `app/shared/feed.py:152`
defines a different **`make_feed`**, and this round is the one that finally
tests the production one. Import the factory under an alias so the bare name
always means production, exactly as sub-projects 48 and 49 did:

```python
from app.shared.feed import delete_feed, join_feed, leave_feed, make_feed
from tests.factories import make_feed as make_feed_factory
```

`make_feed_item`, `make_feed_member`, `make_feed_join_request` and
`make_local_feed` have no production counterpart and are imported plainly.

## THE ORACLE — measured, not assumed

Before this file exists:

| function | executing test contexts |
|---|---|
| `join_feed` | 8, all `tests/test_redirect_back.py::BackSiteContract` |
| `leave_feed` | **0** — `:113` only, its own `def` |
| `make_feed` | **0** — `:152` only, its own `def` |
| `delete_feed` | 3, all `tests/test_redirect_back.py::TestFeedDeleteRedirect` |

Re-derive it yourself before trusting it:

```bash
cd /home/blentz/git/pyfedi
/usr/bin/grep -rln "shared.feed\|shared import feed" tests/
```

Expected: `tests/test_shared_feed_wiring.py` alone (sub-project 49's file, which
tests Group A).

**The eleven redirect tests are this round's regression tripwire.** Task 9 runs
`tests/test_redirect_back.py` explicitly rather than trusting the full suite to
surface a change in them.

## Fixture facts, verified at source during scoping

- `make_feed_factory(instance, name='peerfeed', public=False, local=False,
  with_keys=False)` (`tests/factories.py:156`) sets `ap_id`, `ap_domain`,
  `ap_profile_id`, `ap_public_url`. It does **not** set `user_id`,
  `num_communities`, `ap_following_url`, `ap_inbox_url` or `private_key`.
  `delete_feed:365` compares `feed.user_id != user_id`, so **assign `user_id`
  explicitly in every `delete_feed` test** or the owner check is meaningless.
- `make_local_feed(name='localfeed', public=False)` (`:193`) builds a feed with
  `ap_id` None — the only kind `join_feed`'s **local** arm can resolve, because
  `:34` filters `Feed.query.filter_by(name=actor, ap_id=None)`.
- `make_feed_member(user, feed, is_owner=False)` (`:223`),
  `make_feed_item(feed, community)` (`:215`),
  `make_feed_join_request(user, feed)` (`:248`).
- `make_community_member(user, community, is_moderator=False)` (`:384`) does
  **not** set `joined_via_feed`; the column defaults False
  (`app/models.py:3518`). P1's repaired guard reads exactly that column, so
  every P1 test sets it explicitly.
- `User.feed_auto_follow` and `User.feed_auto_leave` both default **True**
  (`app/models.py:1042-1043`). Never rely on the default in an assertion; set
  the column in the test and say which way.
- `web_ctx(app, user, query_string='')` (`:1217`) pushes a request context and
  logs `user` in. `bearer(user)` (`:1228`) returns a JWT header value.
- **The id-1 admin trap.** `app/models.py:1259-1261` returns True from
  `is_admin()` for id 1, and `tests/conftest.py:131-132` resets every sequence
  after each test, so the first row minted is deterministically id 1. Burn the
  seat with a bystander, and assert the burn.
- **P3 needs a real admin.** Mint the admin as the FIRST user so it takes id 1
  and `is_admin()` is true, or set the role explicitly — the plan's Task 2 says
  which, and asserts `is_admin()` live either way rather than assuming.

## Patch targets — where each name actually resolves

Module-level imports at `app/shared/feed.py:11-22`, so patch on
**`app.shared.feed`**: `send_post_request`, `find_actor_or_create`,
`make_image_sizes`, `RsaKeys`, `get_request`, `task_selector`, `process_upload`,
`authorise_api_user`, `leave_community`, `feed_membership`, `gibberish`,
`is_image_url`, `markdown_to_html`, `piefed_markdown_to_lemmy_markdown`.

**`do_subscribe` is imported INSIDE the function** at `:38`, inside the local
arm's `if`, and the remote arm at `:82-92` reuses that binding, to break an
import cycle. A rebind on
`app.shared.feed` will not take: patch **`app.community.routes.do_subscribe`**.

`_feed_add_community` and `form_communities_to_ids` are defined in this module
and are Group A's, already covered. `make_feed:223` and `:226` call them; patch
them on `app.shared.feed` so this round asserts dispatch instead of re-testing
Group A through `make_feed`.

## Shapes that will bite

- **`join_feed` ends with `finally: db.session.remove()`** (`:109-110`). Every
  ORM object the test holds is detached afterwards — a scoping probe hit
  `DetachedInstanceError` doing exactly this. **Re-query after every `join_feed`
  call**; never read a seed object.
- **`join_feed` catches `Exception`, rolls back and re-raises** (`:106-108`).
  The rollback arm needs its own test and the re-raise must be asserted as the
  same exception type, not as "an exception".
- **`make_feed` calls `RsaKeys.generate_keypair()`** (`:182`). Patch it and
  assert the keys land on the row.
- **`make_feed`'s web arm takes a FORM, not a dict.** Build a stub with
  `SimpleNamespace(url=SimpleNamespace(data=...), ...)` covering `url`, `title`,
  `public`, `description`, `nsfw`, `nsfl`, `communities`, `is_instance_feed`,
  `show_child_posts`, `parent_feed_id`.
- **`delete_feed`'s `abort(404)`** (`:365-366`) must be asserted as
  `werkzeug.exceptions.NotFound`.
- **`cache.delete_memoized` is unobservable** under `tests/conftest.py`'s
  `CACHE_TYPE = 'NullCache'` (D602, D589). Assert the call, never the effect.
- **Four id parameters travel together.** Mint decoys so `feed.id`,
  `community.id`, `user.id` and any `current_feed_id` are pairwise distinct, and
  assert the separation live (D653, fact 272).

## File Structure

| file | responsibility |
|---|---|
| `tests/test_shared_feed_lifecycle.py` | **Create.** Every test this round writes. |
| `app/shared/feed.py` | **Modify**: `leave_feed` (P1, P2, P4's return), `make_feed` (P3). No other function changes. |
| `app/api/alpha/utils/community.py` | **Modify** once: `post_community_leave_all`'s `user_id` initialisation (P4). |
| `tests/README.md` | **Modify.** New facts from the next free number. |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` | **Modify.** Findings from **D672**. |

---

## Task 1: P1 and P2 — `leave_feed`'s two divergences from its web twin

**Pin first.** Two tests that pass against the CURRENT code and state the
defect as the claim:

1. `test_leave_feed_crashes_on_a_community_the_user_never_joined` — feed member,
   one `FeedItem`, **no** `CommunityMember`, `feed_auto_leave = True`, patched
   `task_selector`. Assert `pytest.raises(NoResultFound)`. The scoping probe got
   `NoResultFound No row was found when one was required`.
2. `test_leave_feed_leaves_the_join_request_row_behind` — feed member plus a
   `FeedJoinRequest`, `feed_auto_leave = False`. After the call, assert the
   request row still exists **and** that `Feed.subscribed(user.id)` now returns
   `SUBSCRIPTION_PENDING`. The second assertion is what makes this a user-facing
   defect rather than a stray row: assert the consequence, not the residue.

**Then fix**, in `leave_feed`:

- Replace the bare `leave_community(...)` call at `:139` with the twin's guard
  (`app/feed/routes.py:637-639`): look the `CommunityMember` up with `.first()`
  and act only when it exists **and** `joined_via_feed` is true.
- Delete the user's `FeedJoinRequest` rows for that feed alongside the
  `FeedMember` delete at `:127`.

**Then invert both pins**, keeping the original claim in the docstring, struck,
with the new claim beside it — the campaign's house style.

**Controls that make the inversions kills rather than vacuous passes:**

- A second community in the same feed that the user DID join via the feed, whose
  `leave_community` call must still fire. Without it, "no exception" is
  satisfied by the loop never calling anything.
- A third community joined NOT via the feed (`joined_via_feed = False`), which
  must NOT be left. That row is the one the `joined_via_feed` half of the guard
  is for, and it is the only thing that distinguishes the two halves.
- For P2: a second user with their own join request on the same feed, whose row
  must survive. A `.delete()` missing its `user_id` filter passes every
  single-user test.

**Mutation notes for this task.** `:141` `src == SRC_WEB and not bulk_leave` is
a two-operand conjunction; both operands need isolating. The new guard is
another. Task 10 sweeps them exhaustively; write the tests so the sweep has
something to find.

## Task 2: P3 — `make_feed` accepts `is_instance_feed` from anyone

**Pin first**, on BOTH arms, because the design's fix treats them differently
and a pin on one arm proves nothing about the other:

1. `test_make_feed_api_lets_a_non_admin_mint_an_instance_feed` — non-admin user,
   payload `is_instance_feed: True`, `authorise_api_user` patched to return that
   user. Assert the created `Feed.is_instance_feed is True`, and assert
   `not user.is_admin()` live in the same test.
2. `test_make_feed_web_arm_lets_a_non_admin_mint_an_instance_feed` — the stub
   form with `is_instance_feed.data = True`, `web_ctx` logging in a non-admin.
   Same assertion. The route's `render_kw = {'disabled': True}`
   (`app/feed/routes.py:51`) is a browser-side hint and constrains nothing
   server-side; this test is what says so in executable form.

**Then fix.** API arm: refuse loudly — raise with a message naming the field.
Web arm: coerce to `False`. **Both arms must consult the same predicate**;
write it once and call it twice, so the two arms cannot drift the way this
module's twins already have.

**Then invert**: the API test becomes `pytest.raises`; the web test asserts the
row is created with `is_instance_feed False` — **created**, not refused, because
the web arm coerces. Add an admin control on each arm asserting the field is
still honoured for an admin; without it the fix is indistinguishable from
deleting the feature.

## Task 3: P4 — the `UnboundLocalError` and the asymmetric return

**Pin first:**

1. `test_leave_all_raises_unbound_local_error_for_an_account_that_joined_nothing`
   — call `app.api.alpha.utils.community.post_community_leave_all` with
   `authorise_api_user` patched. Assert `pytest.raises(UnboundLocalError)`. The
   probe's message: `cannot access local variable 'user_id' where it is not
   associated with a value`.
2. `test_leave_feed_returns_nothing_on_the_api_path` — assert
   `leave_feed(feed, SRC_API, auth) is None` while
   `leave_community(..., SRC_API, ...)` returns the user id
   (`app/shared/community.py:79`). One test, both halves, so the asymmetry is
   the claim.

**Then fix**, both halves:

- `leave_feed` returns `user_id` when `src == SRC_API`, matching its twin.
- `post_community_leave_all` binds `user_id` from the already-authorised user
  before its loops.

**Then invert.** Add the case the first pin cannot reach: an account that HAS
joined a feed, asserting the returned view carries the right user id rather than
`None`. That is the silent half of the defect and no exception marks it.

## Task 4: `delete_feed` — 6 statements, 6 arcs

Smallest target; do it first among the pure-coverage tasks so the seed helpers
are exercised early.

Tests:

- The **not-owner** arm: `feed.user_id` set to someone else, assert
  `werkzeug.exceptions.NotFound`, and assert the feed row still exists
  afterwards — an `abort` that fired after the deletes would still raise.
- The **public** fork at `:370-374`, both arms of `current_app.debug`: with
  debug true the task runs inline (patch
  `announce_feed_delete_to_subscribers` on `app.shared.feed` and assert the call
  args are `(user_id, feed.id)`); with debug false assert `.delay` was
  **dispatched**, not executed.
- A **non-public** feed: no announce at all, in either debug state. Two rows of
  a parametrisation, not one, or the `feed.public` guard is never isolated.
- `num_communities > 0` and `== 0`, asserting in both cases that no `FeedItem`
  rows survive — scoping proved the ORM removes them regardless
  (`PROBE C8c ... orphan FeedItem rows: 0`), so the assertion is about the
  guard's arc, and the docstring says so rather than implying the guard is
  load-bearing.
- The `SRC_API` arm, `authorise_api_user` patched, asserting the id it returns
  is the one the ownership check uses: give the API user a different id from the
  feed's owner in a second test and assert the 404.

## Task 5: `leave_feed` — the rest of its 24/16

After Task 1 and Task 3 land. Remaining shapes:

- **Owner refusal, both arms**: `SRC_API` raises `Exception("You cannot leave
  your own feed")` — assert the message, since a bare `Exception` is otherwise
  indistinguishable from any other failure; `SRC_WEB` flashes and returns None,
  asserting the `FeedMember` row SURVIVES. A test that only checks "no
  exception" passes against a function that deleted the row.
- **The `int` versus `Feed` preamble** (`:114-118`): both arms, asserting the
  same effect from both argument shapes.
- **The third preamble path** — an argument that is neither leaves `feed_id`
  unbound. No caller can produce it (`app/api/alpha/utils/feed.py:137` passes a
  `Feed`, `:189` a `Feed`, `app/community/routes.py:2578` a `Feed`, and the
  `int` arm is reachable from none of them today). **Decision: test it as a
  contract** with `pytest.raises(UnboundLocalError)` and register the arc as
  covered rather than declaring it unreachable — it is reachable from the
  function's own signature, which types it `int | Feed`.
- **`bulk_leave` true and false**, crossed with `src`: `:131` and `:141` are
  separate guards on the same flag and a single test collapses them.
- **`subscriptions_count` decrement** asserted with a starting value that is not
  1, so an assignment mutant (`= 0`) is distinguishable from the decrement.

## Task 6: `make_feed` — 52 statements, 10 arcs

The largest target, and the one where an inattentive test asserts nothing: this
function's work is almost entirely field assignment.

- **Both arms** (`SRC_API` dict, `SRC_WEB` stub form), asserting every field
  that is not a straight copy: `name`/`machine_name` from `url`,
  `description` through `piefed_markdown_to_lemmy_markdown`, `description_html`
  through `markdown_to_html`, `show_posts_in_children` from `show_child_posts`,
  the five `ap_*` urls, `ap_domain`, `subscriptions_count = 1`, `instance_id = 1`.
- **The idempotence claim**: the web arm converts the description at `:171` and
  `Feed(...)` converts it again at `:184`. Assert with a description containing
  `\r\n` that the doubled conversion produces the SAME string as one conversion
  — by execution, not by reading the regex. If it does not, that is a defect
  this round found late and it is registered, not quietly fixed.
- **`parent_feed_id` present and absent** (`:198-201`), asserting `None` in the
  absent case rather than "falsy".
- **The two image blocks** (`:203-214`), each in three states: url absent, url
  present but `is_image_url` false, url present and true. Patch `is_image_url`
  and `make_image_sizes`; assert `make_image_sizes` args include the file id
  that was just committed, and mint a decoy `File` first so the id is not 1.
- **The owner membership**: `FeedMember(is_owner=True)` for the creating user —
  assert `is_owner` explicitly. Sub-project 49's D661 item (F) is exactly this
  shape: a `.delay` tuple nobody asserted.
- **The community fan-out at `:223-226`**: patch `form_communities_to_ids` to
  return two ids and assert `_feed_add_community` is called once per id with
  `(id, 0, feed.id, user.id)` — all four positional arguments, with decoys
  minted so no two of them coincide.
- **`RsaKeys.generate_keypair` patched**, with the returned pair asserted onto
  the row.

## Task 7: `join_feed` — the local arm

- **Feed not found** → `abort(404)`, asserted as `NotFound`.
- **Already a member** → the else arm at `:96-99`, asserting no `FeedMember` row
  is added and that the flash fires only for `SRC_WEB`.
- **Fresh local join**: `FeedMember` row created, `subscriptions_count`
  incremented from a non-1 starting value, the three `cache.delete_memoized`
  calls made (assert the calls; NullCache makes the effect unobservable).
- **`feed_auto_follow` true and false**, and within true, both arms of
  `current_app.debug` at `:54-57` — the `.delay` arm asserted as dispatched.
  Two `FeedItem` communities, so the loop is a loop.
- **`success is True` at `:94`** — its False arm is unreachable (R4, fact 75
  **cause 9**: `success` is assigned True at `:39` and never reassigned). State
  the stranded arc in the test file's module docstring and in the register; do
  not chase it.

## Task 8: `join_feed` — the remote arm

- **Remote feed found by `ap_id`** (`:31-33`), with `'@' in actor`.
- **`FeedJoinRequest` minted** (`:62`), asserting the row's `user_id`/`feed_id`
  and that the Follow's `id` carries **that row's uuid** — capture the uuid
  before the call, the way sub-project 49 had to (`552bc690`).
- **`feed.instance.online()` true and false**: false means no Follow, no
  `get_request`, and — this is the part worth asserting — **the membership is
  still committed**, which is the current behaviour and is surprising enough to
  need pinning.
- **`send_post_request` args**: url, activity, `user.private_key`,
  `user.public_url() + '#main-key'`. **Assert `args[2]` and `args[3]`.**
  Sub-project 49's headline finding (D663) was that of three
  `send_post_request` call sites at 100% coverage, the signing credentials were
  asserted at exactly one. This is a fourth site; do not add to that tally.
- **The following-collection loop** (`:82-92`): a community that resolves, one
  that resolves to a non-`Community` (`:85`'s `isinstance` half), and one that
  resolves to `None` — three rows, because `:85` is a two-operand conjunction
  and a single row leaves one operand free.
- **R2's pin**: a collection whose key is `orderedItems` raises `KeyError:
  'items'` (`:82`). Pin it as CURRENT behaviour with the consequence asserted —
  the membership and join request are already committed when it raises, so the
  test asserts both rows exist after the raise. Registered, not fixed.
- **The rollback arm** (`:106-108`): make `get_request` raise, assert the same
  exception type propagates.

## Task 9: Floors and the full suite — CONTROLLER ONLY

```bash
./run_tests.sh tests/ -q --cov=app --cov-report=json && \
podman-compose -f compose.test.yaml exec -T test-runner \
    python tests/check_coverage_floors.py coverage.json coverage_floors.ini
```

- Run it as ONE `&&` chain. A red suite followed by a green ratchet is what the
  `&&` exists to prevent.
- Then run `./run_tests.sh tests/test_redirect_back.py -q` explicitly and record
  its count. Those eleven tests are the only pre-existing tests that execute any
  of this round's targets.
- Record, from the same JSON, filtered in Python: the four functions'
  `missing_lines`/`missing_branches`; `app/shared/feed.py`'s percentage; and the
  six no-regression figures (`post.py`, `reply.py`, `user.py`, `domain.py`,
  `site.py` at `100.0` `[]`/`[]`; `community.py` at `99.714` `[699]` /
  `[[698, 699]]`; Group A of `feed.py` at `[]` / `[[487, 493]]`).
- **No floor is taken for `app/shared/feed.py` this round.** Group C closes the
  module and takes the floor. Assert `coverage_floors.ini` unchanged with
  `git diff --numstat <base> HEAD -- coverage_floors.ini` returning empty and
  `/usr/bin/grep -c '^app/' coverage_floors.ini` returning 27.
- **Take the suite figure AFTER the final fix round**, not after the task the
  plan numbers last. A suite count is only true of the tree it ran on.

## Task 10: Mutation pass

Scope: the four target functions. Report **scoped as D602 requires** — how many
in-scope executed statements carry at least one mutation, and list the ones that
carry none individually rather than folding them into a denominator.

**The conjoined sweep is exhaustive over `{site} × {and, or} × {operand drops}`,
not over a list of pairs the implementer thought of.** Group B's conjunction
sites, located by content and re-derived before use:

| site | expression | operands |
|---|---|---|
| `join_feed:85` | `community and isinstance(community, Community)` | 2 |
| `join_feed:94` | `success is True and src == SRC_WEB` | 2 (one operand is R4's tautology) |
| `leave_feed:141` | `src == SRC_WEB and not bulk_leave` | 2 |
| `make_feed:203` | `icon_url and is_image_url(icon_url)` | 2 |
| `make_feed:209` | `banner_url and is_image_url(banner_url)` | 2 |
| P1's new guard | `membership and membership.joined_via_feed` | 2 |

Sub-project 49 was the campaign's first clean conjoined sweep (31/31 killed).
Keep that: an unkillable operand is a finding, not a nuisance, and it gets a
named fact 75 cause or an explicit statement that none fits (fact 252).

Every survivor is either closed or registered with a named closing test **that
was executed**, not merely derived — see Task 11 for why that qualifier is now
in the plan.

## Task 11: D661's eight carried-forward recipes — EXECUTE each one

Sub-project 49 registered twenty surviving Group A mutants across eight findings
(D661, items D-K), each with a closing test written out. **Seven of the eight
recipes were derived from the mutant and never run.** The eighth, item (K), was
run at 49's final review and failed `assert 1 == 0` against the unmutated tree,
because the recipe was off by one.

For each of D661's items D through K:

1. Apply the mutant it names, singly and marked `# MUT`.
2. Write the recipe's test verbatim as D661 states it.
3. Run it against the **unmutated** tree first. If it fails there, the recipe is
   wrong: register the failure with the output quoted, and correct it.
4. Run it against the mutant. It must fail. If it passes, the recipe does not
   close what it claims and that is a finding.
5. Revert the mutant by hand and prove the tree clean before the next.

Items that hold are closed in `tests/test_shared_feed_wiring.py` (Group A's
file, not this round's). Items that do not are re-registered with the executed
evidence. **Either outcome is a deliverable**; silence is not.

## Task 12: Register the findings

- New entries from **D672** in
  `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`: the
  measurement; P1-P4 each as a defect with its probe output; R1-R7 as
  registered-not-fixed with their reasons; the three killed candidates, so they
  are not raised again; the mutation tally, scoped; Task 11's outcomes.
- New facts in `tests/README.md` from the next free number: the contexts-run
  recipe (`.coveragerc.ctx`, why its `[json] output` must not be
  `coverage.json`); `join_feed`'s `db.session.remove()` detaching the caller's
  rows; the `make_feed` name collision, which now bites in two files.
- **Quote the basis with every figure.** "This-file-alone" and "full suite" are
  different claims and D640 is the entry that shows what conflating them costs.

## Self-review

Before calling the round done:

1. `git diff --numstat <base> HEAD -- app/` names exactly `app/shared/feed.py`
   and `app/api/alpha/utils/community.py`. Anything else is scope creep.
2. Every pin has been inverted and its docstring carries the original claim,
   struck, next to the new one.
3. Every `file:line` citation written this round was checked against the file at
   the commit that writes it. The campaign's dominant failure mode is a
   confident citation to a line that does not say what it is claimed to say.
4. Every figure carries its basis.
5. Name, for each new test, the production change that would make it fail. Where
   it is cheap, make that change and watch it fail.
6. The four functions read `[]`/`[]` on the FULL-SUITE JSON, except arcs
   declared unreachable with a named cause — R4's `:94` is the one already
   known.
7. `tests/test_redirect_back.py` still passes, run on its own.
8. No orphaned `.coveragerc.ctx`, no probe file, no `# MUT` marker, and
   `git status` clean but for the intended files.
