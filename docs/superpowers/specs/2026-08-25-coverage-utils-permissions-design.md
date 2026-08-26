# Coverage Campaign 1b-i: `app/utils.py` permission and authorisation functions

Date: 2026-08-25
Status: Approved design, ready for implementation planning

Sub-project 1b-i of the campaign in
`docs/superpowers/specs/2026-08-25-coverage-campaign-foundation-design.md`.
Findings that govern how this work is reviewed are in
`docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` — read that first.

Sub-project 1a (`docs/superpowers/specs/2026-08-25-coverage-utils-pure-design.md`)
covered the pure and context-only functions and is merged.

## Why this slice, and why it is small

Measured against merged `blentz` (`fd236cc0`): `app/utils.py` is 2,844 statements,
1,241 uncovered, 50.91%. Partitioned by what each gapped function touches:

| group | functions | uncovered |
|---|---|---|
| DB-backed | 110 | 901 |
| I/O | 12 | 248 |
| other (residue from 1a's moves) | 17 | 92 |

The DB group is 2.6× the size of 1a, which took a full session and 30 commits. It is
cut into three: **1b-i permissions (this spec, ~120 statements)**, 1b-ii feed and query
machinery (~215, dominated by `get_deduped_post_ids` at 124), and 1b-iii cached lookups
and the long tail (~480).

Permissions go first because they are authorisation logic — who may post, reply, vote
and upload — and because **every task this campaign has run against security-relevant
code has found a real defect**. A permission check that is wrong in the permissive
direction is the highest-severity class remaining in this file.

Note `archive_post` (84 statements) is classified DB by the partition regex only
because it takes `s3_connection` as a parameter. It is S3 archival and belongs to 1c.

## Scope

### In scope

1. Eight functions to 100% statement and branch coverage:
   `can_create_post` (31 uncovered), `can_create_post_reply` (23), `can_downvote` (22),
   `authorise_api_user` (19), `can_upvote`, `can_upload_video`, `user_access`,
   `role_access`.
2. **Condition coverage, not merely branch coverage** — see below. This is the part
   that makes the sub-project worth doing.
3. **An inverse call-site audit**: find gated actions that never ask permission.
4. Raising `app/utils.py`'s ratchet floor to the level achieved.
5. Reporting defects; fixing only what the project owner authorises.

### Out of scope

- The other ~102 DB-backed functions (1b-ii and 1b-iii).
- `archive_post` and the I/O group (1c).
- Changing any authorisation policy. Several asymmetries below look like bugs; some
  are certainly deliberate. **This sub-project reports; it does not decide.**

## Condition coverage is the point

These functions are dense guard chains. A single line such as

```python
if user is None or community is None or user.banned or user.bot:
    return False
```

reaches 100% *branch* coverage with two tests — one where the whole expression is true,
one where it is false. That leaves three of the four sub-conditions never independently
falsified. For authorisation code that is worthless: the test suite would pass with
`or user.banned` deleted.

**Every sub-condition of every guard gets its own case**, in the direction that matters:
a user who is banned and otherwise eligible must be refused, and the identical user
unbanned must be permitted. The pair is what proves the guard is load-bearing.

Where a guard cannot be isolated — because another guard shadows it for every reachable
input — that is a finding worth reporting, not a case to skip quietly.

## Suspected defects, found while reading

Recorded now so implementers do not have to rediscover them, and so nobody encodes one
as intended behaviour. **Verify each, report, do not fix without authorisation.**

1. **`can_downvote` swallows everything.** `try: site = g.site / except: site = Site.query.get(1)`
   is a bare `except`, catching `KeyboardInterrupt` and `SystemExit` alongside the
   `AttributeError` it means. Establish what it is actually catching.
2. **`can_create_post` checks `content is None` twice** — once alone, then again in
   `if user is None or content is None or user.banned`. Harmless, but it suggests the
   guard chain was edited without being re-read.
3. **`can_upvote` lacks almost every guard `can_downvote` has** — no site check, no
   `local_only`, no attitude or reputation floor, no `downvote_accept_mode` equivalent.
   Plausibly deliberate (upvotes are freer than downvotes), but it should be recorded
   as a policy decision rather than assumed.
4. **`can_create_post_reply` lacks `can_create_post`'s new-account rate limit**
   (`created_very_recently() and post_count > 3`). Also plausibly deliberate.
5. **`can_upload_video` computes `upload_user = user or current_user`, uses it in the
   `'admins'` branch, then ignores it in the `'users'` branch**, which tests
   `not current_user.is_authenticated and user is None`. Passing any user under the
   `'users'` policy therefore returns `True` regardless of that user. This one looks
   like a genuine bug rather than a policy choice.
6. **`user_access` hardcodes `user_id == 1` as an unconditional superuser** and
   `user_id == 0` as an unconditional denial. Both bypass the `role_permission` table
   entirely. Worth stating plainly in a test so the behaviour is visible.
7. **Both `can_create_post` and `can_create_post_reply` mutate `g.site` as a side
   effect** while deciding permission. A permission check that writes to request
   globals is surprising; whether it matters depends on what reads `g.site` afterwards.

## The inverse call-site audit

The forward audit does not scale — there are 248 call sites, dominated by
`authorise_api_user` (138) and `user_access` (65). Verifying each of those is a poor
use of effort, because a site that calls the check is the case that already works.

**Audit the inverse: enumerate the gated actions, and find the ones that never ask.**

This is the shape of this campaign's largest find. `sanitize_svg` was correct while
**five of seven** upload paths never called it — the function was never the bug. The
same question applied here is: which route creates a post, creates a reply, records a
vote, or accepts a video upload *without* consulting the matching function?

Method:
1. Enumerate the actions: post creation, reply creation, vote recording, video upload,
   and the API endpoints that mutate state.
2. For each, find every entry point — web routes, API routes, ActivityPub inbox
   handlers, CLI commands, Celery tasks.
3. Record which consult a permission function and which do not.
4. For each that does not, establish whether something upstream already gated it.

**An unguarded path is a finding, not a fix.** Report it with the evidence; the project
owner decides. Note the ActivityPub inbox is the highest-risk surface, since its input
is remote-controlled and this campaign has already found four remote-triggerable defects
in that path.

Deliver the audit as a table of action × entry point × guard, so it can be re-run and
checked rather than believed.

## The quality bar

Inherited, and restated because this sub-project is authorisation code:

- **For every test, name the production change that would make it fail.** If you cannot
  name one, the test is decoration. This campaign has caught seven hollow tests, five
  of them self-caught by the worker that wrote them; that is the standard.
- Prove each guard is load-bearing by deleting it and watching a named test fail.
- Tests assert observable behaviour, never on whether a mock was called.
- Every pragma carries a written justification.
- An honest gap with a named reason beats a hollow test that reaches the number.
- Dead code is reported, not tested — and **"no caller" does not mean dead.** 1a found
  `inbox_domain` had its logic copied four times inside the same file. Look for the
  duplicate before proposing deletion.

## Project rules

1. `if TYPE_CHECKING` is always a bug. Never introduce it.
2. Imports go at the top of the file. No inline imports.
3. No new runtime dependencies.

## Verification

1. The eight functions reach 100% statement and branch coverage, or carry a documented
   reason.
2. Every sub-condition of every guard has an independent pair of cases, or a recorded
   reason why it cannot be isolated.
3. `app/utils.py`'s floor rises from 50 to the measured figure, rounded down. Floors
   only rise; 1b-ii and 1b-iii raise it further.
4. The full suite stays green — 1482 passed, 3 skipped, 0 failed at the time of writing
   — and stays fast.
5. The inverse audit is delivered as a table, with every unguarded path reported.

## Risks

- **Fixture weight.** These functions need users, communities, instances, roles,
  memberships and bans. `tests/factories.py` will need extending, and a heavy fixture
  per test slows a suite whose speed is a defended property. Prefer building the
  minimum each case needs.
- **`g.site` coupling.** Several functions read or write `g.site`. The `app` fixture is
  session-scoped, so a leaked `g` corrupts later tests — 1a hit exactly this with
  `debug_checkpoint`. Clear per-test state deliberately.
- **Cache interference.** `communities_banned_from`, `banned_instances`,
  `trusted_instance_ids` and `instance_allowed` are `@cache.memoize`d. `TestConfig` sets
  `CACHE_TYPE = 'NullCache'`, so they do not cache under test — verify that still holds
  rather than assuming it, because a stale permission answer is exactly the failure that
  would look like a flake.
- **The audit may surface many unguarded paths.** If it does, the finding is the list,
  not a sprawling fix. Report and let the owner scope the response.
- **Reporting versus fixing.** Seven suspected defects are already listed. The
  temptation is to fix them while the tests are being written. Do not: authorisation
  changes need the owner's decision, and a permission fix that is wrong in the
  restrictive direction locks users out of their own site.
