# Sub-project 49: feed.py's wiring layer — the last module in app/shared

**Status:** design approved, ready for an implementation plan
**Branch:** `blentz`, at `5250ab72`
**Predecessor:** sub-project 48, which closed `app/shared/community.py` at its
ceiling (99.714, floor 99, 27 floors total) with zero production changes.

## Goal

Cover Group A of `app/shared/feed.py` — the wiring layer beneath the feed
lifecycle — and repair three production defects found while scoping it, two
inside the target module and one in the route that calls it.

`app/shared/feed.py` is **the last unfloored module in `app/shared`**. Every
other file in the tier now carries a floor. This round is the first of three
that finish the tier.

## THE FACT THAT SHAPES EVERY TASK IN THIS ROUND

**No test anywhere imports `app.shared.feed`.**

```
$ /usr/bin/grep -rln "shared.feed\|shared import feed" tests/
(none)
```

There are **ten** test files with "feed" in the name — `test_feed_sorts.py`,
`test_feed_cache.py`, `test_feed_visibility_filters.py`,
`test_feed_boost_visibility.py`, `test_feed_display_preferences.py`,
`test_feed_private_communities.py`, `test_ap_actor_json_feed.py`,
`test_apply_feed_url_rules.py`, `test_factories_feed.py`,
`test_subscribed_feed_microblogs.py` — and **not one of them touches this
module.** The module's 14.711% is import-time execution of `def` statements and
decorators, not coverage produced by any test.

This is the campaign's **sixth consecutive oracle trap** and the largest. Every
task must MEASURE THE ORACLE BEFORE USING IT. A test file named after a module
is not evidence that it covers it, and in this module's case ten such files are
evidence of nothing at all.

## Targets

Per-function, from the delivered tree's `--cov=app` JSON at `5250ab72`:

| function | line | missing statements | missing arcs |
|---|---|---|---|
| `_feed_add_community` | `:386` | 20 | 9 |
| `_feed_remove_community` | `:438` | 37 | 20 |
| `announce_feed_add_remove_to_subscribers` | `:504` | 24 | 8 |
| `announce_feed_delete_to_subscribers` | `:569` | 19 | 8 |
| `existing_communities` | `:612` | 1 | 0 |
| `form_communities_to_ids` | `:616` | 12 | 8 |
| **total** | | **113** | **53** |

113 statements sits inside the campaign's 110-130 pace. 53 arcs is just under
the 56-72 band; the round is taken at that size deliberately rather than padded,
because the group has a real boundary and widening it to reach 56 would pull in
a function whose callers are not yet covered.

### Why this group, and why bottom-up

The module is layered, and the layering constrains the order:

- `make_feed:223` calls `form_communities_to_ids`; `:226` calls
  `_feed_add_community`.
- `edit_feed:344` calls `existing_communities`; `:345` `form_communities_to_ids`;
  `:350` `_feed_add_community`; `:353` `_feed_remove_community`.
- `delete_feed:372` calls `announce_feed_delete_to_subscribers`.
- `_feed_add_community:405`, `:424` and `_feed_remove_community:498` all call
  `announce_feed_add_remove_to_subscribers`.

Covering the top layer therefore *executes* the bottom layer whether or not the
bottom layer is tested. Taking the bottom first means rounds B and C inherit a
tested foundation, and it means this round's coverage is produced by tests aimed
at these functions rather than incidentally by tests aimed at their callers.

The three-round split for the module:

| round | functions | missing |
|---|---|---|
| **A — this round** | the six above | **113 / 53** |
| **B** | `join_feed`, `leave_feed`, `make_feed`, `delete_feed` | 116 / 52 |
| **C** | `edit_feed` alone | 105 / 48 |

## Callers, verified with `/usr/bin/grep -rn` over `app/`

- `app/api/alpha/utils/feed.py:7` imports `leave_feed, join_feed, make_feed,
  edit_feed, delete_feed`; `:166` calls `make_feed(input_data, SRC_API, auth)`
  and `:202` `edit_feed(input_data, feed, SRC_API, auth)`.
