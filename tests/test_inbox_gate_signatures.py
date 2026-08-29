"""The inbox gate's signature handling (app/activitypub/routes.py).

Every signature below is produced by HttpSignature.signed_request and checked
by HttpSignature.verify_request -- production's own code on both sides. No
test in this file patches either. A test that stubbed the verifier would
assert only that the stub was called, which is the failure mode this campaign
has already recorded once in another repository's suite.

The `signing_peer` fixture this file uses lives in tests/conftest.py -- a
later sub-project task needs it from a different test module, so it is shared
rather than local to this one.

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
`no_network_ld_signing` fixture below installs a loader backed by the two
context documents' content, fetched once from the real URLs and frozen
verbatim into `_ACTIVITYSTREAMS_CONTEXT`/`_SECURITY_V1_CONTEXT` (not
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
from pyld import jsonld
from werkzeug.http import http_date

from app import db
from app.activitypub.signature import HttpSignature, LDSignature, RsaKeys, default_context
from app.models import ActivityPubLog
from app.utils import utcnow
from tests.factories import inbox_activity, make_instance, make_site, make_user, signed_inbox_post

pytestmark = pytest.mark.usefixtures('redis_double')


# Frozen, verbatim copies of the two JSON-LD context documents `LDSignature.
# normalized_hash` resolves via `pyld.jsonld.normalize` -- fetched once from
# the real URLs (`requests.get('https://www.w3.org/ns/activitystreams', ...)`
# / `.../security/v1`) and pasted in as-received, not hand-written, so
# URDNA2015 normalization sees exactly what production would see over the
# network. See the module docstring's "Producing a valid LD signature"
# section for why these exist: pyld's default document loader reaches the
# real internet through `requests`, which this suite's httpx-only network
# block does not cover, and a unit test should not depend on w3.org/w3id.org
# being reachable.
_ACTIVITYSTREAMS_CONTEXT = json.loads(
    '{"@context":{"@vocab":"_:","xsd":"http://www.w3.org/2001/XMLSchema#","as":"https://www.w3.org/ns/activitystreams#","ldp":"http://www.w3.org/ns/ldp#","vcard":"http://www.w3.org/2006/vcard/ns#","id":"@id","type":"@type","Accept":"as:Accept","Activity":"as:Activity","IntransitiveActivity":"as:IntransitiveActivity","Add":"as:Add","Announce":"as:Announce","Application":"as:Application","Arrive":"as:Arrive","Article":"as:Article","Audio":"as:Audio","Block":"as:Block","Collection":"as:Collection","CollectionPage":"as:CollectionPage","Relationship":"as:Relationship","Create":"as:Create","Delete":"as:Delete","Dislike":"as:Dislike","Document":"as:Document","Event":"as:Event","Follow":"as:Follow","Flag":"as:Flag","Group":"as:Group","Ignore":"as:Ignore","Image":"as:Image","Invite":"as:Invite","Join":"as:Join","Leave":"as:Leave","Like":"as:Like","Link":"as:Link","Mention":"as:Mention","Note":"as:Note","Object":"as:Object","Offer":"as:Offer","OrderedCollection":"as:OrderedCollection","OrderedCollectionPage":"as:OrderedCollectionPage","Organization":"as:Organization","Page":"as:Page","Person":"as:Person","Place":"as:Place","Profile":"as:Profile","Question":"as:Question","Reject":"as:Reject","Remove":"as:Remove","Service":"as:Service","TentativeAccept":"as:TentativeAccept","TentativeReject":"as:TentativeReject","Tombstone":"as:Tombstone","Undo":"as:Undo","Update":"as:Update","Video":"as:Video","View":"as:View","Listen":"as:Listen","Read":"as:Read","Move":"as:Move","Travel":"as:Travel","IsFollowing":"as:IsFollowing","IsFollowedBy":"as:IsFollowedBy","IsContact":"as:IsContact","IsMember":"as:IsMember","subject":{"@id":"as:subject","@type":"@id"},"relationship":{"@id":"as:relationship","@type":"@id"},"actor":{"@id":"as:actor","@type":"@id"},"attributedTo":{"@id":"as:attributedTo","@type":"@id"},"attachment":{"@id":"as:attachment","@type":"@id"},"bcc":{"@id":"as:bcc","@type":"@id"},"bto":{"@id":"as:bto","@type":"@id"},"cc":{"@id":"as:cc","@type":"@id"},"context":{"@id":"as:context","@type":"@id"},"current":{"@id":"as:current","@type":"@id"},"first":{"@id":"as:first","@type":"@id"},"generator":{"@id":"as:generator","@type":"@id"},"icon":{"@id":"as:icon","@type":"@id"},"image":{"@id":"as:image","@type":"@id"},"inReplyTo":{"@id":"as:inReplyTo","@type":"@id"},"items":{"@id":"as:items","@type":"@id"},"instrument":{"@id":"as:instrument","@type":"@id"},"orderedItems":{"@id":"as:items","@type":"@id","@container":"@list"},"last":{"@id":"as:last","@type":"@id"},"location":{"@id":"as:location","@type":"@id"},"next":{"@id":"as:next","@type":"@id"},"object":{"@id":"as:object","@type":"@id"},"oneOf":{"@id":"as:oneOf","@type":"@id"},"anyOf":{"@id":"as:anyOf","@type":"@id"},"closed":{"@id":"as:closed","@type":"xsd:dateTime"},"origin":{"@id":"as:origin","@type":"@id"},"accuracy":{"@id":"as:accuracy","@type":"xsd:float"},"prev":{"@id":"as:prev","@type":"@id"},"preview":{"@id":"as:preview","@type":"@id"},"replies":{"@id":"as:replies","@type":"@id"},"result":{"@id":"as:result","@type":"@id"},"audience":{"@id":"as:audience","@type":"@id"},"partOf":{"@id":"as:partOf","@type":"@id"},"tag":{"@id":"as:tag","@type":"@id"},"target":{"@id":"as:target","@type":"@id"},"to":{"@id":"as:to","@type":"@id"},"url":{"@id":"as:url","@type":"@id"},"altitude":{"@id":"as:altitude","@type":"xsd:float"},"content":"as:content","contentMap":{"@id":"as:content","@container":"@language"},"name":"as:name","nameMap":{"@id":"as:name","@container":"@language"},"duration":{"@id":"as:duration","@type":"xsd:duration"},"endTime":{"@id":"as:endTime","@type":"xsd:dateTime"},"height":{"@id":"as:height","@type":"xsd:nonNegativeInteger"},"href":{"@id":"as:href","@type":"@id"},"hreflang":"as:hreflang","latitude":{"@id":"as:latitude","@type":"xsd:float"},"longitude":{"@id":"as:longitude","@type":"xsd:float"},"mediaType":"as:mediaType","published":{"@id":"as:published","@type":"xsd:dateTime"},"radius":{"@id":"as:radius","@type":"xsd:float"},"rel":"as:rel","startIndex":{"@id":"as:startIndex","@type":"xsd:nonNegativeInteger"},"startTime":{"@id":"as:startTime","@type":"xsd:dateTime"},"summary":"as:summary","summaryMap":{"@id":"as:summary","@container":"@language"},"totalItems":{"@id":"as:totalItems","@type":"xsd:nonNegativeInteger"},"units":"as:units","updated":{"@id":"as:updated","@type":"xsd:dateTime"},"width":{"@id":"as:width","@type":"xsd:nonNegativeInteger"},"describes":{"@id":"as:describes","@type":"@id"},"formerType":{"@id":"as:formerType","@type":"@id"},"deleted":{"@id":"as:deleted","@type":"xsd:dateTime"},"inbox":{"@id":"ldp:inbox","@type":"@id"},"outbox":{"@id":"as:outbox","@type":"@id"},"following":{"@id":"as:following","@type":"@id"},"followers":{"@id":"as:followers","@type":"@id"},"streams":{"@id":"as:streams","@type":"@id"},"preferredUsername":"as:preferredUsername","endpoints":{"@id":"as:endpoints","@type":"@id"},"uploadMedia":{"@id":"as:uploadMedia","@type":"@id"},"proxyUrl":{"@id":"as:proxyUrl","@type":"@id"},"liked":{"@id":"as:liked","@type":"@id"},"oauthAuthorizationEndpoint":{"@id":"as:oauthAuthorizationEndpoint","@type":"@id"},"oauthTokenEndpoint":{"@id":"as:oauthTokenEndpoint","@type":"@id"},"provideClientKey":{"@id":"as:provideClientKey","@type":"@id"},"signClientKey":{"@id":"as:signClientKey","@type":"@id"},"sharedInbox":{"@id":"as:sharedInbox","@type":"@id"},"Public":{"@id":"as:Public","@type":"@id"},"source":"as:source","likes":{"@id":"as:likes","@type":"@id"},"shares":{"@id":"as:shares","@type":"@id"},"alsoKnownAs":{"@id":"as:alsoKnownAs","@type":"@id"}}}'
)
_SECURITY_V1_CONTEXT = json.loads(
    '{"@context":{"id":"@id","type":"@type","dc":"http://purl.org/dc/terms/","sec":"https://w3id.org/security#","xsd":"http://www.w3.org/2001/XMLSchema#","EcdsaKoblitzSignature2016":"sec:EcdsaKoblitzSignature2016","Ed25519Signature2018":"sec:Ed25519Signature2018","EncryptedMessage":"sec:EncryptedMessage","GraphSignature2012":"sec:GraphSignature2012","LinkedDataSignature2015":"sec:LinkedDataSignature2015","LinkedDataSignature2016":"sec:LinkedDataSignature2016","CryptographicKey":"sec:Key","authenticationTag":"sec:authenticationTag","canonicalizationAlgorithm":"sec:canonicalizationAlgorithm","cipherAlgorithm":"sec:cipherAlgorithm","cipherData":"sec:cipherData","cipherKey":"sec:cipherKey","created":{"@id":"dc:created","@type":"xsd:dateTime"},"creator":{"@id":"dc:creator","@type":"@id"},"digestAlgorithm":"sec:digestAlgorithm","digestValue":"sec:digestValue","domain":"sec:domain","encryptionKey":"sec:encryptionKey","expiration":{"@id":"sec:expiration","@type":"xsd:dateTime"},"expires":{"@id":"sec:expiration","@type":"xsd:dateTime"},"initializationVector":"sec:initializationVector","iterationCount":"sec:iterationCount","nonce":"sec:nonce","normalizationAlgorithm":"sec:normalizationAlgorithm","owner":{"@id":"sec:owner","@type":"@id"},"password":"sec:password","privateKey":{"@id":"sec:privateKey","@type":"@id"},"privateKeyPem":"sec:privateKeyPem","publicKey":{"@id":"sec:publicKey","@type":"@id"},"publicKeyBase58":"sec:publicKeyBase58","publicKeyPem":"sec:publicKeyPem","publicKeyWif":"sec:publicKeyWif","publicKeyService":{"@id":"sec:publicKeyService","@type":"@id"},"revoked":{"@id":"sec:revoked","@type":"xsd:dateTime"},"salt":"sec:salt","signature":"sec:signature","signatureAlgorithm":"sec:signingAlgorithm","signatureValue":"sec:signatureValue"}}'
)
_STATIC_LD_CONTEXTS = {
    'https://www.w3.org/ns/activitystreams': _ACTIVITYSTREAMS_CONTEXT,
    'https://w3id.org/security/v1': _SECURITY_V1_CONTEXT,
}


def _static_ld_document_loader(url, options=None):
    """A pyld document loader over the two frozen documents above -- never
    the network. Raises the same `jsonld.JsonLdError` pyld's own loaders
    raise for an unresolvable URL, so a test that accidentally needs a THIRD
    context fails loudly (an unhelpful KeyError would do too, but this stays
    in pyld's own error vocabulary, matching what `normalized_hash`'s callers
    already expect to catch).
    """
    if url not in _STATIC_LD_CONTEXTS:
        raise jsonld.JsonLdError(
            f'no static content for {url!r} -- add it to _STATIC_LD_CONTEXTS '
            f'rather than letting this fall through to the network',
            'jsonld.LoadDocumentError')
    return {'contentType': 'application/ld+json', 'contextUrl': None,
            'documentUrl': url, 'document': _STATIC_LD_CONTEXTS[url]}


@pytest.fixture
def no_network_ld_signing(monkeypatch):
    """Makes `LDSignature.create_signature`/`verify_signature` resolve their
    two `@context` URLs from the frozen local copies above instead of the
    real internet, for the duration of one test, restoring whatever loader
    pyld had beforehand afterwards -- a `jsonld.set_document_loader` override,
    the same configuration point `app/main/routes.py:744`'s dead demo code
    already uses (there, to point pyld AT the network on purpose). This is
    NOT a patch of `LDSignature.verify_signature` or `HttpSignature.
    verify_request` -- neither forbidden name is touched, and the
    normalization/signature math both still run as production wrote them;
    only where the two context DOCUMENTS come from changes.

    Yields the list of URLs actually resolved through the static loader, so a
    test can assert it was genuinely exercised (`{activitystreams,
    security-v1}`, per the module docstring's "Verified to add no false
    confidence" section) rather than merely not having failed.

    Also monkeypatches `requests.get` to raise `AssertionError` if called at
    all -- `pyld.documentloader.requests.requests_document_loader`'s inner
    loader (pyld's DEFAULT, unpatched here) is the only place in this
    dependency chain that reaches the network, and it does so with exactly
    that call (confirmed by reading its source). With the static loader
    installed it should never run, so this is a hard failure if it somehow
    does, rather than a silent real network request passing unnoticed.
    """
    resolved = []

    def _recording_loader(url, options=None):
        resolved.append(url)
        return _static_ld_document_loader(url, options)

    previous_loader = jsonld.get_document_loader()
    jsonld.set_document_loader(_recording_loader)

    def _network_forbidden(*args, **kwargs):
        raise AssertionError(
            f'requests.get was called during a test using no_network_ld_signing '
            f'-- the static document loader should have intercepted every '
            f'jsonld.normalize context resolution before this call site '
            f'(pyld.documentloader.requests.requests_document_loader) could ever '
            f'be reached; args={args!r} kwargs={kwargs!r}')

    monkeypatch.setattr('requests.get', _network_forbidden)
    try:
        yield resolved
    finally:
        jsonld.set_document_loader(previous_loader)


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


def _unsigned_but_precheck_clean_headers(body_bytes: bytes) -> dict:
    """Digest and Date headers computed the same way signed_inbox_post
    computes them internally, for a request built by hand with NO Signature
    header -- the shape every test below this needs, so it is factored out
    rather than repeated three times (mirroring test_a_delete_of_an_unknown_
    account_needs_no_signature and test_an_unresolvable_actor_is_refused,
    tests/test_inbox_gate_refusals.py, which do the same by hand inline).
    """
    return {'Digest': HttpSignature.calculate_digest(body_bytes), 'Date': http_date()}


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
    activity = inbox_activity(signing_peer)
    activity['@context'] = default_context()
    key_id = f'{signing_peer.ap_profile_id}#main-key'
    wrong_private_key, _wrong_public_key = RsaKeys.generate_keypair()
    activity['signature'] = LDSignature.create_signature(activity, wrong_private_key, key_id)
    body_bytes = json.dumps(activity).encode('utf8')

    with app.test_client() as client:
        response = client.post('/inbox', data=body_bytes,
                               headers=_unsigned_but_precheck_clean_headers(body_bytes),
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
    activity = inbox_activity(signing_peer)
    activity['@context'] = default_context()
    key_id = f'{signing_peer.ap_profile_id}#main-key'
    activity['signature'] = LDSignature.create_signature(activity, signing_peer.private_key, key_id)
    body_bytes = json.dumps(activity).encode('utf8')

    with app.test_client() as client:
        response = client.post('/inbox', data=body_bytes,
                               headers=_unsigned_but_precheck_clean_headers(body_bytes),
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
                               headers=_unsigned_but_precheck_clean_headers(body_bytes),
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
                               headers=_unsigned_but_precheck_clean_headers(body_bytes),
                               content_type='application/activity+json')

    assert response.status_code == 400
    assert ActivityPubLog.query.one().exception_message == 'Could not verify HTTP signature: No signature header present'
