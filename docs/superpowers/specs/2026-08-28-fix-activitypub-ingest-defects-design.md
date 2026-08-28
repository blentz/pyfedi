# Fixing the actor and object ingestion defects, tiers 1-4

Date: 2026-08-28
Status: Draft design, awaiting owner review

Sub-project of the campaign in
`docs/superpowers/specs/2026-08-25-coverage-campaign-foundation-design.md`.
The defects fixed here were found and documented by sub-project 2a; their register
and the analysis behind each is in
`docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`, section 6.

Predecessor: 2a, merged to `blentz` at `added1b7`.

## Why this now, and why it is a different kind of sub-project

Every sub-project in this campaign so far has added tests and changed no production
code. This one inverts that: it changes production code and changes tests only where
a fix makes an existing test's assertion wrong.

That inversion is the point. Sub-project 2a left twenty defects reported and unfixed,
which was the correct instruction at the time — a defect found by a test author is a
finding, and whether to act on it is the owner's decision, not the finder's. That
decision has now been made for fifteen of them.

It is also the first sub-project where the tests come first for free. Every defect
below already has at least one test pinning the **current, defective** behaviour,
written by whoever found it. So each fix begins from a known-red state without anyone
writing a new failing test: change the code, and a specific named test fails. That is
as close to test-first as a fix campaign gets, and it means an accidental fix — one
that changes behaviour nobody characterised — shows up as a test failing that nobody
predicted.

## Scope: fifteen defects in four tiers

Tier boundaries are by fix shape and risk, not by severity alone, because tasks are
drawn around work that can be reviewed as a unit.

| tier | defects | function(s) | shape |
|---|---|---|---|
| 1 | D10, D11, D1, D4 | `actor_json_to_model`, `ensure_domains_match`, `verify_object_from_source` | host comparison |
| 2 | D9, D13, D16 | `find_flair_or_create`, `actor_json_to_model` Group and Feed | partially-applied ingest |
| 3 | D2, D3, D12, D14, D15, D17 | `find_community`, `actor_json_to_model` Group and Feed | crash on malformed document |
| 4 | D5, D7 | `verify_object_from_source` | swallowed errors, undifferentiated refusals |

Cite these by function and behaviour rather than by ordinal. The register's own
numbering is not stable across summaries, and this campaign has already produced two
documents that disagree about which number the `ap_following_url` finding carries.

### Out of scope

- **Tier 5** — D6, D8, D18, D19, D20. Cosmetic, dead code, or a deployment state. They
  stay in the register, reported and unfixed.
- **Remediating rows already in the database.** Tier 1 ships a read-only audit query
  (below); what to do about anything it finds is a separate decision.
- **The same host-comparison shape in three functions outside 2a's scope** — see the
  next section. This sub-project reports them; it does not fix them.
- The Update, Create, Delete, Ban/Flag and Announce handlers, which remain sub-projects
  2b-2e.

## A finding that emerged while scoping this work

The `netloc`-instead-of-`hostname` comparison is not confined to the two functions
2a examined. Derived against `blentz` at `added1b7`:

```bash
grep -n '\.netloc' app/activitypub/util.py | wc -l          # 20 lines, one a comment
```

Seven of those reads are in the two in-scope functions — two in `ensure_domains_match`,
five in `verify_object_from_source`. **Ten more are in three functions this campaign
has never tested**: `resolve_remote_post`, `create_resolved_object`, and
`resolve_remote_post_from_search`.

They are the same *shape*. Whether each is the same *defect* is a different question,
and 2a's own headline lesson is exactly why: for both suspected defects the spec named
in advance, the call-site analysis reversed the predicted severity. Reading a
comparison tells you what it does; reading its callers tells you what it is worth.

So this sub-project **verifies and registers** those ten reads and fixes none of them.
Fixing untested code is how this campaign would start producing the defects it exists
to find. They become scope for whichever sub-project covers those functions.

## Design decisions

### 1. One helper, used at every fixed comparison

All four Tier 1 defects are the same mistake — comparing an authority string where a
host comparison was meant. Rather than repeat a guarded `urlparse(...).hostname` at
seven sites, add one module-level helper to `app/activitypub/util.py` returning the
lowercased host of a URL, or the empty string when there is none.

