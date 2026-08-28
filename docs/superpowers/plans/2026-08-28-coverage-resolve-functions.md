# Coverage: the three remote-object resolvers — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take `resolve_remote_post`, `create_resolved_object` and `resolve_remote_post_from_search` from 25/142 statements to full coverage, so the four defects already registered inside them become safe to fix.

**Architecture:** Eight tasks. One establishes the fetch-plus-database fixture shape on the smallest function; five cover the two large ones in slices drawn where a reviewer could reject one and accept its neighbour; one confirms the registered defects against the new tests; one sets the floor and documents. The two large functions are near-duplicates, and the plan deliberately enumerates each separately rather than reusing one enumeration.

**Tech Stack:** Python 3, Flask, SQLAlchemy, pytest, respx via the `http_mock` fixture, coverage.py. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-28-coverage-resolve-functions-design.md`

## Global Constraints

- `if TYPE_CHECKING` is always a bug. Never introduce it, and never add it to `.coveragerc`'s `exclude_lines`.
- Imports go at the top of the file. No inline imports.
- Named exceptions only — never a bare `except:`.
- Every pragma carries a written justification.
- No new runtime dependencies, no migration.
- **Do not cite line numbers** in docstrings or documentation. Identify code by name.
- A count quoted in prose is a claim. Derive it with a command and quote the command.
- **Report defects; do not fix them.** No authorisation exists for any defect in this sub-project. D21-D24 and the `posted_at` defect stay unfixed; this work is what makes fixing them safe later.
- Tests assert observable behaviour — what the function returned, which row exists. Never on generated SQL, never on mocks. Asserting that respx received a call is a mock assertion.
- **NO host Python.** Use `./run_tests.sh [pytest args]`.
- **NEVER** run `./run_tests.sh --down` — it destroys the tmpfs DB and forces a ~269-migration replay.
- One test suite at a time. Check `pgrep -af '/venv/bin/pytest'` first; note pgrep can match its own shell wrapper, so a lone bash line containing your pattern is a false positive.
- Never `git stash` (the stash stack is shared across worktrees). Never `git checkout -- app/`; restore a single file by name.
- Baseline at the branch point: **2570 passed, 3 skipped, 0 failed**.

## Two things every task must respect

**The near-duplicate trap.** `create_resolved_object` and `resolve_remote_post_from_search` contain textually near-identical `attributedTo` walks and domain gates. D23 records that fixing one leaves the other. **Enumerate each function separately with its own command.** A test written against one copy proves nothing about the other, and the divergences between them are themselves a finding.

**Helper commits make row attribution ambiguous.** `create_post`, `create_post_reply`, `update_post_from_activity`, `update_post_reply_from_activity` and `find_actor_or_create` all commit. An assertion that a `Post` exists does not establish which code wrote it. Where it matters, assert on the row's *contents* — the field the function under test set — not merely its existence.

## File structure

| file | role |
|---|---|
| `tests/test_ap_resolve_remote_post.py` | Task 1 |
| `tests/test_ap_create_resolved_object.py` | Tasks 2, 3 |
| `tests/test_ap_resolve_from_search.py` | Tasks 4, 5, 6 |
| `tests/factories.py` | shared fixtures, extended in Task 1 |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` | Tasks 7, 8 |
| `coverage_floors.ini` | Task 8 only |

**On prescribed test bodies:** this plan gives each task its branch table and the derivation command, not finished test bodies. That follows this campaign's precedent — prescribing bodies against an enumeration the implementer has not yet derived is how eight earlier counts came out wrong.

---

### Task 1: `resolve_remote_post`, and the fixture shape

The smallest of the three (11 statements, 4 arcs, currently 0). It exists to establish the fetch-plus-database fixture every later task uses.

**Files:**
- Create: `tests/test_ap_resolve_remote_post.py`
- Modify: `tests/factories.py`

**Interfaces:**
- Produces: whatever fixture builds a fetchable remote post document and registers its route. Later tasks consume it; name it for what it is and give it a docstring saying what it guarantees.

- [ ] **Step 1: Derive the branch table**

