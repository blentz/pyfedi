# Coverage Campaign 2a: `app/activitypub/util.py` actor and object ingestion

Date: 2026-08-27
Status: Approved design, ready for implementation planning

Sub-project of the campaign in
`docs/superpowers/specs/2026-08-25-coverage-campaign-foundation-design.md`.
Read `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` first — it is
short and it governs how this work is reviewed.

Predecessors: 1a (pure functions), 1b-i (permissions), 1b-ii (feed machinery),
1c (link and domain handling). All merged to `blentz`, most recently at `0a20752f`.

## Why this file, and why this part of it first

Re-measured at `0a20752f`, `app/utils.py` is no longer the campaign's largest gap. It
went 60% → 73.24% and now ranks tenth. The largest is:

```
coverage.json → files['app/activitypub/util.py']
  percent_covered = 18.18%   num_statements = 2816   missing = 2223
```

**Every function in this file processes peer-controlled input.** That is the argument
for taking it next, ahead of larger-percentage gaps elsewhere. The defects this
campaign found in the federation path were not found by coverage numbers — they were
found by writing tests that fed a hostile string to a function and watching what
happened:

- `Post.new()` raised `AttributeError` on a peer-supplied url, and `create_post`
  swallowed it silently — `log_incoming_ap` writes nothing when `store_ap_json` is
  off, so a dropped federated post left no trace anywhere.
- `extract_domain_and_actor` parsed a peer-chosen actor url with no guard.
- `post.url = ''` federated `{"href": ""}` back out to peers.

A remote attacker chooses those strings, and nothing in the suite exercised them.

## Decomposition of the file

Grouped by handler, derived by AST over the current file:

| uncovered | group |
|---|---|
| 611 | background tasks — three `refresh_*_profile_task`, `new_instance_profile_task`, `make_image_sizes_async` |
| **416** | **actor and object ingestion — this sub-project** |
| 253 | Update — `update_post_from_activity`, `update_post_reply_from_activity` |
| 252 | Ban and Flag — `ban_user`, `unban_user`, two `*_remove_data`, `process_report` |
| 201 | Create — `create_post_reply`, the two `notify_about_*` |
| 75 | Delete — `delete_post_or_comment`, `restore_post_or_comment` |
| 42 | Announce and Undo |
| 373 | long tail, 54 functions |

Activity handlers are 823 of 2223 — about a third. The two largest blocks are not
activity handlers at all, which is why "decompose by activity type" needed adjusting
rather than adopting wholesale.

**Background tasks (611) are out of scope for this campaign target.** They are Celery
jobs doing image resizing and HTTP profile refresh — closer in kind to the S3 and
image cluster still outstanding in `app/utils.py` than to federation semantics.
Bundling them would drag `moto` and image fixtures into a sub-project about trust
boundaries.

## Scope

| function | uncovered |
|---|---|
| `actor_json_to_model` | 194 |
| `verify_object_from_source` | 62 |
| `find_flair_or_create` | 56 |
| `find_community` | 39 |
| `remote_object_to_json` | 33 |
| `ensure_domains_match` | 26 |
| **total** | **410** |

Plus `find_actor_or_create` and `find_actor_or_create_cached`, which already have tests
(repaired at `0a20752f`) and contribute the remainder toward the 416 figure.

Line ranges are deliberately omitted. Four attempts were spent in sub-project 1c
correcting a single stale line citation, each correct when derived and stale when
committed. **Derive them at implementation time and identify code by name.**

### Why ingestion goes first

Every activity handler resolves an actor before it does anything else. The fixtures
this sub-project builds — a peer actor JSON document, a mocked webfinger, a remote
object fetch — are what 2b through 2e will consume. Writing them once here, correctly,
is cheaper than four sub-projects each inventing their own.

It is also the outermost trust boundary in the file: the point where a peer's JSON
first becomes a row in the database.

### Out of scope

