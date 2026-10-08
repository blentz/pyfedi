# Microblog Server Views Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `/c/microblogs@<host>` shows the stored microblog posts whose author is on `<host>`, with every sort and a Live view that wakes on that server's posts.

**Architecture:** A server view is the local `microblogs` community rendered by the existing `show_community`, with one extra query filter, `Post.instance_id == instance.id`. Resolution, the wake-up key and the link helper live in `app/community/live.py`. No new tables, no migration, no outbound requests.

**Tech Stack:** Flask, SQLAlchemy, Jinja2, flask-caching (`cache.memoize`), FastAPI (`fastapi_server.py`), pytest in containers.

**Spec:** `docs/superpowers/specs/2026-10-08-microblog-instance-views-design.md`

## Global Constraints

- Python tests run only in containers: `./run_tests.sh <paths> -q`.
- No function-level imports anywhere in `app/` (`tests/test_no_inline_imports.py`).
- `app/activitypub/routes.py` imports `app.community.*` modules as modules (`import app.community.routes as community_routes`), because of a circular import. Import `app.community.live` the same way there.
- The microblogs community is the row `find_microblogging_community()` returns: `instance_id == 1`, `user_id == 1`, `name == 'microblogs'`.
- Instance id 1 is this instance. A post with `instance_id == 1` (or `None`) never belongs to a server view.
- `Community.ap_id` is stored lower-case; `community_profile` already lowercases `@` actors before looking them up.
- Server views never federate: an ActivityPub request to `/c/microblogs@<host>` stays 400.
- Nothing is fetched from the remote server to fill a view.
- Every memoized helper here uses `timeout=300`. Tests that touch them use the `fresh_cache` fixture (`tests/discovery_fixtures.py`).
- One commit per task. Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. A logged-in user opening `/c/microblogs@<unknown host>` must get the existing "not found" handling (redirect to lookup), not a crash, and must never trigger a server view. Task 2 pins it.
2. Live on a server view must only insert that server's posts: the fragment must apply the instance filter, not just the page. Task 3 pins it.
3. Pagination on a server view must stay on the server view, not fall back to `/c/microblogs`. Task 2 pins it.
4. Post links on home tabs must not cost a query per teaser for every microblog post: `server_view_actor` is memoized per instance id. Task 4 pins it.
5. A server view's ETag must differ from `/c/microblogs`, or an anonymous browser gets a stale `/c/microblogs` page from cache. Task 2 pins it.

---

## File Structure

- `app/community/live.py`: resolution (`is_local_microblogs`, `server_view_instance`, `microblog_server_view`, `server_view_actor`), the link helper (`post_community_link`), the sidebar query (`busiest_microblog_servers`), the `instance:<id>` wake-up key, `LIVE_KEY_PATTERN`.
- `fastapi_server.py`: `LIVE_FEED_PATTERN` matches `LIVE_KEY_PATTERN`.
- `app/activitypub/routes.py`: `community_profile` renders a server view.
- `app/community/routes.py`: `community_post_query` filter, `show_community(..., from_instance=None)`, `live_posts_fragment` resolves server views.
- `app/request_hooks.py`: registers `post_community_link` as a Jinja global.
- Templates: `community/community.html`, `community/_community_nav.html`, `_side_pane.html`, `post/post_teaser/_macros.html`, `themes/dillo/post/post_teaser/_macros.html`, `post/_breadcrumb_nav.html`.
- Tests: new `tests/test_microblog_server_views.py`; updates to `tests/test_microblog_live.py` and `tests/test_live_stream_server.py`.

The new test file reuses the Live fixtures. Its header, written in Task 1 and extended by later tasks:

```python
"""Per-server views of the microblogs community: /c/microblogs@<host>.

Spec: docs/superpowers/specs/2026-10-08-microblog-instance-views-design.md
"""
import re
from datetime import timedelta

import pytest
from flask import render_template_string

from app import db
from app.models import BannedInstances, Tag
from app.utils import utcnow
from tests.discovery_fixtures import fresh_cache  # noqa: F401
from tests.factories import make_community, make_instance, make_post, make_user
from tests.test_microblog_live import client, live, login, teaser_ids  # noqa: F401

pytestmark = pytest.mark.usefixtures('site', 'fresh_cache')

VIEW = '/c/microblogs@mastodon.example'


def other_toot(live, n=1):
    """A microblog in the same community by an author on another server."""
    other = make_instance(f'other{n}.example')
    author = make_user(other, f'other{n}')
    return make_post(live.microblogs, author, f'https://other{n}.example/statuses/1', microblog=True)


def real_microblogs(live):
    """A real remote community named microblogs on the test server's host."""
    community = make_community('microblogs', host='mastodon.example')
    community.ap_id = 'microblogs@mastodon.example'
    community.instance_id = live.remote.id
    db.session.commit()
    return community
```

