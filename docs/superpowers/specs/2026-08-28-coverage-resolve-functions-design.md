# Coverage: the three remote-object resolvers

Date: 2026-08-28
Status: Draft design, awaiting owner review

Sub-project of the campaign in
`docs/superpowers/specs/2026-08-25-coverage-campaign-foundation-design.md`.
Read `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` first — it is
short, it governs how this work is reviewed, and it already contains four registered
defects in the functions this sub-project covers.

Predecessors: 1a, 1b-i, 1b-ii, 1c, 2a, and the tier 1-4 fix sub-project, all merged to
`blentz`, most recently at `705bde91`.

## Why these three, and why now

Measured at `705bde91` from the campaign's own `coverage.json`:

| function | statements | branch arcs |
|---|---|---|
| `resolve_remote_post` | **0 / 11** | 0 / 4 |
| `create_resolved_object` | 25 / 59 | 10 / 44 |
| `resolve_remote_post_from_search` | **0 / 72** | 0 / 46 |
| **total** | **25 / 142** | **10 / 94** |

Two of the three have never executed a single line under test.

They are next for a specific reason rather than because they are uncovered. Sub-project
2a's spec argued that `app/activitypub/util.py` deserved priority because every function
in it processes peer-controlled input. These three are where that argument is sharpest
and the evidence thinnest:

- **Four defects are already registered inside them** — D21, D22, D23 and D24 — and none
  can be safely fixed today, because a fix to code with no tests is a change nobody can
  verify. This sub-project is what makes those fixable. That is its main purpose; the
  coverage number is the by-product.
- **One of them lies about its own trust boundary.** `resolve_remote_post_from_search`
  carries a comment saying it is called from the UI. It is also called from the `Move`
  handler inside `process_inbox_request`, with `core_activity['object']` — a
  peer-supplied string. A future reader trusting that comment would reason about the
  wrong threat model.
- **A fifth defect spans two of them.** The `posted_at` enrichment in
  `create_resolved_object` and `resolve_remote_post_from_search` assigns
  `post_data['published']` straight from the peer document and commits, so a `published`
  value that is not a timestamp fails at flush with the post already committed.

## Scope

The three functions named above, to 100% statement and branch coverage or a documented
reason, with both mutation directions.

### Explicitly in scope: the duplication

D23 records that `create_resolved_object` and `resolve_remote_post_from_search` are
textual near-duplicates, including both `attributedTo` walks, and that **fixing one
leaves the other**. That is a testing hazard before it is a maintenance one: a test suite
written against one function will look thorough and cover neither the other's copy nor
the divergences between them.

So the enumeration must be done **per function**, not once and applied twice, and the
sub-project must report where the two copies have drifted apart. Where they have, say so
in the test docstrings — a later reader deduplicating them needs to know which
differences are deliberate.

### Out of scope

- **Fixing D21-D24 or the `posted_at` defect.** This sub-project verifies and reports;
  fixing is a separate owner decision, as it has been for every defect this campaign has
  found. The difference is that after this work those fixes become *safe*, which they are
  not today.
- The eleven partially-applied-ingest candidates from the sweep at
  `.superpowers/sdd/fix-ingest-shape-sweep.md`, except S8 where it lands in these two
  functions. They are registered separately.
- `process_microblog_announce` and the other callers, except as far as tracing them is
  needed to establish what a peer controls.
- The background `refresh_*_profile_task` functions, still out of scope as they were in
  2a.

## What makes these harder to test than 2a's targets

2a's functions took a dictionary parsed from a peer's JSON, so most of their tests needed
no network at all. **These three fetch before they parse, and then write.** Each test
therefore needs a mocked HTTP conversation *and* a database assertion.

- `http_mock` (respx) is the established fixture. `block_outbound_http` is session-scoped
  and autouse, raising on any unmatched request, so a forgotten route surfaces as an
  error rather than reaching the network.
- These functions call `create_post` and `create_post_reply`, which **commit**. Tests must
  assert on rows, and must distinguish a row the function under test created from one a
  helper created — the campaign has already misattributed one defect by missing that
  distinction.
- `resolve_remote_post_from_search` has a NodeBB branch reached through a `nodebb = True`
  guard. The sweep established that its `totalItems` and `orderedItems` reads are
  validated by that guard, so it is not the hazard it resembles — but it is a distinct
  path needing its own fixtures.

## The two-caller problem, which the tests must respect

`create_resolved_object` is reached from three callers and **its severity differs between
them**. On the alpha API path `server` is lowercased and `actor_domain` is not, so the
comparison refuses systematically; on the two inbox paths both sides are raw, so refusal
depends on the peer being internally inconsistent.

A test suite that exercises only one caller will report the function as covered and pin
only one of its two behaviours. Cover both, and say in the docstrings which caller each
case represents.

## The quality bar

Inherited, and restated because two of these functions start from zero:

- **For every test, name the production change that would make it fail.** If you cannot
  name one, the test is decoration.
- **Both mutation directions**, and classify the guard's shape first. This campaign has
  now produced five distinct answers — an early-return guard where deleting and
  broadening are the same mutation; a `continue`-shaped guard where they are genuinely
  distinct; a positive enabling gate with one direction; an `except` handler where
  broadening was not distinct for the tests at hand; and a conditional expression that
  emits no branch arc at all. Report only directions that are genuinely distinct.
- **Coverage cannot see inside strings.** Four `.lower()` calls in `find_community` were
  deletable with the whole suite green until someone tried it. These functions compare
  and normalise host strings throughout.
- **A count quoted in prose is a claim.** Eight enumerations in this campaign were wrong
  when re-derived, including one in the sweep that fed this spec.
- Tests assert observable behaviour — what the function returned, which row exists — never
  on generated SQL, never on mocks.

## Verification

1. The three functions reach 100% statement and branch coverage, or carry a documented
   reason. An unreachability claim must state what was tried and failed.
2. Every guard has both genuinely distinct mutation directions, with counts.
3. D21-D24 and the `posted_at` defect are each **confirmed still present** against the new
   tests, or shown to have been misdescribed. A defect that turns out not to exist is as
   valuable a finding as one that does.
4. Where the two near-duplicate functions have drifted, the divergences are recorded.
5. `app/activitypub/util.py`'s floor rises from 35 to the new measured value, rounded
   down. The other three floors stay untouched, and the file is edited additively.
6. The suite stays green. Baseline at `705bde91` is 2570 passed, 3 skipped, 0 failed.

## Risks

- **Fixture weight.** Each test needs a mocked fetch and a database assertion. Build the
  minimum each case needs and share through `tests/factories.py`, which has grown across
  five sub-projects for exactly this reason.
- **The near-duplicates invite one test file for both.** Resist it. Two files, two
  enumerations; a shared fixture is fine, a shared enumeration is not.
- **Helper commits make row attribution ambiguous.** `create_post` and `create_post_reply`
  commit. An assertion that a `Post` exists does not establish which code wrote it.
- **These functions are the last untested path into `create_post`.** Tests here may
  surface defects in code well outside this sub-project's scope. Register them; do not
  chase them.
