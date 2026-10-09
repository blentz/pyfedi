# Server views in the All Communities list

Status: design approved in session (2026-10-08). Fork-only work.
Builds on: `docs/superpowers/specs/2026-10-08-microblog-instance-views-design.md` (server views).

## Problem

A server view, `/c/microblogs@<host>`, shows the stored microblog posts by authors on `<host>`. Server
views have no `Community` row (owner ruling, approach A of the server-views spec), so the All
Communities list (`/communities`, `list_communities` in `app/main/routes.py`) never shows them, and a
community search cannot find them. The community search on `/search` redirects to
`/communities?search=…` (`app/search/routes.py`), so the list is the only surface to change.

## Goals

- Server views appear in `/communities` as rows of the same table. They are sorted and paginated
  together with communities.
- Searching "microblogs" lists every available server view. Searching part of a host (for example
  "infosec") finds that server's view.

Success: on hell.cloud, `/communities?search=microblogs` lists `microblogs@infosec.exchange`,
`microblogs@mastodon.social` and the other servers with stored microblogs, each linking to its view.
`/communities` sorted by posts places each view among the communities by its post count.

Non-goals:
- Joining a server view, or any subscribe, pending or leave control on its row.
- `Community` rows, migrations, or federation for server views.
- Server views in any other list: topics, feeds, the communities menu, the API.

## Decisions (owner rulings, 2026-10-08)

1. **Shape:** one `UNION ALL` of the filtered community query and a server-view aggregate, ordered and
   paginated as one list. Rejected: a separate "Server views" section above the table (views would
   not sort or paginate with communities); a `Community` row per view (reverses the server-views
   spec's ruling).
2. Server views stay browse-only.

## Design

### The two sides of the union

Both sides select the same columns, in the same order:

| Column | Community side | Server-view side |
|---|---|---|
| `community_id` | `Community.id` | `NULL` |
| `instance_id` | `NULL` | `Post.instance_id` |
| `title` | `Community.title` | `'microblogs@' \|\| lower(Instance.domain)` |
| `subscriptions_count` | `Community.subscriptions_count` | `0` |
| `post_count` | `Community.post_count` | `count(Post.id)` |
| `post_reply_count` | `Community.post_reply_count` | `coalesce(sum(Post.reply_count), 0)` |
| `last_active` | `Community.last_active` | `max(Post.posted_at)` |
| `created_at` | `Community.created_at` | `min(Post.posted_at)` |
| `active_weekly` | `Community.active_weekly` | posts with `posted_at > now - 7 days` |

**Community side:** the query `list_communities` builds today, with every existing filter, reduced to
those columns. Its behaviour is unchanged.

**Server-view side:** posts in the microblogs community (`find_microblogging_community()`), joined to
`Instance` on `Post.instance_id`, grouped by instance. A row is kept only when all of these hold:

- `Post.deleted` is false, `Post.instance_id` is not 1 and not NULL.
- The host resolves as a server view under the server-views spec's rules: not this instance's
  `SERVER_NAME`, no real `microblogs@<host>` community, not on the instance ban list. Banned hosts and
  hosts with a real community are excluded in SQL (a `NOT IN` over `BannedInstances.domain` and over
  `Community.ap_id` values that start with `microblogs@`), not by a per-row call. Wildcard bans
  (`*` in `BannedInstances.domain`) are applied in Python after hydration, by dropping rows for which
  `server_view_instance(host)` returns None. That page can then show fewer rows than the page size.
- For a signed-in viewer, the instance is not in `blocked_or_banned_instances(viewer)`.

### When the server-view side is included

The view side is a union member only when every filter the request sets can match a server view:

- `search`: matches if `title` (`microblogs@<host>`) contains the search text, case-insensitively. So
  "microblogs" matches every view and "infosec" matches `microblogs@infosec.exchange`.
- `instance`: matches if the host equals it.
- `nsfw`: `all` or `no` keep the view side. `yes` drops it.
- `home_select=local`, `subscribe_select` other than `any`, `topic_id`, `language_id`, `feed_id`,
  `platform`: any of these set drops the view side.

### Sort and pagination

- The sort uses the same `sort_by` values and the same allowed set as today: `title`,
  `subscriptions_count`, `post_count`, `post_reply_count`, `last_active`, `created_at`,
  `active_weekly`. It applies to the union's column of that name. An unknown value falls back as
  `safe_order_by` does now. Ties break on `title`, then `community_id`, then `instance_id`, so pages
  are stable.
- Pagination is the union's: same page sizes (100 signed in and not low bandwidth, else 50), same
  `next_url` and `prev_url`.

### Hydration

For one page of union rows, in order:

- Community rows load as `Community` objects in one query with `joinedload(Community.instance)`, as
  today.
- View rows become `ServerView` objects (new module `app/community/server_view_list.py`, which also
  builds the union): `is_server_view = True`,
  `instance`, `host`, the aggregate counts, `subscriptions_count = 0`, `nsfw = nsfl = False`,
  `id = None`, `link()` returning `microblogs@<host>`, `display_name()` returning `microblogs@<host>`,
  and `icon_image(size)` returning the microblogs community's icon.
- The template receives a pagination object whose `items` are these objects in union order, with
  `has_next`, `has_prev`, `next_num` and `prev_num` from the union's pagination.

### Template

`list_communities.html` renders a `ServerView` row like a community row, except the join, pending and
leave column is empty. Every other column reads the same attribute names.

### Performance

The view side aggregates every microblogs post on each list request. It is filtered by
`Post.community_id`, which is indexed. If it shows in slow logs, the aggregate moves to a cached
per-instance table. That is out of scope here.

## Errors and edge cases

- No microblogs community yet: `find_microblogging_community()` creates it, and the view side is
  empty.
- A search with SQL `LIKE` metacharacters (`%`, `_`): escaped on both sides the same way.
- An anonymous visitor sees server views, as they see communities.
- Page 2 of a union that holds only communities matches today's page 2.

## Testing

- `/communities?search=microblogs` lists a view per qualifying server and no view for a local
  author, this instance, a banned host, a host with a real `microblogs@<host>` community, or a host
  the viewer blocked.
- A host search finds that view only.
- Sorting by `post_count desc` interleaves a view between two communities by count.
- Pagination: a page boundary falls correctly across communities and views.
- Each excluding filter (topic, language, local, subscribed, feed, platform, `nsfw=yes`) drops views.
  `nsfw=no` and `home_select=remote` keep them.
- A view row links to `/c/microblogs@<host>` and has no Join control.
- The existing `list_communities` tests still pass unchanged.
