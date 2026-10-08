# Per-server views of the microblogs community

Status: design approved in session (2026-10-08). Fork-only work.

## Problem

Every microblog post that reaches this instance without a community of its own is stored in the one
local `microblogs` community (`find_microblogging_community`, `app/activitypub/util.py:5657`). It
arrives three ways:

- `process_new_content` (`app/activitypub/routes.py:2669`): a Create with no community.
- Boost ingestion (`app/activitypub/util.py:4886`): a followed user boosted a post.
- Relays (`app/relays/inbound.py:206`): a relay announced a post.

A relay such as `relay.infosec.exchange` forwards posts from hundreds of servers (infosec.exchange,
mstdn.ca, social.redflag.ps and more). They all land in `/c/microblogs`, so a reader cannot see one
server's posts on their own. A PieFed server's microblogs have a home of their own,
`/c/microblogs@piefed.social`, because PieFed federates that community as a Group. Mastodon has no
Group to follow, so its servers have no equivalent.

## Goals

`/c/microblogs@<host>` shows the microblog posts this instance holds whose author is on `<host>`,
newest first, with every sort the community page has, Live included. Its Live view is this
instance's near real-time counterpart of that server's `/public/local` timeline, bounded by what the
subscribed relays and other federation deliver. Nothing is fetched from the server to fill it.

Success: with the infosec.exchange relay subscribed, a logged-in user opens
`/c/microblogs@infosec.exchange?sort=live`. It lists only posts by infosec.exchange accounts, and a
new one appears without a reload, as on `/c/microblogs`. A user reading `/c/microblogs` or a home tab
can reach that page from a link.

Non-goals:
- Joining or subscribing to a per-server view. It never appears in Subscribed or any home tab.
- Any new outbound federation: no Follow, no fetch, no polling of the server.
- RSS and iCal for these views.
- Moving stored posts. Posts stay in the `microblogs` community.
- Per-server views of any community other than the local `microblogs`.

## Decisions (owner rulings, 2026-10-08)

1. **Purpose:** browse only (option 1 of 3). Home feeds and `MICROBLOG_GATE` are unchanged.
2. **Shape:** a filtered view of the existing community (approach A). There is no community row per
   server, no synthetic actor and no migration. Rejected: a local community row per server (a
   fetchable Group actor per server, special `@` handling, a migration); a fake remote community row
   (outbound code would send votes, replies and joins to an inbox that does not exist).
3. **Discovery:** a microblog post's community link points to its author's server view, and the
   `/c/microblogs` sidebar lists the busiest servers of the last 24 hours.

## Design

### Terms

- **The microblogs community:** the row `find_microblogging_community()` returns: `instance_id == 1`,
  `user_id == 1`, `name == 'microblogs'`.
- **A server view:** that community filtered to posts with `Post.instance_id == <instance>.id`.
  `Post.instance_id` is the author's instance (`Post.new`, `app/models.py:2697`), so no join is needed.
- **A real community:** a `Community` row whose `ap_id` is the requested `microblogs@<host>`.

### Resolving `microblogs@<host>`

One function, `microblog_server_view(actor) -> Instance | None`, in `app/community/live.py`. It
returns the `Instance` when every rule holds, else `None`:

1. `actor` lower-cased splits on its last `@` into `name` and `host`, and `name == 'microblogs'`.
2. No real community exists for `actor`. A real community always wins, so
   `/c/microblogs@piefed.social` keeps showing PieFed's community.
3. `host` is not this instance's `SERVER_NAME`.
4. An `Instance` row has `domain == host`.
5. `instance_banned(host)` is false.

Callers:

- `community_profile` (`app/activitypub/routes.py:585`). When the `@` branch finds no community, and
  the request is not an ActivityPub request (that still answers 400, as today), and
  `microblog_server_view(actor)` returns an instance, it renders
  `show_community(find_microblogging_community(), from_instance=instance)`. Otherwise it answers 404,
  as today.
- The Live fragment route `live_posts_fragment` (`app/community/routes.py:908`) resolves `actor` the
  same way when `actor_to_community` finds nothing.

