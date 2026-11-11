"""Pixelfed's direct messages for its official app (plan W1.4b1; spec 2026-11-10-pixelfed-app-design §6, rulings
2026-11-11): `/api/v1.1/direct/conversations` and `/api/v1.1/direct/unread_count` in Pixelfed's shapes, on PieFed's
conversations and chat messages, through the web chat's own send. One-to-one and, until W1.4b2, text only."""
from unittest.mock import patch

import pytest

from app import db
from app.chat import membership
from app.models import ChatMessage, Notification, User
from app.oauth_server import tokens
from tests.factories import make_follow, make_user, make_user_block

BASE = '/api/v1.1/direct'


@pytest.fixture
def world(app, api_baseline):
    registered, _secret = tokens.register_app('Pixelfed', ['https://app.example/cb'], 'read write', None)
    me, friend, stranger = (make_user(None, name, local=True) for name in ('dmme', 'dmfriend', 'dmstranger'))
    for person in (me, friend, stranger):
        person.verified, person.created = True, person.created.replace(year=2020)
    make_follow(me, friend)
    make_follow(friend, me)
    db.session.commit()
    headers = {}
    for person in (me, friend, stranger):
        access, _refresh = tokens.issue_token(registered, person, 'read write')
        headers[person.user_name] = {'Authorization': f'Bearer {access}'}
    with patch('app.chat.util.publish_sse_event'):
        yield {'me': me, 'friend': friend, 'stranger': stranger, 'client': app.test_client(), 'headers': headers}


def _as(world, who):
    return world['headers'][world[who].user_name]


def _get(world, path, who='me', **query):
    return world['client'].get(BASE + path, headers=_as(world, who), query_string=query)


def _post(world, path, who='me', **body):
    return world['client'].post(BASE + path, headers=_as(world, who), json=body)


def _start(world, who='me', to='friend'):
    response = _post(world, '/conversations', who, recipient_ids=[str(world[to].id)])
    assert response.status_code in (200, 201), response.get_json()
    return response.get_json()


def _say(world, conversation, text, who='me'):
    response = _post(world, f"/conversations/{conversation['id']}/messages", who, message=text)
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def test_a_conversation_is_started_then_found_again(world):
    first = _post(world, '/conversations', recipient_ids=[str(world['friend'].id)])
    again = _post(world, '/conversations', recipient_ids=[world['friend'].id])
    assert (first.status_code, again.status_code) == (201, 200)
    started = first.get_json()
    assert started['id'] == again.get_json()['id'] and started['type'] == 'direct'
    assert [person['username'] for person in started['participants']] == ['dmfriend']
    assert (started['participant_count'], started['state'], started['unread_count']) == (2, 'active', 0)
    assert started['last_message'] is None and started['muted'] is False and started['hidden'] is False


def test_a_message_is_sent_and_read_back_newest_first(world):
    conversation = _start(world)
    sent = _say(world, conversation, 'hello friend')
    assert (sent['text'], sent['is_author'], sent['type'], sent['media']) == ('hello friend', True, 'text', [])
    assert sent['sender_id'] == str(world['me'].id) and sent['conversation_id'] == conversation['id']
    _say(world, conversation, 'and hello back', who='friend')
    listed = _get(world, f"/conversations/{conversation['id']}/messages").get_json()
    assert [message['text'] for message in listed['data']] == ['and hello back', 'hello friend']
    assert [message['is_author'] for message in listed['data']] == [False, True]
    assert listed['meta']['has_more'] is False and listed['meta']['newest_id'] == listed['data'][0]['id']
    stored = ChatMessage.query.filter_by(body='hello friend').one()
    assert (stored.sender_id, stored.recipient_id) == (world['me'].id, world['friend'].id)


