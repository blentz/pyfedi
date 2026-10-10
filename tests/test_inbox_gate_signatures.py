"""The inbox gate's signature handling (app/activitypub/routes.py).

Every signature below is produced by HttpSignature.signed_request and checked
by HttpSignature.verify_request -- production's own code on both sides. No
test in this file patches either. A test that stubbed the verifier would
assert only that the stub was called, which is the failure mode this campaign
has already recorded once in another repository's suite.

The `signing_peer` fixture this file uses lives in tests/conftest.py -- a
later sub-project task needs it from a different test module, so it is shared
rather than local to this one. The `no_network_ld_signing` fixture described
below, the two frozen `@context` documents behind it, and the
`unsigned_but_precheck_clean_headers` / `ld_signed_body` recipes now live
there for the same reason: all three inbox-gate modules use them.

TASK 6 -- signature verification, the LD fallback, and the fediseer exemption
===============================================================================
Source order (routes.py:714-739), all AFTER Task 5's precheck/Delete-shortcut/
actor-resolution outcomes and all reached only once an actor has been
resolved:

  714-716  `HttpSignature.verify_request(request, actor.public_key,
           skip_date=True)` -- the cryptographic HTTP signature check. Success
           (bounced stays False) falls through to dispatch; the existing test
           above pins that arm.
  717-724  On failure (`VerificationError`, which `VerificationFormatError`
           subclasses -- so a missing/malformed Signature header and a
           mismatched key both land here), `bounced` is set True and, if the
           request body carries a `signature` key, `LDSignature.
           verify_signature` is tried as a fallback. Its own failure ->
           ('', 400), logged 'Could not verify LD signature: ' + str(e).
  726-731  Its success has NO return statement -- exactly like the fediseer
           branch below, it falls out of the `except` block and continues
           into the shared instance bookkeeping and dispatch, with `bounced`
           already True from line 718.
  731      The fediseer exemption: `elif (actor.ap_profile_id ==
           'https://fediseer.com/api/v1/user/fediseer' and ... type == Create
           ... object type == ChatMessage): ...`. The branch body is a
           LITERAL `...` (an Ellipsis expression statement) -- it does
           nothing at all but decline to `return`, so nothing is logged on
           this path either; falling out of the `elif` is the entire effect,
           and what happens next is exactly what happens after a successful
           LD-signature fallback: shared bookkeeping, then dispatch.
  737-739  Any other failure (no `signature` key and the fediseer condition
           does not hold) -> ('', 400), logged 'Could not verify HTTP
           signature: ' + str(e). This is the arm a plain wrong-key mismatch
           reaches, since a request produced by `signed_inbox_post` always
           carries a genuine Signature header and no `signature` body key.

Producing a valid LD signature -- checked, not assumed
-------------------------------------------------------------------------
`LDSignature` DOES have a signing counterpart in this codebase:
`LDSignature.create_signature(document, private_key, key_id)`
(app/activitypub/signature.py, used for real -- if unreachably, behind an
early `return` -- in app/main/routes.py:746). It returns the same shape
`verify_signature` consumes (`creator`, `created`, `signatureValue`, `type`,
plus `@context`), computed via the SAME `normalized_hash` both directions
share. So the valid-LD-signature arm (Step 3b) does not need to be reported
as unproducable: it was built and run successfully with production's own
`create_signature`/`verify_signature` pair, confirmed by a standalone probe
inside the test-runner container before it was written into a test:

    priv, pub = RsaKeys.generate_keypair()
    doc = {... 'id', 'type', 'actor', 'object', '@context': [activitystreams,
           security/v1] ...}
    ld = LDSignature.create_signature(doc, priv, key_id)
    doc['signature'] = ld
    LDSignature.verify_signature(doc, pub)   # -> returns cleanly

One thing that probe surfaced, and got this file's first review round wrong:
`normalized_hash` calls `pyld.jsonld.normalize`, which resolves the two
`@context` URLs (`https://www.w3.org/ns/activitystreams`,
`https://w3id.org/security/v1`) through pyld's DEFAULT document loader --
`pyld/jsonld.py`'s `_default_document_loader = requests_document_loader()`,
which uses the `requests` library, NOT `httpx`. `block_outbound_http`
(tests/conftest.py) blocks only httpx traffic; its docstring names exactly
three known unblocked gaps (urllib for app/nntp/, botocore/urllib3 for S3,
smtplib) and does not mention `requests` at all -- so pyld's document loader
is a FOURTH, previously undocumented instance, not another case of an
already-known one. The first version of this file called that "documented"
and left the two LD tests making real outbound HTTPS calls on every run,
which review correctly rejected: a unit test that reaches the public
internet is slow, fails offline, fails in CI without egress, and couples
this suite to a third party's uptime -- exactly what this file otherwise
guards against.

The fix uses the SAME override point `app/main/routes.py:744`'s dead demo
code already uses in this codebase, for the opposite purpose (pointing pyld
AT the network on purpose): `jsonld.set_document_loader`. The
`no_network_ld_signing` fixture (tests/conftest.py, moved there from this
file after a whole-branch review objected to the cross-file fixture import it
had grown in tests/test_inbox_gate_dispatch.py) installs a loader backed by the two
context documents' content, fetched once from the real URLs and frozen
verbatim into app/activitypub/ld_contexts.py, which production serves too (not
synthesized -- URDNA2015 normalization has to see exactly what a real
request would return, or the normalized hash a genuinely-invalid-signature
test relies on being wrong would silently be computed over the wrong
document instead), and restores whatever loader pyld had beforehand once the
test ends. This is a pyld-level configuration override, not a patch of
`LDSignature.verify_signature` or `HttpSignature.verify_request` -- the
normalization math and the signature math are both still production's own;
only where the two context DOCUMENTS come from changes.

Verified to add no false confidence, two ways, both in the fixture itself:
it records every URL the loader is asked to resolve (asserted, per test, to
be exactly `{activitystreams, security-v1}` -- proving the static loader is
what actually ran, not that normalization was skipped somehow), and it
monkeypatches `requests.get` to raise `AssertionError` if called at all
during the test -- proving no request reached the real
`pyld.documentloader.requests.requests_document_loader`'s `requests.get(...)`
call site (confirmed by reading that module's source), which is the only
place in this dependency chain that would reach the network. Both LD tests
passed with `no_network_ld_signing` active and zero calls to the guarded
`requests.get`.

Producing an invalid LD signature (Step 3a) uses the same `create_signature`
call, but with a SECOND keypair's private half (`RsaKeys.generate_keypair()`)
-- so the resulting `signatureValue` does not verify against the actor's real
public key, and `LDSignature.verify_signature` raises `VerificationError
("Signature mismatch")` exactly as it does for a genuinely forged document.

Both LD-signature requests (3a, 3b) are built by hand rather than through
`signed_inbox_post`, and deliberately have NO `Signature` HTTP header at all:
`signed_inbox_post` always produces a valid one, which would make
`HttpSignature.verify_request` succeed and the LD fallback would never be
reached. A correct `Digest` and fresh `Date` are still supplied (computed the
same way `signed_inbox_post` computes them internally) so the request clears
`precheck` on the way to the branch this task is about, exactly as
test_a_delete_of_an_unknown_account_needs_no_signature and
test_an_unresolvable_actor_is_refused (test_inbox_gate_refusals.py) do for
the same reason.

The fediseer exemption (Step 4) needs an actor row this suite has no factory
for -- `ap_profile_id` exactly 'https://fediseer.com/api/v1/user/fediseer'
-- built here with `make_instance`/`make_user` plus the same `ap_fetched_at`
stamp `signing_peer` uses, so `find_actor_or_create_cached` resolves it as a
pure database read (no actor fetch). The activity is UNSIGNED (no Signature
header, no `signature` body key either), which is the case the elif is
FOR: fediseer's real sender apparently cannot produce an HTTP or LD signature
PieFed accepts, so this is the one arm in the whole gate that accepts a
completely bare request, gated only on which actor sent it and what shape the
activity is.

What `fediseer` does NOT do, verified against a hypothesis to avoid getting
it backwards: the exemption reads the ALREADY-RESOLVED `actor` row's
`ap_profile_id`, not the raw `request_json['actor']` string -- so it cannot
be reached by an unresolvable actor URL that merely happens to end in
'/fediseer' (find_actor_or_create_cached would return None for that and the
gate would refuse two branches earlier, at routes.py:703-708, before this
code is ever reached). The test below resolves a REAL row with that exact
identity instead.

A second test right after it sends the IDENTICAL activity shape (unsigned
Create of a ChatMessage) from `signing_peer` instead of the fediseer row --
this is the negative half review's second round asked for, added after
Step 5's mutation run showed the positive test alone cannot tell "the
exemption is scoped to one actor" apart from "the exemption is scoped to
this activity shape, for anyone". Both tests together pin the `and` in the
`elif`'s condition, not just the shape check on its own.

Mutation (Step 5), applied one at a time to app/activitypub/routes.py, this
file's tests run against each mutant, then `git checkout --
app/activitypub/routes.py` before the next:

  1. `bounced = True` (line 718, the top of the `except VerificationError`
     block) forced to `bounced = False`.
  2. The entire `if 'signature' in request_json: try: ... except
     VerificationError as e: ... return '', 400` block deleted, with the
     following `elif` promoted to a bare `if` so the file still parses.
  3. The fediseer condition's actor test dropped: `actor.ap_profile_id ==
     'https://fediseer.com/api/v1/user/fediseer' and` removed from the
     `elif`, leaving only the Create/ChatMessage shape test.

Results and counts are recorded in this sub-project's task-6-report.md
rather than restated here, since this docstring is read before every test
run and a stale count baked into it would be easy to miss updating. (First
pass: mutants 1 and 2 killed, mutant 3 survived. After adding the negative
fediseer test above, mutant 3 was re-run and killed too -- see the report's
fix appendix for the re-run count.)

The fediseer branch's `...` body -- what it actually does
-------------------------------------------------------------------------
`...` on its own line, as a Python statement, is an `Ellipsis` expression
statement -- syntactically valid but semantically inert, exactly like `pass`
would be here. It does not return, does not log, does not touch `bounced`
(already True from line 718) or any other variable. Its only effect is
allowing control flow to fall out of the `except` block's `elif` chain with
no `return`, which is precisely the branch of Python's own semantics this
whole exemption depends on: `elif`/`else` with no matching branch executed
and no explicit `return` inside whichever branch DID execute both continue
execution at the first statement after the `try/except`, which is the shared
instance-bookkeeping code, followed by dispatch. It is not shorthand for
anything specific; it is precisely as inert as `pass`, chosen (guessing from
the surrounding comment, not confirmed by any commit message) probably
because the branch existed at this literal placeholder for a while before the
site operator's exemption request landed and simply never got a body written.
"""
import json

