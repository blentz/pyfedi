"""tests/test_inbox_dispatch_new_content.py"""
from app import db
from app.activitypub import routes as activitypub_routes
from app.models import ActivityPubLog, utcnow
from tests.factories import (inbox_activity, make_community, make_community_member, make_post,
                             make_post_reply, make_user, seed_community_owner, make_site)
from tests.test_inbox_dispatch_lock_delete import record_moderation
from tests.test_inbox_dispatch_preamble import dispatch


def seed_content_pair(host='peer.example'):
    """A remote author and a community that `find_community` will be doubled to
    return. `seed_community_owner` runs FIRST because `make_community`
    hardcodes user_id=1/instance_id=1 against real foreign keys.

    `ap_fetched_at` is stamped so the preamble does not schedule an actor
    refresh, which would attempt a real fetch and surface as a respx error --
    an INFRASTRUCTURE failure that would masquerade as a behavioural one.
    """
    make_site()
    instance = seed_community_owner(host)
    community = make_community(host=host)
    author = make_user(instance, 'author')
    author.ap_fetched_at = utcnow()
    db.session.commit()
    return instance, community, author


def content_object(ap_id, *, object_type='Page', in_reply_to=None, **extra):
    """The inner content object. `in_reply_to` present selects the reply half
    of `process_new_content`; absent selects the post half.
    """
    obj = {'type': object_type, 'id': ap_id}
    if in_reply_to is not None:
        obj['inReplyTo'] = in_reply_to
    obj.update(extra)
    return obj


def direct_activity(author, obj, *, activity_type='Create'):
    """A Create/Update sent straight to the inbox: `announced` is False, and
    the function reads `inReplyTo`/`id` from request_json['object'].

    NOTE: request_json IS activity_json on this path (routes.py:2305), so
    `args[2]` in the tests below is the whole envelope, not `obj` -- the
    content lives at `args[2]['object']`, and `args[2]['id']` is the
    envelope's own (uuid-based) activity id, unrelated to the content's id.
    """
    return inbox_activity(author, activity_type=activity_type, object=obj)


def announced_activity(community, author, obj, *, activity_type='Create'):
    """The same content wrapped in an Announce from the community: `announced`
    is True, and the function reads from request_json['object']['object']
    instead, taking `announce_id` from the OUTER id.

    The outer actor must be the community: the top-level preamble (routes.py,
    the `if request_json['type'] == 'Announce'...` branch) resolves an
    Announce's actor via `find_actor_or_create_cached(actor_id,
    community_only=True, create_if_not_found=False)` BEFORE any arm runs,
    setting `community` directly from that lookup. Because the Create/Update
    arm's own community-resolution block is gated by `if not announced and
    not community:`, it is skipped entirely when `announced` is True -- so
    `_double_the_gate` (which doubles `find_community`/`ensure_domains_match`,
    the delegates that block calls) is neither needed nor exercised on this
    path. What actually reaches `process_new_content` with the right
    `community` is the real DB lookup above resolving the seeded Community
    row by its `ap_profile_id`, which is why `community` here must already be
    a persisted row the community actor URL points at, with `ap_fetched_at`
    stamped to suppress a real refresh fetch.

    activity_json on this path is `request_json['object']` (the inner
    Create/Update activity), whose own `id` is `inner['id']` here, not the
    content object's id -- content lives two levels down, at
    `args[2]['object']['id']`.
    """
    inner = {'id': f'{author.ap_profile_id}/activities/inner',
             'type': activity_type,
             'actor': author.ap_profile_id,
             'object': obj}
    return inbox_activity(community, activity_type='Announce', object=inner)


