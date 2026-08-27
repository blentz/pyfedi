# Permission call-site audit (inverse)

Sub-project 1b-i, Task 9. Branch `coverage-utils-db`.

Tasks 2-8 covered eight permission functions in `app/utils.py`:
`can_create_post`, `can_create_post_reply`, `can_downvote`, `can_upvote`,
`can_upload_video`, `user_access`, `role_access`, `authorise_api_user`.

A correct permission function is worthless at a route that never calls it. This
document is the **inverse** audit: it enumerates entry points by **what the
action does**, not by what it calls, so that a path which never consults the
guard still appears. Every row names the guard, or **NONE**.

**Nothing in `app/` was changed by this audit.**

---

## Status: found deliberately, deferred deliberately

The project owner has ruled on the unguarded paths below. The ruling is
**defer**: they are to be evaluated after the coverage campaign's testing work
lands, not fixed now.

- The 14 unguarded rows are **known, accepted, and deferred pending completion
  of the testing improvements.**
- They are **not latent oversights.** Each was found by deliberate inverse
  audit — searching by what an action does, not by what it calls — and each
  carries a verdict in the tables below.
- **Re-evaluation is expected once the testing improvements are in place**,
  because better coverage of the surrounding code changes what a safe fix looks
  like. Several of these paths share sinks (`create_resolved_object`,
  `resolve_remote_post_from_search`) whose behaviour is not yet covered; a fix
  designed against the current level of coverage would be a guess.

If you are reading this in six months and wondering whether anyone noticed:
someone did. The list below is what they noticed, and this section is the
record that leaving it was a decision rather than an omission.

Nothing here pre-judges the fix. The purpose is that a future reader can pick
these up and evaluate them, with the evidence already gathered.

### Deferred for evaluation

Ordered by exposure — how easily an outside party can reach the path — not by
file. "Upstream provides" is the part that matters most: several of these have
real partial protection, and a reader who misses that will either over-react to
a signature-checked path or under-react to an anonymous one.

**1. `GET /api/alpha/resolve_object` — anonymous callers can persist remote
content.** `app/api/alpha/routes.py:145-150` → `app/api/alpha/utils/misc.py:47`,
reaching `create_resolved_object` at `:417` and `:422` (2 rows: post branch and
reply branch).
*Unguarded:* no `can_create_post` / `can_create_post_reply`, and auth is
optional — `if auth: user_id = authorise_api_user(auth)`, so with no
`Authorization` header the endpoint proceeds with `user_id=None`.
*Upstream provides:* `enable_api()` only, a site-wide on/off switch. There is no
`before_request` auth hook on the API blueprint. This is the only path in the
deferred set reachable with no credential of any kind, which is why it is first.

**2. Poll voting — no permission check on any of its three entry points.**
Web `app/post/routes.py:635-639`, API `app/api/alpha/utils/post.py:1790`, AP
inbox `app/activitypub/routes.py:2434` (3 rows).
*Unguarded:* no `can_upvote`, no `can_downvote`, no `communities_banned_from`,
no `community.local_only` on the AP path, and no `VOTE_QUOTA` on any of the
three — post and reply voting have one.
*Upstream provides:* `vote_for_poll` (`app/shared/post.py:1148`) checks
`user.banned` and `user_ip_banned()`; the API route additionally calls
`authorise_api_user`; `process_poll_vote` checks
`instance_banned(user.instance.domain)`. So the actor is identified and
site-level bans apply — but nothing asks whether that actor may vote *here*.
Poll voting has no permission function of its own, so this is a design gap
rather than a skipped call.

**3. `create_resolved_object` — signature-checked, but no ban or allowlist
enforcement.** `app/activitypub/util.py:3924`, calling `create_post` at `:3979`
and `create_post_reply` at `:3962`. Reached from `process_announce_of_uri` →
`resolve_remote_post`, from `process_microblog_announce`, and from the Celery
task `get_nodebb_replies_in_background` (4 rows).
*Unguarded:* no `can_create_post` / `can_create_post_reply`, so none of
`user.banned`, `user.ban_posts`, `community.banned`,
`community.restricted_to_mods`, `communities_banned_from`, `banned_instances`,
`instance_banned`, or ALLOWLIST_INTENSE is enforced. The shared inbox has no
general `instance_banned` gate either — only `allowlist_mode >=
ALLOWLIST_STRONG` at `app/activitypub/routes.py:673` — so defederation is
enforced per-action, and this action does not enforce it.
*Upstream provides:* real protection, and it should not be discounted — HTTP
signature verification at the inbox; `announcer_is_followed` on the microblog
path; an attributedTo/URI domain-match check that blocks impersonation;
`community.local_only` and non-public-visibility refusal inside `create_post`;
and, for replies, `post.comments_enabled`, `user.ban_comments`,
`blocked_phrases` and `has_blocked_user` inside `PostReply.new`. What is missing
is specifically the *ban and federation-policy* layer.
Note `Post.new` does read `communities_banned_from(user.id)` at
`app/models.py:2202` — but only to suppress the notification. The post is
created either way.

**4. `resolve_remote_post_from_search` — the AP `Move` path has no requester at
all.** `app/activitypub/util.py:4035`, calling `create_post_reply` at `:4109`
and `create_post` at `:4112`. Two entry points: AP inbox `Move`
(`app/activitypub/routes.py:1575`) and web `search.retrieve_remote_post`
(`app/search/routes.py:228`) (3 rows: two post rows, one shared reply row).
*Unguarded:* no `can_create_post` / `can_create_post_reply` — same missing
checks as item 3.
*Upstream provides:* the `uri_domain == actor_domain` impersonation check, and
on the web entry point `@login_required` plus a `current_user.banned` refusal.
That gates the *requester*; it does not gate the *author* of the content
fetched. The `Move` entry point has no requester to gate — it is inbox-driven.

