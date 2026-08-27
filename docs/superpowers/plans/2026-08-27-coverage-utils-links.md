# Coverage Campaign 1c: Link and Domain Handling — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the `domain_from_url` host-confusion defect, then bring the five link-handling functions in `app/utils.py` to full statement and branch coverage.

**Architecture:** One production fix under TDD first, then one test file per function. Each function has a different testing shape — DB write, pure, HTTP+DB, ap_id branch chain, form validator — so each is its own task with its own review gate. A final task fuzzes the three URL parsers, re-measures, and raises the floor.

**Tech Stack:** pytest, `respx` (via the `http_mock` fixture), `atheris`, PostgreSQL test DB in podman, `./run_tests.sh`.

**Spec:** `docs/superpowers/specs/2026-08-27-coverage-utils-links-design.md`

## Global Constraints

- `if TYPE_CHECKING` is always a bug. Never introduce it, and never add it to `.coveragerc`'s `exclude_lines`.
- Imports go at the top of the file. No inline imports — `app/utils.py` has 216 catalogued pre-existing violations; add none.
- Every pragma carries a written justification.
- No new runtime dependencies, no migration.
- Report defects; do not fix them without owner authorisation. The `domain_from_url` fix (Task 1) is authorised; nothing else is.
- Tests assert observable behaviour — what the function returns, which `Domain` row exists — never on generated SQL, never on mocks.
- For every test, name the production change that would make it fail.
- Both mutation directions per rule: delete the rule (fails the absence test) and over-broaden it so it fires for everything (fails the presence test).
- A count quoted in prose is a claim. Derive counts with a command and quote the command.
- NO host Python. Use `./run_tests.sh [pytest args]`. Run tests in the FOREGROUND and block on the call.
- Never run `./run_tests.sh --down`.
- Check `pgrep -af '/venv/bin/pytest'` before starting — one suite at a time.
- Baseline at `9a2fcb0a`: **1832 passed, 3 skipped, 0 failed**. `coverage_floors.ini` has `app/utils.py = 67`.

---

## File Structure

| file | responsibility |
|---|---|
| `app/utils.py:1442-1457` | **Modify** — `domain_from_url` host parsing (Task 1 only) |
| `tests/test_domain_from_url.py` | **Create** — Task 1 |
| `tests/test_remove_tracking_from_link.py` | **Create** — Task 2 |
| `tests/test_fixup_url.py` | **Create** — Task 3 |
| `tests/test_rewrite_href.py` | **Create** — Task 4 |
| `tests/test_apply_feed_url_rules.py` | **Create** — Task 5 |
| `tests/test_link_parsers_fuzz.py` | **Create** — Task 6 |
| `coverage_floors.ini` | **Modify** — Task 6 only |
| `tests/README.md` | **Modify** — Task 6 only |

No changes to `tests/factories.py` are anticipated; `make_domain`, `make_instance`, `make_user`, `make_community`, `make_post`, `make_post_reply` already exist.

---

### Task 1: Fix `domain_from_url` host confusion, then cover it

**Files:**
- Modify: `app/utils.py:1442-1457`
- Test: `tests/test_domain_from_url.py` (create)

**Interfaces:**
- Consumes: `make_domain(name)` from `tests/factories.py`
- Produces: corrected `domain_from_url(url: str, create=True) -> Domain | None`. Later tasks rely on it recording the parsed hostname with at most a leading `www.` removed.

Current production code:

```python
def domain_from_url(url: str, create=True) -> Domain:
    parsed_url = urlparse(url.lower().replace('www.', ''))
    if parsed_url and parsed_url.hostname:
        find_this = parsed_url.hostname.lower()
        if find_this == 'youtu.be':
            find_this = 'youtube.com'
        domain = db.session.query(Domain).filter_by(name=find_this).first()
        if create and domain is None:
            domain = Domain(name=find_this)
            db.session.add(domain)
            db.session.commit()
        return domain
    else:
        return None
```

- [ ] **Step 1: Write the failing tests**

Create `tests/test_domain_from_url.py`:

```python
"""domain_from_url (app/utils.py:1442-1457) decides which Domain row a post is
attributed to. Domain carries `banned` (site-wide admin ban) and is the target
of DomainBlock (per-user block), so a defect here is a blocking defect.

Before the fix in this commit, line 1443 read
`urlparse(url.lower().replace('www.', ''))` -- a blanket replacement over the
whole URL string, before parsing, removing every occurrence rather than a
leading host label. `awww.evil.example` was recorded as `aevil.example`.
"""
from app.utils import domain_from_url
from tests.factories import make_domain


class TestHostIsNotMangled:
    """Mutation that fails these: restoring `.replace('www.', '')` on the whole
    URL string at app/utils.py:1443."""

    def test_an_interior_www_is_not_stripped(self, app, db_session):
        domain = domain_from_url('https://awww.evil.example/post/1')
        assert domain.name == 'awww.evil.example'

    def test_two_distinct_hosts_do_not_collide(self, app, db_session):
        """The sharp one. Pre-fix both mangle to `aevil.example` and share one
        row, so banning either bans both."""
        first = domain_from_url('https://awww.evil.example/a')
        second = domain_from_url('https://aevil.example/a')
        assert first.id != second.id
        assert {first.name, second.name} == {'awww.evil.example', 'aevil.example'}


class TestLeadingWwwIsStripped:
    """The intended normalization, which the fix must preserve. Mutation that
    fails these: deleting the `startswith('www.')` strip."""

    def test_a_leading_www_is_removed(self, app, db_session):
        domain = domain_from_url('https://www.example.com/a')
        assert domain.name == 'example.com'

    def test_www_and_bare_host_share_one_row(self, app, db_session):
        bare = domain_from_url('https://example.com/a')
        with_www = domain_from_url('https://www.example.com/b')
        assert bare.id == with_www.id
```

- [ ] **Step 2: Run the tests and watch them fail on the defect**

Run: `./run_tests.sh tests/test_domain_from_url.py -q`

Expected: `TestHostIsNotMangled` both fail. `test_an_interior_www_is_not_stripped` must fail with `assert 'aevil.example' == 'awww.evil.example'` — the actual mangling, **not** an error. `TestLeadingWwwIsStripped` both pass already.

If either failure is an error rather than a wrong value, stop and fix the test before touching production code.

- [ ] **Step 3: Apply the fix**

Replace `app/utils.py:1443` and add the prefix strip:

```python
def domain_from_url(url: str, create=True) -> Domain:
    parsed_url = urlparse(url.lower())
    if parsed_url and parsed_url.hostname:
        find_this = parsed_url.hostname.lower()
        if find_this.startswith('www.'):
            find_this = find_this[4:]
        if find_this == 'youtu.be':
            find_this = 'youtube.com'
        domain = db.session.query(Domain).filter_by(name=find_this).first()
        if create and domain is None:
            domain = Domain(name=find_this)
            db.session.add(domain)
            db.session.commit()
        return domain
    else:
        return None
```

- [ ] **Step 4: Run the tests and verify they pass**

Run: `./run_tests.sh tests/test_domain_from_url.py -q`
Expected: 4 passed.

- [ ] **Step 5: Cover the remaining branches**

Append to `tests/test_domain_from_url.py`:

```python
class TestYoutubeAlias:
    """Mutation that fails this: deleting the `youtu.be` alias."""

    def test_youtu_be_is_recorded_as_youtube_com(self, app, db_session):
        domain = domain_from_url('https://youtu.be/dQw4w9WgXcQ')
        assert domain.name == 'youtube.com'


class TestCreateFlag:
    """Mutation that fails the first: changing `if create and domain is None`
    to `if domain is None`. Mutation that fails the second: dropping the
    `create` guard entirely."""

    def test_create_false_returns_none_when_no_row_exists(self, app, db_session):
        assert domain_from_url('https://never-seen.example/a', create=False) is None

    def test_create_false_returns_an_existing_row(self, app, db_session):
        existing = make_domain('seen.example')
        assert domain_from_url('https://seen.example/a', create=False).id == existing.id

    def test_create_true_makes_the_row(self, app, db_session):
        domain = domain_from_url('https://fresh.example/a')
        assert domain is not None and domain.name == 'fresh.example'


class TestUnparseable:
    """Mutation that fails these: deleting the `parsed_url.hostname` guard, so
    the function raises or records an empty name instead of returning None."""

    def test_a_url_with_no_host_returns_none(self, app, db_session):
        assert domain_from_url('not-a-url', create=False) is None

    def test_an_empty_string_returns_none(self, app, db_session):
        assert domain_from_url('', create=False) is None
```

- [ ] **Step 6: Run the file and confirm coverage**

