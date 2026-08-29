"""shared_inbox's SUCCESS path (app/activitypub/routes.py:741-763), the three
route aliases that all resolve to it, and the separate `replay_inbox_request`
function (routes.py:781-835). Tasks 2-5 covered every refusal and Task 6
covered signature verification; every test below is on the far side of a
successful signature check (or an exempted equivalent) or bypasses signature
checking entirely, as `replay_inbox_request` itself does.

STEP 1/2 -- the shared instance bookkeeping, and whether `ip_address` blanking
is reachable
===============================================================================
routes.py:741-747, reached once `actor` is resolved and the `bounced`
variable is settled (False on a straight HttpSignature pass, True on any path
through the `except VerificationError` block, per Task 6's module docstring):

    if actor.instance_id:
        actor.instance.last_seen = utcnow()
        actor.instance.dormant = False
        actor.instance.gone_forever = False
        actor.instance.failures = 0
        actor.instance.ip_address = ip_address() if not bounced else ''
    db.session.commit()

This is unconditional bookkeeping on every surviving request, not just the
"normal" one -- it runs identically whether the request arrived through a
straight HTTP-signature pass, the LD-signature fallback, or the fediseer
exemption. The `ip_address` line is the one field that depends on `bounced`,
so it is the one arm that needs a SEPARATE reachability check from the other
four fields.

Reachability of the `ip_address`-blanked arm, checked rather than assumed:
Task 6 already proved `bounced` reaches True while the request still succeeds
-- twice, independently -- via the LD-signature fallback
(test_a_valid_ld_signature_is_accepted) and the fediseer exemption
(test_an_unsigned_fediseer_chat_message_is_exempted), both in
tests/test_inbox_gate_signatures.py, both asserting `instance.ip_address ==
''` already. So this arm is NOT unreachable, and the brief's suggestion that
it "may be blocked" does not hold: Task 6's own work already reached it twice.
What Task 6's tests do NOT do is the thing Task 7 asks for specifically --
start from the OPPOSITE state (a non-blank sentinel `ip_address`) and show the
gate overwrites it to '' rather than merely observing a fresh row's blank
default. test_ld_signature_fallback_blanks_a_populated_ip_address below does
exactly that, reusing the `no_network_ld_signing` fixture Task 6 built and a
whole-branch review then moved into tests/conftest.py alongside the
`unsigned_but_precheck_clean_headers` and `ld_signed_body` recipes this file
used to keep its own copies of (see
its docstring in tests/test_inbox_gate_signatures.py for why the fixture
exists and how it is verified to touch no real network).

STEP 3/4 -- the two dispatch targets, and the DEBUG split
===============================================================================
routes.py:751-763:

    if account_deletion == True:
        if current_app.debug:
            process_delete_request(request_json, store_ap_json)
        else:
            process_delete_request.delay(request_json, store_ap_json)
        return ''

    if current_app.debug:
        process_inbox_request(request_json, store_ap_json)
    else:
        process_inbox_request.delay(request_json, store_ap_json)
    return ''

Two independent axes: WHICH function runs (`account_deletion`, set at
routes.py:695-697 exactly for a `Delete` whose `object` equals its own
`actor`, for an actor that already exists here) and HOW it runs (direct call
under `current_app.debug`, `.delay()` otherwise). The `Recorder` class below
is the same shape as sub-project 3's `Recorder` (tests/test_ap_resolve_from_
search.py) for the same reason: under this suite's eager Celery,
`task_always_eager=True` + `task_eager_propagates=True` means `.delay()` runs
the task INLINE and propagates its exceptions exactly as the direct call
does. That was measured in sub-project 3, not re-measured here -- the two
modes are NOT behaviourally different in this suite, so a test that tried to
tell them apart by effect would be pinning eager Celery, not this branch.
Recording which attribute was invoked is the honest way to pin the branch
that determines it.

STEP 5 -- the three route aliases
===============================================================================
routes.py:766-778: `site_inbox`, `user_inbox`, `community_inbox` are three
bare `return shared_inbox()` wrappers bound to `/site_inbox`, `/u/<actor>/
inbox`, `/c/<actor>/inbox`. The `<actor>` path segment is never read by
either the wrapper or `shared_inbox` (which re-derives its actor from the
JSON body, not the URL) -- it exists only so the alias is a distinct path a
signature's "(request-target)" line can be computed against.
`signed_inbox_post`'s `path=` parameter was built for exactly this (its own
docstring: "`body` overrides the bytes... `host` overrides the Host header
for the same reason, one field over" -- `path` is the third such override,
already wired through to both the signing call and the POST).

STEP 6 -- `replay_inbox_request`'s own branch table, derived from routes.py:
781-835
===============================================================================
This is a SEPARATE function, not a wrapper around `shared_inbox`, and it
diverges from it in ways worth stating plainly rather than assuming symmetry:

  - It takes an already-parsed `request_json` dict directly -- no body
    parsing, no Content-Length/Digest concerns, no `precheck`.
  - It performs NO signature check of any kind (no `HttpSignature.
    verify_request`, no LD fallback, no fediseer exemption) and touches
    neither `redis_client` (no pause-federation check, no duplicate-id
    suppression) nor `g.site` / `Site.query`.
  - It has an ACTIVE `actor.is_local()` refusal (routes.py:824-826) that
    `shared_inbox` does not -- `shared_inbox`'s equivalent check
    (routes.py:710-712) is commented out. So "activity from a local actor"
    is refused here but NOT in `shared_inbox`; this is not an oversight in
    either function, just a real behavioural difference between the live
    gate and its replay path.
  - It never calls `.delay()` anywhere. Both `process_delete_request(
    request_json, True)` and `process_inbox_request(request_json, True)` are
    called directly, unconditionally, with `store_ap_json` hardcoded to
    `True` regardless of `current_app.debug`. **There is no DEBUG split to
    cover in this function** -- Step 6 does NOT mirror Steps 3/4's axis at
    all, and a test that tried to exercise `replay_inbox_request` under both
    DEBUG settings looking for a difference would find none, because the
    source has no branch on `current_app.debug` anywhere in this function.
  - No instance bookkeeping (no `last_seen`/`dormant`/`gone_forever`/
    `failures`/`ip_address` writes) -- that block lives only in
    `shared_inbox`.
  - Every early return is a bare `return` (no HTTP response, no status code)
    -- `replay_inbox_request` is called for its side effects only (logging a
    row, or dispatching), matching that its only two call sites (grep
    confirms; not shown here) pass it a stored JSON body to reprocess rather
    than serve a live request.

Branch table, in source order:

  782-784  Missing one of id/type/actor/object -> log 'REPLAY: Missing
           minimum expected fields in JSON', return. No dispatch.
  787-794  `type == 'Announce'` and `object` is a dict and
           `object_has_missing_fields(object)` is True (the object dict
           itself misses one of id/type/actor/object) ->
             - object has a 'type' key equal to 'Page' or 'Note' -> log
               'REPLAY: Intended for Mastodon' (APLOG_IGNORED), return.
             - anything else -> log 'REPLAY: Missing minimum expected fields
               in JSON Announce object' (APLOG_FAILURE), return.
           No dispatch either way.
  796-799  `type == 'Announce'`, `object` is a dict, NOT missing fields, and
           `object['actor']` is a string starting with
           'https://' + SERVER_NAME -> log 'REPLAY: Activity about local
           content which is already present' (APLOG_DUPLICATE/IGNORED),
           return. No dispatch.
  802-804  Top-level `actor` is a string ending in 'accounts/peertube' ->
           log 'REPLAY: PeerTube View or CacheFile activity'
           (APLOG_IGNORED), return. No dispatch. (Reached for ANY activity
           type, including a plain Like -- the check has no type guard.)
  806-815  `type == 'Delete'`, `object` is a string, and `actor == object`
           (an account self-deletion) -> looked up by
           `User.ap_profile_id == actor.lower()`; if no such row exists ->
           log 'REPLAY: Does not exist here' (APLOG_DELETE/IGNORED), return.
           No dispatch. If the row DOES exist, `account_deletion = True` and
           control falls through to 828-831 below.
  816-817  Any other activity -> `actor = find_actor_or_create_cached(
           request_json['actor'])`.
  819-822  `actor` is falsy (unresolvable, e.g. a 404 on fetch) -> log
           f'REPLAY: Actor could not be found 1: {actor_name}'
           (APLOG_FAILURE), return. No dispatch.
  824-826  `actor.is_local()` is True -> log 'REPLAY: ActivityPub activity
           from a local actor' (APLOG_FAILURE), return. No dispatch. (This
           branch has NO analogue in `shared_inbox` -- see above.)
  828-831  `account_deletion == True` (only reachable via 806-815's row-
           exists case) -> `process_delete_request(request_json, True)`,
           return. No further processing.
  833      Anything else that reached this point -> `process_inbox_request(
           request_json, True)`, return.

Every non-dispatch outcome above is covered by its own test; both dispatch
targets are covered by their own tests too, all asserting on the recorded
call rather than on a return value (there is nothing to assert a return value
against -- every path returns `None`).
"""
import pytest