- Background tasks (611), as above.
- The Update, Create, Delete, Ban/Flag and Announce handlers — sub-projects 2b-2e.
- The 54-function long tail.
- Fixing the two suspected defects below. This sub-project **verifies and reports**
  them; fixing is a separate owner decision, as it was for every defect this campaign
  has found.

## Two suspected defects, found while reading

Recorded so nobody rediscovers them or writes a test that encodes one as intended
behaviour. **Verify, report, do not fix.**

### 1. `actor_json_to_model`'s server check is a substring test

```python
    if server not in activity_json['id']:
        return None
```

Probed at `0a20752f`:

```
server=good.example  id=https://good.example/u/alice                passes=True  real host good.example
server=good.example  id=https://good.example.attacker.net/u/alice   passes=True  real host good.example.attacker.net
server=good.example  id=https://attacker.net/u/x?ref=good.example   passes=True  real host attacker.net
```

A substring standing in for a host comparison. This is the same shape as
`Post.youtube_can_embed`'s `if "youtube.com" not in self.url` gate, which produced a
stored denial of service found and fixed earlier in this campaign.

**Establish what `server` is at each call site before judging severity.** If callers
derive it from the url they just fetched, the check may be weaker than it looks but
not independently exploitable. That determination is part of this sub-project's work.

### 2. `ensure_domains_match` and `verify_object_from_source` compare `netloc`, not `hostname`

```python
        parsed_url = urlparse(note_id)
        id_domain = parsed_url.netloc
```

`netloc` carries userinfo and port; `hostname` does not:

```
https://good.example@evil.net/x   netloc=good.example@evil.net   hostname=evil.net
https://good.example:8443/x       netloc=good.example:8443       hostname=good.example
```

Userinfo smuggling is what `is_safe_redirect_target` was hardened against in
sub-project 1a; these two functions never received the same treatment. The port case
is separate: the same host on two ports compares unequal, which may reject legitimate
peers.

Both also call `urlparse` unguarded, and sub-project 1c established that `urlparse`
raises `ValueError` on malformed netlocs. Whether a peer can reach these with such a
string is part of the work.

## The testing shape

Every function here takes a **dictionary parsed from a peer's JSON**. That makes them
unusually testable — no HTTP is required to exercise the parsing itself, only to
exercise the fetch paths.

Two kinds of test:

**Document-driven.** Build the actor or object JSON as a fixture and assert on what
comes back — a `User`, a `Community`, a `Feed`, or `None`. These need no network at
all. The bulk of `actor_json_to_model`'s 194 statements are of this kind: three actor
types (`Person`/`Service`, `Group`, `Feed`), each with many optional fields.

**Fetch-driven.** `remote_object_to_json` and `verify_object_from_source` retrieve
before they parse. Mock with `http_mock` (respx). `block_outbound_http` is
session-scoped autouse and raises on any unmatched request, so a forgotten route
surfaces as an error rather than reaching the network — as it did for the two tests
repaired at `0a20752f`.

**Malformed input is the point, not an edge case.** These functions exist to decide
whether a peer's claim is acceptable. A test suite that only feeds them well-formed
documents tests the happy path of a security boundary. Every function needs cases
where the peer is lying: a mismatched `id`, an `attributedTo` naming another host,
missing required fields, wrong types in the right keys.

## Depth

Full coverage with both mutation directions for all six functions. There is no lighter
tier in this sub-project — every function here decides whether to trust a remote
claim, and a defect in any of them admits forged content.

## Primary sources

- **ActivityPub** (W3C Recommendation) — actor requirements, the `id` and `type`
  properties, and what a server may assume about a remote object.
- **ActivityStreams 2.0 Core** — `attributedTo`, `actor`, and the shapes those may
  legitimately take. `ensure_domains_match` already handles `attributedTo` as a string,
  a list of strings, and a list of dicts; the spec is the authority on which are valid.