A host with an `Instance` row but no posts gets an empty page, not a 404.

### Query

`community_post_query(community, content_type, flair='', tag='', from_instance=None)`
(`app/community/routes.py:294`) adds `Post.instance_id == from_instance.id` when `from_instance` is
given. The page and the Live fragment both call it, so they cannot disagree on what a viewer sees.

### Page

`show_community(community, from_instance=None)` passes `from_instance` to the query and the template.
With `from_instance` set:

- The heading and the page title read `microblogs@<host>`.
- Every link the page builds to itself keeps the view's path: sort buttons, pagination, layout
  switches and the Live fragment URL. The template gets a `view_actor` variable,
  `'microblogs@' + host`, and uses it wherever it uses `community.link()` for those links.
- Join, leave, notification toggle, new post, and moderation controls are hidden.
- Flair and tag filters are not offered.
- The sidebar is the microblogs community's sidebar.
- The HTTP 304 etag includes the instance id, so a server view and `/c/microblogs` never share one.

### Live

- `live_feed_keys(post, community, backfill)` (`app/community/live.py:62`) adds `instance:<id>` when
  `community` is the microblogs community and `post.instance_id` is set and is not 1. It adds it on
  top of the keys it adds today.
- `LIVE_KEY_PATTERN` becomes `^(any|local|popular|media|community:\d+|instance:\d+)$`. The copy in
  `fastapi_server.py` changes to match; the existing test pins the two together.
- On a server view, `data-sse-url` uses `feed=instance:<id>` and `data-posts-url` uses `view_actor`.
- `live_available` is unchanged: a server view is the microblogs community, which is a Live community.

### Discovery

- **Post links.** A Jinja global, `post_community_link(post) -> str`, returns `microblogs@<host>`
  when the post is in the microblogs community, its `instance_id` is not 1, and that host resolves as
  a server view. Otherwise it returns `post.community.link()`. The teaser byline
  (`post/post_teaser/_macros.html:65`, and the dillo theme's copy) and the post page breadcrumb
  (`post/_breadcrumb_nav.html:6`) use it. Join, flair and moderation links keep
  `post.community.link()`. The breadcrumb text reads `microblogs@<host>` in that case.
  Checking rule 2 costs a query per post. Do it once instead: a cached set of hosts that have a real
  `microblogs@<host>` community (5 minutes, redis via the existing cache helper).
- **Sidebar list.** On `/c/microblogs` itself (not on a server view), the sidebar shows "Servers": up
  to 10 instances with the most posts in the microblogs community in the last 24 hours, excluding
  instance 1. Each is shown with its count and links to its server view. The query groups by
  `Post.instance_id` over `posted_at > now - 24h` and is cached for 5 minutes.

### Errors and edge cases

- `/c/microblogs@<unknown host>`: 404.
- `/c/microblogs@<this server>`: 404. The local community is `/c/microblogs`.
- `/c/Microblogs@InfoSec.Exchange`: resolves case-insensitively, like other `/c/` lookups.
- A banned host: 404, even if its posts are stored.
- A post by a local user in the microblogs community: its link stays `/c/microblogs`.
- A post from a PieFed server stored in the local microblogs community (for example, a boosted post):
  `microblogs@<that host>` is a real community, so the link stays `/c/microblogs`.
- An ActivityPub request for `/c/microblogs@<host>`: 400, as for every `@` path today.

## Testing

- Resolution: a known host renders; unknown, banned, and own hosts get 404; a real community wins; an
  ActivityPub request gets 400; case is ignored.
- Query: a server view shows only that instance's posts, with the viewer's filters still applied.
- Page: hidden controls; sort, pagination and Live links keep `microblogs@<host>`; distinct etags.
- Live: `live_feed_keys` adds `instance:<id>` for a remote author in the microblogs community and not
  otherwise; the pattern accepts it; FastAPI accepts it; the fragment route resolves a server view and
  filters by instance.
- Discovery: `post_community_link` for remote, local, and real-community hosts; the breadcrumb; the
  sidebar list's ordering, its 24-hour window, its exclusion of instance 1, and its absence on server
  views.