from app import db
from app.activitypub.routes import replay_inbox_request
from app.models import ActivityPubLog
from app.utils import utcnow
from tests.conftest import ld_signed_body, unsigned_but_precheck_clean_headers
from tests.factories import inbox_activity, make_instance, make_site, make_user, signed_inbox_post

pytestmark = pytest.mark.usefixtures('redis_double')


# Why the DEBUG=False dispatch tests below build their bodies with
# `ld_signed_body` (tests/conftest.py) rather than with `signed_inbox_post`
# ---------------------------------------------------------------------------
# `signed_inbox_post` -> `HttpSignature.signed_request` -> `is_invalid_get_
# request_uri` (app/utils.py:5460-5462) returns False (i.e. "not invalid")
# ONLY when `current_app.debug` is already True at signing time, and treats
# every `.local`-suffixed host -- which `TestConfig.SERVER_NAME`
# ('test.piefed.local') is -- as invalid otherwise, raising
# `ValueError("URI is invalid")` before any request is even built. There is no
# way to use `signed_inbox_post` to build a request while `current_app.debug`
# is False, which is exactly the state a DEBUG=False dispatch test needs the
# SERVER to be in while handling it -- confirmed experimentally: both
# DEBUG=False dispatch tests raised this ValueError the first time they were
# written using `signed_inbox_post`.
#
# LD-signing never calls `HttpSignature.signed_request` (`LDSignature.
# create_signature` and `client.post()` are used directly instead, exactly as
# `test_an_invalid_ld_signature_is_refused` and
# `test_a_valid_ld_signature_is_accepted` in tests/test_inbox_gate_
# signatures.py already do), so it never reaches `is_invalid_get_request_uri`
# and sidesteps the trap entirely. The only behavioural cost is that `bounced`
# is True on this route rather than False -- irrelevant to what these tests
# assert (which attribute of `process_inbox_request`/`process_delete_request`
# was invoked), since the DEBUG branch is read after `bounced` is settled.


