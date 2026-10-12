"""Interop C1 item 2 (docs/superpowers/specs/2026-10-11-ap-interop-c1-design.md): inbound actor Update.

An Update whose object is a Person, Service, Application or Podcast reached the Create/Update arm and logged
`Unacceptable type (create): Person`, so a peer's new name, bio or rotated key arrived only with the daily
refresh. It now refreshes the signer from the pushed document at once -- no fetch -- but only when the document
describes the signer itself, the signer is a known remote user, and the Update was not merely Announced.

The Group Update path gains the same rule: the document must describe the community the activity is for, and a
remote Group's document is taken only from the Group. The three refresh tasks also take the actor's inbox from
the document, keeping the stored one when the document has none.
"""
import os
from types import SimpleNamespace

import pytest

from app import db
from app.activitypub import routes as activitypub_routes
from app.activitypub import util as activitypub_util
from app.activitypub.util import (INBOX_URL_MAX, LD_ACTOR_UPDATE_REFUSED, delivery_inbox_of, names_actor,
                                  refresh_community_profile_task, refresh_feed_profile_task)
from app.models import ActivityPubLog, Community, Feed, File, User, utcnow
from tests.factories import (inbox_activity, make_community, make_community_member, make_file, make_site, make_user,
                             seed_community_owner)
from tests.test_ap_refresh_profiles import (PEER, _feed_document, _group_document, _person_document,
                                            _remote_community, _remote_feed, _remote_user)
from tests.test_inbox_dispatch_create_update import create_activity
from tests.test_inbox_dispatch_lock_delete import record_moderation
from tests.test_inbox_dispatch_preamble import dispatch

OLD_KEY = '-----BEGIN PUBLIC KEY-----old'
NEW_KEY = '-----BEGIN PUBLIC KEY-----rotated'


def _known_remote_user(name='wakko'):
    """A remote user the dispatcher resolves without a fetch.

    ap_fetched_at is stamped so find_actor_or_create_cached's schedule_actor_refresh does not run a refresh
    inline (eager Celery) -- that would fetch the actor and hide what the pushed document did."""
    user = _remote_user(name)
    user.ap_fetched_at = utcnow()
    user.public_key = OLD_KEY
    user.ap_inbox_url = f'https://{PEER}/u/{name}/inbox'
    db.session.commit()
    return user


def _remote_group(name='books'):
    """A REMOTE community the dispatcher resolves without a fetch. make_community leaves ap_id None, which makes
    is_local() True whatever the host, so ap_id is set to a non-local URL."""
    community = make_community(name, host=PEER)
    community.ap_id = f'https://{PEER}/c/{name}'
    community.ap_fetched_at = utcnow()
    db.session.commit()
    return community


def _actor_update(user, document):
    return create_activity(user, document, activity_type='Update')


# --- names_actor and delivery_inbox_of ---

ACTOR = SimpleNamespace(ap_profile_id=f'https://{PEER}/u/wakko')


@pytest.mark.parametrize('candidate,expected', [
    (f'https://{PEER}/u/wakko', True),
    ('https://PEER.example/u/Wakko', True),
    (f'  https://{PEER}/u/wakko\n', True),
    (f'https://{PEER}/u/yakko', False),
    (None, False),
    (['https://peer.example/u/wakko'], False),
    ('', False),
    (f'https://{PEER}/u/wakko/', False),
    (f'https://{PEER}/u/wakko#main-key', False),
])
def test_names_actor(candidate, expected):
    assert names_actor(candidate, ACTOR) is expected


def test_names_actor_without_an_actor_or_profile_id_is_false():
    assert names_actor('', SimpleNamespace(ap_profile_id=None)) is False
    assert names_actor(f'https://{PEER}/u/wakko', None) is False


SHARED = f'https://{PEER}/inbox'
OWN = f'https://{PEER}/u/wakko/inbox'
TOO_LONG = f'https://{PEER}/' + 'x' * INBOX_URL_MAX