def _double_the_gate(monkeypatch, community):
    """Doubles what stands between the arm's entry and process_new_content:
    community resolution and the domain check. Returns nothing -- callers that
    need call records double the delegates themselves.

    The ANNOUNCED tests deliberately do not call this: an Announce's actor is
    already the community, and the Create/Update arm's own community
    resolution (`find_community`/`ensure_domains_match`) sits inside an `if
    not announced and not community:` block, so it never runs at all when
    `announced` is True -- see `announced_activity`'s docstring for the real
    mechanism.
    """
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: community)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)


def test_a_direct_create_reads_the_object_and_passes_announced_false(app, db_session, monkeypatch):
    """The `not announced` half of the preamble. `create_post` is doubled and
    its arguments recorded: `announce_id` must be None for a direct activity,
    which is the observable difference from the announced shape below.

    `args[2]` is `activity_json`, which on this path IS `request_json` (the
    whole envelope) -- so the content's id is checked at `args[2]['object']
    ['id']`, not `args[2]['id']` (that is the envelope's own uuid-based
    activity id, unrelated to the content).
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    _double_the_gate(monkeypatch, community)
    monkeypatch.setattr(activitypub_routes, 'can_create_post', lambda user, content: True)

    calls = []
    monkeypatch.setattr(activitypub_routes, 'create_post',
                        lambda *args, **kwargs: calls.append((args, kwargs)) or None)
    record_moderation(monkeypatch, 'proactively_delete_content')

    dispatch(direct_activity(author, content_object('https://peer.example/post/1')))

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert kwargs['announce_id'] is None
    assert args[2]['object']['id'] == 'https://peer.example/post/1'


def test_an_announced_create_reads_the_nested_object_and_carries_an_announce_id(
        app, db_session, monkeypatch):
    """The `else` half of the preamble, reached only via an Announce. It reads
    the inner object one level deeper AND sets `announce_id` from the outer
    activity's id -- the only place that value comes from.

    Paired with the test above so neither half of the preamble can be dropped
    without a failure. As with the direct case, `args[2]` (`activity_json`)
    is the wrapping Create/Update activity here (`request_json['object']`),
    so the content's id is at `args[2]['object']['id']`.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    community.ap_fetched_at = utcnow()
    db.session.commit()
    monkeypatch.setattr(activitypub_routes, 'can_create_post', lambda user, content: True)

    calls = []
    monkeypatch.setattr(activitypub_routes, 'create_post',
                        lambda *args, **kwargs: calls.append((args, kwargs)) or None)
    record_moderation(monkeypatch, 'proactively_delete_content')

    activity = announced_activity(community, author, content_object('https://peer.example/post/1'))
    dispatch(activity)

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert kwargs['announce_id'] == activity['id']
    assert args[2]['object']['id'] == 'https://peer.example/post/1'


def test_a_missing_community_falls_back_to_the_microblogging_community(app, db_session, monkeypatch):
    """`if community is None:` -- reached when `find_community` finds nothing,
    which the arm treats as a microblogging post. The fallback's return value
    is what every later dereference of `community` uses, so it is asserted by
    identity through `can_create_post`'s argument rather than merely by the
    delegate being called.

    `process_chat` is doubled to return False: when `find_community` returns
    None, the Create/Update arm calls `process_chat(user, store_ap_json,
    core_activity, session)` first and returns early only if THAT returns
    truthy (treating the activity as a chat message); returning False lets
    the arm fall through to `ensure_domains_match` and ultimately
    `process_new_content`, where the `community is None` fallback under test
    actually lives.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    microblog = make_community(name='microblog', host='peer.example')
    db.session.commit()

    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: None)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)
    monkeypatch.setattr(activitypub_routes, 'process_chat', lambda *a, **k: False)
    monkeypatch.setattr(activitypub_routes, 'find_microblogging_community', lambda: microblog)

    seen = {}

    def fake_can_create_post(user, content):
        seen['community_id'] = content.id
        return False

    monkeypatch.setattr(activitypub_routes, 'can_create_post', fake_can_create_post)
    record_moderation(monkeypatch, 'proactively_delete_content')

    dispatch(direct_activity(author, content_object('https://peer.example/post/1')))

    assert seen['community_id'] == microblog.id


def test_a_create_for_an_existing_post_is_refused_as_processed_after_update(
        app, db_session, monkeypatch):
    """`activity_json['type'] == 'Create'` for a post that already exists means
    an Update won an async race and this Create arrived late. Refused before
    any permission check, so the actor here is the post's own author -- proving
    the refusal is about ordering, not permission.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    post = make_post(community, author, 'https://peer.example/post/1')
    db.session.commit()
    _double_the_gate(monkeypatch, community)
    calls = record_moderation(monkeypatch, 'update_post_from_activity')

    dispatch(direct_activity(author, content_object(post.ap_id)))

    assert calls['update_post_from_activity'] == []
    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Create processed after Update'


