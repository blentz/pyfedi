# Live Feed: Remote Microblogs and Home Live — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing Live view in two ways: to `microblogs` communities hosted on PieFed instances, and to the home page's Subscribed, Local, Popular, Media and All tabs.

**Architecture:**
- A stored post publishes an empty wake-up on `live:<key>` for every feed it belongs to (`community:<id>`, `any`, `local`, `popular`, `media`).
- FastAPI accepts any key that matches a pattern.
- Home Live reuses the home feed's own query (`get_deduped_post_ids`) through a new `newer_than` cursor argument, and a home fragment endpoint returns new teasers.
- The client (`live_feed.js`) is unchanged.

**Tech Stack:** Flask, Jinja2, SQLAlchemy raw SQL (`get_deduped_post_ids`), Flask-Limiter, Redis pub/sub, FastAPI SSE, vanilla JS; pytest in containers, node on the host.

**Spec:** `docs/superpowers/specs/2026-10-07-microblog-live-feed-design.md`. The base design plus **Amendment A** at the end. Read Amendment A.

## Global Constraints

- **Live-capable community:** `name == 'microblogs'` and (`is_local()` or `instance.software.lower() == 'piefed'`).
- **Wake-up keys:**
  - Pattern: `^(any|local|popular|media|community:\d+)$`. The payload is the literal `{}` on Redis channel `live:<key>`.
  - Nothing is published for a backfill, a non-listable post, `status <= POST_STATUS_REVIEWING`, or a deleted post, or when `NOTIF_SERVER` is unset.
- **Key rules:**
  - `community:<id>` when the community is Live-capable.
  - `any` always.
  - `local` when `community.instance_id == 1` and it is not the local microblogs community.
  - `popular` when `community.show_popular`.
  - `media` when `community.instance.software.lower()` is in `app.discovery.MEDIA_SOFTWARE`.
- **Home Live:**
  - Who and when: logged-in users only; tabs `subscribed`, `local`, `popular`, `media`, `all`; home page 1 (`page == 0`, since home pages are 0-based); no tag.
  - Anything else falls back to `new`.
  - SSE key: `any` for subscribed and all; otherwise the tab name.
- **Window and limit:** `LIVE_WINDOW = timedelta(hours=1)`, `LIVE_LIMIT = 40`. The cursor is `Post.id`.
- **Rate limit:** `12/minute` keyed `live:<user id>:<feed>`. `<feed>` is `community:<actor>` for community fragments and the `view_filter` for home fragments.
- **Coverage:**
  - Python: 100% line and branch coverage of new and changed lines (`python3 tests/check_changed_line_coverage.py coverage.json 25baae1ed --branches app/ fastapi_server.py`).
  - JS: `npm run test:js` stays at 100/100/100.
  - No floor in `coverage_floors.ini` may drop.
  - Full suite green.
- **No function-level imports in `app/`** (`tests/test_no_inline_imports.py` is a ratchet).
- Python tests run only inside containers: `./run_tests.sh <paths> -q`. Gate scripts run on the host with `python3`.
- Commits use Conventional Commits and end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- The dillo theme's `_home_nav.html` is not changed.

## Review Focus

1. **A followed author's microblog post on Subscribed Live.** It is not in a `show_all` community. It must still wake the tab (key `any`) and must be returned by the home fragment (`include_following=True`). Tested in Task 1 and Task 4.
2. **The local microblogs community and the Local tab.** Its posts must not wake `local`, because `home_page` excludes that community from Local. Tested in Task 1.
3. **`microblogs` on a non-PieFed instance** (Lemmy, Mastodon). There must be no Live button, the fragment returns 404, and no `community:<id>` wake-up is sent. Tested in Tasks 1 and 3.
4. **Two Live tabs open at once on different feeds.** They must not share a rate-limit budget. Tested in Task 4.
5. **The home `reload_url` auto-refresh.** It must be off on the Live page (`#auto-reload` absent), or two refresh mechanisms fight. Tested in Task 5.

---

## File Structure

| File | Change |
|---|---|
| `app/community/live.py` | `is_live_community` (A1); `live_feed_keys`; `announce_live_post` publishes per key; `HOME_LIVE_FILTERS`, `home_live_available`, `home_live_key`; drop `LIVE_COMMUNITY`/`LIVE_CHANNEL` use |
| `fastapi_server.py` | `LIVE_FEEDS` set → `LIVE_FEED_PATTERN` regex |
| `app/utils.py` | `get_deduped_post_ids(..., newer_than=None)` |
| `app/main/routes.py` | `home_feed_source(view_filter)` extracted; `home_live_posts` fragment route; Live handling in `index`/`home_page` |
| `app/community/routes.py` | community fragment rate-limit key per feed |
| `app/templates/_live_bar.html` (new) | shared `#live_status` + `#live_pill` |
| `app/templates/community/community.html` | include `_live_bar.html`; SSE key `community:<id>` |
| `app/templates/index.html` | Live bar, `#live_feed` attrs, script, Older posts, no `#auto-reload` in Live |
| `app/templates/_home_nav.html` | "Live" after New (dropdown + buttons) |
| `tests/test_microblog_live.py` | updated channel expectations; new key/gating tests |
| `tests/test_home_live.py` (new) | home source move, newer_than, fragment, page |
| `tests/test_live_stream_server.py` | feed-pattern tests |

---

### Task 1: Live-capable remote communities and per-feed wake-up keys

**Files:**
- Modify: `app/community/live.py`
- Modify: `app/templates/community/community.html` (the `data-sse-url` line, ~162)
- Test: `tests/test_microblog_live.py`

