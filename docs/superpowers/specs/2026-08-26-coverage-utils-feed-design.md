# Coverage Campaign 1b-ii: `app/utils.py` feed and query machinery

Date: 2026-08-26
Status: Approved design, ready for implementation planning

Sub-project 1b-ii of the campaign in
`docs/superpowers/specs/2026-08-25-coverage-campaign-foundation-design.md`.
Read `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` first — it is
short and it governs how this work is reviewed.

Predecessors: 1a (pure and context-only functions) and 1b-i (permission and
authorisation functions), both merged or on branch.

## Scope, measured

Against `coverage-utils-db` at `5b88cf8f`: `app/utils.py` is 2,846 statements, 1,018
uncovered, **60.24%**. The DB-backed group is 95 functions and 683 uncovered statements.

This sub-project takes the feed and query machinery:

| function | uncovered |
|---|---|
| `get_deduped_post_ids` | 65 |
| `get_instance_stickies` | 32 |
| `possible_communities` | 29 |
| `post_ids_to_models` | 14 |
| `instance_sticky_posts` | 14 |
| **total** | **154** |

`get_deduped_post_ids` was 124 before the microblog feed fix, which covered nearly half
of it. **These figures were re-measured rather than carried forward** — the pre-1b-i
partition would have been ~280 statements out of date, and this campaign has now had
five hand-carried counts turn out wrong when re-derived.

`archive_post` (84) still classifies as DB-backed only because it takes `s3_connection`
as a parameter. It is S3 archival and belongs to 1c.

### Out of scope

- The remaining ~445 statements of DB-backed functions (1b-iii).
- `archive_post` and the I/O group (1c).
- Changing feed behaviour. Two suspected defects are recorded below; this sub-project
  reports them.

## Two different testing problems

**Python filter chains** — `get_instance_stickies`, `possible_communities` (~61
statements). Each `continue` is a distinct "this post should not be visible" rule, with
different sets for anonymous and authenticated viewers. Cheap fixtures, and the same
condition-coverage discipline 1b-i used: every rule gets its own case, because a chain
of `continue`s reaches full branch coverage while leaving most rules never exercised.

**Raw SQL assembly** — `get_deduped_post_ids`, `post_ids_to_models`,
`instance_sticky_posts` (~93 statements). Heavier fixtures: real posts, communities,
votes, blocks and bans.

### Test end-to-end, not against SQL fragments

Drive the real function and assert on which post ids come back. The microblog feed fix
established this works.

The alternative — asserting that a clause appears in the generated SQL — is the
anti-pattern this campaign just removed. `tests/test_feed_boost_visibility.py` held a
hardcoded copy of a clause and could not notice when the production query was
restructured; it was replaced with imported constants driven end-to-end. **Do not
reintroduce that shape.**

## Depth: visibility filters deep, display preferences lighter

`get_deduped_post_ids` applies roughly twenty conditional filters. They are not equally
important, and effort goes where a defect actually harms someone.

**Full condition coverage, both directions** — filters gating *other people's* content:

- blocked users (`blocked_users`)
- blocked domains (`blocked_domains`)
- blocked and banned instances (`blocked_or_banned_instances`)
- communities banned from (`communities_banned_from`)
- blocked communities (`blocked_communities`)
- blocked flair

A broken block filter shows a user content from someone they blocked. That is a safety
failure, and both directions are needed: the blocked thing absent, and an unblocked
equivalent present. One direction alone cannot distinguish a working filter from one
that excludes everything.

**Lighter, single-direction coverage** — display preferences:

- `hide_nsfw`, `hide_nsfl`, `hide_read_posts`, `hide_gen_ai`, `ignore_bots`
- `read_language_ids`
- `hide_low_quality`

A broken preference filter is an annoyance, not a harm.

This is a deliberate allocation, not an oversight. Record it in the plan so a later
reader does not mistake the lighter half for a gap nobody noticed.

## The Redis hazard, and why it gets its own section

`get_deduped_post_ids` ends with:

```python
if current_user.is_authenticated:
    redis_client.set(result_id, json.dumps(post_ids), ex=86400)
```

