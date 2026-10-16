"""R-UNDORELEASE (spec §6.4, amendment A14): an accepted Undo releases the dedupe claim of the activity it undoes.

PeerTube reuses activity ids: a re-subscribe after an unsubscribe sends a Follow with the first one's id, and a
like after an Undo(Like) reuses `.../likes/<videoId>`. The inbox remembers every verified id for
ACTIVITY_DEDUPE_SECONDS, so the second activity was dropped as already seen: the resubscription stayed pending
forever and the re-like was lost.

Each test here goes through the real /inbox route with a real HTTP signature (DEBUG makes the dispatch inline),
because the claim is taken by the route and released by the dispatcher: neither half alone shows the defect.
"""
import base64
import contextlib
import json
import time
from email.utils import formatdate

import pytest
from flask import current_app

from app import db
from app.activitypub import routes as activitypub_routes
from app.activitypub.signature import RsaKeys
from app.activitypub.util import DEFERRED_HEADERS, DEFERRED_MAX_BYTES, DEFERRED_PER_HOST, DEFERRED_SECONDS, \
    activity_accepted_key, activity_dedupe_key, deferred_count_key, deferred_key, mark_accepted, \
    current_signed_at, parked_request, release_undone_activity, undo_of_key
from app.models import ActivityPubLog, CommunityMember, FeedMember, PostVote, UserFollower, utcnow
from app.activitypub import signature_rfc9421
from app.activitypub.signature import http_date_seconds
from tests.conftest import ld_signed_body, unsigned_but_precheck_clean_headers
from tests.factories import cavage_signed_headers, make_community, make_instance_block, make_feed, make_instance, make_post, make_site, make_user, signed_inbox_post

DUPLICATE = 'Already aware of this activity'   # the start of both messages: before and after the signature check


@pytest.fixture
def inline(app, monkeypatch, redis_double):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    monkeypatch.setitem(app.config, 'DEBUG', True)   # signing to test.piefed.local needs DEBUG; dispatch is inline
    # fakeredis has no Lua, so a lock cannot be released (tests/conftest.py redis_double's CAVEAT)
    monkeypatch.setattr(redis_double, 'lock', lambda *a, **kw: contextlib.nullcontext())
    # Each signed request is one second later than the one before, so a resent activity carries a newer Date, as
    # it does from PeerTube; a test that needs a given Date passes `at` to _post
    CLOCK['next'] = int(time.time()) - 1800
    monkeypatch.setattr('app.activitypub.signature.http_date', _clock_date)
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


CLOCK = {'next': 0}


def _clock_date():
    CLOCK['next'] += 1
    return formatdate(CLOCK['next'], usegmt=True)


def _post(app, activity, sender, at=None):
    if at is not None:
        CLOCK['next'] = at - 1
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


def _race(app, sender, activity, undo):
    """PeerTube's race: `activity` accepted, then the resent copy signed AFTER `undo` but landing before it."""
    now = int(time.time())
    _post(app, activity, sender, at=now - 900)
    _post(app, activity, sender, at=now - 100)
    _post(app, undo, sender, at=now - 200)


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
FOLLOW_ID = f'{ACTOR}/follows/7'
FOLLOW = ('Follow',)


def _accepted(redis_double, activity_id=FOLLOW_ID, kind='Follow', actor=ACTOR):
    """As if PieFed had accepted `activity_id` as a `kind` from `actor`: its claim and its acceptance marker."""
    redis_double.set(activity_dedupe_key(activity_id, actor), 1)
    mark_accepted({'id': activity_id, 'type': kind, 'actor': actor}, announced=False)


@pytest.mark.parametrize('undo, released', [
    ({'id': f'{ACTOR}/undo', 'actor': ACTOR, 'object': {'id': FOLLOW_ID, 'type': 'Follow', 'actor': ACTOR}}, True),
    ({'id': f'{ACTOR}/undo', 'actor': ACTOR, 'object': FOLLOW_ID}, False),   # no inner actor to compare
    ({'id': f'{ACTOR}/undo', 'actor': {'id': ACTOR}, 'object': {'id': FOLLOW_ID, 'type': 'Follow', 'actor': ACTOR}},
     False),
    ({'id': f'{ACTOR}/undo', 'actor': ACTOR, 'object': {'id': 7, 'type': 'Follow', 'actor': ACTOR}}, False),
    ({'id': f'{ACTOR}/undo', 'actor': ACTOR, 'object': {'id': FOLLOW_ID, 'type': 'Follow'}}, False),
    ({'id': f'{ACTOR}/undo', 'actor': ACTOR, 'object': {'id': f'{ACTOR}/undo', 'type': 'Follow', 'actor': ACTOR}},
     False),   # its own id
    ({'id': f'{ACTOR}/undo', 'actor': ACTOR, 'object': {'id': FOLLOW_ID, 'type': 'Like', 'actor': ACTOR}},
     False),   # not the type the arm undid
])
def test_release_undone_activity_conditions(app, redis_double, undo, released):
    _accepted(redis_double)
    redis_double.set(activity_dedupe_key(f'{ACTOR}/undo', ACTOR), 1)

    release_undone_activity(undo, announced=False, types=FOLLOW)

    assert redis_double.exists(activity_dedupe_key(FOLLOW_ID, ACTOR)) == (0 if released else 1)
    assert redis_double.exists(activity_dedupe_key(f'{ACTOR}/undo', ACTOR)) == 1