`live` (from `tests/test_microblog_live.py`) gives: `live.viewer` (local user), `live.author` (on `live.remote`, `mastodon.example`, software mastodon), `live.microblogs` (the local microblogs community), `live.toot(**columns)` (a microblog by `live.author`).

---

### Task 1: Resolving `microblogs@<host>`

**Files:**
- Modify: `app/community/live.py`
- Create: `tests/test_microblog_server_views.py`

**Interfaces:**
- Produces:
  - `is_local_microblogs(community) -> bool`
  - `server_view_instance(host: str) -> Instance | None` (host already lower-cased)
  - `microblog_server_view(actor: str) -> Instance | None`
  - `server_view_actor(instance_id: int) -> str | None` (memoized, 300 s), e.g. `'microblogs@mastodon.example'`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_microblog_server_views.py` with the header from File Structure, then:

```python
from app.community.live import is_local_microblogs, microblog_server_view, server_view_actor


class TestResolution:

    def view(self, app, actor):
        with app.test_request_context():
            found = microblog_server_view(actor)
            return found.id if found is not None else None

    def test_a_known_server_resolves(self, app, live):
        assert self.view(app, 'microblogs@mastodon.example') == live.remote.id

    def test_case_is_ignored(self, app, live):
        assert self.view(app, ' Microblogs@Mastodon.Example ') == live.remote.id

    @pytest.mark.parametrize('actor', ['microblogs@nowhere.example', 'general@mastodon.example',
                                       'microblogs', 'microblogs@', '@mastodon.example'])
    def test_anything_else_does_not(self, app, live, actor):
        assert self.view(app, actor) is None

    def test_this_server_does_not(self, app, live):
        assert self.view(app, 'microblogs@test.piefed.local') is None

    def test_a_banned_server_does_not(self, app, live):
        db.session.add(BannedInstances(domain='mastodon.example'))
        db.session.commit()

        assert self.view(app, 'microblogs@mastodon.example') is None

    def test_a_real_community_wins(self, app, live):
        real_microblogs(live)

        assert self.view(app, 'microblogs@mastodon.example') is None

    def test_server_view_actor_names_the_view(self, app, live):
        with app.test_request_context():
            assert server_view_actor(live.remote.id) == 'microblogs@mastodon.example'

    def test_server_view_actor_is_none_where_there_is_no_view(self, app, live):
        real_microblogs(live)
        with app.test_request_context():
            assert server_view_actor(live.remote.id) is None
            assert server_view_actor(999_999) is None

    def test_only_the_local_microblogs_community_is_local_microblogs(self, app, live):
        with app.test_request_context():
            assert is_local_microblogs(live.microblogs)
            assert not is_local_microblogs(make_community('general'))
            assert not is_local_microblogs(real_microblogs(live))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_microblog_server_views.py -q`
Expected: collection error, `ImportError: cannot import name 'is_local_microblogs'`.

- [ ] **Step 3: Implement**

In `app/community/live.py`, change the imports to:

```python
from datetime import timedelta

from flask import current_app
from sqlalchemy import desc, func

from app import cache, db
from app.constants import POST_STATUS_REVIEWING, VISIBILITY_PUBLIC
from app.models import Community, Instance, Post, post_stored_hooks
from app import utils
from app.utils import instance_banned, utcnow

from app.discovery import MEDIA_SOFTWARE
```

Add after `is_live_community`:

```python
def is_local_microblogs(community) -> bool:
    """The row find_microblogging_community() returns: the community server views filter."""
    return (community.name == LIVE_COMMUNITY and community.instance_id == 1 and community.user_id == 1
            and community.ap_id is None)


def server_view_instance(host: str):
    """The Instance whose server view is /c/microblogs@<host>, else None (spec: Resolving).
    `host` is lower-cased by the caller."""
    if not host or host == current_app.config['SERVER_NAME'].lower():
        return None
    # A real community always wins: /c/microblogs@piefed.social stays PieFed's own community.
    if db.session.query(Community.id).filter(Community.ap_id == f'{LIVE_COMMUNITY}@{host}').first():
        return None
    instance = db.session.query(Instance).filter(func.lower(Instance.domain) == host).first()
    if instance is None or instance_banned(host):
        return None
    return instance


def microblog_server_view(actor: str):
    """The Instance a `microblogs@<host>` actor names, when it is a server view; else None."""
    name, at, host = actor.strip().lower().rpartition('@')
    if not at or name != LIVE_COMMUNITY:
        return None
    return server_view_instance(host)


