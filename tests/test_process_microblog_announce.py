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

    def fake_fetch(uri, retry_delay=3):
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


# --------------------------------------------------------------------------
# A follower of a local author boosting that author's post (interop: Mastodon follower boosts a microblog)
# --------------------------------------------------------------------------


def _local_microblogger_and_follower(is_accepted=True):
    """A local author with a post, and a remote account that follows that author and that no local user follows.
    This is what a Mastodon user who follows a PieFed account looks like when they boost its post."""
    booster_instance = make_instance('mastodon.example')   # first, so a local user's instance_id=1 resolves
    author = make_user(None, 'alice', local=True)
    booster = make_user(booster_instance, 'bob')
    make_follow(author, booster, is_accepted=is_accepted, is_inward=True)
    post = make_post(make_community(), author, 'https://piefed.example/post/1')
    return author, booster, post


def test_a_follower_of_the_local_author_boosts_that_authors_post(db_session, fetch_spy):
    """Spec 2026-10-14 §6.4: an Announce of our object counts as a boost. The announcer follows the author, not the
    other way round, so announcer_is_followed does not open the gate; following the author does."""
    from app.activitypub.util import process_microblog_announce
    from app.models import PostBoost
    _, calls = fetch_spy
    _, booster, post = _local_microblogger_and_follower()

    result = process_microblog_announce(announce(booster.ap_public_url, post.ap_id), 'f1', False)

    assert result is not None and result.id == post.id
    assert calls == [], 'a post we hold must not be fetched'
    assert PostBoost.query.filter_by(post_id=post.id, user_id=booster.id).count() == 1


def test_a_follower_of_one_local_author_cannot_boost_another_authors_post(db_session, fetch_spy, log_spy):
    from app.activitypub.util import process_microblog_announce
    from app.models import PostBoost
    _, calls = fetch_spy
    _, booster, _ = _local_microblogger_and_follower()
    other = make_user(None, 'carol', local=True)
    others_post = make_post(make_community('other-club'), other, 'https://piefed.example/post/2')

    result = process_microblog_announce(announce(booster.ap_public_url, others_post.ap_id), 'f2', False)

    assert result is None
    assert calls == []
    assert PostBoost.query.filter_by(post_id=others_post.id).count() == 0
    assert log_spy[-1][2] == 'Announce from unfollowed actor'


def test_a_follow_the_author_has_not_accepted_does_not_open_the_gate(db_session, fetch_spy, log_spy):
    from app.activitypub.util import process_microblog_announce
    from app.models import PostBoost
    _, booster, post = _local_microblogger_and_follower(is_accepted=None)

    result = process_microblog_announce(announce(booster.ap_public_url, post.ap_id), 'f3', False)

    assert result is None
    assert PostBoost.query.filter_by(post_id=post.id).count() == 0
    assert log_spy[-1][2] == 'Announce from unfollowed actor'


def test_a_follower_of_the_local_author_still_cannot_boost_into_a_private_community(db_session, fetch_spy, log_spy):
    from app import db
    from app.activitypub.util import process_microblog_announce
    from app.models import PostBoost
    _, booster, post = _local_microblogger_and_follower()
    post.community.private = True
    db.session.commit()

    result = process_microblog_announce(announce(booster.ap_public_url, post.ap_id), 'f4', False)

    assert result is None
    assert PostBoost.query.filter_by(post_id=post.id).count() == 0
    assert log_spy[-1][2] == 'Boosted post belongs to a private or local_only community'


# --------------------------------------------------------------------------
# A boost of a local post is refused when the author blocked the announcer, the post is gone or cannot be viewed
# (spec 2026-10-14 §10 A10)
# --------------------------------------------------------------------------


def test_an_author_who_blocked_the_announcer_after_the_follow_gets_no_boost(db_session, fetch_spy, log_spy):
    from app.activitypub.util import process_microblog_announce
    from app.models import PostBoost, UserFollower
    from tests.factories import make_user_block
    author, booster, post = _local_microblogger_and_follower()
    make_user_block(author, booster)    # the accepted inward UserFollower row is still there
    assert UserFollower.query.filter_by(local_user_id=author.id, remote_user_id=booster.id, is_accepted=True).count() == 1

    result = process_microblog_announce(announce(booster.ap_public_url, post.ap_id), 'b1', False)

    assert result is None
    assert PostBoost.query.filter_by(post_id=post.id).count() == 0
    assert log_spy[-1][2] == 'Boosted post author has blocked the announcer or its instance'