def test_an_announced_undo_releases_nothing(app, redis_double):
    """An Undo inside an Announce was verified as the announcer's, not as its own actor's."""
    _accepted(redis_double)

    release_undone_activity({'id': f'{ACTOR}/undo', 'actor': ACTOR,
                             'object': {'id': FOLLOW_ID, 'type': 'Follow', 'actor': ACTOR}}, announced=True,
                            types=FOLLOW)

    assert redis_double.exists(activity_dedupe_key(FOLLOW_ID, ACTOR)) == 1


UNDO_OF_FOLLOW = {'id': f'{ACTOR}/undo', 'actor': ACTOR, 'object': {'id': FOLLOW_ID, 'type': 'Follow', 'actor': ACTOR}}


def test_an_id_never_accepted_is_not_released(app, redis_double):
    redis_double.set(activity_dedupe_key(FOLLOW_ID, ACTOR), 1)   # claimed, but nothing was accepted under it

    release_undone_activity(UNDO_OF_FOLLOW, announced=False, types=FOLLOW)

    assert redis_double.exists(activity_dedupe_key(FOLLOW_ID, ACTOR)) == 1


@pytest.mark.parametrize('kind, actor', [('Create', ACTOR), ('Like', ACTOR), ('Follow', 'https://peer.example/u/mallory')])
def test_an_id_accepted_as_another_type_or_from_another_actor_is_not_released(app, redis_double, kind, actor):
    redis_double.set(activity_dedupe_key(FOLLOW_ID, ACTOR), 1)
    mark_accepted({'id': FOLLOW_ID, 'type': kind, 'actor': actor}, announced=False)

    release_undone_activity(UNDO_OF_FOLLOW, announced=False, types=FOLLOW)

    assert redis_double.exists(activity_dedupe_key(FOLLOW_ID, ACTOR)) == 1


def test_one_acceptance_allows_one_release(app, redis_double):
    """The marker is consumed: a second Undo naming the same, not re-accepted, id releases nothing."""
    _accepted(redis_double)
    release_undone_activity(UNDO_OF_FOLLOW, announced=False, types=FOLLOW)
    redis_double.set(activity_dedupe_key(FOLLOW_ID, ACTOR), 1)

    release_undone_activity(dict(UNDO_OF_FOLLOW, id=f'{ACTOR}/undo/2'), announced=False, types=FOLLOW)

    assert redis_double.exists(activity_dedupe_key(FOLLOW_ID, ACTOR)) == 1


@pytest.mark.parametrize('activity, marked', [
    ({'id': FOLLOW_ID, 'type': 'Follow', 'actor': ACTOR}, True),
    ({'id': FOLLOW_ID, 'type': 'Follow', 'actor': {'id': ACTOR}}, False),
])
def test_mark_accepted(app, redis_double, activity, marked):
    mark_accepted(activity, announced=False)
    mark_accepted(dict(activity, id=f'{ACTOR}/announced'), announced=True)

    assert redis_double.get(activity_accepted_key(FOLLOW_ID, ACTOR)) == ('Follow|' + ACTOR if marked else None)
    assert redis_double.exists(activity_accepted_key(f'{ACTOR}/announced', ACTOR)) == 0


def test_an_undo_naming_a_prior_create_releases_nothing(app, inline, scene, redis_double):
    """The security review's case: carol's Undo really removes her membership, but its embedded "Follow" carries
    the id of an earlier Create of hers. That Create's claim stays, so its replay is still dropped."""
    community, _, _, carol, _ = scene
    _post(app, _follow(carol, community.ap_profile_id), carol)
    create_id = f'{carol.ap_profile_id}/activities/create/1'
    redis_double.set(activity_dedupe_key(create_id, carol.ap_profile_id), 1)
    mark_accepted({'id': create_id, 'type': 'Create', 'actor': carol.ap_profile_id}, announced=False)
    disguised = {'id': create_id, 'type': 'Follow', 'actor': carol.ap_profile_id, 'object': community.ap_profile_id}

    _post(app, _undo(carol, disguised), carol)

    assert _member(community, carol) is None
    assert redis_double.exists(activity_dedupe_key(create_id, carol.ap_profile_id)) == 1
    _post(app, {'id': create_id, 'type': 'Create', 'actor': carol.ap_profile_id,
                'object': {'id': f'{carol.ap_profile_id}/notes/1', 'type': 'Note'}}, carol)
    assert _last_log().exception_message.startswith(DUPLICATE)