@cache.memoize(timeout=300)
def server_view_actor(instance_id: int):
    """'microblogs@<host>' for an instance with a server view, else None. Memoized: post links
    call it once per teaser."""
    instance = db.session.get(Instance, instance_id)
    if instance is None or not instance.domain:
        return None
    host = instance.domain.lower()
    return f'{LIVE_COMMUNITY}@{host}' if server_view_instance(host) is not None else None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_microblog_server_views.py tests/test_microblog_live.py tests/test_no_inline_imports.py -q`
Expected: all pass. If importing `instance_banned` or `cache` from `live.py` raises a circular `ImportError`, report it rather than moving the import into a function.

- [ ] **Step 5: Commit**

```bash
git add app/community/live.py tests/test_microblog_server_views.py
git commit -m "feat: resolve microblogs@<host> to a server view of the microblogs community

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: The server view page

**Files:**
- Modify: `app/community/routes.py` (`community_post_query` ~294, `show_community` ~376-795)
- Modify: `app/activitypub/routes.py` (`community_profile` ~585-690; imports ~33)
- Modify: `app/templates/community/community.html` (lines ~30, ~37)
- Modify: `app/templates/community/_community_nav.html` (lines 131-238)
- Test: `tests/test_microblog_server_views.py`

**Interfaces:**
- Consumes: `microblog_server_view`, `is_local_microblogs` (Task 1).
- Produces:
  - `community_post_query(community, content_type, flair='', tag='', from_instance=None)`
  - `show_community(community, from_instance=None)`
  - Template variables `view_actor` (`'microblogs@<host>'` or `None`) and `live_sse_feed` (used by Task 3).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_microblog_server_views.py`:

```python
class TestServerViewPage:

    def test_it_lists_only_that_servers_posts(self, client, live):
        mine = live.toot()
        other_toot(live)

        html = client.get(f'{VIEW}?sort=new').get_data(as_text=True)

        assert set(teaser_ids(html)) == {mine.id}

    def test_the_title_and_heading_name_the_view(self, client, live):
        html = client.get(VIEW).get_data(as_text=True)

        assert 'microblogs@mastodon.example' in re.search(r'<title>([^<]*)</title>', html).group(1)
        assert re.search(r'<h1[^>]*>\s*microblogs@mastodon.example', html)

    def test_case_is_ignored(self, client, live):
        assert client.get('/c/Microblogs@Mastodon.Example').status_code == 200

    @pytest.mark.parametrize('path', ['/c/microblogs@nowhere.example', '/c/microblogs@test.piefed.local'])
    def test_no_view_is_404_for_a_visitor(self, client, live, path):
        assert client.get(path).status_code == 404

    def test_a_signed_in_user_with_no_view_gets_the_existing_lookup(self, client, live):
        login(client, live.viewer)

        response = client.get('/c/microblogs@nowhere.example')

        assert response.status_code == 302 and 'lookup' in response.headers['Location']

    def test_a_banned_server_is_404(self, client, live):
        live.toot()
        db.session.add(BannedInstances(domain='mastodon.example'))
        db.session.commit()

        assert client.get(VIEW).status_code == 404

    def test_a_real_community_wins(self, client, live):
        real = real_microblogs(live)
        theirs = make_post(real, live.author, 'https://mastodon.example/statuses/real', microblog=True)
        live.toot()

        html = client.get(f'{VIEW}?sort=new').get_data(as_text=True)

        assert set(teaser_ids(html)) == {theirs.id}

    def test_an_activitypub_request_is_400(self, client, live):
        response = client.get(VIEW, headers={'Accept': 'application/activity+json'})

        assert response.status_code == 400

    def test_a_server_with_no_posts_is_an_empty_page(self, client, live):
        response = client.get(VIEW)

        assert response.status_code == 200 and teaser_ids(response.get_data(as_text=True)) == []

    def test_pagination_stays_on_the_view(self, client, live):
        for _ in range(3):
            live.toot()
        live.viewer.page_length = 2
        db.session.commit()
        login(client, live.viewer)

        html = client.get(f'{VIEW}?sort=new').get_data(as_text=True)

        assert re.search(r'href="/c/microblogs(@|%40)mastodon.example\?[^"]*page=2', html)

    def test_join_and_post_controls_are_hidden(self, client, live):
        login(client, live.viewer)
        assert '/community/microblogs/submit' in client.get('/c/microblogs').get_data(as_text=True)

        html = client.get(VIEW).get_data(as_text=True)

        assert '/community/microblogs/submit' not in html
        assert '/community/microblogs/subscribe' not in html

    def test_a_tag_filter_is_not_applied(self, client, live):
        mine = live.toot()
        db.session.add(Tag(name='x'))
        db.session.commit()

        html = client.get(f'{VIEW}?sort=new&tag=x').get_data(as_text=True)

        assert set(teaser_ids(html)) == {mine.id}

    def test_the_etag_differs_from_the_community(self, client, live):
        live.toot()

        view = client.get(f'{VIEW}?sort=new').headers['ETag']
        plain = client.get('/c/microblogs?sort=new').headers['ETag']

        assert view != plain
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_microblog_server_views.py -q`
Expected: the `TestServerViewPage` tests fail (404s and missing markup). `TestResolution` still passes.

- [ ] **Step 3: Add the query filter**

In `app/community/routes.py`, change the signature and the first query line of `community_post_query`:

```python
def community_post_query(community: Community, content_type: str, flair: str = '', tag: str = '',
                         from_instance=None):