**Interfaces:**
- Produces (in `app.community.live`):
  - `is_live_community(community) -> bool` (A1).
  - `live_feed_keys(post, community, backfill: bool) -> set[str]`.
  - `announce_live_post(post, community, backfill)`: publishes `('live:' + key, '{}')` once per key, in sorted order.
  - `HOME_LIVE_FILTERS = ('subscribed', 'local', 'popular', 'media', 'all')`.
  - `home_live_key(view_filter: str) -> str`.
  - `home_live_available(user, view_filter: str, page: int, tag: str) -> bool`.
  - `LIVE_KEY_PATTERN = r'^(any|local|popular|media|community:\d+)$'`, used by Task 2.
- Community page SSE URL: `<NOTIF_SERVER>/live/stream?feed=community:<community.id>`.

- [ ] **Step 1: Update the tests that pin the old single channel, and add new ones**

In `tests/test_microblog_live.py`:

In the `live` fixture, directly after `microblogs = make_community('microblogs')`, mirror production's `find_microblogging_community` (which creates the community with `show_popular=False, show_all=False`). `Community.show_popular` and `show_all` default to True (`app/models.py:1322`), so exact key sets would otherwise include `popular`:

```python
    microblogs.show_popular = False
    microblogs.show_all = False
```

The existing `TestLivePage` tests read the community page, so they do not depend on these flags. Run the file after this change to confirm.

Change `test_a_new_microblogs_post_wakes_the_live_feed` to:

```python
    def test_a_new_microblogs_post_wakes_its_community_and_any(self, app, live, published):
        from app.community.live import announce_live_post

        with app.test_request_context():
            announce_live_post(live.toot(), live.microblogs, backfill=False)

        assert published == [('live:any', '{}'), (f'live:community:{live.microblogs.id}', '{}')]
```

Change `test_a_post_in_another_community_does_not` to assert that the community key is absent while `any` and `local` are present:

```python
    def test_a_post_in_another_local_community_wakes_any_and_local_only(self, app, live, published):
        from app.community.live import announce_live_post

        general = make_community('general')
        general.show_popular = False
        db.session.commit()
        post = make_post(general, live.author, 'https://mastodon.example/statuses/g')
        with app.test_request_context():
            announce_live_post(post, general, backfill=False)

        assert published == [('live:any', '{}'), ('live:local', '{}')]
```

In `test_the_signal_reaches_the_redis_channel`, subscribe to `f'live:community:{live.microblogs.id}'` and assert that channel.

In `TestLivePage.test_with_a_notification_server_the_page_names_the_live_stream`, assert:
`f'data-sse-url="https://notifs.example/live/stream?feed=community:{live.microblogs.id}"' in html`.

Append:

```python
def piefed_microblogs(host='piefed.social', software='piefed'):
    instance = make_instance(host, software=software)
    community = make_community('microblogs', host=host)
    community.ap_id = f'microblogs@{host}'
    community.instance_id = instance.id
    db.session.commit()
    return community


class TestLiveCapableCommunities:

    @pytest.mark.parametrize('software, expected', [
        ('piefed', True), ('PieFed', True), ('lemmy', False), ('mastodon', False)])
    def test_remote_microblogs_is_live_only_on_piefed(self, app, live, software, expected):
        from app.community.live import is_live_community

        community = piefed_microblogs(f'{software.lower()}.example', software)
        with app.test_request_context():
            assert is_live_community(community) is expected

    def test_a_remote_piefed_community_with_another_name_is_not(self, app, live):
        from app.community.live import is_live_community

        community = piefed_microblogs()
        community.name = 'general'
        db.session.commit()
        with app.test_request_context():
            assert not is_live_community(community)

    def test_the_remote_piefed_microblogs_page_goes_live(self, client, live):
        community = piefed_microblogs()
        make_post(community, live.author, 'https://piefed.social/post/1', microblog=True)
        login(client, live.viewer)

        html = client.get('/c/microblogs@piefed.social?sort=live').get_data(as_text=True)

        assert 'id="live_feed"' in html
        assert 'data-posts-url="/community/microblogs@piefed.social/live/posts"' in html

    def test_a_remote_lemmy_microblogs_page_has_no_live(self, client, live):
        piefed_microblogs('lemmy.example', 'lemmy')
        login(client, live.viewer)

        html = client.get('/c/microblogs@lemmy.example?sort=live').get_data(as_text=True)

        assert 'id="live_feed"' not in html and '?sort=live' not in html

    def test_the_remote_piefed_fragment_serves_new_posts(self, client, live):
        community = piefed_microblogs()
        post = make_post(community, live.author, 'https://piefed.social/post/2', microblog=True)
        login(client, live.viewer)

        response = client.get('/community/microblogs@piefed.social/live/posts?after=0')

        assert response.status_code == 200 and teaser_ids(response.get_data(as_text=True)) == [post.id]


class TestLiveFeedKeys:

    def keys(self, app, post, community, backfill=False):
        from app.community.live import live_feed_keys

        with app.test_request_context():
            return live_feed_keys(post, community, backfill)

    def test_a_live_capable_community_post(self, app, live):
        assert self.keys(app, live.toot(), live.microblogs) == {'any', f'community:{live.microblogs.id}'}

    def test_the_local_microblogs_community_does_not_wake_local(self, app, live):
        assert 'local' not in self.keys(app, live.toot(), live.microblogs)

    def test_a_local_popular_community(self, app, live):
        general = make_community('general')
        general.show_popular = True
        db.session.commit()
        post = make_post(general, live.author, 'https://mastodon.example/statuses/p')

        assert self.keys(app, post, general) == {'any', 'local', 'popular'}

    def test_a_remote_media_community(self, app, live):
        tube = make_instance('tube.example', software='PeerTube')
        channel = make_community('films', host='tube.example')
        channel.ap_id = 'films@tube.example'
        channel.instance_id = tube.id
        channel.show_popular = False
        db.session.commit()
        post = make_post(channel, live.author, 'https://tube.example/videos/1')

        assert self.keys(app, post, channel) == {'any', 'media'}

    @pytest.mark.parametrize('columns', [
        {'status': POST_STATUS_REVIEWING}, {'deleted': True}, {'visibility': 'unlisted'}])
    def test_a_post_no_feed_would_list_wakes_nothing(self, app, live, columns):
        assert self.keys(app, live.toot(**columns), live.microblogs) == set()

    def test_a_backfill_wakes_nothing(self, app, live):
        assert self.keys(app, live.toot(), live.microblogs, backfill=True) == set()


class TestHomeLiveRules:

    @pytest.mark.parametrize('view_filter, key', [
        ('subscribed', 'any'), ('all', 'any'), ('local', 'local'), ('popular', 'popular'), ('media', 'media')])
    def test_home_live_key(self, view_filter, key):
        from app.community.live import home_live_key

        assert home_live_key(view_filter) == key

    @pytest.mark.parametrize('logged_in, view_filter, page, tag, expected', [
        (True, 'subscribed', 0, '', True),
        (True, 'all', 0, '', True),
        (False, 'all', 0, '', False),
        (True, 'moderating', 0, '', False),
        (True, 'all', 1, '', False),
        (True, 'all', 0, 'news', False),
    ])
    def test_home_live_available(self, live, logged_in, view_filter, page, tag, expected):
        from flask_login import AnonymousUserMixin

        from app.community.live import home_live_available

        user = live.viewer if logged_in else AnonymousUserMixin()
        assert home_live_available(user, view_filter, page, tag) is expected
```

