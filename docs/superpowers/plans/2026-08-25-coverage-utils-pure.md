# Coverage 1a: `app/utils.py` Pure Functions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take the pure and context-only functions of `app/utils.py` to 100% statement and branch coverage, and fuzz its four security functions.

**Architecture:** Nine tasks, grouped by the setup their tests share: four groups of pure functions, one security-unit group, one fuzz-infrastructure group, then image, Redis-backed and request-context groups. Tests go in new files under `tests/`, one per task, named for the group. No production code changes except where a task explicitly authorises a bug fix.

**Tech Stack:** pytest, Pillow, `atheris` (new test dependency), the fixtures from sub-project 0 (`app`, `db_session`, `redis_double`, `http_mock`, `block_outbound_http`).

**Spec:** `docs/superpowers/specs/2026-08-25-coverage-utils-pure-design.md`

**Campaign findings that govern this work:** `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` — read it first. It is short.

## Global Constraints

- `if TYPE_CHECKING` is always a bug. Never introduce it.
- Imports go at the top of the file. No inline imports. This plan adds none.
- Every pragma carries a written justification. Unexplained pragmas are rejected.
- Tests assert on observable behaviour, never on whether a mock was called.
- **For every test, name the production change that would make it fail.** If you cannot name one, the test is decoration and must be rewritten. Put that name in the test's docstring where it is not obvious.
- Floors in `coverage_floors.ini` only ever RISE.
- No new runtime dependencies. `atheris` is a test dependency.
- No host Python environment exists. Run tests with `./run_tests.sh [pytest args]`. Read `tests/README.md` first. **Never** run `./run_tests.sh --down` — it destroys the tmpfs database and forces a replay of ~269 migrations.
- Run one suite at a time. Two concurrent runs against the same test database corrupt each other.

## Known bugs in scope — report before encoding

Reading the in-scope functions turned up five apparent defects. **Do not write a test that asserts the buggy behaviour is correct** — that cements the bug. For each, write the test that asserts the *intended* behaviour, watch it fail, then STOP and report to the controller. The controller rules on whether to fix it here. If the ruling is "fix", the fix is the one-liner named below and the test then passes.

| Function | Line | Defect | Intended behaviour |
|---|---|---|---|
| `shorten_url` | 1274 | `else: ''` is missing `return`, so it yields `None` | returns `''` for falsy input |
| `links_with_parens` | 1024 | `better_html.replace(...)` result is discarded — a no-op | assign the result, or delete the dead line |
| `user_ip_banned` | 1691-1694 | falls off the end when there is no IP, returning `None` from a `-> bool` function | returns `False` |
| `mastodon_extra_field_link` | 990-993 | returns `None` when the input has no `<a>` | returns `''` or `None` deliberately, documented |
| `expand_hex_color` | 4393 | indexes `text[1..3]` with no length check — `IndexError` on `'#ab'` | documented precondition, or a guard |

`user_ip_banned` also reaches the database via `banned_ip_addresses()`, so it moves out of scope regardless — see below.

## Functions moved OUT of this sub-project

The spec warned the partition was heuristic. These seven were verified misclassified or out of reach while writing this plan. **Do not test them here.** Task 1's first step records them in the ledger as findings.

| Function | Uncovered | Why it moves | Goes to |
|---|---|---|---|
| `jaccard_similarity` | 10 | calls `recently_upvoted_posts()` / `recently_upvoted_post_replies()`, both DB-backed. Also memoises into a module-level `user2_cache` dict that persists across tests. | 1b |
| `actor_contains_blocked_words` | 4 | calls `get_setting()`, which queries the `Setting` table | 1b |
| `user_ip_banned` | 3 | calls `banned_ip_addresses()`, DB-backed | 1b |
| `first_paragraph` | 9 | calls `allowlist_html()` with no `test_env`, which reaches `get_emoji_replacements()` and `fediverse_domains()` — both DB-backed | 1b |
| `is_image_url` | 8 | calls `mime_type_using_head()`, which performs a network HEAD request | 1c |
| `is_local_image_url` | 4 | calls `is_image_url()` — same network dependency | 1c |

| `download_defeds` | 3 | both arms call `download_defeds_worker`, which reaches the network through `retrieve_defederation_list` | 1c |

That is 41 statements. This sub-project therefore targets **307 uncovered statements across 58 functions**.

## File Structure

| File | Responsibility |
|---|---|
| `tests/test_utils_numeric.py` | Task 1 — numbers, dates, sizes |
| `tests/test_utils_strings.py` | Task 2 — strings, URLs, domains |
| `tests/test_utils_html.py` | Task 3 — HTML and markdown fragments |
| `tests/test_utils_security.py` | Task 4 — XML and SVG rejection, hand-written cases |
| `tests/fuzz/__init__.py`, `tests/fuzz/harnesses.py`, `tests/fuzz/run_campaign.py` | Task 5 — fuzz targets and the on-demand runner |
| `tests/fuzz/corpus/<target>/` | Task 5 — committed inputs, replayed every run |
| `tests/test_utils_fuzz_corpus.py` | Task 5 — deterministic corpus replay |
| `tests/test_utils_images.py` | Task 6 — Pillow-backed |
| `tests/test_utils_redis.py` | Task 7 — captcha and SSE, via `redis_double` |
| `tests/test_utils_request_context.py` | Task 8 — helpers needing a request |
| `tests/test_utils_context_globals.py` | Task 9 — `g`, templates, filesystem |

---

### Task 1: Numeric, date and size helpers

**Files:**
- Create: `tests/test_utils_numeric.py`
- Test: `tests/test_utils_numeric.py`

**Interfaces:**
- Consumes: the `app` fixture from `tests/conftest.py` (session-scoped, provides app context).
- Produces: nothing other tasks depend on.

Functions and their uncovered counts: `days_to_add_for_next_month` (16), `wilson_confidence_lower_bound` (12), `human_filesize` (8), `shorten_number` (3), `paginate_post_ids` (3), `sha256_digest` (3), `expand_hex_color` (2), `intlist_to_strlist` (1).

- [ ] **Step 1: Record the moved functions in the ledger**

Before writing any test, append the six moved functions from the table above to the SDD ledger as a finding, so sub-projects 1b and 1c inherit them. This is a documentation step, not a code step.

- [ ] **Step 2: Check for dead code before writing a single test**

The spec's rule: a function with no caller is a finding and a deletion
candidate, not a target for a test that exists only to move a number. The long
tail is where unused functions hide, so check before investing in tests.

For each of the eight functions in this task, and again at the start of every
later task for its own functions:

```bash
grep -rn "\bwilson_confidence_lower_bound\b" app/ --include='*.py' --include='*.html' | grep -v 'def '
```

Repeat per function. A function whose only match is its own definition — and
which is not registered as a Jinja global or filter in `app/request_hooks.py`,
and is not exported through `app/api/` — is dead. **Report it; do not test it.**

Note `app/request_hooks.py` registers many of these as template globals
(`shorten_number`, `human_filesize`, `humanize_number`, `round_invisible_digits`,
`localize_datetime`, `debug_checkpoint` among them), so a bare `grep` over `.py`
files alone will produce false positives. Check `app/request_hooks.py` and the
`templates/` tree before calling anything dead.

- [ ] **Step 3: Write the tests**

