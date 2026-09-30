"""Where a passkey login sends the visitor: `passkey_verification`'s `redirect`.

D1373. The route ended

    login_user(user, remember=True)
    redirect_to = redirect or '/'
    return jsonify({'verified': True, 'redirectTo': redirect_to})

with `redirect` taken straight from the posted body, and
`app/static/js/scripts.js:1375` does

    location.href = verificationJSON.redirectTo;

filling `redirect` from `?next=` on the login page (scripts.js:1336). So
`/auth/login?next=<anything>` decided where a visitor went the moment their
passkey verified. Measured -- every one of these came back as itself:

    https://evil.test/     //evil.test/x     ////evil.test
    \\\\evil.test/x          /\\evil.test       https:/\\evil.test
    http:evil.test         javascript:alert(1)

The last one is not an open redirect. `location.href = 'javascript:alert(1)'`
EXECUTES, in this site's origin, on a page whose session `login_user(...,
remember=True)` has just authenticated -- so a crafted login link ran script as
the victim, with their cookie.

WHY IT SURVIVED. The password arm of the same login form sends the same `?next=`
through `safe_redirect_target` and its comment says exactly why ("`?next=` is
attacker-supplied and this is the login flow, so it gets the same origin check as
every other user-influenced redirect target", app/auth/util.py:447). The passkey
arm was added later and checked nothing. One control, two paths, implemented on
one -- D1359's shape, on the login flow rather than the bot interstitial.

`is_safe_redirect_target` is still the only implementation: `safe_redirect_target`
defers to it, so this file asserts the route's answers AND that they agree with
that function, which is what catches a third implementation appearing.
"""
import pytest
from flask import current_app
from unittest.mock import patch

from app.utils import is_safe_redirect_target
from tests.test_passkeys import _passkey, _seed, authenticated

pytestmark = pytest.mark.usefixtures('site')


def verified(app, next_value, body=None):
    """Complete a passkey login carrying `next_value` and return the JSON.

    `verify_authentication_response` is patched because the assertion is bound to
    a real authenticator; the redirect is computed after it, which is the point.
    """
    instance, alice = _seed()
    _passkey(alice)
    client = app.test_client()
    payload = {'username': 'alice', 'response': {}}
    if next_value is not None:
        payload['redirect'] = next_value
    if body is not None:
        payload = body
    with patch('app.auth.passkeys.parse_authentication_credential_json',
               return_value='CRED'):
        with patch('app.auth.passkeys.verify_authentication_response',
                   return_value=authenticated()):
            response = client.post('/auth/passkeys/login_verification',
                                   json=payload)
    return response


HOSTILE = [
    'https://evil.test/',
    '//evil.test/x',
    '////evil.test',
    '\\\\evil.test/x',
    '/\\evil.test',
    'https:/\\evil.test',
    'http:evil.test',
    'javascript:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    'https://test.piefed.local.evil.test/',
    'https://test.piefed.local@evil.test/',
    'https://evil.test/?x=test.piefed.local',
]


class TestWhatWasServedAndIsNot:
    @pytest.mark.parametrize('next_value', HOSTILE)
    def test_the_visitor_goes_to_the_front_page_instead(self, app, db_session,
                                                        next_value):
        assert verified(app, next_value).get_json() == {'verified': True,
                                                        'redirectTo': '/'}

    @pytest.mark.parametrize('next_value', HOSTILE)
    def test_the_value_is_not_echoed_anywhere_in_the_answer(self, app, db_session,
                                                           next_value):
        """The status is not the assertion: what matters is that the string does
        not reach `location.href`."""
        response = verified(app, next_value)

        assert b'evil.test' not in response.data
        assert b'javascript:' not in response.data
        assert b'data:' not in response.data

    def test_the_login_still_succeeds(self, app, db_session):
        """A refused redirect must not refuse the login -- the passkey verified,
        and dropping the user back at the login page would read as a broken
        credential. `safe_redirect_target` replaces rather than raises."""
        response = verified(app, 'https://evil.test/')

        assert response.get_json()['verified'] is True


class TestWhatMustStillWork:
    @pytest.mark.parametrize('next_value', ['/', '/feed', '/c/news?page=2',
                                            '/post/1', '/search?q=%23tag',
                                            '/u/alice'])
    def test_a_relative_path_is_kept(self, app, db_session, next_value):
        assert verified(app, next_value).get_json()['redirectTo'] == next_value

    def test_this_servers_own_absolute_url_is_kept(self, app, db_session):
        host = current_app.config['SERVER_NAME']

        answer = verified(app, f'https://{host}/c/news').get_json()

        assert answer['redirectTo'] == f'https://{host}/c/news'

    @pytest.mark.parametrize('next_value', ['', None])
    def test_no_target_lands_on_the_front_page(self, app, db_session, next_value):
        """`?next=` absent is the ordinary case -- most logins have no target --
        and an empty string is what the query string gives when it is present and
        blank. Both were already '/' and must stay '/'."""
        assert verified(app, next_value).get_json()['redirectTo'] == '/'


class TestTheRouteAgreesWithTheCanonicalCheck:
    """The property that keeps a third implementation from appearing."""

    VALUES = HOSTILE + ['/', '/feed', '#frag', '?x=1', 'relative/path',
                        'https://test.piefed.local/x',
                        'http://test.piefed.local/x',
                        'ftp://evil.test/', 'http://[', 'http://[zzz]',
                        'https://test.piefed.local:8443/x',
                        'https://test.piefed.local\t/x']

    @pytest.mark.parametrize('next_value', VALUES)
    def test_the_two_answers_match(self, app, db_session, next_value):
        answer = verified(app, next_value).get_json()['redirectTo']

        with current_app.test_request_context():
            canonical = next_value if is_safe_redirect_target(next_value) else '/'

        assert answer == canonical, next_value


