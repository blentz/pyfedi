# Microblog Live Feed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Live" sort to `/c/microblogs`. In it, new microblog posts stream in without a reload, the way Mastodon's Live Feeds does.

**Architecture:**
- The Live page is page 1 of the New sort, plus an ES module, `live_feed.js`.
- The module fetches each viewer's own filtered teaser fragment from a new Flask endpoint, `GET /community/<actor>/live/posts?after=<id>`.
- It fetches on two triggers: an SSE wake-up from `fastapi_server.py` (Redis channel `live:microblogs`, published by `Post.new`), or a 15 s poll when SSE is unavailable.
- The per-viewer filter chain moves out of `show_community` into a shared helper, so the page and the fragment cannot disagree.

**Tech Stack:**
- Server: Flask, Jinja2, SQLAlchemy, Flask-Limiter, Redis pub/sub, FastAPI SSE.
- Client: vanilla ES module.
- Tests: pytest + pytest-cov, run in the test container; node 22 `node --test`, run on the host.

**Spec:** `docs/superpowers/specs/2026-10-07-microblog-live-feed-design.md`

## Global Constraints

- Live exists only where `community.name == 'microblogs'` and `community.is_local()`, for a logged-in viewer, with `content_type == 'posts'` and on `page == 1`. Everywhere else `sort=live` falls back to `new`.
- Fragment window: `Post.posted_at > utcnow() - timedelta(hours=1)`. Limit 40. The cursor is `Post.id`.
- Rate limit: `12/minute` per user.
- Timings:
  - SSE: coalesce fetches to at most one per 5 s, plus a 60 s safety poll.
  - Polling: every 15 s.
  - Backoff: doubles, capped at 120 s.
  - SSE falls back to polling after 3 consecutive errors.
- Display:
  - The hold threshold is 100 px from the top.
  - The DOM cap is 200 teasers.
  - The highlight lasts 2 s.
- The SSE payload is the literal string `{}`. It carries no post data.
- Coverage: 100% line and branch coverage of every new and changed line, in Python (`tests/check_changed_line_coverage.py --branches`) and in `app/static/js/live_feed.js` (`node --test` thresholds). No floor in `coverage_floors.ini` may drop.
- The gates live in the repo: Python runs through `./run_tests.sh`, and JS runs through `npm run test:js`, which `run_tests.sh` calls. Never use ad-hoc `/tmp` scripts.
- Commit messages: Conventional Commits, and end every message with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Spec deviation, deliberate.** The spec names the fragment path `/c/<actor>/live/posts`. The community blueprint is mounted at `/community` (`app/__init__.py:285`), so the real path is `/community/<actor>/live/posts`, endpoint `community.live_posts_fragment`.
- **Spec clarification.** Sticky posts are not shown in Live. Mastodon has none, and a stream that grows at the top cannot keep a pinned block above it.

## Review Focus

1. **Login redirect instead of a status code.** `login_required` answers an expired session with a 302 to the login page. `fetch` follows it and receives a 200 of login HTML. The client must treat `response.redirected` as "stop", and `parse` must keep only `.post_teaser` nodes. Tested in Task 8.
2. **A teaser hidden by a keyword filter.** `_post_teaser.html` renders nothing for `content_blocked == '-1'`. The fragment can then return 200 with fewer teasers than posts, or none at all. The cursor must still advance from `X-Live-Cursor`, so the client does not refetch the same posts forever. Tested in Task 4 (the header is the max id even when every teaser is filtered) and in Task 8 (an empty parse still moves the cursor).
3. **`sort=live` with `content_type=comments`, or with `page=2`.** Neither may render a Live page. Both fall back to `new`. Tested in Task 5.
4. **Server errors other than 429 (500, 502 from a restarting app).** The client backs off exactly as for 429, and never gives up for good. Tested in Task 8.
5. **A redeploy of FastAPI while the tab is open.** The EventSource errors and reconnects by itself. Fewer than 3 consecutive errors keep SSE, because a message resets the count. Three or more fall back to polling for the life of the page. Tested in Task 8.

---

## File Structure

| File | Responsibility |
|---|---|
| `tests/check_changed_line_coverage.py` (modify) | gains `--branches`: fail on an untaken branch out of an added line |
| `app/community/live.py` (create) | Live rules: `is_live_community`, `live_available`, `live_posts`, `announce_live_post`, constants |
| `app/community/routes.py` (modify) | `community_post_query` helper (moved filter chain); `show_community` live branch; `live_posts_fragment` route |
| `app/templates/community/_community_nav.html` (modify) | Live button in the five sort rows |
| `app/templates/community/community.html` (modify) | live status, pill, list data attributes, module script, "Older posts" label |
| `app/templates/community/_live_posts.html` (create) | the fragment: teasers only |
| `app/models.py` (modify) | `Post.new` calls `announce_live_post` after its final commit |
| `fastapi_server.py` (modify) | `/live/stream`, `live_clients`, `live_event_stream`, `fan_out_live`, `live:*` subscription |
| `Caddyfile`, `INSTALL.md` (modify) | proxy `/live/stream` to FastAPI |
| `app/static/js/live_feed.js` (create) | client: fetch, insert or hold, SSE and polling, pause |
| `package.json` (create) | `test:js` script, no dependencies |
| `tests/js/live_feed.test.mjs` (create) | node tests of `live_feed.js` |
| `run_tests.sh` (modify) | runs `npm run test:js` when node is present |
| `coverage_floors.ini` (modify) | `app/community/live.py = 100` |
| `tests/test_microblog_live.py` (create) | Python tests: rules, page, fragment, publish hook |
| `tests/test_live_stream_server.py` (create) | FastAPI tests |
| `tests/test_check_changed_line_coverage.py` (modify) | tests of `--branches` |
| `tests/test_caddyfile_routing.py` (modify) | pins the `/live/stream` block |

How to run things:

- Python tests run in the container. One file: `./run_tests.sh tests/test_microblog_live.py -q`.
- With coverage: `./run_tests.sh <tests> --cov=app --cov=fastapi_server --cov-branch --cov-report=json:coverage.json -q`. `coverage.json` lands in the repo root.
- The coverage gates run on the host with `python3` (stdlib only).
- JS tests run on the host with `npm run test:js`.

---

### Task 1: Branch check in the changed-line gate

**Files:**
- Modify: `tests/check_changed_line_coverage.py`
- Test: `tests/test_check_changed_line_coverage.py`

**Interfaces:**
- Produces:
  - CLI `python3 tests/check_changed_line_coverage.py coverage.json <base> [--branches] [paths...]`
  - `uncovered_added_branches(added: dict[str, set[int]], coverage_data: dict) -> dict[str, list[tuple[int, int]]]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_check_changed_line_coverage.py`:

```python
def branch_report(path, executed, missing, missing_branches):
    """coverage.json fragment for one file, with branch data."""
    return {'files': {path: {'executed_lines': executed, 'missing_lines': missing,
                             'missing_branches': missing_branches}}}


class TestUncoveredAddedBranches:

    def test_an_untaken_branch_out_of_an_added_line_is_reported(self):
        added = {'app/a.py': {3, 4}}
        data = branch_report('app/a.py', [3, 4, 5], [], [[3, 5], [9, -1]])

        assert gate.uncovered_added_branches(added, data) == {'app/a.py': [(3, 5)]}

    def test_a_branch_out_of_a_line_that_was_not_added_is_not_this_diffs_problem(self):
        added = {'app/a.py': {3}}
        data = branch_report('app/a.py', [3, 9], [], [[9, -1]])

        assert gate.uncovered_added_branches(added, data) == {}

    def test_files_the_report_lacks_and_non_python_files_are_skipped(self):
        added = {'app/b.py': {1}, 'app/t.html': {1}}
        data = branch_report('app/a.py', [1], [], [[1, 2]])

        assert gate.uncovered_added_branches(added, data) == {}

    def test_an_entry_without_branch_data_counts_nothing_as_missing(self):
        added = {'app/a.py': {1}}

        assert gate.uncovered_added_branches(added, report(app__a_py=([1], []))) == {}


class TestMainBranches:

    def test_an_untaken_branch_fails_and_is_named(self, cov_path, canned_diff, capsys):
        canned_diff(diff_for('app/a.py', (3, 1)))
        path = cov_path(branch_report('app/a.py', [3, 4], [], [[3, -1]]))

        assert gate.main([path, 'base', '--branches']) == 1
        assert 'app/a.py:3: branch to exit was never taken' in capsys.readouterr().err

    def test_a_branch_to_a_line_is_named_by_that_line(self, cov_path, canned_diff, capsys):
        canned_diff(diff_for('app/a.py', (3, 1)))
        path = cov_path(branch_report('app/a.py', [3, 4], [], [[3, 7]]))

        assert gate.main([path, 'base', '--branches']) == 1
        assert 'app/a.py:3: branch to 7 was never taken' in capsys.readouterr().err

    def test_every_branch_taken_passes(self, cov_path, canned_diff):
        canned_diff(diff_for('app/a.py', (3, 1)))
        path = cov_path(branch_report('app/a.py', [3, 4], [], []))

        assert gate.main([path, 'base', '--branches']) == 0

    def test_without_the_flag_branches_are_not_judged(self, cov_path, canned_diff):
        canned_diff(diff_for('app/a.py', (3, 1)))
        path = cov_path(branch_report('app/a.py', [3, 4], [], [[3, -1]]))

        assert gate.main([path, 'base']) == 0

    def test_the_flag_is_not_read_as_a_path(self, cov_path, canned_diff):
        seen = canned_diff('')

        gate.main([cov_path(branch_report('app/a.py', [1], [], [])), 'base', '--branches', 'app/community'])

        assert seen == [('base', ['app/community'])]

    def test_a_report_with_no_branch_data_fails_closed_under_the_flag(self, cov_path, canned_diff, capsys):
        canned_diff(diff_for('app/a.py', (3, 1)))
        path = cov_path(report(app__a_py=([3], [])))

        assert gate.main([path, 'base', '--branches']) == 2
        assert '--cov-branch' in capsys.readouterr().err

    def test_lines_and_branches_both_reported_in_one_run(self, cov_path, canned_diff, capsys):
        canned_diff(diff_for('app/a.py', (3, 2)))
        path = cov_path(branch_report('app/a.py', [3], [4], [[3, 4]]))

        assert gate.main([path, 'base', '--branches']) == 1
        err = capsys.readouterr().err
        assert 'app/a.py:4: added line was not executed' in err
        assert 'app/a.py:3: branch to 4 was never taken' in err
```

- [ ] **Step 2: Run to verify they fail**

Run: `./run_tests.sh tests/test_check_changed_line_coverage.py -q`
Expected: the new tests FAIL with `AttributeError: module ... has no attribute 'uncovered_added_branches'`, and the `main` tests fail because `--branches` is read as a path that does not exist (exit 2).

