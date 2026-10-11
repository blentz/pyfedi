# ActivityPub Interop C1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the ten small ActivityPub interop fixes of tier C1:

- array-valued `type`;
- content negotiation;
- inbound actor Update;
- string `endpoints`;
- Undo of EmojiReact, Flag, Add and Remove;
- Delete of a remote Group;
- Person `followers`/`following`;
- Person outbox;
- HTML discovery;
- the WebFinger reverse check and JSON host-meta.

**Architecture:**
- **Inbox fixes** go in `process_inbox_request` and `shared_inbox` (`app/activitypub/routes.py`), with their helpers in `app/activitypub/util.py`. The inbox normalises `type` once at entry, on a copy (Task 1). Actor Updates are applied inline from the pushed document (Task 3). The Undo arm gains EmojiReact, Flag and Add/Remove sub-arms (Tasks 5–7). A Group's self-Delete is verified at the origin and marks the community gone (Task 8).
- **Serving fixes:**
  - `ap_media_type()` (beside `ap_content_headers`) picks the AP content type from Accept (Task 2).
  - The Person document gains count-only `followers`/`following` collections and a real paged outbox (Tasks 9–10).
  - HTML pages advertise their AP id (Task 11).
  - `/.well-known/host-meta.json` is added (Task 12).
- **Migrations:** `Report.ap_id` (Task 6) and two partial `(user_id, posted_at DESC)` outbox indexes (Task 10).

**Tech Stack:** Flask, SQLAlchemy/Alembic, Celery (eager in tests), httpx through the capped fetch helpers, respx (`http_mock`), pytest in containers through `./run_tests.sh`.

**Spec:** `docs/superpowers/specs/2026-10-11-ap-interop-c1-design.md` (spec commit `0c861692e`). The roadmap is `docs/superpowers/specs/2026-10-10-ap-gaps-roadmap.md`, tier C.

## Global Constraints

- **One commit per task.** Item 3 has one commit per inner type (Tasks 5, 6, 7). TDD: write the failing test, watch it fail, then implement.
- **Coverage:**
  - 100% line and branch coverage of new and changed lines.
  - Each task ends with a gate:
    1. `./run_tests.sh <touched test modules> --cov=app --cov-branch --cov-report=json -q`
    2. `python3 tests/check_changed_line_coverage.py coverage.json HEAD~1 --branches app/`
    
    `HEAD~1` is the task BASE, because each task is exactly one commit. If the gate fails, fix the gap and amend the task's commit, so the base stays `HEAD~1`.
  - The final gate (Task 13) runs the full suite with coverage, then `python3 tests/check_changed_line_coverage.py coverage.json 0c861692e --branches app/ fastapi_server.py` and `python3 tests/check_coverage_floors.py coverage.json coverage_floors.ini`.
  - Floors only rise. The ones these tasks touch: `app/activitypub/routes.py = 99`, `app/activitypub/util.py = 99`, `app/activitypub/signature.py = 100`, `app/activitypub/actor.py = 98`, `app/utils.py = 99`, `app/models.py = 98`, `app/main/routes.py = 99`, `app/community/routes.py = 98`, `app/user/routes.py = 98`, `app/post/routes.py = 98`, `app/feed/routes.py = 99`, `app/api/alpha/routes.py = 99`.
- **Running tests:** Python tests run only in containers, via `./run_tests.sh <paths> -q`. Gate scripts run on the host with `python3`. Read the passed-test count, not just the exit code: a run that hits the session budget still exits 0 (`pytest.ini`, `session_timeout = 2700`).
- **Imports:** no function-level imports in `app/` (`tests/test_no_inline_imports.py`).
- **Remote fetches:** every remote fetch goes through a capped helper: `get_request`, `get_request_capped`, `signed_get_request`, `signed_get_capped` or `fetch_object_capped`. Never call `httpx_client` directly. New fetches in this plan use `fetch_object_capped` or `get_request_capped`, never `get_request`, because `get_request` sleeps and retries (Spec conflict 1).
- **No sleep or retry on a request or inbox path** (D738/D775).
- **The signed actor is the only identity.** Inbound handling must not trust the body over the verified signer.
- **AP cache policy (D160):** actor 300 s, content 120 s, collections 60 s (`AP_CACHE_COLLECTION`), misses no-store. In secure mode, `secure_mode_cache_headers` still applies.
- **Hosts are compared with `host_of`** (`app/activitypub/util.py`). An empty host never equals anything.
- **Test honesty:**
  - Never patch `HttpSignature.verify_request` or `LDSignature.verify_signature`. Make real signatures with `tests/factories.py:signed_inbox_post` / `signed_get_headers`, or with `tests/conftest.py:ld_signed_body`.
  - Outbound HTTP goes through `http_mock` (respx, `assert_all_called=True`).
  - Inbox dispatch tests call `tests/test_inbox_dispatch_preamble.py:dispatch`.
- **Commits:** Conventional Commits (`fix: ...` / `feat: ...`), a present-tense sentence about behaviour, ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Migrations:**
  - Task 6 creates revision `e2a7c4f9d1b3` with `down_revision = 'd9f3b7a15c2e'`, the current single head.
  - Task 10 creates revision `f4b8d2a6c1e7` with `down_revision = 'e2a7c4f9d1b3'`.
  - Both downgrades must be reversible, and each migration gets a test module in the style of `tests/test_migration_instance_signature_format.py`.
- **Quoted code is at HEAD `0c861692e`.** Each task's "change X to Y" snippets quote the source as it stands before this plan. Where an earlier task already changed a quoted line, apply the change to the line as it now reads. The table "Overlapping edits" below lists every such place. Search by content; line numbers are hints.

## Clarifications

Decisions on details the spec leaves open. Each is binding on its task.

1. **Task 1: `normalise_activity_types` lives in `app/activitypub/util.py`, directly after `shared_inbox_of`.**
   - It returns the same dict when no covered `type` is an array, a deep copy with each array reduced otherwise, and `None` when an array holds no string.
   - A "known type" is one in `DISPATCHED_AP_TYPES`: the type strings the dispatcher compares against, plus Person, Service, Application, Podcast and Tombstone for Task 3.
   - It covers the activity, its `object`, and an Announce's `object.object`.
2. **Task 1: normalisation happens in two places, both on copies.**
   - `shared_inbox` normalises for its own early checks (Announce inner-id dedupe, relay gate, account deletion). It refuses a no-string array before the dedupe key and the signature checks. It passes the original to `LDSignature.verify_signature` and to `process_inbox_request`.
   - `process_inbox_request` normalises its own copy first, before the relay filter and `verify_ld_accepted_activity`. Otherwise an LD-signed `type: ['Create']` would skip the R-LDDOWNGRADE origin refetch.
3. **Task 1: what "the stored or logged original" means.**
   - Top-level log rows (`saved_json`, the relay and LD refusals) store the raw document.
   - An LD-accepted activity keeps logging the verified document, as today.
   - Sub-handlers that log `request_json` themselves log the normalised copy. The original object is never mutated.
   - `replay_inbox_request` uses `normalise_activity_types(...) or request_json` for its own checks, and `process_inbox_request` refuses the `None` case.
4. **Task 1: unknown array types.** A top-level unknown type (`['Foo']` becomes `'Foo'`) matches no arm and is silently ignored, as an unknown string type is today. "Falls through and is logged" is tested on the object-type path, which logs `Unacceptable type (create): Foo`. An inner object refetched by `verify_announced_inner` / `verify_object_from_source` is not re-normalised. It can no longer crash, because the log line is now an f-string.
5. **Task 2: `ap_media_type()` lives in `app/activitypub/routes.py`, directly above `ap_content_headers`.**
   - `app/main/routes.py` already imports from that module, so there is no cycle.
   - The Accept parser `accepted_media_ranges(accept_header) -> list[tuple[str, dict, float]]` lives in `app/utils.py`, beside `is_activitypub_request`, which shares it.
6. **Task 2: the ld+json answer is always the profile form.**
   - `ap_media_type()` returns `application/ld+json; profile="https://www.w3.org/ns/activitystreams"` whether the request listed plain `application/ld+json` or the profile form.
   - An ld+json range with a different profile does not count, and neither does a wildcard.
   - On equal q-values the answer is `application/activity+json`.
   - `is_activitypub_request` still counts an ld+json range whatever its profile, as today. Only q=0 is newly excluded.
7. **Task 2: the duplicate `is_activitypub_request` in `app/activitypub/util.py` is deleted.** It is byte-identical to the one in `app/utils.py`, and util re-exports that one through its existing `from app.utils import`, so callers that import it from `app.activitypub.util` are unchanged (Spec conflict 3).
8. **Task 2: `Vary: Accept` is added centrally.**
   - `app/request_hooks.py`'s `after_request` adds it to every response whose mimetype is an AP type (Spec conflict 4).
   - The same hook's cookie suppression changes from `content_type == 'application/activity+json'` to `mimetype in AP_MEDIA_TYPES`.
   - `activitypub_application`'s `@cache.cached(timeout=6)` gets `key_prefix=ap_application_cache_key`, so each media type is cached separately.
9. **Task 2: the media-type constants and `AP_FETCH_ACCEPT` live in `app/constants.py`.**
   - That module can be imported from `signature.py`, `utils.py`, `util.py` and the feature modules without a cycle.
   - `app/discovery/credits.py:_ACTOR_ACCEPT` becomes an alias of the constant.
   - Nodeinfo fetches and WebFinger keep their own Accept. When a WebFinger `self` link advertises its own `type`, that type is still what is asked for; only the default changes.
   - Accept is cavage-signed: `HttpSignature.signed_request` signs every header it sets, so the new value is what is signed.
10. **Task 3: "inline" means calling `refresh_user_profile_task` directly.**
    - `refresh_user_profile` runs inline only under `current_app.debug`; otherwise it uses `apply_async` with a countdown.
    - The dispatcher already runs in a worker, and the pushed document needs no fetch.
    - `ACTOR_UPDATE_TYPES = ('Person', 'Service', 'Application', 'Podcast')`. `Podcast` is Castopod's actor type, which `actor_json_to_model` already treats as a user.
11. **Task 3: the id comparison is `names_actor(candidate, actor) -> bool` in util.py.**
    - It strips and lowercases `candidate` and compares it with `actor.ap_profile_id.lower()`. This is how `actor_json_to_model` stores ids and how `find_actor_by_url` looks them up.
    - A `None` actor, a non-string candidate, or an actor without an `ap_profile_id` gives False.
    - An announced actor Update is refused (`'Announced actor Update refused: only the actor itself may send it'`), because the announcing Group, not the described actor, signed it.
12. **Task 3: "the shared inbox" is the actor's `ap_inbox_url`.**
    - Actors have no separate shared-inbox column.
    - The new `delivery_inbox_of(actor_json)` returns the shared inbox, else `inbox`. Each must be a non-empty string of at most `INBOX_URL_MAX = 255` characters (the column width).
    - When neither is usable, the stored value is kept.
13. **Task 4: a string `endpoints` is fetched with `fetch_object_capped`.**
    - It is capped at `OBJECT_MAX_BYTES` with a 15 s deadline, tries unsigned first and signs only on a 401, and makes no retry at `retry_delay=0`.
    - An inline dict `endpoints` keeps today's behaviour, with no host check on its `sharedInbox`: the spec's same-host rule is stated for the fetched document.
    - This adds at most one capped GET to actor creation, and only for same-host string `endpoints`.
14. **Task 5: an Undo of an EmojiReact undoes the user's vote only while the vote still carries that emoji.**
    - `undo_vote` gains `emoji=None`.
    - The reaction cache is rebuilt only when the deleted vote carried an emoji, mirroring `Post.vote`'s own `if emoji` gate.
    - `undo_vote`'s Post and PostReply arms are merged.
    - Four test doubles of `undo_vote` gain `emoji=None`.
15. **Task 6: the report withdrawal helpers live in `app/activitypub/util.py`, beside `process_report`.** They are `withdraw_report(report, session)` and `withdraw_flag(user, flag, session) -> (aplog_result, message)`.
    - **Notifications:** "notifications derived from the report" are the unread `NOTIF_REPORT` rows that `process_report` wrote for it. Each recipient's `unread_notifications` is recounted.
    - **Counter:** the suspect's `reports` counter comes down by one, never below 0. A value of -1 (exempt) is left alone.
    - **Long ids:** a Flag id longer than 255 characters is stored as NULL, not truncated.
    - **Fallback lookup:** it resolves the target with `find_reported_object` only when the Flag's `object` is a string. It matches `Report.type` plus that type's suspect column (`REPORT_SUSPECT`), newest by `created_at desc, id desc`.
    - **No relay:** an Undo of a Flag is not relayed to the community's followers.
16. **Task 7: the Add and Remove arms move into `process_add` / `process_remove` in routes.py, with an `undo=False` keyword.**
    - The authority check is one predicate, `may_moderate(community, actor)`. The feed branch had no authority check, because the feed's signed Announce is the authority, and it still has none.
    - The "target state already holds" no-op applies only when `undo=True`. Plain Add and Remove behave exactly as before.
    - An Undo of an Add logs a `Remove` row, and an Undo of a Remove logs an `Add` row.
    - The D115 gate now lets a feed's Announce of an Undo of its own Add or Remove through. Without that, a feed could never undo.
    - An Undo whose object is a bare URI stays refused by the existing D112 check. Nothing is fetched.
17. **Task 8: a Group's self-Delete reaches the dispatcher.**
    - `shared_inbox`'s account-deletion block looks up a `Community` when no `User` matches. If it finds one, it sends the activity to `process_inbox_request`, not `process_delete_request`.
    - Origin verification reuses `_origin_refusal_of_delete` with a new `tombstone_required=True` flag, so only a 410 or a Tombstone counts.
    - `replay_inbox_request`'s twin block is unchanged.
    - **Who may delete (Amendment A):** the Group itself (Community-actor branch), or, in the Delete arm, an actor `may_delete_group(actor_id, community)` accepts: a stored moderator or owner, or an admin of the Group's host. `may_delete_group` delegates to the security track's `_may_remove_from`, so the Group-host admin rule is the same one the S4 moderator-Delete deferral uses. Anyone else is refused and logged, without a fetch.
    - **The origin refetch is always required:** authority only gets a Delete to `delete_remote_group`, which still asks the origin for a 410 or a Tombstone.
    - **A cross-origin Announced moderator Delete of a Group is not deferred.** `_verify_announced_delete` would hand it to `apply_delayed_delete`, which knows only posts and comments and would drop it as "superseded". A Delete naming a stored Group passes straight through to the dispatcher instead (see Spec conflict 13).
18. **Task 8: `Community.is_gone()` is enforced inside four shared predicates in `app/utils.py`.**
    - The predicates are `can_create_post`, `can_create_post_reply`, `can_upvote` and `can_downvote`. Every web, API and inbox write path already calls them.
    - Joins are refused in `do_subscribe` (web) and `post_community_follow` (API).
    - Delivery is skipped in `send_post`, `send_reply` and `delete_object`.
    - Moderator-action sends (locks, adds, removes, blocks, group updates, move) are not gated. A local moderator of a deleted remote Group is rare.
19. **Task 8: unsubscribing touches local members only (`User.ap_id IS NULL`).**
    - Their `CommunityMember` and `CommunityJoinRequest` rows are deleted, and `subscriptions_count` is lowered by that number.
    - The memoized `community_membership`, `joined_communities` and `moderating_communities` lookups are cleared for each member.
    - The notice is a flash in `show_community`, beside the dead-instance warning.
    - A gone Group is ignored, with `GROUP_DELETED_MESSAGE`, in the Announce/Accept/Reject arm and as the first statement of the Community-actor branch.
20. **Task 9: which follow rows count.**
    - **Shapes:** `UserFollower` stores both directions under one pair. A remote follower of local U is `local_user_id=U, is_inward=True`. A local follower A of U is A's outward row `local_user_id=A, remote_user_id=U, is_inward=False`.
    - **Followers:** accepted rows of either shape, excluding banned or deleted accounts and accounts U blocked.
    - **Following:** U's accepted outward rows plus U's unbanned `CommunityMember` rows.
    - **Where:** the counters are raw SQL in util.py, beside `community_members`.
21. **Task 9: one gate helper, `local_actor_user(actor)`, in routes.py.**
    - It matches the name case-insensitively and requires `ap_id IS NULL`.
    - A deleted user gets `abort(410)` and a banned, unknown or remote one gets `abort(404)`, as `user_profile`'s AP answers do.
    - Collection ids are canonical (`user.followers_url()`, `user.public_url() + '/following'` / `'/outbox'`) whatever case the request used.
22. **Task 10: the outbox query shape is one UNION ALL subquery of `(kind, id, posted_at)`.**
    - The page query orders by `posted_at DESC NULLS LAST, id DESC, kind` and applies OFFSET/LIMIT in SQL. Then one `IN (...)` load per kind.
    - `totalItems` is `COUNT(*)` of the same subquery.
    - The indexes are added unconditionally. Only single-column indexes exist today, with no `(user_id, posted_at)` composite.
    - Filters use `deleted == False`, so the predicate matches the partial index.
23. **Task 10: unlisted content is excluded.**
    - `visible_to_clause(model, None)` admits unlisted content. `post_to_page` and `comment_model_to_json` always address Public, so they would republish it as public.
    - The plan therefore also applies `listable_clause`, as `community_outbox` does.
    - Also: `Post.status > POST_STATUS_REVIEWING`, and a comment whose post is deleted is excluded.
    - The Create envelope is factored out of `post_to_activity` as `create_activity_of(obj)`. The output for the community outbox is unchanged.
24. **Task 11: `ap_alternate_url(actor)` lives in `app/utils.py`.**
    - It returns `public_url()` for a local actor and `ap_profile_id` for a remote one. A remote `ap_public_url` can be an HTML page (Mastodon's `/@name`).
    - The post page passes `post.profile_id()`.
    - Every post type by a local author gets `fediverse:creator` (Amendment A). In practice that adds image and video, the only types not already tagged. The condition gains `or post.author.is_local()`.
    - The feed page gets the HTML alternate only, with no `Link` header.
    - The community page already varies on Accept, so only `Link` is added there.
25. **Task 12: one shared helper, `canonical_handle(actor_json, address, server, prefix='')`, in `app/activitypub/util.py`, asked by every WebFinger resolver (Amendment A).**
    - Callers: the webfinger arm of `create_actor_from_remote` (`fetch_actor_from_webfinger` itself is unchanged), `search_for_user`, `search_for_community` and `search_for_feed`.
    - It lives in util.py, not actor.py, because all four caller modules already import from util.py. `app/user/utils.py`, `app/community/util.py` and `app/feed/util.py` importing `app.activitypub.actor` would add a new import edge into a module that util.py itself imports.
    - `prefix='~'` for feeds: their WebFinger resource is `acct:~name@host`.
    - The reverse lookup `webfinger_self_href(address, server)` makes one `get_request_capped` GET. The three search resolvers keep their own existing retries on the forward lookup; removing those is outside C1.
    - A document with no string `preferredUsername` or `id` passes through, and `actor_json_to_model` refuses it as today.
    - The test suite runs `NullCache`, so the host-meta.json test checks the route's `cache_timeout` attribute, which must be 600.

## Spec conflicts

Spec statements that are wrong or impossible against the real code. Each one records the evidence and the resolution the plan implements. Each is marked either **Resolved by Amendment A** (the owner's ruling, now in the spec) or **Controller-accepted resolution**.

1. **Global constraints: `get_request` is not retry-free.** Controller-accepted resolution.
   - Evidence: `app/utils.py:366-388` sleeps `random.randint(3, 10)` and retries once on `httpx.ReadError` / `httpx.HTTPError`. That contradicts "No sleep or retry on a request or inbox path".
   - Resolution: new fetches use `fetch_object_capped` (Task 4) or `get_request_capped` (Task 12). Existing `get_request` callers change only their Accept (Task 2).
2. **Item 2: "An Update for an unknown actor ... does not create the actor."** Resolved by Amendment A.
   - Evidence: `shared_inbox` creates every unknown signer before it verifies the signature, because it needs the key (`actor = find_actor_or_create_cached(request_json['actor'])`, R:~790).
   - Resolution: for actor Updates, dispatch looks the signer up with `create_if_not_found=False`, and logs and drops a miss (Task 3). The HTTP gate's creation is unchanged, because signature verification needs it.
3. **Item 2: the Group Update "same `object['id'] == signer` check".** Resolved by Amendment A.
   - Evidence: for a local community, the signer is the moderator, not the Group (R:1550-1556; `tests/test_inbox_dispatch_create_update.py::test_a_group_update_from_a_LOCAL_communitys_moderator_refreshes_and_announces`). The literal rule would refuse every local moderator edit.
   - Resolution (Task 3):
     - the object id must name the community the activity is processed for;
     - for a remote Group, the Group must also be the signer, either sending the Update itself or announcing it;
     - a user-signed Update of a remote Group is refused, and the test pinning the old behaviour changes.
4. **Item 5: `is_activitypub_request()` has two copies.** Controller-accepted resolution.
   - Evidence: the copy that `routes.py` and `app/main/routes.py` import is `app/activitypub/util.py:2671`, not `app/utils.py:2338`.
   - Resolution: Clarification 7.
5. **Item 5: "`Vary: Accept` is already sent" is not true of every AP route.** Controller-accepted resolution.
   - Evidence: `tombstone_response` (R:2617-2627), `instance_actor` (`app/main/routes.py:1311`), `activities_json` and `quote_boost_auth` set no Vary.
   - Resolution: Clarification 8.
6. **Item 5: the test the spec names as pinning the old Accept is the wrong one.** Controller-accepted resolution.
   - Evidence: `tests/test_activitypub_actor.py:1089` pins a type that the peer's WebFinger link advertises. The old default is pinned at `:1075`.
   - Resolution: update `:1075` and the other default pins (Task 2 lists them). Keep `:1089`, because an advertised link type is still honoured.
7. **Item 3 Flag: there is no reusable "moderator discards a report" code path, and reusing the inline one would be wrong.** Resolved by Amendment A.
   - Evidence: the discard is inline in `community_moderate_report_ignore` (`app/community/routes.py:2752-2800`). It keeps the report, sets `status = REPORT_STATE_DISCARDED`, and sets the suspect's `reports` counter to -1. That value exempts the suspect from all future reports. Notification cleanup there is a `# todo`.
   - Resolution: the new `withdraw_report` deletes the report, lowers the counter (floor 0, -1 untouched), deletes the unread report notifications it raised, and recounts unread counts (Task 6).
8. **Item 3 Flag: "discarded" is -1, not 4.** Controller-accepted resolution.
   - Evidence: `app/constants.py:44-48` defines NEW 0, ESCALATED 1, APPEALED 2, RESOLVED 3, DISCARDED -1. The "4 = discarded" in the models.py comment is stale.
   - Resolution: "kept" is implemented as `report.status != REPORT_STATE_NEW`.
9. **Item 6: "OrderedCollection" versus "the same shape as community followers (D175)".** Controller-accepted resolution.
   - Evidence: the community followers route serves `"type": "Collection"` with `"items": []` (R:2531-2537).
   - Resolution: follow the literal JSON in the spec (`OrderedCollection` / `orderedItems`), and read "same shape" as "count-only, empty list" (Task 9, `count_only_collection`).
10. **Item 1: `fediverse:creator` on image posts contradicts a pinned test.** Resolved by Amendment A.
    - Evidence: `tests/test_post_show.py:963-986` pins `(POST_TYPE_IMAGE, False)` for a local author ("An image post carries no text to attribute").
    - Resolution (Amendment A: every post type by a local author gets the tag, image posts included): Task 11 flips that row.
11. **Item 9 covers only one of four WebFinger resolvers.** Resolved by Amendment A.
    - Evidence: `search_for_user` (`app/user/utils.py:126`), `search_for_community` (`app/community/util.py:77`) and `search_for_feed` (`app/feed/util.py:66`) call `actor_json_to_model` with the looked-up handle directly.
    - Resolution (Amendment A: one shared helper for every resolver): Task 12 puts `canonical_handle` in `app/activitypub/util.py` and calls it from `create_actor_from_remote`, `search_for_user`, `search_for_community` and `search_for_feed`, with a test for each.
12. **Item 10: three statements do not hold against the code.** Resolved by Amendment A.
    - **"The existing 'community not available' message" does not exist.** `grep -rni "not available" app/` finds none for communities. Each path keeps its existing refusal through the shared predicates. The API join gets one new message: `'This community was deleted on its home server'`.
    - **"Inbox ... join" has no path to refuse.** A remote user's Follow of a remote Group never reaches this instance. Joins are refused on web and API.
    - **Lemmy deletes a community with a moderator as the actor.** Amendment A accepts a Delete from the Group, a known moderator, or an admin on the Group's host (the security track's `_may_remove_from`), always verified at the origin. Task 8 implements this with `may_delete_group`.
13. **Item 10 / Amendment A: a cross-origin moderator's Announced Delete of a Group would be deferred and then dropped.** Controller-accepted resolution (found while applying Amendment A).
    - Evidence: `verify_announced_inner` sends a cross-origin Announced Delete to `_verify_announced_delete` (`app/activitypub/util.py:~5672`). That function defers a moderator's Delete to `apply_delayed_delete` (`app/activitypub/routes.py:2298`). `apply_delayed_delete` looks the object up only as a `Post`/`PostReply`, finds no live row for a Group's id, and logs `"Delete superseded by the home server's"`. A Lemmy moderator on another instance than the Group would therefore never delete it.
    - Resolution (Task 8): `_verify_announced_delete` passes a Delete naming a stored Group straight to the dispatcher, with no deferral. The dispatcher applies `may_delete_group`, then the origin refetch, so nothing is accepted on the Announce alone. Test: `test_a_moderator_on_another_host_is_verified_not_deferred`.

## Interface contract

The names and signatures every task relies on.

| Name | Where | Signature / value | Introduced |
|---|---|---|---|
| `DISPATCHED_AP_TYPES` | `app/activitypub/util.py` | `frozenset[str]` of the types the dispatcher compares against | Task 1 |
| `NO_STRING_TYPE_MESSAGE` | `app/activitypub/util.py` | `'Activity type has no string value'` | Task 1 |
| `normalise_activity_types` | `app/activitypub/util.py` | `(activity: dict) -> dict \| None` | Task 1 |
| `AP_MEDIA_TYPE`, `AS2_PROFILE`, `AS2_LD_MEDIA_TYPE`, `AP_MEDIA_TYPES`, `AP_FETCH_ACCEPT` | `app/constants.py` | `'application/activity+json'`; `'https://www.w3.org/ns/activitystreams'`; `'application/ld+json; profile="https://www.w3.org/ns/activitystreams"'`; `('application/activity+json', 'application/ld+json')`; `'application/activity+json, application/ld+json; profile="https://www.w3.org/ns/activitystreams"'` | Task 2 |
| `accepted_media_ranges` | `app/utils.py` | `(accept_header: str) -> list[tuple[str, dict, float]]` | Task 2 |
| `is_activitypub_request` | `app/utils.py` (re-exported by `app/activitypub/util.py`) | `() -> bool`; q=0 and wildcards ignored | Task 2 |
| `ap_media_type` | `app/activitypub/routes.py` | `() -> str` (request context). Every AP response sets `resp.content_type = ap_media_type()` | Task 2 |
| `ap_application_cache_key` | `app/main/routes.py` | `() -> str` | Task 2 |
| `names_actor` | `app/activitypub/util.py` | `(candidate, actor) -> bool` | Task 3 |
| `delivery_inbox_of`, `INBOX_URL_MAX` | `app/activitypub/util.py` | `(actor_json: dict) -> str \| None`; `255` | Task 3 |
| `ACTOR_UPDATE_TYPES`, `process_actor_update` | `app/activitypub/routes.py` | `('Person', 'Service', 'Application', 'Podcast')`; `(user: User, actor_json: dict, id, saved_json) -> None` | Task 3 |
| `shared_inbox_of` | `app/activitypub/util.py` | unchanged `(actor_json: dict) -> str \| None`; a same-host string `endpoints` is fetched once with `fetch_object_capped` | Task 4 |
| `undo_vote` | `app/activitypub/util.py` | `(comment, post, target_ap_id, user, emoji: str \| None = None) -> Post \| PostReply \| None` | Task 5 |
| `Report.ap_id` | `app/models.py` | `db.Column(db.String(255), index=True)`, nullable; index `ix_report_ap_id`; migration `e2a7c4f9d1b3` | Task 6 |
| `REPORT_SUSPECT`, `withdraw_report`, `withdraw_flag` | `app/activitypub/util.py` | `{Post: ('suspect_post_id', REPORT_TYPE_POST), PostReply: ('suspect_post_reply_id', REPORT_TYPE_REPLY), User: ('suspect_user_id', REPORT_TYPE_USER)}`; `(report: Report, session) -> None`; `(user: User, flag: dict, session) -> tuple[aplog_result, str \| None]` | Task 6 |
| `may_moderate` | `app/activitypub/routes.py` | `(community: Community, actor) -> bool` | Task 7 |
| `process_add`, `process_remove` | `app/activitypub/routes.py` | `(core_activity: dict, user, community, feed, announced: bool, id: str, saved_json, session, undo: bool = False) -> None` | Task 7 |
| `Community.is_gone` | `app/models.py` | `() -> bool`: `self.ap_deleted_at is not None` | Task 8 |
| `GROUP_DELETED_MESSAGE` | `app/activitypub/util.py` | `'Community was deleted on its home server'` | Task 8 |
| `_origin_refusal_of_delete` | `app/activitypub/util.py` | `(object_id: str, label: str, tombstone_required: bool = False) -> str \| None` | Task 8 (flag added) |
| `may_delete_group` | `app/activitypub/util.py` | `(actor_id: str, community: Community) -> bool`: the Group itself, or `_may_remove_from(actor_id, community)` (stored moderator/owner, or admin of the Group's host); lookup only | Task 8 |
| `mark_community_gone`, `delete_remote_group` | `app/activitypub/util.py` | `(community: Community) -> None`; `(community: Community, activity_id: str, saved_json) -> None`; the caller has checked authority | Task 8 |
| `user_followers_count`, `user_following_count` | `app/activitypub/util.py` | `(user_id: int) -> int` | Task 9 |
| `local_actor_user` | `app/activitypub/routes.py` | `(actor: str) -> User`; `abort(410)` deleted, `abort(404)` banned/unknown/remote | Task 9 |
| `count_only_collection` | `app/activitypub/routes.py` | `(collection_id: str, total: int) -> Response` | Task 9 |
| `user_following` | `app/activitypub/routes.py` | route `GET /u/<actor>/following` | Task 9 |
| `create_activity_of` | `app/activitypub/util.py` | `(obj: Post \| PostReply) -> dict`; `post_to_activity` uses it | Task 10 |
| `user_outbox_rows`, `user_outbox_page` | `app/activitypub/routes.py` | `(user: User) -> Subquery` of `(kind, id, posted_at)`; `(rows, offset: int, limit: int) -> list[dict]` | Task 10 |
| `ix_post_user_posted`, `ix_post_reply_user_posted` | migration `f4b8d2a6c1e7`, `app/models.py` | `(user_id, posted_at DESC) WHERE deleted = false` | Task 10 |
| `ap_alternate_url` | `app/utils.py` | `(actor: User \| Community \| Feed) -> str` | Task 11 |
| `ap_alternate` | render kwarg of `base.html` and `themes/dillo/base.html` | `str \| None` | Task 11 |
| `webfinger_self_href`, `canonical_handle` | `app/activitypub/util.py` | `(address: str, server: str) -> str \| None`; `(actor_json, address: str, server: str, prefix: str = '') -> tuple[str, str] \| None`. Called by `create_actor_from_remote`, `search_for_user`, `search_for_community`, `search_for_feed` (`prefix='~'`) | Task 12 |
| `host_meta_json` | `app/activitypub/routes.py` | route `GET /.well-known/host-meta.json`, `application/jrd+json`, `@cache.cached(timeout=600)` | Task 12 |

## Review Focus

1. **Identifiers that differ only by case or whitespace.**
   - Cases: a pushed actor `id` such as `https://PEER.example/u/Wakko`, a fetched `sharedInbox` on `PEER.Example`, a request for `/u/ALICE/outbox`.
   - Each must be treated as the same actor or host.
   - A lookalike host (`peer.example.evil.example`) must never be fetched.
   - Tests: `test_names_actor` (Task 3), `test_a_shared_inbox_whose_host_differs_only_by_case_is_used` and the `lookalike-host` row (Task 4), `test_the_lookup_ignores_case_and_answers_the_canonical_id` (Tasks 9 and 10).
2. **A browser's Accept header.**
   - `text/html,*/*;q=0.8`, and an explicit `application/activity+json;q=0`, must still get HTML.
   - A shared cache must not serve one Accept's body to another.
   - Tests: `test_is_activitypub_request_ignores_a_q_value_of_zero` (the `*/*` row), `test_an_activitypub_answer_without_its_own_vary_gets_vary_accept` and `test_the_instance_application_is_cached_once_per_content_type` (Task 2).
3. **An LD-signed activity with `type: ['Create']`.**
   - It must still get the R-LDDOWNGRADE origin refetch.
   - The LD signature must still be checked against the bytes as sent.
   - Tests: `test_an_ld_signed_array_create_still_gets_its_origin_refetch` and `test_the_ld_signature_is_checked_against_the_document_as_sent` (Task 1).
4. **Late or duplicated Undos.**
   - Undo of 🎉 after the user switched to 👍: the 👍 stays.
   - An Undo of an Add or Remove whose target state already holds: a logged no-op, with no second modlog entry, no duplicate FeedItem and no feed auto-unsubscribe sweep.
   - Two reporters, one withdraws: the other report, its notification and one count survive.
   - Tests: `test_an_undo_naming_another_emoji_keeps_the_current_reaction` (Task 5), `test_another_reporters_report_and_notification_are_untouched` (Task 6), the five `..._is_a_logged_no_op` tests (Task 7).
5. **A deleted Group that keeps talking, its remote moderators, and who may delete it.**
   - A repeated Delete, an Update or an Announce from a gone Group is ignored, with no refetch and no second unsubscribe.
   - Remote users' `CommunityMember` rows survive. Only local members are unsubscribed, and no Undo Follow is sent.
   - A moderator on another host than the Group (Lemmy allows it) can delete it: the Announced Delete is verified at the origin, not deferred and dropped. An admin of some other host cannot.
   - Tests: `test_a_gone_groups_update_or_repeated_delete_is_ignored_without_a_fetch`, `test_a_gone_groups_announce_is_ignored`, `test_a_moderator_delete_of_a_group_already_gone_is_ignored_without_a_fetch`, `test_a_verified_self_delete_marks_the_group_and_unsubscribes_only_local_members`, `test_a_moderator_on_another_host_is_verified_not_deferred`, `test_an_admin_of_another_host_may_not_delete_the_group` (Task 8).

---

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `app/activitypub/routes.py` | inbox entry normalisation; `ap_media_type` and AP content types; actor Update arm; Group Update signer rule; Undo sub-arms; `process_add`/`process_remove`/`may_moderate`; Group self-Delete gate and gone-Group checks; Person `followers`/`following`; `local_actor_user`; count-only collections; user outbox; host-meta.json | 1, 2, 3, 5, 6, 7, 8, 9, 10, 12 |
| `app/activitypub/util.py` | `normalise_activity_types`; `names_actor`, `delivery_inbox_of`, inbox refresh; `shared_inbox_of` fetch; `undo_vote`; `process_report` ap_id, `withdraw_report`, `withdraw_flag`; `_verify_announced_delete` Group pass-through, `_origin_refusal_of_delete` flag, `may_delete_group`, `mark_community_gone`, `delete_remote_group`; follow counters; `create_activity_of`; `webfinger_self_href`, `canonical_handle`; outgoing Accept | 1, 2, 3, 4, 5, 6, 8, 9, 10, 12 |
| `app/activitypub/actor.py` | outgoing Accept; reverse check in `create_actor_from_remote` | 2, 12 |
| `app/activitypub/signature.py` | signed GET Accept | 2 |
| `app/constants.py` | AP media-type constants, `AP_FETCH_ACCEPT` | 2 |
| `app/utils.py` | `accepted_media_ranges`, q-aware `is_activitypub_request`; `can_*` predicates refuse a gone community; `ap_alternate_url` | 2, 8, 11 |
| `app/request_hooks.py` | `Vary: Accept` and cookie suppression for every AP media type | 2 |
| `app/main/routes.py` | instance actor/application content types, cache key, index Accept check | 2 |
| `app/models.py` | `Report.ap_id`; `Community.is_gone`; outbox indexes | 6, 8, 10 |
| `migrations/versions/e2a7c4f9d1b3_report_ap_id.py` (new) | `report.ap_id` + `ix_report_ap_id` | 6 |
| `migrations/versions/f4b8d2a6c1e7_user_outbox_indexes.py` (new) | partial outbox indexes | 10 |
| `app/community/routes.py` | `do_subscribe` refuses a gone community; `show_community` notice; `Link` header and `ap_alternate` | 8, 11 |
| `app/api/alpha/utils/community.py` | `post_community_follow` refuses a gone community | 8 |
| `app/shared/tasks/pages.py`, `notes.py`, `deletes.py` | no delivery to a gone Group | 8 |
| `app/post/routes.py`, `app/user/routes.py`, `app/feed/routes.py` | outgoing Accept (post); `ap_alternate`; profile `Link` header; creator for local plain Page posts | 2, 11 |
| `app/community/util.py`, `app/feed/util.py`, `app/user/utils.py` | outgoing Accept default; WebFinger resolvers ask `canonical_handle` | 2, 12 |
| `app/discovery/credits.py` | outgoing Accept | 2 |
| `app/templates/base.html`, `app/templates/themes/dillo/base.html` | AP alternate `<link>` | 11 |
| `tests/test_ap_interop_*.py` (new, one per task) and `tests/test_migration_report_ap_id.py`, `tests/test_migration_user_outbox_indexes.py` (new) | tests | all |

## Overlapping edits

Tasks run in order. Where a later task edits text an earlier task already changed, the later task's "change X" snippet quotes HEAD `0c861692e`. Apply the later change to the text as it now reads.

| Place | Tasks, in order | How to combine |
|---|---|---|
| `app/activitypub/routes.py`, the `from app.activitypub.util import ...` block ending `announce_target_uri, undo_boost, LD_SIGNED_LOG_PREFIX, current_ld_signed` | 1, 3, 6, 8, 9, 10 | Each task appends its names to the block's last line, with a `, \` continuation. |
| `shared_inbox` account-deletion block | 1 (the `if ... 'Delete'` condition reads `typed_json`), 8 (the body looks up a Community) | Independent lines. |
| `process_inbox_request` Create/Update arm | 1 (the `Unacceptable type (create)` line becomes an f-string), 3 (actor-Update arm, Group Update rule), 8 (gone-Group check first in the Community-actor branch) | Task 8's check goes before Task 3's check. |
| `process_inbox_request` Undo arm | 5 (EmojiReact joins Like/Dislike), 6 (Flag sub-arm above `'Unmatched activity'`), 7 (Add/Remove sub-arms directly above the Flag sub-arm) | As stated. |
| `user_profile`, `user_followers`, `user_outbox` | 2 (content type), 9, 10 (rewrite) | Tasks 9 and 10 replace whole functions "as Task 2 left it". |
| `app/community/routes.py:show_community` | 8 (gone flash), 11 (`Link` header, `ap_alternate`) | Independent lines. |
| `app/utils.py` | 2, 8, 11 | Independent functions. |
| `app/models.py` | 6 (`Report`), 8 (`Community`), 10 (`Post`/`PostReply` `__table_args__`) | Independent classes. |
| `app/activitypub/util.py` after `shared_inbox_of` | 1 (adds helpers after it), 3 (`delivery_inbox_of`), 4 (changes its body) | Independent definitions. |
| `app/activitypub/util.py` before `actor_json_to_model` | 12 (`webfinger_self_href`, `canonical_handle`) | Independent definitions. |
| `app/activitypub/util.py` delete helpers (`_verify_announced_delete`, `_origin_refusal_of_delete`) | 2 (`_origin_refusal_of_delete`'s Accept becomes `AP_FETCH_ACCEPT`), 8 (`tombstone_required`; Group pass-through) | Independent lines. |
| `app/user/utils.py:search_for_user`, `app/community/util.py:search_for_community`, `app/feed/util.py:search_for_feed` and their import lines | 2 (`type = links['type'] if ... else AP_FETCH_ACCEPT`; constants imports), 12 (`canonical_handle` before `actor_json_to_model`; `app.activitypub.util` imports) | Independent lines. |
| `app/activitypub/actor.py` `from app.activitypub.util import ...` | 12 appends `canonical_handle`; 2 adds a separate `from app.constants import AP_FETCH_ACCEPT` line | Independent lines. |

## Task index

| Order | Task | Spec item |
|---|---|---|
| 1 | Array-valued `type` | 8 (crash) |
| 2 | Content type | 5 |
| 3 | Inbound actor Update | 2 |
| 4 | String `endpoints` | 4 |
| 5 | Undo of EmojiReact | 3 |
| 6 | Undo of Flag (+ migration) | 3 |
| 7 | Undo of Add/Remove | 3 |
| 8 | Delete of a remote Group | 10 |
| 9 | Person `followers`/`following` | 6 |
| 10 | Person outbox (+ migration) | 7 |
| 11 | HTML discovery | 1 |
| 12 | WebFinger reverse check, host-meta.json | 9 |
| 13 | Final gate | all |

This is the spec's Order: 8, 5, 2, 4, 3, 10, 6, 7, 1, 9. Item 2 (Task 3) lands before item 4 (Task 4), as the spec allows. Task 3 uses `shared_inbox_of` as it stands, and Task 4 extends it underneath, so Task 3's refreshes get the fetch without changing.

---

### Task 1: An array-valued `type` is dispatched on one type (item 8)

**Files:**
- Modify: `app/activitypub/util.py` (add `import copy` to the stdlib imports at the top; add `DISPATCHED_AP_TYPES`, `NO_STRING_TYPE_MESSAGE`, `_single_type`, `_type_holders` and `normalise_activity_types` directly after `shared_inbox_of`, which ends at U:741)
- Modify: `app/activitypub/routes.py`:
  - the `from app.activitypub.util import ...` block (R:24-35): import `normalise_activity_types` and `NO_STRING_TYPE_MESSAGE`;
  - `shared_inbox` (R:747-805): normalise after the minimum-fields check, read `typed_json` for the Announce, relay-gate and account-deletion checks;
  - `replay_inbox_request` (R:939-962): read `typed_json` for the Announce and account-deletion checks;
  - `process_inbox_request` (R:999-1026): normalise first, before the relay and LD-signature steps; keep the raw document for the logs;
  - the Create/Update fallthrough log line (R:1559).
- Test: `tests/test_ap_interop_array_type.py` (new)

**Interfaces:**
- Consumes: `tests/test_inbox_dispatch_preamble.py:dispatch(activity, store_ap_json=True)`; `tests/test_inbox_dispatch_create_update.py:create_activity(actor, obj, *, activity_type='Create', **outer)`; `tests/test_inbox_dispatch_lock_delete.py:record_moderation(monkeypatch, *names)`; `tests/conftest.py:ld_signed_body`, `unsigned_but_precheck_clean_headers`, fixtures `signing_peer`, `no_network_ld_signing`, `redis_double`.
- Produces (all in `app/activitypub/util.py`):
  - `DISPATCHED_AP_TYPES: frozenset[str]`
  - `NO_STRING_TYPE_MESSAGE = 'Activity type has no string value'`
  - `normalise_activity_types(activity: dict) -> dict | None`: returns `activity` itself when no `type` it covers is an array; otherwise a normalised deep copy; None when an array holds no string. Covers `activity['type']`, `activity['object']['type']` (object a dict) and, when the activity is an Announce, `activity['object']['object']['type']`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ap_interop_array_type.py`:

```python
"""Interop C1 item 8 (docs/superpowers/specs/2026-10-11-ap-interop-c1-design.md): an array-valued `type`.

JSON-LD lets `type` be an array. The inbox compared it as a string, so an array activity type was dropped
silently, and an array object type reached `'Unacceptable type (create): ' + object_type`, a TypeError that
failed the Celery task. The inbox now reduces each array to one type on a copy it dispatches; the JSON that is
stored, logged and LD-verified stays as it was sent.
"""
import json

import pytest

from app import db
from app.activitypub import routes as activitypub_routes
from app.activitypub.routes import replay_inbox_request
from app.activitypub.util import NO_STRING_TYPE_MESSAGE, normalise_activity_types
from app.models import ActivityPubLog
from app.utils import utcnow
from tests.conftest import ld_signed_body, unsigned_but_precheck_clean_headers
from tests.factories import inbox_activity, make_community, make_instance, make_site, make_user, seed_community_owner
from tests.test_inbox_dispatch_create_update import create_activity
from tests.test_inbox_dispatch_lock_delete import record_moderation
from tests.test_inbox_dispatch_preamble import dispatch

pytestmark = pytest.mark.usefixtures('redis_double')

LOCAL_ACTOR = 'https://test.piefed.local/u/bob'


# ---- normalise_activity_types ----------------------------------------------------------------------------------

@pytest.mark.parametrize('value, expected', [
    (['Like'], 'Like'),
    (['litepub:EmojiReact', 'Like'], 'Like'),   # a type the inbox dispatches on beats an earlier unknown one
    (['Foo', 'Bar'], 'Foo'),                     # none it dispatches on: the first string
    ([7, 'Note'], 'Note'),                       # non-strings are skipped
])
def test_an_array_activity_type_becomes_one_string(value, expected):
    activity = {'id': 'https://peer.example/a/1', 'type': value, 'actor': 'https://peer.example/u/alice',
                'object': 'https://peer.example/o/1'}
    assert normalise_activity_types(activity)['type'] == expected


def test_the_activity_it_was_given_is_never_changed():
    activity = {'id': 'https://peer.example/a/1', 'type': ['Create'], 'actor': 'https://peer.example/u/alice',
                'object': {'id': 'https://peer.example/o/1', 'type': ['Note', 'as:Note']}}
    original = json.loads(json.dumps(activity))

    normalised = normalise_activity_types(activity)

    assert normalised['type'] == 'Create' and normalised['object']['type'] == 'Note'
    assert normalised is not activity and normalised['object'] is not activity['object']
    assert activity == original


def test_an_activity_whose_types_are_all_strings_is_returned_as_it_is():
    activity = {'type': 'Like', 'object': {'type': 'Note'}}
    assert normalise_activity_types(activity) is activity


def test_an_announced_activitys_object_type_is_normalised():
    announce = {'type': 'Announce', 'object': {'type': 'Create', 'object': {'type': ['Note', 'Article']}}}
    assert normalise_activity_types(announce)['object']['object']['type'] == 'Note'


def test_an_array_announce_type_still_reaches_the_announced_object():
    announce = {'type': ['Announce'], 'object': {'type': 'Create', 'object': {'type': ['Page']}}}
    normalised = normalise_activity_types(announce)
    assert normalised['type'] == 'Announce' and normalised['object']['object']['type'] == 'Page'


def test_the_object_of_an_object_is_left_alone_outside_an_announce():
    """Only an Announce's inner object is something the dispatcher reads the type of."""
    undo = {'type': ['Undo'], 'object': {'type': 'Like', 'object': {'type': ['Note']}}}
    assert normalise_activity_types(undo)['object']['object']['type'] == ['Note']


@pytest.mark.parametrize('holder', ['activity', 'object', 'announced object'])
def test_an_array_with_no_string_is_refused(holder):
    announce = {'type': 'Announce', 'object': {'type': 'Create', 'object': {'type': 'Note'}}}
    target = {'activity': announce, 'object': announce['object'],
              'announced object': announce['object']['object']}[holder]
    target['type'] = [{'@id': 'x'}, 3]
    assert normalise_activity_types(announce) is None


# ---- the dispatcher ----------------------------------------------------------------------------------------------

def _create_setup(app, monkeypatch):
    """The Create/Update arm's prerequisites, as tests/test_inbox_dispatch_create_update.py builds them."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    community = make_community(host='peer.example')
    author = make_user(instance, 'author')
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json, author=None: community)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)
    return author


def test_an_array_activity_type_is_dispatched(app, db_session, monkeypatch):
    make_site()
    actor = make_user(make_instance('peer.example'), 'alice')
    actor.ap_fetched_at = utcnow()
    db.session.commit()
    calls = []
    monkeypatch.setattr(activitypub_routes, 'process_upvote', lambda *args, **kwargs: calls.append(args))
    activity = inbox_activity(actor, activity_type=['Like', 'litepub:EmojiReact'])

    dispatch(activity)

    assert len(calls) == 1
    assert calls[0][2]['type'] == 'Like'
    assert activity['type'] == ['Like', 'litepub:EmojiReact']


def test_an_array_object_type_is_dispatched(app, db_session, monkeypatch):
    author = _create_setup(app, monkeypatch)
    calls = record_moderation(monkeypatch, 'process_new_content')

    dispatch(create_activity(author, {'type': ['Note', 'as:Note'], 'id': 'https://peer.example/post/1'}))

    assert len(calls['process_new_content']) == 1
    args, _kwargs = calls['process_new_content'][0]
    assert args[3]['object']['type'] == 'Note'


def test_an_unknown_array_object_type_falls_through_and_is_logged(app, db_session, monkeypatch):
    """No exception, and the stored JSON is the document as sent."""
    author = _create_setup(app, monkeypatch)

    dispatch(create_activity(author, {'type': ['Foo', 'Bar'], 'id': 'https://peer.example/x/1'}))

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Unacceptable type (create): Foo'
    assert json.loads(log.activity_json)['object']['type'] == ['Foo', 'Bar']


@pytest.mark.parametrize('object_type, shown', [(5, '5'), (None, 'None'), ({'@id': 'Note'}, "{'@id': 'Note'}")])
def test_the_unacceptable_type_log_line_survives_any_type_value(app, db_session, monkeypatch, object_type, shown):
    author = _create_setup(app, monkeypatch)

    dispatch(create_activity(author, {'type': object_type, 'id': 'https://peer.example/x/1'}))

    assert ActivityPubLog.query.one().exception_message == f'Unacceptable type (create): {shown}'


def test_the_dispatcher_refuses_an_array_of_non_strings(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    make_site()
    actor = make_user(make_instance('peer.example'), 'alice')
    activity = inbox_activity(actor, activity_type=[1, 2])

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.result == 'failure' and log.exception_message == NO_STRING_TYPE_MESSAGE
    assert json.loads(log.activity_json)['type'] == [1, 2]


# ---- shared_inbox and replay -------------------------------------------------------------------------------------

@pytest.fixture
def dispatched(app, monkeypatch):
    monkeypatch.setitem(app.config, 'DEBUG', True)
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    calls = []
    monkeypatch.setattr(activitypub_routes, 'process_inbox_request', lambda *a, **kw: calls.append((a, kw)))
    return calls


def _post_unsigned(app, activity):
    body = json.dumps(activity).encode('utf8')
    with app.test_client() as client:
        return client.post('/inbox', data=body, headers=unsigned_but_precheck_clean_headers(body),
                           content_type='application/activity+json')


def test_the_inbox_refuses_an_array_of_non_strings_before_anything_else(app, db_session, dispatched):
    make_site()
    activity = {'id': 'https://peer.example/a/1', 'type': [1, 2], 'actor': 'https://peer.example/u/alice',
                'object': 'https://peer.example/o/1'}

    response = _post_unsigned(app, activity)

    assert response.status_code == 200 and dispatched == []
    assert ActivityPubLog.query.one().exception_message == NO_STRING_TYPE_MESSAGE


def test_the_inbox_reads_an_array_announce_type(app, db_session, dispatched):
    """An Announce of an activity by a local actor is dropped as a duplicate. With `type: ['Announce']` the check
    used to be skipped and the request went on to fail its signature checks instead."""
    make_site()
    activity = {'id': 'https://peer.example/a/1', 'type': ['Announce'], 'actor': 'https://peer.example/c/news',
                'object': {'id': 'https://test.piefed.local/activities/1', 'type': 'Create',
                           'actor': LOCAL_ACTOR, 'object': 'https://test.piefed.local/post/1'}}

    response = _post_unsigned(app, activity)

    assert response.status_code == 200 and dispatched == []
    assert ActivityPubLog.query.one().exception_message == 'Activity about local content which is already present'


def test_the_inbox_reads_an_array_delete_type_for_an_account_deletion(app, db_session, dispatched):
    make_site()
    activity = {'id': 'https://peer.example/a/1', 'type': ['Delete'], 'actor': 'https://peer.example/u/nobody',
                'object': 'https://peer.example/u/nobody'}

    response = _post_unsigned(app, activity)

    assert response.status_code == 200 and dispatched == []
    assert ActivityPubLog.query.one().exception_message == 'Does not exist here'


def test_the_ld_signature_is_checked_against_the_document_as_sent(app, signing_peer, dispatched,
                                                                  no_network_ld_signing):
    """Only an LD signature, so it alone decides. It was made over `type: ['Like']`; checking it against a
    normalised copy would fail with 400. The dispatcher gets the original and normalises its own copy."""
    body = ld_signed_body(signing_peer, activity_type=['Like'])
    with app.test_client() as client:
        response = client.post('/inbox', data=body, headers=unsigned_but_precheck_clean_headers(body),
                               content_type='application/activity+json')

    assert response.status_code == 200
    assert len(dispatched) == 1
    assert dispatched[0][0][0]['type'] == ['Like']
    assert dispatched[0][1] == {'ld_signed': True}


def test_an_ld_signed_array_create_still_gets_its_origin_refetch(app, db_session, http_mock, monkeypatch):
    """Review Focus: verify_ld_accepted_activity replaces a Create's object by its origin's copy, choosing by type.
    Normalising after it would let `type: ['Create']` skip the refetch and be processed as sent."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    make_site()
    actor = make_user(make_instance('peer.example'), 'alice')
    object_id = 'https://peer.example/objects/1'
    origin = http_mock.get(object_id).respond(404)
    activity = inbox_activity(actor, activity_type=['Create'],
                              object={'id': object_id, 'type': 'Note', 'attributedTo': actor.ap_profile_id})

    activitypub_routes.process_inbox_request(activity, True, ld_signed=True)

    assert origin.called
    assert 'LD-signed activity dropped' in ActivityPubLog.query.one().exception_message


def test_replay_reads_an_array_announce_type(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)

    replay_inbox_request({'id': 'https://peer.example/a/1', 'type': ['Announce'],
                          'actor': 'https://peer.example/c/news',
                          'object': {'id': 'https://test.piefed.local/activities/1', 'type': 'Create',
                                     'actor': LOCAL_ACTOR, 'object': 'https://test.piefed.local/post/1'}})

    assert ActivityPubLog.query.one().exception_message == \
        'REPLAY: Activity about local content which is already present'
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_ap_interop_array_type.py -q`
Expected: FAIL at collection with `ImportError: cannot import name 'NO_STRING_TYPE_MESSAGE' from 'app.activitypub.util'`.

- [ ] **Step 3: Implement**

In `app/activitypub/util.py`, add `import copy` to the stdlib imports at the top (after `from __future__ import annotations`, before `import html`). Directly after `shared_inbox_of` (ends at U:741), add:

```python
# Every `type` the inbox dispatches on (process_inbox_request in routes.py and the handlers it calls), so an
# array-valued type resolves to the one this instance acts on rather than to an extension alias listed first.
DISPATCHED_AP_TYPES = frozenset({
    'Accept', 'Add', 'Announce', 'Block', 'ChooseAnswer', 'Create', 'Delete', 'Dislike', 'EmojiReact', 'Flag',
    'Follow', 'Like', 'Lock', 'Move', 'PollVote', 'QuoteRequest', 'Reject', 'Remove', 'Undo', 'Update',
    'Application', 'Article', 'ChatMessage', 'Event', 'Feed', 'Group', 'Link', 'Note', 'OrderedCollection', 'Page',
    'Person', 'Podcast', 'Question', 'Service', 'Tombstone', 'Video',
})

NO_STRING_TYPE_MESSAGE = 'Activity type has no string value'


def _single_type(value):
    """`value` unchanged unless it is an array. For an array: its first type the inbox dispatches on, else its
    first string, else None."""
    if not isinstance(value, list):
        return value
    strings = [entry for entry in value if isinstance(entry, str)]
    dispatched = [entry for entry in strings if entry in DISPATCHED_AP_TYPES]
    return (dispatched or strings or [None])[0]


def _type_holders(activity: dict) -> list:
    """The dicts whose `type` the dispatcher reads: the activity, its object, and an Announce's announced object."""
    holders = [activity]
    obj = activity.get('object')
    if isinstance(obj, dict):
        holders.append(obj)
        if _single_type(activity.get('type')) == 'Announce' and isinstance(obj.get('object'), dict):
            holders.append(obj['object'])
    return holders


def normalise_activity_types(activity: dict) -> dict | None:
    """The activity with each array-valued `type` the dispatcher reads reduced to one string (interop C1 item 8).

    JSON-LD lets `type` be an array; the dispatcher compares it as a string. Returns `activity` itself when no type
    it covers is an array, else a deep copy with each array replaced by `_single_type`'s choice, so the original --
    what is stored, logged and LD-verified -- is never changed. Returns None when an array holds no string."""
    if not any(isinstance(holder.get('type'), list) for holder in _type_holders(activity)):
        return activity
    normalised = copy.deepcopy(activity)
    for holder in _type_holders(normalised):
        if isinstance(holder.get('type'), list):
            single = _single_type(holder['type'])
            if single is None:
                return None
            holder['type'] = single
    return normalised
```

In `app/activitypub/routes.py`, extend the util import's last line (R:35) from:

```python
    announce_target_uri, undo_boost, LD_SIGNED_LOG_PREFIX, current_ld_signed
```

to:

```python
    announce_target_uri, undo_boost, LD_SIGNED_LOG_PREFIX, current_ld_signed, normalise_activity_types, \
    NO_STRING_TYPE_MESSAGE
```

In `shared_inbox`, change:

```python
    id = request_json['id']
    if request_json['type'] == 'Announce' and isinstance(request_json['object'], dict):
        object = request_json['object']
```

to:

```python
    # interop C1 item 8: the checks below read one type each. request_json stays as sent, for the LD signature.
    typed_json = normalise_activity_types(request_json)
    if typed_json is None:
        log_incoming_ap(request_json['id'], APLOG_NOTYPE, APLOG_FAILURE, saved_json, NO_STRING_TYPE_MESSAGE)
        return '', 200

    id = request_json['id']
    if typed_json['type'] == 'Announce' and isinstance(typed_json['object'], dict):
        object = typed_json['object']
```

change:

```python
    answered = relay_inbound.relay_actor_gate(request, request_json)   # after the precheck; relays (spec 2026-10-08): never a User row
```

to:

```python
    answered = relay_inbound.relay_actor_gate(request, typed_json)   # after the precheck; relays (spec 2026-10-08): never a User row
```

and change:

```python
    if request_json['type'] == 'Delete' and 'object' in request_json and isinstance(request_json['object'], str) and \
            request_json['actor'] == request_json['object']:
```

to:

```python
    if typed_json['type'] == 'Delete' and 'object' in request_json and isinstance(request_json['object'], str) and \
            request_json['actor'] == request_json['object']:
```

Leave `LDSignature.verify_signature(request_json, ...)` and the `process_inbox_request(request_json, ...)` / `process_delete_request(request_json, ...)` calls as they are: they get the document as sent, and `process_inbox_request` normalises its own copy.

In `replay_inbox_request`, change:

```python
    id = request_json['id']
    if request_json['type'] == 'Announce' and isinstance(request_json['object'], dict):
        object = request_json['object']
```

to:

```python
    id = request_json['id']
    typed_json = normalise_activity_types(request_json) or request_json   # process_inbox_request refuses a None
    if typed_json['type'] == 'Announce' and isinstance(typed_json['object'], dict):
        object = typed_json['object']
```

and change:

```python
    if (request_json['type'] == 'Delete' and
```

to:

```python
    if (typed_json['type'] == 'Delete' and
```

In `process_inbox_request`, change:

```python
            with patch_db_session(session):
                if relay_id is not None:   # relays: a forwarded activity is kept only when top-level and wanted
                    allowed, reason = relay_inbound.relayed_activity_allowed(request_json)
                    if not allowed:
                        log_incoming_ap(request_json.get('id', ''), APLOG_NOTYPE, APLOG_IGNORED,
                                        request_json if store_ap_json else None, reason)
                        return
                if ld_signed:   # R-LDDOWNGRADE: an LD signature vouches for who and which object, not content
                    verified, reason = verify_ld_accepted_activity(request_json)
                    if verified is None:
                        log_incoming_ap(request_json.get('id', ''), APLOG_NOTYPE, APLOG_FAILURE,
                                        request_json if store_ap_json else None, f'LD-signed activity dropped: {reason}')
                        return
                    request_json = verified
```

to:

```python
            with patch_db_session(session):
                # interop C1 item 8: dispatch reads one type each, on a copy; raw_json is what was sent, and is what
                # the logs store. First, so the relay filter and the LD downgrade below read the type dispatch uses.
                raw_json = request_json
                request_json = normalise_activity_types(raw_json)
                if request_json is None:
                    log_incoming_ap(raw_json.get('id', ''), APLOG_NOTYPE, APLOG_FAILURE,
                                    raw_json if store_ap_json else None, NO_STRING_TYPE_MESSAGE)
                    return
                if relay_id is not None:   # relays: a forwarded activity is kept only when top-level and wanted
                    allowed, reason = relay_inbound.relayed_activity_allowed(request_json)
                    if not allowed:
                        log_incoming_ap(request_json.get('id', ''), APLOG_NOTYPE, APLOG_IGNORED,
                                        raw_json if store_ap_json else None, reason)
                        return
                if ld_signed:   # R-LDDOWNGRADE: an LD signature vouches for who and which object, not content
                    verified, reason = verify_ld_accepted_activity(request_json)
                    if verified is None:
                        log_incoming_ap(request_json.get('id', ''), APLOG_NOTYPE, APLOG_FAILURE,
                                        raw_json if store_ap_json else None, f'LD-signed activity dropped: {reason}')
                        return
                    request_json = verified
```

and, a few lines further down, change:

```python
                saved_json = request_json if store_ap_json else None
```

to:

```python
                # An LD-accepted activity keeps logging the verified document, as before
                saved_json = (request_json if ld_signed else raw_json) if store_ap_json else None
```

Finally, in the Create/Update arm, change:

```python
                            log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Unacceptable type (create): ' + object_type)
```

to:

```python
                            log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, f'Unacceptable type (create): {object_type}')
```

When no type is an array, `normalise_activity_types` returns the same dict, so `raw_json is request_json` and every existing log keeps storing exactly what it stored before, including the in-place `request_json['object'] = verified_inner` replacement.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_ap_interop_array_type.py tests/test_inbox_dispatch_create_update.py tests/test_inbox_dispatch_preamble.py tests/test_inbox_gate_dispatch.py tests/test_inbox_gate_signatures.py tests/test_relays_inbound.py tests/test_ap_security_key_id_host.py -q`
Expected: PASS. `test_an_unacceptable_object_type_names_itself_in_the_failure` and `test_a_create_of_a_group_is_unacceptable_because_the_group_branch_requires_update` keep their exact messages: the f-string renders a string type unchanged.

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/util.py app/activitypub/routes.py tests/test_ap_interop_array_type.py
git commit -m "fix: an activity with an array-valued type is dispatched instead of dropped

JSON-LD allows type to be an array. The inbox compared it as a string, so an
array activity type was silently ignored, and an array object type crashed
the inbox task on 'Unacceptable type (create): ' + object_type. The inbox
now reduces each array (activity, object, an Announce's inner object) to its
first type the dispatcher knows, else its first string, on a copy. The JSON
that is stored, logged and LD-verified stays as sent. An array with no string
is refused and logged, and the fallthrough log line accepts any type value.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Task gate**

Run, in order:
1. `./run_tests.sh tests/test_ap_interop_array_type.py tests/test_inbox_dispatch_create_update.py tests/test_inbox_dispatch_preamble.py tests/test_inbox_gate_dispatch.py tests/test_relays_inbound.py --cov=app --cov-branch --cov-report=json -q`
2. `python3 tests/check_changed_line_coverage.py coverage.json HEAD~1 --branches app/`

Expected: all pass; every added line and branch in `app/` executed.

---

### Task 2: The ActivityPub content type follows the Accept header, and outgoing GETs ask for both forms (item 5)

**Files:**
- Modify: `app/constants.py` (append the media-type constants at the end of the file)
- Modify: `app/utils.py:2338-2339` (`accepted_media_ranges`, new, directly above; `is_activitypub_request` ignores q=0)
- Modify: `app/activitypub/util.py`:
  - delete the byte-identical twin `is_activitypub_request` (U:2671-2672) and import the `app/utils.py` one instead (the `from app.utils import ...` block, U:40-50), so `from app.activitypub.util import is_activitypub_request` (R:26, `app/main/routes.py:19`) keeps working;
  - outgoing AP GETs send `AP_FETCH_ACCEPT` at U:799, U:970, U:1149, U:1189, U:1198, U:1241, U:1332, U:1376, U:1787, U:1819, U:2567, U:2571, U:4052, U:4056, U:5182, U:5725. U:2639 (nodeinfo) keeps its own Accept.
- Modify: `app/activitypub/routes.py`:
  - add `ap_media_type()` directly above `ap_content_headers` (R:2667);
  - every `resp.content_type = 'application/activity+json'` (R:481, 547, 595, 687, 2448, 2481, 2513, 2539, 2574, 2601, 2624, 2702, 2756, 2793, 2820, 2839, 2848, 3317, 3366, 3410, 3451, 3482) and `response.headers['Content-Type'] = 'application/activity+json'` (R:3589) goes through it;
  - the inline Accept tests at R:608 and R:3247-3249 become `is_activitypub_request()`.
- Modify: `app/main/routes.py`:
  - R:61 import; `index` inline Accept test (R:71-72);
  - `activitypub_application` cache key (R:1273);
  - content types at R:1302, 1334, 1344, 1431.
- Modify: `app/request_hooks.py:149` (an `application/ld+json` answer is an ActivityPub answer too; AP answers get `Vary: Accept`)
- Modify: `app/activitypub/signature.py:60` (import) and `:775` (signed GET Accept)
- Modify: `app/activitypub/actor.py:197` and `:275`; `app/utils.py:4059`; `app/post/routes.py:2757`; `app/community/util.py:100`; `app/feed/util.py:89`; `app/user/utils.py:142`; `app/discovery/credits.py:34`
- Modify (tests pinning the old outgoing Accept or the old served type): `tests/test_activitypub_actor.py:1075`, `tests/test_ap_refresh_profiles.py:2123,2160`, `tests/test_user_utils.py:222-230`, `tests/test_podcast_episode_audio.py:47`, `tests/test_activitypub_signature.py:869`, `tests/test_ap_security_double_knock.py:287-292`, `tests/test_actor_profiles.py:101-113`, `tests/test_ap_security_secure_mode.py:206` (docstring)
- Test: `tests/test_ap_interop_content_type.py` (new)

**Interfaces:**
- Consumes: `tests/test_actor_profiles.py:seed_actors(host='peer.example')`, `profile_get(app, path, accept=None)`, `_double_the_renderers(monkeypatch)`; `tests/test_ap_security_entitlement.py:_requester_for(app, path, headers)`; `tests/test_main_front_page.py:env` fixture; `tests/factories.py:signed_get_headers`, `a_keypair`.
- Produces:
  - `app/constants.py`: `AP_MEDIA_TYPE = 'application/activity+json'`; `AS2_PROFILE = 'https://www.w3.org/ns/activitystreams'`; `AS2_LD_MEDIA_TYPE = 'application/ld+json; profile="https://www.w3.org/ns/activitystreams"'`; `AP_MEDIA_TYPES = ('application/activity+json', 'application/ld+json')`; `AP_FETCH_ACCEPT = 'application/activity+json, application/ld+json; profile="https://www.w3.org/ns/activitystreams"'`.
  - `app/utils.py`: `accepted_media_ranges(accept_header: str) -> list[tuple[str, dict, float]]`; `is_activitypub_request() -> bool` (unchanged signature).
  - `app/activitypub/routes.py`: `ap_media_type() -> str` (needs a request context). Every later task that serves an AP document sets `resp.content_type = ap_media_type()`.
  - `app/main/routes.py`: `ap_application_cache_key() -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ap_interop_content_type.py`:

```python
"""Interop C1 item 5 (docs/superpowers/specs/2026-10-11-ap-interop-c1-design.md): the ActivityPub content type.

Served: `application/ld+json; profile="https://www.w3.org/ns/activitystreams"` when the Accept header prefers
JSON-LD, else `application/activity+json`, through one helper, ap_media_type(). Asked for: every outgoing
ActivityPub GET sends both forms (AP_FETCH_ACCEPT), signed GETs included, and the signature covers it.
is_activitypub_request() ignores a media type whose q-value is 0.
"""
import pathlib
import re

import pytest
from flask import Response, g

from app import db
from app.activitypub.actor import fetch_remote_actor_data
from app.activitypub.entitlement import secure_mode_cache_headers
from app.activitypub.routes import ap_media_type
from app.activitypub.signature import signed_get_request
from app.constants import AP_FETCH_ACCEPT, AS2_LD_MEDIA_TYPE
from app.main.routes import ap_application_cache_key
from app.utils import accepted_media_ranges, is_activitypub_request
from tests.factories import a_keypair, make_community, make_instance, make_site, make_user, signed_get_headers
from tests.test_actor_profiles import _double_the_renderers, profile_get, seed_actors
from tests.test_ap_security_entitlement import _requester_for
from tests.test_main_front_page import env  # noqa: F401 -- the fixture

AP = 'application/activity+json'
LD_PLAIN = 'application/ld+json'
APP = pathlib.Path(__file__).resolve().parent.parent / 'app'


# ---- parsing -----------------------------------------------------------------------------------------------------

def test_accepted_media_ranges_reads_types_parameters_and_q_values():
    assert accepted_media_ranges('Application/LD+JSON; Profile="x y"; q=0.4, text/html,,a/b;q=junk;q=2') == [
        ('application/ld+json', {'profile': 'x y'}, 0.4),
        ('text/html', {}, 1.0),
        ('a/b', {}, 1.0),   # an unreadable q counts as 1; one above 1 is clamped to 1
    ]


@pytest.mark.parametrize('accept, expected', [
    (AP, AP),
    (AS2_LD_MEDIA_TYPE, AS2_LD_MEDIA_TYPE),
    (LD_PLAIN, AS2_LD_MEDIA_TYPE),
    (f'{AP};q=0.5, {LD_PLAIN};q=0.9', AS2_LD_MEDIA_TYPE),
    (f'{AP};q=0.9, {AS2_LD_MEDIA_TYPE};q=0.5', AP),
    (AP_FETCH_ACCEPT, AP),                                      # equal preference: activity+json
    (f'{LD_PLAIN};q=0', AP),
    (f'{AP};q=0, {LD_PLAIN}', AS2_LD_MEDIA_TYPE),
    ('application/ld+json; profile="https://example.com/other"', AP),   # JSON-LD, but not ActivityStreams
    ('text/html, */*;q=0.8', AP),
    ('', AP),
])
def test_the_served_content_type_follows_the_accept_header(app, accept, expected):
    with app.test_request_context('/', headers={'Accept': accept}):
        assert ap_media_type() == expected


@pytest.mark.parametrize('accept, expected', [
    (AP, True),
    (LD_PLAIN, True),
    (AS2_LD_MEDIA_TYPE, True),
    (f'text/html, {AP};q=0.1', True),
    (f'{AP};q=0', False),
    (f'text/html, {LD_PLAIN};q=0, {AP};q=0.0', False),
    ('*/*', False),
    ('text/html', False),
    ('', False),
])
def test_is_activitypub_request_ignores_a_q_value_of_zero(app, accept, expected):
    with app.test_request_context('/', headers={'Accept': accept}):
        assert is_activitypub_request() is expected


# ---- served ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('module', ['activitypub/routes.py', 'main/routes.py'])
def test_no_activitypub_route_hard_codes_its_content_type(module):
    source = (APP / module).read_text()
    assert "content_type = 'application/activity+json'" not in source
    assert "headers['Content-Type'] = 'application/activity+json'" not in source


def test_a_user_document_asked_for_as_json_ld_is_served_as_json_ld(app, db_session, monkeypatch):
    site, instance = seed_actors()
    make_user(instance, 'alice', local=True)
    _double_the_renderers(monkeypatch)

    ld = profile_get(app, '/u/alice', accept=LD_PLAIN)
    activity = profile_get(app, '/u/alice', accept=AP)

    assert ld.status_code == 200 and ld.content_type == AS2_LD_MEDIA_TYPE
    assert activity.status_code == 200 and activity.content_type == AP
    assert 'Accept' in [v.strip() for v in ld.headers['Vary'].split(',')]


def test_a_head_request_asked_for_as_json_ld_is_answered_as_json_ld(app, db_session, monkeypatch):
    site, instance = seed_actors()
    make_user(instance, 'alice', local=True)
    _double_the_renderers(monkeypatch)

    with app.test_client() as client:
        response = client.head('/u/alice', headers={'Accept': LD_PLAIN})

    assert response.status_code == 200 and response.content_type == AS2_LD_MEDIA_TYPE


def test_a_community_document_asked_for_with_the_profile_is_served_with_it(app, db_session, monkeypatch):
    seed_actors()
    make_community(name='books', host='test.piefed.local')
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/c/books', accept=AS2_LD_MEDIA_TYPE)

    assert response.status_code == 200 and response.content_type == AS2_LD_MEDIA_TYPE


def test_the_instance_actor_and_its_outbox_follow_the_accept_header(app, db_session):
    make_site()
    with app.test_client() as client:
        for path in ('/actor', '/actor/outbox'):
            assert client.get(path, headers={'Accept': LD_PLAIN}).content_type == AS2_LD_MEDIA_TYPE
            assert client.get(path, headers={'Accept': AP}).content_type == AP


def test_the_instance_application_follows_the_accept_header(env):
    assert env.client.get('/', headers={'Accept': LD_PLAIN}).content_type == AS2_LD_MEDIA_TYPE
    assert env.client.get('/', headers={'Accept': AP}).content_type == AP


def test_the_instance_application_is_cached_once_per_content_type(app):
    """@cache.cached keyed on the path alone would hand the first requester's content type to everyone for 6 s."""
    with app.test_request_context('/', headers={'Accept': LD_PLAIN}):
        ld_key = ap_application_cache_key()
    with app.test_request_context('/', headers={'Accept': AP}):
        ap_key = ap_application_cache_key()
    assert ld_key != ap_key
    assert ld_key.endswith(AS2_LD_MEDIA_TYPE) and ap_key.endswith(AP)


def test_an_activitypub_answer_without_its_own_vary_gets_vary_accept(app, db_session):
    """A public Tombstone sets no Vary of its own; its content type now depends on Accept."""
    seed_actors('test.piefed.local')
    user = make_user(None, 'gone', local=True)
    user.deleted = True
    user.banned = True
    db.session.commit()

    with app.test_client() as client:
        response = client.get('/u/gone', headers={'Accept': LD_PLAIN})

    assert response.status_code == 410
    assert response.content_type == AS2_LD_MEDIA_TYPE
    assert 'Accept' in [v.strip() for v in response.headers['Vary'].split(',')]


def test_a_json_ld_answer_does_not_set_a_session_cookie(app, db_session):
    """As tests/test_request_hooks.py pins for activity+json: an external Referer would mark the session modified."""
    user = make_user(make_instance('test.piefed.local', software='piefed'), 'apuser', local=True)

    with app.test_client() as client:
        response = client.get(f'/u/{user.user_name}', headers={'Accept': LD_PLAIN,
                                                               'Referer': 'http://elsewhere.example/page'})

    assert response.content_type == AS2_LD_MEDIA_TYPE
    assert not any('session=' in h for h in response.headers.getlist('Set-Cookie'))


def test_secure_mode_still_recognises_a_json_ld_answer(app, db_session):
    """secure_mode_cache_headers compares response.mimetype, which drops the profile parameter."""
    site = make_site()
    site.require_signed_fetch = True
    db.session.commit()
    with app.test_request_context('/post/1', method='GET', headers={'Accept': 'text/html'}):
        g.site = site
        response = Response('{}', content_type=AS2_LD_MEDIA_TYPE)
        response.cache_control.public = True
        secure_mode_cache_headers(response)

    assert 'private' in response.headers['Cache-Control']
    assert 'Signature' in response.vary


@pytest.mark.parametrize('path', ['/c/books@peer.example', '/f/news@peer.example'])
def test_a_zero_q_activitypub_accept_is_not_refused_as_an_activitypub_request(app, db_session, monkeypatch, path):
    """These two remote-handle refusals tested the Accept header inline, by substring, so q=0 still got 400."""
    seed_actors()
    _double_the_renderers(monkeypatch)

    with app.test_client() as client:
        response = client.get(path, headers={'Accept': f'{AP};q=0, text/html'})

    assert response.status_code == 404


def test_a_zero_q_activitypub_accept_gets_the_front_page(env):
    response = env.client.get('/', headers={'Accept': f'{AP};q=0, text/html'})
    assert response.status_code == 200
    assert response.content_type.startswith('text/html')


# ---- asked for ---------------------------------------------------------------------------------------------------

def test_an_outgoing_actor_fetch_asks_for_both_forms(app, db_session, http_mock):
    route = http_mock.get('https://peer.example/u/alice').respond(200, json={'id': 'https://peer.example/u/alice'})

    assert fetch_remote_actor_data('https://peer.example/u/alice') == {'id': 'https://peer.example/u/alice'}
    assert route.calls.last.request.headers['Accept'] == AP_FETCH_ACCEPT


def test_a_signed_get_asks_for_both_forms_and_signs_the_accept_header(app, db_session, http_mock):
    route = http_mock.get('https://peer.example/u/bob').respond(200, json={'id': 'https://peer.example/u/bob'})

    signed_get_request('https://peer.example/u/bob', a_keypair()[0], 'https://test.piefed.local/actor#main-key')

    sent = route.calls[0].request.headers
    assert sent['accept'] == AP_FETCH_ACCEPT
    assert 'accept' in re.search(r'headers="([^"]*)"', sent['signature']).group(1).split()


def test_a_signed_get_with_the_new_accept_still_verifies_at_the_peer(app, db_session):
    """Production's verifier, as a peer runs it. Accept is a signed header, so the new value is what is signed:
    changing it after signing breaks the signature."""
    make_site()
    signer = make_user(make_instance('peer.example'), 'alice', with_keys=True)
    headers = signed_get_headers('/post/1', signer)

    assert headers['Accept'] == AP_FETCH_ACCEPT
    assert _requester_for(app, '/post/1', headers) == signer
    headers['Accept'] = AP
    assert _requester_for(app, '/post/1', headers) is None
```

Update the tests that pin the old values:

(a) `tests/test_activitypub_actor.py`: add `from app.constants import AP_FETCH_ACCEPT` to the imports. In `test_a_webfinger_lookup_returns_the_actor_it_points_at` change

```python
    assert request.call_args_list[1].kwargs['headers'] == {'Accept': 'application/activity+json'}
```

to

```python
    assert request.call_args_list[1].kwargs['headers'] == {'Accept': AP_FETCH_ACCEPT}
```

Leave `test_a_link_advertising_its_own_type_is_asked_for_that_type` (`:1089`) unchanged: a type the peer advertises in its WebFinger link is still the one asked for (Clarification).

(b) `tests/test_ap_refresh_profiles.py`: add `from app.constants import AP_FETCH_ACCEPT` to the imports. In the docstring at `:2123` change `` `headers={'Accept': 'application/activity+json'}` `` to `` `headers={'Accept': AP_FETCH_ACCEPT}` ``, and at `:2160` change

```python
    assert sent.headers['accept'] == 'application/activity+json'
```

to

```python
    assert sent.headers['accept'] == AP_FETCH_ACCEPT
```

(c) `tests/test_user_utils.py`: add `from app.constants import AP_FETCH_ACCEPT` to the imports and change

```python
    def test_a_self_link_with_no_type_is_asked_for_as_activity_json(
            self, env, http_mock):
        http_mock.get(WEBFINGER).mock(return_value=httpx.Response(
            200, json={'links': [{'rel': 'self', 'href': ACTOR}]}))
        actor = http_mock.get(ACTOR).mock(
            return_value=httpx.Response(200, json=PERSON))
        search_for_user('@someone@remote.test')
        assert actor.calls[0].request.headers['Accept'] == \
            'application/activity+json'
```

to

```python
    def test_a_self_link_with_no_type_is_asked_for_as_activitypub(
            self, env, http_mock):
        http_mock.get(WEBFINGER).mock(return_value=httpx.Response(
            200, json={'links': [{'rel': 'self', 'href': ACTOR}]}))
        actor = http_mock.get(ACTOR).mock(
            return_value=httpx.Response(200, json=PERSON))
        search_for_user('@someone@remote.test')
        assert actor.calls[0].request.headers['Accept'] == AP_FETCH_ACCEPT
```

(d) `tests/test_podcast_episode_audio.py`: add `from app.constants import AP_FETCH_ACCEPT` to the imports and change `assert route.calls.last.request.headers['Accept'] == 'application/activity+json'` to `assert route.calls.last.request.headers['Accept'] == AP_FETCH_ACCEPT`.

(e) `tests/test_activitypub_signature.py`: add `from app.constants import AP_FETCH_ACCEPT` to the imports and change `assert headers['Accept'] == 'application/ld+json'` (`:869`) to `assert headers['Accept'] == AP_FETCH_ACCEPT`.

(f) `tests/test_ap_security_double_knock.py`: add `from app.constants import AP_FETCH_ACCEPT` to the imports. Rename `test_the_request_is_signed_and_asks_for_json_ld` to `test_the_request_is_signed_and_asks_for_activitypub`, and change `assert sent['accept'] == 'application/ld+json'` to `assert sent['accept'] == AP_FETCH_ACCEPT`.

(g) `tests/test_actor_profiles.py`: in `test_an_ld_json_accept_header_also_selects_the_activitypub_document` change

```python
    assert response.status_code == 200
    assert response.content_type == 'application/activity+json'
```

to

```python
    assert response.status_code == 200
    assert response.content_type == 'application/ld+json; profile="https://www.w3.org/ns/activitystreams"'
```

(h) `tests/test_ap_security_secure_mode.py:206`: change the `_signed_get` docstring to `"""Production's signer adds and signs its Accept header (AP_FETCH_ACCEPT), so the header is sent as signed."""`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_ap_interop_content_type.py -q`
Expected: FAIL at collection with `ImportError: cannot import name 'ap_media_type' from 'app.activitypub.routes'`.

- [ ] **Step 3: Implement**

At the end of `app/constants.py`, append:

```python
# ActivityPub media types (interop C1 item 5)
AP_MEDIA_TYPE = 'application/activity+json'
AS2_PROFILE = 'https://www.w3.org/ns/activitystreams'
AS2_LD_MEDIA_TYPE = f'application/ld+json; profile="{AS2_PROFILE}"'
AP_MEDIA_TYPES = (AP_MEDIA_TYPE, 'application/ld+json')   # compared with a mimetype, which carries no parameters
AP_FETCH_ACCEPT = f'{AP_MEDIA_TYPE}, {AS2_LD_MEDIA_TYPE}'   # every outgoing ActivityPub GET, signed or not
```

In `app/utils.py` (which already has `from app.constants import *`), replace:

```python
def is_activitypub_request():
    return 'application/ld+json' in request.headers.get('Accept', '') or 'application/activity+json' in request.headers.get('Accept', '')
```

with:

```python
def accepted_media_ranges(accept_header: str) -> list:
    """(media type, parameters, q) for each media range of an Accept header, in order. The media type and the
    parameter names are lowercased and quotes are stripped from values; an absent or unreadable q is 1, and q is
    clamped to 0..1 (RFC 9110 section 12.4.2)."""
    ranges = []
    for part in accept_header.split(','):
        pieces = [piece.strip() for piece in part.split(';')]
        media_type = pieces[0].lower()
        if not media_type:
            continue
        params, quality = {}, 1.0
        for piece in pieces[1:]:
            name, _, value = piece.partition('=')
            name, value = name.strip().lower(), value.strip().strip('"')
            if name == 'q':
                try:
                    quality = min(max(float(value), 0.0), 1.0)
                except ValueError:
                    quality = 1.0
            else:
                params[name] = value
        ranges.append((media_type, params, quality))
    return ranges


def is_activitypub_request():
    """Whether the Accept header asks for ActivityPub: it lists application/activity+json or application/ld+json with
    a q-value above 0. A wildcard does not count. (A substring test used to count q=0, which means "not this".)"""
    return any(media_type in AP_MEDIA_TYPES and quality > 0
               for media_type, _params, quality in accepted_media_ranges(request.headers.get('Accept', '')))
```

In `app/activitypub/util.py`, delete:

```python
def is_activitypub_request():
    return 'application/ld+json' in request.headers.get('Accept', '') or 'application/activity+json' in request.headers.get('Accept', '')


```

and extend the last line of the `from app.utils import ...` block from

```python
    url_is_storable, can_create_post, can_create_post_reply, retrieve_image_hash, hash_matches_blocked_image
```

to

```python
    url_is_storable, can_create_post, can_create_post_reply, retrieve_image_hash, hash_matches_blocked_image, \
    is_activitypub_request
```

In `app/activitypub/util.py`, replace `headers={'Accept': 'application/activity+json'}` with `headers={'Accept': AP_FETCH_ACCEPT}` at each of these sites (U:799, 970, 1149, 1189, 1198, 1241, 1332, 1376, 1787, 1819, 2567, 2571, 4052, 4056, 5182, 5725 at HEAD; `AP_FETCH_ACCEPT` arrives through the module's existing `from app.constants import *`). Do NOT change `headers = {'Accept': 'application/activity+json'}` at U:2639: it fetches nodeinfo, which keeps its own Accept. After the edit, `grep -n "application/activity+json" app/activitypub/util.py` must print only that U:2639 line.

In `app/activitypub/routes.py`, add directly above `def ap_content_headers(resp, obj):`:

```python
def ap_media_type() -> str:
    """The content type to serve an ActivityPub document as (interop C1 item 5).

    The AS2 JSON-LD form when the Accept header lists application/ld+json -- plain, or with the activitystreams
    profile -- at a higher q-value than application/activity+json, or without application/activity+json at all;
    else application/activity+json. The answer depends on Accept, so app/request_hooks.py adds `Vary: Accept` to
    any ActivityPub answer that lacks it."""
    ranges = accepted_media_ranges(request.headers.get('Accept', ''))
    ld = max((quality for media_type, params, quality in ranges
              if media_type == 'application/ld+json' and AS2_PROFILE in params.get('profile', AS2_PROFILE).split()),
             default=0.0)
    activity = max((quality for media_type, _params, quality in ranges if media_type == AP_MEDIA_TYPE), default=0.0)
    return AS2_LD_MEDIA_TYPE if ld > activity else AP_MEDIA_TYPE
```

and add `accepted_media_ranges` to the `from app.utils import ...` statement at R:67-73: its last line, `    requestor_domain`, becomes `    requestor_domain, accepted_media_ranges`. `AS2_PROFILE`, `AP_MEDIA_TYPE` and `AS2_LD_MEDIA_TYPE` arrive through the module's existing `from app.constants import *` (R:47).

Then, in `app/activitypub/routes.py`, replace every line

```python
                resp.content_type = 'application/activity+json'
```

(any indentation; 22 occurrences at HEAD: R:481, 547, 595, 687, 2448, 2481, 2513, 2539, 2574, 2601, 2624, 2702, 2756, 2793, 2820, 2839, 2848, 3317, 3366, 3410, 3451, 3482) with the same indentation and

```python
                resp.content_type = ap_media_type()
```

and in `quote_boost_auth` (R:3589) change

```python
    response.headers['Content-Type'] = 'application/activity+json'
```

to

```python
    response.headers['Content-Type'] = ap_media_type()
```

A mechanical way: `sed -i "s/content_type = 'application\/activity+json'/content_type = ap_media_type()/; s/headers\['Content-Type'\] = 'application\/activity+json'/headers['Content-Type'] = ap_media_type()/" app/activitypub/routes.py app/main/routes.py`.

In `community_profile` change

```python
        if 'application/ld+json' in request.headers.get('Accept', '') or 'application/activity+json' in request.headers.get('Accept', ''):
            abort(400)
```

to

```python
        if is_activitypub_request():
            abort(400)
```

and in `feed_profile` change

```python
        if 'application/ld+json' in request.headers.get('Accept',
                                                        '') or 'application/activity+json' in request.headers.get(
                'Accept', ''):
            abort(400)
```

to

```python
        if is_activitypub_request():
            abort(400)
```

In `app/main/routes.py`, change the import at R:61 from

```python
from app.activitypub.routes import replay_inbox_request, paged_outbox, AP_CACHE_COLLECTION
```

to

```python
from app.activitypub.routes import replay_inbox_request, paged_outbox, AP_CACHE_COLLECTION, ap_media_type
```

In `index`, change

```python
    if 'application/ld+json' in request.headers.get('Accept', '') or 'application/activity+json' in request.headers.get(
            'Accept', ''):
        return activitypub_application()
```

to

```python
    if is_activitypub_request():
        return activitypub_application()
```

Change

```python
@cache.cached(timeout=6)
def activitypub_application():
```

to

```python
def ap_application_cache_key() -> str:
    """activitypub_application's content type follows the Accept header (ap_media_type), so its cache entry does."""
    return f'view/activitypub_application/{ap_media_type()}'


@cache.cached(timeout=6, key_prefix=ap_application_cache_key)
def activitypub_application():
```

and apply the same `content_type` replacement as above at R:1302, 1334, 1344 and 1431 (the `sed` command covers them).

In `app/request_hooks.py`, add `AP_MEDIA_TYPES` to the constants import (R:15-17):

```python
from app.constants import POST_TYPE_LINK, POST_TYPE_IMAGE, POST_TYPE_ARTICLE, POST_TYPE_VIDEO, POST_TYPE_POLL, \
    SUBSCRIPTION_MODERATOR, SUBSCRIPTION_MEMBER, SUBSCRIPTION_OWNER, SUBSCRIPTION_PENDING, ROLE_ADMIN_NAME, VERSION, \
    POST_TYPE_EVENT, AP_MEDIA_TYPES
```

and change

```python
        # Don't set cookies for static resources or ActivityPub responses to make them cachable
        if request.path.startswith('/static/') or request.path.startswith('/bootstrap/static/') or response.content_type == 'application/activity+json':
```

to

```python
        # An ActivityPub answer's content type follows the Accept header (ap_media_type), so it varies on it
        ap_response = response.mimetype in AP_MEDIA_TYPES
        if ap_response:
            response.vary.add('Accept')
        # Don't set cookies for static resources or ActivityPub responses to make them cachable
        if request.path.startswith('/static/') or request.path.startswith('/bootstrap/static/') or ap_response:
```

In `app/activitypub/signature.py`, change R:60 `from app.constants import DATETIME_MS_FORMAT` to `from app.constants import AP_FETCH_ACCEPT, DATETIME_MS_FORMAT`, and change

```python
        # GET requests get implicit accept headers added
        if method == "get":
            headers["Accept"] = "application/ld+json"
```

to

```python
        # GET requests get implicit accept headers added. A cavage signature covers every header set here, so the
        # Accept sent is the Accept signed.
        if method == "get":
            headers["Accept"] = AP_FETCH_ACCEPT
```

In `app/activitypub/actor.py`, add `from app.constants import AP_FETCH_ACCEPT` after `from app import cache, db` (R:11); in `fetch_remote_actor_data` change `response = get_request(url, headers={'Accept': 'application/activity+json'})` to `response = get_request(url, headers={'Accept': AP_FETCH_ACCEPT})`; in `fetch_actor_from_webfinger` change `type_header = link.get('type', 'application/activity+json')` to `type_header = link.get('type', AP_FETCH_ACCEPT)`.

In `app/utils.py:4059` (PeerTube video fix-up) change `response = get_request(url, headers={'Accept': 'application/activity+json'})` to `response = get_request(url, headers={'Accept': AP_FETCH_ACCEPT})`.

In `app/post/routes.py`, add `AP_FETCH_ACCEPT` to the first constants import (R:28-29, append `, AP_FETCH_ACCEPT` after `SRC_API`) and at R:2757 change `remote_post_request = get_request(post.ap_id, headers={'Accept': 'application/activity+json'})` to `remote_post_request = get_request(post.ap_id, headers={'Accept': AP_FETCH_ACCEPT})`.

In `app/community/util.py`, change R:29 to `from app.constants import SRC_WEB, POST_TYPE_LINK, AP_FETCH_ACCEPT` and at R:100 change `type = links['type'] if 'type' in links else 'application/activity+json'` to `type = links['type'] if 'type' in links else AP_FETCH_ACCEPT`.

In `app/feed/util.py`, add `from app.constants import AP_FETCH_ACCEPT` after `from app.community.util import search_for_community, retrieve_mods_and_backfill` and at R:89 change `type = links['type'] if 'type' in links else 'application/activity+json'` to `type = links['type'] if 'type' in links else AP_FETCH_ACCEPT`.

In `app/user/utils.py`, change R:11 to `from app.constants import AP_FETCH_ACCEPT, POST_STATUS_REVIEWING` and at R:142 change `type = links['type'] if 'type' in links else 'application/activity+json'` to `type = links['type'] if 'type' in links else AP_FETCH_ACCEPT`.

In `app/discovery/credits.py`, add `from app.constants import AP_FETCH_ACCEPT` after `from app.activitypub.signature import HttpSignature` and change

```python
_ACTOR_ACCEPT = 'application/activity+json, application/ld+json; profile="https://www.w3.org/ns/activitystreams"'
```

to

```python
_ACTOR_ACCEPT = AP_FETCH_ACCEPT   # the one outgoing ActivityPub Accept (interop C1 item 5); the value is unchanged
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_ap_interop_content_type.py tests/test_activitypub_actor.py tests/test_ap_refresh_profiles.py tests/test_user_utils.py tests/test_podcast_episode_audio.py tests/test_activitypub_signature.py tests/test_ap_security_double_knock.py tests/test_actor_profiles.py tests/test_ap_security_secure_mode.py tests/test_ap_security_entitlement.py tests/test_request_hooks.py tests/test_ap_collections.py tests/test_ap_content_objects.py tests/test_main_front_page.py tests/test_main_public_files.py tests/test_upstream_v1_7_17.py -q`
Expected: PASS. Then `grep -rn "'application/activity+json'" app/ --include=*.py` must list only: the new `AP_MEDIA_TYPE` in `app/constants.py`, `app/activitypub/entitlement.py:173` (`_AP_MIMETYPES`), the nodeinfo Accept in `app/activitypub/util.py` (U:2639 at HEAD), and the two nodeinfo `HEADERS` in `app/shared/tasks/maintenance.py` (:441, :547). (Double-quoted uses -- the WebFinger link types at R:179/R:226, `signature.py`'s `content_type` defaults, the post page's `Link` header -- are not AP response types or outgoing Accepts and stay.)

- [ ] **Step 5: Commit**

```bash
git add app/constants.py app/utils.py app/activitypub/util.py app/activitypub/routes.py app/main/routes.py \
    app/request_hooks.py app/activitypub/signature.py app/activitypub/actor.py app/post/routes.py \
    app/community/util.py app/feed/util.py app/user/utils.py app/discovery/credits.py \
    tests/test_ap_interop_content_type.py tests/test_activitypub_actor.py tests/test_ap_refresh_profiles.py \
    tests/test_user_utils.py tests/test_podcast_episode_audio.py tests/test_activitypub_signature.py \
    tests/test_ap_security_double_knock.py tests/test_actor_profiles.py tests/test_ap_security_secure_mode.py
git commit -m "feat: ActivityPub documents are served as JSON-LD to a peer that asks for it

AP routes hard-coded application/activity+json. ap_media_type() now answers
application/ld+json with the activitystreams profile when the Accept header
prefers JSON-LD, and every AP route uses it; AP answers vary on Accept.
is_activitypub_request() parses q-values, so q=0 no longer counts as asking
for ActivityPub, and the three inline Accept tests use it. Outgoing AP GETs,
signed ones included, send both forms from one constant; the cavage signature
covers the new Accept.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Task gate**

Run, in order:
1. `./run_tests.sh tests/test_ap_interop_content_type.py tests/test_actor_profiles.py tests/test_ap_collections.py tests/test_ap_content_objects.py tests/test_ap_security_private_message.py tests/test_ap_security_activities.py tests/test_ap_quote_authorisation.py tests/test_ap_security_actor_outbox.py tests/test_ap_security_tombstones.py tests/test_ap_security_community_outbox.py tests/test_main_public_files.py tests/test_main_front_page.py tests/test_activitypub_actor.py tests/test_ap_refresh_profiles.py tests/test_ap_refresh_community_profile.py tests/test_user_utils.py tests/test_podcast_episode_audio.py tests/test_activitypub_signature.py tests/test_ap_security_double_knock.py tests/test_request_hooks.py tests/test_activitypub_util.py --cov=app --cov-branch --cov-report=json -q`
2. `python3 tests/check_changed_line_coverage.py coverage.json HEAD~1 --branches app/`

Expected: all pass; every added line executed. The changed lines are one-line swaps spread over ~45 call sites; if the check names one, find a module that reaches it (`grep -ln "<route path or function name>" tests/`), add that module to the command in 1, and rerun. Do not add a test only to execute a line that an existing module already reaches.

---

### Task 3: An actor's pushed Update refreshes it at once; a Group Update must describe its Group (item 2)

**Files:**
- Modify: `app/activitypub/util.py`
  - add `names_actor` and `delivery_inbox_of` directly after `shared_inbox_of` (U:731-741);
  - in `refresh_user_profile_task` (U:791), `refresh_community_profile_task` (U:962) and `refresh_feed_profile_task` (U:1233), apply `delivery_inbox_of` right after each `refreshed_pem` block (U:878-880, U:1038-1040, U:1277-1279).
- Modify: `app/activitypub/routes.py`
  - import line R:24-35 (add `names_actor, refresh_user_profile_task`);
  - new constant `ACTOR_UPDATE_TYPES` after `OUTBOX_PAGE_SIZE` (R:85);
  - preamble actor lookup (R:1040, `actor = find_actor_or_create_cached(actor_id)`);
  - Create/Update arm: new actor-Update branch before `if core_activity['object']['type'] == 'ChatMessage':` (R:1488);
  - Group Update branch (R:1550-1556);
  - new function `process_actor_update` directly before `def process_upvote` (R:3004).
- Modify: `tests/test_inbox_dispatch_create_update.py:600-639` (`test_a_group_update_of_a_REMOTE_community_refreshes_without_announcing` becomes a refusal)
- Test: `tests/test_ap_interop_actor_update.py` (new)

**Interfaces:**
- Consumes: `shared_inbox_of(actor_json: dict) -> str | None` (U:731, unchanged here; Task 4 extends it); `tests/test_inbox_dispatch_preamble.py:dispatch`; `tests/test_inbox_dispatch_create_update.py:create_activity`; `tests/test_inbox_dispatch_lock_delete.py:record_moderation`; `tests/test_ap_refresh_profiles.py:_remote_user, _remote_community, _remote_feed, _person_document, _group_document, _feed_document, PEER`.
- Produces:
  - `names_actor(candidate, actor) -> bool` in `app/activitypub/util.py`. True only when `candidate` is a string that, stripped and lowercased, equals `actor.ap_profile_id.lower()`. `actor` None or without `ap_profile_id` gives False.
  - `delivery_inbox_of(actor_json: dict) -> str | None` in `app/activitypub/util.py`: `shared_inbox_of(actor_json)`, else `actor_json['inbox']`, the first that is a non-empty string of at most `INBOX_URL_MAX` (255) characters; else None.
  - `INBOX_URL_MAX = 255` in `app/activitypub/util.py` (the width of `ap_inbox_url` on User, Community and Feed).
  - `ACTOR_UPDATE_TYPES = ('Person', 'Service', 'Application', 'Podcast')` in `app/activitypub/routes.py`.
  - `process_actor_update(user: User, actor_json: dict, id, saved_json) -> None` in `app/activitypub/routes.py`.
  - Log texts: `'Update of an unknown actor'` (ignored), `'Update of a local actor refused'`, `'Update of another actor refused'`, `'Announced actor Update refused: only the actor itself may send it'`, `'Update of another Group refused'`, `'Update of a remote Group not sent by the Group'` (all failure). Task 8 adds its gone-Group check to the same Group branch.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ap_interop_actor_update.py`:

```python
"""Interop C1 item 2 (docs/superpowers/specs/2026-10-11-ap-interop-c1-design.md): inbound actor Update.

An Update whose object is a Person, Service, Application or Podcast reached the Create/Update arm and logged
`Unacceptable type (create): Person`, so a peer's new name, bio or rotated key arrived only with the daily
refresh. It now refreshes the signer from the pushed document at once -- no fetch -- but only when the document
describes the signer itself, the signer is a known remote user, and the Update was not merely Announced.

The Group Update path gains the same rule: the document must describe the community the activity is for, and a
remote Group's document is taken only from the Group. The three refresh tasks also take the actor's inbox from
the document, keeping the stored one when the document has none.
"""
from types import SimpleNamespace

import pytest

from app import db
from app.activitypub import routes as activitypub_routes
from app.activitypub import util as activitypub_util
from app.activitypub.util import (INBOX_URL_MAX, delivery_inbox_of, names_actor,
                                  refresh_community_profile_task, refresh_feed_profile_task)
from app.models import ActivityPubLog, Community, Feed, User, utcnow
from tests.factories import inbox_activity, make_community, make_user, seed_community_owner
from tests.test_ap_refresh_profiles import (PEER, _feed_document, _group_document, _person_document,
                                            _remote_community, _remote_feed, _remote_user)
from tests.test_inbox_dispatch_create_update import create_activity
from tests.test_inbox_dispatch_lock_delete import record_moderation
from tests.test_inbox_dispatch_preamble import dispatch

OLD_KEY = '-----BEGIN PUBLIC KEY-----old'
NEW_KEY = '-----BEGIN PUBLIC KEY-----rotated'


def _known_remote_user(name='wakko'):
    """A remote user the dispatcher resolves without a fetch.

    ap_fetched_at is stamped so find_actor_or_create_cached's schedule_actor_refresh does not run a refresh
    inline (eager Celery) -- that would fetch the actor and hide what the pushed document did."""
    user = _remote_user(name)
    user.ap_fetched_at = utcnow()
    user.public_key = OLD_KEY
    user.ap_inbox_url = f'https://{PEER}/u/{name}/inbox'
    db.session.commit()
    return user


def _remote_group(name='books'):
    """A REMOTE community the dispatcher resolves without a fetch. make_community leaves ap_id None, which makes
    is_local() True whatever the host, so ap_id is set to a non-local URL."""
    community = make_community(name, host=PEER)
    community.ap_id = f'https://{PEER}/c/{name}'
    community.ap_fetched_at = utcnow()
    db.session.commit()
    return community


def _actor_update(user, document):
    return create_activity(user, document, activity_type='Update')


# --- names_actor and delivery_inbox_of ---

ACTOR = SimpleNamespace(ap_profile_id=f'https://{PEER}/u/wakko')


@pytest.mark.parametrize('candidate,expected', [
    (f'https://{PEER}/u/wakko', True),
    ('https://PEER.example/u/Wakko', True),
    (f'  https://{PEER}/u/wakko\n', True),
    (f'https://{PEER}/u/yakko', False),
    (None, False),
    (['https://peer.example/u/wakko'], False),
    ('', False),
])
def test_names_actor(candidate, expected):
    assert names_actor(candidate, ACTOR) is expected


def test_names_actor_without_an_actor_or_profile_id_is_false():
    assert names_actor('', SimpleNamespace(ap_profile_id=None)) is False
    assert names_actor(f'https://{PEER}/u/wakko', None) is False


SHARED = f'https://{PEER}/inbox'
OWN = f'https://{PEER}/u/wakko/inbox'
TOO_LONG = f'https://{PEER}/' + 'x' * INBOX_URL_MAX


@pytest.mark.parametrize('document,expected', [
    ({'endpoints': {'sharedInbox': SHARED}, 'inbox': OWN}, SHARED),
    ({'inbox': OWN}, OWN),
    ({'endpoints': {'sharedInbox': TOO_LONG}, 'inbox': OWN}, OWN),
    ({'inbox': TOO_LONG}, None),
    ({'inbox': ''}, None),
    ({'inbox': ['x']}, None),
    ({}, None),
], ids=['shared', 'own', 'shared-too-long', 'own-too-long', 'empty', 'non-string', 'none'])
def test_delivery_inbox_of(document, expected):
    assert delivery_inbox_of(document) == expected


# --- the actor Update ---

@pytest.mark.parametrize('actor_type,bot', [('Person', False), ('Service', True), ('Application', False)])
def test_an_actor_update_refreshes_the_name_summary_and_key(app, db_session, monkeypatch, actor_type, bot):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    user = _known_remote_user()
    user_id = user.id
    document = _person_document(fields={'type': actor_type, 'name': 'Wakko Warner',
                                        'summary': '<p>a new bio</p>',
                                        'publicKey': {'publicKeyPem': NEW_KEY}})

    dispatch(_actor_update(user, document))

    db.session.expire_all()
    user = db.session.get(User, user_id)
    assert user.title == 'Wakko Warner'
    assert 'a new bio' in user.about_html
    assert user.public_key == NEW_KEY
    assert user.bot is bot
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_a_podcast_update_keeps_the_podcast_community_handling(app, db_session, monkeypatch):
    """Castopod's `Podcast` actor: the refresh still calls ensure_podcast_community (U:910, D24)."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    user = _known_remote_user()
    user_id = user.id
    calls = []
    monkeypatch.setattr(activitypub_util, 'ensure_podcast_community',
                        lambda podcast_user, actor_json: calls.append((podcast_user.id, actor_json['type'])))

    dispatch(_actor_update(user, _person_document(fields={'type': 'Podcast'})))

    assert calls == [(user_id, 'Podcast')]
    assert ActivityPubLog.query.one().result == 'success'


def test_an_update_describing_another_actor_is_refused(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    user = _known_remote_user()
    user_id = user.id
    document = _person_document('yakko', fields={'name': 'Not Wakko',
                                                 'publicKey': {'publicKeyPem': NEW_KEY}})

    dispatch(_actor_update(user, document))

    db.session.expire_all()
    user = db.session.get(User, user_id)
    assert user.title != 'Not Wakko'
    assert user.public_key == OLD_KEY
    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Update of another actor refused'


def test_an_update_from_an_unknown_actor_is_dropped_and_creates_nothing(app, db_session, monkeypatch):
    """No route is registered: the old preamble's find_actor_or_create_cached(actor_id) would fetch the unknown
    actor to create it, which block_outbound_http refuses."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    seed_community_owner(PEER)
    stranger = f'https://{PEER}/u/stranger'
    users_before = User.query.count()

    dispatch({'id': f'{stranger}/activities/1', 'type': 'Update', 'actor': stranger,
              'object': _person_document('stranger')})

    assert User.query.count() == users_before
    log = ActivityPubLog.query.one()
    assert log.result == 'ignored'
    assert log.exception_message == 'Update of an unknown actor'


def test_an_update_from_a_local_actor_is_refused(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    seed_community_owner(PEER)
    local = make_user(None, 'local', local=True)
    local.ap_profile_id = f"{app.config['SERVER_URL']}/u/local".lower()
    db.session.commit()

    dispatch(_actor_update(local, {'type': 'Person', 'id': local.ap_profile_id, 'preferredUsername': 'local',
                                   'name': 'Renamed'}))

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Update of a local actor refused'


def test_an_announced_actor_update_is_refused(app, db_session, monkeypatch):
    """The Group signed the Announce, not the actor the document describes, so the signer rule cannot hold."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    user = _known_remote_user()
    user_id = user.id
    group = _remote_group()

    dispatch(inbox_activity(group, activity_type='Announce',
                            object={'id': f'https://{PEER}/activities/update-1', 'type': 'Update',
                                    'actor': user.ap_profile_id,
                                    'object': _person_document(fields={'name': 'Via Group'})}))

    db.session.expire_all()
    assert db.session.get(User, user_id).title != 'Via Group'
    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Announced actor Update refused: only the actor itself may send it'


def test_a_document_without_a_key_keeps_the_old_key(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    user = _known_remote_user()
    user_id = user.id
    document = _person_document(fields={'name': 'Keyless'})
    del document['publicKey']

    dispatch(_actor_update(user, document))

    db.session.expire_all()
    user = db.session.get(User, user_id)
    assert user.title == 'Keyless'
    assert user.public_key == OLD_KEY


@pytest.mark.parametrize('fields,expected', [
    ({'endpoints': {'sharedInbox': SHARED}, 'inbox': OWN}, SHARED),
    ({'inbox': f'https://{PEER}/u/wakko/new-inbox'}, f'https://{PEER}/u/wakko/new-inbox'),
    ({}, OWN),
], ids=['shared', 'own', 'absent-keeps-old'])
def test_an_actor_update_refreshes_the_inbox(app, db_session, monkeypatch, fields, expected):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    user = _known_remote_user()
    user_id = user.id

    dispatch(_actor_update(user, _person_document(fields=fields)))

    db.session.expire_all()
    assert db.session.get(User, user_id).ap_inbox_url == expected


# --- the refresh tasks take the inbox too ---

@pytest.mark.parametrize('fields,expected', [
    ({'endpoints': {'sharedInbox': SHARED}}, SHARED),
    ({}, 'https://peer.example/c/memes/old-inbox'),
], ids=['shared', 'absent-keeps-old'])
def test_a_community_refresh_takes_the_inbox_from_the_document(app, db_session, http_mock, fields, expected):
    community = _remote_community()
    community.ap_inbox_url = 'https://peer.example/c/memes/old-inbox'
    db.session.commit()
    community_id = community.id

    refresh_community_profile_task(community_id, _group_document(fields=fields))

    db.session.expire_all()
    assert db.session.get(Community, community_id).ap_inbox_url == expected


@pytest.mark.parametrize('fields,expected', [
    ({'inbox': 'https://peer.example/f/news/inbox'}, 'https://peer.example/f/news/inbox'),
    ({}, 'https://peer.example/f/news/old-inbox'),
], ids=['own', 'absent-keeps-old'])
def test_a_feed_refresh_takes_the_inbox_from_the_document(app, db_session, http_mock, fields, expected):
    feed = _remote_feed()
    feed.ap_inbox_url = 'https://peer.example/f/news/old-inbox'
    db.session.commit()
    feed_id = feed.id

    refresh_feed_profile_task(feed_id, _feed_document(fields=fields))

    db.session.expire_all()
    assert db.session.get(Feed, feed_id).ap_inbox_url == expected


# --- the Group Update path ---

def test_a_groups_own_update_of_itself_refreshes_without_announcing(app, db_session, monkeypatch):
    """The a.gup.pe / NodeBB shape: the Group itself sends Update{Group} (preamble R:1047-1050)."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    seed_community_owner(PEER)
    group = _remote_group()
    calls = record_moderation(monkeypatch, 'refresh_community_profile', 'announce_activity_to_followers')

    dispatch(inbox_activity(group, activity_type='Update',
                            object={'type': 'Group', 'id': group.ap_profile_id}))

    assert len(calls['refresh_community_profile']) == 1
    assert calls['announce_activity_to_followers'] == []
    assert ActivityPubLog.query.one().result == 'success'


def test_a_group_announcing_a_moderators_update_of_itself_refreshes(app, db_session, monkeypatch):
    """The Lemmy shape: the Group Announces its moderator's Update{Group}. The announcing Group is the signer."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner(PEER)
    group = _remote_group()
    moderator = make_user(instance, 'moderator')
    moderator.ap_fetched_at = utcnow()
    db.session.commit()
    calls = record_moderation(monkeypatch, 'refresh_community_profile', 'announce_activity_to_followers')

    dispatch(inbox_activity(group, activity_type='Announce',
                            object={'id': f'https://{PEER}/activities/update-group-1', 'type': 'Update',
                                    'actor': moderator.ap_profile_id,
                                    'object': {'type': 'Group', 'id': group.ap_profile_id}}))

    assert len(calls['refresh_community_profile']) == 1
    assert ActivityPubLog.query.one().result == 'success'


def test_a_group_update_describing_another_group_is_refused(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    seed_community_owner(PEER)
    group = _remote_group()
    calls = record_moderation(monkeypatch, 'refresh_community_profile')

    dispatch(inbox_activity(group, activity_type='Update',
                            object={'type': 'Group', 'id': f'https://{PEER}/c/someone_else'}))

    assert calls['refresh_community_profile'] == []
    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Update of another Group refused'


def test_a_user_signed_group_update_with_no_community_is_refused_not_crashed(app, db_session, monkeypatch):
    """find_community finds nothing, process_chat declines: `community` is None at the Group branch, which used to
    raise AttributeError on `community.is_local()`. names_actor(..., None) is False, so it is refused."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner(PEER)
    editor = make_user(instance, 'editor')
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json, author=None: None)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)
    calls = record_moderation(monkeypatch, 'refresh_community_profile', 'process_chat')

    dispatch(create_activity(editor, {'type': 'Group', 'id': f'https://{PEER}/c/books'}, activity_type='Update'))

    assert calls['refresh_community_profile'] == []
    assert ActivityPubLog.query.one().exception_message == 'Update of another Group refused'
```

In `tests/test_inbox_dispatch_create_update.py`, replace `test_a_group_update_of_a_REMOTE_community_refreshes_without_announcing` (R-test lines 600-639) with:

```python
def test_a_user_signed_group_update_of_a_REMOTE_community_is_refused(app, db_session, monkeypatch):
    """Interop C1 item 2. A remote Group's document is taken only from the Group itself -- announced by it or sent
    by it (tests/test_ap_interop_actor_update.py). A user's direct Update of a remote community used to refresh it
    from whatever document that user pushed; it is now refused before the refresh.

    The bare factory is NOT enough to make this community remote: `make_community` never sets `ap_id`, and
    `Community.is_local()` returns True whenever `ap_id is None` regardless of `host`. `ap_id` is set explicitly
    to a URL that does not start with `SERVER_URL`, which is the only thing `is_local()` checks once `ap_id` is
    not None. Without it the moderator guard would answer 'Comm edit by non-moderator' instead.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    remote_community = make_community(host='peer.example')
    remote_community.ap_id = 'https://peer.example/c/microblogs'
    db.session.commit()
    editor = make_user(instance, 'editor')
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json, author=None: remote_community)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)
    calls = record_moderation(monkeypatch, 'refresh_community_profile',
                              'announce_activity_to_followers')

    dispatch(create_activity(editor, {'type': 'Group', 'id': remote_community.ap_profile_id},
                             activity_type='Update'))

    assert calls['refresh_community_profile'] == []
    assert calls['announce_activity_to_followers'] == []
    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Update of a remote Group not sent by the Group'
```

The two LOCAL Group Update tests and `test_a_create_of_a_group_is_unacceptable_because_the_group_branch_requires_update` stay unchanged: their object id is the community's own `ap_profile_id`, and a Create of a Group still logs `Unacceptable type (create): Group`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_ap_interop_actor_update.py tests/test_inbox_dispatch_create_update.py -q`
Expected: FAIL at collection of `tests/test_ap_interop_actor_update.py` with `ImportError: cannot import name 'INBOX_URL_MAX' from 'app.activitypub.util'`. In `tests/test_inbox_dispatch_create_update.py`, `test_a_user_signed_group_update_of_a_REMOTE_community_is_refused` fails with `assert [((...), {})] == []` (the refresh still runs).

- [ ] **Step 3: Implement**

In `app/activitypub/util.py`, directly after `shared_inbox_of`, add:

```python
# The width of ap_inbox_url on User, Community and Feed. A longer URL from a peer is not stored: it would be a
# DataError at commit, which aborts the refresh task and leaves the actor unrefreshable (the D1372 family).
INBOX_URL_MAX = 255


def delivery_inbox_of(actor_json: dict) -> str | None:
    """The inbox to deliver to, as a refresh stores it: the shared inbox when `endpoints` carries a usable one, else
    the actor's own `inbox`. Each must be a non-empty string that fits the column. None when neither is usable, and
    the refresh then keeps the inbox it already holds (interop C1 item 2)."""
    for candidate in (shared_inbox_of(actor_json), actor_json.get('inbox')):
        if isinstance(candidate, str) and candidate and len(candidate) <= INBOX_URL_MAX:
            return candidate
    return None


def names_actor(candidate, actor) -> bool:
    """Whether `candidate`, an id taken from a peer's document, names `actor`.

    Compared the way actor ids are stored and looked up everywhere else (actor_json_to_model stores
    `id.lower()`, find_actor_by_url strips and lowercases): stripped and lowercased, against the lowercased
    `ap_profile_id`. A missing actor, a non-string or an actor with no `ap_profile_id` names nothing
    (interop C1 item 2)."""
    if actor is None or not isinstance(candidate, str) or not actor.ap_profile_id:
        return False
    return candidate.strip().lower() == actor.ap_profile_id.lower()
```

In `refresh_user_profile_task`, change:

```python
                    refreshed_pem = public_key_pem(activity_json)
                    if refreshed_pem:
                        user.public_key = refreshed_pem
                    user.accept_private_messages = activity_json['acceptPrivateMessages'] if 'acceptPrivateMessages' in activity_json else 3
```

to:

```python
                    refreshed_pem = public_key_pem(activity_json)
                    if refreshed_pem:
                        user.public_key = refreshed_pem
                    refreshed_inbox = delivery_inbox_of(activity_json)   # interop C1 item 2: absent keeps the old one
                    if refreshed_inbox:
                        user.ap_inbox_url = refreshed_inbox
                    user.accept_private_messages = activity_json['acceptPrivateMessages'] if 'acceptPrivateMessages' in activity_json else 3
```

In `refresh_community_profile_task`, change:

```python
                    refreshed_pem = public_key_pem(activity_json)
                    if refreshed_pem:
                        community.public_key = refreshed_pem

                    if 'postUrlType' in activity_json and activity_json['postUrlType']:
```

to:

```python
                    refreshed_pem = public_key_pem(activity_json)
                    if refreshed_pem:
                        community.public_key = refreshed_pem
                    refreshed_inbox = delivery_inbox_of(activity_json)   # interop C1 item 2: absent keeps the old one
                    if refreshed_inbox:
                        community.ap_inbox_url = refreshed_inbox

                    if 'postUrlType' in activity_json and activity_json['postUrlType']:
```

In `refresh_feed_profile_task`, change:

```python
                    refreshed_pem = public_key_pem(activity_json)  # D1355
                    if refreshed_pem:
                        feed.public_key = refreshed_pem
```

to:

```python
                    refreshed_pem = public_key_pem(activity_json)  # D1355
                    if refreshed_pem:
                        feed.public_key = refreshed_pem
                    refreshed_inbox = delivery_inbox_of(activity_json)   # interop C1 item 2: absent keeps the old one
                    if refreshed_inbox:
                        feed.ap_inbox_url = refreshed_inbox
```

In `app/activitypub/routes.py`, extend the util import's last line from:

```python
    announce_target_uri, undo_boost, LD_SIGNED_LOG_PREFIX, current_ld_signed
```

to:

```python
    announce_target_uri, undo_boost, LD_SIGNED_LOG_PREFIX, current_ld_signed, names_actor, refresh_user_profile_task
```

After `OUTBOX_PAGE_SIZE = 50`, add:

```python

# Interop C1 item 2: an Update of one of these refreshes the signer from the pushed document. 'Podcast' is
# Castopod's custom actor type, which actor_json_to_model already treats as a user (G1).
ACTOR_UPDATE_TYPES = ('Person', 'Service', 'Application', 'Podcast')
```

In `process_inbox_request`'s preamble, change:

```python
                else:
                    actor = find_actor_or_create_cached(actor_id)
                    if actor and isinstance(actor, User):
```

to:

```python
                else:
                    # Interop C1 item 2: an Update of an actor refreshes a known actor; it never creates one
                    actor_update = (request_json['type'] == 'Update' and isinstance(request_json['object'], dict)
                                    and request_json['object'].get('type') in ACTOR_UPDATE_TYPES)
                    actor = find_actor_or_create_cached(actor_id, create_if_not_found=not actor_update)
                    if actor is None and actor_update:
                        log_incoming_ap(id, APLOG_UPDATE, APLOG_IGNORED, saved_json, 'Update of an unknown actor')
                        return
                    if actor and isinstance(actor, User):
```

In the Create/Update arm, change:

```python
                        log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json,
                                        'Cannot process Create/Update: no user or community resolved')
                        return
                    if core_activity['object']['type'] == 'ChatMessage':
```

to:

```python
                        log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json,
                                        'Cannot process Create/Update: no user or community resolved')
                        return
                    if core_activity['type'] == 'Update' and core_activity['object'].get('type') in ACTOR_UPDATE_TYPES:
                        if announced:   # the Group signed the Announce, not the actor the document describes
                            log_incoming_ap(id, APLOG_UPDATE, APLOG_FAILURE, saved_json,
                                            'Announced actor Update refused: only the actor itself may send it')
                        else:
                            process_actor_update(user, core_activity['object'], id, saved_json)
                        return
                    if core_activity['object']['type'] == 'ChatMessage':
```

Change the Group branch from:

```python
                        elif object_type == 'Group' and core_activity['type'] == 'Update':  # update community/category info
                            if community.is_local() and not community.is_moderator(user):
                                log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Comm edit by non-moderator')
                            else:
```

to:

```python
                        elif object_type == 'Group' and core_activity['type'] == 'Update':  # update community/category info
                            # Interop C1 item 2: the document must describe the community this activity is for, and a
                            # remote Group's document comes only from the Group (it announced this, or sent it itself)
                            if not names_actor(core_activity['object'].get('id'), community):
                                log_incoming_ap(id, APLOG_UPDATE, APLOG_FAILURE, saved_json, 'Update of another Group refused')
                            elif community.is_local() and not community.is_moderator(user):
                                log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Comm edit by non-moderator')
                            elif not community.is_local() and not announced and user is not None:
                                log_incoming_ap(id, APLOG_UPDATE, APLOG_FAILURE, saved_json,
                                                'Update of a remote Group not sent by the Group')
                            else:
```

(the `else:` body -- `refresh_community_profile`, the success log and the local announce -- is unchanged).

Directly before `def process_upvote(user, store_ap_json, request_json, announced):`, add:

```python
def process_actor_update(user: User, actor_json: dict, id, saved_json):
    """Interop C1 item 2: apply a remote actor's pushed Update of itself at once, from the pushed document, so a
    new name, bio, inbox or rotated key takes effect now rather than at the daily refresh.

    The signed actor is the only identity: a document describing anyone else is refused, and so is one for a local
    actor. The task is called directly, not through refresh_user_profile, which outside debug defers it by up to
    ten seconds -- this already runs in the inbox worker, and the document needs no fetch."""
    if user.is_local():
        log_incoming_ap(id, APLOG_UPDATE, APLOG_FAILURE, saved_json, 'Update of a local actor refused')
        return
    if not names_actor(actor_json.get('id'), user):
        log_incoming_ap(id, APLOG_UPDATE, APLOG_FAILURE, saved_json, 'Update of another actor refused')
        return
    refresh_user_profile_task(user.id, actor_json)
    log_incoming_ap(id, APLOG_UPDATE, APLOG_SUCCESS, saved_json)


```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_ap_interop_actor_update.py tests/test_inbox_dispatch_create_update.py tests/test_ap_refresh_profiles.py tests/test_ap_refresh_community_profile.py tests/test_inbox_dispatch_announce.py tests/test_inbox_dispatch_preamble.py -q`
Expected: PASS. The existing refresh tests stay green unchanged: their documents carry no `inbox` or `endpoints`, so `delivery_inbox_of` returns None and the stored inbox is kept.

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/util.py app/activitypub/routes.py tests/test_ap_interop_actor_update.py tests/test_inbox_dispatch_create_update.py
git commit -m "feat: a remote actor's Update of itself refreshes its profile, key and inbox at once

An Update of a Person, Service, Application or Podcast logged
'Unacceptable type (create)', so a rotated key or new inbox arrived only
with the daily refresh. It now applies the pushed document inline when
it describes the signer, a known remote user; anything else is refused
and logged, and an unknown signer is not created. A Group Update must
describe the community it is for, and a remote Group's document is
taken only from the Group. The three refresh tasks also store the
document's inbox, keeping the old one when it has none.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Task gate**

Run, in order:
1. `./run_tests.sh tests/test_ap_interop_actor_update.py tests/test_inbox_dispatch_create_update.py tests/test_ap_refresh_profiles.py tests/test_ap_refresh_community_profile.py tests/test_inbox_dispatch_announce.py tests/test_inbox_dispatch_preamble.py --cov=app --cov-branch --cov-report=json -q`
2. `python3 tests/check_changed_line_coverage.py coverage.json HEAD~1 --branches app/`

Expected: PASS; 100% of changed lines and branches covered.

---

### Task 4: A string `endpoints` is fetched on the actor's own host (item 4)

**Files:**
- Modify: `app/activitypub/util.py:731-741` (`shared_inbox_of`; two private helpers added directly after it)
- Modify: `tests/test_ap_security_shared_inbox.py:18-24` (the `'string endpoints'` row of `SHAPES` becomes a cross-host string, which is still not fetched)
- Test: `tests/test_ap_interop_string_endpoints.py` (new)

**Interfaces:**
- Consumes: `fetch_object_capped(uri: str, retry_delay: float = 0) -> tuple[dict | None, str | None]` (U:5162; unsigned, then signed on 401, capped at `OBJECT_MAX_BYTES`, no retry when `retry_delay` is 0); `host_of(url_string: str) -> str` (U:683); `delivery_inbox_of` (Task 3) and the three actor-creation sites (U:1481, U:1613, U:1891), which all call `shared_inbox_of` and need no change.
- Produces: `shared_inbox_of(actor_json: dict) -> str | None`, same signature. A string `endpoints` on the host of `actor_json['id']` is fetched once; its `sharedInbox` is used when it is a non-empty string on that same host. Private helpers `_usable_shared_inbox(endpoints: dict) -> str | None` and `_shared_inbox_from_endpoints_document(endpoints_url: str, actor_id) -> str | None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ap_interop_string_endpoints.py`:

```python
"""Interop C1 item 4 (docs/superpowers/specs/2026-10-11-ap-interop-c1-design.md): a string `endpoints`.

ActivityPub lets `endpoints` be a URI naming a document instead of an inline object. shared_inbox_of returned
None for it, so such an actor was always delivered to at its own inbox. It now fetches the document -- only on
the actor's own host, capped, once, never retried -- and uses its `sharedInbox` only when that is on the actor's
host too. Any failure is None, and every caller falls back to the actor's own inbox exactly as before.

No route is registered for the cases that must NOT fetch: block_outbound_http (tests/conftest.py) refuses any
unmocked request, so a fetch there fails the test.
"""
import httpx
import pytest

from app import db
from app.activitypub.util import actor_json_to_model, refresh_user_profile_task, shared_inbox_of
from app.models import User
from tests.factories import peer_actor_json, peer_instance
from tests.test_ap_refresh_profiles import _person_document, _remote_user

PEER = 'peer.example'
ACTOR = f'https://{PEER}/u/alice'
OWN_INBOX = f'{ACTOR}/inbox'
ENDPOINTS = f'{ACTOR}/endpoints'
SHARED = f'https://{PEER}/inbox'


def _actor(endpoints=ENDPOINTS, actor_id=ACTOR):
    return {'id': actor_id, 'type': 'Person', 'inbox': OWN_INBOX, 'endpoints': endpoints}


def test_a_same_host_endpoints_document_is_fetched_and_its_shared_inbox_used(app, http_mock):
    route = http_mock.get(ENDPOINTS).respond(200, json={'id': ENDPOINTS, 'sharedInbox': SHARED})

    assert shared_inbox_of(_actor()) == SHARED
    assert route.call_count == 1


def test_a_shared_inbox_whose_host_differs_only_by_case_is_used(app, http_mock):
    http_mock.get(ENDPOINTS).respond(200, json={'sharedInbox': 'https://PEER.Example/inbox'})

    assert shared_inbox_of(_actor()) == 'https://PEER.Example/inbox'


@pytest.mark.parametrize('endpoints', [
    'https://elsewhere.example/endpoints',
    f'https://{PEER}.evil.example/endpoints',
    'not a url',
], ids=['other-host', 'lookalike-host', 'no-host'])
def test_an_endpoints_string_off_the_actors_host_is_not_fetched(app, endpoints):
    assert shared_inbox_of(_actor(endpoints)) is None


def test_an_actor_without_a_usable_id_never_fetches(app):
    assert shared_inbox_of({'endpoints': ENDPOINTS}) is None
    assert shared_inbox_of(_actor(actor_id=['x'])) is None


def test_a_cross_host_shared_inbox_in_the_fetched_document_is_ignored(app, http_mock):
    route = http_mock.get(ENDPOINTS).respond(200, json={'sharedInbox': 'https://elsewhere.example/inbox'})

    assert shared_inbox_of(_actor()) is None
    assert route.call_count == 1


@pytest.mark.parametrize('document', [
    {},
    {'sharedInbox': ''},
    {'sharedInbox': ['https://peer.example/inbox']},
], ids=['no-key', 'empty', 'non-string'])
def test_a_fetched_document_without_a_usable_shared_inbox_gives_none(app, http_mock, document):
    http_mock.get(ENDPOINTS).respond(200, json=document)

    assert shared_inbox_of(_actor()) is None


def test_a_non_dict_result_falls_back(app, http_mock):
    http_mock.get(ENDPOINTS).respond(200, json=[SHARED])

    assert shared_inbox_of(_actor()) is None


@pytest.mark.parametrize('status', [404, 500])
def test_a_failed_fetch_falls_back(app, http_mock, status):
    http_mock.get(ENDPOINTS).respond(status)

    assert shared_inbox_of(_actor()) is None


def test_a_transport_error_is_not_retried(app, http_mock):
    route = http_mock.get(ENDPOINTS).mock(side_effect=httpx.ConnectError('refused'))

    assert shared_inbox_of(_actor()) is None
    assert route.call_count == 1


def test_a_person_with_string_endpoints_is_created_with_the_fetched_shared_inbox(app, db_session, http_mock):
    peer_instance(PEER)
    http_mock.get(ENDPOINTS).respond(200, json={'sharedInbox': SHARED})
    document = peer_actor_json(name='alice', server=PEER, fields={'endpoints': ENDPOINTS, 'inbox': OWN_INBOX})

    user = actor_json_to_model(document, 'alice', PEER)

    assert user.ap_inbox_url == SHARED


def test_a_person_whose_endpoints_fetch_fails_gets_its_own_inbox(app, db_session, http_mock):
    peer_instance(PEER)
    http_mock.get(ENDPOINTS).respond(404)
    document = peer_actor_json(name='alice', server=PEER, fields={'endpoints': ENDPOINTS, 'inbox': OWN_INBOX})

    user = actor_json_to_model(document, 'alice', PEER)

    assert user.ap_inbox_url == OWN_INBOX


def test_a_refresh_uses_the_fetched_shared_inbox(app, db_session, http_mock):
    """Task 3's delivery_inbox_of goes through shared_inbox_of, so the refresh tasks get the fetch too."""
    user = _remote_user('wakko')
    user.ap_inbox_url = f'https://{PEER}/u/wakko/inbox'
    db.session.commit()
    user_id = user.id
    endpoints = f'https://{PEER}/u/wakko/endpoints'
    http_mock.get(endpoints).respond(200, json={'sharedInbox': SHARED})

    refresh_user_profile_task(user_id, _person_document(fields={'endpoints': endpoints}))

    db.session.expire_all()
    assert db.session.get(User, user_id).ap_inbox_url == SHARED
```

In `tests/test_ap_security_shared_inbox.py`, change the `SHAPES` row

```python
    ('string endpoints', f'https://{PEER}/endpoints', None),
```

to

```python
    # Interop C1 item 4: a string on the actor's own host is now fetched (tests/test_ap_interop_string_endpoints.py).
    # One on another host is still never fetched, so this row keeps its expectation without an HTTP route.
    ('cross-host string endpoints', 'https://elsewhere.example/endpoints', None),
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_ap_interop_string_endpoints.py -q`
Expected: FAIL. `test_a_same_host_endpoints_document_is_fetched_and_its_shared_inbox_used` with `assert None == 'https://peer.example/inbox'`; the same for the case-only host test, the creation test (`assert 'https://peer.example/u/alice/inbox' == 'https://peer.example/inbox'`) and the refresh test. Every test that registers a route but never fetches also errors at teardown with `RESPX: some routes were not called!`. The not-fetched, no-id and fallback tests already pass; they guard the new code.

- [ ] **Step 3: Implement**

In `app/activitypub/util.py`, replace `shared_inbox_of`:

```python
def shared_inbox_of(actor_json: dict) -> str | None:
    """The actor's shared inbox, or None when `endpoints` does not carry a usable one.

    The spec lets `endpoints` omit sharedInbox or be a URI string naming a document to fetch. Neither is an
    error: the caller falls back to the actor's own inbox. A string `endpoints` is not dereferenced (roadmap
    tier C)."""
    endpoints = actor_json.get('endpoints')
    if not isinstance(endpoints, dict):
        return None
    shared_inbox = endpoints.get('sharedInbox')
    return shared_inbox if isinstance(shared_inbox, str) and shared_inbox else None
```

with:

```python
def shared_inbox_of(actor_json: dict) -> str | None:
    """The actor's shared inbox, or None when `endpoints` does not carry a usable one.

    The spec lets `endpoints` omit sharedInbox or be a URI string naming a document to fetch. Neither is an
    error: the caller falls back to the actor's own inbox. A string `endpoints` is fetched only on the actor's own
    host, and its sharedInbox is used only on that host too (interop C1 item 4)."""
    endpoints = actor_json.get('endpoints')
    if isinstance(endpoints, str):
        return _shared_inbox_from_endpoints_document(endpoints, actor_json.get('id'))
    if not isinstance(endpoints, dict):
        return None
    return _usable_shared_inbox(endpoints)


def _usable_shared_inbox(endpoints: dict) -> str | None:
    shared_inbox = endpoints.get('sharedInbox')
    return shared_inbox if isinstance(shared_inbox, str) and shared_inbox else None


def _shared_inbox_from_endpoints_document(endpoints_url: str, actor_id) -> str | None:
    """The sharedInbox of the endpoints document at `endpoints_url`, fetched once with fetch_object_capped (capped,
    no retry, no sleep). The URL and the sharedInbox it names must both be on the actor's own host, so a peer
    cannot make us fetch from, or deliver to, a host it does not speak for. An actor id with no host matches
    nothing, rather than '' matching ''."""
    actor_host = host_of(actor_id) if isinstance(actor_id, str) else ''
    if not actor_host or host_of(endpoints_url) != actor_host:
        return None
    document, _reason = fetch_object_capped(endpoints_url)
    if document is None:
        return None
    shared_inbox = _usable_shared_inbox(document)
    if shared_inbox is None or host_of(shared_inbox) != actor_host:
        return None
    return shared_inbox
```

`fetch_object_capped` is defined later in the same module (U:5162); the call is resolved at run time, so no import is needed. It returns a dict or None, never another type, so "the result is a dict" is its own guarantee (a JSON array or scalar body gives `(None, 'the fetched object is not a JSON object')`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_ap_interop_string_endpoints.py tests/test_ap_security_shared_inbox.py tests/test_ap_interop_actor_update.py tests/test_ap_actor_json_person.py tests/test_ap_actor_json_group.py tests/test_ap_actor_json_feed.py tests/test_activitypub_util.py -q`
Expected: PASS. The `TestInboxResolution` classes in the three `test_ap_actor_json_*` modules use dict `endpoints` only and stay green unchanged.

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/util.py tests/test_ap_interop_string_endpoints.py tests/test_ap_security_shared_inbox.py
git commit -m "feat: an actor whose endpoints is a URI gets the shared inbox that document names

shared_inbox_of gave up on a string endpoints, so those actors were
always delivered to one by one. It now fetches the document once,
capped and without retry, only when it is on the actor's own host, and
uses its sharedInbox only when that is on the same host. Any failure
keeps the old fallback to the actor's own inbox. Actor creation and
the refresh tasks both go through it.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Task gate**

Run, in order:
1. `./run_tests.sh tests/test_ap_interop_string_endpoints.py tests/test_ap_security_shared_inbox.py tests/test_ap_interop_actor_update.py tests/test_ap_actor_json_person.py tests/test_ap_actor_json_group.py tests/test_ap_actor_json_feed.py tests/test_activitypub_util.py --cov=app --cov-branch --cov-report=json -q`
2. `python3 tests/check_changed_line_coverage.py coverage.json HEAD~1 --branches app/`

Expected: PASS; 100% of changed lines and branches covered.

---

### Task 5: Undo of an EmojiReact takes the reaction back, and every vote undo rebuilds the reaction cache (item 3, EmojiReact)

**Files:**
- Modify: `app/activitypub/util.py:4449-4482` (`undo_vote`)
- Modify: `app/activitypub/routes.py:2123-2134` (the Undo arm's `Like`/`Dislike` sub-arm)
- Modify: `tests/test_inbox_dispatch_undo_content.py:300`, `:328-329`, `:346-347` (three `undo_vote` doubles gain an `emoji=None` keyword)
- Modify: `tests/test_visibility_relays.py:103` (one `undo_vote` double gains an `emoji=None` keyword)
- Test: `tests/test_ap_interop_undo_emoji_react.py` (new)

**Interfaces:**
- Consumes: `tests/test_inbox_dispatch_preamble.py:dispatch`, `tests/test_inbox_dispatch_undo_content.py:undo_activity(actor, inner_type, inner_object, **inner)`, `tests/test_inbox_dispatch_lock_delete.py:record_moderation`, `Post.update_reaction_cache()` (`app/models.py:3941`), `PostReply.update_reaction_cache()` (`app/models.py:4588`).
- Produces: `undo_vote(comment, post, target_ap_id, user, emoji: str | None = None) -> Post | PostReply | None` in `app/activitypub/util.py`. With `emoji` given, the user's vote is undone only when its stored `emoji` equals it. Whenever a vote that carried an emoji is deleted, the target's `emoji_reactions` cache is rebuilt.

How an EmojiReact is stored today: the inbound arm `if core_activity['type'] == 'Like' or core_activity['type'] == 'EmojiReact'` (R:1635) calls `process_upvote`, which calls `liked.vote(user, 'upvote', emoji)` with `emoji` taken from the activity's `content` (R:3009-3011). That is one `PostVote` / `PostReplyVote` row per user and target, with the emoji in its `emoji` column, and `Post.vote` rebuilds the cache with `update_reaction_cache()`. So an Undo of an EmojiReact is an undo of that vote row.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ap_interop_undo_emoji_react.py`:

```python
"""Interop C1, item 3 (EmojiReact): an Undo of an EmojiReact takes the reaction back,
and every undo of a vote rebuilds the emoji_reactions cache.

Before this change the Undo dispatch had no EmojiReact arm: the activity fell through
to 'Unmatched activity' and the reaction stayed. And `undo_vote` deleted the vote row
without rebuilding `Post.emoji_reactions` / `PostReply.emoji_reactions`, so an Undo
Like of a vote that carried an emoji (a Like with `content`, which `process_upvote`
stores as the vote's emoji) left the reaction on the page.

An inbound EmojiReact is a vote row with an emoji (`process_upvote` ->
`Post.vote(user, 'upvote', emoji)`), one per user and target. Its Undo is therefore
`undo_vote` on that row, and only when the row still carries the emoji being undone:
a user who has since switched to another reaction keeps the new one.
"""
from types import SimpleNamespace

import pytest

from app import db
from app.models import ActivityPubLog, Post, PostReply, PostReplyVote, PostVote
from tests.factories import (make_community, make_post, make_post_reply, make_post_reply_vote,
                             make_post_vote, resolvable_remote_author, seed_community_owner)
from tests.test_inbox_dispatch_lock_delete import record_moderation
from tests.test_inbox_dispatch_preamble import dispatch
from tests.test_inbox_dispatch_undo_content import undo_activity

PEER = 'peer.example'


@pytest.fixture
def scene(app, db_session, monkeypatch):
    """A remote community with a post and a reply, and two remote voters.

    The voters come from `resolvable_remote_author`, so the dispatcher's actor lookup is a
    pure database read. `announce_activity_to_followers` is recorded rather than run: the
    Undo arm relays an undone vote, and the relay is not what these tests are about."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    record_moderation(monkeypatch, 'announce_activity_to_followers')
    instance = seed_community_owner(PEER)
    author = resolvable_remote_author(instance, 'author')
    voter = resolvable_remote_author(instance, 'voter')
    other = resolvable_remote_author(instance, 'other')
    community = make_community(host=PEER)
    post = make_post(community, author, f'https://{PEER}/post/1')
    reply = make_post_reply(post, author)
    reply.ap_id = f'https://{PEER}/comment/1'
    db.session.commit()
    return SimpleNamespace(voter=voter, other=other, post=post, reply=reply)


def react(user, target, emoji):
    """The state `Post.vote(user, 'upvote', emoji)` leaves: one upvote row carrying the
    emoji, the counters moved, and the cache rebuilt."""
    if isinstance(target, Post):
        vote = make_post_vote(user, target, 1.0)
    else:
        vote = make_post_reply_vote(user, target, 1.0)
    vote.emoji = emoji
    target.up_votes += 1
    target.score += 1
    db.session.commit()
    target.update_reaction_cache()
    db.session.commit()


def tokens(model, target_id):
    db.session.expire_all()
    reactions = db.session.get(model, target_id).emoji_reactions
    return None if reactions is None else [reaction['token'] for reaction in reactions]


def test_an_undo_of_an_emoji_react_on_a_post_takes_the_reaction_back(scene):
    react(scene.voter, scene.post, '🎉')
    react(scene.other, scene.post, '👍')

    dispatch(undo_activity(scene.voter, 'EmojiReact', scene.post.ap_id, content='🎉'))

    assert PostVote.query.filter_by(user_id=scene.voter.id, post_id=scene.post.id).count() == 0
    assert tokens(Post, scene.post.id) == ['👍']
    assert db.session.get(Post, scene.post.id).up_votes == 1
    log = ActivityPubLog.query.one()
    assert (log.activity_type, log.result) == ('Undo Vote', 'success')


def test_an_undo_of_an_emoji_react_on_a_reply_takes_the_reaction_back(scene):
    react(scene.voter, scene.reply, '🎉')

    dispatch(undo_activity(scene.voter, 'EmojiReact', scene.reply.ap_id, content='🎉'))

    assert PostReplyVote.query.filter_by(user_id=scene.voter.id, post_reply_id=scene.reply.id).count() == 0
    assert tokens(PostReply, scene.reply.id) == []


def test_an_undo_naming_another_emoji_keeps_the_current_reaction(scene):
    """The voter has switched from 🎉 to 👍 since; the late Undo of the 🎉 must not take
    the 👍 away."""
    react(scene.voter, scene.post, '👍')

    dispatch(undo_activity(scene.voter, 'EmojiReact', scene.post.ap_id, content='🎉'))

    assert PostVote.query.filter_by(user_id=scene.voter.id, post_id=scene.post.id).count() == 1
    assert tokens(Post, scene.post.id) == ['👍']


def test_an_undo_of_an_emoji_react_without_content_takes_the_vote_back(scene):
    """No emoji named, nothing to compare: the user's vote on the target is the reaction."""
    react(scene.voter, scene.post, '🎉')

    dispatch(undo_activity(scene.voter, 'EmojiReact', scene.post.ap_id))

    assert PostVote.query.filter_by(user_id=scene.voter.id, post_id=scene.post.id).count() == 0
    assert tokens(Post, scene.post.id) == []


def test_an_undo_like_of_a_vote_with_an_emoji_rebuilds_the_cache(scene):
    """The stale-cache defect the spec names for Undo Like: the row went, the 🎉 stayed."""
    react(scene.voter, scene.post, '🎉')
    react(scene.other, scene.post, '👍')

    dispatch(undo_activity(scene.voter, 'Like', scene.post.ap_id))

    assert tokens(Post, scene.post.id) == ['👍']


def test_an_undo_like_of_a_vote_with_an_emoji_on_a_reply_rebuilds_the_cache(scene):
    react(scene.voter, scene.reply, '🎉')

    dispatch(undo_activity(scene.voter, 'Like', scene.reply.ap_id))

    assert tokens(PostReply, scene.reply.id) == []


def test_an_undo_like_of_a_plain_vote_leaves_the_cache_alone(scene):
    """A vote with no emoji never touched the cache, so its undo does not write one: a post
    nobody reacted to keeps `emoji_reactions` None rather than gaining []."""
    make_post_vote(scene.voter, scene.post, 1.0)

    dispatch(undo_activity(scene.voter, 'Like', scene.post.ap_id))

    assert PostVote.query.filter_by(user_id=scene.voter.id, post_id=scene.post.id).count() == 0
    assert tokens(Post, scene.post.id) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_ap_interop_undo_emoji_react.py -q`
Expected: FAIL.
- `test_an_undo_of_an_emoji_react_on_a_post_takes_the_reaction_back`, `..._on_a_reply_...` and `..._without_content_...`: `assert 1 == 0` on the vote count (the Undo logs 'Unmatched activity' and nothing changes).
- `test_an_undo_like_of_a_vote_with_an_emoji_rebuilds_the_cache`: `assert ['🎉', '👍'] == ['👍']` (order may vary; the 🎉 is still there). The reply twin: `assert ['🎉'] == []`.
- `test_an_undo_naming_another_emoji_keeps_the_current_reaction` and `test_an_undo_like_of_a_plain_vote_leaves_the_cache_alone` pass already. They guard the emoji match and the "no emoji, no cache write" gate added below.

- [ ] **Step 3: Implement**

In `app/activitypub/util.py`, replace the whole of `undo_vote` (U:4449-4482: from `def undo_vote(comment, post, target_ap_id, user):` down to and including its final `return None`, just above `def undo_boost`) with:

```python
def undo_vote(comment, post, target_ap_id, user, emoji: str | None = None):
    """Take back `user`'s vote on the post or comment at `target_ap_id`, and return that post or comment.

    Returns None when `target_ap_id` names neither. A target with no vote from this user still returns the
    target: a repeated Undo is a successful no-op. `comment` and `post` are unused and kept for the callers.

    `emoji` is set for an Undo of an EmojiReact. An EmojiReact is stored as the user's one vote row with the
    emoji in its `emoji` column (process_upvote), so the row is taken back only while it still carries that
    emoji: a user who has switched to another reaction since keeps the new one.

    A deleted vote that carried an emoji was counted in the target's `emoji_reactions` cache, so the cache is
    rebuilt (interop C1 item 3). It used to be left stale, which showed an undone reaction forever."""
    voted_on = find_liked_object(target_ap_id)
    if isinstance(voted_on, Post):
        existing_vote = PostVote.query.filter_by(user_id=user.id, post_id=voted_on.id).first()
    elif isinstance(voted_on, PostReply):
        existing_vote = PostReplyVote.query.filter_by(user_id=user.id, post_reply_id=voted_on.id).first()
    else:
        return None
    if existing_vote and (emoji is None or existing_vote.emoji == emoji):
        had_emoji = existing_vote.emoji is not None
        with db.session.begin_nested():
            db.session.execute(text('UPDATE "user" SET reputation = reputation - :effect WHERE id = :user_id'),
                               {'effect': existing_vote.effect, 'user_id': voted_on.user_id})
        if existing_vote.effect < 0:  # Lemmy sends 'like' for upvote and 'dislike' for down votes. Cool! When it undoes an upvote it sends an 'Undo Like'. Fine. When it undoes a downvote it sends an 'Undo Like' - not 'Undo Dislike'?!
            voted_on.down_votes -= 1
        else:
            voted_on.up_votes -= 1
        voted_on.score -= existing_vote.effect
        db.session.delete(existing_vote)
        db.session.commit()
        if had_emoji:
            voted_on.update_reaction_cache()
            db.session.commit()
    return voted_on
```

(The Post and PostReply arms were line-for-line twins. `Post` and `PostReply` both carry `user_id`, `up_votes`, `down_votes`, `score` and `update_reaction_cache()`, so the arms are merged and the Lemmy comment is kept.)

In `app/activitypub/routes.py`, in the Undo arm, replace:

```python
                    if core_activity['object']['type'] == 'Like' or core_activity['object']['type'] == 'Dislike':  # Undoing an upvote or downvote
                        post = comment = None
                        target_ap_id = core_activity['object']['object']
                        post_or_comment = undo_vote(comment, post, target_ap_id, user)
```

with:

```python
                    if core_activity['object']['type'] in ('Like', 'Dislike', 'EmojiReact'):  # Undoing an upvote, downvote or reaction
                        post = comment = None
                        target_ap_id = core_activity['object']['object']
                        # An EmojiReact is stored as the user's vote carrying its emoji (process_upvote); its Undo
                        # takes the vote back only while it still carries that emoji (interop C1 item 3)
                        emoji = core_activity['object'].get('content') if core_activity['object']['type'] == 'EmojiReact' else None
                        post_or_comment = undo_vote(comment, post, target_ap_id, user, emoji=emoji)
```

Leave the rest of the sub-arm (`if post_or_comment:` ... `return`) unchanged.

Update the four `undo_vote` doubles so they accept the new keyword:

- `tests/test_inbox_dispatch_undo_content.py:300`: `def fake_undo_vote(comment, post_, target_ap_id, user):` becomes `def fake_undo_vote(comment, post_, target_ap_id, user, emoji=None):`
- `tests/test_inbox_dispatch_undo_content.py:328-329`: `lambda comment, post_, target_ap_id, user: post)` becomes `lambda comment, post_, target_ap_id, user, emoji=None: post)`
- `tests/test_inbox_dispatch_undo_content.py:346-347`: `lambda comment, post_, target_ap_id, user: None)` becomes `lambda comment, post_, target_ap_id, user, emoji=None: None)`
- `tests/test_visibility_relays.py:103`: `lambda comment, post_, target_ap_id, user: post)` becomes `lambda comment, post_, target_ap_id, user, emoji=None: post)`

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_ap_interop_undo_emoji_react.py tests/test_ap_reports_and_undo_votes.py tests/test_inbox_dispatch_undo_content.py tests/test_visibility_relays.py tests/test_inbox_dispatch_votes.py -q`
Expected: PASS. `tests/test_ap_reports_and_undo_votes.py` (`TestUndoingAVoteOnAPost`, `TestUndoingAVoteOnAReply`, `TestUndoingAVoteOnSomethingElse`) pins the counter, reputation and return-value behaviour of the merged arms and must stay green unchanged.

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/util.py app/activitypub/routes.py tests/test_ap_interop_undo_emoji_react.py \
        tests/test_inbox_dispatch_undo_content.py tests/test_visibility_relays.py
git commit -m "fix: an Undo of an EmojiReact takes the reaction back and the reaction cache follows every undone vote

The Undo dispatch had no EmojiReact arm, so a peer withdrawing a
reaction was logged as 'Unmatched activity' and the reaction stayed.
It now takes the user's vote back, but only while the vote still
carries the emoji being undone. undo_vote also rebuilds the
emoji_reactions cache when the deleted vote carried an emoji; it used
to leave the cache stale, which kept an undone Like-with-emoji on the
page too.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Task gate**

Run, in order:
1. `./run_tests.sh tests/test_ap_interop_undo_emoji_react.py tests/test_ap_reports_and_undo_votes.py tests/test_inbox_dispatch_undo_content.py tests/test_visibility_relays.py tests/test_inbox_dispatch_votes.py --cov=app --cov-branch --cov-report=json -q`
2. `python3 tests/check_changed_line_coverage.py coverage.json HEAD~1 --branches app/`

Expected: all pass; every added line and branch in `app/` is covered (exit 0).

---

### Task 6: Undo of a Flag withdraws a report nobody has acted on yet (item 3, Flag)

**Files:**
- Create: `migrations/versions/e2a7c4f9d1b3_report_ap_id.py`
- Modify: `app/models.py:4949-4965` (`Report`: add `ap_id`)
- Modify: `app/activitypub/util.py` (`process_report`, U:4505 onward: store the Flag id on all three `Report(...)` constructions; add `REPORT_SUSPECT`, `withdraw_report` and `withdraw_flag` directly after `process_report`)
- Modify: `app/activitypub/routes.py:24-35` (import `withdraw_flag`) and the Undo arm, directly above `log_incoming_ap(id, APLOG_MONITOR, APLOG_PROCESSING, request_json, 'Unmatched activity')` (R:2254)
- Test: `tests/test_ap_interop_undo_flag.py` (new)
- Test: `tests/test_migration_report_ap_id.py` (new)

**Interfaces:**
- Consumes: `tests/test_inbox_dispatch_preamble.py:dispatch`; `process_report(user, reported, request_json, session) -> bool` and `find_reported_object(ap_id)` in `app/activitypub/util.py`; constants `REPORT_STATE_NEW` (0), `REPORT_STATE_ESCALATED` (1), `REPORT_STATE_APPEALED` (2), `REPORT_STATE_RESOLVED` (3), `REPORT_STATE_DISCARDED` (-1), `REPORT_TYPE_USER/POST/REPLY`, `NOTIF_REPORT`, `APLOG_REPORT` from `app/constants.py`.
- Produces:
  - `Report.ap_id`: `db.Column(db.String(255), index=True)`, nullable. Index `ix_report_ap_id`. Migration revision `e2a7c4f9d1b3`, `down_revision = 'd9f3b7a15c2e'`. **Task 10's index migration (if any) must use `down_revision = 'e2a7c4f9d1b3'`.**
  - `REPORT_SUSPECT: dict[type, tuple[str, int]]` in `app/activitypub/util.py`: `{Post: ('suspect_post_id', REPORT_TYPE_POST), PostReply: ('suspect_post_reply_id', REPORT_TYPE_REPLY), User: ('suspect_user_id', REPORT_TYPE_USER)}`.
  - `withdraw_report(report: Report, session) -> None` in `app/activitypub/util.py`: deletes the report, takes one off its suspect's `reports` counter when that is above 0, deletes the unread `NOTIF_REPORT` notifications the report raised, and recounts each recipient's `unread_notifications`. Commits.
  - `withdraw_flag(user: User, flag: dict, session) -> tuple[tuple, str | None]` in `app/activitypub/util.py`: returns `(aplog_result, message)` for the caller to log with `APLOG_REPORT`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_migration_report_ap_id.py`:

```python
"""Interop C1 item 3 migration e2a7c4f9d1b3: Report.ap_id, its index, and that it reverses cleanly."""
import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text

from app import db
from app.models import Report

MIGRATION = Path(__file__).resolve().parent.parent / 'migrations' / 'versions' / 'e2a7c4f9d1b3_report_ap_id.py'


def load_migration():
    spec = importlib.util.spec_from_file_location('report_ap_id_migration', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def column(connection):
    return next((c for c in inspect(connection).get_columns('report') if c['name'] == 'ap_id'), None)


def index(connection):
    return next((i for i in inspect(connection).get_indexes('report') if i['name'] == 'ix_report_ap_id'), None)


def test_the_revision_chain():
    migration = load_migration()
    assert (migration.revision, migration.down_revision) == ('e2a7c4f9d1b3', 'd9f3b7a15c2e')


def test_a_nullable_indexed_column(app, db_session):
    with db.engine.connect() as connection:
        found = column(connection)
        found_index = index(connection)
    assert found is not None and found['nullable'] is True
    assert found['type'].length == 255
    assert found_index is not None and found_index['column_names'] == ['ap_id']


def test_downgrade_then_upgrade_restores_the_column_and_index(app, db_session):
    report = Report(reasons='spam', description='', type=0)
    db.session.add(report)
    db.session.commit()
    report_id = report.id
    db.session.close()
    migration = load_migration()
    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
                assert column(connection) is None
                assert index(connection) is None
                migration.upgrade()
            assert column(connection) is not None
            assert index(connection) is not None
            assert connection.execute(text('SELECT ap_id FROM report WHERE id = :id'),
                                      {'id': report_id}).scalar() is None
        finally:
            transaction.rollback()
```

Create `tests/test_ap_interop_undo_flag.py`:

```python
"""Interop C1, item 3 (Flag): an Undo of a Flag withdraws the report while it is still open.

Before this change the Undo dispatch had no Flag arm, so a peer withdrawing a report was
logged as 'Unmatched activity' and the report stayed in the moderators' queue.

Owner ruling (2026-10-11): withdraw the report if it is still open (status 0, new). If
moderators already acted on it -- escalated, appealed, resolved or discarded -- keep it
and log the Undo. Only the reporter may undo: the signed actor must be the report's
reporter.

The report is found by `Report.ap_id`, the Flag's id, which `process_report` now stores.
A report filed before that column existed has no ap_id, so failing that the newest report
with the same reporter and suspect target is taken.

Withdrawing corrects what the report raised: one off the suspect's `reports` counter, and
the unread report notifications it sent, with each recipient's unread count recounted.
"""
import uuid
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.activitypub import routes as activitypub_routes
from app.activitypub.util import process_report
from app.constants import (REPORT_STATE_APPEALED, REPORT_STATE_DISCARDED, REPORT_STATE_ESCALATED,
                           REPORT_STATE_RESOLVED)
from app.models import ActivityPubLog, Notification, Post, PostReply, Report, Site, User, utcnow
from tests.factories import (grant_permission, make_community, make_community_member, make_instance,
                             make_post, make_post_reply, make_user)
from tests.test_inbox_dispatch_preamble import dispatch

pytestmark = pytest.mark.usefixtures('site')
PEER = 'peer.test'
HOST = 'test.piefed.local'


@pytest.fixture
def env(app, db_session, monkeypatch):
    """A local community with a moderator, a post and a reply, a local admin, and two remote
    reporters. Built like tests/test_ap_federated_reports.py's `env`, whose docstring says why
    the admin needs a `user_role` row and the author an `ap_profile_id`.

    The Flag arm relays a report of a post to the community's followers; that relay is
    recorded away, it is not what these tests are about."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    monkeypatch.setattr(activitypub_routes, 'announce_activity_to_followers', lambda *args, **kwargs: None)
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    db.session.commit()

    local = make_instance(HOST, software='piefed')
    admin = make_user(local, 'founder', local=True)
    grant_permission(admin, 'change instance settings')
    g.admin_ids = [admin.id]
    author = make_user(local, 'author', local=True)
    author.ap_profile_id = f'https://{HOST}/u/author'
    community = make_community('general')
    moderator = make_user(local, 'moderator', local=True)
    make_community_member(moderator, community, is_moderator=True)
    db.session.commit()
    post = make_post(community, author, f'https://{HOST}/c/general/p/1/a-post', title='A POST')
    reply = make_post_reply(post, author, body='a reply')
    reply.ap_id = f'https://{HOST}/comment/1'
    peer = make_instance(PEER)
    reporter = make_user(peer, 'reporter')
    reporter.ap_profile_id = f'https://{PEER}/u/reporter'
    reporter.ap_fetched_at = utcnow()
    other = make_user(peer, 'other')
    other.ap_profile_id = f'https://{PEER}/u/other'
    other.ap_fetched_at = utcnow()
    db.session.commit()
    return SimpleNamespace(community=community, post=post, reply=reply, author=author, admin=admin,
                           moderator=moderator, reporter=reporter, other=other)


def flag(reporter, target_ap_id):
    return {'id': f'https://{PEER}/activities/flag/{reporter.user_name}', 'type': 'Flag',
            'actor': reporter.ap_profile_id, 'object': target_ap_id, 'summary': 'spam'}


def undo(actor, inner):
    return {'id': f'https://{PEER}/activities/undo/{uuid.uuid4().hex}', 'type': 'Undo',
            'actor': actor.ap_profile_id, 'object': inner}


def last_log():
    return ActivityPubLog.query.order_by(ActivityPubLog.id.desc()).first()


def test_an_inbound_flag_stores_its_id_on_the_report(env):
    dispatch(flag(env.reporter, env.post.ap_id))

    assert Report.query.one().ap_id == f'https://{PEER}/activities/flag/reporter'


def test_a_flag_id_longer_than_the_column_is_not_stored(env):
    """String(255). A longer id is not truncated -- a truncated id would match no Undo
    anyway -- so the report is filed without one and the fallback lookup finds it."""
    process_report(env.reporter, env.post, {'id': f'https://{PEER}/' + 'x' * 300, 'summary': 'spam'},
                   db.session)

    assert Report.query.one().ap_id is None


def test_an_undo_withdraws_an_open_post_report_and_its_notification(env):
    dispatch(flag(env.reporter, env.post.ap_id))
    assert Notification.query.filter_by(user_id=env.moderator.id).count() == 1

    dispatch(undo(env.reporter, flag(env.reporter, env.post.ap_id)))

    db.session.expire_all()
    assert Report.query.count() == 0
    assert db.session.get(Post, env.post.id).reports == 0
    assert Notification.query.filter_by(user_id=env.moderator.id).count() == 0
    log = last_log()
    assert (log.activity_type, log.result) == ('Report', 'success')


def test_an_undo_of_a_user_report_recounts_the_admins_unread_notifications(env):
    dispatch(flag(env.reporter, env.author.ap_profile_id))
    db.session.expire_all()
    assert db.session.get(User, env.admin.id).unread_notifications == 1

    dispatch(undo(env.reporter, flag(env.reporter, env.author.ap_profile_id)))

    db.session.expire_all()
    assert Report.query.count() == 0
    assert db.session.get(User, env.author.id).reports == 0
    assert Notification.query.filter_by(user_id=env.admin.id).count() == 0
    assert db.session.get(User, env.admin.id).unread_notifications == 0


def test_an_undo_of_a_reply_report_never_takes_the_counter_below_zero(env):
    """A moderator resolving another report on the same reply resets its counter to 0
    (community_moderate_report_resolve) while this report is still open."""
    dispatch(flag(env.reporter, env.reply.ap_id))
    reply = db.session.get(PostReply, env.reply.id)
    assert reply.reports == 1
    reply.reports = 0
    db.session.commit()

    dispatch(undo(env.reporter, flag(env.reporter, env.reply.ap_id)))

    db.session.expire_all()
    assert Report.query.count() == 0
    assert db.session.get(PostReply, env.reply.id).reports == 0
    assert Notification.query.filter_by(user_id=env.moderator.id).count() == 0


@pytest.mark.parametrize('status', [REPORT_STATE_ESCALATED, REPORT_STATE_APPEALED, REPORT_STATE_RESOLVED,
                                    REPORT_STATE_DISCARDED],
                         ids=['escalated', 'appealed', 'resolved', 'discarded'])
def test_an_undo_of_a_report_moderators_acted_on_keeps_it_and_logs(env, status):
    dispatch(flag(env.reporter, env.post.ap_id))
    report = Report.query.one()
    report.status = status
    db.session.commit()

    dispatch(undo(env.reporter, flag(env.reporter, env.post.ap_id)))

    db.session.expire_all()
    assert Report.query.one().status == status
    log = last_log()
    assert log.result == 'ignored'
    assert log.exception_message == f'Undo Flag: report {report.id} was already acted on and is kept'


def test_only_the_reporter_may_withdraw_a_report(env):
    """The signed actor is the only identity: an Undo naming someone else's Flag id is
    refused, whatever the inner Flag's own `actor` says."""
    dispatch(flag(env.reporter, env.post.ap_id))

    dispatch(undo(env.other, flag(env.reporter, env.post.ap_id)))

    assert Report.query.count() == 1
    log = last_log()
    assert log.result == 'failure'
    assert log.exception_message == 'Undo Flag: only the reporter may withdraw a report'


def test_an_undo_without_a_flag_id_withdraws_the_newest_matching_report(env):
    """Reports filed before Report.ap_id existed carry none. The Undo then takes the newest
    report by the same reporter about the same target, and only that one."""
    process_report(env.reporter, env.post, {'summary': 'older'}, db.session)
    process_report(env.reporter, env.post, {'summary': 'newer'}, db.session)

    dispatch(undo(env.reporter, {'type': 'Flag', 'actor': env.reporter.ap_profile_id,
                                 'object': env.post.ap_id}))

    assert [report.reasons for report in Report.query.all()] == ['older']


def test_another_reporters_report_and_notification_are_untouched(env):
    dispatch(flag(env.reporter, env.post.ap_id))
    dispatch(flag(env.other, env.post.ap_id))

    dispatch(undo(env.reporter, flag(env.reporter, env.post.ap_id)))

    db.session.expire_all()
    assert [report.reporter_id for report in Report.query.all()] == [env.other.id]
    assert db.session.get(Post, env.post.id).reports == 1
    remaining = Notification.query.filter_by(user_id=env.moderator.id).all()
    assert [notification.author_id for notification in remaining] == [env.other.id]


def test_an_undo_matching_no_report_is_ignored(env):
    dispatch(undo(env.reporter, {'id': f'https://{PEER}/activities/flag/never', 'type': 'Flag',
                                 'object': 'https://peer.test/nothing'}))

    log = last_log()
    assert log.result == 'ignored'
    assert log.exception_message == 'Undo Flag: no matching report'
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_migration_report_ap_id.py tests/test_ap_interop_undo_flag.py -q`
Expected: FAIL.
- `tests/test_migration_report_ap_id.py`: `FileNotFoundError` for `e2a7c4f9d1b3_report_ap_id.py`, and `assert None is not None` for the column.
- `test_an_inbound_flag_stores_its_id_on_the_report`: `AttributeError: 'Report' object has no attribute 'ap_id'`.
- The withdraw tests: `assert 1 == 0` on `Report.query.count()` (the Undo logs 'Unmatched activity').
- The kept-report, reporter-only and no-match tests: `assert 'processing' == 'ignored'` / `== 'failure'`.

- [ ] **Step 3: Implement**

Create `migrations/versions/e2a7c4f9d1b3_report_ap_id.py`:

```python
"""report ap_id (interop C1 item 3: an Undo of a Flag finds its report)

Revision ID: e2a7c4f9d1b3
Revises: d9f3b7a15c2e
"""
import sqlalchemy as sa
from alembic import op

revision = 'e2a7c4f9d1b3'
down_revision = 'd9f3b7a15c2e'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('report', sa.Column('ap_id', sa.String(length=255), nullable=True))
    op.create_index('ix_report_ap_id', 'report', ['ap_id'], unique=False)


def downgrade():
    op.drop_index('ix_report_ap_id', table_name='report')
    op.drop_column('report', 'ap_id')
```

In `app/models.py`, in `class Report`, directly after the `targets = db.Column(db.JSON)` line, add:

```python
    ap_id = db.Column(db.String(255), index=True)   # the inbound Flag's id, so its Undo can find this report
```

In `app/activitypub/util.py`, in `process_report`, directly after the docstring and before `if 'summary' not in request_json:`, add:

```python
    # Report.ap_id lets an Undo of this Flag find the report (interop C1 item 3). An id too long for the
    # column is not truncated: a truncated id matches no Undo, and the reporter-and-target fallback still does.
    flag_id = request_json.get('id')
    ap_id = flag_id if isinstance(flag_id, str) and len(flag_id) <= 255 else None
```

Then add `ap_id=ap_id` to each of the three `Report(...)` constructions in that function:

```python
        report = Report(reasons=reasons[:255], description=description[:255],
                        type=type, reporter_id=user.id, suspect_user_id=reported.id,
                        source_instance_id=user.instance_id, targets=targets_data, ap_id=ap_id)
```

```python
        report = Report(reasons=reasons[:255], description=description[:255], type=type, reporter_id=user.id,
                        suspect_user_id=reported.author.id, suspect_post_id=reported.id,
                        suspect_community_id=reported.community.id, in_community_id=reported.community.id,
                        source_instance_id=user.instance_id, targets=targets_data, ap_id=ap_id)
```

```python
        report = Report(reasons=reasons[:255], description=description[:255], type=type, reporter_id=user.id,
                        suspect_post_id=post.id,
                        suspect_community_id=post.community.id,
                        suspect_user_id=reported.author.id, suspect_post_reply_id=reported.id,
                        in_community_id=post.community.id,
                        source_instance_id=user.instance_id,
                        targets=targets_data, ap_id=ap_id)
```

Directly after `process_report` (above `def process_quote_boost`), add:

```python
# What a report is about, by the type find_reported_object resolves: the Report column naming it, and the
# Report.type process_report gives it. Communities and conversations are not recorded (process_report, D1401).
REPORT_SUSPECT = {Post: ('suspect_post_id', REPORT_TYPE_POST),
                  PostReply: ('suspect_post_reply_id', REPORT_TYPE_REPLY),
                  User: ('suspect_user_id', REPORT_TYPE_USER)}


def withdraw_report(report: Report, session) -> None:
    """Delete an open report and correct what it raised.

    process_report added one to the suspect's `reports` counter and sent NOTIF_REPORT notifications, authored
    by the reporter and carrying the suspect's id in `targets`. Withdrawing takes the one back (never below 0:
    a moderator resolving another report on the same suspect resets the counter to 0, and -1 means exempt),
    deletes those notifications while unread, and recounts each recipient's `unread_notifications`.

    The moderators' Ignore (community_moderate_report_ignore) is not reused: it keeps the report as discarded
    and sets the counter to -1, which exempts the suspect from all future reports. A withdrawn report is one
    that should never have been filed, not one a moderator judged."""
    if report.suspect_post_reply_id:
        suspect = session.get(PostReply, report.suspect_post_reply_id)
        subtype, target_key, target_id = 'comment_reported', 'suspect_comment_id', report.suspect_post_reply_id
    elif report.suspect_post_id:
        suspect = session.get(Post, report.suspect_post_id)
        subtype, target_key, target_id = 'post_reported', 'suspect_post_id', report.suspect_post_id
    else:
        suspect = session.get(User, report.suspect_user_id)
        subtype, target_key, target_id = 'user_reported', 'suspect_user_id', report.suspect_user_id
    if suspect.reports > 0:
        suspect.reports -= 1
    notifications = session.query(Notification).\
        filter_by(notif_type=NOTIF_REPORT, subtype=subtype, author_id=report.reporter_id, read=False).\
        filter(Notification.targets[target_key].as_integer() == target_id).all()
    recipient_ids = {notification.user_id for notification in notifications}
    for notification in notifications:
        session.delete(notification)
    session.delete(report)
    session.flush()
    for recipient_id in recipient_ids:
        recipient = session.get(User, recipient_id)
        recipient.unread_notifications = session.query(Notification).filter_by(user_id=recipient_id,
                                                                                read=False).count()
    session.commit()


def withdraw_flag(user: User, flag: dict, session) -> tuple:
    """An Undo of a Flag by `user`, the signed actor. Returns (aplog_result, message) for the caller to log.

    Owner ruling (interop C1, 2026-10-11): withdraw the report if it is still open; if moderators already
    acted on it (escalated, appealed, resolved or discarded) keep it and log the Undo. Only the reporter may
    undo.

    The report is found by the Flag's id (Report.ap_id). Failing that -- a report filed before the column
    existed, or a Flag without an id -- the newest report by `user` about the same target. The target is
    resolved without fetching (find_reported_object never creates an actor)."""
    report = None
    flag_id = flag.get('id')
    if isinstance(flag_id, str):
        report = session.query(Report).filter_by(ap_id=flag_id).first()
    if report is None:
        target = flag.get('object')
        reported = find_reported_object(target) if isinstance(target, str) else None
        suspect = REPORT_SUSPECT.get(type(reported))
        if suspect is not None:
            column, report_type = suspect
            report = session.query(Report).filter_by(reporter_id=user.id, type=report_type,
                                                     **{column: reported.id}).\
                order_by(Report.created_at.desc(), Report.id.desc()).first()
    if report is None:
        return APLOG_IGNORED, 'Undo Flag: no matching report'
    if report.reporter_id != user.id:
        return APLOG_FAILURE, 'Undo Flag: only the reporter may withdraw a report'
    if report.status != REPORT_STATE_NEW:
        return APLOG_IGNORED, f'Undo Flag: report {report.id} was already acted on and is kept'
    withdraw_report(report, session)
    return APLOG_SUCCESS, None
```

(`Report`, `Notification`, `Post`, `PostReply`, `User` are already imported from `app.models` at U:32-39, and the constants come in through `from app.constants import *` at U:31.)

In `app/activitypub/routes.py`, add `withdraw_flag` to the `from app.activitypub.util import ...` list (R:24-35), after `undo_boost`:

```python
    announce_target_uri, undo_boost, withdraw_flag, LD_SIGNED_LOG_PREFIX, current_ld_signed
```

In the Undo arm, directly above the final

```python
                    log_incoming_ap(id, APLOG_MONITOR, APLOG_PROCESSING, request_json, 'Unmatched activity')
```

add:

```python
                    if core_activity['object']['type'] == 'Flag':  # Withdraw a report (interop C1 item 3)
                        result, message = withdraw_flag(user, core_activity['object'], session)
                        log_incoming_ap(id, APLOG_REPORT, result, saved_json, message)
                        return

```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_migration_report_ap_id.py tests/test_ap_interop_undo_flag.py tests/test_ap_federated_reports.py tests/test_ap_reports_and_undo_votes.py tests/test_inbox_dispatch_undo_content.py -q`
Expected: PASS. `tests/test_ap_federated_reports.py` and the `process_report` classes of `tests/test_ap_reports_and_undo_votes.py` must stay green unchanged; `test_an_undo_of_an_unrecognised_type_falls_through_to_monitor` (inner type `Move`) still reaches 'Unmatched activity'.

- [ ] **Step 5: Commit**

```bash
git add migrations/versions/e2a7c4f9d1b3_report_ap_id.py app/models.py app/activitypub/util.py \
        app/activitypub/routes.py tests/test_ap_interop_undo_flag.py tests/test_migration_report_ap_id.py
git commit -m "feat: a peer's Undo of a Flag withdraws the report while moderators have not acted on it

An Undo of a Flag was logged as 'Unmatched activity' and the report
stayed queued. Inbound reports now keep the Flag's id in the new
Report.ap_id column, and its Undo finds the report by that id, or by
reporter and target for older reports. An open report is deleted with
its unread notifications, and its suspect's report counter comes down
by one. A report that was escalated, appealed, resolved or discarded is
kept and the Undo is logged. Only the reporter may undo.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Task gate**

Run, in order:
1. `./run_tests.sh tests/test_migration_report_ap_id.py tests/test_ap_interop_undo_flag.py tests/test_ap_federated_reports.py tests/test_ap_reports_and_undo_votes.py tests/test_inbox_dispatch_undo_content.py --cov=app --cov-branch --cov-report=json -q`
2. `python3 tests/check_changed_line_coverage.py coverage.json HEAD~1 --branches app/`

Expected: all pass; every added line and branch in `app/` is covered (exit 0).

---

### Task 7: Undo of an Add or a Remove reverses it, under the same authority (item 3, Add/Remove)

**Files:**
- Modify: `app/activitypub/routes.py:1121-1127` (the D115 gate in the Announce preamble: let a feed's Announce of an Undo of its own Add/Remove through)
- Modify: `app/activitypub/routes.py:1737-1923` (the `Add` and `Remove` arms of `process_inbox_request` move, verbatim apart from the marked lines, into two new module-level functions)
- Modify: `app/activitypub/routes.py` (the Undo arm: two new sub-arms directly above the Flag sub-arm Task 6 added, which sits above `'Unmatched activity'`, R:2254)
- Modify: `app/activitypub/routes.py:3126` (add `may_moderate`, `process_add`, `process_remove` directly above `def process_chat`)
- Test: `tests/test_ap_interop_undo_add_remove.py` (new)

**Interfaces:**
- Consumes: `tests/test_inbox_dispatch_preamble.py:dispatch`; `tests/test_inbox_dispatch_lock_delete.py:record_moderation`; `tests/test_inbox_dispatch_add_remove.py:_seed_community_with_mod_and_admin(host='peer.example', name='modcomm') -> (instance, community, moderator, admin)`; factories `make_feed`, `make_feed_item`, `make_community`, `make_post`, `make_user`, `make_community_member`, `inbox_activity`. Task 6's Flag sub-arm in the Undo arm (these sub-arms go directly above it).
- Produces (all in `app/activitypub/routes.py`):
  - `may_moderate(community: Community, actor) -> bool`: `community.is_moderator(actor) or community.is_instance_admin(actor)`.
  - `process_add(core_activity: dict, user, community, feed, announced: bool, id: str, saved_json, session, undo: bool = False) -> None`
  - `process_remove(core_activity: dict, user, community, feed, announced: bool, id: str, saved_json, session, undo: bool = False) -> None`

  Both log their own outcome with `APLOG_ADD` / `APLOG_REMOVE`, so an Undo of an Add is logged as a `Remove` row and an Undo of a Remove as an `Add` row. With `undo=True` a target whose state already holds is a logged `ignored` no-op.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ap_interop_undo_add_remove.py`:

```python
"""Interop C1, item 3 (Add/Remove): an Undo of an Add runs the Remove, and an Undo of a
Remove runs the Add -- for a sticky post, a moderator, and a community in a feed.

Before this change the Undo dispatch had no Add or Remove arm: both fell through to
'Unmatched activity' and the change stayed. A feed's Announce of such an Undo did not even
get that far: the D115 gate refused it because it named no user or community.

The authority is exactly the original handlers': a moderator of the community or an admin
of its instance (`may_moderate`), and for a feed, the feed's own signed Announce. An Undo
whose target is already in the state it asks for is a logged no-op.

The undone activity is the dict inside the Undo. An Undo whose object is a bare URI was
already refused by the Undo arm (D112) and still is: nothing is fetched to find out what
it named.
"""
import pytest

from app import db
from app.activitypub.routes import may_moderate
from app.models import ActivityPubLog, CommunityMember, FeedItem, Post, utcnow
from tests.factories import inbox_activity, make_community, make_feed, make_feed_item, make_instance, make_post, \
    make_user, make_community_member
from tests.test_inbox_dispatch_add_remove import _seed_community_with_mod_and_admin
from tests.test_inbox_dispatch_lock_delete import record_moderation
from tests.test_inbox_dispatch_preamble import dispatch


def undo_from_community(community, actor, inner_type, object_value, target):
    """An Announce by `community` of `actor`'s Undo of an Add or Remove -- the shape Lemmy
    and PieFed use for every moderation activity in a community."""
    inner = {'id': f'{community.ap_profile_id}/activities/{inner_type.lower()}-1', 'type': inner_type,
             'actor': actor.ap_profile_id, 'object': object_value, 'target': target}
    undo = {'id': f'{community.ap_profile_id}/activities/undo-1', 'type': 'Undo',
            'actor': actor.ap_profile_id, 'object': inner}
    return inbox_activity(community, activity_type='Announce', object=undo)


def undo_from_feed(feed, inner_type, community):
    """A feed's Announce of its own Undo of an Add or Remove of `community`, the shape the feed
    Add/Remove tests in tests/test_inbox_dispatch_add_remove.py use for the plain activity."""
    inner = {'id': f'{feed.ap_profile_id}/activities/{inner_type.lower()}-1', 'type': inner_type,
             'actor': feed.ap_profile_id, 'object': {'id': community.ap_profile_id},
             'target': feed.ap_profile_id}
    undo = {'id': f'{feed.ap_profile_id}/activities/undo-1', 'type': 'Undo',
            'actor': feed.ap_profile_id, 'object': inner}
    return inbox_activity(feed, activity_type='Announce', object=undo)


def only_log():
    log = ActivityPubLog.query.one()
    return log.activity_type, log.result, log.exception_message


@pytest.fixture
def modcomm(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, moderator, admin = _seed_community_with_mod_and_admin(name='undocomm')
    community.ap_moderators_url = community.ap_profile_id + '/moderators'
    db.session.commit()
    author = make_user(instance, 'postauthor')
    post = make_post(community, author, ap_id='https://peer.example/post/7')
    return instance, community, moderator, admin, post


@pytest.fixture
def feedcomm(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    make_user(instance, 'community_owner')  # occupies user id=1 for make_community's hardcoded owner
    community = make_community(name='feedcomm', host='peer.example')
    community.ap_fetched_at = utcnow()
    db.session.commit()
    feed = make_feed(instance)
    feed.num_communities = 3
    db.session.commit()
    return community, feed


def test_may_moderate_is_a_moderator_or_an_instance_admin(modcomm):
    instance, community, moderator, admin, post = modcomm
    outsider = make_user(instance, 'outsider')
    assert may_moderate(community, moderator) is True
    assert may_moderate(community, admin) is True
    assert may_moderate(community, outsider) is False


# --- sticky ---------------------------------------------------------------------------

def test_an_undo_of_an_add_to_featured_unsticks_the_post(modcomm):
    instance, community, moderator, admin, post = modcomm
    post.sticky = True
    db.session.commit()

    dispatch(undo_from_community(community, moderator, 'Add', post.ap_id, community.ap_profile_id + '/featured'))

    db.session.expire_all()
    assert db.session.get(Post, post.id).sticky is False
    assert only_log() == ('Remove', 'success', None)


def test_an_undo_of_a_remove_from_featured_sticks_the_post_again(modcomm):
    instance, community, moderator, admin, post = modcomm

    dispatch(undo_from_community(community, admin, 'Remove', post.ap_id, community.ap_profile_id + '/featured'))

    db.session.expire_all()
    assert db.session.get(Post, post.id).sticky is True
    assert only_log() == ('Add', 'success', None)


def test_an_undo_of_an_add_to_featured_of_a_post_that_is_not_sticky_is_a_logged_no_op(modcomm):
    instance, community, moderator, admin, post = modcomm

    dispatch(undo_from_community(community, moderator, 'Add', post.ap_id, community.ap_profile_id + '/featured'))

    db.session.expire_all()
    assert db.session.get(Post, post.id).sticky is False
    assert only_log() == ('Remove', 'ignored', 'Undo: post is not sticky')


def test_an_undo_of_a_remove_from_featured_of_a_sticky_post_is_a_logged_no_op(modcomm):
    instance, community, moderator, admin, post = modcomm
    post.sticky = True
    db.session.commit()

    dispatch(undo_from_community(community, moderator, 'Remove', post.ap_id, community.ap_profile_id + '/featured'))

    db.session.expire_all()
    assert db.session.get(Post, post.id).sticky is True
    assert only_log() == ('Add', 'ignored', 'Undo: post is already sticky')


def test_an_undo_by_someone_without_authority_changes_nothing(modcomm):
    """The original handlers' check, unchanged: neither a moderator nor an instance admin."""
    instance, community, moderator, admin, post = modcomm
    post.sticky = True
    db.session.commit()
    outsider = make_user(instance, 'outsider')
    outsider.ap_fetched_at = utcnow()
    db.session.commit()

    dispatch(undo_from_community(community, outsider, 'Add', post.ap_id, community.ap_profile_id + '/featured'))

    db.session.expire_all()
    assert db.session.get(Post, post.id).sticky is True
    assert only_log() == ('Remove', 'failure', 'Does not have permission')


# --- moderators -----------------------------------------------------------------------

def test_an_undo_of_an_add_mod_demotes_and_writes_the_modlog(modcomm, monkeypatch):
    instance, community, moderator, admin, post = modcomm
    calls = record_moderation(monkeypatch, 'add_to_modlog')
    promoted = make_user(instance, 'promoted')
    promoted.ap_fetched_at = utcnow()
    make_community_member(promoted, community, is_moderator=True)

    dispatch(undo_from_community(community, admin, 'Add', promoted.ap_profile_id, community.ap_moderators_url))

    membership = CommunityMember.query.filter_by(community_id=community.id, user_id=promoted.id).one()
    assert membership.is_moderator is False
    assert [args[0] for args, kwargs in calls['add_to_modlog']] == ['remove_mod']
    assert only_log() == ('Remove', 'success', None)


def test_an_undo_of_a_remove_mod_promotes_again_and_writes_the_modlog(modcomm, monkeypatch):
    instance, community, moderator, admin, post = modcomm
    calls = record_moderation(monkeypatch, 'add_to_modlog')
    demoted = make_user(instance, 'demoted')
    demoted.ap_fetched_at = utcnow()
    make_community_member(demoted, community, is_moderator=False)

    dispatch(undo_from_community(community, moderator, 'Remove', demoted.ap_profile_id, community.ap_moderators_url))

    membership = CommunityMember.query.filter_by(community_id=community.id, user_id=demoted.id).one()
    assert membership.is_moderator is True
    assert [args[0] for args, kwargs in calls['add_to_modlog']] == ['add_mod']
    assert only_log() == ('Add', 'success', None)


@pytest.mark.parametrize('membership', ['none', 'member'])
def test_an_undo_of_an_add_mod_for_someone_not_a_moderator_is_a_logged_no_op(modcomm, monkeypatch, membership):
    instance, community, moderator, admin, post = modcomm
    calls = record_moderation(monkeypatch, 'add_to_modlog')
    someone = make_user(instance, 'someone')
    someone.ap_fetched_at = utcnow()
    db.session.commit()
    if membership == 'member':
        make_community_member(someone, community, is_moderator=False)

    dispatch(undo_from_community(community, moderator, 'Add', someone.ap_profile_id, community.ap_moderators_url))

    assert calls['add_to_modlog'] == []
    assert only_log() == ('Remove', 'ignored', 'Undo: not a moderator')


def test_an_undo_of_a_remove_mod_for_a_moderator_is_a_logged_no_op(modcomm, monkeypatch):
    instance, community, moderator, admin, post = modcomm
    calls = record_moderation(monkeypatch, 'add_to_modlog')
    still_mod = make_user(instance, 'stillmod')
    still_mod.ap_fetched_at = utcnow()
    make_community_member(still_mod, community, is_moderator=True)

    dispatch(undo_from_community(community, admin, 'Remove', still_mod.ap_profile_id, community.ap_moderators_url))

    assert calls['add_to_modlog'] == []
    assert only_log() == ('Add', 'ignored', 'Undo: already a moderator')


# --- feeds ----------------------------------------------------------------------------

def test_a_feeds_undo_of_an_add_takes_the_community_out_of_the_feed(feedcomm):
    community, feed = feedcomm
    make_feed_item(feed, community)

    dispatch(undo_from_feed(feed, 'Add', community))

    db.session.expire_all()
    assert FeedItem.query.filter_by(feed_id=feed.id, community_id=community.id).count() == 0
    assert feed.num_communities == 2
    assert only_log() == ('Remove', 'success', None)


def test_a_feeds_undo_of_a_remove_puts_the_community_back(feedcomm):
    community, feed = feedcomm

    dispatch(undo_from_feed(feed, 'Remove', community))

    db.session.expire_all()
    assert FeedItem.query.filter_by(feed_id=feed.id, community_id=community.id).count() == 1
    assert feed.num_communities == 4
    assert only_log() == ('Add', 'success', None)


def test_a_feeds_undo_of_a_remove_of_a_community_still_in_the_feed_is_a_logged_no_op(feedcomm):
    """A plain Add of a community already in the feed inserts a second FeedItem; an Undo must
    not."""
    community, feed = feedcomm
    make_feed_item(feed, community)

    dispatch(undo_from_feed(feed, 'Remove', community))

    db.session.expire_all()
    assert FeedItem.query.filter_by(feed_id=feed.id, community_id=community.id).count() == 1
    assert feed.num_communities == 3
    assert only_log() == ('Add', 'ignored', 'Undo: community is already in feed')


def test_a_feeds_undo_of_an_add_of_a_community_not_in_the_feed_is_a_logged_no_op(feedcomm):
    """Ignored and returned: the auto-unsubscribe loop that follows a real Remove must not run
    for a removal that did not happen."""
    community, feed = feedcomm

    dispatch(undo_from_feed(feed, 'Add', community))

    db.session.expire_all()
    assert feed.num_communities == 3
    assert only_log() == ('Remove', 'ignored', 'Community is not in feed')


def test_a_feeds_announce_of_an_undo_of_something_else_is_still_refused(feedcomm):
    """The D115 gate is widened for an Undo of an Add or Remove only."""
    community, feed = feedcomm
    undo = {'id': f'{feed.ap_profile_id}/activities/undo-2', 'type': 'Undo', 'actor': feed.ap_profile_id,
            'object': {'id': f'{feed.ap_profile_id}/activities/like-1', 'type': 'Like',
                       'actor': feed.ap_profile_id, 'object': 'https://peer.example/post/1'}}

    dispatch(inbox_activity(feed, activity_type='Announce', object=undo))

    assert only_log() == ('Announce', 'failure', 'Undo announced by a feed has no user or community')
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_ap_interop_undo_add_remove.py -q`
Expected: FAIL.
- Collection error first: `ImportError: cannot import name 'may_moderate' from 'app.activitypub.routes'`.
- Once that name exists, the community tests fail on state (`assert True is False` for the unstick) and on the log row (a `processing` row with message `'Unmatched activity'` instead of `('Remove', ...)`); the feed tests fail with the D115 refusal `'Undo announced by a feed has no user or community'`.
- `test_a_feeds_announce_of_an_undo_of_something_else_is_still_refused` passes already and guards the narrowed gate.

- [ ] **Step 3: Implement**

All edits are in `app/activitypub/routes.py`.

**(a) The D115 gate.** In the Announce preamble, replace:

```python
                    # A feed's Announce leaves user and community None. Only Add/Remove act on the feed itself, and
                    # Create/Update refuse this case on their own; every other arm needs a user or community (D115)
                    if user is None and community is None and request_json['object']['type'] not in ('Add', 'Remove', 'Create', 'Update'):
```

with:

```python
                    # A feed's Announce leaves user and community None. Only Add/Remove act on the feed itself, and
                    # Create/Update refuse this case on their own; every other arm needs a user or community (D115).
                    # An Undo of the feed's own Add/Remove acts on the feed too (interop C1 item 3).
                    undoes_feed_change = request_json['object']['type'] == 'Undo' and \
                        isinstance(request_json['object'].get('object'), dict) and \
                        request_json['object']['object'].get('type') in ('Add', 'Remove')
                    if user is None and community is None and not undoes_feed_change and \
                            request_json['object']['type'] not in ('Add', 'Remove', 'Create', 'Update'):
```

Leave the two lines that follow (the `log_incoming_ap(... f"{request_json['object']['type']} announced by a feed has no user or community")` and `return`) unchanged.

**(b) The Add and Remove arms.** Replace the whole `Add` arm, from `if core_activity['type'] == 'Add':  # Add mods, sticky a post, or add a community to a feed` down to and including its final `return` (R:1737-1813), with:

```python
                if core_activity['type'] == 'Add':  # Add mods, sticky a post, or add a community to a feed
                    process_add(core_activity, user, community, feed, announced, id, saved_json, session)
                    return
```

Replace the whole `Remove` arm, from `if core_activity['type'] == 'Remove':  # Remove mods, unsticky a post, or remove a community from a feed` down to and including its final `return` (R:1815-1923, just above `if core_activity['type'] == 'Move':`), with:

```python
                if core_activity['type'] == 'Remove':  # Remove mods, unsticky a post, or remove a community from a feed
                    process_remove(core_activity, user, community, feed, announced, id, saved_json, session)
                    return
```

**(c) The Undo sub-arms.** In the Undo arm, directly above the Flag sub-arm added by Task 6 (`if core_activity['object']['type'] == 'Flag':  # Withdraw a report (interop C1 item 3)`), add:

```python
                    # Interop C1 item 3: an Undo of an Add runs the Remove, an Undo of a Remove runs the Add, with the
                    # original handlers' authority checks. `user` is the signed Undo's actor, never the inner one's.
                    if core_activity['object']['type'] == 'Add':
                        process_remove(core_activity['object'], user, community, feed, announced, id, saved_json,
                                       session, undo=True)
                        return

                    if core_activity['object']['type'] == 'Remove':
                        process_add(core_activity['object'], user, community, feed, announced, id, saved_json,
                                    session, undo=True)
                        return

```

**(d) The new functions.** Directly above `def process_chat(user, store_ap_json, core_activity, session):`, add (the bodies are the old arms moved one level out; `mod` is replaced by `user`, the authority check by `may_moderate`, and the lines carrying `undo` are new):

```python
def may_moderate(community: Community, actor) -> bool:
    """The authority an inbound Add or Remove asks of its actor, and so does an Undo of either: a moderator of
    the community, or an admin of the community's instance (interop C1 item 3)."""
    return community.is_moderator(actor) or community.is_instance_admin(actor)


def process_add(core_activity, user, community, feed, announced, id, saved_json, session, undo=False):
    """An Add: make a moderator, sticky a post, or add a community to a feed.

    `undo` is set when this runs for an Undo of a Remove (interop C1 item 3). A target that is already in the
    added state is then a logged no-op, not a second write."""
    if not announced and not feed:
        community = find_community(core_activity)
    if feed and 'id' in core_activity['object']:
        community_to_add = find_actor_or_create_cached(core_activity['object']['id'], community_only=True)
        # if community found or created - add the FeedItem and update Feed info
        if community_to_add and isinstance(community_to_add, Community):
            if undo and session.query(FeedItem).filter_by(feed_id=feed.id, community_id=community_to_add.id).first():
                log_incoming_ap(id, APLOG_ADD, APLOG_IGNORED, saved_json, 'Undo: community is already in feed')
                return
            feed_item = FeedItem(feed_id=feed.id, community_id=community_to_add.id)
            session.add(feed_item)
            feed.num_communities += 1
            session.commit()
            # also autosubscribe any feedmembers to the new community
            feed_members = session.query(FeedMember).filter_by(feed_id=feed.id).all()
            for fm in feed_members:
                fm_user = session.get(User, fm.user_id)
                if fm_user.id == feed.user_id:
                    continue
                if fm_user.is_local() and fm_user.feed_auto_follow:
                    # user is local so lets auto-subscribe them to the community
                    actor = community_to_add.ap_id if community_to_add.ap_id else community_to_add.name
                    community_routes.do_subscribe(actor, fm_user.id, joined_via_feed=True)
            log_incoming_ap(id, APLOG_ADD, APLOG_SUCCESS, saved_json)
        else:
            log_incoming_ap(id, APLOG_ADD, APLOG_FAILURE, saved_json, 'Cannot find community to add to feed')
    elif community:
        if not may_moderate(community, user):
            log_incoming_ap(id, APLOG_ADD, APLOG_FAILURE, saved_json, 'Does not have permission')
            return
        if not isinstance(core_activity.get('target'), str):
            log_incoming_ap(id, APLOG_ADD, APLOG_FAILURE, saved_json, 'Add has no target')
            return
        target = core_activity['target']
        if not community.ap_featured_url:
            community.ap_featured_url = community.ap_profile_id + '/featured'
        featured_url = community.ap_featured_url
        moderators_url = community.ap_moderators_url
        if target.lower() == featured_url.lower():
            post = Post.get_by_ap_id(core_activity['object'])
            if post:
                if undo and post.sticky:
                    log_incoming_ap(id, APLOG_ADD, APLOG_IGNORED, saved_json, 'Undo: post is already sticky')
                    return
                post.sticky = True
                session.commit()
                log_incoming_ap(id, APLOG_ADD, APLOG_SUCCESS, saved_json)
            else:
                log_incoming_ap(id, APLOG_ADD, APLOG_FAILURE, saved_json,
                                'Cannot find: ' + core_activity['object'])
            return
        if target == moderators_url:
            new_mod = find_actor_or_create_cached(core_activity['object'])
            if new_mod:
                existing_membership = session.query(CommunityMember).filter_by(community_id=community.id,
                                                                      user_id=new_mod.id).first()
                if undo and existing_membership and existing_membership.is_moderator:
                    log_incoming_ap(id, APLOG_ADD, APLOG_IGNORED, saved_json, 'Undo: already a moderator')
                    return
                if existing_membership:
                    existing_membership.is_moderator = True
                else:
                    new_membership = CommunityMember(community_id=community.id, user_id=new_mod.id,
                                                     is_moderator=True)
                    session.add(new_membership)
                add_to_modlog('add_mod', actor=user, target_user=new_mod, community=community,
                              link_text=new_mod.display_name(), link=new_mod.link())
                session.commit()
                cache.delete_memoized(moderating_communities, new_mod.id)
                cache.delete_memoized(joined_communities, new_mod.id)
                cache.delete_memoized(community_moderators, community.id)
                cache.delete_memoized(moderating_communities_ids, new_mod.id)
                cache.delete_memoized(moderating_communities_ids_all_users)
                cache.delete_memoized(Community.moderators, community)
                log_incoming_ap(id, APLOG_ADD, APLOG_SUCCESS, saved_json)
            else:
                log_incoming_ap(id, APLOG_ADD, APLOG_FAILURE, saved_json,
                                'Cannot find: ' + core_activity['object'])
            return
        log_incoming_ap(id, APLOG_ADD, APLOG_FAILURE, saved_json, 'Unknown target for Add')
    else:
        log_incoming_ap(id, APLOG_ADD, APLOG_FAILURE, saved_json, 'Add: cannot find community or feed')


def process_remove(core_activity, user, community, feed, announced, id, saved_json, session, undo=False):
    """A Remove: demote a moderator, unsticky a post, or take a community out of a feed.

    `undo` is set when this runs for an Undo of an Add (interop C1 item 3). A target that is already in the
    removed state is then a logged no-op, and a feed's auto-unsubscribe loop does not run for it."""
    if not announced and not feed:
        community = find_community(core_activity)
    if feed and 'id' in core_activity['object']:
        community_to_remove = find_actor_or_create_cached(core_activity['object']['id'], community_only=True)
        # if community found or created - remove the FeedItem and update Feed info
        if community_to_remove and isinstance(community_to_remove, Community):
            feed_item = session.query(FeedItem).filter_by(feed_id=feed.id,
                                                 community_id=community_to_remove.id).first()
            if feed_item:
                session.delete(feed_item)
                feed.num_communities -= 1
                session.commit()
                log_incoming_ap(id, APLOG_REMOVE, APLOG_SUCCESS, saved_json)
            else:
                log_incoming_ap(id, APLOG_REMOVE, APLOG_IGNORED, saved_json, 'Community is not in feed')
                if undo:
                    return
            # also auto-unsubscribe any feedmembers from the community
            # who have feed_auto_leave enabled
            feed_members = session.query(FeedMember).filter_by(feed_id=feed.id).all()
            for fm in feed_members:
                fm_user = session.get(User, fm.user_id)
                if fm_user.id == feed.user_id:
                    continue
                if fm_user.is_local() and fm_user.feed_auto_leave:
                    subscription = community_membership(fm_user, community_to_remove)
                    cm = session.query(CommunityMember).filter_by(user_id=fm_user.id,
                                                         community_id=community_to_remove.id).first()
                    if subscription != SUBSCRIPTION_OWNER and cm and cm.joined_via_feed:
                        proceed = True
                        # Undo the Follow
                        if not community_to_remove.is_local():  # this is a remote community, so activitypub is needed
                            if not community_to_remove.instance.gone_forever:
                                follow_id = f"{current_app.config['SERVER_URL']}/activities/follow/{gibberish(15)}"
                                # D89 (owner ruling): the original Follow's id, for every peer rather than only ovo.st
                                join_request = session.query(CommunityJoinRequest).filter_by(user_id=fm_user.id,
                                                                                    community_id=community_to_remove.id).first()
                                if join_request:
                                    follow_id = f"{current_app.config['SERVER_URL']}/activities/follow/{join_request.uuid}"
                                undo_id = f"{current_app.config['SERVER_URL']}/activities/undo/" + gibberish(15)
                                follow = {'actor': fm_user.public_url(),
                                          'to': [community_to_remove.public_url()],
                                          'object': community_to_remove.public_url(), 'type': 'Follow',
                                          'id': follow_id}
                                undo_follow = {'actor': fm_user.public_url(),
                                               'to': [community_to_remove.public_url()], 'type': 'Undo',
                                               'id': undo_id, 'object': follow}
                                send_post_request(community_to_remove.ap_inbox_url, undo_follow,
                                                  fm_user.private_key, fm_user.public_url() + '#main-key',
                                                  timeout=10)

                        if proceed:
                            session.query(CommunityMember).filter_by(user_id=fm_user.id,
                                                                        community_id=community_to_remove.id).delete()
                            session.query(CommunityJoinRequest).filter_by(user_id=fm_user.id,
                                                                             community_id=community_to_remove.id).delete()
                            community_to_remove.subscriptions_count -= 1
                            session.commit()
                            log_incoming_ap(id, APLOG_REMOVE, APLOG_SUCCESS, saved_json,
                                            f'{fm_user.user_name} auto-unfollowed {community_to_remove.ap_public_url} during a feed/remove')
        else:
            log_incoming_ap(id, APLOG_REMOVE, APLOG_FAILURE, saved_json, 'Cannot find community to remove from feed')
    elif community:
        if not may_moderate(community, user):
            log_incoming_ap(id, APLOG_REMOVE, APLOG_FAILURE, saved_json, 'Does not have permission')
            return
        if not isinstance(core_activity.get('target'), str):
            log_incoming_ap(id, APLOG_REMOVE, APLOG_FAILURE, saved_json, 'Remove has no target')
            return
        target = core_activity['target']
        if not community.ap_featured_url:
            community.ap_featured_url = community.ap_profile_id + '/featured'
        featured_url = community.ap_featured_url
        moderators_url = community.ap_moderators_url
        if target.lower() == featured_url.lower():
            post = Post.get_by_ap_id(core_activity['object'])
            if post:
                if undo and not post.sticky:
                    log_incoming_ap(id, APLOG_REMOVE, APLOG_IGNORED, saved_json, 'Undo: post is not sticky')
                    return
                post.sticky = False
                session.commit()
                log_incoming_ap(id, APLOG_REMOVE, APLOG_SUCCESS, saved_json)
            else:
                log_incoming_ap(id, APLOG_REMOVE, APLOG_FAILURE, saved_json,
                                'Cannot find: ' + core_activity['object'])
            return
        if target == moderators_url:
            old_mod = find_actor_or_create_cached(core_activity['object'])
            if old_mod:
                existing_membership = session.query(CommunityMember).filter_by(community_id=community.id,
                                                                      user_id=old_mod.id).first()
                if undo and not (existing_membership and existing_membership.is_moderator):
                    log_incoming_ap(id, APLOG_REMOVE, APLOG_IGNORED, saved_json, 'Undo: not a moderator')
                    return
                if existing_membership:
                    existing_membership.is_moderator = False
                    session.commit()
                    cache.delete_memoized(moderating_communities, old_mod.id)
                    cache.delete_memoized(joined_communities, old_mod.id)
                    cache.delete_memoized(community_moderators, community.id)
                    cache.delete_memoized(moderating_communities_ids, old_mod.id)
                    cache.delete_memoized(moderating_communities_ids_all_users)
                    cache.delete_memoized(Community.moderators, community)
                    log_incoming_ap(id, APLOG_REMOVE, APLOG_SUCCESS, saved_json)
                    add_to_modlog('remove_mod', actor=user, target_user=old_mod, community=community,
                                  link_text=old_mod.display_name(), link=old_mod.link())
            else:
                log_incoming_ap(id, APLOG_REMOVE, APLOG_FAILURE, saved_json,
                                'Cannot find: ' + core_activity['object'])
            return
        log_incoming_ap(id, APLOG_REMOVE, APLOG_FAILURE, saved_json, 'Unknown target for Remove')
    else:
        log_incoming_ap(id, APLOG_REMOVE, APLOG_FAILURE, saved_json, 'Remove: cannot find community or feed')
```

Notes for the move:
- The old arms began with `if user is not None: mod = user`. `user` is never None on the `elif community:` path (a feed's Announce takes the feed branch), so `mod` is replaced by `user` and the assignment goes.
- In `process_remove` the outbound Undo dict is renamed `undo_follow`, because `undo` is now the parameter.
- Every name used here (`find_community`, `find_actor_or_create_cached`, `community_routes`, `add_to_modlog`, `community_membership`, `moderating_communities`, `joined_communities`, `community_moderators`, `moderating_communities_ids`, `moderating_communities_ids_all_users`, `gibberish`, `send_post_request`, `SUBSCRIPTION_OWNER`) is already a module-level name in `routes.py`, so the existing tests' doubles (`record_moderation`, `record_sends`, the `find_actor_or_create_cached` wrappers, `community_routes.do_subscribe`) keep reaching them.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_ap_interop_undo_add_remove.py tests/test_inbox_dispatch_add_remove.py tests/test_inbox_dispatch_undo_content.py tests/test_inbox_dispatch_undo_moderation.py tests/test_inbox_dispatch_announce.py -q`
Expected: PASS. All of `tests/test_inbox_dispatch_add_remove.py` must stay green unchanged: plain Adds and Removes behave exactly as before (`undo` defaults to False, so re-sticking a sticky post or re-adding a moderator still succeeds and writes the modlog).

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/routes.py tests/test_ap_interop_undo_add_remove.py
git commit -m "feat: an Undo of an Add or a Remove reverses the sticky, moderator or feed change

Undo dispatch had no Add or Remove arm, so a moderator taking back a
sticky or a promotion, or a feed taking back a community, was logged as
'Unmatched activity' and nothing changed. The Add and Remove handlers
move into process_add and process_remove, and an Undo of one now runs
the other, with the same authority check (may_moderate) and the same
modlog and cache invalidation. An Undo whose target is already in the
state it asks for is a logged no-op, and a feed's Announce of an Undo
of its own Add or Remove now gets past the D115 gate.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Task gate**

Run, in order:
1. `./run_tests.sh tests/test_ap_interop_undo_add_remove.py tests/test_inbox_dispatch_add_remove.py tests/test_inbox_dispatch_undo_content.py tests/test_inbox_dispatch_undo_moderation.py tests/test_inbox_dispatch_announce.py --cov=app --cov-branch --cov-report=json -q`
2. `python3 tests/check_changed_line_coverage.py coverage.json HEAD~1 --branches app/`

Expected: all pass; every added line and branch in `app/` is covered (exit 0). The moved lines count as added: `tests/test_inbox_dispatch_add_remove.py` is what covers the old arms' paths, which is why it is in the gate run.

---

### Task 8: A remote Group that deletes itself is marked deleted and becomes read-only (item 10)

**Files:**
- Modify: `app/models.py:1482-1483` (add `Community.is_gone()` directly after `Community.is_local()`)
- Modify: `app/activitypub/util.py` (import `community_membership` from `app.utils`, U:40-50; `_verify_announced_delete` passes a Delete of a stored Group through to the dispatcher, U:~5670; `_origin_refusal_of_delete` gains `tombstone_required`, U:5707-5747; new `GROUP_DELETED_MESSAGE`, `may_delete_group`, `mark_community_gone` and `delete_remote_group` directly after `_origin_refusal_of_delete`)
- Modify: `app/activitypub/routes.py` (import the three new names, R:24-36; `shared_inbox` self-Delete lookup, R:794-803; dispatcher preamble, R:1029-1037 and R:1043-1044; Delete arm, R:1609: a moderator's or Group-host admin's Delete of a Group)
- Modify: `app/utils.py:2975`, `:3019`, `:3064`, `:3111` (`can_downvote`, `can_upvote`, `can_create_post`, `can_create_post_reply`)
- Modify: `app/community/routes.py:1003` (`do_subscribe`) and `:755-756` (`show_community` notice)
- Modify: `app/api/alpha/utils/community.py:260-261` (`post_community_follow`)
- Modify: `app/shared/tasks/pages.py:176` (`send_post`), `app/shared/tasks/notes.py:116` (`send_reply`), `app/shared/tasks/deletes.py:148` (`delete_object`)
- Modify: `tests/test_api_routes_community.py` (one test in `TestJoiningAndLeaving`, one name added to its `app.models` import)
- Test: `tests/test_ap_interop_group_delete.py` (new)

**Interfaces:**
- Consumes: `tests/test_inbox_dispatch_preamble.py:dispatch(activity, store_ap_json=True)`; `tests/factories.py:inbox_activity(actor, *, activity_type, object_uri, **fields)`, `signed_inbox_post(client, activity, sender)`, `resolvable_remote_author(instance, name)`, `web_ctx(app, user)`, `bearer(user)`, `a_keypair()`; `tests/conftest.py:unsigned_but_precheck_clean_headers(body_bytes)`; `tests/test_shared_post_edit.py:_Field, _api_input, _web_form`; the `_seed`/`_send`/`_remote_inbox`/`_make_deliverable` helpers of `tests/test_shared_tasks_send_post.py`, `tests/test_shared_tasks_send_reply.py` and `tests/test_shared_tasks_deletes.py`; the existing `_origin_refusal_of_delete(object_id, label)` (U:5707); the security track's Group-host authority predicate `_may_remove_from(actor_id: str, community) -> bool` (U:5648: a stored moderator or owner of `community`, or an `InstanceRole` admin of the instance hosting it; lookup only, never fetches). Task 3 adds an `object['id'] == signer` check to the Group Update path; the `is_gone()` refusal added here sits ahead of it, as the first statement of the Community-actor branch, so a gone Group's Update is ignored before that check runs.
- Produces:
  - `Community.is_gone() -> bool` (`app/models.py`): `self.ap_deleted_at is not None`. The one predicate every write path asks.
  - `GROUP_DELETED_MESSAGE = 'Community was deleted on its home server'` (`app/activitypub/util.py`).
  - `_origin_refusal_of_delete(object_id: str, label: str, tombstone_required: bool = False) -> str | None` (`app/activitypub/util.py`).
  - `mark_community_gone(community: Community) -> None` (`app/activitypub/util.py`): sets `ap_deleted_at`, deletes local members' `CommunityMember` and `CommunityJoinRequest` rows (no Undo Follow), lowers `subscriptions_count`, commits, clears `community_membership`, `joined_communities`, `moderating_communities` for each.
  - `may_delete_group(actor_id: str, community: Community) -> bool` (`app/activitypub/util.py`): Amendment A's authority rule: the Group itself, or `_may_remove_from(actor_id, community)`.
  - `delete_remote_group(community: Community, activity_id: str, saved_json) -> None` (`app/activitypub/util.py`): verifies at the origin, then marks; logs every outcome. The caller has already checked authority.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ap_interop_group_delete.py`:

```python
"""Interop C1 item 10 (docs/superpowers/specs/2026-10-11-ap-interop-c1-design.md): Delete of a remote Group.

A Delete of a remote Group is accepted from the Group itself, a known moderator of it, or an admin on the Group's
host (spec Amendment A; Lemmy sends community deletions with a moderator as the actor, Announced by the Group).
Whoever sends it, it is applied only once the Group's origin confirms it with a 410 or a Tombstone. A 404, a Group still served, or an unreachable origin is not proof, and nothing changes.
On a verified Delete the Group gets `ap_deleted_at`, its LOCAL members are unsubscribed without an Undo Follow,
and it becomes read-only: no post, reply, vote or join through the web, the API or the inbox, and nothing is
delivered to it. Its content stays readable. A Delete of a Group by anyone else is refused. There is no
undelete: anything later from a gone Group is ignored and logged.
"""
import json
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from flask import g

import app.activitypub.util as activitypub_util
from app import db
from app.activitypub.routes import process_new_content, process_upvote
from app.activitypub.util import GROUP_DELETED_MESSAGE, delete_remote_group, mark_community_gone, may_delete_group
from app.community.routes import do_subscribe
from app.constants import POST_TYPE_ARTICLE, SRC_API, SRC_WEB
from app.models import (ActivityPubLog, Community, CommunityJoinRequest, CommunityMember, InstanceRole, Language,
                        Post, PostReply, PostVote, utcnow)
from app.shared.post import make_post as shared_make_post, vote_for_post
from app.shared.reply import make_reply
from app.shared.tasks.deletes import delete_post
from app.utils import (can_create_post, can_create_post_reply, can_downvote, can_upvote, community_membership,
                       joined_communities, moderating_communities)
from tests.conftest import unsigned_but_precheck_clean_headers
from tests.factories import (a_keypair, bearer, inbox_activity, make_community, make_community_member,
                             make_instance, make_post, make_site, make_user, resolvable_remote_author,
                             signed_inbox_post, web_ctx)
from tests.test_inbox_dispatch_preamble import dispatch
from tests.test_shared_post_edit import _Field, _api_input, _web_form
from tests.test_shared_tasks_deletes import PEER_INBOX as DELETE_INBOX
from tests.test_shared_tasks_deletes import _make_deliverable as _deliverable_for_delete
from tests.test_shared_tasks_deletes import _seed as _seed_for_delete
from tests.test_shared_tasks_send_post import PEER_INBOX as POST_INBOX
from tests.test_shared_tasks_send_post import _peer_example_resolves_without_a_resolver  # noqa: F401 -- autouse
from tests.test_shared_tasks_send_post import _remote_inbox as _post_remote_inbox
from tests.test_shared_tasks_send_post import _seed as _seed_for_post
from tests.test_shared_tasks_send_post import _send as _send_post
from tests.test_shared_tasks_send_reply import _make_deliverable as _deliverable_for_reply
from tests.test_shared_tasks_send_reply import _remote_inbox as _reply_remote_inbox
from tests.test_shared_tasks_send_reply import _seed as _seed_for_reply
from tests.test_shared_tasks_send_reply import _send as _send_reply

PEER = 'peer.example'
GROUP = f'https://{PEER}/c/memes'


def _world():
    """The local instance (id 1), a local owner holding user id 1 for make_community's hardcoded user_id, a remote
    Group on peer.example with one local member, one remote author and one of their posts in it.

    The member is keyed: can_create_post refuses a local user without a private key, and the refusal under test
    must be the Group's, not that one. ap_fetched_at is stamped on the Group (and resolvable_remote_author stamps
    the author) so find_actor_or_create_cached never schedules a refresh fetch."""
    site = make_site()
    local = make_instance('test.piefed.local', software='piefed')
    peer = make_instance(PEER)
    make_user(local, 'owner', local=True)
    community = make_community('memes', host=PEER)
    community.ap_id = f'memes@{PEER}'
    community.instance_id = peer.id
    community.ap_inbox_url = f'{GROUP}/inbox'
    community.ap_fetched_at = utcnow()
    community.subscriptions_count = 1
    db.session.commit()
    member = make_user(local, 'member', local=True, with_keys=True)
    make_community_member(member, community)
    alice = resolvable_remote_author(peer, 'alice')
    post = make_post(community, alice, f'https://{PEER}/post/1')
    g.site = site
    return SimpleNamespace(site=site, peer=peer, community=community, member=member, alice=alice, post=post)


def _gone(w):
    w.community.ap_deleted_at = utcnow()
    db.session.commit()


def _self_delete(w, obj=GROUP):
    activity = inbox_activity(w.community, activity_type='Delete', object_uri=GROUP)
    activity['object'] = obj
    return activity


# --- the Delete: verified at the origin, or nothing changes ---

@pytest.mark.parametrize('answer', [{'status_code': 410},
                                    {'status_code': 200, 'json': {'id': GROUP, 'type': 'Tombstone'}}],
                         ids=['410', 'tombstone'])
@pytest.mark.parametrize('obj', [GROUP, {'id': GROUP, 'type': 'Group'}], ids=['string-object', 'dict-object'])
def test_a_verified_self_delete_marks_the_group_and_unsubscribes_only_local_members(
        app, db_session, http_mock, monkeypatch, answer, obj):
    """Remote moderators' CommunityMember rows (read from the Group's moderators collection) are the Group's own
    history, not local subscriptions: they stay. Nothing is sent: no Undo Follow to a Group that is gone."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()
    remote_mod = make_user(w.peer, 'mod')
    make_community_member(remote_mod, w.community, is_moderator=True)
    db.session.add(CommunityJoinRequest(user_id=w.member.id, community_id=w.community.id))
    db.session.commit()
    community_id, member_id, mod_id = w.community.id, w.member.id, remote_mod.id
    http_mock.get(GROUP).respond(**answer)

    dispatch(_self_delete(w, obj))

    db.session.expire_all()
    community = db.session.get(Community, community_id)
    assert community.ap_deleted_at is not None
    assert community.subscriptions_count == 0
    assert db.session.query(CommunityMember).filter_by(community_id=community_id, user_id=member_id).count() == 0
    assert db.session.query(CommunityMember).filter_by(community_id=community_id, user_id=mod_id).count() == 1
    assert db.session.query(CommunityJoinRequest).filter_by(community_id=community_id).count() == 0
    assert db.session.query(Post).filter_by(community_id=community_id).count() == 1   # content is kept
    assert ActivityPubLog.query.filter_by(direction='in').one().result == 'success'
    assert ActivityPubLog.query.filter_by(direction='out').count() == 0


@pytest.mark.parametrize('answer,reason', [
    ({'status_code': 404}, 'Group Delete: origin 404 does not prove it deleted'),
    ({'status_code': 200, 'json': {'id': GROUP, 'type': 'Group'}}, 'Group Delete of an object that still exists'),
    ({'status_code': 500}, 'Group Delete: the object answers HTTP 500'),
], ids=['404', 'still-served', '500'])
def test_an_unproven_self_delete_changes_nothing(app, db_session, http_mock, monkeypatch, answer, reason):
    """Owner ruling: a 404 does not delete (as for private objects); only a 410 or a Tombstone does."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()
    community_id, member_id = w.community.id, w.member.id
    http_mock.get(GROUP).respond(**answer)

    dispatch(_self_delete(w))

    db.session.expire_all()
    assert db.session.get(Community, community_id).ap_deleted_at is None
    assert db.session.query(CommunityMember).filter_by(community_id=community_id, user_id=member_id).count() == 1
    log = ActivityPubLog.query.one()
    assert (log.result, log.exception_message) == ('failure', reason)


def test_an_unreachable_origin_is_not_proof(app, db_session, http_mock, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()
    community_id = w.community.id
    http_mock.get(GROUP).mock(side_effect=httpx.ConnectError('unreachable'))

    dispatch(_self_delete(w))

    db.session.expire_all()
    assert db.session.get(Community, community_id).ap_deleted_at is None
    log = ActivityPubLog.query.one()
    assert (log.result, log.exception_message) == ('failure', 'Group Delete: the object could not be fetched')


GROUP_DELETE_REFUSED = 'Delete of a Group is accepted only from the Group, its moderators or an admin of its host'


def _moderator(w, name='mod', instance=None):
    """A remote moderator of the Group, stored as the Group's moderators collection is stored here."""
    mod = resolvable_remote_author(instance or w.peer, name)
    make_community_member(mod, w.community, is_moderator=True)
    return mod


def _lemmy_delete(actor):
    """Lemmy's shape: the moderator's Delete of the community, Announced by the Group."""
    inner = {'id': f'{actor.ap_profile_id}/activities/delete/{uuid.uuid4().hex}', 'type': 'Delete',
             'actor': actor.ap_profile_id, 'object': GROUP,
             'to': ['https://www.w3.org/ns/activitystreams#Public'], 'cc': [GROUP]}
    return {'id': f'{GROUP}/activities/announce/{uuid.uuid4().hex}', 'type': 'Announce', 'actor': GROUP,
            'object': inner}


def _inbound_logs():
    return [(log.activity_type, log.result, log.exception_message)
            for log in ActivityPubLog.query.filter_by(direction='in').all()]


def test_a_delete_of_a_group_by_a_random_user_is_refused_without_a_fetch(app, db_session, monkeypatch):
    """No http_mock route: a fetch would fail the test. alice posts in the Group but neither moderates it nor
    administers its host."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()
    community_id, member_id = w.community.id, w.member.id

    dispatch(inbox_activity(w.alice, activity_type='Delete', object_uri=GROUP))

    db.session.expire_all()
    assert db.session.get(Community, community_id).ap_deleted_at is None
    assert db.session.query(CommunityMember).filter_by(community_id=community_id, user_id=member_id).count() == 1
    assert _inbound_logs() == [('Delete', 'failure', GROUP_DELETE_REFUSED)]


@pytest.mark.parametrize('answer', [{'status_code': 410},
                                    {'status_code': 200, 'json': {'id': GROUP, 'type': 'Tombstone'}}],
                         ids=['410', 'tombstone'])
def test_a_lemmy_moderator_delete_announced_by_the_group_is_verified_and_marks_it(
        app, db_session, http_mock, monkeypatch, answer):
    """Amendment A: Lemmy deletes a community with a moderator as the actor. The moderator's Delete, Announced
    by the Group, is accepted once the origin confirms it, exactly as the Group's own Delete is."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()
    mod = _moderator(w)
    community_id, member_id, mod_id = w.community.id, w.member.id, mod.id
    route = http_mock.get(GROUP).respond(**answer)

    dispatch(_lemmy_delete(mod))

    db.session.expire_all()
    assert route.call_count == 1
    assert db.session.get(Community, community_id).ap_deleted_at is not None
    assert db.session.query(CommunityMember).filter_by(community_id=community_id, user_id=member_id).count() == 0
    assert db.session.query(CommunityMember).filter_by(community_id=community_id, user_id=mod_id).count() == 1
    assert ('Delete', 'success', None) in _inbound_logs()


def test_a_moderator_delete_is_not_applied_while_the_origin_still_serves_the_group(
        app, db_session, http_mock, monkeypatch):
    """The origin refetch is required whoever sends the Delete: a moderator's word is not proof."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()
    mod = _moderator(w)
    community_id, member_id = w.community.id, w.member.id
    http_mock.get(GROUP).respond(200, json={'id': GROUP, 'type': 'Group'})

    dispatch(_lemmy_delete(mod))

    db.session.expire_all()
    assert db.session.get(Community, community_id).ap_deleted_at is None
    assert db.session.query(CommunityMember).filter_by(community_id=community_id, user_id=member_id).count() == 1
    assert ('Delete', 'failure', 'Group Delete of an object that still exists') in _inbound_logs()


def test_a_moderator_on_another_host_is_verified_not_deferred(app, db_session, http_mock, monkeypatch):
    """A cross-origin Announced Delete by a moderator is normally deferred for cross_origin_delete_delay
    (apply_delayed_delete), which only knows posts and comments and would drop a Group as 'superseded'.
    A Delete naming a stored Group skips the deferral and goes straight to the origin check."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()
    mod = _moderator(w, 'farmod', instance=make_instance('far.example'))
    community_id = w.community.id
    http_mock.get(GROUP).respond(410)

    dispatch(_lemmy_delete(mod))

    db.session.expire_all()
    assert db.session.get(Community, community_id).ap_deleted_at is not None


def test_an_admin_on_the_groups_host_may_delete_it(app, db_session, http_mock, monkeypatch):
    """The security track's Group-host admin rule (_may_remove_from): an InstanceRole admin of the Group's
    instance, sending the Delete directly."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()
    admin = resolvable_remote_author(w.peer, 'admin')
    db.session.add(InstanceRole(instance_id=w.peer.id, user_id=admin.id, role='admin'))
    db.session.commit()
    community_id = w.community.id
    http_mock.get(GROUP).respond(410)

    dispatch(inbox_activity(admin, activity_type='Delete', object_uri=GROUP))

    db.session.expire_all()
    assert db.session.get(Community, community_id).ap_deleted_at is not None
    assert _inbound_logs() == [('Delete', 'success', None)]


def test_an_admin_of_another_host_may_not_delete_the_group(app, db_session, monkeypatch):
    """Admin of far.example, not of the Group's host: refused, and nothing is fetched."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()
    far = make_instance('far.example')
    admin = resolvable_remote_author(far, 'admin')
    db.session.add(InstanceRole(instance_id=far.id, user_id=admin.id, role='admin'))
    db.session.commit()
    community_id = w.community.id

    dispatch(inbox_activity(admin, activity_type='Delete', object_uri=GROUP))

    db.session.expire_all()
    assert db.session.get(Community, community_id).ap_deleted_at is None
    assert _inbound_logs() == [('Delete', 'failure', GROUP_DELETE_REFUSED)]


def test_a_moderator_delete_of_a_group_already_gone_is_ignored_without_a_fetch(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()
    mod = _moderator(w)
    _gone(w)

    dispatch(inbox_activity(mod, activity_type='Delete', object_uri=GROUP))

    assert _inbound_logs() == [('Delete', 'ignored', GROUP_DELETED_MESSAGE)]


def test_may_delete_group(app, db_session):
    w = _world()
    mod = _moderator(w)
    assert may_delete_group(GROUP.upper(), w.community) is True     # the Group itself, any case
    assert may_delete_group(mod.ap_profile_id, w.community) is True
    assert may_delete_group(w.alice.ap_profile_id, w.community) is False
    assert may_delete_group('https://nobody.example/u/x', w.community) is False   # not stored: never fetched
    assert may_delete_group(None, w.community) is False


def test_a_local_community_is_never_marked_by_a_delete(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    make_site()
    make_instance('test.piefed.local', software='piefed')
    make_user(None, 'owner', local=True)
    local_community = make_community('home')

    delete_remote_group(local_community, 'https://peer.example/activities/delete/1', None)

    assert db.session.get(Community, local_community.id).ap_deleted_at is None
    log = ActivityPubLog.query.one()
    assert (log.result, log.exception_message) == ('failure', 'Delete of a local community is not accepted')


def test_marking_a_group_gone_clears_each_local_members_memoized_membership(app, db_session, monkeypatch):
    w = _world()
    cleared = []
    monkeypatch.setattr(activitypub_util.cache, 'delete_memoized', lambda fn, *args: cleared.append((fn, args)))

    mark_community_gone(w.community)

    assert (community_membership, (w.member, w.community)) in cleared
    assert (joined_communities, (w.member.id,)) in cleared
    assert (moderating_communities, (w.member.id,)) in cleared


# --- no undelete: a gone Group is not heard from again ---

@pytest.mark.parametrize('activity_type,obj', [
    ('Update', {'id': GROUP, 'type': 'Group', 'name': 'renamed'}),
    ('Delete', GROUP),
], ids=['update', 'second-delete'])
def test_a_gone_groups_update_or_repeated_delete_is_ignored_without_a_fetch(
        app, db_session, monkeypatch, activity_type, obj):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()
    _gone(w)
    activity = inbox_activity(w.community, activity_type=activity_type, object_uri=GROUP)
    activity['object'] = obj

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert (log.result, log.exception_message) == ('ignored', GROUP_DELETED_MESSAGE)


def test_a_gone_groups_announce_is_ignored(app, db_session, monkeypatch):
    """Announce is how a Group's Create arrives."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()
    _gone(w)

    dispatch(inbox_activity(w.community, activity_type='Announce', object_uri=f'https://{PEER}/activities/create/9'))

    log = ActivityPubLog.query.one()
    assert (log.result, log.exception_message) == ('ignored', GROUP_DELETED_MESSAGE)


def test_a_live_groups_accept_still_reaches_its_arm(app, db_session, monkeypatch):
    """Control for the Announce/Accept/Reject check above: a live Group passes it."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()
    activity = inbox_activity(w.community, activity_type='Accept', object_uri=GROUP)
    activity['object'] = {'id': f'https://{PEER}/follows/1'}

    dispatch(activity)

    assert ActivityPubLog.query.one().exception_message == 'Accept object is not an activity with a type'


def test_a_delete_from_a_live_group_naming_something_else_is_still_unexpected(app, db_session, monkeypatch):
    """Control for the self-Delete check: only a Delete naming the Group itself is one."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()

    dispatch(inbox_activity(w.community, activity_type='Delete', object_uri=f'https://{PEER}/post/1'))

    log = ActivityPubLog.query.one()
    assert (log.result, log.exception_message) == ('failure', 'Unexpected activity from Group')


# --- the inbox gate: a Group's self-Delete is not mistaken for an unknown account's ---

@pytest.fixture
def dispatched(app, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    monkeypatch.setitem(app.config, 'DEBUG', True)
    calls = []
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request', lambda *a, **kw: calls.append(a))
    return calls


def test_a_signed_self_delete_from_a_known_group_reaches_the_dispatcher(app, signing_peer, redis_double, dispatched):
    """shared_inbox used to look a self-Delete's actor up as a User only, so a Group's own Delete was logged
    'Does not exist here' and dropped before its signature was even checked."""
    group = make_community('memes', host=PEER)
    group.ap_id = f'memes@{PEER}'
    group.ap_fetched_at = utcnow()
    group.private_key, group.public_key = a_keypair()
    db.session.commit()
    activity = inbox_activity(group, activity_type='Delete', object_uri=group.ap_profile_id)

    with app.test_client() as client:
        response = signed_inbox_post(client, activity, group)

    assert response.status_code == 200
    assert [call[0]['id'] for call in dispatched] == [activity['id']]
    assert ActivityPubLog.query.count() == 0


def test_a_self_delete_from_an_unknown_actor_is_still_dropped_before_the_signature_check(
        app, signing_peer, redis_double, dispatched):
    ghost = f'https://{PEER}/c/ghost'
    body = json.dumps({'id': f'{ghost}/delete/1', 'type': 'Delete', 'actor': ghost, 'object': ghost}).encode('utf8')

    with app.test_client() as client:
        response = client.post('/inbox', data=body, content_type='application/activity+json',
                               headers={'Host': app.config['SERVER_NAME'], **unsigned_but_precheck_clean_headers(body)})

    assert response.status_code == 200
    assert dispatched == []
    assert ActivityPubLog.query.one().exception_message == 'Does not exist here'


# --- read-only: every write path asks Community.is_gone() ---

def test_the_write_predicates_refuse_a_gone_community(app, db_session):
    w = _world()
    assert can_create_post(w.member, w.community) is True
    assert can_create_post_reply(w.member, w.community) is True
    assert can_upvote(w.member, w.community) is True
    assert can_downvote(w.member, w.community) is True

    _gone(w)

    assert can_create_post(w.member, w.community) is False
    assert can_create_post_reply(w.member, w.community) is False
    assert can_upvote(w.member, w.community) is False
    assert can_downvote(w.member, w.community) is False


def test_a_web_post_is_refused(app, db_session):
    w = _world()
    _gone(w)

    with web_ctx(app, w.member):
        with pytest.raises(Exception, match='You are not permitted to make posts in this community'):
            shared_make_post(_web_form(), w.community, POST_TYPE_ARTICLE, SRC_WEB)

    assert db.session.query(Post).count() == 1


def test_an_api_post_is_refused(app, db_session):
    w = _world()
    _gone(w)

    with pytest.raises(Exception, match='You are not permitted to make posts in this community'):
        shared_make_post(_api_input(), w.community, POST_TYPE_ARTICLE, SRC_API, auth=bearer(w.member))

    assert db.session.query(Post).count() == 1


def test_a_web_reply_is_refused(app, db_session):
    w = _world()
    _gone(w)
    form = SimpleNamespace(body=_Field('hello'), notify_author=_Field(True), language_id=_Field(None),
                           distinguished=_Field(False))

    with web_ctx(app, w.member):
        with pytest.raises(Exception, match='You are not permitted to comment in this community'):
            make_reply(form, w.post, None, SRC_WEB)

    assert db.session.query(PostReply).count() == 0


def test_an_api_reply_is_refused(app, db_session):
    w = _world()
    _gone(w)

    with pytest.raises(Exception, match='You are not permitted to comment in this community'):
        make_reply({'body': 'hello', 'notify_author': True, 'language_id': None}, w.post, None, SRC_API,
                   auth=bearer(w.member))

    assert db.session.query(PostReply).count() == 0


def test_a_web_vote_is_refused(app, db_session):
    w = _world()
    _gone(w)

    with web_ctx(app, w.member):
        result = vote_for_post(w.post.id, 'upvote', True, None, SRC_WEB)

    assert result.status_code == 200
    assert db.session.query(PostVote).count() == 0


def test_an_api_vote_is_refused(app, db_session):
    w = _world()
    _gone(w)

    assert vote_for_post(w.post.id, 'upvote', True, None, SRC_API, auth=bearer(w.member)) == w.member.id
    assert db.session.query(PostVote).count() == 0


def _create(w, **object_fields):
    return {'id': f'https://{PEER}/activities/create/{uuid.uuid4().hex}', 'type': 'Create',
            'actor': w.alice.ap_profile_id,
            'object': {'id': f'https://{PEER}/objects/{uuid.uuid4().hex}', 'type': 'Note',
                       'attributedTo': w.alice.ap_profile_id, 'content': '<p>hello</p>', **object_fields}}


@pytest.mark.parametrize('in_reply_to,message', [(False, 'User cannot create post in Community'),
                                                 (True, 'User cannot create reply in Community')],
                         ids=['post', 'reply'])
def test_an_inbox_post_or_reply_is_refused(app, db_session, monkeypatch, in_reply_to, message):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()
    _gone(w)
    activity = _create(w, inReplyTo=w.post.ap_id) if in_reply_to else _create(w)

    process_new_content(w.alice, w.community, False, activity, False)

    log = ActivityPubLog.query.one()
    assert (log.result, log.exception_message) == ('failure', message)
    assert db.session.query(Post).count() == 1
    assert db.session.query(PostReply).count() == 0


def test_an_inbox_vote_is_refused(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()
    _gone(w)
    like = {'id': f'https://{PEER}/activities/like/1', 'type': 'Like', 'actor': w.alice.ap_profile_id,
            'object': w.post.ap_id}

    process_upvote(w.alice, False, like, False)

    log = ActivityPubLog.query.one()
    assert (log.result, log.exception_message) == ('ignored', 'Cannot upvote this')
    assert db.session.query(PostVote).count() == 0


@pytest.mark.parametrize('gone,status', [(False, 'joined'), (True, 'community not found')], ids=['live', 'gone'])
def test_a_web_join_of_a_gone_community_finds_no_community(app, db_session, gone, status):
    """do_subscribe treats a gone Group as it treats a banned one. admin_preload returns the outcome instead of
    flashing; the dormant instance keeps the live control from sending its Follow."""
    w = _world()
    w.peer.dormant = True
    db.session.commit()
    if gone:
        _gone(w)
    joiner = make_user(None, 'joiner', local=True)
    joiner_id, community_id = joiner.id, w.community.id

    result = do_subscribe(f'memes@{PEER}', joiner_id, admin_preload=True)

    assert result['status'] == status
    db.session.expire_all()
    assert db.session.query(CommunityMember).filter_by(user_id=joiner_id,
                                                       community_id=community_id).count() == (0 if gone else 1)


# --- nothing is delivered to it ---

def test_a_post_is_delivered_to_a_live_remote_group(db_session, http_mock):
    s = _seed_for_post(local_community=False, with_keys=True)
    route = _post_remote_inbox(s, http_mock)

    _send_post(s.post)

    assert route.call_count == 1


def test_a_post_is_not_delivered_to_a_gone_group(db_session):
    """Without a keyed author, a delivery that did happen would leave a failure row: the witness is that none
    exists."""
    s = _seed_for_post(local_community=False)
    s.community.instance_id = make_instance(PEER, software='lemmy').id
    s.community.ap_inbox_url = POST_INBOX
    s.community.ap_deleted_at = utcnow()
    db.session.commit()

    _send_post(s.post)

    assert ActivityPubLog.query.filter_by(direction='out').count() == 0


def test_a_reply_is_delivered_to_a_live_remote_group(db_session, http_mock):
    s = _seed_for_reply(local_community=False, with_keys=True)
    route = _reply_remote_inbox(s, http_mock)

    _send_reply(s)

    assert route.call_count == 1


def test_a_reply_is_not_delivered_to_a_gone_group(db_session):
    s = _seed_for_reply(local_community=False)
    _deliverable_for_reply(s)
    s.community.ap_deleted_at = utcnow()
    db.session.commit()

    _send_reply(s)

    assert ActivityPubLog.query.filter_by(direction='out').count() == 0


def test_a_delete_is_delivered_to_a_live_remote_group(db_session, http_mock):
    s = _seed_for_delete(local_community=False, with_keys=True)
    _deliverable_for_delete(s)
    route = http_mock.post(DELETE_INBOX).respond(200, json={})

    delete_post(None, s.user.id, s.post.id)

    assert route.call_count == 1


def test_a_delete_is_not_delivered_to_a_gone_group(db_session):
    s = _seed_for_delete(local_community=False)
    _deliverable_for_delete(s)
    s.community.ap_deleted_at = utcnow()
    db.session.commit()

    delete_post(None, s.user.id, s.post.id)

    assert ActivityPubLog.query.filter_by(direction='out').count() == 0


# --- the page: content readable, notice shown ---

@pytest.mark.parametrize('gone', [False, True], ids=['live', 'gone'])
def test_the_community_page_lists_its_posts_and_says_when_the_group_was_deleted(app, db_session, gone):
    w = _world()
    w.site.private_instance = False
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.commit()
    if gone:
        _gone(w)

    with patch('app.community.routes.flash') as flashed:
        with patch('app.community.routes.render_template', return_value='rendered') as render:
            response = app.test_client().get(f'/c/memes@{PEER}')

    assert response.status_code == 200
    assert [post.id for post in render.call_args.kwargs['posts'].items] == [w.post.id]
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert ('deleted on its home server' in messages) is gone
```

In `tests/test_api_routes_community.py`, change its `app.models` import (lines 12-13) to

```python
from app.models import (Community, CommunityFlair, CommunityMember, Feed,
                        FeedMember, InstanceBan, Language, Site, utcnow)
```

and add this method to `class TestJoiningAndLeaving`, directly after `test_joining_a_community_nobody_holds`:

```python
    def test_joining_a_community_deleted_on_its_home_server_is_refused(self, env):
        """Interop C1 item 10: a remote Group verified deleted at its origin is read-only, joins included."""
        gone = make_community('gonegroup', host='peer.example')
        gone.ap_id = 'gonegroup@peer.example'
        gone.ap_deleted_at = utcnow()
        db.session.commit()

        response = env.client.post('/api/alpha/community/follow',
                                   headers=auth(env.stranger),
                                   json={'community_id': gone.id,
                                         'follow': True})

        assert response.status_code == 400
        assert response.get_json()['message'] == 'This community was deleted on its home server'
        assert CommunityMember.query.filter_by(
            user_id=env.stranger.id,
            community_id=gone.id).count() == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_ap_interop_group_delete.py tests/test_api_routes_community.py -q`
Expected: FAIL at collection of `tests/test_ap_interop_group_delete.py` with `ImportError: cannot import name 'GROUP_DELETED_MESSAGE' from 'app.activitypub.util'`. In `tests/test_api_routes_community.py`, `test_joining_a_community_deleted_on_its_home_server_is_refused` fails with `assert 200 == 400`; every other test there passes.

- [ ] **Step 3: Implement**

(a) `app/models.py`, in `class Community`, directly after `is_local`:

```python
    def is_local(self):
        return self.ap_id is None or is_local_url(self.profile_id())

    def is_gone(self) -> bool:
        """Interop C1 item 10: a remote Group whose home server deleted it, verified there (delete_remote_group).
        Read-only from then on: its posts and comments stay readable and searchable, but nothing new is posted,
        replied, voted or joined there, and nothing is delivered to it. There is no undelete."""
        return self.ap_deleted_at is not None
```

(b) `app/utils.py`. In both `can_downvote` (:2975) and `can_upvote` (:3019), change

```python
    if user is None or community is None or user.banned or user.bot:
        return False
```

to

```python
    if user is None or community is None or user.banned or user.bot or community.is_gone():
        return False
```

In both `can_create_post` (:3064) and `can_create_post_reply` (:3111), change

```python
    if content.banned:
        return False
```

to

```python
    if content.banned or content.is_gone():   # interop C1 item 10: a Group deleted on its home server is read-only
        return False
```

(c) `app/activitypub/util.py`. Add `community_membership` to the `from app.utils import ...` list. Change U:46

```python
    moderating_communities, get_task_session, is_video_hosting_site, opengraph_parse, mastodon_extra_field_link, \
```

to

```python
    moderating_communities, community_membership, get_task_session, is_video_hosting_site, opengraph_parse, \
    mastodon_extra_field_link, \
```

Replace the head and the private arm of `_origin_refusal_of_delete`:

```python
def _origin_refusal_of_delete(object_id: str, label: str) -> Union[str, None]:
```

becomes

```python
def _origin_refusal_of_delete(object_id: str, label: str, tombstone_required: bool = False) -> Union[str, None]:
```

append this paragraph to its docstring, before the closing `"""`:

```python

    `tombstone_required` holds the object to that private rule whatever is stored here: a remote Group's Delete of
    itself (interop C1 item 10, owner ruling) counts only on a 410 or a Tombstone, since a 404 does not prove a
    Group deleted either.
```

and change

```python
    private = _stored_private(object_id)
```

to

```python
    private = tombstone_required or _stored_private(object_id)
```

and

```python
        if status == 404:
            return f'{label}: origin 404 does not prove a private object deleted'
```

to

```python
        if status == 404:
            return f"{label}: origin 404 does not prove {'it' if tombstone_required else 'a private object'} deleted"
```

In `_verify_announced_delete`, change

```python
    if _may_remove_from(_id_of(inner.get('actor')), community) and not _stored_elsewhere(object_id, community):
        return None, ANNOUNCED_DELETE_DEFERRED
```

to

```python
    # interop C1 item 10: a Delete of a stored Group is never deferred (apply_delayed_delete knows only posts and
    # comments) and never needs this check: the dispatcher asks may_delete_group, then the origin (410/Tombstone)
    if db.session.query(Community.id).filter_by(ap_profile_id=object_id.lower()).first() is not None:
        return inner, None
    if _may_remove_from(_id_of(inner.get('actor')), community) and not _stored_elsewhere(object_id, community):
        return None, ANNOUNCED_DELETE_DEFERRED
```

Directly after `_origin_refusal_of_delete`, add:

```python
GROUP_DELETED_MESSAGE = 'Community was deleted on its home server'


def may_delete_group(actor_id, community: Community) -> bool:
    """Who may delete a remote Group (interop C1 item 10, spec Amendment A): the Group itself, one of its stored
    moderators or owners, or an admin of the instance hosting it -- the security track's Group-host rule,
    _may_remove_from. Lemmy sends a community's deletion with a moderator as the actor. Lookup only: an actor not
    stored here is never fetched. Authority alone never deletes: delete_remote_group still asks the origin."""
    if not isinstance(actor_id, str) or not actor_id.strip():
        return False
    if actor_id.strip().lower() == community.profile_id():
        return True
    return _may_remove_from(actor_id, community)


def mark_community_gone(community: Community) -> None:
    """Record that a remote Group's home server deleted it (interop C1 item 10); the caller verified it there.

    Its posts and comments stay. Local members are unsubscribed WITHOUT an Undo Follow -- there is no Group left to
    tell -- and their pending join requests go with them. Rows of remote users (moderators read from the Group's
    moderators collection) are the Group's own record, not local subscriptions, and are kept."""
    community.ap_deleted_at = utcnow()
    local_members = db.session.query(User).join(CommunityMember, CommunityMember.user_id == User.id).filter(
        CommunityMember.community_id == community.id, User.ap_id.is_(None)).all()
    local_ids = [member.id for member in local_members]
    db.session.query(CommunityMember).filter(CommunityMember.community_id == community.id,
                                             CommunityMember.user_id.in_(local_ids)).delete(synchronize_session=False)
    db.session.query(CommunityJoinRequest).filter(CommunityJoinRequest.community_id == community.id,
                                                  CommunityJoinRequest.user_id.in_(local_ids)).delete(
        synchronize_session=False)
    community.subscriptions_count = max(0, (community.subscriptions_count or 0) - len(local_ids))
    db.session.commit()
    for member in local_members:
        cache.delete_memoized(community_membership, member, community)
        cache.delete_memoized(joined_communities, member.id)
        cache.delete_memoized(moderating_communities, member.id)


def delete_remote_group(community: Community, activity_id: str, saved_json) -> None:
    """A Delete of a remote Group (interop C1 item 10) by an actor may_delete_group accepts. Applied only once the Group's origin no longer serves it:
    one capped fetch, and only a 410 or a Tombstone counts (_origin_refusal_of_delete, tombstone_required). A 404,
    a Group still served or an unreachable origin is logged and changes nothing. No retry (D775)."""
    if community.is_local():
        log_incoming_ap(activity_id, APLOG_DELETE, APLOG_FAILURE, saved_json, 'Delete of a local community is not accepted')
        return
    refusal = _origin_refusal_of_delete(community.ap_profile_id, 'Group Delete', tombstone_required=True)
    if refusal is not None:
        log_incoming_ap(activity_id, APLOG_DELETE, APLOG_FAILURE, saved_json, refusal)
        return
    mark_community_gone(community)
    log_incoming_ap(activity_id, APLOG_DELETE, APLOG_SUCCESS, saved_json)
```

(d) `app/activitypub/routes.py`. In the `from app.activitypub.util import ...` block (R:24-36), change the last line

```python
    announce_target_uri, undo_boost, LD_SIGNED_LOG_PREFIX, current_ld_signed
```

to

```python
    announce_target_uri, undo_boost, LD_SIGNED_LOG_PREFIX, current_ld_signed, delete_remote_group, \
    GROUP_DELETED_MESSAGE, may_delete_group
```

In `shared_inbox` (R:794-803), change

```python
        account_deletion = True
        actor = db.session.query(User).filter_by(ap_profile_id=request_json['actor'].lower()).first()
        if not actor:
            log_incoming_ap(id, APLOG_DELETE, APLOG_IGNORED, saved_json, 'Does not exist here')
            return '', 200
```

to

```python
        account_deletion = True
        actor = db.session.query(User).filter_by(ap_profile_id=request_json['actor'].lower()).first()
        if not actor:
            # interop C1 item 10: a known Group deleting itself goes to the dispatcher, which verifies it at the origin
            actor = db.session.query(Community).filter_by(ap_profile_id=request_json['actor'].lower()).first()
            account_deletion = False
            if not actor:
                log_incoming_ap(id, APLOG_DELETE, APLOG_IGNORED, saved_json, 'Does not exist here')
                return '', 200
```

In `process_inbox_request`, in the Announce/Accept/Reject arm (R:1029-1037), directly after

```python
                    if not community and not feed and not user:
                        aplog_type = {'Announce': APLOG_ANNOUNCE, 'Accept': APLOG_ACCEPT, 'Reject': APLOG_REJECT}[request_json['type']]
                        log_incoming_ap(id, aplog_type, APLOG_FAILURE, saved_json, 'Actor was not a user, feed or a community')
                        return
```

add, at the same indentation:

```python
                    if community is not None and community.is_gone():   # interop C1 item 10: no undelete path
                        log_incoming_ap(id, APLOG_NOTYPE, APLOG_IGNORED, saved_json, GROUP_DELETED_MESSAGE)
                        return
```

In the Community-actor branch (R:1043-1044), change

```python
                    elif actor and isinstance(actor, Community):  # Process a few activities from NodeBB and a.gup.pe
                        if request_json['type'] == 'Add' or request_json['type'] == 'Remove':
```

to

```python
                    elif actor and isinstance(actor, Community):  # Process a few activities from NodeBB and a.gup.pe
                        if actor.is_gone():   # interop C1 item 10: no undelete path, so nothing more from it is processed
                            log_incoming_ap(id, APLOG_NOTYPE, APLOG_IGNORED, saved_json, GROUP_DELETED_MESSAGE)
                            return
                        if request_json['type'] == 'Delete' and _id_of(request_json['object']).lower() == actor.profile_id():
                            delete_remote_group(actor, id, saved_json)   # interop C1 item 10: verified at the origin first
                            return
                        if request_json['type'] == 'Add' or request_json['type'] == 'Remove':
```

(If Task 3 has already added its `object['id'] == signer` check to this branch, these two blocks still go first.)

In the Delete arm (R:1609), directly before

```python
                    to_delete = find_liked_object(ap_id)  # Just for Posts and Replies (User deletes go through process_delete_request())
```

add:

```python
                    # interop C1 item 10 (Amendment A): a Group's moderator or an admin of its host may delete it too
                    # (Lemmy's shape); the Group's own Delete is handled in the Community-actor branch above
                    target_group = session.query(Community).filter_by(ap_profile_id=ap_id.lower()).first()
                    if target_group is not None:
                        if target_group.is_gone():
                            log_incoming_ap(id, APLOG_DELETE, APLOG_IGNORED, saved_json, GROUP_DELETED_MESSAGE)
                        elif may_delete_group(user.ap_profile_id, target_group):
                            delete_remote_group(target_group, id, saved_json)   # still verified at the origin
                        else:
                            log_incoming_ap(id, APLOG_DELETE, APLOG_FAILURE, saved_json, 'Delete of a Group is '
                                            'accepted only from the Group, its moderators or an admin of its host')
                        return
```

`user` here is the Delete's actor: the signer, or for an Announced Delete the inner actor the S4 check vouched for.

(e) `app/community/routes.py`. In `do_subscribe` (:1003), change

```python
                    if community is not None and community.banned:
                        community = None
```

to

```python
                    if community is not None and (community.banned or community.is_gone()):   # interop C1 item 10
                        community = None
```

In `show_community` (:755-756), change

```python
        if is_dead:
            flash(_("This instance no longer online, so posts and comments will only be visible locally"), "warning")
```

to

```python
        if is_dead:
            flash(_("This instance no longer online, so posts and comments will only be visible locally"), "warning")
        if community.is_gone():   # interop C1 item 10
            flash(_('This community was deleted on its home server. Its posts and comments can still be read here, '
                    'but nothing new can be added.'), 'warning')
```

(f) `app/api/alpha/utils/community.py`, in `post_community_follow` (:260-261), change

```python
        if user_banned_from_community(user.id, community.id):  # D995
            raise Exception('You are banned from this community')
```

to

```python
        if user_banned_from_community(user.id, community.id):  # D995
            raise Exception('You are banned from this community')
        if community.is_gone():   # interop C1 item 10: a Group deleted on its home server takes no new members
            raise Exception('This community was deleted on its home server')
```

(g) Delivery. In `app/shared/tasks/pages.py:send_post` (:176) and `app/shared/tasks/notes.py:send_reply` (:116), change

```python
    if community.local_only or community.private:
        return
```

to

```python
    if community.local_only or community.private or community.is_gone():   # interop C1 item 10: nothing goes to a gone Group
        return
```

In `app/shared/tasks/deletes.py:delete_object` (:148), change

```python
    if community.private:
        return
```

to

```python
    if community.private or community.is_gone():   # interop C1 item 10: nothing goes to a gone Group
        return
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_ap_interop_group_delete.py tests/test_api_routes_community.py tests/test_ap_security_announce.py tests/test_inbox_dispatch_preamble.py tests/test_inbox_gate_signatures.py tests/test_inbox_gate_dispatch.py tests/test_ap_delete_request.py tests/test_shared_tasks_send_post.py tests/test_shared_tasks_send_reply.py tests/test_shared_tasks_deletes.py tests/test_community_show.py tests/test_shared_post_interactions.py tests/test_shared_post_make.py tests/test_shared_reply_make.py -q`
Expected: PASS. `tests/test_ap_security_announce.py`'s `PRIVATE_404` rows still read `... origin 404 does not prove a private object deleted`: the message changes only under `tombstone_required`.

- [ ] **Step 5: Commit**

```bash
git add app/models.py app/utils.py app/activitypub/util.py app/activitypub/routes.py app/community/routes.py \
    app/api/alpha/utils/community.py app/shared/tasks/pages.py app/shared/tasks/notes.py app/shared/tasks/deletes.py \
    tests/test_ap_interop_group_delete.py tests/test_api_routes_community.py
git commit -m "feat: a remote Group that deletes itself is marked deleted and becomes read-only

Nothing wrote Community.ap_deleted_at: a Group's self-Delete was logged
'Does not exist here' at the inbox gate, or 'Unexpected activity from
Group'. It now reaches the dispatcher, which refetches the Group once and
applies the Delete only on a 410 or a Tombstone. The Group is marked, its
local members are unsubscribed without an Undo Follow, and
Community.is_gone() refuses every post, reply, vote and join, through the
web, the API and the inbox. Nothing is delivered to it, its content stays
readable, the page says it was deleted, and anything later from it is
ignored. A moderator of the Group or an admin of its host may send the
Delete too (Lemmy's shape), still verified at the origin; anyone else is
refused.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Task gate**

Run, in order:
1. `./run_tests.sh tests/test_ap_interop_group_delete.py tests/test_api_routes_community.py tests/test_ap_security_announce.py --cov=app --cov-branch --cov-report=json -q`
2. `python3 tests/check_changed_line_coverage.py coverage.json HEAD~1 --branches app/`

Expected: all three test modules pass (read the passed count); 100% changed-line and branch coverage. `tests/test_ap_security_announce.py` covers the not-a-Group arm of the new `_verify_announced_delete` check. Each new condition has both arms in the new module: a live and a gone Group for the dispatcher checks, the page notice, `do_subscribe` and the three delivery guards; the predicate test for the four `can_*` changes; a refused and a live API join (`TestJoiningAndLeaving.test_joining`) for `post_community_follow`.

---

### Task 9: Person `followers` and `following`, count-only (item 6)

**Files:**
- Modify: `app/activitypub/util.py` (add `user_followers_count` and `user_following_count` directly after `community_members`, U:61-65)
- Modify: `app/activitypub/routes.py`:
  - the import block from `app.activitypub.util` (R:23-35): add the two counters;
  - `user_profile`'s Person document (R:486-506): add `followers` and `following`;
  - add `local_actor_user` and `count_only_collection` directly after `resolve_remote_handle` (R:559-584);
  - replace `user_followers` (R:2547-2579);
  - add `user_following` directly after `user_followers`.
- Modify: `tests/test_ap_collections.py:543-628` (`test_a_users_followers_are_listed` pinned the old item list; the community test's docstring cites it)
- Test: `tests/test_ap_interop_person_follow_collections.py` (new)

**Interfaces:**
- Consumes: `ap_media_type() -> str` (Task 2), which sets the content type of every AP route; `AP_CACHE_COLLECTION` and `default_context()` (routes.py); factories `make_user`, `make_follow(local_user, remote_user, is_accepted=True, is_inward=False)`, `make_community`, `make_community_member`, `make_instance`; `tests/test_actor_profiles.py:seed_actors`; `tests/test_ap_content_objects.py:ap_get`, `AP_ACCEPT`.
- Produces:
  - `user_followers_count(user_id: int) -> int` and `user_following_count(user_id: int) -> int` in `app/activitypub/util.py`;
  - `local_actor_user(actor: str) -> User` in `app/activitypub/routes.py`: the active local user named `actor` (case-insensitive), else `abort(410)` for a deleted account and `abort(404)` for a banned, unknown or remote one. Task 10 uses it;
  - `count_only_collection(collection_id: str, total: int) -> Response` in `app/activitypub/routes.py`;
  - route `GET /u/<actor>/following`, endpoint `activitypub.user_following`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ap_interop_person_follow_collections.py`:

```python
"""Interop C1 item 6 (spec 2026-10-11): a Person names its `followers` and `following` collections, and both are
count-only (owner ruling, as D175 serves a community's followers): a real `totalItems`, an empty `orderedItems`, so
no follower or followed account is disclosed.

The follow table stores both directions under one pair of columns. A remote follower of a local user is an inward
row (local_user_id = the followed user); a local follower is the follower's own outward row (remote_user_id = the
followed user). The old route counted every row with local_user_id = the user, so the people a user follows were
listed as their followers.
"""
import pytest

from app import db
from app.models import Site, UserBlock
from tests.factories import make_community, make_community_member, make_follow, make_instance, make_user
from tests.test_actor_profiles import seed_actors
from tests.test_ap_content_objects import ap_get

SERVER = 'https://test.piefed.local'
SUFFIXES = ['followers', 'following']


def _alice():
    seed_actors()
    return make_user(None, 'alice', local=True)


def test_the_person_document_names_both_collections(app, db_session):
    _alice()

    response = ap_get(app, '/u/alice')

    assert response.status_code == 200
    assert response.json['followers'] == f'{SERVER}/u/alice/followers'
    assert response.json['following'] == f'{SERVER}/u/alice/following'


def test_followers_counts_accepted_local_and_remote_followers_and_lists_none(app, db_session):
    alice = _alice()
    remote = make_instance('remote.example')
    bob = make_user(remote, 'bob')                  # remote, follows alice: inward row
    carol = make_user(None, 'carol', local=True)    # local, follows alice: carol's outward row
    dave = make_user(remote, 'dave')                # remote, request pending
    erin = make_user(make_instance('other.example'), 'erin')   # alice follows erin: NOT a follower
    make_follow(alice, bob, is_accepted=True, is_inward=True)
    make_follow(carol, alice, is_accepted=True, is_inward=False)
    make_follow(alice, dave, is_accepted=None, is_inward=True)
    make_follow(alice, erin, is_accepted=True, is_inward=False)

    response = ap_get(app, '/u/alice/followers')

    assert response.status_code == 200
    assert response.json['type'] == 'OrderedCollection'
    assert response.json['id'] == f'{SERVER}/u/alice/followers'
    assert response.json['totalItems'] == 2
    assert response.json['orderedItems'] == []
    assert 'items' not in response.json
    body = response.get_data(as_text=True)
    assert bob.ap_public_url not in body and '/u/carol' not in body and erin.ap_public_url not in body


def test_followers_skips_a_banned_or_deleted_follower_and_one_the_user_blocked(app, db_session):
    alice = _alice()
    remote = make_instance('remote.example')
    banned, deleted, blocked = (make_user(remote, name) for name in ('banned', 'deleted', 'blocked'))
    banned.banned = True
    deleted.deleted = True
    db.session.add(UserBlock(blocker_id=alice.id, blocked_id=blocked.id))   # D182: the owner's block hides
    db.session.commit()
    for follower in (banned, deleted, blocked):
        make_follow(alice, follower, is_accepted=True, is_inward=True)

    assert ap_get(app, '/u/alice/followers').json['totalItems'] == 0


def test_following_counts_users_followed_and_communities_joined(app, db_session):
    alice = _alice()
    remote = make_instance('remote.example')
    bob = make_user(remote, 'bob')                  # followed, accepted
    carol = make_user(None, 'carol', local=True)    # followed, accepted (local follow)
    dave = make_user(remote, 'dave')                # followed, still pending
    erin = make_user(remote, 'erin')                # follows alice: NOT followed by her
    make_follow(alice, bob, is_accepted=True, is_inward=False)
    make_follow(alice, carol, is_accepted=True, is_inward=False)
    make_follow(alice, dave, is_accepted=None, is_inward=False)
    make_follow(alice, erin, is_accepted=True, is_inward=True)
    make_community_member(alice, make_community(name='books'))
    banned_from = make_community_member(alice, make_community(name='films'))
    banned_from.is_banned = True
    db.session.commit()

    response = ap_get(app, '/u/alice/following')

    assert response.status_code == 200
    assert response.json['type'] == 'OrderedCollection'
    assert response.json['id'] == f'{SERVER}/u/alice/following'
    assert response.json['totalItems'] == 3     # bob, carol, books
    assert response.json['orderedItems'] == []
    body = response.get_data(as_text=True)
    assert bob.ap_public_url not in body and '/c/books' not in body


@pytest.mark.parametrize('suffix', SUFFIXES)
def test_both_collections_use_the_collection_cache_policy(app, db_session, suffix):
    _alice()

    response = ap_get(app, f'/u/alice/{suffix}')

    assert response.status_code == 200
    assert response.content_type == 'application/activity+json'
    assert response.headers['Cache-Control'] == 'public, max-age=60'
    assert response.headers['Vary'] == 'Accept, Accept-Encoding'   # Flask-Compress appends Accept-Encoding


@pytest.mark.parametrize('suffix', SUFFIXES)
def test_the_lookup_ignores_case_and_answers_the_canonical_id(app, db_session, suffix):
    _alice()

    assert ap_get(app, f'/u/ALICE/{suffix}').json['id'] == f'{SERVER}/u/alice/{suffix}'


@pytest.mark.parametrize('suffix', SUFFIXES)
def test_a_browser_is_sent_to_the_profile(app, db_session, suffix):
    """D177, as every other collection."""
    _alice()
    with app.test_client() as client:
        response = client.get(f'/u/alice/{suffix}', headers={'Accept': 'text/html'})

    assert (response.status_code, response.headers['Location']) == (302, '/u/alice')


@pytest.mark.parametrize('suffix', SUFFIXES)
def test_a_deleted_user_is_gone(app, db_session, suffix):
    """S3.2, as user_profile: account deletion sets both flags, and `deleted` decides."""
    alice = _alice()
    alice.deleted = True
    alice.banned = True
    db.session.commit()

    response = ap_get(app, f'/u/alice/{suffix}')

    assert response.status_code == 410
    assert response.headers['Cache-Control'] == 'no-store'


@pytest.mark.parametrize('suffix', SUFFIXES)
def test_a_banned_user_stays_404_so_the_ban_is_reversible(app, db_session, suffix):
    alice = _alice()
    alice.banned = True
    db.session.commit()

    assert ap_get(app, f'/u/alice/{suffix}').status_code == 404


@pytest.mark.parametrize('path', ['/u/nosuch/{}', '/u/bob/{}', '/u/bob@remote.example/{}'])
@pytest.mark.parametrize('suffix', SUFFIXES)
def test_an_unknown_or_remote_user_is_404(app, db_session, path, suffix):
    """Only a local user's collections are served here; a remote one lives on its own server."""
    _alice()
    make_user(make_instance('remote.example'), 'bob')

    assert ap_get(app, path.format(suffix)).status_code == 404


@pytest.mark.parametrize('suffix', SUFFIXES)
def test_secure_mode_refuses_an_unsigned_fetch(app, db_session, suffix):
    """The app-wide secure-mode hook covers the new route too: it is a collection, not an actor document, so it is
    refused rather than served in minimal form."""
    _alice()
    db.session.get(Site, 1).require_signed_fetch = True
    db.session.commit()

    response = ap_get(app, f'/u/alice/{suffix}')

    assert response.status_code == 401
```

In `tests/test_ap_collections.py`, replace `test_a_users_followers_are_listed` (from its `def` line through `assert bob.ap_public_url in response.json['items']`) with:

```python
def test_a_users_followers_are_counted_not_listed(app, db_session):
    """Interop C1 item 6 (owner ruling): count-only, like D175. The collection
    used to list every follower's URL; it now carries the real count and no
    items. Its `id` is still `User.followers_url()`, so `ap_followers_url` is
    honoured when set.
    """
    site, instance = seed_actors()
    alice = make_user(instance, 'alice', local=True)
    alice.ap_followers_url = 'https://test.piefed.local/u/alice/followers'
    bob = make_user(instance, 'bob')
    db.session.commit()
    _follow(alice, bob)

    response = collection_get(app, '/u/alice/followers')

    assert response.status_code == 200
    assert response.json['type'] == 'OrderedCollection'
    assert response.json['id'] == 'https://test.piefed.local/u/alice/followers'
    assert response.json['totalItems'] == 1
    assert response.json['orderedItems'] == []
```

In the docstring of `test_the_community_followers_items_list_is_always_empty`, replace:

```
    `test_the_feed_followers_items_list_is_always_empty`. `user_followers` --
    the third -- populates its items with real follower URLs and filters
    blocked and unaccepted follows, pinned by `test_a_users_followers_are_listed`.
    So both siblings are exercised here and the two-of-three pattern is proved
    in the suite, not merely read off the source. Two of three contradict
    themselves; one does not.
```

with:

```
    `test_the_feed_followers_items_list_is_always_empty`. `user_followers` --
    the third -- is count-only too since interop C1 item 6, pinned by
    `test_a_users_followers_are_counted_not_listed`.
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_ap_interop_person_follow_collections.py tests/test_ap_collections.py -q`
Expected: FAIL.
- `test_the_person_document_names_both_collections`: `KeyError: 'followers'`.
- `test_followers_counts_...`: `assert 'Collection' == 'OrderedCollection'`.
- every `following` row: 404 where 200, 302 or 410 is expected (there is no route).
- `test_followers_skips_...`: `assert 2 == 0` (banned and deleted followers are counted today).
- `test_a_deleted_user_is_gone[followers]`: `assert 200 == 410` (the old lookup filters only `banned`).
- `test_the_lookup_ignores_case...[followers]`: 404 (the old lookup matches `user_name` exactly).
- `test_a_users_followers_are_counted_not_listed`: `assert 'Collection' == 'OrderedCollection'`.
The banned, unknown/remote, cache and secure-mode rows for `followers` pass already; they guard the rewrite.

- [ ] **Step 3: Implement**

In `app/activitypub/util.py`, directly after `community_members`, add:

```python
def user_followers_count(user_id: int) -> int:
    """How many accepted followers local user `user_id` has, local and remote (interop C1 item 6).

    A remote follower's row is inward (local_user_id is this user); a local follower's row is the follower's own
    outward follow (remote_user_id is this user). A pending or refused follow, a banned or deleted follower and a
    follower this user has blocked (D182) do not count."""
    sql = ('SELECT COUNT(*) FROM user_follower uf '
           'JOIN "user" u ON u.id = CASE WHEN uf.is_inward THEN uf.remote_user_id ELSE uf.local_user_id END '
           'WHERE uf.is_accepted IS TRUE AND u.banned IS FALSE AND u.deleted IS FALSE '
           'AND ((uf.is_inward IS TRUE AND uf.local_user_id = :user_id) '
           'OR (uf.is_inward IS FALSE AND uf.remote_user_id = :user_id)) '
           'AND NOT EXISTS (SELECT 1 FROM user_block ub WHERE ub.blocker_id = :user_id AND ub.blocked_id = u.id)')
    return db.session.execute(text(sql), {'user_id': user_id}).scalar()


def user_following_count(user_id: int) -> int:
    """How many accounts local user `user_id` follows (interop C1 item 6): accepted follows of users, plus
    communities joined -- a join is a Follow of a Group actor, so peers count it too. A followed account that is
    banned or deleted, a membership the user is banned from, and a banned community do not count."""
    sql = ('SELECT (SELECT COUNT(*) FROM user_follower uf JOIN "user" u ON u.id = uf.remote_user_id '
           'WHERE uf.local_user_id = :user_id AND uf.is_inward IS FALSE AND uf.is_accepted IS TRUE '
           'AND u.banned IS FALSE AND u.deleted IS FALSE) + '
           '(SELECT COUNT(*) FROM community_member cm JOIN community c ON c.id = cm.community_id '
           'WHERE cm.user_id = :user_id AND cm.is_banned IS FALSE AND c.banned IS NOT TRUE)')
    return db.session.execute(text(sql), {'user_id': user_id}).scalar()
```

In `app/activitypub/routes.py`, in the `from app.activitypub.util import ...` block, change the line

```python
    announce_target_uri, undo_boost, LD_SIGNED_LOG_PREFIX, current_ld_signed
```

to

```python
    announce_target_uri, undo_boost, LD_SIGNED_LOG_PREFIX, current_ld_signed, user_followers_count, user_following_count
```

In `user_profile`'s `actor_data`, change:

```python
                          "inbox": f"{user.public_url()}/inbox",
                          "outbox": f"{user.public_url()}/outbox",
```

to:

```python
                          "inbox": f"{user.public_url()}/inbox",
                          "outbox": f"{user.public_url()}/outbox",
                          "followers": user.followers_url(),
                          "following": f"{user.public_url()}/following",
```

Directly after `resolve_remote_handle` (before `@bp.route('/u/<actor>/outbox', ...)`), add:

```python
def local_actor_user(actor: str) -> User:
    """The local user an actor collection (`/u/<actor>/...`) belongs to, else abort with what user_profile answers
    an ActivityPub request: 410 for a deleted account (S3.2), 404 for a banned, unknown or remote one. The name is
    matched case-insensitively, as user_profile matches it."""
    name = actor.strip().lower()
    user = User.query.filter(func.lower(User.user_name) == name, User.ap_id.is_(None),
                             User.deleted.is_(False), User.banned.is_(False)).first()
    if user is not None:
        return user
    gone = User.query.filter(func.lower(User.user_name) == name, User.ap_id.is_(None),
                             User.deleted.is_(True)).first()
    abort(410 if gone is not None else 404)


def count_only_collection(collection_id: str, total: int):
    """A user's followers or following collection (interop C1 item 6, owner ruling): the real count and no items, as
    D175 serves a community's followers, so no follower or followed account is disclosed."""
    resp = jsonify({"@context": default_context(), "type": "OrderedCollection", "id": collection_id,
                    "totalItems": total, "orderedItems": []})
    resp.content_type = ap_media_type()
    resp.headers.set('Cache-Control', AP_CACHE_COLLECTION)
    resp.headers.set('Vary', 'Accept')
    return resp
```

Replace the whole `user_followers` function (from `@bp.route('/u/<actor>/followers', methods=['GET'])` through its final `abort(404)`, as Task 2 left it) with:

```python
@bp.route('/u/<actor>/followers', methods=['GET'])
def user_followers(actor):
    actor = actor.strip()
    # D177, as user_outbox above
    if not is_activitypub_request():
        return redirect(url_for('activitypub.user_profile', actor=actor))
    user = local_actor_user(actor)
    return count_only_collection(user.followers_url(), user_followers_count(user.id))


@bp.route('/u/<actor>/following', methods=['GET'])
def user_following(actor):
    actor = actor.strip()
    # D177, as user_outbox above
    if not is_activitypub_request():
        return redirect(url_for('activitypub.user_profile', actor=actor))
    user = local_actor_user(actor)
    return count_only_collection(f"{user.public_url()}/following", user_following_count(user.id))
```

The `UserBlock` import in routes.py stays: `process_inbox_request` still uses it.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_ap_interop_person_follow_collections.py tests/test_ap_collections.py tests/test_actor_profiles.py tests/test_ap_security_tombstones.py tests/test_ap_security_secure_mode.py -q`
Expected: PASS. The existing `user_followers` rows in `tests/test_ap_collections.py` (no followers URL, banned, unaccepted, blocked by the owner, blocker of the owner, cache and Vary, unknown) stay green unchanged: their counts are the same under the new definition.

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/util.py app/activitypub/routes.py tests/test_ap_interop_person_follow_collections.py tests/test_ap_collections.py
git commit -m "feat: a Person names count-only followers and following collections

The Person document now carries followers and following. Both
collections answer an OrderedCollection with the real count and no
items (owner ruling, as D175). Followers counts accepted local and
remote followers; the old list also took in the accounts the user
follows, stored under the same column. Following counts accepted
follows plus communities joined. Both routes refuse a deleted user
with 410 and a banned one with 404, like the Person document.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Task gate**

Run, in order:
1. `./run_tests.sh tests/test_ap_interop_person_follow_collections.py tests/test_ap_collections.py tests/test_actor_profiles.py tests/test_ap_security_tombstones.py tests/test_ap_security_secure_mode.py --cov=app --cov-branch --cov-report=json -q`
2. `python3 tests/check_changed_line_coverage.py coverage.json HEAD~1 --branches app/`

Expected: all pass; 100% of changed lines and branches covered.

---

### Task 10: Person outbox lists the user's public posts and comments (item 7)

**Files:**
- Create: `migrations/versions/f4b8d2a6c1e7_user_outbox_indexes.py`
- Modify: `app/models.py` (`Post.__table_args__`, models.py:2583-2645; `PostReply.__table_args__`, models.py:4030-4051): declare the two new indexes
- Modify: `app/activitypub/util.py` (add `create_activity_of` directly before `post_to_activity`, U:107; `post_to_activity` uses it)
- Modify: `app/activitypub/routes.py`:
  - imports: `from sqlalchemy import desc, or_, text, func` (R:16), the `app.activitypub.util` block (R:23-35), the `app.visibility` line (R:62);
  - replace `user_outbox` (R:587-598); add `user_outbox_rows` and `user_outbox_page` directly before it.
- Modify: `tests/test_coverage_tail_234.py:151-189` (`TestTheUserOutbox` pinned the fixed empty collection and the no-lookup disclosure decision)
- Test: `tests/test_ap_interop_person_outbox.py` (new), `tests/test_migration_user_outbox_indexes.py` (new)

**Interfaces:**
- Consumes:
  - `local_actor_user(actor: str) -> User` (Task 9, routes.py): the active local user, else `abort(410)` (deleted) or `abort(404)`;
  - `ap_media_type() -> str` (Task 2);
  - migration revision `e2a7c4f9d1b3` (Task 6, `report.ap_id`), the `down_revision` here;
  - `paged_outbox(outbox_id, total, items_from)`, `OUTBOX_PAGE_SIZE` (routes.py), `visible_to_clause(model, viewer_id)` and `listable_clause(model)` (`app/visibility.py`), `post_to_page`, `comment_model_to_json` (util.py);
  - factories `make_user`, `make_community`, `make_post`, `make_post_reply`, `make_chat_message`, `make_instance`; `seed_actors`, `ap_get`.
- Produces:
  - `create_activity_of(obj: Post | PostReply) -> dict` in `app/activitypub/util.py`: the Create envelope the community outbox has always used;
  - `user_outbox_rows(user: User)` in routes.py: a subquery of `(kind, id, posted_at)`, `kind` being `'post'` or `'reply'`;
  - `user_outbox_page(rows, offset: int, limit: int) -> list[dict]` in routes.py;
  - indexes `ix_post_user_posted` on `post (user_id, posted_at DESC) WHERE deleted = false` and `ix_post_reply_user_posted` on `post_reply (user_id, posted_at DESC) WHERE deleted = false`; revision `f4b8d2a6c1e7`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ap_interop_person_outbox.py`:

```python
"""Interop C1 item 7 (spec 2026-10-11): `/u/<name>/outbox` lists the user's own posts and comments, newest first,
as the Create activities the community outbox already wraps them in, 50 to a page.

Only what a logged-out viewer could see is listed: public, not deleted, published, and not in a private or
local-only community. Private messages are ChatMessage rows, a separate table, so they can never appear. The gates
are item 6's: a deleted user is 410, a banned, unknown or remote one 404.
"""
from datetime import timedelta

import pytest
from sqlalchemy import event

from app import db
from app.activitypub import routes as activitypub_routes
from app.constants import POST_STATUS_REVIEWING, VISIBILITY_FOLLOWERS, VISIBILITY_UNLISTED
from app.models import Community, Post, User, utcnow
from tests.factories import make_chat_message, make_community, make_instance, make_post, make_post_reply, make_user
from tests.test_actor_profiles import seed_actors
from tests.test_ap_content_objects import ap_get

SERVER = 'https://test.piefed.local'
OUTBOX = f'{SERVER}/u/alice/outbox'


def _world():
    seed_actors()
    return make_user(None, 'alice', local=True), make_community(name='books')


def _post(community, author, minutes_ago, **columns):
    post = make_post(community, author, None)
    post.ap_id = f'{SERVER}/post/{post.id}'
    post.posted_at = utcnow() - timedelta(minutes=minutes_ago)
    for name, value in columns.items():
        setattr(post, name, value)
    db.session.commit()
    return post


def _reply(post, author, minutes_ago, **columns):
    reply = make_post_reply(post, author)
    reply.ap_id = f'{SERVER}/comment/{reply.id}'
    reply.posted_at = utcnow() - timedelta(minutes=minutes_ago)
    for name, value in columns.items():
        setattr(reply, name, value)
    db.session.commit()
    return reply


def _hidden_community(flag):
    community = make_community(name=f'hidden_{flag}')
    setattr(community, flag, True)
    db.session.commit()
    return community


def _someone_elses_post(community, deleted=False):
    return _post(community, make_user(None, 'other', local=True), 500, deleted=deleted)


def _object_ids(document):
    return [item['object']['id'] for item in document['orderedItems']]


def test_posts_and_comments_are_listed_newest_first_as_creates(app, db_session):
    author, community = _world()
    old = _post(community, author, 30)
    middle = _reply(old, author, 20)
    new = _post(community, author, 10)

    response = ap_get(app, '/u/alice/outbox')

    assert response.status_code == 200
    assert response.json['type'] == 'OrderedCollection'
    assert response.json['id'] == OUTBOX
    assert response.json['totalItems'] == 3
    assert _object_ids(response.json) == [new.ap_id, middle.ap_id, old.ap_id]
    items = response.json['orderedItems']
    assert [item['type'] for item in items] == ['Create', 'Create', 'Create']
    assert [item['object']['type'] for item in items] == ['Page', 'Note', 'Page']
    assert {item['actor'] for item in items} == {f'{SERVER}/u/alice'}
    assert {item['audience'] for item in items} == {community.public_url()}
    assert all(item['id'].startswith(f'{SERVER}/activities/create/') for item in items)


def test_the_outbox_is_paged_with_the_first_page_inline(app, db_session, monkeypatch):
    monkeypatch.setattr(activitypub_routes, 'OUTBOX_PAGE_SIZE', 2)
    author, community = _world()
    oldest = _post(community, author, 30)
    middle = _reply(oldest, author, 20)
    newest = _post(community, author, 10)

    root = ap_get(app, '/u/alice/outbox').json
    first = ap_get(app, '/u/alice/outbox?page=1').json
    second = ap_get(app, '/u/alice/outbox?page=2').json

    assert (root['totalItems'], root['first'], root['last']) == (3, f'{OUTBOX}?page=1', f'{OUTBOX}?page=2')
    assert _object_ids(root) == [newest.ap_id, middle.ap_id]
    assert _object_ids(first) == [newest.ap_id, middle.ap_id]
    assert (first['type'], first['partOf'], first['next']) == ('OrderedCollectionPage', OUTBOX, f'{OUTBOX}?page=2')
    assert _object_ids(second) == [oldest.ap_id]
    assert (second['totalItems'], second['prev']) == (3, f'{OUTBOX}?page=1')
    assert 'next' not in second


# Each builder makes one object of alice's that a logged-out viewer may not see, and returns its ap_id.
EXCLUDED = {
    'followers-only post': lambda author, community: _post(community, author, 5, visibility=VISIBILITY_FOLLOWERS).ap_id,
    'unlisted post': lambda author, community: _post(community, author, 5, visibility=VISIBILITY_UNLISTED).ap_id,
    'deleted post': lambda author, community: _post(community, author, 5, deleted=True).ap_id,
    'post under review': lambda author, community: _post(community, author, 5, status=POST_STATUS_REVIEWING).ap_id,
    'post in a private community': lambda author, community: _post(_hidden_community('private'), author, 5).ap_id,
    'post in a local-only community': lambda author, community: _post(_hidden_community('local_only'), author, 5).ap_id,
    'deleted comment': lambda author, community: _reply(_someone_elses_post(community), author, 5, deleted=True).ap_id,
    'followers-only comment': lambda author, community: _reply(_someone_elses_post(community), author, 5,
                                                              visibility=VISIBILITY_FOLLOWERS).ap_id,
    'comment in a private community': lambda author, community: _reply(
        _someone_elses_post(_hidden_community('private')), author, 5).ap_id,
    'comment in a local-only community': lambda author, community: _reply(
        _someone_elses_post(_hidden_community('local_only')), author, 5).ap_id,
    'comment under a deleted post': lambda author, community: _reply(
        _someone_elses_post(community, deleted=True), author, 5).ap_id,
}


@pytest.mark.parametrize('label', EXCLUDED)
def test_what_a_logged_out_viewer_cannot_see_is_not_listed(app, db_session, label):
    author, community = _world()
    shown = _post(community, author, 60)
    hidden = EXCLUDED[label](author, community)

    response = ap_get(app, '/u/alice/outbox')

    assert response.json['totalItems'] == 1
    assert _object_ids(response.json) == [shown.ap_id]
    assert hidden not in response.get_data(as_text=True)


def test_a_private_message_is_never_listed(app, db_session):
    author, community = _world()
    message_id = make_chat_message(author, make_user(None, 'bob', local=True), f'{SERVER}/private_message/1').ap_id

    response = ap_get(app, '/u/alice/outbox')

    assert response.json['totalItems'] == 0
    assert message_id not in response.get_data(as_text=True)


def test_the_lookup_ignores_case_and_answers_the_canonical_id(app, db_session):
    _world()

    assert ap_get(app, '/u/ALICE/outbox').json['id'] == OUTBOX


def test_the_outbox_uses_the_collection_cache_policy(app, db_session):
    _world()

    response = ap_get(app, '/u/alice/outbox')

    assert response.content_type == 'application/activity+json'
    assert response.headers['Cache-Control'] == 'public, max-age=60'
    assert response.headers['Vary'] == 'Accept, Accept-Encoding'


def test_a_deleted_user_is_gone(app, db_session):
    author, community = _world()
    author.deleted = True
    author.banned = True
    db.session.commit()

    assert ap_get(app, '/u/alice/outbox').status_code == 410


def test_a_banned_user_stays_404(app, db_session):
    author, community = _world()
    author.banned = True
    db.session.commit()

    assert ap_get(app, '/u/alice/outbox').status_code == 404


@pytest.mark.parametrize('path', ['/u/nosuch/outbox', '/u/bob/outbox', '/u/bob@remote.example/outbox'])
def test_an_unknown_or_remote_user_outbox_is_not_served(app, db_session, path):
    _world()
    bob = make_user(make_instance('remote.example'), 'bob')
    _post(make_community(name='elsewhere'), bob, 5)

    assert ap_get(app, path).status_code == 404


def _statements_for(app, path):
    """How many SQL statements one request runs. The session is closed first, so both measurements start from an
    empty identity map and neither is cheaper for objects an earlier request left loaded."""
    db.session.close()
    seen = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(db.engine, 'before_cursor_execute', record)
    try:
        response = ap_get(app, path)
    finally:
        event.remove(db.engine, 'before_cursor_execute', record)
    assert response.status_code == 200
    return len(seen)


def test_a_page_costs_the_same_queries_however_much_the_user_has_written(app, db_session, monkeypatch):
    """The page query is bounded: LIMIT/OFFSET over one union, then one load per kind. Page 1 shows the same two
    objects both times; only the older history behind it grows."""
    monkeypatch.setattr(activitypub_routes, 'OUTBOX_PAGE_SIZE', 2)
    author, community = _world()
    author_id, community_id = author.id, community.id
    base_id = _someone_elses_post(community).id

    def write(count, start):
        author, community = db.session.get(User, author_id), db.session.get(Community, community_id)
        for i in range(count):
            if i % 2:
                _reply(db.session.get(Post, base_id), author, start + i)
            else:
                _post(community, author, start + i)

    write(4, 0)
    small = _statements_for(app, '/u/alice/outbox?page=1')
    write(20, 100)
    large = _statements_for(app, '/u/alice/outbox?page=1')

    assert small == large
```

Create `tests/test_migration_user_outbox_indexes.py`:

```python
"""Interop C1 item 7 migration f4b8d2a6c1e7: the (user_id, posted_at DESC) indexes the user outbox pages with, and
that it reverses cleanly."""
import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text

from app import db

MIGRATION = Path(__file__).resolve().parent.parent / 'migrations' / 'versions' / 'f4b8d2a6c1e7_user_outbox_indexes.py'
INDEXES = {'post': 'ix_post_user_posted', 'post_reply': 'ix_post_reply_user_posted'}


def load_migration():
    spec = importlib.util.spec_from_file_location('user_outbox_indexes_migration', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def index(connection, table, name):
    return next((found for found in inspect(connection).get_indexes(table) if found['name'] == name), None)


def test_the_revision_chain():
    migration = load_migration()
    assert (migration.revision, migration.down_revision) == ('f4b8d2a6c1e7', 'e2a7c4f9d1b3')


def test_both_indexes_lead_with_the_author(app, db_session):
    with db.engine.connect() as connection:
        for table, name in INDEXES.items():
            found = index(connection, table, name)
            assert found is not None
            assert found['column_names'][0] == 'user_id'


def test_downgrade_then_upgrade_restores_both_indexes(app, db_session):
    db.session.close()    # nothing of the session's may still hold a lock on post or post_reply
    migration = load_migration()
    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
                for table, name in INDEXES.items():
                    assert index(connection, table, name) is None
                migration.upgrade()
            for table, name in INDEXES.items():
                assert index(connection, table, name) is not None
        finally:
            transaction.rollback()
```

In `tests/test_coverage_tail_234.py`, replace the whole `TestTheUserOutbox` class (R:151 to the line before `class TestACommunityThisInstanceDoesNotHave:`) with:

```python
class TestTheUserOutbox:
    """Every peer polls `/u/<actor>/outbox`. Since interop C1 item 7 it lists the user's public
    posts and comments; an account with none answers an empty collection. The owner is looked
    up, as the Person document is: `/u/<name>` already tells anyone whether the name exists, so
    answering an unknown name with 404 here discloses nothing new.
    """

    def test_it_answers_an_empty_ordered_collection(self, env):
        author = make_user(env.baseline.instance_local, 'outboxowner', local=True)
        db.session.commit()

        response = env.client.get(f'/u/{author.user_name}/outbox', headers={'Accept': 'application/activity+json'})

        assert response.status_code == 200
        body = response.get_json()
        assert body['type'] == 'OrderedCollection'
        assert body['totalItems'] == 0
        assert body['orderedItems'] == []

    def test_it_is_served_as_activitypub_and_varies_on_accept(self, env):
        """The three headers. `Vary: Accept` is what stops a cache handing an HTML answer
        to a peer asking for JSON, and this endpoint is cacheable for the collection
        max-age (D180, fixed by owner ruling: ten seconds before)."""
        make_user(env.baseline.instance_local, 'anybody', local=True)
        db.session.commit()

        response = env.client.get('/u/anybody/outbox', headers={'Accept': 'application/activity+json'})

        assert response.content_type == 'application/activity+json'
        # `Accept` as a whole entry: Flask appends `Accept-Encoding` to Vary on its own, so
        # a substring test passes even with the header this route sets removed.
        assert 'Accept' in [part.strip()
                            for part in response.headers['Vary'].split(',')]
        assert response.headers['Cache-Control'] == 'public, max-age=60'

    def test_an_unknown_actor_is_404(self, env):
        response = env.client.get('/u/nobody-at-all/outbox', headers={'Accept': 'application/activity+json'})

        assert response.status_code == 404
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_ap_interop_person_outbox.py tests/test_migration_user_outbox_indexes.py tests/test_coverage_tail_234.py -q`
Expected: FAIL.
- The listing, paging, exclusion and query-count rows: `assert 0 == 3` (or `== 1`) on `totalItems`; the outbox is always empty.
- `test_a_deleted_user_is_gone`: `assert 200 == 410`; the banned, unknown and remote rows and `test_an_unknown_actor_is_404`: `assert 200 == 404` (the route never looks the user up).
- `test_the_lookup_ignores_case...`: the id is `.../u/ALICE/outbox`.
- The three migration tests: `FileNotFoundError` for `f4b8d2a6c1e7_user_outbox_indexes.py`.
`test_a_private_message_is_never_listed` and the cache-policy row pass already; they guard the rewrite.

- [ ] **Step 3: Implement**

Create `migrations/versions/f4b8d2a6c1e7_user_outbox_indexes.py`:

```python
"""user outbox indexes (interop C1 item 7): a user's posts and comments, newest first

Revision ID: f4b8d2a6c1e7
Revises: e2a7c4f9d1b3
"""
import sqlalchemy as sa
from alembic import op

revision = 'f4b8d2a6c1e7'
down_revision = 'e2a7c4f9d1b3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index('ix_post_user_posted', 'post', ['user_id', sa.literal_column('posted_at DESC')], unique=False,
                    postgresql_where=sa.text('deleted = false'))
    op.create_index('ix_post_reply_user_posted', 'post_reply', ['user_id', sa.literal_column('posted_at DESC')],
                    unique=False, postgresql_where=sa.text('deleted = false'))


def downgrade():
    op.drop_index('ix_post_reply_user_posted', table_name='post_reply')
    op.drop_index('ix_post_user_posted', table_name='post')
```

In `app/models.py`, at the end of `Post.__table_args__`, change:

```python
        Index(
            'ix_post_feed_community',
            'community_id', desc('sticky'), desc('posted_at'),
            postgresql_where=db.text('deleted = false AND status > 0')
        ),
    )
```

to:

```python
        Index(
            'ix_post_feed_community',
            'community_id', desc('sticky'), desc('posted_at'),
            postgresql_where=db.text('deleted = false AND status > 0')
        ),
        Index(   # interop C1 item 7: the user outbox, newest first
            'ix_post_user_posted',
            'user_id', desc('posted_at'),
            postgresql_where=db.text('deleted = false')
        ),
    )
```

At the end of `PostReply.__table_args__`, change:

```python
        db.Index(
            'ix_post_reply_community_posted_bot',
            'community_id', 'posted_at',
            postgresql_where=db.text('from_bot = false')
        )
    )
```

to:

```python
        db.Index(
            'ix_post_reply_community_posted_bot',
            'community_id', 'posted_at',
            postgresql_where=db.text('from_bot = false')
        ),
        db.Index(   # interop C1 item 7: the user outbox, newest first
            'ix_post_reply_user_posted',
            'user_id', desc('posted_at'),
            postgresql_where=db.text('deleted = false')
        ),
    )
```

In `app/activitypub/util.py`, directly before `def post_to_activity`, add:

```python
def create_activity_of(obj: Post | PostReply) -> dict:
    """The Create a post or comment is wrapped in where an outbox lists it: inside the community outbox's Announce
    (post_to_activity) and in its author's outbox (interop C1 item 7). A local object has no stored Create id, so
    one is made up, as the community outbox always has."""
    community = obj.community
    create_id = obj.ap_create_id if obj.ap_create_id else f"{current_app.config['SERVER_URL']}/activities/create/{gibberish(15)}"
    return {
        "id": create_id,
        "actor": obj.author.public_url(),
        "to": [
            "https://www.w3.org/ns/activitystreams#Public"
        ],
        "object": post_to_page(obj) if isinstance(obj, Post) else comment_model_to_json(obj),
        "cc": [
            community.public_url()
        ],
        "type": "Create",
        "audience": community.public_url()
    }
```

and change `post_to_activity` from:

```python
def post_to_activity(post: Post, community: Community):
    # local PieFed posts do not have a create or announce id
    create_id = post.ap_create_id if post.ap_create_id else f"{current_app.config['SERVER_URL']}/activities/create/{gibberish(15)}"
    announce_id = post.ap_announce_id if post.ap_announce_id else f"{current_app.config['SERVER_URL']}/activities/announce/{gibberish(15)}"
    activity_data = {
        "actor": community.public_url(),
        "to": [
            "https://www.w3.org/ns/activitystreams#Public"
        ],
        "object": {
            "id": create_id,
            "actor": post.author.public_url(),
            "to": [
                "https://www.w3.org/ns/activitystreams#Public"
            ],
            "object": post_to_page(post),
            "cc": [
                community.public_url()
            ],
            "type": "Create",
            "audience": community.public_url()
        },
        "cc": [
```

to:

```python
def post_to_activity(post: Post, community: Community):
    # local PieFed posts do not have a create or announce id
    announce_id = post.ap_announce_id if post.ap_announce_id else f"{current_app.config['SERVER_URL']}/activities/announce/{gibberish(15)}"
    activity_data = {
        "actor": community.public_url(),
        "to": [
            "https://www.w3.org/ns/activitystreams#Public"
        ],
        "object": create_activity_of(post),   # its only caller, community_outbox, passes post.community
        "cc": [
```

In `app/activitypub/routes.py`:

- change `from sqlalchemy import desc, or_, text, func` to `from sqlalchemy import desc, or_, text, func, select, union_all, literal_column`;
- in the `from app.activitypub.util import ...` block, change the last line (as Task 9 left it)

```python
    announce_target_uri, undo_boost, LD_SIGNED_LOG_PREFIX, current_ld_signed, user_followers_count, user_following_count
```

  to

```python
    announce_target_uri, undo_boost, LD_SIGNED_LOG_PREFIX, current_ld_signed, user_followers_count, user_following_count, \
    create_activity_of
```

- change `from app.visibility import OPEN_VISIBILITIES, followers_only_addressing, is_open, listable_clause, post_title_for` to `from app.visibility import OPEN_VISIBILITIES, followers_only_addressing, is_open, listable_clause, post_title_for, visible_to_clause`.

Replace the whole `user_outbox` function (from `@bp.route('/u/<actor>/outbox', methods=['GET'])` through `return resp`, as Task 2 left it) with:

```python
def user_outbox_rows(user: User):
    """One row per post or comment of local `user` that a logged-out viewer may see (interop C1 item 7), as
    (kind, id, posted_at) with kind 'post' or 'reply'. A subquery: the count and each page select from it.

    Seen by no viewer (visible_to_clause), narrowed to public (listable_clause), because post_to_page and
    comment_model_to_json address everything to Public and an unlisted object must not be republished as public.
    Not deleted, not under review, and not in a private or local-only community; a comment's post must not be
    deleted either. A private message is a ChatMessage, never one of these rows. The author is already known to be
    active: local_actor_user refused a banned or deleted one."""
    posts = (select(literal_column("'post'").label('kind'), Post.id.label('id'), Post.posted_at.label('posted_at'))
             .join(Community, Community.id == Post.community_id)
             .where(Post.user_id == user.id, Post.deleted == False, Post.status > POST_STATUS_REVIEWING,
                    visible_to_clause(Post, None), listable_clause(Post),
                    Community.private.isnot(True), Community.local_only.isnot(True)))
    replies = (select(literal_column("'reply'").label('kind'), PostReply.id.label('id'),
                      PostReply.posted_at.label('posted_at'))
               .join(Post, Post.id == PostReply.post_id)
               .join(Community, Community.id == PostReply.community_id)
               .where(PostReply.user_id == user.id, PostReply.deleted == False, Post.deleted == False,
                      visible_to_clause(PostReply, None), listable_clause(PostReply),
                      Community.private.isnot(True), Community.local_only.isnot(True)))
    return union_all(posts, replies).subquery()


def user_outbox_page(rows, offset: int, limit: int) -> list[dict]:
    """One page of a user outbox, newest first, as Create activities. One bounded query picks the page's rows out of
    the union (the (user_id, posted_at DESC) indexes serve both halves), then one query per kind loads them."""
    page = db.session.execute(select(rows.c.kind, rows.c.id)
                              .order_by(desc(rows.c.posted_at).nulls_last(), desc(rows.c.id), rows.c.kind)
                              .offset(offset).limit(limit)).all()
    posts = {post.id: post for post in Post.query.filter(Post.id.in_([row.id for row in page if row.kind == 'post']))}
    replies = {reply.id: reply for reply in
               PostReply.query.filter(PostReply.id.in_([row.id for row in page if row.kind == 'reply']))}
    return [create_activity_of(posts[row.id] if row.kind == 'post' else replies[row.id]) for row in page]


@bp.route('/u/<actor>/outbox', methods=['GET'])
def user_outbox(actor):
    # D177 (owner ruling): a browser asking for a collection is sent to the page of the actor it belongs to,
    # as the actor endpoints negotiate
    if not is_activitypub_request():
        return redirect(url_for('activitypub.user_profile', actor=actor))
    user = local_actor_user(actor)
    rows = user_outbox_rows(user)
    total = db.session.execute(select(func.count()).select_from(rows)).scalar()
    outbox = paged_outbox(f"{user.public_url()}/outbox", total,
                          lambda offset, limit: user_outbox_page(rows, offset, limit))
    resp = jsonify(outbox)
    resp.content_type = ap_media_type()
    resp.headers.set('Cache-Control', AP_CACHE_COLLECTION)
    resp.headers.set('Vary', 'Accept')
    return resp
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_ap_interop_person_outbox.py tests/test_migration_user_outbox_indexes.py tests/test_coverage_tail_234.py tests/test_ap_collections.py tests/test_ap_security_community_outbox.py tests/test_coverage_tail_264.py -q`
Expected: PASS. `test_a_user_outbox_pages_its_empty_collection` and the community outbox rows in `tests/test_ap_collections.py` stay green unchanged: `post_to_activity`'s output is the same document.

- [ ] **Step 5: Commit**

```bash
git add migrations/versions/f4b8d2a6c1e7_user_outbox_indexes.py app/models.py app/activitypub/util.py app/activitypub/routes.py tests/test_ap_interop_person_outbox.py tests/test_migration_user_outbox_indexes.py tests/test_coverage_tail_234.py
git commit -m "feat: a user's outbox lists their public posts and comments

/u/<name>/outbox was a fixed empty collection. It now pages the user's
own posts and comments, newest first, as the Create activities the
community outbox already uses. Only what a logged-out viewer may see
is listed: public, undeleted, published, and outside private and
local-only communities. A page is one LIMIT/OFFSET query over a union,
served by new (user_id, posted_at DESC) indexes. A deleted user is 410
and a banned, unknown or remote one 404, as for the Person document.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Task gate**

Run, in order:
1. `./run_tests.sh tests/test_ap_interop_person_outbox.py tests/test_migration_user_outbox_indexes.py tests/test_coverage_tail_234.py tests/test_ap_collections.py tests/test_ap_security_community_outbox.py tests/test_coverage_tail_264.py --cov=app --cov-branch --cov-report=json -q`
2. `python3 tests/check_changed_line_coverage.py coverage.json HEAD~1 --branches app/`

Expected: all pass; 100% of changed lines and branches covered.

---

### Task 11: HTML pages advertise their ActivityPub id (item 1)

**Files:**
- Modify: `app/utils.py` (add `ap_alternate_url` directly after `render_template`, which ends at line 111, before `request_etag_matches` at line 114)
- Modify: `app/templates/base.html:89-91` (after the `fediverse:creator` block)
- Modify: `app/templates/themes/dillo/base.html:26-28` (after the RSS block)
- Modify: `app/post/routes.py:342-346` (the fep-2345 `creator` block) and `:418` (the `render_template('post/post.html', ...)` kwargs line `event=event, creator=creator,`)
- Modify: `app/user/routes.py:206-226` (`show_profile`'s final `return render_template('user/show_profile.html', ...)`) and its `from app.utils import` block (line 40)
- Modify: `app/community/routes.py:769-799` (`show_community`'s `resp = make_response(render_template('community/community.html', ...))` and the header block after it) and its import at line 80
- Modify: `app/feed/routes.py:593-594` (`show_feed`'s `render_template('feed/show_feed.html', ...)`) and its imports
- Modify: `tests/test_post_show.py:963-986` (the `POST_TYPE_IMAGE` row of `test_the_creator_tag_is_offered_for_written_posts`)
- Test: `tests/test_ap_interop_html_discovery.py` (new)

**Interfaces:**
- Consumes: `User.public_url()`, `Community.public_url()`, `Feed.public_url()`, `.is_local()` and `.ap_profile_id` on all three (`app/models.py`); `Post.profile_id()` (`app/models.py:3644`).
- Produces:
  - `ap_alternate_url(actor) -> str` in `app/utils.py`. `actor` is a `User`, `Community` or `Feed`. Returns `actor.public_url()` when `actor.is_local()`, else `actor.ap_profile_id`.
  - Render kwarg `ap_alternate: str | None`, read by `base.html` and `themes/dillo/base.html`. Passed by `show_post`, `show_profile`, `show_community` and `show_feed`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ap_interop_html_discovery.py`:

```python
"""ActivityPub interop C1, item 1: HTML discovery.

Every HTML page whose subject has an ActivityPub id -- a post, a user profile
(local or remote), a community, a feed -- names that id in its head with
`<link rel="alternate" type="application/activity+json" href="...">`, in the
default theme's base.html and in the dillo theme's. The user profile and the
community page also send it as an HTTP `Link` header, with `Vary: Accept`, as
the post page has since D1086. The alternate is the id itself: our
public_url() for a local actor, the actor's ap_profile_id for a remote one --
never a remote ap_public_url, which Mastodon sets to its HTML page.

A private or local-only community still emits the alternate: the page is the
same for every viewer (no ETag change), and the ActivityPub side already
answers 403 for both (community_profile).

`fediverse:creator` (fep-2345) is also offered for a plain Page post -- image
and video, the types the old list left out -- when its author is local.

Spec: docs/superpowers/specs/2026-10-11-ap-interop-c1-design.md, item 1.
"""
from unittest.mock import patch

import pytest

from app import db
from app.constants import POST_TYPE_IMAGE, POST_TYPE_VIDEO
from app.models import Feed, Instance, Language, Site
from tests.factories import (make_community, make_community_member, make_instance, make_post,
                             make_user)

pytestmark = pytest.mark.usefixtures('site')

AP_LINK = '<link rel="alternate" type="application/activity+json" href="{}"'
AP_HEADER = '<{}>; rel="alternate"; type="application/activity+json"'


def instance(domain='test.piefed.local', software='piefed'):
    """The local Instance row, created once (tests/test_post_show.py's helper)."""
    existing = Instance.query.filter_by(domain=domain).first()
    return existing if existing is not None else make_instance(domain, software=software)


def as_user(app, user):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return client


def ap_link_headers(response):
    """The ActivityPub `Link` values only: render_template sets a preload Link and after_request may add more."""
    return [link for link in response.headers.get_all('Link') if 'application/activity+json' in link]


def vary_tokens(response):
    """`Vary` split into names: a substring test would find 'Accept' inside 'Accept-Language'."""
    return [token.strip() for token in response.headers.get('Vary', '').split(',')]


def show_community(client, path):
    """GET a community page with render_template doubled; return the response and the template context."""
    with patch('app.community.routes.render_template', return_value='rendered') as render:
        response = client.get(path)
    return response, render.call_args.kwargs


@pytest.fixture
def env(app, db_session):
    """A public instance, a local viewer and author, and a local community holding one post by the author.

    Site.private_instance defaults to True, which would redirect every anonymous request before the route runs."""
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1  # make_community hardcodes user_id=1
    viewer = make_user(local, 'viewer', local=True)
    author = make_user(local, 'author', local=True)
    community = make_community('general')
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.add(Language(code='en', name='English'))
    db.session.commit()
    make_community_member(author, community)
    post = make_post(community, author, 'https://test.piefed.local/post/1', title='THETITLE')
    post.body = 'the body'
    post.body_html = '<p>the body</p>'
    db.session.commit()
    return viewer, author, community, post


# --- the post page ---------------------------------------------------------


def test_the_post_page_links_its_activitypub_id(app, env):
    viewer, author, community, post = env
    post_id, ap_id = post.id, post.profile_id()

    html = app.test_client().get(f'/post/{post_id}').get_data(as_text=True)

    assert AP_LINK.format(ap_id) in html


def test_the_dillo_theme_links_the_activitypub_id_too(app, env):
    """The dillo base template had none of the discovery links (spec item 1)."""
    viewer, author, community, post = env
    viewer.theme = 'dillo'
    db.session.commit()
    post_id, ap_id = post.id, post.profile_id()

    html = as_user(app, viewer).get(f'/post/{post_id}').get_data(as_text=True)

    assert 'themes/dillo/styles.css' in html
    assert AP_LINK.format(ap_id) in html


@pytest.mark.parametrize('post_type', [POST_TYPE_IMAGE, POST_TYPE_VIDEO])
def test_a_local_authors_plain_page_post_names_its_creator(app, env, post_type):
    viewer, author, community, post = env
    post.type = post_type
    db.session.commit()
    post_id = post.id

    with patch('app.post.routes.render_template', return_value=app.response_class('rendered')) as render:
        app.test_client().get(f'/post/{post_id}')

    assert render.call_args.kwargs['creator'] == '@author@test.piefed.local'


def test_a_remote_authors_plain_page_post_names_no_creator(app, env):
    viewer, author, community, post = env
    remote = make_user(make_instance('peer.example'), 'faraway')
    remote_post = make_post(community, remote, 'https://peer.example/post/9')
    remote_post.type = POST_TYPE_IMAGE
    db.session.commit()
    post_id = remote_post.id

    with patch('app.post.routes.render_template', return_value=app.response_class('rendered')) as render:
        app.test_client().get(f'/post/{post_id}')

    assert render.call_args.kwargs['creator'] is None


# --- the user profile ------------------------------------------------------


def test_a_local_profile_links_its_activitypub_id_in_the_page_and_the_headers(app, env):
    viewer, author, community, post = env
    expected = 'https://test.piefed.local/u/author'   # public_url(), the id user_profile serves

    response = as_user(app, viewer).get('/u/author')

    assert response.status_code == 200
    assert AP_LINK.format(expected) in response.get_data(as_text=True)
    assert ap_link_headers(response) == [AP_HEADER.format(expected)]
    assert 'Accept' in vary_tokens(response)


def test_a_remote_profile_links_the_actors_own_id_not_its_html_page(app, env):
    viewer, author, community, post = env
    remote = make_user(make_instance('peer.example'), 'wakko')
    remote.ap_public_url = 'https://peer.example/@wakko'   # the HTML page, as Mastodon publishes it
    db.session.commit()
    expected = remote.ap_profile_id                         # https://peer.example/users/wakko

    response = as_user(app, viewer).get('/u/wakko@peer.example')

    assert response.status_code == 200
    assert AP_LINK.format(expected) in response.get_data(as_text=True)
    assert ap_link_headers(response) == [AP_HEADER.format(expected)]


# --- the community page ----------------------------------------------------


def test_the_community_page_passes_and_sends_its_activitypub_id(app, env):
    viewer, author, community, post = env
    expected = 'https://test.piefed.local/c/general'

    response, context = show_community(as_user(app, viewer), '/c/general')

    assert response.status_code == 200
    assert context['ap_alternate'] == expected
    assert ap_link_headers(response) == [AP_HEADER.format(expected)]
    assert 'Accept' in vary_tokens(response)


def test_a_remote_community_page_names_the_groups_own_id(app, env):
    viewer, author, community, post = env
    other = make_instance('other.example', software='lemmy')
    remote = make_community('remote', host='other.example')
    remote.ap_id = 'remote@other.example'
    remote.instance_id = other.id
    remote.ap_public_url = 'https://other.example/groups/remote'   # an HTML page, not the id
    db.session.commit()

    response, context = show_community(as_user(app, viewer), '/c/remote@other.example')

    assert response.status_code == 200
    assert context['ap_alternate'] == 'https://other.example/c/remote'
    assert ap_link_headers(response) == [AP_HEADER.format('https://other.example/c/remote')]


def test_a_local_only_community_still_advertises_its_alternate(app, env):
    """The ActivityPub side answers 403 for it (community_profile); the page link is the same for every viewer."""
    viewer, author, community, post = env
    community.local_only = True
    db.session.commit()

    response, context = show_community(as_user(app, viewer), '/c/general')

    assert response.status_code == 200
    assert context['ap_alternate'] == 'https://test.piefed.local/c/general'
    assert ap_link_headers(response) == [AP_HEADER.format('https://test.piefed.local/c/general')]


def test_a_private_community_still_advertises_its_alternate_to_a_member(app, env):
    viewer, author, community, post = env
    community.private = True
    db.session.commit()
    make_community_member(viewer, community)   # show_community answers 403 to a non-member

    response, context = show_community(as_user(app, viewer), '/c/general')

    assert response.status_code == 200
    assert context['ap_alternate'] == 'https://test.piefed.local/c/general'


# --- the feed page ---------------------------------------------------------


def test_the_feed_page_passes_its_activitypub_id(app, env):
    viewer, author, community, post = env
    feed = Feed(user_id=viewer.id, title='news', name='news', machine_name='news', instance_id=1, public=True,
                ap_profile_id='https://test.piefed.local/f/news', ap_public_url='https://test.piefed.local/f/news')
    db.session.add(feed)
    db.session.commit()

    with patch('app.feed.routes.render_template', return_value='rendered') as render:
        response = as_user(app, viewer).get('/f/news')

    assert response.status_code == 200
    assert render.call_args.kwargs['ap_alternate'] == 'https://test.piefed.local/f/news'
```

In `tests/test_post_show.py`, the `env` author is local, so the image row now gets a creator. Replace the parametrisation and docstring of `test_the_creator_tag_is_offered_for_written_posts`:

```python
@pytest.mark.parametrize('post_type, expected', [
    (POST_TYPE_ARTICLE, True),
    (POST_TYPE_LINK, True),
    (POST_TYPE_POLL, True),
    (POST_TYPE_EVENT, True),
    (POST_TYPE_IMAGE, True),
])
def test_the_creator_tag_is_offered_for_written_posts(app, env, post_type,
                                                      expected):
    """fep-2345's `fediverse:creator` names the author to an aggregator.
    C1 item 1, spec Amendment A: every post type by a local author gets the
    tag, image posts included (they federate as a plain Page). A remote author's image post is not
    (tests/test_ap_interop_html_discovery.py)."""
```

(The body of the test is unchanged.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_ap_interop_html_discovery.py tests/test_post_show.py -q`
Expected: FAIL.
- The two post-page tests and the local profile test: `AssertionError` on `assert AP_LINK.format(...) in html`. No template emits an activity+json `<link>`.
- The remote profile test: the same `AssertionError`.
- The community and feed tests: `KeyError: 'ap_alternate'`.
- `test_a_local_authors_plain_page_post_names_its_creator[3]` and `[4]`: `assert None == '@author@test.piefed.local'`.
- `test_the_creator_tag_is_offered_for_written_posts[3-True]`: `assert (creator is not None) is expected` fails.
- `test_a_remote_authors_plain_page_post_names_no_creator` already passes. It guards the new condition.

- [ ] **Step 3: Implement**

(a) In `app/utils.py`, directly after `render_template` (before `def request_etag_matches`), add:

```python
def ap_alternate_url(actor) -> str:
    """The ActivityPub id an HTML page about `actor` (a User, Community or Feed) advertises as its alternate (C1
    item 1). A local actor's is public_url(), the id its actor document serves. A remote actor's is its own id,
    ap_profile_id -- not ap_public_url, which a peer may point at its HTML page (Mastodon's /@name)."""
    return actor.public_url() if actor.is_local() else actor.ap_profile_id
```

(b) In `app/templates/base.html`, after

```
        {% if creator -%}
            <meta name="fediverse:creator" content="{{ creator }}" />
        {% endif -%}
```

add

```
        {% if ap_alternate -%}
            <link rel="alternate" type="application/activity+json" href="{{ ap_alternate }}" />
        {% endif -%}
```

(c) In `app/templates/themes/dillo/base.html`, after

```
        {% if rss_feed -%}
            <link rel="alternate" type="application/rss+xml" title="{{ rss_feed_name }}" href="{{ rss_feed }}" />
        {% endif -%}
```

add the same three lines:

```
        {% if ap_alternate -%}
            <link rel="alternate" type="application/activity+json" href="{{ ap_alternate }}" />
        {% endif -%}
```

(d) In `app/post/routes.py`, `show_post`, change

```python
        # fep-2345 - fediverse:creator tag
        if post.type == POST_TYPE_ARTICLE or post.type == POST_TYPE_LINK or post.type == POST_TYPE_POLL or post.type == POST_TYPE_EVENT or post.is_microblog():
            creator = '@' + post.author.lemmy_link()
        else:
            creator = None
```

to

```python
        # fep-2345 - fediverse:creator tag. C1 item 1: every other type (image, video) federates as a plain Page
        # (post_to_page), and is named only when the author is ours.
        if post.type == POST_TYPE_ARTICLE or post.type == POST_TYPE_LINK or post.type == POST_TYPE_POLL or post.type == POST_TYPE_EVENT or post.is_microblog() or post.author.is_local():
            creator = '@' + post.author.lemmy_link()
        else:
            creator = None
```

In the `render_template('post/post.html', ...)` call of the same function, change the line

```python
                                   event=event, creator=creator,
```

to

```python
                                   event=event, creator=creator, ap_alternate=post.profile_id(),
```

(`profile_id()` is the post's `ap_id`, or `SERVER_URL/post/<id>` when it has none. For a remote post that is the origin's id, which our own `/post/<id>` AP route 301s to. The D1086 `Link` header below is unchanged.)

(e) In `app/user/routes.py`, add after the existing `from app.utils import user_filters_languages` line:

```python
from app.utils import ap_alternate_url
```

In `show_profile`, replace the first line of the final return

```python
    return render_template('user/show_profile.html', user=user, posts=posts, post_replies=post_replies,
```

with

```python
    ap_alternate = ap_alternate_url(user)
    response = make_response(render_template('user/show_profile.html', user=user, posts=posts, post_replies=post_replies,
```

and its last line

```python
                           bot_challenge=bot_challenge, vote_quota_used=vote_quota_used)
```

with

```python
                           bot_challenge=bot_challenge, vote_quota_used=vote_quota_used,
                           ap_alternate=ap_alternate))
    # C1 item 1: the profile advertises its ActivityPub id as the post page does (D1086). `add`, not `set`: the
    # preload Link render_template wrote must survive.
    response.headers.add('Link', f'<{ap_alternate}>; rel="alternate"; type="application/activity+json"')
    response.vary.add('Accept')
    return response
```

(`make_response` is already imported. The wrap is what the spec asks for. It also keeps the existing tests that double `render_template` with a plain string working.)

(f) In `app/community/routes.py`, change the import at line 80

```python
from app.utils import get_recipient_language, subscribed_feeds, feed_membership, is_local_microblogs
```

to

```python
from app.utils import get_recipient_language, subscribed_feeds, feed_membership, is_local_microblogs, ap_alternate_url
```

In `show_community`, directly before `resp = make_response(render_template('community/community.html', ...`, add

```python
    ap_alternate = ap_alternate_url(community)
```

In that call, change

```python
                                         canonical=community.profile_id(), can_upvote_here=can_upvote(user, community),
```

to

```python
                                         canonical=community.profile_id(), ap_alternate=ap_alternate,
                                         can_upvote_here=can_upvote(user, community),
```

and directly after the closing `))` of the call, before `if current_user.is_anonymous:`, add

```python
    # C1 item 1: as the post page (D1086). Both Vary values set below already name Accept.
    resp.headers.add('Link', f'<{ap_alternate}>; rel="alternate"; type="application/activity+json"')
```

The ETag/304 early return above is untouched: the link is the same for every viewer.

(g) In `app/feed/routes.py`, add after the existing `from app.utils import ...` block:

```python
from app.utils import ap_alternate_url
```

In `show_feed`, change

```python
                                             sort=sort, owner=owner,
```

to

```python
                                             sort=sort, owner=owner, ap_alternate=ap_alternate_url(current_feed),
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_ap_interop_html_discovery.py tests/test_post_show.py tests/test_user_profile_page.py tests/test_community_show.py tests/test_feed_reading_routes.py tests/test_actor_profiles.py tests/test_dillo_content_warning.py -q`
Expected: PASS. The existing profile, community and feed tests double `render_template` with a string or a recorder and must stay green unchanged.

- [ ] **Step 5: Commit**

```bash
git add app/utils.py app/templates/base.html app/templates/themes/dillo/base.html app/post/routes.py \
        app/user/routes.py app/community/routes.py app/feed/routes.py \
        tests/test_ap_interop_html_discovery.py tests/test_post_show.py
git commit -m "feat: post, profile, community and feed pages advertise their ActivityPub id

Every such page now carries <link rel=\"alternate\"
type=\"application/activity+json\"> to its subject's id, in both the
default and the dillo theme: public_url() for a local actor, the
actor's ap_profile_id for a remote one, the post's own id for a post.
The user profile and community page also send it as a Link header with
Vary: Accept, as the post page already did. fediverse:creator now
names the local author of an image or video post too.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Task gate**

Run, in order:
1. `./run_tests.sh tests/test_ap_interop_html_discovery.py tests/test_post_show.py tests/test_user_profile_page.py tests/test_community_show.py tests/test_feed_reading_routes.py tests/test_actor_profiles.py tests/test_dillo_content_warning.py --cov=app --cov-branch --cov-report=json -q`
2. `python3 tests/check_changed_line_coverage.py coverage.json HEAD~1 --branches app/`

Expected: all pass; 100% changed-line and branch coverage.

---

### Task 12: WebFinger reverse check, and JSON host-meta (item 9)

**Files:**
- Modify: `app/activitypub/util.py` (add `webfinger_self_href` and `canonical_handle` directly before `actor_json_to_model`, U:1403)
- Modify: `app/activitypub/actor.py` (the `from app.activitypub.util import ...` line, line 12-13; use `canonical_handle` in the webfinger arm of `create_actor_from_remote`, line 311)
- Modify: `app/user/utils.py:10` and `:175-177` (`search_for_user`), `app/community/util.py:26-27` and `:111-112` (`search_for_community`), `app/feed/util.py:11` and `:96-97` (`search_for_feed`): each asks `canonical_handle` before `actor_json_to_model`
- Modify: `app/activitypub/routes.py` (add `host_meta_json` directly after `host_meta`, R:263-270)
- Test: `tests/test_ap_interop_webfinger_reverse.py` (new)

**Interfaces:**
- Consumes: `host_of(url_string: str) -> str` (`app/activitypub/util.py:683`); `get_request_capped(uri, max_bytes: int, headers=None, max_seconds: float = 15) -> tuple[int, bytes | None]` and `FETCH_MAX_BYTES` (already imported into `app/activitypub/util.py` from `app.utils`); `actor_json_to_model(activity_json, address, server)` (unchanged).
- Produces, in `app/activitypub/util.py` (spec Amendment A: one shared helper for every WebFinger resolver; util.py because `actor.py`, `app/user/utils.py`, `app/community/util.py` and `app/feed/util.py` all import from it already, so no new import edge and no cycle):
  - `webfinger_self_href(address: str, server: str) -> str | None`: the `self` link's `href` that `acct:address@server` resolves to. It makes one GET through `get_request_capped`, with no retry and no sleep. Returns None on any failure.
  - `canonical_handle(actor_json, address: str, server: str, prefix: str = '') -> tuple[str, str] | None`: the `(address, server)` to store a WebFinger-resolved actor under, or None to refuse it. `prefix` is prepended to the name in the reverse lookup only: `'~'` for a feed, whose WebFinger resource is `acct:~name@host` (R:153-155).
- Produces, in `app/activitypub/routes.py`: the route `GET /.well-known/host-meta.json` (`host_meta_json`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ap_interop_webfinger_reverse.py`:

```python
"""ActivityPub interop C1, item 9: the WebFinger reverse check, and JSON host-meta.

A handle `name@server` resolves through `server`'s WebFinger to an actor
document. That document names its own canonical handle,
`preferredUsername@<host of its id>`.

- The canonical handle equals the looked-up one (case-insensitively): the
  actor is stored as before, with no second request.
- Otherwise ONE capped WebFinger lookup of the canonical handle must resolve
  to the same actor id. The actor is then stored under the canonical handle,
  never under the alias.
- Otherwise the actor is refused and the refusal logged.

Every WebFinger resolver asks the same helper (spec Amendment A): the webfinger
arm of create_actor_from_remote, search_for_user, search_for_community and
search_for_feed.

Before this, a handle whose server differed from the id's host was refused
outright by actor_json_to_model's host gate (D32), and a same-host alias
(`nick@host` answered by `preferredUsername: alice`) was stored under the
alias. The reverse lookup uses get_request_capped, not get_request: the
latter sleeps and retries on a transport error, which D738/D775 forbid on a
request path.

`/.well-known/host-meta.json` is the JRD twin of the XML host-meta.

Spec: docs/superpowers/specs/2026-10-11-ap-interop-c1-design.md, item 9.
"""
import httpx
import pytest
from flask import g

from app import cache, db
from app.activitypub import routes as activitypub_routes
from app.activitypub.actor import create_actor_from_remote
from app.activitypub.util import webfinger_self_href
from app.community.util import search_for_community
from app.feed.util import search_for_feed
from app.models import User
from app.user.utils import search_for_user
from tests.factories import PEER_ACTOR_PUBLIC_KEY_PEM, make_instance

pytestmark = pytest.mark.usefixtures('site')

ALICE = 'https://social.example/users/alice'


def actor_document(actor_id, name, **fields):
    document = {'@context': 'https://www.w3.org/ns/activitystreams', 'type': 'Person', 'id': actor_id,
                'preferredUsername': name, 'inbox': f'{actor_id}/inbox',
                'publicKey': {'id': f'{actor_id}#main-key', 'owner': actor_id,
                              'publicKeyPem': PEER_ACTOR_PUBLIC_KEY_PEM}}
    document.update(fields)
    return document


def serve_webfinger(http_mock, server, href):
    """`server`'s WebFinger, answering every resource with one self link to `href`."""
    return http_mock.get(f'https://{server}/.well-known/webfinger').respond(
        200, json={'links': [{'rel': 'self', 'type': 'application/activity+json', 'href': href}]})


def serve_actor(http_mock, document):
    return http_mock.get(document['id']).respond(200, json=document)


# --- the reverse check -----------------------------------------------------


def test_a_handle_that_is_already_canonical_is_stored_without_a_second_lookup(app, db_session, http_mock):
    make_instance('social.example')
    webfinger = serve_webfinger(http_mock, 'social.example', ALICE)
    serve_actor(http_mock, actor_document(ALICE, 'alice'))

    user = create_actor_from_remote('alice@social.example')

    assert user.ap_id == 'alice@social.example'
    assert webfinger.call_count == 1


def test_the_handle_comparison_ignores_case(app, db_session, http_mock):
    make_instance('social.example')
    webfinger = serve_webfinger(http_mock, 'social.example', ALICE)
    serve_actor(http_mock, actor_document(ALICE, 'Alice'))

    user = create_actor_from_remote('ALICE@Social.Example')

    assert user.ap_id == 'alice@social.example'
    assert webfinger.call_count == 1


def test_a_cross_host_handle_is_stored_under_the_canonical_one_when_it_resolves_back(app, db_session, http_mock):
    """alice@alias.example is answered by an actor on social.example. Before C1 the host gate refused it."""
    make_instance('social.example')
    alias = serve_webfinger(http_mock, 'alias.example', ALICE)
    serve_actor(http_mock, actor_document(ALICE, 'alice'))
    reverse = serve_webfinger(http_mock, 'social.example', ALICE)

    user = create_actor_from_remote('alice@alias.example')

    assert user.ap_id == 'alice@social.example'
    assert user.ap_domain == 'social.example'
    assert alias.call_count == 1 and reverse.call_count == 1
    assert reverse.calls.last.request.url.params['resource'] == 'acct:alice@social.example'
    assert db.session.query(User).filter_by(ap_id='alice@alias.example').count() == 0


def test_a_same_host_alias_is_stored_under_the_preferred_username(app, db_session, http_mock):
    make_instance('social.example')
    webfinger = serve_webfinger(http_mock, 'social.example', ALICE)
    serve_actor(http_mock, actor_document(ALICE, 'alice'))

    user = create_actor_from_remote('nick@social.example')

    assert user.ap_id == 'alice@social.example'
    assert webfinger.call_count == 2     # nick, then the reverse lookup of alice


def test_a_mismatch_the_reverse_lookup_does_not_confirm_is_refused_and_logged(app, db_session, http_mock, caplog):
    make_instance('social.example')
    serve_webfinger(http_mock, 'alias.example', ALICE)
    serve_actor(http_mock, actor_document(ALICE, 'alice'))
    serve_webfinger(http_mock, 'social.example', 'https://social.example/users/somebody-else')

    with caplog.at_level('WARNING', logger=app.logger.name):
        assert create_actor_from_remote('alice@alias.example') is None

    assert db.session.query(User).count() == 0
    assert 'WebFinger reverse check refused alice@alias.example' in caplog.text


def test_a_reverse_lookup_that_fails_refuses_the_actor(app, db_session, http_mock):
    make_instance('social.example')
    serve_webfinger(http_mock, 'alias.example', ALICE)
    serve_actor(http_mock, actor_document(ALICE, 'alice'))
    http_mock.get('https://social.example/.well-known/webfinger').respond(404)

    assert create_actor_from_remote('alice@alias.example') is None
    assert db.session.query(User).count() == 0


def test_a_document_without_a_preferred_username_skips_the_check(app, db_session, http_mock):
    """Nothing to compare: actor_json_to_model refuses it as before (D1372), and no reverse lookup is made."""
    make_instance('social.example')
    webfinger = serve_webfinger(http_mock, 'social.example', ALICE)
    document = actor_document(ALICE, 'alice')
    del document['preferredUsername']
    serve_actor(http_mock, document)

    assert create_actor_from_remote('alice@social.example') is None
    assert webfinger.call_count == 1


def test_a_handle_webfinger_does_not_know_creates_nothing(app, db_session, http_mock):
    http_mock.get('https://social.example/.well-known/webfinger').respond(404)

    assert create_actor_from_remote('alice@social.example') is None


# --- the other three resolvers ask the same helper (Amendment A) -----------
#
# actor_json_to_model is recorded, not run: what these tests prove is the handle each resolver hands it, and
# that a refused actor never reaches it. Row creation is create_actor_from_remote's tests' business above.

GROUP = 'https://social.example/c/memes'
FEED = 'https://social.example/f/news'


@pytest.fixture
def stored_as(app, db_session, site, monkeypatch):
    """Record every actor_json_to_model call of the three resolver modules; answer None (nothing created)."""
    g.admin_ids = []
    g.site = site
    calls = []

    def record(activity_json, address, server):
        calls.append((activity_json['id'], address, server))
        return None

    for module in ('app.user.utils', 'app.community.util', 'app.feed.util'):
        monkeypatch.setattr(f'{module}.actor_json_to_model', record)
    return calls


def group_document(actor_id, username):
    document = actor_document(actor_id, username, type='Group', outbox=f'{actor_id}/outbox')
    document['name'] = username.title()   # the display name; not a keyword: actor_document's own parameter is `name`
    return document


def feed_document(actor_id, username):
    document = actor_document(actor_id, username, type='Feed', following=f'{actor_id}/following')
    document['name'] = username.title()
    return document


def test_search_for_user_stores_a_confirmed_alias_under_the_canonical_handle(stored_as, http_mock):
    serve_webfinger(http_mock, 'alias.example', ALICE)
    serve_actor(http_mock, actor_document(ALICE, 'alice'))
    reverse = serve_webfinger(http_mock, 'social.example', ALICE)

    search_for_user('alice@alias.example')

    assert stored_as == [(ALICE, 'alice', 'social.example')]
    assert reverse.calls.last.request.url.params['resource'] == 'acct:alice@social.example'


def test_search_for_user_refuses_an_alias_the_reverse_lookup_does_not_confirm(stored_as, http_mock):
    serve_webfinger(http_mock, 'alias.example', ALICE)
    serve_actor(http_mock, actor_document(ALICE, 'alice'))
    serve_webfinger(http_mock, 'social.example', 'https://social.example/users/somebody-else')

    assert search_for_user('alice@alias.example') is None
    assert stored_as == []


def test_search_for_community_stores_a_confirmed_alias_under_the_canonical_handle(stored_as, http_mock):
    serve_webfinger(http_mock, 'alias.example', GROUP)
    serve_actor(http_mock, group_document(GROUP, 'memes'))
    reverse = serve_webfinger(http_mock, 'social.example', GROUP)

    search_for_community('!memes@alias.example')

    assert stored_as == [(GROUP, 'memes', 'social.example')]
    assert reverse.calls.last.request.url.params['resource'] == 'acct:memes@social.example'


def test_search_for_community_refuses_an_alias_the_reverse_lookup_does_not_confirm(stored_as, http_mock):
    serve_webfinger(http_mock, 'alias.example', GROUP)
    serve_actor(http_mock, group_document(GROUP, 'memes'))
    http_mock.get('https://social.example/.well-known/webfinger').respond(404)

    assert search_for_community('!memes@alias.example') is None
    assert stored_as == []


def test_search_for_community_asks_nothing_more_for_a_canonical_handle(stored_as, http_mock):
    webfinger = serve_webfinger(http_mock, 'social.example', GROUP)
    serve_actor(http_mock, group_document(GROUP, 'Memes'))

    search_for_community('!memes@social.example')

    assert stored_as == [(GROUP, 'memes', 'social.example')]
    assert webfinger.call_count == 1


def test_search_for_feed_reverse_looks_up_the_feed_handle_with_its_tilde(stored_as, http_mock):
    serve_webfinger(http_mock, 'alias.example', FEED)
    serve_actor(http_mock, feed_document(FEED, 'news'))
    reverse = serve_webfinger(http_mock, 'social.example', FEED)

    search_for_feed('~news@alias.example')

    assert stored_as == [(FEED, 'news', 'social.example')]
    assert reverse.calls.last.request.url.params['resource'] == 'acct:~news@social.example'


def test_search_for_feed_refuses_an_alias_the_reverse_lookup_does_not_confirm(stored_as, http_mock):
    serve_webfinger(http_mock, 'alias.example', FEED)
    serve_actor(http_mock, feed_document(FEED, 'news'))
    serve_webfinger(http_mock, 'social.example', 'https://social.example/f/other')

    assert search_for_feed('~news@alias.example') is None
    assert stored_as == []


# --- webfinger_self_href ---------------------------------------------------


def test_the_first_usable_self_link_is_the_href(app, db_session, http_mock):
    http_mock.get('https://social.example/.well-known/webfinger').respond(200, json={'links': [
        {'rel': 'http://webfinger.net/rel/profile-page', 'href': 'https://social.example/@alice'},
        {'rel': 'self'},
        {'rel': 'self', 'href': ALICE}]})

    assert webfinger_self_href('alice', 'social.example') == ALICE


def test_a_document_without_a_self_link_has_no_href(app, db_session, http_mock):
    http_mock.get('https://social.example/.well-known/webfinger').respond(200, json={'links': [
        {'rel': 'http://webfinger.net/rel/profile-page', 'href': 'https://social.example/@alice'}]})

    assert webfinger_self_href('alice', 'social.example') is None


def test_a_body_that_is_not_json_has_no_href(app, db_session, http_mock):
    http_mock.get('https://social.example/.well-known/webfinger').respond(200, content=b'<html>nope</html>')

    assert webfinger_self_href('alice', 'social.example') is None


def test_a_transport_error_has_no_href_and_is_not_retried(app, db_session, http_mock):
    route = http_mock.get('https://social.example/.well-known/webfinger').mock(
        side_effect=httpx.ConnectError('down'))

    assert webfinger_self_href('alice', 'social.example') is None
    assert route.call_count == 1


# --- host-meta.json --------------------------------------------------------


def test_host_meta_json_is_the_jrd_twin_of_the_xml(app, db_session):
    cache.clear()

    response = app.test_client().get('/.well-known/host-meta.json')

    assert response.status_code == 200
    assert response.mimetype == 'application/jrd+json'
    assert response.get_json() == {'links': [{
        'rel': 'lrdd', 'template': 'https://test.piefed.local/.well-known/webfinger?resource={uri}'}]}


def test_host_meta_json_is_cached_like_the_xml_route():
    """The suite runs NullCache, so the decorator is checked where flask-caching records it."""
    assert activitypub_routes.host_meta_json.cache_timeout == activitypub_routes.host_meta.cache_timeout == 600
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./run_tests.sh tests/test_ap_interop_webfinger_reverse.py -q`
Expected: FAIL at collection with `ImportError: cannot import name 'webfinger_self_href' from 'app.activitypub.util'`.

- [ ] **Step 3: Implement**

(a) In `app/activitypub/util.py`, directly before `def actor_json_to_model(activity_json, address, server):`, add the two helpers. Every name they use is already imported there: `json` (from `flask`), `urllib.parse`, `httpx`, `current_app`, `get_request_capped`, `FETCH_MAX_BYTES`, and `host_of` in the same module.

```python
def webfinger_self_href(address: str, server: str) -> str | None:
    """The href of the first `self` link `acct:address@server` resolves to, or None (C1 item 9).

    ONE GET through get_request_capped: at most FETCH_MAX_BYTES, a wall-clock deadline, no redirects -- and, unlike
    get_request, no sleep-and-retry on a transport error. It runs on request and inbox paths (D738/D775)."""
    resource = urllib.parse.quote(f'acct:{address}@{server}', safe=':@')
    try:
        status, body = get_request_capped(f'https://{server}/.well-known/webfinger?resource={resource}',
                                          FETCH_MAX_BYTES)
    except httpx.HTTPError:
        return None
    if status != 200 or body is None:
        return None
    try:
        document = json.loads(body)
    except ValueError:
        return None
    links = document.get('links') if isinstance(document, dict) else None
    for link in links if isinstance(links, list) else ():
        if isinstance(link, dict) and link.get('rel') == 'self' and isinstance(link.get('href'), str):
            return link['href']
    return None


def canonical_handle(actor_json, address: str, server: str, prefix: str = '') -> tuple[str, str] | None:
    """The (address, server) to store a WebFinger-resolved actor under, or None to refuse it (C1 item 9).

    The one check every WebFinger resolver makes (spec Amendment A): create_actor_from_remote, search_for_user,
    search_for_community and search_for_feed. The document names its own handle: preferredUsername @ the host of
    its id. When that is the handle we looked up (case-insensitively), nothing changes. Otherwise one
    webfinger_self_href lookup of the canonical handle must resolve to the same actor id, and the actor is stored
    under the canonical handle -- never under an alias some other server chose to answer for. A mismatch is
    refused and logged. `prefix` goes on the name in the reverse lookup only: '~' for a feed, whose WebFinger
    resource is acct:~name@host.

    A document without a string preferredUsername and id is passed through untouched: actor_json_to_model already
    refuses it (D1372), and there is nothing to compare."""
    actor_id = actor_json.get('id') if isinstance(actor_json, dict) else None
    name = actor_json.get('preferredUsername') if isinstance(actor_json, dict) else None
    if not (isinstance(name, str) and name and isinstance(actor_id, str) and actor_id):
        return address, server
    id_host = host_of(actor_id)
    if f'{name}@{id_host}'.lower() == f'{address}@{server}'.lower():
        return address, server
    href = webfinger_self_href(f'{prefix}{name}', id_host)
    if href is not None and href.lower() == actor_id.lower():
        return name.lower(), id_host
    current_app.logger.warning(f'WebFinger reverse check refused {address}@{server}: '
                               f'{prefix}{name}@{id_host} does not resolve to {actor_id}')
    return None
```

(b) In `app/activitypub/actor.py`, change

```python
from app.activitypub.util import get_request, signed_get_request, actor_json_to_model, refresh_user_profile, \
    refresh_community_profile, refresh_feed_profile, extract_domain_and_actor, normalise_actor_string
```

to

```python
from app.activitypub.util import get_request, signed_get_request, actor_json_to_model, refresh_user_profile, \
    refresh_community_profile, refresh_feed_profile, extract_domain_and_actor, normalise_actor_string, \
    canonical_handle
```

and in `create_actor_from_remote`, change

```python
        actor_json = fetch_actor_from_webfinger(address, server, retry=retry)

    if actor_json:
```

to

```python
        actor_json = fetch_actor_from_webfinger(address, server, retry=retry)
        if actor_json:
            handle = canonical_handle(actor_json, address, server)   # interop C1 item 9
            if handle is None:
                return None
            address, server = handle

    if actor_json:
```

`fetch_actor_from_webfinger` is unchanged. Its tests feed it exactly two canned responses (`tests/test_activitypub_actor.py:134-147`), and the URL arm of `create_actor_from_remote` has no handle to check.

(c) In `app/user/utils.py`, change `from app.activitypub.util import actor_json_to_model` (line 10) to `from app.activitypub.util import actor_json_to_model, canonical_handle`, and in `search_for_user` change

```python
                if 'type' in object and object['type'] in ('Person', 'Service', 'Podcast'):
                    user = actor_json_to_model(object, name, server)
                    return user
```

to

```python
                if 'type' in object and object['type'] in ('Person', 'Service', 'Podcast'):
                    handle = canonical_handle(object, name, server)   # interop C1 item 9 (Amendment A)
                    if handle is None:
                        return None
                    user = actor_json_to_model(object, *handle)
                    return user
```

(d) In `app/community/util.py`, change

```python
from app.activitypub.util import find_actor_or_create, actor_json_to_model, \
    find_hashtag_or_create, create_post, remote_object_to_json, find_flair, activitypub_visibility, host_of
```

to

```python
from app.activitypub.util import find_actor_or_create, actor_json_to_model, \
    find_hashtag_or_create, create_post, remote_object_to_json, find_flair, activitypub_visibility, host_of, \
    canonical_handle
```

and in `search_for_community` change

```python
                        if community_json['type'] == 'Group':
                            community = actor_json_to_model(community_json, name, server)
```

to

```python
                        if community_json['type'] == 'Group':
                            handle = canonical_handle(community_json, name, server)   # interop C1 item 9
                            if handle is None:
                                return None
                            name, server = handle
                            community = actor_json_to_model(community_json, name, server)
```

(`name` and `server` are rebound because `retrieve_mods_and_backfill(community.id, server, name, ...)` just below must use the stored handle too.)

(e) In `app/feed/util.py`, change `from app.activitypub.util import actor_json_to_model` (line 11) to `from app.activitypub.util import actor_json_to_model, canonical_handle`, and in `search_for_feed` change

```python
                        if feed_json['type'] == 'Feed':
                            feed = actor_json_to_model(feed_json, name, server)
```

to

```python
                        if feed_json['type'] == 'Feed':
                            handle = canonical_handle(feed_json, name, server, prefix='~')   # interop C1 item 9
                            if handle is None:
                                return None
                            feed = actor_json_to_model(feed_json, *handle)
```

These three resolvers keep their own sleep-and-retry WebFinger and actor fetches; only the new reverse lookup is retry-free. Removing their retries is outside C1.

(f) In `app/activitypub/routes.py`, directly after `host_meta`, add:

```python
@bp.route('/.well-known/host-meta.json')
@cache.cached(timeout=600)
def host_meta_json():
    """The JRD form of host-meta (RFC 6415 section 3), the same lrdd template as the XML route above (C1 item 9)."""
    resp = jsonify({'links': [{'rel': 'lrdd',
                               'template': 'https://' + current_app.config["SERVER_NAME"] +
                                           '/.well-known/webfinger?resource={uri}'}]})
    resp.content_type = 'application/jrd+json'
    return resp
```

`/.well-known/` is already in `SIGNED_FETCH_EXEMPT_PREFIXES` (`app/activitypub/entitlement.py`), so secure mode does not gate the new route.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./run_tests.sh tests/test_ap_interop_webfinger_reverse.py tests/test_activitypub_actor.py tests/test_ap_instance_metadata.py tests/test_webfinger.py tests/test_user_utils.py tests/test_community_search.py tests/test_community_util.py tests/test_api_search_resolve.py tests/test_feed_util.py -q`
Expected: PASS. `tests/test_activitypub_actor.py` must stay green unchanged: its webfinger-arm documents carry no `preferredUsername`, so `canonical_handle` passes them through. If an existing resolver test fails on an unmatched request to `/.well-known/webfinger`, its served document names a handle other than the one looked up; that test is now on the alias path and gets a reverse WebFinger route answering the document's own id (do not weaken the check).

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/util.py app/activitypub/actor.py app/activitypub/routes.py app/user/utils.py \
    app/community/util.py app/feed/util.py tests/test_ap_interop_webfinger_reverse.py
git commit -m "feat: an actor found by WebFinger is stored under the handle its own server confirms

When the actor document's preferredUsername@id-host differs from the
handle that was looked up, one capped WebFinger lookup of that
canonical handle must return the same actor id. The actor is then
stored under the canonical handle, and a mismatch is refused and
logged. One helper, canonical_handle, makes the check for every
WebFinger resolver: actor resolution and the user, community and feed
searches. A cross-host handle that the reverse lookup confirms now
resolves, where the host gate used to refuse it. host-meta is also
served as JRD at /.well-known/host-meta.json.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Task gate**

Run, in order:
1. `./run_tests.sh tests/test_ap_interop_webfinger_reverse.py tests/test_activitypub_actor.py tests/test_ap_instance_metadata.py tests/test_webfinger.py tests/test_user_utils.py tests/test_community_search.py tests/test_feed_util.py --cov=app --cov-branch --cov-report=json -q`
2. `python3 tests/check_changed_line_coverage.py coverage.json HEAD~1 --branches app/`

Expected: all pass; 100% changed-line and branch coverage.

---

### Task 13: Final gate for the C1 track

**Files:**
- None changed. This task proves the branch as a whole.

**Interfaces:**
- Consumes: every commit of Tasks 1–12.
- Produces: nothing.

- [ ] **Step 1: Run the full suite with coverage**

Run: `./run_tests.sh --cov=app --cov-branch --cov-report=json -q`
Expected: PASS. Read the passed-test count, not only the exit code: a run that hits the session budget still exits 0 (see `pytest.ini`). The count must be at least the count before Task 1 plus the tests Tasks 1–12 added.

- [ ] **Step 2: Changed-line and branch coverage over the whole track**

Run: `python3 tests/check_changed_line_coverage.py coverage.json 0c861692e --branches app/ fastapi_server.py`
Expected: exit 0. `0c861692e` is the spec commit, so this covers every line Tasks 1–12 added. Chain it to Step 1 with `&&` if you rerun, because a failed run leaves the previous `coverage.json` behind.

- [ ] **Step 3: Per-module floors**

Run: `python3 tests/check_coverage_floors.py coverage.json coverage_floors.ini`
Expected: exit 0. No floor drops. In particular `app/activitypub/routes.py` stays at or above 99, `app/activitypub/util.py` at or above 99, `app/activitypub/signature.py` at 100 and `app/utils.py` at or above 99.

- [ ] **Step 4: Migration chain**

The repo has no single-head test, so check on the host:

```bash
python3 - <<'EOF'
import glob, re
revs, downs = {}, set()
for f in glob.glob('migrations/versions/*.py'):
    s = open(f).read()
    r = re.search(r"^revision\s*=\s*['\"]([^'\"]+)", s, re.M)
    d = re.search(r"^down_revision\s*=\s*(.+)$", s, re.M)
    if r:
        revs[r.group(1)] = f
        if d:
            downs.update(re.findall(r"['\"]([0-9a-f]+)['\"]", d.group(1)))
print([r for r in revs if r not in downs])
EOF
```

Expected: exactly one head, `['f4b8d2a6c1e7']` (Task 10's migration, which revises Task 6's `e2a7c4f9d1b3`, which revises `d9f3b7a15c2e`).

No commit: this task changes nothing. If any step fails, fix it in the task that owns the failing line, as a new commit with that task's commit style, and rerun Steps 1–4.
