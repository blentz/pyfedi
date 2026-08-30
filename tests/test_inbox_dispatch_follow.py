"""Sub-project 5b, Task 1 -- shared fixtures for the Follow, Accept and Reject
arms (`record_sends`), and the Follow arm's outcome table.

`record_sends` is consumed by Tasks 2-4 (the three Follow target branches):
every `send_post_request` call in this sub-project's scope -- routes.py:962,
980, 999, 1014, 1045 -- sits inside the Follow arm. Accept (1075-1148) and
Reject (1150-1191) make no outbound sends at all, so nothing downstream of
Follow needs this fixture.

The table below is derived directly from routes.py:935-1073, read line by
line for this task -- not copied from the plan or the design spec. It was
cross-checked against both afterward and no disagreement turned up; three
observations below (the User branch's silent refusals, the shared-log-call
nuance on the auto-accept split, and the if/elif vs. plain-`or` contrast
between Community's and User's guards) are not called out in either document
and are recorded here because they surfaced during the re-derivation, which
is the point of doing it independently.

Target resolution happens once, at :938
(`find_actor_or_create_cached(target_ap_id)`); which of the three branches
below runs is decided entirely by what that lookup returns.

| branch (lines)                                    | selects it                                                                                                    | writes                                                                                                                    | sends                          | logs |
|-----------------------------------------------------|----------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------|---------------------------------|------|
| unfound target (939-941)                           | `find_actor_or_create_cached` returns falsy                                                                    | nothing                                                                                                                     | nothing                          | APLOG_FOLLOW/APLOG_FAILURE 'Could not find target of Follow' |
| Community, local_only (945-947, 954-962)           | `community.local_only` is True                                                                                 | nothing                                                                                                                     | Reject                           | APLOG_FOLLOW/APLOG_FAILURE 'Local only cannot be followed by remote users' -- BEFORE the send |
| Community, banned (949-953, 954-962)               | `community.local_only` is False AND a `CommunityBan(user_id, community_id)` row exists -- checked only when NOT local_only; this is if/elif, not two independently-evaluated alternatives | nothing                                                                                                                     | Reject                           | APLOG_FOLLOW/APLOG_FAILURE 'Remote user has been banned' -- BEFORE the send |
| Community, new member (964-981)                    | not rejected, and no existing `CommunityMember(user_id, community_id)`                                          | `CommunityMember` row; `community.subscriptions_count` +1; `community.last_active` and `user.last_seen` stamped            | Accept                           | APLOG_FOLLOW/APLOG_SUCCESS -- AFTER the send |
| Community, already a member (964-965, silent)      | `existing_member` truthy                                                                                        | nothing                                                                                                                     | nothing                           | nothing at all |
| Feed, non-public (989-999)                         | `not feed.public`                                                                                               | nothing                                                                                                                     | Reject                           | NOTHING -- no `log_incoming_ap` call anywhere on this path, unlike either Community reject reason above |
| Feed, new subscriber (1001-1016)                   | `feed_membership(user, feed) != SUBSCRIPTION_MEMBER`                                                            | `FeedMember` row; `feed.subscriptions_count` +1 (no `last_active`/`last_seen`-equivalent stamp -- Feed has no such column touched here) | Accept                           | APLOG_FOLLOW/APLOG_SUCCESS |
| Feed, already subscribed (1001, silent)            | `feed_membership(user, feed) == SUBSCRIPTION_MEMBER`                                                            | nothing                                                                                                                     | nothing                           | nothing |
| User, remote target (1021-1024)                    | `not local_user.is_local()`                                                                                     | nothing                                                                                                                     | nothing -- no Reject; this branch never sends a federated reply on any refusal | APLOG_FOLLOW/APLOG_FAILURE 'Follow request for remote user received' |
| User, blocked (1025-1027)                          | `has_blocked_user(...) or has_blocked_instance(...) or instance_banned(...)` -- a single plain `or`, all three checked unconditionally (short-circuited) and sharing ONE log message, unlike Community's if/elif with a distinct message per reason | nothing                                                                                                                     | nothing                           | APLOG_FOLLOW/APLOG_FAILURE 'Attempt to follow denied due to block' |
| User, auto-accept (1030-1071, `auto_accept` True)  | not `existing_follower`, and `local_user.ap_manually_approves_followers is False`                              | `UserFollower(is_accepted=True, is_inward=True)` via the task-local `session`; `ap_followers_url` backfilled if empty; `Notification(notif_type=NOTIF_FOLLOW)` added via `db.session` and committed separately -- a different session object than the `UserFollower` write, in the same logical write (D60's divergence made concrete); `unread_notifications` +1 | Accept                           | APLOG_FOLLOW/APLOG_SUCCESS -- the SAME log call as the manual-approval row below |
| User, manual approval (1030-1071, `auto_accept` False) | not `existing_follower`, and `ap_manually_approves_followers` anything other than the literal `False`      | `UserFollower(is_accepted=None, is_inward=True)` via `session`; `Notification(notif_type=NOTIF_FOLLOW_REQUEST)` via `db.session`; `unread_notifications` +1 | nothing                           | APLOG_FOLLOW/APLOG_SUCCESS -- logged as SUCCESS the moment the pending request is recorded, not when a human later approves it |
| User, already following (1030, silent)             | `existing_follower` truthy                                                                                      | nothing                                                                                                                     | nothing                           | nothing |

Asymmetries worth carrying into the tests that cover them:

  - Community's two reject reasons both log APLOG_FAILURE before the Reject
    is sent; Feed's one reject reason (:989) logs nothing at all, even
    though the surrounding shape (guard sets `reject_follow`, then sends a
    Reject) is otherwise identical. Registered by the design spec at
    routes.py:989-999; independently confirmed here.
  - Three "already-done" paths are uniformly silent: an existing
    `CommunityMember` (:964-965), an already-subscribed `FeedMember`
    (:1001), and an existing `UserFollower` (:1030) each write nothing,
    send nothing, and log nothing.
  - The User branch's refusals send no federated reply at all, on either
    refusal reason -- Community and Feed both notify the remote actor with
    a Reject on their own refusal paths. This asymmetry is not called out
    in the design spec's own registered-findings list.
  - `:1033`'s `is_accepted=auto_accept if auto_accept else None` can only
    ever write `True` or `None`; `UserFollower.is_accepted`'s own column
    comment (`app/models.py:3533`) documents `False` ('Rejected') as a
    third meaningful state this expression can never produce.
  - The `UserFollower` row (:1036, task-local `session`) and its
    accompanying `Notification` (:1067, `db.session`) are added and
    committed through two different session objects inside one logical
    write -- the concrete instance of 5a's D60 session divergence that this
    sub-project's own design spec calls out by line number.
"""

from app.activitypub import routes as activitypub_routes


def record_sends(monkeypatch):
    """Double send_post_request at its binding site on the routes module and
    return the list it records into.

    routes.py imports send_post_request by name, so patching
    app.activitypub.signature would leave routes' copy pointing at the
    original. Same binding-site trap tests/conftest.py:394 documents.
    """
    sends = []
    monkeypatch.setattr(
        activitypub_routes, 'send_post_request',
        lambda uri, body, private_key, key_id, **kw: sends.append((uri, body, key_id)))
    return sends
