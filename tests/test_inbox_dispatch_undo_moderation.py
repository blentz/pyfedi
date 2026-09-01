"""tests/test_inbox_dispatch_undo_moderation.py"""
from sqlalchemy import inspect as sa_inspect

from app import db
from app.activitypub import routes as activitypub_routes
from app.models import ActivityPubLog, InstanceRole, User, utcnow
from tests.factories import (inbox_activity, make_community, make_community_member,
                             make_instance, make_post, make_post_reply, make_site,
                             make_user, seed_community_owner)
from tests.test_inbox_dispatch_lock_delete import record_moderation
from tests.test_inbox_dispatch_preamble import dispatch


def undo_lock_activity(actor, target_ap_id, **outer):
    """An Undo wrapping a Lock of `target_ap_id`."""
    return inbox_activity(actor, activity_type='Undo',
                          object={'type': 'Lock', 'object': target_ap_id}, **outer)


def _seed_lockable_post(host='peer.example'):
    """A community whose owner is also a moderator, plus a locked post."""
    instance = seed_community_owner(host)
    mod = make_user(instance, 'mod')
    community = make_community(host=host)
    make_community_member(mod, community, is_moderator=True)
    author = make_user(instance, 'author')
    post = make_post(community, author, f'https://{host}/post/1')
    post.comments_enabled = False
    db.session.commit()
    return instance, mod, community, author, post


def test_a_successful_post_unlock_logs_success_and_nothing_else(app, db_session, monkeypatch):
    """FIX 3. The failure log now fires only when NEITHER a post nor a reply
    was found, so a successful post unlock records exactly one row.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, mod, community, author, post = _seed_lockable_post()
    record_moderation(monkeypatch, 'add_to_modlog')
    post_id = post.id

    dispatch(undo_lock_activity(mod, post.ap_id))

    db.session.expire_all()
    assert db.session.get(type(post), post_id).comments_enabled is True

    logs = ActivityPubLog.query.all()
    assert len(logs) == 1
    assert logs[0].result == 'success'


def test_an_unlock_of_something_that_exists_nowhere_logs_not_found(app, db_session, monkeypatch):
    """The failure log's remaining reason to exist: neither a post nor a reply
    matched. Paired with the test above so the guard cannot be dropped in
    either direction.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, mod, community, author, post = _seed_lockable_post()

    dispatch(undo_lock_activity(mod, 'https://peer.example/post/404'))

    logs = ActivityPubLog.query.all()
    assert len(logs) == 1
    assert logs[0].result == 'failure'
    assert logs[0].exception_message == 'Unlock: post not found'


