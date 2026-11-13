"""A passkey credential that is not one (fuzz-routes finding, 2026-11-13): POST /user/passkeys/registration/verification
handed the body's `response` to py_webauthn's parse_registration_credential_json, which raises InvalidJSONStructure (and
its kin) on anything that is not a credential, and nothing caught it: a 500. The login endpoint parsed its credential
the same way. A malformed credential is a malformed request: a 400, before any account is looked at, so the login
endpoint's answer still names no one."""
import pytest

from app import db
from tests.factories import make_instance, make_user
from tests.test_admin_site_profile import login

pytestmark = pytest.mark.usefixtures('site')

MALFORMED = [None, 5, 'not json', '[]', [], {}, {'id': 'x'},
             {'id': 'x', 'rawId': '!!', 'type': 'public-key', 'response': {}},
             {'id': 'eA', 'rawId': 'eA', 'type': 'public-key',
              'response': {'clientDataJSON': 5, 'attestationObject': [], 'authenticatorData': None, 'signature': {}}}]
IDS = ['null', 'number', 'text', 'json-list-text', 'list', 'empty', 'id-only', 'bad-rawid', 'bad-fields']


@pytest.mark.parametrize('credential', MALFORMED, ids=IDS)
def test_registering_a_malformed_credential_is_a_bad_request(app, db_session, credential):
    user = make_user(make_instance('test.piefed.local', software='piefed'), 'passkeyfuzz', local=True)
    user.verified = True
    db.session.commit()
    client = app.test_client()
    login(client, user)
    response = client.post('/user/passkeys/registration/verification', json={'response': credential, 'device': 'x'})
    assert response.status_code == 400


def test_a_device_name_that_is_not_text_is_a_bad_request(app, db_session):
    user = make_user(make_instance('test.piefed.local', software='piefed'), 'passkeydevice', local=True)
    user.verified = True
    db.session.commit()
    client = app.test_client()
    login(client, user)
    response = client.post('/user/passkeys/registration/verification', json={'response': {}, 'device': 12345})
    assert response.status_code == 400


@pytest.mark.parametrize('credential', MALFORMED, ids=IDS)
def test_logging_in_with_a_malformed_credential_is_a_bad_request(app, db_session, credential):
    response = app.test_client().post('/auth/passkeys/login_verification',
                                      json={'username': 'nobody', 'redirect': '/', 'response': credential})
    assert response.status_code == 400
