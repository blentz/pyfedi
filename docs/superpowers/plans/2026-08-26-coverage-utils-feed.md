# Coverage 1b-ii: Feed and Query Machinery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take the five feed and query functions of `app/utils.py` to 100% statement and branch coverage, with visibility filters proved in both directions.

**Architecture:** Eight tasks. Task 1 settles the Redis-write hazard and extends the factories. Tasks 2-3 cover sort dispatch, which is a third of the work and is mechanical. Tasks 4-5 cover visibility filters deep and display preferences lightly. Tasks 6-7 cover the two Python filter chains. Task 8 raises the floor.

**Tech Stack:** pytest, the fixtures from sub-project 0, `tests/factories.py`.

**Spec:** `docs/superpowers/specs/2026-08-26-coverage-utils-feed-design.md`

**Campaign findings that govern this work:** `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` — read first.

## Global Constraints

- `if TYPE_CHECKING` is always a bug. Never introduce it.
- Imports go at the top of the file. No inline imports.
- Every pragma carries a written justification.
- **Tests assert on which post ids come back — never on generated SQL, never on mocks.** A test asserting a clause appears in a SQL string was removed from this repo for being unable to notice a restructure. Do not reintroduce it.
- **For every test, name the production change that would make it fail.**
- **Visibility filters get both directions.** The blocked thing absent AND an unblocked equivalent present. One direction alone cannot distinguish a working filter from one excluding everything.
- Report defects; do not fix them.
- Floors only RISE. `app/utils.py` is at 60. **Do not round up to manufacture a rise** — 1b-i correctly left it unchanged when the measurement did not support one.
- A count quoted in prose is a claim. Derive counts with a command and quote it.
- No new dependencies, no migration.
- NO host Python environment. Run `./run_tests.sh [pytest args]`; read `tests/README.md` first. Never `./run_tests.sh --down`. One suite at a time.

## Baseline

`./run_tests.sh tests/ -q --ignore=tests/test_activitypub_util.py` → **1658 passed, 3 skipped, 0 failed**. `app/utils.py` 2846 statements, 1018 uncovered, 60.24%.

## What is actually uncovered

Derived from `coverage.json`, not from reading:

| function | uncovered | dominated by |
|---|---|---|
| `get_deduped_post_ids` | 65 | ~14 filters, its sort chain, 8 `top_*` windows |
| `get_instance_stickies` | 35 | anonymous and authenticated filter chains |
| `possible_communities` | 29 | three-group assembly with dedup |
| `post_ids_to_models` | 17 | a 7-branch sort chain |
| `instance_sticky_posts` | 14 | a 7-branch sort chain |

**Three separate sort chains** across the three query functions, plus eight `top_*` cutoff windows, account for roughly a third of the total. They are mechanical and parametrisable — which is why they get their own tasks rather than being scattered.

## File Structure

| File | Responsibility |
|---|---|
| `tests/factories.py` | Task 1 — blocks, bans, flair, read/hidden posts, votes |
| `tests/test_feed_sorts.py` | Tasks 2-3 — the three sort chains and the `top_*` windows |
| `tests/test_feed_visibility_filters.py` | Task 4 — filters gating other people's content |
| `tests/test_feed_display_preferences.py` | Task 5 — NSFW, languages, bots, read posts |
| `tests/test_instance_stickies.py` | Task 6 — the sticky filter chains |
| `tests/test_possible_communities.py` | Task 7 — the grouped picker |

---

### Task 1: Settle the Redis hazard, then extend the factories

**Files:** Modify `tests/factories.py`, `tests/conftest.py`; Test: `tests/test_factories_feed.py`

**Interfaces produced** — every later task consumes these.

- [ ] **Step 1: Decide and implement the Redis-write policy — before anything else**

`get_deduped_post_ids` ends with:

```python
if current_user.is_authenticated:
    redis_client.set(result_id, json.dumps(post_ids), ex=86400)
```

Every authenticated call writes a key with a **24-hour TTL** to the test Redis, which only `./run_tests.sh --down` clears — and this campaign forbids `--down`. Sub-project 1a found the same shape in Flask-Limiter, where accumulated 24-hour counters silently degraded the suite across runs until `/auth/login` returned 429 to everything.

It will not break this suite — keys are unique per call and nothing reads them back — but ~40 new authenticated feed tests multiply unbounded growth in a tmpfs container.

