"""S3.5: a DM's id is dereferenceable, but only by the parties to it (spec 2026-10-10)."""
from app import db
from app.chat import util as chat_util
from app.models import Role
from tests.factories import make_chat_message, make_conversation, make_instance, make_user
from tests.test_actor_profiles import seed_actors
from tests.test_ap_content_objects import ap_get, browser_get, signed_ap_get

PM_PREFIX = 'https://test.piefed.local/private_message/'


def _dm(software='lemmy', deleted=False, local_origin=True):
    """A local alice -> remote bob DM, as send_message stores it. bob and dave share bob's host."""
    seed_actors('test.piefed.local')
    peer = make_instance('peer.example', software=software)
    alice = make_user(None, 'alice', local=True)
    bob = make_user(peer, 'bob', with_keys=True)
    dave = make_user(peer, 'dave', with_keys=True)
    conversation = make_conversation(alice, bob)
    message = make_chat_message(alice, bob, 'pending', body='hi bob', deleted=deleted)
    message.body_html = '<p>hi bob</p>'
    message.conversation_id = conversation.id
    message.ap_id = f'{PM_PREFIX}{message.id}' if local_origin else 'https://peer.example/pm/1'
    db.session.commit()
    return message, conversation, bob, dave


def test_the_recipient_can_fetch_the_message(app, db_session):
    message, conversation, bob, dave = _dm()

    response = signed_ap_get(app, f'/private_message/{message.id}', bob, 'Test')

    assert response.status_code == 200
    assert response.content_type == 'application/activity+json'
    assert response.headers['Cache-Control'] == 'private, no-store'
    assert response.headers['Vary'] == 'Accept, Signature, Accept-Encoding'   # Flask-Compress appends the last
    body = response.json
    assert body['id'] == message.ap_id
    assert body['type'] == 'ChatMessage'             # lemmy recipient, as send_message federates it
    assert body['attributedTo'] == 'https://test.piefed.local/u/alice'
    assert body['to'] == ['https://peer.example/users/bob']
    assert body['content'] == '<p>hi bob</p>'
    assert '@context' in body


def test_a_mastodon_recipient_gets_a_note_with_a_mention(app, db_session):
    message, conversation, bob, dave = _dm(software='mastodon')

    body = signed_ap_get(app, f'/private_message/{message.id}', bob, 'Test').json

    assert body['type'] == 'Note'
    assert body['tag'][0]['type'] == 'Mention'
    assert body['tag'][0]['href'] == 'https://peer.example/users/bob'


def test_a_signer_on_the_recipients_host_can_fetch_the_message(app, db_session):
    """The recipient's host already received it through its shared inbox (host rule 3)."""
    message, conversation, bob, dave = _dm()

    assert signed_ap_get(app, f'/private_message/{message.id}', dave, 'Test').status_code == 200


def test_a_signer_on_an_unrelated_host_gets_404(app, db_session):
    message, conversation, bob, dave = _dm()
    stranger = make_user(make_instance('other.example'), 'eve', with_keys=True)

    response = signed_ap_get(app, f'/private_message/{message.id}', stranger, 'Test')

    assert response.status_code == 404
    assert response.headers['Cache-Control'] == 'no-store'


def test_an_anonymous_fetch_gets_404(app, db_session):
    message, conversation, bob, dave = _dm()

    response = ap_get(app, f'/private_message/{message.id}')

    assert response.status_code == 404
    assert response.headers['Cache-Control'] == 'no-store'


def test_a_deleted_message_is_404_even_to_the_recipient(app, db_session):
    message, conversation, bob, dave = _dm(deleted=True)

    assert signed_ap_get(app, f'/private_message/{message.id}', bob, 'Test').status_code == 404


def test_a_message_that_did_not_originate_here_is_404(app, db_session):
    message, conversation, bob, dave = _dm(local_origin=False)

    assert signed_ap_get(app, f'/private_message/{message.id}', bob, 'Test').status_code == 404


def test_a_message_with_no_recipient_is_404(app, db_session):
    message, conversation, bob, dave = _dm()
    message.recipient_id = None
    db.session.commit()

    assert signed_ap_get(app, f'/private_message/{message.id}', bob, 'Test').status_code == 404


def test_a_missing_message_is_404(app, db_session):
    message, conversation, bob, dave = _dm()

    assert signed_ap_get(app, f'/private_message/{message.id + 999}', bob, 'Test').status_code == 404


def _login(app, user):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True
    return client


def test_a_member_browser_is_sent_to_the_conversation(app, db_session):
    message, conversation, bob, dave = _dm()
    alice = db.session.get(type(bob), message.sender_id)

    response = _login(app, alice).get(f'/private_message/{message.id}')

    assert response.status_code == 302
    assert response.headers['Location'].endswith(f'/chat/{conversation.id}')


def test_an_anonymous_browser_gets_404_not_a_redirect(app, db_session):
    message, conversation, bob, dave = _dm()

    assert browser_get(app, f'/private_message/{message.id}').status_code == 404


def test_a_non_member_browser_gets_404(app, db_session):
    message, conversation, bob, dave = _dm()
    carol = make_user(None, 'carol', local=True)

    assert _login(app, carol).get(f'/private_message/{message.id}').status_code == 404


def test_a_deleted_message_is_404_to_a_member_browser(app, db_session):
    message, conversation, bob, dave = _dm(deleted=True)
    alice = db.session.get(type(bob), message.sender_id)

    assert _login(app, alice).get(f'/private_message/{message.id}').status_code == 404


def test_an_admin_browser_is_sent_to_the_conversation(app, db_session):
    message, conversation, bob, dave = _dm()
    root = make_user(None, 'root', local=True)
    root.roles.append(db.session.query(Role).filter_by(name='Admin').first() or Role(name='Admin'))
    db.session.commit()

    assert _login(app, root).get(f'/private_message/{message.id}').status_code == 302


def test_a_local_to_local_message_is_404_to_a_remote_signer(app, db_session):
    """P12: only the recipient's host is entitled; a local recipient's host is ours, not a remote signer's."""
    message, conversation, bob, dave = _dm()
    local = make_user(None, 'lena', local=True)
    message.recipient_id = local.id
    db.session.commit()

    assert signed_ap_get(app, f'/private_message/{message.id}', bob, 'Test').status_code == 404


def test_a_member_browser_asking_for_a_message_without_a_conversation_gets_404(app, db_session):
    message, conversation, bob, dave = _dm()
    message.conversation_id = None
    db.session.commit()
    alice = db.session.get(type(bob), message.sender_id)

    assert _login(app, alice).get(f'/private_message/{message.id}').status_code == 404


def test_update_message_still_federates_the_same_object(app, db_session, monkeypatch):
    """update_message now builds its object with chat_message_ap_object; the pushed Update is unchanged."""
    message, conversation, bob, dave = _dm(software='mastodon')
    sent = []
    monkeypatch.setattr(chat_util, 'send_post_request', lambda inbox, body, key, key_id: sent.append(body))

    chat_util.update_message(message)

    obj = sent[0]['object']
    assert sent[0]['type'] == 'Update'
    assert obj['type'] == 'Note'
    assert obj['id'] == message.ap_id
    assert obj['to'] == ['https://peer.example/users/bob']
    assert obj['tag'][0]['href'] == 'https://peer.example/users/bob'
    assert obj['updated'].endswith('Z')
    assert obj['published'] == message.created_at.isoformat() + 'Z'
