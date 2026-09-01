"""tests/test_inbox_dispatch_create_update.py"""
from datetime import timedelta

import pytest

from app import db
from app.activitypub import routes as activitypub_routes
from app.models import ActivityPubLog, utcnow
from tests.factories import (inbox_activity, make_community, make_community_member,
                             make_feed, make_instance, make_poll, make_poll_choice,
                             make_post, make_site, make_user, seed_community_owner)
from tests.test_inbox_dispatch_lock_delete import record_moderation
from tests.test_inbox_dispatch_preamble import dispatch


def create_activity(actor, obj, *, activity_type='Create', **outer):
    """A Create (or Update) whose `object` is `obj`.

    `inbox_activity` applies **fields last, so passing `object=` replaces its
    default string object. `obj` may be a dict OR a bare string -- the arm's
    first branch exists precisely to handle the string form.
    """
    return inbox_activity(actor, activity_type=activity_type, object=obj, **outer)


def test_an_unverifiable_string_object_logs_the_refusal_reason(app, db_session, monkeypatch):
    """`isinstance(core_activity['object'], str)` sends the activity to
    `verify_object_from_source`, which returns `(None, reason)` on refusal. The
    arm puts that reason into the log rather than a generic sentence, which is
    the whole point of the delegate returning it -- so the assertion pins the
    reason, not merely the failure.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    author = make_user(instance, 'author')
    monkeypatch.setattr(activitypub_routes, 'verify_object_from_source',
                        lambda activity: (None, 'host mismatch'))

    dispatch(create_activity(author, 'https://peer.example/objects/1'))

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Could not verify unsigned request from source: host mismatch'


def test_a_verified_string_object_continues_into_the_normal_path(app, db_session, monkeypatch):
    """On success `verify_object_from_source` returns the activity with its
    `object` replaced by the fetched document, and processing continues. The
    double returns a ChatMessage object so the continuation is observable via
    `process_chat` without also exercising the content path.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    author = make_user(instance, 'author')

    def fake_verify(activity):
        activity['object'] = {'type': 'ChatMessage', 'id': 'https://peer.example/pm/1'}
        return activity, None

    monkeypatch.setattr(activitypub_routes, 'verify_object_from_source', fake_verify)
    calls = record_moderation(monkeypatch, 'process_chat')

    dispatch(create_activity(author, 'https://peer.example/objects/1'))

    assert len(calls['process_chat']) == 1