import pytest

from app import db
from app.activitypub.signature import HttpSignature, RsaKeys
from app.models import ActivityPubLog
from app.utils import utcnow
from tests.conftest import ld_signed_body, unsigned_but_precheck_clean_headers
from tests.factories import inbox_activity, make_instance, make_site, make_user, signed_inbox_post

pytestmark = pytest.mark.usefixtures('redis_double')


def test_a_genuinely_signed_activity_is_accepted(app, signing_peer, monkeypatch):
    """The spike this sub-project's feasibility rests on: a request signed by
    production's signer passes production's verifier through the test client.

    Asserts on the dispatch rather than on the 200, because a bare 200 is also
    what six refusal paths return.
    """
    dispatched = []
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request',
                        lambda *args, **kwargs: dispatched.append(args))
    monkeypatch.setitem(app.config, 'DEBUG', True)

    with app.test_client() as client:
        response = signed_inbox_post(client, inbox_activity(signing_peer), signing_peer)

    assert response.status_code == 200
    assert len(dispatched) == 1


def test_a_signature_verified_against_the_wrong_key_is_refused(app, signing_peer, monkeypatch):
    """routes.py:714-716/737-739: `HttpSignature.verify_request` checks the
    request's Signature header against `actor.public_key`, read fresh from
    the database (CACHE_TYPE=NullCache defeats find_actor_or_create_cached's
    memoized lookup, per test_a_disallowed_actor_is_refused_under_strong_
    allowlist's docstring in test_inbox_gate_refusals.py). Swapping the row's
    public key to a second, unrelated keypair's public half -- after the
    request has been signed with the FIRST keypair's private half -- makes
    that lookup return a key the signature was never made with.

    The swap happens after `signed_inbox_post` returns rather than between
    signing and sending, but the two orderings are observably identical:
    `HttpSignature.signed_request` (app/activitypub/signature.py:454) takes
    `sender.private_key` as a plain argument and never reads `sender.
    public_key` at all, so nothing about how the signature bytes are produced
    depends on what is currently stored in the actor row's public_key column.
    What matters is only that verify_request, running AFTER the request
    reaches the server, sees the swapped value -- which committing before
    signed_inbox_post's single call already guarantees.

    No `signature` body key is present (signed_inbox_post never adds one), so
    this refusal falls all the way through to the final else clause
    (routes.py:737-739) rather than the LD fallback this file covers below.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    monkeypatch.setitem(app.config, 'DEBUG', True)
    activity = inbox_activity(signing_peer)
    _wrong_private_key, wrong_public_key = RsaKeys.generate_keypair()
    signing_peer.public_key = wrong_public_key
    db.session.commit()

    with app.test_client() as client:
        response = signed_inbox_post(client, activity, signing_peer)

    assert response.status_code == 400
    assert ActivityPubLog.query.one().exception_message == 'Could not verify HTTP signature: Signature mismatch'


def test_a_body_substituted_after_signing_fails_precheck(app, signing_peer, monkeypatch):
    """`signed_inbox_post`'s `body=` escape hatch sends different bytes than
    were signed, so the Digest header (computed by `HttpSignature.
    signed_request` over the REAL activity) no longer matches what
    `HttpSignature.precheck` (app/activitypub/signature.py:380-395) hashes
    from the substituted body actually received.

    A second wrong instruction, caught the same way Task 5's precheck
    instruction was: this task's brief says to use `body=b'{"id":
    "tampered"}'` verbatim. That body is missing type/actor/object, so it
    never reaches precheck at all -- it is refused with `('', 200)` by the
    minimum-field check (routes.py:650), which runs BEFORE precheck
    (routes.py:687) in source order, exactly as test_inbox_gate_refusals.py's
    own Task 3 section documents. Tried literally first and confirmed to fail
    with `assert 200 == 400` before being corrected here. The substituted
    body below keeps all four required keys (with different VALUES, so the
    field check passes but the bytes still do not match what was signed) --
    the same shape test_a_tampered_body_fails_precheck in test_inbox_gate_
    refusals.py uses, though that one adds an extra key to a copy of the
    original activity rather than replacing every value; both land on the
    same branch (routes.py:687-691) for the same underlying reason (the
    bytes precheck hashes are not the bytes verify_request's Signature header
    was computed over), so the overlap between the two tests is expected, not
    an oversight.

    `HttpSignature.verify_request` (the arm this file's docstring says is
    never patched) is not reached on this path at all -- precheck runs
    first and fails before verify_request is ever called -- so this test's
    place in a file about signature verification is as the boundary case
    immediately before it, not a test of verify_request itself.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    monkeypatch.setitem(app.config, 'DEBUG', True)
    activity = inbox_activity(signing_peer)
    substituted_body = json.dumps({'id': 'https://tampered.example/activities/1', 'type': 'Like',
                                   'actor': activity['actor'], 'object': activity['object']}).encode('utf8')

    with app.test_client() as client:
        response = signed_inbox_post(client, activity, signing_peer, body=substituted_body)

    assert response.status_code == 400
    assert ActivityPubLog.query.one().exception_message == 'Precheck failed: Digest is incorrect'


