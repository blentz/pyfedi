# Server Views in the Community List Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `/communities` lists every `microblogs@<host>` server view as a row, sorted and paginated with the communities, and a search for "microblogs" (or part of a host) finds them.

**Architecture:** A new module, `app/community/server_view_list.py`, builds a `UNION ALL` of the existing filtered community query (reduced to nine shared columns) and a per-instance aggregate over the microblogs community's posts. It orders and paginates the union, then hydrates each row into a `Community` or a lightweight `ServerView`. `list_communities` swaps its `order_by` + `paginate` for this. The template leaves the Join column empty on a `ServerView` row.

**Tech Stack:** Flask, SQLAlchemy 2 (`select`, `union_all`), Jinja2, pytest in containers.

**Spec:** `docs/superpowers/specs/2026-10-08-server-views-in-community-list-design.md`. It builds on `docs/superpowers/specs/2026-10-08-microblog-instance-views-design.md`.

## Global Constraints

- Python tests run only in containers: `./run_tests.sh <paths> -q`.
- No function-level imports anywhere in `app/` (`tests/test_no_inline_imports.py`).
- The microblogs community is the row `find_microblogging_community()` returns. Instance id 1 is this instance.
- Union columns, in this order: `community_id`, `instance_id`, `title`, `subscriptions_count`, `post_count`, `post_reply_count`, `last_active`, `created_at`, `active_weekly`.
- Sortable fields: `title`, `subscriptions_count`, `post_count`, `post_reply_count`, `last_active`, `created_at`, `active_weekly`. An unknown or empty `sort_by` sorts `active_weekly desc`, as `safe_order_by` does today. Ties break on `title`, then `community_id`, then `instance_id`.
- A view's title is `'microblogs@' + lower(Instance.domain)`. Its `subscriptions_count` is 0.
- The view side is dropped when `home_select == 'local'`, `subscribe_select != 'any'`, `topic_id != 0`, `language_id != 0`, `feed_id != 0`, `platform != ''`, or `nsfw == 'yes'`.
- Page sizes stay as today: 100 for a signed-in user without low bandwidth, else 50.
- Server views get no Join, Pending or Leave control, no `Community` row, and no federation.
- One commit per task, with messages ending `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. A `BannedInstances` row with a NULL domain must not empty the view side (SQL `NOT IN` against a NULL). Task 1 pins it.
2. A search containing `%` or `_` must match literally on both sides, not as a wildcard. Tasks 1 and 2 pin it.
3. Page 2 must continue exactly where page 1 stopped when communities and views tie on the sort key. Task 1 pins it.
4. A wildcard ban (`*` in a ban entry) must hide the matching view even though SQL equality misses it. Task 1 pins it.
5. The existing `/communities` behaviour must not change for the filters that drop views, and must stay a 200 for every `sort_by` value. Task 2 pins it, and the existing `tests/test_main_modlog.py::TestTheCommunityDirectory` and `tests/test_safe_order_by.py` must still pass.

---

## File Structure

- Create `app/community/server_view_list.py` for `ServerView`, `like_pattern`, `parse_sort`, `view_side_allowed`, `community_select`, `server_view_select`, `UnionPage` and `paginate_union`.
- Modify `app/main/routes.py` (`list_communities`) for the community-side search escape, the view side, and `paginate_union` in place of `order_by` + `paginate`.
- Modify `app/templates/list_communities.html` (row loop, ~line 291-313) to leave the Join column empty for a view.
- Create `tests/test_server_views_in_community_list.py`.

The test file's header, written in Task 1 and extended in Task 2:

```python
"""Server views in the All Communities list (/communities).

Spec: docs/superpowers/specs/2026-10-08-server-views-in-community-list-design.md
"""
from datetime import timedelta

import pytest

from app import db
from app.models import BannedInstances, Community, InstanceBlock
from app.utils import utcnow
from tests.discovery_fixtures import fresh_cache  # noqa: F401
from tests.factories import make_community, make_instance, make_post, make_user
from tests.test_microblog_live import client, live, login  # noqa: F401

pytestmark = pytest.mark.usefixtures('site', 'fresh_cache')