**5. `retrieve_mods_and_backfill` — no attributedTo domain-match, so any
instance can be attributed.** Celery task, `app/community/util.py:93`, calling
`create_post` at `:198` and `PostReply.new` at `:272` (2 rows).
*Unguarded:* no `can_create_post` / `can_create_post_reply`; and, unlike item 3,
**no check that the post's author domain matches the community's server** — the
backfill takes `activity['attributedTo']` straight to `find_actor_or_create` at
`app/community/util.py:184`. A remote community's outbox can therefore attribute
posts to accounts on any instance, defederated ones included.
*Upstream provides:* `search_for_community` (`app/community/util.py:38-41`)
refuses a server that fails `instance_allowed` or passes `instance_banned`, so
the *community's* instance is checked. Nothing checks the *author's*.
Triggerable by any user who causes a new remote community to be fetched (search
UI, feed subscription at `app/feed/util.py:115`, profile import at
`app/user/routes.py:1314`) — and, via item 1, by an unauthenticated caller.

**Count:** 14 rows across 10 distinct entry points, reaching 5 sinks. Twelve of
the fourteen carry a literal **NONE** in the tables; the two poll-voting rows at
item 2 carry a partial guard (`authorise_api_user` only, `instance_banned` only)
that is not a permission check for voting.

### Tracked follow-up: item 1 (`GET /api/alpha/resolve_object`)

This section does not change item 1's status above — it is still **deferred,
not fixed**, on the same "defer" ruling as the other thirteen. What changes is
visibility: this branch's final reviewer went through all fourteen and singled
out item 1 as the one severe enough to need a tracked action item of its own,
rather than living only as a row in the table above, because it is the only
member of the set reachable with **no credential of any kind** — `if auth:` at
`app/api/alpha/utils/misc.py:50` makes authentication optional, so the sole
gate between an anonymous caller and `create_resolved_object` persisting
arbitrary remote posts and replies is `enable_api()`, a site-wide on/off
switch with no `before_request` auth hook behind it. In the reviewer's words:
"F3 (`GET /api/alpha/resolve_object`, anonymous write path) deserves a tracked
follow-up now, not just a paragraph in an audit document."

This document does not prescribe a fix. The point of tracking it is that it
stays findable and gets evaluated on its own timeline — which the reviewer
put at "weeks, not quarters" rather than waiting for the whole deferred batch
— not that a remediation approach is pre-decided here.

This needed its own section rather than a note added to item 1's bullet above:
the "Deferred for evaluation" list is presented, and should stay presented, as
one accepted batch of fourteen with one shared ruling. Marking one member as
individually tracked inline would either read as a change to that shared
ruling (it is not) or get lost the next time someone skims the batch assuming
every item in it reads the same as its neighbours.

### Recorded with verdicts, but NOT part of the deferred 14

These four are kept separate so the counts above stay honest. They are findings
with verdicts, not members of the deferred set: three are asymmetries or
wrong-subject calls rather than absent guards, and one is a read rather than a
write.

- **F5 — AP `Update` re-checks replies but not posts** (2 rows).
  `app/activitypub/routes.py:2298-2299` checks authorship/mod only; the reply
  branch twelve lines below at `:2341` does the same check *and*
  `can_create_post_reply`. The PeerTube `Video` update at `:1240-1241` checks
  only `user.id == post.user_id`. A guard is present on this path — the two
  branches simply disagree about which one.
- **F10 — `emoji_set` routes skip `@validation_required` / `@approval_required`**
  (2 rows). `app/post/routes.py:606-608` and `:620-622` carry no decorators and
  check `current_user.is_authenticated` in-body. The vote still reaches
  `can_upvote`, so the permission function *is* consulted; what is missing is
  the verified/approved check the four sibling vote routes get from decorators,
  which `can_upvote` does not perform.
- **F13 — `/admin/` has no `@permission_required`** (1 row).
  `app/admin/routes.py:55`, the one route of 53 in that file without one. It
  renders host load, CPU count, disk usage, loaded plugins and their hooks,
  translation languages, and overdue cron jobs to any authenticated user. `POST`
  is declared but the body handles no form: this is an unguarded **read**, not a
  write, which is why it is not in the deferred set.
- **F12 — `can_upload_video()` called with no user** (2 rows).
  `app/shared/post.py:200` and `:464`. The guard is called; it is called with
  the wrong subject. It fails closed, so it is a correctness and consistency
  defect rather than a bypass. **See the cross-reference below.**

The remaining verdicts — F6, F7, F8, F9, F14 — are in the findings section and
are upstream-gated or deliberate; they are not in the deferred set either. One
of them is worth knowing about anyway: **F7**, where a repeating scheduled post
is re-published without re-checking `can_create_post`, so the guard ran once at
schedule time and never again. That is a timing gap rather than an absent
guard, which is why it sits here and not in the deferred list, but it is the
only one of the five that is not simply "working as intended".

### F12 and Task 7 are the same defect from two directions

Task 7 audited `can_upload_video` forward and found that its `'users'` branch
**ignores its own injected user**: the condition is
`not current_user.is_authenticated and user is None`, so the moment `user` is
anything other than `None` the compound is `False` regardless of what was
passed, and the function falls through to `return True` — for a banned user, a
deleted user, an unverified user, even a bare `AnonymousUserMixin()`. Recorded
in `.superpowers/sdd/2026-08-25-coverage-utils-permissions/task-7-report.md`
and in the docstring of `tests/test_utils_upload_video.py`.

This audit approached the same function backwards and found F12: `make_post`
(`app/shared/post.py:200`) and `edit_post` (`:464`) call it as
`can_upload_video()` with **no** user at all, on a path also used by `SRC_API`
where `current_user` is anonymous and the real caller is a token owner.

Put together, the `'users'` branch never consults the actual API caller in
either direction:

- pass a user (`process_upload`, `app/shared/upload.py:22`) → `user is None` is
  `False`, the branch is inert, everyone gets `True`;
- pass no user (`make_post` / `edit_post`) → `current_user` is anonymous, the
  branch matches, and the legitimate API caller gets `False`.

The `user 1` and `admins` branches have the second half of this problem too:
called with no user, they evaluate `current_user.get_id()` and
`current_user.is_admin_or_staff()` against the anonymous web user rather than
against the token's owner.

A reader who finds either half should be led to the other; neither is complete
on its own.

---

## How the enumeration was done