def test_an_update_by_the_posts_author_updates_and_announces(app, db_session, monkeypatch):
    """First disjunct of the permission check: `user.id == post.user_id`. The
    announce fires because the activity is not announced -- its own guard,
    asserted here and pinned from the other side by the announced test below.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    post = make_post(community, author, 'https://peer.example/post/1')
    db.session.commit()
    _double_the_gate(monkeypatch, community)
    calls = record_moderation(monkeypatch, 'update_post_from_activity',
                              'announce_activity_to_followers')

    dispatch(direct_activity(author, content_object(post.ap_id), activity_type='Update'))

    assert len(calls['update_post_from_activity']) == 1
    assert len(calls['announce_activity_to_followers']) == 1
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_an_announced_update_does_not_re_announce(app, db_session, monkeypatch):
    """`if not announced:` -- the other side. An Announce-wrapped Update still
    updates the post but must not be re-announced to followers, or the activity
    would loop back out to the instance that sent it.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    community.ap_fetched_at = utcnow()
    post = make_post(community, author, 'https://peer.example/post/1')
    db.session.commit()
    calls = record_moderation(monkeypatch, 'update_post_from_activity',
                              'announce_activity_to_followers')

    dispatch(announced_activity(community, author, content_object(post.ap_id),
                                activity_type='Update'))

    assert len(calls['update_post_from_activity']) == 1
    assert calls['announce_activity_to_followers'] == []