# --- gap 1: PeerTube reuses the Undo id too (`<id>/undo`), so every cycle repeats both ids ---


def test_three_community_follow_cycles_reusing_both_ids_all_take_effect(app, inline, scene):
    community, _, _, carol, _ = scene
    follow = _follow(carol, community.ap_profile_id)
    undo = _undo(carol, follow)
    for _cycle in range(3):
        _post(app, follow, carol)
        assert _member(community, carol) is not None
        _post(app, undo, carol)
        assert _member(community, carol) is None
    assert [body['type'] for body in inline].count('Accept') == 3


def test_three_user_follow_cycles_reusing_both_ids_all_take_effect(app, inline, scene):
    _, local_user, _, carol, _ = scene
    follow = _follow(carol, local_user.ap_profile_id)
    undo = _undo(carol, follow)
    for _cycle in range(3):
        _post(app, follow, carol)
        assert UserFollower.query.filter_by(local_user_id=local_user.id, remote_user_id=carol.id).count() == 1
        _post(app, undo, carol)
        assert UserFollower.query.filter_by(local_user_id=local_user.id, remote_user_id=carol.id).count() == 0


def test_three_like_cycles_reusing_both_ids_all_take_effect(app, inline, scene):
    _, _, post, carol, _ = scene
    like = _like(carol, post)
    undo = _undo(carol, like)
    for _cycle in range(3):
        _post(app, like, carol)
        assert PostVote.query.filter_by(user_id=carol.id, post_id=post.id).count() == 1
        _post(app, undo, carol)
        assert PostVote.query.filter_by(user_id=carol.id, post_id=post.id).count() == 0


def test_a_replayed_undo_with_no_follow_between_is_still_dropped(app, inline, scene, redis_double):
    community, _, _, carol, _ = scene
    follow = _follow(carol, community.ap_profile_id)
    undo = _undo(carol, follow)
    _post(app, follow, carol)
    _post(app, undo, carol)

    _post(app, undo, carol)

    assert _last_log().exception_message.startswith(DUPLICATE)
    assert redis_double.exists(activity_dedupe_key(undo['id'], carol.ap_profile_id)) == 1


def test_a_follow_that_was_never_undone_releases_no_undo(app, redis_double):
    """mark_accepted releases an Undo's claim only when one was recorded against the activity, for the same actor."""
    redis_double.set(activity_dedupe_key(f'{ACTOR}/undo', ACTOR), 1)
    redis_double.set(undo_of_key(FOLLOW_ID, ACTOR), f'{ACTOR}/undo|https://peer.example/u/mallory')

    mark_accepted({'id': FOLLOW_ID, 'type': 'Follow', 'actor': ACTOR}, announced=False)
    mark_accepted({'id': f'{ACTOR}/follows/8', 'type': 'Follow', 'actor': ACTOR}, announced=False)

    assert redis_double.exists(activity_dedupe_key(f'{ACTOR}/undo', ACTOR)) == 1


# --- gap 4: PeerTube sends Undo(X) and the new X at once; a reused-id X landing first is parked, not lost ---


def test_a_follow_reusing_the_id_that_lands_before_its_undo_takes_effect_after_it(app, inline, scene, redis_double):
    community, _, _, carol, _ = scene
    follow = _follow(carol, community.ap_profile_id)
    now = int(time.time())
    _post(app, follow, carol, at=now - 900)
    _post(app, follow, carol, at=now - 100)   # the resubscription, ahead of the Undo: a duplicate, parked
    assert _last_log().exception_message.startswith(DUPLICATE)
    assert redis_double.exists(deferred_key(follow['id'], carol.ap_profile_id)) == 1

    _post(app, _undo(carol, follow), carol, at=now - 200)   # signed before the resubscription, landing after it

    assert _member(community, carol) is not None
    assert [body['type'] for body in inline].count('Accept') == 2
    assert redis_double.exists(deferred_key(follow['id'], carol.ap_profile_id)) == 0


def test_a_like_reusing_the_id_that_lands_before_its_undo_takes_effect_after_it(app, inline, scene):
    _, _, post, carol, _ = scene
    like = _like(carol, post)

    _race(app, carol, like, _undo(carol, like))

    assert PostVote.query.filter_by(user_id=carol.id, post_id=post.id).count() == 1


