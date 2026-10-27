"""Every alpha API endpoint that takes an object's id or name answers an onion row off the onion exactly as one nobody
holds (follow-up F6, round 4). The table below names, for each id or name field of each endpoint's schema, what kind
of row it names; a walk of the URL map fails on any field the table does not know, as tests/test_hidden_routes_table.py
does for the web's route arguments, so a new endpoint cannot skip the shared finders unnoticed."""
import re

import pytest

from app import db
from app.models import ChatMessage, CommunityFlair, Notification, UserRegistration
from tests.conftest import forget_login
from tests.factories import grant_permission
from tests.test_api_routes_gate_sweep import documented_routes, minimal_payload
from tests.test_hidden_routes import talking  # noqa: F401 -- a fixture
from tests.test_instance_visible_here import HOSTS, scene  # noqa: F401 -- scene is a fixture
from tests.test_local_only_actors_hidden import ONION_ART, ONION_BOB, world  # noqa: F401 -- world is a fixture
from tests.test_onion_hidden_api_ids import NOBODY, alpha, call, onion_in_reply, reports  # noqa: F401 -- fixtures
from tests.test_onion_hidden_on_clearnet import onion  # noqa: F401 -- a fixture
from tests.test_per_domain_ap_routes import onion_proxy, twins  # noqa: F401
from tests.two_domains import FIRST, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')

# A field that may name an object: an id, a list of ids, or a name
OBJECT_FIELD = re.compile(r'(^id|_id|_ids|name)$')

# Fields of that shape that name no account, community, post, reply, conversation, message, report, notification or
# flair: nothing an onion row could be told apart by
NOT_OBJECTS = {
    'language_id': 'a language',
    'topic_id': 'a topic: an onion community in it is filtered from the posts listed',
    'feed_id': 'a feed: feeds owned by onion accounts are deferred to per-domain feeds Task 7 (ruling N-m8)',
    'parent_feed_id': 'a feed (ruling N-m8)',
    'instance_id': 'an instance: site/block is accepted by ruling',
    'choice_id': 'a poll choice, read only after its post went through the finder',
    'display_name': "the caller's own display name",
}
# Endpoint fields that name an object but are not looked up by a caller's id or name
EXEMPT = {
    ('GET', '/api/alpha/feed', 'id'): 'a feed (ruling N-m8)',
    ('GET', '/api/alpha/feed', 'name'): 'a feed (ruling N-m8)',
    ('POST', '/api/alpha/feed', 'name'): 'the name of a feed being created',
    ('POST', '/api/alpha/community', 'name'): 'the name of a community being created, on this storefront',
    ('POST', '/api/alpha/user/login', 'username'): "a login names this storefront's own account",
    ('POST', '/api/alpha/user/verify_credentials', 'username'): "as login: this storefront's own account",
    ('POST', '/api/alpha/user/register', 'username'): 'the name of an account being created, on this storefront',
}


