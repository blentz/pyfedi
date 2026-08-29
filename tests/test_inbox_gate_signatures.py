"""The inbox gate's signature handling (app/activitypub/routes.py).

Every signature below is produced by HttpSignature.signed_request and checked
by HttpSignature.verify_request -- production's own code on both sides. No
test in this file patches either. A test that stubbed the verifier would
assert only that the stub was called, which is the failure mode this campaign
has already recorded once in another repository's suite.

The `signing_peer` fixture this file uses lives in tests/conftest.py -- a
later sub-project task needs it from a different test module, so it is shared
rather than local to this one.
"""

import pytest

from tests.factories import inbox_activity, signed_inbox_post

pytestmark = pytest.mark.usefixtures('redis_double')


def test_a_genuinely_signed_activity_is_accepted(app, signing_peer, monkeypatch):
    """The spike this sub-project's feasibility rests on: a request signed by
    production's signer passes production's verifier through the test client.

    Asserts on the dispatch rather than on the 200, because a bare 200 is also
    what six refusal paths return.
    """
    dispatched = []
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request',
                        lambda *args: dispatched.append(args))
    monkeypatch.setitem(app.config, 'DEBUG', True)

    with app.test_client() as client:
        response = signed_inbox_post(client, inbox_activity(signing_peer), signing_peer)

    assert response.status_code == 200
    assert len(dispatched) == 1
