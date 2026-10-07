# Live feed for the microblogs community

Status: design approved in session (2026-10-07). Fork-only work.

## Problem

`/c/microblogs` collects microblog posts from around the fediverse (`find_microblogging_community`,
`app/activitypub/util.py:5657`). Reading it means reloading the page. Mastodon's Live Feeds shows a
newest-first federated stream that grows by itself while the reader watches. PieFed has nothing like it.

## Goals

Add a fourth sort button, "Live", next to New, Old and Active on the microblogs community. It
reproduces Mastodon's Live Feed: new posts appear without a reload. If the reader is scrolled down, the
new posts are held behind an "N new posts" pill.

Success: a logged-in user opens `/c/microblogs?sort=live` and leaves the tab open. A microblog post
that federates in, and that the user's filters allow, appears within about 5 s where the notification
server runs and within about 15 s where it does not. The page never jumps under a reader who has
scrolled down.

Non-goals:
- Live updates of edits, deletes, votes or reply counts. Only new posts stream in.
- A "This server / All servers" toggle.
- Live on any other community, feed, or the home page.
- Live for anonymous visitors.
- Masonry layouts in Live.

## Decisions (owner rulings, 2026-10-07)

1. **Scope:** only the local special community, where `community.name == 'microblogs'` and
   `community.is_local()`. No new community flag.
2. **Arrival:** Mastodon-style. A reader within 100 px of the top gets new posts prepended at once.
   A reader scrolled further down sees them held behind a sticky "N new posts" pill. Clicking the pill
   scrolls to the top and inserts them.
3. **Filter:** all servers only. No local/remote tabs.
4. **Transport:** hybrid. An SSE wake-up runs through `fastapi_server.py` when `NOTIF_SERVER` is set.
   Polling is the fallback. Each viewer always fetches their own filtered fragment from Flask.
5. **Audience:** logged-in users only. Anonymous users see no Live button. For them, and for every
   community other than microblogs, `sort=live` falls back to `new`.
6. **Coverage:** 100% line and branch coverage of all new and changed code, in Python and in
   JavaScript (see Testing).

## User-facing behavior

- **Button.** "Live" follows Active in all five sort-row copies in
  `app/templates/community/_community_nav.html`. Two are dropdown items, three are button groups. It is
  rendered only when the Live conditions hold (decisions 1 and 5). The link is `?sort=live`, without
  `layout`.
- **Initial render.** Identical to `sort=new` page 1. Live forces the list layout whatever the user's
  saved layout is. The pagination bar is replaced by an "Older posts" link to `?sort=new&page=2`.
- **Status indicator.** A small label next to the post list reads "Live" while connected or polling.
  It reads "Paused" while the tab is hidden.
- **Arrival.** Posts are prepended newest-first, and a newly inserted teaser gets a 2 s highlight
  class. Held posts collect in a buffer, and the pill shows the buffer count. Clicking the pill
  scrolls to the top and flushes the buffer. Scrolling back to within 100 px of the top flushes it too.
- **De-duplication.** A post whose `id` is already in the list is never inserted twice.
- **DOM cap.** At most 200 teasers are kept. Inserting past 200 removes the oldest from the bottom.
- **Hidden tab.** SSE is closed and polling stops. When the tab becomes visible, the client makes one
  catch-up fetch, then resumes.

## Server

### Shared filter helper (refactor)

The filter chain applied to each viewer in `show_community` (`app/community/routes.py:412`-`500`) is
moved into a function in `app/community/util.py`:

```python
def community_post_query(community: Community, user: User | None, content_type: str, tag=None)
```

It covers bots, NSFW and NSFL, hidden read posts, gen-AI, blocked or banned instances, blocked users,
blocked communities and languages. It returns the unsorted, unpaginated `Post` query plus the
`content_filters` dict the template uses. `show_community` calls it and keeps its own sorting and
pagination. This is a pure move: existing community view tests must pass unchanged.

### Fragment endpoint

`GET /c/<actor>/live/posts?after=<int>` in `app/community/routes.py`.