- `app/feed/routes.py:27-28` imports `join_feed, _feed_add_community,
  announce_feed_delete_to_subscribers, edit_feed, form_communities_to_ids,
  make_feed, delete_feed`; `:350` calls
  `_feed_add_community(community_id, current_feed_id, feed_id, user_id)`.
- `app/community/routes.py:66` and `app/api/alpha/utils/community.py:17` import
  `leave_feed` only.
- **`announce_feed_add_remove_to_subscribers` has no caller outside this
  module.** Its only call sites are `:405`, `:407`, `:424`, `:426`, `:498`,
  `:500`, all within `feed.py`.

`make_feed:226` and `edit_feed:350` both pass `current_feed_id=0`, so the
`:389` branch is reached in production **only** through
`app/feed/routes.py:350`, which takes `current_feed_id` from the query string.

## THE THREE PRODUCTION CHANGES

Each is pinned first — tests asserting the broken behaviour, passing — and then
inverted, in the pattern sub-projects 43 through 47 used. A pin that cannot be
made to fail after the fix has not proved the fix load-bearing.

### P1 — `_feed_add_community:430` reads `current_user` where `user_id` is a parameter

```python
429    current_membership = CommunityMember.query.filter_by(user_id=user_id, community_id=community_id).first()
430    if current_membership is None and current_user.feed_auto_follow:
```

The function's signature is
`_feed_add_community(community_id, current_feed_id, feed_id, user_id)`. Line
`:429` correctly uses the parameter. Line `:430` reads the request-global
`current_user` instead.

**The module gets this right everywhere else** — `:49`, `:87`, `:135` and `:455`
all read the preference off a resolved `user` model. `:430` is the lone
outlier, and it is the only such site with `user_id` already in scope.

**Reachable, two ways.** On `SRC_API` (`app/api/alpha/utils/feed.py:166`,
`:202`) authentication is a bearer token resolved by `authorise_api_user` and
there is no logged-in `current_user`, so `current_user.feed_auto_follow` raises
`AttributeError` on `AnonymousUserMixin` — a 500 on an ordinary API call. Where
a session cookie does exist, it silently reads the wrong user's preference.

**Fix:** resolve the user from `user_id` and read the preference from it.

### P2 — `announce_feed_add_remove_to_subscribers:550-555` ignores `feed_auto_follow`

```python
550            if fm_user.is_local():
551                # user is local so lets auto-subscribe them to the community
552                from app.community.routes import do_subscribe
553                actor = community.ap_id if community.ap_id else community.name
554                do_subscribe(actor, fm_user.id, joined_via_feed=True)
555                continue
```

Every local feed member is subscribed to the community unconditionally. The
user preference that exists to govern exactly this is not consulted.

**Proved by its own twin.** `app/activitypub/routes.py:1440` performs the same
operation on the federated path and reads
`if fm_user.is_local() and fm_user.feed_auto_follow:`. One implementation
checks the preference and the other does not.

**Fix:** add the `feed_auto_follow` check, matching the twin.

### P3 — `app/feed/routes.py:334-350` has no ownership check on the acting user

```python
341    user_id = int(request.args.get('user_id'))
342    feed_id = int(request.args.get('new_feed_id'))
343    current_feed_id = int(request.args.get('current_feed_id'))
344    community_id = int(request.args.get('community_id'))
345
346    # make sure the user owns this feed
347    if Feed.query.get(feed_id).user_id != user_id:
348        abort(404)
349
350    _feed_add_community(community_id, current_feed_id, feed_id, user_id)
```

The guard at `:347` compares the **caller-supplied** `user_id` against the
feed's owner. It never compares either value against `current_user.id`.
`@login_required` establishes only that someone is signed in. Supplying another
user's id together with that user's own feed id satisfies the guard.

Three consequences, all reachable:

1. `_feed_add_community:435` calls `do_subscribe(actor, user_id,
   joined_via_feed=True)`. `do_subscribe` (`app/community/routes.py:828`) acts
   on the `user_id` it is given and does not re-validate it. The victim is
   subscribed to a community the caller chose.
2. P1 is what makes that fire: `:430` consults the **caller's**
   `feed_auto_follow`, which defaults to `True` (`app/models.py:1042`).