def server(live, host, posts=1):
    """`posts` microblogs in the microblogs community by an author on `host`."""
    instance = make_instance(host)
    author = make_user(instance, f'u_{host}')
    for n in range(posts):
        make_post(live.microblogs, author, f'https://{host}/statuses/{n}', microblog=True)
    return instance


def community(name, post_count):
    row = make_community(name)
    row.post_count = post_count
    db.session.commit()
    return row
```

`live` (from `tests/test_microblog_live.py`) provides `live.viewer` (a local user on instance 1), `live.author` (on `live.remote`, `mastodon.example`), `live.microblogs` (the local microblogs community) and `live.toot(**columns)`. It also sets `Site.private_instance = False`.

---

### Task 1: The union query module

**Files:**
- Create: `app/community/server_view_list.py`
- Create: `tests/test_server_views_in_community_list.py`

**Interfaces:**
- Consumes: none from this plan. From the codebase: `LIVE_COMMUNITY` (`app/community/live.py`), `instance_banned` (`app/utils.py`), `Community`, `Instance`, `Post`, `BannedInstances` (`app/models.py`).
- Produces:
  - `class ServerView`
  - `like_pattern(text: str) -> str`
  - `parse_sort(sort_by: str | None) -> tuple[str, str]`
  - `view_side_allowed(*, home_select, subscribe_select, topic_id, language_id, feed_id, platform, nsfw) -> bool`
  - `community_select(community_query) -> Select`
  - `server_view_select(microblogs_id: int, search: str = '', host: str = '', blocked_instance_ids=()) -> Select`
  - `class UnionPage` (`items`, `page`, `per_page`, `total`, `has_prev`, `has_next`, `prev_num`, `next_num`)
  - `paginate_union(community_query, view_select, sort_by, page, per_page, microblogs) -> UnionPage`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_server_views_in_community_list.py` with the header above, then:

```python
from app.community.server_view_list import (ServerView, like_pattern, paginate_union, parse_sort,
                                            server_view_select, view_side_allowed)


def view_rows(app, live, **kwargs):
    with app.test_request_context():
        return {row.title: row for row in db.session.execute(server_view_select(live.microblogs.id, **kwargs)).all()}


def page(app, live, sort_by='post_count desc', n=1, per_page=50, view=True, query=None, **kwargs):
    with app.test_request_context():
        communities = query if query is not None else Community.query.filter_by(banned=False)
        views = server_view_select(live.microblogs.id, **kwargs) if view else None
        result = paginate_union(communities, views, sort_by, n, per_page, live.microblogs)
        return result, [item.link() for item in result.items]


class TestServerViewSelect:

    def test_one_row_per_server_with_its_counts(self, app, live):
        live.toot()
        live.toot(posted_at=utcnow() - timedelta(days=10), reply_count=3)

        row = view_rows(app, live)['microblogs@mastodon.example']

        assert row.community_id is None and row.instance_id == live.remote.id
        assert (row.subscriptions_count, row.post_count, row.post_reply_count, row.active_weekly) == (0, 2, 3, 1)
        assert row.last_active > row.created_at

    def test_local_deleted_banned_and_real_community_servers_are_left_out(self, app, live):
        make_post(live.microblogs, live.viewer, 'https://test.piefed.local/post/1', microblog=True)
        live.toot(deleted=True)
        server(live, 'banned.example')
        db.session.add(BannedInstances(domain='banned.example'))
        server(live, 'piefed.example')
        real = make_community('microblogs', host='piefed.example')
        real.ap_id = 'microblogs@piefed.example'
        db.session.commit()

        assert view_rows(app, live) == {}

    def test_a_ban_with_no_domain_does_not_hide_every_server(self, app, live):
        live.toot()
        db.session.add(BannedInstances(domain=None))
        db.session.commit()

        assert list(view_rows(app, live)) == ['microblogs@mastodon.example']

    def test_a_viewers_blocked_servers_are_left_out(self, app, live):
        live.toot()
        server(live, 'other.example')

        assert list(view_rows(app, live, blocked_instance_ids=[live.remote.id])) == ['microblogs@other.example']

    @pytest.mark.parametrize('search, expected', [
        ('microblogs', {'microblogs@mastodon.example', 'microblogs@infosec.exchange'}),
        ('InfoSec', {'microblogs@infosec.exchange'}),
        ('%', set()),
        ('_', set()),
    ])
    def test_search_matches_the_title_literally(self, app, live, search, expected):
        live.toot()
        server(live, 'infosec.exchange')

        assert set(view_rows(app, live, search=search)) == expected

    def test_the_instance_filter_picks_one_host(self, app, live):
        live.toot()
        server(live, 'infosec.exchange')

        assert list(view_rows(app, live, host='InfoSec.Exchange')) == ['microblogs@infosec.exchange']


class TestPaginateUnion:

    def test_views_sort_among_communities(self, app, live):
        community('big', 5)
        community('small', 1)
        server(live, 'mid.example', posts=3)

        _, links = page(app, live, search='', sort_by='post_count desc')

        assert links.index('big') < links.index('microblogs@mid.example') < links.index('small')

    def test_items_are_communities_or_server_views(self, app, live):
        community('big', 5)
        live.toot()

        result, _ = page(app, live)

        kinds = {type(item) for item in result.items}
        assert kinds == {Community, ServerView}
        view = next(item for item in result.items if isinstance(item, ServerView))
        assert view.link() == view.display_name() == 'microblogs@mastodon.example'
        assert view.id is None and view.subscriptions_count == 0 and not view.nsfw and not view.nsfl
        assert view.is_server_view and view.instance.id == live.remote.id
        assert view.icon_image('tiny') == live.microblogs.icon_image('tiny')

    def test_pages_continue_without_gaps_or_repeats_when_keys_tie(self, app, live):
        for n in range(3):
            community(f'c{n}', 1)
        for n in range(3):
            server(live, f's{n}.example', posts=1)

        first, one = page(app, live, per_page=4)
        second, two = page(app, live, per_page=4, n=2)

        assert first.total == 7   # 3 communities + 3 views + the microblogs community itself
        assert len(one) == 4 and len(two) == 3 and not set(one) & set(two)
        assert first.has_next and not first.has_prev and first.next_num == 2 and first.prev_num is None
        assert second.has_prev and not second.has_next and second.prev_num == 1 and second.next_num is None

    def test_without_a_view_side_only_communities_are_listed(self, app, live):
        live.toot()

        _, links = page(app, live, view=False)

        assert not any(link.startswith('microblogs@') for link in links)

    def test_a_wildcard_ban_hides_the_view(self, app, live):
        server(live, 'evil.example')
        db.session.add(BannedInstances(domain='ev*l.example'))
        db.session.commit()

        _, links = page(app, live)

        assert 'microblogs@evil.example' not in links

    def test_a_page_below_one_is_page_one(self, app, live):
        result, _ = page(app, live, n=0)

        assert result.page == 1


class TestHelpers:

    @pytest.mark.parametrize('sort_by, expected', [
        ('post_count desc', ('post_count', 'desc')),
        ('title', ('title', 'asc')),
        ('title ASC', ('title', 'asc')),
        ('', ('active_weekly', 'desc')),
        (None, ('active_weekly', 'desc')),
        ('nonsense desc', ('active_weekly', 'desc')),
        ('id desc', ('active_weekly', 'desc')),
    ])
    def test_parse_sort(self, sort_by, expected):
        assert parse_sort(sort_by) == expected

    def test_like_pattern_escapes_wildcards(self):
        assert like_pattern('a%b_c\\') == '%a\\%b\\_c\\\\%'

    base = dict(home_select='any', subscribe_select='any', topic_id=0, language_id=0, feed_id=0, platform='',
                nsfw='all')

    def test_the_view_side_is_allowed_by_default(self):
        assert view_side_allowed(**self.base)

    @pytest.mark.parametrize('change', [
        {'home_select': 'local'}, {'subscribe_select': 'subscribed'}, {'subscribe_select': 'not_subscribed'},
        {'topic_id': 3}, {'topic_id': -1}, {'language_id': 2}, {'feed_id': 1}, {'platform': 'peertube'},
        {'nsfw': 'yes'},
    ])
    def test_filters_a_view_cannot_match_drop_it(self, change):
        assert not view_side_allowed(**{**self.base, **change})

    @pytest.mark.parametrize('change', [{'home_select': 'remote'}, {'nsfw': 'no'}])
    def test_filters_a_view_can_match_keep_it(self, change):
        assert view_side_allowed(**{**self.base, **change})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_server_views_in_community_list.py -q`