- `@login_required`. Returns 404 unless the community is the local `microblogs` community.
- A missing or non-integer `after` returns 400.
- Query: `community_post_query(...)`, plus `Post.id > after`, plus
  `Post.posted_at > utcnow() - timedelta(hours=1)`, ordered by `Post.posted_at desc`, limit 40.
- The 1-hour window keeps backfilled posts out: they are old posts with new, high ids. It still admits
  posts that federate in a few minutes late.
- The cursor is `Post.id` because local ids only grow. Remote `published` timestamps can be skewed or
  backdated.
- A non-empty result returns 200 with teasers rendered by the same list-layout macro the community page
  uses. The header `X-Live-Cursor` carries the largest `Post.id` returned.
- An empty result returns 204 with no body.
- `@limiter.limit("12/minute", key_func=lambda: str(current_user.id))`. The app limiter's default key is
  the IP address (`app/__init__.py:102`), and users behind one NAT must not share a budget. The client
  never needs more than 12 requests a minute (see Client timings).

### Signal publish

`Post.new()` (`app/models.py:2638`) publishes after its commit when all of these hold:

- `current_app.config['NOTIF_SERVER']` is set
- the community is the local `microblogs` community
- `backfill` is false
- the post is listable and `status > POST_STATUS_REVIEWING`

It then calls `publish_sse_event("live:microblogs", "{}")`, the helper in `app/utils.py`.

- The payload is empty on purpose. It only wakes clients up, and each client fetches its own filtered
  fragment. The broadcast reveals nothing a viewer's filters would hide.
- The publish runs inside `try/except Exception`, logged at warning. A Redis outage never fails ingest.

## SSE server (`fastapi_server.py`)

- A new registry, `live_clients: dict[str, set[asyncio.Queue]]`. It is separate from
  `connected_clients`, so per-user notification streams never see broadcasts.
- `LIVE_FEEDS = {"microblogs"}`.
- New route `GET /live/stream?feed=<name>`. A feed not in `LIVE_FEEDS` returns 404. Otherwise it
  streams like `/notifications/stream`: `": connected"`, then a heartbeat every 60 s, and the queue is
  removed on disconnect. There is no auth, because the payload carries no data.
- `redis_listener` also subscribes to `live:*`. For `live:<feed>` it puts the data on every queue in
  `live_clients[feed]`.

## Proxy

- Caddyfile: add `handle /live/stream { reverse_proxy piefednotifs:8000 { ... } }`, with the same
  options as the existing `/notifications/stream` block.
- `INSTALL.md`: add the matching nginx `location = /live/stream` next to the existing notifications
  stream location.
- `tests/test_caddyfile_routing.py`: every FastAPI path, now including `/live/stream`, is routed to
  FastAPI.

## Client

`app/static/js/live_feed.js` is an ES module. Only `community.html` loads it, and only when
`sort == 'live'`. The page passes its configuration through `data-` attributes on the post list:
fragment URL, initial cursor, the SSE URL (empty when `NOTIF_SERVER` is unset), and translated strings.

The module separates logic from environment. Every browser dependency is injected:

```js
export function createLiveFeed({ list, pill, status, fetch, EventSource, document, window,
                                 setTimeout, clearTimeout, now, config })
```

`createLiveFeed` is the only export besides the pure helpers that it uses:
- `shouldHold(scrollY)`
- `nextBackoff(ms)`
- `dedupeIds(existing, incoming)`
- `trimToCap(n)`

A one-line bootstrap at the bottom of the file passes the real globals. It is the only line not run
under node, and it is excluded with a `/* node:coverage ignore next */` comment.

### Client timings

- **SSE mode.** Each signal schedules a fetch, coalesced so at most one fetch runs per 5 s. A 60 s
  safety poll runs as well, because Redis pub/sub is not durable.
- **Fallback to polling.** The client polls every 15 s when:
  - the SSE URL is empty, or
  - `EventSource` is undefined, or
  - SSE errors 3 times in a row.
- **Responses.**
  - 200: parse the fragment, de-duplicate, then insert or hold, and set the cursor from
    `X-Live-Cursor`.
  - 204: do nothing.
  - 429 or a network error: double the poll interval, up to a cap of 120 s. The next 200 or 204 resets
    it.
  - 401 or 404: stop and set the status to "Paused".