**Decide and record which:** patch `app.redis_client` for feed tests via an autouse fixture; delete the written keys in teardown; or accept the growth with a measured justification.

Note `redis_double` patches `get_redis_connection` and does **NOT** cover `app.redis_client` — a different object read by roughly fourteen modules. Read `redis_double`'s docstring in `tests/conftest.py` before choosing.

Whatever you choose, **prove it**: run the feed tests twice and show the test Redis key count does not grow between runs, or show the measured growth and why it is acceptable.

- [ ] **Step 2: Establish what already exists**

`tests/factories.py` currently provides `make_instance`, `make_user`, `make_community`, `make_post`, `make_site`, `make_follow`, `grant_permission`, `make_community_member`, `ban_user_from_community`, `make_instance_ban`.

Read each before adding anything. Read the production query each new factory must satisfy — Task 1 of 1b-i was held to this and it is why the seven tasks after it could be trusted.

- [ ] **Step 3: Write the failing proof tests**

For each new factory, a test proving the production function reads what the factory writes. The pattern from 1b-i:

```python
def test_block_user_is_visible_to_blocked_users(app, db_session):
    """Fails if the factory writes rows blocked_users() does not read.

    Without this, every later blocked-user test passes without exercising a block.
    """
    blocker = make_user(None, 'blocker', local=True)
    blocked = make_user(None, 'blocked', local=True)
    block_user(blocker, blocked)

    assert blocked.id in blocked_users(blocker.id)
```

Write the equivalent for each of: `block_user`, `block_domain`, `block_instance`, `block_community`, `mark_post_read`, `hide_post`, `make_post_flair` + `block_flair`, and a vote helper if `post_ids_to_models`'s score ordering needs one.

Check the production reader for each: `blocked_users`, `blocked_domains`, `blocked_or_banned_instances`, `blocked_communities`, and the raw `read_posts` / `hidden_posts` queries in `get_deduped_post_ids` and `get_instance_stickies`.

- [ ] **Step 4: Implement, following the existing factory style**

Build the row, `db.session.add`, `db.session.commit`, return the object.

- [ ] **Step 5: Verify `NullCache` still holds**

Several filters call `@cache.memoize`d helpers. `TestConfig` sets `CACHE_TYPE = 'NullCache'` and sub-project 0 verified it — but verify again rather than assume, because a stale filter answer is indistinguishable from a flake:

```python
def test_blocked_users_is_not_cached_between_calls(app, db_session):
    blocker = make_user(None, 'cachecheck', local=True)
    blocked = make_user(None, 'target', local=True)

    assert blocked_users(blocker.id) == []
    block_user(blocker, blocked)
    assert blocked.id in blocked_users(blocker.id)
```

If this fails, STOP and report — seven tasks depend on it.

- [ ] **Step 6: Commit**

```bash
git add tests/factories.py tests/conftest.py tests/test_factories_feed.py
git commit -m "test: add feed factories and settle the Redis-write hazard"
```

---

### Task 2: The three sort chains

**Files:** Create `tests/test_feed_sorts.py`

`get_deduped_post_ids`, `post_ids_to_models` and `instance_sticky_posts` each carry their own `elif sort ==` chain dispatching to a different ORDER BY. Seven sort values each.

**These are three separate implementations of the same concept, and they do not agree.** `post_ids_to_models` orders `top` by `Post.score`; `instance_sticky_posts` orders it by `Post.up_votes - Post.down_votes`. Establish whether those are equivalent in practice, and **report the divergence** — do not reconcile it.

- [ ] **Step 1: Write parametrised sort tests**

Assert on **observed order**, not on which branch was taken:

```python
import pytest

from app.utils import instance_sticky_posts, post_ids_to_models

SORTS = ['', 'hot', 'scaled', 'top', 'new', 'old', 'active']


@pytest.mark.parametrize('sort', SORTS)
def test_post_ids_to_models_returns_every_requested_post(app, db_session, sort):
    """Whatever the ordering, no post may be dropped. Fails if a sort branch
    filters instead of ordering."""
```

Then, per sort, a test asserting the actual order with posts whose relevant field differs — `ranking` for hot, `posted_at` for new and old, `score` for top, `last_active` for active, `ranking_scaled` for scaled.

`scaled` also appends `p.ranking_scaled is not null AND p.from_bot is false` in `get_deduped_post_ids` — that is a **filter**, not just an ordering, so it needs its own both-directions case: a post with null `ranking_scaled` absent, one with a value present.

- [ ] **Step 2: Prove ordering, not just membership**