def test_a_chat_message_object_delegates_to_process_chat_and_returns(app, db_session, monkeypatch):
    """The `ChatMessage` branch delegates and returns immediately. `user` is the
    signed outer actor, and `session` is the dispatcher's own task session --
    both asserted positionally here, because the delegate's signature is
    `process_chat(user, store_ap_json, core_activity, session)` and a later
    reader cannot otherwise tell which argument is which.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    author = make_user(instance, 'author')
    calls = record_moderation(monkeypatch, 'process_chat', 'process_new_content')

    activity = create_activity(author, {'type': 'ChatMessage', 'id': 'https://peer.example/pm/1'})
    dispatch(activity)

    assert len(calls['process_chat']) == 1
    args, kwargs = calls['process_chat'][0]
    from sqlalchemy import inspect as sa_inspect
    assert sa_inspect(args[0]).identity[0] == author.id
    assert args[2] is activity
    assert calls['process_new_content'] == []


def seed_poll_post(host='peer.example', choice_text='yes', local_author=False):
    """A post carrying a poll with one choice, plus the voter.

    Returns (instance, voter, post, poll, choice). `local_author` controls
    whether the POST's author is local, which is what the arm's
    `post_being_replied_to.author.is_local()` branch keys off -- not the voter.
    """
    instance = seed_community_owner(host)
    community = make_community(host=host)
    if local_author:
        author = make_user(None, 'localauthor', local=True)
    else:
        author = make_user(instance, 'author')
    voter = make_user(instance, 'voter')
    post = make_post(community, author, f'https://{host}/post/1')
    poll = make_poll(post)
    choice = make_poll_choice(post, choice_text)
    db.session.commit()
    return instance, voter, post, poll, choice


def poll_note(post_ap_id, choice_text, **extra):
    """The exact object shape the poll-vote guard selects: a Note carrying a
    `name`, an `inReplyTo` and an `attributedTo`, and NO `published`.

    `**extra` lets a test add or override one field to break exactly one
    conjunct, which is how the five mutation kills below stay independent.
    """
    obj = {'type': 'Note', 'name': choice_text, 'inReplyTo': post_ap_id,
           'attributedTo': 'https://peer.example/u/voter'}
    obj.update(extra)
    return obj


def test_a_poll_shaped_note_is_selected_and_records_the_vote(app, db_session, monkeypatch):
    """All five conjuncts true. The vote is asserted through PollChoiceVote
    rather than through the delegate, because the arm calls
    `poll_data.vote_for_choice(...)` directly rather than a doubled function.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, voter, post, poll, choice = seed_poll_post()
    calls = record_moderation(monkeypatch, 'process_new_content')

    dispatch(create_activity(voter, poll_note(post.ap_id, 'yes')))

    from app.models import PollChoiceVote
    db.session.expire_all()
    vote = db_session.query(PollChoiceVote).filter_by(user_id=voter.id).one()
    assert vote.choice_id == choice.id
    assert calls['process_new_content'] == []   # selected the poll path, not content

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


@pytest.mark.parametrize('breaker,description', [
    ({'type': 'Article'}, 'type is not Note'),
    ({'name': None}, 'name absent'),
    ({'inReplyTo': None}, 'inReplyTo absent'),
    ({'attributedTo': None}, 'attributedTo absent'),
    ({'published': '2026-01-01T00:00:00Z'}, 'published present'),
])
def test_breaking_any_one_conjunct_leaves_the_poll_path(app, db_session, monkeypatch, breaker, description):
    """Each parametrisation breaks exactly ONE of the guard's five conjuncts and
    asserts the activity no longer takes the poll path -- it reaches
    `process_new_content` instead (a Note is in new_content_types).

    A `None` value in `breaker` means "delete this key", since four of the five
    conjuncts are membership tests rather than value tests.

    This is what makes each conjunct independently load-bearing: five separate
    parametrisations, five separate failures if any conjunct is dropped.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, voter, post, poll, choice = seed_poll_post()
    obj = poll_note(post.ap_id, 'yes')
    for key, value in breaker.items():
        if value is None:
            obj.pop(key)
        else:
            obj[key] = value

    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: None)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)
    monkeypatch.setattr(activitypub_routes, 'process_chat', lambda *a, **k: False)
    calls = record_moderation(monkeypatch, 'process_new_content')

    dispatch(create_activity(voter, obj))

    from app.models import PollChoiceVote
    assert db_session.query(PollChoiceVote).count() == 0, description
    assert len(calls['process_new_content']) == 1, description


def test_a_poll_vote_for_an_unknown_post_is_now_logged(app, db_session, monkeypatch):
    """Was `test_a_poll_vote_for_an_unknown_post_is_dropped_silently`.
    `post_being_replied_to` is None, so the block falls to its unconditional
    `return` -- but now with a log naming this specific outcome, distinct from
    the other two silent branches. `LOG_ACTIVITYPUB_TO_DB` is explicit True so
    the log is real evidence, not logging switched off producing a false zero.

    It still does NOT fall through to content handling: `process_new_content`
    is doubled and must not be called. The unconditional `return` at the end
    of the poll block is unchanged and is not this test's concern -- only that
    the outcome is now logged.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, voter, post, poll, choice = seed_poll_post()
    calls = record_moderation(monkeypatch, 'process_new_content')

    dispatch(create_activity(voter, poll_note('https://peer.example/post/404', 'yes')))

    assert calls['process_new_content'] == []
    log = ActivityPubLog.query.one()
    assert log.result == 'ignored'
    assert log.exception_message == 'Poll vote for an unknown post'