class Recorder:
    """Stands in for process_inbox_request / process_delete_request and
    records HOW it was called -- inline, or through .delay -- rather than
    trying to observe an effect the two modes do not actually differ in.

    Under this suite's eager Celery (task_always_eager=True,
    task_eager_propagates=True), `.delay()` runs the task INLINE and
    propagates its exceptions exactly as calling it directly does -- measured
    in sub-project 3 (tests/test_ap_resolve_from_search.py's own `Recorder`),
    not re-measured here. So `if current_app.debug: f(...) else: f.delay(...)`
    is behaviourally inert in this suite; this class pins WHICH attribute
    routes.py actually calls, which is the only thing left to pin once the
    two modes are known to behave identically.
    """

    def __init__(self):
        self.inline = []
        self.delayed = []

    def __call__(self, *args, **kwargs):
        self.inline.append((args, kwargs))

    def delay(self, *args, **kwargs):
        self.delayed.append((args, kwargs))


# ---------------------------------------------------------------------------
# Step 1/2 -- instance bookkeeping, and the ip_address-blanking arm
# ---------------------------------------------------------------------------

def test_a_successful_delivery_updates_instance_bookkeeping(app, signing_peer, monkeypatch):
    """routes.py:741-747: every field is set to the OPPOSITE of what a
    success should leave it as, so a passing assertion actually shows the
    gate WROTE these fields rather than merely observing a row that already
    happened to be correct (fresh Instance rows already default to
    dormant=False/gone_forever=False/failures=0 -- a test that didn't invert
    them first could pass on a gate that touched nothing at all).

    A genuinely HTTP-signature-verified request (bounced stays False) is used
    so `ip_address` is set to the real value rather than blanked -- the
    blanking arm has its own test right below, which starts this same field
    from a populated sentinel instead of the pre-request unset state so it
    can show the gate OVERWRITES a real address with '', not merely that a
    fresh row's ip_address remains unset.
    """
    monkeypatch.setitem(app.config, 'DEBUG', True)
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request',
                        lambda *args, **kwargs: None)
    instance = signing_peer.instance
    instance.last_seen = utcnow().replace(year=2000)
    instance.dormant = True
    instance.gone_forever = True
    instance.failures = 3
    instance.ip_address = 'stale-sentinel'
    db.session.commit()
    before = utcnow()

    with app.test_client() as client:
        response = signed_inbox_post(client, inbox_activity(signing_peer), signing_peer)

    assert response.status_code == 200
    db.session.refresh(instance)
    assert instance.last_seen >= before
    assert instance.dormant is False
    assert instance.gone_forever is False
    assert instance.failures == 0
    # '127.0.0.1' is Werkzeug's test-client default REMOTE_ADDR (confirmed by a
    # throwaway probe against this exact request shape, not assumed) -- the
    # value `ip_address()` (app/utils.py's get_ip_address) resolves to absent
    # any X-Forwarded-For/TRUSTED_CLIENT_IP_HEADER override.
    assert instance.ip_address == '127.0.0.1'