def test_messages_page_back_by_max_id_and_poll_by_min_id(world):
    conversation = _start(world)
    ids = [_say(world, conversation, f'message {n}')['id'] for n in range(5)]
    page = _get(world, f"/conversations/{conversation['id']}/messages", limit=2).get_json()
    assert [m['id'] for m in page['data']] == [ids[4], ids[3]] and page['meta']['has_more'] is True
    older = _get(world, f"/conversations/{conversation['id']}/messages", limit=2, max_id=ids[3]).get_json()
    assert [m['id'] for m in older['data']] == [ids[2], ids[1]]
    newer = _get(world, f"/conversations/{conversation['id']}/messages", min_id=ids[2]).get_json()
    assert [m['id'] for m in newer['data']] == [ids[4], ids[3]] and newer['meta']['has_more'] is False
    assert _get(world, f"/conversations/{conversation['id']}/messages", limit=0).status_code == 422


def test_the_inbox_lists_conversations_with_their_last_message_and_unread(world):
    conversation = _start(world)
    _say(world, conversation, 'are you there')
    _start(world, to='stranger')   # no message yet: listed nowhere, as Pixelfed's
    theirs = _get(world, '/conversations', who='friend').get_json()
    assert [c['id'] for c in theirs['data']] == [conversation['id']] and theirs['meta']['filter'] == 'primary'
    listed = theirs['data'][0]
    assert listed['last_message']['text'] == 'are you there' and listed['unread_count'] == 1
    assert _get(world, '/unread_count', who='friend').get_json() == {'primary': 1, 'requests': 0}


def test_a_stranger_s_first_message_is_a_request_until_accepted(world):
    conversation = _start(world, who='stranger', to='me')
    _say(world, conversation, 'hi, we have not met', who='stranger')
    assert _get(world, '/conversations').get_json()['data'] == []
    requests = _get(world, '/conversations', filter='requests').get_json()['data']
    assert [c['id'] for c in requests] == [conversation['id']] and requests[0]['state'] == 'request'
    assert _get(world, '/unread_count').get_json() == {'primary': 0, 'requests': 1}
    assert Notification.query.filter_by(user_id=world['me'].id).count() == 0
    accepted = _post(world, f"/conversations/{conversation['id']}/accept").get_json()
    assert accepted['state'] == 'active' and accepted['id'] == conversation['id']
    assert [c['id'] for c in _get(world, '/conversations').get_json()['data']] == [conversation['id']]


def test_hidden_conversations_are_filed_apart_until_a_new_message(world):
    conversation = _start(world)
    _say(world, conversation, 'hello')
    hidden = _post(world, f"/conversations/{conversation['id']}/hide").get_json()
    assert hidden['hidden'] is True
    assert _get(world, '/conversations').get_json()['data'] == []
    assert [c['id'] for c in _get(world, '/conversations', filter='hidden').get_json()['data']] == \
        [conversation['id']]
    _say(world, conversation, 'still there?', who='friend')
    assert [c['id'] for c in _get(world, '/conversations').get_json()['data']] == [conversation['id']]
    _post(world, f"/conversations/{conversation['id']}/hide")
    assert _post(world, f"/conversations/{conversation['id']}/unhide").get_json()['hidden'] is False


def test_a_muted_conversation_notifies_nobody_and_is_not_counted(world):
    conversation = _start(world)
    assert _post(world, f"/conversations/{conversation['id']}/mute").get_json()['muted'] is True
    _say(world, conversation, 'shh', who='friend')
    assert Notification.query.filter_by(user_id=world['me'].id).count() == 0
    assert _get(world, '/unread_count').get_json()['primary'] == 0
    assert _post(world, f"/conversations/{conversation['id']}/unmute").get_json()['muted'] is False
    assert _get(world, '/unread_count').get_json()['primary'] == 1


def test_reading_marks_messages_and_their_notifications_read(world):
    conversation = _start(world)
    first = _say(world, conversation, 'one', who='friend')
    second = _say(world, conversation, 'two', who='friend')
    me = db.session.get(User, world['me'].id)
    assert me.unread_notifications == 2
    state = _post(world, f"/conversations/{conversation['id']}/read", message_id=first['id']).get_json()
    assert state['unread_count'] == 1 and state['last_read_message_id'] == first['id']
    assert _post(world, f"/conversations/{conversation['id']}/read").get_json()['unread_count'] == 0
    db.session.expire_all()
    assert db.session.get(User, world['me'].id).unread_notifications == 0
    assert Notification.query.filter_by(user_id=world['me'].id, read=False).count() == 0
    assert second['id'] == _get(world, f"/conversations/{conversation['id']}").get_json()['last_read_message_id']