- [ ] **Step 3: Implement**

In `tests/check_changed_line_coverage.py`:

Docstring usage line becomes:

```
    python tests/check_changed_line_coverage.py coverage.json <base-ref> [--branches] [paths...]
```

Add after the usage paragraph:

```
With --branches it also fails every branch coverage.py measured out of an added
line that no test took (`missing_branches`, written when the run used
--cov-branch or `branch = True`). A report with no branch data at all is an
error under --branches, not a pass.
```

Add after `uncovered_added_lines`:

```python
def uncovered_added_branches(added, coverage_data):
    """{path: sorted [(line, destination)]} for each untaken branch out of an added line.

    A destination below zero is coverage.py's "exit from the function"."""
    files = repo_relative_files(coverage_data['files'])
    uncovered = {}
    for path in sorted(added):
        entry = files.get(path)
        if entry is None or not path.endswith('.py'):
            continue
        hit = sorted((source, destination) for source, destination in entry.get('missing_branches', [])
                     if source in added[path])
        if hit:
            uncovered[path] = hit
    return uncovered


def has_branch_data(coverage_data):
    return any('missing_branches' in entry for entry in coverage_data['files'].values())
```

In `main`, replace the first lines through `paths = paths or DEFAULT_PATHS` with:

```python
    branches = '--branches' in argv
    argv = [argument for argument in argv if argument != '--branches']
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2

    report_path, base_ref, *paths = argv
    paths = paths or DEFAULT_PATHS
```

After the `files = ...` "lists no files" check, add:

```python
    if branches and not has_branch_data(coverage_data):
        print(f'ERROR: {report_path} has no branch data; run the tests with --cov-branch.', file=sys.stderr)
        return 2
```

Replace the tail of `main`, from `for path, lines in uncovered.items():` to the end of the function, with:

```python
    for path, lines in uncovered.items():
        for number in lines:
            print(f'{path}:{number}: added line was not executed by any test', file=sys.stderr)

    missed = uncovered_added_branches(added, coverage_data) if branches else {}
    for path, pairs in missed.items():
        for source, destination in pairs:
            target = 'exit' if destination < 0 else destination
            print(f'{path}:{source}: branch to {target} was never taken', file=sys.stderr)

    if uncovered or missed:
        total = sum(len(lines) for lines in uncovered.values())
        branch_total = sum(len(pairs) for pairs in missed.values())
        print(f'{total} added line(s) not executed, {branch_total} branch(es) out of added lines not taken.',
              file=sys.stderr)
        return 1
    print(f'Every added line since {base_ref} that coverage measured was executed'
          f'{" and every branch out of one was taken" if branches else ""}.')
    return 0
```

- [ ] **Step 4: Run to verify they pass**

Run: `./run_tests.sh tests/test_check_changed_line_coverage.py -q`
Expected: all PASS. The existing `TestMain` tests still pass: `'Every added line'` is still in the success message.

- [ ] **Step 5: Commit**

```bash
git add tests/check_changed_line_coverage.py tests/test_check_changed_line_coverage.py
git commit -m "test: the changed-line gate can also fail an untaken branch out of an added line

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Extract `community_post_query` (pure move)

**Files:**
- Modify: `app/community/routes.py:412-482` (the posts branch of `show_community`)
- Test: `tests/test_microblog_live.py` (create)

**Interfaces:**
- Produces: `community_post_query(community: Community, content_type: str, flair: str = '', tag: str = '') -> tuple[Query, dict]` in `app/community/routes.py`. It returns the unsorted, unpaginated, sticky-inclusive post query for `current_user`, plus `content_filters`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_microblog_live.py`:

```python
"""The Live view of /c/microblogs: new posts arrive without a reload.

Spec: docs/superpowers/specs/2026-10-07-microblog-live-feed-design.md

Fixture notes, from tests/test_community_show.py: `Site.private_instance`
defaults to True, so every test here makes the instance public; and a local
community is reached at /c/<name>.
"""
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app import db
from app.constants import POST_STATUS_REVIEWING
from app.models import Instance, Language, Post, Site
from app.utils import utcnow
from tests.factories import (make_community, make_instance, make_instance_block,
                             make_post, make_user, make_user_block)

pytestmark = pytest.mark.usefixtures('site')


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def local_instance():
    existing = Instance.query.filter_by(domain='test.piefed.local').first()
    return existing if existing is not None else make_instance('test.piefed.local', software='piefed')


@pytest.fixture
def live(app, db_session):
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    local = local_instance()
    make_user(local, 'founder', local=True)
    viewer = make_user(local, 'viewer', local=True)
    remote = make_instance('mastodon.example')
    author = make_user(remote, 'tooter')
    microblogs = make_community('microblogs')
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.commit()
    counter = iter(range(1, 10_000))

    def toot(**columns):
        post = make_post(microblogs, author, f'https://mastodon.example/statuses/{next(counter)}', microblog=True)
        for name, value in columns.items():
            setattr(post, name, value)
        db.session.commit()
        return post

    return SimpleNamespace(viewer=viewer, author=author, remote=remote, microblogs=microblogs, toot=toot)


def teaser_ids(html):
    return [int(found) for found in re.findall(r'id="post_(\d+)"', html)]


class TestCommunityPostQuery:
    """The filter chain show_community applied inline, now shared with the Live fragment."""

    def test_a_logged_in_viewer_does_not_see_a_blocked_authors_post(self, app, live):
        from flask_login import login_user

        from app.community.routes import community_post_query

        kept = live.toot()
        other = make_user(live.remote, 'pest')
        blocked = make_post(live.microblogs, other, 'https://mastodon.example/statuses/pest', microblog=True)
        make_user_block(live.viewer, other)
        with app.test_request_context():
            login_user(live.viewer)
            posts, content_filters = community_post_query(live.microblogs, 'posts')
            ids = {post.id for post in posts}

        assert kept.id in ids and blocked.id not in ids
        assert isinstance(content_filters, dict)

    def test_an_anonymous_viewer_gets_no_content_filters(self, app, live):
        from app.community.routes import community_post_query

        live.toot()
        with app.test_request_context():
            posts, content_filters = community_post_query(live.microblogs, 'posts')
            assert posts.count() == 1

        assert content_filters == {}
```

- [ ] **Step 2: Run to verify they fail**

Run: `./run_tests.sh tests/test_microblog_live.py -q`
Expected: FAIL with `ImportError: cannot import name 'community_post_query'`.

- [ ] **Step 3: Move the code**

In `app/community/routes.py`, add this function directly above `def show_community(community: Community):` (line 296):

```python
def community_post_query(community: Community, content_type: str, flair: str = '', tag: str = ''):
    """The posts `community` shows the current viewer: listable, and filtered by that viewer's
    settings and blocks. Unsorted and unpaginated, sticky posts included; callers order and page it.

    Shared by the community page and its Live fragment, so the two can never disagree on what a
    viewer may see. Returns (query, content_filters).
    """
```

Then **cut** the body of the posts branch of `show_community`, from the comment block that begins `# No Post.private filter: private is the microblog marker` (line 408) through the end of the `# Filter by post tag` block (the line `posts = posts.join(post_tag).filter(post_tag.c.tag_id == tag_record.id)`). Paste it, de-indented by one level, as the body of `community_post_query`, and end the function with:

```python
    return posts, content_filters
```

Inside the moved code, delete the two assignments to `user` (`user = None` and `user = current_user`). `user` is not part of the helper's contract.

In `show_community`, the posts branch now begins:

```python
    if content_type == 'posts' or content_type == 'events':
        posts, content_filters = community_post_query(community, content_type, flair, tag)
        user = None if current_user.is_anonymous else current_user

        sticky_posts = posts.filter(Post.sticky == True)
        posts = posts.filter(Post.sticky == False)
```

The rest (sort branches, `per_page`, paginate) is unchanged.

- [ ] **Step 4: Run the new tests and the existing community page tests**

Run: `./run_tests.sh tests/test_microblog_live.py tests/test_community_show.py tests/test_private_community_page.py tests/test_tag_page.py -q`
Expected: all PASS. The existing tests do not change. That is the proof the move preserved behavior.

- [ ] **Step 5: Commit**

```bash
git add app/community/routes.py tests/test_microblog_live.py
git commit -m "refactor: the community page's per-viewer post filters live in one function

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Live rules module

**Files:**
- Create: `app/community/live.py`
- Modify: `coverage_floors.ini`
- Test: `tests/test_microblog_live.py`

**Interfaces:**
- Consumes: `community_post_query` (Task 2), indirectly, through callers.
- Produces (all in `app.community.live`):
  - `LIVE_COMMUNITY = 'microblogs'`, `LIVE_CHANNEL = 'live:microblogs'`, `LIVE_WINDOW = timedelta(hours=1)`, `LIVE_LIMIT = 40`
  - `is_live_community(community) -> bool`
  - `live_available(community, user, content_type: str, page: int) -> bool`
  - `live_posts(posts_query, after: int) -> list[Post]`
  - `announce_live_post(post, community, backfill: bool) -> None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_microblog_live.py`:

```python
class TestLiveRules:

    def test_the_local_microblogs_community_is_live(self, app, live):
        from app.community.live import is_live_community

        with app.test_request_context():
            assert is_live_community(live.microblogs)

    def test_another_local_community_is_not(self, app, live):
        from app.community.live import is_live_community

        with app.test_request_context():
            assert not is_live_community(make_community('general'))

    def test_a_remote_community_named_microblogs_is_not(self, app, live):
        from app.community.live import is_live_community

        remote = make_community('microblogs', host='other.example')
        remote.ap_id = 'microblogs@other.example'
        remote.instance_id = live.remote.id
        db.session.commit()
        with app.test_request_context():
            assert not is_live_community(remote)

    @pytest.mark.parametrize('logged_in, content_type, page, expected', [
        (True, 'posts', 1, True),
        (False, 'posts', 1, False),
        (True, 'comments', 1, False),
        (True, 'posts', 2, False),
    ])
    def test_live_is_available_only_to_a_logged_in_viewer_of_page_one_of_posts(
            self, app, live, logged_in, content_type, page, expected):
        from flask_login import AnonymousUserMixin

        from app.community.live import live_available

        user = live.viewer if logged_in else AnonymousUserMixin()
        with app.test_request_context():
            assert live_available(live.microblogs, user, content_type, page) is expected

    def test_live_posts_are_newer_than_the_cursor_recent_unpinned_and_newest_first(self, app, live):
        from app.community.live import live_posts

        seen = live.toot()
        older = live.toot(posted_at=utcnow() - timedelta(minutes=10))
        newer = live.toot()
        live.toot(posted_at=utcnow() - timedelta(hours=2))      # a backfill: new id, old post
        live.toot(sticky=True)

        result = live_posts(Post.query.filter(Post.community_id == live.microblogs.id), seen.id)

        assert [post.id for post in result] == [newer.id, older.id]

    def test_live_posts_stop_at_the_limit(self, app, live):
        from app.community import live as live_module

        for _ in range(3):
            live.toot()
        query = Post.query.filter(Post.community_id == live.microblogs.id)
        original = live_module.LIVE_LIMIT
        live_module.LIVE_LIMIT = 2
        try:
            assert len(live_module.live_posts(query, 0)) == 2
        finally:
            live_module.LIVE_LIMIT = original

    def test_the_limit_is_forty(self):
        from app.community.live import LIVE_LIMIT

        assert LIVE_LIMIT == 40