```bash
podman-compose -f compose.test.yaml exec -T -w /app test-runner python -c "
import ast
src = open('app/activitypub/util.py').read()
for n in ast.walk(ast.parse(src)):
    if isinstance(n, ast.FunctionDef) and n.name == 'resolve_remote_post':
        print('If:', len([x for x in ast.walk(n) if isinstance(x, ast.If)]))
        for s in [x for x in ast.walk(n) if isinstance(x, ast.If)]:
            print('  ', ast.unparse(s.test)[:100])
"
```

Quote the command and its output in the module docstring.

- [ ] **Step 2: Cover the domain gate**

The gate is a three-part condition: `announce_actor_domain != 'ovo.st'`, `not nodebb`, and `announce_actor_domain != uri_domain`. Each of the three can independently allow the call through, so each needs a case where it is the operand doing the work.

**The `ovo.st` literal and the `nodebb` bypass are both registered as distinct from the `netloc` defect.** Cover them; do not fold them together.

- [ ] **Step 3: Cover the `remote_object_to_json` falsy return**

Both a `None` and a falsy-but-not-None return, if the latter is reachable — establish which by reading `remote_object_to_json`, do not assume.

- [ ] **Step 4: Cover the delegation**

That a successful path returns exactly what `create_resolved_object` returned. Assert on the returned object's identity or a field it carries, not on the fact a call happened.

- [ ] **Step 5: Mutation, both directions, classified first**

This campaign has produced five distinct guard shapes and the classification has mattered every time. Work out which shape this gate is — deleting a bare early-return guard and broadening it are the *same* mutation — then report only genuinely distinct directions with counts.

- [ ] **Step 6: Measure and commit**

Report `resolve_remote_post`'s own statement and branch figures from `coverage.json`, not the file's.

