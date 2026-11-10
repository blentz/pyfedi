"""A person's inbox in Lemmy's API for Jerboa (plan W3; spec 2026-11-10-lemmy-v3-design §2): unread counts, replies,
mentions and private messages answer Lemmy 0.19.11's models from alpha's handlers, and marking one read marks its
notification; sending a message shares alpha's limit."""
import uuid
from datetime import timedelta

import pytest

from app import cache, db, limiter
from app.constants import NOTIF_MENTION, NOTIF_REPLY
from app.models import ChatMessage, Notification, utcnow
from tests import lemmy_app_contract as contract
from tests import test_onion_guard_limits as limit_tests
from tests.factories import make_community, make_post, make_post_reply, make_user

limited_app = limit_tests.limited_app   # an app whose limiter counts


@pytest.fixture
def world(app, api_baseline, monkeypatch):
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    cache.clear()
    person, author = make_user(None, 'lemmyinbox', local=True), make_user(None, 'lemmysender', local=True)
    for account in (person, author):
        account.verified, account.created = True, utcnow() - timedelta(days=60)
    post = make_post(make_community('lemmyinbox'), author, ap_id=f'https://test.piefed.local/post/{uuid.uuid4().hex}')
    reply, mention = make_post_reply(post, author, body='a reply to you'), make_post_reply(post, author, body='@you')
    for comment in (reply, mention):
        comment.path, comment.depth = [0, comment.id], 0
    db.session.commit()
    return {'person': person, 'author': author, 'reply': reply, 'mention': mention, 'client': app.test_client(),
            'headers': {'Authorization': f'Bearer {person.encode_jwt_token()}'},
            'author_headers': {'Authorization': f'Bearer {author.encode_jwt_token()}'}}


def _notify(world, comment, subtype, notif_type):
    db.session.add(Notification(title='something', url=f'/post/1#comment_{comment.id}', user_id=world['person'].id,
                                author_id=world['author'].id, notif_type=notif_type, subtype=subtype, read=False,
                                targets={'gen': '0', 'comment_id': comment.id}))
    world['person'].unread_notifications = (world['person'].unread_notifications or 0) + 1
    db.session.commit()


@pytest.fixture
def told(world):
    _notify(world, world['reply'], 'new_reply_on_followed_comment', NOTIF_REPLY)
    _notify(world, world['mention'], 'comment_mention', NOTIF_MENTION)
    return world


def _call(world, method, path, model, headers=None, **kwargs):
    response = getattr(world['client'], method)(path, headers=headers or world['headers'], **kwargs)
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    assert (found := contract.problems(body, model)) == [], found
    return body


def test_the_badges_counts(told):
    assert _call(told, 'get', '/api/v3/user/unread_count', 'GetUnreadCountResponse') == \
        {'replies': 1, 'mentions': 1, 'private_messages': 0}


def test_replies_and_mentions_are_listed_and_marked_read(told):
    replies = _call(told, 'get', '/api/v3/user/replies', 'GetRepliesResponse', query_string={'sort': 'New'})
    assert [view['comment']['content'] for view in replies['replies']] == ['a reply to you']
    entry = replies['replies'][0]['comment_reply']
    assert entry['comment_id'] == told['reply'].id and entry['recipient_id'] == told['person'].id
    mentions = _call(told, 'get', '/api/v3/user/mention', 'GetPersonMentionsResponse',
                     query_string={'unread_only': 'true'})
    assert [view['person_mention']['comment_id'] for view in mentions['mentions']] == [told['mention'].id]

    marked = _call(told, 'post', '/api/v3/comment/mark_as_read', 'CommentReplyResponse',
                   json={'comment_reply_id': entry['id'], 'read': True})
    assert marked['comment_reply_view']['comment']['id'] == told['reply'].id
    marked = _call(told, 'post', '/api/v3/user/mention/mark_as_read', 'PersonMentionResponse',
                   json={'person_mention_id': told['mention'].id, 'read': True})
    assert marked['person_mention_view']['comment']['id'] == told['mention'].id
    assert Notification.query.filter_by(user_id=told['person'].id, read=False).count() == 0


def test_everything_is_marked_read_at_once(told):
    assert _call(told, 'post', '/api/v3/user/mark_all_as_read', 'GetRepliesResponse') == {'replies': []}
    assert Notification.query.filter_by(user_id=told['person'].id, read=False).count() == 0


def test_a_private_message_is_sent_listed_and_marked_read(world):
    sent = _call(world, 'post', '/api/v3/private_message', 'PrivateMessageResponse', headers=world['author_headers'],
                 json={'content': 'Hello privately', 'recipient_id': world['person'].id})
    message = sent['private_message_view']['private_message']
    assert message['content'] == 'Hello privately' and sent['private_message_view']['recipient']['name'] == 'lemmyinbox'
    listed = _call(world, 'get', '/api/v3/private_message/list', 'PrivateMessagesResponse',
                   query_string={'unread_only': 'true', 'page': 1, 'limit': 20})
    assert [view['private_message']['id'] for view in listed['private_messages']] == [message['id']]
    assert _call(world, 'get', '/api/v3/user/unread_count', 'GetUnreadCountResponse')['private_messages'] == 1
    read = _call(world, 'post', '/api/v3/private_message/mark_as_read', 'PrivateMessageResponse',
                 json={'private_message_id': message['id'], 'read': True})
    assert read['private_message_view']['private_message']['read'] and db.session.get(ChatMessage, message['id']).read


def test_sending_messages_shares_alphas_limit(limited_app, api_baseline, monkeypatch):
    monkeypatch.setitem(limited_app.config, 'ENABLE_ALPHA_API', 'true')
    with limited_app.app_context():
        sender, recipient = make_user(None, 'lemmypmlimit', local=True), make_user(None, 'lemmypmto', local=True)
        for account in (sender, recipient):
            account.verified, account.created = True, utcnow() - timedelta(days=60)
        db.session.commit()
        headers, away = {'Authorization': f'Bearer {sender.encode_jwt_token()}'}, {'REMOTE_ADDR': '203.0.113.6'}
        client = limited_app.test_client()
        limiter.reset()
        limiter.enabled = True
        try:
            codes = [client.post(path, json={'content': f'hi {n}', 'recipient_id': recipient.id}, headers=headers,
                                 environ_base=away).status_code
                     for n, path in enumerate(['/api/alpha/private_message'] * 2 + ['/api/v3/private_message'] * 2)]
        finally:
            limiter.reset()
            limiter.enabled = False
    assert codes == [200, 200, 200, 429]