```

and directly after `posts = Post.query.filter(Post.community_id == community.id, listable_clause(Post))` add:

```python
    if from_instance is not None:   # a server view: /c/microblogs@<host> (spec 2026-10-08)
        posts = posts.filter(Post.instance_id == from_instance.id)
```

Add one line to its docstring: `from_instance limits it to posts by authors on that instance (a server view).`

- [ ] **Step 4: Thread `from_instance` through `show_community`**

In `app/community/routes.py`:

1. Change `from app.community.live import is_live_community, live_available, live_posts` to
   `from app.community.live import LIVE_COMMUNITY, is_live_community, is_local_microblogs, live_available, live_posts`.
2. Signature: `def show_community(community: Community, from_instance=None):`
3. Directly after `tag = request.args.get('tag', '')`, add:

```python
    # A server view (/c/microblogs@<host>) is this community filtered to one author instance.
    view_actor = f'{LIVE_COMMUNITY}@{from_instance.domain.lower()}' if from_instance is not None else None
    if view_actor:
        flair = tag = ''        # a server view offers neither filter
```

4. Replace the `current_etag = ...` line with:

```python
    current_etag = (f"{community.id}{'@' + str(from_instance.id) if from_instance is not None else ''}"
                    f"{sort}{post_layout}_{hash(community.last_active)}")
```

   In the `render_template` call replace `etag=f"{community.id}{sort}{post_layout}_{hash(community.last_active)}"` with `etag=current_etag`, and replace `resp.headers.set('ETag', f"{community.id}{sort}{post_layout}_{hash(community.last_active)}")` with `resp.headers.set('ETag', current_etag)`.
5. Change `community_post_query(community, content_type, flair, tag)` to `community_post_query(community, content_type, flair, tag, from_instance=from_instance)`.
6. Directly before `if content_type == 'posts' or content_type == 'events':` that builds `next_url`, add
   `page_actor = view_actor or (community.ap_id if community.ap_id is not None else community.name)`
   and replace each of the four `actor=community.ap_id if community.ap_id is not None else community.name,` in the `next_url`/`prev_url` calls with `actor=page_actor,`.
7. In the `render_template('community/community.html', ...)` call:
   - `title=community.title` becomes `title=view_actor or community.title`
   - `community_flair=shared_community.get_comm_flair_list(community)` becomes `community_flair=[] if view_actor else shared_community.get_comm_flair_list(community)`
   - `tags=hashtags_used_in_community(community.id, content_filters)` becomes `tags=[] if view_actor else hashtags_used_in_community(community.id, content_filters)`
   - add `view_actor=view_actor,` and `live_sse_feed=f'instance:{from_instance.id}' if from_instance is not None else f'community:{community.id}',`

- [ ] **Step 5: Route `/c/microblogs@<host>` to it**

In `app/activitypub/routes.py`, next to `import app.community.routes as community_routes`, add:

```python
import app.community.live as community_live
```

In `community_profile`, in the final `else:` branch (no community found), insert before `if is_activitypub_request():`:

```python
        # A server view of the microblogs community (spec 2026-10-08). An ActivityPub request for an
        # @ path was already refused with 400 above, so this only ever renders HTML.
        server_view = community_live.microblog_server_view(actor) if '@' in actor else None
        if server_view is not None:
            return community_routes.show_community(find_microblogging_community(), from_instance=server_view)
