"""Sub-project 5c, Task 1 -- shared fixtures for the moderation arms
(`record_moderation`), and the Delete and Lock arms' outcome tables.

`record_moderation` generalises 5b's `record_sends` over however many
delegate functions a moderation arm calls (`add_to_modlog`,
`delete_post_or_comment`, `announce_activity_to_followers`, ...), since
these arms do not send federated replies the way Follow/Accept/Reject do.
It is consumed by Tasks 3, 5, 7, 9, 10 and 11 (the Lock, Delete, Add, Remove
and Block coverage tasks), imported from this file the same way
`test_inbox_dispatch_follow.py:record_sends` was imported cross-file in 5b.

Both tables below are derived directly from source, read line by line for
this task -- not copied from the plan or the design spec:

  - Delete: routes.py:1264-1327
  - Lock:   routes.py:1356-1395

They were cross-checked against the plan's Task 2, 4 and 5 briefs afterward
and agree with those briefs' account of the two registered defects (Lock's
`post`-instead-of-`post_reply` references, and Delete's `feed.user_id`/
`user.id` reads ahead of their None-guards). No disagreement turned up
there. Re-deriving independently did surface three things not spelled out
in either the plan or this task's own brief, noted after the tables.

**2026-08-31 note (fix wave):** both tables below are Task 1's PRE-FIX
derivation, read from source before this same branch's authorised fixes
landed, and never revised afterward. At HEAD, commits `b79f43f9` (Lock) and
`c1bae61c` (Delete) have fixed exactly the crash/dead-code rows the tables
describe below -- those rows are struck and annotated with the commit that
fixed them, in place, rather than rewritten, since the pre-fix derivation
itself remains useful record of what Task 1 actually found. The individual
tests further down this file (e.g. `test_a_delete_naming_an_unknown_feed_is_
refused`, `test_a_moderator_can_lock_a_comment`) already say "pre-fix
numbering" / "Asserts the corrected behaviour" in their own docstrings and
were never stale -- only these two summary tables were.

### Delete (routes.py:1264-1327)

`core_activity['object']` is inspected THREE different ways before anything
is deleted: a dict with `type == 'Feed'` is a feed deletion, handled
entirely inside this arm; a bare string (Lemmy) or a dict without that type
(kbin) both reduce to an `ap_id` and fall into a shared continuation that
looks the target up as content, then as a private message.

| branch (lines)                                                | selects it                                                                                                                    | writes                                                                                                                                                    | delegates to                                                                                          | logs |
|-----------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------|------|
| Feed-delete, unresolved actor or feed (1268-1273)               | `object` is a dict with `type == 'Feed'`                                                                                       | nothing                                                                                                                                                        | `find_actor_or_create_cached(actor_id)` for `user`; a raw `session.query(Feed)` (not a delegate function) for `feed` | ~~**CRASHES before any log call.** `:1273`'s `user.id == feed.user_id` dereferences `user.id` (None if the actor did not resolve) or `feed.user_id` (None if no feed row matched) with no guard on either -- an unhandled `AttributeError` escapes `process_inbox_request` instead of reaching a log call. Registered; Task 4's to fix, not this task's.~~ **FIXED, commit `c1bae61c` (D98/D99).** Two early guards, `if not user:` and `if not feed:`, each `return` after logging their own APLOG_FAILURE message, now precede the owner check -- neither `user.id` nor `feed.user_id` is ever dereferenced while either is `None`. See `test_a_delete_naming_an_unknown_feed_is_refused` and `test_a_delete_from_an_unresolvable_actor_is_refused` below. |
| Feed-delete, owner mismatch (1273-1275)                         | both resolved, and `user.id != feed.user_id`                                                                                    | nothing                                                                                                                                                        | nothing                                                                                                    | APLOG_DELETE/APLOG_FAILURE 'Delete rejected, request came from non-owner.' |
| Feed-delete, success (1277-1298)                                | ~~owner matches; reaches `if feed:`, which is unconditionally True here (see note below)~~ **STALE, commit `c1bae61c`.** The pre-fix `if feed: ... else: ...` structure this row describes no longer exists; the fix replaced it with the early `if not feed:` guard in the row above, so this row's own premise (an `if feed:` reached with `feed` unconditionally truthy) is moot post-fix, not merely fixed. The deletes/commit sequence and its SUCCESS log, the two columns to the right, are unaffected and still accurate. | deletes every `FeedItem` row for `feed.id` (commit per row), every `FeedMember` row (commit per row), every `FeedJoinRequest` row (commit per row), then the `Feed` row itself (commit) | nothing -- every delete goes through the task-local `session` directly, no helper function is called      | APLOG_DELETE/APLOG_SUCCESS naming the feed's `object['id']` |
| Feed-delete, `else: feed not found` (1299-1301)                 | would fire when `feed` is falsy at `:1278`                                                                                     | nothing                                                                                                                                                        | nothing                                                                                                    | ~~**DEAD CODE.** The only way to reach `:1278` with `feed` falsy is `feed is None`, and that already raised at `:1273` before this line is ever reached -- a `Feed` row the ORM returns has no `__bool__`/`__len__` override, so it is never falsy on its own. This log line can never fire while `:1273` reads `feed.user_id` unguarded.~~ **FIXED, commit `c1bae61c` (D98).** No longer dead, and no longer a separate `else` at all: the fix replaced this pre-fix `if feed: ... else: log(...)` shape with the early `if not feed: log(...); return` guard two rows above, making the not-found message directly and correctly reachable. `test_a_delete_naming_an_unknown_feed_is_refused` below asserts it fires. |
| bare string object -- Lemmy shape (1302-1303)                   | `object` is a `str`                                                                                                             | none yet -- sets `ap_id = object`, falls into the shared continuation below                                                                                    | --                                                                                                          | -- |
| dict object, not a Feed -- kbin shape (1304-1305)                | `object` is a dict whose `type != 'Feed'`                                                                                       | none yet -- sets `ap_id = object['id']`, falls into the shared continuation below                                                                              | --                                                                                                          | -- |
| shared: content found, already deleted (1308-1311)              | `find_liked_object(ap_id)` truthy and `to_delete.deleted` is True                                                               | nothing                                                                                                                                                        | nothing                                                                                                    | APLOG_DELETE/APLOG_IGNORED 'Activity about local content which is already deleted' |
| shared: content found, deletes it (1312-1316)                   | `find_liked_object(ap_id)` truthy and not yet deleted                                                                           | none directly in this arm -- the mutation happens inside the delegate                                                                                          | `delete_post_or_comment(user, to_delete, store_ap_json, request_json, reason)` unconditionally; `announce_activity_to_followers(to_delete.community, user, request_json)` only when `not announced` | **NOTHING.** No `log_incoming_ap` call anywhere on this path -- the one path in this arm that does real, successful work and logs none of it. |
| shared: nothing found, PM found (1319-1325)                     | `find_liked_object` falsy; `ChatMessage` row matches `ap_id` and `sender_id == user.id`                                         | `updated_message.read = True`, `.deleted = True`, commit                                                                                                       | nothing                                                                                                    | APLOG_DELETE/APLOG_SUCCESS 'Delete: PM {ap_id} deleted' |
| shared: nothing found at all (1317-1326)                        | neither `find_liked_object` nor the `ChatMessage` lookup found anything                                                         | nothing                                                                                                                                                        | nothing                                                                                                    | **NOTHING.** Falls straight through to the bare `return` at `:1326` -- the arm's only fully-silent no-op. |

### Lock (routes.py:1356-1395)

Target resolution (`:1360-1367`) happens once, before either outcome
branch: `/post/` in the object picks `Post.get_by_ap_id`; `/comment/` picks
`PostReply.get_by_ap_id`; neither substring tries `Post.get_by_ap_id`
first and only tries `PostReply.get_by_ap_id` on that returning `None`.
`reason` (`:1368`) is computed once regardless of which target resolved.

| branch (lines)                                                   | selects it                                                                                                     | writes                                                                                                          | delegates to                                                                                                                          | logs |
|---------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------|------|
| post, moderator or instance admin (1369-1376)                       | `post` truthy and (`post.community.is_moderator(mod)` or `post.community.is_instance_admin(mod)`)                | `post.comments_enabled = False`, commit                                                                          | `add_to_modlog('lock_post', actor=mod, target_user=post.author, reason=reason, community=post.community, post=post, link_text=..., link=...)` | APLOG_LOCK/APLOG_SUCCESS |
| post, no permission (1369-1370, 1377-1378)                          | `post` truthy, both disjuncts False                                                                               | nothing                                                                                                          | nothing                                                                                                                                    | APLOG_LOCK/APLOG_FAILURE 'Lock: Does not have permission' |
| post None, post_reply, moderator branch of the `or` is True (1379-1389) | `post` falsy, `post_reply` truthy, `post_reply.community.is_moderator(mod)` True -- the `or`'s second operand (`post.community.is_instance_admin(mod)`) is short-circuited and never evaluated here | `post_reply.replies_enabled = False`; a raw-SQL subtree UPDATE sets `replies_enabled = False` for every reply whose `path` contains `post_reply.id`; commit. **These writes DO happen.** | ~~**CRASHES immediately after the writes, before any delegate runs.** `add_to_modlog(..., target_user=post.author, ...)` at `:1386` dereferences `post`, which is guaranteed `None` in this branch (reaching `elif post_reply:` requires the earlier `if post:` to have been falsy) -- `AttributeError: 'NoneType' object has no attribute 'author'` aborts the call before `add_to_modlog` runs and before either the modlog entry or the `:1387` `community=post.community` keyword is ever evaluated.~~ **FIXED, commit `b79f43f9` (D97).** All three `post` references in this branch (the `:1380` disjunct, `:1386`, `:1387`) were corrected to `post_reply`; `add_to_modlog` now receives `target_user=post_reply.author` and `community=post_reply.community` and runs to completion. See `test_a_moderator_can_lock_a_comment` below. | ~~**NOTHING.** The intended APLOG_LOCK/APLOG_SUCCESS at `:1389` is never reached, and the writes above are never rolled back either -- they were already committed at `:1385`, one statement before the crash. `session.rollback()` in the arm's outer `except Exception:` (`:1885`) has nothing pending left to undo.~~ **FIXED, commit `b79f43f9` (D97).** APLOG_LOCK/APLOG_SUCCESS is now reached and logged. |
| post None, post_reply, moderator branch of the `or` is False (1379-1380) | `post` falsy, `post_reply` truthy, `post_reply.community.is_moderator(mod)` False                                | **NONE.** Unlike the row above, this path crashes before any write.                                              | ~~**CRASHES while evaluating the `if` condition itself.** Because the first disjunct is False, Python evaluates the second, `post.community.is_instance_admin(mod)`, with `post` `None` -- `AttributeError: 'NoneType' object has no attribute 'community'` fires before the loop body or either arm of the intended if/else runs.~~ **FIXED, commit `b79f43f9` (D97).** The `:1380` disjunct now reads `post_reply.community.is_instance_admin(mod)`; the condition evaluates cleanly to False and falls through to the refusal log. See `test_a_non_moderator_locking_a_comment_is_refused` below. | ~~**NOTHING.** The intended APLOG_LOCK/APLOG_FAILURE 'Lock: Does not have permission' at `:1391` is never reached.~~ **FIXED, commit `b79f43f9` (D97).** APLOG_LOCK/APLOG_FAILURE 'Lock: Does not have permission' is now reached and logged. |
| neither post nor post_reply resolved (1392-1393)                    | both `post` and `post_reply` falsy after target resolution                                                        | nothing                                                                                                          | nothing                                                                                                                                    | APLOG_LOCK/APLOG_FAILURE 'Lock: post not found' |

~~Net effect of the two crash rows: given current source, a federated Lock of
a COMMENT can never complete successfully or be cleanly refused -- both of
its outcomes raise an unhandled `AttributeError` that escapes
`process_inbox_request` (caught only by the bare `except Exception: ...;
raise` at `:1885`, which rolls back and re-raises). Locking a POST is
unaffected; only the `post_reply` half is broken.~~ **FIXED, commit
`b79f43f9` (D97) -- this paragraph described PRE-FIX source and is no
longer true at HEAD.** A federated Lock of a comment now completes
successfully (moderator/admin) or is cleanly refused (neither privilege),
exactly like a Lock of a post; see `test_a_moderator_can_lock_a_comment` and
`test_a_non_moderator_locking_a_comment_is_refused` below, which assert
both outcomes directly. This is the "three dereferences of `post`" the task
brief names -- concretely, of the three textual references (`:1380`'s
second disjunct, `:1386`, `:1387`), only ONE ever executed per call
pre-fix: the moderator branch reached `:1386` and died there before
`:1387` was evaluated (Python evaluates keyword arguments left to right and
aborts on the first exception); the non-moderator branch died at `:1380`
and never reached `:1386`/`:1387` at all. Registered pre-fix as Task 2's to
fix, not this task's; fixed by Task 2 as D97 above.

### Observations beyond the two registered defects

  - Delete's own selector for the Feed-vs-other split, `:1266`
    (`core_activity['object']['type'] == 'Feed'`), used to read `['type']`
    unconditionally once `object` was confirmed a dict, so a dict with no
    'type' raised KeyError (D92, fixed). It now reads the type with
    `.get()`, so an untyped dict takes the kbin path by its id, and a dict
    with no id either is refused and logged.
  - Delete's Feed-branch reassigns the arm's `user` local from
    `find_actor_or_create_cached(actor_id)` (`:1268`), which is a SEPARATE
    lookup from whatever `user` the preamble resolved before dispatch
    reached this arm. Since the branch always returns before falling
    through to the shared post/PM continuation, this shadowing never
    leaks into the other two object shapes in practice -- but a reader
    tracing `user` through the whole arm has to notice the reassignment is
    branch-local.
  - Lock's raw-SQL subtree UPDATE (`:1382-1384`) is a second write
    independent of the `post_reply.replies_enabled = False` line right
    above it -- the ORM attribute write touches only the one row; the raw
    UPDATE is what actually cascades to descendant replies via the
    `path @> ARRAY[:parent_id]` predicate. A test asserting only the ORM
    attribute would miss the subtree ever being touched.
"""