```python
from datetime import datetime

import pytest

from app.utils import (days_to_add_for_next_month, expand_hex_color, human_filesize,
                       intlist_to_strlist, paginate_post_ids, sha256_digest,
                       shorten_number, wilson_confidence_lower_bound)


class TestWilsonConfidenceLowerBound:
    """Fails if the n == 0 guard or the negative clamp is removed."""

    def test_no_votes_scores_zero(self):
        assert wilson_confidence_lower_bound(0, 0) == 0.0

    def test_none_is_treated_as_zero(self):
        assert wilson_confidence_lower_bound(None, None) == 0.0

    def test_negative_counts_are_clamped_not_propagated(self):
        assert wilson_confidence_lower_bound(-5, -5) == 0.0
        assert wilson_confidence_lower_bound(-5, 10) == wilson_confidence_lower_bound(0, 10)

    def test_more_upvotes_scores_higher(self):
        assert wilson_confidence_lower_bound(100, 0) > wilson_confidence_lower_bound(10, 0)

    def test_confidence_grows_with_sample_size(self):
        """Ten of ten is a stronger claim than one of one, at the same ratio."""
        assert wilson_confidence_lower_bound(10, 0) > wilson_confidence_lower_bound(1, 0)

    def test_result_is_a_probability(self):
        for ups, downs in [(1, 0), (0, 1), (5, 5), (99, 1)]:
            assert 0.0 <= wilson_confidence_lower_bound(ups, downs) <= 1.0


class TestDaysToAddForNextMonth:
    """Fails if the backtracking loop or the December rollover is removed."""

    def test_mid_month_is_the_month_length(self):
        assert days_to_add_for_next_month(datetime(2026, 1, 15)) == 31

    def test_december_rolls_over_to_january(self):
        assert days_to_add_for_next_month(datetime(2026, 12, 15)) == 31

    def test_the_31st_backtracks_to_the_last_valid_day(self):
        """Jan 31 has no Feb 31; the loop must land on Feb 28 in a non-leap year."""
        assert days_to_add_for_next_month(datetime(2026, 1, 31)) == 28

    def test_the_31st_backtracks_to_february_29_in_a_leap_year(self):
        assert days_to_add_for_next_month(datetime(2028, 1, 31)) == 29

    def test_the_31st_reaches_a_31_day_month_intact(self):
        assert days_to_add_for_next_month(datetime(2026, 3, 31)) == 30


class TestHumanFilesize:
    """Fails if the zero guard or the unit-cap on the while loop is removed."""

    def test_zero(self):
        assert human_filesize(0) == '0 B'

    def test_bytes_below_one_kilobyte(self):
        assert human_filesize(1023) == '1023.0 B'

    def test_exactly_one_kilobyte(self):
        assert human_filesize(1024) == '1.0 KB'

    def test_each_unit_step(self):
        assert human_filesize(1024 ** 2) == '1.0 MB'
        assert human_filesize(1024 ** 3) == '1.0 GB'
        assert human_filesize(1024 ** 4) == '1.0 TB'
        assert human_filesize(1024 ** 5) == '1.0 PB'

    def test_beyond_the_largest_unit_stays_in_petabytes(self):
        """The loop is capped at len(units) - 1, so it must not run off the end."""
        assert human_filesize(1024 ** 6) == '1024.0 PB'


class TestShortenNumber:
    def test_below_one_thousand_is_unchanged(self):
        assert shorten_number(999) == '999'

    def test_thousands(self):
        assert shorten_number(1000) == '1.0k'

    def test_millions(self):
        assert shorten_number(1_500_000) == '1.5M'


class TestPaginatePostIds:
    def test_first_page(self):
        assert paginate_post_ids([1, 2, 3, 4, 5, 6, 7], 0, 3) == [1, 2, 3]

    def test_second_page(self):
        assert paginate_post_ids([1, 2, 3, 4, 5, 6, 7], 1, 3) == [4, 5, 6]

    def test_page_past_the_end_is_empty(self):
        assert paginate_post_ids([1, 2, 3], 9, 3) == []


class TestSha256Digest:
    def test_known_digest_of_the_empty_string(self):
        assert sha256_digest('') == (
            'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855')

    def test_non_ascii_is_encoded_as_utf8(self):
        """Fails if the .encode('utf-8') is dropped, which would raise."""
        assert len(sha256_digest('café')) == 64


class TestIntlistToStrlist:
    def test_converts_each_element(self):
        assert intlist_to_strlist([1, 2, 3]) == ['1', '2', '3']

    def test_empty(self):
        assert intlist_to_strlist([]) == []


class TestExpandHexColor:
    def test_three_digit_shorthand_expands(self):
        assert expand_hex_color('#abc') == '#aabbcc'

    def test_short_input_raises_index_error(self):
        """BUG (see plan): no length guard. Report before deciding the fix."""
        with pytest.raises(IndexError):
            expand_hex_color('#ab')
```

- [ ] **Step 4: Run to verify they fail or pass for the right reason**

Run: `./run_tests.sh tests/test_utils_numeric.py -q`

Every test here exercises existing production code, so most should PASS immediately. That is expected for coverage work and is NOT the TDD red step being skipped — the red step for coverage tests is proving the test *discriminates*, which Step 5 does.

`test_short_input_raises_index_error` documents a defect. Report it per the "Known bugs" section rather than treating a green run as done.

- [ ] **Step 5: Prove the tests discriminate**

For each of the three largest functions, temporarily break the production code, confirm the matching test fails, then restore with `git checkout -- app/utils.py`:

| Break | Test that must fail |
|---|---|
| delete `if n == 0: return 0.0` from `wilson_confidence_lower_bound` | `test_no_votes_scores_zero` (ZeroDivisionError) |
| change `while i < len(units) - 1` to `while True` in `human_filesize` | `test_beyond_the_largest_unit_stays_in_petabytes` |
| delete `new_day -= 1` from `days_to_add_for_next_month` | `test_the_31st_backtracks_to_the_last_valid_day` (hangs or errors) |

Record the three observations in the task report. If a test does NOT fail when its code is broken, it is decoration — rewrite it.

- [ ] **Step 6: Confirm the coverage gap closed**

Run: `./run_tests.sh tests/test_utils_numeric.py -q --cov=app.utils --cov-report=term-missing`

Every line listed for these eight functions in the plan's gap tables must be gone from the "Missing" column. Any that remains is reported with the reason.

- [ ] **Step 7: Commit**

```bash
git add tests/test_utils_numeric.py
git commit -m "test: cover numeric and date helpers in app/utils.py"
```

---

### Task 2: String, URL and domain helpers

**Files:**
- Create: `tests/test_utils_strings.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: nothing later tasks depend on.

Functions: `is_video_hosting_site` (9), `domain_from_email` (6), `inbox_domain` (4), `mimetype_from_url` (4), `reply_is_just_link_to_gif_reaction` (4), `reply_is_low_effort` (4), `shorten_url` (3), `shorten_string` (2), `is_bot` (2).

- [ ] **Step 1: Write the tests**

```python
from app.utils import (domain_from_email, inbox_domain, is_bot, is_video_hosting_site,
                       mimetype_from_url, reply_is_just_link_to_gif_reaction,
                       reply_is_low_effort, shorten_string, shorten_url)


class TestIsVideoHostingSite:
    """Fails if the None/empty guard, the prefix list, or the PeerTube check is removed."""

    def test_none(self):
        assert is_video_hosting_site(None) is False

    def test_empty_string(self):
        assert is_video_hosting_site('') is False

    def test_each_known_host(self):
        for url in ['https://youtube.com/watch?v=x', 'https://www.youtube.com/watch?v=x',
                    'https://youtu.be/x', 'https://vimeo.com/1', 'https://www.vimeo.com/1',
                    'https://streamable.com/x', 'https://www.redgifs.com/watch/x']:
            assert is_video_hosting_site(url) is True, url

    def test_peertube_is_matched_by_path_not_host(self):
        assert is_video_hosting_site('https://tilvids.com/videos/watch/abc') is True

    def test_unrelated_site(self):
        assert is_video_hosting_site('https://example.com/page') is False

    def test_prefix_match_is_anchored(self):
        """A host that merely contains youtube.com must not match."""
        assert is_video_hosting_site('https://notyoutube.com/watch') is False


class TestDomainFromEmail:
    def test_extracts_the_domain(self):
        assert domain_from_email('user@example.com') == 'example.com'

    def test_none(self):
        assert domain_from_email(None) == ''

    def test_empty_and_whitespace(self):
        assert domain_from_email('') == ''
        assert domain_from_email('   ') == ''

    def test_no_at_sign(self):
        assert domain_from_email('notanemail') == ''

    def test_multiple_at_signs_takes_the_last_part(self):
        assert domain_from_email('a@b@example.com') == 'example.com'


class TestInboxDomain:
    def test_https_url_reduces_to_host(self):
        assert inbox_domain('https://example.com/inbox') == 'example.com'

    def test_http_url_reduces_to_host(self):
        assert inbox_domain('http://example.com/inbox') == 'example.com'

    def test_case_is_normalised(self):
        assert inbox_domain('https://EXAMPLE.com/Inbox') == 'example.com'

    def test_bare_domain_passes_through_lowercased(self):
        assert inbox_domain('Example.com') == 'example.com'


class TestMimetypeFromUrl:
    def test_png(self):
        assert mimetype_from_url('https://example.com/a.png') == 'image/png'

    def test_query_string_is_stripped_before_guessing(self):
        assert mimetype_from_url('https://example.com/a.png?v=2') == 'image/png'

    def test_unknown_extension_is_none(self):
        assert mimetype_from_url('https://example.com/a.unknownext') is None


class TestReplyIsJustLinkToGifReaction:
    def test_each_known_gif_host(self):
        for host in ['https://media.tenor.com/', 'https://media1.tenor.com/',
                     'https://media2.tenor.com/', 'https://media3.tenor.com/',
                     'https://i.giphy.com/', 'https://i.imgflip.com',
                     'https://media1.giphy.com/', 'https://media2.giphy.com/',
                     'https://media3.giphy.com/', 'https://media4.giphy.com/']:
            assert reply_is_just_link_to_gif_reaction(host + 'x.gif') is True, host

    def test_surrounding_whitespace_is_stripped_first(self):
        assert reply_is_just_link_to_gif_reaction('  https://i.giphy.com/x.gif  ') is True

    def test_ordinary_reply(self):
        assert reply_is_just_link_to_gif_reaction('a real comment') is False


class TestReplyIsLowEffort:
    def test_the_three_recognised_forms(self):
        for body in ['this', 'this.', 'this!']:
            assert reply_is_low_effort(body) is True, body

    def test_case_and_whitespace_insensitive(self):
        assert reply_is_low_effort('  THIS!  ') is True

    def test_a_real_reply(self):
        assert reply_is_low_effort('this is a real point') is False


class TestShortenString:
    def test_short_input_is_unchanged(self):
        assert shorten_string('abc') == 'abc'

    def test_long_input_is_truncated_with_an_ellipsis(self):
        result = shorten_string('a' * 100, max_length=10)
        assert result == 'aaaaaaa…'
        assert len(result) == 8

    def test_falsy_input(self):
        assert shorten_string('') == ''
        assert shorten_string(None) == ''


class TestShortenUrl:
    def test_scheme_is_stripped(self):
        assert shorten_url('https://example.com') == 'example.com'
        assert shorten_url('http://example.com') == 'example.com'

    def test_falsy_input_returns_empty_string(self):
        """BUG (see plan): `else: ''` is missing its `return`, so this yields None.

        Report before deciding the fix. The one-line fix is `return ''`.
        """
        assert shorten_url('') == ''