def test_a_poll_vote_on_a_post_with_no_poll_is_now_logged(app, db_session, monkeypatch):
    """Was `test_a_poll_vote_on_a_post_with_no_poll_is_dropped_silently`.
    `poll_data` is None: the post exists but carries no Poll row -- the FIRST
    conjunct of `if poll_data and choice:`. This is distinct from the
    unknown-choice case (the OTHER conjunct of the same `if`) and from the
    unknown-post case (which never reaches `if poll_data and choice:` at all;
    it fails the separate, outer `if post_being_replied_to:`) -- which is why
    all three tests are separate, and why the message must differ across all
    three.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    community = make_community(host='peer.example')
    author = make_user(instance, 'author')
    voter = make_user(instance, 'voter')
    post = make_post(community, author, 'https://peer.example/post/1')
    db.session.commit()

    dispatch(create_activity(voter, poll_note(post.ap_id, 'yes')))

    log = ActivityPubLog.query.one()
    assert log.result == 'ignored'
    assert log.exception_message == 'Poll vote for a post with no poll'


def test_a_poll_vote_for_an_unknown_choice_is_now_logged(app, db_session, monkeypatch):
    """Was `test_a_poll_vote_for_an_unknown_choice_is_dropped_silently`.
    `choice` is None: the poll exists but has no option with this `name`. The
    other conjunct of `if poll_data and choice:`, distinct from the no-poll
    case above -- so its message must be distinct too.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, voter, post, poll, choice = seed_poll_post(choice_text='yes')

    dispatch(create_activity(voter, poll_note(post.ap_id, 'maybe')))

    from app.models import PollChoiceVote
    assert db_session.query(PollChoiceVote).count() == 0
    log = ActivityPubLog.query.one()
    assert log.result == 'ignored'
    assert log.exception_message == 'Poll vote for an unknown choice'


def test_a_poll_vote_on_a_local_authors_post_stamps_it_and_schedules_an_edit(app, db_session, monkeypatch):
    """`post_being_replied_to.author.is_local()` -- the LOCAL branch. `edited_at`
    is seeded to a stale value first, so the stamp is evidence of the write
    rather than a default sitting there. `task_selector` is doubled and its
    kwargs asserted, because the task key and post id are a contract with the
    background worker.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, voter, post, poll, choice = seed_poll_post(local_author=True)
    stale = utcnow() - timedelta(days=3)
    post.edited_at = stale
    db.session.commit()
    post_id = post.id

    calls = record_moderation(monkeypatch, 'task_selector')

    dispatch(create_activity(voter, poll_note(post.ap_id, 'yes')))

    db.session.expire_all()
    assert db.session.get(type(post), post_id).edited_at > stale
    assert len(calls['task_selector']) == 1
    args, kwargs = calls['task_selector'][0]
    assert args[0] == 'edit_post'
    assert kwargs['post_id'] == post_id


def test_a_poll_vote_on_a_remote_authors_post_neither_stamps_nor_schedules(app, db_session, monkeypatch):
    """The other side of `is_local()`. Paired with the test above so the guard
    cannot be dropped in either direction. `edited_at` is seeded stale and must
    stay exactly stale.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, voter, post, poll, choice = seed_poll_post(local_author=False)
    stale = utcnow() - timedelta(days=3)
    post.edited_at = stale
    db.session.commit()
    post_id = post.id

    calls = record_moderation(monkeypatch, 'task_selector')

    dispatch(create_activity(voter, poll_note(post.ap_id, 'yes')))

    db.session.expire_all()
    assert db.session.get(type(post), post_id).edited_at == stale
    assert calls['task_selector'] == []
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_an_unresolvable_community_falls_back_to_process_chat_and_returns(app, db_session, monkeypatch):
    """`find_community` returns None, so `process_chat` is tried; a truthy
    return means it handled the activity and the arm returns immediately --
    proved here by `ensure_domains_match` never being reached.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    author = make_user(instance, 'author')
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: None)
    monkeypatch.setattr(activitypub_routes, 'process_chat', lambda *a, **k: True)
    calls = record_moderation(monkeypatch, 'ensure_domains_match')

    dispatch(create_activity(author, {'type': 'Page', 'id': 'https://peer.example/post/1'}))

    assert calls['ensure_domains_match'] == []


def test_a_falsy_process_chat_continues_into_the_domain_check(app, db_session, monkeypatch):
    """The other side: `process_chat` returns falsy, so the arm does NOT return
    and reaches `ensure_domains_match`. Paired with the test above so the
    `if process_chat(...)` guard cannot be dropped in either direction.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    author = make_user(instance, 'author')
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: None)
    monkeypatch.setattr(activitypub_routes, 'process_chat', lambda *a, **k: False)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: False)

    dispatch(create_activity(author, {'type': 'Page', 'id': 'https://peer.example/post/1'}))

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Domains do not match'