- **Init.** Inserted nodes get the same initialization the page gives teasers: `htmx.process(node)`
  plus the existing per-teaser bindings in `scripts.js`. If those bindings live in a function that
  only `DOMContentLoaded` runs, it is exported or extracted so it can run per node.

## Error handling summary

| Failure | Behavior |
|---|---|
| Redis down at publish | logged at warning, ingest continues, clients catch up by poll |
| FastAPI down or SSE errors 3× | client falls back to 15 s polling |
| Missed pub/sub message | 60 s safety poll picks it up |
| Fragment 429 / network error | exponential backoff, capped at 120 s |
| Fragment 401 / 404 | client stops, status "Paused" |
| Malformed `after` | 400 |

## Testing

TDD throughout. Coverage requirement: **100% line and branch coverage of every new and changed line**,
in both languages. No touched module's floor in `coverage_floors.ini` may drop. Raising a floor that
this work makes reachable is allowed.

### Python (pytest, `--cov-branch`)

- **Refactor:** the existing community view tests pass unchanged. New direct tests of
  `community_post_query` cover each filter branch for an anonymous viewer (with and without
  `CONTENT_WARNING`) and for a logged-in viewer.
- **Nav:** the Live button renders only for a logged-in user on the local `microblogs` community. It
  is absent for anonymous users, other communities and a remote community named `microblogs`.
  `sort=live` renders as `new` in each of those cases. Live forces the list layout and shows the
  "Older posts" link.
- **Endpoint:**
  - Anonymous users are redirected to login.
  - A non-microblogs community gets 404.
  - A bad `after` gets 400.
  - `after` excludes older ids.
  - A post with a new id but `posted_at` 2 h ago is excluded (backfill).
  - An empty result gives 204.
  - The `X-Live-Cursor` value is correct.
  - The limit is 40.
  - The viewer's filters apply: a blocked user's post, NSFW with `hide_nsfw`, a blocked instance, and
    a post under review are all absent.
  - The 13th request in a minute gets 429.
- **Publish hook:**
  - It fires once for a microblogs post.
  - It does not fire for a backfill, for another community, for a post under review, or with
    `NOTIF_SERVER` unset.
  - With a Redis error the post still commits, and a warning is logged.
- **FastAPI:** `fastapi.testclient.TestClient` tests of `/live/stream`:
  - An unknown feed gets 404.
  - The connected preamble is sent.
  - A `live:microblogs` message reaches live clients and not notification clients.
  - A queue is removed on disconnect.

  The listener's fan-out function is factored out so it can be tested without Redis. `fastapi_server.py`
  is outside `.coveragerc`'s `source = app`, so the targeted run adds `--cov=fastapi_server`. Its new
  lines must reach 100%.

### JavaScript (node built-in runner)

- `package.json` at repo root: no dependencies, with the script
  `"test:js": "node --test --experimental-test-coverage --test-coverage-lines=100 --test-coverage-branches=100 --test-coverage-include=app/static/js/live_feed.js tests/js/"`.
  The thresholds make the run fail below 100%.
- `tests/js/live_feed.test.mjs` drives `createLiveFeed` with hand-written fakes for `fetch`,
  `EventSource`, timers, `document`, `window` and the list, pill and status elements. Each branch of
  Client timings and Arrival is a test case:
  - insert at top, hold when scrolled, pill count, flush on click, flush on scroll to top
  - de-duplication, cap at 200
  - SSE coalescing, the 3-error fallback, the safety poll
  - backoff and reset, stop on 401 and 404
  - pause and catch-up on visibility change
  - fallback when there is no SSE URL and when `EventSource` is undefined
- `run_tests.sh` runs `npm run test:js` when `node` is present, so the gate lives in the repo.

### Browser verification

Before claiming done, run the real app with `NOTIF_SERVER` set and again unset. Use Playwright to
check:
- insert at top
- hold and pill while scrolled
- that a vote button on an inserted teaser works (init applied)
- the hidden-tab pause and catch-up
- the polling fallback

This is verification, not a committed gate.
