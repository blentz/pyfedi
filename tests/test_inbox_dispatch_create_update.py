"""tests/test_inbox_dispatch_create_update.py"""
from datetime import timedelta

import pytest

from app import db
from app.activitypub import routes as activitypub_routes
from app.models import ActivityPubLog, utcnow
from tests.factories import (inbox_activity, make_community, make_instance, make_poll,
                             make_poll_choice, make_post, make_user, seed_community_owner)
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


def test_a_poll_vote_for_an_unknown_post_is_dropped_silently(app, db_session, monkeypatch):
    """`post_being_replied_to` is None, so the block falls to its unconditional
    `return` having logged NOTHING -- asserted with LOG_ACTIVITYPUB_TO_DB
    explicitly True so the zero is real silence, not logging switched off.

    It also does NOT fall through to content handling: `process_new_content` is
    doubled and must not be called. That combination -- consumed, unlogged,
    unprocessed -- is the finding this test exists to pin.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, voter, post, poll, choice = seed_poll_post()
    calls = record_moderation(monkeypatch, 'process_new_content')

    dispatch(create_activity(voter, poll_note('https://peer.example/post/404', 'yes')))

    assert ActivityPubLog.query.count() == 0
    assert calls['process_new_content'] == []


def test_a_poll_vote_on_a_post_with_no_poll_is_dropped_silently(app, db_session, monkeypatch):
    """`poll_data` is None: the post exists but carries no Poll row. Same
    silence as above, reached by a different conjunct of `if poll_data and
    choice:` -- which is why this test and the next are separate.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    community = make_community(host='peer.example')
    author = make_user(instance, 'author')
    voter = make_user(instance, 'voter')
    post = make_post(community, author, 'https://peer.example/post/1')
    db.session.commit()

    dispatch(create_activity(voter, poll_note(post.ap_id, 'yes')))

    assert ActivityPubLog.query.count() == 0


def test_a_poll_vote_for_an_unknown_choice_is_dropped_silently(app, db_session, monkeypatch):
    """`choice` is None: the poll exists but has no option with this `name`.
    The other conjunct of `if poll_data and choice:`.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, voter, post, poll, choice = seed_poll_post(choice_text='yes')

    dispatch(create_activity(voter, poll_note(post.ap_id, 'maybe')))

    from app.models import PollChoiceVote
    assert db_session.query(PollChoiceVote).count() == 0
    assert ActivityPubLog.query.count() == 0


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