class TestAnnounceLivePost:
    """The SSE wake-up. The payload is empty on purpose: each client fetches its own filtered posts."""

    @pytest.fixture
    def published(self, app, monkeypatch):
        calls = []
        monkeypatch.setattr('app.utils.publish_sse_event', lambda key, value: calls.append((key, value)))
        monkeypatch.setitem(app.config, 'NOTIF_SERVER', 'https://notifs.example')
        return calls

    def test_a_new_microblogs_post_wakes_the_live_feed(self, app, live, published):
        from app.community.live import announce_live_post

        with app.test_request_context():
            announce_live_post(live.toot(), live.microblogs, backfill=False)

        assert published == [('live:microblogs', '{}')]

    def test_a_backfilled_post_does_not(self, app, live, published):
        from app.community.live import announce_live_post

        with app.test_request_context():
            announce_live_post(live.toot(), live.microblogs, backfill=True)

        assert published == []

    def test_a_post_in_another_community_does_not(self, app, live, published):
        from app.community.live import announce_live_post

        general = make_community('general')
        post = make_post(general, live.author, 'https://mastodon.example/statuses/g')
        with app.test_request_context():
            announce_live_post(post, general, backfill=False)

        assert published == []

    @pytest.mark.parametrize('columns', [
        {'status': POST_STATUS_REVIEWING},
        {'deleted': True},
        {'visibility': 'unlisted'},
    ])
    def test_a_post_the_feed_would_not_list_does_not(self, app, live, published, columns):
        from app.community.live import announce_live_post

        with app.test_request_context():
            announce_live_post(live.toot(**columns), live.microblogs, backfill=False)

        assert published == []

    def test_without_a_notification_server_nothing_is_published(self, app, live, published):
        from app.community.live import announce_live_post

        app.config['NOTIF_SERVER'] = ''
        with app.test_request_context():
            announce_live_post(live.toot(), live.microblogs, backfill=False)

        assert published == []

    def test_a_redis_failure_is_logged_not_raised(self, app, live, monkeypatch, caplog):
        from app.community.live import announce_live_post

        def broken(key, value):
            raise ConnectionError('redis is down')
        monkeypatch.setattr('app.utils.publish_sse_event', broken)
        monkeypatch.setitem(app.config, 'NOTIF_SERVER', 'https://notifs.example')
        with app.test_request_context():
            announce_live_post(live.toot(), live.microblogs, backfill=False)

        assert 'redis is down' in caplog.text

    def test_the_signal_reaches_the_redis_channel(self, app, live, redis_double, monkeypatch):
        from app.community.live import announce_live_post

        monkeypatch.setitem(app.config, 'NOTIF_SERVER', 'https://notifs.example')
        subscriber = redis_double.pubsub()
        subscriber.subscribe('live:microblogs')
        subscriber.get_message(timeout=1)       # the subscribe confirmation
        with app.test_request_context():
            announce_live_post(live.toot(), live.microblogs, backfill=False)

        message = subscriber.get_message(timeout=1)
        assert message['channel'] == 'live:microblogs' and message['data'] == '{}'
```

- [ ] **Step 2: Run to verify they fail**

Run: `./run_tests.sh tests/test_microblog_live.py -q`
Expected: the new tests FAIL with `ModuleNotFoundError: No module named 'app.community.live'`.

- [ ] **Step 3: Implement**

Create `app/community/live.py`:

```python
"""The Live view of the microblogs community: new posts arrive without a reload, the way
Mastodon's Live Feeds does.

Spec: docs/superpowers/specs/2026-10-07-microblog-live-feed-design.md
"""
from datetime import timedelta

from flask import current_app
from sqlalchemy import desc

from app.constants import POST_STATUS_REVIEWING, VISIBILITY_PUBLIC
from app.models import Post
from app.utils import utcnow

LIVE_COMMUNITY = 'microblogs'
LIVE_CHANNEL = 'live:microblogs'
# A backfilled post is old but gets a new, high id; this window keeps it out of the stream
# while still admitting a post that federated in a few minutes late.
LIVE_WINDOW = timedelta(hours=1)
LIVE_LIMIT = 40


def is_live_community(community) -> bool:
    return community.name == LIVE_COMMUNITY and community.is_local()


def live_available(community, user, content_type: str, page: int) -> bool:
    return user.is_authenticated and content_type == 'posts' and page == 1 and is_live_community(community)


def live_posts(posts_query, after: int) -> list:
    """Posts from `posts_query` newer than the cursor `after` (a Post.id: local ids only grow,
    unlike a peer's `published`), recent, unpinned, newest first."""
    return posts_query.filter(Post.id > after, Post.posted_at > utcnow() - LIVE_WINDOW,
                              Post.sticky == False).order_by(desc(Post.posted_at)).limit(LIVE_LIMIT).all()


def announce_live_post(post, community, backfill: bool) -> None:
    """Wake every open Live view after a post lands. The payload is empty: each viewer then
    fetches its own filtered posts, so the broadcast reveals nothing a filter would hide."""
    if not current_app.config['NOTIF_SERVER'] or backfill or not is_live_community(community):
        return
    if post.visibility != VISIBILITY_PUBLIC or post.status <= POST_STATUS_REVIEWING or post.deleted:
        return
    from app import utils      # looked up at call time, so a test's replacement is the one called
    try:
        utils.publish_sse_event(LIVE_CHANNEL, '{}')
    except Exception as exc:
        current_app.logger.warning(f'Live feed wake-up not sent: {exc}')
```

Note: `app.models` does not import `app.community.live` at module level. Task 6 imports it inside `Post.new`, which is why there is no cycle.

In `coverage_floors.ini`, add at the end of `[floors]`:

```ini
# Microblog Live feed (2026-10-07): a new module, born fully covered.
app/community/live.py = 100
```

- [ ] **Step 4: Run to verify they pass**

Run: `./run_tests.sh tests/test_microblog_live.py -q`
Expected: all PASS. If `caplog` misses the warning, check that the app logger propagates in tests. `tests/test_community_show.py` uses `caplog` the same way. If it does not propagate, assert through `monkeypatch.setattr(app.logger, 'warning', recorder)` instead.

- [ ] **Step 5: Commit**

```bash
git add app/community/live.py coverage_floors.ini tests/test_microblog_live.py
git commit -m "feat: rules for the microblogs Live view and its wake-up signal

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: The Live fragment endpoint

**Files:**
- Modify: `app/community/routes.py` (new route after `show_community_ical`, ~line 884)
- Create: `app/templates/community/_live_posts.html`
- Test: `tests/test_microblog_live.py`

**Interfaces:**
- Consumes: `community_post_query` (Task 2); `is_live_community` and `live_posts` (Task 3); `actor_to_community` (`app/community/util.py:498`).
- Produces: `GET /community/<actor>/live/posts?after=<int>`, endpoint `community.live_posts_fragment`.
  - 200: teaser HTML, plus header `X-Live-Cursor: <max id returned>`.
  - 204: empty.
  - 400: a bad `after`.
  - 404: not the live community.
  - 302 to login when anonymous.
  - 429 over `12/minute` per user.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_microblog_live.py`:

```python
FRAGMENT = '/community/microblogs/live/posts'


class TestLiveFragment:

    def test_new_posts_after_the_cursor_come_back_newest_first_with_the_new_cursor(self, client, live):
        seen = live.toot()
        older = live.toot(posted_at=utcnow() - timedelta(minutes=5))
        newer = live.toot()
        login(client, live.viewer)

        response = client.get(f'{FRAGMENT}?after={seen.id}')

        assert response.status_code == 200
        assert teaser_ids(response.get_data(as_text=True)) == [newer.id, older.id]
        assert response.headers['X-Live-Cursor'] == str(max(newer.id, older.id))

    def test_nothing_new_is_204(self, client, live):
        latest = live.toot()
        login(client, live.viewer)

        response = client.get(f'{FRAGMENT}?after={latest.id}')

        assert response.status_code == 204 and response.get_data() == b''

    def test_a_backfilled_post_is_not_new(self, client, live):
        live.toot(posted_at=utcnow() - timedelta(hours=2))
        login(client, live.viewer)

        assert client.get(f'{FRAGMENT}?after=0').status_code == 204

    def test_at_most_forty_posts(self, client, live):
        for _ in range(41):
            live.toot()
        login(client, live.viewer)

        assert len(teaser_ids(client.get(f'{FRAGMENT}?after=0').get_data(as_text=True))) == 40

    def test_the_viewers_blocks_and_settings_apply(self, client, live):
        kept = live.toot()
        pest = make_user(live.remote, 'pest')
        make_post(live.microblogs, pest, 'https://mastodon.example/statuses/pest', microblog=True)
        make_user_block(live.viewer, pest)
        live.toot(nsfw=True)
        live.toot(status=POST_STATUS_REVIEWING)
        elsewhere = make_instance('blocked.example')
        stranger = make_user(elsewhere, 'stranger')
        make_post(live.microblogs, stranger, 'https://blocked.example/statuses/1', microblog=True)
        make_instance_block(live.viewer, elsewhere)
        live.viewer.hide_nsfw = 1
        db.session.commit()
        login(client, live.viewer)

        assert teaser_ids(client.get(f'{FRAGMENT}?after=0').get_data(as_text=True)) == [kept.id]

    def test_the_cursor_advances_even_when_a_keyword_filter_hides_every_teaser(self, client, live, monkeypatch):
        hidden = live.toot()
        monkeypatch.setattr(Post, 'blocked_by_content_filter', lambda self, filters, user_id: '-1')
        login(client, live.viewer)

        response = client.get(f'{FRAGMENT}?after=0')

        assert response.status_code == 200
        assert teaser_ids(response.get_data(as_text=True)) == []
        assert response.headers['X-Live-Cursor'] == str(hidden.id)

    def test_an_anonymous_visitor_is_sent_to_log_in(self, client, live):
        response = client.get(f'{FRAGMENT}?after=0')

        assert response.status_code == 302 and '/auth/login' in response.headers['Location']

    def test_another_community_has_no_live_fragment(self, client, live):
        make_community('general')
        login(client, live.viewer)

        assert client.get('/community/general/live/posts?after=0').status_code == 404

    def test_an_unknown_community_has_none_either(self, client, live):
        login(client, live.viewer)

        assert client.get('/community/nosuch/live/posts?after=0').status_code == 404

    @pytest.mark.parametrize('query', ['', '?after=', '?after=abc'])
    def test_a_missing_or_unreadable_cursor_is_400(self, client, live, query):
        login(client, live.viewer)

        assert client.get(f'{FRAGMENT}{query}').status_code == 400

    def test_the_thirteenth_request_in_a_minute_is_refused(self, client, live):
        from app import limiter

        login(client, live.viewer)
        limiter.enabled = True
        limiter.reset()
        try:
            codes = [client.get(f'{FRAGMENT}?after=0').status_code for _ in range(13)]
        finally:
            limiter.reset()
            limiter.enabled = False

        assert codes[:12] == [204] * 12 and codes[12] == 429