def test_an_invalid_ld_signature_is_refused(app, signing_peer, monkeypatch, no_network_ld_signing):
    """routes.py:717-724: with no Signature header at all, `HttpSignature.
    verify_request` raises `VerificationFormatError` ("No signature header
    present") -- a `VerificationError` subclass, so it lands in the same
    `except` block a mismatched-key HTTP signature does. The activity body
    carries a `signature` key, so the LD fallback is attempted instead of
    falling straight to the generic 'Could not verify HTTP signature'
    message.

    The LD signature itself is genuinely invalid: `LDSignature.
    create_signature` (app/activitypub/signature.py) is called with a SECOND
    keypair's private half, unrelated to `signing_peer`'s. `LDSignature.
    verify_signature` (never patched, per this file's own module docstring)
    then genuinely fails against `actor.public_key`, which is `signing_peer`'s
    real, unmodified public key -- raising VerificationError('Signature
    mismatch'), confirmed against production's own code by a standalone probe
    before this test was written (see the module docstring's "Producing an
    invalid LD signature" section).

    `no_network_ld_signing` makes the normalization this needs resolve its
    `@context` documents locally rather than over the real internet -- see
    the module docstring's "Producing a valid LD signature" section for why
    that fixture exists and how it is verified to have actually run rather
    than merely not failed. The assertion on `resolved` below is that
    verification specifically for THIS test, not a repeat of the fixture's
    own docstring.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    wrong_private_key, _wrong_public_key = RsaKeys.generate_keypair()
    body_bytes = ld_signed_body(signing_peer, signing_key=wrong_private_key)

    with app.test_client() as client:
        response = client.post('/inbox', data=body_bytes,
                               headers=unsigned_but_precheck_clean_headers(body_bytes),
                               content_type='application/activity+json')

    assert response.status_code == 400
    assert ActivityPubLog.query.one().exception_message == 'Could not verify LD signature: Signature mismatch'
    assert set(no_network_ld_signing) == {'https://www.w3.org/ns/activitystreams', 'https://w3id.org/security/v1'}


def test_a_valid_ld_signature_is_accepted(app, signing_peer, monkeypatch, no_network_ld_signing):
    """routes.py:717-731: the same no-Signature-header setup as the invalid
    case above, but the LD signature is made with `signing_peer`'s OWN
    private key -- the one whose public half is genuinely on the actor row --
    so `LDSignature.verify_signature` succeeds. Line 726-731 (the success
    path) has no return statement, so control falls out of the `except`
    block straight into the shared instance bookkeeping and dispatch, exactly
    as the design's row-18 analysis (test_inbox_gate_refusals.py's module
    docstring, Task 2) says it does.

    Three things distinguish this from a coincidental 200 elsewhere in the
    gate, asserted together:

    - dispatch actually ran (`process_inbox_request` called once) -- several
      OTHER outcomes in this gate also return bare 200 without dispatching;
    - NO ActivityPubLog row exists -- nothing on this success path calls
      log_incoming_ap, unlike every refusal in this file;
    - `signing_peer.instance.ip_address` was blanked to '' -- routes.py:746
      does this precisely when `bounced` is True, which line 718 sets
      unconditionally on entry to the except block this whole test lives
      inside, REGARDLESS of which of the two fallbacks (LD or fediseer)
      ultimately let the request through. A production change that reset
      `bounced` back to False anywhere on this path would flip this specific
      assertion without changing the response at all.

    `no_network_ld_signing` (see its own docstring and the module docstring's
    "Producing a valid LD signature" section) keeps this test off the real
    internet; `resolved` is asserted to hold exactly the two context URLs so
    the static loader is shown to have actually run the normalization this
    test depends on, not merely to have gone unused while something else
    made it pass.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    monkeypatch.setitem(app.config, 'DEBUG', True)
    dispatched = []
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request',
                        lambda *args, **kwargs: dispatched.append(args))
    body_bytes = ld_signed_body(signing_peer)

    with app.test_client() as client:
        response = client.post('/inbox', data=body_bytes,
                               headers=unsigned_but_precheck_clean_headers(body_bytes),
                               content_type='application/activity+json')

    assert response.status_code == 200
    assert len(dispatched) == 1
    assert ActivityPubLog.query.count() == 0
    assert signing_peer.instance.ip_address == ''
    assert set(no_network_ld_signing) == {'https://www.w3.org/ns/activitystreams', 'https://w3id.org/security/v1'}