@pytest.mark.parametrize('document,expected', [
    ({'endpoints': {'sharedInbox': SHARED}, 'inbox': OWN}, SHARED),
    ({'inbox': OWN}, OWN),
    ({'endpoints': {'sharedInbox': TOO_LONG}, 'inbox': OWN}, OWN),
    ({'inbox': TOO_LONG}, None),
    ({'inbox': ''}, None),
    ({'inbox': ['x']}, None),
    ({}, None),
], ids=['shared', 'own', 'shared-too-long', 'own-too-long', 'empty', 'non-string', 'none'])
def test_delivery_inbox_of(document, expected):
    assert delivery_inbox_of(document, f'https://{PEER}/u/wakko') == expected


# --- the actor Update ---

@pytest.mark.parametrize('actor_type,bot', [('Person', False), ('Service', True), ('Application', False)])
def test_an_actor_update_refreshes_the_name_summary_and_key(app, db_session, monkeypatch, actor_type, bot):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    user = _known_remote_user()
    user_id = user.id
    document = _person_document(fields={'type': actor_type, 'name': 'Wakko Warner',
                                        'summary': '<p>a new bio</p>',
                                        'publicKey': {'publicKeyPem': NEW_KEY}})

    dispatch(_actor_update(user, document))

    db.session.expire_all()
    user = db.session.get(User, user_id)
    assert user.title == 'Wakko Warner'
    assert 'a new bio' in user.about_html
    assert user.public_key == NEW_KEY
    assert user.bot is bot
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_a_podcast_update_keeps_the_podcast_community_handling(app, db_session, monkeypatch):
    """Castopod's `Podcast` actor: the refresh still calls ensure_podcast_community (U:910, D24)."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    user = _known_remote_user()
    user_id = user.id
    calls = []
    monkeypatch.setattr(activitypub_util, 'ensure_podcast_community',
                        lambda podcast_user, actor_json: calls.append((podcast_user.id, actor_json['type'])))

    dispatch(_actor_update(user, _person_document(fields={'type': 'Podcast'})))

    assert calls == [(user_id, 'Podcast')]
    assert ActivityPubLog.query.one().result == 'success'


def test_an_update_describing_another_actor_is_refused(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    user = _known_remote_user()
    user_id = user.id
    document = _person_document('yakko', fields={'name': 'Not Wakko',
                                                 'publicKey': {'publicKeyPem': NEW_KEY}})

    dispatch(_actor_update(user, document))

    db.session.expire_all()
    user = db.session.get(User, user_id)
    assert user.title != 'Not Wakko'
    assert user.public_key == OLD_KEY
    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Update of another actor refused'


def test_an_update_from_an_unknown_actor_is_dropped_and_creates_nothing(app, db_session, monkeypatch):
    """No route is registered: the old preamble's find_actor_or_create_cached(actor_id) would fetch the unknown
    actor to create it, which block_outbound_http refuses."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    seed_community_owner(PEER)
    stranger = f'https://{PEER}/u/stranger'
    users_before = User.query.count()

    dispatch({'id': f'{stranger}/activities/1', 'type': 'Update', 'actor': stranger,
              'object': _person_document('stranger')})

    assert User.query.count() == users_before
    log = ActivityPubLog.query.one()
    assert log.result == 'ignored'
    assert log.exception_message == 'Update of an unknown actor'


@pytest.mark.parametrize('suffix', ['/', '#main-key'])
def test_an_update_whose_id_differs_by_a_trailing_slash_or_fragment_is_refused(app, db_session, monkeypatch, suffix):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    user = _known_remote_user()
    user_id = user.id
    document = _person_document('wakko') | {'id': user.ap_profile_id + suffix, 'name': 'Not Wakko'}

    dispatch(_actor_update(user, document))

    assert db.session.get(User, user_id).title != 'Not Wakko'
    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Update of another actor refused'