The searches below are the whole method. They are re-runnable; re-run them
before trusting this table.

    # 1. Every construction of the gated models, anywhere in the app.
    grep -rn "Post(" app/ --include=*.py | grep -v "PostReply(\|PostVote(\|PostReplyVote(\|PostBookmark("
    grep -rn "PostReply(" app/ --include=*.py
    grep -rn "PostVote(" app/ --include=*.py
    grep -rn "PostReplyVote(" app/ --include=*.py

    # 2. The model-layer factories those searches reveal, and their callers.
    grep -rn "Post\.new(\|PostReply\.new(" app/ --include=*.py
    grep -rn "\.vote(" app/ --include=*.py

    # 3. The shared-layer funnel, and its callers.
    grep -rn "make_post(\|edit_post(\|make_reply(\|edit_reply(" app/ --include=*.py
    grep -rn "vote_for_post(\|vote_for_reply(\|vote_for_poll(" app/ --include=*.py
    grep -rn "process_upload(" app/ --include=*.py

    # 4. The ActivityPub ingest functions, and their callers.
    grep -rn "create_post(\|create_post_reply(" app/ --include=*.py
    grep -rn "create_resolved_object(\|resolve_remote_post(\|resolve_remote_post_from_search" app/ --include=*.py
    grep -rn "update_post_from_activity(\|update_post_reply_from_activity(" app/ --include=*.py

    # 5. Video specifically: the only three places video extensions are admitted.
    grep -rn "mp4\|webm\|\.mov" app/ --include=*.py

    # 6. A path that bypasses the ORM entirely would not show up above.
    grep -rni 'INSERT INTO "\?post' app/ --include=*.py      # -> no hits

    # 7. user_access / role_access: admin routes without @permission_required,
    #    and every mutating route in every blueprint without any auth decorator.
    #    (AST walk over app/*/routes.py collecting each route's decorator stack.)

    # 8. authorise_api_user: every post_/put_/delete_ in app/api/alpha/utils/
    #    that neither calls authorise_api_user nor calls an app/shared/ function
    #    that does.

Search 6 returned nothing: there is no raw-SQL insert into `post`, `post_reply`,
`post_vote` or `post_reply_vote`. Every write goes through the ORM, so searches
1-3 are exhaustive for content creation and voting.

Entry-point classes covered: web routes (`app/*/routes.py`), API routes
(`app/api/alpha/`), ActivityPub inbox (`app/activitypub/`), the NNTP gateway
(`app/nntp/`), CLI commands (`app/cli.py`), and Celery tasks
(`app/shared/tasks/`, `app/community/util.py`, `app/activitypub/util.py`).

`app/shared/tasks/` was checked and creates no local content: it is
federation-out only (searches 1-3 return no hits under that directory).

---

## Table: post creation

| action | entry point | guard | notes |
|---|---|---|---|
| create post | web `community.add_post`, `app/community/routes.py:1018` → `make_post` at `:1086` | `can_create_post` | via `app/shared/post.py:187`. Route also has `@login_required @validation_required @approval_required` and an in-body `banned / ban_posts / user_ip_banned` check. |
| create post | API `POST /api/alpha/post` → `post_post`, `app/api/alpha/utils/post.py:1474` | `authorise_api_user` + `can_create_post` | both inside `make_post`. |
| create post | NNTP `POST` (no `References`), `app/nntp/server.py:645` → `post_post` | `authorise_api_user` + `can_create_post` | delegates to the API function with the client's bearer token. |
| create post | AP inbox `Create`/`Update` of Page/Article/Link/Note/Question/Event, new object — `app/activitypub/routes.py:2308` | `can_create_post` | the well-guarded case. Rejected posts get a proactive `Delete` back. |
| create post | web `add_local` → `publicize_community` → `make_post`, `app/community/util.py:981` | `can_create_post` | posts an announcement into a remote "new communities" community. |
| create post | CLI `rss-feeds` → `make_post(SRC_API)`, `app/cli.py:1358` | `authorise_api_user` + `can_create_post` | uses the feed owner's API token. |
| create post | web `post.post_cross_post`, `app/post/routes.py:2258` | (redirects) | sets a cookie and 302s to `community.add_post`; creates nothing itself. |
| edit post | web `post.post_edit` → `edit_post`, `app/post/routes.py:1074` | **NONE** | see F6. |
| edit post | API `PUT /api/alpha/post` → `edit_post`, `app/api/alpha/utils/post.py:1521` | `authorise_api_user(id_match=post.user_id)` | ownership enforced by the guard itself. |
| edit post | web `post.post_fixup_from_remote` → `update_post_from_activity`, `app/post/routes.py:2254` | `user_access` via `@permission_required('change instance settings')` | admin only. |
| edit post | AP inbox `Update` of an existing post, `app/activitypub/routes.py:2298-2299` | **NONE** | see F5. |
| edit post | AP inbox `Update` of a PeerTube `Video`, `app/activitypub/routes.py:1240-1241` | **NONE** | see F5. |
| create post | AP Announce-of-URI, community path: `process_announce_of_uri` → `resolve_remote_post` → `create_resolved_object` → `create_post`, `app/activitypub/util.py:3679, 3921, 3979` | **NONE** | see F1. |
| create post | AP Announce-of-URI, microblog boost: `process_microblog_announce` → `create_resolved_object` → `create_post`, `app/activitypub/util.py:3634, 3979` | **NONE** | see F1. |
| create post | AP inbox `Move` of a post not held locally → `resolve_remote_post_from_search`, `app/activitypub/routes.py:1575` → `app/activitypub/util.py:4112` | **NONE** | see F2. |
| create post | web `search.retrieve_remote_post` → `resolve_remote_post_from_search`, `app/search/routes.py:235` → `app/activitypub/util.py:4112` | **NONE** | see F2. |
| create post | API `GET /api/alpha/resolve_object` → `create_resolved_object` → `create_post`, `app/api/alpha/utils/misc.py:417, 422` | **NONE** | see F3. **Auth is optional on this endpoint.** |
| create post | Celery `retrieve_mods_and_backfill` → `create_post`, `app/community/util.py:198` | **NONE** | see F4. |
| create post | CLI `lemmy-import` → `Post(...)` direct, `app/cli.py:597` | **NONE** | see F8. |
| publish post | CLI `publish-scheduled-posts`, repeat occurrence → `Post()` copy, `app/cli.py:1066-1090` | **NONE at publish** | see F7. |

