# Coverage Campaign 1c: `app/utils.py` link and domain handling

Date: 2026-08-27
Status: Approved design, ready for implementation planning

Sub-project of the campaign in
`docs/superpowers/specs/2026-08-25-coverage-campaign-foundation-design.md`.
Read `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` first — it is
short and it governs how this work is reviewed.

Predecessors: 1a (pure and context-only functions), 1b-i (permission and
authorisation functions), 1b-ii (feed and query machinery). All merged to `blentz`
at `922f55ae`.

## This sub-project replaces the planned 1b-iii/1c split

The foundation design partitioned the remainder of `app/utils.py` into 1b-iii
(~445 DB-backed statements) and 1c (~248 I/O statements). **That partition predates
1b-i and 1b-ii and no longer describes what is left.** Re-measured against
`922f55ae`:

```
coverage.json → files['app/utils.py']
  percent_covered = 67.6478%   missing_stmts = 848   num_statements = 2849
```

848 uncovered statements spread across 116 top-level functions. The DB-backed group
1b-iii was scoped for is now the *smallest* of the three remaining clusters, because
1b-i and 1b-ii consumed most of it. This is the sixth count in this campaign to be
wrong when re-derived, and the reason this document derives every figure with a
command rather than carrying one forward.

The remaining work clusters by **purpose**, not by dependency class:

| cluster | approximate uncovered statements |
|---|---|
| I/O (S3, image, HTTP fetch) | ~310 |
| link and domain handling | 92 |
| notifications | 32 |
| DB-backed remainder | ~100 |
| long tail (106 functions) | remainder |

Grouping by purity was considered and rejected: of the five functions below, only
`remove_tracking_from_link` is pure. `fixup_url` performs a DB query *and* an HTTP
fetch; `rewrite_href` and `apply_feed_url_rules` are DB-backed; `domain_from_url`
writes. A partition along "pure / DB / I/O" lines would split a coherent behaviour
across three sub-projects.

## Scope

| function | lines | uncovered |
|---|---|---|
| `domain_from_url` | 1442-1455 | 12 |
| `remove_tracking_from_link` | 3081-3103 | 14 |
| `fixup_url` | 3108-3165 | 23 |
| `apply_feed_url_rules` | 4484-4514 | 23 |
| `rewrite_href` | 4966-4989 | 20 |
| **total** | | **92** |

Line ranges derived by AST walk over `app/utils.py` at `922f55ae`; uncovered counts
from the same `coverage.json` quoted above.

These five decide what a link *means* — which domain a post is attributed to, which
URL gets embedded, where an href resolves. That is the surface on which this campaign
has already found an open-redirect family, a `javascript:` scheme bypass, and a
WHATWG normalization gap.

### Out of scope

- The I/O cluster (`archive_post` 84, `url_to_thumbnail_file` 60, `move_file_to_s3`
  42, `parse_page` 24). Needs `moto` fixtures that do not exist yet; its own
  sub-project.
- `notif_id_to_string` (32). A different domain.
- The DB-backed remainder and the long tail.
- Backfilling existing `Domain` rows. See "Federation implications" below.

## The confirmed defect this sub-project opens with

`app/utils.py:1443`:

```python
parsed_url = urlparse(url.lower().replace('www.', ''))
```

The replacement runs over the **whole URL string, before parsing**, and removes every
occurrence rather than a leading host label. Probed at `922f55ae`:

```
https://www.example.com/a      real=www.example.com    recorded=example.com
https://awww.evil.com/a        real=awww.evil.com      recorded=aevil.com     MISMATCH
https://wwww.example.com/a     real=wwww.example.com   recorded=wexample.com  MISMATCH
```

`domain_from_url` assigns `post.domain_id` at `app/shared/post.py:192,443,566`,
`app/models.py:2013`, and `app/activitypub/util.py:3125`. `Domain` carries `banned`
(site-wide admin ban) and is the target of `DomainBlock` (per-user block,
`app/models.py:3320-3357`) — the same filter covered by 1b-ii's Task 4.

Two consequences:

1. **Collision.** Distinct hostnames collapse into one `Domain` row. `awww.evil.com`
   and `aevil.com` become the same row, so banning either bans both.
