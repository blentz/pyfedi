"""tests/test_inbox_dispatch_new_content.py"""
from app import db
from app.activitypub import routes as activitypub_routes
from app.models import ActivityPubLog, utcnow
from tests.factories import (inbox_activity, make_community, make_post, make_user,
                             seed_community_owner, make_site)
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