`make_instance` is already imported in this file. Check that `PeerTube` lower-cased is in `app.discovery.MEDIA_SOFTWARE` (`grep -n MEDIA_SOFTWARE app/discovery/__init__.py`). If the stored spelling differs, use the exact member.

- [ ] **Step 2: Run to verify they fail**

Run: `./run_tests.sh tests/test_microblog_live.py -q`
Expected:
- the new tests FAIL with `ImportError` (`live_feed_keys`, `home_live_key`, `home_live_available`);
- the piefed cases FAIL on `is_live_community`;
- the changed publish tests FAIL because the old code publishes `live:microblogs`.

- [ ] **Step 3: Implement**

In `app/community/live.py`, replace the constants, `is_live_community` and `announce_live_post`, and add the home helpers. The final module body, below the docstring and imports, is:

```python
from app.discovery import MEDIA_SOFTWARE

LIVE_COMMUNITY = 'microblogs'
LIVE_KEY_PATTERN = r'^(any|local|popular|media|community:\d+)$'
# A backfilled post is old but gets a new, high id; this window keeps it out of the stream
# while still admitting a post that federated in a few minutes late.
LIVE_WINDOW = timedelta(hours=1)
LIVE_LIMIT = 40
HOME_LIVE_FILTERS = ('subscribed', 'local', 'popular', 'media', 'all')


def _software(community) -> str:
    """The community's instance software, lower-cased; '' when the instance row is missing (D1007)."""
    instance = community.instance
    return (instance.software or '').lower() if instance is not None else ''


def is_live_community(community) -> bool:
    """A `microblogs` community that is local, or hosted on another PieFed instance."""
    if community.name != LIVE_COMMUNITY:
        return False
    return community.is_local() or _software(community) == 'piefed'


def live_available(community, user, content_type: str, page: int) -> bool:
    return user.is_authenticated and content_type == 'posts' and page == 1 and is_live_community(community)


def home_live_available(user, view_filter: str, page: int, tag: str) -> bool:
    return user.is_authenticated and view_filter in HOME_LIVE_FILTERS and page == 0 and not tag


def home_live_key(view_filter: str) -> str:
    """Subscribed includes followed authors in any community, so it wakes on every post, as All does."""
    return 'any' if view_filter in ('subscribed', 'all') else view_filter


def live_posts(posts_query, after: int) -> list:
    (unchanged)


def live_feed_keys(post, community, backfill: bool) -> set:
    """The Live feeds a stored post belongs to (spec Amendment A, "Wake-up keys")."""
    if backfill or post.visibility != VISIBILITY_PUBLIC or post.status <= POST_STATUS_REVIEWING or post.deleted:
        return set()
    keys = {'any'}
    if is_live_community(community):
        keys.add(f'community:{community.id}')
    if community.instance_id == 1 and community.name != LIVE_COMMUNITY:
        keys.add('local')
    if community.show_popular:
        keys.add('popular')
    if _software(community) in MEDIA_SOFTWARE:
        keys.add('media')
    return keys


def announce_live_post(post, community, backfill: bool) -> None:
    """Wake every open Live view this post belongs to. Payloads are empty: each viewer then
    fetches its own filtered posts, so a broadcast reveals nothing a filter would hide."""
    if not current_app.config['NOTIF_SERVER']:
        return
    for key in sorted(live_feed_keys(post, community, backfill)):
        try:
            utils.publish_sse_event(f'live:{key}', '{}')
        except Exception as exc:
            current_app.logger.warning(f'Live feed wake-up {key} not sent: {exc}')
```

(Keep `live_posts` exactly as it is.) Remove `LIVE_CHANNEL`. `grep -rn LIVE_CHANNEL app tests` must return nothing afterwards.

Two notes:
- `community.instance` can be `None` for a remote community with no instance row (D1007); `_software` covers that. Add this test to `TestLiveCapableCommunities`:

```python
    def test_a_remote_microblogs_with_no_instance_row_is_not_live(self, app, live):
        from app.community.live import is_live_community, live_feed_keys

        community = piefed_microblogs()
        community.instance_id = 999999
        db.session.commit()
        post = make_post(community, live.author, 'https://piefed.social/post/9', microblog=True)
        with app.test_request_context():
            assert not is_live_community(community)
            assert 'media' not in live_feed_keys(post, community, False)
```

  If the foreign key refuses an instance id with no row, use `community.instance_id = None` instead.