def test_ld_signature_fallback_blanks_a_populated_ip_address(app, signing_peer, monkeypatch, no_network_ld_signing):
    """routes.py:717-731/746: the LD-signature fallback sets `bounced = True`
    at the top of the `except VerificationError` block (line 718)
    unconditionally, before either fallback is even attempted -- so a
    request that succeeds ONLY via the LD path still has `bounced` True by
    the time the bookkeeping block runs, and `ip_address` is blanked to ''
    rather than set to the real address.

    Task 6's own test_a_valid_ld_signature_is_accepted (tests/
    test_inbox_gate_signatures.py) already asserts `instance.ip_address ==
    ''`, which proves the arm is reachable -- contrary to the brief's
    "may be blocked" hedge, which does not hold here: Task 6's own work
    already produced a valid LD signature and exercised this exact line.
    What that test does not do, and this one adds, is start `ip_address`
    from a POPULATED sentinel rather than a fresh row's unset default, so
    passing here shows the gate genuinely overwrites a real value with ''
    rather than merely leaving an already-blank field alone.

    Built the same way Task 6 built it: `LDSignature.create_signature`
    (never `verify_signature`, which stays unpatched) signs with
    `signing_peer`'s own private key, so `LDSignature.verify_signature`
    genuinely succeeds against the actor's real public key.
    `no_network_ld_signing` keeps the JSON-LD context resolution this needs
    off the real internet -- see its docstring and Task 6's module docstring
    for how that is verified to have actually run rather than merely not
    failed.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    monkeypatch.setitem(app.config, 'DEBUG', True)
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request',
                        lambda *args, **kwargs: None)
    signing_peer.instance.ip_address = 'stale-sentinel'
    db.session.commit()

    body_bytes = ld_signed_body(signing_peer)

    with app.test_client() as client:
        response = client.post('/inbox', data=body_bytes,
                               headers=unsigned_but_precheck_clean_headers(body_bytes),
                               content_type='application/activity+json')

    assert response.status_code == 200
    assert set(no_network_ld_signing) == {'https://www.w3.org/ns/activitystreams', 'https://w3id.org/security/v1'}
    db.session.refresh(signing_peer.instance)
    assert signing_peer.instance.ip_address == ''


# ---------------------------------------------------------------------------
# Step 3/4 -- the two dispatch targets, and the DEBUG split
# ---------------------------------------------------------------------------

def _patch_dispatch_recorders(monkeypatch):
    inbox_recorder = Recorder()
    delete_recorder = Recorder()
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request', inbox_recorder)
    monkeypatch.setattr('app.activitypub.routes.process_delete_request', delete_recorder)
    return inbox_recorder, delete_recorder


def test_a_non_delete_activity_dispatches_process_inbox_request_inline_under_debug(app, signing_peer, monkeypatch):
    """routes.py:758-761, DEBUG branch: an ordinary (non-self-delete) activity
    calls `process_inbox_request` directly, and `process_delete_request` is
    never touched. `current_app.debug=True` selects the direct-call arm.
    """
    monkeypatch.setitem(app.config, 'DEBUG', True)
    inbox_recorder, delete_recorder = _patch_dispatch_recorders(monkeypatch)

    with app.test_client() as client:
        response = signed_inbox_post(client, inbox_activity(signing_peer), signing_peer)

    assert response.status_code == 200
    assert len(inbox_recorder.inline) == 1
    assert inbox_recorder.delayed == []
    assert delete_recorder.inline == [] and delete_recorder.delayed == []


def test_a_non_delete_activity_dispatches_process_inbox_request_via_delay_when_debug_is_off(app, signing_peer, monkeypatch, no_network_ld_signing):
    """routes.py:758-761, non-DEBUG branch: the same activity, with
    `current_app.debug=False` (TestConfig's default -- not set anywhere), now
    calls `.delay()` instead of the function directly. Per the module
    docstring, this is not a behavioural difference under eager Celery --
    only which attribute is invoked differs, which is exactly what
    `Recorder` pins.

    Sent via `ld_signed_body` rather than `signed_inbox_post` -- see the
    comment above `Recorder` in this file for why `signed_inbox_post` cannot
    be used to reach `shared_inbox` while `current_app.debug` is False at all
    in this suite.
    """
    inbox_recorder, delete_recorder = _patch_dispatch_recorders(monkeypatch)
    body_bytes = ld_signed_body(signing_peer)

    with app.test_client() as client:
        response = client.post('/inbox', data=body_bytes,
                               headers=unsigned_but_precheck_clean_headers(body_bytes),
                               content_type='application/activity+json')

    assert response.status_code == 200
    assert len(inbox_recorder.delayed) == 1
    assert inbox_recorder.inline == []
    assert delete_recorder.inline == [] and delete_recorder.delayed == []
    assert set(no_network_ld_signing) == {'https://www.w3.org/ns/activitystreams', 'https://w3id.org/security/v1'}


def test_a_self_delete_of_an_existing_actor_dispatches_process_delete_request_inline_under_debug(app, signing_peer, monkeypatch):
    """routes.py:695-697/751-756: a `Delete` whose `object` equals its own
    `actor`, sent by an actor that already has a row here, sets
    `account_deletion = True` and dispatches `process_delete_request`
    instead of `process_inbox_request` -- the two targets are mutually
    exclusive, asserted here by checking BOTH recorders rather than only the
    one expected to fire (a production change that dispatched both would
    still pass a test that checked only one).
    """
    monkeypatch.setitem(app.config, 'DEBUG', True)
    inbox_recorder, delete_recorder = _patch_dispatch_recorders(monkeypatch)
    activity = inbox_activity(signing_peer, activity_type='Delete', object_uri=signing_peer.ap_profile_id)

    with app.test_client() as client:
        response = signed_inbox_post(client, activity, signing_peer)

    assert response.status_code == 200
    assert len(delete_recorder.inline) == 1
    assert delete_recorder.delayed == []
    assert inbox_recorder.inline == [] and inbox_recorder.delayed == []


def test_a_self_delete_of_an_existing_actor_dispatches_process_delete_request_via_delay_when_debug_is_off(app, signing_peer, monkeypatch, no_network_ld_signing):
    """routes.py:751-756, non-DEBUG branch: same self-delete shape as above,
    with DEBUG left at its default False, so `.delay()` is the call made.

    Sent via `ld_signed_body`, for the same reason the sibling test above
    uses it instead of `signed_inbox_post`.
    """
    inbox_recorder, delete_recorder = _patch_dispatch_recorders(monkeypatch)
    body_bytes = ld_signed_body(signing_peer, activity_type='Delete', object_uri=signing_peer.ap_profile_id)

    with app.test_client() as client:
        response = client.post('/inbox', data=body_bytes,
                               headers=unsigned_but_precheck_clean_headers(body_bytes),
                               content_type='application/activity+json')

    assert response.status_code == 200
    assert len(delete_recorder.delayed) == 1
    assert delete_recorder.inline == []
    assert inbox_recorder.inline == [] and inbox_recorder.delayed == []
    assert set(no_network_ld_signing) == {'https://www.w3.org/ns/activitystreams', 'https://w3id.org/security/v1'}


# ---------------------------------------------------------------------------
# Step 5 -- the three route aliases
# ---------------------------------------------------------------------------

def test_site_inbox_alias_dispatches_the_gate(app, signing_peer, monkeypatch):
    """routes.py:766-768: `/site_inbox` is a bare `return shared_inbox()` --
    this is the only thing pinning that the route is wired up at all, since a
    typo in the decorator or a route that returned something else entirely
    would still be "a route that exists" without this test.
    """
    monkeypatch.setitem(app.config, 'DEBUG', True)
    dispatched = []
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request',
                        lambda *args, **kwargs: dispatched.append(args))

    with app.test_client() as client:
        response = signed_inbox_post(client, inbox_activity(signing_peer), signing_peer, path='/site_inbox')

    assert response.status_code == 200
    assert len(dispatched) == 1


def test_user_inbox_alias_dispatches_the_gate(app, signing_peer, monkeypatch):
    """routes.py:771-773: `/u/<actor>/inbox` -- the `<actor>` path segment is
    never read by `shared_inbox` (it re-derives the actor from the JSON
    body), so an arbitrary value ('alice') is enough to exercise the route.
    """
    monkeypatch.setitem(app.config, 'DEBUG', True)
    dispatched = []
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request',
                        lambda *args, **kwargs: dispatched.append(args))

    with app.test_client() as client:
        response = signed_inbox_post(client, inbox_activity(signing_peer), signing_peer, path='/u/alice/inbox')

    assert response.status_code == 200
    assert len(dispatched) == 1


def test_community_inbox_alias_dispatches_the_gate(app, signing_peer, monkeypatch):
    """routes.py:776-778: `/c/<actor>/inbox`, the community-scoped mirror of
    the user alias above.
    """
    monkeypatch.setitem(app.config, 'DEBUG', True)
    dispatched = []
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request',
                        lambda *args, **kwargs: dispatched.append(args))

    with app.test_client() as client:
        response = signed_inbox_post(client, inbox_activity(signing_peer), signing_peer, path='/c/general/inbox')

    assert response.status_code == 200
    assert len(dispatched) == 1


# ---------------------------------------------------------------------------
# Step 6 -- replay_inbox_request
# ---------------------------------------------------------------------------

def test_replay_missing_fields_is_ignored_without_dispatch(app, db_session, monkeypatch):
    """routes.py:782-784: any of id/type/actor/object missing -> logged,
    no dispatch. `object` is the one left out here.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    inbox_recorder, delete_recorder = _patch_dispatch_recorders(monkeypatch)

    replay_inbox_request({'id': 'https://peer.example/activities/1', 'type': 'Like',
                          'actor': 'https://peer.example/users/alice'})

    assert ActivityPubLog.query.one().exception_message == 'REPLAY: Missing minimum expected fields in JSON'
    assert inbox_recorder.inline == [] and delete_recorder.inline == []


def test_replay_announce_of_a_mastodon_shaped_object_is_ignored(app, db_session, monkeypatch):
    """routes.py:787-794: an Announce'd object missing required fields, typed
    'Page' -> logged as "Intended for Mastodon" rather than as a failure.
    The inner object is missing 'actor', which is what makes
    object_has_missing_fields True in the first place.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    inbox_recorder, delete_recorder = _patch_dispatch_recorders(monkeypatch)
    request_json = {
        'id': 'https://peer.example/activities/1', 'type': 'Announce',
        'actor': 'https://peer.example/users/alice',
        'object': {'id': 'https://peer.example/objects/1', 'type': 'Page'},
    }

    replay_inbox_request(request_json)

    assert ActivityPubLog.query.one().exception_message == 'REPLAY: Intended for Mastodon'
    assert inbox_recorder.inline == [] and delete_recorder.inline == []


def test_replay_announce_of_a_non_mastodon_shaped_object_is_a_failure(app, db_session, monkeypatch):
    """routes.py:787-794: the same missing-fields Announce object, but typed
    something other than Page/Note -- lands on the generic failure message
    instead of the Mastodon-specific ignore.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    inbox_recorder, delete_recorder = _patch_dispatch_recorders(monkeypatch)
    request_json = {
        'id': 'https://peer.example/activities/1', 'type': 'Announce',
        'actor': 'https://peer.example/users/alice',
        'object': {'id': 'https://peer.example/objects/1', 'type': 'Video'},
    }

    replay_inbox_request(request_json)

    assert (ActivityPubLog.query.one().exception_message ==
            'REPLAY: Missing minimum expected fields in JSON Announce object')
    assert inbox_recorder.inline == [] and delete_recorder.inline == []


def test_replay_announce_of_already_present_local_content_is_ignored(app, db_session, monkeypatch):
    """routes.py:796-799: an Announce whose (complete, not-missing-fields)
    object's own 'actor' starts with this server's own URL -- i.e. the
    announced content originated here -- is ignored as a duplicate. The
    inner object carries all four of id/type/actor/object so
    object_has_missing_fields is False, which is what routes this to the
    "already present" branch instead of the missing-fields branch above.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    inbox_recorder, delete_recorder = _patch_dispatch_recorders(monkeypatch)
    local_url = f"https://{app.config['SERVER_NAME']}/u/localuser"
    request_json = {
        'id': 'https://peer.example/activities/1', 'type': 'Announce',
        'actor': 'https://peer.example/users/alice',
        'object': {'id': f'{local_url}/objects/1', 'type': 'Note', 'actor': local_url,
                   'object': f'{local_url}/objects/1'},
    }

    replay_inbox_request(request_json)

    assert (ActivityPubLog.query.one().exception_message ==
            'REPLAY: Activity about local content which is already present')
    assert inbox_recorder.inline == [] and delete_recorder.inline == []


def test_replay_peertube_view_activity_is_ignored(app, db_session, monkeypatch):
    """routes.py:802-804: an actor URI ending in 'accounts/peertube' is
    ignored regardless of activity type -- the check has no type guard.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    inbox_recorder, delete_recorder = _patch_dispatch_recorders(monkeypatch)
    request_json = {
        'id': 'https://peer.example/activities/1', 'type': 'View',
        'actor': 'https://peer.example/accounts/peertube',
        'object': 'https://peer.example/objects/1',
    }

    replay_inbox_request(request_json)

    assert ActivityPubLog.query.one().exception_message == 'REPLAY: PeerTube View or CacheFile activity'
    assert inbox_recorder.inline == [] and delete_recorder.inline == []


def test_replay_delete_of_an_unknown_account_is_ignored(app, db_session, monkeypatch):
    """routes.py:806-815: a self-referential Delete (actor == object) for an
    ap_profile_id with no matching User row is ignored rather than
    dispatched -- mirrors shared_inbox's equivalent shortcut
    (test_a_delete_of_an_unknown_account_needs_no_signature,
    tests/test_inbox_gate_refusals.py), which this codepath was clearly
    written to match.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    inbox_recorder, delete_recorder = _patch_dispatch_recorders(monkeypatch)
    ghost = 'https://peer.example/users/ghost'
    request_json = {'id': f'{ghost}/activities/1', 'type': 'Delete', 'actor': ghost, 'object': ghost}

    replay_inbox_request(request_json)

    assert ActivityPubLog.query.one().exception_message == 'REPLAY: Does not exist here'
    assert inbox_recorder.inline == [] and delete_recorder.inline == []


def test_replay_an_unresolvable_actor_is_a_failure(app, db_session, monkeypatch, http_mock):
    """routes.py:816-822: for anything that is not the Delete-of-unknown-
    account shortcut, the actor is resolved through
    `find_actor_or_create_cached`; a 404 on the one fetch that resolution
    makes (CACHE_TYPE=NullCache means it is never a cache hit) leaves `actor`
    None, logged and refused without dispatch. Mirrors shared_inbox's own
    test_an_unresolvable_actor_is_refused (tests/test_inbox_gate_refusals.py)
    for the same reason.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    inbox_recorder, delete_recorder = _patch_dispatch_recorders(monkeypatch)
    ghost = 'https://ghost.example/u/nobody'
    http_mock.get(ghost).respond(404)
    request_json = {'id': f'{ghost}/activities/1', 'type': 'Like', 'actor': ghost,
                    'object': f'{ghost}/objects/1'}

    replay_inbox_request(request_json)

    assert ActivityPubLog.query.one().exception_message == f'REPLAY: Actor could not be found 1: {ghost}'
    assert inbox_recorder.inline == [] and delete_recorder.inline == []


def test_replay_refuses_activity_from_a_local_actor(app, db_session, monkeypatch):
    """routes.py:824-826: `actor.is_local()` True refuses the activity. This
    branch has NO analogue in `shared_inbox` -- its equivalent check
    (routes.py:710-712) is commented out there -- so this is a genuine
    behavioural difference between the live gate and its replay path, not a
    restatement of anything Task 2-6 already covered.

    The local actor is built directly rather than via `make_user(...,
    local=True)` alone: that factory leaves `ap_profile_id` None, which
    `find_remote_actor` (app/activitypub/actor.py) can never match against a
    URL string, so the row would never be found by
    `find_actor_or_create_cached` at all. Setting `ap_profile_id` to a
    '/u/'-shaped URL under this server's own domain makes the DB lookup
    succeed while `ap_id is None` still makes `is_local()` True (User.
    is_local, app/models.py:1242-1243: `self.ap_id is None or ...`) --
    `schedule_actor_refresh` (app/activitypub/actor.py:118) explicitly skips
    any local actor, so no HTTP fetch is attempted for this row either.
    """
    make_site()
    instance = make_instance(app.config['SERVER_NAME'], software='piefed')
    local_actor = make_user(instance, 'localactor', local=True)
    local_actor.ap_profile_id = f"https://{app.config['SERVER_NAME']}/u/localactor"
    db.session.commit()
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    inbox_recorder, delete_recorder = _patch_dispatch_recorders(monkeypatch)
    request_json = {
        'id': f'{local_actor.ap_profile_id}/activities/1', 'type': 'Like',
        'actor': local_actor.ap_profile_id, 'object': f'{local_actor.ap_profile_id}/objects/1',
    }

    replay_inbox_request(request_json)

    assert ActivityPubLog.query.one().exception_message == 'REPLAY: ActivityPub activity from a local actor'
    assert inbox_recorder.inline == [] and delete_recorder.inline == []


def test_replay_dispatches_process_delete_request_for_an_existing_actors_self_delete(app, signing_peer, monkeypatch):
    """routes.py:828-831: a self-delete for an actor that DOES already exist
    here falls through the 806-815 lookup with `account_deletion = True` and
    dispatches `process_delete_request(request_json, True)` -- called
    DIRECTLY, never `.delay()`'d (see the module docstring's Step 6 section:
    this function has no DEBUG branch at all), with `store_ap_json`
    hardcoded True regardless of `app.config['DEBUG']`.
    """
    inbox_recorder, delete_recorder = _patch_dispatch_recorders(monkeypatch)
    request_json = inbox_activity(signing_peer, activity_type='Delete', object_uri=signing_peer.ap_profile_id)

    replay_inbox_request(request_json)

    assert delete_recorder.inline == [((request_json, True), {})]
    assert delete_recorder.delayed == []
    assert inbox_recorder.inline == [] and inbox_recorder.delayed == []


def test_replay_dispatches_process_inbox_request_for_everything_else(app, signing_peer, monkeypatch):
    """routes.py:833: a resolvable, non-local, non-self-delete actor's
    activity dispatches `process_inbox_request(request_json, True)`
    directly -- same "no `.delay()`, `store_ap_json` hardcoded True" shape as
    the delete branch above.
    """
    inbox_recorder, delete_recorder = _patch_dispatch_recorders(monkeypatch)
    request_json = inbox_activity(signing_peer)

    replay_inbox_request(request_json)

    assert inbox_recorder.inline == [((request_json, True), {})]
    assert inbox_recorder.delayed == []
    assert delete_recorder.inline == [] and delete_recorder.delayed == []
