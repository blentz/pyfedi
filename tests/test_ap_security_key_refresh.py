"""S2.3: on a failed verification, one signed refetch of the signer's key, rate-limited, then one retry."""
import json

import httpx
import pytest
from flask import request

from app import db
from app.activitypub.signature import (KEY_REFETCH_SECONDS, HttpSignature, VerificationError, _fetch_actor_key,
                                       request_key_id, verify_refreshing_key, verify_with_key_refresh)
from app.models import Site
from tests.factories import a_keypair, cavage_signed_headers, inbox_activity, make_instance, make_site, make_user

pytestmark = pytest.mark.usefixtures('redis_double')

OWNER = 'https://peer.example/users/alice'


def _two_keypairs():
    first = a_keypair()
    second = next(pair for pair in (a_keypair() for _ in range(16)) if pair[1] != first[1])
    return first, second


@pytest.fixture
def signing_site(app):
    site = db.session.get(Site, 1) or make_site()
    site.private_key, _ = a_keypair()
    db.session.commit()
    return site


class TestRequestKeyId:

    def test_reads_the_cavage_key_id(self, app):
        with app.test_request_context('/inbox', headers={'Signature': f'keyId="{OWNER}#main-key",signature="x"'}):
            assert request_key_id(request) == f'{OWNER}#main-key'

    def test_none_without_a_signature(self, app):
        with app.test_request_context('/inbox'):
            assert request_key_id(request) is None


class TestVerifyRefreshingKey:
    """The core, with real signatures and recording fetch/store callables."""

    def _run(self, app, headers, stored_key, *, owner_url=OWNER, fetched=None, rate_key='sig-refetch:test'):
        calls = {'fetch': 0, 'stored': []}

        def fetch_key():
            calls['fetch'] += 1
            return fetched

        with app.test_request_context('/inbox', method='POST', headers=headers):
            try:
                verify_refreshing_key(request, stored_key, owner_url=owner_url, rate_key=rate_key,
                                      fetch_key=fetch_key, store_key=calls['stored'].append)
                calls['result'] = 'verified'
            except VerificationError as e:
                calls['result'] = str(e)
        return calls

    def test_a_valid_stored_key_never_fetches(self, app):
        (private_key, public_key), _ = _two_keypairs()
        calls = self._run(app, cavage_signed_headers(private_key, f'{OWNER}#main-key'), public_key)
        assert calls == {'fetch': 0, 'stored': [], 'result': 'verified'}

    def test_a_rotated_key_is_fetched_stored_and_used(self, app):
        (private_key, public_key), (_, stale) = _two_keypairs()
        calls = self._run(app, cavage_signed_headers(private_key, f'{OWNER}#main-key'), stale, fetched=public_key)
        assert calls == {'fetch': 1, 'stored': [public_key], 'result': 'verified'}

    def test_a_gotosocial_style_key_id_is_eligible(self, app):
        """Review Focus 1: GoToSocial keyIds are a path under the actor, not a fragment."""
        (private_key, public_key), (_, stale) = _two_keypairs()
        calls = self._run(app, cavage_signed_headers(private_key, f'{OWNER}/main-key'), stale, fetched=public_key)
        assert calls['result'] == 'verified' and calls['fetch'] == 1

    def test_no_stored_key_goes_straight_to_a_fetch(self, app):
        (private_key, public_key), _ = _two_keypairs()
        calls = self._run(app, cavage_signed_headers(private_key, f'{OWNER}#main-key'), None, fetched=public_key)
        assert calls == {'fetch': 1, 'stored': [public_key], 'result': 'verified'}

    def test_an_unusable_stored_pem_goes_to_a_fetch(self, app):
        (private_key, public_key), _ = _two_keypairs()
        calls = self._run(app, cavage_signed_headers(private_key, f'{OWNER}#main-key'), 'not a pem', fetched=public_key)
        assert calls['result'] == 'verified'

    def test_a_key_id_on_another_host_never_fetches(self, app):
        (private_key, public_key), (_, stale) = _two_keypairs()
        calls = self._run(app, cavage_signed_headers(private_key, 'https://evil.example/actor#main-key'), stale,
                          fetched=public_key)
        assert calls['fetch'] == 0 and calls['result'] == 'Signature mismatch'

    def test_no_owner_never_fetches(self, app):
        (private_key, public_key), (_, stale) = _two_keypairs()
        calls = self._run(app, cavage_signed_headers(private_key, f'{OWNER}#main-key'), stale, owner_url=None,
                          fetched=public_key)
        assert calls['fetch'] == 0

    def test_an_unparseable_key_id_never_fetches(self, app):
        (_, public_key), (_, stale) = _two_keypairs()
        headers = {'Signature': 'keyId="not a url",headers="date",signature="YQ==",algorithm="rsa-sha256"'}
        calls = self._run(app, headers, stale, fetched=public_key)
        assert calls['fetch'] == 0

    def test_a_second_failure_inside_the_window_does_not_fetch(self, app, redis_double):
        (private_key, public_key), (_, stale) = _two_keypairs()
        headers = cavage_signed_headers(private_key, f'{OWNER}#main-key')
        self._run(app, headers, stale, fetched=stale)
        calls = self._run(app, headers, stale, fetched=public_key)
        assert calls['fetch'] == 0 and calls['result'] == 'Signature mismatch'
        assert 0 < redis_double.ttl('sig-refetch:test') <= KEY_REFETCH_SECONDS

    @pytest.mark.parametrize('fetched', [None, 'SAME'])
    def test_no_new_key_keeps_the_original_error(self, app, fetched):
        (private_key, _), (_, stale) = _two_keypairs()
        calls = self._run(app, cavage_signed_headers(private_key, f'{OWNER}#main-key'), stale,
                          fetched=stale if fetched == 'SAME' else None)
        assert calls['stored'] == [] and calls['result'] == 'Signature mismatch'

    def test_a_refetched_key_that_still_fails_is_refused_after_storing_it(self, app):
        (private_key, _), (_, other) = _two_keypairs()
        calls = self._run(app, cavage_signed_headers(private_key, f'{OWNER}#main-key'), 'not a pem', fetched=other)
        assert calls['stored'] == [other] and calls['result'] == 'Signature mismatch'

    def test_a_refetched_unusable_pem_is_a_verification_error(self, app):
        (private_key, _), (_, stale) = _two_keypairs()
        calls = self._run(app, cavage_signed_headers(private_key, f'{OWNER}#main-key'), stale, fetched='junk')
        assert calls['result'] == 'The refetched public key is not a usable PEM'