def test_an_unsigned_fediseer_chat_message_is_exempted(app, db_session, monkeypatch):
    """routes.py:717-731: with NEITHER a Signature header NOR a `signature`
    body key, every other actor falls to the generic 'Could not verify HTTP
    signature' refusal (see test_a_signature_verified_against_the_wrong_key_
    is_refused above) -- except this one, exact actor identity, sending this
    one exact activity shape. No factory in tests/factories.py builds an
    actor with this `ap_profile_id`, so it is constructed here directly:
    `make_instance`/`make_user` give it a row, then `ap_profile_id` is
    overwritten to the literal string the source compares against, and
    `ap_fetched_at` is stamped to now so `find_actor_or_create_cached`
    resolves it as a pure database read (the same reason signing_peer's own
    fixture does this, tests/conftest.py).

    The exemption reads the RESOLVED actor's `ap_profile_id`, confirmed by
    reading routes.py:727 rather than assumed -- so this test proves the
    identity check is about the database row, not about the raw
    `request_json['actor']` string coincidentally containing 'fediseer'.

    Like the valid-LD-signature test above, three assertions together pin
    this to the exemption specifically rather than to any other 200 outcome:
    dispatch ran once, no ActivityPubLog row was written (the `...` body logs
    nothing, see the module docstring's closing section), and `bounced`'s
    unconditional True (line 718) blanked the instance's ip_address.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    monkeypatch.setitem(app.config, 'DEBUG', True)
    dispatched = []
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request',
                        lambda *args, **kwargs: dispatched.append(args))
    make_site()
    instance = make_instance('fediseer.com')
    fediseer = make_user(instance, 'fediseer')
    fediseer.ap_profile_id = 'https://fediseer.com/api/v1/user/fediseer'
    fediseer.ap_public_url = fediseer.ap_profile_id
    fediseer.ap_inbox_url = f'{fediseer.ap_profile_id}/inbox'
    fediseer.ap_fetched_at = utcnow()
    db.session.commit()
    activity = inbox_activity(fediseer, activity_type='Create',
                              object={'id': f'{fediseer.ap_profile_id}/objects/1', 'type': 'ChatMessage'})
    body_bytes = json.dumps(activity).encode('utf8')

    with app.test_client() as client:
        response = client.post('/inbox', data=body_bytes,
                               headers=unsigned_but_precheck_clean_headers(body_bytes),
                               content_type='application/activity+json')

    assert response.status_code == 200
    assert len(dispatched) == 1
    assert ActivityPubLog.query.count() == 0
    assert fediseer.instance.ip_address == ''


def test_an_unsigned_chat_message_from_a_non_fediseer_actor_is_refused(app, signing_peer, monkeypatch):
    """routes.py:726-731: the fediseer `elif` is an `and` of TWO conditions --
    the resolved actor's `ap_profile_id` AND the Create/ChatMessage shape.
    The test above only ever sends that shape from the one actor that
    satisfies both, so on its own it cannot tell "this exemption is scoped to
    the fediseer identity" apart from "this exemption is scoped to any sender
    of this shape" -- a mutant that deletes the `actor.ap_profile_id == ... and`
    clause (Step 5's mutant 3) still passes it, because the surviving
    Create/ChatMessage check is still true for that one actor either way.

    `signing_peer` sends the IDENTICAL activity shape (unsigned Create of a
    dict object typed ChatMessage) the fediseer test above sends, so the only
    variable between the two tests is which actor sent it. Under real
    production code this still falls to the generic `else` (routes.py:737-739)
    because `signing_peer.ap_profile_id` is 'https://peer.example/users/alice',
    not the fediseer literal -- refused with the same message a plain
    unsigned request from ANY non-exempt actor gets.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    activity = inbox_activity(signing_peer, activity_type='Create',
                              object={'id': f'{signing_peer.ap_profile_id}/objects/1', 'type': 'ChatMessage'})
    body_bytes = json.dumps(activity).encode('utf8')

    with app.test_client() as client:
        response = client.post('/inbox', data=body_bytes,
                               headers=unsigned_but_precheck_clean_headers(body_bytes),
                               content_type='application/activity+json')

    assert response.status_code == 400
    assert ActivityPubLog.query.one().exception_message == 'Could not verify HTTP signature: No signature header present'


def test_an_ld_signature_whose_context_cannot_be_fetched_is_retryable(app, signing_peer, monkeypatch,
                                                                      no_network_ld_signing):
    """An LD-signed activity naming a context with no bundled copy, sent while
    that context's host is unreachable: the inbox answers 503 so the sender
    retries later, logs the failure, and does not dispatch. Previously pyld's
    JsonLdError escaped shared_inbox as an unhandled 500.
    """
    import requests
    from pyld import jsonld
    from app.activitypub.ld_contexts import ld_document_loader

    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    dispatched = []
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request',
                        lambda *args, **kwargs: dispatched.append(args))
    activity = json.loads(ld_signed_body(signing_peer))
    activity['@context'].append('https://unreachable.example/ns')
    body_bytes = json.dumps(activity).encode('utf8')

    # Signing needed the fixture's static loader; verifying uses production's,
    # with the network down the way w3id.org was in the original traceback.
    jsonld.set_document_loader(ld_document_loader)

    def _refuse(self, method, url, *args, **kwargs):
        raise requests.exceptions.ConnectionError('Remote end closed connection without response')

    monkeypatch.setattr(requests.Session, 'request', _refuse)

    with app.test_client() as client:
        response = client.post('/inbox', data=body_bytes,
                               headers=unsigned_but_precheck_clean_headers(body_bytes),
                               content_type='application/activity+json')

    assert response.status_code == 503
    assert dispatched == []
    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert 'unreachable.example' in log.exception_message