## Table: reply creation

| action | entry point | guard | notes |
|---|---|---|---|
| create reply | web `post.show_post` top-level reply → `make_reply`, `app/post/routes.py:153` | `can_create_post_reply` | via `app/shared/reply.py:178`. |
| create reply | web `post.add_reply` → `make_reply`, `app/post/routes.py:838` | `can_create_post_reply` | same. |
| create reply | web `post.add_reply_inline` (htmx) → `PostReply.new` direct, `app/post/routes.py:916` | `can_create_post_reply` | checked in the route itself at `app/post/routes.py:873`. This is a direct-to-model call that IS guarded — worth noting because the model factory does not guard itself. |
| create reply | API `POST /api/alpha/comment` → `make_reply`, `app/api/alpha/utils/reply.py:478` | `authorise_api_user` + `can_create_post_reply` | |
| create reply | NNTP `POST` with `References`, `app/nntp/server.py:624` → `post_reply` | `authorise_api_user` + `can_create_post_reply` | |
| create reply | AP inbox `Create` Note, new object, `app/activitypub/routes.py:2351` | `can_create_post_reply` | |
| edit reply | AP inbox `Update` of an existing reply, `app/activitypub/routes.py:2341` | `can_create_post_reply` | re-checked on edit — the post path does not do this (F5). |
| edit reply | web `post.post_reply_edit` → `edit_reply`, `app/post/routes.py:1897` | **NONE** | `@login_required` + in-body `post_reply.user_id == current_user.id`. Same shape as F6. |
| edit reply | API `PUT /api/alpha/comment` → `edit_reply`, `app/api/alpha/utils/reply.py:499` | `authorise_api_user(id_match=reply.user_id)` | |
| create reply | AP Announce-of-URI → `create_resolved_object` → `create_post_reply`, `app/activitypub/util.py:3962` | **NONE** | see F1. |
| create reply | `resolve_remote_post_from_search` → `create_post_reply`, `app/activitypub/util.py:4109` | **NONE** | see F2 / F3. |
| create reply | API `GET /api/alpha/resolve_object` (reply branch), `app/api/alpha/utils/misc.py:417` | **NONE** | see F3. |
| create reply | Celery `retrieve_mods_and_backfill` → `PostReply.new`, `app/community/util.py:272` | **NONE** | see F4. |
| create reply | Celery `get_nodebb_replies_in_background` → `resolve_remote_post`, `app/activitypub/util.py:4002` | **NONE** | see F1; same sink. |
| create reply | CLI `lemmy-import` → `PostReply(...)` direct, `app/cli.py:705` | **NONE** | see F8. |
| (not a write) | `app/post/util.py:55` `PostReply()` | n/a | transient, never added to a session — rehydrates archived replies for rendering only. Listed so the `PostReply(` search result is accounted for. |

## Table: vote recording

| action | entry point | guard | notes |
|---|---|---|---|
| post vote | web `post.post_vote`, `app/post/routes.py:539-544` | `can_upvote` / `can_downvote` | via `app/shared/post.py:34-42`. `@login_required @validation_required @approval_required`. |
| reply vote | web `post.comment_vote`, `app/post/routes.py:552-556` | `can_upvote` / `can_downvote` | via `app/shared/reply.py:21-25`. |
| reply vote | web `post.comment_emoji_reaction`, `app/post/routes.py:564-568` | `can_upvote` / `can_downvote` | |
| post vote | web `post.post_emoji_set`, `app/post/routes.py:606-608` | `can_upvote` | **but no `@login_required` / `@validation_required` / `@approval_required`** — see F10. |
| reply vote | web `post.comment_emoji_set`, `app/post/routes.py:620-622` | `can_upvote` | same as above — see F10. |
| post vote | API `POST /api/alpha/post/like`, `app/api/alpha/utils/post.py:1399` | `authorise_api_user` + `can_upvote`/`can_downvote` | |
| reply vote | API `POST /api/alpha/comment/like`, `app/api/alpha/utils/reply.py:444` | `authorise_api_user` + `can_upvote`/`can_downvote` | |
| post/reply vote | AP inbox `Like`, `app/activitypub/routes.py:2400` | `can_upvote` + `instance_banned` + `blocked_users` + `VOTE_QUOTA` | |
| post/reply vote | AP inbox `Dislike`, `app/activitypub/routes.py:2421` | `can_downvote` + `instance_banned` + `blocked_users` + `VOTE_QUOTA` | |
| author self-upvote | `make_post`, `app/shared/post.py:226` | **NONE** | F9 — deliberate. |
| author self-upvote | `Post.new`, `app/models.py:2207` | **NONE** | F9 — deliberate. |
| author self-upvote | `PostReply.new`, `app/models.py:2944` | **NONE** | F9 — deliberate. |
| author self-upvote | CLI `publish-scheduled-posts`, `app/cli.py:1096` | **NONE** | F9 — deliberate. |
| poll vote | web `post.poll_vote`, `app/post/routes.py:635-639` | **NONE** | see F11. |
| poll vote | API `POST /api/alpha/post/poll_vote`, `app/api/alpha/utils/post.py:1790` | `authorise_api_user` only | see F11. |
| poll vote | AP inbox poll vote, `app/activitypub/routes.py:2434` | `instance_banned` only | see F11. |

## Table: video and file upload

Video extensions (`.mp4`, `.webm`, `.mov`) are admitted in exactly three places
in the whole codebase (search 5). All three consult `can_upload_video`.