def test_a_duplicate_from_another_actor_than_the_accepted_one_is_not_parked(app, inline, scene, redis_double):
    """mallory, on carol's host, sends a verified Follow under carol's follow id: the id was accepted from carol, so
    mallory's duplicate is dropped, not parked, and carol's Undo runs nothing of mallory's."""
    community, _, _, carol, mallory = scene
    follow = _follow(carol, community.ap_profile_id)
    _post(app, follow, carol)
    _post(app, dict(follow, actor=mallory.ap_profile_id), mallory)
    assert _last_log().exception_message.startswith(DUPLICATE)
    assert redis_double.exists(deferred_key(follow['id'], carol.ap_profile_id)) == 0

    _post(app, _undo(carol, follow), carol)

    assert _member(community, mallory) is None and _member(community, carol) is None


def test_a_duplicate_of_an_id_never_accepted_is_not_parked(app, inline, scene, redis_double):
    """A claim with no acceptance marker (a Follow that was refused, say) parks nothing."""
    community, _, _, carol, _ = scene
    follow = _follow(carol, community.ap_profile_id)
    redis_double.set(activity_dedupe_key(follow['id'], carol.ap_profile_id), 1)

    _post(app, follow, carol)

    assert _last_log().exception_message.startswith(DUPLICATE)
    assert redis_double.exists(deferred_key(follow['id'], carol.ap_profile_id)) == 0


def test_a_host_parks_at_most_fifty_bodies_in_the_window(app, inline, scene, redis_double):
    community, _, _, carol, _ = scene
    follow = _follow(carol, community.ap_profile_id)
    _post(app, follow, carol)
    redis_double.set(deferred_count_key(carol.ap_profile_id), DEFERRED_PER_HOST - 1, ex=DEFERRED_SECONDS)

    _post(app, follow, carol)   # the 50th
    assert redis_double.exists(deferred_key(follow['id'], carol.ap_profile_id)) == 1
    redis_double.delete(deferred_key(follow['id'], carol.ap_profile_id))

    _post(app, follow, carol)   # the 51st
    assert redis_double.exists(deferred_key(follow['id'], carol.ap_profile_id)) == 0
    assert 50 < redis_double.ttl(deferred_count_key(carol.ap_profile_id)) <= 60


def test_the_parking_limits(app):
    assert DEFERRED_MAX_BYTES == 8 * 1024
    assert DEFERRED_PER_HOST == 50


def test_a_parked_body_holds_only_what_the_rerun_needs(app, inline, scene, redis_double):
    community, _, _, carol, _ = scene
    follow = _follow(carol, community.ap_profile_id)
    _post(app, follow, carol)
    _post(app, follow, carol)

    parked = json.loads(redis_double.get(deferred_key(follow['id'], carol.ap_profile_id)))
    assert set(parked) == {'path', 'headers', 'body', 'signed_at'}
    assert set(parked['headers']) <= set(DEFERRED_HEADERS)
    assert json.loads(base64.b64decode(parked['body'])) == follow


def test_a_parked_follow_failing_its_signature_on_the_rerun_is_discarded(app, inline, scene, redis_double):
    community, _, _, carol, _ = scene
    follow = _follow(carol, community.ap_profile_id)
    now = int(time.time())
    _post(app, follow, carol, at=now - 900)
    _post(app, follow, carol, at=now - 100)
    key = deferred_key(follow['id'], carol.ap_profile_id)
    parked = json.loads(redis_double.get(key))
    parked['headers']['Signature'] = parked['headers']['Signature'].replace('signature="', 'signature="AAAA')
    redis_double.set(key, json.dumps(parked), keepttl=True)

    _post(app, _undo(carol, follow), carol, at=now - 200)

    assert _member(community, carol) is None
    assert ActivityPubLog.query.filter(ActivityPubLog.exception_message.startswith('Could not verify')).count() == 1
    assert redis_double.exists(key) == 0


def test_a_parked_body_lives_a_minute(app, inline, scene, redis_double):
    community, _, _, carol, _ = scene
    follow = _follow(carol, community.ap_profile_id)
    _post(app, follow, carol)
    _post(app, follow, carol)

    assert DEFERRED_SECONDS == 60
    assert 50 < redis_double.ttl(deferred_key(follow['id'], carol.ap_profile_id)) <= 60