```

- [ ] **Step 2: Run to verify they fail**

Run: `./run_tests.sh tests/test_microblog_live.py::TestLiveFragment -q`
Expected: FAIL with 404s, because the route does not exist.

- [ ] **Step 3: Implement**

Create `app/templates/community/_live_posts.html`:

```html
{# The Live view's new teasers, rendered exactly as the community page renders them. #}
{% for post in posts -%}
    {% include 'post/_post_teaser.html' -%}
{% endfor -%}
```

In `app/community/routes.py`, add to the imports:

```python
from app.community.live import is_live_community, live_posts
```

Make sure `actor_to_community` and `UserFlair` are imported. Both are already used in this file: `grep -n "actor_to_community\|UserFlair" app/community/routes.py`. Then add after the `show_community_ical` function:

```python
@bp.route('/<actor>/live/posts', methods=['GET'])
@login_required
@limiter.limit('12/minute', key_func=lambda: f'live_posts:{current_user.id}')
def live_posts_fragment(actor):
    """New teasers for the Live view, filtered for the current viewer. `after` is the newest
    Post.id the page already has. 204 when nothing is new. The client advances its cursor from
    X-Live-Cursor, not from the teasers, because a keyword filter can hide every one of them."""
    community = actor_to_community(actor)
    if community is None or not is_live_community(community):
        abort(404)
    after = request.args.get('after', type=int)
    if after is None:
        abort(400)

    posts_query, content_filters = community_post_query(community, 'posts')
    posts = live_posts(posts_query, after)
    if not posts:
        return '', 204

    user_flair = {flair.user_id: flair.flair
                  for flair in UserFlair.query.filter(UserFlair.community_id == community.id)}
    response = make_response(render_template(
        'community/_live_posts.html', posts=posts, community=community, sort='new',
        content_filters=content_filters, show_post_community=False,
        low_bandwidth=request.cookies.get('low_bandwidth', '0') == '1',
        reported_posts=reported_posts(current_user.get_id(), current_user.get_id() in g.admin_ids),
        user_notes=user_notes(current_user.get_id()), user_flair=user_flair,
        recently_upvoted=recently_upvoted_posts(current_user.id),
        recently_downvoted=recently_downvoted_posts(current_user.id),
        can_upvote_here=can_upvote(current_user, community),
        can_downvote_here=can_downvote(current_user, community),
        user_pronouns=user_pronouns(), moderated_community_ids=moderating_communities_ids(current_user.get_id()),
        community_flair=shared_community.get_comm_flair_list(community)))
    response.headers['X-Live-Cursor'] = str(max(post.id for post in posts))
    return response
```

`login_required` is the one already imported from `app.utils` (line 62). It redirects anonymous users to `/auth/login`.

- [ ] **Step 4: Run to verify they pass**

Run: `./run_tests.sh tests/test_microblog_live.py -q`
Expected: all PASS. If the rate-limit test gets 200 or 204 for all 13, check that `limiter.enabled` is honored at request time and that the `key_func` is used. A failure there is a real bug: fix the decorator, never the test.

- [ ] **Step 5: Commit**

```bash
git add app/community/routes.py app/templates/community/_live_posts.html tests/test_microblog_live.py
git commit -m "feat: the Live view fetches new teasers filtered for its viewer

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: The Live sort on the community page

**Files:**
- Modify: `app/community/routes.py` (`show_community`: sort parse ~line 335, layout ~line 345, sort branches ~line 509, sticky fetch ~line 528, `next_url` ~line 693, `render_template` ~line 731)
- Modify: `app/templates/community/_community_nav.html` (after each of the five `{{ _('Active') }}` links: lines 27-30, 112-114, 258-260, 343-345, 467-469)
- Modify: `app/templates/community/community.html:146-198`
- Test: `tests/test_microblog_live.py`

**Interfaces:**
- Consumes: `live_available(community, user, content_type, page)` (Task 3); route `community.live_posts_fragment` (Task 4).
- Produces:
  - Template variables `live` (bool) and `live_button` (bool).
  - DOM contract for Task 8:
    - `#live_feed` (the `.post_list`) with `data-posts-url`, `data-cursor`, `data-sse-url`, `data-str-live`, `data-str-paused` and `data-str-new-posts` (containing `%d`)
    - `#live_status`
    - `#live_pill` (starts `hidden`)
  - The fragment URL comes from `url_for('community.live_posts_fragment', actor=...)`, defined in Task 4.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_microblog_live.py`:

```python
class TestLivePage:

    def test_the_live_button_shows_for_a_logged_in_viewer_of_microblogs(self, client, live):
        login(client, live.viewer)

        assert '?sort=live' in client.get('/c/microblogs').get_data(as_text=True)

    def test_no_live_button_for_an_anonymous_visitor(self, client, live):
        assert '?sort=live' not in client.get('/c/microblogs').get_data(as_text=True)

    def test_no_live_button_on_another_community(self, client, live):
        make_community('general')
        login(client, live.viewer)

        assert '?sort=live' not in client.get('/c/general').get_data(as_text=True)

    def test_the_live_page_carries_the_client_contract(self, client, live):
        posts = [live.toot(), live.toot()]
        login(client, live.viewer)

        html = client.get('/c/microblogs?sort=live').get_data(as_text=True)

        assert 'id="live_feed"' in html
        assert f'data-cursor="{max(post.id for post in posts)}"' in html
        assert 'data-posts-url="/community/microblogs/live/posts"' in html
        assert 'data-sse-url=""' in html
        assert 'id="live_status"' in html and 'id="live_pill"' in html
        assert 'js/live_feed.js' in html
        assert set(teaser_ids(html)) == {post.id for post in posts}

    def test_an_empty_community_starts_the_cursor_at_zero(self, client, live):
        login(client, live.viewer)

        assert 'data-cursor="0"' in client.get('/c/microblogs?sort=live').get_data(as_text=True)

    def test_with_a_notification_server_the_page_names_the_live_stream(self, app, client, live, monkeypatch):
        monkeypatch.setitem(app.config, 'NOTIF_SERVER', 'https://notifs.example')
        login(client, live.viewer)

        html = client.get('/c/microblogs?sort=live').get_data(as_text=True)

        assert 'data-sse-url="https://notifs.example/live/stream?feed=microblogs"' in html

    @pytest.mark.parametrize('path, as_viewer', [
        ('/c/microblogs?sort=live', False),
        ('/c/general?sort=live', True),
        ('/c/microblogs?sort=live&content_type=comments', True),
        ('/c/microblogs?sort=live&page=2', True),
    ])
    def test_sort_live_falls_back_to_new_where_live_is_not_available(self, client, live, path, as_viewer):
        make_community('general')
        if as_viewer:
            login(client, live.viewer)

        response = client.get(path)

        assert response.status_code == 200
        assert 'id="live_feed"' not in response.get_data(as_text=True)

    def test_live_leaves_out_sticky_posts(self, client, live):
        pinned = live.toot(sticky=True)
        login(client, live.viewer)

        assert pinned.id not in teaser_ids(client.get('/c/microblogs?sort=live').get_data(as_text=True))

    def test_live_forces_the_list_layout(self, client, live):
        live.microblogs.default_layout = 'masonry'
        db.session.commit()
        live.toot()
        login(client, live.viewer)

        html = client.get('/c/microblogs?sort=live').get_data(as_text=True)

        assert 'id="masonry"' not in html and 'id="live_feed"' in html

    def test_live_works_in_low_bandwidth_mode(self, client, live):
        live.toot()
        login(client, live.viewer)
        client.set_cookie('low_bandwidth', '1')

        assert 'id="live_feed"' in client.get('/c/microblogs?sort=live').get_data(as_text=True)

    def test_older_posts_continue_in_the_new_sort(self, client, live):
        for _ in range(3):
            live.toot()
        live.viewer.page_length = 2
        db.session.commit()
        login(client, live.viewer)

        html = client.get('/c/microblogs?sort=live').get_data(as_text=True)

        assert 'Older posts' in html
        assert re.search(r'href="[^"]*page=2[^"]*sort=new|href="[^"]*sort=new[^"]*page=2', html)
```

- [ ] **Step 2: Run to verify they fail**

Run: `./run_tests.sh tests/test_microblog_live.py::TestLivePage -q`
Expected: FAIL. There is no `?sort=live` and no `id="live_feed"`, and `sort=live` matches no sort branch.

- [ ] **Step 3: Implement the route changes**

In `app/community/routes.py`, extend the live import from Task 4 to:

```python
from app.community.live import is_live_community, live_available, live_posts
```

In `show_community`, directly after `low_bandwidth = request.cookies.get('low_bandwidth', '0') == '1'`:

```python
    live = sort == 'live' and live_available(community, current_user, content_type, page)
    if sort == 'live' and not live:
        sort = 'new'
```

Directly after the `post_layout` if/else block (and before the ETag line):

```python
    if live and post_layout is not None:
        post_layout = 'list'        # Live inserts teasers into a list; masonry cannot take them
```

In the posts sort chain, change `elif sort == 'new':` to:

```python
        elif sort == 'new' or sort == 'live':
```

Change `sticky_posts = sticky_posts.all()` (after `posts.paginate(...)`) to:

```python
        sticky_posts = [] if live else sticky_posts.all()
```

In the posts `next_url = url_for(...)`, change `sort=sort` to `sort='new' if live else sort`.

In the `render_template('community/community.html', ...)` call, add:

```python
                                         live=live,
                                         live_button=live_available(community, current_user, content_type, 1),
                                         live_cursor=max((post.id for post in posts.items), default=0) if live else 0,
```

(`posts` is a Pagination only in the posts branch. `live` is True only there, because `live_available` requires `content_type == 'posts'`.)

- [ ] **Step 4: Implement the nav buttons**

In `app/templates/community/_community_nav.html`, after the closing `</li>` of the Active dropdown item at lines 27-30 and again at lines 258-260, insert:

```html
                        {% if live_button -%}
                        <li>
                            <a href="?sort=live" aria-label="{{ _('Live feed of new posts') }}" class="dropdown-item {{ 'active' if sort == 'live' }}" rel="nofollow noindex">
                                {{ _('Live') }}
                            </a>
                        </li>
                        {% endif -%}
```

After the closing `</a>` of the Active button at lines 112-114, 343-345 and 467-469, insert:

```html
                    {% if live_button -%}
                    <a href="?sort=live" aria-label="{{ _('Live feed of new posts') }}" class="btn {{ 'btn-primary' if sort == 'live' else 'btn-outline-secondary' }}" rel="nofollow noindex">
                        {{ _('Live') }}
                    </a>
                    {% endif -%}
```

Match the indentation of the surrounding block in each place.

- [ ] **Step 5: Implement the page markup**

In `app/templates/community/community.html`, replace `<div class="post_list">` (line 153) with:

```html
                        {% if live -%}
                            <div class="position-sticky top-0 z-3 d-flex justify-content-center align-items-center gap-2 py-1">
                                <span id="live_status" class="badge text-bg-success" aria-live="polite">{{ _('Live') }}</span>
                                <button type="button" id="live_pill" class="btn btn-primary btn-sm" hidden></button>
                            </div>
                        {% endif -%}
                        <div class="post_list"{% if live %} id="live_feed"
                             data-posts-url="{{ url_for('community.live_posts_fragment', actor=community.link()) }}"
                             data-cursor="{{ live_cursor }}"
                             data-sse-url="{{ notif_server ~ '/live/stream?feed=microblogs' if notif_server else '' }}"
                             data-str-live="{{ _('Live') }}" data-str-paused="{{ _('Paused') }}"
                             data-str-new-posts="{{ _('%(num)s new posts', num='%d') }}"{% endif %}>
```

The template's empty-state branch (`{% if posts or sticky_posts %}`) is left alone. A Pagination object is truthy, so an empty Live list still renders `#live_feed`. The `data-cursor="0"` test above pins this.

Directly before `{% endif -%}` closes the `{% if community.private and ... %}...{% else %}` block (just before `</main>`, line 198), add:

```html
            {% if live -%}
                <script type="module" src="{{ url_for('static', filename='js/live_feed.js', changed=getmtime('js/live_feed.js')) }}" nonce="{{ nonce }}"></script>
            {% endif -%}
```

In the pagination `<nav>`, change the next-page label line to:

```html
                        {{ _('Older posts') if live else _('Next page') }} <span aria-hidden="true">&rarr;</span>
```

Create an empty placeholder `app/static/js/live_feed.js` so `getmtime` does not fail. Task 8 fills it:

```js
// Live view of /c/microblogs. Filled in by Task 8 of docs/superpowers/plans/2026-10-07-microblog-live-feed.md.
```

- [ ] **Step 6: Run to verify they pass**

Run: `./run_tests.sh tests/test_microblog_live.py tests/test_community_show.py -q`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add app/community/routes.py app/templates/community/_community_nav.html app/templates/community/community.html app/static/js/live_feed.js tests/test_microblog_live.py
git commit -m "feat: a Live sort on the microblogs community page

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: `Post.new` wakes the Live feed

**Files:**
- Modify: `app/models.py:3250-3251` (after `post.generate_slug(community)` / `db.session.commit()` in `Post.new`)
- Test: `tests/test_microblog_live.py`

**Interfaces:**
- Consumes: `announce_live_post(post, community, backfill)` (Task 3).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_microblog_live.py`:

```python
class TestPostNewAnnounces:
    """Post.new is where an inbound Create becomes a Post; the wake-up goes out after its commit."""

    PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'
    AUTHOR = 'https://remote.test/u/tooter'

    @pytest.fixture
    def env(self, app, api_baseline, monkeypatch):
        from flask import g

        from tests.factories import make_community_member

        g.admin_ids = []
        g.site = db.session.get(Site, 1)
        community = make_community('livenew')
        author = make_user(api_baseline.instance_remote, 'tooter')
        author.ap_id = 'tooter@remote.test'
        author.ap_profile_id = self.AUTHOR
        author.ap_public_url = self.AUTHOR
        db.session.commit()
        make_community_member(author, community)
        calls = []
        monkeypatch.setattr('app.community.live.announce_live_post',
                            lambda post, community, backfill: calls.append((post.id, community.id, backfill)))
        return SimpleNamespace(community=community, author=author, calls=calls)

    def create(self, env, number, backfill=False):
        document = {'id': f'https://remote.test/p/{number}', 'type': 'Page', 'name': 'a post',
                    'attributedTo': self.AUTHOR, 'to': [self.PUBLIC],
                    'published': '2026-01-01T00:00:00Z', 'content': '<p>body</p>'}
        return Post.new(env.author, env.community,
                        {'id': f'https://remote.test/c/{number}', 'type': 'Create', 'to': [self.PUBLIC],
                         'object': document}, backfill=backfill)

    def test_a_new_post_is_announced_once_after_it_is_stored(self, app, env):
        post = self.create(env, 1)

        assert env.calls == [(post.id, env.community.id, False)]

    def test_a_backfilled_post_is_passed_as_one(self, app, env):
        post = self.create(env, 2, backfill=True)

        assert env.calls == [(post.id, env.community.id, True)]
```

- [ ] **Step 2: Run to verify they fail**

Run: `./run_tests.sh tests/test_microblog_live.py::TestPostNewAnnounces -q`
Expected: FAIL with `assert [] == [(...)]`.

- [ ] **Step 3: Implement**

In `app/models.py`, in `Post.new`, directly after:

```python
            post.generate_slug(community)
            db.session.commit()
```

insert:

```python
            # Wake any open Live view of the microblogs community (app/community/live.py)
            from app.community import live  # cycle: app.community.live imports from this module
            live.announce_live_post(post, community, backfill)
```

- [ ] **Step 4: Run to verify they pass**

Run: `./run_tests.sh tests/test_microblog_live.py tests/test_ap_peer_source_and_timestamps.py -q`
Expected: all PASS. The second file proves ordinary ingest is unaffected: `NOTIF_SERVER` is empty in tests, so `announce_live_post` returns at once.

- [ ] **Step 5: Commit**

```bash
git add app/models.py tests/test_microblog_live.py
git commit -m "feat: a stored microblog post wakes every open Live view

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: The `/live/stream` SSE route, and its proxy

**Files:**
- Modify: `fastapi_server.py` (after `notifications_stream`, ~line 92; `redis_listener`, ~line 135-152)
- Modify: `Caddyfile:20-24`, `INSTALL.md:774-805`
- Test: `tests/test_live_stream_server.py` (create), `tests/test_caddyfile_routing.py`

**Interfaces:**
- Produces:
  - `GET /live/stream?feed=microblogs`: the SSE stream `": connected"`, then `data: {}` per wake-up, plus heartbeats.
  - Module names: `LIVE_FEEDS`, `LIVE_HEARTBEAT_SECONDS`, `live_clients`, `live_event_stream(feed, q)`, `fan_out_live(channel, data)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_live_stream_server.py`:

```python
"""fastapi_server.py's /live/stream: one broadcast SSE stream per Live feed.

The stream never ends, so the route is called directly rather than read through
TestClient; the generator and the fan-out are driven with asyncio.run.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

import fastapi_server


@pytest.fixture(autouse=True)
def no_clients():
    fastapi_server.live_clients.clear()
    fastapi_server.connected_clients.clear()
    yield
    fastapi_server.live_clients.clear()
    fastapi_server.connected_clients.clear()


def test_an_unknown_feed_is_404():
    response = TestClient(fastapi_server.app).get('/live/stream?feed=nosuch')

    assert response.status_code == 404


def test_a_known_feed_registers_a_queue_and_streams_events():
    response = asyncio.run(fastapi_server.live_stream('microblogs'))

    assert response.media_type == 'text/event-stream'
    assert response.headers['x-accel-buffering'] == 'no'
    assert len(fastapi_server.live_clients['microblogs']) == 1


def test_the_stream_says_connected_then_relays_then_heartbeats_then_cleans_up(monkeypatch):
    monkeypatch.setattr(fastapi_server, 'LIVE_HEARTBEAT_SECONDS', 0.01)

    async def scenario():
        q = asyncio.Queue()
        other = asyncio.Queue()
        fastapi_server.live_clients['microblogs'] = {q, other}
        stream = fastapi_server.live_event_stream('microblogs', q)
        first = await stream.__anext__()
        await q.put('{}')
        second = await stream.__anext__()
        third = await stream.__anext__()
        await stream.aclose()
        still_there = fastapi_server.live_clients['microblogs'] == {other}
        fourth_stream = fastapi_server.live_event_stream('microblogs', other)
        await fourth_stream.__anext__()
        await fourth_stream.aclose()
        return first, second, third, still_there

    first, second, third, still_there = asyncio.run(scenario())

    assert first == ': connected\n\n'
    assert second == 'data: {}\n\n'
    assert third == ': heartbeat\n\n'
    assert still_there
    assert 'microblogs' not in fastapi_server.live_clients


def test_a_wake_up_reaches_live_clients_and_no_notification_client():
    async def scenario():
        live_q, notif_q = asyncio.Queue(), asyncio.Queue()
        fastapi_server.live_clients['microblogs'] = {live_q}
        fastapi_server.connected_clients['microblogs'] = {notif_q}   # a user id that collides with the feed name
        await fastapi_server.fan_out_live('live:microblogs', '{}')
        await fastapi_server.fan_out_live('live:nobody', '{}')
        return live_q.get_nowait(), notif_q.empty()

    message, notif_empty = asyncio.run(scenario())

    assert message == '{}' and notif_empty


def test_the_listener_subscribes_to_live_channels_and_fans_them_out(monkeypatch):
    patterns = []

    class FakePubSub:
        async def psubscribe(self, *names):
            patterns.extend(names)

        async def listen(self):
            yield {'type': 'pmessage', 'channel': 'live:microblogs', 'data': '{}'}
            raise asyncio.CancelledError

    class FakeRedis:
        def pubsub(self):
            return FakePubSub()

    monkeypatch.setattr(fastapi_server, 'r', FakeRedis())

    async def scenario():
        q = asyncio.Queue()
        fastapi_server.live_clients['microblogs'] = {q}
        with pytest.raises(asyncio.CancelledError):
            await fastapi_server.redis_listener()
        return q.get_nowait()

    assert asyncio.run(scenario()) == '{}'
    assert 'live:*' in patterns
```

In `tests/test_caddyfile_routing.py`, append:

```python
def test_the_live_stream_is_proxied_to_fastapi():
    assert '/live/stream' in handled_paths()
```

- [ ] **Step 2: Run to verify they fail**

Run: `./run_tests.sh tests/test_live_stream_server.py tests/test_caddyfile_routing.py -q`
Expected: FAIL with `AttributeError: module 'fastapi_server' has no attribute 'live_clients'`, and the Caddy test fails because `/live/stream` is not in `handled_paths()`.

- [ ] **Step 3: Implement FastAPI**

In `fastapi_server.py`, change the import line to:

```python
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
```

After `connected_clients = {}` add:

```python
# Live feeds: one broadcast SSE stream per feed, for every viewer of it. Kept apart from
# connected_clients so a user's notification stream never receives a broadcast. The payload
# is only a wake-up; each browser then fetches its own filtered posts from Flask.
LIVE_FEEDS = {"microblogs"}
LIVE_HEARTBEAT_SECONDS = 60.0
live_clients = {}
```

After the `notifications_stream` function add:

```python
async def live_event_stream(feed: str, q: asyncio.Queue):
    try:
        yield ": connected\n\n"
        while True:
            try:
                message = await asyncio.wait_for(q.get(), timeout=LIVE_HEARTBEAT_SECONDS)
                yield f"data: {message}\n\n"
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
    finally:
        clients = live_clients.get(feed, set())
        clients.discard(q)
        if not clients:
            live_clients.pop(feed, None)


@app.get("/live/stream")
async def live_stream(feed: str):
    if feed not in LIVE_FEEDS:
        return JSONResponse({"error": "Unknown feed"}, status_code=404)
    q = asyncio.Queue()
    live_clients.setdefault(feed, set()).add(q)
    return StreamingResponse(
        live_event_stream(feed, q),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


async def fan_out_live(channel: str, data: str):
    _, feed = channel.split(":", 1)
    for q in list(live_clients.get(feed, ())):
        await q.put(data)
```

In `redis_listener`, change the subscribe line to:

```python
            await pubsub.psubscribe("notifications:*", "http_posts:*", "messages:*", "live:*")
```

and add a branch before `elif channel.startswith("http_posts:"):`:

```python
                    elif channel.startswith("live:"):
                        await fan_out_live(channel, data)
```

- [ ] **Step 4: Implement the proxy configuration**

In `Caddyfile`, after the `handle /notifications/stream { ... }` block, add:

```
	# The Live view's wake-up stream (fastapi_server.py, /live/stream). Same unbuffered proxying.
	handle /live/stream {
		reverse_proxy piefednotifs:8000 {
			flush_interval -1
		}
	}
```

In `INSTALL.md`, find the closing `}` of the `location /notifications/stream { ... }` block (around line 805). After the code fence that contains it, add:

````markdown
If you use the Live view of the microblogs community, proxy its stream the same way, also within the `server` block:

```
    location = /live/stream {
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_set_header Host $http_host;
            proxy_http_version 1.1;
            proxy_set_header Connection "";
            proxy_pass http://notif_server;

            proxy_buffering off;
            proxy_cache off;
            proxy_redirect off;
            proxy_read_timeout 3600s;
    }
```

Without it, the Live view still works by polling every 15 seconds.
````

- [ ] **Step 5: Run to verify they pass**

Run: `./run_tests.sh tests/test_live_stream_server.py tests/test_caddyfile_routing.py -q`
Expected: all PASS. If `redis_listener` swallows `CancelledError` (read its `except` clauses below line 174 first), the listener test hangs and `pytest-timeout` trips. In that case end the fake `listen` by raising a dedicated `class Stop(BaseException)` and expect that instead. A hang is a bug in the test, never something to wait out.

- [ ] **Step 6: Commit**

```bash
git add fastapi_server.py Caddyfile INSTALL.md tests/test_live_stream_server.py tests/test_caddyfile_routing.py
git commit -m "feat: the notification server broadcasts Live wake-ups on /live/stream

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: The client, `live_feed.js`, with the node test gate

**Files:**
- Create: `package.json`, `tests/js/live_feed.test.mjs`
- Modify: `app/static/js/live_feed.js` (replace the Task 5 placeholder), `run_tests.sh`

**Interfaces:**
- Consumes: the fragment's 200/204/302/404/429 contract from Task 4, and the DOM contract from Task 5.
- Produces:
  - `export function createLiveFeed(deps) -> { start() }`
  - Pure helpers: `shouldHold`, `nextBackoff`, `dedupeNodes`, `trimCount`
  - Constants as listed in the code below.
- scripts.js globals called after an insert (all exist as top-level function declarations, so they are `window` properties):
  - `setupTeaserClick`
  - `setupPostTeaserHandler`
  - `setupBlurredPostImages`
  - `setupVideoSpoilers`
  - `setupVotableElements`
  - `setupLightboxTeaser` (not in low bandwidth)

  Each is safe to re-run over the whole page:
  - `setupTeaserClick` and `setupBlurredPostImages` skip nodes they already bound.
  - `setupPostTeaserHandler` assigns `onclick`.
  - `baguetteBox.run` clears its cache for the selector first (`lightbox/baguetteBox.js:167`).
  - The re-bound video and votable listeners set the same state again.

  `setupDynamicContent` needs no call: scripts.js's MutationObserver runs it for inserted nodes (`scripts.js:1644`).

- [ ] **Step 1: Create the gate and write the failing tests**

Create `package.json`:

```json
{
  "name": "pyfedi-js-tests",
  "private": true,
  "type": "module",
  "description": "Client-side tests only. No runtime dependencies; nothing here is served.",
  "scripts": {
    "test:js": "node --test --experimental-test-coverage --test-coverage-include=app/static/js/live_feed.js --test-coverage-lines=100 --test-coverage-branches=100 --test-coverage-functions=100 'tests/js/*.test.mjs'"
  }
}
```

Create `tests/js/live_feed.test.mjs`:

```js
// Tests of app/static/js/live_feed.js, driven entirely through injected fakes.
// Run: npm run test:js (fails below 100% line, branch or function coverage).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
    createLiveFeed, shouldHold, nextBackoff, dedupeNodes, trimCount,
    POLL_MS, SAFETY_POLL_MS, COALESCE_MS, MAX_BACKOFF_MS, HIGHLIGHT_MS, MAX_TEASERS, TEASER_SETUPS,
} from '../../app/static/js/live_feed.js';