def test_an_update_by_an_unrelated_user_is_denied(app, db_session, monkeypatch):
    """All three disjuncts false: not the author, not a moderator, not an
    instance admin. This is the test the three permission mutations in Task 3
    are measured against.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    outsider = make_user(instance, 'outsider')
    outsider.ap_fetched_at = utcnow()
    post = make_post(community, author, 'https://peer.example/post/1')
    db.session.commit()
    _double_the_gate(monkeypatch, community)
    calls = record_moderation(monkeypatch, 'update_post_from_activity')

    dispatch(direct_activity(outsider, content_object(post.ap_id), activity_type='Update'))

    assert calls['update_post_from_activity'] == []
    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Edit attempt denied'


def test_an_update_by_a_community_moderator_is_permitted(app, db_session, monkeypatch):
    """Second disjunct. The editor is NOT the author, so the first disjunct is
    false and this test isolates `post.community.is_moderator(user)`.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    mod = make_user(instance, 'mod')
    mod.ap_fetched_at = utcnow()
    make_community_member(mod, community, is_moderator=True)
    post = make_post(community, author, 'https://peer.example/post/1')
    db.session.commit()
    _double_the_gate(monkeypatch, community)
    calls = record_moderation(monkeypatch, 'update_post_from_activity',
                              'announce_activity_to_followers')

    dispatch(direct_activity(mod, content_object(post.ap_id), activity_type='Update'))

    assert len(calls['update_post_from_activity']) == 1
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_an_update_by_an_instance_admin_is_permitted(app, db_session, monkeypatch):
    """Third disjunct: `post.community.is_instance_admin(user)`. The editor is
    neither the author nor a moderator, so this test is the only one that can
    kill that disjunct.

    `Community.is_instance_admin(user)` checks an InstanceRole against the
    COMMUNITY's instance -- a different method from `User.is_instance_admin()`,
    which takes no arguments and checks the user's own. Read both before
    seeding; sub-project 5c documented the pair being easy to conflate.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    admin = make_user(instance, 'admin')
    admin.ap_fetched_at = utcnow()
    from app.models import InstanceRole
    db.session.add(InstanceRole(instance_id=community.instance_id, user_id=admin.id, role='admin'))
    post = make_post(community, author, 'https://peer.example/post/1')
    db.session.commit()
    _double_the_gate(monkeypatch, community)
    calls = record_moderation(monkeypatch, 'update_post_from_activity')

    dispatch(direct_activity(admin, content_object(post.ap_id), activity_type='Update'))

    assert len(calls['update_post_from_activity']) == 1
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def _permit_and_return(monkeypatch, post_or_none):
    """Double `can_create_post` to True and `create_post` to return the given
    object. `create_post` is doubled in every test that reaches the creation
    path: it is large, writes many rows, and is its own future slice -- and
    its return value is exactly the switch this function branches on.
    """
    monkeypatch.setattr(activitypub_routes, 'can_create_post', lambda user, content: True)
    monkeypatch.setattr(activitypub_routes, 'create_post',
                        lambda *args, **kwargs: post_or_none)


def _seed_created_post(community, author):
    """The Post that `create_post` is doubled to return.

    Its ap_id MUST DIFFER from the one the test dispatches. `process_new_content`
    looks up an existing post by the dispatched ap_id BEFORE it reaches the
    creation path -- seeding the returned post under the dispatched ap_id would
    make the function take the existing-post branch instead, and the test would
    silently measure Task 2's path rather than this one.
    """
    return make_post(community, author, 'https://peer.example/post/created')


def test_a_create_that_succeeds_logs_success_and_announces(app, db_session, monkeypatch):
    """The ordinary new-post path. `edited_at` is left None (the column has no
    declared default), so the lost-race branch below is the one NOT taken here
    -- proved by `update_post_from_activity` never being called.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    created = _seed_created_post(community, author)
    db.session.commit()
    _double_the_gate(monkeypatch, community)
    _permit_and_return(monkeypatch, created)
    calls = record_moderation(monkeypatch, 'update_post_from_activity',
                              'announce_activity_to_followers')

    dispatch(direct_activity(author, content_object('https://peer.example/post/new')))

    assert calls['update_post_from_activity'] == []
    assert len(calls['announce_activity_to_followers']) == 1
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_an_update_that_lost_a_race_to_a_create_is_applied_afterwards(app, db_session, monkeypatch):
    """`activity_json['type'] == 'Update' and post.edited_at is None` -- an
    Update arrived, found no post, created one, and must then apply itself.
    Both conjuncts are true here.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    created = _seed_created_post(community, author)
    created.edited_at = None
    db.session.commit()
    _double_the_gate(monkeypatch, community)
    _permit_and_return(monkeypatch, created)
    calls = record_moderation(monkeypatch, 'update_post_from_activity',
                              'announce_activity_to_followers')

    dispatch(direct_activity(author, content_object('https://peer.example/post/new'),
                             activity_type='Update'))

    assert len(calls['update_post_from_activity']) == 1
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_an_update_on_an_already_edited_post_is_not_re_applied(app, db_session, monkeypatch):
    """The second conjunct: `post.edited_at is None` is FALSE, so the freshly
    created post is left alone. `edited_at` is seeded to an explicit timestamp
    -- the column has no declared default, so the value is this test's own
    choice and the assertion is not vacuous.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    created = _seed_created_post(community, author)
    created.edited_at = utcnow()
    db.session.commit()
    _double_the_gate(monkeypatch, community)
    _permit_and_return(monkeypatch, created)
    calls = record_moderation(monkeypatch, 'update_post_from_activity')

    dispatch(direct_activity(author, content_object('https://peer.example/post/new'),
                             activity_type='Update'))

    assert calls['update_post_from_activity'] == []
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_a_refused_post_is_deleted_remotely_and_logs_nothing(app, db_session, monkeypatch):
    """PINS a defect. `create_post` returning None means the post was not
    allowed, so a Delete is sent back to the remote instance -- but the branch
    then neither logs nor returns. Control leaves the `try` without the
    `except` firing, leaves `if can_create_post(...)`, leaves the post half,
    and falls off the end of the function.

    Asserted with LOG_ACTIVITYPUB_TO_DB explicitly True, so the zero is real
    silence rather than logging being switched off. An operator cannot tell a
    refused-and-deleted post from one that was never received.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    _double_the_gate(monkeypatch, community)
    _permit_and_return(monkeypatch, None)
    calls = record_moderation(monkeypatch, 'proactively_delete_content')

    dispatch(direct_activity(author, content_object('https://peer.example/post/1')))

    assert len(calls['proactively_delete_content']) == 1
    args, kwargs = calls['proactively_delete_content'][0]
    assert args[1] == 'https://peer.example/post/1'
    assert ActivityPubLog.query.count() == 0


def test_a_refused_post_in_a_remote_community_is_not_deleted(app, db_session, monkeypatch):
    """`if community.is_local():` -- the other side. A remote community's own
    instance is responsible for its content, so no Delete is sent.

    `make_community` never sets `ap_id`, so every factory community is
    is_local() == True regardless of host (tests/README.md fact 20); the
    community is made genuinely remote here by setting `ap_id` explicitly.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    community.ap_id = 'https://peer.example/c/microblogs'
    db.session.commit()
    _double_the_gate(monkeypatch, community)
    _permit_and_return(monkeypatch, None)
    calls = record_moderation(monkeypatch, 'proactively_delete_content')

    dispatch(direct_activity(author, content_object('https://peer.example/post/1')))

    assert calls['proactively_delete_content'] == []