| action | entry point | guard | notes |
|---|---|---|---|
| choose video file | web `community.add_post`, `app/community/routes.py:1089` | `can_upload_video()` | no-arg form; `current_user` is the real web user here, so correct. |
| admit video ext | `make_post`, `app/shared/post.py:200` | `can_upload_video()` | **no `user` argument** — see F12. |
| admit video ext | `edit_post`, `app/shared/post.py:464` | `can_upload_video()` | **no `user` argument** — see F12. |
| admit video ext | `process_upload`, `app/shared/upload.py:22` | `can_upload_video(user)` | correct: the caller's user is passed. |
| upload media | API `POST /api/alpha/upload/image`, `app/api/alpha/utils/upload.py:31` | `authorise_api_user` + `can_upload_video(user)` | |
| upload community image | API `POST /api/alpha/upload/community_image`, `app/api/alpha/utils/upload.py:37` | `authorise_api_user`; `user=None` | `user=None` means video extensions are never admitted. Correct by construction. |
| upload user image | API `POST /api/alpha/upload/user_image`, `app/api/alpha/utils/upload.py:43` | `authorise_api_user`; `user=None` | same. |
| upload media | web `user.user_file_upload`, `app/user/routes.py:2345-2363` | `@login_required` + `can_upload_video(current_user)` | ten `process_upload` calls, all with `user=current_user`. |
| community icon/banner | `edit_community` (also reached from `make_community`), `app/shared/community.py:310-311` | `user=None` | no video. |
| feed icon/banner | `make_feed` / `edit_feed`, `app/shared/feed.py:172-173, 249-250` | `user=None` | no video. |

## Table: `user_access` / `role_access` — admin surface

53 routes in `app/admin/routes.py`; 52 carry `@permission_required`.

| action | entry point | guard | notes |
|---|---|---|---|
| all admin routes except one | `app/admin/routes.py` | `user_access` via `@permission_required` | |
| view admin dashboard | `app/admin/routes.py:55` `admin_home`, `GET,POST /admin/` | **NONE** (`@login_required` only) | see F13. |
| all 50 admin routes with both decorators | `app/admin/routes.py` | `user_access` | decorator order puts `@permission_required` **outside** `@login_required` — see F14. |

## Table: `authorise_api_user` — API mutating surface

Every `post_*` / `put_*` / `delete_*` in `app/api/alpha/utils/` either calls
`authorise_api_user` directly or calls an `app/shared/` function that does, with
exactly three exceptions, all of which are correctly public:

| entry point | guard | verdict |
|---|---|---|
| `post_user_verify_credentials`, `app/api/alpha/utils/user.py:878` | NONE | correct — it *is* the credential check. |
| `post_user_logout`, `app/api/alpha/utils/user.py:1000` | NONE | correct — decodes and revokes the presented JWT itself. |
| `post_user_register`, `app/api/alpha/utils/user.py:1026` | NONE | correct — registration is pre-auth. Body is a stub (`...`). |

`GET /api/alpha/resolve_object` is a `get_*` and so is outside this scan's
shape, which is exactly why it needed the by-effect search — see F3.

---

## Findings: every NONE row, with its verdict

### F1 — `create_resolved_object` creates posts and replies with no permission check *(remote-triggerable)*

`app/activitypub/util.py:3924` `create_resolved_object` calls `create_post`
(`:3979`) and `create_post_reply` (`:3962`). Neither `can_create_post` nor
`can_create_post_reply` is consulted on this path. It is reached from the
ActivityPub inbox by:

- `process_announce_of_uri` → `resolve_remote_post` (`:3921`) — an `Announce`
  whose object is a bare URI, addressed to a community;
- `process_microblog_announce` (`:3634`) — an `Announce` of a bare URI with no
  community, i.e. a microblog boost;
- `get_nodebb_replies_in_background` (`:4002`), a Celery task fed by the above.

**What upstream does gate it:** HTTP signature verification at the inbox;
`announcer_is_followed` on the microblog path; a domain-match check
(`announce_actor_domain == uri_domain` in `resolve_remote_post`, and
`uri_domain == actor_domain` in `create_resolved_object`) that blocks
impersonation; `community.local_only` and non-public visibility inside
`create_post`; `post.comments_enabled`, `user.ban_comments`, `blocked_phrases`
and `has_blocked_user` inside `PostReply.new`.

**What is not checked on this path but is checked by `can_create_post`:**
`user.banned`, `user.ban_posts`, `community.banned`,
`community.restricted_to_mods`, `communities_banned_from(user.id)`,
`banned_instances(user.id)`, `instance_banned(user.ap_domain)`, and
`ALLOWLIST_INTENSE` allowlist mode. The shared inbox does **not** apply a
general `instance_banned` check of its own — only `allowlist_mode >=
ALLOWLIST_STRONG` (`app/activitypub/routes.py:673`) — so defederation is
enforced per-action, and this action does not enforce it.

`Post.new` reads `communities_banned_from(user.id)` at `app/models.py:2202`, but
only to suppress the notification; the post is created regardless.

**Verdict: unguarded, remote-triggerable.** A user banned from the community, a
user banned site-wide, or a user on a defederated instance can have content
persisted through this path. Reported, not fixed; deferred for evaluation after the testing work (see Status).

### F2 — `resolve_remote_post_from_search` creates posts and replies with no permission check

`app/activitypub/util.py:4035` (`resolve_remote_post_from_search`). Calls `create_post_reply` at `:4109` and
`create_post` at `:4112`, with no permission function. Two entry points:

- web `app/search/routes.py:228` `retrieve_remote_post` — `@login_required`,
  plus `current_user.banned` → `show_ban_message()`. So the *requester* is
  gated; the *author* of the fetched content is not.
- AP inbox `Move`, `app/activitypub/routes.py:1575` — remote-triggerable, no
  requester at all.

The function does apply the `uri_domain == actor_domain` impersonation check.
It does not apply `can_create_post`.

**Verdict: unguarded.** Same missing checks as F1. The web entry point requires
a logged-in, unbanned local user, which caps the abuse; the `Move` entry point
does not. Reported, not fixed; deferred for evaluation after the testing work
(see Status).

### F3 — `GET /api/alpha/resolve_object` creates posts and replies, and does not require authentication

`app/api/alpha/routes.py:145-150` → `app/api/alpha/utils/misc.py:47`
`get_resolve_object` → `create_resolved_object` at `:417` and `:422`.

    def get_resolve_object(auth, data, user_id=None, recursive=False):
        ...
        if auth:
            user_id = authorise_api_user(auth)