const settle = () => new Promise(resolve => setImmediate(resolve));

class FakeClassList {
    constructor() { this.names = new Set(); }
    add(name) { this.names.add(name); }
    remove(name) { this.names.delete(name); }
    toggle(name, on) { if (on) this.add(name); else this.remove(name); }
    contains(name) { return this.names.has(name); }
}

class FakeNode {
    constructor(id) { this.id = id; this.classList = new FakeClassList(); this.parent = null; }
    remove() { this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; }
}

class FakeList {
    constructor(ids = []) { this.children = []; for (const id of ids) this.adopt(new FakeNode(id), false); }
    adopt(node, atFront) { node.parent = this; if (atFront) this.children.unshift(node); else this.children.push(node); }
    prepend(...nodes) { for (const node of [...nodes].reverse()) this.adopt(node, true); }
    get lastElementChild() { return this.children[this.children.length - 1]; }
    ids() { return this.children.map(node => node.id); }
}

class FakeTarget {
    constructor() { this.listeners = {}; }
    addEventListener(type, fn) { (this.listeners[type] ??= []).push(fn); }
    fire(type) { for (const fn of this.listeners[type] ?? []) fn(); }
}

function fakeClock() {
    let time = 0;
    let sequence = 0;
    const timers = new Map();
    return {
        now: () => time,
        setTimeout: (fn, ms) => { const id = ++sequence; timers.set(id, { at: time + ms, fn }); return id; },
        clearTimeout: id => { timers.delete(id); },
        async advance(ms) {
            const end = time + ms;
            for (;;) {
                let next = null;
                for (const [id, timer] of timers) {
                    if (timer.at <= end && (next === null || timer.at < next[1].at)) next = [id, timer];
                }
                if (next === null) break;
                timers.delete(next[0]);
                time = next[1].at;
                next[1].fn();
                await settle();
            }
            time = end;
            await settle();
        },
    };
}