def test_a_type_error_from_create_post_is_logged_and_returned(app, db_session, monkeypatch):
    """The `except TypeError:` fallback. The delegate is doubled to raise, which
    is the only way to reach it -- nothing in this function raises TypeError
    itself.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    _double_the_gate(monkeypatch, community)
    monkeypatch.setattr(activitypub_routes, 'can_create_post', lambda user, content: True)

    def boom(*args, **kwargs):
        raise TypeError('malformed')

    monkeypatch.setattr(activitypub_routes, 'create_post', boom)

    dispatch(direct_activity(author, content_object('https://peer.example/post/1')))

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'TypeError. See log file.'


def test_a_user_who_cannot_post_is_refused_and_their_content_deleted(app, db_session, monkeypatch):
    """`can_create_post` false. Unlike the refused-by-the-delegate branch above,
    this one DOES log -- which is the asymmetry the register records.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    _double_the_gate(monkeypatch, community)
    monkeypatch.setattr(activitypub_routes, 'can_create_post', lambda user, content: False)
    calls = record_moderation(monkeypatch, 'proactively_delete_content')

    dispatch(direct_activity(author, content_object('https://peer.example/post/1')))

    assert len(calls['proactively_delete_content']) == 1
    log = ActivityPubLog.query.one()
    assert log.exception_message == 'User cannot create post in Community'


def _seed_reply(community, author, ap_id='https://peer.example/comment/1'):
    """A reply with an explicit ap_id -- `make_post_reply` does not set one, and
    `process_new_content` resolves the reply by exactly that value.
    """
    parent = make_post(community, author, 'https://peer.example/post/1')
    reply = make_post_reply(parent, author)
    reply.ap_id = ap_id
    db.session.commit()
    return parent, reply