def test_an_expired_parked_body_is_gone_and_the_undo_still_applies(app, inline, scene, redis_double):
    community, _, _, carol, _ = scene
    follow = _follow(carol, community.ap_profile_id)
    _post(app, follow, carol)
    _post(app, follow, carol)
    redis_double.delete(deferred_key(follow['id'], carol.ap_profile_id))   # as at the end of its TTL

    _post(app, _undo(carol, follow), carol)

    assert _member(community, carol) is None
    assert [body['type'] for body in inline].count('Accept') == 1


def test_an_unverified_duplicate_is_never_parked(app, inline, scene, redis_double):
    """Only a body whose signature verified is parked: a forgery cannot take the genuine resubscription's place."""
    community, _, _, carol, _ = scene
    follow = _follow(carol, community.ap_profile_id)
    _post(app, follow, carol)
    body = json.dumps(follow).encode()
    with app.test_client() as client:
        forged = client.post('/inbox', data=body, content_type='application/activity+json',
                             headers={'Host': app.config['SERVER_NAME'], **unsigned_but_precheck_clean_headers(body)})
    assert forged.status_code == 400
    assert redis_double.exists(deferred_key(follow['id'], carol.ap_profile_id)) == 0


def test_a_duplicate_of_another_type_is_still_dropped_before_any_signature_check(app, inline, scene, redis_double):
    _, _, _, carol, _ = scene
    create = {'id': f'{carol.ap_profile_id}/activities/create/9', 'type': 'Create', 'actor': carol.ap_profile_id,
              'object': {'id': f'{carol.ap_profile_id}/notes/9', 'type': 'Note'}}
    redis_double.set(activity_dedupe_key(create['id'], carol.ap_profile_id), 1)

    _post(app, create, carol)

    assert _last_log().exception_message == 'Already aware of this activity (before any signature check)'


def test_a_duplicate_over_8_kib_is_not_parked(app, inline, scene, redis_double):
    community, _, _, carol, _ = scene
    follow = dict(_follow(carol, community.ap_profile_id), summary='x' * (8 * 1024))
    _post(app, follow, carol)

    _post(app, follow, carol)

    assert redis_double.exists(deferred_key(follow['id'], carol.ap_profile_id)) == 0


@pytest.mark.parametrize('body', [
    {'id': FOLLOW_ID, 'type': 'Like', 'actor': ACTOR},     # another type
    {'id': f'{ACTOR}/follows/8', 'type': 'Follow', 'actor': ACTOR},   # another id
    {'id': FOLLOW_ID, 'type': 'Follow', 'actor': 'https://peer.example/u/mallory'},   # another actor
])
def test_a_parked_body_not_matching_the_undone_activity_is_discarded(app, redis_double, body):
    _accepted(redis_double)
    redis_double.set(deferred_key(FOLLOW_ID, ACTOR),
                     json.dumps({'path': '/inbox', 'headers': {}, 'body': base64.b64encode(json.dumps(body).encode()).decode()}))

    assert release_undone_activity(UNDO_OF_FOLLOW, announced=False, types=FOLLOW) is None
    assert redis_double.exists(deferred_key(FOLLOW_ID, ACTOR)) == 0


# --- round 3, I1: only a newer, HTTP-signed copy is parked ---


def test_an_ld_only_duplicate_is_not_parked(app, inline, scene, redis_double, no_network_ld_signing):
    """Anyone holding an LD-signed copy of carol's Follow could deliver it; it must never be parked to reverse her
    next Undo."""
    community, _, _, carol, _ = scene
    follow = _follow(carol, community.ap_profile_id)
    _post(app, follow, carol)
    body = ld_signed_body(carol, id=follow['id'], activity_type='Follow', object_uri=community.ap_profile_id)
    with app.test_client() as client:
        response = client.post('/inbox', data=body, headers=unsigned_but_precheck_clean_headers(body),
                               content_type='application/activity+json')

    assert response.status_code == 200
    assert _last_log().exception_message.endswith(DUPLICATE)
    assert redis_double.exists(deferred_key(follow['id'], carol.ap_profile_id)) == 0


def test_a_retry_of_the_old_activity_signed_before_the_undo_is_discarded(app, inline, scene, redis_double):
    """A sender's retry of the old Follow is re-signed, but before the user's Undo: it is parked, then discarded, so
    it cannot reverse the Undo."""
    community, _, _, carol, _ = scene
    follow = _follow(carol, community.ap_profile_id)
    now = int(time.time())
    _post(app, follow, carol, at=now - 900)
    _post(app, follow, carol, at=now - 300)   # the retry
    assert redis_double.exists(deferred_key(follow['id'], carol.ap_profile_id)) == 1

    _post(app, _undo(carol, follow), carol, at=now - 200)

    assert _member(community, carol) is None
    assert [body['type'] for body in inline].count('Accept') == 1
    assert redis_double.exists(deferred_key(follow['id'], carol.ap_profile_id)) == 0