def test_a_mismatched_domain_is_refused(app, db_session, monkeypatch):
    """`ensure_domains_match` False -> FAILURE, and `process_new_content` is
    never reached.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    community = make_community(host='peer.example')
    author = make_user(instance, 'author')
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: community)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: False)
    calls = record_moderation(monkeypatch, 'process_new_content')

    dispatch(create_activity(author, {'type': 'Page', 'id': 'https://peer.example/post/1'}))

    assert calls['process_new_content'] == []
    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Domains do not match'


def test_a_remote_create_into_a_local_only_community_is_refused(app, db_session, monkeypatch):
    """`community.local_only` -- seeded explicitly True, since the column's
    default is False and an assertion resting on that would prove nothing.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    community = make_community(host='peer.example')
    community.local_only = True
    db.session.commit()
    author = make_user(instance, 'author')
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: community)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)
    calls = record_moderation(monkeypatch, 'process_new_content')

    dispatch(create_activity(author, {'type': 'Page', 'id': 'https://peer.example/post/1'}))

    assert calls['process_new_content'] == []
    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Remote Create in local_only community'


def test_a_remote_create_into_a_non_local_only_community_proceeds_to_content(app, db_session, monkeypatch):
    """The other side of `community.local_only`: seeded explicitly False
    (the column's default, but stated explicitly here since resting an
    assertion on a default is forbidden), the arm does NOT refuse and
    reaches `process_new_content` -- proving `local_only` is actually
    consulted rather than the mere truthiness of `community`.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    community = make_community(host='peer.example')
    community.local_only = False
    db.session.commit()
    author = make_user(instance, 'author')
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: community)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)
    calls = record_moderation(monkeypatch, 'process_new_content')

    dispatch(create_activity(author, {'type': 'Page', 'id': 'https://peer.example/post/1'}))

    assert len(calls['process_new_content']) == 1
    assert ActivityPubLog.query.filter_by(exception_message='Remote Create in local_only community').count() == 0


@pytest.mark.parametrize('object_type', ['Page', 'Article', 'Link', 'Question', 'Event'])
def test_each_new_content_type_reaches_process_new_content(app, db_session, monkeypatch, object_type):
    """Every member of `new_content_types` except 'Note', which is covered
    separately because a bare Note without the poll fields also reaches here
    (see the poll-guard tests) and parametrising it twice would obscure that.

    `announced` is passed through to the delegate, so it is asserted: this
    activity is not announced, so the fifth positional argument must be False.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    community = make_community(host='peer.example')
    author = make_user(instance, 'author')
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: community)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)
    calls = record_moderation(monkeypatch, 'process_new_content')

    dispatch(create_activity(author, {'type': object_type, 'id': 'https://peer.example/post/1'}))

    assert len(calls['process_new_content']) == 1
    args, kwargs = calls['process_new_content'][0]
    assert args[4] is False        # announced