def test_a_create_for_an_existing_reply_is_refused_as_processed_after_update(
        app, db_session, monkeypatch):
    """The reply half's mirror of the post half's ordering refusal."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    parent, reply = _seed_reply(community, author)
    _double_the_gate(monkeypatch, community)
    calls = record_moderation(monkeypatch, 'update_post_reply_from_activity')

    dispatch(direct_activity(author, content_object(reply.ap_id, in_reply_to=parent.ap_id)))

    assert calls['update_post_reply_from_activity'] == []
    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Create processed after Update'


def test_an_update_by_the_replys_author_updates_and_announces(app, db_session, monkeypatch):
    """The permitted path, with `can_create_post_reply` true."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    parent, reply = _seed_reply(community, author)
    _double_the_gate(monkeypatch, community)
    monkeypatch.setattr(activitypub_routes, 'can_create_post_reply', lambda user, content: True)
    calls = record_moderation(monkeypatch, 'update_post_reply_from_activity',
                              'announce_activity_to_followers')

    dispatch(direct_activity(author, content_object(reply.ap_id, in_reply_to=parent.ap_id),
                             activity_type='Update'))

    assert len(calls['update_post_reply_from_activity']) == 1
    assert len(calls['announce_activity_to_followers']) == 1
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_a_permitted_editor_who_cannot_reply_is_dropped_silently(app, db_session, monkeypatch):
    """PINS a defect. The outer permission check passes but
    `can_create_post_reply` is false, so the bare `return` fires with NO log on
    any path -- the update does not happen and nothing records why.

    The post half has no equivalent inner check at all, which is what makes
    this an asymmetry rather than a deliberate design.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    parent, reply = _seed_reply(community, author)
    _double_the_gate(monkeypatch, community)
    monkeypatch.setattr(activitypub_routes, 'can_create_post_reply', lambda user, content: False)
    calls = record_moderation(monkeypatch, 'update_post_reply_from_activity')

    dispatch(direct_activity(author, content_object(reply.ap_id, in_reply_to=parent.ap_id),
                             activity_type='Update'))

    assert calls['update_post_reply_from_activity'] == []
    assert ActivityPubLog.query.count() == 0


def test_an_instance_admin_cannot_edit_a_reply(app, db_session, monkeypatch):
    """PINS the headline defect. The post half permits an instance admin via a
    third disjunct; the reply half's check is
    `user.id == reply.user_id or reply.community.is_moderator(user)` -- the
    third disjunct is absent, so the same admin who may edit a post is refused
    on a reply.

    Seeded identically to Task 3's post-side admin test, so the difference
    observed is the code's, not the fixture's.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    admin = make_user(instance, 'admin')
    admin.ap_fetched_at = utcnow()
    from app.models import InstanceRole
    db.session.add(InstanceRole(instance_id=community.instance_id, user_id=admin.id, role='admin'))
    parent, reply = _seed_reply(community, author)
    _double_the_gate(monkeypatch, community)
    monkeypatch.setattr(activitypub_routes, 'can_create_post_reply', lambda user, content: True)
    calls = record_moderation(monkeypatch, 'update_post_reply_from_activity')

    dispatch(direct_activity(admin, content_object(reply.ap_id, in_reply_to=parent.ap_id),
                             activity_type='Update'))

    assert calls['update_post_reply_from_activity'] == []
    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Edit attempt denied'