def test_a_duplicate_whose_date_the_signature_does_not_cover_is_not_parked(app, inline, scene, redis_double):
    community, _, _, carol, _ = scene
    follow = _follow(carol, community.ap_profile_id)
    _post(app, follow, carol)
    body = json.dumps(follow).encode()
    headers = cavage_signed_headers(carol.private_key, f'{carol.ap_profile_id}#main-key', body=body,
                                    signed=('(request-target)', 'host', 'digest'))
    with app.test_client() as client:
        response = client.post('/inbox', data=body, headers=headers, content_type='application/activity+json')

    assert response.status_code == 200
    assert _last_log().exception_message.startswith(DUPLICATE)
    assert redis_double.exists(deferred_key(follow['id'], carol.ap_profile_id)) == 0
    assert redis_double.exists(deferred_count_key(carol.ap_profile_id)) == 0   # nor counted


def test_an_rfc9421_duplicate_is_parked_with_its_created_time(app, inline, scene, redis_double):
    community, _, _, carol, _ = scene
    follow = _follow(carol, community.ap_profile_id)
    _post(app, follow, carol)
    created = int(time.time()) - 50
    body = json.dumps(follow).encode()
    host = current_app.config['SERVER_NAME']
    headers = signature_rfc9421.sign('post', f'https://{host}/inbox', body, carol.private_key,
                                     f'{carol.ap_profile_id}#main-key', created=created)
    headers['Host'] = host
    with app.test_client() as client:
        client.post('/inbox', data=body, headers=headers, content_type='application/activity+json')

    assert json.loads(redis_double.get(deferred_key(follow['id'], carol.ap_profile_id)))['signed_at'] == created


@pytest.mark.parametrize('parked_at, undo_at, rerun', [(10, 5, True), (5, 5, True), (4, 5, False),
                                                       (None, 5, False), (10, None, False)])
def test_a_parked_request_runs_only_if_signed_no_earlier_than_the_undo(app, redis_double, parked_at, undo_at, rerun):
    _accepted(redis_double)
    parked = {'path': '/inbox', 'headers': {}, 'signed_at': parked_at,
              'body': base64.b64encode(json.dumps({'id': FOLLOW_ID, 'type': 'Follow', 'actor': ACTOR}).encode()).decode()}
    redis_double.set(deferred_key(FOLLOW_ID, ACTOR), json.dumps(parked))
    token = current_signed_at.set(undo_at)
    try:
        result = release_undone_activity(UNDO_OF_FOLLOW, announced=False, types=FOLLOW)
    finally:
        current_signed_at.reset(token)

    assert (result == parked) if rerun else (result is None)


@pytest.mark.parametrize('value, expected', [('Thu, 15 Oct 2026 12:00:00 GMT', 1792065600), ('not a date', None),
                                             (None, None)])
def test_http_date_seconds(value, expected):
    assert http_date_seconds(value) == expected


# --- round 3, I2: the per-host cap is a fixed window ---


def test_a_park_does_not_extend_the_hosts_window(app, inline, scene, redis_double):
    """A host that keeps parking must not keep its counter alive: the window ends on time while traffic goes on."""
    community, _, _, carol, _ = scene
    follow = _follow(carol, community.ap_profile_id)
    _post(app, follow, carol)
    counter = deferred_count_key(carol.ap_profile_id)
    redis_double.set(counter, 1, ex=5)

    _post(app, follow, carol)

    assert redis_double.get(counter) == '2'
    assert redis_double.ttl(counter) <= 5


def test_the_hosts_window_starts_again_after_it_ends(app, inline, scene, redis_double):
    community, _, _, carol, _ = scene
    follow = _follow(carol, community.ap_profile_id)
    _post(app, follow, carol)
    counter = deferred_count_key(carol.ap_profile_id)
    redis_double.set(counter, DEFERRED_PER_HOST, ex=5)
    _post(app, follow, carol)
    assert redis_double.exists(deferred_key(follow['id'], carol.ap_profile_id)) == 0   # the window is full
    redis_double.delete(counter)   # as when the window's TTL runs out

    _post(app, follow, carol)

    assert redis_double.exists(deferred_key(follow['id'], carol.ap_profile_id)) == 1
    assert redis_double.get(counter) == '1'
    assert 50 < redis_double.ttl(counter) <= 60


# --- round 3, M1: check-then-delete is decided by the delete ---


def _parked_follow(redis_double, signed_at=10):
    parked = {'path': '/inbox', 'headers': {}, 'signed_at': signed_at,
              'body': base64.b64encode(json.dumps({'id': FOLLOW_ID, 'type': 'Follow', 'actor': ACTOR}).encode()).decode()}
    redis_double.set(deferred_key(FOLLOW_ID, ACTOR), json.dumps(parked))
    return parked