- **RFC 3986** — the authority component, and why `netloc` and `hostname` differ.

Where the code accommodates a specific implementation rather than a specification —
the comment `some Akkoma instances return an empty actor?!` is one — say so in the test
docstring. Interoperability workarounds are not spec compliance, and a later reader
needs to know which they are looking at.

## The quality bar

Inherited, and restated because this sub-project is about whom to believe:

- **For every test, name the production change that would make it fail.** If you cannot
  name one, the test is decoration.
- **Both mutation directions.** Deleting a guard proves an absence test is load-bearing
  and says nothing about a presence test, because the deleted code is unreachable in
  the presence case. Only over-broadening — making a guard reject everything — can fail
  a presence test.
- **Wide blast radius needs the narrow mutation on the same rule.** Guard-level
  mutations stay narrow because the guard sits inside a branch already selected;
  dispatch-condition mutations go wide because they change which branch runs. Radius
  alone is not a signal.
- **A mutation that survives is not proof the code is unmutable.** It may mean the test
  cannot observe the effect. Sub-project 1c found a bare `except:` swallowing respx's
  assertion error, so a test written to catch a mutation passed under it. Investigate
  before concluding.
- **Coverage cannot see inside strings.** Regex alternations, URL literals and the
  substring gate above are invisible to the percentage.
- **A count quoted in prose is a claim.** Seven enumerations in this campaign were wrong
  when re-derived. Derive with a command and quote the command.
- Tests assert observable behaviour — what the function returned, which row exists —
  never on generated SQL, never on mocks. Asserting that respx received a call is a
  mock assertion.

## Project rules

1. `if TYPE_CHECKING` is always a bug. Never introduce it, and never add it to
   `.coveragerc`'s `exclude_lines`.
2. Imports go at the top of the file. No inline imports.
3. Every pragma carries a written justification.
4. Named exceptions only — never a bare `except:`.
5. No new runtime dependencies, no migration.
6. Report defects; do not fix them without owner authorisation.
7. **Do not cite line numbers in docstrings.** Identify code by name and behaviour.

## Verification

1. The six functions reach 100% statement and branch coverage, or carry a documented
   reason. An unreachability claim must state what was tried and failed to reach it.
2. Every guard has both mutation directions, with counts reported.
3. The two suspected defects are verified and reported, with the call-site analysis
   that determines their severity.
4. A new floor is set for `app/activitypub/util.py` in `coverage_floors.ini`. It has
   none today — this sub-project establishes the first one, measured and rounded down.
   `app/utils.py`'s floor of 73 stays untouched.
5. The suite stays green. Baseline at `0a20752f` is 2238 passed, 3 skipped, 0 failed,
   with the plain command `./run_tests.sh tests/ -q` — the `--ignore` was removed at
   that commit and must not come back.
6. Every `file:line` in committed documentation is verified against the current file,
   or omitted in favour of a name.

## Risks

- **Fixture weight.** A peer actor document has many optional fields, and three actor
  types multiply them. Build the minimum each case needs and share a base document;
  1b-ii's six test files each reinvented `make_instance` + `make_user` and that
  duplication is still open as a DRY target.
- **`actor_json_to_model` is 372 lines with three top-level branches.** It is the
  largest single function this campaign has targeted. If its three actor types prove to
  need three separate task-sized efforts, split them rather than writing one
  unreviewable test file.
- **Cache interference.** `find_actor_or_create_cached` is `@cache.memoize`d, and the
  repair at `0a20752f` established that `NullCache` makes memoize a no-op — so a
  caching assertion passes vacuously unless a real cache is arranged. That file's
  `real_cache` fixture is the model.
- **Existing tests encode current behaviour.** `tests/test_activitypub_util.py` was
  rewritten at `0a20752f` and asserts things about `find_actor_or_create`. If this
  sub-project's work contradicts one, that is a finding to investigate, not a test to
  quietly adjust.