def test_an_update_by_a_reply_community_moderator_is_permitted(app, db_session, monkeypatch):
    """Second disjunct of the reply guard, isolated: the editor is NOT the
    reply's author (first disjunct false) but IS a moderator of the reply's
    community (`reply.community.is_moderator(user)` true). Added per Task 6's
    Step 3 -- this is the test that kills the mutation dropping `or
    reply.community.is_moderator(user)`; no earlier test in this file isolates
    that disjunct alone for a reply.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    mod = make_user(instance, 'mod')
    mod.ap_fetched_at = utcnow()
    make_community_member(mod, community, is_moderator=True)
    parent, reply = _seed_reply(community, author)
    _double_the_gate(monkeypatch, community)
    monkeypatch.setattr(activitypub_routes, 'can_create_post_reply', lambda user, content: True)
    calls = record_moderation(monkeypatch, 'update_post_reply_from_activity',
                              'announce_activity_to_followers')

    dispatch(direct_activity(mod, content_object(reply.ap_id, in_reply_to=parent.ap_id),
                             activity_type='Update'))

    assert len(calls['update_post_reply_from_activity']) == 1
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def _permit_and_return_reply(monkeypatch, reply_or_none):
    """`create_post_reply` is doubled in every test that REACHES the reply
    creation path, for the same reason `create_post` is: it is large, writes
    many rows, and is its own future slice, and its return value is the switch
    this half branches on. The existing-reply tests above never call it.
    """
    monkeypatch.setattr(activitypub_routes, 'can_create_post_reply', lambda user, content: True)
    monkeypatch.setattr(activitypub_routes, 'create_post_reply',
                        lambda *args, **kwargs: reply_or_none)


def test_a_new_reply_that_succeeds_logs_success_and_announces(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    parent, reply = _seed_reply(community, author, ap_id='https://peer.example/comment/existing')
    _double_the_gate(monkeypatch, community)
    _permit_and_return_reply(monkeypatch, reply)
    calls = record_moderation(monkeypatch, 'update_post_reply_from_activity',
                              'announce_activity_to_followers')

    dispatch(direct_activity(author, content_object('https://peer.example/comment/new',
                                                    in_reply_to=parent.ap_id)))

    assert calls['update_post_reply_from_activity'] == []
    assert len(calls['announce_activity_to_followers']) == 1
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_a_reply_update_that_lost_a_race_is_applied_afterwards(app, db_session, monkeypatch):
    """`activity_json['type'] == 'Update' and reply.edited_at is None`, both
    conjuncts true. `edited_at` has no declared default on PostReply either.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    parent, reply = _seed_reply(community, author, ap_id='https://peer.example/comment/existing')
    reply.edited_at = None
    db.session.commit()
    _double_the_gate(monkeypatch, community)
    _permit_and_return_reply(monkeypatch, reply)
    calls = record_moderation(monkeypatch, 'update_post_reply_from_activity')

    dispatch(direct_activity(author, content_object('https://peer.example/comment/new',
                                                    in_reply_to=parent.ap_id),
                             activity_type='Update'))

    assert len(calls['update_post_reply_from_activity']) == 1


def test_a_reply_update_on_an_already_edited_reply_is_not_re_applied(app, db_session, monkeypatch):
    """The second conjunct false: `edited_at` seeded to an explicit timestamp."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    parent, reply = _seed_reply(community, author, ap_id='https://peer.example/comment/existing')
    reply.edited_at = utcnow()
    db.session.commit()
    _double_the_gate(monkeypatch, community)
    _permit_and_return_reply(monkeypatch, reply)
    calls = record_moderation(monkeypatch, 'update_post_reply_from_activity')

    dispatch(direct_activity(author, content_object('https://peer.example/comment/new',
                                                    in_reply_to=parent.ap_id),
                             activity_type='Update'))

    assert calls['update_post_reply_from_activity'] == []


def test_a_refused_reply_is_deleted_remotely_and_logs_nothing(app, db_session, monkeypatch):
    """The reply half's mirror of the post half's refusal. This one DOES return
    explicitly, unlike its post-half twin -- but it still logs nothing, which
    is the shared half of that defect.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    parent = make_post(community, author, 'https://peer.example/post/1')
    db.session.commit()
    _double_the_gate(monkeypatch, community)
    _permit_and_return_reply(monkeypatch, None)
    calls = record_moderation(monkeypatch, 'proactively_delete_content')

    dispatch(direct_activity(author, content_object('https://peer.example/comment/new',
                                                    in_reply_to=parent.ap_id)))

    assert len(calls['proactively_delete_content']) == 1
    assert ActivityPubLog.query.count() == 0