class TestIsBot:
    def test_bot_substring(self):
        assert is_bot('Googlebot/2.1') is True

    def test_meta_external_agent(self):
        assert is_bot('meta-externalagent/1.1') is True

    def test_case_insensitive(self):
        assert is_bot('GOOGLEBOT') is True

    def test_ordinary_browser(self):
        assert is_bot('Mozilla/5.0 (X11; Linux x86_64)') is False
```

- [ ] **Step 2: Run**

Run: `./run_tests.sh tests/test_utils_strings.py -q`

Expected: all pass except `TestShortenUrl::test_falsy_input_returns_empty_string`, which fails with `assert None == ''`. That failure is the bug report — do not "fix" it by changing the test to `is None`.

- [ ] **Step 3: Report the `shorten_url` defect and STOP for a ruling**

Report to the controller: the function is annotated `-> str` but returns `None` for falsy input, because line 1274's `''` has no `return`. The fix is one word. Await the ruling before continuing.

- [ ] **Step 4: Prove discrimination**

Break `is_video_hosting_site` by deleting `if 'videos/watch' in url: return True`; confirm `test_peertube_is_matched_by_path_not_host` fails. Restore with `git checkout -- app/utils.py` and record the observation.

- [ ] **Step 5: Commit**

```bash
git add tests/test_utils_strings.py
git commit -m "test: cover string, URL and domain helpers in app/utils.py"
```

---

### Task 3: HTML and markdown fragment helpers

**Files:**
- Create: `tests/test_utils_html.py`

**Interfaces:**
- Consumes: the `app` fixture — `community_link_to_href` and friends read `current_app.config['SERVER_NAME']`.
- Produces: nothing later tasks depend on.

Functions: `remove_images` (6), `links_with_parens` (4), `escape_img` (3), `piefed_markdown_to_lemmy_markdown` (3), `mastodon_extra_field_link` (3), `community_link_to_href` (1), `feed_link_to_href` (1), `person_link_to_href` (1), `handle_lemmy_autocomplete` (1), `allowlist_html` residue (6).

`allowlist_html`'s six remaining lines are 387, 392, 407, 471, 479 and 494. Pass `test_env={'fn_string': 'fn-test'}` as the existing `tests/test_allowlist_html.py` does — without it the function reaches `get_emoji_replacements()` and `fediverse_domains()`, which are DB-backed and would put this function out of scope. **If any of the six lines cannot be reached with `test_env` set, report it and move `allowlist_html` to 1b rather than pulling in the database.**

- [ ] **Step 1: Write the tests**

```python
from app.utils import (community_link_to_href, escape_img, feed_link_to_href,
                       handle_lemmy_autocomplete, links_with_parens,
                       mastodon_extra_field_link, person_link_to_href,
                       piefed_markdown_to_lemmy_markdown, remove_images)


class TestRemoveImages:
    """Fails if either decompose() loop is removed."""

    def test_img_tags_are_removed(self):
        assert '<img' not in remove_images('<p>text</p><img src="x.png">')

    def test_video_tags_are_removed(self):
        assert '<video' not in remove_images('<p>text</p><video src="x.mp4"></video>')

    def test_surrounding_text_survives(self):
        assert 'keep me' in remove_images('<p>keep me</p><img src="x.png">')

    def test_html_without_media_is_unchanged(self):
        assert remove_images('<p>plain</p>') == '<p>plain</p>'


class TestEscapeImg:
    def test_img_becomes_a_placeholder(self):
        result = escape_img('before <img src="x.png"> after')
        assert '<img' not in result
        assert 'image placeholder' in result

    def test_text_without_images_is_unchanged(self):
        assert escape_img('no images here') == 'no images here'


class TestPiefedMarkdownToLemmyMarkdown:
    def test_soft_break_gains_two_spaces(self):
        assert piefed_markdown_to_lemmy_markdown('a\r\nb') == 'a  \r\nb'

    def test_text_without_breaks_is_unchanged(self):
        assert piefed_markdown_to_lemmy_markdown('one line') == 'one line'


class TestMastodonExtraFieldLink:
    def test_returns_the_first_href(self):
        html = '<a href="https://example.com/">example</a>'
        assert mastodon_extra_field_link(html) == 'https://example.com/'

    def test_no_anchor_returns_none(self):
        """BUG (see plan): falls off the end rather than returning a documented value."""
        assert mastodon_extra_field_link('<p>no link</p>') is None


class TestLinksWithParens:
    """Fails if either paren-balancing arm is removed."""

    def test_trailing_paren_outside_the_link_is_pulled_in(self):
        html = '<a href="https://en.wikipedia.org/wiki/Foo_(bar">Foo_(bar</a>)'
        result = links_with_parens(html)
        assert 'Foo_(bar)"' in result or 'Foo_(bar)' in result

    def test_trailing_paren_inside_the_link_is_pushed_out(self):
        html = '<a href="https://example.com/x)">x)</a>'
        result = links_with_parens(html)
        assert 'href="https://example.com/x"' in result

    def test_balanced_link_is_untouched(self):
        html = '<a href="https://example.com/(x)">(x)</a>'
        assert links_with_parens(html) == html


class TestActorLinkHelpers:
    """The uncovered line in each is the `current_app.config` fallback branch,
    reached only when server_name_override is not passed."""

    def test_community_link_uses_the_configured_server_name(self, app):
        result = community_link_to_href('!news@lemmy.world')
        assert 'test.piefed.local/community/lookup/news/lemmy.world' in result

    def test_community_link_honours_the_override(self, app):
        result = community_link_to_href('!news@lemmy.world', server_name_override='other.example')
        assert 'other.example/community/lookup/' in result

    def test_feed_link_uses_the_configured_server_name(self, app):
        result = feed_link_to_href('~tech@lemmy.world')
        assert 'test.piefed.local/feed/lookup/tech/lemmy.world' in result

    def test_person_link_uses_the_configured_server_name(self, app):
        result = person_link_to_href('@bob@lemmy.world')
        assert 'test.piefed.local/user/lookup/bob/lemmy.world' in result


class TestHandleLemmyAutocomplete:
    def test_autocompleted_community_link_becomes_bare_handle(self):
        text = '[!news@lemmy.world](https://lemmy.world/c/news)'
        assert handle_lemmy_autocomplete(text) == '!news@lemmy.world'

    def test_autocompleted_person_link_becomes_bare_handle(self):
        text = '[@bob@lemmy.world](https://lemmy.world/u/bob)'
        assert handle_lemmy_autocomplete(text) == '@bob@lemmy.world'

    def test_an_ordinary_markdown_link_is_left_alone(self):
        text = '[example](https://example.com)'
        assert handle_lemmy_autocomplete(text) == text
```

- [ ] **Step 2: Run and iterate on the paren cases**

Run: `./run_tests.sh tests/test_utils_html.py -q`

`links_with_parens` manipulates a BeautifulSoup tree and its exact output is fiddly. If an assertion is wrong, fix the *assertion* to match real behaviour — but only after confirming by inspection that the real behaviour is correct. If the real behaviour looks wrong, report it rather than encoding it.

- [ ] **Step 3: Close `allowlist_html`'s six lines**

Run: `./run_tests.sh tests/test_utils_html.py tests/test_allowlist_html.py -q --cov=app.utils --cov-report=term-missing`

Read which of 387, 392, 407, 471, 479, 494 remain. Add cases to `tests/test_utils_html.py` — always passing `test_env={'fn_string': 'fn-test'}` — until they are covered, or report the ones that resist with the reason.

- [ ] **Step 4: Prove discrimination**

Break `remove_images` by deleting the `<video>` loop; confirm `test_video_tags_are_removed` fails. Restore and record.

- [ ] **Step 5: Commit**

```bash
git add tests/test_utils_html.py
git commit -m "test: cover HTML and markdown fragment helpers in app/utils.py"
```

---

### Task 4: XML and SVG rejection — hand-written cases

**Files:**
- Create: `tests/test_utils_security.py`

**Interfaces:**
- Consumes: the `app` fixture (`sanitize_svg` logs through `current_app.logger`), and pytest's `tmp_path`.
- Produces: the hostile-input cases Task 5's fuzz corpus is seeded from.

Functions: `is_valid_xml_utf8` (21), `sanitize_svg` (11), `sanitize_svg_bytes` (9).

These exist to reject hostile input. Cover the rejection paths, not just the accept path.

- [ ] **Step 1: Write the tests**

```python
import pytest

from app.utils import is_valid_xml_utf8, sanitize_svg, sanitize_svg_bytes