- `make_community` sets `instance_id=1`. The local microblogs community has `instance_id == 1`, so the `community.name != LIVE_COMMUNITY` check is what keeps it out of `local`. A *remote* `microblogs` community has `instance_id != 1`, so it never matches `local` either way.

In `app/templates/community/community.html`, change the `data-sse-url` line to:

```html
                             data-sse-url="{{ notif_server ~ '/live/stream?feed=community:' ~ community.id if notif_server else '' }}"
```

- [ ] **Step 4: Run to verify they pass**

Run: `./run_tests.sh tests/test_microblog_live.py tests/test_ap_peer_source_and_timestamps.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/community/live.py app/templates/community/community.html tests/test_microblog_live.py
git commit -m "feat: Live covers PieFed microblogs communities, and a stored post wakes every feed it belongs to

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: FastAPI accepts every Live feed key

**Files:**
- Modify: `fastapi_server.py` (~L45, the `live_stream` route ~L122)
- Test: `tests/test_live_stream_server.py`

**Interfaces:**
- Consumes: the key pattern string from Task 1, `^(any|local|popular|media|community:\d+)$`. fastapi_server must not import `app`, so it holds its own copy of the pattern. The Python test asserts the two copies are equal.
- Produces: `LIVE_FEED_PATTERN` (a compiled regex) in `fastapi_server`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_live_stream_server.py`:

```python
@pytest.mark.parametrize('feed', ['any', 'local', 'popular', 'media', 'community:12'])
def test_every_live_key_is_a_known_feed(feed):
    response = asyncio.run(fastapi_server.live_stream(feed))

    assert response.media_type == 'text/event-stream'


@pytest.mark.parametrize('feed', ['microblogs', 'community:x', 'community:', '../x', 'any ', 'community:12:3'])
def test_anything_else_is_404(feed):
    response = TestClient(fastapi_server.app).get('/live/stream', params={'feed': feed})

    assert response.status_code == 404


def test_the_server_and_the_app_agree_on_the_key_pattern():
    from app.community.live import LIVE_KEY_PATTERN

    assert fastapi_server.LIVE_FEED_PATTERN.pattern == LIVE_KEY_PATTERN
```

Replace every remaining `'microblogs'` feed name in this file's existing tests with `'community:1'`, and every `'live:microblogs'` channel with `'live:community:1'`. Leave the test that adds a colliding id to `connected_clients` as it is, but use the new key in both places.

- [ ] **Step 2: Run to verify they fail**

Run: `./run_tests.sh tests/test_live_stream_server.py -q`
Expected: the new key tests FAIL with 404 for `any`, `local`, `popular`, `media` and `community:12`, and an `AttributeError` for `LIVE_FEED_PATTERN`.

- [ ] **Step 3: Implement**

In `fastapi_server.py`, add `import re` with the imports, then replace `LIVE_FEEDS = {"microblogs"}` with:

```python
# Must equal app.community.live.LIVE_KEY_PATTERN (a test pins it); this server does not import the app.
LIVE_FEED_PATTERN = re.compile(r'^(any|local|popular|media|community:\d+)$')
```

In `live_stream`, change `if feed not in LIVE_FEEDS:` to `if not LIVE_FEED_PATTERN.fullmatch(feed):`.

- [ ] **Step 4: Run to verify they pass**

Run: `./run_tests.sh tests/test_live_stream_server.py tests/test_caddyfile_routing.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add fastapi_server.py tests/test_live_stream_server.py
git commit -m "feat: the notification server streams every Live feed key, not only the microblogs community

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Home feed source, newer-than cursor, and per-feed rate limit keys

**Files:**
- Modify: `app/main/routes.py` (`home_page`, L99-152)
- Modify: `app/utils.py` (`get_deduped_post_ids`, L4715)
- Modify: `app/community/routes.py` (the `live_posts_fragment` limiter line, ~L907)
- Test: `tests/test_home_live.py` (new), `tests/test_microblog_live.py`

**Interfaces:**
- Produces:
  - `home_feed_source(view_filter: str) -> tuple[list[int], str | None]` in `app/main/routes.py`, for the current viewer.
  - `get_deduped_post_ids(result_id, community_ids, sort, hashtag='', include_following=False, community_sql=None, newer_than: tuple[int, datetime] | None = None) -> list[int]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_home_live.py`:

```python
"""Home Live (spec Amendment A): the home feed source, the newer-than cursor, the fragment and the page."""
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.models import Site
from app.utils import utcnow
from tests.factories import (make_community, make_community_member, make_post,
                             make_user_block)


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def teaser_ids(html):
    return [int(found) for found in re.findall(r'id="post_(\d+)"', html)]