def test_an_unacceptable_object_type_names_itself_in_the_failure(app, db_session, monkeypatch):
    """The fallthrough. The type is concatenated into the message, so the
    assertion pins the whole string rather than just the failure.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    community = make_community(host='peer.example')
    author = make_user(instance, 'author')
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: community)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)

    dispatch(create_activity(author, {'type': 'Tombstone', 'id': 'https://peer.example/x/1'}))

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Unacceptable type (create): Tombstone'


def test_a_create_of_a_group_is_unacceptable_because_the_group_branch_requires_update(app, db_session, monkeypatch):
    """`elif object_type == 'Group' and core_activity['type'] == 'Update'` --
    the second conjunct. A CREATE of a Group therefore falls through to the
    unacceptable-type log rather than refreshing a community profile. This is
    the test that kills that conjunct.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    community = make_community(host='peer.example')
    author = make_user(instance, 'author')
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: community)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)
    calls = record_moderation(monkeypatch, 'refresh_community_profile')

    dispatch(create_activity(author, {'type': 'Group', 'id': community.ap_profile_id}))

    assert calls['refresh_community_profile'] == []
    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Unacceptable type (create): Group'


def _seed_video_post(host='peer.example'):
    instance = seed_community_owner(host)
    community = make_community(host=host)
    owner = make_user(instance, 'owner')
    post = make_post(community, owner, f'https://{host}/videos/watch/1')
    db.session.commit()
    return instance, community, owner, post


def test_a_peertube_video_edit_by_its_owner_updates_the_post(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, owner, post = _seed_video_post()
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: community)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)
    calls = record_moderation(monkeypatch, 'update_post_from_activity')

    dispatch(create_activity(owner, {'type': 'Video', 'id': post.ap_id}, activity_type='Update'))

    assert len(calls['update_post_from_activity']) == 1
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_a_peertube_video_edit_by_another_user_is_denied(app, db_session, monkeypatch):
    """`user.id == post.user_id` is the ownership test; a different actor is
    refused. Paired with the test above so the guard dies in both directions.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, owner, post = _seed_video_post()
    interloper = make_user(instance, 'interloper')
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: community)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)
    calls = record_moderation(monkeypatch, 'update_post_from_activity')

    dispatch(create_activity(interloper, {'type': 'Video', 'id': post.ap_id}, activity_type='Update'))

    assert calls['update_post_from_activity'] == []
    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Edit attempt denied'


def test_a_peertube_video_edit_for_an_unknown_post_is_refused(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, owner, post = _seed_video_post()
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: community)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)

    dispatch(create_activity(owner, {'type': 'Video', 'id': 'https://peer.example/videos/watch/404'},
                             activity_type='Update'))

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'PeerTube post not found'


def test_a_group_update_from_a_non_moderator_of_a_LOCAL_community_is_refused(app, db_session, monkeypatch):
    """`community.is_local() and not community.is_moderator(user)`. Both
    conjuncts matter: this test supplies the local half, and the remote-community
    test below supplies the other.

    `make_community` never sets `ap_id` (see `tests/factories.py`'s
    `make_community`), so `Community.is_local()`
    (`self.ap_id is None or self.profile_id().startswith(SERVER_URL)`,
    `app/models.py:778`) is True through its FIRST disjunct -- `ap_id is
    None` -- for every community this factory builds, regardless of which
    `host` is passed. This community is local because of that, not because
    `host` was given as `app.config['SERVER_NAME']`; passing that host is
    harmless but not what makes it local.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    local_community = make_community(host=app.config['SERVER_NAME'])
    outsider = make_user(instance, 'outsider')
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: local_community)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)
    calls = record_moderation(monkeypatch, 'refresh_community_profile')

    dispatch(create_activity(outsider, {'type': 'Group', 'id': local_community.ap_profile_id},
                             activity_type='Update'))

    assert calls['refresh_community_profile'] == []
    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Comm edit by non-moderator'


