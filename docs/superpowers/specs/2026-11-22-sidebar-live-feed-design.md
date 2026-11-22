# The sidebar's live feed box

A "Live feed" box for both sidebars (home page and community pages): the newest posts arriving here, kept current
without a reload by the same loop as the home page's Live tabs (spec 2026-10-07-microblog-live-feed, Amendment A).

## Owner rulings, 2026-11-22

1. **Off by default.** The box keeps fetching while it is open, so it adds load only where someone chose it.
2. **Both sidebars**, each placing and hiding it on its own, like every other box.
3. **What it shows:** all posts by default, as the home page's Live, All tab. A user can choose posts from the people
   and communities they follow instead: the Subscribed tab's posts, which include followed people's posts in
   communities the user has not joined.

## An optional box

`app.sidebar.OPTIONAL` (`live_feed`) marks a box that is hidden until someone shows it:

- A stored order that does not name it hides it (`layout`), so every existing site, domain and user order hides it.
- It is offered to users whatever the domain chose (`domain_offered`). The domain's choice is only what users who have
  not chosen see: an admin who shows it shows it to everyone who has not hidden it. An admin cannot stop a user
  showing it. Each viewer's fetches are rate limited (below).
- Templates build it only when the request shows it (`shows_box`), so a hidden box costs nothing.

## The box

`_sidebar_live_feed.html`, for signed-in readers only; anonymous visitors never see it, because the fragment is
login-only, as Live is. It names what it shows ("All posts" or "Posts from people and communities you follow"),
links to the choice, and holds an empty list that `live_feed.js` fills: its first fetch (cursor 0) brings the
newest posts of the last hour. Without JavaScript it says it needs JavaScript. The dillo theme, for browsers without
JavaScript, does not take the box.

## The fragment

`GET /sidebar/live_posts?after=<id>` (`main.sidebar_live_posts`), `@login_required`.

- The home tab: `all`, or `subscribed` when `user.sidebar_live_feed == 'subscribed'` (`sidebar_live_filter`).
- `home_feed_source` and `get_deduped_post_ids(newer_than=(after, now - LIVE_WINDOW))`, as the home Live fragment,
  at most `SIDEBAR_LIVE_LIMIT` (10) newest.
- A title cannot be blurred or faded, so a post the teaser would filter, fade or blur for this viewer
  (`sidebar_live_hidden`: keyword filters, NSFW/NSFL, bots and reposters at blur or fade, gen-AI at fade, spoiler
  flair) is left out. The cursor still passes it.
- Each item: the title (90 characters) linking to the post, the author, the community and the time.
- 200 with `X-Live-Cursor` (the largest id fetched), 204 when nothing is new, 400 for a missing or unreadable `after`.
- `12/minute` keyed `live:<user id>:sidebar`: its own budget, apart from the home and community Live tabs.
- SSE: `live/stream?feed=any`, as the All and Subscribed tabs.

## Client

`live_feed.js` gains `config.compact`: nothing is held behind a pill while the reader scrolls (the box is not what
they are reading), the list keeps the newest `COMPACT_MAX_ITEMS` (10), and items need no teaser set-up. The bootstrap
starts both `#live_feed` and `#sidebar_live_feed`: a page with both loads the module once.

## The choice

`user.sidebar_live_feed` (`'all'` or `'subscribed'`, `NOT NULL`, default `'all'`; migration `8d2f6a1c4e70`). Settings,
Sidebar: "Live feed": *All posts* / *People and communities I follow*, one POST each (`live:all`, `live:subscribed`),
CSRF-checked. Any other `live:` value is a 400. Showing, hiding and placing the box are the page's ordinary buttons.