def test_only_one_s_own_message_is_deleted(world):
    conversation = _start(world)
    mine = _say(world, conversation, 'oops')
    theirs = _say(world, conversation, 'not yours', who='friend')
    with patch('app.mastodon.direct.task_selector') as federated:
        deleted = world['client'].delete(f"{BASE}/conversations/{conversation['id']}/messages/{mine['id']}",
                                         headers=_as(world, 'me'))
        refused = world['client'].delete(f"{BASE}/conversations/{conversation['id']}/messages/{theirs['id']}",
                                         headers=_as(world, 'me'))
    assert deleted.get_json() == {'deleted': True} and refused.status_code == 404
    federated.assert_called_once_with('delete_pm', message_id=int(mine['id']))
    listed = _get(world, f"/conversations/{conversation['id']}/messages").get_json()
    assert [m['text'] for m in listed['data']] == ['not yours']


def test_leaving_drops_the_conversation(world):
    conversation = _start(world)
    _say(world, conversation, 'bye')
    left = _post(world, f"/conversations/{conversation['id']}/leave").get_json()
    assert left['state'] == 'left'
    assert _get(world, f"/conversations/{conversation['id']}").status_code == 404
    assert _get(world, '/conversations').get_json()['data'] == []


@pytest.mark.parametrize('body,status', [
    ({'recipient_ids': []}, 422),
    ({'recipient_ids': 'nobody'}, 422),
    ({'recipient_ids': ['999999']}, 404),
    ({'recipient_ids': ['x']}, 404),
])
def test_starting_with_nobody_is_refused(world, body, status):
    assert _post(world, '/conversations', **body).status_code == status


def test_a_group_is_refused(world):
    response = _post(world, '/conversations', recipient_ids=[world['friend'].id, world['stranger'].id])
    assert response.status_code == 422 and 'one person' in response.get_json()['error']


def test_oneself_alone_is_refused(world):
    assert _post(world, '/conversations', recipient_ids=[world['me'].id]).status_code == 422


@pytest.mark.parametrize('blocker,blocked', [('friend', 'me'), ('me', 'friend')])
def test_a_block_either_way_refuses_the_conversation(world, blocker, blocked):
    make_user_block(world[blocker], world[blocked])
    assert _post(world, '/conversations', recipient_ids=[world['friend'].id]).status_code == 403


def test_one_who_may_not_send_is_refused(world, monkeypatch):
    conversation = _start(world)
    monkeypatch.setattr(User, 'can_send_pm_to', lambda self, recipient: False)
    assert _post(world, '/conversations', recipient_ids=[world['stranger'].id]).status_code == 403
    assert _post(world, f"/conversations/{conversation['id']}/messages", message='hi').status_code == 403


@pytest.mark.parametrize('body', [{}, {'message': ''}, {'message': '   '}, {'message': 'x' * 5001},
                                  {'message': 7}])
def test_an_empty_or_overlong_message_is_refused(world, body):
    conversation = _start(world)
    assert _post(world, f"/conversations/{conversation['id']}/messages", **body).status_code == 422


def test_media_waits_for_w1_4b2(world):
    conversation = _start(world)
    response = _post(world, f"/conversations/{conversation['id']}/messages", message='look', media_ids=['1'])
    assert response.status_code == 422


@pytest.mark.parametrize('path', ['', '/messages'])
def test_a_conversation_one_is_not_in_is_not_found(world, path):
    conversation = _start(world)
    assert _get(world, f"/conversations/{conversation['id']}{path}", who='stranger').status_code == 404
    assert _get(world, f'/conversations/99999999{path}').status_code == 404
    assert _get(world, f'/conversations/{2 ** 40}{path}').status_code == 404


def test_the_list_pages_by_cursor(world):
    others = [make_user(None, f'dmpeer{n}', local=True) for n in range(3)]
    for other in others:
        make_follow(world['me'], other)
        conversation = membership.start_conversation(other, world['me'])
        _say(world, {'id': str(conversation.id)}, f'from me to {other.user_name}')
    first = _get(world, '/conversations', limit=2).get_json()
    assert len(first['data']) == 2 and first['meta']['next_cursor']
    rest = _get(world, '/conversations', limit=2, cursor=first['meta']['next_cursor']).get_json()
    assert len(rest['data']) == 1 and rest['meta']['next_cursor'] is None
    assert _get(world, '/conversations', cursor='garbage').status_code == 422
    assert _get(world, '/conversations', filter='everything').status_code == 422