def _cases():
    """(method, path, field) -> the kind of row it names, and the fields the request must also carry to reach it."""
    p = '/api/alpha'
    on_first_art = {'community_id': 'first_art'}
    rows = {
        ('GET', '/search', 'community_name'): ('community_name', {}),
        ('GET', '/search', 'community_id'): ('community', {}),
        ('GET', '/modlog', 'mod_person_id'): ('person', {}),
        ('GET', '/modlog', 'other_person_id'): ('person', {}),
        ('GET', '/modlog', 'community_id'): ('community', {}),
        ('GET', '/modlog', 'post_id'): ('post', {}),
        ('GET', '/modlog', 'comment_id'): ('reply', {}),
        ('GET', '/community', 'id'): ('community', {}),
        ('GET', '/community', 'name'): ('community_name', {}),
        ('POST', '/community/follow', 'community_id'): ('community', {}),
        ('POST', '/community/block', 'community_id'): ('community', {}),
        ('PUT', '/community', 'community_id'): ('community', {}),
        ('PUT', '/community/subscribe', 'community_id'): ('community', {}),
        ('POST', '/community/delete', 'community_id'): ('community', {}),
        ('POST', '/community/mod', 'community_id'): ('community', {'person_id': 'admin'}),
        ('POST', '/community/mod', 'person_id'): ('person', on_first_art),
        ('GET', '/community/moderate/bans', 'community_id'): ('community', {}),
        ('PUT', '/community/moderate/unban', 'community_id'): ('community', {'user_id': 'first_bob'}),
        ('PUT', '/community/moderate/unban', 'user_id'): ('person', on_first_art),
        ('POST', '/community/moderate/ban', 'community_id'): ('community', {'user_id': 'first_bob'}),
        ('POST', '/community/moderate/ban', 'user_id'): ('person', on_first_art),
        ('POST', '/community/moderate/post/nsfw', 'post_id'): ('post', {}),
        ('POST', '/community/flair', 'community_id'): ('community', {}),
        ('PUT', '/community/flair', 'flair_id'): ('flair', {}),
        ('POST', '/community/flair/delete', 'flair_id'): ('flair', {}),
        ('GET', '/post/list', 'community_name'): ('community_name', {}),
        ('GET', '/post/list', 'community_id'): ('community', {}),
        ('GET', '/post/list', 'person_id'): ('person', {}),
        ('GET', '/post/list2', 'community_name'): ('community_name', {}),
        ('GET', '/post/list2', 'community_id'): ('community', {}),
        ('GET', '/post/list2', 'person_id'): ('person', {}),
        ('GET', '/post', 'id'): ('post', {}),
        ('GET', '/post/replies', 'post_id'): ('post', {}),
        ('GET', '/post/replies', 'parent_id'): ('reply', {}),
        ('POST', '/post/like', 'post_id'): ('post', {}),
        ('PUT', '/post/save', 'post_id'): ('post', {}),
        ('PUT', '/post/subscribe', 'post_id'): ('post', {}),
        ('POST', '/post', 'community_id'): ('community', {}),
        ('PUT', '/post', 'post_id'): ('post', {}),
        ('POST', '/post/delete', 'post_id'): ('post', {}),
        ('POST', '/post/report', 'post_id'): ('post', {}),
        ('GET', '/post/report/list', 'community_id'): ('community', {}),
        ('GET', '/post/report/list', 'post_id'): ('post', {}),
        ('PUT', '/post/report/resolve', 'report_id'): ('post_report', {}),
        ('POST', '/post/lock', 'post_id'): ('post', {}),
        ('POST', '/post/hide', 'post_id'): ('post', {}),
        ('POST', '/post/feature', 'post_id'): ('post', {}),
        ('POST', '/post/remove', 'post_id'): ('post', {}),
        ('POST', '/post/mark_as_read', 'post_id'): ('post', {}),
        ('POST', '/post/mark_as_read', 'post_ids'): ('posts', {}),
        ('GET', '/post/like/list', 'post_id'): ('post', {}),
        ('POST', '/post/assign_flair', 'post_id'): ('post', {}),
        ('POST', '/post/poll_vote', 'post_id'): ('post', {}),
        ('GET', '/comment/list', 'person_id'): ('person', {}),
        ('GET', '/comment/list', 'community_id'): ('community', {}),
        ('GET', '/comment/list', 'post_id'): ('post', {}),
        ('GET', '/comment/list', 'parent_id'): ('reply', {}),
        ('POST', '/comment/like', 'comment_id'): ('reply', {}),
        ('PUT', '/comment/save', 'comment_id'): ('reply', {}),
        ('PUT', '/comment/subscribe', 'comment_id'): ('reply', {}),
        ('POST', '/comment', 'post_id'): ('post', {}),
        ('POST', '/comment', 'parent_id'): ('reply', {'post_id': 'first_post'}),
        ('PUT', '/comment', 'comment_id'): ('reply', {}),
        ('POST', '/comment/delete', 'comment_id'): ('reply', {}),
        ('POST', '/comment/report', 'comment_id'): ('reply', {}),
        ('GET', '/comment/report/list', 'comment_id'): ('reply', {}),
        ('GET', '/comment/report/list', 'community_id'): ('community', {}),
        ('PUT', '/comment/report/resolve', 'report_id'): ('reply_report', {}),
        ('POST', '/comment/remove', 'comment_id'): ('reply', {}),
        ('POST', '/comment/mark_as_read', 'comment_reply_id'): ('reply', {}),
        ('POST', '/comment/mark_as_answer', 'comment_reply_id'): ('reply', {}),
        ('POST', '/comment/distinguish', 'comment_reply_id'): ('reply', {}),
        ('GET', '/comment', 'id'): ('reply', {}),
        ('POST', '/comment/lock', 'comment_id'): ('reply', {}),
        ('GET', '/comment/like/list', 'comment_id'): ('reply', {}),
        ('GET', '/private_message/conversation', 'person_id'): ('person', {}),
        ('GET', '/private_message/conversation', 'conversation_id'): ('conversation', {}),
        ('POST', '/private_message/conversation/leave', 'conversation_id'): ('conversation', {}),
        ('POST', '/private_message', 'recipient_id'): ('person', {}),
        ('PUT', '/private_message', 'private_message_id'): ('message', {}),
        ('POST', '/private_message/mark_as_read', 'private_message_id'): ('message', {}),
        ('POST', '/private_message/delete', 'private_message_id'): ('message', {}),
        ('POST', '/private_message/report', 'private_message_id'): ('message', {}),
        ('POST', '/private_message/conversation/report', 'conversation_id'): ('conversation', {}),
        ('GET', '/private_message/report/list', 'conversation_id'): ('conversation', {}),
        ('GET', '/private_message/report/list', 'private_message_id'): ('message', {}),
        ('GET', '/private_message/conversation/report/list', 'conversation_id'): ('conversation', {}),
        ('PUT', '/private_message/report/resolve', 'report_id'): ('message_report', {}),
        ('PUT', '/private_message/conversation/report/resolve', 'report_id'): ('message_report', {}),
        ('GET', '/user', 'person_id'): ('person', {}),
        ('GET', '/user', 'username'): ('person_name', {}),
        ('GET', '/user', 'community_id'): ('community', {'person_id': 'first_bob'}),
        ('POST', '/user/block', 'person_id'): ('person', {}),
        ('PUT', '/user/subscribe', 'person_id'): ('person', {}),
        ('PUT', '/user/notification_state', 'notif_id'): ('notification', {}),
        ('POST', '/user/set_flair', 'community_id'): ('community', {}),
        ('POST', '/user/follow', 'user_id'): ('person', {}),
        ('POST', '/user/unfollow', 'user_id'): ('person', {}),
        ('POST', '/user/note', 'person_id'): ('person', {}),
        ('POST', '/user/ban', 'person_id'): ('person', {}),
        ('POST', '/user/unban', 'person_id'): ('person', {}),
        ('PUT', '/admin/registration_application/approve', 'user_id'): ('person', {}),
    }
    return {(method, p + path, field): case for (method, path, field), case in rows.items()}