function response(status, { body = '', cursor = null, redirected = false } = {}) {
    return { status, redirected, headers: { get: name => (name === 'X-Live-Cursor' ? cursor : null) },
             text: async () => body };
}

function makeEventSource() {
    const instances = [];
    class FakeEventSource {
        constructor(url) { this.url = url; this.closed = false; instances.push(this); }
        close() { this.closed = true; }
    }
    return { FakeEventSource, instances };
}

// Builds a feed. `replies` is consumed one per fetch; once empty, every fetch answers 204.
// A reply that is an Error makes the fetch reject.
function setup({ replies = [], ids = ['post_1'], sseUrl = '', EventSource, lowBandwidth = false,
                 hidden = false, scrollY = 0, withHelpers = true } = {}) {
    const clock = fakeClock();
    const list = new FakeList(ids);
    const pill = Object.assign(new FakeTarget(), { hidden: true, textContent: '' });
    const status = { textContent: '', classList: new FakeClassList() };
    const document = Object.assign(new FakeTarget(), { hidden });
    const calls = [];
    const window = Object.assign(new FakeTarget(), { scrollY, scrolledTo: [] });
    window.scrollTo = options => window.scrolledTo.push(options.top);
    if (withHelpers) {
        window.htmx = { process: node => calls.push(`htmx:${node.id}`) };
        for (const name of [...TEASER_SETUPS, 'setupLightboxTeaser']) window[name] = () => calls.push(name);
    }
    const urls = [];
    const queue = [...replies];
    const fetch = url => {
        urls.push(url);
        const reply = queue.length ? queue.shift() : response(204);
        if (typeof reply === 'function') return reply();
        return reply instanceof Error ? Promise.reject(reply) : Promise.resolve(reply);
    };
    const parse = html => (html ? html.split(',').map(id => new FakeNode(id)) : []);
    const feed = createLiveFeed({
        list, pill, status, fetch, EventSource, document, window, parse,
        setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout, now: clock.now,
        config: { postsUrl: '/live/posts', cursor: 1, sseUrl, lowBandwidth,
                  strings: { live: 'Live', paused: 'Paused', newPosts: '%d new posts' } },
    });
    return { feed, clock, list, pill, status, document, window, urls, calls };
}

test('pure helpers', () => {
    assert.equal(shouldHold(100), false);
    assert.equal(shouldHold(101), true);
    assert.equal(nextBackoff(POLL_MS), POLL_MS * 2);
    assert.equal(nextBackoff(MAX_BACKOFF_MS), MAX_BACKOFF_MS);
    const kept = dedupeNodes(['post_1'], [new FakeNode('post_1'), new FakeNode('post_2'),
                                          new FakeNode('post_2'), new FakeNode('')]);
    assert.deepEqual(kept.map(node => node.id), ['post_2']);
    assert.equal(trimCount(MAX_TEASERS), 0);
    assert.equal(trimCount(MAX_TEASERS + 3), 3);
});

