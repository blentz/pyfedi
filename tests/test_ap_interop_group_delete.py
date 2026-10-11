"""Interop C1 item 10 (docs/superpowers/specs/2026-10-11-ap-interop-c1-design.md): Delete of a remote Group.

A Delete of a remote Group is accepted from the Group itself, a known moderator of it, or an admin on the Group's
host (spec Amendment A; Lemmy sends community deletions with a moderator as the actor, Announced by the Group).
Whoever sends it, it is applied only once the Group's origin confirms it with a 410 or a Tombstone. A 404, a Group still served, or an unreachable origin is not proof, and nothing changes.
On a verified Delete the Group gets `ap_deleted_at`, its LOCAL members are unsubscribed without an Undo Follow,
and it becomes read-only: no post, reply, vote or join through the web, the API or the inbox, and nothing is
delivered to it. Its content stays readable. A Delete of a Group by anyone else is refused. There is no
undelete: anything later from a gone Group is ignored and logged.
"""
import json
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from flask import g

import app.activitypub.util as activitypub_util
from app import db
from app.activitypub.routes import process_new_content, process_upvote
from app.activitypub.util import GROUP_DELETED_MESSAGE, delete_remote_group, mark_community_gone, may_delete_group
from app.community.routes import do_subscribe
from app.constants import POST_TYPE_ARTICLE, SRC_API, SRC_WEB
from app.models import (ActivityPubLog, Community, CommunityJoinRequest, CommunityMember, InstanceRole, Language,
                        Post, PostReply, PostVote, utcnow)
from app.shared.post import make_post as shared_make_post, vote_for_post
from app.shared.reply import make_reply
from app.shared.tasks.deletes import delete_post
from app.utils import (can_create_post, can_create_post_reply, can_downvote, can_upvote, community_membership,
                       joined_communities, moderating_communities)
from tests.conftest import unsigned_but_precheck_clean_headers
from tests.factories import (a_keypair, bearer, inbox_activity, make_community, make_community_member,
                             make_instance, make_post, make_site, make_user, resolvable_remote_author,
                             signed_inbox_post, web_ctx)
from tests.test_inbox_dispatch_preamble import dispatch
from tests.test_shared_post_edit import _Field, _api_input, _web_form
from tests.test_shared_tasks_deletes import PEER_INBOX as DELETE_INBOX
from tests.test_shared_tasks_deletes import _make_deliverable as _deliverable_for_delete
from tests.test_shared_tasks_deletes import _seed as _seed_for_delete
from tests.test_shared_tasks_send_post import PEER_INBOX as POST_INBOX
from tests.test_shared_tasks_send_post import _peer_example_resolves_without_a_resolver  # noqa: F401 -- autouse
from tests.test_shared_tasks_send_post import _remote_inbox as _post_remote_inbox
from tests.test_shared_tasks_send_post import _seed as _seed_for_post
from tests.test_shared_tasks_send_post import _send as _send_post
from tests.test_shared_tasks_send_reply import _make_deliverable as _deliverable_for_reply
from tests.test_shared_tasks_send_reply import _remote_inbox as _reply_remote_inbox
from tests.test_shared_tasks_send_reply import _seed as _seed_for_reply
from tests.test_shared_tasks_send_reply import _send as _send_reply

PEER = 'peer.example'
GROUP = f'https://{PEER}/c/memes'


def _world():
    """The local instance (id 1), a local owner holding user id 1 for make_community's hardcoded user_id, a remote
    Group on peer.example with one local member, one remote author and one of their posts in it.

    The member is keyed: can_create_post refuses a local user without a private key, and the refusal under test
    must be the Group's, not that one. ap_fetched_at is stamped on the Group (and resolvable_remote_author stamps
    the author) so find_actor_or_create_cached never schedules a refresh fetch."""
    site = make_site()
    local = make_instance('test.piefed.local', software='piefed')
    peer = make_instance(PEER)
    make_user(local, 'owner', local=True)
    community = make_community('memes', host=PEER)
    community.ap_id = f'memes@{PEER}'
    community.instance_id = peer.id
    community.ap_inbox_url = f'{GROUP}/inbox'
    community.ap_fetched_at = utcnow()
    community.subscriptions_count = 1
    db.session.commit()
    member = make_user(local, 'member', local=True, with_keys=True)
    make_community_member(member, community)
    alice = resolvable_remote_author(peer, 'alice')
    post = make_post(community, alice, f'https://{PEER}/post/1')
    g.site = site
    return SimpleNamespace(site=site, peer=peer, community=community, member=member, alice=alice, post=post)