def test_a_banned_actors_update_logs_that_it_is_banned(app, db_session, monkeypatch):
    """The no-create lookup misses a banned actor as it misses an unknown one; the log says which it was."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    user = _known_remote_user()
    user_id = user.id
    user.banned = True
    db.session.commit()

    dispatch(_actor_update(user, _person_document('wakko') | {'name': 'Not Wakko'}))

    assert db.session.get(User, user_id).title != 'Not Wakko'
    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Update from a banned, deleted or blocked actor'


def test_an_update_of_a_url_from_an_unknown_actor_creates_nothing(app, db_session, monkeypatch):
    """The object is only a URL, so it may name an actor: the signer is looked up without being created, and
    nothing is fetched (no route is registered)."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    seed_community_owner(PEER)
    stranger = f'https://{PEER}/u/stranger'
    users_before = User.query.count()

    dispatch({'id': f'{stranger}/activities/1', 'type': 'Update', 'actor': stranger, 'object': stranger})

    assert User.query.count() == users_before
    log = ActivityPubLog.query.one()
    assert log.result == 'ignored'
    assert log.exception_message == 'Update from an unknown actor'


def test_a_group_signed_update_of_a_person_is_refused(app, db_session, monkeypatch):
    """Only the actor itself may push its document: a Group sending Update{Person} changes nobody."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    user = _known_remote_user()
    user_id = user.id
    group = _remote_group()
    calls = record_moderation(monkeypatch, 'process_actor_update', 'refresh_user_profile_task')

    dispatch(inbox_activity(group, activity_type='Update',
                            object=_person_document('wakko') | {'name': 'Not Wakko'}))

    assert calls['process_actor_update'] == [] and calls['refresh_user_profile_task'] == []
    assert db.session.get(User, user_id).title != 'Not Wakko'
    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Unexpected Update activity from Group'


def test_an_update_from_a_local_actor_is_refused(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    seed_community_owner(PEER)
    local = make_user(None, 'local', local=True)
    local.ap_profile_id = f"{app.config['SERVER_URL']}/u/local".lower()
    db.session.commit()

    dispatch(_actor_update(local, {'type': 'Person', 'id': local.ap_profile_id, 'preferredUsername': 'local',
                                   'name': 'Renamed'}))

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Update of a local actor refused'


def test_an_announced_actor_update_is_refused(app, db_session, monkeypatch):
    """The Group signed the Announce, not the actor the document describes, so the signer rule cannot hold."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    user = _known_remote_user()
    user_id = user.id
    group = _remote_group()

    dispatch(inbox_activity(group, activity_type='Announce',
                            object={'id': f'https://{PEER}/activities/update-1', 'type': 'Update',
                                    'actor': user.ap_profile_id,
                                    'object': _person_document(fields={'name': 'Via Group'})}))

    db.session.expire_all()
    assert db.session.get(User, user_id).title != 'Via Group'
    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Announced actor Update refused: only the actor itself may send it'


