"""One gate in front of every route that names an account, community, post or reply (follow-up F6, review I8).

Off the onion, a route whose arguments name an onion row answers as a route naming a row nobody holds: the same status
and the same body, so no route is an oracle for what exists on the onion. The gate is one table of argument names
(app/hidden_routes.py): tests/test_hidden_routes_table.py walks the URL map against it."""
import pytest
from sqlalchemy.exc import NoResultFound

from app import db
from app.hidden_routes import _value_hidden, hidden_notifications
from app.local_only import found_here, hidden_message_clause, one_here
from app.models import ChatMessage, Conversation, utcnow
from tests.test_instance_visible_here import get, scene  # noqa: F401 -- scene is a fixture
from tests.test_local_only_actors_hidden import world  # noqa: F401 -- world is a fixture
from tests.test_onion_hidden_on_clearnet import api, onion  # noqa: F401 -- onion is a fixture
from tests.test_per_domain_ap_routes import onion_proxy, twins  # noqa: F401
from tests.test_per_domain_html_routes import Visitor
from tests.two_domains import FIRST, ONION, default_request, onion_request, two_domains  # noqa: F401
from tests.conftest import forget_login

pytestmark = pytest.mark.usefixtures('site')

NOBODY = 2 ** 30   # an id no row holds


def answer(response):
    return response.status_code, response.headers.get('Location'), response.get_data()


def visit(app, user, method, path, host=FIRST):
    forget_login()
    visitor = Visitor(app, user, host)
    return visitor.post(path) if method == 'POST' else visitor.get(path)


def assert_same(app, user, method, hidden, missing, host=FIRST):
    found, absent = visit(app, user, method, hidden, host), visit(app, user, method, missing, host)
    assert answer(found) == answer(absent), (hidden, answer(found)[:2], answer(absent)[:2])
    assert found.status_code == 404, hidden


# --- C3: a moderator's routes on a user id ------------------------------------------------------------------------

def moderator_routes(onion, user_id):
    community = onion.first_art.id
    return [('GET', f'/community/community/{community}/{user_id}/ban_user_community'),
            ('POST', f'/community/community/{community}/{user_id}/ban_user_community'),
            ('POST', f'/community/community/{community}/{user_id}/unban_user_community'),
            ('POST', f'/community/community/{community}/{user_id}/kick_user_community'),
            ('POST', f'/community/community/{community}/make_owner/{user_id}'),
            ('POST', f'/community/community/{community}/remove_owner/{user_id}'),
            ('POST', f'/community/community/{community}/moderators/add/{user_id}'),
            ('POST', f'/community/community/{community}/moderators/remove/{user_id}'),
            ('GET', f'/chat/ban_from_mod/{user_id}/{community}')]


def test_a_moderators_routes_answer_an_onion_account_as_an_id_nobody_holds(app, onion):
    for (method, hidden), (_, missing) in zip(moderator_routes(onion, onion.onion_bob.id),
                                              moderator_routes(onion, NOBODY)):
        assert_same(app, onion.admin, method, hidden, missing)
    assert db.session.get(type(onion.onion_bob), onion.onion_bob.id).id == onion.onion_bob.id   # nothing was done


def test_on_the_onion_the_ban_page_names_its_account(app, onion):
    response = visit(app, onion.admin, 'GET', f'/community/community/{onion.onion_art.id}/{onion.onion_bob.id}'
                                              f'/ban_user_community', host=ONION)
    assert response.status_code == 200


# --- C2: a new message to an account by id --------------------------------------------------------------------------

@pytest.mark.parametrize('method', ['GET', 'POST'])
def test_a_new_message_to_an_onion_account_answers_as_one_to_an_id_nobody_holds(app, onion, method):
    assert_same(app, onion.admin, method, f'/chat/{onion.onion_bob.id}/new', f'/chat/{NOBODY}/new')


def test_on_the_onion_a_new_message_to_it_is_offered(app, onion):
    assert visit(app, onion.admin, 'GET', f'/chat/{onion.onion_bob.id}/new', host=ONION).status_code == 200


# --- I1: the lookup redirects -----------------------------------------------------------------------------------------

