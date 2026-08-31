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

### Delete (routes.py:1264-1327)

`core_activity['object']` is inspected THREE different ways before anything
is deleted: a dict with `type == 'Feed'` is a feed deletion, handled
entirely inside this arm; a bare string (Lemmy) or a dict without that type
(kbin) both reduce to an `ap_id` and fall into a shared continuation that
looks the target up as content, then as a private message.

| branch (lines)                                                | selects it                                                                                                                    | writes                                                                                                                                                    | delegates to                                                                                          | logs |
|-----------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------|------|
| Feed-delete, unresolved actor or feed (1268-1273)               | `object` is a dict with `type == 'Feed'`                                                                                       | nothing                                                                                                                                                        | `find_actor_or_create_cached(actor_id)` for `user`; a raw `session.query(Feed)` (not a delegate function) for `feed` | **CRASHES before any log call.** `:1273`'s `user.id == feed.user_id` dereferences `user.id` (None if the actor did not resolve) or `feed.user_id` (None if no feed row matched) with no guard on either -- an unhandled `AttributeError` escapes `process_inbox_request` instead of reaching a log call. Registered; Task 4's to fix, not this task's. |
| Feed-delete, owner mismatch (1273-1275)                         | both resolved, and `user.id != feed.user_id`                                                                                    | nothing                                                                                                                                                        | nothing                                                                                                    | APLOG_DELETE/APLOG_FAILURE 'Delete rejected, request came from non-owner.' |
| Feed-delete, success (1277-1298)                                | owner matches; reaches `if feed:`, which is unconditionally True here (see note below)                                        | deletes every `FeedItem` row for `feed.id` (commit per row), every `FeedMember` row (commit per row), every `FeedJoinRequest` row (commit per row), then the `Feed` row itself (commit) | nothing -- every delete goes through the task-local `session` directly, no helper function is called      | APLOG_DELETE/APLOG_SUCCESS naming the feed's `object['id']` |
| Feed-delete, `else: feed not found` (1299-1301)                 | would fire when `feed` is falsy at `:1278`                                                                                     | nothing                                                                                                                                                        | nothing                                                                                                    | **DEAD CODE.** The only way to reach `:1278` with `feed` falsy is `feed is None`, and that already raised at `:1273` before this line is ever reached -- a `Feed` row the ORM returns has no `__bool__`/`__len__` override, so it is never falsy on its own. This log line can never fire while `:1273` reads `feed.user_id` unguarded. |
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
| post None, post_reply, moderator branch of the `or` is True (1379-1389) | `post` falsy, `post_reply` truthy, `post_reply.community.is_moderator(mod)` True -- the `or`'s second operand (`post.community.is_instance_admin(mod)`) is short-circuited and never evaluated here | `post_reply.replies_enabled = False`; a raw-SQL subtree UPDATE sets `replies_enabled = False` for every reply whose `path` contains `post_reply.id`; commit. **These writes DO happen.** | **CRASHES immediately after the writes, before any delegate runs.** `add_to_modlog(..., target_user=post.author, ...)` at `:1386` dereferences `post`, which is guaranteed `None` in this branch (reaching `elif post_reply:` requires the earlier `if post:` to have been falsy) -- `AttributeError: 'NoneType' object has no attribute 'author'` aborts the call before `add_to_modlog` runs and before either the modlog entry or the `:1387` `community=post.community` keyword is ever evaluated. | **NOTHING.** The intended APLOG_LOCK/APLOG_SUCCESS at `:1389` is never reached, and the writes above are never rolled back either -- they were already committed at `:1385`, one statement before the crash. `session.rollback()` in the arm's outer `except Exception:` (`:1885`) has nothing pending left to undo. |
| post None, post_reply, moderator branch of the `or` is False (1379-1380) | `post` falsy, `post_reply` truthy, `post_reply.community.is_moderator(mod)` False                                | **NONE.** Unlike the row above, this path crashes before any write.                                              | **CRASHES while evaluating the `if` condition itself.** Because the first disjunct is False, Python evaluates the second, `post.community.is_instance_admin(mod)`, with `post` `None` -- `AttributeError: 'NoneType' object has no attribute 'community'` fires before the loop body or either arm of the intended if/else runs. | **NOTHING.** The intended APLOG_LOCK/APLOG_FAILURE 'Lock: Does not have permission' at `:1391` is never reached. |
| neither post nor post_reply resolved (1392-1393)                    | both `post` and `post_reply` falsy after target resolution                                                        | nothing                                                                                                          | nothing                                                                                                                                    | APLOG_LOCK/APLOG_FAILURE 'Lock: post not found' |

Net effect of the two crash rows: given current source, a federated Lock of
a COMMENT can never complete successfully or be cleanly refused -- both of
its outcomes raise an unhandled `AttributeError` that escapes
`process_inbox_request` (caught only by the bare `except Exception: ...;
raise` at `:1885`, which rolls back and re-raises). Locking a POST is
unaffected; only the `post_reply` half is broken. This is the "three
dereferences of `post`" the task brief names -- concretely, of the three
textual references (`:1380`'s second disjunct, `:1386`, `:1387`), only ONE
ever executes per call: the moderator branch reaches `:1386` and dies
there before `:1387` is evaluated (Python evaluates keyword arguments left
to right and aborts on the first exception); the non-moderator branch dies
at `:1380` and never reaches `:1386`/`:1387` at all. Registered; Task 2's
to fix, not this task's.

### Observations beyond the two registered defects

  - Delete's own selector for the Feed-vs-other split, `:1266`
    (`core_activity['object']['type'] == 'Feed'`), reads `['type']`
    unconditionally once `object` is confirmed a dict. A dict `object` with
    no `'type'` key at all raises `KeyError` right there, before either
    registered defect is reached and before ANY `ap_id` extraction --
    a third crash path in this arm, distinct from the two named in the
    plan. Not registered by this task's brief; noted here for whoever
    triages defects next, not treated as this task's to fix.
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
from app.models import ActivityPubLog, FeedItem, FeedMember, InstanceRole, utcnow
from tests.factories import inbox_activity, make_community, make_community_member, make_feed, \
    make_feed_item, make_feed_member, make_instance, make_post, make_post_reply, make_user, \
    seed_community_owner
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
    tests/conftest.py:394 documents.

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