test('starting visible fetches at once from the cursor, then polls every 15 s', async () => {
    const { feed, clock, urls, status } = setup();
    feed.start();
    await settle();
    assert.deepEqual(urls, ['/live/posts?after=1']);
    assert.equal(status.textContent, 'Live');
    assert.ok(status.classList.contains('text-bg-success'));
    await clock.advance(POLL_MS - 1);
    assert.equal(urls.length, 1);
    await clock.advance(1);
    assert.equal(urls.length, 2);
});

test('new posts at the top are inserted newest first, highlighted, initialised; the cursor moves', async () => {
    const { feed, clock, list, urls, calls } = setup({ replies: [response(200, { body: 'post_3,post_2', cursor: '3' })] });
    feed.start();
    await settle();
    assert.deepEqual(list.ids(), ['post_3', 'post_2', 'post_1']);
    assert.ok(list.children[0].classList.contains('bg-warning-subtle'));
    assert.deepEqual(calls, ['htmx:post_3', 'htmx:post_2', ...TEASER_SETUPS, 'setupLightboxTeaser']);
    await clock.advance(HIGHLIGHT_MS);
    assert.ok(!list.children[0].classList.contains('bg-warning-subtle'));
    await clock.advance(POLL_MS);
    assert.equal(urls.at(-1), '/live/posts?after=3');
});

test('low bandwidth skips the lightbox', async () => {
    const { feed, calls } = setup({ lowBandwidth: true, replies: [response(200, { body: 'post_2', cursor: '2' })] });
    feed.start();
    await settle();
    assert.ok(!calls.includes('setupLightboxTeaser'));
});

test('missing page helpers are skipped, not fatal', async () => {
    const { feed, list } = setup({ withHelpers: false, replies: [response(200, { body: 'post_2', cursor: '2' })] });
    feed.start();
    await settle();
    assert.deepEqual(list.ids(), ['post_2', 'post_1']);
});

test('a 200 with no cursor header keeps the old cursor; one with no teasers still moves it', async () => {
    const { feed, clock, urls, list } = setup({ replies: [response(200, { body: 'post_2' }),
                                                          response(200, { body: '', cursor: '9' })] });
    feed.start();
    await settle();
    await clock.advance(POLL_MS);
    assert.equal(urls[1], '/live/posts?after=1');
    await clock.advance(POLL_MS);
    assert.equal(urls[2], '/live/posts?after=9');
    assert.deepEqual(list.ids(), ['post_2', 'post_1']);
});

test('a reader scrolled down gets a pill, not a jump; the pill flushes to the top', async () => {
    const { feed, clock, list, pill, window } = setup({
        scrollY: 500,
        replies: [response(200, { body: 'post_2', cursor: '2' }), response(200, { body: 'post_4,post_3', cursor: '4' })],
    });
    feed.start();
    await settle();
    assert.deepEqual(list.ids(), ['post_1']);
    assert.equal(pill.hidden, false);
    assert.equal(pill.textContent, '1 new posts');
    await clock.advance(POLL_MS);
    assert.equal(pill.textContent, '3 new posts');
    pill.fire('click');
    assert.deepEqual(window.scrolledTo, [0]);
    assert.deepEqual(list.ids(), ['post_4', 'post_3', 'post_2', 'post_1']);
    assert.equal(pill.hidden, true);
});

test('scrolling back to the top flushes held posts; other scrolls do nothing', async () => {
    const { feed, list, window } = setup({ scrollY: 500, replies: [response(200, { body: 'post_2', cursor: '2' })] });
    feed.start();
    await settle();
    window.fire('scroll');                 // still scrolled down: nothing
    assert.deepEqual(list.ids(), ['post_1']);
    window.scrollY = 0;
    window.fire('scroll');
    assert.deepEqual(list.ids(), ['post_2', 'post_1']);
    window.fire('scroll');                 // nothing held: nothing
    assert.deepEqual(list.ids(), ['post_2', 'post_1']);
});

test('a pill click with nothing held only hides the pill', () => {
    const { feed, pill, list } = setup();
    feed.start();
    pill.hidden = false;
    pill.fire('click');
    assert.equal(pill.hidden, true);
    assert.deepEqual(list.ids(), ['post_1']);
});

test('a post already listed or already held is never added twice', async () => {
    const { feed, clock, list, pill, window } = setup({
        replies: [response(200, { body: 'post_1', cursor: '1' }), response(200, { body: 'post_2', cursor: '2' }),
                  response(200, { body: 'post_2', cursor: '2' })],
    });
    feed.start();
    await settle();
    assert.deepEqual(list.ids(), ['post_1']);
    assert.equal(pill.hidden, true);
    window.scrollY = 500;
    await clock.advance(POLL_MS);
    await clock.advance(POLL_MS);
    assert.equal(pill.textContent, '1 new posts');
});

test('the list never grows past the cap; the oldest teasers go', async () => {
    const ids = Array.from({ length: MAX_TEASERS - 1 }, (_, i) => `post_${i + 10}`);
    const { feed, list } = setup({ ids, replies: [response(200, { body: 'post_a,post_b,post_c', cursor: '9999' })] });
    feed.start();
    await settle();
    assert.equal(list.children.length, MAX_TEASERS);
    assert.deepEqual(list.ids().slice(0, 3), ['post_a', 'post_b', 'post_c']);
    assert.ok(!list.ids().includes(`post_${MAX_TEASERS - 1 + 9}`));
});

test('429, server errors and network errors back off to a cap; a 204 resets', async () => {
    const replies = [response(429), response(500), new Error('offline'), response(429), response(429), response(204)];
    const { feed, clock, urls } = setup({ replies });
    feed.start();
    await settle();                                         // 429 -> next in 30 s
    await clock.advance(POLL_MS * 2);                        // 500 -> 60 s
    assert.equal(urls.length, 2);
    await clock.advance(POLL_MS * 4);                        // network error -> 120 s
    await clock.advance(MAX_BACKOFF_MS);                     // 429 -> stays 120 s
    await clock.advance(MAX_BACKOFF_MS);                     // 429 -> 120 s
    await clock.advance(MAX_BACKOFF_MS);                     // 204 -> back to 15 s
    assert.equal(urls.length, 6);
    await clock.advance(POLL_MS);
    assert.equal(urls.length, 7);
});

for (const [label, reply] of [['a login redirect', response(200, { body: 'post_9', redirected: true })],
                              ['403', response(403)], ['404', response(404)]]) {
    test(`${label} stops the feed for good`, async () => {
        const { feed, clock, urls, status, list, document } = setup({ replies: [reply] });
        feed.start();
        await settle();
        assert.equal(status.textContent, 'Paused');
        assert.ok(status.classList.contains('text-bg-secondary'));
        assert.deepEqual(list.ids(), ['post_1']);
        document.hidden = false;
        document.fire('visibilitychange');
        await clock.advance(MAX_BACKOFF_MS * 2);
        assert.equal(urls.length, 1);
    });
}

test('with SSE, wake-ups are coalesced to one fetch per 5 s and a 60 s safety poll runs', async () => {
    const { FakeEventSource, instances } = makeEventSource();
    const { feed, clock, urls } = setup({ sseUrl: 'https://n.example/live/stream?feed=microblogs',
                                          EventSource: FakeEventSource });
    feed.start();
    await settle();
    assert.equal(instances[0].url, 'https://n.example/live/stream?feed=microblogs');
    assert.equal(urls.length, 1);
    instances[0].onmessage();
    instances[0].onmessage();
    await clock.advance(COALESCE_MS - 1);
    assert.equal(urls.length, 1);
    await clock.advance(1);
    assert.equal(urls.length, 2);
    await clock.advance(COALESCE_MS + 1);
    instances[0].onmessage();                                // long after the last fetch: no wait
    await clock.advance(0);
    assert.equal(urls.length, 3);
    await clock.advance(SAFETY_POLL_MS);
    assert.equal(urls.length, 4);
});

test('three SSE errors in a row fall back to polling; a message in between resets the count', async () => {
    const { FakeEventSource, instances } = makeEventSource();
    const { feed, clock, urls, document } = setup({ sseUrl: '/s', EventSource: FakeEventSource });
    feed.start();
    await settle();
    const source = instances[0];
    source.onerror(); source.onerror();
    source.onmessage();
    source.onerror(); source.onerror();
    assert.equal(source.closed, false);
    source.onerror();
    assert.equal(source.closed, true);
    const before = urls.length;
    await clock.advance(POLL_MS);
    assert.equal(urls.length, before + 1);
    document.hidden = true; document.fire('visibilitychange');
    document.hidden = false; document.fire('visibilitychange');
    assert.equal(instances.length, 1);                       // SSE is not retried after it failed
});

test('an SSE URL without EventSource support polls', async () => {
    const { feed, clock, urls } = setup({ sseUrl: '/s', EventSource: undefined });
    feed.start();
    await settle();
    await clock.advance(POLL_MS);
    assert.equal(urls.length, 2);
});

test('a hidden tab pauses everything; showing it again catches up at once', async () => {
    const { FakeEventSource, instances } = makeEventSource();
    const { feed, clock, urls, status, document } = setup({ sseUrl: '/s', EventSource: FakeEventSource });
    feed.start();
    await settle();
    instances[0].onmessage();
    document.hidden = true;
    document.fire('visibilitychange');
    assert.equal(status.textContent, 'Paused');
    assert.equal(instances[0].closed, true);
    await clock.advance(SAFETY_POLL_MS * 2);
    assert.equal(urls.length, 1);
    document.hidden = false;
    document.fire('visibilitychange');
    await settle();
    assert.equal(urls.length, 2);
    assert.equal(instances.length, 2);
    assert.equal(status.textContent, 'Live');
});

test('a polling tab pauses and resumes too', async () => {
    const { feed, clock, urls, document } = setup();
    feed.start();
    await settle();
    document.hidden = true;
    document.fire('visibilitychange');
    await clock.advance(POLL_MS * 2);
    assert.equal(urls.length, 1);
});

test('starting in a hidden tab fetches nothing until it is shown', async () => {
    const { feed, urls, status } = setup({ hidden: true });
    feed.start();
    await settle();
    assert.equal(urls.length, 0);
    assert.equal(status.textContent, 'Paused');
});