def test_the_comment_url_branch_selects_a_reply_directly(app, db_session, monkeypatch):
    """FIX 2. The membership test now runs against `target_ap_id`, the string,
    so an id containing '/comment/' selects PostReply WITHOUT first trying
    Post.get_by_ap_id. Proved by seeding a Post whose ap_id is identical to the
    reply's: if the else fallback were still running it would find that post
    first and unlock IT instead.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, mod, community, author, post = _seed_lockable_post()
    decoy = make_post(community, author, 'https://peer.example/comment/1')
    decoy.comments_enabled = False
    reply = make_post_reply(post, author)
    reply.ap_id = 'https://peer.example/comment/1'
    reply.replies_enabled = False
    db.session.commit()
    decoy_id, reply_id = decoy.id, reply.id

    record_moderation(monkeypatch, 'add_to_modlog')

    dispatch(undo_lock_activity(mod, 'https://peer.example/comment/1'))

    db.session.expire_all()
    assert db.session.get(type(reply), reply_id).replies_enabled is True
    assert db.session.get(type(decoy), decoy_id).comments_enabled is False  # decoy untouched


def test_a_nodebb_reply_whose_url_contains_post_still_falls_back_to_the_reply(
        app, db_session, monkeypatch):
    """FIX 2 REGRESSION. NodeBB replies carry '/post/' in their ap_id --
    app/activitypub/util.py:1984 calls this out by name as a misleading hint
    -- so routing every '/post/' id to Post.get_by_ap_id alone loses replies
    that live at such an id. Both canonical resolvers in this codebase keep
    a PostReply fallback for exactly this case: find_reply_parent
    (util.py:1970-1994) and _find_liked_object_id (util.py:2007-2022). Fix 2
    dropped that fallback from the Undo/Lock '/post/' branch; this restores
    it there, scoped to Undo/Lock only -- the sibling Lock arm has the same
    gap and is deliberately left untouched (registered as a separate
    finding, not this task's to fix).

    No Post carries this ap_id, only the PostReply does, so a correct
    fallback must reach the reply and log success; the pre-fix code finds
    neither object and logs 'Unlock: post not found' instead.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, mod, community, author, post = _seed_lockable_post()
    reply = make_post_reply(post, author)
    reply.ap_id = 'https://peer.example/post/999'  # NodeBB-style: '/post/' but not a Post row
    reply.replies_enabled = False
    db.session.commit()
    reply_id = reply.id

    dispatch(undo_lock_activity(mod, reply.ap_id))

    db.session.expire_all()
    assert db.session.get(type(reply), reply_id).replies_enabled is True

    logs = ActivityPubLog.query.all()
    assert len(logs) == 1
    assert logs[0].result == 'success'


def test_unlocking_a_comment_records_the_reply_author_and_community(app, db_session, monkeypatch):
    """FIX 1, the twin of D97. The modlog entry now reads its author and
    community off `post_reply`, not off the always-None `post`. add_to_modlog
    is doubled so the arguments can be inspected -- which is only safe now that
    evaluating them no longer raises.

    The captured kwargs are the real objects the dispatcher's own independent
    session (get_task_session()) loaded and, per routes.py's `finally:
    session.close()`, that session is closed before dispatch() returns here.
    Its objects were already expired by an intervening session.commit()
    before the (mocked) call captured them, so reading a plain attribute
    like `.id` off them re-triggers a load against a closed session and
    raises DetachedInstanceError. `sa_inspect(obj).identity[0]` reads the
    primary-key tuple SQLAlchemy stores on the instance's state at load
    time, which survives both expiration and detachment -- the same
    reasoning and pattern test_inbox_dispatch_lock_delete.py:266-274 already
    uses for this exact reason.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, mod, community, author, post = _seed_lockable_post()
    reply = make_post_reply(post, author)
    reply.ap_id = 'https://peer.example/comment/1'
    reply.replies_enabled = False
    db.session.commit()
    reply_id = reply.id

    calls = record_moderation(monkeypatch, 'add_to_modlog')

    dispatch(undo_lock_activity(mod, 'https://peer.example/comment/1'))

    db.session.expire_all()
    assert db.session.get(type(reply), reply_id).replies_enabled is True

    assert len(calls['add_to_modlog']) == 1
    args, kwargs = calls['add_to_modlog'][0]
    assert args[0] == 'unlock_post_reply'
    assert sa_inspect(kwargs['target_user']).identity[0] == author.id
    assert sa_inspect(kwargs['community']).identity[0] == community.id
    assert sa_inspect(kwargs['reply']).identity[0] == reply_id


def test_unlocking_a_comment_re_enables_the_descendant_subtree(app, db_session, monkeypatch):
    """The whole reason the raw-SQL `update post_reply set replies_enabled =
    :replies_enabled where path @> ARRAY[:parent_id]` exists (routes.py:1805):
    unlocking a reply must re-enable its descendants too, not just the reply
    itself. 5c's sibling `test_a_moderator_can_lock_a_comment`
    (tests/test_inbox_dispatch_lock_delete.py) asserts the LOCK direction
    propagates to a child reply; no test on the unlock side seeded a child at
    all, so this statement was executed but never behaviourally checked.

    `child_reply.path` follows PostReply.new()'s own convention
    (app/models.py:3016-3023): a root reply's path is `[0, self.id]`, a
    child's is its parent's path with its own id appended -- the same
    convention tests/test_inbox_dispatch_lock_delete.py's
    `_seed_lockable_comment` uses.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, mod, community, author, post = _seed_lockable_post()
    parent_reply = make_post_reply(post, author)
    parent_reply.ap_id = 'https://peer.example/comment/1'
    parent_reply.path = [0, parent_reply.id]
    parent_reply.replies_enabled = False
    db.session.commit()

    child_reply = make_post_reply(post, author)
    child_reply.path = [0, parent_reply.id, child_reply.id]
    child_reply.replies_enabled = False  # seeded explicitly False, not left at the column default
    db.session.commit()
    parent_id, child_id = parent_reply.id, child_reply.id

    dispatch(undo_lock_activity(mod, 'https://peer.example/comment/1'))

    db.session.expire_all()
    assert db.session.get(type(parent_reply), parent_id).replies_enabled is True
    assert db.session.get(type(child_reply), child_id).replies_enabled is True

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_unlocking_without_permission_logs_failure(app, db_session, monkeypatch):
    """The permission guard, which is NOT defective. A user who is neither
    moderator nor instance admin gets FAILURE and no unlock.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, mod, community, author, post = _seed_lockable_post()
    outsider = make_user(instance, 'outsider')
    post_id = post.id

    dispatch(undo_lock_activity(outsider, post.ap_id))

    db.session.expire_all()
    assert db.session.get(type(post), post_id).comments_enabled is False  # untouched
    logs = ActivityPubLog.query.order_by(ActivityPubLog.id).all()
    assert logs[0].result == 'failure'
    assert logs[0].exception_message == 'Unlock: Does not have permission'


def test_unlocking_a_comment_without_permission_logs_failure(app, db_session, monkeypatch):
    """The `post_reply` branch's OWN permission-denied log (routes.py:1814),
    a distinct statement from the post branch's identical string at :1801.
    `test_unlocking_without_permission_logs_failure` above only ever drives a
    POST through this guard -- this is its untested twin on the reply side.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, mod, community, author, post = _seed_lockable_post()
    reply = make_post_reply(post, author)
    reply.ap_id = 'https://peer.example/comment/1'
    reply.replies_enabled = False
    db.session.commit()
    reply_id = reply.id
    outsider = make_user(instance, 'outsider')

    dispatch(undo_lock_activity(outsider, reply.ap_id))

    db.session.expire_all()
    assert db.session.get(type(reply), reply_id).replies_enabled is False  # untouched
    logs = ActivityPubLog.query.order_by(ActivityPubLog.id).all()
    assert logs[0].result == 'failure'
    assert logs[0].exception_message == 'Unlock: Does not have permission'


def test_undo_lock_of_a_url_shaped_like_neither_post_nor_comment_falls_back_to_a_post(
        app, db_session, monkeypatch):
    """The `else:` fallback (routes.py:1787-1790), taken when `target_ap_id`
    contains neither '/post/' nor '/comment/' -- a Mastodon-style status URL
    is the real-world shape that lands here. This only became reachable when
    Task 9 made the '/post/' and '/comment/' branches test the target string
    itself rather than the surrounding dict; before that fix every id fell
    into this same `else`, but the two sibling branches this test
    distinguishes from didn't exist as live alternatives yet. This half of
    the fallback (`Post.get_by_ap_id` hits) is the one the '/post/' branch's
    own NodeBB regression test does NOT exercise, since that test's whole
    point is a `Post` lookup that MISSES.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, mod, community, author, post = _seed_lockable_post()
    post.ap_id = 'https://peer.example/statuses/1'  # neither '/post/' nor '/comment/'
    db.session.commit()
    post_id = post.id

    dispatch(undo_lock_activity(mod, post.ap_id))

    db.session.expire_all()
    assert db.session.get(type(post), post_id).comments_enabled is True
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_undo_lock_of_a_url_shaped_like_neither_post_nor_comment_falls_back_to_a_reply(
        app, db_session, monkeypatch):
    """The `else:` fallback's other half: when `Post.get_by_ap_id` misses,
    `PostReply.get_by_ap_id` is tried next -- the same two-step shape the
    '/post/' branch's own NodeBB fallback uses (see the regression test
    above), here on a target that matches neither hint string at all.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, mod, community, author, post = _seed_lockable_post()
    reply = make_post_reply(post, author)
    reply.ap_id = 'https://peer.example/statuses/2'  # neither '/post/' nor '/comment/'
    reply.replies_enabled = False
    db.session.commit()
    reply_id = reply.id

    dispatch(undo_lock_activity(mod, reply.ap_id))

    db.session.expire_all()
    assert db.session.get(type(reply), reply_id).replies_enabled is True
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