def test_reads_need_read_and_writes_need_write(world, app):
    registered, _secret = tokens.register_app('Reader', ['https://app.example/cb'], 'read', None)
    access, _refresh = tokens.issue_token(registered, world['me'], 'read')
    reader = {'Authorization': f'Bearer {access}'}
    assert world['client'].get(BASE + '/conversations', headers=reader).status_code == 200
    assert world['client'].post(BASE + '/conversations', headers=reader,
                                json={'recipient_ids': [world['friend'].id]}).status_code == 403
    assert world['client'].get(BASE + '/unread_count').status_code == 401


@pytest.mark.parametrize('path', ['/messages', '/read', '/accept', '/mute', '/hide', '/leave'])
def test_writes_to_a_conversation_one_is_not_in_are_not_found(world, path):
    conversation = _start(world)
    assert _post(world, f"/conversations/{conversation['id']}{path}", who='stranger', message='hi').status_code == 404


def test_a_bad_message_id_to_read_up_to_is_refused(world):
    conversation = _start(world)
    assert _post(world, f"/conversations/{conversation['id']}/read", message_id='latest').status_code == 422


def test_a_local_only_account_cannot_message_a_remote_one(world, monkeypatch):
    monkeypatch.setattr('app.mastodon.direct.refuses_remote_contact', lambda sender, other: True)
    response = _post(world, '/conversations', recipient_ids=[world['friend'].id])
    assert response.status_code == 403 and 'from here' in response.get_json()['error']


def test_the_people_to_message_first_are_those_who_follow_back(world):
    """The app's New message screen lists `compose/mutuals` (Pixelfed's FollowerService::getMutualsWithProfiles):
    whom the viewer follows and who follow them, ten at a time, by `cursor` (the last one's id)."""
    more = [make_user(None, f'dmmutual{n}', local=True) for n in range(11)]
    for other in more:
        make_follow(world['me'], other)
        make_follow(other, world['me'])
    make_follow(world['me'], world['stranger'])   # one way only: not listed
    first = _get(world, '/compose/mutuals').get_json()
    assert len(first['data']) == 10 and first['has_more'] is True and first['total_count'] == 12
    rest = _get(world, '/compose/mutuals', cursor=first['next_cursor']).get_json()
    assert len(rest['data']) == 2 and rest['has_more'] is False and rest['next_cursor'] is None
    names = {person['username'] for person in first['data'] + rest['data']}
    assert names == {'dmfriend', *(other.user_name for other in more)}
    assert _get(world, '/compose/mutuals', cursor='x').status_code == 422


def _lookup(world, query, **extra):
    return world['client'].post(BASE + '/lookup', headers=_as(world, 'me'), json={'q': query, **extra})


def test_looking_someone_up_by_name_finds_them_as_pixelfed_wraps_them(world):
    """The app's New message search (Pixelfed's composeLookup): up to eight accounts by name, each wrapped as
    {local, id, name, privacy, avatar, account}; with `_pe` the accounts themselves."""
    found = _lookup(world, 'dmfri').get_json()
    assert [(row['name'], row['local'], row['id']) for row in found] == [('dmfriend', True, str(world['friend'].id))]
    assert found[0]['account']['username'] == 'dmfriend' and found[0]['privacy'] is True
    assert [row['username'] for row in _lookup(world, '@dmfriend', _pe=1).get_json()] == ['dmfriend']


def test_a_lookup_leaves_out_blocks_either_way(world):
    make_user_block(world['me'], world['friend'])
    make_user_block(world['stranger'], world['me'])
    assert {row['name'] for row in _lookup(world, 'dm').get_json()} == {'dmme'}


@pytest.mark.parametrize('query', ['', 'd', 'x' * 51, 7])
def test_a_lookup_needs_two_to_fifty_characters(world, query):
    assert _lookup(world, query).status_code == 422
