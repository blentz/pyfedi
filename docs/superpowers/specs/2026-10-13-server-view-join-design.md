# Joining a server view

Status: design approved in session (2026-10-13). Fork-only work. Amends
`2026-10-08-microblog-instance-views-design.md`, whose non-goal "Joining or subscribing to a
per-server view" this reverses.

## Problem

A server view, `/c/microblogs@<host>`, lets a reader browse one server's microblogs, but there is no
way to have them arrive in a home feed short of following each author. A Mastodon server has no
Group to join, so its posts cannot reach the Subscribed tab the way a community's do.

## Goals

A signed-in user can Join a server view. Its public microblogs then appear in their Subscribed home
tab, as if they followed every account on that server, including accounts this instance has not seen
yet. Leave stops that.

Success: a user opens `/c/microblogs@infosec.exchange` and presses Join. A new public post by any
infosec.exchange account, stored in the microblogs community, appears in their Subscribed tab and its
Live view. After Leave it does not.

Non-goals:
- Any outbound federation. Join sends no Follow and Leave sends no Undo. Nothing is fetched.
- Listing joined server views among the user's joined communities, in the sidebar, or in Subscribed
  filters of the community list.
- Notifications for a joined server view (the bell stays hidden on server views).
- Changing the Popular, All, Local or Media tabs.

## Decisions (owner rulings, 2026-10-13)

1. **Join means a local subscription**, not bulk follows. A row records (user, instance). Rejected:
   a Follow to every known account on the host (thousands of activities, misses new accounts);
   that plus auto-following each new author (most traffic, reads as follow-spam).
2. **Public only.** A joined server contributes posts whose visibility is `public`
   (`listable_sql`), like its `/public/local` timeline. Unlisted and followers-only posts reach the
   feed only through the existing followed-author source.
3. **Placement:** the server view page (sidebar, mobile Options menu, dillo theme) and server-view
   rows in the All Communities list.
4. **Feeds:** every place `include_following` applies: the home Subscribed tab, its Live fragment, and
   the subscribed RSS feed (see Residuals).

## Design

### Model

`ServerViewMember(user_id, instance_id, created_at)`, primary key `(user_id, instance_id)`, both
foreign keys indexed by the key or an index on `instance_id`. One migration creates the table.
Deleting a user or an instance deletes its rows (`ondelete='CASCADE'`).

### Routes

The existing `POST /community/<actor>/subscribe` and `/unsubscribe` gain a branch, taken first, when
`actor_to_community(actor)` is `None` and `microblog_server_view(actor)` returns an instance:

- subscribe: insert the row if absent. No `do_subscribe`, no `subscriptions_count` change.
- unsubscribe: delete the row if present.

Both answer as they do for a community: htmx gets the swapped button, a plain form gets a redirect
back. A banned host does not resolve, so its view cannot be joined; a row for a host banned later is
inert because the feed already filters banned instances. Leave resolves the host from `Instance` by
domain, not through `microblog_server_view`, so a user can still leave a host that stopped resolving.

Keeping the community URLs means every button template keeps building `/community/<link>/subscribe`
from `link()`, and `ServerView.link()` already returns `microblogs@<host>`.

### Membership lookup

`joined_server_views(user_id) -> set[int]`: the instance ids a user joined. Memoized like
`joined_communities`, deleted on join and leave. The page, the community list, and the feed gate use
it.

### Feed source

A third follow-style source in `app/utils.py`, next to `FOLLOWED_AUTHOR_SQL`:

```
JOINED_SERVER_SQL = """(EXISTS (SELECT 1 FROM server_view_member svm
                                WHERE svm.user_id = :local_user_id
                                AND svm.instance_id = p.instance_id)
                        AND c.instance_id = 1 AND c.user_id = 1 AND c.name = 'microblogs' AND """ + listable_sql('p') + ')'
```

`get_deduped_post_ids` appends it when `include_following` and the viewer has joined at least one
server view, and matches the microblogs community on the joined `c` by `find_microblogging_community`'s own predicate. The existing gate
`current_user.num_following and include_following` stays for the two follow sources; the new source
has its own gate. The early `return []` for an empty `community_ids` no longer fires when a joined
server view gives the feed a source.

All the viewer's other filters already apply to it: blocked and banned instances (`p.instance_id`),
blocked users, NSFW, bots, languages, read and hidden posts.

### Live

No change. The Subscribed tab's Live view wakes on the `any` key, which every public post already
publishes.

### Page

On a server view, the sidebar, the mobile Options menu and the dillo side pane show Join when the
user has not joined it and Leave when they have. Create post, Membership, notification toggle, flair
and moderation controls stay hidden. The microblogs community's own page is unchanged.

### Community list

A server-view row shows Join or Leave by `joined_server_views`. An anonymous viewer sees Join, as for
a community row; the route then asks them to log in.

## Errors and edge cases

- Joining twice: one row. Leaving when not joined: no-op, same response.
- `/community/microblogs@<unknown host>/subscribe`: the existing community path, which 404s.
- A real `microblogs@<host>` community: the community path wins, as everywhere else.
- A post by a joined server's author that is also from a followed author: appears once (deduped).
- An instance blocked by the user after joining: its posts stay filtered; the row stays.

## Residuals

- **The subscribed RSS feed ignores follow sources.** `get_deduped_post_ids` reads `current_user`,
  and an RSS request authenticates by token without a session, so `current_user` is anonymous and no
  follow source is added. Followed authors are already missing from that feed today. A joined server
  view will be missing from it the same way. Fixing it needs `get_deduped_post_ids` to take the
  viewer as a parameter; tracked separately.

## Testing

- Model and routes: join creates one row; join twice keeps one; leave deletes; htmx responses swap
  the button; a plain form redirects; unknown host 404; a real community takes the community path;
  leave works after the host is banned.
- Feed: a joined server's public microblog is in the Subscribed tab; unlisted and followers-only are
  not; another server's microblog is not; a post by that server's author in another community is
  not; a user with no community memberships still gets the joined server's posts; a blocked instance
  hides them; the Live fragment returns them.
- Page: Join shows on a server view for a signed-in user, Leave after joining; Create post, the
  notification toggle and flair stay hidden; dillo shows the same; `/c/microblogs` is unchanged.
- List: a server-view row shows Join, then Leave after joining.