test('a fetch already in flight is not doubled, and a pause during it schedules nothing', async () => {
    const { FakeEventSource, instances } = makeEventSource();
    let release;
    const slow = () => new Promise(resolve => { release = () => resolve(response(204)); });
    const { feed, clock, urls, document } = setup({ sseUrl: '/s', EventSource: FakeEventSource, replies: [slow] });
    feed.start();
    await clock.advance(COALESCE_MS);
    instances[0].onmessage();
    await clock.advance(COALESCE_MS);
    assert.equal(urls.length, 1);
    document.hidden = true;
    document.fire('visibilitychange');
    release();
    await settle();
    await clock.advance(SAFETY_POLL_MS * 2);
    assert.equal(urls.length, 1);
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `npm run test:js`
Expected: FAIL. The placeholder `live_feed.js` exports nothing, so the import fails with a `SyntaxError` naming `createLiveFeed`.

- [ ] **Step 3: Implement**

Replace `app/static/js/live_feed.js` with:

```js
// Live view of /c/microblogs: new posts arrive without a reload, the way Mastodon's Live Feeds does.
// Spec: docs/superpowers/specs/2026-10-07-microblog-live-feed-design.md
//
// Every browser dependency is passed in, so tests/js/live_feed.test.mjs drives all of it under node
// with fakes. Only the bootstrap at the bottom touches real globals.

export const TOP_THRESHOLD_PX = 100;
export const MAX_TEASERS = 200;
export const POLL_MS = 15000;
export const SAFETY_POLL_MS = 60000;    // with SSE: Redis pub/sub is not durable, so a wake-up can be lost
export const COALESCE_MS = 5000;
export const MAX_BACKOFF_MS = 120000;
export const SSE_ERROR_LIMIT = 3;
export const HIGHLIGHT_MS = 2000;
// scripts.js setups that are safe to re-run over the whole page: each skips what it already
// bound, or re-binds to the same effect. setupDynamicContent needs no call: scripts.js's
// MutationObserver runs it for inserted nodes.
export const TEASER_SETUPS = ['setupTeaserClick', 'setupPostTeaserHandler', 'setupBlurredPostImages',
                              'setupVideoSpoilers', 'setupVotableElements'];

export function shouldHold(scrollY) {
    return scrollY > TOP_THRESHOLD_PX;
}

export function nextBackoff(ms) {
    return Math.min(ms * 2, MAX_BACKOFF_MS);
}

// The nodes of `incoming` whose id is neither in `seenIds` nor earlier in `incoming`.
export function dedupeNodes(seenIds, incoming) {
    const ids = new Set(seenIds);
    return incoming.filter(node => {
        if (!node.id || ids.has(node.id)) return false;
        ids.add(node.id);
        return true;
    });
}

export function trimCount(length) {
    return Math.max(0, length - MAX_TEASERS);
}

export function createLiveFeed({ list, pill, status, fetch, EventSource, document, window, setTimeout,
                                 clearTimeout, now, parse, config }) {
    let cursor = config.cursor;
    let held = [];
    let interval = POLL_MS;
    let source = null;
    let sseErrors = 0;
    let sseFailed = false;
    let timer = null;
    let coalesceTimer = null;
    let lastFetch = -Infinity;
    let inFlight = false;
    let paused = false;
    let stopped = false;

    function setStatus(live) {
        status.textContent = live ? config.strings.live : config.strings.paused;
        status.classList.toggle('text-bg-success', live);
        status.classList.toggle('text-bg-secondary', !live);
    }

    function schedule(ms) {
        clearTimeout(timer);
        timer = setTimeout(fetchNow, ms);
    }

    function initTeasers(nodes) {
        for (const node of nodes) window.htmx?.process(node);
        const setups = config.lowBandwidth ? TEASER_SETUPS : TEASER_SETUPS.concat('setupLightboxTeaser');
        for (const name of setups) window[name]?.();
    }

    function insert(nodes) {
        list.prepend(...nodes);
        for (const node of nodes) {
            node.classList.add('bg-warning-subtle');
            setTimeout(() => node.classList.remove('bg-warning-subtle'), HIGHLIGHT_MS);
        }
        initTeasers(nodes);
        for (let extra = trimCount(list.children.length); extra > 0; extra--) list.lastElementChild.remove();
    }

    function flush() {
        pill.hidden = true;
        if (held.length === 0) return;
        const nodes = held;
        held = [];
        insert(nodes);
    }

    function receive(html) {
        const seen = Array.from(list.children, node => node.id).concat(held.map(node => node.id));
        const nodes = dedupeNodes(seen, parse(html));
        if (nodes.length === 0) return;
        if (shouldHold(window.scrollY)) {
            held = nodes.concat(held);
            pill.textContent = config.strings.newPosts.replace('%d', held.length);
            pill.hidden = false;
        } else {
            insert(nodes);
        }
    }

    async function fetchNow() {
        if (inFlight) return;
        inFlight = true;
        lastFetch = now();
        try {
            const response = await fetch(`${config.postsUrl}?after=${cursor}`, { credentials: 'same-origin' });
            if (response.redirected || response.status === 403 || response.status === 404) {
                stop();           // logged out (login_required redirects) or Live is gone
            } else if (response.status === 200) {
                receive(await response.text());
                cursor = Number(response.headers.get('X-Live-Cursor')) || cursor;
                interval = POLL_MS;
            } else if (response.status === 204) {
                interval = POLL_MS;
            } else {
                interval = nextBackoff(interval);
            }
        } catch (error) {
            interval = nextBackoff(interval);
        } finally {
            inFlight = false;
        }
        if (!paused) schedule(source ? SAFETY_POLL_MS : interval);
    }

    function requestFetch() {
        if (coalesceTimer !== null) return;
        const wait = Math.max(0, lastFetch + COALESCE_MS - now());
        coalesceTimer = setTimeout(() => { coalesceTimer = null; fetchNow(); }, wait);
    }

    function closeSse() {
        if (source) source.close();
        source = null;
    }

    function openSse() {
        source = new EventSource(config.sseUrl);
        source.onmessage = () => { sseErrors = 0; requestFetch(); };
        source.onerror = () => {
            sseErrors += 1;
            if (sseErrors >= SSE_ERROR_LIMIT) {
                sseFailed = true;
                closeSse();
                schedule(interval);
            }
        };
    }

    function pause() {
        paused = true;
        clearTimeout(timer);
        clearTimeout(coalesceTimer);
        coalesceTimer = null;
        closeSse();
        setStatus(false);
    }

    function resume() {
        paused = false;
        setStatus(true);
        if (config.sseUrl && EventSource && !sseFailed) openSse();
        fetchNow();
    }

    function stop() {
        stopped = true;
        pause();
    }

    function start() {
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) pause();
            else if (!stopped) resume();
        });
        window.addEventListener('scroll', () => {
            if (held.length > 0 && !shouldHold(window.scrollY)) flush();
        }, { passive: true });
        pill.addEventListener('click', () => {
            window.scrollTo({ top: 0 });
            flush();
        });
        if (document.hidden) pause();
        else resume();
    }

    return { start };
}

/* node:coverage disable */
if (typeof window !== 'undefined') {
    const list = window.document.getElementById('live_feed');
    if (list) {
        createLiveFeed({
            list,
            pill: window.document.getElementById('live_pill'),
            status: window.document.getElementById('live_status'),
            fetch: window.fetch.bind(window),
            EventSource: window.EventSource,
            document: window.document,
            window,
            setTimeout: window.setTimeout.bind(window),
            clearTimeout: window.clearTimeout.bind(window),
            now: () => Date.now(),
            parse: html => {
                const template = window.document.createElement('template');
                template.innerHTML = html;
                return Array.from(template.content.querySelectorAll(':scope > .post_teaser'));
            },
            config: {
                postsUrl: list.dataset.postsUrl,
                cursor: Number(list.dataset.cursor),
                sseUrl: list.dataset.sseUrl,
                lowBandwidth: window.document.body.classList.contains('low_bandwidth'),
                strings: { live: list.dataset.strLive, paused: list.dataset.strPaused,
                           newPosts: list.dataset.strNewPosts },
            },
        }).start();
    }
}
/* node:coverage enable */
```

In `run_tests.sh`, directly after the `--down` block (`fi` following `exit 0`), add:

```bash
# The client-side tests (package.json's test:js) need only node, on the host: the test
# container has none. They take about a second and fail below 100% coverage of live_feed.js.
if command -v node >/dev/null 2>&1; then
    npm run --silent test:js
else
    echo "run_tests.sh: node not found; the JavaScript tests did not run." >&2
fi
```

- [ ] **Step 4: Run to verify they pass at 100%**

Run: `npm run test:js`
Expected: every test passes, and the coverage table shows `live_feed.js` at 100 line, 100 branch and 100 functions with exit code 0.
- If a branch is reported uncovered, add the missing test case. Do not add a `node:coverage ignore`: the only exempt region is the bootstrap.
- If a test hangs, a fake promise is never resolved. Fix the test.

- [ ] **Step 5: Commit**

```bash
git add package.json tests/js/live_feed.test.mjs app/static/js/live_feed.js run_tests.sh
git commit -m "feat: the Live view streams new posts in, holding them while the reader is scrolled down

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Coverage gates and browser verification

**Files:** none new. Fix whatever the gates find in the owning task's files.

- [ ] **Step 1: Run the full suite with coverage**

Run: `./run_tests.sh tests/ --cov=app --cov=fastapi_server --cov-branch --cov-report=json:coverage.json -q`
Expected: all tests pass. `run_tests.sh` runs `npm run test:js` first, and that also passes.

- [ ] **Step 2: Changed lines and branches at 100%**

Run: `python3 tests/check_changed_line_coverage.py coverage.json d2f67b2fb --branches app/ fastapi_server.py`
Expected: exit 0, printing `Every added line since d2f67b2fb that coverage measured was executed and every branch out of one was taken.` `d2f67b2fb` is the spec commit, the last commit before this work.
- On exit 1, each `path:line` named is a missing test. Add it to the owning task's test file, rerun Step 1, and commit with `test: cover <what>`.

- [ ] **Step 3: Floors**

Run: `python3 tests/check_coverage_floors.py coverage.json coverage_floors.ini`
Expected: exit 0. `app/community/live.py` is at 100, and no other floor dropped.

- [ ] **Step 4: Browser verification (not a committed gate)**

Start the app with `NOTIF_SERVER` set and the FastAPI service running, following the `run` skill or the project's dev compose (`compose.dev.yaml`). Log in. Open `/c/microblogs?sort=live` with Playwright, then check each item below:

1. **Live button.** It appears after Active, and the status badge reads "Live".
2. **Insert at the top.** Create a microblog post in that community, either through a federated Create or a `flask shell` call to `Post.new`. It appears at the top within ~5 s, highlighted.
3. **Hold while scrolled.** Scroll down more than 100 px and create another post. The page does not move, and the pill shows "1 new posts". Click it: the page scrolls to the top and the post appears.
4. **Inserted teasers work.** On an inserted teaser, the vote button works (htmx processed) and the teaser click opens the post.
5. **Hidden tab.** Switch tabs for 30 s. No requests to `/live/posts` appear in the network log while it is hidden, and one goes out on return.
6. **Polling fallback.** Restart with `NOTIF_SERVER` unset. The page polls `/live/posts` every 15 s, and a new post appears within 15 s.

Record the result of each check in the final report. If any fails, open a defect, fix it test-first in the owning task's files, and rerun Steps 1-3.

- [ ] **Step 5: Final commit, if Steps 2-4 changed anything**

```bash
git add -A && git commit -m "test: cover the last Live feed lines the gates found

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