`auth` is read from the `Authorization` header at the route and is optional: if
absent, `authorise_api_user` is never called and the function proceeds with
`user_id=None`. The only gate is `enable_api()`
(`app/api/alpha/routes.py:47`), which is a site-wide on/off switch, and there is
no `before_request` auth hook on the API blueprint.

Because `create_resolved_object` carries no permission check (F1), an
**unauthenticated** caller can make this instance fetch an arbitrary remote
object and persist it as a `Post` or `PostReply` in a local community. The same
call also recurses into `get_resolve_object` for the audience/`inReplyTo`
chain, and via `search_for_community` can cause a whole remote community to be
created and backfilled (F4).

**Verdict: unguarded, and unauthenticated.** The most reachable of the F1/F2/F3
group. Reported, not fixed; deferred for evaluation after the testing work (see Status).

### F4 — Celery `retrieve_mods_and_backfill` creates posts and replies with no permission check

`app/community/util.py:93`. `create_post` at `:198`, `PostReply.new` at `:272`.
No `can_create_post` / `can_create_post_reply`.

**What upstream gates it:** `search_for_community`
(`app/community/util.py:38-41`) refuses a server that fails `instance_allowed`
or passes `instance_banned`, so the *community's* instance is checked.

**What is not gated:** the *author* of each backfilled post. The backfill takes
`activity['attributedTo']` and calls `find_actor_or_create` on it
(`app/community/util.py:184`) with **no check that the author's domain matches
the community's server** — unlike `create_resolved_object`, which does apply
that check. So a remote community's outbox can attribute posts to accounts on
any instance, including a defederated one, and those posts are created. Author
site-bans, community bans and `restricted_to_mods` are likewise not consulted.

Triggerable by any user who causes a new remote community to be fetched
(`search_for_community` from the search UI, feed subscription at
`app/feed/util.py:115`, profile import at `app/user/routes.py:1314`) — and,
because of F3, by an unauthenticated API caller.

**Verdict: unguarded.** Reported, not fixed; deferred for evaluation after the testing work (see Status).

### F5 — AP `Update` of an existing post is not re-checked; `Update` of an existing reply is

`app/activitypub/routes.py:2298-2299`:

    if user.id == post.user_id or post.community.is_moderator(user) or post.community.is_instance_admin(user):
        update_post_from_activity(post, activity_json)

The reply branch twelve lines below (`:2341`) does the same authorship check and
then **additionally** requires `can_create_post_reply(user, community)`. The
post branch has no equivalent. The PeerTube `Video` update branch
(`app/activitypub/routes.py:1240-1241`) checks only `user.id == post.user_id`.

**Verdict: an asymmetry, most likely unintended.** A remote author who has since
been banned from the community, banned site-wide, or whose instance has since
been defederated can still rewrite the title, body and URL of their existing
post; the same actor cannot rewrite their existing reply. Whether editing should
be gated by a *creation* permission is a policy call, but the two branches
should not disagree. Reported, not fixed; recorded with a verdict, NOT part of the deferred 14 (see Status).

### F6 — Editing your own post/reply on the web is gated by ownership, not by `can_create_post`

`app/post/routes.py:1055-1074` (`post_edit`) guards with `@login_required`,
`post.user_id == current_user.id`, `communities_banned_from(current_user.id)`
and `user_ip_banned()`. `app/post/routes.py:1897` (`post_reply_edit`) guards
with `@login_required` and `post_reply.user_id == current_user.id` only.
`edit_post` / `edit_reply` call no permission function on the `SRC_WEB` path.

**Verdict: upstream-gated, and defensible as policy.** `post_edit` does check
the community ban, which is the check that matters most; `post_reply_edit` does
not even do that. Recorded so the difference from F5 is on the record: the AP
reply path re-checks on edit, the web reply path does not.

### F7 — A repeating scheduled post is re-published without re-checking `can_create_post`

`app/cli.py:1031` `publish_scheduled_posts`. For a post with
`repeat != 'none'`, a fresh `Post` row is created at `app/cli.py:1066-1082` and
published, with an author self-upvote at `:1096`. `can_create_post` was checked
once, when the post was first scheduled through `make_post`; it is never checked
again.

**Verdict: gated at schedule time only.** A user banned from the community — or
banned site-wide, or whose community was banned — between scheduling and
publication continues to publish on the schedule, indefinitely for a repeating
post. Reported, not fixed; recorded with a verdict, NOT part of the deferred 14 (see Status).

### F8 — CLI `lemmy-import` writes `Post` and `PostReply` rows directly

`app/cli.py:597` and `app/cli.py:705`, inside `@app.cli.command("lemmy-import")`.

**Verdict: upstream-gated by the trust boundary.** A Flask CLI command runs as
the operator on the server, with no HTTP request and no user session. There is
no permission function to apply. Recorded for completeness, not a defect.

#### Note — the same command also bypasses password-rotation revocation, and it is a different concern from F8

Found during this branch's final review, after commit `27403474` ("security: a
password reset now revokes API tokens; expiry stops being an error") — not one
of the fourteen deferred rows and not part of the F1-F14 set or its row
counts, since it is not a permission-guard gap. Recorded here because it lives
in the same `lemmy-import` command F8 already covers.

`authorise_api_user` (`app/utils.py`) refuses a bearer token whose `iat` predates
`password_updated_at` — the only mechanism by which changing a password
revokes that user's existing API tokens. Every genuine password change is
supposed to stamp it, via `User.set_password()`:

    def set_password(self, password, revoke_sessions=True):
        ...
        self.password_hash = generate_password_hash(password)
        if revoke_sessions:
            self.password_updated_at = utcnow()

(`app/models.py:1095, 1112-1114`.) There are nine `set_password()` call sites
in `app/`: `app/cli.py:252`, `app/cli.py:271`, `app/cli.py:1206`,
`app/admin/routes.py:1898`, `app/auth/util.py:271`, `app/auth/util.py:297`,
`app/auth/routes.py:160`, `app/user/routes.py:240`, and `app/models.py:1149`.
The last is `check_password`'s legacy-bcrypt rehash, the one caller that
deliberately opts out with `revoke_sessions=False` — it re-saves the hash of a
password the user just proved they know, not a password change, so it should
not sign that user's API clients out. The other eight take the default and
stamp. Commit `27403474` summarised this as "all EIGHT password-changing sites
now stamp."