```

- [ ] **Step 6: Template changes**

`app/templates/community/community.html`:
- Line ~30, `{{ (community.title + '@' + community.ap_domain)|shorten }}` becomes
  `{{ (view_actor or (community.title + '@' + community.ap_domain))|shorten }}`
- Line ~37, the `{{ community.title }}` directly inside `<h1 class="mt-2"  aria-live="assertive">` becomes `{{ view_actor or community.title }}`

`app/templates/community/_community_nav.html`: the `<div class="mobile_create_post">` element opens at line 131 and closes at line 238. Insert `{% if not view_actor -%}` on a new line before line 131 and `{% endif -%}` on a new line after line 238.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_microblog_server_views.py tests/test_microblog_live.py tests/test_community_show.py tests/test_no_inline_imports.py -q`
Expected: all pass. `test_join_and_post_controls_are_hidden` first asserts that the plain `/c/microblogs` page shows `/community/microblogs/submit` to a signed-in user. If that precondition fails, the link is not on that page at all: replace the `submit` pair with a link the mobile Options dropdown does show there, and say so in the report.

- [ ] **Step 8: Commit**

```bash
git add app/community/routes.py app/activitypub/routes.py app/templates/community/community.html app/templates/community/_community_nav.html tests/test_microblog_server_views.py
git commit -m "feat: /c/microblogs@<host> shows the microblogs community filtered to that server

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Live on a server view

**Files:**
- Modify: `app/community/live.py` (`LIVE_KEY_PATTERN`, `live_feed_keys`)
- Modify: `fastapi_server.py:47`
- Modify: `app/community/routes.py` (`live_posts_fragment` ~908; import from `app.activitypub.util` ~19)
- Modify: `app/templates/community/community.html` (lines ~157-159)
- Test: `tests/test_microblog_server_views.py`, `tests/test_microblog_live.py`, `tests/test_live_stream_server.py`

**Interfaces:**
- Consumes: `is_local_microblogs`, `microblog_server_view` (Task 1); `view_actor`, `live_sse_feed`, `community_post_query(..., from_instance=)` (Task 2).
- Produces: wake-up key `instance:<id>`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_microblog_server_views.py`:

```python
FRAGMENT = '/community/microblogs@mastodon.example/live/posts'


class TestServerViewLive:

    def keys(self, app, post, community):
        from app.community.live import live_feed_keys

        with app.test_request_context():
            return live_feed_keys(post, community, False)

    def test_a_remote_authors_post_wakes_its_server_view(self, app, live):
        post = live.toot()

        assert f'instance:{live.remote.id}' in self.keys(app, post, live.microblogs)

    def test_a_local_authors_post_does_not(self, app, live):
        post = make_post(live.microblogs, live.viewer, 'https://test.piefed.local/post/1', microblog=True)

        assert not any(key.startswith('instance:') for key in self.keys(app, post, live.microblogs))

    def test_a_post_in_another_community_does_not(self, app, live):
        general = make_community('general')
        post = make_post(general, live.author, 'https://mastodon.example/statuses/g')

        assert not any(key.startswith('instance:') for key in self.keys(app, post, general))

    def test_the_fragment_serves_only_that_servers_new_posts(self, client, live):
        mine = live.toot()
        other_toot(live)
        login(client, live.viewer)

        response = client.get(f'{FRAGMENT}?after=0')

        assert response.status_code == 200
        assert teaser_ids(response.get_data(as_text=True)) == [mine.id]

    def test_the_fragment_is_204_when_only_other_servers_posted(self, client, live):
        other_toot(live)
        login(client, live.viewer)

        assert client.get(f'{FRAGMENT}?after=0').status_code == 204

    def test_no_view_has_no_fragment(self, client, live):
        login(client, live.viewer)

        assert client.get('/community/microblogs@nowhere.example/live/posts?after=0').status_code == 404

    def test_the_live_page_wires_the_view(self, app, client, live, monkeypatch):
        monkeypatch.setitem(app.config, 'NOTIF_SERVER', 'https://notifs.example')
        live.toot()
        login(client, live.viewer)

        html = client.get(f'{VIEW}?sort=live').get_data(as_text=True)

        assert 'id="live_feed"' in html
        assert re.search(r'data-posts-url="/community/microblogs(@|%40)mastodon.example/live/posts"', html)
        assert f'data-sse-url="https://notifs.example/live/stream?feed=instance:{live.remote.id}"' in html
```

In `tests/test_live_stream_server.py`, add `'instance:7'` to the parametrize list of `test_every_live_key_is_a_known_feed`, and add `'instance:x'`, `'instance:'` to the list of `test_anything_else_is_404`.

In `tests/test_microblog_live.py`, update the two tests whose exact expectations now include the new key:

```python
    def test_a_new_microblogs_post_wakes_its_community_and_any(self, app, live, published):
        from app.community.live import announce_live_post

        with app.test_request_context():
            announce_live_post(live.toot(), live.microblogs, backfill=False)

        assert published == [('live:any', '{}'), (f'live:community:{live.microblogs.id}', '{}'),
                             (f'live:instance:{live.remote.id}', '{}'), 'execute']
        assert len(published.connections) == 1
```