Expected: a collection error, `ModuleNotFoundError: No module named 'app.community.server_view_list'`.

- [ ] **Step 3: Implement the module**

Create `app/community/server_view_list.py`:

```python
"""Server views (/c/microblogs@<host>) as rows of the All Communities list: one UNION ALL of the
filtered community query and a per-instance aggregate of the microblogs community's posts, ordered
and paginated as one list.

Spec: docs/superpowers/specs/2026-10-08-server-views-in-community-list-design.md
"""
from datetime import timedelta

from flask import current_app
from sqlalchemy import Integer, cast, func, literal, null, select, union_all
from sqlalchemy.orm import joinedload

from app import db
from app.community.live import LIVE_COMMUNITY
from app.models import BannedInstances, Community, Instance, Post
from app.utils import instance_banned, utcnow

VIEW_PREFIX = f'{LIVE_COMMUNITY}@'
SORT_FIELDS = ('title', 'subscriptions_count', 'post_count', 'post_reply_count', 'last_active',
               'created_at', 'active_weekly')
DEFAULT_SORT = ('active_weekly', 'desc')   # what safe_order_by falls back to for these fields


class ServerView:
    """A server view as a row of the list. It reads like a Community where the template needs it."""
    is_server_view = True
    id = None
    instance_id = None       # platform_of reads instance_id: a view carries no platform badge
    nsfw = False
    nsfl = False
    subscriptions_count = 0

    def __init__(self, instance, microblogs, post_count, post_reply_count, last_active, created_at,
                 active_weekly):
        self.instance = instance
        self.host = instance.domain.lower()
        self._microblogs = microblogs
        self.post_count = post_count
        self.post_reply_count = post_reply_count
        self.last_active = last_active
        self.created_at = created_at
        self.active_weekly = active_weekly

    def link(self) -> str:
        return f'{VIEW_PREFIX}{self.host}'

    def display_name(self) -> str:
        return self.link()

    def icon_image(self, size='default') -> str:
        return self._microblogs.icon_image(size)


def like_pattern(text: str) -> str:
    """A LIKE pattern matching `text` anywhere, with LIKE's own wildcards taken literally (escape '\\')."""
    escaped = text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return f'%{escaped}%'


def parse_sort(sort_by):
    """(field, 'asc'|'desc') for a `sort_by` like 'post_count desc'; DEFAULT_SORT when it is not one."""
    parts = (sort_by or '').strip().split()
    field = parts[0] if parts else ''
    if field not in SORT_FIELDS:
        return DEFAULT_SORT
    direction = parts[1].lower() if len(parts) > 1 else 'asc'
    return field, 'desc' if direction == 'desc' else 'asc'


def view_side_allowed(*, home_select, subscribe_select, topic_id, language_id, feed_id, platform, nsfw) -> bool:
    """False when a filter is set that no server view can satisfy."""
    return not (home_select == 'local' or subscribe_select != 'any' or topic_id or language_id or feed_id
                or platform or nsfw == 'yes')


def community_select(community_query):
    """The filtered community query reduced to the union's columns, unordered."""
    return community_query.order_by(None).with_entities(
        Community.id.label('community_id'), cast(null(), Integer).label('instance_id'),
        Community.title.label('title'), Community.subscriptions_count.label('subscriptions_count'),
        Community.post_count.label('post_count'), Community.post_reply_count.label('post_reply_count'),
        Community.last_active.label('last_active'), Community.created_at.label('created_at'),
        Community.active_weekly.label('active_weekly')).statement


def server_view_select(microblogs_id: int, search: str = '', host: str = '', blocked_instance_ids=()):
    """One row per server with a server view, aggregated over the microblogs community's posts.
    Wildcard bans are not matched here; paginate_union drops those rows."""
    domain = func.lower(Instance.domain)
    title = func.concat(VIEW_PREFIX, domain)
    week_ago = utcnow() - timedelta(days=7)
    banned = select(func.lower(BannedInstances.domain)).where(BannedInstances.domain.isnot(None))
    real = select(Community.ap_id).where(Community.ap_id.like(f'{VIEW_PREFIX}%'))
    stmt = (select(cast(null(), Integer).label('community_id'), Post.instance_id.label('instance_id'),
                   title.label('title'), literal(0).label('subscriptions_count'),
                   func.count(Post.id).label('post_count'),
                   func.coalesce(func.sum(Post.reply_count), 0).label('post_reply_count'),
                   func.max(Post.posted_at).label('last_active'), func.min(Post.posted_at).label('created_at'),
                   func.count(Post.id).filter(Post.posted_at > week_ago).label('active_weekly'))
            .select_from(Post)
            .join(Instance, Instance.id == Post.instance_id)
            .where(Post.community_id == microblogs_id, Post.deleted == False, Post.instance_id != 1,
                   domain != current_app.config['SERVER_NAME'].lower(),
                   domain.not_in(banned), title.not_in(real))
            .group_by(Post.instance_id, domain))
    if search:
        stmt = stmt.where(title.ilike(like_pattern(search), escape='\\'))
    if host:
        stmt = stmt.where(domain == host.strip().lower())
    if blocked_instance_ids:
        stmt = stmt.where(Post.instance_id.not_in(list(blocked_instance_ids)))
    return stmt


class UnionPage:
    """The slice of Flask-SQLAlchemy's Pagination the list route and template read."""

    def __init__(self, items, page, per_page, total):
        self.items = items
        self.page = page
        self.per_page = per_page
        self.total = total

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page * self.per_page < self.total

    @property
    def prev_num(self):
        return self.page - 1 if self.has_prev else None

    @property
    def next_num(self):
        return self.page + 1 if self.has_next else None


def _hydrate(rows, microblogs) -> list:
    community_ids = [row.community_id for row in rows if row.community_id is not None]
    instance_ids = [row.instance_id for row in rows if row.instance_id is not None]
    communities = {c.id: c for c in Community.query.options(joinedload(Community.instance))
                   .filter(Community.id.in_(community_ids))} if community_ids else {}
    instances = {i.id: i for i in Instance.query.filter(Instance.id.in_(instance_ids))} if instance_ids else {}
    items = []
    for row in rows:
        if row.community_id is not None:
            if row.community_id in communities:
                items.append(communities[row.community_id])
            continue
        instance = instances.get(row.instance_id)
        if instance is None or instance_banned(instance.domain.lower()):   # wildcard bans: not matched in SQL
            continue
        items.append(ServerView(instance, microblogs, row.post_count, row.post_reply_count, row.last_active,
                                row.created_at, row.active_weekly))
    return items


def paginate_union(community_query, view_select, sort_by, page, per_page, microblogs) -> UnionPage:
    """One page of communities and (when view_select is given) server views, ordered as one list."""
    parts = [community_select(community_query)]
    if view_select is not None:
        parts.append(view_select)
    union = (union_all(*parts) if len(parts) > 1 else parts[0]).subquery()
    field, direction = parse_sort(sort_by)
    key = union.c[field]
    order = [key.desc() if direction == 'desc' else key.asc(), union.c.title, union.c.community_id,
             union.c.instance_id]
    page = max(page, 1)
    total = db.session.scalar(select(func.count()).select_from(union))
    rows = db.session.execute(select(union).order_by(*order).limit(per_page).offset((page - 1) * per_page)).all()
    return UnionPage(_hydrate(rows, microblogs), page, per_page, total)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_server_views_in_community_list.py tests/test_no_inline_imports.py -q`