def test_a_type_error_from_create_post_reply_is_logged(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    parent = make_post(community, author, 'https://peer.example/post/1')
    db.session.commit()
    _double_the_gate(monkeypatch, community)
    monkeypatch.setattr(activitypub_routes, 'can_create_post_reply', lambda user, content: True)

    def boom(*args, **kwargs):
        raise TypeError('malformed')

    monkeypatch.setattr(activitypub_routes, 'create_post_reply', boom)

    dispatch(direct_activity(author, content_object('https://peer.example/comment/new',
                                                    in_reply_to=parent.ap_id)))

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'TypeError. See log file.'


def test_a_user_who_cannot_reply_is_refused_and_their_content_deleted(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    parent = make_post(community, author, 'https://peer.example/post/1')
    db.session.commit()
    _double_the_gate(monkeypatch, community)
    monkeypatch.setattr(activitypub_routes, 'can_create_post_reply', lambda user, content: False)
    calls = record_moderation(monkeypatch, 'proactively_delete_content')

    dispatch(direct_activity(author, content_object('https://peer.example/comment/new',
                                                    in_reply_to=parent.ap_id)))

    assert len(calls['proactively_delete_content']) == 1
    log = ActivityPubLog.query.one()
    assert log.exception_message == 'User cannot create reply in Community'


def test_the_id_truncation_mutates_the_callers_activity(app, db_session, monkeypatch):
    """PINS a defect. `activity_json['id'] = shorten_string(activity_json['id'], 100)`
    writes back into the dict the caller owns. For an ANNOUNCED activity
    `activity_json` IS `request_json['object']`, so the truncation is visible
    in the activity object this test constructed: production mutates a dict
    the caller still holds a reference to, after `dispatch()` returns.

    Follower propagation of the truncated id is a DIRECT-path consequence,
    not an announced-path one: both `announce_activity_to_followers(...,
    request_json)` call sites inside this function's post-creation branches
    sit under `if not announced:`, so on the announced shape this test
    exercises, neither is reachable. What IS true on both paths is that the
    already-truncated `activity_json` is passed into `create_post` /
    `create_post_reply`, which forward it into `Post.new`/`PostReply.new`,
    where it is stored as `ap_create_id` (app/models.py:1861 and :2968
    respectively) -- a `db.String(100)` column on both `Post` and
    `PostReply` (app/models.py:1722 and :2893). That width is very likely why
    the truncation exists at all: the comment above the mutation says
    over-long ids "will crash the app", and String(100) is exactly what an
    untruncated id would overflow.

    The comment above that line claims the id is "not referred to again, so it
    shouldn't matter if they're truncated". This test is the counter-example:
    the object asserted below is the very dict the test passed in.

    The inner id is deliberately longer than 100 characters so truncation is
    observable; a short id would leave the mutation invisible.

    `shorten_string(s, 100)` (defined in app/utils.py, imported into
    routes.py) does NOT return a 100-character string: for input longer than
    max_length it returns `s[:max_length - 3] + '…'`, i.e. 97 characters of
    the original plus a single ellipsis character, for a total length of 98
    -- confirmed by running the function directly against a 150-character
    input before writing this assertion.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    community.ap_fetched_at = utcnow()
    db.session.commit()
    _permit_and_return(monkeypatch, None)
    record_moderation(monkeypatch, 'proactively_delete_content')

    long_id = f'{author.ap_profile_id}/activities/' + ('x' * 150)
    activity = announced_activity(community, author, content_object('https://peer.example/post/1'))
    activity['object']['id'] = long_id
    original_length = len(activity['object']['id'])

    dispatch(activity)

    assert original_length > 100
    assert len(activity['object']['id']) == 98
    assert activity['object']['id'] == long_id[:97] + '…'