CASES = _cases()


def object_fields(app):
    """(method, path, field, location) for every id or name field of every documented alpha API endpoint."""
    found = []
    for method, path, parameters in documented_routes(app):
        for parameter in parameters:
            schema = parameter['schema']
            schema = schema() if isinstance(schema, type) else schema
            for name, field in schema.fields.items():
                key = field.data_key or name
                if OBJECT_FIELD.search(key) and key not in NOT_OBJECTS:
                    found.append((method, path, key, parameter.get('in')))
    return found


def test_every_id_or_name_an_endpoint_takes_is_in_the_table(app):
    """A new endpoint, or a new field, that names a row fails here until it is in CASES (and goes through a finder)."""
    keys = {(method, path, field) for method, path, field, _ in object_fields(app)}
    unknown = sorted(keys - set(CASES) - set(EXEMPT))
    assert unknown == [], '\n'.join(map(str, unknown))
    assert sorted((set(CASES) | set(EXEMPT)) - keys) == []   # no stale entry


def hidden_and_missing(world, viewer, kind):
    """The value naming a row of `kind` hidden here, and the one naming no row, for `viewer`."""
    values = {
        'post': (world.post.id, NOBODY), 'posts': ([world.post.id], [NOBODY]), 'reply': (world.reply.id, NOBODY),
        'community': (world.onion_art.id, NOBODY), 'community_name': (ONION_ART, 'nosuchart'),
        'person': (world.onion_bob.id, NOBODY), 'person_name': (ONION_BOB, 'nosuchbob'),
        'conversation': (world.with_onion.id, NOBODY), 'message': (world.onion_message.id, NOBODY),
        'post_report': (world.reports[0].id, NOBODY), 'reply_report': (world.reports[1].id, NOBODY),
        'message_report': (world.reports[2].id, NOBODY), 'flair': (world.flair.id, NOBODY),
        'notification': (world.notifications[viewer.id], NOBODY),
    }
    return values[kind]