Run: `./run_tests.sh tests/test_domain_from_url.py -q --cov=app.utils --cov-branch --cov-report=term-missing`

Confirm no line in 1442-1457 appears in `Missing`, and no partial-branch arrow falls in that range. Quote the command and the relevant output lines in your report.

- [ ] **Step 7: Revert check — prove the regression test is load-bearing**

Restore the old line temporarily:

```bash
# in app/utils.py, change line 1443 back to:
#     parsed_url = urlparse(url.lower().replace('www.', ''))
# and delete the three-line startswith('www.') strip
./run_tests.sh tests/test_domain_from_url.py -q
git checkout -- app/utils.py
git status --porcelain   # must show no app/ modification
```

Expected: exactly the two `TestHostIsNotMangled` tests fail; everything else passes. Report both counts.

- [ ] **Step 8: Run the full suite**

Run: `./run_tests.sh tests/ -q --ignore=tests/test_activitypub_util.py`
Expected: 1832 + your new tests passed, 3 skipped, 0 failed.

If any pre-existing test fails, that is a **finding** — the fix changed domain attribution and some test may have encoded the old behaviour. Update it to assert the corrected behaviour and say so in your report. **Do not delete a test to make the suite pass.**

- [ ] **Step 9: Check line-number churn**

The fix adds two lines to `app/utils.py`. Every docstring reference below line 1443 in `tests/` is now off by that amount.

```bash
grep -rn 'app/utils\.py:[0-9]' tests/ docs/ | wc -l
```

Prove the shift's shape before bulk-updating — 1b-ii's two security fixes shifted once uniformly (+7) and once piecewise (+13/+0/+10). Update every affected reference. Report how many you changed and how you proved the shift.

- [ ] **Step 10: Commit**

```bash
git add app/utils.py tests/test_domain_from_url.py
git commit -m "security: record the parsed hostname, not a string-replaced URL

domain_from_url stripped 'www.' from the whole URL string before parsing,
removing every occurrence rather than a leading host label. Distinct hosts
collapsed into one Domain row -- awww.evil.example and aevil.example shared a
row, so banning either banned both.

Domain carries `banned` and is DomainBlock's target, so this was a blocking
defect. Fixes future attribution only; existing rows are left as they are.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: `remove_tracking_from_link`

**Files:**
- Test: `tests/test_remove_tracking_from_link.py` (create)

**Interfaces:**
- Consumes: nothing. This is the only pure function in the sub-project — no fixtures beyond `app`.
- Produces: nothing later tasks depend on.

Current production code (`app/utils.py:3083-3105`):

```python
def remove_tracking_from_link(url):
    parsed_url = urlparse(url)
    if parsed_url.netloc == 'youtu.be':
        video_id = parsed_url.path[1:]
        query_params = parse_qs(parsed_url.query)
        if 't' in query_params:
            new_query_params = {'t': query_params['t']}
            new_query_string = urlencode(new_query_params, doseq=True)
        else:
            new_query_string = ''
        cleaned_url = f"https://youtube.com/watch?v={video_id}"
        if new_query_string:
            new_query_string = new_query_string.replace('t=', 'start=')
            cleaned_url += f"&{new_query_string}"
        return cleaned_url
    else:
        return url
```

- [ ] **Step 1: Write the tests**

Create `tests/test_remove_tracking_from_link.py`:

```python
"""remove_tracking_from_link (app/utils.py:3083-3105) rewrites youtu.be share
links to youtube.com/watch, preserving only the timestamp parameter. Every
other host passes through untouched.

This is the only pure function in sub-project 1c -- no DB, no network.
"""
from app.utils import remove_tracking_from_link


class TestYoutubeShortLinks:
    """Mutation that fails these: changing the `netloc == 'youtu.be'` test, or
    deleting the rewrite so the url returns unchanged."""

    def test_a_bare_short_link_becomes_a_watch_url(self, app):
        assert remove_tracking_from_link('https://youtu.be/abc123') == \
            'https://youtube.com/watch?v=abc123'

    def test_tracking_parameters_are_dropped(self, app):
        """si= is the share-tracking parameter; only t= survives."""
        assert remove_tracking_from_link('https://youtu.be/abc123?si=TRACKING') == \
            'https://youtube.com/watch?v=abc123'

    def test_the_timestamp_is_preserved_and_renamed(self, app):
        """t= on youtu.be becomes start= on youtube.com. Mutation that fails
        this: deleting the `.replace('t=', 'start=')`."""
        assert remove_tracking_from_link('https://youtu.be/abc123?t=42') == \
            'https://youtube.com/watch?v=abc123&start=42'

    def test_a_timestamp_alongside_tracking_keeps_only_the_timestamp(self, app):
        assert remove_tracking_from_link('https://youtu.be/abc123?si=X&t=42') == \
            'https://youtube.com/watch?v=abc123&start=42'