from sqlalchemy import inspect as sa_inspect

from app import db
from app.activitypub import routes as activitypub_routes
from app.models import ActivityPubLog, ChatMessage, Conversation, Feed, FeedItem, FeedJoinRequest, \
    FeedMember, InstanceRole, utcnow
from tests.factories import inbox_activity, make_community, make_community_member, make_feed, \
    make_feed_item, make_feed_join_request, make_feed_member, make_instance, make_post, \
    make_post_reply, make_site, make_user, seed_community_owner
from tests.test_inbox_dispatch_preamble import dispatch


def record_moderation(monkeypatch, *names):
    """Double each of `names` at its binding site on the routes module, and
    return a dict of name -> list of (args, kwargs) tuples recorded for it.

    Generalises 5b's `record_sends` over however many delegates a
    moderation arm calls (`add_to_modlog`, `delete_post_or_comment`,
    `announce_activity_to_followers`, ...). routes.py imports each of these
    by name (or, for `announce_activity_to_followers`, defines it itself),
    so patching the DEFINING module under a different name would leave
    routes' own copy pointing at the original -- the same binding-site trap
    tests/conftest.py:442 documents.

    The lambda binds `_n=name` as a keyword default so each closure closes
    over its OWN name, rather than every closure sharing whichever value
    `name` holds when the loop finishes -- the classic late-binding bug a
    bare `name` reference here would produce.
    """
    calls = {name: [] for name in names}
    for name in names:
        monkeypatch.setattr(
            activitypub_routes, name,
            lambda *args, _n=name, **kwargs: calls[_n].append((args, kwargs)))
    return calls