def _gone(w):
    w.community.ap_deleted_at = utcnow()
    db.session.commit()


def _self_delete(w, obj=GROUP):
    activity = inbox_activity(w.community, activity_type='Delete', object_uri=GROUP)
    activity['object'] = obj
    return activity


# --- the Delete: verified at the origin, or nothing changes ---

@pytest.mark.parametrize('answer', [{'status_code': 410},
                                    {'status_code': 200, 'json': {'id': GROUP, 'type': 'Tombstone'}}],
                         ids=['410', 'tombstone'])
@pytest.mark.parametrize('obj', [GROUP, {'id': GROUP, 'type': 'Group'}], ids=['string-object', 'dict-object'])
def test_a_verified_self_delete_marks_the_group_and_unsubscribes_only_local_members(
        app, db_session, http_mock, monkeypatch, answer, obj):
    """Remote moderators' CommunityMember rows (read from the Group's moderators collection) are the Group's own
    history, not local subscriptions: they stay. Nothing is sent: no Undo Follow to a Group that is gone."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()
    remote_mod = make_user(w.peer, 'mod')
    make_community_member(remote_mod, w.community, is_moderator=True)
    db.session.add(CommunityJoinRequest(user_id=w.member.id, community_id=w.community.id))
    db.session.commit()
    community_id, member_id, mod_id = w.community.id, w.member.id, remote_mod.id
    http_mock.get(GROUP).respond(**answer)

    dispatch(_self_delete(w, obj))

    db.session.expire_all()
    community = db.session.get(Community, community_id)
    assert community.ap_deleted_at is not None
    assert community.subscriptions_count == 0
    assert db.session.query(CommunityMember).filter_by(community_id=community_id, user_id=member_id).count() == 0
    assert db.session.query(CommunityMember).filter_by(community_id=community_id, user_id=mod_id).count() == 1
    assert db.session.query(CommunityJoinRequest).filter_by(community_id=community_id).count() == 0
    assert db.session.query(Post).filter_by(community_id=community_id).count() == 1   # content is kept
    assert ActivityPubLog.query.filter_by(direction='in').one().result == 'success'
    assert ActivityPubLog.query.filter_by(direction='out').count() == 0


@pytest.mark.parametrize('answer,reason', [
    ({'status_code': 404}, 'Group Delete: origin 404 does not prove it deleted'),
    ({'status_code': 200, 'json': {'id': GROUP, 'type': 'Group'}}, 'Group Delete of an object that still exists'),
    ({'status_code': 500}, 'Group Delete: the object answers HTTP 500'),
], ids=['404', 'still-served', '500'])
def test_an_unproven_self_delete_changes_nothing(app, db_session, http_mock, monkeypatch, answer, reason):
    """Owner ruling: a 404 does not delete (as for private objects); only a 410 or a Tombstone does."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()
    community_id, member_id = w.community.id, w.member.id
    http_mock.get(GROUP).respond(**answer)

    dispatch(_self_delete(w))

    db.session.expire_all()
    assert db.session.get(Community, community_id).ap_deleted_at is None
    assert db.session.query(CommunityMember).filter_by(community_id=community_id, user_id=member_id).count() == 1
    log = ActivityPubLog.query.one()
    assert (log.result, log.exception_message) == ('failure', reason)