# --- Task 10: the Undo/Block arm (routes.py:1819-1869) -- both unban paths ---


def undo_block_activity(actor, blocked_ap_id, target, **outer):
    """An Undo wrapping a Block. `target` lives on the INNER object here,
    unlike the Block arm (test_inbox_dispatch_block.py) where it sits on the
    outer activity.
    """
    return inbox_activity(actor, activity_type='Undo',
                          object={'type': 'Block', 'object': blocked_ap_id,
                                  'target': target}, **outer)


def _seed_site_unban(host='peer.example', grant_admin=True):
    """Unblocker and unblocked share ONE instance, and unblocked is remote --
    the only combination that reaches the ordinary site-unban write, since the
    is_local() and cross-instance checks both return before it.
    """
    instance = make_instance(host)
    unblocker = make_user(instance, 'admin')
    unblocker.ap_fetched_at = utcnow()
    if grant_admin:
        db.session.add(InstanceRole(instance_id=instance.id, user_id=unblocker.id, role='admin'))
    victim = make_user(instance, 'victim')
    victim.banned = True
    victim.banned_until = utcnow()
    db.session.commit()
    return instance, unblocker, victim


def test_site_unban_clears_banned_and_the_expiry(app, db_session, monkeypatch):
    """The ordinary site-unban write. Both columns are seeded to non-default
    values first, so clearing them is real evidence. This is the path whose
    correct use of `banned_until` was the decisive evidence in D91 that the
    Block arm's `ban_until` was a typo.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, unblocker, victim = _seed_site_unban()
    victim_id = victim.id

    dispatch(undo_block_activity(unblocker, victim.ap_profile_id, f'https://{instance.domain}'))

    db.session.expire_all()
    fresh = db.session.get(User, victim_id)
    assert fresh.banned is False
    assert fresh.banned_until is None
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_site_unban_by_a_non_admin_is_refused(app, db_session, monkeypatch):
    """`is_instance_admin()` guard, checked before locality."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, unblocker, victim = _seed_site_unban(grant_admin=False)
    victim_id = victim.id

    dispatch(undo_block_activity(unblocker, victim.ap_profile_id, f'https://{instance.domain}'))

    db.session.expire_all()
    assert db.session.get(User, victim_id).banned is True  # untouched
    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Does not have permission'