```bash
git add tests/test_ap_resolve_remote_post.py tests/factories.py
git commit -m "test: cover resolve_remote_post's domain gate and delegation

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: `create_resolved_object` — the `attributedTo` walk and the domain gate

**Files:**
- Create: `tests/test_ap_create_resolved_object.py`

- [ ] **Step 1: Derive this function's own enumeration**

Same command shape as Task 1 with the function name changed. **Do not reuse Task 4's or Task 5's figures** — these functions are near-duplicates and the whole point is to establish where they differ.

- [ ] **Step 2: Cover every `attributedTo` shape**

The walk handles: the key absent; a string; a list whose first usable element is a dict with `type == 'Person'`; a list whose first usable element is a bare string; a dict element whose `id` is **not** a string (the `isinstance(actor, str)` guard leaves `actor_domain` unset while `actor` is bound); and a list containing neither shape, which falls out of the loop.

Each needs its own case. The `break` placement means only the **first** usable element is read — cover a list where a later element would have matched differently, and pin that it is ignored.

- [ ] **Step 3: Cover the domain gate, including the `None` case**

`actor_domain` starts as `None`. A document with no `attributedTo` reaches `uri_domain != actor_domain` with `actor_domain` still `None`, which refuses. That is a different path from a mismatched domain and needs its own test.

**This is D22's surface.** Do not fix it; pin what it does today.

- [ ] **Step 4: Cover `find_actor_or_create` returning falsy**

And the three-part `if user and community and post_data` — each operand needs a case where it is the one that fails.

- [ ] **Step 5: Mutation, classified, both directions**

- [ ] **Step 6: Commit**

---

### Task 3: `create_resolved_object` — the create/update dispatch and the `posted_at` enrichment

**Files:**
- Modify: `tests/test_ap_create_resolved_object.py`

- [ ] **Step 1: Cover the activity dispatch**

`activity` is `'update'` when `'updated' in post_data` and `'create'` otherwise, and it selects between four paths: reply-update, reply-create, post-update, post-create. The update paths **fall back to create** when `get_by_ap_id` finds nothing — that fallback is a distinct path from a plain create and needs its own test.

- [ ] **Step 2: Cover the `inReplyTo` split**

The branch turns on `'inReplyTo' in request_json['object'] and request_json['object']['inReplyTo']` — so a **present but falsy** `inReplyTo` takes the post branch, not the reply branch. Pin that; it is exactly the kind of thing a reader assumes wrongly.

- [ ] **Step 3: Cover the `posted_at` enrichment**

Present in both branches, guarded only by `'published' in post_data`. Assert the values actually landed on the row, and that the post branch sets `last_active` where the reply branch sets it on `post_reply.post`.

**This is the registered `posted_at` defect's surface.** A `published` value that is not a timestamp is reported to fail at flush — **that was inferred, not observed.** Establish what actually happens by writing the case, and report which it is. If it does not fail, the register is wrong and that is the finding.

- [ ] **Step 4: Cover the falsy-helper returns**

`create_post_reply` and `create_post` can return falsy, and the enrichment is skipped when they do. Both need a case.

- [ ] **Step 5: Whole-function confirmation**

Run coverage over **all of `create_resolved_object`**, not just this task's slice, and confirm Tasks 2 and 3 together leave nothing uncovered. Report the figures. A gap between two tasks' slices shows up nowhere else.

- [ ] **Step 6: Mutation, classified, both directions. Commit.**

---

### Task 4: `resolve_remote_post_from_search` — the pre-fetch chain

**Files:**
- Create: `tests/test_ap_resolve_from_search.py`

- [ ] **Step 1: Derive this function's own enumeration, separately**

- [ ] **Step 2: Cover the two early returns**

The function opens with `Post.get_by_ap_id(uri)` and returns any existing post before fetching. After the fetch chain it checks again with `post_data['id']`, which catches "different but equivalent URLs". Both need tests, and the second needs a case where the two URLs genuinely differ.

Note `post_data['id']` is an **unguarded read** on a peer document. Pin what happens when the key is absent, and report it.

- [ ] **Step 3: Cover the Conversation refetch**

Guarded by `type == 'Conversation'` and `isinstance(post_data['posts'], str)`. Each conjunct needs a case where it is the one that fails, plus the refetch returning falsy.

- [ ] **Step 4: Cover the OrderedCollection/nodebb branch**

Four conjuncts: `type == 'OrderedCollection'`, `'totalItems' in post_data`, `totalItems > 0`, and `isinstance(orderedItems, list)`. Each needs its own failing case.

The sweep established these reads are **validated by this guard**, so the later `orderedItems[1:]` and `totalItems` reads are not the hazard they resemble. Say so in the docstring — a later reader will otherwise re-report it.

- [ ] **Step 5: Cover `uri` and `uri_domain` being reassigned**

The nodebb branch replaces both from `orderedItems[0]`. A document whose collection points at a different host than the collection itself changes what the later domain gate compares. Pin it.

- [ ] **Step 6: Mutation, classified. Commit.**

---

### Task 5: `resolve_remote_post_from_search` — the `attributedTo` walk, the domain gate, and the drift report

**Files:**
- Modify: `tests/test_ap_resolve_from_search.py`

- [ ] **Step 1: Cover this copy of the walk**

Same shapes as Task 2, **derived again from this function's source**. Do not copy Task 2's table.

- [ ] **Step 2: Produce the drift report — this is the task's distinctive deliverable**

Diff the two `attributedTo` walks and the two domain gates mechanically:

```bash
# extract each function's source and compare the walks
podman-compose -f compose.test.yaml exec -T -w /app test-runner python -c "
import ast
src = open('app/activitypub/util.py').read()
for n in ast.walk(ast.parse(src)):
    if isinstance(n, ast.FunctionDef) and n.name in ('create_resolved_object','resolve_remote_post_from_search'):
        print('=====', n.name)
        print(ast.unparse(n))