def test_an_unreachable_origin_is_not_proof(app, db_session, http_mock, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()
    community_id = w.community.id
    http_mock.get(GROUP).mock(side_effect=httpx.ConnectError('unreachable'))

    dispatch(_self_delete(w))

    db.session.expire_all()
    assert db.session.get(Community, community_id).ap_deleted_at is None
    log = ActivityPubLog.query.one()
    assert (log.result, log.exception_message) == ('failure', 'Group Delete: the object could not be fetched')


GROUP_DELETE_REFUSED = 'Delete of a Group is accepted only from the Group, its moderators or an admin of its host'


def _moderator(w, name='mod', instance=None):
    """A remote moderator of the Group, stored as the Group's moderators collection is stored here."""
    mod = resolvable_remote_author(instance or w.peer, name)
    make_community_member(mod, w.community, is_moderator=True)
    return mod


def _lemmy_delete(actor):
    """Lemmy's shape: the moderator's Delete of the community, Announced by the Group."""
    inner = {'id': f'{actor.ap_profile_id}/activities/delete/{uuid.uuid4().hex}', 'type': 'Delete',
             'actor': actor.ap_profile_id, 'object': GROUP,
             'to': ['https://www.w3.org/ns/activitystreams#Public'], 'cc': [GROUP]}
    return {'id': f'{GROUP}/activities/announce/{uuid.uuid4().hex}', 'type': 'Announce', 'actor': GROUP,
            'object': inner}


def _inbound_logs():
    return [(log.activity_type, log.result, log.exception_message)
            for log in ActivityPubLog.query.filter_by(direction='in').all()]


def test_a_delete_of_a_group_by_a_random_user_is_refused_without_a_fetch(app, db_session, monkeypatch):
    """No http_mock route: a fetch would fail the test. alice posts in the Group but neither moderates it nor
    administers its host."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()
    community_id, member_id = w.community.id, w.member.id

    dispatch(inbox_activity(w.alice, activity_type='Delete', object_uri=GROUP))

    db.session.expire_all()
    assert db.session.get(Community, community_id).ap_deleted_at is None
    assert db.session.query(CommunityMember).filter_by(community_id=community_id, user_id=member_id).count() == 1
    assert _inbound_logs() == [('Delete', 'failure', GROUP_DELETE_REFUSED)]


@pytest.mark.parametrize('answer', [{'status_code': 410},
                                    {'status_code': 200, 'json': {'id': GROUP, 'type': 'Tombstone'}}],
                         ids=['410', 'tombstone'])
def test_a_lemmy_moderator_delete_announced_by_the_group_is_verified_and_marks_it(
        app, db_session, http_mock, monkeypatch, answer):
    """Amendment A: Lemmy deletes a community with a moderator as the actor. The moderator's Delete, Announced
    by the Group, is accepted once the origin confirms it, exactly as the Group's own Delete is."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()
    mod = _moderator(w)
    community_id, member_id, mod_id = w.community.id, w.member.id, mod.id
    route = http_mock.get(GROUP).respond(**answer)

    dispatch(_lemmy_delete(mod))

    db.session.expire_all()
    assert route.call_count == 1
    assert db.session.get(Community, community_id).ap_deleted_at is not None
    assert db.session.query(CommunityMember).filter_by(community_id=community_id, user_id=member_id).count() == 0
    assert db.session.query(CommunityMember).filter_by(community_id=community_id, user_id=mod_id).count() == 1
    assert ('Delete', 'success', None) in _inbound_logs()


def test_a_moderator_delete_is_not_applied_while_the_origin_still_serves_the_group(
        app, db_session, http_mock, monkeypatch):
    """The origin refetch is required whoever sends the Delete: a moderator's word is not proof."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()
    mod = _moderator(w)
    community_id, member_id = w.community.id, w.member.id
    http_mock.get(GROUP).respond(200, json={'id': GROUP, 'type': 'Group'})

    dispatch(_lemmy_delete(mod))

    db.session.expire_all()
    assert db.session.get(Community, community_id).ap_deleted_at is None
    assert db.session.query(CommunityMember).filter_by(community_id=community_id, user_id=member_id).count() == 1
    assert ('Delete', 'failure', 'Group Delete of an object that still exists') in _inbound_logs()


def test_a_moderator_on_another_host_is_verified_not_deferred(app, db_session, http_mock, monkeypatch):
    """A cross-origin Announced Delete by a moderator is normally deferred for cross_origin_delete_delay
    (apply_delayed_delete), which only knows posts and comments and would drop a Group as 'superseded'.
    A Delete naming a stored Group skips the deferral and goes straight to the origin check."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()
    mod = _moderator(w, 'farmod', instance=make_instance('far.example'))
    community_id = w.community.id
    http_mock.get(GROUP).respond(410)

    dispatch(_lemmy_delete(mod))

    db.session.expire_all()
    assert db.session.get(Community, community_id).ap_deleted_at is not None


def test_an_admin_on_the_groups_host_may_delete_it(app, db_session, http_mock, monkeypatch):
    """The security track's Group-host admin rule (_may_remove_from): an InstanceRole admin of the Group's
    instance, sending the Delete directly."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()
    admin = resolvable_remote_author(w.peer, 'admin')
    db.session.add(InstanceRole(instance_id=w.peer.id, user_id=admin.id, role='admin'))
    db.session.commit()
    community_id = w.community.id
    http_mock.get(GROUP).respond(410)

    dispatch(inbox_activity(admin, activity_type='Delete', object_uri=GROUP))

    db.session.expire_all()
    assert db.session.get(Community, community_id).ap_deleted_at is not None
    assert _inbound_logs() == [('Delete', 'success', None)]


def test_an_admin_of_another_host_may_not_delete_the_group(app, db_session, monkeypatch):
    """Admin of far.example, not of the Group's host: refused, and nothing is fetched."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()
    far = make_instance('far.example')
    admin = resolvable_remote_author(far, 'admin')
    db.session.add(InstanceRole(instance_id=far.id, user_id=admin.id, role='admin'))
    db.session.commit()
    community_id = w.community.id

    dispatch(inbox_activity(admin, activity_type='Delete', object_uri=GROUP))

    db.session.expire_all()
    assert db.session.get(Community, community_id).ap_deleted_at is None
    assert _inbound_logs() == [('Delete', 'failure', GROUP_DELETE_REFUSED)]


def test_a_moderator_delete_of_a_group_already_gone_is_ignored_without_a_fetch(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()
    mod = _moderator(w)
    _gone(w)

    dispatch(inbox_activity(mod, activity_type='Delete', object_uri=GROUP))

    assert _inbound_logs() == [('Delete', 'ignored', GROUP_DELETED_MESSAGE)]


def test_may_delete_group(app, db_session):
    w = _world()
    mod = _moderator(w)
    assert may_delete_group(GROUP.upper(), w.community) is True     # the Group itself, any case
    assert may_delete_group(mod.ap_profile_id, w.community) is True
    assert may_delete_group(w.alice.ap_profile_id, w.community) is False
    assert may_delete_group('https://nobody.example/u/x', w.community) is False   # not stored: never fetched
    assert may_delete_group(None, w.community) is False


def test_a_local_community_is_never_marked_by_a_delete(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    make_site()
    make_instance('test.piefed.local', software='piefed')
    make_user(None, 'owner', local=True)
    local_community = make_community('home')

    delete_remote_group(local_community, 'https://peer.example/activities/delete/1', None)

    assert db.session.get(Community, local_community.id).ap_deleted_at is None
    log = ActivityPubLog.query.one()
    assert (log.result, log.exception_message) == ('failure', 'Delete of a local community is not accepted')


def test_marking_a_group_gone_clears_each_local_members_memoized_membership(app, db_session, monkeypatch):
    w = _world()
    cleared = []
    monkeypatch.setattr(activitypub_util.cache, 'delete_memoized', lambda fn, *args: cleared.append((fn, args)))

    mark_community_gone(w.community)

    assert (community_membership, (w.member, w.community)) in cleared
    assert (joined_communities, (w.member.id,)) in cleared
    assert (moderating_communities, (w.member.id,)) in cleared


# --- no undelete: a gone Group is not heard from again ---

@pytest.mark.parametrize('activity_type,obj', [
    ('Update', {'id': GROUP, 'type': 'Group', 'name': 'renamed'}),
    ('Delete', GROUP),
], ids=['update', 'second-delete'])
def test_a_gone_groups_update_or_repeated_delete_is_ignored_without_a_fetch(
        app, db_session, monkeypatch, activity_type, obj):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()
    _gone(w)
    activity = inbox_activity(w.community, activity_type=activity_type, object_uri=GROUP)
    activity['object'] = obj

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert (log.result, log.exception_message) == ('ignored', GROUP_DELETED_MESSAGE)


def test_a_gone_groups_announce_is_ignored(app, db_session, monkeypatch):
    """Announce is how a Group's Create arrives."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()
    _gone(w)

    dispatch(inbox_activity(w.community, activity_type='Announce', object_uri=f'https://{PEER}/activities/create/9'))

    log = ActivityPubLog.query.one()
    assert (log.result, log.exception_message) == ('ignored', GROUP_DELETED_MESSAGE)


def test_a_live_groups_accept_still_reaches_its_arm(app, db_session, monkeypatch):
    """Control for the Announce/Accept/Reject check above: a live Group passes it."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()
    activity = inbox_activity(w.community, activity_type='Accept', object_uri=GROUP)
    activity['object'] = {'id': f'https://{PEER}/follows/1'}

    dispatch(activity)

    assert ActivityPubLog.query.one().exception_message == 'Accept object is not an activity with a type'


def test_a_delete_from_a_live_group_naming_something_else_is_still_unexpected(app, db_session, monkeypatch):
    """Control for the self-Delete check: only a Delete naming the Group itself is one."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()

    dispatch(inbox_activity(w.community, activity_type='Delete', object_uri=f'https://{PEER}/post/1'))

    log = ActivityPubLog.query.one()
    assert (log.result, log.exception_message) == ('failure', 'Unexpected activity from Group')


# --- the inbox gate: a Group's self-Delete is not mistaken for an unknown account's ---

@pytest.fixture
def dispatched(app, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    monkeypatch.setitem(app.config, 'DEBUG', True)
    calls = []
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request', lambda *a, **kw: calls.append(a))
    return calls


def test_a_signed_self_delete_from_a_known_group_reaches_the_dispatcher(app, signing_peer, redis_double, dispatched):
    """shared_inbox used to look a self-Delete's actor up as a User only, so a Group's own Delete was logged
    'Does not exist here' and dropped before its signature was even checked."""
    group = make_community('memes', host=PEER)
    group.ap_id = f'memes@{PEER}'
    group.ap_fetched_at = utcnow()
    group.private_key, group.public_key = a_keypair()
    db.session.commit()
    activity = inbox_activity(group, activity_type='Delete', object_uri=group.ap_profile_id)

    with app.test_client() as client:
        response = signed_inbox_post(client, activity, group)

    assert response.status_code == 200
    assert [call[0]['id'] for call in dispatched] == [activity['id']]
    assert ActivityPubLog.query.count() == 0


def test_a_self_delete_from_an_unknown_actor_is_still_dropped_before_the_signature_check(
        app, signing_peer, redis_double, dispatched):
    ghost = f'https://{PEER}/c/ghost'
    body = json.dumps({'id': f'{ghost}/delete/1', 'type': 'Delete', 'actor': ghost, 'object': ghost}).encode('utf8')

    with app.test_client() as client:
        response = client.post('/inbox', data=body, content_type='application/activity+json',
                               headers={'Host': app.config['SERVER_NAME'], **unsigned_but_precheck_clean_headers(body)})

    assert response.status_code == 200
    assert dispatched == []
    assert ActivityPubLog.query.one().exception_message == 'Does not exist here'


# --- read-only: every write path asks Community.is_gone() ---

def test_the_write_predicates_refuse_a_gone_community(app, db_session):
    w = _world()
    assert can_create_post(w.member, w.community) is True
    assert can_create_post_reply(w.member, w.community) is True
    assert can_upvote(w.member, w.community) is True
    assert can_downvote(w.member, w.community) is True

    _gone(w)

    assert can_create_post(w.member, w.community) is False
    assert can_create_post_reply(w.member, w.community) is False
    assert can_upvote(w.member, w.community) is False
    assert can_downvote(w.member, w.community) is False


def test_a_web_post_is_refused(app, db_session):
    w = _world()
    _gone(w)

    with web_ctx(app, w.member):
        with pytest.raises(Exception, match='You are not permitted to make posts in this community'):
            shared_make_post(_web_form(), w.community, POST_TYPE_ARTICLE, SRC_WEB)

    assert db.session.query(Post).count() == 1


def test_an_api_post_is_refused(app, db_session):
    w = _world()
    _gone(w)

    with pytest.raises(Exception, match='You are not permitted to make posts in this community'):
        shared_make_post(_api_input(), w.community, POST_TYPE_ARTICLE, SRC_API, auth=bearer(w.member))

    assert db.session.query(Post).count() == 1


def test_a_web_reply_is_refused(app, db_session):
    w = _world()
    _gone(w)
    form = SimpleNamespace(body=_Field('hello'), notify_author=_Field(True), language_id=_Field(None),
                           distinguished=_Field(False))

    with web_ctx(app, w.member):
        with pytest.raises(Exception, match='You are not permitted to comment in this community'):
            make_reply(form, w.post, None, SRC_WEB)

    assert db.session.query(PostReply).count() == 0


def test_an_api_reply_is_refused(app, db_session):
    w = _world()
    _gone(w)

    with pytest.raises(Exception, match='You are not permitted to comment in this community'):
        make_reply({'body': 'hello', 'notify_author': True, 'language_id': None}, w.post, None, SRC_API,
                   auth=bearer(w.member))

    assert db.session.query(PostReply).count() == 0


def test_a_web_vote_is_refused(app, db_session):
    w = _world()
    _gone(w)

    with web_ctx(app, w.member):
        result = vote_for_post(w.post.id, 'upvote', True, None, SRC_WEB)

    assert result.status_code == 200
    assert db.session.query(PostVote).count() == 0


def test_an_api_vote_is_refused(app, db_session):
    w = _world()
    _gone(w)

    assert vote_for_post(w.post.id, 'upvote', True, None, SRC_API, auth=bearer(w.member)) == w.member.id
    assert db.session.query(PostVote).count() == 0


def _create(w, **object_fields):
    return {'id': f'https://{PEER}/activities/create/{uuid.uuid4().hex}', 'type': 'Create',
            'actor': w.alice.ap_profile_id,
            'object': {'id': f'https://{PEER}/objects/{uuid.uuid4().hex}', 'type': 'Note',
                       'attributedTo': w.alice.ap_profile_id, 'content': '<p>hello</p>', **object_fields}}


@pytest.mark.parametrize('in_reply_to,message', [(False, 'User cannot create post in Community'),
                                                 (True, 'User cannot create reply in Community')],
                         ids=['post', 'reply'])
def test_an_inbox_post_or_reply_is_refused(app, db_session, monkeypatch, in_reply_to, message):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()
    _gone(w)
    activity = _create(w, inReplyTo=w.post.ap_id) if in_reply_to else _create(w)

    process_new_content(w.alice, w.community, False, activity, False)

    log = ActivityPubLog.query.one()
    assert (log.result, log.exception_message) == ('failure', message)
    assert db.session.query(Post).count() == 1
    assert db.session.query(PostReply).count() == 0


def test_an_inbox_vote_is_refused(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    w = _world()
    _gone(w)
    like = {'id': f'https://{PEER}/activities/like/1', 'type': 'Like', 'actor': w.alice.ap_profile_id,
            'object': w.post.ap_id}

    process_upvote(w.alice, False, like, False)

    log = ActivityPubLog.query.one()
    assert (log.result, log.exception_message) == ('ignored', 'Cannot upvote this')
    assert db.session.query(PostVote).count() == 0


@pytest.mark.parametrize('gone,status', [(False, 'joined'), (True, 'community not found')], ids=['live', 'gone'])
def test_a_web_join_of_a_gone_community_finds_no_community(app, db_session, gone, status):
    """do_subscribe treats a gone Group as it treats a banned one. admin_preload returns the outcome instead of
    flashing; the dormant instance keeps the live control from sending its Follow."""
    w = _world()
    w.peer.dormant = True
    db.session.commit()
    if gone:
        _gone(w)
    joiner = make_user(None, 'joiner', local=True)
    joiner_id, community_id = joiner.id, w.community.id

    result = do_subscribe(f'memes@{PEER}', joiner_id, admin_preload=True)

    assert result['status'] == status
    db.session.expire_all()
    assert db.session.query(CommunityMember).filter_by(user_id=joiner_id,
                                                       community_id=community_id).count() == (0 if gone else 1)


# --- nothing is delivered to it ---

def test_a_post_is_delivered_to_a_live_remote_group(db_session, http_mock):
    s = _seed_for_post(local_community=False, with_keys=True)
    route = _post_remote_inbox(s, http_mock)

    _send_post(s.post)

    assert route.call_count == 1


def test_a_post_is_not_delivered_to_a_gone_group(db_session):
    """Without a keyed author, a delivery that did happen would leave a failure row: the witness is that none
    exists."""
    s = _seed_for_post(local_community=False)
    s.community.instance_id = make_instance(PEER, software='lemmy').id
    s.community.ap_inbox_url = POST_INBOX
    s.community.ap_deleted_at = utcnow()
    db.session.commit()

    _send_post(s.post)

    assert ActivityPubLog.query.filter_by(direction='out').count() == 0


def test_a_reply_is_delivered_to_a_live_remote_group(db_session, http_mock):
    s = _seed_for_reply(local_community=False, with_keys=True)
    route = _reply_remote_inbox(s, http_mock)

    _send_reply(s)

    assert route.call_count == 1


def test_a_reply_is_not_delivered_to_a_gone_group(db_session):
    s = _seed_for_reply(local_community=False)
    _deliverable_for_reply(s)
    s.community.ap_deleted_at = utcnow()
    db.session.commit()

    _send_reply(s)

    assert ActivityPubLog.query.filter_by(direction='out').count() == 0


def test_a_delete_is_delivered_to_a_live_remote_group(db_session, http_mock):
    s = _seed_for_delete(local_community=False, with_keys=True)
    _deliverable_for_delete(s)
    route = http_mock.post(DELETE_INBOX).respond(200, json={})

    delete_post(None, s.user.id, s.post.id)

    assert route.call_count == 1


def test_a_delete_is_not_delivered_to_a_gone_group(db_session):
    s = _seed_for_delete(local_community=False)
    _deliverable_for_delete(s)
    s.community.ap_deleted_at = utcnow()
    db.session.commit()

    delete_post(None, s.user.id, s.post.id)

    assert ActivityPubLog.query.filter_by(direction='out').count() == 0


# --- the page: content readable, notice shown ---

@pytest.mark.parametrize('gone', [False, True], ids=['live', 'gone'])
def test_the_community_page_lists_its_posts_and_says_when_the_group_was_deleted(app, db_session, gone):
    w = _world()
    w.site.private_instance = False
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.commit()
    if gone:
        _gone(w)

    with patch('app.community.routes.flash') as flashed:
        with patch('app.community.routes.render_template', return_value='rendered') as render:
            response = app.test_client().get(f'/c/memes@{PEER}')

    assert response.status_code == 200
    assert [post.id for post in render.call_args.kwargs['posts'].items] == [w.post.id]
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert ('deleted on its home server' in messages) is gone