class TestEverythingElsePassesThrough:
    """Mutation that fails these: removing the `else: return url` arm, or
    widening the netloc test so it matches every host."""

    def test_a_full_youtube_url_is_untouched(self, app):
        url = 'https://www.youtube.com/watch?v=abc123&si=TRACKING'
        assert remove_tracking_from_link(url) == url

    def test_an_unrelated_host_is_untouched(self, app):
        url = 'https://example.com/a?utm_source=newsletter'
        assert remove_tracking_from_link(url) == url

    def test_a_host_merely_containing_youtu_be_is_untouched(self, app):
        """netloc equality, not substring. Mutation that fails this: changing
        `==` to `in`."""
        url = 'https://notyoutu.be/abc123'
        assert remove_tracking_from_link(url) == url
```

- [ ] **Step 2: Run and confirm they pass**

Run: `./run_tests.sh tests/test_remove_tracking_from_link.py -q`
Expected: 7 passed.

These describe existing correct behaviour, so they pass immediately. That is expected for coverage work and is why Step 3 exists — a test that has never failed is unproven.

- [ ] **Step 3: Both mutation directions**

For each of the two classes, run both:

```bash
# DELETE direction: in app/utils.py, change the youtu.be test to `if False:`
./run_tests.sh tests/test_remove_tracking_from_link.py -q
git checkout -- app/utils.py

# OVER-BROADEN direction: change it to `if True:`
./run_tests.sh tests/test_remove_tracking_from_link.py -q
git checkout -- app/utils.py
git status --porcelain    # must be clean
```

Expected: the delete direction fails only `TestYoutubeShortLinks`; the over-broaden direction fails only `TestEverythingElsePassesThrough`. Report both counts. If either direction fails nothing, that is a finding — say so.

- [ ] **Step 4: Confirm coverage and commit**

Run: `./run_tests.sh tests/test_remove_tracking_from_link.py -q --cov=app.utils --cov-branch --cov-report=term-missing`

Confirm 3081-3103 is fully covered, both arms of every `if`.

```bash
git add tests/test_remove_tracking_from_link.py
git commit -m "test: cover remove_tracking_from_link's youtu.be rewrite

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: `fixup_url`

**Files:**
- Test: `tests/test_fixup_url.py` (create)

**Interfaces:**
- Consumes: `make_instance(domain, software)` from `tests/factories.py`; the `http_mock` fixture (respx router) from `tests/conftest.py`.
- Produces: nothing later tasks depend on.

`fixup_url` (`app/utils.py:3110-3167`) returns a `(thumbnail_url, embed_url)` tuple. It has two independent halves: a peertube branch that makes an HTTP request, and a YouTube URL matrix.

**The peertube branch needs BOTH a seeded DB row and a mocked route.** It only fires when the URL ends in a `/w/`-shaped path *and* `parsed_url.netloc` appears in `SELECT domain FROM instance WHERE software = 'peertube'`. Seed with `make_instance('peertube.example', software='peertube')`.

`get_request` uses httpx, so `http_mock` intercepts it. The session-scoped `block_outbound_http` router registers zero routes and raises on anything `http_mock` does not match — so a forgotten route surfaces as an error, not a hang.

- [ ] **Step 1: Write the YouTube matrix tests**

Create `tests/test_fixup_url.py`:

```python
"""fixup_url (app/utils.py:3110-3167) returns (thumbnail_url, embed_url) for a
submitted link. Two independent halves: a peertube branch that fetches the
canonical video id over HTTP, and a YouTube URL matrix.

YouTube's URL formats have no specification. The expectations below are derived
from the formats the production code already handles -- observed behaviour, not
an authoritative source. WHATWG URL Standard and RFC 3986 govern the parsing
underneath; YouTube's path conventions do not.
"""
import pytest

from app.utils import fixup_url
from tests.factories import make_instance


class TestNonYoutubePassesThrough:
    """Mutation that fails this: removing the youtube_domains membership test
    so every host takes the YouTube path."""

    def test_an_unrelated_host_returns_the_url_unchanged_in_both_slots(self, app):
        with app.test_request_context('/'):
            thumbnail, embed = fixup_url('https://example.com/article')
        assert thumbnail == 'https://example.com/article'
        assert embed == 'https://example.com/article'


class TestYoutubeVideoForms:
    """Each parametrised case is one path shape the production code handles.
    Mutation that fails each: deleting that shape's branch at 3145-3150."""

    @pytest.mark.parametrize('url,video_id', [
        ('https://www.youtube.com/watch?v=abc123', 'abc123'),
        ('https://youtube.com/shorts/abc123', 'abc123'),
        ('https://youtu.be/abc123', 'abc123'),
        ('https://m.youtube.com/watch?v=abc123', 'abc123'),
        ('https://music.youtube.com/watch?v=abc123', 'abc123'),
    ])
    def test_a_video_url_yields_canonical_thumbnail_and_embed(self, app, url, video_id):
        with app.test_request_context('/'):
            thumbnail, embed = fixup_url(url)
        assert thumbnail == f'https://youtu.be/{video_id}'
        assert embed == f'https://www.youtube.com/watch?v={video_id}'


class TestTimestamps:
    """`start` wins over `t` -- they are checked in that order at 3153-3156.
    Mutation that fails the third: swapping the elif order."""

    def test_a_start_parameter_is_appended(self, app):
        with app.test_request_context('/'):
            _, embed = fixup_url('https://www.youtube.com/watch?v=abc123&start=42')
        assert embed == 'https://www.youtube.com/watch?v=abc123&start=42'

    def test_a_t_parameter_is_appended_as_start(self, app):
        with app.test_request_context('/'):
            _, embed = fixup_url('https://www.youtube.com/watch?v=abc123&t=42')
        assert embed == 'https://www.youtube.com/watch?v=abc123&start=42'

    def test_start_takes_precedence_over_t(self, app):
        with app.test_request_context('/'):
            _, embed = fixup_url('https://www.youtube.com/watch?v=abc123&start=1&t=2')
        assert embed == 'https://www.youtube.com/watch?v=abc123&start=1'


class TestPassThroughYoutubeForms:
    """Playlists and posts are let through unmolested with an EMPTY thumbnail --
    a distinct return shape. Mutation that fails these: deleting the early
    return at 3139-3142."""

    def test_a_playlist_returns_an_empty_thumbnail(self, app):
        url = 'https://www.youtube.com/playlist?list=PL123'
        with app.test_request_context('/'):
            thumbnail, embed = fixup_url(url)
        assert thumbnail == ''
        assert embed == url

    def test_a_post_returns_an_empty_thumbnail(self, app):
        url = 'https://www.youtube.com/post/abc123'
        with app.test_request_context('/'):
            thumbnail, embed = fixup_url(url)
        assert thumbnail == ''
        assert embed == url
```

- [ ] **Step 2: Run and confirm they pass**

Run: `./run_tests.sh tests/test_fixup_url.py -q`

If any case fails, do **not** adjust the assertion to match. Establish first whether the production code or your expectation is wrong, and report it either way.

- [ ] **Step 3: Write the peertube tests**

Append:

```python
class TestPeertube:
    """The peertube branch fires only when the path is /w/-shaped AND the host
    is a known peertube instance. Both halves must be seeded: a DB row via
    make_instance(software='peertube'), and an http_mock route.

    Mutation that fails the first test: deleting the `if parsed_url.netloc in
    peertube_domains` check, or the embed_url assignment at 3122.
    """

    def test_a_known_peertube_host_uses_the_canonical_id(self, app, db_session, http_mock):
        make_instance('peertube.example', software='peertube')
        url = 'https://peertube.example/w/aaaaaaaaaaaaaaaaaaaaaa'
        http_mock.get(url).respond(json={'id': 'https://peertube.example/videos/watch/real-id'})
        with app.test_request_context('/'):
            thumbnail, embed = fixup_url(url)
        assert embed == 'https://peertube.example/videos/watch/real-id'
        assert thumbnail == url

    def test_an_unknown_host_makes_no_request(self, app, db_session, http_mock):
        """No instance row seeded, so the netloc test fails and no HTTP call is
        made. If the production code called out anyway, block_outbound_http
        would raise rather than reach the network."""
        url = 'https://unknown.example/w/aaaaaaaaaaaaaaaaaaaaaa'
        with app.test_request_context('/'):
            thumbnail, embed = fixup_url(url)
        assert (thumbnail, embed) == (url, url)

    def test_a_non_200_response_leaves_the_embed_url_alone(self, app, db_session, http_mock):
        """Mutation that fails this: removing the `status_code == 200` check."""
        make_instance('peertube.example', software='peertube')
        url = 'https://peertube.example/w/aaaaaaaaaaaaaaaaaaaaaa'
        http_mock.get(url).respond(status_code=404)
        with app.test_request_context('/'):
            thumbnail, embed = fixup_url(url)
        assert embed == url

    def test_a_response_without_an_id_leaves_the_embed_url_alone(self, app, db_session, http_mock):
        make_instance('peertube.example', software='peertube')
        url = 'https://peertube.example/w/aaaaaaaaaaaaaaaaaaaaaa'
        http_mock.get(url).respond(json={'not_id': 'x'})
        with app.test_request_context('/'):
            thumbnail, embed = fixup_url(url)
        assert embed == url
```

- [ ] **Step 4: Cover what the bare `except:` clauses swallow**

`app/utils.py:3126` and `:3128` are bare `except:`. Each needs a case establishing what it actually catches.

```python
class TestPeertubeErrorSwallowing:
    """app/utils.py:3126 catches a malformed JSON body; :3128 catches a
    transport failure. Both are BARE `except:`, which also catches
    KeyboardInterrupt and SystemExit -- reported as a defect, not fixed here.
    """

    def test_a_malformed_json_body_is_swallowed(self, app, db_session, http_mock):
        make_instance('peertube.example', software='peertube')
        url = 'https://peertube.example/w/aaaaaaaaaaaaaaaaaaaaaa'
        http_mock.get(url).respond(status_code=200, content=b'not json')
        with app.test_request_context('/'):
            thumbnail, embed = fixup_url(url)
        assert (thumbnail, embed) == (url, url)

    def test_a_transport_failure_is_swallowed(self, app, db_session, http_mock):
        import httpx
        make_instance('peertube.example', software='peertube')
        url = 'https://peertube.example/w/aaaaaaaaaaaaaaaaaaaaaa'
        http_mock.get(url).mock(side_effect=httpx.ConnectError('boom'))
        with app.test_request_context('/'):
            thumbnail, embed = fixup_url(url)
        assert (thumbnail, embed) == (url, url)
```

Note: `import httpx` inside the test body above violates the imports-at-top rule. **Move it to the top of the file** when you write it — it is shown inline here only to keep the snippet self-contained.

- [ ] **Step 5: Run, confirm coverage, both mutation directions**

Run: `./run_tests.sh tests/test_fixup_url.py -q --cov=app.utils --cov-branch --cov-report=term-missing`

Confirm 3108-3165 is fully covered. For the two mutation directions, use the peertube netloc test and the youtube_domains membership test as targets. Report counts. Wide blast radius is expected where an early branch short-circuits later ones — pair each wide mutation with the narrow one on the same rule, per the Global Constraints.

- [ ] **Step 6: Commit**

```bash
git add tests/test_fixup_url.py
git commit -m "test: cover fixup_url's youtube matrix and peertube branch

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: `rewrite_href`

**Files:**
- Test: `tests/test_rewrite_href.py` (create)

**Interfaces:**
- Consumes: `make_instance`, `make_user`, `make_community`, `make_post`, `make_post_reply` from `tests/factories.py`.
- Produces: nothing later tasks depend on.

`rewrite_href` (`app/utils.py:4968-4991`) is a four-branch if/elif over ActivityPub id lookups, with an `else` that re-queries.

**One case per rule, not one per branch.** A branch chain reaches full branch coverage while most rules stay unexercised — the failure mode named in 1b-ii's spec for `continue` chains, and it applies identically here.

- [ ] **Step 1: Enumerate the rules with a command**

```bash
./run_tests.sh --help >/dev/null 2>&1  # ensure the stack is up first
podman-compose -f compose.test.yaml exec -T -w /app test-runner python -c "
import ast
src = open('app/utils.py').read()
for n in ast.walk(ast.parse(src)):
    if isinstance(n, ast.FunctionDef) and n.name == 'rewrite_href':
        for sub in ast.walk(n):
            if isinstance(sub, ast.If):
                print(f'if at line {sub.lineno}, orelse={len(sub.orelse)}')