def test_a_group_update_of_a_REMOTE_community_refreshes_without_announcing(app, db_session, monkeypatch):
    """A remote community: `is_local()` is False, so the permission guard's
    first conjunct short-circuits and the refresh runs. `announce_activity_to_followers`
    is gated on `community.is_local()` a second time, so it must NOT fire here --
    which is what distinguishes this test from a local-community success.

    The bare factory is NOT enough to make this community remote: `make_community`
    never sets `ap_id`, and `Community.is_local()` returns True whenever
    `ap_id is None` regardless of `host` -- so a community built only with
    `host='peer.example'` is still `is_local() == True` and this test would
    exercise the wrong branch. `editor` here is never made a moderator, so
    without the `ap_id` fix below this test would not silently pass -- it
    would fail loudly, with the guard denying as 'Comm edit by
    non-moderator' instead of succeeding. `ap_id` is set explicitly here to
    a URL that does not start with `SERVER_URL`, which is the only thing
    `is_local()` actually checks once `ap_id` is not None. The local
    community's moderator-edit combination (`refresh_community_profile`
    AND `announce_activity_to_followers` both firing) is exercised
    separately by
    `test_a_group_update_from_a_LOCAL_communitys_moderator_refreshes_and_announces`
    below.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    remote_community = make_community(host='peer.example')
    remote_community.ap_id = 'https://peer.example/c/microblogs'
    db.session.commit()
    editor = make_user(instance, 'editor')
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: remote_community)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)
    calls = record_moderation(monkeypatch, 'refresh_community_profile',
                              'announce_activity_to_followers')

    dispatch(create_activity(editor, {'type': 'Group', 'id': remote_community.ap_profile_id},
                             activity_type='Update'))

    assert len(calls['refresh_community_profile']) == 1
    assert calls['announce_activity_to_followers'] == []
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_a_group_update_from_a_LOCAL_communitys_moderator_refreshes_and_announces(app, db_session, monkeypatch):
    """The success path the REMOTE test cannot reach: a LOCAL community
    (`ap_id` left `None` by the bare `make_community` factory, so
    `is_local()` is True via its first disjunct) whose editor IS a
    moderator. The permission guard's `not community.is_moderator(user)`
    conjunct is then False, so the guard as a whole is False and the refresh
    runs; `community.is_local()` is checked a SECOND time afterward and is
    True, so `announce_activity_to_followers` also fires here -- the one
    statement in the arm no other test in this file reaches.

    `make_community_member(editor, community, is_moderator=True)` creates a
    `CommunityMember` row with `is_moderator=True`, `is_owner=False`,
    `is_banned=False` (all set explicitly by the factory, not left to
    column defaults). `Community.is_moderator(user)` (`app/models.py:719`)
    delegates to `moderators()` (`app/models.py:699`), which selects
    `CommunityMember` rows for this community where `is_owner OR
    is_moderator` is true AND `is_banned == False`, then checks whether any
    such row's `user_id` matches. This row satisfies that: `is_moderator`
    is True and `is_banned` is False, so `editor` is picked up by
    `moderators()` and `is_moderator(editor)` is True -- confirmed by
    reading both methods, not assumed.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    local_community = make_community(host=app.config['SERVER_NAME'])
    editor = make_user(instance, 'editor')
    make_community_member(editor, local_community, is_moderator=True)
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: local_community)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)
    calls = record_moderation(monkeypatch, 'refresh_community_profile',
                              'announce_activity_to_followers')

    dispatch(create_activity(editor, {'type': 'Group', 'id': local_community.ap_profile_id},
                             activity_type='Update'))

    assert len(calls['refresh_community_profile']) == 1
    assert len(calls['announce_activity_to_followers']) == 1
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


