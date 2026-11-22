# The microblog feed setting

A user setting, **Shown in Microblog Feed**, deciding what Mastodon and other microblog software see of a local user.
Code: `app/microblog_feed.py`; column `user.microblog_feed` (migration `9c1e42dfcd53`).

## Values

| Value | Label | The feed carries |
|---|---|---|
| `microblog` (default) | Microblog posts only | posts in a local microblogs community (`Community.kind == 'microblogs'`, no `ap_id`) |
| `community` | Community + microblog posts | those, and posts in every other community |
| `all` | All activity (includes comments & replies) | those, and every comment and reply |

"Microblog post" means the post's community, not `Post.microblog`. A titled post or an event in the microblogs community
counts. A post in a remote community of kind `microblogs` does not.

The column is `NOT NULL` with server default `microblog`. A value outside the three choices reads as the default, and the
settings form refuses one.

## Where it applies

1. **ActivityPub outbox** (`/u/<actor>/outbox`, `user_outbox_rows`): the posts the feed carries, and comments only at
   `all`. The visibility rules for logged-out viewers still apply on top.
2. **Mastodon client API**
   - `GET /api/v1/accounts/:id/statuses`: a local person's posts and comments, narrowed the same way.
   - `GET /api/v1/timelines/home`: each local author's posts and comments, narrowed by that author's setting. This
     includes the viewer's own rows: the setting is what every Mastodon client shows of the person.
   - Followers-only and direct rows are not narrowed, because their author addressed them to the reader.
   - Remote authors have no setting here, so all of their rows are shown.
3. **Pushes to the author's followers' servers**
   - `send_post` (Page path) and `send_peertube_video`: the author's followers are sent a community post only at
     `community` or `all`.
   - `send_microblog_note` always sends, because its posts are microblog posts.
   - `send_reply`, new: at `all`, a public comment is also sent to the hosts of the author's followers. The rule matches
     a post's follower copy: PieFed hosts, hosts already sent it (as the community's Announce or a mention), and
     undeliverable hosts are left out.
   - Mastodon's own home-feed filtering still applies on the receiving side. It shows a reply in a follower's home only
     when they also follow the person replied to, but the profile's "Posts and replies" tab shows it.

## What it does not change

- Followers-only posts and comments still reach accepted followers.
- Mentioned users still receive what mentions them.
- A community's followers still receive its Announces, so a Mastodon account that follows a community still sees its
  boosts.
- The web UI, the alpha API and the Lemmy API.

## Behaviour change

Every existing user starts at `microblog`. Their outbox and Mastodon feeds narrow to microblog posts, and their remote
followers stop receiving their community posts, until they choose a wider setting.