@pytest.mark.parametrize('signed_in', [False, True])
@pytest.mark.parametrize('hidden, missing', [
    (f'/community/lookup/art/{ONION}', f'/community/lookup/nope/{ONION}'),
    (f'/user/lookup/bob/{ONION}', f'/user/lookup/nobody/{ONION}'),
    (f'/community/lookup/art/www.{ONION}', f'/community/lookup/nope/www.{ONION}'),
    (f'/c/art@{ONION}', f'/c/nope@{ONION}'),
    (f'/u/bob@{ONION}', f'/u/nobody@{ONION}'),
    (f'/community/art@{ONION}/moderate', f'/community/nope@{ONION}/moderate'),
])
def test_a_lookup_of_an_onion_name_answers_as_one_of_a_name_nobody_holds(app, onion, signed_in, hidden, missing):
    if signed_in:
        assert_same(app, onion.first_bob, 'GET', hidden, missing)
    else:
        found, absent = get(app, hidden), get(app, missing)
        assert answer(found) == answer(absent), hidden
        assert found.status_code == 404, hidden


def test_on_the_onion_its_lookups_still_redirect(app, onion):
    response = visit(app, onion.first_bob, 'GET', f'/community/lookup/art/{ONION}', host=ONION)
    assert response.status_code == 302 and response.headers['Location'].endswith('/c/art')
    response = visit(app, onion.first_bob, 'GET', f'/user/lookup/bob/{ONION}', host=ONION)
    assert response.status_code == 302 and response.headers['Location'].endswith('/u/bob')


# --- I2: subscribing to an account's posts ---------------------------------------------------------------------------

def test_subscribing_to_an_onion_account_answers_as_for_an_id_nobody_holds(app, onion):
    assert_same(app, onion.first_bob, 'POST', f'/user/{onion.onion_bob.id}/notification',
                f'/user/{NOBODY}/notification')
    assert not onion.onion_bob.notify_new_posts(onion.first_bob.id)


# --- M6: an account's alternate name ------------------------------------------------------------------------------

@pytest.mark.parametrize('sub', ['', '/report', '/block', '/upvotes'])
def test_an_onion_accounts_alternate_name_answers_as_a_name_nobody_holds(app, onion, sub):
    onion.onion_bob.alt_user_name = 'quietotter'
    db.session.commit()
    method = 'POST' if sub == '/block' else 'GET'
    assert_same(app, onion.first_bob, method, f'/u/quietotter{sub}', f'/u/loudbadger{sub}')


# --- M10: junk in an argument -------------------------------------------------------------------------------------

@pytest.mark.parametrize('path', ['/u/%00', '/u/%00/upvotes', '/u/bob%00@x/report', '/c/%00', '/community/%00/moderate',
                                  '/community/%00/feed', f'/community/lookup/%00/{ONION}', '/community/lookup/art/%00',
                                  '/user/lookup/%00/x', '/user/lookup/bob/a%00b', '/u/bob@[::/upvotes',
                                  '/community/lookup/art/[::', '/c/a@b@c'])
@pytest.mark.parametrize('signed_in', [False, True])
def test_junk_in_an_object_argument_is_not_a_server_error(app, onion, path, signed_in):
    response = visit(app, onion.first_bob, 'GET', path) if signed_in else get(app, path)
    assert response.status_code in (302, 400, 404), (path, response.status_code)


# --- C2: a conversation with an onion account -----------------------------------------------------------------------

@pytest.fixture
def talking(onion):
    """The first domain's bob talks with the onion's bob (from the onion: nowhere else can he be seen) and with the first
    domain's admin."""
    onion.with_onion = Conversation(user_id=onion.first_bob.id)
    onion.with_onion.members.extend([onion.first_bob, onion.onion_bob])
    onion.with_admin = Conversation(user_id=onion.first_bob.id)
    onion.with_admin.members.extend([onion.first_bob, onion.admin])
    db.session.add_all([onion.with_onion, onion.with_admin])
    db.session.commit()
    for conversation, sender, recipient, text in ((onion.with_onion, onion.onion_bob, onion.first_bob, 'ONIONCHAT'),
                                                  (onion.with_admin, onion.admin, onion.first_bob, 'FIRSTCHAT')):
        db.session.add(ChatMessage(conversation_id=conversation.id, sender_id=sender.id, recipient_id=recipient.id,
                                   body=text, body_html=f'<p>{text}</p>'))
    onion.with_onion.updated_at = utcnow()   # the newest: /chat opens it first
    db.session.commit()
    return onion


