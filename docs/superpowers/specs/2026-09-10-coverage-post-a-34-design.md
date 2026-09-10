# Coverage sub-project 34: `post.py` Group A, the reader interactions

**Status:** approved, not yet implemented
**Branch:** `blentz`
**Predecessor:** sub-project 33, 18 commits `daf84875..e7768168`, `app/shared/tasks/maintenance.py` closed at 100%, module floor 89 → 100

## Goal

Take the eight reader-facing functions of `app/shared/post.py` to zero missing
statements and zero missing arcs, and fix three production defects found while
reading them.

This is the first of five sub-projects against `app/shared/post.py`. It is also
the first round of this campaign that will not close its module: the module
stays well below 100 until Group E lands, so the floor this round raises is a
partial ratchet rather than a completion.

## Where this sits in the decomposition

`app/shared/post.py` is 1174 lines, 772 statements, 424 branches, and 40.55%
covered against the full suite — 445 missing statements and 266 missing arcs.
That is three times sub-project 32 and eight times sub-project 33, so the module
was decomposed into five sub-projects before any spec was written. The split is
by caller role, which is also the split by test harness:

| Group | Functions | Stmts | Arcs |
|---|---|---|---|
| **A — reader interactions** | `vote_for_post`, `bookmark_post`, `remove_bookmark_post`, `subscribe_post`, `mark_post_read`, `vote_for_poll`, `get_post_flair_list`, `extra_rate_limit_check` | 63 | 51 |
| B — moderator verbs | `lock_post`, `move_post`, `sticky_post`, `hide_post`, `mod_remove_post`, `mod_restore_post` | 110 | 56 |
| C — author lifecycle and reporting | `delete_post`, `restore_post`, `report_post` | 101 | 56 |
| D — `make_post` | | 61 | 26 |
| E — `edit_post` | | 110 | 77 |

The groups sum exactly to 445 and 266. Order is A → B → C → D → E: Group A
builds the `SRC_API`/`SRC_WEB` harness that roughly nineteen functions need, and
it builds it in the cheapest round, because five of its eight functions are
within four statements of closed already. The two heavyweight bodies come last,
when the harness and the module's idioms are mapped.

**This spec covers Group A only.** Later groups get their own spec, plan and
round.

## Scope

63 statements and 51 missing branch arcs, measured against the full suite on the
current 1174-line tree:

| Function | Lines | Missing stmts | Missing arcs |
|---|---|---|---|
| `vote_for_post` | `:31-74` | 29 | 18 |
| `vote_for_poll` | `:1146-1174` | 20 | 18 |
| `bookmark_post` | `:77-94` | 4 | 4 |
| `subscribe_post` | `:115-152` | 4 | 4 |
| `mark_post_read` | `:1115-1130` | 2 | 3 |
| `get_post_flair_list` | `:1133-1143` | 2 | 2 |
| `remove_bookmark_post` | `:97-112` | 1 | 2 |
| `extra_rate_limit_check` | `:155-160` | 1 | 0 |

Counts are **decorator-inclusive**, this campaign's convention throughout, and
were measured against the full suite rather than against a single test file. An
earlier measurement using only `tests/test_shared_post_edit.py` reported 34.7%
for the module and understated every function, because other test files reach
`post.py` incidentally. Any implementer re-measuring must use the full suite or
the per-function numbers will not reproduce.

Roughly 60 tests, against sub-project 33's 28 for 54 statements and 35 arcs.

## The harness is this round's real cost, and it is new

Every previous round in this campaign called Celery task bodies directly — no
request, no user, no session. Group A cannot do that, and the difference is
large enough to be the round's dominant risk.

### There is no `user=` escape hatch

`tests/test_shared_post_edit.py` works without login or request context because
`edit_post` accepts `user=` and checks `if not user:` on both source arms
(`:252` and `:316`), so passing a user skips `authorise_api_user` and
`current_user` alike. **No function in Group A has that parameter.**
`vote_for_post:41`, `bookmark_post:78`, `remove_bookmark_post:98`,
`subscribe_post:117` and `vote_for_poll:1150` all read `current_user` or call
`authorise_api_user` with no way around it.