`app/cli.py:360`, inside `lemmy-import`, changes an **existing** user's
credential without going through any of the nine:

    existing_user.password_hash = row.password_encrypted

This is a direct assignment, not a `set_password()` call, so it was never one
of the nine to begin with — it was correctly excluded from the "eight sites
now stamp" count, not overlooked by it. The exclusion is right: this line
copies an already-hashed credential (`row.password_encrypted`, from the Lemmy
export being imported) during an operator-run bulk migration, not a
user-initiated password change, and `lemmy-import` runs offline with no HTTP
request or session — the same trust boundary F8 already applies to this
command's other two direct-write bypasses. **That exclusion stands.**

It is nonetheless still a path where an existing user's stored credential
changes and `password_updated_at` is not stamped — so any API tokens that user
already holds survive the import, unlike every other credential change in the
app. A reader who sees only the exclusion (line above) would conclude there is
nothing here; a reader who sees only the non-stamping (this line) would
conclude it is the same kind of gap `27403474` just closed elsewhere. Both are
true at once, which is why it belongs on a list rather than nowhere. Not fixed
here — worth evaluating alongside the rest of `lemmy-import`'s trust-boundary
exceptions (F8), whenever that command's design is next revisited.

`app/cli.py:377`, the sibling assignment inside the same command's **new-user**
constructor branch (`password_hash=row.password_encrypted or ""`, a few lines
into the `User(...)` call), does **not** share this problem. `password_updated_at`
is declared
`db.Column(db.DateTime, default=utcnow)` (`app/models.py:1042`), the
constructor at `app/cli.py:372-390` never passes that column explicitly, and
SQLAlchemy applies the column default on insert — so the new row is stamped
automatically. A brand-new user also has no prior credential and no API tokens
issued yet, so there is nothing for a missing stamp to fail to revoke even in
principle. Checked because the same re-reviewer that found `:360` flagged
`:377` as a second bypass in the same path; it turns out to be a different
shape, not a second instance of the same defect.

### F9 — Author self-upvotes bypass `can_upvote`

Four sites: `app/shared/post.py:226` (`make_post`), `app/models.py:2207`
(`Post.new`), `app/models.py:2944` (`PostReply.new`), `app/cli.py:1096`
(scheduled republish).

**Verdict: deliberate policy, consistently applied.** The initial score-1 vote is
part of creating the content; the creation itself was gated (or, on the AP
ingest path, was not — F1, which is where the real gap is). All four sites agree
with each other. Not a defect.

### F10 — `post_emoji_set` / `comment_emoji_set` skip `@validation_required` and `@approval_required`

`app/post/routes.py:607` and `app/post/routes.py:621` carry no decorators at all;
each checks `current_user.is_authenticated` in the body and `abort(403)`s
otherwise. Their four sibling vote routes (`:539-543`, `:552-556`, `:565-569`,
`:637-640`) all carry `@login_required @validation_required @approval_required`.

The vote itself still reaches `can_upvote`, so the *permission function* is
consulted. But `can_upvote` does not check `user.verified` or
`user.private_key is None` — those are precisely what the two missing decorators
supply (`app/utils.py`, `validation_required` / `approval_required`).

**Verdict: partially gated.** An unverified account, or one awaiting approval on
a `RequireApplication` / `Closed` instance, can cast an emoji upvote through
these two routes while being blocked from the four ordinary vote routes.
Reported, not fixed; recorded with a verdict, NOT part of the deferred 14 (see Status).

### F11 — Poll voting consults no permission function on any of its three entry points

- web `app/post/routes.py:639` `poll_vote` → `vote_for_poll`
- API `app/api/alpha/utils/post.py:1790` `post_poll_vote` → `vote_for_poll`
- AP inbox `app/activitypub/routes.py:2434` `process_poll_vote`

`vote_for_poll` (`app/shared/post.py:1148`) checks `user.banned` and
`user_ip_banned()` and nothing else. `process_poll_vote` checks
`instance_banned(user.instance.domain)` and nothing else.

None of the three consults `can_upvote`, `can_downvote`, or
`communities_banned_from`. A user banned from a community can still vote in that
community's polls from the web and the API; `community.local_only` is not
consulted on the AP path either. There is no `VOTE_QUOTA` check on any of the
three, unlike post and reply voting.

**Verdict: unguarded.** Poll voting has no permission function of its own, so
this is a design gap rather than a skipped call, but it is the same shape: an
action that records a vote without asking whether the actor may vote here.
Reported, not fixed; deferred for evaluation after the testing work (see Status).

### F12 — `make_post` and `edit_post` call `can_upload_video()` without a user

`app/shared/post.py:200` and `app/shared/post.py:464`:

    if type == POST_TYPE_VIDEO and can_upload_video():

`can_upload_video` (`app/utils.py:2409`) falls back to `current_user` when no
`user` is passed:

    upload_user = user or current_user
    ...
    elif upload_access == 'users' and not current_user.is_authenticated and user is None:
        return False

Both call sites are reached on the `SRC_API` path, where the caller is
authenticated by token and `current_user` is anonymous. The `user` object is in
scope at both sites and is not passed. Consequences on the API path:

- setting `users` → returns `False`; an authenticated API user cannot attach a
  video even though the setting permits every user to.
- setting `user 1` → compares `current_user.get_id()` (None) against 1 →
  `False`, even when the API caller *is* user 1.
- setting `admins` → evaluates `current_user.is_admin_or_staff()` on the
  anonymous user rather than on the API caller.

`process_upload` (`app/shared/upload.py:22`) does it correctly:
`can_upload_video(user)`.

**Verdict: the guard is called, with the wrong subject.** It fails closed on the
API path, so this is a correctness/consistency defect rather than a bypass —
but it means the API path's video permission is decided by whoever
`current_user` happens to be rather than by the token's owner. Reported, not
fixed; not part of the deferred 14, since the guard is present.