3. `current_feed_id` is never ownership-checked at all. `:390-392` looks up a
   `FeedItem` by `(current_feed_id, community_id)` — both caller-supplied — and
   deletes it, then `:396-399` decrements that feed's `num_communities`. This
   removes entries from feeds the caller does not own, and where no such row
   exists it reaches the `db.session.delete(None)` crash registered below.

The route is a `GET`, so it is also reachable by cross-site request forgery.

**Fix:** require the acting user to be `current_user`, and check ownership of
`current_feed_id` as well as `feed_id`. **Fixing P1 alone does not close this** —
the missing authorization is in the route, and a spec that implies otherwise
would be worse than one that says nothing.

**Precedent for fixing rather than registering:** sub-project 44 repaired a live
authentication bypass the moment it was found. This is the same class.

## Registered, not fixed — with the reason for each

1. **`.first()`-then-`db.session.delete(None)` at two sites.**
   `_feed_add_community:390-392` and `_feed_remove_community:439-440` both take
   `.first()` and pass the result straight to `db.session.delete`. A miss yields
   `db.session.delete(None)`. Same family as **D643** and **D614**.
   **Why not fixed:** P3's repair removes the caller-controlled route to the
   miss, so what remains is a latent internal-consistency failure rather than a
   reachable one, and deciding whether a miss should be silent or loud is a
   behaviour question this round has no basis to settle.

2. **`:547` uses the request session inside a task that opened its own.**
   `announce_feed_add_remove_to_subscribers:544` calls `get_task_session()`, and
   `:558` uses it — but `:547` reads `User.query.get(fm.user_id)` off the
   request session, as do `:506` and `:508`. Its twin
   `announce_feed_delete_to_subscribers:596` correctly uses
   `session.query(User)`. **Why not fixed:** the two functions disagree and the
   right resolution is to make both use the task session throughout, which
   touches lines this round's tests will pin; changing session ownership inside
   a Celery task is a correctness change deserving its own round.

3. **`_feed_remove_community:458`/`:486` — `proceed` is a tautology.**
   `proceed = True` at `:458`, never reassigned, read at `:486` as
   `if proceed:`. The False arm is unreachable. **This is not a defect**; it is
   the shape **D578** proposed and is handled under the taxonomy section below.

4. **`:463` hardcodes an instance domain.**
   `if community.instance.domain == 'ovo.st':` selects a different `follow_id`
   for one named remote instance. Registered as a maintenance hazard.

5. **`existing_communities:612-614` annotates `-> List` and returns a
   `ScalarResult`.** `db.session.execute(text(...)).scalars()` is a live
   iterator bound to the session, not a list. Consumed at `edit_feed:344`.
   Registered; the annotation is wrong and the iterator's session-lifetime
   coupling is a trap for round C.

6. **`feed_auto_leave`'s default disagrees between model and form.**
   `app/models.py:1043` declares `default=True`; `app/user/forms.py:78` declares
   `default=False`. A user row created outside the settings form gets `True`,
   while the form presents `False`. `feed_auto_follow` agrees at `True` in both
   (`models.py:1042`, `forms.py:77`). Registered as a divergence, not repaired,
   because which default is intended is a product question.

## The taxonomy edit: enact D578's shape as CAUSE 9

**D578** proposed a fact 75 shape it did not enact: *an invariant that makes a
condition a TAUTOLOGY and strands its FALSE arm* — the mirror image of cause
4(b), whose text strands a **True** arm. It has stood unenacted for three rounds
on three sites, all in `app/shared/auth.py` (`:54`, `:74`, `:111`).

`_feed_remove_community:486` is a **fourth instance and the first outside
`auth.py`**, which is the threshold the campaign has used before: cause 6 stood
on two instances, cause 7 on one.

### It is numbered 9, NOT 4(c), and the reason is recorded

4(c) is where the shape taxonomically belongs — it is clause-level, like causes
1-5. It is being numbered 9 anyway, because:

- **`tests/README.md` fact 251 is titled "FACT 75 HAS NO CAUSE 4(c), AND THE
  LABEL THAT DOES NOT EXIST WAS CITED…".** Its entire thesis is the
  nonexistence. Enacting 4(c) would not amend fact 251, it would falsify it.
