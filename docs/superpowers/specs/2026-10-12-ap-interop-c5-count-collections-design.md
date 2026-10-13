# AP interop C5: count-only `following`, `liked`, `likes`, `dislikes` and `shares` — design

Date: 2026-10-12. Track: tier C of `docs/superpowers/specs/2026-10-10-ap-gaps-roadmap.md` ("`following` on Group;
`liked`; `likes` and `shares` on objects").

## Goal

Our actors and objects advertise the standard collections, so a peer that reads them gets real numbers. Mastodon 4.3+
shows a remote post's like and boost counts from `likes.totalItems` and `shares.totalItems`; today our posts carry
neither, so they show 0 there.

Out of scope: item lists, paging, reading remote `likes`/`shares` (the existing PeerTube read in
`update_post_from_activity` is unchanged), any inbox change.

## Owner rulings (2026-10-12)

1. **Count-only.** Every new collection carries `totalItems` and no items, as C1 ruled for a user's followers and
   following and D175 for a community's followers. No voter, booster or followed account is disclosed.
2. **`dislikes` too.** Posts and comments also carry a `dislikes` count (PeerTube's extension, which this codebase
   already reads from PeerTube).
3. **`vote_privately` reports 0.** A user with `User.vote_privately` set has a `liked` collection whose `totalItems`
   is 0. The property is still advertised, so its absence does not reveal the setting.
4. **Inline and served.** Posts and comments embed each collection inline as `{id, type: "Collection", totalItems}`
   (Mastodon reads the inline `totalItems` without fetching), and the same document is served at its `id`.

## Group `following`

- `community_actor_data` (`app/activitypub/util.py`) adds `"following": f"{community.public_url()}/following"`.
- New route `GET /c/<actor>/following` (`app/activitypub/routes.py`), next to `community_followers`:
  - a non-ActivityPub request redirects to the community profile, as `community_followers` does;
  - the community is looked up as `community_followers` looks it up (`name=actor, banned=False, ap_id=None`); none →
    404; `local_only` or `private` → 403;
  - answer: `count_only_collection(f'{SERVER_URL}/c/{actor}/following', 0)` — a community follows nothing.

## Person `liked`

- The Person actor document adds `"liked": f"{user.public_url()}/liked"`.
- New route `GET /u/<actor>/liked`, next to `user_following`:
  - a non-ActivityPub request redirects to the profile; `local_actor_user(actor)` gives 410/404 as for followers;
  - answer: `count_only_collection(f"{user.public_url()}/liked", user_liked_count(user))`.
- `user_liked_count(user) -> int` (new, `app/activitypub/util.py`, beside `user_following_count`): 0 when
  `user.vote_privately`; otherwise the number of the user's `PostVote` rows with `effect > 0` plus `PostReplyVote` rows
  with `effect > 0`, counting only votes `send_vote` would have federated: the voted object is not deleted, its
  visibility is in `OPEN_VISIBILITIES`, and its community is neither `local_only` nor `private`.

## Post and comment counts

### Inline

- `post_to_page` adds:
  - `"likes": {"id": f"{SERVER_URL}/post/{post.id}/likes", "type": "Collection", "totalItems": post.up_votes or 0}`
  - `"dislikes": {... "/dislikes", "totalItems": post.down_votes or 0}`
  - `"shares": {... "/shares", "totalItems": <count of PostBoost rows for the post>}`
- `comment_model_to_json` adds `likes` and `dislikes` with ids under `/comment/{reply.id}/` and counts
  `reply.up_votes or 0`, `reply.down_votes or 0`. No `shares`: comments have no boost records, and an inline 0 could
  make a peer overwrite its own count with 0.
- One builder, `count_collection(collection_id: str, total: int) -> dict` (new, `app/activitypub/util.py`), makes the
  inline object; the routes below serve the same dict plus `@context`.

### Served

New GET-only routes (Flask answers HEAD, as D192):

- `/post/<int:post_id>/likes`, `/post/<int:post_id>/dislikes`, `/post/<int:post_id>/shares`
- `/comment/<int:comment_id>/likes`, `/comment/<int:comment_id>/dislikes`

Each:

- a non-ActivityPub request redirects to the object's page;
- the object is fetched by id; none → 404; not local → 404 (only our own objects advertise these ids);
- refused exactly as its object route refuses: posts through `post_ap_refusal`; comments through a new
  `comment_ap_refusal(reply)` that holds `comment_ap`'s existing checks in their existing order (local_only/private
  403, followers-only without entitlement 404, deleted → tombstone, author-blocked instance 401), and `comment_ap`
  calls it so the two cannot diverge;
- answers `count_collection(...)` plus `@context`, `ap_media_type()`, and `ap_content_headers(resp, obj)` as the
  replies route does. The counts equal the inline ones for the same object.

## Errors and limits

- Counts are read from cached columns (`up_votes`, `down_votes`) or one `COUNT` query; no fetch, sleep or retry on any
  path (D738/D775).
- A NULL cached count is served as 0.

## Testing

TDD, 100% line and branch coverage of changed lines, one commit per defect, gates via `./run_tests.sh`.

- Group: the actor JSON advertises `following`; the route answers 0; 403 for local-only and private; 404 for unknown
  and remote; non-AP redirect.
- Person: the actor JSON advertises `liked`; the count includes post and comment upvotes, excludes downvotes, votes in
  local-only and private communities, followers-only and deleted objects; `vote_privately` gives 0; 410/404 as the
  followers route; non-AP redirect.
- Objects: inline counts in `post_to_page` and `comment_model_to_json`; comments carry no `shares`; each route's body
  equals the inline object plus `@context`; each route refuses exactly as its object route for every refusal case;
  non-local → 404; `comment_ap` behaviour is unchanged after the refactor.

## Review focus

- No route discloses an object, or that an object existed, that its object route would refuse.
- `liked` never counts a vote that was not federated.
- No item lists anywhere.