# --- Feed-Announce crash surface (routes.py:914-924 into the Create/Update arm) ---
#
# Reachability was verified by reading the preamble before any test here was written.
# For an Announce whose OUTER actor resolves to a Feed: routes.py:862 resolves
# `community` to None (the actor isn't a Community); routes.py:864 then resolves
# `feed` to the Feed row; routes.py:865's `if not feed:` is False so `user` is never
# looked up there. routes.py:867's `if not community and not feed and not user:`
# guard does not fire, since `feed` is truthy. At routes.py:914, `if not feed:` is
# again False, so the else at 923-924 runs: `user = None` explicitly. `community`
# is never reassigned anywhere in this path, so it is still the None from 862.
# routes.py:928 sets `announced = True`. So the Create/Update arm is entered
# (routes.py:1193) with `user is None`, `community is None`, `announced is True` --
# exactly the brief's claim. 5a's test_an_announce_from_a_feed_skips_the_inner_actor_walk
# (tests/test_inbox_dispatch_announce.py:386-417) independently confirms `user is
# None` is reached this way; this file adds the observation of what the Create/Update
# arm specifically does with that state, since 5a's test never reaches core_activity's
# 'Create'/'Update' branch (line 1193) at all -- it uses a 'Like' inner activity.


def _seed_feed_announcer(host='peer.example'):
    """A Feed resolvable as the OUTER Announce actor.

    This is the whole crash surface in one fixture. When the preamble resolves
    an Announce's actor to a FEED, its `if not feed:` takes the else, so `user`
    is left None -- and `community` was never set either -- while `announced`
    becomes True. 5a proved this shape reachable in
    tests/test_inbox_dispatch_announce.py::_seed_announcing_feed.
    """
    make_site()
    instance = make_instance(host)
    feed = make_feed(instance)
    return instance, feed


def announced_create(feed, inner_object, *, inner_type='Create'):
    """An Announce sent BY a feed, wrapping a Create/Update.

    The preamble sets `core_activity = request_json['object']`, so the inner
    dict IS the Create activity the arm then dispatches on. The inner `actor`
    is deliberately absent: it is read only under `if not feed:`, which a feed
    Announce skips, and including one would imply it mattered.
    """
    return inbox_activity(feed, activity_type='Announce',
                          object={'id': f'{feed.ap_profile_id}/activities/inner',
                                  'type': inner_type,
                                  'object': inner_object})


def test_a_feed_announced_group_update_is_refused_instead_of_crashing_on_a_none_community(
        app, db_session, monkeypatch):
    """Was `test_a_feed_announced_group_update_crashes_on_a_none_community`,
    which pinned the Group half of the crash: `announced` is True, so the arm
    skipped its `if not announced and not community:` resolution chain
    entirely and `community` was still None at `community.is_local()`.

    Now the arm refuses before it ever reaches the object_type dispatch --
    `user is None and community is None` is caught immediately after the
    ChatMessage branch, well before the Group branch's `community.is_local()`
    would be evaluated. `refresh_community_profile` is doubled here (unlike
    the crash-pinning version) precisely to prove it is never reached.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, feed = _seed_feed_announcer()
    calls = record_moderation(monkeypatch, 'refresh_community_profile')

    activity = announced_create(feed,
                                {'type': 'Group', 'id': 'https://peer.example/c/books'},
                                inner_type='Update')

    dispatch(activity)

    assert calls['refresh_community_profile'] == []
    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Cannot process Create/Update: no user or community resolved'


def test_a_feed_announced_poll_vote_is_refused_instead_of_crashing_on_a_none_user(
        app, db_session, monkeypatch):
    """Was `test_a_feed_announced_poll_vote_crashes_on_a_none_user`, which
    pinned the poll half of the crash: `vote_for_choice(choice.id, user.id)`
    used to raise once a post, its poll and a matching choice all resolved --
    so all three are still seeded here, to prove the new guard fires even
    though every downstream conjunct would otherwise be satisfied. A test
    that seeded less would pass for the wrong reason, by returning early
    before ever reaching the poll block at all.

    `author` is created BEFORE `make_community`: make_community hardcodes
    `user_id=1` (tests/factories.py:140), and `_seed_feed_announcer` creates no
    User at all (only a Site, an Instance and a Feed) -- so a User has to exist
    first or the community insert violates the user.id foreign key. Creating
    `author` here first also makes it the row that lands on id 1, satisfying
    that constraint; the instance from `_seed_feed_announcer` is likewise the
    first Instance row, landing on id 1 to satisfy make_community's hardcoded
    `instance_id=1`.

    No vote is recorded, since the guard now fires before the poll block is
    ever reached.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, feed = _seed_feed_announcer()
    author = make_user(instance, 'author')
    community = make_community(host='peer.example')
    post = make_post(community, author, 'https://peer.example/post/1')
    make_poll(post)
    choice = make_poll_choice(post, 'yes')
    db.session.commit()

    activity = announced_create(feed, poll_note(post.ap_id, 'yes'))

    dispatch(activity)

    from app.models import PollChoiceVote
    assert db_session.query(PollChoiceVote).count() == 0
    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Cannot process Create/Update: no user or community resolved'