Expected: all pass. If `test_a_wildcard_ban_hides_the_view` fails because `instance_banned('evil.example')` does not match `ev*l.example`, read `instance_banned` (`app/utils.py`, wildcard branch: `*` matches one letter or digit) and change the test's ban entry to one it does match. Report the change.

- [ ] **Step 5: Commit**

```bash
git add app/community/server_view_list.py tests/test_server_views_in_community_list.py
git commit -m "feat: a union of communities and microblog server views, ordered and paginated as one list

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Wire it into `/communities`

**Files:**
- Modify: `app/main/routes.py` (`list_communities`, ~lines 377-605, and imports)
- Modify: `app/templates/list_communities.html` (row loop ~291-313)
- Test: `tests/test_server_views_in_community_list.py`

**Interfaces:**
- Consumes: `like_pattern`, `view_side_allowed`, `server_view_select`, `paginate_union`, `ServerView.is_server_view` (Task 1).
- Produces: nothing for later tasks.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_server_views_in_community_list.py`:

```python
VIEW_LINK = 'href="/c/microblogs@mastodon.example"'


class TestTheList:

    def test_search_microblogs_lists_every_server_view(self, client, live):
        live.toot()
        server(live, 'infosec.exchange')

        html = client.get('/communities?search=microblogs').get_data(as_text=True)

        assert VIEW_LINK in html and 'href="/c/microblogs@infosec.exchange"' in html

    def test_a_host_search_finds_its_view(self, client, live):
        live.toot()
        server(live, 'infosec.exchange')

        html = client.get('/communities?search=infosec').get_data(as_text=True)

        assert 'href="/c/microblogs@infosec.exchange"' in html and VIEW_LINK not in html

    def test_the_unfiltered_list_includes_views(self, client, live):
        live.toot()

        assert VIEW_LINK in client.get('/communities').get_data(as_text=True)

    def test_a_view_row_has_no_join_control(self, client, live):
        live.toot()
        login(client, live.viewer)

        html = client.get('/communities?search=microblogs').get_data(as_text=True)

        assert VIEW_LINK in html
        assert '/community/microblogs@mastodon.example/subscribe' not in html
        assert '/community/microblogs@mastodon.example/unsubscribe' not in html

    @pytest.mark.parametrize('query', ['home_select=local', 'subscribe_select=subscribed', 'topic_id=1',
                                       'language_id=1', 'feed_id=1', 'platform=peertube', 'nsfw=yes'])
    def test_filters_a_view_cannot_match_drop_views(self, client, live, query):
        live.toot()
        login(client, live.viewer)

        response = client.get(f'/communities?{query}')

        assert response.status_code == 200 and VIEW_LINK not in response.get_data(as_text=True)

    @pytest.mark.parametrize('query', ['home_select=remote', 'nsfw=no', 'instance=mastodon.example'])
    def test_filters_a_view_can_match_keep_views(self, client, live, query):
        live.toot()

        assert VIEW_LINK in client.get(f'/communities?{query}').get_data(as_text=True)

    def test_a_blocked_server_is_not_listed_for_its_blocker(self, client, live):
        live.toot()
        db.session.add(InstanceBlock(user_id=live.viewer.id, instance_id=live.remote.id))
        db.session.commit()
        login(client, live.viewer)

        assert VIEW_LINK not in client.get('/communities?search=microblogs').get_data(as_text=True)

    def test_a_community_search_takes_wildcards_literally(self, client, live):
        make_community('plain')

        assert 'href="/c/plain"' not in client.get('/communities?search=%25').get_data(as_text=True)

    @pytest.mark.parametrize('sort_by', ['', 'title asc', 'post_count desc', 'nonsense', '1; DROP TABLE x'])
    def test_every_sort_answers(self, client, live, sort_by):
        live.toot()

        assert client.get('/communities', query_string={'sort_by': sort_by}).status_code == 200

    def test_pagination_crosses_from_communities_to_views(self, client, live):
        for n in range(60):
            community(f'c{n:02d}', 100 + n)
        live.toot()

        first = client.get('/communities?sort_by=post_count desc').get_data(as_text=True)
        second = client.get('/communities?sort_by=post_count desc&page=2').get_data(as_text=True)

        assert VIEW_LINK not in first and 'page=2' in first
        assert VIEW_LINK in second
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_server_views_in_community_list.py -q`
Expected: the `TestTheList` tests that look for `VIEW_LINK`, and the wildcard-search test, fail. Task 1's tests still pass.

