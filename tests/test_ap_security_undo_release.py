"""R-UNDORELEASE (spec §6.4, amendment A14): an accepted Undo releases the dedupe claim of the activity it undoes.

PeerTube reuses activity ids: a re-subscribe after an unsubscribe sends a Follow with the first one's id, and a
like after an Undo(Like) reuses `.../likes/<videoId>`. The inbox remembers every verified id for
ACTIVITY_DEDUPE_SECONDS, so the second activity was dropped as already seen: the resubscription stayed pending
forever and the re-like was lost.

Each test here goes through the real /inbox route with a real HTTP signature (DEBUG makes the dispatch inline),
because the claim is taken by the route and released by the dispatcher: neither half alone shows the defect.
"""
import contextlib

import pytest
from flask import current_app

from app import db
from app.activitypub import routes as activitypub_routes
from app.activitypub.signature import RsaKeys
from app.activitypub.util import activity_dedupe_key, release_undone_activity
from app.models import ActivityPubLog, CommunityMember, FeedMember, PostVote, UserFollower, utcnow
from tests.factories import make_community, make_feed, make_instance, make_post, make_site, make_user, signed_inbox_post

DUPLICATE = 'Already aware of this activity'   # the start of both messages: before and after the signature check


@pytest.fixture
def inline(app, monkeypatch, redis_double):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    monkeypatch.setitem(app.config, 'DEBUG', True)   # signing to test.piefed.local needs DEBUG; dispatch is inline
    # fakeredis has no Lua, so a lock cannot be released (tests/conftest.py redis_double's CAVEAT)
    monkeypatch.setattr(redis_double, 'lock', lambda *a, **kw: contextlib.nullcontext())
    sends = []
    monkeypatch.setattr(activitypub_routes, 'send_post_request',
                        lambda uri, body, private_key, key_id, **kw: sends.append(body))
    monkeypatch.setattr(activitypub_routes, 'announce_activity_to_followers', lambda *a, **kw: None)
    return sends


@pytest.fixture
def scene(db_session):
    """A local community with keys (it signs its Accept), a local user with keys, a post in the community, and two
    remote senders on one host: carol (the PeerTube account) and mallory."""
    make_site()
    owner_instance = make_instance('owner.example')
    owner = make_user(owner_instance, 'community_owner')
    community = make_community('vc2')
    community.private_key, community.public_key = RsaKeys.generate_keypair()
    local_user = make_user(owner_instance, 'bob', local=True, with_keys=True)
    local_user.ap_profile_id = f"https://{current_app.config['SERVER_NAME']}/u/{local_user.user_name}"
    local_user.ap_manually_approves_followers = False
    peer = make_instance('peer.example')
    carol = make_user(peer, 'carol', with_keys=True)
    mallory = make_user(peer, 'mallory', with_keys=True)
    carol.ap_fetched_at = mallory.ap_fetched_at = utcnow()
    db.session.commit()
    post = make_post(community, owner, f"https://{current_app.config['SERVER_NAME']}/post/77")
    db.session.commit()
    return community, local_user, post, carol, mallory


def _post(app, activity, sender):
    with app.test_client() as client:
        response = signed_inbox_post(client, activity, sender)
    assert response.status_code == 200
    db.session.expire_all()


def _follow(sender, target, n=7):
    return {'id': f'{sender.ap_profile_id}/follows/{n}', 'type': 'Follow', 'actor': sender.ap_profile_id,
            'object': target}


def _like(sender, post, kind='Like'):
    return {'id': f'{sender.ap_profile_id}/likes/{post.id}', 'type': kind, 'actor': sender.ap_profile_id,
            'object': post.ap_id}


def _undo(sender, undone, suffix='/undo'):
    return {'id': undone['id'] + suffix, 'type': 'Undo', 'actor': sender.ap_profile_id, 'object': undone}


def _last_log():
    return ActivityPubLog.query.order_by(ActivityPubLog.id.desc()).first()


def _member(community, user):
    return CommunityMember.query.filter_by(community_id=community.id, user_id=user.id).first()


# --- the defect: a reused id after an accepted Undo ---


def test_a_community_resubscribe_reusing_the_follow_id_is_accepted_again(app, inline, scene):
    community, _, _, carol, _ = scene
    follow = _follow(carol, community.ap_profile_id)
    _post(app, follow, carol)
    assert _member(community, carol) is not None
    _post(app, _undo(carol, follow), carol)
    assert _member(community, carol) is None

    _post(app, follow, carol)

    assert not (_last_log().exception_message or '').startswith(DUPLICATE)
    assert _member(community, carol) is not None
    accepts = [body for body in inline if body['type'] == 'Accept']
    assert len(accepts) == 2 and accepts[1]['object']['id'] == follow['id']