def test_a_feed_announced_page_is_refused_instead_of_reaching_process_new_content(
        app, db_session, monkeypatch):
    """Was `test_a_feed_announced_page_hands_process_new_content_two_nones`,
    which pinned the third consequence as a NON-crash: at the time, `process_new_content`
    was doubled, so the test only observed that it received two `None`s
    (`user` and `community`), not what it would have done with them.

    That was an artefact of doubling, not of the real code: `process_new_content`'s
    first executable line is `if user.user_name == 'rimu':`, so
    `process_new_content(None, ...)` crashes on `user.user_name` immediately.
    All three of Task 8's consequences were crashes; this one only looked
    survivable because the delegate was replaced. `process_new_content` is
    doubled here too (to keep the assertion cheap and avoid depending on its
    internals), but the finding is recorded correctly as "no longer reached"
    -- because the pre-fix code would have crashed here, not merely produced
    a bad call.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, feed = _seed_feed_announcer()
    calls = record_moderation(monkeypatch, 'process_new_content')

    dispatch(announced_create(feed, {'type': 'Page', 'id': 'https://peer.example/post/1'}))

    assert calls['process_new_content'] == []
    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Cannot process Create/Update: no user or community resolved'


def test_a_feed_announced_chat_message_is_refused_instead_of_crashing_on_a_none_user(
        app, db_session, monkeypatch):
    """A fourth consequence Task 8 did not enumerate: the ChatMessage branch
    sits ABOVE the poll-vote/object_type dispatch, so a guard placed only
    below it (as this campaign's first pass at Fix A did) leaves this branch
    unprotected. `process_chat`'s second statement is
    `sender = session.query(User).get(user.id)`, so `process_chat(None, ...)`
    crashes on `user.id` immediately -- and the inner object's `type` is
    entirely peer-controlled, so any peer announcing through a feed could
    reach this by wrapping a ChatMessage instead of a Page or a Group.

    The guard now sits before the ChatMessage check as well as everything
    else in the arm, so this is refused the same way and `process_chat` is
    never reached. `process_chat` is doubled and asserted never called, since
    the crash would otherwise be immediate and uninformative about what was
    prevented.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, feed = _seed_feed_announcer()
    calls = record_moderation(monkeypatch, 'process_chat')

    activity = announced_create(feed, {'type': 'ChatMessage', 'id': 'https://peer.example/pm/1'})

    dispatch(activity)

    assert calls['process_chat'] == []
    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Cannot process Create/Update: no user or community resolved'