- [ ] **Step 3: Use the union in `list_communities`**

In `app/main/routes.py`:

1. Add the module import next to the other `app.community` imports:

```python
from app.community import server_view_list
```

2. Make sure `find_microblogging_community` and `blocked_or_banned_instances` are imported. `find_microblogging_community` is already used in this file (`home_feed_source`), and `blocked_or_banned_instances` is already used in `list_communities`. Add whichever is missing to its existing import line.

3. Replace the search filter:

```python
        communities = communities.filter(or_(Community.title.ilike(f"%{search_param}%"), Community.ap_id.ilike(f"%{search_param}%")))
```

with:

```python
        pattern = server_view_list.like_pattern(search_param)   # %, _ and \ match literally
        communities = communities.filter(or_(Community.title.ilike(pattern, escape='\\'),
                                             Community.ap_id.ilike(pattern, escape='\\')))
```

4. Delete the `communities = communities.order_by(safe_order_by(sort_by, Community, {...}))` statement (the union orders instead). If `safe_order_by` is then unused in this file, remove it from the import line.

5. Replace:

```python
    communities = communities.options(joinedload(Community.instance)).paginate(page=page,
                                       per_page=100 if current_user.is_authenticated and not low_bandwidth else 50,
                                       error_out=False)
```

with:

```python
    # Server views (/c/microblogs@<host>) join the list as rows (spec 2026-10-08, server views in the list)
    microblogs = find_microblogging_community()
    view_rows = None
    if server_view_list.view_side_allowed(home_select=home_select, subscribe_select=subscribe_select,
                                          topic_id=topic_id, language_id=language_id, feed_id=feed_id,
                                          platform=platform, nsfw=nsfw):
        blocked = blocked_or_banned_instances(current_user.id) if current_user.is_authenticated else ()
        view_rows = server_view_list.server_view_select(microblogs.id, search_param, instance, blocked)
    communities = server_view_list.paginate_union(
        communities, view_rows, sort_by, page,
        100 if current_user.is_authenticated and not low_bandwidth else 50, microblogs)
```

Keep the comment that sat above the old paginate call ("platform_of reads each row's instance…") only if it still describes the code. `paginate_union` loads `Community.instance` itself.

- [ ] **Step 4: The template**

In `app/templates/list_communities.html`, the first cell of each row is `<td width="100">` followed by `{% if current_user.is_authenticated -%}` and the pending/leave/join branches, ending `{% endif -%}</td>`. Wrap everything between `<td width="100">` and `</td>` in:

```html
{% if not community.is_server_view -%}
    ...the existing pending/leave/join branches, unchanged...
{% endif -%}
```

so a server view row gets an empty first cell. A `Community` has no `is_server_view` attribute. Jinja reads it as undefined, which is falsy, so community rows are unchanged.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_server_views_in_community_list.py tests/test_main_modlog.py tests/test_safe_order_by.py tests/test_query_string_integers.py tests/test_discovery_search.py tests/test_crafted_id_parameters.py tests/test_no_inline_imports.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add app/main/routes.py app/templates/list_communities.html tests/test_server_views_in_community_list.py
git commit -m "feat: /communities lists microblog server views among communities, and search finds them

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Full suite and gates

**Files:** none changed unless a gate fails.

- [ ] **Step 1: Run the full suite with coverage**

Run: `./run_tests.sh --cov=app --cov=fastapi_server --cov-branch --cov-report=json:coverage.json -q`
Expected: all pass.

- [ ] **Step 2: Run the gates on the host**

```bash
python3 tests/check_changed_line_coverage.py coverage.json <commit before Task 1> --branches app/ fastapi_server.py
python3 tests/check_coverage_floors.py coverage.json coverage_floors.ini
```

Expected: both pass. An uncovered changed line gets a test in the owning task's test class, in a follow-up commit ending with the same `Co-Authored-By` line.