```python
    def test_a_live_capable_community_post(self, app, live):
        assert self.keys(app, live.toot(), live.microblogs) == {
            'any', f'community:{live.microblogs.id}', f'instance:{live.remote.id}'}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_microblog_server_views.py tests/test_microblog_live.py tests/test_live_stream_server.py -q`
Expected: the new and updated tests fail; everything else passes.

- [ ] **Step 3: Implement the key and pattern**

In `app/community/live.py`:

```python
LIVE_KEY_PATTERN = r'^(any|local|popular|media|community:\d+|instance:\d+)$'
```

In `live_feed_keys`, before `return keys`:

```python
    if is_local_microblogs(community) and post.instance_id and post.instance_id != 1:
        keys.add(f'instance:{post.instance_id}')   # its author's server view (spec 2026-10-08)
```

In `fastapi_server.py`:

```python
LIVE_FEED_PATTERN = re.compile(r'^(any|local|popular|media|community:\d+|instance:\d+)$')
```

- [ ] **Step 4: Resolve server views in the fragment route**

In `app/community/routes.py`, add `find_microblogging_community` to the existing `from app.activitypub.util import ...` line (~19), and add `microblog_server_view` to the `from app.community.live import ...` line. In `live_posts_fragment`, replace:

```python
    community = actor_to_community(actor)
    if community is None or not is_live_community(community):
        abort(404)
```

with:

```python
    community = actor_to_community(actor)
    from_instance = None
    if community is None:   # perhaps a server view: microblogs@<host> (spec 2026-10-08)
        from_instance = microblog_server_view(actor)
        if from_instance is not None:
            community = find_microblogging_community()
    if community is None or not is_live_community(community):
        abort(404)
```

and change `posts_query, content_filters = community_post_query(community, 'posts')` to
`posts_query, content_filters = community_post_query(community, 'posts', from_instance=from_instance)`.

- [ ] **Step 5: Wire the page**

In `app/templates/community/community.html`, in the `#live_feed` attributes:

```html
                             data-posts-url="{{ url_for('community.live_posts_fragment', actor=view_actor or community.link()) }}"
                             data-cursor="{{ live_cursor }}"
                             data-sse-url="{{ notif_server ~ '/live/stream?feed=' ~ live_sse_feed if notif_server else '' }}"
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_microblog_server_views.py tests/test_microblog_live.py tests/test_live_stream_server.py tests/test_home_live.py -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add app/community/live.py fastapi_server.py app/community/routes.py app/templates/community/community.html tests/test_microblog_server_views.py tests/test_microblog_live.py tests/test_live_stream_server.py
git commit -m "feat: Live on a microblogs server view wakes on that server's posts

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Post links point to the author's server view

**Files:**
- Modify: `app/community/live.py` (add `post_community_link`)
- Modify: `app/request_hooks.py` (Jinja global)
- Modify: `app/templates/post/post_teaser/_macros.html:65-67`
- Modify: `app/templates/themes/dillo/post/post_teaser/_macros.html:51` (and its next two lines)
- Modify: `app/templates/post/_breadcrumb_nav.html:6`
- Test: `tests/test_microblog_server_views.py`

**Interfaces:**
- Consumes: `is_local_microblogs`, `server_view_actor` (Task 1).
- Produces: `post_community_link(post) -> str`, a Jinja global.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_microblog_server_views.py`:

```python
class TestPostLinks:

    def link(self, app, post):
        from app.community.live import post_community_link

        with app.test_request_context():
            return post_community_link(post)

    def test_a_remote_microblog_links_to_its_server_view(self, app, live):
        assert self.link(app, live.toot()) == 'microblogs@mastodon.example'

    def test_a_local_authors_microblog_links_to_the_community(self, app, live):
        post = make_post(live.microblogs, live.viewer, 'https://test.piefed.local/post/2', microblog=True)

        assert self.link(app, post) == 'microblogs'

    def test_a_server_with_a_real_community_links_to_the_community(self, app, live):
        real_microblogs(live)

        assert self.link(app, live.toot()) == 'microblogs'

    def test_a_post_in_another_community_keeps_its_link(self, app, live):
        general = make_community('general')
        post = make_post(general, live.author, 'https://mastodon.example/statuses/h')

        assert self.link(app, post) == 'general'

    def test_the_lookup_is_memoized_per_server(self):
        # Tests run with NullCache, so the cache itself is not exercised here: pin the decorator.
        from app.community.live import server_view_actor

        assert server_view_actor.cache_timeout == 300

    def test_the_post_page_breadcrumb_names_the_view(self, client, live):
        post = live.toot()

        html = client.get(f'/post/{post.id}').get_data(as_text=True)

        assert re.search(r'<a href="/c/microblogs@mastodon.example">microblogs@mastodon.example</a>', html)

    def test_a_teaser_byline_links_to_the_view(self, app, live):
        post = live.toot()

        with app.test_request_context():
            rendered = render_template_string(
                "{% from 'post/post_teaser/_macros.html' import render_title %}"
                "{{ render_title(post, show_post_community=True, request=request) }}", post=post)

        assert 'href="/c/microblogs@mastodon.example"' in rendered
        assert '>@mastodon.example</span>' in rendered
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_microblog_server_views.py -q`
Expected: `TestPostLinks` fails with `ImportError: cannot import name 'post_community_link'`.