" > /dev/null   # then diff the two regions by eye and by tool
```

Report **every** difference, however small, and say for each whether it is deliberate or drift. D23 says fixing one leaves the other; a later engineer deduplicating them needs to know which differences carry meaning.

- [ ] **Step 3: Cover the `find_community` fallback**

`if not community and nodebb: community = find_community(topic_post_data)` — the "use `audience` from topic" path. Needs the nodebb branch from Task 4, and a case where the post document alone yields no community.

- [ ] **Step 4: Mutation, classified. Commit.**

---

### Task 6: `resolve_remote_post_from_search` — creation, enrichment, background replies, return shape

**Files:**
- Modify: `tests/test_ap_resolve_from_search.py`

- [ ] **Step 1: Cover the `inReplyTo` split**

Guarded by `'inReplyTo' in post_data and post_data['inReplyTo'] is not None` — note this copy tests `is not None` where `create_resolved_object` tests truthiness. **A present-but-empty-string `inReplyTo` therefore takes a different branch in the two functions.** Pin both behaviours and add it to Task 5's drift report.

- [ ] **Step 2: Cover the enrichment**

`object.posted_at` is set in both cases but `object.last_active` only when `not in_reply_to`. Assert both.

- [ ] **Step 3: Cover the return shape**

`return object if not in_reply_to else object.post` — a reply resolves to its **parent post**, not the reply. That is a surprising contract and needs pinning explicitly.

- [ ] **Step 4: Cover the nodebb background dispatch, both modes**

`if current_app.debug:` calls `get_nodebb_replies_in_background` inline, else `.delay()`. Both need a case. This campaign has already found one defect whose *exception type* differed between DEBUG modes — exercise both settings rather than reasoning about them, and say how.

Guarded by `nodebb and topic_post_data['totalItems'] > 1`, so cover `totalItems == 1` too.

- [ ] **Step 5: Whole-function confirmation**

Run coverage over **all of `resolve_remote_post_from_search`** and confirm Tasks 4, 5 and 6 together leave nothing uncovered. Report the figures.

- [ ] **Step 6: Mutation, classified. Commit.**

---

### Task 7: Confirm the registered defects against the new tests

**Report only. Fix nothing.**

**Files:**
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`

- [ ] **Step 1: Confirm or refute each**

D21, D22, D23, D24 and the `posted_at` defect. For each, write the test that demonstrates it — or, if it turns out not to exist as described, say so with evidence. **A defect that turns out not to exist is as valuable a finding as one that does**, and this campaign has already corrected several register entries that were right in substance and wrong in detail.

- [ ] **Step 2: Record what changed**

Whether the severity stated in the register still holds now that the behaviour is pinned. The register's severities were derived by reading; you now have tests.

- [ ] **Step 3: Note that these are now safe to fix**

That is this sub-project's purpose. Say plainly, per defect, what a fix would have to preserve, drawing on the tests that now exist.

- [ ] **Step 4: Commit**

---

### Task 8: Floor and documentation

**Files:**
- Modify: `coverage_floors.ini`, `tests/README.md`

- [ ] **Step 1: Measure**

```bash
./run_tests.sh tests/ -q --cov=app --cov-report=json
podman-compose -f compose.test.yaml exec -T -w /app test-runner \
  python tests/check_coverage_floors.py coverage.json coverage_floors.ini
```

`percent_covered` is the **blended statement+branch** figure and is what the checker reads. Say so.

- [ ] **Step 2: Raise the floor, rounded DOWN**

From 35 to the new measured value. **Edit the file additively** — an agent in this campaign once emptied it wholesale, deleting three floors including a prior sub-project's deliverable. Leave the other three untouched, and end with `git diff coverage_floors.ini` showing one modified line.

Prove it bites: set it one higher, confirm exit 1 naming the module; restore, confirm exit 0.

- [ ] **Step 3: Document**

A "resolve functions" section in `tests/README.md` covering the fixture shape, the near-duplicate hazard and the drift report's conclusions, and the DEBUG-mode split.

- [ ] **Step 4: Commit**

---

## Plan Self-Review

**Spec coverage.** Every spec section maps to a task: the three functions to Tasks 1-6; the duplication and drift report to Task 5 with a contribution from Task 6; the two-caller severity split to Task 2; defect confirmation to Task 7; the floor to Task 8. The spec's "helper commits make attribution ambiguous" warning appears as an explicit assertion requirement in the two-things preamble rather than as a general instruction.

**Placeholder scan.** No TBD or TODO. Branch tables and derivation commands are given rather than finished test bodies, deliberately and for the reason stated in the File Structure section.

**Type consistency.** `resolve_remote_post(uri, community, announce_id, store_ap_json, nodebb=False)`, `create_resolved_object(uri, post_data, uri_domain, community, announce_id, store_ap_json)` and `resolve_remote_post_from_search(uri)` are used with those signatures throughout. Task 1's fixture is named as a product and consumed by name in Tasks 2-6.

**Ordering.** Tasks 2-3 and 4-5-6 are each a sequence over one file; Task 5's drift report needs Task 2 complete, and Task 6 contributes one row to it. Task 7 needs all six. No other ordering is load-bearing.