class TestIsValidXmlUtf8:
    """A hand-rolled UTF-8 byte scanner. Each test names the check it exercises."""

    def test_plain_ascii_is_valid(self):
        assert is_valid_xml_utf8(b'hello world') is True

    def test_a_str_is_encoded_before_scanning(self):
        assert is_valid_xml_utf8('hello world') is True

    def test_permitted_whitespace_is_valid(self):
        assert is_valid_xml_utf8(b'a\tb\nc\rd') is True

    def test_nul_is_rejected(self):
        assert is_valid_xml_utf8(b'\x00') is False

    def test_vertical_tab_and_form_feed_are_rejected(self):
        assert is_valid_xml_utf8(b'\x0b') is False
        assert is_valid_xml_utf8(b'\x0c') is False

    def test_control_range_14_to_31_is_rejected(self):
        assert is_valid_xml_utf8(b'\x0e') is False
        assert is_valid_xml_utf8(b'\x1f') is False

    def test_delete_is_rejected(self):
        assert is_valid_xml_utf8(b'\x7f') is False

    def test_control_char_in_a_long_string_is_rejected_by_the_first_loop(self):
        """Long enough that the control byte is found before the tail loop."""
        assert is_valid_xml_utf8(b'aaaaaaaaaa\x00aaaaaaaaaa') is False

    def test_control_char_in_the_last_two_bytes_is_rejected_by_the_tail_loop(self):
        """The first loop stops at c_end - 2, so the tail loop must catch this."""
        assert is_valid_xml_utf8(b'aaaaaaaaaa\x00') is False

    def test_forbidden_fffe_is_rejected(self):
        assert is_valid_xml_utf8(b'\xef\xbf\xbe') is False

    def test_forbidden_ffff_is_rejected(self):
        assert is_valid_xml_utf8(b'\xef\xbf\xbf') is False

    def test_surrogate_range_is_rejected(self):
        assert is_valid_xml_utf8(b'\xed\xa0\x80') is False   # \ud800, low end
        assert is_valid_xml_utf8(b'\xed\xbf\xbf') is False   # \udfff, high end

    def test_legitimate_multibyte_text_is_valid(self):
        assert is_valid_xml_utf8('日本語のテキスト'.encode('utf-8')) is True

    def test_empty_input_is_valid(self):
        assert is_valid_xml_utf8(b'') is True

    def test_short_inputs_do_not_raise(self):
        """The loop bounds are hand-written; 1- and 2-byte inputs are the edge."""
        for raw in [b'', b'a', b'ab', b'\xef', b'\xef\xbf']:
            assert isinstance(is_valid_xml_utf8(raw), bool)


class TestSanitizeSvgBytes:
    """The XXE and script-injection boundary for uploaded SVGs."""

    def test_a_plain_svg_survives(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'
        assert b'<rect' in sanitize_svg_bytes(svg)

    def test_doctype_entity_declaration_is_stripped(self):
        svg = (b'<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
               b'<svg xmlns="http://www.w3.org/2000/svg"><text>&xxe;</text></svg>')
        result = sanitize_svg_bytes(svg)
        assert b'<!ENTITY' not in result
        assert b'DOCTYPE' not in result

    def test_processing_instruction_is_stripped(self):
        svg = b'<?xml-stylesheet href="evil.xsl"?><svg xmlns="http://www.w3.org/2000/svg"/>'
        assert b'<?xml-stylesheet' not in sanitize_svg_bytes(svg)

    def test_script_element_does_not_survive(self):
        svg = (b'<svg xmlns="http://www.w3.org/2000/svg">'
               b'<script>alert(1)</script></svg>')
        assert b'<script' not in sanitize_svg_bytes(svg).lower()

    def test_event_handler_attribute_does_not_survive(self):
        svg = (b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)">'
               b'<rect onclick="alert(2)"/></svg>')
        result = sanitize_svg_bytes(svg).lower()
        assert b'onload' not in result
        assert b'onclick' not in result

    def test_oversize_input_is_refused(self):
        with pytest.raises(ValueError, match='SVG file too large'):
            sanitize_svg_bytes(b'a' * (10 * 1024 * 1024 + 1))

    def test_exactly_the_limit_is_allowed(self):
        """Boundary: the guard is `>`, so the limit itself must pass it."""
        sanitize_svg_bytes(b'<svg/>' + b' ' * (10 * 1024 * 1024 - 6))


class TestSanitizeSvgFile:
    def test_a_clean_file_is_left_alone_and_reports_success(self, app, tmp_path):
        path = tmp_path / 'clean.svg'
        original = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'
        path.write_bytes(original)

        assert sanitize_svg(str(path)) is True

    def test_a_hostile_file_is_rewritten_in_place(self, app, tmp_path):
        path = tmp_path / 'hostile.svg'
        path.write_bytes(b'<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
                         b'<svg xmlns="http://www.w3.org/2000/svg"/>')

        assert sanitize_svg(str(path)) is True
        assert b'<!ENTITY' not in path.read_bytes()

    def test_a_missing_file_reports_failure_rather_than_raising(self, app, tmp_path):
        """The except arm: callers rely on False, not on an exception."""
        assert sanitize_svg(str(tmp_path / 'does-not-exist.svg')) is False

    def test_an_oversize_file_reports_failure(self, app, tmp_path):
        path = tmp_path / 'huge.svg'
        path.write_bytes(b'a' * (10 * 1024 * 1024 + 1))
        assert sanitize_svg(str(path)) is False
```

- [ ] **Step 2: Run**

Run: `./run_tests.sh tests/test_utils_security.py -q`

If `test_event_handler_attribute_does_not_survive` or `test_script_element_does_not_survive` FAILS, that is a security finding, not a broken test. **Stop and report it** — do not weaken the assertion.

- [ ] **Step 3: Prove discrimination**

Break `sanitize_svg_bytes` by deleting the `<\!.*?>` substitution; confirm `test_doctype_entity_declaration_is_stripped` fails. Break `is_valid_xml_utf8`'s tail loop by changing `while i < c_end` to `while False`; confirm `test_control_char_in_the_last_two_bytes_is_rejected_by_the_tail_loop` fails. Restore after each and record both observations.

- [ ] **Step 4: Commit**

```bash
git add tests/test_utils_security.py
git commit -m "test: cover XML and SVG rejection paths in app/utils.py"
```

---

### Task 5: Fuzz harnesses, corpus and campaign runner

**Files:**
- Create: `tests/fuzz/__init__.py`, `tests/fuzz/harnesses.py`, `tests/fuzz/run_campaign.py`
- Create: `tests/fuzz/corpus/is_valid_xml_utf8/`, `tests/fuzz/corpus/sanitize_svg_bytes/`
- Create: `tests/test_utils_fuzz_corpus.py`
- Modify: `requirements.txt`, `tests/README.md`

**Interfaces:**
- Consumes: the hostile inputs written in Task 4.
- Produces: `tests/fuzz/harnesses.py` exposing `check_is_valid_xml_utf8(data: bytes) -> None` and `check_sanitize_svg_bytes(data: bytes) -> None`, each raising `AssertionError` on a property violation. `tests/test_utils_fuzz_corpus.py` replays every file under `tests/fuzz/corpus/<name>/` through the matching `check_<name>`.

- [ ] **Step 1: Add the dependency**

Append to `requirements.txt`, beside the other test dependencies:

```
atheris
```

Rebuild the container so it is installed:

```bash
podman-compose -f compose.test.yaml build test-runner
```

Confirm: `podman-compose -f compose.test.yaml run --rm test-runner python -c "import atheris; print(atheris.__name__)"`

- [ ] **Step 2: Write the property checks**

`tests/fuzz/harnesses.py` — plain functions with no atheris import, so the corpus replay can use them without the fuzzer:

```python
"""Property checks shared by the fuzz campaign and the corpus replay.

Each check raises AssertionError when the property is violated. "It did not
crash" is a weak oracle -- it would pass a sanitizer that returned its input
unchanged -- so each check asserts a security property instead.
"""

import re

from app.utils import allowlist_html, is_valid_xml_utf8, sanitize_svg_bytes

EVENT_HANDLER = re.compile(rb'\son[a-z]+\s*=', re.IGNORECASE)
EVENT_HANDLER_TEXT = re.compile(r'\son[a-z]+\s*=', re.IGNORECASE)


def check_is_valid_xml_utf8(data: bytes) -> None:
    """Must return a bool for any input, and never raise.

    The bug being hunted is IndexError from the hand-written s[i+1] / s[i+2]
    indexing against the c_end - 2 loop bound.
    """
    result = is_valid_xml_utf8(data)
    assert isinstance(result, bool), f'returned {type(result).__name__}, not bool'


def check_sanitize_svg_bytes(data: bytes) -> None:
    """Output must never carry script, event handlers, or entity declarations.

    Oversize input is allowed to raise ValueError -- that is the documented
    contract -- but nothing else may escape.
    """
    try:
        result = sanitize_svg_bytes(data)
    except ValueError:
        return
    assert isinstance(result, bytes), f'returned {type(result).__name__}, not bytes'
    lowered = result.lower()
    assert b'<script' not in lowered, 'script element survived sanitization'
    assert b'<!entity' not in lowered, 'entity declaration survived sanitization'
    assert b'javascript:' not in lowered, 'javascript: URL survived sanitization'
    assert not EVENT_HANDLER.search(result), 'event handler attribute survived sanitization'


def check_allowlist_html(data: bytes) -> None:
    """The XSS boundary for all remote content: no script, no handlers, no
    javascript: URLs may survive, for any input.

    test_env is passed so the function does not reach get_emoji_replacements()
    or fediverse_domains(), both of which are DB-backed. Fuzzing must not need
    a database.
    """
    text = data.decode('utf-8', errors='replace')
    result = allowlist_html(text, test_env={'fn_string': 'fn-test'})
    assert isinstance(result, str), f'returned {type(result).__name__}, not str'
    lowered = result.lower()
    assert '<script' not in lowered, 'script element survived the allowlist'
    assert 'javascript:' not in lowered, 'javascript: URL survived the allowlist'
    assert not EVENT_HANDLER_TEXT.search(result), 'event handler survived the allowlist'