def test_site_unban_of_an_unknown_user_is_ignored(app, db_session, monkeypatch):
    """No User row matches the lowercased ap_profile_id."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, unblocker, victim = _seed_site_unban()

    dispatch(undo_block_activity(unblocker, 'https://peer.example/u/ghost',
                                 f'https://{instance.domain}'))

    log = ActivityPubLog.query.one()
    assert log.result == 'ignored'
    assert log.exception_message == 'Does not exist here'


def test_site_unban_of_a_local_user_delegates_to_unban_user(app, db_session, monkeypatch):
    """`unblocked.is_local()` -- delegates and returns before the direct write."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, unblocker, _ = _seed_site_unban()
    victim = make_user(None, 'victim2', local=True)
    victim.ap_profile_id = f"{app.config['SERVER_URL']}/u/victim2".lower()
    victim.banned = True
    db.session.commit()
    victim_id = victim.id

    calls = record_moderation(monkeypatch, 'unban_user')

    dispatch(undo_block_activity(unblocker, victim.ap_profile_id, f'https://{instance.domain}'))

    assert len(calls['unban_user']) == 1
    db.session.expire_all()
    assert db.session.get(User, victim_id).banned is True  # delegate was doubled
    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Remote Admin in unbanning one of our users from their site'


def test_site_unban_of_a_user_on_a_third_instance_is_only_monitored(app, db_session, monkeypatch):
    """`unblocked.instance_id != unblocker.instance_id` -- no unban at all."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, unblocker, _ = _seed_site_unban()
    other = make_instance('third.example')
    victim = make_user(other, 'victim3')
    victim.banned = True
    db.session.commit()
    victim_id = victim.id

    calls = record_moderation(monkeypatch, 'unban_user')

    dispatch(undo_block_activity(unblocker, victim.ap_profile_id, f'https://{instance.domain}'))

    assert calls['unban_user'] == []
    db.session.expire_all()
    assert db.session.get(User, victim_id).banned is True
    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Remote Admin is unbanning a user of a different instance from their site'


def test_community_unban_delegates_to_unban_user(app, db_session, monkeypatch):
    """target.count('/') >= 4 selects the community branch. The unblocker is
    seeded as a moderator so the permission guard passes.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    mod = make_user(instance, 'mod')
    community = make_community(host='peer.example')
    make_community_member(mod, community, is_moderator=True)
    victim = make_user(instance, 'victim')
    victim.banned = True
    db.session.commit()

    calls = record_moderation(monkeypatch, 'unban_user')

    dispatch(undo_block_activity(mod, victim.ap_profile_id, community.ap_profile_id))

    assert len(calls['unban_user']) == 1
    args, kwargs = calls['unban_user'][0]
    assert sa_inspect(args[2]).identity[0] == community.id
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_community_unban_without_permission_is_refused(app, db_session, monkeypatch):
    """Neither moderator nor instance admin."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    outsider = make_user(instance, 'outsider')
    community = make_community(host='peer.example')
    victim = make_user(instance, 'victim')
    victim.banned = True
    db.session.commit()

    calls = record_moderation(monkeypatch, 'unban_user')

    dispatch(undo_block_activity(outsider, victim.ap_profile_id, community.ap_profile_id))

    assert calls['unban_user'] == []
    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Does not have permission'


def test_community_unban_by_an_instance_admin_who_is_not_a_moderator_succeeds(app, db_session, monkeypatch):
    """The guard's OTHER half: `not community.is_moderator(unblocker) and not
    community.is_instance_admin(unblocker)`. This fixture makes
    `is_instance_admin` True (an InstanceRole naming the COMMUNITY's home
    instance, per Community.is_instance_admin's own instance_id,
    app/models.py:752-757 -- not the unblocker's own instance_id) and
    `is_moderator` False (no CommunityMember row at all). The two sibling
    tests above and below only ever seed a moderator, so neither can
    distinguish dropping this second conjunct from dropping the whole guard --
    this is the one that can.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    admin = make_user(instance, 'admin')
    community = make_community(host='peer.example')
    db.session.add(InstanceRole(instance_id=community.instance_id, user_id=admin.id, role='admin'))
    victim = make_user(instance, 'victim')
    victim.banned = True
    db.session.commit()
    assert community.is_instance_admin(admin) is True
    assert community.is_moderator(admin) is False

    calls = record_moderation(monkeypatch, 'unban_user')

    dispatch(undo_block_activity(admin, victim.ap_profile_id, community.ap_profile_id))

    assert len(calls['unban_user']) == 1
    args, kwargs = calls['unban_user'][0]
    assert sa_inspect(args[2]).identity[0] == community.id
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_community_unban_of_an_unfound_community_is_ignored(app, db_session, monkeypatch):
    """`find_actor_or_create_cached` doubled, but scoped to ONLY the one URL
    this test wants unresolvable -- an unconditional double also intercepts
    the outer activity's own signed actor, resolved by
    process_inbox_request's preamble (routes.py:871) before ANY Undo arm
    runs. A blanket double fails that resolution first, with 'Actor was not
    a user or a community', and this branch is never reached at all. The
    real function is captured before patching and delegated to for every
    other URL -- including the preamble's own lookup of `mod` -- so only the
    unfound community's target is actually unresolvable.

    An undoubled lookup of the unfound target would attempt a real fetch,
    which block_outbound_http turns into a respx error -- an INFRASTRUCTURE
    failure that would masquerade as a behavioural one -- so doubling is
    necessary here, not merely convenient.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    mod = make_user(instance, 'mod')
    mod.ap_fetched_at = utcnow()
    victim = make_user(instance, 'victim')
    victim.banned = True
    db.session.commit()

    unfound_target = 'https://peer.example/c/gone/moderators'
    real_find_actor_or_create_cached = activitypub_routes.find_actor_or_create_cached

    def scoped_find_actor_or_create_cached(actor, *args, **kwargs):
        actor_id = actor['id'] if isinstance(actor, dict) else actor
        if actor_id == unfound_target:
            return None
        return real_find_actor_or_create_cached(actor, *args, **kwargs)

    monkeypatch.setattr(activitypub_routes, 'find_actor_or_create_cached',
                        scoped_find_actor_or_create_cached)

    dispatch(undo_block_activity(mod, victim.ap_profile_id, unfound_target))

    log = ActivityPubLog.query.one()
    assert log.result == 'ignored'
    assert log.exception_message == 'Blocked or unfound community'


def test_undo_block_when_announced_empties_both_cc_lists(app, db_session, monkeypatch):
    """The two statements under `if announced and store_ap_json:`
    (routes.py:1820-1822), which blank `core_activity['cc']` (the outer
    Undo's own cc) and `core_activity['object']['cc']` (the inner Block's
    cc). `announced` is only ever True when the activity arrived wrapped in
    an Announce (routes.py:928/931), so this needs an Undo/Block built that
    way. Per test_inbox_dispatch_announce.py's `_seed_announcing_community`
    and test_inbox_dispatch_block.py's
    `test_community_ban_when_announced_short_circuits_target_resolution`,
    the OUTER Announce actor must resolve as a Community, not a User --
    make_community's hardcoded owner (instance_id=1/user_id=1) requires
    seed_community_owner() and make_site() to run first.

    `core_activity` is `request_json['object']`, the SAME dict this test
    built as `inner_undo` -- dispatch() mutates it in place, so reading the
    two `cc` lists back off `inner_undo` afterward, rather than re-fetching
    anything, is what proves the write happened.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    make_site()
    instance = seed_community_owner('announcer.example')
    community = make_community(host='announcer.example')
    community.ap_fetched_at = utcnow()
    unblocker = make_user(instance, 'admin')
    unblocker.ap_fetched_at = utcnow()
    db.session.add(InstanceRole(instance_id=instance.id, user_id=unblocker.id, role='admin'))
    victim = make_user(instance, 'victim')
    victim.banned = True
    victim.banned_until = utcnow()
    db.session.commit()

    inner_undo = undo_block_activity(unblocker, victim.ap_profile_id,
                                     f'https://{instance.domain}')
    inner_undo['cc'] = ['https://peer.example/a/very/long/list/of/instances']
    inner_undo['object']['cc'] = ['https://peer.example/another/long/list']

    activity = inbox_activity(community, activity_type='Announce', object=inner_undo)

    dispatch(activity)

    assert inner_undo['cc'] == []
    assert inner_undo['object']['cc'] == []
    log = ActivityPubLog.query.one()
    assert log.result == 'success'