def test_record_moderation_gives_each_patched_name_its_own_call_list(monkeypatch):
    """Proves the closure-per-name claim in `record_moderation`'s docstring:
    patching two names and calling both through the routes module records
    each call under its own key, not merged or overwritten by the other.
    """
    calls = record_moderation(monkeypatch, 'add_to_modlog', 'delete_post_or_comment')

    activitypub_routes.add_to_modlog('lock_post', actor=1, target_user=2)
    activitypub_routes.delete_post_or_comment(3, 4)

    assert calls == {
        'add_to_modlog': [(('lock_post',), {'actor': 1, 'target_user': 2})],
        'delete_post_or_comment': [((3, 4), {})],
    }


def test_make_feed_item_and_make_feed_member_build_expected_rows(app, db_session):
    """Smoke-tests the two factories Task 1 adds: each produces a row with
    the foreign keys the moderation arms read (`FeedItem.feed_id`/
    `community_id`; `FeedMember.feed_id`/`user_id`/`is_owner`), and
    `is_owner` defaults False to match the model's own default
    (app/models.py:4034).
    """
    seed_community_owner('owner.example')  # instance id 1 + local owner user
    # id 1, which make_community() below hardcodes as its instance_id/user_id.
    instance = make_instance('peer.example')
    feed = make_feed(instance, local=True)
    community = make_community()
    user = make_user(instance, 'alice')

    item = make_feed_item(feed, community)
    member = make_feed_member(user, feed)
    owner_member = make_feed_member(user, feed, is_owner=True)

    assert isinstance(item, FeedItem)
    assert item.feed_id == feed.id
    assert item.community_id == community.id

    assert isinstance(member, FeedMember)
    assert member.feed_id == feed.id
    assert member.user_id == user.id
    assert member.is_owner is False

    assert owner_member.is_owner is True


def _seed_lockable_comment(instance_domain='peer.example'):
    """A community with one moderatable comment thread: `parent_reply` (the
    Lock target, `ap_id` containing '/comment/' so routes.py:1362 selects it
    via PostReply.get_by_ap_id) and `child_reply`, whose `path` contains
    `parent_reply.id` so the raw-SQL subtree UPDATE at routes.py:1382-1384
    (`where path @> ARRAY[:parent_id]`) reaches it too. `path` values follow
    PostReply.new()'s own convention (app/models.py:3016-3023): a root
    reply's path is `[0, self.id]`; a child's is its parent's path with its
    own id appended.

    Returns (mod, community, post, parent_reply, child_reply, author). `mod`
    is a plain remote User with `ap_fetched_at` stamped (so
    find_actor_or_create_cached does not schedule a real actor refresh) and
    is NOT yet a community moderator -- callers that need a moderator call
    `make_community_member(mod, community, is_moderator=True)` themselves.
    """
    instance = seed_community_owner(instance_domain)  # instance id 1 + local owner user id 1
    community = make_community(host=instance_domain)
    author = make_user(instance, 'commenter')
    post = make_post(community, author, ap_id=f'https://{instance_domain}/post/1')

    parent_reply = make_post_reply(post, author)
    parent_reply.ap_id = f'https://{instance_domain}/comment/{parent_reply.id}'
    parent_reply.path = [0, parent_reply.id]
    db.session.commit()

    child_reply = make_post_reply(post, author)
    child_reply.path = [0, parent_reply.id, child_reply.id]
    db.session.commit()

    mod = make_user(instance, 'moduser')
    mod.ap_fetched_at = utcnow()
    db.session.commit()

    return mod, community, post, parent_reply, child_reply, author