@pytest.fixture
def walk(reports):  # noqa: F811
    onion = reports
    grant_permission(onion.admin, 'approve registrations')
    onion.onion_message = ChatMessage.query.filter_by(conversation_id=onion.with_onion.id,
                                                      recipient_id=onion.first_bob.id).first()
    onion.flair = CommunityFlair(community_id=onion.onion_art.id, flair='ONIONFLAIR')
    db.session.add(onion.flair)
    db.session.add(UserRegistration(user_id=onion.onion_bob.id, answer='ONIONANSWER', status=0))
    onion.notifications = {}
    for viewer in (onion.admin, onion.first_bob):
        notification = Notification(user_id=viewer.id, title='t', url=f'/comment/{onion.reply.id}', notif_type=4,
                                    subtype='new_reply_on_followed_comment', author_id=onion.onion_bob.id,
                                    targets={'comment_id': onion.reply.id, 'post_id': onion.post.id})
        db.session.add(notification)
        db.session.flush()
        onion.notifications[viewer.id] = notification.id
    db.session.commit()
    return onion


def request_for(app, world, method, path, field, value, also):
    """The query string or body naming `value` in `field`: every required field, the rows `also` names, then it."""
    parameters = next(params for m, p, params in documented_routes(app) if (m, p) == (method, path))
    payload = {}
    for parameter in parameters:
        payload.update(minimal_payload(parameter['schema']))
    named = {'first_art': world.first_art.id, 'first_bob': world.first_bob.id, 'admin': world.admin.id,
             'first_post': world.first_post.id}
    payload.update({key: named[name] for key, name in also.items()})
    payload[field] = value
    return payload


def call_query(app, viewer, path, query):
    forget_login()
    headers = {'Authorization': f'Bearer {viewer.encode_jwt_token()}'}
    return app.test_client().get(path, base_url=HOSTS[FIRST], headers=headers, query_string=query)


def spelled(value) -> bytes:
    return str(value[0] if isinstance(value, list) else value).encode()


@pytest.mark.parametrize('key', sorted(CASES), ids=lambda key: f'{key[0]} {key[1][10:]} {key[2]}')
def test_a_hidden_id_answers_as_one_nobody_holds_on_every_endpoint(app, walk, key):
    """Byte for byte, but for the id or name itself where an answer spells it back."""
    method, path, field = key
    kind, also = CASES[key]
    for viewer in (walk.admin, walk.first_bob):
        hidden, missing = hidden_and_missing(walk, viewer, kind)
        answers = []
        for value in (hidden, missing):
            payload = request_for(app, walk, method, path, field, value, also)
            answer = call_query(app, viewer, path, payload) if method == 'GET' else \
                call(app, viewer, method, path, payload)
            answers.append((answer.status_code, answer.get_data().replace(spelled(value), spelled(hidden))))
        assert answers[0] == answers[1], (key, viewer.user_name, answers[0][0], answers[0][1][:300],
                                          answers[1][0], answers[1][1][:300])
        assert answers[0][0] < 500, (key, answers[0][1][:300])
