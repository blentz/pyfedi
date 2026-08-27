# Coverage Campaign 1a: `app/utils.py`, pure and context-only functions

Date: 2026-08-25
Status: Approved design, ready for implementation planning

Sub-project 1a of the campaign described in
`docs/superpowers/specs/2026-08-25-coverage-campaign-foundation-design.md`.
Findings carried forward from sub-project 0 are in
`docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` — read that
first; it is short and it governs how this work is reviewed.

## Why this slice

The campaign spec grouped `models.py`, `utils.py`, `cli.py` and `email.py` as a
single "top level" sub-project of ~8,845 statements. Measured against the merged
foundation branch, the real figure is 7,898 statements with 4,755 uncovered —
still four sub-projects' worth of work, not one. Splitting it is a correction to
the campaign spec, not a deviation from it.

`app/utils.py` alone holds 2,770 statements, 1,645 uncovered, 1,208 branches, at
35.0%. Partitioning its 195 gapped functions by what they actually touch:

| group | functions | uncovered statements |
|---|---|---|
| pure — no DB, no network, no request context | 45 | 267 |
| context-only — needs a request context, no DB | 20 | 81 |
| DB-backed | 116 | 937 |
| I/O — network, S3, images from disk | 14 | 360 |

**This sub-project covers the first two groups: 65 functions, 348 uncovered
statements.** The other two become sub-projects 1b and 1c.

Pure functions go first for a reason worth stating, because it is the opposite
of the obvious ordering. Sub-projects 2-14 will raise `utils.py` coverage
incidentally as they exercise it — so some work done here would have happened
anyway. But incidental coverage follows the happy path. It does not reach the
error branches and rejected-input paths that make up most of this tail, and it
never reaches a pure function nothing currently calls. The pure slice is the
part least likely to be covered for free.

## Scope

### In scope

1. The 45 pure and 20 context-only functions listed in the partition, to 100%
   statement and branch coverage each.
2. **Fuzz testing of the security-relevant functions**, with `atheris`. See the
   dedicated section below — this is new capability, not just more tests.
3. Raising `app/utils.py`'s ratchet floor to the level actually achieved.
4. Reporting any function in scope that turns out to be dead, misclassified, or
   untestable in place.

### Out of scope

- The 116 DB-backed and 14 I/O functions (sub-projects 1b and 1c).
- `app/models.py`, `app/cli.py`, `app/email.py` and the other top-level modules.
- **Fixing anything the fuzzing finds.** See "If fuzzing finds a real
  vulnerability" below. Reporting is in scope; a security fix to production code
  is a separate decision.
- The pre-existing inline imports, including the `import c2pa` inside
  `inspect_image_c2pa` (`app/utils.py:5488`) noticed while partitioning. It
  violates the project rule and belongs to the catalogued inline-import project.

## The partition is measured, but it is a heuristic

The four groups above come from classifying each gapped function by regex over
its body — matching on `db.session`, `.query(`, model names, `get_request`,
`httpx`, `boto3`, `request`, `current_app`, and the `@cache.memoize` decorator.

That is good enough to plan from and not good enough to trust per-function.
Known suspects, all currently classified `db` on matches that may be incidental:
`notif_id_to_string` (32 uncovered), `rewrite_href` (20),
`remove_tracking_from_link` (14).

**Every task verifies its own functions before testing them.** A function that
turns out to need a database or the network moves to 1b or 1c and is reported —
it is not forced into this sub-project with a mock to keep the count up. A
function currently in `db` or `io` that turns out to be pure may be pulled in,
which is a bonus rather than an obligation.

## Fuzz testing

Four functions in scope exist to reject hostile input, and two of them parse
untrusted bytes by hand:

- `is_valid_xml_utf8` (`app/utils.py:4659`, 21 uncovered) — a hand-rolled UTF-8
  byte scanner looking for forbidden codepoints and surrogates. It indexes
  `s[i+1]` and `s[i+2]` inside a `while i < c_end - 2` loop and then runs a
  second loop over the tail. Hand-written index arithmetic over attacker-supplied
  bytes is the canonical fuzz target.