It must guard `urlparse` with `except ValueError`. Sub-project 1c established that
`urlparse` raises on a malformed netloc — an unbalanced IPv6 bracket, two `::` runs, a
host failing its NFKC confusability check — and every string reaching these functions
is chosen by a remote peer. The existing `extract_domain_and_actor` in the same module
already handles this, with a written justification for the value it degrades to; the
new helper follows it, and the comment explaining why is not optional.

Returning `''` rather than `None` matters: two failed parses must not compare equal.
Two empty strings would. The helper's callers therefore treat an empty result as a
refusal, and the tests must pin that — a malformed id and a malformed actor must not
be accepted as a matching pair.

### 2. Comparing hosts means same host on two ports is one host

`hostname` drops the port. That is the fix for D1 and D4, where a peer inconsistent
about its port is currently refused. Applying the same helper to D10's gate means an
actor id on a different port than the address PieFed fetched from now passes.

This is a deliberate loosening, and it is the one Tier 1 decision a reviewer should
feel free to challenge. The argument for it: an instance serving on two ports is one
instance, the campaign's own probe showed the port row is a false *reject* rather than
a hole, and a single consistent rule across all seven sites is worth more than a
per-site judgement nobody will remember. The argument against is that it discards a
signal. If the reviewer prefers keeping the port, the helper grows a parameter and the
tests double; that is a legitimate outcome of review, not a defect in the plan.

### 3. Guard and skip, for the three partially-applied-ingest defects

Owner decision. A malformed optional entry is treated the way the surrounding code
already treats every other optional key: test for it, skip it, continue. The
`Community` or `Feed` row lands with the good entries and without the bad one, and no
caller sees a new exception.

This is the smallest diff and it matches the local idiom — in each of the three cases,
the unguarded read sits directly beside optional reads that *are* guarded, which is
what made the asymmetry a defect rather than a design.

**It has a cost that must be paid in the same task:** a skipped entry is a peer's data
being silently dropped. Sub-project 2a's founding example was a dropped federated post
that left no trace anywhere. So each skip logs, and the Tier 4 work that makes those
logs distinguishable is a **prerequisite**, not a sibling — Tier 4 lands before Tier 2.

D9 additionally has to hold for both callers. It is reachable from
`refresh_community_profile_task`, whose session comes from `get_task_session()` with
autoflush at its default of on, and unreachable from `actor_json_to_model`, which uses
`db.session` where the application factory sets `autoflush: False`. The fix must be
correct under both, and the test must exercise the autoflush-on path, because that is
the only one where the defect fires.

### 4. `verify_object_from_source` returns a reason

D7 is ten distinct refusal paths collapsing into one log line at the single caller.
The function has exactly one caller and ten `return None` statements — derived:

```bash
# an invocation is the name followed by '(' -- this excludes the wrapped
# import in routes.py, which names it without calling it
grep -rn 'verify_object_from_source(' app/ --include=*.py | grep -v 'def '
```

Change the return to a two-tuple of the object and a reason string, and have the caller
pass the reason into the `log_incoming_ap` call it already makes. This keeps the
existing logging channel, invents no new one, and makes each refusal identifiable in
the table operators already read.

A single caller is what makes this contained. Were there five, the right answer would
be different.

### 5. D5 is a one-word change

The two bare `except:` clauses wrap only the JSON parse. `JSONDecodeError` is already
imported at the top of the module, so the fix names it and adds no import.

Sub-project 1c established the general hazard — a bare `except:` swallows respx's
`AllMockedAssertionError`, so a test written to catch a mutation passes under it. Note
for the implementer that 2a measured this specific pair and found them *not* resistant,
because the fetch itself is guarded separately by `except httpx.HTTPError`. The fix is
worth making regardless; the mutation-resistance argument is not the reason, and
repeating it as though it were would put a wrong claim into a third document.

### 6. Tier 1 ships a read-only audit query

Owner decision. Closing the substring gate stops new cross-host actors being minted; it
says nothing about rows already present. `User`, `Community` and `Feed` each carry
`ap_profile_id` and `ap_domain`, so a query can report rows where the profile URL's
host disagrees with the recorded domain.

It reports and changes nothing. It is not a migration. Where it lives — a management
command, or a documented query in `tests/README.md`'s neighbourhood — is the
implementer's call, provided running it cannot modify a row.