def test_a_user_refollow_reusing_the_follow_id_is_accepted_again(app, inline, scene):
    _, local_user, _, carol, _ = scene
    follow = _follow(carol, local_user.ap_profile_id)
    _post(app, follow, carol)
    _post(app, _undo(carol, follow), carol)
    assert UserFollower.query.filter_by(local_user_id=local_user.id, remote_user_id=carol.id).count() == 0

    _post(app, follow, carol)

    row = UserFollower.query.filter_by(local_user_id=local_user.id, remote_user_id=carol.id, is_inward=True).one()
    assert row.is_accepted is True
    assert [body['type'] for body in inline].count('Accept') == 2


def test_a_feed_resubscribe_reusing_the_follow_id_is_accepted_again(app, inline, scene):
    _, _, _, carol, _ = scene
    feed = make_feed(make_instance('feed.example'), 'peerfeed', public=True, local=True, with_keys=True)
    db.session.commit()
    follow = _follow(carol, feed.ap_profile_id)
    _post(app, follow, carol)
    _post(app, _undo(carol, follow), carol)
    assert FeedMember.query.filter_by(user_id=carol.id, feed_id=feed.id).count() == 0

    _post(app, follow, carol)

    assert FeedMember.query.filter_by(user_id=carol.id, feed_id=feed.id).count() == 1
    assert [body['type'] for body in inline].count('Accept') == 2


def test_an_ignored_undo_feed_follow_releases_nothing(app, inline, scene, redis_double):
    _, _, _, carol, _ = scene
    feed = make_feed(make_instance('feed.example'), 'peerfeed', public=True, local=True, with_keys=True)
    db.session.commit()
    follow = _follow(carol, feed.ap_profile_id)
    key = activity_dedupe_key(follow['id'], carol.ap_profile_id)
    redis_double.set(key, 1)

    _post(app, _undo(carol, follow), carol)

    assert _last_log().result == 'success'
    assert redis_double.exists(key) == 1


@pytest.mark.parametrize('kind', ['Like', 'Dislike'])
def test_a_revote_reusing_the_vote_id_is_recorded_again(app, inline, scene, kind):
    _, _, post, carol, _ = scene
    vote = _like(carol, post, kind)
    _post(app, vote, carol)
    assert PostVote.query.filter_by(user_id=carol.id, post_id=post.id).count() == 1
    _post(app, _undo(carol, vote), carol)
    assert PostVote.query.filter_by(user_id=carol.id, post_id=post.id).count() == 0

    _post(app, vote, carol)

    assert not (_last_log().exception_message or '').startswith(DUPLICATE)
    assert PostVote.query.filter_by(user_id=carol.id, post_id=post.id).count() == 1


# --- what must still be dropped ---


def test_a_replay_of_the_follow_with_no_undo_between_is_still_dropped(app, inline, scene):
    community, _, _, carol, _ = scene
    follow = _follow(carol, community.ap_profile_id)
    _post(app, follow, carol)

    _post(app, follow, carol)

    assert _last_log().exception_message.startswith(DUPLICATE)
    assert [body['type'] for body in inline].count('Accept') == 1


def test_an_undo_whose_inner_actor_differs_releases_nothing(app, inline, scene, redis_double):
    """mallory signs an Undo naming carol's Follow; mallory's own membership is what the handler removes, but carol's
    claim stays, so a replay of carol's Follow is still dropped."""
    community, _, _, carol, mallory = scene
    carols = _follow(carol, community.ap_profile_id)
    _post(app, carols, carol)
    _post(app, _follow(mallory, community.ap_profile_id), mallory)
    key = activity_dedupe_key(carols['id'], carol.ap_profile_id)

    _post(app, {'id': f'{mallory.ap_profile_id}/undo/1', 'type': 'Undo', 'actor': mallory.ap_profile_id,
                'object': carols}, mallory)

    assert _member(community, mallory) is None
    assert redis_double.exists(key) == 1
    _post(app, carols, carol)
    assert _last_log().exception_message.startswith(DUPLICATE)


def test_an_undo_naming_another_actors_follow_as_its_own_releases_nothing(app, inline, scene, redis_double):
    """The inner actor is the Undo's signer, but the undone id lives on another host: the key is scoped to the
    signer's host, so only an id on that host is released."""
    community, _, _, _, mallory = scene
    follow = _follow(mallory, community.ap_profile_id)
    _post(app, follow, mallory)
    foreign = dict(follow, id='https://elsewhere.example/follows/7')
    redis_double.set(activity_dedupe_key(foreign['id'], mallory.ap_profile_id), 1)

    _post(app, _undo(mallory, foreign), mallory)

    assert redis_double.exists(activity_dedupe_key(foreign['id'], mallory.ap_profile_id)) == 1