"
```

Put the output in your report. Do not enumerate by reading — five enumerations in this campaign were wrong when re-derived.

- [ ] **Step 2: Write one class per rule**

Create `tests/test_rewrite_href.py` with one class per rule the enumeration found. Each class needs:

- a test where the rule **matches** and the href is rewritten
- a test where the same URL shape is present but the lookup **misses**, so the original url returns

The four rules and their URL shapes, from the production code:

| rule | URL shape | rewrite target |
|---|---|---|
| post | `/post/` in url, or `/c/`+`/p/`, or `/m/`+`/t/` without `/comment/` | `post.slug`, else `/post/{id}` |
| comment | `/comment/` in url | `/comment/{reply.id}` |
| community | `/c/` without `/p/`, or `/m/` without `/t/` | `/c/{community.link()}` — **remote only** |
| fallthrough `else` | none of the above | `/comment/{reply.id}` if a reply matches, else unchanged |

Two traps to cover explicitly:

- the post rule returns `post.slug` when set and `/post/{id}` when not — **two distinct outcomes, two tests**
- the community rule rewrites **only if `not community.is_local()`**. A local community falls through and returns the url unchanged. Mutation that fails this: deleting the `is_local()` guard.

- [ ] **Step 3: Run and confirm coverage**

Run: `./run_tests.sh tests/test_rewrite_href.py -q --cov=app.utils --cov-branch --cov-report=term-missing`

Confirm 4966-4989 is fully covered.

- [ ] **Step 4: Both mutation directions on two rules**

Pick the community rule and the fallthrough `else`. For each: delete it (only its match test fails), then over-broaden it (its miss test fails). Restore with `git checkout -- app/utils.py` and confirm `git status` is clean. Report all four counts.

- [ ] **Step 5: Commit**

```bash
git add tests/test_rewrite_href.py
git commit -m "test: cover rewrite_href's four ap_id resolution rules

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: `apply_feed_url_rules`

**Files:**
- Test: `tests/test_apply_feed_url_rules.py` (create)

**Interfaces:**
- Consumes: `make_user` from `tests/factories.py`; `Feed` from `app.models`.
- Produces: nothing later tasks depend on.

`apply_feed_url_rules` (`app/utils.py:4486-4516`) is a **form validator bound to `self`**, reading `current_user.user_name`. It needs a form instance, not a plain call. Find the form class it is attached to:

```bash
grep -rn 'apply_feed_url_rules' app/ --include=*.py
```

**It mutates `self.url.data` before validating it.** Every test must assert on the mutation as well as on the return value, or half the function is untested.

- [ ] **Step 1: Write the tests**

Cover each of these, one class per rule:

| rule | input | expected |
|---|---|---|
| `-` rejected | url containing `-` | returns `False`, error appended |
| private, no `/` | `myfeed`, `public=False` | `self.url.data` becomes `myfeed/<username>` |
| public, with `/` | `myfeed/extra`, `public=True` | `self.url.data` becomes `myfeed` |
| neither | `myfeed`, `public=True` | `self.url.data` becomes `myfeed` (stripped, lowered) |
| regex, public | non-alphanumeric | returns `False`, error appended |
| regex, private | non-alphanumeric | returns `False`, error appended |
| uniqueness, no `feed_id` | name already taken | returns `False`, error appended |
| uniqueness, with `feed_id` | same name, different id | returns `False` |
| uniqueness, with `feed_id` | same name, same id | returns `True` |

The `feed_id` branch is selected by `try: self.feed_id / except AttributeError:` — an edit form has the attribute, a create form does not. Build both.

- [ ] **Step 2: Probe the username-regex risk**

The private regex is built by interpolation:

```python
regex = r'^[a-zA-Z0-9_]+(?:/' + current_user.user_name.lower() + ')?$'
```

A username containing regex metacharacters changes the pattern's meaning. Write a test with such a username and record what happens.

**If it produces incorrect validation, that is a defect — report it, do not fix it.** If usernames cannot contain metacharacters, establish that from the registration validator with a command and say so.

- [ ] **Step 3: Run, confirm coverage, both mutation directions**

Run: `./run_tests.sh tests/test_apply_feed_url_rules.py -q --cov=app.utils --cov-branch --cov-report=term-missing`