def test_a_moderator_can_lock_a_comment(app, db_session, monkeypatch):
    """routes.py:1379-1388. Pre-fix this raises AttributeError at :1386
    (`target_user=post.author`, with post None) on the SUCCESS path.

    Asserts the corrected behaviour: replies_enabled goes False on the reply
    AND on its subtree via the raw UPDATE at :1382-1385, add_to_modlog is
    called with the reply's own author and community, and SUCCESS is logged.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    calls = record_moderation(monkeypatch, 'add_to_modlog')

    mod, community, post, parent_reply, child_reply, author = _seed_lockable_comment()
    make_community_member(mod, community, is_moderator=True)

    activity = inbox_activity(mod, activity_type='Lock',
                              object_uri=parent_reply.ap_id, summary='breaking the rules')

    dispatch(activity)

    db.session.expire_all()
    assert parent_reply.replies_enabled is False
    assert child_reply.replies_enabled is False

    assert len(calls['add_to_modlog']) == 1
    args, kwargs = calls['add_to_modlog'][0]
    assert args == ('lock_post_reply',)
    # The routes-module call ran on the dispatcher's own independent
    # session (get_task_session()), which is closed (`finally: session.close()`,
    # routes.py:1888-1889) before dispatch() returns here, and its objects
    # were already expired by an intervening session.commit() before the
    # (mocked) call captured them -- so `.id` on the captured objects raises
    # DetachedInstanceError. `inspect(obj).identity` reads the primary-key
    # tuple SQLAlchemy stores on the instance's state at load time, which
    # survives both expiration and detachment.
    assert sa_inspect(kwargs['actor']).identity[0] == mod.id
    assert sa_inspect(kwargs['target_user']).identity[0] == author.id
    assert kwargs['reason'] == 'breaking the rules'
    assert sa_inspect(kwargs['community']).identity[0] == community.id
    assert sa_inspect(kwargs['reply']).identity[0] == parent_reply.id
    assert kwargs['link'] == f'post/{post.id}#comment_{parent_reply.id}'

    log = ActivityPubLog.query.one()
    assert log.result == 'success'
    assert log.exception_message is None


def test_a_non_moderator_locking_a_comment_is_refused(app, db_session, monkeypatch):
    """routes.py:1380. `post_reply.community.is_moderator(mod)` is False, so
    the `or` does NOT short-circuit and `post.community` is evaluated with
    post None -- turning the permission refusal into an AttributeError.

    Asserts the corrected behaviour: 'Lock: Does not have permission' logged,
    replies_enabled unchanged.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    calls = record_moderation(monkeypatch, 'add_to_modlog')

    mod, community, post, parent_reply, child_reply, author = _seed_lockable_comment()
    # mod is deliberately NOT made a community moderator here.

    activity = inbox_activity(mod, activity_type='Lock',
                              object_uri=parent_reply.ap_id, summary='breaking the rules')

    dispatch(activity)

    db.session.expire_all()
    assert parent_reply.replies_enabled is True
    assert child_reply.replies_enabled is True
    assert len(calls['add_to_modlog']) == 0

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Lock: Does not have permission'


def _seed_lockable_post(instance_domain='peer.example', ap_id=None):
    """A community with one moderatable post, for the Lock arm's post branch
    (routes.py:1369-1378). `ap_id` defaults to containing '/post/' so
    routes.py:1360-1361 selects it via Post.get_by_ap_id; callers covering
    the neither-substring fallback (:1364-1367) pass an `ap_id` with neither
    substring.

    Returns (mod, community, post, author). `mod` is a plain remote User
    with `ap_fetched_at` stamped and is NEITHER a community moderator NOR an
    instance admin -- callers that need one grant it themselves:
    make_community_member(mod, community, is_moderator=True) for the
    former; an InstanceRole row naming community.instance_id and mod.id for
    the latter, since Community.is_instance_admin is scoped to the
    COMMUNITY's home instance (app/models.py:752-759), not the user's --
    make_community hardcodes community.instance_id to 1, the same instance
    id seed_community_owner's Instance row gets.
    """
    instance = seed_community_owner(instance_domain)  # instance id 1 + local owner user id 1
    community = make_community(host=instance_domain)
    author = make_user(instance, 'postauthor')
    post = make_post(community, author, ap_id=ap_id or f'https://{instance_domain}/post/1')

    mod = make_user(instance, 'moduser')
    mod.ap_fetched_at = utcnow()
    db.session.commit()

    return mod, community, post, author