2. **Ban evasion.** With `evil.com` banned, `www.evil.com` mangles *to* `evil.com`
   and is caught — but `wwww.evil.com` mangles to `wevil.com`, which is not banned.
   An operator whose domain is banned can serve from a `wwww.` subdomain they
   already control.

### Fix first, then cover

The sub-project's first task **fixes this under TDD**, and the remaining tasks cover
corrected behaviour.

Writing tests against a function known to be broken encodes the defect as expected
behaviour, and the fix then invalidates them. This campaign has paid that cost twice:
1b-ii Task 8's `..._still_writes_a_wasted_cache_entry` had to be rewritten when the
cache defect was fixed, and Task 7's module docstring had enumerated the
`possible_communities` predicates as exactly three, encoding a missing private filter
as settled fact.

The fix: strip `www.` from the **parsed hostname**, as a prefix only, after parsing.
Its blast radius is the five `post.domain_id` call sites above, so it carries the
same discipline as the four security fixes merged at `922f55ae` — failing test first,
watched to fail on the defect rather than on an error, and a revert check confirming
the regression test is load-bearing.

### Federation implications

The fix changes **future attribution only**. Existing `Domain` rows that were
mis-attributed stay as they are. No migration, no backfill. An operator who has
banned a domain whose row was created under the old mangling keeps that row's
behaviour until a new row is created for the corrected hostname.

This is a deliberate choice, recorded so a later reader does not mistake it for an
oversight. Backfilling would require deciding what to do with `DomainBlock` rows,
`post_count` totals, and posts already attributed — a data migration with its own
risk, out of proportion to a fix that stops the bleeding.

## Five functions, five shapes

These do not share a testing shape, which is why they are separate tasks.

**`domain_from_url`** — DB write. Both `create=True` and `create=False` paths, the
`youtu.be` → `youtube.com` alias, the `None` return for an unparseable URL, and host
normalization. Fuzz target.

**`remove_tracking_from_link`** — the only pure function here. The `youtu.be`
rewrite, the `t=` → `start=` parameter rename, and passthrough for every other host.
Fuzz target.

**`fixup_url`** — DB query plus an HTTP fetch to a peertube instance, then a YouTube
URL matrix: `/shorts/`, `/watch?v=`, `/playlist`, `/post/`, bare path, and the
`start`/`t` timestamp parameters. Mock the fetch with `respx`.

Two bare `except:` clauses (`app/utils.py:3124`, `3126`) swallow everything. Each
needs a case establishing what it actually swallows — a bare `except:` also catches
`KeyboardInterrupt` and `SystemExit`. **Report that; do not fix it.** It is outside
the defect this sub-project addresses.

**`rewrite_href`** — a four-branch if/elif over ActivityPub id lookups, with an
`else` that re-queries. The failure mode is the one named in 1b-ii's spec for
`continue` chains: a branch chain reaches full branch coverage while most *rules*
stay unexercised. **One case per rule, not one per branch.**

**`apply_feed_url_rules`** — a form validator bound to `self` and reading
`current_user`. Needs a form instance, not a plain call. Covers public and private
URL shapes, the `-` rejection, the alphanumeric regex, and the uniqueness query in
both its `feed_id`-present and `AttributeError` forms.

It **mutates `self.url.data` before validating it**. Tests must assert on the
mutation as well as on the return value, or half the function is untested.

## Depth

Full coverage with both mutation directions for all five. Unlike 1b-ii, there is no
lighter tier here: every function in scope influences either domain attribution
(which drives blocking) or link resolution (which drives navigation). A defect in any
of them is a correctness or safety failure, not an annoyance.

## Fuzzing

`atheris` is already in `requirements-test.txt` (moved there in 1a after it broke
ARM64 production installs from `requirements.txt`). Fuzz `domain_from_url`,
`remove_tracking_from_link`, and `fixup_url`'s URL parsing.

The property under test is **no unhandled exception and no host confusion**: the
recorded domain must equal the parsed hostname with at most a leading `www.` removed.
That property is checkable without an oracle, which is what makes fuzzing worthwhile
here rather than decorative.

## Primary sources

Expectations come from specifications where one exists:

- **WHATWG URL Standard** — host parsing and normalization.
- **RFC 3986** — generic URI syntax, and the authority component in particular.

YouTube's URL formats have no specification. Derive expectations from the formats the
code already handles, and **state in the test docstring that the source is observed
behaviour rather than a spec** — so a later reader knows which expectations are
authoritative and which are descriptive.

## The quality bar

Inherited, and restated because this sub-project decides what links mean:

- **For every test, name the production change that would make it fail.** If you
  cannot name one, the test is decoration.
- **Both mutation directions.** Deleting a rule proves an *absence* test is
  load-bearing and says nothing about a *presence* test, because the deleted code is
  unreachable in the presence case. Only over-broadening — making a rule fire for
  everything — can fail a presence test. Three tasks in 1b-ii shipped with only the
  delete direction before this became standard.
- **Wide blast radius needs the narrow mutation on the same rule.** In a branch
  chain, a rule firing universally short-circuits every downstream test, so wide
  radius is expected geometry. It is *also* the symptom of hidden fixture coupling —
  1b-ii found tests sharing state through an unset column default. The paired narrow
  mutation is what distinguishes them.
- **Coverage cannot see inside strings.** 1b-ii found a SQL predicate
  (`OR p.language_id is null`) completely untested while coverage read 100%. Regex
  alternations and URL-matching string literals in this sub-project have the same
  property. Enumerate them; do not trust the percentage.
- **A count quoted in prose is a claim.** Six enumerations in this campaign were
  wrong when re-derived. Derive counts with a command and quote the command.
- Tests assert observable behaviour — what the function returns, which `Domain` row
  exists — never on generated SQL, never on mocks.

## Project rules

1. `if TYPE_CHECKING` is always a bug. Never introduce it, and never add it to
   `.coveragerc`'s `exclude_lines`.
2. Imports go at the top of the file. No inline imports — `app/utils.py` has 216
   catalogued pre-existing violations; this campaign adds none.
3. Every pragma carries a written justification.
4. No new runtime dependencies, no migration.
5. Report defects; do not fix them without owner authorisation. The
   `domain_from_url` fix is authorised; nothing else here is.

## Verification

1. The five functions reach 100% statement and branch coverage, or carry a documented
   reason. An unreachability claim must state what was tried and failed to reach it —
   1b-ii's two dead arms in `possible_communities` were accepted only after a
   reviewer instrumented them and attacked from four directions.
2. Every rule has both mutation directions, with counts reported.
3. `domain_from_url`'s regression test is proved load-bearing by reverting the fix
   alone and watching it fail.
4. `app/utils.py`'s floor rises from 67 to the measured figure, rounded **down**.
   Floors only rise. If the measurement does not support a rise, leave it — 1b-i's
   Task 9 correctly left its floor unchanged rather than inflate it.
5. The suite stays green. Baseline at `922f55ae` is 1832 passed, 3 skipped, 0 failed.
6. Every `file:line` cited in committed documentation is verified against the current
   file. Stale references are this campaign's signature defect in its own artefacts —
   nine were found in committed docs in 1a, and a phantom filename in `tests/README.md`
   propagated into a review briefing in 1b-ii before anyone checked it.

## Risks

- **Line-number churn.** Fixing `domain_from_url` shifts every line below it. 1b-ii's
  security fixes shifted `app/utils.py` twice — once uniformly (+7), once piecewise
  (+13/+0/+10) — and both required proving the shift's shape before bulk-updating
  docstring references. Expect the same here.
- **`fixup_url`'s network path.** `respx` blocks httpx by default in this suite
  (`block_outbound_http`, session-scoped autouse). A test that forgets to register a
  route gets a connection error rather than a hang, which is the desired failure —
  but the peertube branch also depends on a DB row (`instance.software = 'peertube'`),
  so both halves must be seeded.
- **`current_user` coupling.** `apply_feed_url_rules` reads `current_user.user_name`
  and builds a regex from it. A username containing regex metacharacters would change
  the pattern's meaning — worth a test, and possibly a defect to report.
- **Fixture weight.** Keep fixtures minimal. 1b-ii's six files added 147 tests for
  10.5 seconds of runtime; that ratio is the target.