So the SRC_WEB arms need a real Flask-Login session and the SRC_API arms need a
real bearer token. `tests/test_shared_post_edit.py` has one precedent for each:
`test_web_branch_falls_back_to_the_logged_in_user_when_none_is_passed` and
`test_api_branch_authorises_from_the_bearer_token_when_no_user_is_passed`. Those
two tests are the model, and `tests/factories.py:119-120` shows the
`app.test_request_context('/')` plus `login_user(viewer)` pattern the repository
already uses.

### The SRC_API arms need a request context too

This is the fact most likely to be missed. `vote_for_post:50` and
`vote_for_poll:1152` both read `if user.banned or user_ip_banned():` on the path
*after* the source fork, so both arms reach it. `user_ip_banned`
(`app/utils.py:2311-2314`) calls `ip_address`, which `app/utils.py:2308` binds to
`app/__init__.py`'s `get_ip_address`, and that reads `request`.

**There is no context-free path through either function.** A test that arranges
a bearer token and calls without a request context fails inside `user_ip_banned`,
not at the assertion.

### The WEB arms render real templates

`vote_for_post:47` and `:73` render `post/_post_voting_buttons.html` or
`post/_post_voting_buttons_masonry.html`, chosen by
`request.args.get('style', '')`. `subscribe_post:152` renders
`post/_post_notification_toggle.html`. All three files exist. They are Jinja
templates over real model objects, so a missing `g` attribute or an unseeded
relation surfaces as a template error rather than an assertion failure — the
same silent-misdirection shape earlier rounds hit with swallowed exceptions,
one layer out.

**Task 1 probes this and records what the templates actually require. No later
task may assume a template renders.**

### `task_selector` runs synchronously

Celery is eager (`tests/conftest.py:105-110`), so `vote_for_post:60` and
`vote_for_poll:1165` and `:1173` execute real task bodies in
`app/shared/tasks/likes.py` rather than enqueueing them. Task 1 records whether
those bodies return early for a local community with no followers, or whether
they need arranging. This matters twice over: it is a harness fact, and PC2's
observation depends on being able to see whether `task_selector` fired.

### `mark_post_read` imports redis inside the function body

`mark_post_read:1126` is `from app import redis_client`, written inside the
function, and `:1127` takes a lock on it. `tests/conftest.py:459-463` warns in
so many words that its `redis_double` fixture covers `app.redis_client` but that
at least one `from app import redis_client` site does that import inside a
function body rather than at module level. This is that site, or one of them.

**Probe it. Do not assume the fixture reaches it**, and record the answer either
way — `delete_post:765` does the same thing and Group C will need the answer.

## Production changes

Three. Each lands as its own commit with its own failing observation.

**Every one must be observed failing before it is fixed.** A production change
with no failing observation behind it is the same error as a test that cannot
fail, one level up.

### PC1 — `vote_for_poll` accepts a choice belonging to another poll

`app/api/alpha/utils/post.py:1791-1797` reads `choice_id` from the request body
and passes it to `vote_for_poll` unexamined. Neither `:1164` nor `:1172` checks
that the choice belongs to `post_id`.

`Poll.vote_for_choice` (`app/models.py:3794-3803`) then constructs
`PollChoiceVote(choice_id=choice_id, user_id=user_id, post_id=self.post_id)` —
`post_id` taken from the *target* poll — and increments `num_votes` on the
*foreign* choice. One API call therefore inflates any choice on any poll in the
database and stores a vote row whose `post_id` and `choice_id` disagree about
which poll was voted in.

A nonexistent `choice_id` fails differently and worse: `:1166` evaluates
`PollChoice.query.get(votes[0]).choice_text`, dereferencing `None`.

**Fix.** Filter the requested choices down to those belonging to this poll,
before any voting happens. A choice that survives the filter is voted normally;
a rejected choice **raises for `SRC_API` and is skipped silently for
`SRC_WEB`**.