def test_an_ignored_undo_follow_releases_nothing(app, inline, scene, redis_double):
    """carol was never a member: the handler logs success but nothing was undone, so the claim stays."""
    community, _, _, carol, _ = scene
    follow = _follow(carol, community.ap_profile_id)
    key = activity_dedupe_key(follow['id'], carol.ap_profile_id)
    redis_double.set(key, 1)

    _post(app, _undo(carol, follow), carol)

    assert redis_double.exists(key) == 1


def test_an_ignored_undo_user_follow_releases_nothing(app, inline, scene, redis_double):
    _, local_user, _, carol, _ = scene
    follow = _follow(carol, local_user.ap_profile_id)
    key = activity_dedupe_key(follow['id'], carol.ap_profile_id)
    redis_double.set(key, 1)

    _post(app, _undo(carol, follow), carol)

    assert redis_double.exists(key) == 1


def test_an_undo_like_with_no_vote_to_take_back_releases_nothing(app, inline, scene, redis_double):
    _, _, post, carol, _ = scene
    vote = _like(carol, post)
    key = activity_dedupe_key(vote['id'], carol.ap_profile_id)
    redis_double.set(key, 1)

    _post(app, _undo(carol, vote), carol)

    assert redis_double.exists(key) == 1


def test_a_refused_undo_like_releases_nothing(app, inline, scene, redis_double):
    """The target is unknown: the Undo is logged as a failure and nothing is released."""
    _, _, _, carol, _ = scene
    vote = {'id': f'{carol.ap_profile_id}/likes/999', 'type': 'Like', 'actor': carol.ap_profile_id,
            'object': f"https://{current_app.config['SERVER_NAME']}/post/999999"}
    key = activity_dedupe_key(vote['id'], carol.ap_profile_id)
    redis_double.set(key, 1)

    _post(app, _undo(carol, vote), carol)

    assert _last_log().result == 'failure'
    assert redis_double.exists(key) == 1


def test_an_undo_of_a_never_seen_follow_is_processed_without_error(app, inline, scene, redis_double):
    community, _, _, carol, _ = scene
    follow = _follow(carol, community.ap_profile_id, n=404)
    _post(app, follow, carol)
    redis_double.delete(activity_dedupe_key(follow['id'], carol.ap_profile_id))   # as if the window had passed

    _post(app, _undo(carol, follow), carol)

    assert _last_log().result == 'success'
    assert _member(community, carol) is None


def test_the_undos_own_claim_is_kept(app, inline, scene, redis_double):
    community, _, _, carol, _ = scene
    follow = _follow(carol, community.ap_profile_id)
    undo = _undo(carol, follow)
    _post(app, follow, carol)
    _post(app, undo, carol)

    assert redis_double.exists(activity_dedupe_key(undo['id'], carol.ap_profile_id)) == 1
    _post(app, undo, carol)
    assert _last_log().exception_message.startswith(DUPLICATE)


# --- release_undone_activity's own conditions ---


ACTOR = 'https://peer.example/u/carol'


@pytest.mark.parametrize('undo, released', [
    ({'id': f'{ACTOR}/undo', 'actor': ACTOR, 'object': {'id': f'{ACTOR}/follows/7', 'actor': ACTOR}}, True),
    ({'id': f'{ACTOR}/undo', 'actor': ACTOR, 'object': f'{ACTOR}/follows/7'}, False),   # no inner actor to compare
    ({'id': f'{ACTOR}/undo', 'actor': {'id': ACTOR}, 'object': {'id': f'{ACTOR}/follows/7', 'actor': ACTOR}}, False),
    ({'id': f'{ACTOR}/undo', 'actor': ACTOR, 'object': {'id': 7, 'actor': ACTOR}}, False),
    ({'id': f'{ACTOR}/undo', 'actor': ACTOR, 'object': {'id': f'{ACTOR}/follows/7'}}, False),
    ({'id': f'{ACTOR}/undo', 'actor': ACTOR, 'object': {'id': f'{ACTOR}/undo', 'actor': ACTOR}}, False),   # its own id
])
def test_release_undone_activity_conditions(app, redis_double, undo, released):
    key = activity_dedupe_key(f'{ACTOR}/follows/7', ACTOR)
    redis_double.set(key, 1)
    redis_double.set(activity_dedupe_key(f'{ACTOR}/undo', ACTOR), 1)

    release_undone_activity(undo, announced=False)

    assert redis_double.exists(key) == (0 if released else 1)
    assert redis_double.exists(activity_dedupe_key(f'{ACTOR}/undo', ACTOR)) == 1


def test_an_announced_undo_releases_nothing(app, redis_double):
    """An Undo inside an Announce was verified as the announcer's, not as its own actor's."""
    key = activity_dedupe_key(f'{ACTOR}/follows/7', ACTOR)
    redis_double.set(key, 1)

    release_undone_activity({'id': f'{ACTOR}/undo', 'actor': ACTOR,
                             'object': {'id': f'{ACTOR}/follows/7', 'actor': ACTOR}}, announced=True)

    assert redis_double.exists(key) == 1