**Every authenticated call writes a key to the test Redis with a 24-hour TTL.** The test
Redis is only destroyed by `./run_tests.sh --down`, which this campaign forbids because
it forces a replay of ~269 migrations.

This is the same class as the defect found in 1a, where Flask-Limiter counted into the
test Redis with a 24-hour TTL and silently degraded the suite across runs until
`/auth/login` began returning 429 to every later run. That one was found only when a
worker re-ran the baseline with its own work stashed.

Here it will not break the suite — the keys are unique per call and nothing reads them
back — but it is unbounded growth in a tmpfs container, and ~40 new authenticated feed
tests multiply it.

**The plan must decide how tests handle this** and say so explicitly: patch
`app.redis_client` for feed tests, delete the keys afterwards, or accept the growth with
a recorded reason. Note `redis_double` patches `get_redis_connection` and does **not**
cover `app.redis_client`, which is a different object read by roughly fourteen modules.

## Suspected defects, found while reading

Recorded so nobody rediscovers them or encodes one as intended. **Verify, report, do not
fix.**

1. **The cache read and write are guarded differently.** The read is
   `if result_id:`; the write is `if current_user.is_authenticated:`. So an
   authenticated call with an empty `result_id` — the caller saying "do not cache" —
   still writes, to a key literally named `''`, which the read path can never return. A
   pure wasted write. Establish whether any caller passes an empty `result_id`.
2. **`from app import redis_client` is an inline import** at the top of
   `get_deduped_post_ids`, violating a project rule. It is one of the 216 pre-existing
   violations catalogued as their own future project, so it is noted rather than fixed
   here — but it should be counted, not silently passed over.

## The quality bar

Inherited, and restated because this sub-project is about who sees what:

- **For every test, name the production change that would make it fail.** If you cannot
  name one, the test is decoration. This campaign has caught eight hollow tests, five of
  them self-caught by the worker that wrote them.
- Prove each filter is load-bearing by deleting it and watching a named test fail.
- Tests assert observable behaviour — which post ids come back — never on generated SQL
  or on whether a mock was called.
- An honest gap with a named reason beats a hollow test that reaches the number.
- **A count quoted in prose is a claim.** Five enumerations in this campaign were wrong
  when re-derived. Derive counts with a command and quote the command.

## Project rules

1. `if TYPE_CHECKING` is always a bug. Never introduce it.
2. Imports go at the top of the file. No inline imports — the campaign adds none.
3. No new runtime dependencies.

## Verification

1. The five functions reach 100% statement and branch coverage, or carry a documented
   reason.
2. Every visibility filter has both directions; every display preference has at least
   one. Any filter that cannot be isolated is reported, not skipped quietly.
3. `app/utils.py`'s floor rises from 60 to the measured figure, rounded down. Floors only
   rise. If the measurement does not support a rise, **do not round up to manufacture
   one** — 1b-i's Task 9 correctly left the floor at 60 rather than inflate it.
4. The suite stays green — 1658 passed, 3 skipped, 0 failed at the time of writing — and
   stays near its current ~60s.
5. The Redis-key decision is recorded, and the suite does not leave unbounded state
   behind.

## Risks

- **Fixture weight.** Feed tests need posts, communities, memberships, votes, blocks and
  bans. `tests/factories.py` will grow. Build the minimum each case needs; a heavy
  fixture per test is how a fast suite becomes a slow one.
- **`current_user` coupling.** `get_deduped_post_ids` and `get_instance_stickies` both
  read `current_user` directly. Tests need a real logged-in user, not a mock — and the
  `app` fixture is session-scoped, so leaked login state corrupts later tests.
- **Cache interference.** Many filters call `@cache.memoize`d helpers. `TestConfig` sets
  `CACHE_TYPE = 'NullCache'`, verified in sub-project 0 — but verify it still holds
  rather than assuming, because a stale filter answer looks exactly like a flake.
- **The 1000-row limit.** The query ends `LIMIT 1000`. A test seeding more than that
  would see truncation rather than filtering. Keep fixtures small, and if any test
  approaches the limit, say so.
- **Dedup interacts with filtering.** `dedupe_post_ids` runs after the query and collapses
  cross-posts. A test asserting "this post is absent" must distinguish filtered-out from
  deduped-away, or it will pass for the wrong reason.