def test_the_chat_lists_no_conversation_with_an_onion_account_on_the_first_domain(app, talking, monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # the reply form renders its token
    response = visit(app, talking.first_bob, 'GET', '/chat')
    assert response.status_code == 302 and response.headers['Location'].endswith(f'/chat/{talking.with_admin.id}')
    body = visit(app, talking.first_bob, 'GET', f'/chat/{talking.with_admin.id}').get_data(as_text=True)
    assert 'FIRSTCHAT' in body and 'ONIONCHAT' not in body and ONION not in body
    assert f'/chat/{talking.with_onion.id}' not in body
    assert_same(app, talking.first_bob, 'GET', f'/chat/{talking.with_onion.id}', f'/chat/{NOBODY}')


def test_the_apis_messages_leave_out_a_conversation_with_an_onion_account_on_the_first_domain(app, talking,
                                                                                             monkeypatch):
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    body = api(app, '/api/alpha/private_message/list', talking.first_bob).get_data(as_text=True)
    assert 'FIRSTCHAT' in body and 'ONIONCHAT' not in body
    found = api(app, f'/api/alpha/private_message/conversation?conversation_id={talking.with_onion.id}',
                talking.first_bob)
    absent = api(app, f'/api/alpha/private_message/conversation?conversation_id={NOBODY}', talking.first_bob)
    assert (found.status_code, found.get_data()) == (absent.status_code, absent.get_data())
    found = api(app, f'/api/alpha/private_message/conversation?person_id={talking.onion_bob.id}', talking.first_bob)
    assert 'ONIONCHAT' not in found.get_data(as_text=True)


def test_on_the_onion_the_conversation_is_listed(app, talking, monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # the reply form renders its token
    body = visit(app, talking.first_bob, 'GET', f'/chat/{talking.with_onion.id}', host=ONION).get_data(as_text=True)
    assert 'ONIONCHAT' in body


def test_the_api_sends_no_message_to_an_onion_account_on_the_first_domain(app, talking, monkeypatch):
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    answers = []
    for recipient in (talking.onion_bob.id, NOBODY):
        forget_login()
        answers.append(app.test_client().post('/api/alpha/private_message', base_url=f'https://{FIRST}',
                                              json={'recipient_id': recipient, 'content': 'hello'},
                                              headers={'Authorization': f'Bearer {talking.first_bob.encode_jwt_token()}'}))
    assert (answers[0].status_code, answers[0].get_data()) == (answers[1].status_code, answers[1].get_data())
    assert ChatMessage.query.filter_by(recipient_id=talking.onion_bob.id).count() == 0


def test_the_apis_subscribe_answers_an_onion_account_as_an_id_nobody_holds(app, onion, monkeypatch):
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    answers = []
    for person in (onion.onion_bob.id, NOBODY):
        forget_login()
        answers.append(app.test_client().put('/api/alpha/user/subscribe', base_url=f'https://{FIRST}',
                                             json={'person_id': person, 'subscribe': True},
                                             headers={'Authorization': f'Bearer {onion.first_bob.encode_jwt_token()}'}))
    assert (answers[0].status_code, answers[0].get_data()) == (answers[1].status_code, answers[1].get_data())
    assert not onion.onion_bob.notify_new_posts(onion.first_bob.id)


# --- the gate and the views read a name alike (security scan) ------------------------------------------------------

@pytest.mark.parametrize('spelling', ['QuietOtter', 'QUIETOTTER', 'quietotter%20', '%20quietotter', 'quietotter%09'])
@pytest.mark.parametrize('sub', ['', '/report', '/upvotes'])
def test_an_alternate_name_in_any_case_or_spacing_answers_as_a_name_nobody_holds(app, onion, spelling, sub):
    onion.onion_bob.alt_user_name = 'quietotter'
    db.session.commit()
    assert_same(app, onion.first_bob, 'GET', f'/u/{spelling}{sub}', f'/u/loudbadger{sub}')


@pytest.mark.parametrize('hidden, missing', [
    (f'/u/BOB@{ONION.upper()}', f'/u/NOBODY@{ONION.upper()}'),
    (f'/u/bob@{ONION}%20', f'/u/nobody@{ONION}%20'),
    (f'/u/%20bob@{ONION}', f'/u/%20nobody@{ONION}'),
    (f'/c/ART@{ONION.upper()}', f'/c/NOPE@{ONION.upper()}'),
    (f'/c/art@{ONION}%20', f'/c/nope@{ONION}%20'),
    (f'/community/Art@{ONION}./moderate', f'/community/nope@{ONION}./moderate'),
    (f'/user/lookup/BOB/{ONION.upper()}', f'/user/lookup/NOBODY/{ONION.upper()}'),
    (f'/user/lookup/bob/{ONION}%20', f'/user/lookup/nobody/{ONION}%20'),
    (f'/community/lookup/art/{ONION.upper()}:80', f'/community/lookup/nope/{ONION.upper()}:80'),
    (f'/feed/lookup/news/{ONION.upper()}', f'/feed/lookup/nothing/{ONION.upper()}'),
])
def test_a_name_on_the_onion_in_any_case_or_spacing_answers_as_a_name_nobody_holds(app, onion, hidden, missing):
    assert_same(app, onion.first_bob, 'GET', hidden, missing)


# --- N-m7: a NUL in any string argument; N-m2: an alternate name that is also an account's name --------------------

@pytest.mark.parametrize('path', ['/f/%00/submit', '/topic/%00/submit', '/tag/%00', '/f/a%00b', '/topic/a%00b',
                                  '/feed/lookup/%00/x'])
@pytest.mark.parametrize('signed_in', [False, True])
def test_a_nul_in_any_string_argument_is_not_a_server_error(app, onion, path, signed_in):
    response = visit(app, onion.first_bob, 'GET', path) if signed_in else get(app, path)
    assert response.status_code in (302, 400, 404), (path, response.status_code)


def test_an_accounts_own_name_wins_over_an_onion_accounts_alternate_name(app, onion):
    onion.onion_bob.alt_user_name = 'bob'   # the first domain's bob's own name
    db.session.commit()
    response = visit(app, onion.first_bob, 'GET', '/u/bob')
    assert response.status_code == 200 and ONION not in response.get_data(as_text=True)


def test_a_message_goes_with_its_conversation_for_the_gate_the_finder_and_the_batch(app, talking):
    """m4: the gate asked about the sender and recipient, the finder and the batch about the conversation's members.
    In a group conversation with an onion member, a message between two clearnet accounts is hidden by all three."""
    group = Conversation(user_id=talking.first_bob.id)
    group.members.extend([talking.first_bob, talking.admin, talking.onion_bob])
    db.session.add(group)
    db.session.commit()
    message = ChatMessage(conversation_id=group.id, sender_id=talking.admin.id, recipient_id=talking.first_bob.id,
                          body='GROUP', body_html='<p>GROUP</p>')
    db.session.add(message)
    db.session.commit()
    rule = '/private_message/<int:message_id>'
    notice = type('Notice', (), {'url': f'/private_message/{message.id}', 'targets': {}, 'author_id': None})()
    with default_request(app):
        assert (_value_hidden('message_id', message.id, rule), found_here(ChatMessage, message.id),
                hidden_notifications([notice])) == (True, None, [True])
    with onion_request(app):
        assert not _value_hidden('message_id', message.id, rule)


def test_a_message_whose_conversation_cannot_be_read_is_hidden_wherever_anything_is(app, talking):
    """N4 (re-review 3): a message with no conversation (a legacy row) was shown everywhere; it fails closed, as a
    reply whose ancestry cannot be read does (N-m3)."""
    message = ChatMessage(conversation_id=None, sender_id=talking.admin.id, recipient_id=talking.first_bob.id,
                          body='LEGACY', body_html='<p>LEGACY</p>')
    db.session.add(message)
    db.session.commit()
    rule = '/private_message/<int:message_id>'
    notice = type('Notice', (), {'url': f'/private_message/{message.id}', 'targets': {}, 'author_id': None})()
    with default_request(app):
        assert (_value_hidden('message_id', message.id, rule), found_here(ChatMessage, message.id),
                hidden_notifications([notice])) == (True, None, [True])
        with pytest.raises(NoResultFound):
            one_here(ChatMessage.query.filter_by(id=message.id), ChatMessage)
        assert ChatMessage.query.filter_by(id=message.id).filter(hidden_message_clause()).count() == 1
    with onion_request(app):   # nothing is hidden on the onion
        assert found_here(ChatMessage, message.id) is not None