```

`sanitize_svg` is deliberately NOT a separate fuzz target. It is a thin wrapper
that reads a file, calls `sanitize_svg_bytes`, and writes the result back; all
of its input handling is `sanitize_svg_bytes`, which IS fuzzed. Fuzzing a
filesystem path would exercise `open()`, not PieFed. Record this reasoning in
the task report so the spec's "all four targets" line is answered rather than
quietly dropped.

- [ ] **Step 3: Write the corpus replay test**

`tests/test_utils_fuzz_corpus.py`:

```python
"""Replays every committed fuzz input deterministically.

The campaign itself is nondeterministic and runs on demand (see tests/README.md).
This file is what keeps a finding fixed: an input that once broke a property is
committed here and checked on every run, forever.
"""

import pathlib

import pytest

from tests.fuzz.harnesses import (check_allowlist_html, check_is_valid_xml_utf8,
                                  check_sanitize_svg_bytes)

CORPUS_ROOT = pathlib.Path(__file__).parent / 'fuzz' / 'corpus'

CHECKS = {
    'allowlist_html': check_allowlist_html,
    'is_valid_xml_utf8': check_is_valid_xml_utf8,
    'sanitize_svg_bytes': check_sanitize_svg_bytes,
}


def corpus_cases():
    for name, check in CHECKS.items():
        directory = CORPUS_ROOT / name
        for path in sorted(directory.glob('*')):
            if path.is_file():
                yield pytest.param(check, path, id=f'{name}/{path.name}')


@pytest.mark.parametrize('check,path', corpus_cases())
def test_corpus_input_still_satisfies_its_property(check, path):
    check(path.read_bytes())


@pytest.mark.parametrize('name', sorted(CHECKS))
def test_every_target_has_a_seeded_corpus(name):
    """A target whose corpus directory vanished would otherwise silently stop
    being replayed, since parametrize over an empty glob collects nothing."""
    directory = CORPUS_ROOT / name
    assert directory.is_dir(), f'{directory} is missing'
    assert any(p.is_file() for p in directory.iterdir()), f'{directory} is empty'
```

- [ ] **Step 4: Seed the corpus from Task 4's hostile inputs**

Write one file per hostile case, so the corpus starts from known-interesting inputs rather than from random bytes:

```bash
mkdir -p tests/fuzz/corpus/is_valid_xml_utf8 tests/fuzz/corpus/sanitize_svg_bytes
printf 'hello world' > tests/fuzz/corpus/is_valid_xml_utf8/ascii
printf '\x00' > tests/fuzz/corpus/is_valid_xml_utf8/nul
printf '\xef\xbf\xbe' > tests/fuzz/corpus/is_valid_xml_utf8/fffe
printf '\xed\xa0\x80' > tests/fuzz/corpus/is_valid_xml_utf8/surrogate_low
printf '\xed\xbf\xbf' > tests/fuzz/corpus/is_valid_xml_utf8/surrogate_high
printf '\xef' > tests/fuzz/corpus/is_valid_xml_utf8/truncated_multibyte
printf 'aaaaaaaaaa\x00' > tests/fuzz/corpus/is_valid_xml_utf8/control_in_tail
printf '<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>' > tests/fuzz/corpus/sanitize_svg_bytes/plain
printf '<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><svg/>' > tests/fuzz/corpus/sanitize_svg_bytes/xxe
printf '<svg onload="alert(1)"><script>alert(2)</script></svg>' > tests/fuzz/corpus/sanitize_svg_bytes/script_and_handler
printf '<?xml-stylesheet href="evil.xsl"?><svg/>' > tests/fuzz/corpus/sanitize_svg_bytes/stylesheet_pi
mkdir -p tests/fuzz/corpus/allowlist_html
printf '<p>plain text</p>' > tests/fuzz/corpus/allowlist_html/plain
printf '<script>alert(1)</script>' > tests/fuzz/corpus/allowlist_html/script
printf '<a href="javascript:alert(1)">x</a>' > tests/fuzz/corpus/allowlist_html/javascript_url
printf '<img src=x onerror="alert(1)">' > tests/fuzz/corpus/allowlist_html/onerror
printf '<div onclick="alert(1)">x</div>' > tests/fuzz/corpus/allowlist_html/onclick
```

- [ ] **Step 5: Write the campaign runner**

`tests/fuzz/run_campaign.py`:

```python
"""On-demand fuzz campaign. NOT collected by the default test suite.

    python tests/fuzz/run_campaign.py is_valid_xml_utf8 -max_total_time=60

Runs outside the coverage run deliberately: atheris installs its own tracing to
guide mutation, and coverage.py is already tracing. Any input that trips a
property check is written to tests/fuzz/corpus/<target>/ by libFuzzer, where the
corpus replay test picks it up permanently.
"""

import sys

import atheris

from tests.fuzz.harnesses import (check_allowlist_html, check_is_valid_xml_utf8,
                                  check_sanitize_svg_bytes)