def test_a_document_without_a_key_keeps_the_old_key(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    user = _known_remote_user()
    user_id = user.id
    document = _person_document(fields={'name': 'Keyless'})
    del document['publicKey']

    dispatch(_actor_update(user, document))

    db.session.expire_all()
    user = db.session.get(User, user_id)
    assert user.title == 'Keyless'
    assert user.public_key == OLD_KEY


@pytest.mark.parametrize('fields,expected', [
    ({'endpoints': {'sharedInbox': SHARED}, 'inbox': OWN}, SHARED),
    ({'inbox': f'https://{PEER}/u/wakko/new-inbox'}, f'https://{PEER}/u/wakko/new-inbox'),
    ({}, OWN),
], ids=['shared', 'own', 'absent-keeps-old'])
def test_an_actor_update_refreshes_the_inbox(app, db_session, monkeypatch, fields, expected):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    user = _known_remote_user()
    user_id = user.id

    dispatch(_actor_update(user, _person_document(fields=fields)))

    db.session.expire_all()
    assert db.session.get(User, user_id).ap_inbox_url == expected


# --- the refresh tasks take the inbox too ---

@pytest.mark.parametrize('fields,expected', [
    ({'endpoints': {'sharedInbox': SHARED}}, SHARED),
    ({}, 'https://peer.example/c/memes/old-inbox'),
], ids=['shared', 'absent-keeps-old'])
def test_a_community_refresh_takes_the_inbox_from_the_document(app, db_session, http_mock, fields, expected):
    community = _remote_community()
    community.ap_inbox_url = 'https://peer.example/c/memes/old-inbox'
    db.session.commit()
    community_id = community.id

    refresh_community_profile_task(community_id, _group_document(fields=fields))

    db.session.expire_all()
    assert db.session.get(Community, community_id).ap_inbox_url == expected


@pytest.mark.parametrize('fields,expected', [
    ({'inbox': 'https://peer.example/f/news/inbox'}, 'https://peer.example/f/news/inbox'),
    ({}, 'https://peer.example/f/news/old-inbox'),
], ids=['own', 'absent-keeps-old'])
def test_a_feed_refresh_takes_the_inbox_from_the_document(app, db_session, http_mock, fields, expected):
    feed = _remote_feed()
    feed.ap_inbox_url = 'https://peer.example/f/news/old-inbox'
    db.session.commit()
    feed_id = feed.id

    refresh_feed_profile_task(feed_id, _feed_document(fields=fields))

    db.session.expire_all()
    assert db.session.get(Feed, feed_id).ap_inbox_url == expected


# --- the Group Update path ---

def test_a_groups_own_update_of_itself_refreshes_without_announcing(app, db_session, monkeypatch):
    """The a.gup.pe / NodeBB shape: the Group itself sends Update{Group} (preamble R:1047-1050)."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    seed_community_owner(PEER)
    group = _remote_group()
    calls = record_moderation(monkeypatch, 'refresh_community_profile', 'announce_activity_to_followers')

    dispatch(inbox_activity(group, activity_type='Update',
                            object={'type': 'Group', 'id': group.ap_profile_id}))

    assert len(calls['refresh_community_profile']) == 1
    assert calls['announce_activity_to_followers'] == []
    assert ActivityPubLog.query.one().result == 'success'


def test_a_group_announcing_a_moderators_update_of_itself_refreshes(app, db_session, monkeypatch):
    """The Lemmy shape: the Group Announces its moderator's Update{Group}. The announcing Group is the signer."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner(PEER)
    group = _remote_group()
    moderator = make_user(instance, 'moderator')
    moderator.ap_fetched_at = utcnow()
    db.session.commit()
    calls = record_moderation(monkeypatch, 'refresh_community_profile', 'announce_activity_to_followers')

    dispatch(inbox_activity(group, activity_type='Announce',
                            object={'id': f'https://{PEER}/activities/update-group-1', 'type': 'Update',
                                    'actor': moderator.ap_profile_id,
                                    'object': {'type': 'Group', 'id': group.ap_profile_id}}))

    assert len(calls['refresh_community_profile']) == 1
    assert ActivityPubLog.query.one().result == 'success'


def test_a_group_update_describing_another_group_is_refused(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    seed_community_owner(PEER)
    group = _remote_group()
    calls = record_moderation(monkeypatch, 'refresh_community_profile')

    dispatch(inbox_activity(group, activity_type='Update',
                            object={'type': 'Group', 'id': f'https://{PEER}/c/someone_else'}))

    assert calls['refresh_community_profile'] == []
    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Update of another Group refused'


def test_a_user_signed_group_update_with_no_community_is_refused_not_crashed(app, db_session, monkeypatch):
    """find_community finds nothing, process_chat declines: `community` is None at the Group branch, which used to
    raise AttributeError on `community.is_local()`. names_actor(..., None) is False, so it is refused."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner(PEER)
    editor = make_user(instance, 'editor')
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json, author=None: None)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)
    calls = record_moderation(monkeypatch, 'refresh_community_profile', 'process_chat')

    dispatch(create_activity(editor, {'type': 'Group', 'id': f'https://{PEER}/c/books'}, activity_type='Update'))

    assert calls['refresh_community_profile'] == []
    assert ActivityPubLog.query.one().exception_message == 'Update of another Group refused'


def test_an_actor_update_from_an_offline_instance_is_ignored_not_succeeded(app, db_session, monkeypatch):
    """refresh_user_profile_task applies nothing for an actor whose instance is dormant or gone, so the Update is
    logged as ignored rather than as a success it was not."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    user = _known_remote_user()
    user_id = user.id
    user.instance.dormant = True
    db.session.commit()

    dispatch(_actor_update(user, _person_document(fields={'name': 'Offline', 'publicKey': {'publicKeyPem': NEW_KEY}})))

    db.session.expire_all()
    user = db.session.get(User, user_id)
    assert user.title != 'Offline'
    assert user.public_key == OLD_KEY
    log = ActivityPubLog.query.one()
    assert log.result == 'ignored'
    assert log.exception_message == 'Update of an actor on an offline instance'


# --- a local community's identity is not a peer's to replace ---

ATTACKER_KEY = '-----BEGIN PUBLIC KEY-----attacker'
ATTACKER_FOLLOWERS = f'https://{PEER}/attacker/followers'
ATTACKER_FEATURED = f'https://{PEER}/attacker/featured'
ATTACKER_MODERATORS = f'https://{PEER}/attacker/moderators'
ATTACKER_INBOX = f'https://{PEER}/attacker/inbox'


def _hostile_group_document(community_id):
    """A Group document for `community_id` that also tries to replace everything that makes up its identity."""
    return {'type': 'Group', 'id': community_id, 'preferredUsername': 'microblogs',
            'name': 'Renamed by a moderator', 'summary': '<p>a new description</p>',
            'publicKey': {'publicKeyPem': ATTACKER_KEY},
            'followers': ATTACKER_FOLLOWERS, 'featured': ATTACKER_FEATURED, 'attributedTo': ATTACKER_MODERATORS,
            'inbox': ATTACKER_INBOX, 'endpoints': {'sharedInbox': ATTACKER_INBOX},
            'postingWarning': 'attacker warning', 'theme': 'attacker', 'nsfl': True, 'genAI': True,
            'postUrlType': 'ap',
            'tag': [{'type': 'lemmy:CommunityTag', 'id': f'{community_id}/tag/x', 'display_name': 'x'}]}


def _record_fetches(monkeypatch):
    """Every get_request the refresh task makes, answered 404 so a task that should not fetch runs to its end."""
    fetched = []

    def _spy(url, *args, **kwargs):
        fetched.append(url)
        return SimpleNamespace(status_code=404, close=lambda: None)

    monkeypatch.setattr(activitypub_util, 'get_request', _spy)
    return fetched


def test_a_moderators_update_of_a_local_community_keeps_its_key_and_ids(app, db_session, monkeypatch):
    """A remote moderator of a LOCAL community may edit how it is presented. The pushed document reached
    refresh_community_profile_task, which overwrote the key, inbox, followers, featured and moderators URLs too --
    and the community's actor document would then publish the sender's key."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner(PEER)
    community = make_community(host=app.config['SERVER_NAME'])
    community.public_key = OLD_KEY
    community.ap_inbox_url = f"https://{app.config['SERVER_NAME']}/c/microblogs/inbox"
    community.ap_featured_url = f"https://{app.config['SERVER_NAME']}/c/microblogs/featured"
    community.ap_moderators_url = None
    community.ap_followers_url = None
    db.session.commit()
    community_id, profile_id = community.id, community.ap_profile_id
    before = {column: getattr(community, column) for column in
              ('public_key', 'ap_inbox_url', 'ap_followers_url', 'ap_featured_url', 'ap_moderators_url',
               'ap_profile_id', 'ap_public_url', 'ap_id', 'instance_id', 'posting_warning', 'theme',
               'ai_generated', 'post_url_type')}
    moderator = make_user(instance, 'moderator')
    moderator.ap_fetched_at = utcnow()   # else find_actor_or_create_cached refreshes it, a fetch of its own
    make_community_member(moderator, community, is_moderator=True)
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json, author=None: community)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)
    record_moderation(monkeypatch, 'announce_activity_to_followers')
    fetched = _record_fetches(monkeypatch)

    dispatch(create_activity(moderator, _hostile_group_document(profile_id), activity_type='Update'))

    db.session.expire_all()
    community = db.session.get(Community, community_id)
    assert {column: getattr(community, column) for column in before} == before
    assert community.title == 'Renamed by a moderator'
    assert community.nsfl is True   # a presentation flag, like nsfw
    assert 'a new description' in community.description_html
    assert community.flair == []
    assert fetched == []
    assert ActivityPubLog.query.one().result == 'success'


def test_the_same_document_for_a_remote_community_still_replaces_its_key(app, db_session, monkeypatch):
    community = _remote_community()
    community.public_key = OLD_KEY
    db.session.commit()
    community_id = community.id
    fetched = _record_fetches(monkeypatch)

    refresh_community_profile_task(community_id, _hostile_group_document(community.ap_profile_id))

    db.session.expire_all()
    community = db.session.get(Community, community_id)
    assert community.public_key == ATTACKER_KEY
    assert community.ap_inbox_url == ATTACKER_INBOX
    assert community.ap_followers_url == ATTACKER_FOLLOWERS
    assert community.ap_featured_url == ATTACKER_FEATURED
    assert community.ap_moderators_url == ATTACKER_MODERATORS
    assert community.posting_warning == 'attacker warning'
    assert community.theme == 'attacker'
    assert community.nsfl is True
    assert community.ai_generated is True
    assert community.post_url_type == 'ap'
    assert [flair.flair for flair in community.flair] == ['x']
    assert fetched == [ATTACKER_MODERATORS, ATTACKER_FOLLOWERS, ATTACKER_FEATURED]


def test_a_remote_community_document_without_a_key_keeps_the_old_key(app, db_session):
    community = _remote_community()
    community.public_key = OLD_KEY
    db.session.commit()
    community_id = community.id
    document = _group_document(fields={'summary': '<p>keyless</p>'})
    del document['publicKey']

    refresh_community_profile_task(community_id, document)

    db.session.expire_all()
    community = db.session.get(Community, community_id)
    assert 'keyless' in community.description_html
    assert community.public_key == OLD_KEY


@pytest.mark.parametrize('enabled', [True, False])
def test_a_local_communitys_nsfw_and_nsfl_obey_the_site_settings(app, db_session, enabled):
    """R203, as on the local edit path (app/shared/community.py): the site's switch wins over a moderator's
    choice, and a pushed document is a moderator's choice too."""
    seed_community_owner(PEER)
    site = make_site()
    site.enable_nsfw = enabled
    site.enable_nsfl = enabled
    community = make_community(host=app.config['SERVER_NAME'])
    community.ap_followers_url = None
    db.session.commit()
    community_id = community.id

    refresh_community_profile_task(community_id, {'type': 'Group', 'id': community.ap_profile_id,
                                                  'name': 'microblogs', 'sensitive': True, 'nsfl': True})

    db.session.expire_all()
    community = db.session.get(Community, community_id)
    assert community.nsfw is enabled
    assert community.nsfl is enabled


# --- a local community's own uploaded icon and banner are not a pushed document's to replace ---

def _local_community_with_uploads(app, tmp_path):
    """A local community whose icon and banner are uploads held on disk: File rows with a file_path and no
    source_url, as app/community/util.py makes them."""
    seed_community_owner(PEER)
    community = make_community(host=app.config['SERVER_NAME'])
    community.ap_followers_url = None
    for column in ('icon', 'image'):
        path = tmp_path / f'{column}.png'
        path.write_bytes(b'png')
        setattr(community, column, make_file(file_path=str(path)))
    db.session.commit()
    return community


def _lemmy_group_update_document(community, icon_url, image_url):
    """The whole Group document Lemmy echoes in every Update{Group}, a moderator's new description included."""
    return {'type': 'Group', 'id': community.ap_profile_id, 'preferredUsername': community.name,
            'name': community.title, 'summary': '<p>a moderator edited this</p>',
            'icon': {'type': 'Image', 'url': icon_url}, 'image': {'type': 'Image', 'url': image_url}}


def _record_image_work(monkeypatch):
    calls = []
    monkeypatch.setattr(activitypub_util, 'make_image_sizes', lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(File, 'delete_from_disk', lambda self, *args, **kwargs: calls.append(('delete', self.id)))
    return calls


def test_a_moderators_update_echoing_a_local_communitys_own_icon_keeps_the_upload(app, db_session, monkeypatch,
                                                                                    tmp_path):
    """Final review I2. A Lemmy moderator's edit of a local community echoes our own served icon and banner URLs.
    Compared only with source_url (None for an upload), they read as new: the upload was deleted from disk and the
    URL of the file just deleted was fetched in its place."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    community = _local_community_with_uploads(app, tmp_path)
    community_id, icon_id, image_id = community.id, community.icon_id, community.image_id
    icon_path, image_path = community.icon.file_path, community.image.file_path
    document = _lemmy_group_update_document(
        community, f"{app.config['SERVER_URL']}{community.icon_image()}",
        f"{app.config['SERVER_URL']}{community.header_image()}")
    moderator = make_user(db.session.get(Community, community_id).instance, 'lemmymod')
    moderator.ap_fetched_at = utcnow()
    make_community_member(moderator, community, is_moderator=True)
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json, author=None: community)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)
    record_moderation(monkeypatch, 'announce_activity_to_followers')
    fetched = _record_fetches(monkeypatch)
    calls = []
    monkeypatch.setattr(activitypub_util, 'make_image_sizes', lambda *args, **kwargs: calls.append(args))

    dispatch(create_activity(moderator, document, activity_type='Update'))

    db.session.expire_all()
    community = db.session.get(Community, community_id)
    assert 'a moderator edited this' in community.description_html
    assert (community.icon_id, community.image_id) == (icon_id, image_id)
    assert (community.icon.file_path, community.icon.source_url) == (icon_path, None)
    assert (community.image.file_path, community.image.source_url) == (image_path, None)
    assert os.path.isfile(icon_path) and os.path.isfile(image_path)
    assert calls == [] and fetched == []
    assert ActivityPubLog.query.one().result == 'success'


def test_any_image_on_our_own_host_leaves_a_local_communitys_upload_alone(app, db_session, monkeypatch, tmp_path):
    community = _local_community_with_uploads(app, tmp_path)
    community_id, icon_id, image_id = community.id, community.icon_id, community.image_id
    own = f"https://{app.config['SERVER_NAME'].upper()}/static/media/communities/elsewhere.png"
    calls = _record_image_work(monkeypatch)

    refresh_community_profile_task(community_id, _lemmy_group_update_document(community, own, own))

    db.session.expire_all()
    community = db.session.get(Community, community_id)
    assert (community.icon_id, community.image_id) == (icon_id, image_id)
    assert calls == []


def test_a_new_remote_icon_for_a_local_community_is_applied_without_deleting_the_upload(app, db_session,
                                                                                         monkeypatch, tmp_path):
    """A moderator's real new remote URL is still applied, as before; the upload it replaces stays on disk."""
    community = _local_community_with_uploads(app, tmp_path)
    community_id, icon_id, image_id = community.id, community.icon_id, community.image_id
    calls = _record_image_work(monkeypatch)
    new_icon, new_image = f'https://{PEER}/pictrs/image/new-icon.png', f'https://{PEER}/pictrs/image/new-banner.png'

    refresh_community_profile_task(community_id, _lemmy_group_update_document(community, new_icon, new_image))

    db.session.expire_all()
    community = db.session.get(Community, community_id)
    assert (community.icon.source_url, community.image.source_url) == (new_icon, new_image)
    assert community.icon_id != icon_id and community.image_id != image_id
    assert [call for call in calls if call[0] == 'delete'] == []
    assert len(calls) == 2   # both new images are sized


def test_a_local_communitys_fetched_icon_is_still_replaced_and_deleted(app, db_session, monkeypatch):
    """An icon a moderator set from a remote URL earlier is a fetched copy, not an upload: a newer remote URL
    replaces it and deletes the copy, as before."""
    seed_community_owner(PEER)
    community = make_community(host=app.config['SERVER_NAME'])
    community.ap_followers_url = None
    community.icon = make_file(source_url=f'https://{PEER}/pictrs/image/old-icon.png')
    community.image = make_file(source_url=f'https://{PEER}/pictrs/image/old-banner.png')
    db.session.commit()
    community_id, icon_id, image_id = community.id, community.icon_id, community.image_id
    calls = _record_image_work(monkeypatch)

    refresh_community_profile_task(community_id, _lemmy_group_update_document(
        community, f'https://{PEER}/pictrs/image/new-icon.png', f'https://{PEER}/pictrs/image/new-banner.png'))

    db.session.expire_all()
    community = db.session.get(Community, community_id)
    assert community.icon.source_url == f'https://{PEER}/pictrs/image/new-icon.png'
    assert ('delete', icon_id) in calls and ('delete', image_id) in calls


def test_an_upload_served_from_our_object_store_is_ours_too(app, db_session, monkeypatch):
    """With S3 storage an upload's file_path is the object store's URL, on another host: echoed back it is still
    our own file, not a new image."""
    seed_community_owner(PEER)
    community = make_community(host=app.config['SERVER_NAME'])
    community.ap_followers_url = None
    community.icon = make_file(file_path='https://media.example/communities/ab/cd/icon.png')
    community.image = make_file(file_path='https://media.example/communities/ab/cd/banner.png')
    db.session.commit()
    community_id, icon_id, image_id = community.id, community.icon_id, community.image_id
    calls = _record_image_work(monkeypatch)

    refresh_community_profile_task(community_id, _lemmy_group_update_document(
        community, community.icon_image(), community.header_image()))

    db.session.expire_all()
    community = db.session.get(Community, community_id)
    assert (community.icon_id, community.image_id) == (icon_id, image_id)
    assert calls == []


def test_an_ld_signed_actor_update_is_refused_and_logged_without_a_fetch(app, db_session, monkeypatch):
    """Final review M8. An LD-signed Update{Person} had its object refetched from its origin
    (verify_ld_accepted_activity), and an actor document is attributed to nobody, so it could never pass: one wasted
    fetch, then a refusal blaming the document. It is now refused before any fetch, with its own reason."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    user = _known_remote_user()
    document = _person_document(fields={'name': 'Renamed over LD'})
    fetched = []
    monkeypatch.setattr(activitypub_util, 'fetch_object_capped', lambda uri, *a, **kw: fetched.append(uri) or
                        (document, None))

    activitypub_routes.process_inbox_request(create_activity(user, document, activity_type='Update'), True,
                                             ld_signed=True)

    db.session.expire_all()
    assert db.session.get(User, user.id).title != 'Renamed over LD'
    assert fetched == []
    log = ActivityPubLog.query.one()
    assert (log.result, log.exception_message) == ('failure', f'LD-signed activity dropped: {LD_ACTOR_UPDATE_REFUSED}')