- `sanitize_svg_bytes` (`app/utils.py:5364`, 9 uncovered) — strips XML
  declarations and processing instructions by regex to prevent XXE and billion
  laughs, then delegates to `py-svg-hush`'s `filter_svg`.
- `sanitize_svg` (`app/utils.py:5433`, 11 uncovered) — the file-based wrapper.
- `allowlist_html` (`app/utils.py:318`, 6 uncovered) — the XSS boundary for all
  remote content.

### Design

`atheris` 3.1.0 publishes a cp313 manylinux wheel; verified downloadable in this
project's test container, which runs Python 3.13.15. It is a test dependency,
added alongside `respx`, `moto` and `fakeredis`.

Fuzzing runs in **two modes**, and the split is the important part:

**Mode 1 — the campaign, run on demand.** Harnesses live in `tests/fuzz/`, one
per target. They are NOT collected by the default suite. A documented command
runs them with an explicit budget (`-max_total_time` or `-runs`). This is where
new inputs are discovered.

**Mode 2 — the corpus, run every time.** Every input that trips an assertion is
saved to `tests/fuzz/corpus/<target>/` and committed. A normal pytest test
iterates that directory and replays each input deterministically. Findings
become permanent regressions, and the default suite stays fast and reproducible.

The default suite must remain deterministic and roughly its current 10 seconds.
Fuzzing is nondeterministic by nature, so nothing nondeterministic runs in it.

### Assert properties, not absence of crashes

"It did not crash" is a weak oracle and would let a sanitizer that returns its
input unchanged pass. Each harness asserts a property:

- `is_valid_xml_utf8` — returns a `bool` for any `bytes` or `str` input, and
  never raises. `IndexError` from the hand-rolled indexing is the specific bug
  being hunted.
- `sanitize_svg_bytes` — output contains no `<script`, no `on*=` event handler
  attribute, no `javascript:` URL, and no `<!ENTITY`, for any input; and it
  either returns bytes or raises `ValueError` for oversize input, nothing else.
- `allowlist_html` — output contains no tag outside the allowlist, no `on*=`
  attribute, and no `javascript:` URL.

### Interaction with coverage measurement

`atheris` installs its own tracing to guide mutation, and the suite already runs
under `coverage.py`. The two must not fight: fuzz campaigns run **outside** the
coverage run. Corpus replay is ordinary Python and measures normally. If this
turns out to be wrong in practice, that is a finding to report, not something to
work around by disabling coverage.

### If fuzzing finds a real vulnerability

Realistic outcome, given a hand-rolled byte scanner and a regex-based XML
stripper. PieFed is deployed software.

The rule: **report, do not fix.** A crash or property violation is written up
with its reproducing input, and the input is committed to the corpus with the
regression test marked expected-to-fail if it cannot pass yet. Whether to fix it
here, fix it separately, or handle it as a coordinated disclosure is the project
owner's decision, not a coverage sub-project's.

## Tasks

Grouped so each shares setup. Statement counts are current uncovered figures.

1. **Numeric, date and formatting** — `wilson_confidence_lower_bound` (12),
   `days_to_add_for_next_month` (16), `jaccard_similarity` (10), `human_filesize`
   (8), `get_timezones` (8), `round_invisible_digits` (5), `localize_datetime`
   (4), and the smaller helpers in the tail.
2. **URL and domain** — `is_video_hosting_site` (9), `is_image_url` (8),
   `domain_from_email` (6), `mimetype_from_url` (4), `inbox_domain` (4),
   `shorten_url` (3).
3. **HTML and text** — `first_paragraph` (9), `remove_images` (6),
   `allowlist_html` residue (6), `links_with_parens` (4), `reply_is_low_effort`
   (4), `reply_is_just_link_to_gif_reaction` (4), `actor_contains_blocked_words`
   (4).
4. **XML and SVG safety, unit tests** — `is_valid_xml_utf8` (21),
   `sanitize_svg` (11), `sanitize_svg_bytes` (9). Hand-written hostile inputs:
   script-in-SVG, entity expansion, oversize payload, malformed UTF-8,
   truncated multi-byte sequences, lone surrogates.