That asymmetry is deliberate and is the module's own idiom, not an invention:
`:1160-1162` and `:1168-1169` both already raise for `SRC_API` and do nothing
for `SRC_WEB`. It also avoids giving `app/post/routes.py:635-646` a failure mode
it has never had — that route has no error handling and flashes
`'Vote has been cast.'` unconditionally after the call returns.

Filtering by membership closes the `None` dereference in the same stroke, since
a nonexistent choice is not a member. The implementer must confirm that rather
than assume it.

### PC2 — multi-mode federates votes that were never recorded

`Poll.vote_for_choice` is a silent no-op when `PollChoiceVote` already holds a
row for that `(user_id, choice_id)` pair — `app/models.py:3797`'s
`if not existing_vote:` guards the entire body. But `vote_for_poll:1173` calls
`task_selector('vote_for_poll', ...)` unconditionally inside the `:1171` loop.

Re-submitting the same choices to a multi-mode poll therefore changes nothing
locally and federates one phantom vote per choice. Remote instances increment
totals that the origin instance does not have.

The single-mode arm does not have this defect: `:1163`'s `if not
poll.has_voted(user.id):` guards both the vote and the `task_selector` call
together.

**Fix.** Federate only when the vote was actually created. Note this requires
knowing whether `vote_for_choice` did anything, and `vote_for_choice` currently
returns `None` in both cases — the implementer decides whether to change its
return or to determine the answer at the call site, and records which and why.
Changing `vote_for_choice`'s return affects `app/shared/reply.py` and any other
caller, so **the implementer must enumerate its callers before choosing.**

### PC3 — `vote_for_post` marks the post read before checking the vote quota

`:53` calls `mark_post_read([post.id], True, user.id)` and `:55` then checks
`if votes_cast_today(user.id) > current_app.config['VOTE_QUOTA']: abort(429)`.

A user over quota therefore has the post written to `read_posts` and
`last_seen` bumped — `mark_post_read:1118-1120` writes the row and `:1128-1130`
bumps `last_seen` and commits — and only
then receives a 429. The side effect survives the rejection.

**Fix.** Move the `mark_post_read` call below the quota check.

The ordering against `:50`'s ban check is already correct and must stay that
way: a banned user aborts before either.

## Test design

New file: `tests/test_shared_post_interactions.py`.

### Every test must be able to fail

Three rounds running, roughly ten tests across this campaign could not fail, and
every one had the same shape: an oracle reaching its assertion through a crash
swallowed by an `except`, rather than through the guard its docstring named.

Group A's version of that trap is different and needs naming separately.
**These functions have almost no `except` blocks** — the swallow shape barely
exists here. What exists instead is `abort()`: `vote_for_post:51` and `:56`,
`vote_for_poll:1153`. Outside a request context `abort` still raises, so a test
that means to assert a 403 and instead crashes earlier can look identical from
the outside if it only asserts "something raised."

**Every abort assertion must pin the status code**, and must additionally assert
that the side effect the abort was supposed to prevent did not happen — for PC3
specifically, that no `read_posts` row exists after a 429.

### Compound conditions are one arc pair to coverage.py

`vote_for_post:43-44` is a disjunction of two conjunctions:

```python
if (vote_direction == 'upvote' and not can_upvote(user, post.community)) or (
        vote_direction == 'downvote' and not can_downvote(user, post.community)):