@pytest.fixture
def home(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    g.site.private_instance = False
    community = make_community('homeland')
    community.show_all = True
    community.show_popular = True
    db.session.commit()
    reader = api_baseline.user3
    author = api_baseline.user2
    make_community_member(reader, community)
    counter = iter(range(1, 10_000))

    def post(**columns):
        created = make_post(community, author, f'https://test.piefed.local/h/{next(counter)}')
        for name, value in columns.items():
            setattr(created, name, value)
        db.session.commit()
        return created

    return SimpleNamespace(client=app.test_client(), community=community, reader=reader, author=author,
                           post=post, baseline=api_baseline)


class TestNewerThan:

    def ids(self, app, home, after, view_filter='all'):
        from flask_login import login_user

        from app.main.routes import home_feed_source
        from app.utils import get_deduped_post_ids

        with app.test_request_context():
            login_user(home.reader)
            community_ids, community_sql = home_feed_source(view_filter)
            return get_deduped_post_ids('', community_ids, 'new', include_following=view_filter == 'subscribed',
                                        community_sql=community_sql,
                                        newer_than=(after, utcnow() - timedelta(hours=1)))

    def test_only_posts_after_the_cursor_and_inside_the_window(self, app, home):
        seen = home.post()
        newer = home.post()
        home.post(posted_at=utcnow() - timedelta(hours=2))

        assert self.ids(app, home, seen.id) == [newer.id]

    def test_the_viewers_blocks_still_apply(self, app, home):
        kept = home.post()
        make_user_block(home.reader, home.author)

        assert kept.id not in self.ids(app, home, 0)

    @pytest.mark.parametrize('view_filter', ['subscribed', 'local', 'popular', 'all'])
    def test_each_tab_sees_its_own_post(self, app, home, view_filter):
        post = home.post()

        assert post.id in self.ids(app, home, 0, view_filter)

    def test_media_does_not_see_a_local_discussion_post(self, app, home):
        post = home.post()

        assert post.id not in self.ids(app, home, 0, 'media')

```

Add the per-feed rate-limit test to `tests/test_microblog_live.py` as `TestLiveFragment.test_another_community_feed_has_its_own_budget`. The suite app is built with `RATELIMIT_ENABLED=False`, so copy the exact limited-app technique from `tests/test_microblog_live.py::TestLiveFragment::test_the_thirteenth_request_in_a_minute_is_refused` (read it first) and apply it as follows:
- Make 12 requests to `/community/microblogs/live/posts?after=0` for one viewer. All 12 must be non-429.
- The 13th request to the same URL is 429.
- One request to `/community/microblogs@piefed.social/live/posts?after=0` (a PieFed microblogs community created as in Task 1's `piefed_microblogs`) is **not** 429.

The `live` fixture already provides the local `microblogs` community. `piefed_microblogs` is defined in Task 1 at module level in the same file.

The pure-move proof for `home_feed_source` is Step 4's run of the existing `tests/test_main_front_page.py` and `tests/test_front_page_media_filter.py`, unchanged.

- [ ] **Step 2: Run to verify they fail**

Run: `./run_tests.sh tests/test_home_live.py -q`
Expected: FAIL with `ImportError: cannot import name 'home_feed_source'` and `TypeError: ... unexpected keyword argument 'newer_than'`. Also run `./run_tests.sh tests/test_microblog_live.py::TestLiveFragment -q`: the new rate test fails because both URLs share `live_posts:<uid>`.

- [ ] **Step 3: Implement**

`app/utils.py`, in `get_deduped_post_ids`:
- Add the keyword argument `newer_than=None` to the signature.
- Directly before the `# sorting` comment, add:

```python
    # Live views (app/community/live.py): only posts newer than the client's cursor, inside the window.
    if newer_than is not None:
        post_id_where.append('p.id > :live_after AND p.posted_at > :live_since ')
        params['live_after'], params['live_since'] = newer_than
```

`app/main/routes.py`:
- Move the block in `home_page`, from `# view filter - subscribed/local/all` through `community_ids = list(community_ids)`, into a new module-level function directly above `home_page`.
- Keep `ensure_rss_token(current_user)` and the `enable_mod_filter` computation in `home_page`. `enable_mod_filter` still needs `modded_communities`, so compute it in `home_page` with `moderating_communities_ids(current_user.id) if current_user.is_authenticated else []`.

```python
def home_feed_source(view_filter: str):
    """The communities a home tab draws from, for the current viewer, as get_deduped_post_ids
    takes them: (community_ids, community_sql). Shared by the home page and its Live fragment."""
    (moved block, unchanged, minus ensure_rss_token)
    return list(community_ids), community_sql
```

`home_page` then reads:

```python
    if current_user.is_authenticated:
        ensure_rss_token(current_user)  # so a private rss feed can be generated
        modded_communities = moderating_communities_ids(current_user.id)
    else:
        modded_communities = []
    enable_mod_filter = len(modded_communities) > 0
    community_ids, community_sql = home_feed_source(view_filter)
```

followed by the unchanged `post_ids = get_deduped_post_ids(...)` call. Read the moved block carefully: its `moderating` branch uses `modded_communities`, so the helper computes its own copy.

`app/community/routes.py`: change the community fragment limiter to:

```python
@limiter.limit('12/minute', key_func=lambda: f'live:{current_user.id}:community:{request.view_args["actor"]}')
```

- [ ] **Step 4: Run to verify they pass**

Run: `./run_tests.sh tests/test_home_live.py tests/test_main_front_page.py tests/test_front_page_media_filter.py tests/test_microblog_live.py -q`
Expected: all PASS. The two existing front-page files pass unchanged, which is the proof that the move is pure.

- [ ] **Step 5: Commit**

```bash
git add app/main/routes.py app/utils.py app/community/routes.py tests/test_home_live.py tests/test_microblog_live.py
git commit -m "refactor: the home tabs' community source is one function; feed query takes a Live cursor; Live rate limits are per feed

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: The home Live fragment

**Files:**
- Modify: `app/main/routes.py` (new route after `index`)
- Test: `tests/test_home_live.py`

**Interfaces:**
- Consumes:
  - `home_feed_source` and `newer_than` (Task 3).
  - `HOME_LIVE_FILTERS`, `LIVE_WINDOW` and `LIVE_LIMIT` from `app.community.live` (Task 1).
  - The existing `community/_live_posts.html` template.
- Produces: `GET /home/live_posts/<view_filter>?after=<int>`, endpoint `main.home_live_posts`, with the same response contract as the community fragment.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_home_live.py`:

```python
class TestHomeLiveFragment:

    def url(self, view_filter='all', after=0):
        return f'/home/live_posts/{view_filter}?after={after}'

    @pytest.mark.parametrize('view_filter', ['subscribed', 'local', 'popular', 'all'])
    def test_new_posts_come_back_with_the_cursor(self, home, view_filter):
        post = home.post()
        login(home.client, home.reader)

        response = home.client.get(self.url(view_filter))

        assert response.status_code == 200
        assert post.id in teaser_ids(response.get_data(as_text=True))
        assert response.headers['X-Live-Cursor'] == str(post.id)

    def test_media_with_nothing_new_is_204(self, home):
        home.post()
        login(home.client, home.reader)

        assert home.client.get(self.url('media')).status_code == 204

    def test_nothing_new_is_204(self, home):
        latest = home.post()
        login(home.client, home.reader)

        response = home.client.get(self.url(after=latest.id))

        assert response.status_code == 204 and response.get_data() == b''

    def test_at_most_forty(self, home):
        for _ in range(41):
            home.post()
        login(home.client, home.reader)

        assert len(teaser_ids(home.client.get(self.url()).get_data(as_text=True))) == 40

    @pytest.mark.parametrize('view_filter', ['moderating', 'nosuch'])
    def test_other_filters_are_404(self, home, view_filter):
        login(home.client, home.reader)

        assert home.client.get(self.url(view_filter)).status_code == 404

    @pytest.mark.parametrize('query', ['', '?after=', '?after=abc'])
    def test_a_bad_cursor_is_400(self, home, query):
        login(home.client, home.reader)

        assert home.client.get(f'/home/live_posts/all{query}').status_code == 400

    def test_anonymous_is_sent_to_log_in(self, home):
        response = home.client.get(self.url())

        assert response.status_code == 302 and '/auth/login' in response.headers['Location']

    def test_subscribed_includes_a_followed_authors_post_outside_show_all(self, home):
        from tests.factories import make_follow

        hidden = make_community('quietplace')
        hidden.show_all = False
        db.session.commit()
        post = make_post(hidden, home.author, 'https://test.piefed.local/q/1')
        make_follow(home.reader, home.author)
        login(home.client, home.reader)

        assert post.id in teaser_ids(home.client.get(self.url('subscribed')).get_data(as_text=True))
```

Check `make_follow(local_user, remote_user, ...)` in `tests/factories.py:605` before relying on it. The home feed's follow source (`FOLLOWED_AUTHOR_SQL`) needs `current_user.num_following > 0`, so set `home.reader.num_following = 1` and commit if the factory doesn't. If the author has to be remote for that SQL, create a remote author with `make_user(make_instance('remote.example'), 'followed')` and post as them.

Add the per-feed rate-limit test, using the same limited-app technique as Task 3:
- 13 requests to `/home/live_posts/all?after=0`: the 13th is 429.
- One request to `/home/live_posts/local?after=0`: it is not 429.

- [ ] **Step 2: Run to verify they fail**

Run: `./run_tests.sh tests/test_home_live.py::TestHomeLiveFragment -q`
Expected: FAIL with 404 for every valid filter, because the route does not exist.

- [ ] **Step 3: Implement**

In `app/main/routes.py`, add to the imports:

```python
from app.community.live import HOME_LIVE_FILTERS, LIVE_LIMIT, LIVE_WINDOW, home_live_available, home_live_key
```

(`home_live_available` and `home_live_key` are used in Task 5. Importing them now is fine, but if the linter flags unused imports, add them in Task 5 instead.) Check the cycle: `app.community.live` imports `app.models` and `app.utils`, not `app.main`, so there is none.

Add after `index`:

```python
@bp.route('/home/live_posts/<view_filter>', methods=['GET'])
@login_required
@limiter.limit('12/minute', key_func=lambda: f'live:{current_user.id}:{request.view_args["view_filter"]}')
def home_live_posts(view_filter):
    """New teasers for a home tab's Live view, filtered for the viewer exactly as the tab is.
    204 when nothing is new; the client advances its cursor from X-Live-Cursor."""
    if view_filter not in HOME_LIVE_FILTERS:
        abort(404)
    after = request.args.get('after', type=int)
    if after is None:
        abort(400)

    community_ids, community_sql = home_feed_source(view_filter)
    post_ids = get_deduped_post_ids('', community_ids, 'new', include_following=view_filter == 'subscribed',
                                    community_sql=community_sql,
                                    newer_than=(after, utcnow() - LIVE_WINDOW))[:LIVE_LIMIT]
    if not post_ids:
        return '', 204

    user_id = current_user.get_id()
    response = make_response(render_template(
        'community/_live_posts.html', posts=post_ids_to_models(post_ids, 'new'), sort='new',
        show_post_community=True, low_bandwidth=request.cookies.get('low_bandwidth', '0') == '1',
        content_filters=user_filters_home(current_user.id),
        recently_upvoted=recently_upvoted_posts(current_user.id),
        recently_downvoted=recently_downvoted_posts(current_user.id),
        communities_banned_from_list=communities_banned_from(current_user.id),
        reported_posts=reported_posts(user_id, user_id in g.admin_ids), user_notes=user_notes(user_id),
        joined_communities=joined_or_modding_communities(user_id),
        moderated_community_ids=moderating_communities_ids(user_id), user_pronouns=user_pronouns()))
    response.headers['X-Live-Cursor'] = str(max(post_ids))
    return response
```

Confirm that every name used here is already imported in `app/main/routes.py`, with `grep -n "abort\|recently_upvoted_posts\|communities_banned_from\|user_pronouns\|utcnow" app/main/routes.py | head`. Add any missing name to the existing import lines, never inside the function.

- [ ] **Step 4: Run to verify they pass**

Run: `./run_tests.sh tests/test_home_live.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/main/routes.py tests/test_home_live.py
git commit -m "feat: a home tab's Live fragment returns new posts filtered exactly as the tab is

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: The home Live page, nav entry and shared Live bar

**Files:**
- Create: `app/templates/_live_bar.html`
- Modify:
  - `app/templates/community/community.html` (~L153-158)
  - `app/templates/index.html` (L29-45)
  - `app/templates/_home_nav.html` (after the New `<option>` ~L6-8, and after the New button ~L95-97)
  - `app/main/routes.py` (`index` and `home_page`)
- Test: `tests/test_home_live.py`

**Interfaces:**
- Consumes: `home_live_available`, `home_live_key` (Task 1), and `main.home_live_posts` (Task 4).
- Produces: the same DOM contract `live_feed.js` reads: `#live_feed` with `data-posts-url`, `data-cursor`, `data-sse-url`, `data-str-live`, `data-str-paused` and `data-str-new-posts`, plus `#live_status`, `#live_pill`, and the module script.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_home_live.py`:

```python
class TestHomeLivePage:

    def test_the_live_entry_shows_for_a_logged_in_reader(self, home):
        login(home.client, home.reader)

        assert '/home/live/all' in home.client.get('/home/new/all').get_data(as_text=True)

    def test_no_live_entry_for_anonymous(self, home):
        assert '/home/live/' not in home.client.get('/home/new/all').get_data(as_text=True)

    @pytest.mark.parametrize('view_filter, key', [
        ('subscribed', 'any'), ('local', 'local'), ('popular', 'popular'), ('media', 'media'), ('all', 'any')])
    def test_the_live_page_carries_the_client_contract(self, app, home, monkeypatch, view_filter, key):
        monkeypatch.setitem(app.config, 'NOTIF_SERVER', 'https://notifs.example')
        post = home.post()
        login(home.client, home.reader)

        html = home.client.get(f'/home/live/{view_filter}').get_data(as_text=True)

        assert 'id="live_feed"' in html
        assert f'data-posts-url="/home/live_posts/{view_filter}"' in html
        assert f'data-sse-url="https://notifs.example/live/stream?feed={key}"' in html
        assert 'id="live_status"' in html and 'id="live_pill"' in html and 'js/live_feed.js' in html
        assert 'id="auto-reload"' not in html
        if view_filter in ('local', 'popular', 'all', 'subscribed'):
            assert f'data-cursor="{post.id}"' in html

    def test_no_instance_stickies_in_live(self, home):
        sticky = home.post(instance_sticky=True)
        login(home.client, home.reader)

        assert sticky.id not in teaser_ids(home.client.get('/home/live/all').get_data(as_text=True))

    @pytest.mark.parametrize('path, as_reader', [
        ('/home/live/all', False),
        ('/home/live/moderating', True),
        ('/home/live/all?page=1', True),
        ('/home/live/all?tag=news', True),
    ])
    def test_live_falls_back_to_new_where_unavailable(self, home, path, as_reader):
        if as_reader:
            login(home.client, home.reader)

        response = home.client.get(path)

        assert response.status_code in (200, 302)
        assert 'id="live_feed"' not in response.get_data(as_text=True)

    def test_older_posts_continue_in_new(self, app, home, monkeypatch):
        monkeypatch.setitem(app.config, 'PAGE_LENGTH', 2)
        for _ in range(3):
            home.post()
        login(home.client, home.reader)

        html = home.client.get('/home/live/all').get_data(as_text=True)

        assert 'Older posts' in html and '/home/new/all?page=1' in html

    def test_the_community_page_still_renders_the_shared_live_bar(self, client, app):
        # The community page includes _live_bar.html; tests/test_microblog_live.py's
        # TestLivePage already asserts its ids. This test only pins that both pages use the include.
        import pathlib
        templates = pathlib.Path(app.root_path) / 'templates'
        assert "_live_bar.html" in (templates / 'community' / 'community.html').read_text()
        assert "_live_bar.html" in (templates / 'index.html').read_text()
```

If anonymous `/home/live/all` redirects because of private-instance gating, the `home` fixture sets `private_instance = False`, so expect 200. The `(200, 302)` check covers either.

- [ ] **Step 2: Run to verify they fail**

Run: `./run_tests.sh tests/test_home_live.py::TestHomeLivePage -q`
Expected: FAIL. There is no Live entry and no `#live_feed`, and `sort='live'` hits no sort branch in `get_deduped_post_ids`.

- [ ] **Step 3: Implement**

`app/templates/_live_bar.html` (new). Move the block out of `community.html`:

```html
{# The Live view's status badge and its "new posts" pill (fixed at the bottom of the window,
   clear of the sticky site navbar). Read by static/js/live_feed.js. #}
<div class="d-flex align-items-center gap-2 py-1">
    <span id="live_status" class="badge text-bg-success" aria-live="polite">{{ _('Live') }}</span>
    <button type="button" id="live_pill" class="btn btn-primary btn-sm position-fixed bottom-0 start-50 translate-middle-x mb-3 z-3 shadow" hidden></button>
</div>
```

In `community/community.html`, replace the inner `<div class="d-flex ...">...</div>` of the `{% if live -%}` block with `{% include '_live_bar.html' %}`.

`app/main/routes.py`, `index`: after `view_filter` is resolved and before the ETag line, add:

```python
    page = request.args.get('page', 0, type=int)
    tag = request.args.get('tag', '')
    live = sort == 'live' and home_live_available(current_user, view_filter, page, tag)
    if sort == 'live' and not live:
        sort = 'new'
```

Pass `page=page`, `tag=tag` and the new `live=live` to `home_page`, and add `live` as its last parameter.

In `home_page`:
- **Query.** Use `query_sort = 'new' if live else sort` for `get_deduped_post_ids(...)` and `post_ids_to_models(...)`. Keep `sort` for the template so the nav highlights Live.
- **Stickies.** `instance_stickies = get_instance_stickies(...) if page == 0 and not live else []`.
- **Pagination.** `next_url`: when `live`, use `url_for('main.index', page=page + 1, sort='new', view_filter=view_filter)` and drop `result_id`. `prev_url` stays as is; on page 0 it is `None`.
- **Template variables.** Pass these to the full-page `render_template('index.html', ...)`:
  - `live=live`
  - `live_cursor=max((post.id for post in posts), default=0) if live else 0`
  - `live_key=home_live_key(view_filter) if live else ''`
  - `reload_url=None if live else reload_url(sort, view_filter)`

`app/templates/index.html`: replace the `.post_list` div (L29-31) with:

```html
        {% if live -%}
            {% include '_live_bar.html' %}
        {% endif -%}
        <div class="post_list h-feed hide_flair" {% if live %}id="live_feed"
             data-posts-url="{{ url_for('main.home_live_posts', view_filter=view_filter) }}"
             data-cursor="{{ live_cursor }}"
             data-sse-url="{{ notif_server ~ '/live/stream?feed=' ~ live_key if notif_server else '' }}"
             data-str-live="{{ _('Live') }}" data-str-paused="{{ _('Paused') }}"
             data-str-new-posts="{{ _('New posts: %(num)s', num='%d') }}"
             {%- elif reload_url %}id="auto-reload" hx-get="{{ reload_url }}" hx-trigger="refreshFragment" hx-swap="innerHTML" {% endif %}>
            {% include "index_fragment.html" %}
        </div>
        {% if live -%}
            <script type="module" src="{{ url_for('static', filename='js/live_feed.js', changed=getmtime('js/live_feed.js')) }}" nonce="{{ nonce }}"></script>
        {% endif -%}
```

In the pagination `<nav>`, change the next label to `{{ _('Older posts') if live else _('Next page') }}`.

`app/templates/_home_nav.html`:
- After the New `<option>` (which ends `</option>` after `{{ _('New') }}`), add:

```html
        {% if current_user.is_authenticated and view_filter in ('subscribed', 'local', 'popular', 'media', 'all') -%}
        <option value="/home/live/{{ view_filter }}" aria-label="{{ _('Live feed of new posts') }}" {{ 'selected' if sort == 'live' }}>
            {{ _('Live') }}
        </option>
        {% endif -%}
```

- After the New button's closing `</a>` (the `<a href="/home/new/{{ view_filter }}" class="btn ...">` near L95), add:

```html
{% if current_user.is_authenticated and view_filter in ('subscribed', 'local', 'popular', 'media', 'all') -%}<a href="/home/live/{{ view_filter }}" class="btn {{ 'btn-primary' if sort == 'live' else 'btn-outline-secondary' }}" rel="nofollow noindex" title="{{ _('Live feed of new posts') }}">
        {{ _('Live') }}
    </a>{% endif -%}
```

Keep the existing inline whitespace style. The buttons there are concatenated with no whitespace between `</a>` and `<a`, so the button group renders without gaps. Do not edit `app/templates/themes/dillo/_home_nav.html`.

- [ ] **Step 4: Run to verify they pass**

Run: `./run_tests.sh tests/test_home_live.py tests/test_microblog_live.py tests/test_main_front_page.py tests/test_front_page_media_filter.py tests/test_dillo_content_warning.py tests/test_dillo_video_teaser.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/templates/_live_bar.html app/templates/community/community.html app/templates/index.html app/templates/_home_nav.html app/main/routes.py tests/test_home_live.py
git commit -m "feat: the home page has a Live view for its Subscribed, Local, Popular, Media and All tabs

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Gates and browser verification

**Files:** none new. Fix whatever the gates find, test-first, in the owning task's files.

- [ ] **Step 1: Full suite with coverage**

Run: `./run_tests.sh tests/ --cov=app --cov=fastapi_server --cov-branch --cov-report=json:coverage.json -q`
Expected: 0 failed, and `npm run test:js` (which `run_tests.sh` runs first) passes at 100%.

- [ ] **Step 2: Changed lines and branches**

Run: `python3 tests/check_changed_line_coverage.py coverage.json 25baae1ed --branches app/ fastapi_server.py`
Expected: exit 0. `25baae1ed` is the Amendment A commit.

- [ ] **Step 3: Floors and the inline-import ratchet**

Run: `python3 tests/check_coverage_floors.py coverage.json coverage_floors.ini`
Expected: exit 0. `tests/test_no_inline_imports.py` already ran in Step 1, and it must have passed.

- [ ] **Step 4: Browser (verification, not a gate)**

On the local stack (`127.0.0.1:8030`), set `NOTIF_SERVER` temporarily as in the previous run, then restore `.env.docker`.
1. Rebuild web, celery, celery_background and piefednotifs **one service at a time** (`podman-compose build <service>`). The previous run showed a combined build silently skipping `piefednotifs`.
2. Check each image's creation time (`podman images`).
3. Recreate the containers with `podman-compose up -d --force-recreate --no-deps <service>`.

Then, logged in, check in the browser:
1. `/home/live/all`: a new post in a `show_all` community appears, with the fetch driven by SSE.
2. `/home/live/popular`: a new post in a non-popular community does **not** wake it (no fetch within 5 s).
3. A PieFed remote microblogs community page (create one locally if none exists) shows Live and inserts a new post.
4. Hold behind the bottom pill, then click to insert, on a home Live page.

Record each result. Fix any defect test-first and re-run Steps 1-3.

- [ ] **Step 5: Commit any fixes**

```bash
git add -A && git commit -m "test: cover the last home Live lines the gates found

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