## The testing shape

**Every fix flips at least one existing test from green to red.** That is the safety
net and the main source of work.

For each defect the implementer must, in order: name the test that pins the current
behaviour and run it; make the production change; watch that named test fail; update
its assertion to the new intended behaviour; and confirm no *other* test failed. A test
failing that was not predicted means the change did more than intended.

Two things follow from this that the implementer must not get wrong:

- **Updating a pinning test is a deliberate act with a written reason.** D20's tests
  open with `PINS PRESENT BEHAVIOUR, NOT DESIRED BEHAVIOUR` precisely so a later reader
  fixes the code rather than the test. The tests touched here carry no such warning,
  but the same discipline applies: the commit message says which defect the changed
  assertion corresponds to.
- **A fix that flips no test is a fix nobody characterised.** Stop and work out why
  before continuing. Either the defect was not really pinned — in which case pin it
  first, watch it fail, then fix — or the change does not do what it appears to.

Mutation testing still applies to newly guarded paths, in both directions, as
established across four sub-projects: deleting a guard proves an absence test is
load-bearing, and only over-broadening can fail a presence test.

## Depth

Full, for every defect in tiers 1-4. There is no lighter tier here: each of these
functions decides whether to trust a remote claim or writes a row on the strength of
one, and every fix is small enough that the test work dominates it anyway.

## Primary sources

- **ActivityPub** (W3C Recommendation) — actor requirements and the `id` property.
- **RFC 3986**, section 3.2 — the authority component, and why `netloc` and `hostname`
  differ. This is the authority for the Tier 1 rewrite and should be cited in the
  helper's docstring.
- **ActivityStreams 2.0 Core** — `attributedTo` and the shapes it may legitimately take,
  which `ensure_domains_match` already handles three of.

## Project rules

Inherited unchanged from the campaign, and all of them have been violated at least once
in a preceding sub-project:

1. `if TYPE_CHECKING` is always a bug. Never introduce it, and never add it to
   `.coveragerc`'s `exclude_lines`.
2. Imports go at the top of the file. No inline imports.
3. Every pragma carries a written justification.
4. Named exceptions only — never a bare `except:`.
5. No new runtime dependencies, no migration.
6. **Do not cite line numbers** in docstrings or documentation. Identify code by name.
   Violated fifteen times inside sub-project 2a and caught by no reviewer until it
   became a named item in the return contract; it held after that.
7. A count quoted in prose is a claim. Derive it with a command and quote the command.
8. Report defects; do not fix them without owner authorisation. That authorisation
   exists for the fifteen in scope here and for nothing else.

## Verification

1. All fifteen defects fixed, each with the named test that flipped and the commit that
   flipped it.
2. No test failed that was not predicted, across the whole suite.
3. The ten out-of-scope `.netloc` reads verified and registered, with the call-site
   analysis that determines each one's severity — not merely listed.
4. `app/activitypub/util.py`'s coverage floor of 35 holds or rises. It must not fall:
   these fixes add guarded branches, and a guard without a test lowers the number.
5. The audit query runs read-only and its output is recorded.
6. The suite stays green. Baseline at `added1b7` is 2549 passed, 3 skipped, 0 failed.
7. The register in the campaign findings doc is updated: fifteen marked fixed with their
   commits, five still open, and the new entries added.

## Risks

- **A fix that changes federation behaviour with real peers.** Tier 1 tightens what
  `actor_json_to_model` accepts. A peer whose actor id host differs from the address it
  is served from stops resolving. That is the intended effect, and the audit query is
  how you find out whether it affects anyone already in the database — but it is the one
  change here that a production instance could notice.
- **Fifteen fixes in two files.** Order matters more than usual: Tier 4 lands first
  because Tier 2's guard-and-skip depends on its logging, and Tier 1's helper is
  consumed by nothing else, so it can land in parallel.
- **Updating pinning tests is where a fix campaign goes wrong.** The failure mode is
  adjusting an assertion until it passes rather than to what the fix intends. Every
  changed assertion needs its reason in the commit message, and a reviewer who checks
  the new assertion against the spec's intent rather than against the new code.
- **The autoflush geometry behind D9** is not local to the defect. A fix that only
  satisfies `actor_json_to_model`'s caller will look correct and leave the reachable
  path broken.
