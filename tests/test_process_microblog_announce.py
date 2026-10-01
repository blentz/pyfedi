import pytest

from tests.factories import make_community, make_follow, make_instance, make_post, make_site, make_user


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


@pytest.fixture
def log_spy(monkeypatch):
    """Capture the (aplog_type, aplog_result, message) passed to log_incoming_ap.

    LOG_ACTIVITYPUB_TO_DB defaults False (config.py:84), so log_incoming_ap is a
    no-op under test unless intercepted here. This makes the distinct-reason-
    string requirement on every exit path observable and assertable.
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


def test_unfollowed_actor_is_rejected_without_fetching(db_session, fetch_spy, log_spy):
    """The trust gate runs before any network I/O"""
    from app.activitypub.util import process_microblog_announce
    _, calls = fetch_spy
    instance = make_instance('m.example')
    stranger = make_user(instance, 'stranger')

    result = process_microblog_announce(
        announce(stranger.ap_public_url, 'https://other.example/notes/9'), 'a1', False)

    assert result is None
    assert calls == [], 'no fetch may happen for an unfollowed actor'
    assert log_spy[-1][2] == 'Announce from unfollowed actor'


def test_banned_actor_is_rejected_without_fetching(db_session, fetch_spy, followed_booster, log_spy):
    """A banned actor is rejected, but NOT via process_microblog_announce's own
    announcer.banned branch.

    find_actor_or_create_cached() -> find_actor_or_create() -> find_actor_by_url()
    already rejects a banned actor upstream: for any actor that already exists as
    a User row (which followed_booster does, having been created by the fixture),
    find_actor_by_url() calls validate_remote_actor(actor_url, actor) WITH the
    actor object, which returns False for actor.banned (app/activitypub/actor.py
    lines 59-60), and find_actor_or_create() then returns None. So
    process_microblog_announce exits at the "Announce actor is not a known user"
    branch, not at its own `if announcer.banned:` check -- that check is
    defence in depth that the lookup now makes unreachable, a stale ID-cache
    hit included (D59).
    """
    from app import db
    from app.activitypub.util import process_microblog_announce
    _, calls = fetch_spy
    followed_booster.banned = True
    db.session.commit()

    result = process_microblog_announce(
        announce(followed_booster.ap_public_url, 'https://other.example/notes/9'), 'a2', False)

    assert result is None
    assert calls == []
    assert log_spy[-1][2] == 'Announce actor is not a known user'


def test_boosted_reply_is_ignored(db_session, fetch_spy, followed_booster, log_spy):
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
    assert log_spy[-1][2] == 'Boosted object is a reply'


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


def test_announce_without_object_is_rejected(db_session, fetch_spy, followed_booster, log_spy):
    """A malformed Announce is rejected without fetching"""
    from app.activitypub.util import process_microblog_announce
    _, calls = fetch_spy

    result = process_microblog_announce(
        {'id': 'a6', 'type': 'Announce', 'actor': followed_booster.ap_public_url}, 'a6', False)

    assert result is None
    assert calls == []
    assert log_spy[-1][2] == 'Announce has no object URI'


def test_successful_boost_creates_post_and_records_boost(db_session, fetch_spy, followed_booster):
    """The success path: the six-argument positional call to create_resolved_object
    resolves a realistic Mastodon-shaped Note into a real Post, and the boost is
    recorded for the announcer. This is the riskiest line in the function -- a
    positional-argument-order regression would otherwise ship silently.
    """
    from app.activitypub.util import process_microblog_announce
    from app.models import Post, PostBoost
    fake_fetch, calls = fetch_spy

    make_site()  # Post.new() -> blocked_phrases() looks up Site id 1 unconditionally
    author = make_user(make_instance('other.example'), 'alice')
    note_uri = 'https://other.example/notes/42'
    fake_fetch.result = {
        'id': note_uri,
        'type': 'Note',
        'attributedTo': author.ap_profile_id,
        'content': '<p>hello fediverse</p>',
        'published': '2026-08-20T12:00:00Z',
        'to': ['https://www.w3.org/ns/activitystreams#Public'],
    }

    result = process_microblog_announce(
        announce(followed_booster.ap_public_url, note_uri), 'a7', False)

    assert isinstance(result, Post)
    assert result.ap_id == note_uri
    assert len(calls) == 1, 'exactly one fetch for a newly-seen post'
    assert PostBoost.query.filter_by(post_id=result.id, user_id=followed_booster.id).count() == 1


def test_boost_of_private_community_post_is_refused(db_session, fetch_spy, followed_booster, log_spy):
    """FINDING 2: local post ap_ids are guessable (https://<server>/post/<id>), so
    any remote actor a single local user follows could Announce an arbitrary
    local post URI and get a PostBoost row created for a post in a private
    (invite-only) community it was never a member of. The get_by_ap_id
    short-circuit must refuse before record_boost, not after.
    """
    from app import db
    from app.activitypub.util import process_microblog_announce
    from app.models import PostBoost
    _, calls = fetch_spy
    author = make_user(make_instance('other.example'), 'bob')
    community = make_community('secret-club')
    community.private = True
    db.session.commit()
    post = make_post(community, author, 'https://other.example/notes/70')

    result = process_microblog_announce(
        announce(followed_booster.ap_public_url, post.ap_id), 'b1', False)

    assert result is None
    assert calls == [], 'the post is already held locally; no fetch should happen'
    assert PostBoost.query.filter_by(post_id=post.id).count() == 0
    assert log_spy[-1][2] == 'Boosted post belongs to a private or local_only community'


def test_boost_of_local_only_community_post_is_refused(db_session, fetch_spy, followed_booster, log_spy):
    """Same as above, for a local_only community (no federation intended at all)."""
    from app import db
    from app.activitypub.util import process_microblog_announce
    from app.models import PostBoost
    _, calls = fetch_spy
    author = make_user(make_instance('other2.example'), 'carol')
    community = make_community('local-only-comm')
    community.local_only = True
    db.session.commit()
    post = make_post(community, author, 'https://other2.example/notes/71')

    result = process_microblog_announce(
        announce(followed_booster.ap_public_url, post.ap_id), 'b2', False)

    assert result is None
    assert calls == []
    assert PostBoost.query.filter_by(post_id=post.id).count() == 0
    assert log_spy[-1][2] == 'Boosted post belongs to a private or local_only community'


def test_rejected_follow_does_not_open_gate(db_session, fetch_spy):
    """A follow the remote side explicitly rejected (is_accepted False) must not
    grant trust -- only pending or accepted follows should."""
    from app.activitypub.util import process_microblog_announce
    _, calls = fetch_spy
    instance = make_instance('m2.example')
    booster = make_user(instance, 'rejected_booster')
    local = make_user(None, 'localuser2', local=True)
    make_follow(local, booster, is_accepted=False)

    result = process_microblog_announce(
        announce(booster.ap_public_url, 'https://other.example/notes/9'), 'a8', False)

    assert result is None
    assert calls == []


def test_pending_follow_opens_gate(db_session, fetch_spy):
    """A pending follow (is_accepted None -- request sent, not yet accepted)
    still lets the timeline work: Mastodon follows can sit pending indefinitely."""
    from app.activitypub.util import process_microblog_announce
    instance = make_instance('m3.example')
    booster = make_user(instance, 'pending_booster')
    local = make_user(None, 'localuser3', local=True)
    make_follow(local, booster, is_accepted=None)
    author = make_user(make_instance('other2.example'), 'carol')
    post = make_post(make_community(), author, 'https://other2.example/notes/11')

    result = process_microblog_announce(
        announce(booster.ap_public_url, post.ap_id), 'a9', False)

    assert result is not None and result.id == post.id


def test_missing_actor_is_rejected_without_fetching(db_session, fetch_spy, log_spy):
    """An Announce missing 'actor' entirely must be rejected, logged, and not
    raise -- routes.py:617 makes this unreachable from the live inbox, but this
    is a public util Tasks 5 and 6 also call."""
    from app.activitypub.util import process_microblog_announce
    _, calls = fetch_spy

    result = process_microblog_announce(
        {'id': 'a10', 'type': 'Announce', 'object': 'https://other.example/notes/9'}, 'a10', False)

    assert result is None
    assert calls == []
    assert log_spy[-1][2] == 'Announce has no usable actor'


def test_list_actor_is_rejected_without_fetching(db_session, fetch_spy, log_spy):
    """An actor of a type find_actor_or_create_cached() cannot handle (it calls
    .strip() on a bare string or unwraps a dict's 'id', so a list would raise
    AttributeError) must be rejected before reaching that call, not raise."""
    from app.activitypub.util import process_microblog_announce
    _, calls = fetch_spy

    result = process_microblog_announce(
        {'id': 'a11', 'type': 'Announce', 'actor': ['https://m.example/users/booster'],
         'object': 'https://other.example/notes/9'}, 'a11', False)

    assert result is None
    assert calls == []
    assert log_spy[-1][2] == 'Announce has no usable actor'


def test_rejection_reasons_are_all_distinct(db_session, fetch_spy, followed_booster, log_spy):
    """Every distinct rejection branch this suite exercises logs a distinct
    reason string, per the plan constraint that every exit path logs a distinct
    reason. (The rejected-follow case is deliberately not included here: it hits
    the same "unfollowed actor" branch as an actual stranger, so it is expected
    to log the identical string -- that is covered by
    test_rejected_follow_does_not_open_gate instead, with its own log_spy.)
    """
    from app.activitypub.util import process_microblog_announce
    fake_fetch, calls = fetch_spy

    # Branch: no object URI
    process_microblog_announce(
        {'id': 'd1', 'type': 'Announce', 'actor': followed_booster.ap_public_url}, 'd1', False)

    # Branch: no usable actor
    process_microblog_announce(
        {'id': 'd2', 'type': 'Announce', 'object': 'https://other.example/notes/1'}, 'd2', False)

    # Branch: actor is not a known user (never-seen-before actor URL)
    process_microblog_announce(
        announce('https://unknown.example/users/nobody', 'https://other.example/notes/1'), 'd3', False)

    # Branch: unfollowed actor (exists, not banned, not followed)
    stranger = make_user(make_instance('stranger.example'), 'stranger_d')
    process_microblog_announce(
        announce(stranger.ap_public_url, 'https://other.example/notes/1'), 'd4', False)

    # Branch: boosted object is a reply
    fake_fetch.result = {
        'id': 'https://other.example/notes/2',
        'type': 'Note',
        'inReplyTo': 'https://other.example/notes/1',
        'attributedTo': 'https://other.example/users/bob',
    }
    process_microblog_announce(
        announce(followed_booster.ap_public_url, 'https://other.example/notes/2'), 'd5', False)

    # Branch: fetch failed
    fake_fetch.result = None
    process_microblog_announce(
        announce(followed_booster.ap_public_url, 'https://other.example/notes/3'), 'd6', False)

    reasons = [message for (_aplog_type, _aplog_result, message) in log_spy]
    assert len(reasons) == len(set(reasons)), f'duplicate reason strings logged: {reasons}'
