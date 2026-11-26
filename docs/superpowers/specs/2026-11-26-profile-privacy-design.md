# Profile privacy

A local user chooses which parts of their profile other people see. By default a profile shows only their
microblog posts; community posts and comments are opt-in, and the followers list can be shown as a count or not at
all.

## Settings

Three columns on `user` (migration `f2b7c9d4e1a6`), shown on the settings page under "Visibility":

| Column | Type | Default | Form field |
|---|---|---|---|
| `profile_community_posts` | boolean, `NOT NULL` | `false` | "Show my posts in communities on my profile" |
| `profile_comments` | boolean, `NOT NULL` | `false` | "Show my comments on my profile" |
| `profile_followers` | `varchar(8)`, `NOT NULL` | `'list'` | "My followers": *Show who follows me* / *Show only how many* / *Don't show* (`list` / `count` / `none`) |

The defaults apply to existing accounts too: after the migration their profiles show only microblog posts until they
opt back in. The followers list keeps today's behaviour unless changed.

A **microblog post** is a post in the local microblogs community (`app.microblog_feed.microblog_community_ids`). A
**community post** is any other post. **Comments** are every comment and reply, the microblogs community's included.

## Who the settings bind

`app.profile_privacy.exempt(person, viewer_id)`: the settings do not apply when

- the person is remote (they have no such settings: whatever reached us is shown as before),
- the viewer is the person, or
- the viewer may `view user details` or `administer all users` on them (staff need the whole record).

Everyone else is bound, signed in or not: a profile setting that registering an account undoes would protect nothing.

The settings narrow what the profile already lists. Every existing rule (visibility, local-only, private
communities, deleted, under review) still applies first. A hidden post or comment is still readable on its own page
and in its community: the settings govern the profile's collection of a person's activity, not the content.

## Where they apply

- **Web profile** (`/u/<name>`): the Overview, Posts and Comments tabs (`app/user/utils.py`); the "Posts" and
  "Comments" counts become the counts of what the viewer can list, and the "Comments" line is left out when comments
  are hidden. Followers: `list` keeps the tab, `count` replaces it with a "Followers: N" line, `none` shows neither.
- **RSS** (`/u/<name>/feed`): community posts only when `profile_community_posts`. It never had comments.
- **Lemmy (alpha) API**: `post/list` and `comment/list` with `person_id` (and `user` with `include_content`), through
  `Listing.profile_privacy` / `CommentListing.profile_privacy`, which the alpha API sets. The person's `post_count`
  and `comment_count` (`reads_users.listable_counts`, used by every API) count only what is shown. `follower_count`
  is 0 when followers are `none`.
- **Mastodon API**: `followers_count` is 0 when `none`, and `hide_collections` is true unless `list`. The account's
  `statuses_count` follows `listable_counts` as above. Which statuses the account lists is unchanged: that is the
  microblog feed setting's (spec 2026-11-21-microblog-feed-setting).
- **ActivityPub** followers collection: no `totalItems` when `none`. It never listed followers.

## Not changed

- The outbox, the Mastodon API's account statuses and what is pushed to followers stay governed by the microblog
  feed setting: those are what microblog software sees of the person, chosen separately.
- The following list, moderated communities, joined communities (`show_subscribed_communities`) and feeds.
- Search and community pages: they are not the profile.
