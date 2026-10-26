"""A local-only account cannot follow or message a remote person, and is told so (final fix F6, spec 3.4).

The outbound guard refuses the Follow or the message, so they used to be accepted here and never sent: a pending
follow that stayed pending, a message that never arrived. They are now refused with a flash (web) or an error (API).
Joining a remote community stays allowed, with no Follow sent, and a local person may still be followed and messaged.
"""
from datetime import timedelta

import pytest

from app import db
from app.api.alpha.utils.private_message import post_private_message
from app.api.alpha.utils.user import post_user_follow
from app.models import ChatMessage, Conversation, UserFollower, utcnow
from tests.conftest import forget_login
from tests.factories import make_instance, make_user
from tests.test_community_lifecycle import csrf, login
from tests.test_local_only_invisible import world  # noqa: F401 -- world is a fixture
from tests.test_per_domain_html_routes import Visitor
from tests.two_domains import ONION

pytestmark = pytest.mark.usefixtures('site')

REFUSAL = 'Your account is not visible to other servers, so it cannot follow or message people there.'


@pytest.fixture
def scene(app, world):
    for user in (world.alice, world.onionite):
        user.created = utcnow() - timedelta(days=365)   # old enough to be trusted and to send messages
    world.remote = make_user(make_instance('remote.test'), 'faraway')
    db.session.commit()
    return world


def _client(app, user):
    client = app.test_client()
    login(client, user)
    client.token = csrf(app, client)
    forget_login()
    return client


def _flashes(client):
    with client.session_transaction() as session:
        return [message for _, message in session.get('_flashes', [])]


def _follows(user, target):
    return UserFollower.query.filter_by(local_user_id=user.id, remote_user_id=target.id).count()


def test_a_local_only_account_cannot_follow_a_remote_person(app, scene):
    client = _client(app, scene.onionite)
    response = client.post(f'/u/{scene.remote.ap_id}/follow', data={'csrf_token': client.token})
    assert response.status_code == 302
    assert REFUSAL in _flashes(client)
    assert _follows(scene.onionite, scene.remote) == 0


def test_the_htmx_follow_button_says_so_too(app, scene):
    client = _client(app, scene.onionite)
    response = client.post(f'/u/{scene.remote.ap_id}/follow', data={'csrf_token': client.token},
                           headers={'HX-Request': 'true'})
    assert REFUSAL in response.get_data(as_text=True)
    assert _follows(scene.onionite, scene.remote) == 0


def test_a_local_only_account_may_follow_a_local_person(app, scene):
    client = _client(app, scene.onionite)
    client.post('/u/alice/follow', data={'csrf_token': client.token})
    assert _follows(scene.onionite, scene.alice) == 1


def test_the_api_refuses_the_remote_follow(app, scene):
    with pytest.raises(Exception, match='local_only_remote_contact'):
        post_user_follow(f'Bearer {scene.onionite.encode_jwt_token()}', {'user_id': scene.remote.id})
    assert _follows(scene.onionite, scene.remote) == 0


def test_a_local_only_account_cannot_start_a_conversation_with_a_remote_person(app, scene):
    client = _client(app, scene.onionite)
    response = client.post(f'/chat/{scene.remote.id}/new', data={'message': 'hello', 'csrf_token': client.token})
    assert response.status_code == 302
    assert REFUSAL in _flashes(client)
    assert ChatMessage.query.count() == 0


def test_a_local_only_account_cannot_write_into_a_conversation_with_a_remote_person(app, scene):
    conversation = Conversation(user_id=scene.remote.id)
    conversation.members.extend([scene.remote, scene.onionite])
    db.session.add(conversation)
    db.session.commit()
    # on the onion: off it a conversation with the onion's account answers as one nobody holds (follow-up F6)
    visitor = Visitor(app, scene.onionite, ONION)
    response = visitor.post(f'/chat/{conversation.id}', data={'message': 'hello'})
    assert response.status_code == 302
    with visitor.client.session_transaction(base_url=visitor.base) as session:
        assert REFUSAL in [message for _, message in session.get('_flashes', [])]
    assert ChatMessage.query.count() == 0


def test_a_local_only_account_may_message_a_local_person(app, scene):
    client = _client(app, scene.onionite)
    client.post(f'/chat/{scene.alice.id}/new', data={'message': 'hello', 'csrf_token': client.token})
    assert ChatMessage.query.count() == 1


def test_the_api_refuses_the_remote_message(app, scene):
    with pytest.raises(Exception, match='local_only_remote_contact'):
        post_private_message(f'Bearer {scene.onionite.encode_jwt_token()}',
                             {'recipient_id': scene.remote.id, 'content': 'hello'})
    assert ChatMessage.query.count() == 0