- [ ] **Step 3: Implement the helper**

In `app/community/live.py`, after `server_view_actor`:

```python
def post_community_link(post) -> str:
    """Where a post's community link points: its author's server view for a remote microblog in
    the microblogs community, else the community itself (spec: Discovery)."""
    community = post.community
    if is_local_microblogs(community) and post.instance_id and post.instance_id != 1:
        actor = server_view_actor(post.instance_id)
        if actor:
            return actor
    return community.link()
```

In `app/request_hooks.py`, add the import at module level next to the other `app.` imports:

```python
from app.community.live import post_community_link
```

and register it next to `viewer_can_view`:

```python
    app.jinja_env.globals['post_community_link'] = post_community_link
```

If that import is circular at app start, report it rather than moving it into a function.

- [ ] **Step 4: Templates**

`app/templates/post/post_teaser/_macros.html` lines 65-67 become:

```html
    {% set community_link = post_community_link(post) -%}
    <div class="author small">{% if show_post_community -%}<a href="{{ (request.url_root + 'c/' + community_link if embed else '/c/' + community_link) }}" {{ 'target="_blank"' if embed }}>
        {% if post.community.icon_id and not low_bandwidth %}<img class="community_icon_small rounded-circle" src="{{ post.community.icon_image('tiny') }}" alt="Community icon" loading="lazy" />{% endif -%}
        {{ post.community.name }}</a><span class="community_instance text-muted {{ 'local' if post.community.is_local() and community_link == post.community.link() }}">@{{ community_link.rpartition('@')[2] if community_link != post.community.link() else post.community.ap_domain }}</span> {% endif -%}
```

Apply the same three-line change to the matching lines in `app/templates/themes/dillo/post/post_teaser/_macros.html` (line 51 and the two after it), keeping that file's own indentation.

`app/templates/post/_breadcrumb_nav.html` line 6 becomes:

```html
    {% set community_link = post_community_link(post) -%}
    <li class="breadcrumb-item"><a href="/c/{{ community_link }}">{{ community_link if community_link != post.community.link() else post.community.title + '@' + post.community.ap_domain }}</a></li>
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_microblog_server_views.py tests/test_microblog_live.py tests/test_home_live.py tests/test_no_inline_imports.py -q`
Expected: all pass. If `render_title` needs more arguments than `post`, `show_post_community` and `request` to render, pass the extra ones as `None`, the way its other callers do.

- [ ] **Step 6: Commit**

```bash
git add app/community/live.py app/request_hooks.py app/templates/post/post_teaser/_macros.html app/templates/themes/dillo/post/post_teaser/_macros.html app/templates/post/_breadcrumb_nav.html tests/test_microblog_server_views.py
git commit -m "feat: a remote microblog's community link points to its server view

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: The "Servers" sidebar on `/c/microblogs`

**Files:**
- Modify: `app/community/live.py` (add `busiest_microblog_servers`)
- Modify: `app/community/routes.py` (`show_community` render call)
- Modify: `app/templates/_side_pane.html` (~line 87)
- Test: `tests/test_microblog_server_views.py`

**Interfaces:**
- Consumes: `is_local_microblogs`, `server_view_instance` (Task 1); `view_actor` (Task 2).
- Produces: `busiest_microblog_servers(community_id: int) -> list[tuple[str, int]]` (memoized, 300 s); template variable `microblog_servers`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_microblog_server_views.py`:

```python
class TestServersSidebar:

    def servers(self, app, live):
        from app.community.live import busiest_microblog_servers

        with app.test_request_context():
            return busiest_microblog_servers(live.microblogs.id)

    def test_servers_are_ranked_by_posts_in_the_last_day(self, app, live):
        live.toot()
        live.toot()
        other_toot(live, 1)

        assert self.servers(app, live) == [('mastodon.example', 2), ('other1.example', 1)]

    def test_older_posts_do_not_count(self, app, live):
        live.toot(posted_at=utcnow() - timedelta(hours=25))

        assert self.servers(app, live) == []

    def test_local_authors_and_servers_without_a_view_are_left_out(self, app, live):
        make_post(live.microblogs, live.viewer, 'https://test.piefed.local/post/3', microblog=True)
        live.toot()
        real_microblogs(live)

        assert self.servers(app, live) == []

    def test_at_most_ten(self, app, live):
        for n in range(12):
            other_toot(live, n)

        assert len(self.servers(app, live)) == 10

    def test_the_query_is_memoized(self):
        # Tests run with NullCache: pin the decorator.
        from app.community.live import busiest_microblog_servers

        assert busiest_microblog_servers.cache_timeout == 300

    def test_the_sidebar_lists_them_on_the_community(self, client, live):
        live.toot()

        html = client.get('/c/microblogs').get_data(as_text=True)

        assert 'id="microblog_servers"' in html
        assert 'href="/c/microblogs@mastodon.example"' in html

    def test_not_on_a_server_view(self, client, live):
        live.toot()

        assert 'id="microblog_servers"' not in client.get(VIEW).get_data(as_text=True)

    def test_not_on_another_community(self, client, live):
        make_community('general')

        assert 'id="microblog_servers"' not in client.get('/c/general').get_data(as_text=True)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_microblog_server_views.py -q`
Expected: `TestServersSidebar` fails with `ImportError: cannot import name 'busiest_microblog_servers'`.

- [ ] **Step 3: Implement the query**

In `app/community/live.py`, add:

```python
SIDEBAR_SERVERS = 10


@cache.memoize(timeout=300)
def busiest_microblog_servers(community_id: int) -> list:
    """(host, posts in the last 24 h) for the servers with the most posts in the microblogs
    community, busiest first, only those with a server view (spec: Discovery)."""
    count = func.count(Post.id)
    rows = (db.session.query(func.lower(Instance.domain), count)
            .join(Instance, Instance.id == Post.instance_id)
            .filter(Post.community_id == community_id, Post.instance_id != 1, Post.deleted == False,
                    Post.posted_at > utcnow() - timedelta(hours=24))
            .group_by(func.lower(Instance.domain))
            .order_by(count.desc(), func.lower(Instance.domain))
            .limit(SIDEBAR_SERVERS * 2)      # headroom for hosts that have no view
            .all())
    return [(host, posts) for host, posts in rows if server_view_instance(host) is not None][:SIDEBAR_SERVERS]
```

- [ ] **Step 4: Pass it to the page**

In `app/community/routes.py`, add `busiest_microblog_servers` to the `from app.community.live import ...` line, and add to the `render_template('community/community.html', ...)` call:

```python
                                         microblog_servers=busiest_microblog_servers(community.id)
                                         if is_local_microblogs(community) and not view_actor else None,
```

- [ ] **Step 5: Render it**

In `app/templates/_side_pane.html`, directly before the line `<div class="card {% if not hide_community_actions -%}mt-3{% endif %}" id="about_community">`, insert:

```html
        {% if microblog_servers -%}
        <div class="card {% if not hide_community_actions -%}mt-3{% endif %}" id="microblog_servers">
            <div class="card-header">
                 <h2>{{ _('Servers') }}</h2>
            </div>
            <div class="card-body">
                <ul class="list-unstyled mb-0">
                {% for host, count in microblog_servers -%}
                    <li><a href="/c/microblogs@{{ host }}">{{ host }}</a> <span class="text-muted">{{ count }}</span></li>
                {% endfor -%}
                </ul>
            </div>
        </div>
        {% endif -%}
```

and change the about card's opening line to:

```html
        <div class="card {% if not hide_community_actions or microblog_servers -%}mt-3{% endif %}" id="about_community">
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_microblog_server_views.py tests/test_microblog_live.py tests/test_community_show.py -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add app/community/live.py app/community/routes.py app/templates/_side_pane.html tests/test_microblog_server_views.py
git commit -m "feat: /c/microblogs lists its busiest servers, each linking to its server view

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Full suite and gates

**Files:** none changed unless a gate fails.

- [ ] **Step 1: Run the full suite**

Run: `./run_tests.sh -q` (writes `coverage.json`)
Expected: all pass. A failure in a test this plan did not touch is investigated and fixed, not skipped.

- [ ] **Step 2: Run the gates on the host**

```bash
python3 tests/check_changed_line_coverage.py coverage.json $(git merge-base HEAD main) --branches app/ fastapi_server.py
python3 tests/check_coverage_floors.py coverage.json coverage_floors.ini
```

Expected: both pass. Uncovered changed lines get a test in the task's test class, in a follow-up commit.

- [ ] **Step 3: Commit any fixes**

```bash
git add -A tests app
git commit -m "test: cover the remaining microblog server view lines

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

Skip this step when nothing changed.