def test_two_concurrent_releases_run_the_parked_request_once(app, redis_double, monkeypatch):
    """The second release read the marker and the parked body before the first deleted them: only the release whose
    delete removed them acts."""
    _accepted(redis_double)
    _parked_follow(redis_double)
    stale = {key: redis_double.get(key) for key in redis_double.keys()}
    reruns = []
    monkeypatch.setattr(activitypub_routes.rerun_parked_request, 'delay', reruns.append)
    token = current_signed_at.set(5)
    try:
        activitypub_routes._release_undone(UNDO_OF_FOLLOW, False, FOLLOW)
        monkeypatch.setattr(redis_double, 'get', stale.get)
        activitypub_routes._release_undone(UNDO_OF_FOLLOW, False, FOLLOW)
    finally:
        current_signed_at.reset(token)

    assert len(reruns) == 1


def test_a_parked_request_already_taken_is_not_returned_again(app, redis_double, monkeypatch):
    parked = _parked_follow(redis_double)
    stale = redis_double.get(deferred_key(FOLLOW_ID, ACTOR))
    assert parked_request(FOLLOW_ID, ACTOR, FOLLOW, 5) == parked
    monkeypatch.setattr(redis_double, 'get', lambda key: stale)

    assert parked_request(FOLLOW_ID, ACTOR, FOLLOW, 5) is None


def test_a_recorded_undo_already_taken_is_not_released_again(app, redis_double, monkeypatch):
    redis_double.set(undo_of_key(FOLLOW_ID, ACTOR), f'{ACTOR}/undo|{ACTOR}')
    stale = redis_double.get(undo_of_key(FOLLOW_ID, ACTOR))
    mark_accepted({'id': FOLLOW_ID, 'type': 'Follow', 'actor': ACTOR}, announced=False)
    redis_double.set(activity_dedupe_key(f'{ACTOR}/undo', ACTOR), 1)   # claimed again since
    monkeypatch.setattr(redis_double, 'get', lambda key: stale if key == undo_of_key(FOLLOW_ID, ACTOR) else None)

    mark_accepted({'id': FOLLOW_ID, 'type': 'Follow', 'actor': ACTOR}, announced=False)

    assert redis_double.exists(activity_dedupe_key(f'{ACTOR}/undo', ACTOR)) == 1


# --- round 3, M3: a vote is marked accepted only when it made or changed a vote ---


def test_a_like_that_takes_a_standing_vote_back_is_not_marked(app, inline, scene, redis_double):
    """Post.vote toggles: a second upvote, under another id, removes the first. Nothing stands, so nothing is marked."""
    _, _, post, carol, _ = scene
    _post(app, _like(carol, post), carol)
    second = dict(_like(carol, post), id=f'{carol.ap_profile_id}/likes/second')

    _post(app, second, carol)

    assert PostVote.query.filter_by(user_id=carol.id, post_id=post.id).count() == 0
    assert redis_double.exists(activity_accepted_key(second['id'], carol.ap_profile_id)) == 0


def test_a_dislike_refused_by_post_vote_is_not_marked(app, inline, scene, redis_double):
    """The author blocked carol's instance: Post.vote does nothing for a downvote, and nothing is marked."""
    _, _, post, carol, _ = scene
    make_instance_block(post.author, carol.instance)
    dislike = _like(carol, post, 'Dislike')

    _post(app, dislike, carol)

    assert PostVote.query.filter_by(user_id=carol.id, post_id=post.id).count() == 0
    assert redis_double.exists(activity_accepted_key(dislike['id'], carol.ap_profile_id)) == 0


def test_a_like_that_turns_a_downvote_up_is_marked(app, inline, scene, redis_double):
    _, _, post, carol, _ = scene
    _post(app, _like(carol, post, 'Dislike'), carol)
    like = dict(_like(carol, post), id=f'{carol.ap_profile_id}/likes/up')

    _post(app, like, carol)

    assert redis_double.get(activity_accepted_key(like['id'], carol.ap_profile_id)) == f'Like|{carol.ap_profile_id}'


# --- round 3, M4: the cap covers everything stored ---


def test_a_duplicate_whose_stored_blob_exceeds_8_kib_is_not_parked(app, inline, scene, redis_double):
    """A 6 KB body is under the cap alone, but base64 and the headers make what would be stored larger than 8 KiB."""
    community, _, _, carol, _ = scene
    follow = dict(_follow(carol, community.ap_profile_id), summary='x' * 6000)
    assert len(json.dumps(follow)) < DEFERRED_MAX_BYTES
    _post(app, follow, carol)

    _post(app, follow, carol)

    assert redis_double.exists(deferred_key(follow['id'], carol.ap_profile_id)) == 0
    assert redis_double.exists(deferred_count_key(carol.ap_profile_id)) == 0