A test asserting the same three posts come back for every sort proves nothing about ordering. For each sort, seed at least two posts whose order differs from insertion order and assert the returned sequence.

- [ ] **Step 3: Prove discrimination**

Swap two sort branches — make `new` use `asc(Post.posted_at)` — and confirm exactly the `new` ordering test fails. Restore with `git checkout -- app/utils.py` and report the count.

- [ ] **Step 4: Commit**

---

### Task 3: The eight `top_*` cutoff windows

**Files:** Modify `tests/test_feed_sorts.py`

`get_deduped_post_ids` sets a different `top_cutoff` for `top_1h`, `top_6h`, `top_12h`, `top`, `top_1w`, `top_1m`, `top_1y`, and appends no cutoff at all for `top_all`.

- [ ] **Step 1: Write the tests**

Parametrise over `(sort, window)` pairs. For each: a post inside the window returned, a post outside it absent. `top_all` returns both.

```python
TOP_WINDOWS = [
    ('top_1h', timedelta(hours=1)), ('top_6h', timedelta(hours=6)),
    ('top_12h', timedelta(hours=12)), ('top', timedelta(hours=24)),
    ('top_1w', timedelta(days=7)), ('top_1m', timedelta(days=28)),
    ('top_1y', timedelta(days=365)),
]
```

Set `posted_at` explicitly — do not rely on insertion time. A post just inside and just outside each boundary is what proves the window, and the boundary is where an off-by-one lives.

- [ ] **Step 2: The unrecognised-`top` fallthrough**

`elif sort != 'top_all': params['top_cutoff'] = utcnow() - timedelta(days=1)` catches any other `top`-prefixed value. Cover it with something like `top_nonsense`, and note that it silently behaves as 24 hours rather than erroring — **report that, do not change it**.

- [ ] **Step 3: Prove discrimination**

Change `top_1w` to `timedelta(days=1)` and confirm only its test fails. Restore and report.

- [ ] **Step 4: Commit**

---

### Task 4: Visibility filters — both directions

**Files:** Create `tests/test_feed_visibility_filters.py`

These gate **other people's content**. A defect here shows a user content from someone they blocked.

Filters, with the production helper each consults:

| filter | helper | line |
|---|---|---|
| blocked users | `blocked_users` | 3887 |
| blocked domains | `blocked_domains` | 3877 |
| blocked instances | `blocked_or_banned_instances` | 3840, 3880 |
| blocked communities | `blocked_communities` | 3883 |
| banned from community | `communities_banned_from` | 3891 |
| blocked flair | direct query | 3897-3899 |
| filtered-out communities | `filtered_out_communities` | 3836 |

- [ ] **Step 1: Both directions for each**

```python
def test_a_blocked_authors_post_is_absent(app, db_session, site):
    """Fails if the blocked_accounts clause is removed."""

def test_an_unblocked_authors_post_is_present(app, db_session, site):
    """The other direction. Without it, a filter excluding EVERYTHING would
    also pass -- the absence test alone cannot tell those apart."""
```

Both tests seed the same shape and differ only in whether the block exists.

- [ ] **Step 2: Mind the two traps**

**`LIMIT 1000`** — the query truncates. Keep fixtures small; if any test approaches it, say so.

**`dedupe_post_ids`** runs after the query and collapses cross-posts. A test asserting "this post is absent" must distinguish *filtered out* from *deduped away*. Give each seeded post a distinct `url` or no cross-post relationship, and say in the report how you ensured it.

- [ ] **Step 3: Prove discrimination**

Delete the `blocked_accounts` clause; confirm only its absence test fails and the presence test still passes. Then delete `communities_banned_from`'s clause and confirm the same. Restore after each and report both counts.

- [ ] **Step 4: Commit**

---

### Task 5: Display preferences — lighter

**Files:** Create `tests/test_feed_display_preferences.py`

Single-direction coverage is sufficient here, per the spec's deliberate allocation: a broken preference filter is an annoyance, not a harm.

Cover: `hide_nsfw`, `hide_nsfl`, `hide_read_posts`, `hide_gen_ai`, `ignore_bots`, `read_language_ids`, `hide_low_quality`, and the anonymous `CONTENT_WARNING` branch at 3847-3850.

- [ ] **Step 1: One case per preference**

Each: a user with the preference set, a post that trips it, assert absent. Where a preference has an obvious inverse that costs nothing, take it — but do not build a second fixture for it.