def test_an_author_who_blocked_the_announcers_instance_gets_no_boost(db_session, fetch_spy, log_spy):
    from app.activitypub.util import process_microblog_announce
    from app.models import PostBoost
    from tests.factories import make_instance_block
    author, booster, post = _local_microblogger_and_follower()
    make_instance_block(author, booster.instance)

    result = process_microblog_announce(announce(booster.ap_public_url, post.ap_id), 'b2', False)

    assert result is None
    assert PostBoost.query.filter_by(post_id=post.id).count() == 0
    assert log_spy[-1][2] == 'Boosted post author has blocked the announcer or its instance'


def test_a_block_by_the_author_also_stops_an_announcer_a_local_user_follows(db_session, fetch_spy, log_spy):
    from app.activitypub.util import process_microblog_announce
    from app.models import PostBoost
    from tests.factories import make_user_block
    author, booster, post = _local_microblogger_and_follower()
    make_follow(make_user(None, 'carol', local=True), booster)     # announcer_is_followed opens the gate too
    make_user_block(author, booster)

    assert process_microblog_announce(announce(booster.ap_public_url, post.ap_id), 'b3', False) is None
    assert PostBoost.query.filter_by(post_id=post.id).count() == 0


def test_a_deleted_post_gets_no_boost(db_session, fetch_spy, log_spy):
    from app import db
    from app.activitypub.util import process_microblog_announce
    from app.models import PostBoost
    _, booster, post = _local_microblogger_and_follower()
    post.deleted = True
    db.session.commit()

    assert process_microblog_announce(announce(booster.ap_public_url, post.ap_id), 'd1', False) is None
    assert PostBoost.query.filter_by(post_id=post.id).count() == 0
    assert log_spy[-1][2] == 'Boosted post is deleted or not visible to the announcer'


def test_a_followers_only_post_is_not_boosted_even_by_an_accepted_follower(db_session, fetch_spy, log_spy):
    """can_view reads a viewer's accepted OUTWARD follow (a local user following the author). A remote follower of a
    local author is an inward row, so can_view is False for them: the boost is refused, which is the safe side."""
    from app import db
    from app.activitypub.util import process_microblog_announce
    from app.constants import VISIBILITY_FOLLOWERS
    from app.models import PostBoost
    from app.visibility import can_view
    _, booster, post = _local_microblogger_and_follower()
    post.visibility = VISIBILITY_FOLLOWERS
    db.session.commit()
    assert not can_view(post, booster.id)

    assert process_microblog_announce(announce(booster.ap_public_url, post.ap_id), 'v1', False) is None
    assert PostBoost.query.filter_by(post_id=post.id).count() == 0
    assert log_spy[-1][2] == 'Boosted post is deleted or not visible to the announcer'


def test_a_followers_only_post_is_not_boosted_by_a_non_follower_who_passed_another_gate(db_session, fetch_spy, log_spy):
    from app import db
    from app.activitypub.util import process_microblog_announce
    from app.constants import VISIBILITY_FOLLOWERS
    from app.models import PostBoost
    elsewhere = make_instance('elsewhere.example')      # first, so a local user's instance_id=1 resolves
    author = make_user(None, 'alice', local=True)
    followed = make_user(elsewhere, 'dana')
    make_follow(make_user(None, 'carol', local=True), followed)    # announcer_is_followed opens the gate
    post = make_post(make_community(), author, 'https://piefed.example/post/9')
    post.visibility = VISIBILITY_FOLLOWERS
    db.session.commit()

    assert process_microblog_announce(announce(followed.ap_public_url, post.ap_id), 'v2', False) is None
    assert PostBoost.query.filter_by(post_id=post.id).count() == 0
    assert log_spy[-1][2] == 'Boosted post is deleted or not visible to the announcer'