**This is one half of a defect Task 7 found from the other side.** See
"F12 and Task 7 are the same defect from two directions" above: Task 7 found the
`'users'` branch inert whenever a user *is* passed; this found the two callers
that pass none. Do not act on either half without reading the other.

### F13 — `/admin/` has no `@permission_required`

`app/admin/routes.py:55` `admin_home`, methods `GET, POST`, decorated only with
`@login_required`. It is the one route of 53 in that file without a permission
check.

It renders host load average (`os.getloadavg()`), configured/detected CPU count,
disk-usage percentage of the server's working directory, the list of loaded
plugins and every hook they register, the LibreTranslate language list, and the
names/frequencies/last-run times of overdue cron jobs.

**Verdict: unguarded read.** No state is mutated (the `POST` method is declared
but the body handles no form), so this is information disclosure to any
authenticated user, not a privilege escalation. Reported, not fixed; recorded with a verdict, NOT part of the deferred 14 (see Status).

### F14 — `@permission_required` is applied outside `@login_required` on 50 admin routes

E.g. `app/admin/routes.py:109-111`:

    @bp.route('/site', methods=['GET', 'POST'])
    @permission_required('change instance settings')
    @login_required
    def admin_site():

Decorators apply bottom-up, so `permission_required` runs **first**, before
`login_required` has had a chance to redirect an anonymous visitor.

Traced: for an anonymous user `current_user.get_id()` returns `None`;
`user_access(permission, None)` (`app/utils.py:1510`) fails `user_id == 0`,
fails `user_id == 1`, and runs the `role_permission` join with `ur.user_id =
NULL`, which matches nothing in SQL, so it returns `False`.

**Verdict: safe, but wrong.** Access is correctly denied. The consequences are
that an anonymous visitor is sent to `auth.permission_denied` rather than to the
login page, and that every anonymous hit on an admin URL costs a database query.
Not a security finding. Recorded because the audit had to establish it rather
than assume it.

### Secondary observation (outside the eight functions)

`app/post/routes.py:2363` `post_check_ai` and `app/post/routes.py:2414`
`post_reply_check_ai` are `POST` routes with no decorators and no
`is_authenticated` check. Each issues an outbound HTTP request to
`DETECT_AI_ENDPOINT` per call, with no rate limit. Unauthenticated
request-amplification. Not a permission-function gap; noted in passing.

---

## Summary

Counted from the tables above, not from memory. The command that regenerates
these numbers is at the end of this section; if you change a table, re-run it
rather than editing the counts by hand.

| | count |
|---|---|
| rows in the four content tables (post, reply, vote, upload) | 62 |
| of those, entry points that actually write | 61 |
| of those, not a write (`app/post/util.py:55`, transient) | 1 |
| rows in the two permission-sweep tables (`user_access`, `authorise_api_user`) | 6 |
| **rows in all six tables** | **68** |
| rows whose guard is a literal **NONE** | 23 |
| **deferred for evaluation** (F1, F2, F3, F4, F11) | **14 rows / 10 entry points / 5 sinks** |
| — of those, literal **NONE** | 12 |
| — of those, partial guard that is not a voting permission check | 2 |
| gated upstream or by design (F6, F8, F9, F14) | 8 rows |
| asymmetry / wrong subject / unguarded read (F5, F7, F10, F12, F13) | 8 rows |

The `user_access` and `authorise_api_user` tables hold 6 rows because those two
sweeps are summarised by category, not one row per call site: 53 admin routes
collapse to 3 rows, and the `authorise_api_user` scan lists only its 3
exceptions out of the whole `app/api/alpha/utils/` mutating surface.

**A correction, made while writing the deferred list.** Earlier drafts of this
summary said "63 entry points, 24 NONE, 13 genuinely unguarded". All three were
estimates written before the tables were finished, and all three were wrong.
The figures above are extracted from the tables. The deferred set is **14 rows**,
not 13: the miscount came from counting only rows with a literal **NONE** and so
dropping two of poll voting's three entry points, whose guard cells name a
partial check (`authorise_api_user` only, `instance_banned` only) rather than
NONE. Recorded rather than silently corrected, because a count that changed
without explanation is exactly the kind of thing that stops a reader trusting
the rest.

To regenerate:

    python3 - <<'PY'
    import re
    p='docs/superpowers/specs/2026-08-25-permission-callsite-audit.md'
    sec=None; counts={}
    for l in open(p).read().splitlines():
        if l.startswith('## ') or l.startswith('### '):
            sec=l.strip('# ').strip()
        if l.startswith('|') and not re.match(r'^\|[\s:|-]+\|$', l):
            c=[x.strip() for x in l.strip('|').split('|')]
            if c[0] in ('action','entry point','#','','what'): continue
            counts[sec]=counts.get(sec,0)+1
    for k,v in counts.items():
        if k.startswith('Table:'): print(f'{v:3}  {k}')
    PY

The concentration is exactly where the brief predicted: the paths that ingest
remote content **without** going through `process_new_content` — the
Announce-of-URI resolver, the search resolver, the API `resolve_object`
endpoint, and the community backfill task — are the four sinks that never
consult `can_create_post` or `can_create_post_reply`, and three of the four are
remote- or unauthenticated-triggerable.

The functions themselves were never the bug. `process_new_content`
(`app/activitypub/routes.py:2308`, `:2351`) is the one AP path that asks, and it
is the one that behaves correctly.

---

## The coverage floor

Measured on this branch, after the run recorded in
`.superpowers/sdd/2026-08-25-coverage-utils-permissions/task-9-report.md`:

| module | measured `percent_covered` | floor |
|---|---|---|
| `app/utils.py` | 60.2356 | 60 (unchanged) |
| `app/models.py` | 42.6908 | none — see the report |

`percent_covered` is the blended statement-and-branch figure, which is why the
floor is the measured value rounded down rather than a target.

`app/utils.py` measured 60.2356; rounded down that is 60, which is what the floor
already is. Floors only ever rise, and rounding down does not raise this one, so
`coverage_floors.ini` is unchanged by this task. The floor was nonetheless
exercised: raised to 61 it fails, naming the module; restored to 60 it passes.
Both outputs are in the task report.
