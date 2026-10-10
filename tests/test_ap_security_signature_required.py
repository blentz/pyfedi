"""S2.1: the headers a signature must cover (spec 2026-10-10 ap-security-track, phase S2).

Every signature here is made with production's key handling (tests.factories.cavage_signed_headers or
signed_inbox_post) and checked by the unpatched HttpSignature.verify_request.
"""
import json
import re

import pytest
from flask import request

from app.activitypub.signature import GET_REQUIRED, POST_REQUIRED, HttpSignature, VerificationError
from app.models import ActivityPubLog
from config import Config
from tests.factories import a_keypair, cavage_signed_headers, inbox_activity

pytestmark = pytest.mark.usefixtures('redis_double')

KEY_ID = 'https://peer.example/users/alice#main-key'


def _verify(app, headers, required, method='POST', public_key=None, body=b''):
    with app.test_request_context('/inbox', method=method, headers=headers, data=body):
        return HttpSignature.verify_request(request, public_key, required)


def test_the_flag_is_off_by_default():
    assert Config.SIG_REQUIRED_HEADERS_ENFORCE is False


def test_the_required_sets():
    assert POST_REQUIRED == frozenset({'(request-target)', 'host', 'date', 'digest'})
    assert GET_REQUIRED == frozenset({'(request-target)', 'host', 'date'})


def test_skip_date_is_gone(app):
    private_key, public_key = a_keypair()
    with app.test_request_context('/inbox', method='POST', headers=cavage_signed_headers(private_key, KEY_ID)):
        with pytest.raises(TypeError):
            HttpSignature.verify_request(request, public_key, skip_date=True)


@pytest.mark.parametrize('left_out', sorted(POST_REQUIRED))
def test_flag_off_a_missing_required_header_is_logged_and_accepted(app, caplog, monkeypatch, left_out):
    monkeypatch.setitem(app.config, 'SIG_REQUIRED_HEADERS_ENFORCE', False)
    private_key, public_key = a_keypair()
    headers = cavage_signed_headers(private_key, KEY_ID, signed=tuple(h for h in sorted(POST_REQUIRED) if h != left_out))
    with caplog.at_level('WARNING', logger=app.logger.name):
        assert _verify(app, headers, POST_REQUIRED, public_key=public_key) is True
    assert f'Signature from peer.example does not sign: {left_out}' in caplog.text


@pytest.mark.parametrize('left_out', sorted(POST_REQUIRED))
def test_flag_on_a_missing_required_header_is_refused(app, monkeypatch, left_out):
    monkeypatch.setitem(app.config, 'SIG_REQUIRED_HEADERS_ENFORCE', True)
    private_key, public_key = a_keypair()
    headers = cavage_signed_headers(private_key, KEY_ID, signed=tuple(h for h in sorted(POST_REQUIRED) if h != left_out))
    with pytest.raises(VerificationError, match=re.escape(f'unsigned required header: {left_out}')):
        _verify(app, headers, POST_REQUIRED, public_key=public_key)


def test_flag_on_a_get_missing_date_is_refused(app, monkeypatch):
    monkeypatch.setitem(app.config, 'SIG_REQUIRED_HEADERS_ENFORCE', True)
    private_key, public_key = a_keypair()
    headers = cavage_signed_headers(private_key, KEY_ID, method='get', signed=('(request-target)', 'host'))
    with pytest.raises(VerificationError, match='unsigned required header: date'):
        _verify(app, headers, GET_REQUIRED, method='GET', public_key=public_key)


def test_flag_on_every_required_header_signed_passes_without_a_warning(app, caplog, monkeypatch):
    monkeypatch.setitem(app.config, 'SIG_REQUIRED_HEADERS_ENFORCE', True)
    private_key, public_key = a_keypair()
    with caplog.at_level('WARNING', logger=app.logger.name):
        assert _verify(app, cavage_signed_headers(private_key, KEY_ID), POST_REQUIRED, public_key=public_key) is True
    assert 'does not sign' not in caplog.text


def test_no_required_set_keeps_the_old_behaviour(app, caplog, monkeypatch):
    monkeypatch.setitem(app.config, 'SIG_REQUIRED_HEADERS_ENFORCE', True)
    private_key, public_key = a_keypair()
    headers = cavage_signed_headers(private_key, KEY_ID, signed=('date',))
    with caplog.at_level('WARNING', logger=app.logger.name):
        assert _verify(app, headers, frozenset(), public_key=public_key) is True
    assert 'does not sign' not in caplog.text


def test_the_inbox_refuses_an_unsigned_digest_when_enforcing(app, signing_peer, monkeypatch):
    monkeypatch.setitem(app.config, 'SIG_REQUIRED_HEADERS_ENFORCE', True)
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    monkeypatch.setitem(app.config, 'DEBUG', True)
    dispatched = []
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request', lambda *a, **kw: dispatched.append(a))
    activity = inbox_activity(signing_peer)
    body = json.dumps(activity).encode('utf8')
    headers = cavage_signed_headers(signing_peer.private_key, f'{signing_peer.ap_profile_id}#main-key', body=body,
                                    signed=('(request-target)', 'host', 'date'))
    with app.test_client() as client:
        response = client.post('/inbox', data=body, headers=headers, content_type='application/activity+json')
    assert response.status_code == 400
    assert dispatched == []
    assert ActivityPubLog.query.one().exception_message == \
        'Could not verify HTTP signature: unsigned required header: digest'