- [ ] **Step 2: The anonymous branch**

Lines 3847-3850 differ by `current_app.config['CONTENT_WARNING']`. With it set, anonymous users get `from_bot/nsfl/deleted` filtering; without, `nsfw` is filtered too. Cover both, restoring the config in a `finally`.

- [ ] **Step 3: Prove discrimination**

Delete the `ignore_bots` clause; confirm only its test fails. Restore and report.

- [ ] **Step 4: Commit**

---

### Task 6: `get_instance_stickies` filter chains

**Files:** Create `tests/test_instance_stickies.py`

A Python filter chain with two distinct paths. Each `continue` is a visibility rule.

**Anonymous:** `CONTENT_WARNING` + nsfw/nsfl; community not in view.
**Authenticated:** community not in view; `hide_nsfl`; `hide_nsfw`; read + `hide_read_posts`; hidden post.

Plus the `all_communities` flag: `len(community_ids) == 1 and community_ids[0] < 0`.

- [ ] **Step 1: One case per rule, everything else held visible**

A chain of `continue`s reaches full branch coverage while leaving most rules never exercised. Each rule needs a post that trips only it.

- [ ] **Step 2: Cover `all_communities` both ways**

With `[-1]`, a sticky from any community appears. With a real community id, one from elsewhere does not.

- [ ] **Step 3: Prove discrimination**

Delete the hidden-post `continue`; confirm only its test fails. Restore and report.

- [ ] **Step 4: Commit**

---

### Task 7: `possible_communities`

**Files:** Create `tests/test_possible_communities.py`

Builds a grouped picker: `Moderating`, `Joined communities`, `Others`, deduplicating via `already_added`.

- [ ] **Step 1: Cover the grouping and the dedup**

- a moderated community appears under `Moderating`
- a joined-but-not-moderated one under `Joined communities`
- an unrelated one under `Others`
- **a community both moderated and joined appears ONCE, under `Moderating`** — the dedup, and the reason `already_added` exists
- an empty group is omitted entirely rather than present-and-empty

- [ ] **Step 2: Cover the exclusions**

The `Others` query excludes `Community.banned == True`, `Instance.gone_forever == True`, and `Community.name != 'microblogs'`. One case each.

Note the microblogs exclusion is **by name**, so it excludes remote microblog communities too — there are two on the owner's instance, local and remote. Confirm the behaviour and report it; do not change it.

- [ ] **Step 3: Cover the display-name branch**

`display_name` is `c.title` when `ap_id` is None, else `f"{c.title}@{c.ap_domain}"`. Both.

- [ ] **Step 4: Prove discrimination**

Delete `already_added.add(c.id)` from the moderating loop; confirm the dedup test fails. Restore and report.

- [ ] **Step 5: Commit**

---

### Task 8: Measure, raise the floor, document

**Files:** Modify `coverage_floors.ini`, `tests/README.md`

- [ ] **Step 1: Measure**

```bash
./run_tests.sh tests/ -q --ignore=tests/test_activitypub_util.py --cov=app --cov-report=json && \
podman-compose -f compose.test.yaml exec -T test-runner \
    python tests/check_coverage_floors.py coverage.json coverage_floors.ini
```

- [ ] **Step 2: Raise the floor to the measured figure, rounded DOWN**

It is currently 60. **If the measurement does not support a rise, leave it and say so** — 1b-i's Task 9 correctly left it at 60 rather than inflate it. `percent_covered` is a blended statement-and-branch figure.

- [ ] **Step 3: Prove the floor bites**

Raise it one point, re-run, confirm non-zero exit naming the module, restore. Report both outputs. A floor nobody has seen fail is not a floor.

- [ ] **Step 4: Report what remains**

List every line still uncovered in the five functions with the reason. An honest gap with a named reason beats a hollow test.

- [ ] **Step 5: Document**

A short `tests/README.md` section: what 1b-ii covered, the Redis-write decision from Task 1 and why, and the sort-chain divergence from Task 2.

- [ ] **Step 6: Commit**

---

## Verification

1. The five functions reach 100% statement and branch coverage, or carry documented reasons.
2. Every visibility filter has both directions; every display preference has at least one.
3. The suite stays green — 1658 passed at the time of writing — and near its ~60s runtime.
4. The Redis-write decision is recorded and proved.
5. The floor has risen, or has been left with a stated reason, and has been seen to fail.
6. Every suspected defect from the spec has a recorded verdict.