```

Coverage.py records this as a single arc pair. Four distinct inputs are needed
to exercise its parts, and only mutation sees inside it. `:50`'s
`user.banned or user_ip_banned()` is the same shape with two conjuncts.

The spec calls these out because sub-project 32 shipped exactly this hole and
its final review found it.

### Model facts the tests depend on

- **`Poll`'s primary key is the post id.** `vote_for_poll:1158` is
  `Poll.query.get_or_404(post_id)`, and `Poll.vote_for_choice` writes
  `post_id=self.post_id`. A fixture that seeds a `Poll` with an id different
  from its `Post` will produce results that look like PC1's defect and are not.
- **`PollChoiceVote` is keyed on `(user_id, choice_id)`** for the purposes of
  `app/models.py:3795-3797`'s duplicate check — it filters on those two columns
  and ignores `post_id`. This is what makes PC1's misattributed row possible and
  what makes PC2's no-op silent.
- **`read_posts` is written by raw SQL**, not the ORM —
  `mark_post_read:1118-1120` issues an `INSERT ... ON CONFLICT DO UPDATE` and
  `:1123-1125` a `DELETE`. Assertions about read state must query the table
  directly; there is no model to inspect.
- **`mark_post_read:1116` tests `read is True`, not truthiness.** A truthy
  non-`True` value takes the DELETE branch. Tests must pass `True` and `False`
  literals and must not depend on the coercion.

### Harness facts carried from previous rounds

- **No ordered assertions over rows a query planner returned.** Compare sets.
- **An oracle over a field several arms write pins nothing.** Every assertion
  names the arm it pins.
- **`tests/conftest.py:68` sets `CACHE_TYPE = 'NullCache'`**, so
  `cache.memoize` decorators do not retain values between calls. `banned_ip_addresses`
  (`app/utils.py:2410-2411`), reached through `user_ip_banned`, carries
  `@cache.memoize(timeout=30)`; under NullCache it re-queries on every call.
- **`tests/conftest.py:262`'s session-scoped autouse `block_outbound_http`** is
  still in force. Any path here that would make a real request fails loudly.

### Coverage per function

**`vote_for_post` (`:31-74`)** — the source fork at `:32`; `:35` and `:37`'s
API-side upvote and downvote gates on both arms; `:43-44`'s compound with each
conjunct exercised separately; `:45`'s style ternary on both arms, which appears
twice (`:45` and `:72`) and must be taken on both; `:50`'s ban compound with
each conjunct separately; `:55`'s quota boundary in **both directions**; `:62`'s
return fork; `:67` and `:69`'s `undo is None` arms, including the case where
`undo` is not `None`.

**`vote_for_poll` (`:1146-1174`)** — the source fork at `:1147`; `:1152`'s ban
compound; `:1155`'s `isinstance(votes, int)` on both arms; `:1159`'s mode fork;
`:1160`'s length check with `SRC_API` raising and `SRC_WEB` falling through;
`:1163`'s `has_voted` fork with `:1168`'s API raise; the `:1171` loop over zero,
one and several choices; and, after PC1 and PC2 land, the new membership filter
on both arms and the new federation guard on both arms.

**`bookmark_post` (`:77-94`)** — `:78`'s source ternary; `:83`'s existing-bookmark
fork; `:88`'s API-versus-WEB split inside the duplicate arm; `:93`'s return fork.

**`remove_bookmark_post` (`:97-112`)** — `:101`'s fork and `:106`'s split.

**`subscribe_post` (`:115-152`)** — `:119`'s `SRC_WEB` override of the
`subscribe` argument, which means the WEB arm ignores what the caller passed;
`:124`'s `subscribe == False` fork; `:125` and `:136`'s existing-notification
forks; `:130` and `:138`'s API-versus-WEB splits; `:149`'s return fork.

**`mark_post_read` (`:1115-1130`)** — `:1116`'s read fork with both literals;
both loops over an empty list and a several-element list; the `ON CONFLICT`
path, which needs the same `(user_id, post_id)` inserted twice.

**`get_post_flair_list` (`:1133-1143`)** — `:1134`'s `isinstance(post, int)` on
both arms; `:1137`'s null-flair fork on both arms.

**`extra_rate_limit_check` (`:155-160`)** — a direct call. Its only caller is
`make_post:166`, which is Group D, so this round reaches it directly and says so
in the test's docstring.

## Verification

- **Mutation pass** over the branch points, one at a time: dry-run without `-i`
  and read the produced line, apply, run, restore, then assert an empty
  `git diff -- app/` and the expected `wc -l`. Restore before any point where
  the work might stop — a process that dies mid-probe cannot restore, and this
  campaign has had two do exactly that. **Enumerate every site in the coverage
  table above and give each a row.** Sub-project 32's pass listed a site in its
  own prose and then never mutated it, and the final review found the hole.
- **Mutate boundaries in both directions.** `:55`'s `>` against `>=` is this
  round's instance, and it is the shape that round missed.
- **Mutate the compound conditions conjunct by conjunct.** `:43-44` and `:50`
  are single arc pairs; coverage cannot see inside them and only mutation can.
- **Coverage** measured with the dotted form `--cov=app.shared.post` and
  `--cov-branch`, across `tests/test_shared_post_interactions.py` and
  `tests/test_shared_post_edit.py` together. A path form collects nothing,
  writes no JSON and still exits 0. Read `summary.percent_covered`, not
  `percent_statements_covered`. Write the JSON outside the repository — `/app`
  is bind-mounted.
- **Floor** raised from 40 to the measured integer, rounded down. **This round
  does not close the module**, so the floor lands well short of 100 and that is
  correct. Do not treat a sub-100 result as a failure to finish.
- **Full suite** run by the controller alone, foreground and unpiped, with floor
  enforcement as the separate chained step at `tests/README.md:403-405`.

## Register

Findings from D392, facts from 206.

- The three production changes, with PC2's decision about `vote_for_choice`'s
  return value and its caller enumeration recorded.
- **The harness facts**, which are the round's most reusable output: that Group A
  has no `user=` escape, that both source arms need a request context because of
  `user_ip_banned`, what the three templates require, whether `redis_double`
  reaches a function-body `from app import redis_client`, and whether the eager
  `likes.py` task bodies return early. Groups B through E all depend on these.
- **PC4, not fixed:** `:1160`'s single-mode length check is API-only, and
  `:1161`'s `if src == SRC_API:` has a `SRC_WEB` arm that falls through to
  `:1164`'s `votes[0]`. It is unreachable today because
  `app/post/routes.py:642` normalizes single mode to one int before calling. A
  change here would have no reachable failing observation, which is why it is
  registered rather than fixed.
- **`vote_for_poll` returns `None` on `SRC_API`** where every sibling in the
  module returns `user.id`. `app/api/alpha/utils/post.py:1797` ignores the
  return, so this is latent rather than live.
- **`mark_post_read:1116`'s `read is True` identity test**, which routes a
  truthy non-`True` down the DELETE branch.
- **`vote_for_post:33` uses `.get()` where `:40` uses `get_or_404`.** The API arm
  gives a 500 at `:35`'s `post.community` where the WEB arm gives a 404. This is
  one of roughly nine sites of the same asymmetry across the module —
  `delete_post:757`, `restore_post:798`, `lock_post:933` and others — so it is
  registered whole and fixed in no round that can only reach one site.
- **`subscribe_post:116` uses `.one()`**, raising `NoResultFound` for a deleted
  or missing post where the module's convention is a 404.
- **`extra_rate_limit_check` is near-duplicated** at `app/shared/reply.py:134-139`.
  Both return `False` unconditionally; the two bodies differ only in the
  docstring's noun, "posts" against "comments".
- **`app/post/routes.py:280`'s precedence bug**, outside this module and outside
  this round: `[post.id] + post.cross_posts if post.cross_posts is not None else []`
  binds the conditional to the whole expression, so a post with no cross-posts
  yields `[]` and is never marked read.
- **The decomposition itself**, so the next four rounds do not re-derive it: the
  five groups, their measured sizes, the ordering and why.

## Risks carried deliberately

- **The harness is unproven.** Every earlier round in this campaign reused a
  known harness; this one builds a new one against templates, Flask-Login and a
  bearer token. Task 1 is a probe task, not an implementation task, and the plan
  must treat a surprising probe result as more valuable than the prediction it
  contradicts.
- **PC2's fix reaches outside `post.py` if the implementer changes
  `vote_for_choice`'s return.** The spec permits either resolution and requires
  the caller enumeration first. If the enumeration shows the change is not
  contained, the call-site resolution is the one to take.
- **PC1 is the one with integrity consequences**, and a wrong filter rejects
  legitimate choices. The implementer must demonstrate, on a fixture holding two
  polls with distinct choices, that every choice belonging to the target poll is
  still voted after the change and only the foreign one is rejected.
- **The module does not close this round.** Four more rounds follow, and the
  register entry naming the decomposition is what keeps them from being
  re-scoped from scratch.