def test_a_moderator_can_lock_a_post(app, db_session, monkeypatch):
    """routes.py:1360-1361 ('/post/' substring selects Post.get_by_ap_id)
    and :1369-1376 (the moderator alternative of the post permission guard).

    This mod is a community MODERATOR and deliberately NOT an instance
    admin (no InstanceRole row exists at all): is_moderator is True,
    is_instance_admin is False. That makes this test -- not
    test_an_instance_admin_can_lock_a_post below -- the one that kills the
    mutant dropping the FIRST disjunct of `post.community.is_moderator(mod)
    or post.community.is_instance_admin(mod)` (:1370): with only
    is_instance_admin left, this mod's permission check goes False and the
    SUCCESS assertions below fail. Asserts the modlog call's target_user,
    community and post arguments, not merely that it was called.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    calls = record_moderation(monkeypatch, 'add_to_modlog')

    mod, community, post, author = _seed_lockable_post()
    make_community_member(mod, community, is_moderator=True)

    activity = inbox_activity(mod, activity_type='Lock',
                              object_uri=post.ap_id, summary='breaking the rules')

    dispatch(activity)

    db.session.expire_all()
    assert post.comments_enabled is False

    assert len(calls['add_to_modlog']) == 1
    args, kwargs = calls['add_to_modlog'][0]
    assert args == ('lock_post',)
    assert sa_inspect(kwargs['actor']).identity[0] == mod.id
    assert sa_inspect(kwargs['target_user']).identity[0] == author.id
    assert kwargs['reason'] == 'breaking the rules'
    assert sa_inspect(kwargs['community']).identity[0] == community.id
    assert sa_inspect(kwargs['post']).identity[0] == post.id
    assert kwargs['link'] == f'post/{post.id}'

    log = ActivityPubLog.query.one()
    assert log.result == 'success'
    assert log.exception_message is None


def test_an_instance_admin_can_lock_a_post(app, db_session, monkeypatch):
    """routes.py:1370's second disjunct, `post.community.is_instance_admin
    (mod)`. This mod is an INSTANCE ADMIN of the community's home instance
    and deliberately NOT a community moderator (no CommunityMember row at
    all): is_instance_admin is True, is_moderator is False. That makes this
    test the one that kills the mutant dropping the SECOND disjunct: with
    only is_moderator left, this mod's permission check goes False and the
    SUCCESS assertions below fail.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    calls = record_moderation(monkeypatch, 'add_to_modlog')

    mod, community, post, author = _seed_lockable_post()
    db.session.add(InstanceRole(instance_id=community.instance_id, user_id=mod.id, role='admin'))
    db.session.commit()

    activity = inbox_activity(mod, activity_type='Lock',
                              object_uri=post.ap_id, summary='breaking the rules')

    dispatch(activity)

    db.session.expire_all()
    assert post.comments_enabled is False
    assert len(calls['add_to_modlog']) == 1
    args, kwargs = calls['add_to_modlog'][0]
    assert args == ('lock_post',)

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_a_non_moderator_locking_a_post_is_refused(app, db_session, monkeypatch):
    """routes.py:1377-1378. mod is neither a community moderator nor an
    instance admin here, so both disjuncts of :1370 are False.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    calls = record_moderation(monkeypatch, 'add_to_modlog')

    mod, community, post, author = _seed_lockable_post()
    # mod is deliberately neither a moderator nor an instance admin here.

    activity = inbox_activity(mod, activity_type='Lock',
                              object_uri=post.ap_id, summary='breaking the rules')

    dispatch(activity)

    db.session.expire_all()
    assert post.comments_enabled is True
    assert len(calls['add_to_modlog']) == 0

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Lock: Does not have permission'


def test_lock_of_unknown_object_is_reported_not_found(app, db_session, monkeypatch):
    """routes.py:1392-1393. The object URL has neither '/post/' nor
    '/comment/', and both of the fallback's lookups (:1365-1367) miss: no
    Post and no PostReply has this ap_id.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    calls = record_moderation(monkeypatch, 'add_to_modlog')

    mod, community, post, author = _seed_lockable_post()

    activity = inbox_activity(mod, activity_type='Lock',
                              object_uri='https://peer.example/things/does-not-exist',
                              summary='breaking the rules')

    dispatch(activity)

    assert len(calls['add_to_modlog']) == 0
    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Lock: post not found'


def test_lock_fallback_tries_post_before_post_reply(app, db_session, monkeypatch):
    """routes.py:1364-1366. An object URL with neither '/post/' nor
    '/comment/' resolves via the fallback's FIRST attempt, Post.get_by_ap_id
    (:1365) -- proved here by giving a PostReply the IDENTICAL ap_id: Post
    and PostReply are separate tables with independent ap_id uniqueness, so
    nothing stops both matching the same string. If the fallback tried
    PostReply first, or unconditionally, the reply would be the one locked
    instead of the post.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    calls = record_moderation(monkeypatch, 'add_to_modlog')

    ambiguous_ap_id = 'https://peer.example/things/1'
    mod, community, post, author = _seed_lockable_post(ap_id=ambiguous_ap_id)
    make_community_member(mod, community, is_moderator=True)

    reply = make_post_reply(post, author)
    reply.ap_id = ambiguous_ap_id
    db.session.commit()

    activity = inbox_activity(mod, activity_type='Lock',
                              object_uri=ambiguous_ap_id, summary='breaking the rules')

    dispatch(activity)

    db.session.expire_all()
    assert post.comments_enabled is False
    assert reply.replies_enabled is True

    assert len(calls['add_to_modlog']) == 1
    args, kwargs = calls['add_to_modlog'][0]
    assert args == ('lock_post',)

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_lock_fallback_second_half_resolves_a_reply_when_no_post_matches(app, db_session, monkeypatch):
    """routes.py:1366-1367. An object URL with neither substring, where
    Post.get_by_ap_id (:1365) returns None, falls through to
    PostReply.get_by_ap_id (:1367) -- the fallback's second half, which the
    brief calls out as needing its own test distinct from the '/post/' and
    '/comment/' substring paths and the fallback's first half above.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    calls = record_moderation(monkeypatch, 'add_to_modlog')

    mod, community, post, parent_reply, child_reply, author = _seed_lockable_comment()
    fallback_ap_id = 'https://peer.example/things/2'
    parent_reply.ap_id = fallback_ap_id
    db.session.commit()
    make_community_member(mod, community, is_moderator=True)

    activity = inbox_activity(mod, activity_type='Lock',
                              object_uri=fallback_ap_id, summary='breaking the rules')

    dispatch(activity)

    db.session.expire_all()
    assert parent_reply.replies_enabled is False
    assert child_reply.replies_enabled is False

    assert len(calls['add_to_modlog']) == 1
    args, kwargs = calls['add_to_modlog'][0]
    assert args == ('lock_post_reply',)

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_a_nodebb_reply_whose_url_contains_post_can_be_locked(app, db_session, monkeypatch):
    """D104, fixed. NodeBB replies carry '/post/' in their ap_id, and the
    '/post/' branch tried only Post.get_by_ap_id, so such a reply could not
    be locked. It now falls back to PostReply on a miss, as the Undo/Lock
    arm and the no-substring branch already do.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)

    mod, community, post, parent_reply, child_reply, author = _seed_lockable_comment()
    nodebb_ap_id = 'https://peer.example/post/99'
    parent_reply.ap_id = nodebb_ap_id
    db.session.commit()
    make_community_member(mod, community, is_moderator=True)

    activity = inbox_activity(mod, activity_type='Lock', object_uri=nodebb_ap_id)

    dispatch(activity)

    db.session.expire_all()
    assert parent_reply.replies_enabled is False
    assert child_reply.replies_enabled is False
    assert ActivityPubLog.query.one().result == 'success'


def test_a_delete_naming_an_unknown_feed_is_refused(app, db_session, monkeypatch):
    """routes.py:1268-1279 (pre-fix numbering). The feed lookup returns None
    -- no Feed row on this instance has this `ap_public_url` -- and
    (pre-fix) :1273 reads `feed.user_id` before the `if feed:` guard at
    :1278 is ever reached, so the `else` branch written to log exactly this
    case is unreachable and an AttributeError escapes instead.

    Asserts the corrected behaviour: the not-found failure is logged.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)

    instance = make_instance('peer.example')
    user = make_user(instance, 'alice')
    user.ap_fetched_at = utcnow()
    db.session.commit()

    missing_feed_id = 'https://peer.example/f/does-not-exist'
    activity = inbox_activity(user, activity_type='Delete',
                              object={'type': 'Feed', 'id': missing_feed_id})

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == f'Delete: cannot find {missing_feed_id}'


def test_a_delete_from_an_unresolvable_actor_is_refused(app, db_session, monkeypatch):
    """routes.py:1268 and :1273 (pre-fix numbering). find_actor_or_create_cached
    can return None -- validate_remote_actor refuses banned and malformed
    actors -- and (pre-fix) :1273 then reads `user.id` unguarded.

    The preamble (routes.py:871) and this arm's own re-lookup (:1268) call
    find_actor_or_create_cached with the identical `actor_id`, so getting a
    real actor from the first call and None from the second -- the exact
    shape this defect needs -- is simulated with a stateful stub rather than
    two different real actors: production has no state that changes between
    the two calls within one request, so this is the only way to exercise
    :1273 with `user` None while still reaching the Delete/Feed arm at all
    (an unresolvable actor at the PREAMBLE's own lookup is refused earlier,
    at routes.py:890-892, before the Delete arm is ever reached).

    Asserts the corrected behaviour: a logged failure rather than a crash.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)

    instance = make_instance('peer.example')
    user = make_user(instance, 'alice')
    user.ap_fetched_at = utcnow()
    feed = make_feed(instance)
    feed.user_id = user.id
    db.session.commit()

    call_count = [0]

    def _resolves_once_then_vanishes(*args, **kwargs):
        call_count[0] += 1
        return user if call_count[0] == 1 else None

    monkeypatch.setattr(activitypub_routes, 'find_actor_or_create_cached',
                        _resolves_once_then_vanishes)

    activity = inbox_activity(user, activity_type='Delete',
                              object={'type': 'Feed', 'id': feed.ap_public_url})

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Delete rejected, could not find the sender.'


def test_an_instance_admin_can_lock_a_comment(app, db_session, monkeypatch):
    """routes.py:1380's second disjunct, `post_reply.community.
    is_instance_admin(mod)` -- the corrected comment-branch counterpart to
    test_an_instance_admin_can_lock_a_post. This mod is an INSTANCE ADMIN of
    the community's home instance and deliberately NOT a community
    moderator (no CommunityMember row at all): is_instance_admin is True,
    is_moderator is False. That makes this test -- not
    test_a_moderator_can_lock_a_comment above -- the one that kills the
    mutant dropping this disjunct: with only is_moderator left, this mod's
    permission check goes False and the SUCCESS assertions below fail.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    calls = record_moderation(monkeypatch, 'add_to_modlog')

    mod, community, post, parent_reply, child_reply, author = _seed_lockable_comment()
    db.session.add(InstanceRole(instance_id=community.instance_id, user_id=mod.id, role='admin'))
    db.session.commit()

    activity = inbox_activity(mod, activity_type='Lock',
                              object_uri=parent_reply.ap_id, summary='breaking the rules')

    dispatch(activity)

    db.session.expire_all()
    assert parent_reply.replies_enabled is False
    assert child_reply.replies_enabled is False
    assert len(calls['add_to_modlog']) == 1
    args, kwargs = calls['add_to_modlog'][0]
    assert args == ('lock_post_reply',)

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


# --- Task 5: the rest of the Delete arm ---
#
# The brief for this task (task-5-brief.md) cites routes.py:1264-1327 and a
# set of line numbers within that range for each sub-step. Every one of
# those line citations is STALE: Task 4's fix (removing an unreachable
# `else:` and de-indenting the feed-deletion body) shifted everything from
# roughly :1270 onward by 13-17 lines. Re-read against current source, the
# arm now runs :1264-1330. Corrected citations, used throughout this
# section instead of the brief's:
#   - non-owner refusal message:            :1283          (brief said :1273-1275)
#   - the three per-row delete loops:        :1287-1300     (brief didn't cite these directly)
#   - feed-row delete + SUCCESS log:         :1301-1304
#   - bare-string (Lemmy) shape:             :1306-1307     (brief said :1302)
#   - dict-with-id (kbin) shape:             :1308-1309     (brief said :1304)
#   - find_liked_object call:                :1310
#   - already-deleted / IGNORED branch:      :1312-1315     (brief said :1306-1317)
#   - success delete + conditional announce: :1316-1320
#   - PM found branch:                       :1322-1329     (brief said :1319-1326)
#   - PM not found (fully silent):           falls through to the bare `return` at :1330


def _seed_feed_with_owner(host='peer.example'):
    """A Feed whose `user_id` names a real, resolvable local-to-the-test
    sender, for the Feed-delete arm's owner check (routes.py:1282-1284).
    `seed_community_owner` is used (not a bare `make_instance`) so callers
    that also need Communities for FeedItems can add them afterward without
    a second instance-id-1 collision.
    """
    instance = seed_community_owner(host)
    sender = make_user(instance, 'feedowner')
    sender.ap_fetched_at = utcnow()
    feed = make_feed(instance)
    feed.user_id = sender.id
    db.session.commit()
    return instance, sender, feed


def test_a_delete_of_a_feed_removes_items_members_and_join_requests(app, db_session, monkeypatch):
    """routes.py:1286-1304 (post Task-4 fix numbering). The owner-matched
    success path runs three separate per-row delete loops -- FeedItem,
    FeedMember, FeedJoinRequest, each committing after every row -- then
    deletes the Feed row itself and logs SUCCESS naming the feed's
    `ap_public_url`. Two FeedItems, two FeedMembers and one
    FeedJoinRequest are seeded so each loop is proven to walk ALL of its
    rows, not just stop after one.

    Also confirms the Feed shape's early return (routes.py:1305, the
    `return` ending this branch): find_liked_object is monkeypatched to
    record calls, and none arrive, proving this shape never falls into the
    shared post/PM continuation the other two object shapes share.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, feed = _seed_feed_with_owner()

    community_a = make_community(name='community-a', host='peer.example')
    community_b = make_community(name='community-b', host='peer.example')
    make_feed_item(feed, community_a)
    make_feed_item(feed, community_b)

    member_1 = make_user(instance, 'member1')
    member_2 = make_user(instance, 'member2')
    make_feed_member(member_1, feed)
    make_feed_member(member_2, feed)

    joiner = make_user(instance, 'joiner')
    make_feed_join_request(joiner, feed)

    feed_id = feed.id
    feed_url = feed.ap_public_url

    find_liked_calls = []
    monkeypatch.setattr(activitypub_routes, 'find_liked_object',
                        lambda *a, **k: find_liked_calls.append((a, k)))

    activity = inbox_activity(sender, activity_type='Delete',
                              object={'type': 'Feed', 'id': feed_url})

    dispatch(activity)

    db.session.expire_all()
    assert FeedItem.query.filter_by(feed_id=feed_id).count() == 0
    assert FeedMember.query.filter_by(feed_id=feed_id).count() == 0
    assert FeedJoinRequest.query.filter_by(feed_id=feed_id).count() == 0
    assert Feed.query.filter_by(id=feed_id).first() is None
    assert find_liked_calls == []

    log = ActivityPubLog.query.one()
    assert log.result == 'success'
    assert log.exception_message == f'Delete: Feed {feed_url} deleted'


def test_a_delete_of_a_feed_by_a_non_owner_is_refused(app, db_session, monkeypatch):
    """routes.py:1282-1284 (post Task-4 fix numbering; brief's stale
    citation was :1273-1275). Both actor and feed resolve, but
    `user.id != feed.user_id` -- a feed that genuinely belongs to someone
    else. Asserts the failure log and that nothing about the feed changed.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    owner = make_user(instance, 'owner')
    sender = make_user(instance, 'notowner')
    sender.ap_fetched_at = utcnow()
    feed = make_feed(instance)
    feed.user_id = owner.id
    db.session.commit()

    activity = inbox_activity(sender, activity_type='Delete',
                              object={'type': 'Feed', 'id': feed.ap_public_url})

    dispatch(activity)

    assert db.session.get(Feed, feed.id) is not None

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Delete rejected, request came from non-owner.'


def _seed_deletable_post(host='peer.example', ap_id_suffix='1'):
    """A community with one deletable Post, for the shared post/PM
    continuation (routes.py:1306-1330). `sender` is a distinct user from
    `author` -- the arm never checks that the Delete's sender authored the
    content (that check lives inside the mocked `delete_post_or_comment`
    delegate, out of scope here), so using two different users proves the
    arm passes `user` through as-is rather than silently substituting the
    author.
    """
    instance = seed_community_owner(host)
    community = make_community(host=host)
    author = make_user(instance, 'author')
    sender = make_user(instance, 'sender')
    sender.ap_fetched_at = utcnow()
    db.session.commit()
    post = make_post(community, author, ap_id=f'https://{host}/post/{ap_id_suffix}')
    return instance, community, sender, author, post


def test_delete_of_a_bare_string_object_extracts_ap_id_and_deletes_the_content(
        app, db_session, monkeypatch):
    """routes.py:1306-1307 (brief's stale citation was :1302): `object` is a
    bare string (Lemmy shape) -- `ap_id = object` directly, no `['id']`
    indexing. Also covers :1316-1320's not-yet-deleted success path: the
    ap_id that reaches find_liked_object resolves to the real seeded Post,
    proven by asserting `delete_post_or_comment`'s SECOND argument's
    identity, not merely that the call happened.

    delete_post_or_comment is called with the reason taken from
    `summary` (:1317), and -- because this activity is NOT wrapped in an
    Announce, so `announced` is False -- announce_activity_to_followers IS
    called (:1319-1320's `if not announced:` guard), with
    `(to_delete.community, user, request_json)`.

    Also asserts ActivityPubLog.query.count() == 0: per this arm's own
    module docstring (Task 1's table), the successful-delete path logs
    NOTHING -- no `log_incoming_ap` call exists anywhere on it.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, sender, author, post = _seed_deletable_post()

    calls = record_moderation(monkeypatch, 'delete_post_or_comment', 'announce_activity_to_followers')

    activity = inbox_activity(sender, activity_type='Delete',
                              object_uri=post.ap_id, summary='rule violation')

    dispatch(activity)

    assert len(calls['delete_post_or_comment']) == 1
    args, kwargs = calls['delete_post_or_comment'][0]
    assert kwargs == {}
    user_arg, to_delete_arg, store_ap_json_arg, request_json_arg, reason_arg = args
    assert sa_inspect(user_arg).identity[0] == sender.id
    assert sa_inspect(to_delete_arg).identity[0] == post.id
    assert store_ap_json_arg is True
    assert request_json_arg is activity
    assert reason_arg == 'rule violation'

    assert len(calls['announce_activity_to_followers']) == 1
    a_args, a_kwargs = calls['announce_activity_to_followers'][0]
    assert a_kwargs == {}
    community_arg, announce_user_arg, announce_request_arg = a_args
    assert sa_inspect(community_arg).identity[0] == community.id
    assert sa_inspect(announce_user_arg).identity[0] == sender.id
    assert announce_request_arg is activity

    assert ActivityPubLog.query.count() == 0


def test_delete_of_a_dict_object_with_id_is_ignored_when_already_deleted(app, db_session, monkeypatch):
    """routes.py:1308-1309 (brief's stale citation was :1304): `object` is a
    dict without `type == 'Feed'` (kbin shape) -- `ap_id = object['id']`.
    Also covers :1312-1315's already-deleted branch: `to_delete.deleted` is
    True (set directly here, since Post.deleted defaults False on the model
    (app/models.py:1682) -- this write is what proves the branch, not a
    default already sitting there), so IGNORED is logged and neither
    delegate runs.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, sender, author, post = _seed_deletable_post(ap_id_suffix='99')
    post.deleted = True
    db.session.commit()

    calls = record_moderation(monkeypatch, 'delete_post_or_comment', 'announce_activity_to_followers')

    activity = inbox_activity(sender, activity_type='Delete',
                              object={'id': post.ap_id, 'type': 'Note'})

    dispatch(activity)

    assert calls['delete_post_or_comment'] == []
    assert calls['announce_activity_to_followers'] == []

    log = ActivityPubLog.query.one()
    assert log.result == 'ignored'
    assert log.exception_message == 'Activity about local content which is already deleted'


def _seed_announcing_community(host='announcer.example'):
    """A Community resolvable as the OUTER Announce actor (routes.py:862),
    mirroring test_inbox_dispatch_announce.py's own private helper of the
    same name -- duplicated rather than imported, since that module's
    helpers are underscore-private to their own file.
    """
    make_site()
    instance = make_instance(host)
    make_user(instance, 'community_owner')
    community = make_community(host=host)
    community.ap_fetched_at = utcnow()
    db.session.commit()
    return instance, community


def test_delete_announced_does_not_reannounce(app, db_session, monkeypatch):
    """routes.py:1319-1320's `if not announced:` guard, the other half of
    the pair proven above: this Delete arrives wrapped in a real Announce
    (the only way `announced` becomes True for this arm -- there is no
    separate `process_delete` function taking `announced` as a parameter
    the way process_upvote does), so `announce_activity_to_followers` must
    NOT be called even though the content deletion itself still goes
    through -- delete_post_or_comment is unconditional on this path,
    :1318.

    `request_json` passed to delete_post_or_comment is the OUTER Announce
    activity (`activity`), not the inner Delete object -- routes.py always
    threads the ORIGINAL request_json through, exactly as
    test_inbox_dispatch_announce.py's own
    test_an_announce_sets_core_activity_to_the_inner_object establishes for
    the Like arm.
    """
    instance, community = _seed_announcing_community()
    author = make_user(instance, 'author')
    inner_sender = make_user(instance, 'inner_sender')
    inner_sender.ap_fetched_at = utcnow()
    db.session.commit()
    post = make_post(community, author, ap_id=f'https://{community.ap_domain}/post/1')

    calls = record_moderation(monkeypatch, 'delete_post_or_comment', 'announce_activity_to_followers')

    inner_delete = {
        'id': f'https://{community.ap_domain}/activities/delete-1',
        'type': 'Delete',
        'actor': inner_sender.ap_profile_id,
        'object': post.ap_id,
        'summary': 'rule violation',
    }
    activity = inbox_activity(community, activity_type='Announce', object=inner_delete)

    dispatch(activity)

    assert len(calls['delete_post_or_comment']) == 1
    args, kwargs = calls['delete_post_or_comment'][0]
    user_arg, to_delete_arg, store_ap_json_arg, request_json_arg, reason_arg = args
    assert sa_inspect(user_arg).identity[0] == inner_sender.id
    assert sa_inspect(to_delete_arg).identity[0] == post.id
    assert request_json_arg is activity
    assert reason_arg == 'rule violation'

    assert len(calls['announce_activity_to_followers']) == 0


def test_delete_of_a_chat_message_marks_it_read_and_deleted(app, db_session, monkeypatch):
    """routes.py:1322-1329 (brief's stale citation was :1319-1326): neither
    find_liked_object nor a Post/PostReply matches this ap_id, but a
    ChatMessage does (matched on `ap_id` AND `sender_id == user.id`) -- the
    PM path. `read` and `deleted` both default False on the model
    (app/models.py:291/295), so both flipping True proves the write.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    sender = make_user(instance, 'sender')
    sender.ap_fetched_at = utcnow()
    recipient = make_user(None, 'recipient', local=True)
    conversation = Conversation(user_id=sender.id)
    db.session.add(conversation)
    db.session.commit()

    ap_id = 'https://peer.example/private-message/1'
    message = ChatMessage(sender_id=sender.id, recipient_id=recipient.id,
                          conversation_id=conversation.id, body='hi', ap_id=ap_id,
                          read=False, deleted=False)
    db.session.add(message)
    db.session.commit()

    activity = inbox_activity(sender, activity_type='Delete', object_uri=ap_id)

    dispatch(activity)

    updated = ChatMessage.query.filter_by(ap_id=ap_id).one()
    assert updated.read is True
    assert updated.deleted is True

    log = ActivityPubLog.query.one()
    assert log.result == 'success'
    assert log.exception_message == f'Delete: PM {ap_id} deleted'


def test_delete_of_an_unmatched_ap_id_logs_nothing(app, db_session, monkeypatch):
    """routes.py: falls through to the bare `return` at :1330 -- neither
    find_liked_object nor the ChatMessage lookup matches anything at all.
    REGISTERED, not fixed: this is the arm's only fully-silent no-op, per
    Task 1's module docstring table ('shared: nothing found at all').

    LOG_ACTIVITYPUB_TO_DB is explicitly enabled here (unlike this suite's
    usual default of leaving it off) specifically so that
    ActivityPubLog.query.count() == 0 proves the silence, rather than
    merely reflecting logging being disabled for an unrelated reason --
    with logging off, the count would be 0 regardless of what this arm
    does, making the assertion vacuous.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    sender = make_user(instance, 'sender')
    sender.ap_fetched_at = utcnow()
    db.session.commit()

    activity = inbox_activity(sender, activity_type='Delete',
                              object_uri='https://peer.example/objects/does-not-exist')

    dispatch(activity)

    assert ActivityPubLog.query.count() == 0


def test_delete_of_a_dict_object_with_no_type_key_takes_the_kbin_path(app, db_session, monkeypatch):
    """D92, fixed. The Feed-vs-other selector read `object['type']` once the
    object was a dict, so a dict with an id but no 'type' raised KeyError
    before its id was ever used. Only a Feed needs the type; any other dict
    is handled by its id (the kbin shape), so an untyped dict now reaches
    the same continuation and its content is deleted.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, sender, author, post = _seed_deletable_post()

    calls = record_moderation(monkeypatch, 'delete_post_or_comment', 'announce_activity_to_followers')

    activity = inbox_activity(sender, activity_type='Delete', object={'id': post.ap_id})

    dispatch(activity)

    assert len(calls['delete_post_or_comment']) == 1
    args, _kwargs = calls['delete_post_or_comment'][0]
    assert sa_inspect(args[1]).identity[0] == post.id


def test_delete_of_a_dict_object_with_no_id_is_refused(app, db_session, monkeypatch):
    """D92, fixed. With the type read made safe, a dict object with neither
    'type' nor 'id' would raise KeyError at the kbin path's `object['id']`
    instead. It is refused and logged, and nothing is deleted.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, sender, author, post = _seed_deletable_post()

    calls = record_moderation(monkeypatch, 'delete_post_or_comment', 'announce_activity_to_followers')

    activity = inbox_activity(sender, activity_type='Delete', object={'summary': 'no id here'})

    dispatch(activity)

    assert calls['delete_post_or_comment'] == []
    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Delete object has no id'