def test_every_stored_blob_is_within_the_cap(app, inline, scene, redis_double):
    community, _, _, carol, _ = scene
    follow = _follow(carol, community.ap_profile_id)
    _post(app, follow, carol)
    _post(app, follow, carol)

    assert len(redis_double.get(deferred_key(follow['id'], carol.ap_profile_id))) <= DEFERRED_MAX_BYTES


# --- round 3, M6: what a re-run answers is logged; it does not touch the sender's address ---


def _parked_for_rerun(app, carol, follow, redis_double):
    now = int(time.time())
    _post(app, follow, carol, at=now - 900)
    _post(app, follow, carol, at=now - 100)
    return json.loads(redis_double.get(deferred_key(follow['id'], carol.ap_profile_id)))


def test_a_rerun_answered_with_other_than_success_is_logged(app, inline, scene, redis_double):
    community, _, _, carol, _ = scene
    follow = _follow(carol, community.ap_profile_id)
    parked = _parked_for_rerun(app, carol, follow, redis_double)
    redis_double.set('pause_federation', '1')

    activitypub_routes.rerun_parked_request(parked)

    row = _last_log()
    assert (row.activity_id, row.result) == (follow['id'], 'failure')
    assert row.exception_message == 'Parked request re-run answered 429'


def test_a_rerun_does_not_touch_the_instances_ip_address(app, inline, scene, redis_double):
    community, _, _, carol, _ = scene
    follow = _follow(carol, community.ap_profile_id)
    parked = _parked_for_rerun(app, carol, follow, redis_double)
    instance = db.session.get(type(carol.instance), carol.instance_id)
    instance.ip_address = '203.0.113.9'
    db.session.commit()
    redis_double.delete(activity_dedupe_key(follow['id'], carol.ap_profile_id))   # as when an Undo released it

    activitypub_routes.rerun_parked_request(parked)

    db.session.expire_all()
    assert db.session.get(type(carol.instance), carol.instance_id).ip_address == '203.0.113.9'
    assert not (_last_log().exception_message or '').startswith('Parked request')


# --- round 3, M2: EmojiReact, the third vote type an Undo arm undoes ---


def _react(sender, post, emoji='🎉'):
    return {'id': f'{sender.ap_profile_id}/reactions/{post.id}', 'type': 'EmojiReact', 'actor': sender.ap_profile_id,
            'object': post.ap_id, 'content': emoji}


def _reaction(post, user):
    vote = PostVote.query.filter_by(user_id=user.id, post_id=post.id).first()
    return vote.emoji if vote else None


def test_three_emoji_react_cycles_reusing_both_ids_all_take_effect(app, inline, scene):
    _, _, post, carol, _ = scene
    react = _react(carol, post)
    undo = _undo(carol, react)
    for _cycle in range(3):
        _post(app, react, carol)
        assert _reaction(post, carol) == '🎉'
        _post(app, undo, carol)
        assert _reaction(post, carol) is None


def test_an_emoji_react_reusing_the_id_that_lands_before_its_undo_takes_effect_after_it(app, inline, scene,
                                                                                        redis_double):
    _, _, post, carol, _ = scene
    react = _react(carol, post)

    _race(app, carol, react, _undo(carol, react))

    assert _reaction(post, carol) == '🎉'
    assert redis_double.exists(deferred_key(react['id'], carol.ap_profile_id)) == 0


class _RecordedTask:
    """rerun_parked_request as _release_undone sees it: called in line, or queued with .delay."""

    def __init__(self):
        self.calls = []

    def __call__(self, parked):
        self.calls.append(('direct', parked))

    def delay(self, parked):
        self.calls.append(('delay', parked))


@pytest.mark.parametrize('debug, how', [(True, 'direct'), (False, 'delay')])
def test_the_parked_request_is_run_in_line_in_debug_and_queued_otherwise(app, redis_double, monkeypatch, debug, how):
    """The pattern of the inbox's other tasks: a debug server has no worker, so it runs the re-run itself."""
    _accepted(redis_double)
    parked = _parked_follow(redis_double)
    task = _RecordedTask()
    monkeypatch.setattr(activitypub_routes, 'rerun_parked_request', task)
    monkeypatch.setattr(app, 'debug', debug)
    token = current_signed_at.set(5)
    try:
        activitypub_routes._release_undone(UNDO_OF_FOLLOW, False, FOLLOW)
    finally:
        current_signed_at.reset(token)

    assert task.calls == [(how, parked)]