Confirm 4484-4514 is fully covered. Run both mutation directions on the `-` rejection and on the uniqueness query. Report counts.

- [ ] **Step 4: Commit**

```bash
git add tests/test_apply_feed_url_rules.py
git commit -m "test: cover apply_feed_url_rules' url mutation and validation

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Fuzz the parsers, measure, ratchet, document

**Files:**
- Test: `tests/test_link_parsers_fuzz.py` (create)
- Modify: `coverage_floors.ini`
- Modify: `tests/README.md`
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`

**Interfaces:**
- Consumes: all five functions, now covered.
- Produces: the raised floor.

- [ ] **Step 1: Write the fuzz harness**

`atheris` is in `requirements-test.txt`. Fuzz `domain_from_url`, `remove_tracking_from_link`, and `fixup_url`'s parsing.

The property is **no unhandled exception and no host confusion**: for any input, the recorded domain equals the parsed hostname with at most a leading `www.` removed. That property is checkable without an oracle, which is what makes this worth doing rather than decorative.

Keep the iteration count low enough that the suite stays fast — `pytest.ini` sets a 60s per-test timeout. State the count you chose and why.

- [ ] **Step 2: Run the full suite**

Run: `./run_tests.sh tests/ -q --ignore=tests/test_activitypub_util.py`
Expected: 0 failed.

- [ ] **Step 3: Measure**

```bash
./run_tests.sh tests/ -q --ignore=tests/test_activitypub_util.py --cov=app --cov-report=json
podman-compose -f compose.test.yaml exec -T -w /app test-runner python -c "
import json; d=json.load(open('coverage.json'))
print(d['files']['app/utils.py']['summary']['percent_covered'])
"
```

`percent_covered` is a **blended statement+branch** figure — that is what `tests/check_coverage_floors.py` reads. Say so in your report.

- [ ] **Step 4: Raise the floor, rounded DOWN**

Edit `coverage_floors.ini`, `app/utils.py = <measured, rounded down>`.

**Do not round up to manufacture a rise.** If the measurement does not support a rise, leave it at 67 and say so — 1b-i's Task 9 correctly left its floor unchanged rather than inflate it.

Prove it bites in both directions:

```bash
# set the floor one higher, expect failure naming the module
./run_tests.sh python tests/check_coverage_floors.py ; echo "exit=$?"
# restore, expect "All 3 module floors met."
```

- [ ] **Step 5: Document**

Add a "Sub-project 1c" section to `tests/README.md` and to `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`. Cover:

- the `domain_from_url` defect, what it was, and that the fix changes future attribution only — no backfill
- the bare `except:` clauses at `app/utils.py:3126,3128` as a reported, unfixed defect
- anything the username-regex probe in Task 5 turned up
- that YouTube expectations are observed behaviour, not a specification

**Verify every `file:line` you cite against the current file before committing.** Stale references are this campaign's signature defect in its own artefacts — nine were found in 1a, and a phantom filename in `tests/README.md` propagated into a review briefing in 1b-ii. Say in your report that you verified them and how.

- [ ] **Step 6: Commit**

```bash
git add tests/test_link_parsers_fuzz.py coverage_floors.ini tests/README.md \
        docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md
git commit -m "test: fuzz the link parsers, raise app/utils.py's floor to <N>

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Plan Self-Review

**Spec coverage.** Every spec section maps to a task: the `domain_from_url` defect and its fix-first rationale → Task 1; the five per-function shapes → Tasks 1-5; fuzzing → Task 6; primary sources → Task 3's docstring and Task 6's documentation; the floor → Task 6; federation implications → Task 1's commit message and Task 6's write-up. The two reported-not-fixed items (bare `except:`, username regex) have explicit steps in Tasks 3 and 5.

**Placeholder scan.** No TBD/TODO. Tasks 4 and 5 give rule tables rather than complete test bodies — deliberate, because both depend on an enumeration the implementer must derive first (Task 4 Step 1) or a form class it must locate (Task 5). Both tasks say exactly what to derive and how.

**Type consistency.** `domain_from_url(url, create=True) -> Domain | None` is used consistently in Task 1. `fixup_url` returns a `(thumbnail_url, embed_url)` tuple in every Task 3 assertion. `apply_feed_url_rules(self) -> bool` is treated as a bound method throughout Task 5. Factory names match `tests/factories.py` as listed at the top of each task's Interfaces block.