class TestFetchActorKey:

    def test_a_published_key_is_returned(self, app, http_mock, signing_site):
        http_mock.get(OWNER).respond(200, json={'id': OWNER, 'publicKey': {'publicKeyPem': 'PEM'}})
        assert _fetch_actor_key(OWNER) == 'PEM'

    @pytest.mark.parametrize('response', [httpx.Response(404), httpx.Response(200, content=b'not json'),
                                          httpx.Response(200, json={'id': OWNER})])
    def test_anything_else_is_none(self, app, http_mock, signing_site, response):
        http_mock.get(OWNER).mock(return_value=response)
        assert _fetch_actor_key(OWNER) is None

    def test_a_transport_failure_is_none(self, app, http_mock, signing_site):
        http_mock.get(OWNER).mock(side_effect=httpx.ConnectError('down'))
        assert _fetch_actor_key(OWNER) is None

    def test_no_site_key_is_none(self, app):
        site = db.session.get(Site, 1) or make_site()
        site.private_key = None
        db.session.commit()
        assert _fetch_actor_key(OWNER) is None


class TestThroughTheInbox:

    @pytest.fixture
    def dispatched(self, app, monkeypatch):
        monkeypatch.setitem(app.config, 'DEBUG', True)
        monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
        calls = []
        monkeypatch.setattr('app.activitypub.routes.process_inbox_request', lambda *a, **kw: calls.append(a))
        return calls

    @pytest.mark.parametrize('key_suffix', ['#main-key', '/main-key'])
    def test_a_rotated_key_is_accepted_after_one_refetch(self, app, signing_peer, http_mock, signing_site,
                                                         dispatched, key_suffix):
        (private_key, public_key), (_, stale) = _two_keypairs()
        signing_peer.private_key, signing_peer.public_key = private_key, stale
        db.session.commit()
        route = http_mock.get(signing_peer.ap_profile_id).respond(
            200, json={'id': signing_peer.ap_profile_id, 'publicKey': {'publicKeyPem': public_key}})
        activity = inbox_activity(signing_peer)
        _uri, headers, body = HttpSignature.signed_request(
            f"https://{app.config['SERVER_NAME']}/inbox", activity, private_key,
            f'{signing_peer.ap_profile_id}{key_suffix}', send_via_async=True)
        with app.test_client() as client:
            response = client.post('/inbox', data=body, headers=headers, content_type='application/activity+json')
        assert response.status_code == 200 and len(dispatched) == 1
        assert route.call_count == 1
        db.session.refresh(signing_peer)
        assert signing_peer.public_key == public_key

    def test_a_local_actor_is_never_refetched(self, app):
        """verify_with_key_refresh passes owner_url=None for a local actor."""
        (private_key, _), (_, other) = _two_keypairs()
        local = make_user(make_instance(app.config['SERVER_NAME'], software='piefed'), 'bob', local=True)
        local.public_key = other
        db.session.commit()
        headers = cavage_signed_headers(private_key, f"https://{app.config['SERVER_NAME']}/u/bob#main-key")
        with app.test_request_context('/inbox', method='POST', headers=headers):
            with pytest.raises(VerificationError, match='Signature mismatch'):
                verify_with_key_refresh(request, local)

    @pytest.mark.parametrize('state', ['dormant', 'gone_forever'])
    def test_an_actor_on_a_dead_instance_is_never_refetched(self, app, signing_peer, http_mock, signing_site, state):
        """R-DEADREFETCH: no fetch to an instance that is dormant or gone for good."""
        (private_key, _), (_, stale) = _two_keypairs()
        signing_peer.public_key = stale
        setattr(signing_peer.instance, state, True)
        db.session.commit()
        headers = cavage_signed_headers(private_key, f'{signing_peer.ap_profile_id}#main-key')
        with app.test_request_context('/inbox', method='POST', headers=headers):
            with pytest.raises(VerificationError, match='Signature mismatch'):
                verify_with_key_refresh(request, signing_peer)
        assert len(http_mock.calls) == 0