- Eleven further sites assert the nonexistence in passing — five in
  `tests/README.md` (`:7551`, `:7554`, `:7569`, `:7592`, `:7792`) and seven in
  the register.
- **A stale `4(c)` citation would stop being obviously wrong.** Today a reader
  who meets `4(c)` knows it is an error. After enactment it would look valid
  while pointing at the wrong shape.

### The header must be rewritten, or cause 9 creates a new ambiguity

Fact 75 is **organised by syntactic unit**, and its header says so: causes 1-5
are clause-level, 6 and 8 statement-level, 7 the expression-arm case. A reader
can currently infer the unit from the number. Cause 9 is **clause-level**, so
numbering it 9 breaks that inference.

**The header sentence must be rewritten to state the unit of each cause
explicitly rather than by grouping**, so that nobody reads "9 > 8" as "a new
syntactic unit". Enacting cause 9 without this trades fact 251's contradiction
for a subtler one, which is not a trade worth making.

### D577's citation is corrected on its own merits, not rescued

`tests/test_shared_tasks_send_reply.py:1584` reads:

> `# tests/README.md fact 75, cause 4(c) -- a handler for an exception the callee`
> `# cannot raise on this path.`

That is **cause 8**, the unreachable handler, described in cause 8's own terms.
It was never the tautology shape, so enacting either number would have left it
wrong. Correct it to cause 8, correct the same label at `findings.md:7189`, and
close **D577**.

## Shapes the tests must handle

- **`:404`/`:423`/`:497` are `if current_app.debug:` / `else: .delay(...)`
  forks** — the sync-versus-async shape sub-project 45 met at
  `join_community:36-40`. Both arms need cover at all three sites, and the
  `.delay` arm must be asserted as *dispatched*, not as executed.
- **`announce_feed_add_remove_to_subscribers` and
  `announce_feed_delete_to_subscribers` are `@celery.task`-decorated.** Call the
  underlying function directly; a test that goes through `.delay` is testing the
  broker.
- **`:559` and `:603` are three-operand conditions** —
  `instance.inbox and instance.online() and not instance_banned(instance.domain)`.
  Each operand needs isolating, and the two sites are twins whose divergence is
  registered above, so they must be covered separately rather than by one shared
  helper.
- **`:455` is a four-operand conjunction** —
  `user.is_local() and user.feed_auto_leave and cm.joined_via_feed is not None
  and cm.joined_via_feed`. The third and fourth operands are near-duplicates;
  establish whether `is not None` is subsumed by the truthiness test, and if it
  is, say which fact 75 cause fits or that none does.
- **`_feed_remove_community` iterates every `CommunityMember`** while
  `_feed_add_community` acts on one user. Removing a community from a feed
  un-follows *every* local member with `feed_auto_leave`; adding one subscribes
  only the acting user. Cover the loop with more than one member, and with a
  member who fails the guard.
- **`form_communities_to_ids:619`** does `form_communities.strip().split('\n')`,
  so an empty string yields `['']` and the function then searches for
  `'!@<SERVER_NAME>'`. Cover the empty input.
- **`:621-624`** prefix and suffix normalisation — a bare name, a name already
  carrying `!`, a name already carrying `@host`, and one carrying both.
- **`cache.delete_memoized` mutants at `:492-493` are unkillable** under
  `tests/conftest.py:68`'s `CACHE_TYPE = 'NullCache'` (**D602**, **D589**). Do
  not build assertions on them.
- **Patch by rebinding on `app.shared.feed`** — `send_post_request`,
  `get_task_session`, `instance_banned`, `do_subscribe`, `search_for_community`,
  `community_membership`, `gibberish`. A `from ... import` binds into the
  importing module's globals.
- **`do_subscribe` and `search_for_community` are imported INSIDE the functions**
  (`:432`, `:552`, `:617`) to break an import cycle. A module-level rebind will
  not take: patch where the deferred import resolves.

## The method this round inherits

Sub-project 47 shipped five mechanism-(e) lockstep gaps; 48 shipped two more,
both found by reviewers rather than implementers. The corrections stand:

1. **Key the decoupling table by BRANCH SITE, not condition name.**
2. **Record OBSERVABILITY, not merely reached-with-value.** A blank means
   unreached, not False.
3. **Build the table over EVERY test in the file, not only the task's own.**
   Sub-project 48's table covered 8 of the 19 tests that reached its targets and
   the under-scoping hid nothing only by luck.
4. **The mutation pass sweeps {site} × {and, or} × {values} exhaustively.** An
   implementer's own pairs are the ones it already thought about.

**And one new to this round, from sub-project 48's `user.id == community.id`:**
a fixture that mints its rows in lockstep makes id-pairing assertions blind.
This module's functions take `community_id`, `feed_id`, `current_feed_id` and
`user_id` — **four** id parameters, several passed adjacently. Every test
asserting on an id pairing must mint decoys so no two of the four coincide, and
pin the separation with live asserts.

## Success criteria

- All six functions at `missing_lines []` / `missing_branches []`, measured on
  the full-suite `--cov=app` run, with any line ruled unreachable carrying a
  **named** fact 75 cause and a proof — or an explicit statement that none fits,
  per **fact 252**.
- **NO floor for `app/shared/feed.py` this round.** It is one group of three,
  and a floor set now would ratchet against work not yet written. Record the
  measured percentage in the register instead. **27 floors, unchanged.**
- The three production changes land with pins inverted and every control still
  passing. `git diff --numstat <base> HEAD -- app/` names exactly
  `app/shared/feed.py` and `app/feed/routes.py`.
- No regression: `post.py`, `reply.py`, `user.py`, `domain.py`, `site.py` all
  still 100.0 with `[]`/`[]`; `app/shared/community.py` still 99.714 with
  `[699]` / `[[698, 699]]`, its proven ceiling.
- Full suite green; floors check chained with `&&` and **both** arguments
  against a `--cov=app` JSON whose printed mtime matches the run.
- A mutation pass over the six functions, reported **scoped to what was
  mutated** as D602 does, with an exhaustive conjoined sweep, and zero survivors
  or each survivor carrying a proof.
- **Cause 9 enacted**, fact 75's unit-ordering sentence rewritten, D577's
  citation corrected to cause 8 at both sites, D577 and D578 closed.
- Findings registered from **D654**; `tests/README.md` facts from **277**.

## Environment

Unchanged and binding: no host Python with flask or pytest, so everything runs
through `./run_tests.sh`; there is no `--exec` flag; `compose.test.yaml:67`
bind-mounts `./:/app:z` so repo files ARE shared with the container and only
`/tmp` is not; coverage takes the DOTTED module form and a path form silently
collects nothing, writes no JSON and exits 0; write coverage JSON outside the
repo, where it lands in the container's `/tmp`; `run_tests.sh:83` runs
`flask db upgrade` before every invocation; `pytest` exits 1 on session timeout
and a pipeline eats the status, so read `${PIPESTATUS[0]}` or do not pipe; the
suite needs `-o session_timeout=1800` and **`pytest.ini` must NOT be edited**;
`tests/check_coverage_floors.py` takes TWO arguments and counts a floored module
absent from the report as 0.0.

**Only the controller runs the full suite, one pytest session at a time, and
NEVER kill a running one** — teardown will not run and the test database is left
corrupt; recover with `./run_tests.sh --down`. **Sub-project 48 established that
this binds the controller's SCHEDULING, not merely the subagents' behaviour: a
single-file run by a subagent is still a pytest session.** A full-suite run that
overlapped a subagent's single-file run produced five failures across five
unrelated files and had to be discarded.

**D610** records the harness baseline: 287-385s normal band, with 402s and 409s
measured under load in sub-project 48. Read the output before assigning a cause.

**A suite count is only true of the tree it ran on.** Take the figure after the
final review's fix round, not after the task the plan numbers last. Sub-project
48 re-ran the suite three times for this reason and the register carries the
last one.

**Never run an unanchored replace over the register.** Sub-project 48 rewrote
eleven unrelated historical entries with a global two-word substitution and
caught it only by reading the diff. Anchor every edit to a unique long string
asserted to occur exactly once, or address it by line number against the commit
object.