5. **Fuzz harnesses and corpus** — the `atheris` dependency, `tests/fuzz/`, the
   four harnesses, the corpus replay test, and the documented campaign command.
   Depends on Task 4, so the hand-written cases exist before the fuzzer runs.
6. **Image processing** — `to_srgb` (20), `get_new_frames` (9), `scale_gif` (6),
   `save_new_gif`, on real images generated with Pillow rather than fixtures on
   disk.
7. **Captcha** — `create_captcha` (11), `decode_captcha` (9), through
   `redis_double`.
8. **Request-context helpers** — `ip_address` (7), `requestor_domain` (7),
   `show_ban_message` (5), `referrer` (4), `back` (4), `display_back_button` (3),
   `debug_mode_only` (3), `user_cookie_banned` (2), `block_bots` (1).
9. **Theme and template** — `theme_list` (7), `render_from_tpl` (7),
   `debug_checkpoint` (7), `ensure_directory_exists` (9).

## The quality bar

Inherited from the campaign spec and from sub-project 0's findings, restated
because this sub-project is where the long tail lives and the long tail is where
hollow tests are cheapest to write:

- Every test asserts on observable behaviour — a return value, a written file, a
  raised exception. Never on whether a mock was called.
- **For every test, name the production change that would make it fail.** If you
  cannot name one, the test is decoration. Sub-project 0 shipped a test that
  passed with the entire regex guard deleted from `decode_captcha`; that
  function is in scope here, in Task 7, and its rewritten test is the model.
- An honest gap with a named reason beats a hollow test that reaches the number.
- Branch coverage is not condition coverage. Where a compound condition matters,
  each sub-condition gets its own case.
- Every pragma carries a written justification.
- **Dead code is reported, not tested.** Any in-scope function with no caller
  anywhere in `app/` is a finding and a deletion candidate. Writing a test for
  an uncalled function to move a number is the purest form of the thing this
  campaign's quality bar exists to prevent.

## Project rules

1. `if TYPE_CHECKING` is always a bug. Never introduce it.
2. Imports go at the top of the file. No inline imports. The campaign adds none.
3. No new runtime dependencies. `atheris` is a test dependency.

## Verification

1. The 65 in-scope functions reach 100% statement and branch coverage, or carry
   a documented reason why not.
2. `app/utils.py`'s ratchet floor rises from its current level to the level
   achieved — expected around 47%, set to the measured figure. **Not 100**: this
   is the first sub-project to use the ratchet as a partial-progress gate, and
   1b and 1c raise it further.
3. The full suite stays green and stays fast: 348 tests passed, 1 skipped, 0
   failed in ~10s at the time of writing. (Coincidentally the same number as
   this sub-project's uncovered statements — unrelated quantities.) The test
   count rises as this work lands; the failure count must stay 0 and the
   runtime must stay in seconds.
4. The fuzz corpus replays deterministically in the default suite.
5. A fuzz campaign of at least the documented budget has actually been run
   against all four targets, and its outcome reported — including "found
   nothing", which is a result.

## Risks

- **The long tail invites hollow tests.** 95 of the in-scope functions have
  one to three uncovered statements each, typically an early return or an
  exception branch. These are exactly the cases where a test can execute a line
  without testing anything. The "name the production change" rule is the
  control, and reviewers should apply it hardest here.
- **Fuzzing may find nothing, and that is fine.** A clean campaign against four
  targets is a real result and should be reported as one. The failure mode to
  avoid is running a token campaign and implying broader assurance than a
  60-second budget supports. Report the budget with the outcome.
- **Fuzzing may find something serious.** Covered above: report, do not fix.
- **`atheris` may fight `coverage.py`.** Mitigated by running campaigns outside
  the coverage run. If they conflict anyway, report rather than disable coverage.
- **The partition may be wrong in more places than the three named suspects.**
  Each task verifies before testing; moved functions are findings, not failures.
- **Incidental overlap with later sub-projects.** Some of this work would have
  been done for free by sub-projects 2-14. Accepted deliberately: the error
  branches that dominate this tail are precisely what incidental coverage misses.
