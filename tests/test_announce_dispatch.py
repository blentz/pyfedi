import pytest

from app.constants import APLOG_ANNOUNCE, APLOG_DUPLICATE, APLOG_FAILURE, APLOG_IGNORED, APLOG_SUCCESS
from tests.factories import make_community, make_follow, make_instance, make_post, make_user


@pytest.fixture
def followed_booster(db_session):
    instance = make_instance('m.example')
    booster = make_user(instance, 'booster')
    local = make_user(None, 'localuser', local=True)
    make_follow(local, booster)
    return booster


@pytest.fixture
def log_spy(monkeypatch):
    """Capture the (aplog_type, aplog_result, message) passed to log_incoming_ap.

    LOG_ACTIVITYPUB_TO_DB defaults False (config.py:84), so log_incoming_ap is a
    no-op under test unless intercepted here. This makes double-logging (or its
    absence) on the microblog path observable and assertable.
    """
    calls = []

    def fake_log(id, aplog_type, aplog_result, saved_json, message=None, session=None):
        calls.append((aplog_type, aplog_result, message))

    monkeypatch.setattr('app.activitypub.util.log_incoming_ap', fake_log)
    return calls


def announce(actor_uri, object_uri):
    return {
        'id': f'{actor_uri}/statuses/1/activity',
        'type': 'Announce',
        'actor': actor_uri,
        'object': object_uri,
    }


def make_owned_community(name):
    """make_community() hardcodes owner user_id=1 and instance_id=1; both rows must exist."""
    make_user(make_instance('owner.example'), 'community_owner')
    return make_community(name)


def test_local_post_boost_is_recorded(db_session, followed_booster, monkeypatch):
    """A followed account boosting a locally authored post records the boost"""
    from app.activitypub.util import process_announce_of_uri
    from app.models import PostBoost
    monkeypatch.setattr('app.activitypub.util.remote_object_to_json',
                        lambda uri: pytest.fail('must not fetch local content'))
    author = make_user(None, 'localauthor', local=True)
    post = make_post(make_community('news'), author, 'https://test.piefed.local/c/news/p/1/hello')

    result = process_announce_of_uri(announce(followed_booster.ap_public_url, post.ap_id),
                                     None, 'd1', False)

    assert result is not None and result.id == post.id
    assert PostBoost.query.filter_by(post_id=post.id, user_id=followed_booster.id).count() == 1


def test_community_announce_of_local_content_is_ignored(db_session, monkeypatch, log_spy):
    """The community path still discards duplicates of local content, logging
    exactly the same type/result/message routes.py used to log at its old call
    site, exactly once."""
    from app.activitypub.util import process_announce_of_uri
    monkeypatch.setattr('app.activitypub.util.resolve_remote_post',
                        lambda *args, **kwargs: pytest.fail('must not resolve local content'))
    community = make_owned_community('news')

    result = process_announce_of_uri(
        announce('https://m.example/users/booster', 'https://test.piefed.local/c/news/p/1/hello'),
        community, 'd2', False)

    assert result is None
    assert len(log_spy) == 1
    aplog_type, aplog_result, message = log_spy[0]
    assert (aplog_type, aplog_result) == (APLOG_DUPLICATE, APLOG_IGNORED)
    assert message == 'Activity about local content which is already present'


def test_community_announce_of_remote_content_resolves(db_session, monkeypatch, log_spy):
    """The community path still resolves remote content, logging exactly the
    same success type/result routes.py used to log at its old call site,
    exactly once."""
    from app.activitypub.util import process_announce_of_uri
    seen = []
    monkeypatch.setattr('app.activitypub.util.resolve_remote_post',
                        lambda uri, community, announce_id, store: seen.append(uri) or 'resolved')
    community = make_owned_community('news')

    result = process_announce_of_uri(
        announce('https://m.example/users/booster', 'https://other.example/notes/3'),
        community, 'd3', False)

    assert result == 'resolved'
    assert seen == ['https://other.example/notes/3']
    assert len(log_spy) == 1
    aplog_type, aplog_result, message = log_spy[0]
    assert (aplog_type, aplog_result) == (APLOG_ANNOUNCE, APLOG_SUCCESS)
    assert message is None


def test_community_announce_resolution_failure_logs_once(db_session, monkeypatch, log_spy):
    """The community path still logs a failure, with the same type/result/
    message routes.py used to log at its old call site, exactly once -- not
    twice, which was the regression this test guards against."""
    from app.activitypub.util import process_announce_of_uri
    monkeypatch.setattr('app.activitypub.util.resolve_remote_post',
                        lambda *args, **kwargs: None)
    community = make_owned_community('news')

    result = process_announce_of_uri(
        announce('https://m.example/users/booster', 'https://other.example/notes/9'),
        community, 'd6', False)

    assert result is None
    assert len(log_spy) == 1
    aplog_type, aplog_result, message = log_spy[0]
    assert (aplog_type, aplog_result) == (APLOG_ANNOUNCE, APLOG_FAILURE)
    assert message == 'Could not resolve post'


