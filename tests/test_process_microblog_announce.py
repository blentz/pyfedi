import pytest

from tests.factories import make_community, make_follow, make_instance, make_post, make_user


@pytest.fixture
def followed_booster(db_session):
    """A remote account that a local user follows."""
    instance = make_instance('m.example')
    booster = make_user(instance, 'booster')
    local = make_user(None, 'localuser', local=True)
    make_follow(local, booster)
    return booster


@pytest.fixture
def fetch_spy(monkeypatch):
    """Replace the remote fetch, and count how many times it is called."""
    calls = []

    def fake_fetch(uri):
        calls.append(uri)
        return fake_fetch.result

    fake_fetch.result = None
    monkeypatch.setattr('app.activitypub.util.remote_object_to_json', fake_fetch)
    return fake_fetch, calls


def announce(actor_uri, object_uri):
    return {
        'id': f'{actor_uri}/statuses/1/activity',
        'type': 'Announce',
        'actor': actor_uri,
        'object': object_uri,
    }


def test_unfollowed_actor_is_rejected_without_fetching(db_session, fetch_spy):
    """The trust gate runs before any network I/O"""
    from app.activitypub.util import process_microblog_announce
    _, calls = fetch_spy
    instance = make_instance('m.example')
    stranger = make_user(instance, 'stranger')

    result = process_microblog_announce(
        announce(stranger.ap_public_url, 'https://other.example/notes/9'), 'a1', False)

    assert result is None
    assert calls == [], 'no fetch may happen for an unfollowed actor'


def test_banned_actor_is_rejected_without_fetching(db_session, fetch_spy, followed_booster):
    """A banned actor is rejected even if followed"""
    from app import db
    from app.activitypub.util import process_microblog_announce
    _, calls = fetch_spy
    followed_booster.banned = True
    db.session.commit()

    result = process_microblog_announce(
        announce(followed_booster.ap_public_url, 'https://other.example/notes/9'), 'a2', False)

    assert result is None
    assert calls == []


def test_boosted_reply_is_ignored(db_session, fetch_spy, followed_booster):
    """A boosted reply is dropped; ancestor backfill is out of scope"""
    from app.activitypub.util import process_microblog_announce
    fake_fetch, calls = fetch_spy
    fake_fetch.result = {
        'id': 'https://other.example/notes/9',
        'type': 'Note',
        'inReplyTo': 'https://other.example/notes/8',
        'attributedTo': 'https://other.example/users/bob',
    }

    result = process_microblog_announce(
        announce(followed_booster.ap_public_url, 'https://other.example/notes/9'), 'a3', False)

    assert result is None
    assert len(calls) == 1, 'exactly one fetch per activity'


def test_existing_local_post_is_boosted_without_fetching(db_session, fetch_spy, followed_booster):
    """A boost of a post we already have short-circuits before the fetch"""
    from app.activitypub.util import process_microblog_announce
    from app.models import PostBoost
    _, calls = fetch_spy
    author = make_user(make_instance('other.example'), 'bob')
    post = make_post(make_community(), author, 'https://other.example/notes/7')

    result = process_microblog_announce(
        announce(followed_booster.ap_public_url, post.ap_id), 'a4', False)

    assert result is not None and result.id == post.id
    assert calls == [], 'a post we already have must not be fetched'
    assert PostBoost.query.filter_by(post_id=post.id, user_id=followed_booster.id).count() == 1


def test_redelivery_does_not_double_count(db_session, fetch_spy, followed_booster):
    """The same Announce arriving twice records one boost"""
    from app.activitypub.util import process_microblog_announce
    from app.models import PostBoost
    author = make_user(make_instance('other.example'), 'bob')
    post = make_post(make_community(), author, 'https://other.example/notes/7')
    activity = announce(followed_booster.ap_public_url, post.ap_id)

    process_microblog_announce(activity, 'a5', False)
    process_microblog_announce(activity, 'a5', False)

    assert PostBoost.query.filter_by(post_id=post.id).count() == 1


def test_announce_without_object_is_rejected(db_session, fetch_spy, followed_booster):
    """A malformed Announce is rejected without fetching"""
    from app.activitypub.util import process_microblog_announce
    _, calls = fetch_spy

    result = process_microblog_announce(
        {'id': 'a6', 'type': 'Announce', 'actor': followed_booster.ap_public_url}, 'a6', False)

    assert result is None
    assert calls == []