class TestTheBodyThisEndpointIsPosted:
    """D1373's other half. The route is unauthenticated and read four keys with
    `request_json[...]`, so a body missing any of them was a 500 -- on an auth
    endpoint anyone can reach."""

    @pytest.mark.parametrize('body', [
        {},
        {'username': 'alice'},
        {'username': 'alice', 'redirect': '/'},
        {'redirect': '/', 'response': {}},
        {'username': 'alice', 'response': {}},
    ])
    def test_a_body_missing_keys_is_answered_not_crashed(self, app, db_session,
                                                         body):
        """Each of these raised KeyError on one of the four reads. What the
        answer SAYS depends on the body -- a missing `redirect` is an ordinary
        login that lands on '/' -- so this asserts only that the endpoint answers
        rather than 500s, and that its answer is the JSON contract the client
        parses."""
        response = verified(app, None, body=body)

        assert response.status_code == 200
        assert 'verified' in response.get_json()

    def test_a_body_with_no_username_finds_nobody(self, app, db_session):
        """`username` reached three separate `request_json["username"]` reads and
        is one variable now. With no username the query must match no user rather
        than match one whose column happens to be NULL -- `User.user_name == None`
        is NULL in SQL and matches nothing, which is the behaviour relied on."""
        response = verified(app, None, body={'redirect': '/', 'response': {}})

        assert response.get_json()['verified'] is False

    @pytest.mark.parametrize('body', [[], 'alice', 5, True])
    def test_a_body_that_is_not_an_object_is_refused(self, app, db_session, body):
        """`get_json(force=True)` returns whatever parses, and `.get` on a list
        is an AttributeError -- another 500 on an unauthenticated endpoint."""
        response = verified(app, None, body=body)

        assert response.status_code == 400

    def test_an_unknown_user_is_answered_as_a_user_with_no_valid_passkeys(
            self, app, db_session):
        """The `username` variable replaced three re-reads of the same key, one
        of which built this message. The message must stay the one that does NOT
        distinguish "no such account" from "that account's passkeys all failed" --
        the enumeration property tests/test_passkeys.py pins for this endpoint,
        and the reason its wording differs from the options endpoint's."""
        unknown = verified(app, '/', body={'username': 'nobody',
                                           'redirect': '/', 'response': {}})

        assert unknown.get_json() == {
            'verified': False,
            'message': 'No valid passkeys found for nobody'}


class TestTheRegistrationTwin:
    """`user_passkey_verification` (app/user/passkeys.py) read `response` and
    `device` the same way, and wrote `device` into a String(50) column
    unbounded."""

    def _register(self, app, body):
        instance, alice = _seed()
        client = app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(alice.id)
            sess['_fresh'] = True
        with patch('app.user.passkeys.parse_registration_credential_json',
                   return_value='CRED'), \
                patch('app.user.passkeys.verify_registration_response') as verify:
            verify.return_value.credential_id = b'cred-id'
            verify.return_value.credential_public_key = b'rawkey'
            verify.return_value.sign_count = 0
            response = client.post('/user/passkeys/registration/verification',
                                   json=body)
        return alice, response

    def test_a_body_missing_keys_does_not_crash(self, app, db_session):
        alice, response = self._register(app, {})

        assert response.status_code == 200

    @pytest.mark.parametrize('body', [[], 'phone', 5])
    def test_a_body_that_is_not_an_object_is_refused(self, app, db_session, body):
        alice, response = self._register(app, body)

        assert response.status_code == 400

    def test_a_device_name_wider_than_the_column_is_cut(self, app, db_session):
        """String(50). Unbounded this was a DataError at the commit, so the
        passkey the user had just registered was lost with the request."""
        from app.models import Passkey

        alice, response = self._register(app, {'response': {},
                                               'device': 'd' * 300})

        stored = Passkey.query.filter_by(user_id=alice.id).one()
        assert stored.device == 'd' * 50

    def test_an_ordinary_device_name_is_stored_whole(self, app, db_session):
        from app.models import Passkey

        alice, response = self._register(app, {'response': {},
                                               'device': 'Pixel 9'})

        assert Passkey.query.filter_by(user_id=alice.id).one().device == 'Pixel 9'


class TestTheOptionsEndpointsBody:
    """`passkey_options`, the unauthenticated endpoint the login flow calls
    first. It read `request_json["username"]` three times, so the same body
    shapes were the same 500."""

    def _options(self, app, body):
        instance, alice = _seed()
        _passkey(alice)
        return app.test_client().post('/auth/passkeys/login_options', json=body)

    @pytest.mark.parametrize('body', [[], 'alice', 5, True])
    def test_a_body_that_is_not_an_object_is_refused(self, app, db_session, body):
        assert self._options(app, body).status_code == 400

    def test_a_body_with_no_username_is_answered(self, app, db_session):
        """Answered as any unknown name is since D888: options offering no
        credential, not an error naming the account."""
        response = self._options(app, {})

        assert response.status_code == 200
        assert response.get_json()['allowCredentials'] == []

    def test_a_known_user_still_gets_a_challenge(self, app, db_session):
        """The other direction: the shape test must not refuse the ordinary
        body, which is the only one the site's own JS sends."""
        response = self._options(app, {'username': 'alice'})

        assert response.status_code == 200
        assert 'challenge' in response.get_json()