def test_community_announce_without_object_uri_logs_once(db_session, monkeypatch, log_spy):
    """A malformed Announce (empty object string) on the community path is
    rejected with its own distinct reason, logged exactly once, instead of
    falling through to resolve_remote_post(None, ...)."""
    from app.activitypub.util import process_announce_of_uri
    monkeypatch.setattr('app.activitypub.util.resolve_remote_post',
                        lambda *args, **kwargs: pytest.fail('must not resolve a missing URI'))
    community = make_owned_community('news')

    result = process_announce_of_uri(
        announce('https://m.example/users/booster', ''), community, 'd7', False)

    assert result is None
    assert len(log_spy) == 1
    aplog_type, aplog_result, message = log_spy[0]
    assert (aplog_type, aplog_result) == (APLOG_ANNOUNCE, APLOG_FAILURE)
    assert message == 'Announce has no object URI'


def test_microblog_path_does_not_double_log_on_rejection(db_session, followed_booster, log_spy):
    """process_microblog_announce logs one specific rejection reason for an
    unfollowed actor; process_announce_of_uri must not add a second, generic
    row on top of it (the bug this task fixes in routes.py)."""
    from app.activitypub.util import process_announce_of_uri
    stranger = make_user(make_instance('stranger.example'), 'stranger')

    result = process_announce_of_uri(
        announce(stranger.ap_public_url, 'https://other.example/notes/1'), None, 'd4', False)

    assert result is None
    assert len(log_spy) == 1
    assert log_spy[0][2] == 'Announce from unfollowed actor'


def test_microblog_path_does_not_double_log_on_silent_success(db_session, followed_booster, log_spy):
    """A boost of a post already held locally short-circuits inside
    process_microblog_announce without logging anything (Task 4 behaviour).
    process_announce_of_uri must not add a log row of its own on top of that
    either."""
    from app.activitypub.util import process_announce_of_uri
    author = make_user(None, 'localauthor2', local=True)
    post = make_post(make_community('news2'), author, 'https://test.piefed.local/c/news2/p/2/hello')

    result = process_announce_of_uri(announce(followed_booster.ap_public_url, post.ap_id),
                                     None, 'd5', False)

    assert result is not None
    assert log_spy == []


def test_undo_boost_removes_the_row(db_session, followed_booster):
    """Un-boosting removes the PostBoost row and returns the post"""
    from app.activitypub.util import record_boost, undo_boost
    from app.models import PostBoost
    author = make_user(make_instance('other.example'), 'bob')
    post = make_post(make_community(), author, 'https://other.example/notes/7')
    record_boost(post, followed_booster)

    result = undo_boost(post.ap_id, followed_booster)

    assert result is not None and result.id == post.id
    assert PostBoost.query.filter_by(post_id=post.id).count() == 0
    assert post.post_boosts == []


def test_undo_boost_for_unknown_post_returns_none(db_session, followed_booster):
    """An Undo for a post we do not have is ignored, not an error"""
    from app.activitypub.util import undo_boost

    assert undo_boost('https://other.example/notes/404', followed_booster) is None


def test_undo_boost_twice_is_a_no_op(db_session, followed_booster):
    """Remote instances re-send; the second Undo must not raise"""
    from app.activitypub.util import record_boost, undo_boost
    author = make_user(make_instance('other.example'), 'bob')
    post = make_post(make_community(), author, 'https://other.example/notes/7')
    record_boost(post, followed_booster)

    undo_boost(post.ap_id, followed_booster)
    assert undo_boost(post.ap_id, followed_booster) is not None


def test_undo_boost_only_removes_the_undoing_users_boost(db_session, followed_booster):
    """One user's Undo leaves another user's boost intact"""
    from app.activitypub.util import record_boost, undo_boost
    from app.models import PostBoost
    author = make_user(make_instance('other.example'), 'bob')
    other = make_user(make_instance('third.example'), 'carol')
    post = make_post(make_community(), author, 'https://other.example/notes/7')
    record_boost(post, followed_booster)
    record_boost(post, other)

    undo_boost(post.ap_id, followed_booster)

    assert PostBoost.query.filter_by(post_id=post.id).count() == 1
    assert PostBoost.query.filter_by(post_id=post.id, user_id=other.id).count() == 1


def test_undo_boost_does_not_log(db_session, followed_booster, log_spy):
    """undo_boost() must never log; the routes.py Undo branch is the sole
    logger for this activity, exactly as undo_vote()'s call site is today.
    Covers both the found-and-removed path and the target-not-found path."""
    from app.activitypub.util import record_boost, undo_boost
    author = make_user(make_instance('other.example'), 'bob')
    post = make_post(make_community(), author, 'https://other.example/notes/7')
    record_boost(post, followed_booster)

    undo_boost(post.ap_id, followed_booster)
    undo_boost('https://other.example/notes/404', followed_booster)

    assert log_spy == []
