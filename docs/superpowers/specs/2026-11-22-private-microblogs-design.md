# Private microblog posts

A local user decides whether their microblog posts are **public and federated** or **private and not federated**.

A private post is an ordinary post stamped `local_only`, the stamp a local-only domain's content already carries
(spec 2026-10-19-per-domain-identity §3.4). Everything that stamp already does applies unchanged:

- The outbound gate (`app.local_only.outbound_refusal`) refuses any activity that names it, so its Create, Update,
  Delete, Announce, boosts, likes and quotes never leave the server. Replies under it are stamped too
  (`content_local_only`'s parents), so the thread stays here as well.
- Logged-out readers and RSS never see it (`can_view`, `without_local_only`). Any signed-in user here can.
- The person's ActivityPub outbox leaves it out, the post's ActivityPub URL is a 404 to other servers, and its page
  carries no `alternate` link.
- It shows the "Local" badge.

## Choosing

- **Setting.** `user.microblogs_private` (boolean, `NOT NULL`, default `false`; migration `2daa520cdaa7`). Settings
  page: "My microblog posts": *Public and federated* / *Private: this server only, not federated*.
- **Per post.** In the local microblogs community (`is_local_microblogs`) the author can override the setting:
  - web compose form: the `private_microblog` select ("Privacy"), shown with Visibility and preset from the
    setting. It is a select rather than a checkbox because an unticked box sends nothing, which would be
    indistinguishable from a form that never showed the field.
  - alpha API `POST /api/alpha/post` (and scheduled posts): `local_only` true / false.
  - Mastodon API `POST /api/v1/statuses`: `local_only`, as glitch-soc names it.
- The quick post box has no options, so it uses the setting.

`app.shared.post.requested_local_only` is the one rule:

1. A local-only author or community: private.
2. In the local microblogs community: the per-post choice when one was sent (web: the field was submitted; API: the
   key is present and not null), else the setting.
3. Anywhere else: private only when an API caller asks (`local_only: true`, as PeerTube's Internal already did).
   `false` never makes a post public that rule 1 makes private, and the setting does not apply to community posts.

## Fixed at creation

Like visibility, it is decided when the post is made. The edit form has no field for it and shows it with the
visibility ("Public, private: this server only"). Changing the setting changes only new posts: nothing already sent
is retracted and nothing already private is announced. A scheduled post's copies take the stamp with every other
column.

## Cross-posting

The compose form is never prefilled from a private post for a community where the new post would federate
(`content_local_only` of the caller and the target community is false): that would republish it. 404, as for a
followers-only source.

## Not changed

- Comments and replies follow their parents, not the setting.
- Followers-only posts can also be private: then only the author's followers on this server see them.