TARGETS = {
    'allowlist_html': check_allowlist_html,
    'is_valid_xml_utf8': check_is_valid_xml_utf8,
    'sanitize_svg_bytes': check_sanitize_svg_bytes,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in TARGETS:
        print(f'usage: {sys.argv[0]} <{"|".join(sorted(TARGETS))}> [libfuzzer args]')
        return 2
    name = sys.argv[1]
    check = TARGETS[name]
    corpus = f'tests/fuzz/corpus/{name}'
    atheris.Setup([sys.argv[0], corpus] + sys.argv[2:], check)
    atheris.Fuzz()
    return 0


if __name__ == '__main__':
    sys.exit(main())
```

- [ ] **Step 6: Verify the replay runs and the campaign starts**

Run: `./run_tests.sh tests/test_utils_fuzz_corpus.py -q`
Expected: one passing test per seeded corpus file, plus the two non-empty checks.

Then run a real campaign for each target, 60 seconds each:

```bash
podman-compose -f compose.test.yaml exec -T -w /app test-runner \
    python tests/fuzz/run_campaign.py is_valid_xml_utf8 -max_total_time=60
podman-compose -f compose.test.yaml exec -T -w /app test-runner \
    python tests/fuzz/run_campaign.py sanitize_svg_bytes -max_total_time=60
podman-compose -f compose.test.yaml exec -T -w /app test-runner \
    python tests/fuzz/run_campaign.py allowlist_html -max_total_time=60
```

- [ ] **Step 7: Report the campaign outcome, whatever it is**

Record in the task report, per target: the budget used, the number of executions libFuzzer reported, and the outcome. **"Found nothing in 60 seconds" is a result and must be reported as one** — do not imply broader assurance than the budget supports.

If a campaign finds a crash: libFuzzer writes the input to the corpus directory. Commit it. If the resulting corpus test cannot pass because the bug is real and unfixed, mark that single case `xfail` with a comment naming the defect, and **report it — do not fix production code.** A security fix in deployed software is the project owner's decision.

If atheris and coverage.py conflict when the suite later runs with `--cov`, report that rather than disabling coverage.

- [ ] **Step 8: Document it**

Add a `## Fuzzing` section to `tests/README.md` covering: what the two modes are and why they are split, the campaign command with its budget flag, the fact that campaigns must run outside the coverage run, that a discovered input belongs in the corpus, and that findings are reported rather than fixed.

- [ ] **Step 9: Commit**

```bash
git add requirements.txt tests/fuzz tests/test_utils_fuzz_corpus.py tests/README.md
git commit -m "test: fuzz the XML and SVG rejection paths with atheris"
```

---

### Task 6: Image helpers

**Files:**
- Create: `tests/test_utils_images.py`

**Interfaces:**
- Consumes: pytest's `tmp_path`. Pillow is already a runtime dependency.
- Produces: nothing later tasks depend on.

Functions: `to_srgb` (20), `get_new_frames` (9), `scale_gif` (6), `save_new_gif` (1).

Build real images with Pillow. Do not add binary fixtures to the repo.

- [ ] **Step 1: Write the tests**

```python
from PIL import Image, ImageCms

from app.utils import get_new_frames, save_new_gif, scale_gif, to_srgb


def make_gif(path, frames=3, size=(64, 64)):
    images = [Image.new('RGBA', size, (i * 60 % 255, 0, 0, 255)) for i in range(frames)]
    images[0].save(path, save_all=True, append_images=images[1:], duration=40, loop=0)
    return path


class TestToSrgb:
    def test_cmyk_is_converted_to_rgb(self):
        """The CMYK arm: profileToProfile cannot take CMYK directly."""
        result = to_srgb(Image.new('CMYK', (8, 8)))
        assert result.mode == 'RGB'

    def test_rgb_without_a_profile_is_returned_as_rgb(self):
        result = to_srgb(Image.new('RGB', (8, 8), (10, 20, 30)))
        assert result.mode == 'RGB'

    def test_an_embedded_profile_is_used_as_the_source(self):
        """The icc_bytes arm, rather than the assume= fallback."""
        image = Image.new('RGB', (8, 8), (10, 20, 30))
        image.info['icc_profile'] = ImageCms.ImageCmsProfile(
            ImageCms.createProfile('sRGB')).tobytes()

        result = to_srgb(image)

        assert result.mode == 'RGB'
        assert 'icc_profile' in result.info

    def test_output_carries_an_srgb_tag(self):
        result = to_srgb(Image.new('RGB', (8, 8), (10, 20, 30)))
        assert 'icc_profile' in result.info


class TestGetNewFrames:
    def test_every_frame_is_returned(self):
        """Fails if the loop stops short of gif.n_frames."""
        with Image.open(make_gif('/tmp/frames.gif', frames=4)) as gif:
            assert len(get_new_frames(gif, (16, 16))) == 4

    def test_frames_are_thumbnailed_to_fit_the_scale(self):
        with Image.open(make_gif('/tmp/frames2.gif', size=(64, 64))) as gif:
            frames = get_new_frames(gif, (16, 16))
        assert all(f.width <= 16 and f.height <= 16 for f in frames)

    def test_frames_are_rgba(self):
        with Image.open(make_gif('/tmp/frames3.gif')) as gif:
            frames = get_new_frames(gif, (16, 16))
        assert all(f.mode == 'RGBA' for f in frames)


class TestScaleGif:
    def test_scaling_in_place_shrinks_the_file(self, tmp_path):
        path = tmp_path / 'anim.gif'
        make_gif(path, frames=3, size=(128, 128))

        scale_gif(str(path), (16, 16))

        with Image.open(path) as result:
            assert result.width <= 16
            assert result.n_frames == 3

    def test_scaling_to_a_new_path_leaves_the_original(self, tmp_path):
        source = tmp_path / 'source.gif'
        target = tmp_path / 'target.gif'
        make_gif(source, size=(128, 128))

        scale_gif(str(source), (16, 16), new_path=str(target))

        with Image.open(source) as original:
            assert original.width == 128
        with Image.open(target) as scaled:
            assert scaled.width <= 16


class TestSaveNewGif:
    def test_writes_an_animation_with_every_frame(self, tmp_path):
        target = tmp_path / 'out.gif'
        frames = [Image.new('RGBA', (8, 8), (i * 80 % 255, 0, 0, 255)) for i in range(3)]
        info = {'loop': True, 'duration': 40, 'background': 223,
                'extension': b'NETSCAPE2.0', 'transparency': 223}

        save_new_gif(frames, info, str(target))

        with Image.open(target) as result:
            assert result.n_frames == 3
```

- [ ] **Step 2: Run**

Run: `./run_tests.sh tests/test_utils_images.py -q`

- [ ] **Step 3: Check `to_srgb`'s fallback arms**

Run with `--cov=app.utils --cov-report=term-missing` and read which of `to_srgb`'s lines remain. Its `except ImageCms.PyCMSError` and `except AttributeError` arms may be unreachable: `src = ImageCms.ImageCmsProfile(io.BytesIO(icc_bytes))` sits OUTSIDE the try block, so a corrupt profile raises before the guarded call rather than inside it.

If those arms cannot be reached without patching Pillow internals, **report it as an honest gap with that reason** and add a `# pragma: no cover` carrying the explanation. Do not reach for `unittest.mock` to force an exception out of a library call — that would assert on a mock.

- [ ] **Step 4: Prove discrimination**

Break `get_new_frames` by changing `for frame in range(actual_frames)` to `range(1)`; confirm `test_every_frame_is_returned` fails. Restore and record.

- [ ] **Step 5: Commit**

```bash
git add tests/test_utils_images.py
git commit -m "test: cover image helpers in app/utils.py with real Pillow images"
```

---

### Task 7: Redis-backed helpers

**Files:**
- Create: `tests/test_utils_redis.py`

**Interfaces:**
- Consumes: `redis_double` and `app` from `tests/conftest.py`. `redis_double` patches `get_redis_connection` in all four modules that bind it — see its docstring.
- Produces: nothing later tasks depend on.

Functions: `create_captcha` (11), `decode_captcha` (9), `get_redis_connection` (3), `publish_sse_event` (2).

`create_captcha` was classified pure by the spec's heuristic but calls `get_redis_connection()`. It belongs here.

`decode_captcha`'s existing test lives in `tests/test_fixture_proofs.py` and is the model referenced by the spec — it was rewritten in sub-project 0 after the original passed with the regex guard deleted. Do not duplicate it; add only the cases it does not cover.

- [ ] **Step 1: Write the tests**

```python
import re

import redis

from app.utils import create_captcha, decode_captcha, get_redis_connection, publish_sse_event


class TestCreateCaptcha:
    def test_returns_an_image_an_audio_clip_and_a_uuid(self, app, redis_double):
        result = create_captcha()

        assert re.fullmatch(r'[0-9a-f]{24}', result['uuid'])
        assert result['image'].startswith('data:image/jpeg;base64,')
        assert result['audio'].startswith('data:audio/wav;base64,')

    def test_the_stored_code_is_accepted_by_decode_captcha(self, app, redis_double):
        """The round trip both functions exist for.

        Fails if create_captcha stops storing the code, or stores it under a
        different key than decode_captcha reads.
        """
        result = create_captcha()
        stored = redis_double.get('captcha_' + result['uuid'])

        assert stored is not None
        assert decode_captcha(result['uuid'], stored) is True

    def test_the_requested_length_is_honoured(self, app, redis_double):
        result = create_captcha(length=6)
        assert len(redis_double.get('captcha_' + result['uuid'])) == 6

    def test_each_captcha_gets_its_own_uuid(self, app, redis_double):
        assert create_captcha()['uuid'] != create_captcha()['uuid']


class TestDecodeCaptcha:
    def test_a_wrong_code_is_rejected(self, app, redis_double):
        redis_double.set('captcha_' + 'b' * 24, 'WXYZ')
        assert decode_captcha('b' * 24, 'nope') is False

    def test_an_unknown_uuid_is_rejected(self, app, redis_double):
        assert decode_captcha('c' * 24, 'wxyz') is False

    def test_a_none_uuid_is_rejected_without_raising(self, app, redis_double):
        """The except TypeError arm: re.fullmatch(None) raises."""
        assert decode_captcha(None, 'wxyz') is False


class TestGetRedisConnection:
    def test_a_tcp_connection_string_is_parsed(self, app):
        """redis.Redis does not connect until a command is issued, so this
        builds a client without touching the network."""
        client = get_redis_connection('redis://localhost:6379/2')
        assert isinstance(client, redis.Redis)
        assert client.connection_pool.connection_kwargs['db'] == 2

    def test_a_unix_socket_connection_string_is_parsed(self, app):
        client = get_redis_connection('unix:///var/run/redis.sock?db=3')
        assert isinstance(client, redis.Redis)
        assert client.connection_pool.connection_kwargs['path'] == '/var/run/redis.sock'

    def test_no_argument_falls_back_to_configured_url(self, app):
        assert isinstance(get_redis_connection(), redis.Redis)


class TestPublishSseEvent:
    def test_publishes_to_the_named_channel(self, app, redis_double):
        """Fails if the key or value is dropped on the way to publish().

        Uses a real subscriber on the double rather than asserting a call.
        """
        pubsub = redis_double.pubsub()
        pubsub.subscribe('notifications:7')
        pubsub.get_message(timeout=1)          # the subscribe confirmation

        publish_sse_event('notifications:7', '{"num_notifs": 3}')

        message = pubsub.get_message(timeout=1)
        assert message['data'] == '{"num_notifs": 3}'
```

- [ ] **Step 2: Run**

Run: `./run_tests.sh tests/test_utils_redis.py -q`

`create_captcha` generates an image and an audio clip and is slower than the rest of the suite. If the four captcha tests together exceed about two seconds, report the figure — the suite's speed is a property worth defending.

- [ ] **Step 3: Prove discrimination**

Delete the `redis_client.set(...)` line from `create_captcha`; confirm `test_the_stored_code_is_accepted_by_decode_captcha` fails. Restore and record.

- [ ] **Step 4: Commit**

```bash
git add tests/test_utils_redis.py
git commit -m "test: cover captcha and SSE helpers in app/utils.py"
```

---

### Task 8: Request-context helpers

**Files:**
- Create: `tests/test_utils_request_context.py`

**Interfaces:**
- Consumes: the `app` fixture. Use `app.test_request_context(...)` for each case.
- Produces: nothing later tasks depend on.

Functions: `ip_address` (7), `requestor_domain` (7), `show_ban_message` (5), `referrer` (4), `back` (4), `display_back_button` (3), `debug_mode_only` (3), `on_unread_notifications_set` (2), `user_cookie_banned` (2), `block_bots` (1), `compaction_level` (1).

- [ ] **Step 1: Write the tests**

```python
import pytest
from flask import abort

from app.utils import (back, block_bots, compaction_level, debug_mode_only,
                       display_back_button, ip_address, referrer, requestor_domain,
                       show_ban_message, user_cookie_banned)


class TestIpAddress:
    def test_cloudflare_header_wins(self, app):
        with app.test_request_context(headers={'CF-Connecting-IP': '1.2.3.4',
                                               'X-Forwarded-For': '5.6.7.8'}):
            assert ip_address() == '1.2.3.4'

    def test_forwarded_for_is_used_when_cloudflare_is_absent(self, app):
        with app.test_request_context(headers={'X-Forwarded-For': '5.6.7.8'}):
            assert ip_address() == '5.6.7.8'

    def test_remote_addr_is_the_last_resort(self, app):
        with app.test_request_context(environ_base={'REMOTE_ADDR': '9.9.9.9'}):
            assert ip_address() == '9.9.9.9'

    def test_only_the_first_of_a_proxy_chain_is_kept(self, app):
        """Fails if the comma-splitting is removed; the rest of the chain is
        attacker-controlled and must not be trusted as the client IP."""
        with app.test_request_context(headers={'X-Forwarded-For': '1.2.3.4, 5.6.7.8, 9.9.9.9'}):
            assert ip_address() == '1.2.3.4'


class TestRequestorDomain:
    def test_domain_is_taken_from_a_bot_style_user_agent(self, app):
        ua = 'SomeCrawler/1.0 (+https://crawler.example/info)'
        with app.test_request_context(headers={'User-Agent': ua}):
            assert requestor_domain() == 'crawler.example'

    def test_a_user_agent_without_a_plus_yields_nothing(self, app):
        with app.test_request_context(headers={'User-Agent': 'Mozilla/5.0'}):
            assert requestor_domain() == ''

    def test_an_absent_user_agent_yields_nothing(self, app):
        with app.test_request_context():
            assert requestor_domain() == ''


class TestReferrer:
    def test_the_next_query_parameter_wins(self, app):
        with app.test_request_context('/?next=/somewhere'):
            assert referrer() == '/somewhere'

    def test_a_posted_referrer_field_is_next(self, app):
        with app.test_request_context('/', method='POST', data={'referrer': '/from-form'}):
            assert referrer() == '/from-form'

    def test_an_on_site_referer_header_is_used(self, app):
        with app.test_request_context('/', headers={'Referer': 'https://test.piefed.local/x'}):
            assert referrer() == 'https://test.piefed.local/x'

    def test_an_off_site_referer_header_is_ignored(self, app):
        """Fails if the SERVER_NAME check is dropped -- an open-redirect guard."""
        with app.test_request_context('/', headers={'Referer': 'https://evil.example/x'}):
            assert referrer(default='/fallback') == '/fallback'

    def test_the_default_is_used_when_nothing_else_matches(self, app):
        with app.test_request_context('/'):
            assert referrer(default='/fallback') == '/fallback'

    def test_the_index_is_the_final_fallback(self, app):
        with app.test_request_context('/'):
            assert referrer() == '/'


class TestBack:
    def test_redirects_to_the_referrer(self, app):
        with app.test_request_context('/here', headers={'Referer': '/there'}):
            assert back('/default').headers['Location'] == '/there'

    def test_falls_back_when_there_is_no_referrer(self, app):
        with app.test_request_context('/here'):
            assert back('/default').headers['Location'] == '/default'

    def test_falls_back_when_the_referrer_is_the_current_url(self, app):
        """Otherwise the user is redirected to the page they are already on."""
        with app.test_request_context('/here') as ctx:
            with app.test_request_context('/here', headers={'Referer': ctx.request.url}):
                assert back('/default').headers['Location'] == '/default'


class TestUserCookieBanned:
    def test_the_ban_cookie_is_detected(self, app):
        with app.test_request_context('/', headers={'Cookie': 'sesion=17489047567495'}):
            assert user_cookie_banned() is True

    def test_no_cookie(self, app):
        with app.test_request_context('/'):
            assert user_cookie_banned() is False


class TestCompactionLevel:
    def test_the_cookie_value_is_returned(self, app):
        with app.test_request_context('/', headers={'Cookie': 'compact_level=compact-max'}):
            assert compaction_level() == 'compact-max'

    def test_absent_cookie_returns_none_under_https(self, app):
        with app.test_request_context('/'):
            assert compaction_level() is None


class TestDisplayBackButton:
    def test_ios_with_an_on_site_referrer_shows_the_button(self, app):
        ua = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)'
        with app.test_request_context('/', headers={
                'User-Agent': ua, 'Referer': app.config['SERVER_URL'] + '/x'}):
            assert display_back_button() == 'display_back_button'

    def test_ios_with_an_off_site_referrer_does_not(self, app):
        ua = 'Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X)'
        with app.test_request_context('/', headers={
                'User-Agent': ua, 'Referer': 'https://elsewhere.example/x'}):
            assert display_back_button() == ''

    def test_a_desktop_browser_never_shows_it(self, app):
        with app.test_request_context('/', headers={'User-Agent': 'Mozilla/5.0 (X11)'}):
            assert display_back_button() == ''


class TestBlockBots:
    def test_a_human_reaches_the_view(self, app):
        decorated = block_bots(lambda: 'view ran')
        with app.test_request_context('/', headers={'User-Agent': 'Mozilla/5.0'}):
            assert decorated() == 'view ran'

    def test_a_bot_is_refused(self, app):
        decorated = block_bots(lambda: 'view ran')
        with app.test_request_context('/', headers={'User-Agent': 'Googlebot/2.1'}):
            with pytest.raises(Exception) as excinfo:
                decorated()
            assert '403' in str(excinfo.value)


class TestDebugModeOnly:
    def test_the_view_runs_in_debug_mode(self, app):
        decorated = debug_mode_only(lambda: 'view ran')
        original = app.debug
        app.debug = True
        try:
            with app.test_request_context('/'):
                assert decorated() == 'view ran'
        finally:
            app.debug = original

    def test_the_view_is_refused_in_production_mode(self, app):
        decorated = debug_mode_only(lambda: 'view ran')
        original = app.debug
        app.debug = False
        try:
            with app.test_request_context('/'):
                with pytest.raises(Exception) as excinfo:
                    decorated()
                assert '403' in str(excinfo.value)
        finally:
            app.debug = original


class TestShowBanMessage:
    def test_redirects_to_the_index_and_sets_the_ban_cookie(self, app, db_session):
        """Fails if the cookie stops being set -- it is how the ban survives logout."""
        with app.test_request_context('/'):
            response = show_ban_message()

        assert response.status_code == 302
        assert 'sesion=' in response.headers.get('Set-Cookie', '')
```

- [ ] **Step 2: Run**

Run: `./run_tests.sh tests/test_utils_request_context.py -q`

`show_ban_message` calls `logout_user()`, which needs a login manager and may need a session. If it fails for that reason, add whatever the failure names — do not delete the test.

`compaction_level`'s uncovered line is the `HTTP_PROTOCOL == 'mixed'` branch. If `TestConfig` does not set `mixed`, that line stays uncovered: set it for that one test with `app.config['HTTP_PROTOCOL'] = 'mixed'` inside a try/finally that restores it, and add a test asserting the `compact-min compact-max` default.

- [ ] **Step 3: Cover `on_unread_notifications_set`**

This is a SQLAlchemy event listener, not a function anyone calls directly. It
fires when `User.unread_notifications` is assigned and publishes an SSE event.
Drive it by assigning the attribute and observing the published message:

```python
class TestUnreadNotificationsListener:
    def test_setting_the_count_publishes_an_sse_event(self, app, db_session, redis_double):
        """Fails if the event listener is unregistered, or stops publishing.

        NOTIF_SERVER must be truthy or the listener returns early by design.
        """
        original = app.config['NOTIF_SERVER']
        app.config['NOTIF_SERVER'] = 'https://notif.example'
        try:
            user = make_user(None, 'notified', local=True)
            pubsub = redis_double.pubsub()
            pubsub.subscribe(f'notifications:{user.id}')
            pubsub.get_message(timeout=1)          # the subscribe confirmation

            user.unread_notifications = 5

            message = pubsub.get_message(timeout=1)
            assert message is not None, 'no SSE event was published'
            assert json.loads(message['data']) == {'num_notifs': 5}
        finally:
            app.config['NOTIF_SERVER'] = original

    def test_setting_the_same_value_publishes_nothing(self, app, db_session, redis_double):
        """The `value != oldvalue` guard: re-saving an unchanged count must not
        wake every connected client."""
        original = app.config['NOTIF_SERVER']
        app.config['NOTIF_SERVER'] = 'https://notif.example'
        try:
            user = make_user(None, 'unchanged', local=True)
            user.unread_notifications = 5
            pubsub = redis_double.pubsub()
            pubsub.subscribe(f'notifications:{user.id}')
            pubsub.get_message(timeout=1)

            user.unread_notifications = 5

            assert pubsub.get_message(timeout=1) is None
        finally:
            app.config['NOTIF_SERVER'] = original
```

Add `import json` and `from tests.factories import make_user` to the top of the
file — not inside the tests.

`download_defeds` is **moved out of scope**. Its three lines are a dispatcher
branching on `current_app.debug`, and both arms invoke
`download_defeds_worker`, which calls the network-bound
`retrieve_defederation_list`. Covering it means covering that worker, which is
1c's job. Record the move in the report alongside the other six.

- [ ] **Step 4: Prove discrimination**

Delete the comma-splitting from `ip_address`; confirm `test_only_the_first_of_a_proxy_chain_is_kept` fails. Delete the `SERVER_NAME` check from `referrer`; confirm `test_an_off_site_referer_header_is_ignored` fails. Restore after each and record both.

- [ ] **Step 5: Commit**

```bash
git add tests/test_utils_request_context.py
git commit -m "test: cover request-context helpers in app/utils.py"
```

---

### Task 9: Application-context globals, templates and filesystem

**Files:**
- Create: `tests/test_utils_context_globals.py`
- Modify: `coverage_floors.ini`
- Modify: `tests/README.md`

**Interfaces:**
- Consumes: the `app` fixture, pytest's `tmp_path`, and `flask.g`.
- Produces: the raised floor for `app/utils.py` that sub-projects 1b and 1c build on.

Functions: `ensure_directory_exists` (9), `get_timezones` (8), `theme_list` (7), `render_from_tpl` (7), `debug_checkpoint` (7), `round_invisible_digits` (5), `localize_datetime` (4), `humanize_number` (3), `orjson_response` (1).

`humanize_number`, `round_invisible_digits` and `debug_checkpoint` read `flask.g`, so each needs an app or request context with `g.locale` set.

- [ ] **Step 1: Write the tests**

```python
import json
from datetime import datetime, timedelta

from flask import g

from app.utils import (debug_checkpoint, ensure_directory_exists, get_timezones,
                       humanize_number, localize_datetime, orjson_response,
                       render_from_tpl, round_invisible_digits, theme_list)
from app.models import utcnow


class TestGetTimezones:
    def test_regions_are_grouped(self, app):
        result = get_timezones()
        assert 'Europe' in result
        assert 'America' in result

    def test_excluded_regions_are_absent(self, app):
        """Fails if the exclusion list is dropped -- Etc/ entries are noise in a picker."""
        result = get_timezones()
        for region in ['Arctic', 'Atlantic', 'Etc', 'Other']:
            assert region not in result

    def test_entries_are_value_label_pairs(self, app):
        assert ('Europe/London', 'Europe/London') in get_timezones()['Europe']

    def test_zones_without_a_region_are_skipped(self, app):
        """UTC has no slash, so it must not appear as its own region."""
        assert 'UTC' not in get_timezones()


class TestRenderFromTpl:
    def test_year_is_substituted(self, app):
        assert render_from_tpl('{% year %}') == str(utcnow().year)

    def test_whitespace_inside_the_tag_is_ignored(self, app):
        assert render_from_tpl('{%year%}') == str(utcnow().year)

    def test_day_and_month_are_zero_padded(self, app):
        now = utcnow()
        assert render_from_tpl('{% day %}') == f'{now.day:02d}'
        assert render_from_tpl('{% month %}') == f'{now.month:02d}'

    def test_week_is_the_iso_week_number(self, app):
        assert render_from_tpl('{% week %}') == f'{utcnow().isocalendar()[1]:02d}'

    def test_an_unknown_tag_is_left_alone(self, app):
        assert render_from_tpl('{% nonsense %}') == '{% nonsense %}'

    def test_text_around_the_tag_survives(self, app):
        assert render_from_tpl('backup-{% year %}.zip').startswith('backup-')


class TestHumanizeNumber:
    def test_falsy_values_render_as_zero(self, app):
        with app.test_request_context('/'):
            g.locale = 'en'
            assert humanize_number(0) == '0'
            assert humanize_number(None) == '0'

    def test_large_numbers_are_abbreviated(self, app):
        with app.test_request_context('/'):
            g.locale = 'en'
            assert humanize_number(1215) == '1.2K'


class TestRoundInvisibleDigits:
    def test_none_becomes_zero(self, app):
        with app.test_request_context('/'):
            g.locale = 'en'
            assert round_invisible_digits(None) == 0

    def test_small_numbers_are_returned_unchanged(self, app):
        with app.test_request_context('/'):
            g.locale = 'en'
            assert round_invisible_digits(123) == 123

    def test_numbers_that_abbreviate_are_rounded_to_the_visible_digits(self, app):
        """1215 renders as '1.2K', so the count must round to a value that
        matches the plural form the abbreviation implies."""
        with app.test_request_context('/'):
            g.locale = 'en'
            assert round_invisible_digits(1215) == 1000


class TestLocalizeDatetime:
    def test_a_recent_time_reads_as_relative(self, app):
        result = localize_datetime(datetime.utcnow() - timedelta(hours=2))
        assert 'hour' in result

    def test_an_unknown_locale_falls_back_to_english(self, app):
        """The except ValueError arm."""
        result = localize_datetime(datetime.utcnow() - timedelta(hours=2), locale='nonsense')
        assert 'hour' in result


class TestDebugCheckpoint:
    def test_the_first_checkpoint_has_no_delta(self, app):
        with app.test_request_context('/'):
            _, delta = debug_checkpoint('first')
            assert delta == 0

    def test_a_later_checkpoint_measures_from_the_previous_one(self, app):
        with app.test_request_context('/'):
            debug_checkpoint('first')
            _, delta = debug_checkpoint('second')
            assert delta >= 0

    def test_checkpoints_accumulate_on_g(self, app):
        with app.test_request_context('/'):
            debug_checkpoint('first')
            debug_checkpoint('second')
            assert [name for name, _ in g._debug_checkpoints] == ['first', 'second']

    def test_checkpoints_do_not_leak_between_requests(self, app):
        """g is per-request; a leak here would corrupt every later measurement."""
        with app.test_request_context('/'):
            debug_checkpoint('first')
        with app.test_request_context('/'):
            _, delta = debug_checkpoint('fresh')
            assert delta == 0


class TestThemeList:
    def test_the_built_in_theme_is_always_first(self, app):
        assert theme_list()[0] == ('piefed', 'PieFed')

    def test_every_entry_is_a_directory_and_display_name_pair(self, app):
        assert all(isinstance(entry, tuple) and len(entry) == 2 for entry in theme_list())


class TestOrjsonResponse:
    def test_serialises_the_object_as_json(self, app):
        response = orjson_response({'a': 1})
        assert response.mimetype == 'application/json'
        assert json.loads(response.get_data()) == {'a': 1}

    def test_the_status_and_headers_are_honoured(self, app):
        response = orjson_response({}, status=404, headers={'X-Test': 'yes'})
        assert response.status_code == 404
        assert response.headers['X-Test'] == 'yes'


class TestEnsureDirectoryExists:
    def test_a_nested_directory_is_created(self, app, tmp_path):
        target = tmp_path / 'a' / 'b' / 'c'

        ensure_directory_exists(str(target))

        assert target.is_dir()

    def test_an_existing_directory_is_left_alone(self, app, tmp_path):
        target = tmp_path / 'exists'
        target.mkdir()
        (target / 'keep.txt').write_text('kept')

        ensure_directory_exists(str(target))

        assert (target / 'keep.txt').read_text() == 'kept'

    def test_an_unwritable_directory_warns_rather_than_raising(self, app, tmp_path, caplog):
        """The os.access arm. Skipped as root, which bypasses mode bits."""
        import os
        if os.geteuid() == 0:
            import pytest
            pytest.skip('running as root; mode bits do not restrict access')
        target = tmp_path / 'readonly'
        target.mkdir(mode=0o500)

        ensure_directory_exists(str(target))

        assert 'not writable' in caplog.text
```

Note: the `import os` and `import pytest` inside `test_an_unwritable_directory_warns_rather_than_raising` are inline imports and violate the project rule. Move both to the top of the file when writing it — they are shown inline here only to keep the example self-contained.

- [ ] **Step 2: Run**

Run: `./run_tests.sh tests/test_utils_context_globals.py -q`

`ensure_directory_exists` splits on `/` and rebuilds from `''`, so for an absolute path the first `os.mkdir('')` would raise. Check whether it actually works with the absolute path `tmp_path` gives. If it does not, that is a real defect in a function used for upload directories — **report it** rather than working around it by passing a relative path.

`humanize_number(1215) == '1.2K'` depends on Babel's locale data. If the real output differs in case or separator, correct the assertion to the real value after confirming it is sensible.

- [ ] **Step 3: Measure the achieved coverage**

Run the full suite with coverage:

```bash
./run_tests.sh tests/ -q --ignore=tests/test_activitypub_util.py --cov=app --cov-report=json && \
podman-compose -f compose.test.yaml exec -T test-runner \
    python tests/check_coverage_floors.py coverage.json coverage_floors.ini
```

Read `app/utils.py`'s `percent_covered` from the report.

- [ ] **Step 4: Raise the floor**

Add to `coverage_floors.ini` under `[floors]`, using the measured figure rounded DOWN to the nearest whole number so a later rounding difference cannot trip it:

```ini
app/utils.py = <measured, rounded down>
```

Remember `percent_covered` is a blended statement-and-branch figure — this is the case the campaign findings warn about, and it is why the floor is the measured value rather than a target.

- [ ] **Step 5: Prove the new floor bites**

Temporarily raise `app/utils.py`'s floor by one point, re-run the checker, and confirm it exits non-zero naming the module. Restore the correct value. Record both outputs in the report.

- [ ] **Step 6: Document what moved**

Add a short section to `tests/README.md` recording that sub-project 1a covered the pure and context-only functions of `app/utils.py`, that six functions moved to 1b/1c with the reasons, and where the fuzz corpus lives.

- [ ] **Step 7: Commit**

```bash
git add tests/test_utils_context_globals.py coverage_floors.ini tests/README.md
git commit -m "test: cover context globals in app/utils.py, raise its floor"
```

---

## Verification

After Task 9:

1. `./run_tests.sh tests/ -q --ignore=tests/test_activitypub_util.py` — 0 failed, runtime in seconds.
2. The floors checker reports all three modules met.
3. The 58 in-scope functions are at 100% statement and branch coverage, or carry a reported reason.
4. A fuzz campaign of at least 60 seconds has run against both targets and its outcome — including "found nothing" — is in the report.
5. The five suspected bugs each have a ruling recorded.
6. The seven moved functions are recorded for sub-projects 1b and 1c.
